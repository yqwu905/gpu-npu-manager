import json
import os
import sys

import numpy as np
import pytest
from PIL import Image

import evaluate

skimage_metrics = pytest.importorskip("skimage.metrics")


def random_pair(seed, shape=(37, 53, 3)):
    rng = np.random.default_rng(seed)
    ref = rng.integers(0, 256, size=shape).astype(np.float64)
    pred = np.clip(ref + rng.normal(0, 20, size=shape), 0, 255).round()
    return pred, ref


@pytest.mark.parametrize("seed", [0, 1, 2])
def test_ssim_matches_skimage(seed):
    pred, ref = random_pair(seed)
    expected = skimage_metrics.structural_similarity(
        pred, ref, gaussian_weights=True, sigma=1.5, use_sample_covariance=False, data_range=255, channel_axis=-1
    )
    assert evaluate.ssim(pred, ref) == pytest.approx(expected, abs=1e-9)


def test_psnr_matches_skimage_and_caps():
    pred, ref = random_pair(3)
    expected = skimage_metrics.peak_signal_noise_ratio(ref, pred, data_range=255)
    assert evaluate.psnr(pred, ref) == pytest.approx(expected)
    assert evaluate.psnr(ref, ref) == evaluate.PSNR_CAP


def test_text_metrics():
    assert evaluate.edit_distance("kitten", "sitting") == 3
    assert evaluate.edit_distance("", "abc") == 3
    distance, ref_len, exact, ned = evaluate.text_scores("今天天汽", "今天天气")
    assert (distance, ref_len, exact) == (1, 4, False)
    assert ned == pytest.approx(0.75)
    assert evaluate.text_scores("", "") == (0, 0, True, 1.0)


def test_evaluate_end_to_end(tmp_path):
    images = tmp_path / "images"
    images.mkdir()
    gt_dir = tmp_path / "gt"
    gt_dir.mkdir()
    pred, ref = random_pair(4)
    Image.fromarray(pred.astype(np.uint8)).save(images / "a.png")
    Image.fromarray(ref.astype(np.uint8)).save(gt_dir / "a.png")
    Image.fromarray(ref.astype(np.uint8)).save(images / "b.png")
    Image.fromarray(np.zeros((5, 5, 3), np.uint8)).save(images / "c.png")
    lines = [
        {"id": "a", "image": "images/a.png", "text": "hello", "ref_text": "hello"},
        {"id": "b", "image": "images/b.png", "text": "helo", "ref_text": "hello"},
        {"id": "c", "image": "images/c.png", "text": "x"},
    ]
    (tmp_path / "predictions.jsonl").write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    # 参考图放在单独的 jsonl 中，按 id 合并
    refs = [{"id": "a", "ref_image": "a.png"}, {"id": "b", "ref_image": "a.png"}, {"id": "c", "ref_image": "a.png"}]
    (gt_dir / "refs.jsonl").write_text("\n".join(json.dumps(x) for x in refs))

    out = tmp_path / "eval" / "1"
    result = evaluate.evaluate(
        str(tmp_path / "predictions.jsonl"), str(out), ["psnr", "ssim", "lpips", "ocr_a", "cer", "ned"],
        reference_path=str(gt_dir / "refs.jsonl"), log=lambda *_: None,
    )
    m = result["metrics"]
    assert m["psnr"] == pytest.approx((evaluate.psnr(pred, ref) + evaluate.PSNR_CAP) / 2)
    assert result["counts"]["psnr"] == 2  # c 尺寸不一致被跳过
    assert result["num_skipped"] == 1 and result["skipped"][0]["id"] == "c"
    assert m["ocr_a"] == 0.5
    assert m["cer"] == pytest.approx(1 / 10)  # 语料级：1 处错误 / 10 个参考字符
    assert m["ned"] == pytest.approx((1 + 0.8) / 2)
    assert m["lpips"] is None and "lpips" in result["errors"]  # 测试环境没有安装 lpips

    rows = [json.loads(line) for line in (out / "per_sample.jsonl").read_text().splitlines()]
    assert [r["id"] for r in rows] == ["a", "b", "c"]
    assert rows[1]["psnr"] == evaluate.PSNR_CAP and rows[1]["ocr_a"] == 0.0
    assert "psnr" not in rows[2] and "cer" not in rows[2]
    assert json.loads((out / "metrics.json").read_text())["metrics"] == m


def test_ocr_label_reference(tmp_path):
    """文字参考值是 PaddleOCR 格式的标注文件：按图片文件名配对，多个框按阅读顺序拼接。"""
    def box(text, x, y, w=100, h=40, difficult=False):
        return {"transcription": text, "points": [[x, y], [x + w, y], [x + w, y + h], [x, y + h]], "difficult": difficult}

    label = tmp_path / "Label.txt"
    rows = {
        # 第二行的两个框高度略有错开，仍算同一行；difficult 和 ### 的框忽略
        "img_a_INPUT.jpg": [box("P38", 300, 105), box("第二行", 10, 300), box("P18", 100, 100),
                            box("看不清", 10, 500, difficult=True), box("###", 10, 600), box("标题", 10, 10)],
        "img_b_INPUT.jpg": [box("HEFU", 0, 0)],
    }
    label.write_text("\n".join(f"{name}\t{json.dumps(boxes, ensure_ascii=False)}" for name, boxes in rows.items()) + "\n")
    assert evaluate.is_ocr_label_file(str(label))
    assert evaluate.load_ocr_labels(str(label))["img_a_INPUT"]["ref_text"] == "标题\nP18 P38\n第二行"

    preds = [{"id": "out/img_a_INPUT", "text": "标题\nP18 P38\n第二行"}, {"id": "img_b_INPUT", "text": "HEFV"}]
    (tmp_path / "predictions.jsonl").write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in preds) + "\n")
    result = evaluate.evaluate(
        str(tmp_path / "predictions.jsonl"), str(tmp_path / "eval"), ["ocr_a", "cer"],
        reference_path=str(label), log=lambda *_: None,
    )
    assert result["counts"] == {"ocr_a": 2, "cer": 2}
    assert result["metrics"]["ocr_a"] == pytest.approx(0.5)
    assert result["metrics"]["cer"] == pytest.approx(1 / (len("标题\nP18 P38\n第二行") + 4))


class FakePaddleOCR3:
    """模拟 paddleocr 3.x：predict 返回含 rec_texts / rec_polys 的结果。"""

    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def predict(self, path):
        name = os.path.basename(path)
        texts = {"a.png": ["P38", "标题"], "b.png": ["HEFV"]}[name]
        polys = {"a.png": [[[300, 10], [400, 10], [400, 50], [300, 50]], [[10, 300], [90, 300], [90, 340], [10, 340]]],
                 "b.png": [[[0, 0], [100, 0], [100, 40], [0, 40]]]}[name]
        return [{"rec_texts": texts, "rec_polys": [np.array(p) for p in polys]}]


class FakePaddleOCR2:
    """模拟 paddleocr 2.x：ocr 返回 [[框, (文本, 置信度)], ...]，没有文字时为 None。"""

    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def ocr(self, path, cls=True):
        if os.path.basename(path) == "b.png":
            return [None]
        return [[[[[300, 10], [400, 10], [400, 50], [300, 50]], ("P38", 0.9)],
                 [[[10, 300], [90, 300], [90, 340], [10, 340]], ("标题", 0.9)]]]


@pytest.mark.parametrize("engine", [FakePaddleOCR3, FakePaddleOCR2])
def test_ocr_on_predicted_images(tmp_path, monkeypatch, engine):
    """样本只有预测图时，用 PaddleOCR 识别后再和标注比较。"""
    import types

    monkeypatch.setitem(sys.modules, "paddleocr", types.SimpleNamespace(PaddleOCR=engine))
    for name in ("a", "b"):
        Image.fromarray(np.zeros((8, 8, 3), np.uint8)).save(tmp_path / f"{name}.png")
    label = tmp_path / "Label.txt"
    label.write_text(
        'a.jpg\t[{"transcription": "P38", "points": [[300, 12], [400, 12], [400, 52], [300, 52]]},'
        ' {"transcription": "标题", "points": [[10, 300], [90, 300], [90, 340], [10, 340]]}]\n'
        'b.jpg\t[{"transcription": "HEFU", "points": [[0, 0], [100, 0], [100, 40], [0, 40]]}]\n'
    )
    out = tmp_path / "eval"
    result = evaluate.evaluate(str(tmp_path), str(out), ["ocr_a", "cer"], reference_path=str(label),
                               device="cuda:0", log=lambda *_: None)
    assert result["counts"] == {"ocr_a": 2, "cer": 2} and result["metrics"]["ocr_a"] == pytest.approx(0.5)
    ocr_lines = (out / "ocr_results.txt").read_text().splitlines()
    assert ocr_lines[0].startswith("a.png\t") and json.loads(ocr_lines[0].split("\t")[1])[1]["transcription"] == "标题"


def test_ocr_unavailable(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "paddleocr", None)  # import 失败
    Image.fromarray(np.zeros((8, 8, 3), np.uint8)).save(tmp_path / "a.png")
    (tmp_path / "Label.txt").write_text('a.jpg\t[{"transcription": "x", "points": [[0, 0], [1, 1]]}]\n')
    result = evaluate.evaluate(str(tmp_path), str(tmp_path / "eval"), ["cer"],
                               reference_path=str(tmp_path / "Label.txt"), log=lambda *_: None)
    assert result["metrics"]["cer"] is None and "paddleocr" in result["errors"]["cer"]

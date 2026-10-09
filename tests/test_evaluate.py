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


@pytest.mark.parametrize("seed", [0, 1])
def test_torch_metrics_match_numpy(seed):
    pytest.importorskip("torch")
    pred, ref = random_pair(seed)
    values = evaluate.TorchImageMetrics("cpu")(pred, ref, ["psnr", "ssim"])
    assert values["ssim"] == pytest.approx(evaluate.ssim(pred, ref), abs=1e-5)
    assert values["psnr"] == pytest.approx(evaluate.psnr(pred, ref), abs=1e-4)
    assert evaluate.TorchImageMetrics("cpu")(ref, ref, ["psnr"]) == {"psnr": evaluate.PSNR_CAP}


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
        FakePaddleOCR3.last_kwargs = kwargs

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
    """参考文本不是来自标注文件、样本只有预测图时，用 PaddleOCR 对整图检测和识别后再比较。"""
    import types

    monkeypatch.setitem(sys.modules, "paddleocr", types.SimpleNamespace(PaddleOCR=engine))
    for name in ("a", "b"):
        Image.fromarray(np.zeros((8, 8, 3), np.uint8)).save(tmp_path / f"{name}.png")
    preds = [{"id": "a", "image": "a.png", "ref_text": "P38\n标题"}, {"id": "b", "image": "b.png", "ref_text": "HEFU"}]
    (tmp_path / "predictions.jsonl").write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in preds) + "\n")
    out = tmp_path / "eval"
    logs = []
    result = evaluate.evaluate(str(tmp_path / "predictions.jsonl"), str(out), ["ocr_a", "cer"],
                               device="cuda:0", log=logs.append)
    assert logs[0] == "共 2 个样本，指标 ocr_a,cer，设备 cuda:0" and logs[-1].startswith("已处理 2/2")
    if engine is FakePaddleOCR3:
        # 评测不需要文档方向分类和去扭曲，3.x 默认开启
        assert FakePaddleOCR3.last_kwargs["device"] == "gpu:0"
        assert FakePaddleOCR3.last_kwargs["use_doc_unwarping"] is False
        assert FakePaddleOCR3.last_kwargs["use_doc_orientation_classify"] is False
    assert result["counts"] == {"ocr_a": 2, "cer": 2} and result["metrics"]["ocr_a"] == pytest.approx(0.5)
    ocr_lines = (out / "ocr_results.txt").read_text().splitlines()
    assert ocr_lines[0].startswith("a.png\t") and json.loads(ocr_lines[0].split("\t")[1])[1]["transcription"] == "标题"


def fake_text(crop):
    """按裁出区域的平均亮度给出识别文本，用来确认裁的是标注框。"""
    mean = float(np.mean(crop))
    return "P38" if mean < 85 else "HEFV" if mean < 160 else "标题"


class FakeTextRecognition3:
    """模拟 paddleocr 3.x 的 TextRecognition：predict 输入裁好的图列表，结果含 rec_text。"""

    def __init__(self, **kwargs):
        FakeTextRecognition3.last_kwargs = kwargs

    def predict(self, crops):
        return [{"rec_text": fake_text(c), "rec_score": 0.9} for c in crops]


class FakeRecognizer2:
    """模拟 paddleocr 2.x：ocr(图列表, det=False, cls=False) 返回 [[(文本, 置信度), ...]]。"""

    def __init__(self, **kwargs):
        pass

    def ocr(self, crops, det=True, cls=True):
        assert det is False and cls is False and isinstance(crops, list)
        return [[(fake_text(c), 0.9) for c in crops]]


@pytest.mark.parametrize("version", [3, 2])
def test_ocr_label_regions(tmp_path, monkeypatch, version):
    """参考值来自标注文件时只识别标注框，逐框比较；预测图尺寸与参考图不同时按比例缩放坐标。"""
    import types

    pytest.importorskip("cv2")
    module = (types.SimpleNamespace(TextRecognition=FakeTextRecognition3, PaddleOCR=FakePaddleOCR3) if version == 3
              else types.SimpleNamespace(PaddleOCR=FakeRecognizer2))
    monkeypatch.setitem(sys.modules, "paddleocr", module)

    def draw(scale, blocks):
        img = np.zeros((100 * scale, 200 * scale, 3), np.uint8)
        for (x0, y0, x1, y1), value in blocks:
            img[y0 * scale:y1 * scale, x0 * scale:x1 * scale] = value
        return img

    # a：两个标注框，另有一块没有标注的文字区域（不应参与评测）；b：一个框，识别结果与标注差一个字
    layouts = {"a": [((10, 10, 90, 40), 50), ((110, 50, 190, 90), 230), ((10, 60, 90, 90), 120)],
               "b": [((20, 20, 120, 60), 120)]}
    for folder, scale in (("pred", 2), ("gt", 2), ("lq", 1)):
        (tmp_path / folder).mkdir()
        for name, blocks in layouts.items():
            Image.fromarray(draw(scale, blocks)).save(tmp_path / folder / f"{name}.png")

    def box(text, x0, y0, x1, y1, **kw):
        return {"transcription": text, "points": [[x0, y0], [x1, y0], [x1, y1], [x0, y1]], **kw}

    # 标注坐标对应 GT（2 倍大小）
    label = tmp_path / "Label.txt"
    label.write_text("\n".join([
        "a.jpg\t" + json.dumps([box("P38", 24, 24, 176, 76), box("标题", 224, 104, 376, 176),
                                box("看不清", 20, 120, 180, 180, difficult=True)], ensure_ascii=False),
        "b.jpg\t" + json.dumps([box("HEFU", 44, 44, 236, 116)]),
    ]) + "\n")
    refs = [str(tmp_path / "gt"), str(label)]
    expected = {"ocr_a": 2 / 3, "cer": 1 / 9, "ned": (1 + 1 + 0.75) / 3}
    for folder in ("pred", "lq"):
        out = tmp_path / f"eval-{folder}"
        result = evaluate.evaluate(str(tmp_path / folder), str(out), ["ocr_a", "cer", "ned"], reference_path=refs,
                                   device="cuda:0", log=lambda *_: None)
        assert result["num_text_regions"] == 3, folder
        for metric, value in expected.items():
            assert result["metrics"][metric] == pytest.approx(value), (folder, metric)
        rows = {r["id"]: r for r in map(json.loads, (out / "per_sample.jsonl").read_text().splitlines())}
        assert rows["a"]["ocr_a"] == 1.0 and rows["a"]["ocr_text"] == "P38\n标题"
        assert rows["b"]["ocr_regions"] == [{"ref": "HEFU", "pred": "HEFV"}] and rows["b"]["cer"] == pytest.approx(0.25)
        assert (out / "ocr_results.txt").is_file()
    if version == 3:
        assert FakeTextRecognition3.last_kwargs == {"device": "gpu:0"}


def test_ocr_unavailable(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "paddleocr", None)  # import 失败
    Image.fromarray(np.zeros((8, 8, 3), np.uint8)).save(tmp_path / "a.png")
    (tmp_path / "Label.txt").write_text('a.jpg\t[{"transcription": "x", "points": [[0, 0], [1, 1]]}]\n')
    result = evaluate.evaluate(str(tmp_path), str(tmp_path / "eval"), ["cer"],
                               reference_path=str(tmp_path / "Label.txt"), log=lambda *_: None)
    assert result["metrics"]["cer"] is None and "paddleocr" in result["errors"]["cer"]

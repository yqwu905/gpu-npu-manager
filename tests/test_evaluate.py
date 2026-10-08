import json

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

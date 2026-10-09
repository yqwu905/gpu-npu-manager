#!/usr/bin/env python3
"""推理结果评测脚本，由中心服务作为普通任务在结果所在的服务器上运行。

输入：结果集目录（--predictions），有两种形式：
  1. 目录下有 predictions.jsonl，每行一个样本：
       {"id": "0001", "image": "images/0001.png", "ref_image": "/data/gt/0001.png",
        "text": "识别结果", "ref_text": "真实文本"}
     相对路径相对于 predictions.jsonl 所在目录。
  2. 没有 predictions.jsonl 时扫描目录：每个图片或 .txt 文件是一个样本，样本 ID 为去掉扩展名的相对路径，
     同名的图片和 .txt 属于同一个样本（.txt 内容为识别文本）。
  参考值（--reference）可以是 jsonl（按 id 合并 ref_image / ref_text），也可以是目录：
  按样本 ID 配对同名的图片和 .txt，找不到时再按文件名（不含目录）配对。
  文字参考值还可以是 PaddleOCR 格式的标注文件，每行“图片文件名<Tab>文本框 JSON 数组”：
    xxx.jpg	[{"transcription": "文字", "points": [[x, y], ...], "difficult": false}, ...]
  按图片文件名（去掉扩展名）与样本配对；忽略 difficult 为 true 或内容为 ### 的框，
  其余框按阅读顺序（从上到下分行，行内从左到右）拼成参考文本：同一行用空格连接，不同行用换行连接。

输出（--output 目录）：
  - metrics.json      整体指标、每个指标的有效样本数和错误
  - per_sample.jsonl  逐样本指标，顺序与输入样本相同

指标：
  图像  psnr、ssim（numpy + Pillow）、lpips（需要 torch 和 lpips 包）
  文本  ocr_a（整句完全一致的比例）、cer（字符错误率）、ned（1-NED，归一化编辑距离相似度）
"""
import argparse
import json
import math
import os
import sys

IMAGE_METRICS = ("psnr", "ssim", "lpips")
TEXT_METRICS = ("ocr_a", "cer", "ned")
ALL_METRICS = IMAGE_METRICS + TEXT_METRICS
# 两张图完全相同时 PSNR 为无穷大，按惯例截断为 100 dB
PSNR_CAP = 100.0


# ---------------------------------------------------------------------------
# 文本指标
# ---------------------------------------------------------------------------


def edit_distance(a, b):
    """字符级 Levenshtein 距离。"""
    if len(a) < len(b):
        a, b = b, a
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        current = [i]
        for j, cb in enumerate(b, start=1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (ca != cb)))
        previous = current
    return previous[-1]


def text_scores(pred, ref):
    """返回单个样本的 (编辑距离, 参考长度, 是否完全一致, 1-NED)。"""
    distance = edit_distance(pred, ref)
    longest = max(len(pred), len(ref))
    ned = 1.0 if longest == 0 else 1.0 - distance / float(longest)
    return distance, len(ref), pred == ref, ned


# ---------------------------------------------------------------------------
# 图像指标
# ---------------------------------------------------------------------------


def load_image(path):
    import numpy as np
    from PIL import Image

    with Image.open(path) as img:
        return np.asarray(img.convert("RGB"), dtype=np.float64)


def psnr(pred, ref):
    import numpy as np

    mse = np.mean((pred - ref) ** 2)
    if mse == 0:
        return PSNR_CAP
    return min(PSNR_CAP, 10.0 * math.log10(255.0 ** 2 / mse))


def _gaussian_kernel(sigma=1.5, truncate=3.5):
    import numpy as np

    radius = int(truncate * sigma + 0.5)
    x = np.arange(-radius, radius + 1, dtype=np.float64)
    kernel = np.exp(-0.5 * (x / sigma) ** 2)
    return kernel / kernel.sum(), radius


def _gaussian_filter(img, kernel, radius):
    """二维可分离高斯滤波，边界按对称方式延拓（与 scipy.ndimage 的 reflect 一致）。"""
    import numpy as np

    padded = np.pad(img, radius, mode="symmetric")
    rows = sum(kernel[k] * padded[:, k : k + img.shape[1]] for k in range(len(kernel)))
    return sum(kernel[k] * rows[k : k + img.shape[0], :] for k in range(len(kernel)))


def ssim(pred, ref):
    """Wang et al. 2004 的 SSIM：11x11 高斯窗、sigma=1.5、K1=0.01、K2=0.03，RGB 各通道平均。

    与 skimage.metrics.structural_similarity(gaussian_weights=True, sigma=1.5,
    use_sample_covariance=False, data_range=255, channel_axis=-1) 结果一致。
    """
    kernel, radius = _gaussian_kernel()
    c1 = (0.01 * 255) ** 2
    c2 = (0.03 * 255) ** 2
    values = []
    for channel in range(pred.shape[2]):
        x = pred[:, :, channel]
        y = ref[:, :, channel]
        ux = _gaussian_filter(x, kernel, radius)
        uy = _gaussian_filter(y, kernel, radius)
        vx = _gaussian_filter(x * x, kernel, radius) - ux * ux
        vy = _gaussian_filter(y * y, kernel, radius) - uy * uy
        vxy = _gaussian_filter(x * y, kernel, radius) - ux * uy
        s = ((2 * ux * uy + c1) * (2 * vxy + c2)) / ((ux ** 2 + uy ** 2 + c1) * (vx + vy + c2))
        values.append(s[radius:-radius, radius:-radius].mean())
    return float(sum(values) / len(values))


class Lpips(object):
    """LPIPS（AlexNet 版本），输入缩放到 [-1, 1]。"""

    def __init__(self, device):
        import lpips  # noqa: F401  需要 pip install lpips
        import torch

        if device.startswith("npu"):
            import torch_npu  # noqa: F401

        self.torch = torch
        self.device = device
        self.model = lpips.LPIPS(net="alex", verbose=False).to(device).eval()

    def __call__(self, pred, ref):
        torch = self.torch

        def to_tensor(img):
            t = torch.from_numpy(img / 127.5 - 1.0).permute(2, 0, 1).unsqueeze(0).float()
            return t.to(self.device)

        with torch.no_grad():
            return float(self.model(to_tensor(pred), to_tensor(ref)).item())


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def read_jsonl(path):
    records = []
    with open(path, encoding="utf-8") as f:
        for number, line in enumerate(f, start=1):
            if line.strip():
                try:
                    records.append(json.loads(line))
                except ValueError as exc:
                    raise SystemExit("{} 第 {} 行不是合法 JSON: {}".format(path, number, exc))
    return records


def resolve(base_dir, path):
    return path if os.path.isabs(path) else os.path.join(base_dir, path)


PREDICTIONS_FILE = "predictions.jsonl"
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tif", ".tiff")
TEXT_EXTS = (".txt",)
# 扫描目录时跳过平台生成的评测输出
SKIP_DIRS = ("eval",)


def scan_samples(directory):
    """扫描目录里的图片和 .txt，按去掉扩展名的相对路径组成样本，按 ID 排序。

    文本只记录文件路径（text_file），需要内容时用 fill_text 读取。
    """
    samples = {}
    for root, dirs, files in os.walk(directory):
        rel_root = os.path.relpath(root, directory)
        dirs[:] = sorted(
            d for d in dirs if not d.startswith(".") and not (rel_root == "." and d in SKIP_DIRS)
        )
        for name in files:
            stem, ext = os.path.splitext(name)
            ext = ext.lower()
            if name.startswith(".") or ext not in IMAGE_EXTS + TEXT_EXTS:
                continue
            rel = name if rel_root == "." else os.path.join(rel_root, name)
            sample_id = os.path.splitext(rel)[0].replace(os.sep, "/")
            sample = samples.setdefault(sample_id, {"id": sample_id})
            key = "image" if ext in IMAGE_EXTS else "text_file"
            sample.setdefault(key, rel.replace(os.sep, "/"))
    return [samples[k] for k in sorted(samples)]


def fill_text(base_dir, record):
    """把 text_file 指向的文本读到 text 字段。"""
    if "text_file" in record and "text" not in record:
        try:
            with open(resolve(base_dir, record["text_file"]), encoding="utf-8", errors="replace") as f:
                record["text"] = f.read().strip()
        except OSError as exc:
            record["_error"] = "读取 {} 失败: {}".format(record["text_file"], exc)
    return record


def load_records(path):
    """读取样本，返回 (样本列表, 相对路径的基准目录)。path 可以是目录或 jsonl 文件。"""
    if os.path.isdir(path):
        jsonl = os.path.join(path, PREDICTIONS_FILE)
        if os.path.isfile(jsonl):
            return read_jsonl(jsonl), os.path.abspath(path)
        base_dir = os.path.abspath(path)
        return [fill_text(base_dir, r) for r in scan_samples(base_dir)], base_dir
    return read_jsonl(path), os.path.dirname(os.path.abspath(path))


# PaddleOCR 标注中表示“无法辨认、不参与评测”的内容
IGNORED_TRANSCRIPTIONS = ("###",)


def _parse_ocr_label_line(line):
    """解析标注文件的一行，返回 (图片文件名, 文本框列表)；不是该格式时返回 None。"""
    name, sep, payload = line.rstrip("\r\n").partition("\t")
    if not sep or not payload.lstrip().startswith("["):
        return None
    try:
        boxes = json.loads(payload)
    except ValueError:
        return None
    return (name.strip(), boxes) if isinstance(boxes, list) else None


def is_ocr_label_file(path):
    """第一行非空内容是否为“图片文件名<Tab>JSON 数组”。"""
    if not os.path.isfile(path):
        return False
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            if line.strip():
                return _parse_ocr_label_line(line) is not None
    return False


def boxes_to_text(boxes):
    """把文本框按阅读顺序拼成一段文本：中心高度相近的框归为一行，行内按左边界排序。"""
    items = []
    for box in boxes:
        text = box.get("transcription")
        if box.get("difficult") or text is None or str(text) in IGNORED_TRANSCRIPTIONS:
            continue
        points = box.get("points") or [[0, 0]]
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        items.append((min(ys), max(ys), min(xs), str(text)))
    items.sort(key=lambda b: (b[0] + b[1]) / 2.0)
    lines = []
    for top, bottom, left, text in items:
        center, height = (top + bottom) / 2.0, bottom - top
        if lines:
            line = lines[-1]
            if abs(center - line["center"]) <= max(height, line["height"]) / 2.0:
                line["boxes"].append((left, text))
                continue
        lines.append({"center": center, "height": height, "boxes": [(left, text)]})
    return "\n".join(" ".join(t for _, t in sorted(line["boxes"])) for line in lines)


def load_ocr_labels(path):
    """读取 PaddleOCR 格式标注文件：去掉扩展名的图片文件名 -> {id, ref_text}。"""
    labels = {}
    with open(path, encoding="utf-8", errors="replace") as f:
        for number, line in enumerate(f, start=1):
            if not line.strip():
                continue
            parsed = _parse_ocr_label_line(line)
            if parsed is None:
                raise SystemExit("{} 第 {} 行不是“图片文件名<Tab>JSON 数组”格式".format(path, number))
            name, boxes = parsed
            sample_id = os.path.splitext(os.path.basename(name))[0]
            labels[sample_id] = {"id": sample_id, "ref_text": boxes_to_text(boxes)}
    return labels


def load_references(path):
    """参考值：样本 ID -> {ref_image, ref_text}；目录形式另按文件名建索引用于兜底配对。"""
    if is_ocr_label_file(path):
        labels = load_ocr_labels(path)
        return labels, labels
    records, base_dir = load_records(path)
    by_id, by_name = {}, {}
    from_dir = os.path.isdir(path) and not os.path.isfile(os.path.join(path, PREDICTIONS_FILE))
    for item in records:
        if from_dir:
            # 目录里的文件本身就是参考值
            item = {"id": item["id"], "ref_image": item.get("image"), "ref_text": item.get("text")}
            item = {k: v for k, v in item.items() if v is not None}
        if "ref_image" in item:
            item["ref_image"] = resolve(base_dir, item["ref_image"])
        sample_id = str(item.get("id"))
        by_id[sample_id] = item
        if from_dir:
            by_name.setdefault(sample_id.rsplit("/", 1)[-1], []).append(item)
    # 文件名重复时无法确定配对，不用于兜底
    return by_id, {k: v[0] for k, v in by_name.items() if len(v) == 1}


def evaluate(predictions_path, output_dir, metrics, reference_path=None, device="cpu", log=print):
    records, base_dir = load_records(predictions_path)
    references, references_by_name = {}, {}
    if reference_path:
        references, references_by_name = load_references(reference_path)

    errors = {}
    lpips_model = None
    if "lpips" in metrics:
        try:
            lpips_model = Lpips(device)
        except Exception as exc:  # 缺少依赖或设备不可用
            errors["lpips"] = "LPIPS 初始化失败（需要安装 torch 和 lpips）: {}".format(exc)

    sums = {m: 0.0 for m in metrics}
    counts = {m: 0 for m in metrics}
    cer_distance = 0
    cer_length = 0
    skipped = []
    per_sample = []

    for index, record in enumerate(records):
        sample_id = record.get("id", index)
        reference = references.get(str(sample_id))
        if reference is None:
            reference = references_by_name.get(str(sample_id).rsplit("/", 1)[-1], {})
        merged = dict(reference)
        merged.update({k: v for k, v in record.items() if v is not None})
        row = {"id": sample_id}

        wants_image = any(m in metrics for m in IMAGE_METRICS)
        if wants_image and merged.get("image") and merged.get("ref_image"):
            try:
                pred = load_image(resolve(base_dir, merged["image"]))
                ref = load_image(resolve(base_dir, merged["ref_image"]))
                if pred.shape != ref.shape:
                    raise ValueError("尺寸不一致 {} vs {}".format(pred.shape[:2], ref.shape[:2]))
                if "psnr" in metrics:
                    row["psnr"] = psnr(pred, ref)
                if "ssim" in metrics:
                    row["ssim"] = ssim(pred, ref)
                if lpips_model is not None:
                    row["lpips"] = lpips_model(pred, ref)
            except Exception as exc:
                skipped.append({"id": sample_id, "error": "图像指标: {}".format(exc)})

        wants_text = any(m in metrics for m in TEXT_METRICS)
        if wants_text and merged.get("ref_text") is not None:
            distance, ref_len, exact, ned = text_scores(str(merged.get("text") or ""), str(merged["ref_text"]))
            cer_distance += distance
            cer_length += ref_len
            if "ocr_a" in metrics:
                row["ocr_a"] = 1.0 if exact else 0.0
            if "cer" in metrics:
                row["cer"] = distance / float(ref_len) if ref_len else float(distance > 0)
            if "ned" in metrics:
                row["ned"] = ned

        for metric in metrics:
            if metric in row and metric != "cer":
                sums[metric] += row[metric]
                counts[metric] += 1
            elif metric == "cer" and metric in row:
                counts[metric] += 1
        per_sample.append(row)
        if (index + 1) % 500 == 0:
            log("已处理 {}/{}".format(index + 1, len(records)))

    summary = {}
    for metric in metrics:
        if metric == "cer":
            # 语料级 CER：总编辑距离 / 参考文本总字符数
            summary[metric] = cer_distance / float(cer_length) if cer_length else None
        else:
            summary[metric] = sums[metric] / counts[metric] if counts[metric] else None
        if counts[metric] == 0 and metric not in errors:
            errors[metric] = "没有可用于该指标的样本（检查 image/ref_image 或 ref_text 字段）"

    if not os.path.isdir(output_dir):
        os.makedirs(output_dir)
    with open(os.path.join(output_dir, "per_sample.jsonl"), "w", encoding="utf-8") as f:
        for row in per_sample:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    result = {
        "metrics": summary,
        "counts": counts,
        "errors": errors,
        "num_samples": len(records),
        "skipped": skipped[:100],
        "num_skipped": len(skipped),
    }
    # 最后写 metrics.json，它的存在代表评测完成
    with open(os.path.join(output_dir, "metrics.json"), "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    return result


def main():
    parser = argparse.ArgumentParser(description="推理结果评测")
    parser.add_argument("--predictions", required=True, help="结果集目录（或 predictions.jsonl 路径）")
    parser.add_argument("--output", required=True, help="评测输出目录")
    parser.add_argument("--metrics", required=True, help="逗号分隔: " + ",".join(ALL_METRICS))
    parser.add_argument(
        "--reference", default=None, help="可选，参考值 jsonl、目录（按文件名配对）或 PaddleOCR 格式的文字标注文件"
    )
    parser.add_argument("--device", default="cpu", help="LPIPS 使用的设备，如 cpu、cuda、npu")
    args = parser.parse_args()

    metrics = [m.strip() for m in args.metrics.split(",") if m.strip()]
    unknown = [m for m in metrics if m not in ALL_METRICS]
    if unknown:
        raise SystemExit("未知指标: {}".format(", ".join(unknown)))
    result = evaluate(args.predictions, args.output, metrics, args.reference, args.device)
    print(json.dumps(result["metrics"], ensure_ascii=False))
    if result["errors"]:
        print("部分指标未能计算: {}".format(json.dumps(result["errors"], ensure_ascii=False)), file=sys.stderr)


if __name__ == "__main__":
    main()

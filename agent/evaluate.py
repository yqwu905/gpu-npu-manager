#!/usr/bin/env python3
"""推理结果评测脚本，由中心服务作为普通任务在结果所在的服务器上运行。

输入：结果集目录（--predictions），有两种形式：
  1. 目录下有 predictions.jsonl，每行一个样本：
       {"id": "0001", "image": "images/0001.png", "ref_image": "/data/gt/0001.png",
        "text": "识别结果", "ref_text": "真实文本"}
     相对路径相对于 predictions.jsonl 所在目录。
  2. 没有 predictions.jsonl 时扫描目录：每个图片或 .txt 文件是一个样本，样本 ID 为去掉扩展名的相对路径，
     同名的图片和 .txt 属于同一个样本（.txt 内容为识别文本）。
  参考值（--reference，可以给多个，例如 GT 目录和文字标注文件，按顺序合并）可以是 jsonl（按 id 合并
  ref_image / ref_text），也可以是目录：按样本 ID 配对同名的图片和 .txt，找不到时再按文件名（不含目录）配对。
  文字参考值还可以是 PaddleOCR 格式的标注文件，每行“图片文件名<Tab>文本框 JSON 数组”：
    xxx.jpg	[{"transcription": "文字", "points": [[x, y], ...], "difficult": false}, ...]
  按图片文件名（去掉扩展名）与样本配对；忽略 difficult 为 true 或内容为 ### 的框，
  其余框按阅读顺序（从上到下分行，行内从左到右）拼成参考文本：同一行用空格连接，不同行用换行连接。
  样本没有识别文本（text）但有预测图时，用 PaddleOCR（pip 包 paddleocr，2.x 和 3.x 均可）识别：
  - 参考值来自标注文件时，只识别标注框内的文字（标注不一定覆盖图中所有文字）：按框裁图（四点框透视变换拉正），
    只做识别不做检测，逐框与 transcription 比较。标注坐标对应参考图，预测图尺寸不同（如 LQ）时按比例缩放；
  - 其他情况对整图做检测和识别，识别出的框按同样的规则拼成文本后与参考文本比较。
  识别结果另存为输出目录下的 ocr_results.txt（标注文件格式）。

  LQ 目录（--lq）按同样的规则与样本配对，配对到的 LQ 图片路径写入逐样本结果，供页面并排展示；
  加 --lq-baseline 时把 LQ 图片当作预测结果，用同样的参考值和指标再评测一次，作为基线。

输出（--output 目录）：
  - metrics.json      整体指标、每个指标的有效样本数和错误；计算了 LQ 基线时另有 lq 字段
  - per_sample.jsonl  逐样本指标，顺序与输入样本相同，另含配对到的 ref_image、ref_text、lq_image 和 OCR 识别文本
  - progress.json     评测过程中的进度 {"stage": "lq" 或 "main", "done", "total", "elapsed"}，每 10 秒更新
  - ocr_results.txt   做了 OCR 时的识别结果，每行“图片路径<Tab>文本框 JSON 数组”
  - lq/               LQ 基线的评测输出（结构同上）

指标：
  图像  psnr、ssim（--device 不是 cpu 且装了 torch 时在 GPU/NPU 上算，否则用 numpy）、lpips（需要 torch 和 lpips 包）
  文本  ocr_a（完全一致的区域占比）、cer（总编辑距离 / 参考总字符数）、ned（各区域 1-NED 的平均）
        区域：标注文件的每个框；按整段文本比较的样本整个算一个区域
"""
import argparse
import json
import math
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

IMAGE_METRICS = ("psnr", "ssim", "lpips")
TEXT_METRICS = ("ocr_a", "cer", "ned")
ALL_METRICS = IMAGE_METRICS + TEXT_METRICS
# 两张图完全相同时 PSNR 为无穷大，按惯例截断为 100 dB
PSNR_CAP = 100.0
# 进度日志的最小间隔（秒）
PROGRESS_SECONDS = 10
# 并行解码图片的线程数，也是提前解码的样本数
IMAGE_WORKERS = max(1, min(4, os.cpu_count() or 1))


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


class TorchImageMetrics(object):
    """在 GPU/NPU 上用 torch 算 PSNR 和 SSIM，公式与上面的 numpy 版本相同（float32 计算，差异在 1e-4 以内）。

    SSIM 最后会裁掉边缘 radius 个像素，裁剪后的区域不依赖边界延拓，所以这里直接做不补边的卷积。
    """

    def __init__(self, device):
        import torch
        import torch.nn.functional as F

        if device.startswith("npu"):
            import torch_npu  # noqa: F401

        self.torch = torch
        self.F = F
        self.device = device
        kernel, self.radius = _gaussian_kernel()
        k = torch.tensor(kernel, dtype=torch.float32, device=device)
        self.kx = k.view(1, 1, 1, -1).repeat(3, 1, 1, 1)
        self.ky = k.view(1, 1, -1, 1).repeat(3, 1, 1, 1)

    def _filter(self, x):
        x = self.F.conv2d(x, self.kx, groups=3)
        return self.F.conv2d(x, self.ky, groups=3)

    def __call__(self, pred, ref, metrics):
        torch = self.torch

        def to_tensor(img):
            return torch.from_numpy(img).to(self.device, torch.float32).permute(2, 0, 1).unsqueeze(0)

        values = {}
        with torch.no_grad():
            x = to_tensor(pred)
            y = to_tensor(ref)
            if "psnr" in metrics:
                mse = float(torch.mean((x - y) ** 2).item())
                values["psnr"] = PSNR_CAP if mse == 0 else min(PSNR_CAP, 10.0 * math.log10(255.0 ** 2 / mse))
            if "ssim" in metrics:
                c1 = (0.01 * 255) ** 2
                c2 = (0.03 * 255) ** 2
                ux = self._filter(x)
                uy = self._filter(y)
                vx = self._filter(x * x) - ux * ux
                vy = self._filter(y * y) - uy * uy
                vxy = self._filter(x * y) - ux * uy
                s = ((2 * ux * uy + c1) * (2 * vxy + c2)) / ((ux ** 2 + uy ** 2 + c1) * (vx + vy + c2))
                values["ssim"] = float(s.mean(dim=(2, 3)).mean().item())
        return values


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


class PaddleOcr(object):
    """用 PaddleOCR 检测并识别图片中的文字，返回 [{"transcription", "points"}, ...]。

    兼容 paddleocr 3.x（PaddleOCR.predict，结果字段 rec_texts / rec_polys）和
    2.x（PaddleOCR.ocr，结果为 [[框, (文本, 置信度)], ...]，没有文字时为 None）。
    """

    def __init__(self, device):
        from paddleocr import PaddleOCR  # 需要 pip install paddleocr 及对应的 paddlepaddle

        self.v3 = hasattr(PaddleOCR, "predict")
        if self.v3:
            # paddle 的设备名：cpu、gpu:0、npu:0
            # 默认流水线（paddlex 的 OCR.yaml）会先做文档方向分类和 UVDoc 去扭曲，很慢且会改变图片坐标，评测不需要
            self.engine = PaddleOCR(
                lang="ch", device=device.replace("cuda", "gpu"),
                use_doc_orientation_classify=False, use_doc_unwarping=False,
            )
        else:
            self.engine = PaddleOCR(
                lang="ch", use_angle_cls=True, show_log=False,
                use_gpu=device.startswith("cuda"), use_npu=device.startswith("npu"),
            )

    def __call__(self, path):
        if self.v3:
            result = self.engine.predict(path)[0]
            pairs = zip(result["rec_texts"], result["rec_polys"])
        else:
            page = self.engine.ocr(path, cls=True)[0] or []
            pairs = ((text, box) for box, (text, _score) in page)
        return [
            {"transcription": str(text), "points": [[int(x), int(y)] for x, y in poly], "difficult": False}
            for text, poly in pairs
        ]


def crop_text_region(img, points):
    """按标注框从 BGR 图中裁出文字区域，与 PaddleOCR 的做法一致：四点框做透视变换拉正，
    其他多边形取最小外接矩形；裁出的图高宽比不小于 1.5 时（竖排文字）旋转 90 度。"""
    import cv2
    import numpy as np

    pts = np.array(points, dtype=np.float32).reshape(-1, 2)
    if len(pts) != 4:
        rect = cv2.boxPoints(cv2.minAreaRect(pts))
        rect = sorted(rect.tolist(), key=lambda p: p[0])
        left = sorted(rect[:2], key=lambda p: p[1])
        right = sorted(rect[2:], key=lambda p: p[1])
        pts = np.array([left[0], right[0], right[1], left[1]], dtype=np.float32)
    width = int(max(np.linalg.norm(pts[0] - pts[1]), np.linalg.norm(pts[2] - pts[3])))
    height = int(max(np.linalg.norm(pts[0] - pts[3]), np.linalg.norm(pts[1] - pts[2])))
    width, height = max(width, 1), max(height, 1)
    target = np.float32([[0, 0], [width, 0], [width, height], [0, height]])
    matrix = cv2.getPerspectiveTransform(pts, target)
    crop = cv2.warpPerspective(img, matrix, (width, height), borderMode=cv2.BORDER_REPLICATE, flags=cv2.INTER_CUBIC)
    if crop.shape[0] * 1.0 / crop.shape[1] >= 1.5:
        crop = np.rot90(crop)
    return np.ascontiguousarray(crop)


class TextRecognizer(object):
    """只做文字识别（不做检测），输入裁好的 BGR 文字区域图列表，返回识别文本列表。

    兼容 paddleocr 3.x（TextRecognition.predict，结果字段 rec_text）和
    2.x（PaddleOCR.ocr(..., det=False, cls=False)，结果为 [[(文本, 置信度), ...]]）。
    """

    def __init__(self, device):
        import paddleocr  # 需要 pip install paddleocr 及对应的 paddlepaddle
        import cv2  # noqa: F401  裁图需要 opencv，paddleocr 的依赖里已包含

        if hasattr(paddleocr, "TextRecognition"):
            self.v3 = True
            self.engine = paddleocr.TextRecognition(device=device.replace("cuda", "gpu"))
        else:
            self.v3 = False
            self.engine = paddleocr.PaddleOCR(
                lang="ch", use_angle_cls=False, show_log=False,
                use_gpu=device.startswith("cuda"), use_npu=device.startswith("npu"),
            )

    def __call__(self, crops):
        if not crops:
            return []
        if self.v3:
            return [str(r["rec_text"]) for r in self.engine.predict(crops)]
        return [str(text) for text, _score in self.engine.ocr(crops, det=False, cls=False)[0]]


def recognize_regions(recognizer, image_path, boxes, ref_image_path=None):
    """逐个标注框裁图识别。标注坐标对应参考图（GT）；预测图（如 LQ）尺寸不同时按比例缩放坐标。"""
    import cv2
    import numpy as np
    from PIL import Image

    with Image.open(image_path) as img:
        img = cv2.cvtColor(np.asarray(img.convert("RGB")), cv2.COLOR_RGB2BGR)
    sx = sy = 1.0
    if ref_image_path and os.path.isfile(ref_image_path):
        with Image.open(ref_image_path) as ref:
            rw, rh = ref.size
        sx, sy = img.shape[1] / float(rw), img.shape[0] / float(rh)
    crops = [crop_text_region(img, [[p[0] * sx, p[1] * sy] for p in box["points"]]) for box in boxes]
    return recognizer(crops)


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
            # 参与评测的框：忽略 difficult 和 ###，没有坐标的框无法裁图，也忽略
            regions = [
                {"transcription": str(b.get("transcription")), "points": b.get("points")}
                for b in boxes
                if not b.get("difficult") and b.get("transcription") is not None
                and str(b.get("transcription")) not in IGNORED_TRANSCRIPTIONS and len(b.get("points") or []) >= 3
            ]
            labels[sample_id] = {"id": sample_id, "ref_text": boxes_to_text(boxes), "ref_boxes": regions}
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


def merge_references(paths):
    """合并多个参考值来源，同一样本的字段合并（如 GT 目录给 ref_image、标注文件给 ref_text）。"""
    references, references_by_name = {}, {}
    for path in paths:
        by_id, by_name = load_references(path)
        for target, source in ((references, by_id), (references_by_name, by_name)):
            for key, item in source.items():
                target[key] = dict(target.get(key, {}), **item)
    return references, references_by_name


def load_lq_images(path):
    """LQ 目录：样本 ID -> 图片绝对路径，另按文件名建索引用于兜底配对。"""
    records, base_dir = load_records(path)
    by_id, by_name = {}, {}
    for item in records:
        if not item.get("image"):
            continue
        sample_id = str(item.get("id"))
        by_id[sample_id] = resolve(base_dir, item["image"])
        by_name.setdefault(sample_id.rsplit("/", 1)[-1], []).append(by_id[sample_id])
    return by_id, {k: v[0] for k, v in by_name.items() if len(v) == 1}


def log_flush(message):
    """输出到任务日志并立即刷新：任务的 stdout 是文件，默认按块缓冲，进度会迟迟看不到。"""
    print(message)
    sys.stdout.flush()


def write_progress(path, stage, done, total, elapsed):
    """写进度文件供中心服务读取，先写临时文件再改名，避免读到写了一半的内容。"""
    if not path:
        return
    directory = os.path.dirname(path)
    if directory and not os.path.isdir(directory):
        os.makedirs(directory)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"stage": stage, "done": done, "total": total, "elapsed": round(elapsed, 1)}, f)
    os.replace(tmp, path)


def evaluate(predictions_path, output_dir, metrics, reference_path=None, device="cpu", log=log_flush,
             lq_path=None, extra=None, progress_path=None, stage="main"):
    records, base_dir = load_records(predictions_path)
    if isinstance(reference_path, str):
        reference_path = [reference_path]
    references, references_by_name = merge_references(reference_path or [])
    lq_by_id, lq_by_name = load_lq_images(lq_path) if lq_path else ({}, {})

    errors = {}
    lpips_model = None
    log("共 {} 个样本，指标 {}，设备 {}".format(len(records), ",".join(metrics), device))
    if "lpips" in metrics:
        try:
            log("加载 LPIPS 模型")
            lpips_model = Lpips(device)
        except Exception as exc:  # 缺少依赖或设备不可用
            errors["lpips"] = "LPIPS 初始化失败（需要安装 torch 和 lpips）: {}".format(exc)
    torch_metrics = None
    if device != "cpu" and ("psnr" in metrics or "ssim" in metrics):
        try:
            torch_metrics = TorchImageMetrics(device)
        except Exception as exc:  # 没装 torch 或设备不可用时退回 numpy
            log("PSNR/SSIM 改用 CPU 计算（torch 不可用: {}）".format(exc))
    ocr = None
    ocr_error = None
    ocr_lines = []

    sums = {m: 0.0 for m in metrics}
    counts = {m: 0 for m in metrics}
    cer_distance = 0
    cer_length = 0
    # 文字指标按区域汇总：标注文件中的每个框是一个区域，样本自带识别文本时整个样本算一个区域
    text_regions = 0
    text_exact = 0
    text_ned = 0.0
    recognizer = None
    skipped = []
    per_sample = []
    started = last_log = time.time()
    write_progress(progress_path, stage, 0, len(records), 0)

    def merge(index, record):
        sample_id = record.get("id", index)
        reference = references.get(str(sample_id))
        if reference is None:
            reference = references_by_name.get(str(sample_id).rsplit("/", 1)[-1], {})
        merged = dict(reference)
        merged.update({k: v for k, v in record.items() if v is not None})
        return merged

    wants_image = any(m in metrics for m in IMAGE_METRICS)
    merged_records = [merge(index, record) for index, record in enumerate(records)]

    def load_pair(merged):
        return load_image(resolve(base_dir, merged["image"])), load_image(resolve(base_dir, merged["ref_image"]))

    # 后台线程提前解码后面几个样本的图片，计算和解码并行
    pool = ThreadPoolExecutor(IMAGE_WORKERS) if wants_image else None
    pending = {}

    def prefetch(start):
        for j in range(start, min(start + IMAGE_WORKERS, len(records))):
            if j not in pending and merged_records[j].get("image") and merged_records[j].get("ref_image"):
                pending[j] = pool.submit(load_pair, merged_records[j])

    for index, record in enumerate(records):
        sample_id = record.get("id", index)
        merged = merged_records[index]
        row = {"id": sample_id}
        lq_image = lq_by_id.get(str(sample_id)) or lq_by_name.get(str(sample_id).rsplit("/", 1)[-1])
        if lq_image:
            row["lq_image"] = lq_image
        if merged.get("ref_image"):
            row["ref_image"] = resolve(base_dir, merged["ref_image"])
        if merged.get("ref_text") is not None:
            row["ref_text"] = merged["ref_text"]

        if wants_image and merged.get("image") and merged.get("ref_image"):
            prefetch(index)
            try:
                pred, ref = pending.pop(index).result()
                if pred.shape != ref.shape:
                    raise ValueError("尺寸不一致 {} vs {}".format(pred.shape[:2], ref.shape[:2]))
                if torch_metrics is not None:
                    row.update(torch_metrics(pred, ref, metrics))
                else:
                    if "psnr" in metrics:
                        row["psnr"] = psnr(pred, ref)
                    if "ssim" in metrics:
                        row["ssim"] = ssim(pred, ref)
                if lpips_model is not None:
                    row["lpips"] = lpips_model(pred, ref)
            except Exception as exc:
                skipped.append({"id": sample_id, "error": "图像指标: {}".format(exc)})

        wants_text = any(m in metrics for m in TEXT_METRICS)
        regions = merged.get("ref_boxes")
        by_region = wants_text and regions is not None and merged.get("text") is None and merged.get("image")
        needs_ocr = (wants_text and not by_region and merged.get("ref_text") is not None
                     and merged.get("text") is None and merged.get("image"))
        if by_region:
            # 标注文件只标了部分文字：只识别标注框内的文字，逐框与标注比较
            if recognizer is None and ocr_error is None:
                try:
                    log("初始化 PaddleOCR 文字识别（第一次运行会下载模型到 ~/.paddlex 或 ~/.paddleocr，离线服务器需提前放好）")
                    recognizer = TextRecognizer(device)
                except Exception as exc:
                    ocr_error = "OCR 初始化失败（需要在服务器上安装 paddleocr 和 paddlepaddle）: {}".format(exc)
            if recognizer is not None and regions:
                try:
                    texts = recognize_regions(recognizer, resolve(base_dir, merged["image"]), regions,
                                              merged.get("ref_image") and resolve(base_dir, merged["ref_image"]))
                    distance = length = exact = 0
                    ned_sum = 0.0
                    for box, text in zip(regions, texts):
                        d, n, same, ned = text_scores(text, box["transcription"])
                        distance, length, exact, ned_sum = distance + d, length + n, exact + same, ned_sum + ned
                    cer_distance += distance
                    cer_length += length
                    text_regions += len(regions)
                    text_exact += exact
                    text_ned += ned_sum
                    if "ocr_a" in metrics:
                        row["ocr_a"] = exact / float(len(regions))
                    if "cer" in metrics:
                        row["cer"] = distance / float(length) if length else float(distance > 0)
                    if "ned" in metrics:
                        row["ned"] = ned_sum / len(regions)
                    recognized = [{"transcription": t, "points": b["points"], "difficult": False}
                                  for b, t in zip(regions, texts)]
                    row["ocr_text"] = boxes_to_text(recognized)
                    row["ocr_regions"] = [{"ref": b["transcription"], "pred": t} for b, t in zip(regions, texts)]
                    ocr_lines.append(merged["image"] + "\t" + json.dumps(recognized, ensure_ascii=False))
                except Exception as exc:
                    skipped.append({"id": sample_id, "error": "OCR: {}".format(exc)})
        if needs_ocr:
            # 没有现成的识别文本，对预测图做 OCR
            if ocr is None and ocr_error is None:
                try:
                    log("初始化 PaddleOCR（第一次运行会下载模型到 ~/.paddlex 或 ~/.paddleocr，离线服务器需提前放好）")
                    ocr = PaddleOcr(device)
                except Exception as exc:
                    ocr_error = "OCR 初始化失败（需要在服务器上安装 paddleocr 和 paddlepaddle）: {}".format(exc)
            if ocr is not None:
                try:
                    boxes = ocr(resolve(base_dir, merged["image"]))
                    merged["text"] = boxes_to_text(boxes)
                    row["ocr_text"] = merged["text"]
                    ocr_lines.append(merged["image"] + "\t" + json.dumps(boxes, ensure_ascii=False))
                except Exception as exc:
                    skipped.append({"id": sample_id, "error": "OCR: {}".format(exc)})
        # OCR 不可用或失败的样本不计入文字指标
        if (wants_text and not by_region and merged.get("ref_text") is not None
                and not (needs_ocr and merged.get("text") is None)):
            distance, ref_len, exact, ned = text_scores(str(merged.get("text") or ""), str(merged["ref_text"]))
            cer_distance += distance
            cer_length += ref_len
            text_regions += 1
            text_exact += exact
            text_ned += ned
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
        now = time.time()
        if now - last_log >= PROGRESS_SECONDS or index + 1 == len(records):
            last_log = now
            log("已处理 {}/{}，用时 {:.0f} 秒".format(index + 1, len(records), now - started))
            write_progress(progress_path, stage, index + 1, len(records), now - started)
    if pool is not None:
        pool.shutdown()

    summary = {}
    for metric in metrics:
        if metric == "cer":
            # 语料级 CER：总编辑距离 / 参考文本总字符数
            summary[metric] = cer_distance / float(cer_length) if cer_length else None
        elif metric == "ocr_a":
            # 完全识别正确的区域占比
            summary[metric] = text_exact / float(text_regions) if text_regions else None
        elif metric == "ned":
            # 各区域 1-NED 的平均
            summary[metric] = text_ned / text_regions if text_regions else None
        else:
            summary[metric] = sums[metric] / counts[metric] if counts[metric] else None
        if counts[metric] == 0 and metric not in errors:
            errors[metric] = "没有可用于该指标的样本（检查 image/ref_image 或 ref_text 字段）"

    if not os.path.isdir(output_dir):
        os.makedirs(output_dir)
    if ocr_error:
        for metric in metrics:
            if metric in TEXT_METRICS and counts[metric] == 0:
                errors[metric] = ocr_error
    if ocr_lines:
        with open(os.path.join(output_dir, "ocr_results.txt"), "w", encoding="utf-8") as f:
            f.write("\n".join(ocr_lines) + "\n")
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
        "num_text_regions": text_regions,
    }
    result.update(extra or {})
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
        "--reference", action="append", default=[],
        help="可选，可给多个：参考值 jsonl、目录（按文件名配对）或 PaddleOCR 格式的文字标注文件"
    )
    parser.add_argument("--lq", default=None, help="可选，LQ 图片目录，按文件名与样本配对")
    parser.add_argument(
        "--lq-baseline", action="store_true", help="把 LQ 图片当作预测结果再评测一次，结果写入 metrics.json 的 lq 字段"
    )
    parser.add_argument("--device", default="cpu", help="LPIPS 使用的设备，如 cpu、cuda、npu")
    args = parser.parse_args()

    metrics = [m.strip() for m in args.metrics.split(",") if m.strip()]
    unknown = [m for m in metrics if m not in ALL_METRICS]
    if unknown:
        raise SystemExit("未知指标: {}".format(", ".join(unknown)))
    extra = None
    progress_path = os.path.join(args.output, "progress.json")
    if args.lq and args.lq_baseline:
        log_flush("计算 LQ 基线指标")
        lq = evaluate(args.lq, os.path.join(args.output, "lq"), metrics, args.reference, args.device,
                      progress_path=progress_path, stage="lq")
        extra = {"lq": {k: lq[k] for k in ("metrics", "counts", "errors", "num_samples", "num_skipped")}}
    result = evaluate(args.predictions, args.output, metrics, args.reference, args.device, lq_path=args.lq, extra=extra,
                      progress_path=progress_path)
    print(json.dumps(result["metrics"], ensure_ascii=False))
    if result["errors"]:
        print("部分指标未能计算: {}".format(json.dumps(result["errors"], ensure_ascii=False)), file=sys.stderr)


if __name__ == "__main__":
    main()

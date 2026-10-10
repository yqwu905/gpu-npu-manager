"""生成对比页的测试数据集（基准测试、Playwright 用）。

    python scripts/make_compare_dataset.py --out DIR [--sets 6 --images 20000 --distinct 40 --size 3840x2160
                                                      --big 12@7680x4320 --tiff 4] [--small]

每组结果集一个目录 DIR/setK/：images/、predictions.jsonl、meta.json。图片是带噪声的渐变（压缩率接近照片），
每组只生成 --distinct 张不同的图，再用硬链接铺满 --images 个文件名。另外：
- 每组 --big 张大图、--tiff 张 TIFF（浏览器不能直接显示，走转码和瓦片），各不相同；
- 序号是 1000 的倍数的文件为已知图案 px(x, y) = [(x*7 + seed) & 255, (y*5) & 255, ((x ^ y)*3) & 255]，seed 为组号，
  用来逐像素校验；
- 组号除 3 余 2 的组文件名带 _sr 后缀（Alt 文件名匹配）；每组 predictions.jsonl 有几条指向不存在的文件。
DIR/manifest.json 记录各组的目录、大图 / TIFF / 图案 / 缺失文件的序号。--small 为 3 组 × 2000 张，用于功能测试。
需要 numpy 和 Pillow（requirements-dev.txt）。
"""

import argparse
import json
import os
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
from PIL import Image

PATTERN_EVERY = 1000
MISSING_PER_SET = 5
STRIP = 256


def photo(w: int, h: int, seed: int) -> np.ndarray:
    """平滑的彩色渐变加高斯噪声，分条生成以限制内存（8K 约 100 MB）。"""
    rng = np.random.default_rng(seed)
    a = rng.uniform(0.5, 3.0, (3, 4)).astype(np.float32)
    out = np.empty((h, w, 3), np.uint8)
    x = np.arange(w, dtype=np.float32)[None, :] / w
    for y0 in range(0, h, STRIP):
        y = np.arange(y0, min(h, y0 + STRIP), dtype=np.float32)[:, None] / h
        for c in range(3):
            base = 128 + 70 * np.sin(np.pi * (x * a[c, 0] + a[c, 1])) * np.cos(np.pi * (y * a[c, 2] + a[c, 3]))
            base = base + 40 * (x - y) * (c - 1)
            noise = rng.standard_normal((y.shape[0], w), dtype=np.float32) * 6
            out[y0 : y0 + y.shape[0], :, c] = np.clip(base + noise, 0, 255)
    return out


def pattern(w: int, h: int, seed: int) -> np.ndarray:
    x = np.arange(w, dtype=np.int64)[None, :]
    y = np.arange(h, dtype=np.int64)[:, None]
    r = np.broadcast_to((x * 7 + seed) & 255, (h, w))
    g = np.broadcast_to((y * 5) & 255, (h, w))
    b = ((x ^ y) * 3) & 255
    return np.stack([r, g, b], -1).astype(np.uint8)


def write(job: tuple) -> str:
    kind, path, w, h, seed = job
    pixels = pattern(w, h, seed) if kind == "pattern" else photo(w, h, seed)
    image = Image.fromarray(pixels)
    if path.endswith(".tif"):
        image.save(path)
    else:
        image.save(path, compress_level=1)
    return path


def parse_size(text: str) -> tuple[int, int]:
    w, h = text.lower().split("x")
    return int(w), int(h)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--out", required=True)
    parser.add_argument("--sets", type=int, default=6)
    parser.add_argument("--images", type=int, default=20000)
    parser.add_argument("--distinct", type=int, default=40)
    parser.add_argument("--size", default="3840x2160")
    parser.add_argument("--big", default="12@7680x4320", help="每组大图数量@尺寸，0 表示不要")
    parser.add_argument("--tiff", type=int, default=4)
    parser.add_argument("--small", action="store_true", help="3 组 × 2000 张（10 张不同的图、2 张大图、2 张 TIFF）")
    parser.add_argument("--jobs", type=int, default=os.cpu_count() or 1)
    args = parser.parse_args()
    if args.small:
        args.sets, args.images, args.distinct, args.big, args.tiff = 3, 2000, 10, "2@7680x4320", 2
    w, h = parse_size(args.size)
    big_count, _, big_size = args.big.partition("@")
    big_count, (bw, bh) = int(big_count), parse_size(big_size or "7680x4320")
    out = Path(args.out).resolve()

    n = args.images
    spread = lambda count, offset: sorted({(i * n) // count + offset for i in range(count)}) if count else []  # noqa: E731
    patterns = list(range(0, n, PATTERN_EVERY))
    bigs = [i for i in spread(big_count, 7) if i < n and i % PATTERN_EVERY]
    tiffs = [i for i in spread(args.tiff, 13) if i < n and i % PATTERN_EVERY and i not in bigs]
    missing = [i for i in spread(MISSING_PER_SET, 3) if i < n and i % PATTERN_EVERY and i not in bigs + tiffs]

    jobs, links, manifest = [], [], {"size": [w, h], "big_size": [bw, bh], "sets": []}
    for k in range(args.sets):
        folder = out / f"set{k}"
        images = folder / "images"
        source = folder / "_distinct"
        images.mkdir(parents=True, exist_ok=True)
        source.mkdir(exist_ok=True)
        suffix = "_sr" if k % 3 == 2 else ""
        name = lambda i: f"img_{i:05d}{suffix}" + (".tif" if i in tiffs else ".png")  # noqa: E731
        for j in range(args.distinct):
            jobs.append(("photo", str(source / f"d{j:03d}.png"), w, h, k * 1000 + j))
        jobs.append(("pattern", str(source / "pattern.png"), w, h, k))
        for i in bigs:
            jobs.append(("photo", str(images / name(i)), bw, bh, 10**6 + k * 1000 + i))
        for i in tiffs:
            jobs.append(("photo", str(images / name(i)), w, h, 2 * 10**6 + k * 1000 + i))
        records = []
        for i in range(n):
            records.append({"id": f"{i:05d}", "image": f"images/{name(i)}"})
            if i in bigs or i in tiffs or i in missing:
                continue
            src = source / ("pattern.png" if i in patterns else f"d{(i * 7 + k) % args.distinct:03d}.png")
            links.append((src, images / name(i)))
        (folder / "predictions.jsonl").write_text("".join(json.dumps(r) + "\n" for r in records))
        (folder / "meta.json").write_text(json.dumps({"name": f"set{k}", "model": f"model-{k}", "dataset": "compare-bench"}))
        manifest["sets"].append({"path": str(folder), "seed": k, "suffix": suffix})

    print(f"生成 {len(jobs)} 张图片（{args.jobs} 个进程）…", flush=True)
    with ProcessPoolExecutor(args.jobs) as pool:
        for done, _ in enumerate(pool.map(write, jobs, chunksize=1), start=1):
            if done % 20 == 0 or done == len(jobs):
                print(f"  {done}/{len(jobs)}", flush=True)
    for src, dst in links:
        if not dst.exists():
            os.link(src, dst)
    manifest.update(patterns=patterns, bigs=bigs, tiffs=tiffs, missing=missing, images=n,
                    pattern="[(x*7 + seed) & 255, (y*5) & 255, ((x ^ y)*3) & 255]")
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    print(f"完成：{out}（{args.sets} 组 × {n} 张）")


if __name__ == "__main__":
    main()

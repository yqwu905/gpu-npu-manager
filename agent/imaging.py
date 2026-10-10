# 图片缩略图、预览、瓦片与磁盘缓存。只依赖标准库，装了 Pillow（>=7.0）时才能生成；中心服务也加载本文件
"""图片派生：缩略图（thumb）、预览（preview）、无损原图（full）和 512² 瓦片金字塔（tile）。

所有配置都通过构造参数传入，缺省时取 GNM_AGENT_* 环境变量；Agent 和中心服务用同一份代码，派生图逐字节一致。
"""
import hashlib
import io
import json
import logging
import os
import shutil
import struct
import threading
import time
from collections import OrderedDict

GEN, TILE, THUMB, PREVIEW_SIZES = 1, 512, 256, (1024, 2048, 3072)
NATIVE = ("png", "jpeg", "gif", "webp")  # bmp ≤ 8 MB 也算
# bmp 原样给浏览器、预览直接返回原图的文件大小上限
SMALL_BYTES = 8 << 20
# WebP 的最大边长，超过时无损编码改用 PNG
WEBP_MAX_SIDE = 16383
MAX_PIXELS = int(float(os.environ.get("GNM_AGENT_MAX_PIXELS") or 3e8))
# 只用这些解码器打开（MPO 由 JPEG 的解码器打开）：其他插件如 EPS 会对文件内容调用外部程序（Ghostscript）
FORMATS = ("PNG", "JPEG", "MPO", "GIF", "WEBP", "BMP", "TIFF", "PPM", "TGA", "JPEG2000")
_openers = None  # FORMATS 中本机 Pillow 能打开的
# 瓦片、原图用过的解码图在这么多秒内优先保留，不被只生成了预览的解码图挤出
TILED_SECONDS = 60
CONTENT_TYPES = {
    "png": "image/png", "jpeg": "image/jpeg", "gif": "image/gif", "webp": "image/webp", "bmp": "image/bmp",
    "tiff": "image/tiff",
}

try:
    if os.environ.get("GNM_AGENT_NO_PILLOW") == "1":
        raise ImportError("GNM_AGENT_NO_PILLOW=1")
    import PIL
    from PIL import Image, features as _features
    if tuple(int(p) for p in PIL.__version__.split(".")[:2]) < (7, 0):
        raise ImportError("Pillow 版本低于 7.0")
    PIL_VERSION = PIL.__version__
    WEBP = bool(_features.check("webp"))
    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    _BILINEAR = getattr(Image, "Resampling", Image).BILINEAR
except Exception:
    Image = None
    PIL_VERSION = None
    WEBP = False

log = logging.getLogger(__name__)

# EXIF 方向 -> 转正用的 transpose 方法名
_TRANSPOSE = {
    2: "FLIP_LEFT_RIGHT", 3: "ROTATE_180", 4: "FLIP_TOP_BOTTOM", 5: "TRANSPOSE",
    6: "ROTATE_270", 7: "TRANSVERSE", 8: "ROTATE_90",
}


class Busy(Exception):
    """排队的请求太多或等待超时，稍后重试。"""

    def __init__(self, retry_after=1):
        Exception.__init__(self, "图片生成繁忙，请稍后重试")
        self.retry_after = retry_after


class Unsupported(Exception):
    """没有可用的 Pillow。"""


class TooLarge(Exception):
    """像素数超过上限。"""


class BadImage(Exception):
    """无法解码。"""


class Gone(Exception):
    """客户端已断开，放弃还没开始解码的请求。"""


def _never():
    return False


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------


def version(st):
    """文件版本号：修改时间和大小的摘要，10 位十六进制。"""
    return hashlib.sha1(b"%d:%d" % (st.st_mtime_ns, st.st_size)).hexdigest()[:10]


def sniff(src):
    """按文件头的魔数判断格式：png / jpeg / gif / webp / bmp / tiff / other。src 为路径或已打开的二进制文件。"""
    if hasattr(src, "read"):
        pos = src.tell()
        head = src.read(16)
        src.seek(pos)
    else:
        with open(src, "rb") as f:
            head = f.read(16)
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    if head[:2] == b"BM":
        return "bmp"
    if head[:4] in (b"II*\0", b"MM\0*", b"II+\0", b"MM\0+"):
        return "tiff"
    return "other"


def is_native(fmt, size):
    """浏览器能否直接显示原图。"""
    return fmt in NATIVE or (fmt == "bmp" and size is not None and size <= SMALL_BYTES)


def bucket(size=None):
    """预览尺寸向上取到 1024 / 2048 / 3072，超过 3072 按 3072，缺省 2048。"""
    if size is None:
        return 2048
    for s in PREVIEW_SIZES:
        if size <= s:
            return s
    return PREVIEW_SIZES[-1]


def code(kind, size=None, l=None, x=None, y=None):
    """派生图代号：t256 / p2048 / f / l2x3y1。"""
    if kind == "thumb":
        return "t%d" % THUMB
    if kind == "preview":
        return "p%d" % bucket(size)
    if kind == "full":
        return "f"
    if kind == "tile":
        return "l%dx%dy%d" % (l, x, y)
    raise ValueError("未知的图片类型: {}".format(kind))


def etag(v, kind, size=None, l=None, x=None, y=None):
    """强 ETag（带引号）。kind 为 "raw" 时是原图（含原样返回的 full）"<v>"，否则为 "<v>.<代号>.<GEN>"。"""
    if kind == "raw":
        return '"%s"' % v
    return '"%s.%s.%d"' % (v, code(kind, size, l, x, y), GEN)


def level_dims(w, h, l):
    """第 l 层的尺寸（与 Image.reduce 一样向上取整）。"""
    f = 1 << l
    return (w + f - 1) // f, (h + f - 1) // f


def max_level(w, h):
    """最粗的层：长边不超过一个瓦片。"""
    l, m = 0, max(w, h)
    while (TILE << l) < m:
        l += 1
    return l


def check_tile(w, h, l, x, y):
    """瓦片坐标超出范围时抛出 ValueError。"""
    if not 0 <= l <= max_level(w, h):
        raise ValueError("瓦片层级超出范围")
    wl, hl = level_dims(w, h, l)
    if not (0 <= x < (wl + TILE - 1) // TILE and 0 <= y < (hl + TILE - 1) // TILE):
        raise ValueError("瓦片坐标超出范围")


def fit(w, h, s):
    """长边缩到不超过 s（不放大）后的尺寸。"""
    if max(w, h) <= s:
        return w, h
    if w >= h:
        return s, max(1, int(round(h * s / float(w))))
    return max(1, int(round(w * s / float(h)))), s


def _env_int(name, default):
    try:
        return int(float(os.environ.get(name) or default))
    except ValueError:
        return default


def _mem_total_mb():
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        pass
    return 16384


def default_cache_dir():
    return os.environ.get("GNM_AGENT_CACHE_DIR") or os.path.join(
        os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"), "gnm-agent", "img"
    )


# ---------------------------------------------------------------------------
# 解码
# ---------------------------------------------------------------------------


class Info(object):
    """只读文件头得到的信息：w / h 为按 EXIF 方向转正后的原图尺寸，stored 为文件里存储的尺寸。"""

    def __init__(self, w, h, stored, orientation, fmt, size, mode):
        self.w, self.h, self.stored, self.orientation = w, h, stored, orientation
        self.fmt, self.size, self.mode = fmt, size, mode


def _orientation(im):
    try:
        # PNG 的 eXIf 块可能在图像数据之后，读它要整图解码，这里只认图像数据之前的
        if im.format == "PNG" and "exif" not in im.info:
            return 1
        o = im.getexif().get(0x0112, 1)
        return o if o in _TRANSPOSE else 1
    except Exception:
        return 1


def _header(im, path, max_pixels):
    w, h = im.size
    limit = max_pixels or Image.MAX_IMAGE_PIXELS
    if limit and w * h > limit:
        raise TooLarge("图片有 {}×{} 像素，超过上限 {}".format(w, h, limit))
    o = _orientation(im)
    return Info(h if o >= 5 else w, w if o >= 5 else h, (w, h), o, sniff(path), os.path.getsize(path), im.mode)


def _pillow_errors(func):
    """把 Pillow 的各种异常归为 TooLarge / BadImage。"""

    def wrapper(*args, **kwargs):
        if Image is None:
            raise Unsupported("没有可用的 Pillow（>=7.0）")
        try:
            return func(*args, **kwargs)
        except (TooLarge, Unsupported, BadImage):
            raise
        except Image.DecompressionBombError as exc:
            raise TooLarge(str(exc))
        except MemoryError:
            raise TooLarge("内存不足")
        except Exception as exc:
            raise BadImage("无法解码: {}".format(exc))

    return wrapper


def open_image(src):
    """Image.open，但只按 FORMATS 里的格式打开（Pillow < 8 没有 formats 参数，打开后再检查）。src 为路径或二进制文件。"""
    global _openers
    if _openers is None:
        Image.init()
        _openers = tuple(f for f in FORMATS if f in Image.OPEN)
    try:
        im = Image.open(src, formats=_openers)
    except TypeError:
        im = Image.open(src)
    if im.format not in FORMATS:
        im.close()
        raise BadImage("不支持的图片格式: {}".format(im.format))
    return im


@_pillow_errors
def probe(path, max_pixels=None):
    """只读文件头，返回 Info。"""
    with open_image(path) as im:
        return _header(im, path, max_pixels)


def draft_scale(info, target):
    """JPEG 按长边 target 缩小解码时的倍数（1 / 2 / 4 / 8，与 Pillow 的 draft 一致）。"""
    tw, th = fit(info.w, info.h, target)
    if info.orientation >= 5:
        tw, th = th, tw
    scale = min(info.stored[0] // tw, info.stored[1] // th)
    for s in (8, 4, 2):
        if scale >= s:
            return s
    return 1


def _normalize(im):
    """固定的模式转换，结果为 RGB / RGBA / L；返回 (图片, 是否按本图最大值缩放过)。"""
    mode = im.mode
    if mode in ("RGB", "RGBA", "L"):
        return im, False
    if mode == "1":
        return im.convert("L"), False
    if mode.startswith("I;16"):
        return im.convert("I").point(lambda v: v * (1 / 257.0) + 0.5).convert("L"), False
    if mode in ("I", "F"):
        hi = im.getextrema()[1]
        if mode == "F" and hi <= 1:
            k = 255.0
        elif hi <= 255:
            k = 1.0
        elif hi <= 65535:
            k = 1 / 257.0
        else:
            k = 255.0 / hi
        return im.point(lambda v: v * k + 0.5).convert("L"), hi > 65535
    alpha = mode in ("LA", "PA", "La", "RGBa") or (mode == "P" and "transparency" in im.info)
    return im.convert("RGBA" if alpha else "RGB"), False


class Display(object):
    """8 位显示图像（第 0 层）及按需生成的缩小层；w / h 为原图尺寸（缩小解码时 im 比它小）。"""

    def __init__(self, im, info, icc, normalized):
        self.im, self.w, self.h, self.fmt, self.size = im, info.w, info.h, info.fmt, info.size
        self.icc, self.normalized = icc, normalized
        self.drafted = im.size != (info.w, info.h)
        self.tiled = None  # 最近一次用来切瓦片或转码原图的时间
        self.levels = {0: im}
        self._levels_lock = threading.Lock()
        # 多个线程编码同一个图片对象时要串行（Image.save 会改写图片对象的属性）
        self.save_lock = threading.Lock()
        self.cost = im.size[0] * im.size[1] * (1 if im.mode == "L" else 4) * 4 // 3

    def level(self, l):
        im = self.levels.get(l)
        if im is None:
            with self._levels_lock:
                im = self.levels.get(l)
                if im is None:
                    im = self.levels[l] = self.im.reduce(1 << l)
        return im

    def hdr(self, ct, lossless):
        return {
            "ct": ct, "w": self.w, "h": self.h, "fmt": self.fmt, "native": int(is_native(self.fmt, self.size)),
            "lossless": lossless, "normalized": int(self.normalized),
        }


@_pillow_errors
def open_display(path, draft_target=None, max_pixels=None):
    """解码成显示图像：第 0 帧，JPEG 可按长边 draft_target 缩小解码，按 EXIF 方向转正，固定的模式转换。"""
    with open_image(path) as im:
        info = _header(im, path, max_pixels)
        im.seek(0)
        if draft_target and info.fmt == "jpeg" and draft_scale(info, draft_target) > 1:
            tw, th = fit(info.w, info.h, draft_target)
            im.draft("RGB", (th, tw) if info.orientation >= 5 else (tw, th))
        im.load()
        icc = im.info.get("icc_profile")
        out = im
        if info.orientation in _TRANSPOSE:
            out = im.transpose(getattr(getattr(Image, "Transpose", Image), _TRANSPOSE[info.orientation]))
    out, normalized = _normalize(out)
    return Display(out, info, icc, normalized)


# ---------------------------------------------------------------------------
# 编码
# ---------------------------------------------------------------------------


def _icc_for(icc, mode):
    """ICC 的色彩空间与图片模式一致时才保留（CMYK 转出的 RGB、灰度转出的 RGB 不带）。"""
    if icc and icc[16:20] == (b"GRAY" if mode == "L" else b"RGB "):
        return icc
    return None


def _save(im, fmt, icc, **opts):
    icc = _icc_for(icc, im.mode)
    if icc:
        opts["icc_profile"] = icc
    buf = io.BytesIO()
    im.save(buf, fmt, **opts)
    return "image/" + fmt.lower(), buf.getvalue()


def encode_lossy(im, icc, quality, subsampling):
    """有损编码（缩略图、预览）：不透明用 JPEG，有透明度用 WebP（没有 WebP 时用 PNG）。返回 (Content-Type, 内容)。"""
    if im.mode == "RGBA" and im.getchannel("A").getextrema()[0] == 255:
        im = im.convert("RGB")
    if im.mode in ("RGB", "L"):
        return _save(im, "JPEG", icc, quality=quality, subsampling=subsampling, optimize=False, progressive=False)
    if WEBP:
        return _save(im, "WEBP", icc, quality=quality, method=2)
    return _save(im, "PNG", icc, compress_level=1)


def encode_lossless(im, icc):
    """无损编码（瓦片、转码的原图、无损预览）：WebP 无损（method 0），没有 WebP 或边长超过 16383 时用 PNG。"""
    if WEBP and max(im.size) <= WEBP_MAX_SIDE:
        if im.mode == "L":  # WebP 不支持灰度
            im = im.convert("RGB")
        return _save(im, "WEBP", icc, lossless=True, method=0, quality=50, exact=True)
    return _save(im, "PNG", icc, compress_level=1)


def _resize(d, src, s, gap):
    """把 src（d 的显示图像或由它缩出的图）缩到原图长边为 s 时的尺寸；尺寸不变时返回副本。"""
    target = fit(d.w, d.h, s)
    if src.size == target:
        return src.copy()
    return src.resize(target, _BILINEAR, reducing_gap=gap)


def render_thumb(d, src=None):
    """缩略图：长边不超过 256，不透明 JPEG q80 4:2:0。src 为已缩小的图（如预览）时从它生成。"""
    ct, data = encode_lossy(_resize(d, d.im if src is None else src, THUMB, 3.0), d.icc, 80, 2)
    return d.hdr(ct, 0), data


def render_preview(d, size):
    """预览：长边不超过 size，不透明 JPEG q90 4:4:4；原图不超过 size 时无损。返回 (头信息, 内容, 缩略图)。"""
    if max(d.w, d.h) <= size:
        with d.save_lock:
            ct, data = encode_lossless(d.im, d.icc)
        return d.hdr(ct, 1), data, render_thumb(d)
    out = _resize(d, d.im, size, 2.0)
    ct, data = encode_lossy(out, d.icc, 90, 0)
    return d.hdr(ct, 0), data, render_thumb(d, out)


def render_full(d):
    """原分辨率无损转码（TIFF、大 BMP 等浏览器不能直接显示的格式）。"""
    with d.save_lock:
        ct, data = encode_lossless(d.im, d.icc)
    return d.hdr(ct, 1), data


def render_tile(d, l, x, y):
    """第 l 层（第 0 层的 reduce(2**l)）的 512² 瓦片 (x, y)，边缘的瓦片更小。"""
    check_tile(d.w, d.h, l, x, y)
    lv = d.level(l)
    x0, y0 = x * TILE, y * TILE
    ct, data = encode_lossless(lv.crop((x0, y0, min(lv.size[0], x0 + TILE), min(lv.size[1], y0 + TILE))), d.icc)
    return d.hdr(ct, 1), data


# ---------------------------------------------------------------------------
# 磁盘缓存
# ---------------------------------------------------------------------------

MAGIC = b"GNMC"


class DiskCache(object):
    """派生图的磁盘缓存：<dir>/<k[:2]>/<k>.bin，内容为 GNMC + 头长度 + 头 JSON + 内容。

    按修改时间近似 LRU；后台线程每 5 分钟（或写入超过容量的 10% 后）清理，超过容量时删到 80%。
    剩余空间不足 1 GB 或 5% 时不再写入，但仍可读取。
    """

    EVICT_SECONDS = 300
    TMP_SECONDS = 3600
    TOUCH_SECONDS = 3600

    def __init__(self, directory, cap_mb):
        self.dir = directory
        self.cap = int(cap_mb) << 20
        self.min_free = 1 << 30
        self.min_free_ratio = 0.05
        self._written = 0
        self._space = (0.0, True)
        self._wake = threading.Event()
        self._thread = None
        self._lock = threading.Lock()

    @staticmethod
    def key(key_base, kind, params=""):
        text = "%s\0%s\0%s\0%d" % (key_base, kind, params, GEN)
        return hashlib.sha1(text.encode("utf-8", "surrogateescape")).hexdigest()

    def path(self, key):
        return os.path.join(self.dir, key[:2], key + ".bin")

    def exists(self, key):
        return os.path.isfile(self.path(key))

    def open(self, key):
        """命中时返回 (头信息, 已打开并定位到内容的文件, 内容偏移, 内容长度)，文件由调用方关闭；否则返回 None。"""
        path = self.path(key)
        try:
            f = io.open(path, "rb")
        except OSError:
            return None
        try:
            head = f.read(8)
            if len(head) != 8 or head[:4] != MAGIC:
                raise ValueError("缓存文件损坏")
            n = struct.unpack(">I", head[4:])[0]
            hdr = json.loads(f.read(n).decode("utf-8"))
            st = os.fstat(f.fileno())
            if time.time() - st.st_mtime > self.TOUCH_SECONDS:
                try:
                    os.utime(path)
                except OSError:
                    pass
            return hdr, f, 8 + n, st.st_size - 8 - n
        except (OSError, ValueError):
            f.close()
            return None

    def get(self, key):
        """命中时返回 (头信息, 路径, 内容偏移, 内容长度)。"""
        got = self.open(key)
        if got is None:
            return None
        got[1].close()
        return got[0], self.path(key), got[2], got[3]

    def put(self, key, hdr, payload):
        """写入一项，空间不足或出错时跳过，返回是否写入。"""
        if self.cap <= 0 or self.low_space():
            return False
        path = self.path(key)
        head = json.dumps(hdr, separators=(",", ":")).encode("utf-8")
        tmp = "%s.%d.%d.tmp" % (path, os.getpid(), threading.get_ident())
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with io.open(tmp, "wb") as f:
                f.write(MAGIC + struct.pack(">I", len(head)) + head)
                f.write(payload)
            os.replace(tmp, path)
        except OSError as exc:
            log.warning("写入图片缓存失败: %s", exc)
            try:
                os.remove(tmp)
            except OSError:
                pass
            return False
        with self._lock:
            self._written += len(payload) + len(head) + 8
            if self._thread is None:
                self._thread = threading.Thread(target=self._loop, name="gnm-image-cache", daemon=True)
                self._thread.start()
            if self._written > self.cap // 10:
                self._wake.set()
        return True

    def low_space(self):
        """剩余空间不足（结果缓存 5 秒）。"""
        checked, low = self._space
        now = time.monotonic()
        if now - checked > 5 or not checked:
            try:
                os.makedirs(self.dir, exist_ok=True)
                u = shutil.disk_usage(self.dir)
                low = u.free < self.min_free or u.free < u.total * self.min_free_ratio
            except OSError:
                low = True
            self._space = (now, low)
        return low

    def evict(self):
        """删除 1 小时前的临时文件；超过容量时按修改时间从旧到新删除，直到不超过容量的 80%。返回清理后的占用字节数。"""
        now, total, entries = time.time(), 0, []
        try:
            subs = os.listdir(self.dir)
        except OSError:
            return 0
        for sub in subs:
            folder = os.path.join(self.dir, sub)
            if len(sub) != 2 or not os.path.isdir(folder):
                continue
            try:
                names = os.listdir(folder)
            except OSError:
                continue
            for name in names:
                p = os.path.join(folder, name)
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                if name.endswith(".tmp"):
                    if now - st.st_mtime > self.TMP_SECONDS:
                        try:
                            os.remove(p)
                        except OSError:
                            pass
                elif name.endswith(".bin"):
                    entries.append((st.st_mtime, st.st_size, p))
                    total += st.st_size
        with self._lock:
            self._written = 0
        if total > self.cap:
            entries.sort()
            for _, size, p in entries:
                if total <= self.cap * 0.8:
                    break
                try:
                    os.remove(p)
                    total -= size
                except OSError:
                    pass
        return total

    def _loop(self):
        while True:
            self._wake.wait(self.EVICT_SECONDS)
            self._wake.clear()
            try:
                self.evict()
            except Exception:
                log.exception("清理图片缓存失败")


# ---------------------------------------------------------------------------
# 并发控制
# ---------------------------------------------------------------------------


class Budget(object):
    """加权信号量（内存预算按 MB 给出，unit=1 时为 CPU 槽位）。先到先得（urgent 的排在普通的前面）；排队的超过
    max_waiting 个时直接抛出 Busy，排队超过 timeout 秒也抛出 Busy；每 0.5 秒检查一次 gone()，返回真时抛出 Gone。"""

    def __init__(self, mem_mb, max_waiting=64, timeout=30, unit=1 << 20):
        self.total = max(1, int(mem_mb * unit))
        self.used = 0
        self.max_waiting = max_waiting
        self.timeout = timeout
        self._cond = threading.Condition()
        self._queue = []
        self._urgent = 0  # 队首 urgent 的个数

    @staticmethod
    def cost(w, h, bpp=4):
        """解码和缩放的工作内存估计。"""
        return w * h * bpp * 2 + (16 << 20)

    @property
    def waiting(self):
        return len(self._queue)

    def acquire(self, cost=1, gone=None, urgent=False, limit=None):
        """返回实际占用的量（超过总量的按总量算），释放时传回 release。
        urgent 的请求（已解码、占着内存的）排在普通请求前面（彼此仍先到先得），不受排队数量和时间的限制；
        limit 为这次占用后总占用的上限（给别的请求留出余量）。"""
        cost = min(cost, self.total)
        cap = self.total if limit is None else max(cost, min(limit, self.total))
        with self._cond:
            if not self._queue and self.used + cost <= cap:
                self.used += cost
                return cost
            if not urgent and len(self._queue) >= self.max_waiting:
                raise Busy(1)
            ticket = object()
            if urgent:
                self._queue.insert(self._urgent, ticket)
                self._urgent += 1
            else:
                self._queue.append(ticket)
            deadline = time.monotonic() + (1e9 if urgent else self.timeout)
            try:
                while not (self._queue[0] is ticket and self.used + cost <= cap):
                    left = deadline - time.monotonic()
                    if left <= 0:
                        raise Busy(1)
                    if gone is not None and gone():
                        raise Gone()
                    self._cond.wait(min(0.5, left))
                self.used += cost
                return cost
            finally:
                self._queue.remove(ticket)
                if urgent:
                    self._urgent -= 1
                self._cond.notify_all()

    def release(self, cost=1):
        with self._cond:
            self.used -= cost
            self._cond.notify_all()


class _Flight(object):
    """正在进行的生成或解码；完成后 result / error 之一有值，预览还会带上顺带生成的缩略图。"""

    def __init__(self):
        self.done = threading.Event()
        self.result = self.error = self.thumb = None

    def wait(self, gone):
        while not self.done.wait(0.5):
            if gone():
                raise Gone()


class Result(object):
    """render 的结果：hdr 为头信息（ct, w, h, fmt, native, lossless, normalized, ms），
    内容在 data 中，或在已打开的文件 file 的 [offset, offset + length) 中；cache 为 hit / miss / join。"""

    def __init__(self, hdr, data=None, file=None, offset=0, length=None, cache="miss"):
        self.hdr, self.data, self.file, self.offset, self.cache = hdr, data, file, offset, cache
        self.length = len(data) if data is not None else length
        self.ms = 0.0

    def read(self):
        if self.data is None:
            with self.file:
                self.file.seek(self.offset)
                self.data = self.file.read(self.length)
            self.file = None
        return self.data

    def close(self):
        if self.file is not None:
            self.file.close()
            self.file = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def _noop():
    pass


class Service(object):
    """生成派生图：磁盘缓存、同一派生图只生成一次、同一张图只整图解码一次（解码结果按 LRU 保留）、
    CPU 槽位（解码最多 workers 个，加上编码最多 workers + 1 个）和内存预算排队、客户端断开时放弃还没开始解码的请求。

    参数缺省时取环境变量 GNM_AGENT_CACHE_DIR / GNM_AGENT_CACHE_MB / GNM_AGENT_IMAGE_WORKERS /
    GNM_AGENT_IMAGE_MEM_MB / GNM_AGENT_DECODED_MB / GNM_AGENT_MAX_PIXELS。
    """

    def __init__(self, cache_dir=None, cache_mb=None, workers=None, mem_mb=None, decoded_mb=None,
                 max_pixels=None, max_waiting=64, queue_timeout=30):
        mem = min(1024, _mem_total_mb() // 16)
        if cache_mb is None:
            cache_mb = _env_int("GNM_AGENT_CACHE_MB", 2048)
        if workers is None:
            workers = _env_int("GNM_AGENT_IMAGE_WORKERS", max(2, min(6, (os.cpu_count() or 1) // 4)))
        if mem_mb is None:
            mem_mb = _env_int("GNM_AGENT_IMAGE_MEM_MB", mem)
        if decoded_mb is None:
            # Pillow 的 RGB 每像素占 4 字节，一张 8K 解码图（含金字塔）约 177 MB：2 GB 约 11 张，6 组 8K 同屏时不会互相挤出
            decoded_mb = _env_int("GNM_AGENT_DECODED_MB", min(2048, _mem_total_mb() // 8))
        self.disk = DiskCache(cache_dir or default_cache_dir(), cache_mb)
        self.workers = max(1, workers)
        self.max_pixels = max_pixels or MAX_PIXELS
        # 解码和编码共用 workers + 1 个 CPU 槽位，解码最多占 workers 个：已解码的图切瓦片、缩放、编码只要几十到几百毫秒，
        # 排在解码前面且总有一个槽位，不等别的图 1 秒多的整图解码（放大平移时新露出的瓦片、刚解码完那张的瓦片）
        self.cpu = Budget(self.workers + 1, max_waiting, queue_timeout, unit=1)
        # 缩略图的整图解码（PNG、TIFF 等不能缩小解码）最多 workers - 1 个，在占内存之前排队：
        # 缩略图栏滚动时不占满解码槽位和内存，当前图的预览、瓦片不用排在一屏冷缩略图后面
        self.thumbs = Budget(max(1, self.workers - 1), max_waiting, queue_timeout, unit=1)
        self.memory = Budget(mem_mb, max_waiting, queue_timeout)
        self.decoded_cap = decoded_mb << 20
        self._lock = threading.Lock()
        self._inflight = {}  # 派生图缓存键 -> _Flight
        self._previews = {}  # key_base -> 正在生成的预览
        self._sources = {}  # key_base -> 正在整图解码
        self._decoded = OrderedDict()  # key_base -> Display
        self._decoded_bytes = 0

    def render(self, path, key_base, kind, params=None, gone=None):
        """返回 Result。key_base 标识源文件的版本（如 "路径\\0大小\\0mtime_ns"），params 为 {"size"} 或 {"l", "x", "y"}。"""
        if Image is None:
            raise Unsupported("没有可用的 Pillow（>=7.0）")
        gone = gone or _never
        params = dict(params or {})
        if kind == "preview":
            params = {"size": bucket(params.get("size"))}
        elif kind == "tile":
            params = {"l": int(params["l"]), "x": int(params["x"]), "y": int(params["y"])}
        else:
            params = {}
        c = code(kind, **params)
        key = self.disk.key(key_base, kind, c)
        t0 = time.monotonic()
        res = self._hit(key)
        if res is None:
            res = self._miss(path, key_base, kind, params, key, gone)
        res.ms = (time.monotonic() - t0) * 1000
        return res

    def decoded(self, key_base):
        with self._lock:
            d = self._decoded.get(key_base)
            if d is not None:
                self._decoded.move_to_end(key_base)
            return d

    def _hit(self, key):
        got = self.disk.open(key)
        if got is None:
            return None
        hdr, f, offset, length = got
        return Result(hdr, file=f, offset=offset, length=length, cache="hit")

    def _miss(self, path, key_base, kind, params, key, gone):
        info = None
        if kind in ("preview", "tile", "full"):
            info = self.decoded(key_base) or probe(path, self.max_pixels)
            if kind == "tile":
                check_tile(info.w, info.h, params["l"], params["x"], params["y"])
            elif is_native(info.fmt, info.size) and (
                kind == "full" or (max(info.w, info.h) <= params["size"] and info.size <= SMALL_BYTES)
            ):
                # 浏览器能直接显示的原图，以及小图的预览，直接返回原图
                hdr = {"ct": CONTENT_TYPES[info.fmt], "w": info.w, "h": info.h, "fmt": info.fmt, "native": 1,
                       "lossless": 1, "normalized": 0, "ms": 0}
                return Result(hdr, file=io.open(path, "rb"), length=info.size, cache="hit")
            if isinstance(info, Display):
                info = None
        while True:
            with self._lock:
                flight = self._inflight.get(key)
                owner = flight is None
                if owner:
                    flight = self._inflight[key] = _Flight()
                    if kind == "preview":
                        self._previews[key_base] = flight
                    pending = self._previews.get(key_base) if kind == "thumb" else None
            if owner:
                break
            flight.wait(gone)
            if flight.result is not None:
                return Result(flight.result[0], data=flight.result[1], cache="join")
            if not isinstance(flight.error, Gone):
                raise flight.error
            # 生成它的请求已取消，自己来
        try:
            if pending is not None:
                # 同一张图正在生成预览，预览会顺带生成缩略图
                pending.wait(gone)
                if pending.thumb is not None:
                    flight.result = pending.thumb
                    return Result(pending.thumb[0], data=pending.thumb[1], cache="join")
            t0 = time.monotonic()
            hdr, data, thumb = self._generate(path, key_base, kind, params, info, gone)
            hdr["ms"] = round((time.monotonic() - t0) * 1000, 1)
            self.disk.put(key, hdr, data)
            if thumb is not None:
                thumb[0]["ms"] = hdr["ms"]
                tkey = self.disk.key(key_base, "thumb", code("thumb"))
                if not self.disk.exists(tkey):
                    self.disk.put(tkey, thumb[0], thumb[1])
                flight.thumb = thumb
            flight.result = (hdr, data)
            return Result(hdr, data=data, cache="miss")
        except BaseException as exc:
            flight.error = exc
            raise
        finally:
            with self._lock:
                self._inflight.pop(key, None)
                if self._previews.get(key_base) is flight:
                    del self._previews[key_base]
            flight.done.set()

    def _generate(self, path, key_base, kind, params, info, gone):
        """返回 (头信息, 内容, 顺带生成的缩略图或 None)。"""
        draft = {"thumb": THUMB * 3, "preview": params.get("size", 0) * 2}.get(kind)
        d, release = self._source(path, key_base, kind, draft, info, gone)
        try:
            # 已经解码，客户端断开也做完并写入缓存
            self.cpu.acquire(1, urgent=True)
            try:
                if kind == "thumb":
                    return render_thumb(d) + (None,)
                if kind == "preview":
                    return render_preview(d, params["size"])
                if kind == "full":
                    return render_full(d) + (None,)
                return render_tile(d, params["l"], params["x"], params["y"]) + (None,)
            finally:
                self.cpu.release(1)
        finally:
            release()

    def _source(self, path, key_base, kind, draft, info, gone):
        """取显示图像，返回 (Display, 释放内存预算的函数)：优先用已解码的整图，JPEG 能缩小解码时缩小解码，
        否则整图解码（同一张图同时只解码一次）。缩略图的整图解码先占缩略图槽位。"""
        slot = False
        try:
            while True:
                with self._lock:
                    d = self._decoded.get(key_base)
                    flight = self._sources.get(key_base)
                if slot and (d is not None or flight is not None):  # 用别的解码结果，不必占着槽位
                    self.thumbs.release(1)
                    slot = False
                if d is not None:
                    self._keep(key_base, d, kind)
                    return d, _noop
                if flight is not None:
                    flight.wait(gone)
                    if flight.result is not None:
                        self._keep(key_base, flight.result, kind)
                        return flight.result, _noop
                    if not isinstance(flight.error, Gone):
                        raise flight.error
                    continue
                if draft:
                    info = info or probe(path, self.max_pixels)
                    if info.fmt == "jpeg" and draft_scale(info, draft) > 1:
                        return self._decode(path, draft, info, gone)
                if kind == "thumb" and not slot:
                    slot = self._thumb_slot(key_base, gone)
                    continue  # 排队时可能已有了这张图的解码
                with self._lock:
                    if key_base in self._sources or key_base in self._decoded:
                        continue
                    flight = self._sources[key_base] = _Flight()
                try:
                    d, release = self._decode(path, None, info, gone)
                    self._keep(key_base, d, kind)
                    flight.result = d
                    return d, release
                except BaseException as exc:
                    flight.error = exc
                    raise
                finally:
                    with self._lock:
                        self._sources.pop(key_base, None)
                    flight.done.set()
        finally:
            if slot:
                self.thumbs.release(1)

    def _thumb_slot(self, key_base, gone):
        """占一个缩略图解码槽位返回 True；排队时这张图开始了别的解码（如预览）或已解码时返回 False，改用它。"""
        other = lambda: key_base in self._sources or key_base in self._decoded
        try:
            self.thumbs.acquire(1, lambda: other() or gone())
        except Gone:
            if gone():
                raise
            return False
        return True

    def _decode(self, path, draft, info, gone):
        if gone():
            raise Gone()
        info = info or probe(path, self.max_pixels)
        s = draft_scale(info, draft) if draft and info.fmt == "jpeg" else 1
        cost = self.memory.acquire(Budget.cost(-(-info.w // s), -(-info.h // s)), gone)
        try:
            self.cpu.acquire(1, gone, limit=self.workers)
            try:
                if gone():
                    raise Gone()
                d = open_display(path, draft, self.max_pixels)
            finally:
                self.cpu.release(1)
        except BaseException:
            self.memory.release(cost)
            raise
        return d, lambda: self.memory.release(cost)

    def _keep(self, key_base, d, kind):
        """解码图放入 LRU 或移到最新（缩小解码的、比容量还大的不放）。只为缩略图解码的不放入、也不刷新，
        免得滚动缩略图栏挤出正在放大查看的原图；超出容量时先删 TILED_SECONDS 秒内没切过瓦片的。"""
        if kind == "thumb" or d.drafted or d.cost > self.decoded_cap:
            return
        with self._lock:
            now = time.monotonic()
            if kind in ("tile", "full"):
                d.tiled = now
            old = self._decoded.pop(key_base, None)
            if old is not None:
                self._decoded_bytes -= old.cost
            self._decoded[key_base] = d
            self._decoded_bytes += d.cost
            while self._decoded_bytes > self.decoded_cap:
                victim = next(
                    (k for k, e in self._decoded.items() if e.tiled is None or now - e.tiled > TILED_SECONDS), None
                )
                if victim is None:
                    victim = next(iter(self._decoded))
                self._decoded_bytes -= self._decoded.pop(victim).cost

"""agent/imaging.py 的单元测试：缩略图、预览、瓦片、模式转换、磁盘缓存和内存预算。"""
import io
import os
import threading
import time

import numpy as np
import pytest
from PIL import Image

import imaging

pytestmark = pytest.mark.skipif(imaging.Image is None, reason="没有 Pillow")

# 色彩空间为 RGB 的假 ICC（Pillow 原样写入和读回，不做校验）
ICC = b"\0" * 16 + b"RGB " + b"\0" * 108


def photo(w, h, mode="RGB"):
    """渐变加异或纹理，像素值可预测。"""
    yy, xx = np.mgrid[0:h, 0:w]
    bands = [xx * 255 // max(1, w - 1), yy * 255 // max(1, h - 1), (xx ^ yy) & 255]
    if mode == "RGBA":
        bands.append(np.full((h, w), 255))
    return Image.fromarray(np.stack(bands, -1).astype("uint8"), mode)


@pytest.fixture
def svc(tmp_path):
    return imaging.Service(cache_dir=str(tmp_path / "cache"), cache_mb=64, workers=2, mem_mb=256, decoded_mb=256)


def render(svc, path, kind, **params):
    st = os.stat(path)
    key_base = "%s\0%d\0%d" % (path, st.st_size, st.st_mtime_ns)
    res = svc.render(str(path), key_base, kind, params)
    return res.hdr, res.read(), res.cache


def decode(data):
    im = Image.open(io.BytesIO(data))
    im.load()
    return im


def test_thumb_and_preview(svc, tmp_path):
    path = tmp_path / "a.png"
    photo(1000, 600).save(path)
    hdr, data, cache = render(svc, path, "thumb")
    im = decode(data)
    assert cache == "miss" and hdr["ct"] == "image/jpeg" and im.format == "JPEG"
    assert max(im.size) == 256 and abs(im.size[1] - 600 * 256 / 1000) <= 1
    assert (hdr["w"], hdr["h"], hdr["lossless"], hdr["fmt"], hdr["native"]) == (1000, 600, 0, "png", 1)
    assert render(svc, path, "thumb")[2] == "hit"

    for size in (2048, 900):  # 900 向上取到 1024；原图更小，原样返回
        hdr, data, _ = render(svc, path, "preview", size=size)
        assert data == path.read_bytes() and hdr["lossless"] == 1 and (hdr["w"], hdr["h"]) == (1000, 600)

    big = tmp_path / "big.png"
    photo(2500, 1200).save(big)
    hdr, data, _ = render(svc, big, "preview", size=1024)
    im = decode(data)
    assert im.format == "JPEG" and max(im.size) <= 1024 and hdr["lossless"] == 0

    rgba = photo(600, 300, "RGBA")
    rgba.putpixel((0, 0), (1, 2, 3, 0))
    rgba.save(tmp_path / "alpha.png")
    hdr, data, _ = render(svc, tmp_path / "alpha.png", "thumb")
    im = decode(data)
    assert im.format in ("WEBP", "PNG") and im.mode == "RGBA" and hdr["ct"] != "image/jpeg"


def test_preview_writes_thumb(svc, tmp_path):
    path = tmp_path / "big.png"
    photo(2500, 1200).save(path)
    render(svc, path, "preview", size=2048)
    hdr, data, cache = render(svc, path, "thumb")
    assert cache == "hit" and max(decode(data).size) == 256


def test_lossless_preview_of_non_native(svc, tmp_path):
    path = tmp_path / "a.tif"
    src = photo(300, 200)
    src.save(path)
    hdr, data, _ = render(svc, path, "preview", size=2048)
    assert hdr["lossless"] == 1 and hdr["fmt"] == "tiff" and hdr["native"] == 0
    assert np.array_equal(np.asarray(decode(data).convert("RGB")), np.asarray(src))


def make_mode(mode, values, size=(4, 1)):
    im = Image.new(mode, size)
    for i, v in enumerate(values):
        im.putpixel((i, 0), v)
    return im


def display_of(tmp_path, im, name="m.tif"):
    path = tmp_path / name
    im.save(path)
    return imaging.open_display(str(path))


def test_mode_normalization(tmp_path):
    d = display_of(tmp_path, make_mode("I;16", [0, 257, 65535, 386]), "i16.png")
    assert d.im.mode == "L" and list(d.im.tobytes()) == [0, 1, 255, 2] and not d.normalized

    d = display_of(tmp_path, make_mode("F", [0.0, 0.5, 1.0, 0.2]), "f.tif")
    assert list(d.im.tobytes()) == [0, 128, 255, 51] and not d.normalized

    d = display_of(tmp_path, make_mode("I", [0, 100000, 200000, 50000]), "i32.tif")
    assert list(d.im.tobytes()) == [0, 128, 255, 64] and d.normalized

    d = display_of(tmp_path, make_mode("I", [0, 257, 65535, 10]), "i32b.tif")
    assert list(d.im.tobytes()) == [0, 1, 255, 0] and not d.normalized

    assert display_of(tmp_path, Image.new("P", (4, 4)), "p.png").im.mode == "RGB"
    assert display_of(tmp_path, Image.new("LA", (4, 4)), "la.png").im.mode == "RGBA"
    assert display_of(tmp_path, Image.new("CMYK", (4, 4)), "c.jpg").im.mode == "RGB"
    assert display_of(tmp_path, Image.new("1", (4, 4)), "b.png").im.mode == "L"


def test_exif_orientation(svc, tmp_path):
    path = tmp_path / "rot.jpg"
    exif = Image.Exif()
    exif[0x0112] = 6
    photo(40, 20).save(path, exif=exif.tobytes())
    info = imaging.probe(str(path))
    assert (info.w, info.h, info.orientation) == (20, 40, 6)
    d = imaging.open_display(str(path))
    assert d.im.size == (20, 40) and (d.w, d.h) == (20, 40)
    hdr, data, _ = render(svc, path, "thumb")
    im = decode(data)
    assert (hdr["w"], hdr["h"]) == (20, 40) and im.size[1] > im.size[0]

    # 缩小解码的 JPEG：目标尺寸按存储方向换算
    big = tmp_path / "rot_big.jpg"
    photo(2400, 1200).save(big, exif=exif.tobytes(), quality=90)
    d = imaging.open_display(str(big), draft_target=768)
    assert d.drafted and d.im.size == (600, 1200) and (d.w, d.h) == (1200, 2400)
    hdr, data, _ = render(svc, big, "thumb")
    assert decode(data).size == (128, 256)


def test_icc_kept(svc, tmp_path):
    path = tmp_path / "icc.png"
    photo(2200, 1000).save(path, icc_profile=ICC)
    _, data, _ = render(svc, path, "preview", size=1024)
    assert decode(data).info.get("icc_profile") == ICC
    _, data, _ = render(svc, path, "tile", l=0, x=1, y=1)
    assert decode(data).info.get("icc_profile") == ICC
    # CMYK 转成 RGB 后不再带 CMYK 的 ICC
    assert imaging._icc_for(b"\0" * 16 + b"CMYK", "RGB") is None


def test_full_transcode_is_exact(svc, tmp_path):
    src = photo(700, 500)
    src.save(tmp_path / "a.tif")
    hdr, data, _ = render(svc, tmp_path / "a.tif", "full")
    assert hdr["lossless"] == 1 and hdr["ct"] in ("image/webp", "image/png")
    assert np.array_equal(np.asarray(decode(data).convert("RGB")), np.asarray(src))

    src.save(tmp_path / "a.bmp")
    _, data = imaging.render_full(imaging.open_display(str(tmp_path / "a.bmp")))
    assert np.array_equal(np.asarray(decode(data).convert("RGB")), np.asarray(src))
    assert imaging.is_native("bmp", 1 << 20) and not imaging.is_native("bmp", 9 << 20)
    assert not imaging.is_native("tiff", 10)


def test_tiles(svc, tmp_path):
    src = photo(1300, 700)
    path = tmp_path / "t.png"
    src.save(path)
    hdr, data, cache = render(svc, path, "tile", l=0, x=2, y=1)
    tile = decode(data).convert("RGB")
    assert cache == "miss" and tile.size == (276, 188) and hdr["lossless"] == 1
    assert np.array_equal(np.asarray(tile), np.asarray(src.crop((1024, 512, 1300, 700))))
    _, data, _ = render(svc, path, "tile", l=1, x=1, y=0)
    tile = decode(data).convert("RGB")
    assert np.array_equal(np.asarray(tile), np.asarray(src.reduce(2).crop((512, 0, 650, 350))))
    assert imaging.level_dims(1300, 700, 1) == (650, 350) and imaging.level_dims(1001, 999, 2) == (251, 250)
    assert src.reduce(4).size == imaging.level_dims(1300, 700, 2)
    assert [imaging.max_level(*s) for s in ((512, 100), (513, 10), (1024, 1024), (1025, 3), (7680, 4320))] == [0, 1, 1, 2, 4]
    for l, x, y in ((0, 3, 0), (0, 0, 2), (2, 1, 0), (3, 0, 0), (-1, 0, 0)):
        with pytest.raises(ValueError):
            render(svc, path, "tile", l=l, x=x, y=y)
    assert render(svc, path, "tile", l=2, x=0, y=0)[0]["w"] == 1300


def test_encode_not_behind_decode(tmp_path, monkeypatch):
    """已解码的图切瓦片用编码槽位，不排在别的图正在进行的整图解码后面（解码槽位只有 1 个时也一样）。"""
    svc = imaging.Service(cache_dir=str(tmp_path / "cache"), cache_mb=64, workers=1, mem_mb=256, decoded_mb=256)
    for name in ("a", "b"):
        photo(1300, 700).save(tmp_path / (name + ".png"))
    render(svc, tmp_path / "a.png", "tile", l=0, x=0, y=0)  # a 已解码
    original = imaging.open_display
    monkeypatch.setattr(imaging, "open_display", lambda *a, **k: (time.sleep(1.5), original(*a, **k))[1])
    slow = threading.Thread(target=render, args=(svc, tmp_path / "b.png", "tile"), kwargs=dict(l=0, x=0, y=0))
    slow.start()
    time.sleep(0.3)  # b 占着唯一的解码槽位
    t0 = time.monotonic()
    assert render(svc, tmp_path / "a.png", "tile", l=0, x=1, y=1)[2] == "miss"
    assert time.monotonic() - t0 < 0.8
    slow.join(10)


def test_decoded_cache_default(tmp_path, monkeypatch):
    """已解码整图的缓存默认 min(2 GB, 内存 / 8)：6 张 8K（每张约 177 MB）同屏时放得下，不会轮流挤出重新解码。"""
    monkeypatch.delenv("GNM_AGENT_DECODED_MB", raising=False)
    for mem, cap in ((65536, 2048), (16384, 2048), (4096, 512)):
        monkeypatch.setattr(imaging, "_mem_total_mb", lambda: mem)
        svc = imaging.Service(cache_dir=str(tmp_path / "c"))
        assert svc.decoded_cap == cap << 20
    assert 6 * 7680 * 4320 * 4 * 4 // 3 < 2048 << 20


def test_errors_and_helpers(svc, tmp_path, monkeypatch):
    path = tmp_path / "a.png"
    photo(64, 64).save(path)
    with pytest.raises(imaging.TooLarge):
        imaging.probe(str(path), max_pixels=100)
    monkeypatch.setattr(imaging.Image, "MAX_IMAGE_PIXELS", 100)
    with pytest.raises(imaging.TooLarge):
        imaging.open_display(str(path))
    monkeypatch.undo()
    (tmp_path / "bad.png").write_bytes(b"\x89PNG\r\n\x1a\n" + os.urandom(200))
    with pytest.raises(imaging.BadImage):
        render(svc, tmp_path / "bad.png", "thumb")
    (tmp_path / "x.txt").write_text("not an image")
    with pytest.raises(imaging.BadImage):
        render(svc, tmp_path / "x.txt", "preview")

    assert [imaging.bucket(s) for s in (None, 1, 1024, 1025, 2048, 3000, 9999)] == [2048, 1024, 1024, 2048, 2048, 3072, 3072]
    assert imaging.etag("9f2c01ab3e", "raw") == '"9f2c01ab3e"'
    assert imaging.etag("9f2c01ab3e", "thumb") == '"9f2c01ab3e.t256.1"'
    assert imaging.etag("9f2c01ab3e", "preview", size=1500) == '"9f2c01ab3e.p2048.1"'
    assert imaging.etag("9f2c01ab3e", "full") == '"9f2c01ab3e.f.1"'
    assert imaging.etag("9f2c01ab3e", "tile", l=2, x=3, y=1) == '"9f2c01ab3e.l2x3y1.1"'
    st = os.stat(path)
    v = imaging.version(st)
    assert len(v) == 10 and int(v, 16) >= 0
    assert [imaging.sniff(str(path)), imaging.sniff(str(tmp_path / "x.txt"))] == ["png", "other"]


def test_disk_cache(tmp_path):
    cache = imaging.DiskCache(str(tmp_path / "c"), 1)
    k = cache.key("/a.png\0100\0111", "thumb", "t256")
    assert k != cache.key("/a.png\0100\0112", "thumb", "t256") != cache.key("/a.png\0100\0111", "preview", "p2048")
    assert cache.get(k) is None
    assert cache.put(k, {"ct": "image/jpeg", "w": 3}, b"payload")
    hdr, path, offset, length = cache.get(k)
    with open(path, "rb") as f:
        f.seek(offset)
        assert hdr == {"ct": "image/jpeg", "w": 3} and f.read(length) == b"payload"

    # 超过容量时按修改时间删到 80% 以下，新的保留（先按大容量写入，避免后台线程提前清理）
    cache.cap = 100 << 20
    keys = []
    for i in range(40):
        key = cache.key("/img%d" % i, "tile", "l0x0y0")
        cache.put(key, {}, os.urandom(50 * 1024))
        os.utime(cache.path(key), (1000 + i, 1000 + i))
        keys.append(key)
    stale = os.path.join(cache.dir, "ab", "x.bin.1.2.tmp")
    fresh = os.path.join(cache.dir, "ab", "y.bin.1.2.tmp")
    for p in (stale, fresh):
        os.makedirs(os.path.dirname(p), exist_ok=True)
        open(p, "wb").close()
    os.utime(stale, (time.time() - 7200, time.time() - 7200))
    cache.cap = 1 << 20
    total = cache.evict()
    assert total <= 0.8 * (1 << 20)
    assert cache.exists(keys[-1]) and not cache.exists(keys[0])
    assert not os.path.exists(stale) and os.path.exists(fresh)

    # 剩余空间不足时不写入，但仍能读取
    low = imaging.DiskCache(cache.dir, 1)
    low.min_free = 1 << 62
    key = cache.key("/new", "thumb", "t256")
    assert not low.put(key, {}, b"x") and not low.exists(key)
    assert low.get(keys[-1]) is not None


def test_budget():
    budget = imaging.Budget(10)
    mb = 1 << 20
    assert budget.acquire(6 * mb) == 6 * mb
    got = []
    t = threading.Thread(target=lambda: got.append(budget.acquire(6 * mb)))
    t.start()
    time.sleep(0.2)
    assert not got and budget.waiting == 1  # 按权重阻塞
    budget.release(6 * mb)
    t.join(2)
    assert got == [6 * mb]
    budget.release(6 * mb)
    assert budget.acquire(100 * mb) == 10 * mb  # 比总量大的占满整个预算
    budget.release(10 * mb)
    assert imaging.Budget.cost(100, 100) == 100 * 100 * 8 + 16 * mb


def test_budget_waiters_busy_and_gone():
    budget = imaging.Budget(1, max_waiting=1, timeout=5)
    budget.acquire(1 << 20)
    gone = threading.Event()
    errors = []

    def wait():
        try:
            budget.acquire(1, gone=gone.is_set)
        except Exception as exc:
            errors.append(exc)

    t = threading.Thread(target=wait)
    t.start()
    time.sleep(0.2)
    with pytest.raises(imaging.Busy):  # 排队的已满
        budget.acquire(1)
    gone.set()  # 客户端断开，离开队列
    t.join(2)
    assert isinstance(errors[0], imaging.Gone) and budget.waiting == 0
    # 排队超时
    short = imaging.Budget(1, timeout=0.3)
    short.acquire(1 << 20)
    started = time.monotonic()
    with pytest.raises(imaging.Busy):
        short.acquire(1)
    assert time.monotonic() - started < 2


def test_budget_urgent_fifo_and_limit():
    """urgent 的排在普通请求前面，彼此先到先得；limit 给别的请求留出余量。"""
    budget = imaging.Budget(1, unit=1)
    budget.acquire(1)
    order = []

    def wait(name, urgent):
        budget.acquire(1, urgent=urgent)
        order.append(name)
        budget.release(1)

    threads = []
    for name, urgent in (("n0", False), ("u0", True), ("n1", False), ("u1", True), ("u2", True)):
        threads.append(threading.Thread(target=wait, args=(name, urgent)))
        threads[-1].start()
        time.sleep(0.05)
    budget.release(1)
    for t in threads:
        t.join(5)
    assert order == ["u0", "u1", "u2", "n0", "n1"]
    two = imaging.Budget(2, unit=1, timeout=0.3)
    two.acquire(1, limit=1)
    with pytest.raises(imaging.Busy):  # 占用后不能超过 1
        two.acquire(1, limit=1)
    assert two.acquire(1, urgent=True) == 1 and two.used == 2


def test_other_formats_rejected(tmp_path, monkeypatch):
    """只用常见图片格式的解码器：内容是 EPS 的文件不会交给 Ghostscript。"""
    from PIL import EpsImagePlugin

    calls = []
    monkeypatch.setattr(EpsImagePlugin, "Ghostscript", lambda *a, **k: calls.append(a))
    monkeypatch.setattr(EpsImagePlugin, "has_ghostscript", lambda: True)
    path = tmp_path / "evil.png"
    path.write_bytes(b"%!PS-Adobe-3.0 EPSF-3.0\n%%BoundingBox: 0 0 100 100\nshowpage\n%%EOF\n")
    svc = imaging.Service(cache_dir=str(tmp_path / "cache"), cache_mb=64, workers=2, mem_mb=256, decoded_mb=256)
    for kind in ("thumb", "preview", "full"):
        with pytest.raises(imaging.BadImage):
            render(svc, path, kind)
    with pytest.raises(imaging.BadImage):
        imaging.probe(str(path))
    assert not calls


def counted_decodes(monkeypatch, delay=0.0):
    names = []
    original = imaging.open_display

    def counted(path, *args, **kwargs):
        names.append(os.path.basename(path).split(".")[0])
        time.sleep(delay)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(imaging, "open_display", counted)
    return names


def test_decoded_lru_keeps_tiled_sources(tmp_path, monkeypatch):
    """只为缩略图解码的不放入 LRU；只生成了预览的解码图先被挤出，正在切瓦片的原图不用重新解码。"""
    for k in range(6):
        photo(1100, 1100).save(tmp_path / ("img%d.png" % k))
    svc = imaging.Service(cache_dir=str(tmp_path / "cache"), cache_mb=64, workers=2, mem_mb=256, decoded_mb=14)
    names = counted_decodes(monkeypatch)
    render(svc, tmp_path / "img0.png", "tile", l=0, x=0, y=0)
    for k in (1, 2, 3):
        render(svc, tmp_path / ("img%d.png" % k), "thumb")
    render(svc, tmp_path / "img0.png", "tile", l=0, x=1, y=0)
    assert names == ["img0", "img1", "img2", "img3"]
    render(svc, tmp_path / "img1.png", "tile", l=0, x=0, y=0)  # 两张都在切瓦片，正好占满
    for k in (4, 5):
        render(svc, tmp_path / ("img%d.png" % k), "preview", size=1024)
    render(svc, tmp_path / "img0.png", "tile", l=0, x=0, y=1)
    render(svc, tmp_path / "img1.png", "tile", l=0, x=1, y=1)
    assert names == ["img0", "img1", "img2", "img3", "img1", "img4", "img5"]
    # 很久没切瓦片的不再优先保留
    monkeypatch.setattr(imaging, "TILED_SECONDS", 0)
    render(svc, tmp_path / "img4.png", "tile", l=0, x=0, y=0)
    render(svc, tmp_path / "img0.png", "tile", l=0, x=1, y=1)
    assert names[-2:] == ["img4", "img0"]


def test_thumb_decodes_leave_a_slot(tmp_path, monkeypatch):
    """缩略图的整图解码最多 workers - 1 个：一屏冷缩略图在解码时当前图的预览不用排队；
    排队中的缩略图遇到同一张图的预览时用预览的解码结果，不再解码一次。"""
    for k in range(4):
        photo(1100, 1100).save(tmp_path / ("img%d.png" % k))
    svc = imaging.Service(cache_dir=str(tmp_path / "cache"), cache_mb=64, workers=2, mem_mb=256, decoded_mb=256)
    assert (svc.cpu.total, svc.thumbs.total) == (3, 1)
    names = counted_decodes(monkeypatch, 0.8)
    thumbs = [threading.Thread(target=render, args=(svc, tmp_path / ("img%d.png" % k), "thumb")) for k in (0, 1, 2)]
    for t in thumbs:
        t.start()
    time.sleep(0.2)
    started = time.monotonic()
    render(svc, tmp_path / "img2.png", "preview", size=1024)
    assert time.monotonic() - started < 1.2
    for t in thumbs:
        t.join(10)
    assert sorted(names) == ["img0", "img1", "img2"]
    assert render(svc, tmp_path / "img2.png", "thumb")[2] == "hit"

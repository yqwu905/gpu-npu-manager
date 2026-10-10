"""在线程中启动真实的 Agent，验证图片列表、流式原图、缩略图接口、长连接和并发控制。"""
import ast
import gzip
import http.client
import io
import json
import os
import socket
import sys
import threading
import time
import tracemalloc
from pathlib import Path
from urllib.parse import quote

import pytest
from PIL import Image

import agent
import imaging

ROOT = Path(__file__).resolve().parent.parent
TOKEN = "secret"


@pytest.fixture
def make_agent(tmp_path, monkeypatch):
    """启动 Agent，返回端口；allow_roots 为 tmp_path/root。"""
    monkeypatch.setenv("GNM_NVIDIA_SMI", "")
    monkeypatch.setenv("GNM_NPU_SMI", "")
    monkeypatch.setattr(agent.shutil, "which", lambda name: None)
    (tmp_path / "root").mkdir()
    servers = []

    def start(**image_opts):
        opts = dict(cache_dir=str(tmp_path / ("cache%d" % len(servers))), cache_mb=64, workers=2, mem_mb=256, decoded_mb=256)
        opts.update(image_opts)
        server = agent.build_server(
            "127.0.0.1", 0, TOKEN, ["/"], str(tmp_path / "data"), [str(tmp_path / "root")], image_opts=opts
        )
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return server.server_address[1]

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


class Client:
    """一个长连接。"""

    def __init__(self, port):
        self.conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)

    def get(self, path, headers=None, method="GET", body=None, token=TOKEN):
        self.conn.request(method, path, body=body, headers=dict({"X-Agent-Token": token}, **(headers or {})))
        resp = self.conn.getresponse()
        return resp, resp.read()

    def close(self):
        self.conn.close()


def q(path, **params):
    return "path=" + quote(str(path), safe="") + "".join("&%s=%s" % kv for kv in params.items())


def png(path, size=(64, 48), color=(10, 20, 30)):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path)
    return path


def write_jsonl(path, records):
    lines = [r if isinstance(r, str) else json.dumps(r) for r in records]
    path.write_text("\n".join(lines) + "\n")


def test_image_list_jsonl(make_agent, tmp_path):
    root = tmp_path / "root"
    res = root / "res"
    for rel in ("b/001.png", "a/001.png", "a/002.png", "c/000.png"):
        png(res / rel)
    png(root / "other" / "001.png")
    png(tmp_path / "outside.png")
    os.symlink(tmp_path / "outside.png", res / "a" / "link.png")
    write_jsonl(res / "predictions.jsonl", [
        {"id": 1, "image": "b/001.png"},
        {"id": 2, "image": "a/002.png"},
        {"id": 3, "image": "a/001.png"},
        {"id": 4, "image": "a/001.png"},  # 重复
        {"id": 5, "image": str(root / "other" / "001.png")},  # 绝对路径
        {"id": 6},  # 没有 image
        {"id": 7, "image": 5},
        "not json",
        "",
        {"id": 8, "image": "a/missing.png"},
        {"id": 9, "image": str(tmp_path / "outside.png")},  # 不在允许的目录内
        {"id": 10, "image": "a/link.png"},  # 符号链接逃逸
        {"id": 11, "image": "c\\000.png"},
    ])
    client = Client(make_agent())
    resp, data = client.get("/v1/files/images?" + q(res), {"Accept-Encoding": "gzip"})
    assert resp.status == 200 and resp.getheader("Content-Encoding") == "gzip"
    body = json.loads(gzip.decompress(data))
    paths = [f[0] for f in body["files"]]
    # 按 (文件名, 路径) 排序，文件名按 / 和 \\ 分割
    assert paths == [
        "c\\000.png", str(root / "other" / "001.png"), "a/001.png", "b/001.png", "a/002.png", "a/link.png",
        "a/missing.png", str(tmp_path / "outside.png"),
    ]
    assert (body["total"], body["missing"], body["skipped"], body["truncated"], body["source"]) == (8, 4, 3, False, "agent")
    by_path = {f[0]: f for f in body["files"]}
    st = os.stat(res / "a" / "001.png")
    assert by_path["a/001.png"][1:] == [st.st_size, imaging.version(st)]
    for bad in ("a/missing.png", "a/link.png", str(tmp_path / "outside.png"), "c\\000.png"):
        assert by_path[bad][1:] == [-1, ""]

    # ETag 与 304；不接受 gzip 时返回原文
    etag = resp.getheader("ETag")
    resp, data = client.get("/v1/files/images?" + q(res), {"If-None-Match": etag})
    assert resp.status == 304 and data == b""
    resp, data = client.get("/v1/files/images?" + q(res))
    assert resp.getheader("Content-Encoding") is None and json.loads(data) == body

    # 改写 predictions.jsonl 后缓存失效
    write_jsonl(res / "predictions.jsonl", [{"image": "a/002.png"}])
    resp, data = client.get("/v1/files/images?" + q(res), {"If-None-Match": etag})
    assert resp.status == 200 and [f[0] for f in json.loads(data)["files"]] == ["a/002.png"]
    resp, data = client.get("/v1/files/images?" + q(res, limit=0))
    assert json.loads(data)["truncated"] is True and json.loads(data)["files"] == []


def test_image_list_scan_mode(make_agent, tmp_path):
    res = tmp_path / "root" / "scan"
    png(res / "x" / "1.png")
    png(res / "2.png")
    png(res / "eval" / "3.png")  # 评测输出目录不扫描
    (res / "only.txt").write_text("text")
    client = Client(make_agent())
    resp, data = client.get("/v1/files/images?" + q(res))
    body = json.loads(data)
    assert [f[0] for f in body["files"]] == ["x/1.png", "2.png"] and body["skipped"] == 1
    assert client.get("/v1/files/images?" + q(tmp_path / "root" / "nope"))[0].status == 404
    assert client.get("/v1/files/images?" + q(tmp_path))[0].status == 403


def test_image_list_20k_fast(make_agent, tmp_path):
    res = tmp_path / "root" / "big"
    (res / "images").mkdir(parents=True)
    names = ["images/%05d.png" % i for i in range(20000)]
    for name in names[:19990]:
        with open(res / name, "wb") as f:
            f.write(b"x")
    write_jsonl(res / "predictions.jsonl", [{"id": i, "image": name} for i, name in enumerate(reversed(names))])
    client = Client(make_agent())
    started = time.monotonic()
    resp, data = client.get("/v1/files/images?" + q(res), {"Accept-Encoding": "gzip"})
    elapsed = time.monotonic() - started
    body = json.loads(gzip.decompress(data))
    assert body["total"] == 20000 and body["missing"] == 10 and [f[0] for f in body["files"]] == names
    assert elapsed < 1.0, elapsed
    started = time.monotonic()
    client.get("/v1/files/images?" + q(res), {"Accept-Encoding": "gzip"})
    assert time.monotonic() - started < 0.2  # 缓存命中


def test_image_list_builds_one_at_a_time(tmp_path, monkeypatch):
    """不同目录的列表尽量一个一个构建（并发时 stat 交接 GIL 反而慢几倍），同一目录只构建一次；已缓存的目录不等别的目录构建。"""
    root = tmp_path / "root"
    for k in range(3):
        (root / ("s%d" % k) / "images").mkdir(parents=True)
        write_jsonl(root / ("s%d" % k) / "predictions.jsonl", [{"image": "images/a.png"}])
    files = agent.FileStore([str(root)])
    files.image_list(str(root / "s0"))
    active, peak, built, gate = [0], [0], [], threading.Event()
    original = agent.FileStore._build_list

    def slow(self, real, *args):
        active[0] += 1
        peak[0] = max(peak[0], active[0])
        built.append(os.path.basename(real))
        gate.wait(5)
        active[0] -= 1
        return original(self, real, *args)

    monkeypatch.setattr(agent.FileStore, "_build_list", slow)
    threads = [threading.Thread(target=files.image_list, args=(str(root / name),)) for name in ("s1", "s2", "s1")]
    for t in threads:
        t.start()
    time.sleep(0.2)
    started = time.monotonic()
    files.image_list(str(root / "s0"))  # 缓存命中，不等正在构建的 s1 / s2
    assert time.monotonic() - started < 0.1
    gate.set()
    for t in threads:
        t.join(10)
    assert peak[0] == 1 and sorted(built) == ["s1", "s2"]


def test_image_list_slow_dir_does_not_block(tmp_path, monkeypatch):
    """某个目录构建卡住（如 NFS 挂起）时，其他目录最多等 BUILD_WAIT 秒；同一目录排队的请求在客户端断开后放弃。"""
    root = tmp_path / "root"
    for name in ("slow", "fast"):
        (root / name).mkdir(parents=True)
        write_jsonl(root / name / "predictions.jsonl", [{"image": "a.png"}])
    monkeypatch.setattr(agent, "BUILD_WAIT", 0.3)
    files = agent.FileStore([str(root)])
    gate = threading.Event()
    original = agent.FileStore._build_list

    def hang(self, real, *args):
        if real.endswith("slow"):
            gate.wait(10)
        return original(self, real, *args)

    monkeypatch.setattr(agent.FileStore, "_build_list", hang)
    t = threading.Thread(target=files.image_list, args=(str(root / "slow"),))
    t.start()
    time.sleep(0.1)
    started = time.monotonic()
    assert json.loads(files.image_list(str(root / "fast"))[1])["total"] == 1
    assert time.monotonic() - started < 1.0
    started = time.monotonic()
    with pytest.raises(ConnectionError):
        files.image_list(str(root / "slow"), gone=lambda: True)
    assert time.monotonic() - started < 2.0
    gate.set()
    t.join(10)
    assert not files._building


def test_image_list_cache_after_slow_build(tmp_path, monkeypatch):
    """缓存从构建完成时开始计时：构建比 SCAN_CACHE_SECONDS 还慢时下一次请求仍命中缓存。"""
    res = tmp_path / "root" / "res"
    res.mkdir(parents=True)
    write_jsonl(res / "predictions.jsonl", [{"image": "a.png"}])
    monkeypatch.setattr(agent, "SCAN_CACHE_SECONDS", 0.3)
    files = agent.FileStore([str(tmp_path / "root")])
    calls, original = [], agent.FileStore._build_list

    def slow(self, *args):
        calls.append(1)
        time.sleep(0.4)
        return original(self, *args)

    monkeypatch.setattr(agent.FileStore, "_build_list", slow)
    files.image_list(str(res))
    files.image_list(str(res))
    assert len(calls) == 1


def test_image_list_bad_records(tmp_path):
    """image 含 NUL 或单独的代理字符时只跳过这一条，不让整个列表失败。"""
    res = tmp_path / "root" / "res"
    png(res / "a.png")
    (res / "predictions.jsonl").write_text(
        '{"image": "a.png"}\n{"image": "b\\u0000.png"}\n{"image": "\\ud800.png"}\n{"image": "\\udcc4.png"}\n'
    )
    for roots in ([], [str(tmp_path / "root")]):
        body = json.loads(agent.FileStore(roots).image_list(str(res))[1].decode("utf-8", "surrogateescape"))
        assert [f[0] for f in body["files"]] == ["a.png", "\udcc4.png"]
        assert (body["skipped"], body["missing"]) == (2, 1)


def test_raw_streams_large_file(make_agent, tmp_path):
    big = tmp_path / "root" / "big.bin"
    with open(big, "wb") as f:
        f.truncate(100 << 20)  # 稀疏文件，超过原来 64 MB 的上限
    client = Client(make_agent())
    buf = memoryview(bytearray(256 * 1024))
    tracemalloc.start()
    try:
        client.conn.request("GET", "/v1/files/raw?" + q(big), headers={"X-Agent-Token": TOKEN})
        resp = client.conn.getresponse()
        total = 0
        while True:
            n = resp.readinto(buf)
            if not n:
                break
            total += n
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert resp.status == 200 and int(resp.getheader("Content-Length")) == total == 100 << 20
    assert peak < 4 << 20, peak
    st = os.stat(big)
    assert resp.getheader("ETag") == '"%s"' % imaging.version(st) and resp.getheader("X-Source-Version") == imaging.version(st)
    assert resp.getheader("Last-Modified")
    resp, data = client.get("/v1/files/raw?" + q(big), {"If-None-Match": resp.getheader("ETag")})
    assert resp.status == 304 and data == b""
    # 304 之后连接仍可用
    resp, data = client.get("/v1/health")
    assert resp.status == 200


def test_keep_alive(make_agent, tmp_path):
    image = png(tmp_path / "root" / "a.png", (300, 200))
    client = Client(make_agent())
    resp, data = client.get("/v1/health")
    sock = client.conn.sock
    assert resp.status == 200 and json.loads(data)["ok"]
    assert client.get("/v1/nope")[0].status == 404
    resp, data = client.get("/v1/files/raw?" + q(image))
    assert resp.status == 200 and data == image.read_bytes()
    resp, data = client.get("/v1/files/image?" + q(image, kind="thumb"))
    assert resp.status == 200 and resp.getheader("Content-Type") == "image/jpeg"
    assert client.conn.sock is sock  # 一直是同一个连接

    # 未知路由的 POST 之后，下一个请求不受影响
    resp, data = client.get("/v1/unknown", method="POST", body=b'{"a": 1}' * 100)
    assert resp.status == 404
    resp, data = client.get("/v1/health")
    assert resp.status == 200 and json.loads(data)["ok"]
    assert client.conn.sock is sock
    # 未授权时不读请求体，回复 401 后断开连接，重连后的请求不受影响
    resp, data = client.get("/v1/jobs", method="POST", body=b'{"job_id": "x", "command": "true"}', token="wrong")
    assert resp.status == 401 and resp.getheader("Connection") == "close"
    resp, data = client.get("/v1/health")
    assert resp.status == 200 and json.loads(data)["ok"]


def test_unauthorized_body_not_read(make_agent):
    """没有 token 的请求不会让 Agent 读入请求体。"""
    port = make_agent()
    with socket.create_connection(("127.0.0.1", port), timeout=10) as sock:
        sock.sendall(b"POST /v1/jobs HTTP/1.1\r\nHost: x\r\nX-Agent-Token: wrong\r\nContent-Length: 16777216\r\n\r\n")
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = sock.recv(65536)
            if not chunk:
                break
            data += chunk
        assert data.startswith(b"HTTP/1.1 401") and b"Connection: close" in data


def test_image_endpoint(make_agent, tmp_path):
    res = tmp_path / "root" / "res"
    png(res / "big.png", (2500, 1100))
    small = png(res / "small.png", (300, 200))
    Image.new("RGB", (700, 500), (1, 2, 3)).save(res / "a.tif")
    write_jsonl(res / "predictions.jsonl", [{"image": "big.png"}, {"image": "small.png"}, {"image": "a.tif"}])
    client = Client(make_agent())
    files = {f[0]: f for f in json.loads(client.get("/v1/files/images?" + q(res))[1])["files"]}
    big = res / "big.png"

    resp, data = client.get("/v1/files/image?" + q(big, kind="thumb"))
    assert resp.status == 200 and resp.getheader("X-Cache") == "miss"
    assert resp.getheader("X-Source-Version") == files["big.png"][2]
    assert resp.getheader("ETag") == '"%s.t256.1"' % files["big.png"][2]
    assert (resp.getheader("X-Image-Width"), resp.getheader("X-Image-Height")) == ("2500", "1100")
    assert (resp.getheader("X-Lossless"), resp.getheader("X-Image-Format"), resp.getheader("X-Image-Native")) == ("0", "png", "1")
    assert float(resp.getheader("X-Gen-Ms")) >= 0
    resp, again = client.get("/v1/files/image?" + q(big, kind="thumb"))
    assert resp.getheader("X-Cache") == "hit" and again == data
    resp, _ = client.get("/v1/files/image?" + q(big, kind="thumb"), {"If-None-Match": '"%s.t256.1"' % files["big.png"][2]})
    assert resp.status == 304

    resp, data = client.get("/v1/files/image?" + q(big, kind="preview", size=1500))
    assert resp.getheader("ETag").endswith('.p2048.1"') and resp.getheader("X-Lossless") == "0"
    assert Image.open(io.BytesIO(data)).size == (2048, 901)
    resp, data = client.get("/v1/files/image?" + q(small, kind="preview"))
    assert resp.getheader("X-Lossless") == "1" and data == small.read_bytes()

    resp, data = client.get("/v1/files/image?" + q(big, kind="tile", l=0, x=4, y=2))
    assert resp.status == 200 and resp.getheader("X-Tile-Size") == "512" and resp.getheader("X-Lossless") == "1"
    assert Image.open(io.BytesIO(data)).size == (2500 - 2048, 1100 - 1024)
    for params in ({"l": 0, "x": 5, "y": 0}, {"l": 4, "x": 0, "y": 0}, {"l": 0, "x": 0}, {"l": "a", "x": 0, "y": 0}):
        assert client.get("/v1/files/image?" + q(big, kind="tile", **params))[0].status == 400
    assert client.get("/v1/files/image?" + q(big, kind="huge"))[0].status == 400

    # 浏览器能直接显示的原图原样返回；TIFF 转成无损 WebP
    resp, data = client.get("/v1/files/image?" + q(big, kind="full"))
    assert data == big.read_bytes() and resp.getheader("ETag") == '"%s"' % files["big.png"][2]
    assert resp.getheader("Content-Type") == "image/png" and resp.getheader("X-Image-Width") == "2500"
    resp, data = client.get("/v1/files/image?" + q(res / "a.tif", kind="full"))
    assert resp.getheader("Content-Type") == "image/webp" and resp.getheader("X-Image-Native") == "0"
    assert resp.getheader("ETag") == '"%s.f.1"' % files["a.tif"][2]
    assert Image.open(io.BytesIO(data)).convert("RGB").getpixel((5, 5)) == (1, 2, 3)

    (res / "bad.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\0" * 100)
    assert client.get("/v1/files/image?" + q(res / "bad.png", kind="thumb"))[0].status == 415
    assert client.get("/v1/files/image?" + q(res / "nope.png", kind="thumb"))[0].status == 404


def test_too_large_image(make_agent, tmp_path):
    image = png(tmp_path / "root" / "a.png", (300, 200))
    client = Client(make_agent(max_pixels=1000))
    assert client.get("/v1/files/image?" + q(image, kind="thumb"))[0].status == 413
    assert client.get("/v1/files/image?" + q(image, kind="full"))[0].status == 200


def test_without_pillow(make_agent, tmp_path, monkeypatch):
    monkeypatch.setattr(imaging, "Image", None)
    image = png(tmp_path / "root" / "a.png")
    client = Client(make_agent())
    features = json.loads(client.get("/v1/health")[1])["features"]
    assert features == {"list": 1, "stream": 1, "image": None, "webp": False, "tiles": False}
    assert client.get("/v1/files/image?" + q(image, kind="thumb"))[0].status == 501
    resp, data = client.get("/v1/files/image?" + q(image, kind="full"))
    assert resp.status == 200 and data == image.read_bytes() and resp.getheader("X-Image-Width") is None
    assert client.get("/v1/files/images?" + q(tmp_path / "root"))[0].status == 200


def test_health_features(make_agent):
    body = json.loads(Client(make_agent()).get("/v1/health")[1])
    assert body["features"] == {"list": 1, "stream": 1, "image": imaging.PIL_VERSION, "webp": imaging.WEBP, "tiles": True}
    assert body["agent_version"].startswith("0.2.0+")


class Calls(list):
    delay = 0.3


@pytest.fixture
def counted(monkeypatch):
    """记录解码的文件名，每次解码慢 calls.delay 秒。"""
    calls = Calls()
    original = imaging.open_display

    def slow(path, *args, **kwargs):
        calls.append(os.path.basename(path))
        time.sleep(calls.delay)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(imaging, "open_display", slow)
    return calls


def parallel(port, paths):
    results = [None] * len(paths)

    def run(i):
        client = Client(port)
        resp, data = client.get(paths[i])
        results[i] = (resp.status, resp.getheader("X-Cache"), resp.getheader("Retry-After"))
        client.close()

    threads = [threading.Thread(target=run, args=(i,)) for i in range(len(paths))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    return results


def test_single_flight(make_agent, tmp_path, counted):
    res = tmp_path / "root"
    png(res / "a.png", (2400, 1200))
    png(res / "b.png", (2400, 1200), (200, 0, 0))
    port = make_agent()
    results = parallel(port, ["/v1/files/image?" + q(res / "a.png", kind="thumb")] * 8)
    assert counted == ["a.png"]
    assert sorted(r[1] for r in results) == ["join"] * 7 + ["miss"]
    # 冷图同时请求缩略图和预览，只解码一次
    results = parallel(port, ["/v1/files/image?" + q(res / "b.png", kind=k, size=1024) for k in ("thumb", "preview")] * 2)
    assert counted == ["a.png", "b.png"] and all(r[0] == 200 for r in results)
    # 之后的瓦片用已解码的整图
    assert parallel(port, ["/v1/files/image?" + q(res / "b.png", kind="tile", l=0, x=1, y=1)])[0][0] == 200
    assert counted == ["a.png", "b.png"]


def test_backpressure(make_agent, tmp_path, counted):
    counted.delay = 1.0
    res = tmp_path / "root"
    for name in "abc":
        png(res / (name + ".png"))
    port = make_agent(workers=1, max_waiting=1)
    results = {}

    def run(name):
        client = Client(port)
        resp, _ = client.get("/v1/files/image?" + q(res / (name + ".png"), kind="thumb"))
        results[name] = (resp.status, resp.getheader("Retry-After"))

    threads = []
    for name in "abc":
        threads.append(threading.Thread(target=run, args=(name,)))
        threads[-1].start()
        time.sleep(0.2)
    for t in threads:
        t.join(30)
    assert results["a"][0] == results["b"][0] == 200
    assert results["c"] == (503, "1")


def raw_request(port, path):
    sock = socket.create_connection(("127.0.0.1", port))
    sock.sendall(("GET %s HTTP/1.1\r\nHost: x\r\nX-Agent-Token: %s\r\n\r\n" % (path, TOKEN)).encode())
    return sock


def test_disconnect_while_queued(make_agent, tmp_path, counted):
    counted.delay = 1.5
    res = tmp_path / "root"
    png(res / "a.png")
    png(res / "b.png")
    port = make_agent(workers=1)
    first = raw_request(port, "/v1/files/image?" + q(res / "a.png", kind="thumb"))
    time.sleep(0.3)
    second = raw_request(port, "/v1/files/image?" + q(res / "b.png", kind="thumb"))
    time.sleep(0.3)
    second.close()  # 排队时断开
    first.settimeout(10)
    assert first.recv(12) == b"HTTP/1.1 200"
    first.close()
    time.sleep(1.0)
    assert counted == ["a.png"]


def test_abandoned_requests(make_agent, tmp_path, counted):
    res = tmp_path / "root"
    for i in range(50):
        png(res / ("%02d.png" % i))
    port = make_agent(workers=2)
    for i in range(50):
        raw_request(port, "/v1/files/image?" + q(res / ("%02d.png" % i), kind="thumb")).close()
    time.sleep(2.0)
    assert len(counted) <= 3, counted


def test_allow_roots(make_agent, tmp_path):
    root = tmp_path / "root"
    png(tmp_path / "secret.png")
    os.symlink(tmp_path / "secret.png", root / "link.png")
    os.symlink(tmp_path, root / "dirlink")
    client = Client(make_agent())
    for path in (root / ".." / "secret.png", root / "link.png", tmp_path / "secret.png", root / "dirlink" / "secret.png"):
        assert client.get("/v1/files/image?" + q(path, kind="thumb"))[0].status == 403
        assert client.get("/v1/files/image?" + q(path, kind="full"))[0].status == 403
        assert client.get("/v1/files/raw?" + q(path))[0].status == 403
    for path in (root / "..", root / "dirlink", "/etc"):
        assert client.get("/v1/files/images?" + q(path))[0].status == 403
    # predictions.jsonl 是指向允许的目录外的符号链接时与 /samples 一样 403，不泄露外面文件的内容
    write_jsonl(tmp_path / "outside.jsonl", [{"image": "SECRET"}])
    (root / "ds").mkdir()
    os.symlink(tmp_path / "outside.jsonl", root / "ds" / "predictions.jsonl")
    resp, data = client.get("/v1/files/images?" + q(root / "ds"))
    assert resp.status == 403 and b"SECRET" not in data
    assert client.get("/v1/files/samples?" + q(root / "ds"))[0].status == 403


def top_level_imports(tree):
    for node in tree.body:
        if isinstance(node, ast.Import):
            yield from (alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            yield node.module.split(".")[0]


def test_python37_stdlib_only():
    """Agent 要能在 Python 3.7 上运行，导入时只依赖标准库（Pillow 在 try 中导入，可选）。"""
    for name in ("agent.py", "evaluate.py", "imaging.py"):
        source = (ROOT / "agent" / name).read_text(encoding="utf-8")
        tree = ast.parse(source, feature_version=(3, 7))
        for module in top_level_imports(tree):
            assert module in sys.stdlib_module_names or module in ("evaluate", "imaging"), (name, module)

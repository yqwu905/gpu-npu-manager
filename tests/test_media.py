"""对比页的图片接口：在真实 Agent 上验证 /images、/image、流式 /file，以及 Agent 没有 Pillow 时由中心服务生成。"""

import asyncio
import io
import json
import os
import re
import socket
import struct
import threading
import time
import tracemalloc
import zlib

import httpx
import numpy as np
import pytest
import uvicorn
from PIL import Image

import agent
import imaging
from app import images as app_images
from app.images import ProxyStream
from test_results import env  # noqa: F401  复用：真实 Agent、两个结果集目录、已登记的服务器


@pytest.fixture(autouse=True)
def cache_dirs(tmp_path, monkeypatch):
    """中心服务和 Agent 的图片缓存放到临时目录（autouse，先于 env 执行）。"""
    monkeypatch.setenv("GNM_IMAGE_CACHE_DIR", str(tmp_path / "central-cache"))
    monkeypatch.setenv("GNM_AGENT_CACHE_DIR", str(tmp_path / "agent-cache"))


def register(client, server_id, path) -> int:
    resp = client.post("/api/results", json={"server_id": server_id, "path": str(path)})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def image_list(client, result_id, **headers):
    resp = client.get(f"/api/results/{result_id}/images", headers=headers)
    assert resp.status_code == 200, resp.text
    return resp, {f[0]: f for f in resp.json()["files"]}


def get_image(client, result_id, path, **params):
    return client.get(f"/api/results/{result_id}/image", params={"path": path, **params})


def timing(resp) -> dict:
    """Server-Timing -> {名称: (dur, desc)}"""
    out = {}
    for item in resp.headers.get("Server-Timing", "").split(","):
        if item.strip():
            name, *fields = item.strip().split(";")
            kv = dict(f.split("=", 1) for f in fields)
            out[name] = (kv.get("dur"), kv.get("desc", "").strip('"'))
    return out


def noise(path, size, seed=0, mode="RGB"):
    rng = np.random.default_rng(seed)
    channels = {"RGB": 3, "RGBA": 4}[mode]
    Image.fromarray(rng.integers(0, 256, (size[1], size[0], channels)).astype(np.uint8), mode).save(path)
    return path


def pixels(data: bytes) -> np.ndarray:
    return np.asarray(Image.open(io.BytesIO(data)).convert("RGB"))


def test_images_list(env, monkeypatch):
    client, data, server_id = env
    res = data / "model-a"
    with open(res / "predictions.jsonl", "a") as f:
        f.write(json.dumps({"id": "s3", "image": "images/missing.png"}) + "\n")
        f.write(json.dumps({"id": "s4", "image": "images/0.png"}) + "\n")  # 重复
        f.write(json.dumps({"id": "s5"}) + "\n")  # 没有图片
    a = register(client, server_id, res)

    samples = client.get(f"/api/results/{a}/samples", params={"limit": 500}).json()["items"]
    images = list(dict.fromkeys(s["image"] for s in samples if isinstance(s.get("image"), str)))
    images.sort(key=lambda p: (re.split(r"[\\/]", p)[-1], p))

    resp, files = image_list(client, a)
    body = resp.json()
    assert resp.headers["content-encoding"] == "gzip"
    assert resp.headers["cache-control"] == "private, no-cache" and resp.headers["vary"] == "Accept-Encoding"
    assert resp.headers["x-content-type-options"] == "nosniff" and "agent;dur=" in resp.headers["server-timing"]
    assert [f[0] for f in body["files"]] == images
    assert (body["total"], body["missing"], body["skipped"], body["truncated"], body["source"]) == (4, 1, 1, False, "agent")
    for path, size, v in body["files"]:
        if path == "images/missing.png":
            assert (size, v) == (-1, "")
        else:
            st = os.stat(res / path)
            assert (size, v) == (st.st_size, imaging.version(st))

    etag = resp.headers["etag"]
    resp = client.get(f"/api/results/{a}/images", headers={"If-None-Match": etag})
    assert resp.status_code == 304 and resp.headers["etag"] == etag and resp.content == b""
    resp = client.get(f"/api/results/{a}/images", headers={"Accept-Encoding": "identity"})
    assert "content-encoding" not in resp.headers and resp.json() == body

    # 旧版 Agent（没有 features）：分页读取样本拼出列表
    async def old_agent(host, port):
        return {}

    with monkeypatch.context() as m:
        m.setattr(client.app.state.scheduler.agent, "features", old_agent)
        resp, _ = image_list(client, a)
        compat = resp.json()
        assert resp.headers["content-encoding"] == "gzip"
        assert compat["source"] == "samples" and compat["skipped"] == 1 and compat["total"] == 4
        assert compat["files"] == [[p, None, ""] for p in images]
        resp = client.get(f"/api/results/{a}/images", headers={"If-None-Match": resp.headers["etag"]})
        assert resp.status_code == 304

    assert client.get("/api/results/9999/images").status_code == 404
    os.rename(res, data / "moved")
    resp = client.get(f"/api/results/{a}/images")
    assert resp.status_code == 422 and resp.json()["detail"].startswith("读取图片列表失败")


def test_lq_sets(env):
    """评测用过的 LQ 目录按（评测服务器, 目录）去重列出，图片列表和图片接口与结果集相同。"""
    from app.models import Evaluation, ResultSet

    client, data, server_id = env
    a = register(client, server_id, data / "model-a")
    gt = str(data / "gt")
    with client.app.state.session_factory() as session:
        result = session.get(ResultSet, a)
        for lq_dir in (gt, gt, None):
            session.add(Evaluation(result_set=result, metrics=["psnr"], lq_dir=lq_dir, status="succeeded", output_dir="/x"))
        session.commit()
        first = min(e.id for e in result.evaluations if e.lq_dir)

    sets = client.get("/api/lq-sets").json()
    assert [(x["id"], x["name"], x["server_id"]) for x in sets] == [(first, "gt", server_id)]
    resp = client.get(f"/api/lq-sets/{first}/images")
    assert resp.status_code == 200, resp.text
    assert [f[0] for f in resp.json()["files"]] == ["0.png", "1.png", "2.png"]
    resp = client.get(f"/api/lq-sets/{first}/image", params={"path": "1.png", "kind": "thumb"})
    assert resp.status_code == 200 and resp.headers["x-image-width"] == "32"
    assert client.get(f"/api/lq-sets/{first + 2}/images").status_code == 404  # 没有 LQ 目录的评测
    assert client.get("/api/lq-sets/9999/image", params={"path": "1.png"}).status_code == 404


def test_image_kinds(env, monkeypatch):
    client, data, server_id = env
    res = data / "model-a"
    noise(res / "images" / "big.png", (1300, 700), seed=3)
    Image.fromarray(np.asarray(Image.open(res / "images" / "big.png"))[:200, :300]).save(res / "images" / "t.tif")
    a = register(client, server_id, res)
    _, files = image_list(client, a)
    v0 = files["images/0.png"][2]
    assert re.fullmatch("[0-9a-f]{10}", v0)
    big_v = imaging.version(os.stat(res / "images" / "big.png"))

    # 缩略图：带版本号时永久缓存，第二次从 L2 取
    resp = get_image(client, a, "images/0.png", v=v0, kind="thumb")
    assert resp.status_code == 200 and resp.headers["content-type"] == "image/jpeg"
    assert resp.headers["cache-control"] == "private, max-age=31536000, immutable"
    assert resp.headers["etag"] == f'"{v0}.t256.1"'
    h = resp.headers
    assert (h["x-image-width"], h["x-image-height"], h["x-image-format"], h["x-image-native"], h["x-lossless"]) == (
        "32", "24", "png", "1", "0")
    assert h["x-content-type-options"] == "nosniff" and "x-normalized" not in h
    assert timing(resp)["gen"][1] == "agent" and timing(resp)["cache"][1] == "miss"
    thumb = resp.content
    assert Image.open(io.BytesIO(thumb)).size == (32, 24)
    again = get_image(client, a, "images/0.png", v=v0, kind="thumb")
    assert again.content == thumb and timing(again)["cache"][1] == "l2" and again.headers["etag"] == h["etag"]

    # If-None-Match 一致时直接 304，不访问 Agent
    with monkeypatch.context() as m:
        def boom(*args, **kwargs):
            raise AssertionError("不应访问 Agent")

        m.setattr(client.app.state.scheduler.agent, "open_stream", boom)
        m.setattr(client.app.state.scheduler.agent, "features", boom)
        resp = client.get(f"/api/results/{a}/image", params={"path": "images/0.png", "v": v0, "kind": "thumb"},
                          headers={"If-None-Match": f'"{v0}.t256.1"'})
        assert resp.status_code == 304 and resp.headers["cache-control"].endswith("immutable")
        resp = client.get(f"/api/results/{a}/image", params={"path": "images/big.png", "v": big_v, "kind": "full"},
                          headers={"If-None-Match": f'"{big_v}"'})
        assert resp.status_code == 304

    # 版本号不对（文件改写过）：不能长期缓存，带真实的 ETag；不带版本号缓存 5 分钟
    resp = get_image(client, a, "images/0.png", v="0123456789", kind="thumb")
    assert resp.status_code == 200 and resp.headers["cache-control"] == "private, no-cache"
    assert resp.headers["etag"] == f'"{v0}.t256.1"' and resp.content == thumb
    resp = get_image(client, a, "images/0.png")
    assert resp.headers["cache-control"] == "private, max-age=300" and resp.content == thumb

    # 预览：尺寸取档位；原图不超过档位时无损，浏览器能直接显示的小图返回原图
    resp = get_image(client, a, "images/big.png", v=big_v, kind="preview", size=1000)
    assert resp.headers["etag"] == f'"{big_v}.p1024.1"' and resp.headers["x-lossless"] == "0"
    assert Image.open(io.BytesIO(resp.content)).size == (1024, 551)
    resp = get_image(client, a, "images/big.png", v=big_v, kind="preview", size=1500)
    assert resp.headers["etag"] == f'"{big_v}.p2048.1"' and resp.headers["x-lossless"] == "1"
    assert resp.content == (res / "images" / "big.png").read_bytes()

    # 瓦片：第 0 层与原图逐像素一致，第 1 层为 reduce(2)
    src = Image.open(res / "images" / "big.png").convert("RGB")
    resp = get_image(client, a, "images/big.png", v=big_v, kind="tile", l=0, x=2, y=1)
    assert resp.status_code == 200 and resp.headers["x-tile-size"] == "512" and resp.headers["x-lossless"] == "1"
    assert resp.headers["etag"] == f'"{big_v}.l0x2y1.1"'
    tile = pixels(resp.content)
    assert tile.shape == (188, 276, 3) and np.array_equal(tile, np.asarray(src.crop((1024, 512, 1300, 700))))
    resp = get_image(client, a, "images/big.png", v=big_v, kind="tile", l=1, x=0, y=0)
    assert np.array_equal(pixels(resp.content), np.asarray(src.reduce(2).crop((0, 0, 512, 350))))
    assert get_image(client, a, "images/big.png", v=big_v, kind="tile", l=0, x=3, y=0).status_code == 400
    assert get_image(client, a, "images/big.png", v=big_v, kind="tile", l=2, x=0, y=0).status_code == 200
    assert get_image(client, a, "images/big.png", v=big_v, kind="tile", l=3, x=0, y=0).status_code == 400

    # 原图：PNG 原样返回，TIFF 转成无损图
    resp = get_image(client, a, "images/big.png", v=big_v, kind="full")
    assert resp.content == (res / "images" / "big.png").read_bytes() and resp.headers["content-type"] == "image/png"
    assert resp.headers["etag"] == f'"{big_v}"' and resp.headers["x-lossless"] == "1"
    resp = get_image(client, a, "images/t.tif", kind="full")
    assert resp.headers["x-image-format"] == "tiff" and resp.headers["x-image-native"] == "0"
    assert resp.headers["content-type"] in ("image/webp", "image/png")
    assert np.array_equal(pixels(resp.content), np.asarray(Image.open(res / "images" / "t.tif").convert("RGB")))

    # 参数校验
    for params in ({"kind": "huge"}, {"v": "xyz"}, {"kind": "tile", "l": 0, "x": 0}, {"kind": "preview", "size": 0},
                   {"kind": "tile", "l": -1, "x": 0, "y": 0}):
        assert get_image(client, a, "images/0.png", **params).status_code == 422, params
    assert client.get(f"/api/results/{a}/image").status_code == 422
    resp = get_image(client, a, "images/nope.png")
    assert resp.status_code == 404 and resp.headers["cache-control"] == "no-store"
    assert get_image(client, 9999, "images/0.png").status_code == 404
    (res / "images" / "bad.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\0" * 64)
    assert get_image(client, a, "images/bad.png").status_code == 415


def test_large_derivative_streams(env, monkeypatch):
    """超过内存转发上限的派生图（如 3072 档的无损预览）流式转发，不报错、不写 L2。"""
    client, data, server_id = env
    res = data / "model-a"
    noise(res / "images" / "big.png", (1300, 700), seed=4)
    a = register(client, server_id, res)
    v = imaging.version(os.stat(res / "images" / "big.png"))
    monkeypatch.setattr(app_images, "MAX_DERIVATIVE", 4096)
    for _ in range(2):
        resp = get_image(client, a, "images/big.png", v=v, kind="preview", size=2048)
        assert resp.status_code == 200 and resp.content == (res / "images" / "big.png").read_bytes()
        assert resp.headers["etag"] == f'"{v}.p2048.1"' and resp.headers["cache-control"].endswith("immutable")
        assert resp.headers["x-lossless"] == "1" and timing(resp)["gen"][1] == "agent"


def test_special_paths_and_evaluation(env):
    client, data, server_id = env
    res = data / "model-a"
    name = "images/a+b #%c 中文 d.png"
    noise(res / name, (40, 30), seed=5)
    a = register(client, server_id, res)
    b = register(client, server_id, data / "model-b")
    resp = get_image(client, a, name, kind="thumb")
    assert resp.status_code == 200 and resp.headers["x-image-width"] == "40"
    resp = client.get(f"/api/results/{a}/file", params={"path": name})
    assert resp.status_code == 200 and resp.content == (res / name).read_bytes()
    resp = get_image(client, a, str(res / name), kind="full")
    assert resp.content == (res / name).read_bytes()

    # 评测 ID 必须属于该结果集
    ev = client.post("/api/evaluations", json={"result_set_id": a, "metrics": ["psnr"]}).json()
    assert get_image(client, a, "images/0.png", evaluation_id=ev["id"]).status_code == 200
    assert get_image(client, b, "images/0.png", evaluation_id=ev["id"]).status_code == 404
    assert client.get(f"/api/results/{b}/file", params={"path": "images/0.png", "evaluation_id": ev["id"]}).status_code == 404


def test_file_streaming(env):
    client, data, server_id = env
    res = data / "model-a"
    (res / "page.html").write_text("<script>alert(1)</script>")
    (res / "pic.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"/>')
    a = register(client, server_id, res)
    st = os.stat(res / "images" / "1.png")
    v = imaging.version(st)

    resp = client.get(f"/api/results/{a}/file", params={"path": "images/1.png"})
    assert resp.status_code == 200 and resp.content == (res / "images" / "1.png").read_bytes()
    assert resp.headers["content-type"] == "image/png" and resp.headers["etag"] == f'"{v}"'
    assert resp.headers["cache-control"] == "private, max-age=300" and resp.headers["x-content-type-options"] == "nosniff"
    assert int(resp.headers["content-length"]) == st.st_size and resp.headers["last-modified"]
    resp = client.get(f"/api/results/{a}/file", params={"path": "images/1.png"}, headers={"If-None-Match": f'"{v}"'})
    assert resp.status_code == 304 and resp.headers["etag"] == f'"{v}"'
    resp = client.get(f"/api/results/{a}/file", params={"path": "images/1.png", "v": v})
    assert resp.headers["cache-control"].endswith("immutable")
    resp = client.get(f"/api/results/{a}/file", params={"path": "images/1.png", "v": "0" * 10})
    assert resp.headers["cache-control"] == "private, no-cache"

    for path in ("page.html", "pic.svg"):
        resp = client.get(f"/api/results/{a}/file", params={"path": path})
        assert resp.status_code == 200
        assert resp.headers["content-disposition"] == "attachment" and resp.headers["content-security-policy"] == "sandbox"
    resp = client.get(f"/api/results/{a}/file", params={"path": "meta.json"})
    assert "content-disposition" not in resp.headers
    # xhtml、xml 等也能执行脚本：所有文件都禁止脚本
    (res / "report.xhtml").write_text('<html xmlns="http://www.w3.org/1999/xhtml"><script>alert(1)</script></html>')
    for path in ("meta.json", "report.xhtml", "images/1.png"):
        resp = client.get(f"/api/results/{a}/file", params={"path": path})
        assert resp.status_code == 200 and resp.headers["content-security-policy"] == "sandbox"
    assert client.get(f"/api/results/{a}/file", params={"path": "../../../etc/passwd"}).status_code == 404
    assert client.get(f"/api/results/{a}/file", params={"path": "images/nope.png"}).status_code == 404


@pytest.fixture
def live(env):
    """在线程中用真实的 uvicorn 运行同一个应用（TestClient 会缓冲响应体，测不了流式转发）。"""
    client = env[0]
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(client.app, log_level="warning"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.01)
    yield f"http://127.0.0.1:{sock.getsockname()[1]}"
    server.should_exit = True
    thread.join(10)


def test_file_streaming_memory(env, live):
    client, data, server_id = env
    res = data / "model-a"
    with open(res / "big.bin", "wb") as f:
        f.truncate(100 << 20)  # 稀疏文件
    a = register(client, server_id, res)
    total = 0
    with httpx.Client(timeout=60) as http:
        # 先请求一次小文件：建好两边的连接池（加载 CA 证书等），只统计传输期间的内存
        assert http.get(f"{live}/api/results/{a}/file", params={"path": "meta.json"}).status_code == 200
        tracemalloc.start()
        try:
            with http.stream("GET", f"{live}/api/results/{a}/file", params={"path": "big.bin"}) as resp:
                assert resp.status_code == 200 and int(resp.headers["content-length"]) == 100 << 20
                for chunk in resp.iter_raw():
                    total += len(chunk)
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
    assert total == 100 << 20
    assert peak < 16 << 20, peak


class Endless(httpx.AsyncByteStream):
    """一直有数据的上游响应体，记录是否被关闭。"""

    closed = False

    async def __aiter__(self):
        while True:
            yield b"x" * 65536
            await asyncio.sleep(0.01)

    async def aclose(self):
        self.closed = True


def test_proxy_stream_closes_upstream_on_disconnect():
    stream = Endless()
    released = []

    class FakeSlot:
        def release(self):
            released.append(1)

    response = ProxyStream(httpx.Response(200, stream=stream), FakeSlot(), headers={"Content-Type": "image/png"})
    sent = []

    async def receive():
        await asyncio.sleep(0.05)
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message["type"])

    asyncio.run(asyncio.wait_for(response({"type": "http"}, receive, send), 5))
    assert stream.closed and released == [1]
    assert sent[0] == "http.response.start" and len(sent) > 1


@pytest.fixture
def plain_agent(env, tmp_path):
    """另一个 Agent：没有 Pillow（imaging 只在构建时视为不可用），读取同一批目录；返回 (client, data, 结果集 ID)。"""
    client, data, _ = env
    with pytest.MonkeyPatch.context() as m:
        m.setattr(imaging, "Image", None)
        server = agent.build_server("127.0.0.1", 0, "t", ["/"], str(tmp_path / "agent2"), [str(data)],
                                    image_opts={"cache_dir": str(tmp_path / "agent2-cache")})
    threading.Thread(target=server.serve_forever, daemon=True).start()
    resp = client.post("/api/servers", json={"name": "plain", "host": "127.0.0.1", "port": server.server_address[1]})
    yield client, data, register(client, resp.json()["id"], data / "model-a")
    server.shutdown()
    server.server_close()


def test_central_fallback(env, plain_agent, live, tmp_path, monkeypatch):
    client, data, plain = plain_agent
    res = data / "model-a"
    noise(res / "images" / "wide.png", (2000, 900), seed=7)
    with_pillow = register(client, env[2], res)
    _, files = image_list(client, plain)
    v0 = files["images/0.png"][2]

    resp = get_image(client, plain, "images/0.png", v=v0, kind="thumb")
    assert resp.status_code == 200 and resp.headers["cache-control"].endswith("immutable")
    assert timing(resp)["gen"][1] == "central" and timing(resp)["cache"][1] == "miss"
    assert resp.headers["etag"] == f'"{v0}.t256.1"' and resp.headers["x-image-width"] == "32"
    # 中心服务与 Agent 用同一份 imaging.py，派生图逐字节一致
    assert resp.content == get_image(client, with_pillow, "images/0.png", v=v0, kind="thumb").content
    again = get_image(client, plain, "images/0.png", v=v0, kind="thumb")
    assert again.content == resp.content and timing(again)["cache"][1] == "l2"
    spool = tmp_path / "central-cache" / "spool"
    assert len([p for p in os.listdir(spool) if not p.endswith(".tmp")]) == 1

    # 没有 Pillow 的 Agent 仍原样流式返回 PNG 原图
    resp = get_image(client, plain, "images/wide.png", kind="full")
    assert resp.content == (res / "images" / "wide.png").read_bytes() and timing(resp)["gen"][1] == "agent"

    # 同一张冷图并发 20 个请求只拉取一次原图、生成一次
    fetches = []
    media_agent = client.app.state.scheduler.agent
    original = media_agent.open_stream

    async def counting(host, port, path, *args, **kwargs):
        if path == "/v1/files/raw":
            fetches.append(path)
        return await original(host, port, path, *args, **kwargs)

    monkeypatch.setattr(media_agent, "open_stream", counting)
    wide_v = imaging.version(os.stat(res / "images" / "wide.png"))

    async def burst():
        async with httpx.AsyncClient(base_url=live, timeout=60) as http:
            params = {"path": "images/wide.png", "v": wide_v, "kind": "preview", "size": 1024}
            return await asyncio.gather(*[http.get(f"/api/results/{plain}/image", params=params) for _ in range(20)])

    results = asyncio.run(burst())
    assert [r.status_code for r in results] == [200] * 20 and len({r.content for r in results}) == 1
    assert fetches == ["/v1/files/raw"]
    assert sorted(timing(r)["cache"][1] for r in results).count("miss") == 1
    assert Image.open(io.BytesIO(results[0].content)).size == (1024, 461)

    # 暂存超出容量（不到两倍）时，最近用过的原图不删（可能正在生成），同一张图的其他请求不必重新拉取
    settings = client.app.state.media.settings
    settings.image_spool_mb = 4
    assert get_image(client, plain, "images/1.png", v=files["images/1.png"][2], kind="thumb").status_code == 200
    deadline = time.time() + 5
    while len(fetches) < 2 and time.time() < deadline:
        time.sleep(0.05)
    time.sleep(0.3)  # 拉取后在线程中清理暂存
    assert len([p for p in os.listdir(spool) if not p.endswith(".tmp")]) == 3
    resp = get_image(client, plain, "images/wide.png", v=wide_v, kind="tile", l=0, x=0, y=0)
    assert resp.status_code == 200 and resp.headers["x-image-width"] == "2000"
    assert fetches == ["/v1/files/raw"] * 2

    # 不是图片的文件（如误点的模型权重）读到文件头就返回 415，不下载整个文件
    settings.image_spool_mb = 4096
    with open(res / "weights.bin", "wb") as f:
        f.truncate(64 << 20)
    resp = get_image(client, plain, "weights.bin", kind="thumb")
    assert resp.status_code == 415 and "不是图片" in resp.json()["detail"]
    assert len(os.listdir(spool)) == 3


def png_header(path, w, h, pad):
    """只有文件头的 PNG（声明 w×h），后面补 pad 字节。"""
    def chunk(cid, body):
        return struct.pack(">I", len(body)) + cid + body + struct.pack(">I", zlib.crc32(cid + body))

    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)))
        f.write(chunk(b"IDAT", b"\0" * pad))


def test_spool_limits(env, plain_agent, tmp_path, monkeypatch):
    """暂存原图：文件头的像素数超过上限、文件超过单个文件的上限时不下载完就返回 413，磁盘空间不足时 503。"""
    client, data, plain = plain_agent
    res = data / "model-a"
    spool = tmp_path / "central-cache" / "spool"
    media = client.app.state.media
    fetched = []
    original = media._write_spool

    async def counting(head, rest, path):
        async def counted():
            async for chunk in rest:
                fetched.append(len(chunk))
                yield chunk
        await original(head, counted(), path)

    monkeypatch.setattr(media, "_write_spool", counting)
    for w, h in ((40000, 40000), (20000, 20000)):  # Pillow 的炸弹检查、按 GNM_IMAGE_MAX_PIXELS 检查
        png_header(res / "huge.png", w, h, 64 << 20)
        fetched.clear()
        resp = get_image(client, plain, "huge.png", kind="thumb")
        assert resp.status_code == 413 and "像素" in resp.json()["detail"]
        assert sum(fetched) < 16 << 20
    assert not os.path.exists(spool) or not os.listdir(spool)

    noise(res / "images" / "big.png", (1200, 1000), seed=3)
    media.settings.image_spool_mb = 1
    fetched.clear()
    resp = get_image(client, plain, "images/big.png", kind="thumb")
    assert resp.status_code == 413 and "MB" in resp.json()["detail"] and not fetched
    media.settings.image_spool_mb = 4096
    monkeypatch.setattr(media.cache, "low_space", lambda: True)
    resp = get_image(client, plain, "images/big.png", kind="thumb")
    assert resp.status_code == 503 and resp.headers["retry-after"] == "30"
    assert not [p for p in os.listdir(spool) if not p.endswith(".tmp")] if os.path.exists(spool) else True


def test_old_agent_full(env, monkeypatch):
    """旧版 Agent（没有 features）：原图先读第一块判断格式，PNG 原样转发，TIFF 由中心服务转码。"""
    client, data, server_id = env
    res = data / "model-a"
    Image.fromarray(np.asarray(Image.open(res / "images" / "0.png"))).save(res / "images" / "0.tif")
    a = register(client, server_id, res)

    async def old_agent(host, port):
        return {}

    media_agent = client.app.state.scheduler.agent
    monkeypatch.setattr(media_agent, "features", old_agent)
    fetches = []
    original = media_agent.open_stream

    async def counting(host, port, path, *args, **kwargs):
        fetches.append(path)
        return await original(host, port, path, *args, **kwargs)

    monkeypatch.setattr(media_agent, "open_stream", counting)
    resp = get_image(client, a, "images/0.png", kind="full")
    assert resp.status_code == 200 and resp.content == (res / "images" / "0.png").read_bytes()
    assert (resp.headers["content-type"], resp.headers["x-image-native"]) == ("image/png", "1")
    fetches.clear()
    resp = get_image(client, a, "images/0.tif", kind="full")
    assert resp.status_code == 200 and timing(resp)["gen"][1] == "central"
    assert fetches == ["/v1/files/raw"]  # 判断格式时读到一半的原图接着写入暂存，不再拉取一次
    assert np.array_equal(pixels(resp.content), np.asarray(Image.open(res / "images" / "0.png").convert("RGB")))
    resp = get_image(client, a, "images/0.png", kind="thumb")
    assert resp.status_code == 200 and timing(resp)["gen"][1] == "central"


def test_disconnect_reaches_agent(env, live, tmp_path, monkeypatch):
    """浏览器中断时中心服务关闭到 Agent 的连接，Agent 放弃排队中还没解码的请求。"""
    client, data, _ = env
    server = agent.build_server("127.0.0.1", 0, "t", ["/"], str(tmp_path / "agent3"), [str(data)],
                                image_opts={"cache_dir": str(tmp_path / "agent3-cache"), "workers": 1})
    threading.Thread(target=server.serve_forever, daemon=True).start()
    sid = client.post("/api/servers", json={"name": "one-worker", "host": "127.0.0.1", "port": server.server_address[1]}).json()["id"]
    rid = register(client, sid, data / "model-a")
    decoded = []
    original = imaging.open_display

    def slow(path, *args, **kwargs):
        decoded.append(os.path.basename(path))
        time.sleep(1.5)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(imaging, "open_display", slow)

    async def scenario():
        async with httpx.AsyncClient(base_url=live, timeout=30) as http:
            url = f"/api/results/{rid}/image"
            first = asyncio.create_task(http.get(url, params={"path": "images/0.png"}))
            await asyncio.sleep(0.3)
            second = asyncio.create_task(http.get(url, params={"path": "images/1.png"}))
            await asyncio.sleep(0.3)
            second.cancel()  # 浏览器中断（排队在 Agent 唯一的槽位后面）
            assert (await first).status_code == 200

    try:
        asyncio.run(scenario())
        time.sleep(1.5)
        assert decoded == ["0.png"]
    finally:
        server.shutdown()
        server.server_close()

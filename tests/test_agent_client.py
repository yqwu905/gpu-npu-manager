"""图片请求复用 Agent 长连接、SSH 转发目标的刷新频率、读取文件的目标缓存、按类别限流。"""

import asyncio
import http.client
import json
import threading
import time

import pytest
from fastapi.testclient import TestClient
from PIL import Image

import agent
from app.agent_client import AgentClient, AgentError, ClientGone
from app.config import Settings
from app.main import create_app


@pytest.fixture
def stack(tmp_path, monkeypatch):
    """真实 Agent（记录接受的连接数）+ 中心服务，登记好服务器和一个结果集；返回 (client, 结果集 ID, 连接记录)。"""
    monkeypatch.setenv("GNM_NVIDIA_SMI", "")
    monkeypatch.setenv("GNM_NPU_SMI", "")
    monkeypatch.setattr(agent.shutil, "which", lambda name: None)
    res = tmp_path / "data" / "res"
    (res / "images").mkdir(parents=True)
    for i in range(3):
        Image.new("RGB", (64, 48), (i * 40, 20, 30)).save(res / "images" / f"{i}.png")
    (res / "predictions.jsonl").write_text("".join(json.dumps({"id": i, "image": f"images/{i}.png"}) + "\n" for i in range(3)))
    server = agent.build_server("127.0.0.1", 0, "t", ["/"], str(tmp_path / "agent"), [str(tmp_path / "data")],
                                image_opts={"cache_dir": str(tmp_path / "agent-cache")})
    accepts = []
    get_request = server.get_request

    def counting():
        accepts.append(time.monotonic())
        return get_request()

    server.get_request = counting
    threading.Thread(target=server.serve_forever, daemon=True).start()
    settings = Settings(database_url=f"sqlite:///{tmp_path}/t.db", agent_token="t", enable_poller=False,
                        image_cache_dir=str(tmp_path / "central-cache"))
    with TestClient(create_app(settings)) as client:
        sid = client.post("/api/servers", json={"name": "s", "host": "127.0.0.1", "port": server.server_address[1]}).json()["id"]
        rid = client.post("/api/results", json={"server_id": sid, "path": str(res)}).json()["id"]
        yield client, rid, sid, accepts
    server.shutdown()
    server.server_close()


def test_media_requests_reuse_connections(stack):
    client, rid, _, accepts = stack
    accepts.clear()
    for i in range(20):
        # 不带版本号，每次都访问 Agent（Agent 磁盘缓存命中）
        resp = client.get(f"/api/results/{rid}/image", params={"path": f"images/{i % 3}.png", "kind": "thumb"})
        assert resp.status_code == 200 and resp.headers["content-type"] == "image/jpeg"
    assert client.get(f"/api/results/{rid}/images").status_code == 200
    assert client.get(f"/api/results/{rid}/file", params={"path": "images/0.png"}).status_code == 200
    assert len(accepts) <= 2, len(accepts)


def test_tunnels_refresh_throttled(stack, monkeypatch):
    client, rid, sid, _ = stack
    tunnels = client.app.state.scheduler.agent.tunnels
    calls = []
    refresh = tunnels.refresh
    monkeypatch.setattr(tunnels, "refresh", lambda: (calls.append(1), refresh()))

    async def lookups(n):
        for _ in range(n):
            assert await tunnels.endpoint("10.0.0.9", 9100) == ("10.0.0.9", 9100)

    tunnels._refreshed = 0.0
    asyncio.run(lookups(10))
    assert len(calls) == 1  # 不需要转发的服务器 5 秒内只读一次数据库
    tunnels.invalidate()
    asyncio.run(lookups(3))
    assert len(calls) == 2
    tunnels._refreshed -= 6
    asyncio.run(lookups(3))
    assert len(calls) == 3
    # 修改、删除服务器时失效
    assert client.patch(f"/api/servers/{sid}", json={"note": "x"}).status_code == 200
    asyncio.run(lookups(1))
    assert len(calls) == 4


def test_media_target_cache(stack):
    client, rid, sid, _ = stack
    targets = client.app.state.media_targets
    assert client.get(f"/api/results/{rid}/file", params={"path": "images/0.png"}).status_code == 200
    assert (rid, None) in targets
    assert client.patch(f"/api/servers/{sid}", json={"note": "y"}).status_code == 200
    assert targets == {}
    assert client.get(f"/api/results/{rid}/image", params={"path": "images/0.png"}).status_code == 200
    assert (rid, None) in targets
    assert client.delete(f"/api/results/{rid}").status_code == 204
    assert targets == {}
    assert client.get(f"/api/results/{rid}/file", params={"path": "images/0.png"}).status_code == 404
    assert client.get(f"/api/results/{rid}/image", params={"path": "images/0.png"}).status_code == 404


def test_slot_queue(tmp_path):
    """每个 Agent 每类请求的名额：先来先得，排队超时为 503，浏览器断开时退出排队。"""
    settings = Settings(database_url=f"sqlite:///{tmp_path}/t.db", image_thumb_concurrency=2, image_queue_wait=0.3)
    client = AgentClient(settings)

    async def run():
        held = [await client.slot("h", 1, "thumb") for _ in range(2)]
        order = []

        async def waiter(name):
            slot = await client.slot("h", 1, "thumb")
            order.append(name)
            await asyncio.sleep(0.01)
            slot.release()

        tasks = [asyncio.create_task(waiter(n)) for n in "ab"]
        await asyncio.sleep(0.05)
        held[0].release()
        held[0].release()  # 重复释放无效
        await asyncio.gather(*tasks)
        assert order == ["a", "b"]
        # 另一个 Agent、另一个类别不受影响
        (await client.slot("h", 2, "thumb")).release()
        (await client.slot("h", 1, "main")).release()
        # 名额占满：排队超时、浏览器断开
        extra = await client.slot("h", 1, "thumb")
        started = time.monotonic()
        with pytest.raises(AgentError) as exc:
            await client.slot("h", 1, "thumb")
        assert exc.value.status == 503 and exc.value.retry_after == "1"
        assert 0.25 < time.monotonic() - started < 1.0

        async def gone():
            return True

        with pytest.raises(ClientGone):
            await client.slot("h", 1, "thumb", gone)
        held[1].release()
        extra.release()
        # 排队退出的请求没有占着名额
        slots = [await client.slot("h", 1, "thumb") for _ in range(2)]
        assert client._semaphore("h", 1, "thumb").locked()
        for slot in slots:
            slot.release()
        assert not client._semaphore("h", 1, "thumb").locked()

    asyncio.run(run())


def test_agent_keep_alive_latency(stack):
    """长连接上的小响应不能被 Nagle 与对端延迟确认拖慢（每个约 40 ms）。"""
    client, rid, sid, _ = stack
    port = client.get(f"/api/servers/{sid}").json()["port"]
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    times = []
    for _ in range(10):
        started = time.perf_counter()
        conn.request("GET", "/v1/health", headers={"X-Agent-Token": "t"})
        resp = conn.getresponse()
        assert resp.status == 200 and resp.read()
        times.append(time.perf_counter() - started)
    conn.close()
    assert sorted(times)[5] < 0.02, times


def test_error_body_cut_off(tmp_path):
    """Agent 的错误响应体没读完连接就断了，仍按状态码报错（503 带 Retry-After），失败也缓存。"""
    import httpx

    class Broken(httpx.AsyncByteStream):
        async def __aiter__(self):
            raise httpx.ReadError("connection reset")
            yield b""

    def handler(request):
        return httpx.Response(503, headers={"Retry-After": "2", "Content-Type": "application/json"}, stream=Broken())

    settings = Settings(database_url=f"sqlite:///{tmp_path}/t.db")
    client = AgentClient(settings, transport=httpx.MockTransport(handler))

    async def run():
        with pytest.raises(AgentError) as exc:
            await client.open_stream("h", 1, "/v1/files/raw")
        assert exc.value.status == 503 and exc.value.retry_after == "2"
        with pytest.raises(AgentError):
            await client.features("h", 1)
        assert isinstance(client._features[("h", 1)][1], AgentError)
        await client.aclose()

    asyncio.run(run())


def test_read_log_reuses_connections(stack, tmp_path):
    """反复读取任务日志时应复用长连接，不每次新建 TCP 连接。"""
    client, _, sid, accepts = stack
    port = client.get(f"/api/servers/{sid}").json()["port"]
    agent_client = client.app.state.scheduler.agent
    # 直接在 Agent 上创建一个已结束任务并写入日志
    asyncio.run(agent_client.start_job("127.0.0.1", port, 99, "echo hello-log", str(tmp_path), {}, []))
    accepts.clear()

    async def poll_many():
        for _ in range(20):
            res = await agent_client.read_log("127.0.0.1", port, 99, tail=4096)
            assert res["size"] >= 0

    asyncio.run(poll_many())
    assert len(accepts) <= 2, len(accepts)


def test_read_log_tail_old_agent_fallback(tmp_path):
    """旧版 Agent 忽略 tail/end 且不返回 X-Log-Tail 时，中心服务仅用 1 字节探测 size 再精准回读末尾区间。"""
    import httpx

    content = ("0123456789abcdef" * 1024).encode("utf-8")  # 16 KB
    calls = []

    def old_agent(request: httpx.Request):
        params = dict(request.url.params)
        calls.append(params)
        offset = max(0, min(int(params.get("offset", "0")), len(content)))
        limit = max(1, min(int(params.get("limit", "65536")), 1024 * 1024))
        chunk = content[offset : offset + limit].decode("utf-8")
        return httpx.Response(
            200,
            json={"offset": offset, "next_offset": offset + len(chunk), "size": len(content), "data": chunk},
        )

    settings = Settings(database_url=f"sqlite:///{tmp_path}/t.db")
    client = AgentClient(settings, transport=httpx.MockTransport(old_agent))

    async def run():
        tail = await client.read_log("h", 1, 7, tail=1024)
        assert tail["offset"] == len(content) - 1024
        assert tail["next_offset"] == len(content)
        assert tail["data"] == content[-1024:].decode("utf-8")
        # 第一次探测只传 limit=1，避免传输多余数据；第二次直接按目标偏移量读取
        assert calls[0]["limit"] == "1" and calls[0]["tail"] == "1024"
        assert calls[1] == {"offset": str(len(content) - 1024), "limit": "1024"}

        calls.clear()
        earlier = await client.read_log("h", 1, 7, tail=512, end=tail["offset"])
        assert earlier["offset"] == tail["offset"] - 512
        assert earlier["next_offset"] == tail["offset"]
        assert earlier["data"] == content[tail["offset"] - 512 : tail["offset"]].decode("utf-8")
        await client.aclose()

    asyncio.run(run())

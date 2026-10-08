import asyncio
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

import agent
from app.config import Settings
from app.main import create_app

FIXTURES = Path(__file__).parent / "fixtures"


def agent_payload(accelerator):
    if accelerator == "npu":
        devices = agent.parse_npu_smi_info((FIXTURES / "npu-smi-910b.txt").read_text())
    else:
        devices = agent.parse_nvidia_gpus(
            (FIXTURES / "nvidia-smi-gpu.csv").read_text(), (FIXTURES / "nvidia-smi-apps.csv").read_text()
        )
    return {
        "agent_version": "0.1.0",
        "hostname": f"{accelerator}-host",
        "time": 0,
        "accelerator": accelerator,
        "host": {"cpu_count": 64, "cpu_percent": 12.5, "load1": 1.0, "memory_total_mb": 1000, "memory_used_mb": 100, "disks": []},
        "devices": devices,
        "errors": [],
    }


class FakeAgents:
    """按主机名返回固定状态；down 集合里的主机模拟连接失败。"""

    def __init__(self):
        self.down = set()
        self.tokens = []

    def __call__(self, request: httpx.Request):
        self.tokens.append(request.headers.get("X-Agent-Token"))
        host = request.url.host
        if host in self.down:
            raise httpx.ConnectError("connection refused", request=request)
        return httpx.Response(200, json=agent_payload("npu" if host.startswith("npu") else "gpu"))


@pytest.fixture
def agents():
    return FakeAgents()


@pytest.fixture
def client(tmp_path, agents):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path}/test.db",
        agent_token="secret",
        enable_poller=False,
        offline_after=2,
    )
    app = create_app(settings, transport=httpx.MockTransport(agents))
    with TestClient(app) as c:
        c.app_ref = app
        yield c


def add(client, name, host, **kw):
    resp = client.post("/api/servers", json={"name": name, "host": host, **kw})
    assert resp.status_code == 201, resp.text
    return resp.json()


def poll(client, ids=None):
    asyncio.run(client.app_ref.state.poller.poll(ids))


def test_create_and_poll(client, agents):
    created = add(client, "gpu-01", "gpu1", group="cv", owner="alice", tags=["a100", "a100", " "])
    assert created["tags"] == ["a100"]
    # 创建后会在后台立即拉取一次
    server = client.get(f"/api/servers/{created['id']}").json()
    assert server["status"] == "online"
    assert server["accelerator"] == "gpu"
    assert server["device_count"] == 2
    assert server["models"] == ["NVIDIA A100-SXM4-80GB"]
    # 卡 0 有进程，卡 1 空闲
    assert [d["idle"] for d in server["devices"]] == [False, True]
    assert server["idle_device_count"] == 1
    assert agents.tokens[-1] == "secret"


def test_npu_idle_threshold(client):
    created = add(client, "npu-01", "npu1")
    server = client.get(f"/api/servers/{created['id']}").json()
    assert server["accelerator"] == "npu"
    # NPU 0 有进程；NPU 7 无进程，HBM 3179MB 低于默认阈值 6144MB
    assert [(d["npu_id"], d["idle"]) for d in server["devices"]] == [(0, False), (7, True)]


def test_offline_after_failures(client, agents):
    created = add(client, "gpu-01", "gpu1")
    agents.down.add("gpu1")
    poll(client)
    server = client.get(f"/api/servers/{created['id']}").json()
    assert server["status"] == "online"  # 只失败 1 次
    poll(client)
    server = client.get(f"/api/servers/{created['id']}").json()
    assert server["status"] == "offline"
    assert "ConnectError" in server["last_error"]
    # 离线服务器的卡不算空闲
    assert server["idle_device_count"] == 0
    agents.down.clear()
    server = client.post(f"/api/servers/{created['id']}/refresh").json()
    assert server["status"] == "online"


def test_filters_and_groups(client):
    add(client, "gpu-01", "gpu1", group="cv", owner="alice", tags=["a100", "lab1"])
    add(client, "gpu-02", "gpu2", group="nlp", owner="bob", tags=["a100"])
    add(client, "npu-01", "npu1", group="cv", owner="alice", tags=["lab1"], schedulable=False)

    names = lambda resp: [s["name"] for s in resp.json()]
    assert names(client.get("/api/servers", params={"group": "cv"})) == ["gpu-01", "npu-01"]
    assert names(client.get("/api/servers", params=[("tag", "a100"), ("tag", "lab1")])) == ["gpu-01"]
    assert names(client.get("/api/servers", params={"accelerator": "npu"})) == ["npu-01"]
    assert names(client.get("/api/servers", params={"q": "GPU-0"})) == ["gpu-01", "gpu-02"]
    assert names(client.get("/api/servers", params={"schedulable": "false"})) == ["npu-01"]
    assert names(client.get("/api/servers", params={"model": "910B2"})) == ["npu-01"]

    groups = client.get("/api/servers/grouped", params={"by": "tag"}).json()
    assert [(g["key"], [s["name"] for s in g["servers"]]) for g in groups] == [
        ("a100", ["gpu-01", "gpu-02"]),
        ("lab1", ["gpu-01", "npu-01"]),
    ]
    groups = client.get("/api/servers/grouped", params={"by": "owner", "include_devices": "false"}).json()
    assert [(g["key"], g["server_count"], g["device_count"]) for g in groups] == [("alice", 2, 4), ("bob", 1, 2)]
    assert groups[0]["servers"][0]["devices"] == []

    options = client.get("/api/meta/filters").json()
    assert options == {
        "groups": ["cv", "nlp"],
        "owners": ["alice", "bob"],
        "tags": ["a100", "lab1"],
        "models": ["910B2", "NVIDIA A100-SXM4-80GB"],
        "accelerators": ["gpu", "npu"],
    }

    overview = client.get("/api/overview").json()
    assert overview["servers_total"] == 3 and overview["servers_online"] == 3
    assert overview["devices_total"] == 6 and overview["devices_idle"] == 3
    assert overview["by_accelerator"]["npu"] == {"servers": 1, "devices": 2, "idle_devices": 1}


def test_update_and_delete(client):
    created = add(client, "gpu-01", "gpu1")
    add(client, "gpu-02", "gpu2")
    resp = client.patch(f"/api/servers/{created['id']}", json={"owner": "carol", "tags": ["x"], "group": None})
    assert resp.status_code == 200
    body = resp.json()
    assert body["owner"] == "carol" and body["tags"] == ["x"] and body["group"] is None
    assert client.patch(f"/api/servers/{created['id']}", json={"name": "gpu-02"}).status_code == 409
    assert client.patch(f"/api/servers/{created['id']}", json={"name": None}).status_code == 422
    assert client.post("/api/servers", json={"name": "gpu-02", "host": "x"}).status_code == 409

    history = client.get(f"/api/servers/{created['id']}/history").json()
    assert [h["device_index"] for h in history] == [0, 1]
    assert history[0]["points"][0]["memory_used_mb"] == 1024

    assert client.delete(f"/api/servers/{created['id']}").status_code == 204
    assert client.get(f"/api/servers/{created['id']}").status_code == 404

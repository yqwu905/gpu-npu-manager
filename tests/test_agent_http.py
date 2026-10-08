"""启动真实的 Agent HTTP 服务（用脚本模拟 npu-smi），验证中心服务能拉取并入库。"""

import asyncio
import json
import stat
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

import agent
from app.config import Settings
from app.main import create_app

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def running_agent(tmp_path, monkeypatch):
    fake = tmp_path / "npu-smi"
    fake.write_text(f"#!/bin/sh\ncat {FIXTURES / 'npu-smi-910b.txt'}\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("GNM_NPU_SMI", str(fake))
    monkeypatch.setenv("GNM_NVIDIA_SMI", "")
    monkeypatch.setattr(agent.shutil, "which", lambda name: None)
    server = agent.build_server("127.0.0.1", 0, "secret", ["/"])
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_address[1]
    server.shutdown()


def get(port, path, token):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers={"X-Agent-Token": token})
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read())


def test_agent_requires_token(running_agent):
    with pytest.raises(urllib.error.HTTPError) as exc:
        get(running_agent, "/v1/status", "wrong")
    assert exc.value.code == 401
    assert get(running_agent, "/v1/health", "secret")["ok"] is True


def test_agent_status(running_agent):
    status = get(running_agent, "/v1/status", "secret")
    assert status["accelerator"] == "npu"
    assert status["errors"] == []
    assert [d["npu_id"] for d in status["devices"]] == [0, 7]
    assert status["host"]["memory_total_mb"] > 0


def test_server_polls_real_agent(running_agent, tmp_path):
    settings = Settings(database_url=f"sqlite:///{tmp_path}/t.db", agent_token="secret", enable_poller=False)
    app = create_app(settings)
    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        created = client.post("/api/servers", json={"name": "npu-01", "host": "127.0.0.1", "port": running_agent}).json()
        asyncio.run(app.state.poller.poll())
        server = client.get(f"/api/servers/{created['id']}").json()
    assert server["status"] == "online"
    assert server["device_count"] == 2
    assert server["devices"][0]["processes"][0]["pid"] == 218169

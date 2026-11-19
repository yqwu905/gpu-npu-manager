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
    server = agent.build_server("127.0.0.1", 0, "secret", ["/"], str(tmp_path / "agent-data"))
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


def test_job_runs_on_real_agent(running_agent, tmp_path):
    import time

    settings = Settings(database_url=f"sqlite:///{tmp_path}/t.db", agent_token="secret", enable_poller=False)
    app = create_app(settings)
    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        client.post("/api/servers", json={"name": "npu-01", "host": "127.0.0.1", "port": running_agent})
        asyncio.run(app.state.poller.poll())
        job = client.post(
            "/api/jobs", json={"command": 'echo "cards=$ASCEND_RT_VISIBLE_DEVICES"', "workdir": str(tmp_path)}
        ).json()
        asyncio.run(app.state.scheduler.run_once())
        # NPU 0 上有进程，只有逻辑卡 1（NPU 7）空闲
        assert client.get(f"/api/jobs/{job['id']}").json()["device_indices"] == [1]
        for _ in range(100):
            asyncio.run(app.state.scheduler.run_once())
            status = client.get(f"/api/jobs/{job['id']}").json()
            if status["status"] == "succeeded":
                break
            time.sleep(0.05)
        assert status["status"] == "succeeded" and status["exit_code"] == 0
        log = client.get(f"/api/jobs/{job['id']}/log").json()
        assert log["data"] == "cards=1\n"

        # 写入较大日志，验证 Agent 与中心服务的 gzip 压缩及 tail / end 分段倒序拉取
        import gzip

        lines = [f"step={i:05d} loss={1.0 / (i + 1):.6f} 日志内容\n" for i in range(4000)]
        full_text = "".join(lines)
        log_path = tmp_path / "agent-data" / "jobs" / f"gnm-{job['id']}.log"
        log_path.write_text(full_text, encoding="utf-8")
        total_size = log_path.stat().st_size

        # 直接访问 Agent 验证 gzip 和 X-Log-Tail 响应头
        req = urllib.request.Request(
            f"http://127.0.0.1:{running_agent}/v1/jobs/gnm-{job['id']}/log?tail=16384",
            headers={"X-Agent-Token": "secret", "Accept-Encoding": "gzip"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            assert resp.headers.get("Content-Encoding") == "gzip"
            assert resp.headers.get("X-Log-Tail") == "1"
            raw = resp.read()
            assert len(raw) < 16384  # 压缩后远小于明文
            agent_tail = json.loads(gzip.decompress(raw))
        assert agent_tail["next_offset"] == total_size and agent_tail["size"] == total_size
        assert full_text.endswith(agent_tail["data"])

        # 通过中心服务验证 gzip 与 tail + end 无损拼回全文
        r1 = client.get(f"/api/jobs/{job['id']}/log", params={"tail": 32768}, headers={"Accept-Encoding": "gzip"})
        assert r1.status_code == 200 and r1.headers.get("content-encoding") == "gzip"
        d1 = r1.json()
        assert d1["next_offset"] == total_size and d1["offset"] > 0
        chunks = [d1["data"]]
        cur_end = d1["offset"]
        while cur_end > 0:
            r = client.get(f"/api/jobs/{job['id']}/log", params={"tail": 32768, "end": cur_end})
            assert r.status_code == 200
            d = r.json()
            assert d["next_offset"] == cur_end
            chunks.append(d["data"])
            cur_end = d["offset"]
        chunks.reverse()
        assert "".join(chunks) == full_text

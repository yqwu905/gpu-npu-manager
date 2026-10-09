"""通过 SSH 安装、升级 Agent。

用一个假的 ssh 命令把安装脚本交给本机 bash 执行（HOME 指向临时目录），真实启动 Agent 并通过 HTTP 验证。
"""
import asyncio
import os
import signal
import socket
import sqlite3
import stat
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.deployer import package_version
from app.evaluations import build_command
from app.main import create_app
from app.models import Evaluation, ResultSet

ROOT = Path(__file__).resolve().parent.parent


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def fake_ssh(tmp_path, body):
    path = tmp_path / "fake-ssh"
    path.write_text("#!/bin/sh\n" + body + "\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


def stop_agent(home: Path):
    pid_file = home / ".gnm-agent" / "agent.pid"
    if pid_file.exists():
        try:
            os.killpg(int(pid_file.read_text()), signal.SIGTERM)
        except ProcessLookupError:
            pass


def make_client(tmp_path, **kw):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path}/t.db", enable_poller=False, agent_token="secret", agent_timeout=2, **kw
    )
    return TestClient(create_app(settings))


def wait_deploys(client):
    client.portal.call(client.app.state.deployer.wait)


def test_install_and_upgrade(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("FAKE_SSH_HOME", str(home))
    monkeypatch.setenv("FAKE_SSH_LOG", str(tmp_path / "ssh.log"))
    ssh = str(ROOT / "tests" / "fake_ssh.py")
    port = free_port()
    try:
        with make_client(tmp_path, ssh_command=ssh) as client:
            pkg = client.get("/api/agent-package").json()
            assert pkg["version"] == package_version(str(ROOT / "agent")) and pkg["ssh_available"]

            resp = client.post(
                "/api/servers/batch",
                json={
                    "servers": [
                        {"host": "127.0.0.1", "port": port, "ssh_user": "alice", "group": "cv"},
                        {"host": "10.0.0.9", "name": "manual"},
                        {"host": "10.0.0.10", "name": "manual"},
                    ]
                },
            )
            assert resp.status_code == 201, resp.text
            body = resp.json()
            assert [s["name"] for s in body["created"]] == ["127.0.0.1", "manual"]
            assert body["errors"] == [{"index": 2, "host": "10.0.0.10", "error": "名称 manual 已存在"}]
            sid = body["created"][0]["id"]
            assert body["created"][0]["deploy"]["status"] == "pending"
            assert body["created"][1]["deploy"] is None and not body["created"][1]["managed"]

            wait_deploys(client)
            server = client.get(f"/api/servers/{sid}").json()
            assert server["deploy"]["status"] == "succeeded", client.get(f"/api/servers/{sid}/deploy").json()
            assert server["deploy"]["action"] == "install"
            assert server["status"] == "online" and server["agent_version"] == pkg["version"]
            assert not server["agent_outdated"] and server["managed"]
            env = (home / ".gnm-agent" / "bin" / "agent.env").read_text()
            assert "GNM_AGENT_TOKEN=secret" in env and "GNM_AGENT_ALLOW_ROOTS" not in env
            # 默认经 SSH 转发访问，Agent 只监听本机
            assert "GNM_AGENT_HOST=127.0.0.1" in env and server["ssh_tunnel"]
            assert f"-N -o ExitOnForwardFailure=yes" in (tmp_path / "ssh.log").read_text()
            assert "已安装" in client.get(f"/api/servers/{sid}/deploy").json()["log"]
            first_pid = (home / ".gnm-agent" / "agent.pid").read_text()

            # 升级：只有托管且版本落后或未安装的服务器会被选中
            resp = client.post("/api/servers/deploy", json={"outdated": True})
            assert resp.json() == []
            # 改为直接连接后重新安装，Agent 对外监听
            client.patch(f"/api/servers/{sid}", json={"ssh_tunnel": False})
            resp = client.post(f"/api/servers/{sid}/deploy")
            assert resp.status_code == 200 and resp.json()["deploy"]["action"] == "upgrade"
            assert client.post(f"/api/servers/{sid}/deploy").status_code == 409  # 正在安装中
            wait_deploys(client)
            server = client.get(f"/api/servers/{sid}").json()
            assert server["deploy"]["status"] == "succeeded" and server["status"] == "online"
            assert "GNM_AGENT_HOST" not in (home / ".gnm-agent" / "bin" / "agent.env").read_text()
            assert (home / ".gnm-agent" / "agent.pid").read_text() != first_pid
            assert client.post(f"/api/servers/{body['created'][1]['id']}/deploy").status_code == 409
    finally:
        stop_agent(home)


def test_deploy_failures(tmp_path):
    ssh = fake_ssh(tmp_path, 'cat > /dev/null; echo "alice@h: Permission denied (publickey)." >&2; exit 255')
    with make_client(tmp_path, ssh_command=ssh) as client:
        sid = client.post("/api/servers", json={"host": "h", "ssh_user": "alice"}).json()["id"]
        wait_deploys(client)
        deploy = client.get(f"/api/servers/{sid}/deploy").json()
        assert deploy["status"] == "failed"
        assert deploy["error"].startswith("SSH 连接失败：alice@h: Permission denied")

    # 脚本执行成功但 Agent 起不来（端口上没有服务）
    import app.deployer as deployer_module

    ssh = fake_ssh(tmp_path, "cat > /dev/null; echo ok")
    old_wait, deployer_module.HEALTH_WAIT = deployer_module.HEALTH_WAIT, 1
    try:
        with make_client(tmp_path / "b", ssh_command=ssh) as client:
            sid = client.post("/api/servers", json={"host": "127.0.0.1", "port": free_port(), "ssh_user": "a"}).json()["id"]
            wait_deploys(client)
            assert "连不上 Agent" in client.get(f"/api/servers/{sid}").json()["deploy"]["error"]
    finally:
        deployer_module.HEALTH_WAIT = old_wait


def test_restart_marks_interrupted_and_auto_upgrade(tmp_path):
    with make_client(tmp_path, ssh_command="true") as client:
        sid = client.post("/api/servers", json={"host": "h", "ssh_user": "a"}, params={"deploy": False}).json()["id"]
        other = client.post("/api/servers", json={"host": "h2"}).json()["id"]
        with client.app.state.session_factory() as session:
            from app.models import Server

            for server_id in (sid, other):
                server = session.get(Server, server_id)
                server.status, server.agent_version = "online", "0.0.1+old"
            session.commit()
        submitted = []
        client.app.state.deployer.submit = lambda ids: submitted.append(ids) or ids
        client.portal.call(client.app.state.deployer.auto_upgrade)
        assert submitted == [[sid]]  # 没有 SSH 信息的服务器不自动升级

        client.app.state.deployer.submit = type(client.app.state.deployer).submit.__get__(client.app.state.deployer)
        with client.app.state.session_factory() as session:
            server = session.get(Server, sid)
            server.deploy_status, server.deploy_version = "running", client.app.state.deployer.version
            session.commit()
    with make_client(tmp_path, ssh_command="true") as client:
        deploy = client.get(f"/api/servers/{sid}").json()["deploy"]
        assert deploy["status"] == "failed" and "中断" in deploy["error"]
        # 同一版本已经尝试过，不再自动升级
        submitted = []
        client.app.state.deployer.submit = lambda ids: submitted.append(ids) or ids
        client.portal.call(client.app.state.deployer.auto_upgrade)
        assert submitted == []


def test_old_database_gets_new_columns(tmp_path):
    db = tmp_path / "t.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE servers (id INTEGER PRIMARY KEY, name VARCHAR(128) UNIQUE, host VARCHAR(255), port INTEGER, "
        '"group" VARCHAR(128), owner VARCHAR(128), tags JSON, note TEXT, schedulable BOOLEAN, accelerator VARCHAR(16), '
        "status VARCHAR(16), hostname VARCHAR(255), agent_version VARCHAR(32), host_info JSON, last_seen_at DATETIME, "
        "last_error TEXT, fail_count INTEGER, created_at DATETIME, updated_at DATETIME)"
    )
    conn.execute(
        "INSERT INTO servers (name, host, port, tags, schedulable, status, fail_count, created_at, updated_at) "
        "VALUES ('old', 'h', 9100, '[]', 1, 'unknown', 0, '2026-10-01 00:00:00', '2026-10-01 00:00:00')"
    )
    conn.commit()
    conn.close()
    with make_client(tmp_path) as client:
        old = client.get("/api/servers").json()[0]
        assert (old["name"], old["ssh_user"], old["ssh_port"], old["deploy"]) == ("old", None, 22, None)
        assert client.patch(f"/api/servers/{old['id']}", json={"ssh_user": " bob "}).json()["ssh_user"] == "bob"


def test_eval_command_uses_agent_dir():
    result = ResultSet(path="/data/r1")
    evaluation = Evaluation(output_dir="/data/r1/eval/1", metrics=["psnr"], reference=None)
    command = build_command(Settings(eval_script=""), result, evaluation, "cpu")
    assert command.startswith('python3 "$GNM_AGENT_DIR/evaluate.py" --predictions /data/r1/')


def test_ssh_target_uses_alias(tmp_path):
    ssh = fake_ssh(tmp_path, f'cat > /dev/null; echo "$@" > "{tmp_path}/args"; exit 255')
    with make_client(tmp_path, ssh_command=ssh) as client:
        body = {"host": "10.0.1.5", "ssh_host": "gpu-05", "ssh_user": "alice", "ssh_port": 2222}
        client.post("/api/servers", json=body)
        wait_deploys(client)
    args = (tmp_path / "args").read_text().split()
    assert args[-3:] == ["alice@gpu-05", "bash", "-s"] and "2222" in args


def test_tunnel_failure_reported(tmp_path):
    ssh = fake_ssh(tmp_path, 'echo "alice@h: Permission denied (publickey)." >&2; exit 255')
    with make_client(tmp_path, ssh_command=ssh) as client:
        sid = client.post("/api/servers", json={"host": "h", "ssh_user": "alice"}, params={"deploy": False}).json()["id"]
        server = client.post(f"/api/servers/{sid}/refresh").json()
        assert server["last_error"] == "SSH 转发失败：alice@h: Permission denied (publickey)."
        # 关闭转发后直接连接
        client.patch(f"/api/servers/{sid}", json={"ssh_tunnel": False})
        assert "SSH" not in client.post(f"/api/servers/{sid}/refresh").json()["last_error"]


def test_failed_deploy_cleared_when_agent_reachable(tmp_path):
    """安装时连不上 Agent，之后（例如改走 SSH 转发）连上且版本正确，安装失败的提示应消除。"""
    from app.models import Server
    from app.poller import apply_status

    with make_client(tmp_path) as client:
        version = client.app.state.deployer.version
        sid = client.post("/api/servers", json={"host": "h", "ssh_user": "a"}, params={"deploy": False}).json()["id"]
        with client.app.state.session_factory() as session:
            server = session.get(Server, sid)
            server.deploy_status, server.deploy_version, server.deploy_error = "failed", version, "20 秒内连不上 Agent"
            apply_status(session, server, {"agent_version": "0.0.1+old"}, client.app.state.settings, server.created_at)
            assert server.deploy_status == "failed"  # 版本不对，仍算失败
            apply_status(session, server, {"agent_version": version}, client.app.state.settings, server.created_at)
            session.commit()
        deploy = client.get(f"/api/servers/{sid}").json()["deploy"]
        assert deploy["status"] == "succeeded" and deploy["error"] is None


def test_relay_when_forwarding_prohibited(tmp_path, monkeypatch):
    """sshd 禁止端口转发时，改为经 ssh 标准输入输出中继访问 Agent。"""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("FAKE_SSH_HOME", str(home))
    monkeypatch.setenv("FAKE_SSH_LOG", str(tmp_path / "ssh.log"))
    monkeypatch.setenv("FAKE_SSH_NO_FORWARD", "1")
    ssh = str(ROOT / "tests" / "fake_ssh.py")
    try:
        with make_client(tmp_path, ssh_command=ssh) as client:
            sid = client.post("/api/servers", json={"host": "127.0.0.1", "port": free_port(), "ssh_user": "alice"}).json()["id"]
            wait_deploys(client)
            server = client.get(f"/api/servers/{sid}").json()
            assert server["deploy"]["status"] == "succeeded", server["deploy"]
            assert server["status"] == "online"
            log = (tmp_path / "ssh.log").read_text()
            assert "ControlMaster=auto" in log and "exec python3 -c" in log
            # 每次请求新建一个中继连接
            for _ in range(3):
                assert client.post(f"/api/servers/{sid}/refresh").json()["status"] == "online"
        assert "-O exit" in (tmp_path / "ssh.log").read_text()
    finally:
        stop_agent(home)

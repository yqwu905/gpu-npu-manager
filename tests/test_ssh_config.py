from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.ssh_config import list_hosts

CONFIG = """
# 注释
Include conf.d/*.conf

Host gpu-01 gpu-02
    HostName 10.0.1.%h
    User alice

Host npu-01
    HostName=10.0.2.11
    Port 2222

Host jump *.internal !secret
    ProxyJump bastion

Match host other
    User nobody

Host *
    User fallback
    Port 22022
"""


def write_config(home):
    ssh = home / ".ssh"
    (ssh / "conf.d").mkdir(parents=True)
    (ssh / "config").write_text(CONFIG)
    (ssh / "conf.d" / "lab.conf").write_text('Host "lab box"\n  HostName 10.9.9.9\n  User Bob\n')


def test_list_hosts(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USER", "me")
    write_config(tmp_path)
    hosts = {h.alias: h for h in list_hosts()}
    assert list(hosts) == ["lab box", "gpu-01", "gpu-02", "npu-01", "jump"]
    assert (hosts["gpu-01"].hostname, hosts["gpu-01"].user, hosts["gpu-01"].port) == ("10.0.1.gpu-01", "alice", 22022)
    # 每个选项取第一个匹配的值，Host * 只补充前面没设置的
    assert (hosts["npu-01"].hostname, hosts["npu-01"].user, hosts["npu-01"].port) == ("10.0.2.11", "fallback", 2222)
    assert (hosts["jump"].hostname, hosts["jump"].user) == ("jump", "fallback")
    assert (hosts["lab box"].hostname, hosts["lab box"].user) == ("10.9.9.9", "Bob")
    assert list_hosts(tmp_path / "missing") == []


def test_import_endpoint_and_ssh_target(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    write_config(tmp_path)
    settings = Settings(database_url=f"sqlite:///{tmp_path}/t.db", enable_poller=False)
    with TestClient(create_app(settings)) as client:
        client.post("/api/servers", json={"host": "10.0.2.11", "name": "npu"})
        data = client.get("/api/ssh-config").json()
        assert data["path"] == str(tmp_path / ".ssh" / "config")
        added = {h["alias"]: h["added"] for h in data["hosts"]}
        assert added["npu-01"] and not added["gpu-01"]

        body = {"host": "10.0.1.gpu-01", "name": "gpu-01", "ssh_host": "gpu-01", "ssh_user": "alice", "ssh_port": 22022}
        server = client.post("/api/servers", json=body, params={"deploy": False}).json()
        assert server["ssh_host"] == "gpu-01"
        assert client.get("/api/ssh-config").json()["hosts"][1]["added"]
        assert client.patch(f"/api/servers/{server['id']}", json={"ssh_host": " "}).json()["ssh_host"] is None

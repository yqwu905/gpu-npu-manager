from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def make_client(tmp_path, web_dir):
    settings = Settings(database_url=f"sqlite:///{tmp_path}/test.db", enable_poller=False, web_dir=str(web_dir))
    return TestClient(create_app(settings))


def test_serves_built_frontend(tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><title>算力平台</title>", encoding="utf-8")
    with make_client(tmp_path, dist) as client:
        assert client.get("/ui/").text.startswith("<!doctype html>")
        resp = client.get("/", follow_redirects=False)
        assert resp.status_code == 307 and resp.headers["location"] == "/ui/"
        resp = client.get("/favicon.ico", follow_redirects=False)
        assert resp.headers["location"] == "/ui/favicon.svg"
        # 前端挂载不影响接口
        assert client.get("/api/servers").json() == []


def test_without_frontend_only_api(tmp_path):
    with make_client(tmp_path, tmp_path / "missing") as client:
        assert client.get("/ui/").status_code == 404
        assert client.get("/api/servers").status_code == 200

"""本地外部应用的跨域调用、无需鉴权约定与输入校验。"""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


@pytest.fixture
def client(tmp_path):
    settings = Settings(database_url=f"sqlite:///{tmp_path}/api.db", enable_poller=False)
    with TestClient(create_app(settings)) as client:
        yield client


@pytest.mark.parametrize("origin", [
    "http://localhost:3000", "http://127.0.0.1:5173", "https://localhost",
    "http://[::1]:9000", "https://127.0.0.1", "https://[::1]",
])
def test_local_browser_calls(client, origin):
    preflight = client.options("/api/jobs", headers={
        "Origin": origin, "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "Content-Type",
    })
    assert preflight.status_code == 200
    assert preflight.headers["access-control-allow-origin"] == origin
    assert "POST" in preflight.headers["access-control-allow-methods"]
    resp = client.get("/api/servers", headers={"Origin": origin})
    assert resp.status_code == 200 and resp.json() == []
    assert resp.headers["access-control-allow-origin"] == origin
    # 业务错误也可被外部浏览器读取。
    resp = client.post("/api/jobs", json={"command": " "}, headers={"Origin": origin})
    assert resp.status_code == 422 and resp.headers["access-control-allow-origin"] == origin


@pytest.mark.parametrize("origin", [
    "https://example.com", "http://localhost.example.com:3000", "http://127.0.0.1.evil:3000", "null",
])
def test_other_origins_have_no_cors_access(client, origin):
    resp = client.options("/api/jobs", headers={
        "Origin": origin, "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "Content-Type",
    })
    assert resp.status_code == 400
    assert "access-control-allow-origin" not in resp.headers


def test_public_api_contract(client):
    spec = client.get("/openapi.json").json()
    assert spec == json.loads((Path(__file__).resolve().parents[1] / "docs" / "openapi.json").read_text())
    operations = {
        "/api/servers": "get", "/api/jobs": "post", "/api/results": "post",
        "/api/evaluations": "post", "/api/evaluations/{evaluation_id}": "get",
        "/api/projects/{project_id}/evaluations": "post",
    }
    assert not spec.get("security") and not spec["components"].get("securitySchemes")
    for path, method in operations.items():
        assert method in spec["paths"][path]
        assert not spec["paths"][path][method].get("security")
    assert client.get("/api/jobs").json() == {"total": 0, "items": []}
    assert client.get("/api/evaluations").json() == []
    assert client.get("/docs").status_code == 200
    assert client.get("/redoc").status_code == 200


@pytest.mark.parametrize("command", ["", " ", "\t\n"])
def test_blank_command_is_validation_error(client, command):
    resp = client.post("/api/jobs", json={"command": command})
    assert resp.status_code == 422
    assert client.get("/api/jobs").json()["total"] == 0

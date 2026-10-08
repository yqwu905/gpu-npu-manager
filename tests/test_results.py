"""在真实 Agent 上登记结果集、运行评测脚本，并验证浏览和对比接口。"""

import asyncio
import json
import sys
import threading
import time
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

import agent
from app.config import Settings
from app.main import create_app

ROOT = Path(__file__).resolve().parent.parent


def make_result(base: Path, name: str, noise: float, texts: list[str], gt: Path):
    (base / "images").mkdir(parents=True)
    (base / "meta.json").write_text(json.dumps({"name": name, "model": name, "dataset": "demo"}))
    rng = np.random.default_rng(0)
    lines = []
    for i, text in enumerate(texts):
        ref = np.asarray(Image.open(gt / f"{i}.png"), dtype=np.float64)
        pred = np.clip(ref + rng.normal(0, noise, ref.shape), 0, 255).astype(np.uint8)
        Image.fromarray(pred).save(base / "images" / f"{i}.png")
        lines.append({"id": f"s{i}", "image": f"images/{i}.png", "ref_image": str(gt / f"{i}.png"),
                      "text": text, "ref_text": ["hello", "world", "中文"][i]})
    (base / "predictions.jsonl").write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in lines) + "\n")


@pytest.fixture
def env(tmp_path, monkeypatch):
    data = tmp_path / "data"
    gt = data / "gt"
    gt.mkdir(parents=True)
    rng = np.random.default_rng(1)
    for i in range(3):
        Image.fromarray(rng.integers(0, 256, (24, 32, 3)).astype(np.uint8)).save(gt / f"{i}.png")
    make_result(data / "model-a", "model-a", 5, ["hello", "world", "中文"], gt)
    make_result(data / "model-b", "model-b", 30, ["hello", "wrld", "中"], gt)

    monkeypatch.setenv("GNM_NVIDIA_SMI", "")
    monkeypatch.setenv("GNM_NPU_SMI", "")
    monkeypatch.setattr(agent.shutil, "which", lambda name: None)
    server = agent.build_server("127.0.0.1", 0, "t", ["/"], str(tmp_path / "agent"), [str(data)])
    threading.Thread(target=server.serve_forever, daemon=True).start()

    settings = Settings(
        database_url=f"sqlite:///{tmp_path}/t.db", agent_token="t", enable_poller=False,
        eval_python=sys.executable, eval_script=str(ROOT / "agent" / "evaluate.py"),
    )
    app = create_app(settings)
    with TestClient(app) as client:
        resp = client.post("/api/servers", json={"name": "srv", "host": "127.0.0.1", "port": server.server_address[1]})
        asyncio.run(app.state.poller.poll())
        yield client, data, resp.json()["id"]
    server.shutdown()


def run_until(client, check, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        asyncio.run(client.app.state.scheduler.run_once())
        if check():
            return
        time.sleep(0.1)
    raise AssertionError("timeout")


def test_results_flow(env):
    client, data, server_id = env
    # 登记
    resp = client.post("/api/results", json={"server_id": server_id, "path": str(data / "model-a")})
    assert resp.status_code == 201, resp.text
    a = resp.json()
    assert (a["name"], a["sample_count"], a["meta"]["dataset"], a["metrics"]) == ("model-a", 3, "demo", {})
    b = client.post("/api/results", json={"server_id": server_id, "path": str(data / "model-b"), "name": "B"}).json()
    assert b["name"] == "B"
    assert client.post("/api/results", json={"server_id": server_id, "path": "/etc"}).status_code == 422
    assert client.post("/api/results", json={"server_id": server_id, "path": str(data / "gt")}).status_code == 422

    # 评测
    metrics = ["psnr", "ssim", "lpips", "ocr_a", "cer", "ned"]
    evals = [client.post("/api/evaluations", json={"result_set_id": r["id"], "metrics": metrics}).json()
             for r in (a, b)]
    assert evals[0]["status"] == "pending" and evals[0]["job_id"]
    assert client.get(f"/api/results/{a['id']}").json()["evaluating"] is True

    def done():
        return all(client.get(f"/api/evaluations/{e['id']}").json()["status"] == "succeeded" for e in evals)

    run_until(client, done)
    ev = client.get(f"/api/evaluations/{evals[1]['id']}").json()
    assert ev["values"]["ocr_a"] == pytest.approx(1 / 3)
    assert ev["values"]["cer"] == pytest.approx(2 / 12)  # 1 + 1 个错误 / 5+5+2 个参考字符
    assert ev["values"]["lpips"] is None and "lpips" in ev["errors"]
    assert ev["counts"]["psnr"] == 3

    results = {r["name"]: r for r in client.get("/api/results").json()}
    assert results["model-a"]["metrics"]["psnr"] > results["B"]["metrics"]["psnr"]
    assert results["model-a"]["metrics"]["ocr_a"] == 1.0
    assert "lpips" not in results["model-a"]["metrics"]

    # 样本浏览与文件
    page = client.get(f"/api/results/{a['id']}/samples", params={"offset": 1, "limit": 1}).json()
    assert page["total"] == 3 and page["items"][0]["id"] == "s1"
    assert set(page["items"][0]["metrics"]) == {"psnr", "ssim", "ocr_a", "cer", "ned"}
    img = client.get(f"/api/results/{a['id']}/file", params={"path": "images/0.png"})
    assert img.status_code == 200 and img.headers["content-type"] == "image/png"
    assert client.get(f"/api/results/{a['id']}/file", params={"path": "../../../etc/passwd"}).status_code == 404

    # 对比
    cmp = client.get("/api/compare/metrics", params=[("ids", a["id"]), ("ids", b["id"])]).json()
    assert [m["name"] for m in cmp["metrics"]] == ["psnr", "ssim", "ocr_a", "cer", "ned"]
    assert cmp["values"][str(b["id"])]["cer"] == pytest.approx(2 / 12)
    samples = client.get("/api/compare/samples", params=[("ids", a["id"]), ("ids", b["id"]),
                                                          ("sort_metric", "ned"), ("limit", 2)]).json()
    assert samples["total"] == 3
    # 差异最大的是 s2（中文 vs 中，1-NED 差 0.5），其次 s1（world vs wrld，差 0.2）
    assert [item["id"] for item in samples["items"]] == ["s2", "s1"]
    assert samples["items"][0]["spread"] == pytest.approx(0.5)
    assert samples["items"][0]["results"][str(b["id"])]["text"] == "中"

    assert client.get("/api/evaluators").json()[2] == {
        "name": "lpips", "label": "LPIPS", "kind": "image", "unit": None, "higher_is_better": False,
        "description": "感知距离（AlexNet），需要服务器上安装 torch 和 lpips",
    }
    assert client.delete(f"/api/results/{b['id']}").status_code == 204
    assert len(client.get("/api/results").json()) == 1


def test_failed_evaluation(env):
    client, data, server_id = env
    (data / "broken").mkdir()
    (data / "broken" / "predictions.jsonl").write_text("not json\n")
    r = client.post("/api/results", json={"server_id": server_id, "path": str(data / "broken")}).json()
    ev = client.post("/api/evaluations", json={"result_set_id": r["id"], "metrics": ["psnr"]}).json()
    run_until(client, lambda: client.get(f"/api/evaluations/{ev['id']}").json()["status"] == "failed")
    assert "评测任务失败" in client.get(f"/api/evaluations/{ev['id']}").json()["error"]

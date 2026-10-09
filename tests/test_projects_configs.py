"""项目归档、结果标签筛选、评测配置，以及拷贝到评测服务器上评测。"""

import asyncio
import json
import shlex
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
from test_results import run_until

ROOT = Path(__file__).resolve().parent.parent


def save(path: Path, array) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(np.clip(array, 0, 255).astype(np.uint8)).save(path)


@pytest.fixture
def env(tmp_path, monkeypatch):
    data = tmp_path / "data"
    rng = np.random.default_rng(1)
    texts = ["hello", "world", "中文"]
    labels = []
    for i, text in enumerate(texts):
        gt = rng.integers(0, 256, (24, 32, 3)).astype(np.float64)
        save(data / "gt" / f"{i}.png", gt)
        save(data / "lq" / f"{i}.png", gt + rng.normal(0, 40, gt.shape))
        for name, noise in (("model-a", 5), ("model-b", 20)):
            save(data / name / f"{i}.png", gt + rng.normal(0, noise, gt.shape))
            (data / name / f"{i}.txt").write_text(text if name == "model-a" or i == 0 else "x")
        box = {"transcription": text, "points": [[0, 0], [10, 0], [10, 10], [0, 10]], "difficult": False}
        labels.append(f"{i}.png\t" + json.dumps([box], ensure_ascii=False))
    (data / "label.txt").write_text("\n".join(labels) + "\n")
    (data / "model-a" / "eval" / "old").mkdir(parents=True)  # 已有的评测输出不拷贝

    monkeypatch.setenv("GNM_NVIDIA_SMI", "")
    monkeypatch.setenv("GNM_NPU_SMI", "")
    monkeypatch.setattr(agent.shutil, "which", lambda name: None)
    server = agent.build_server("127.0.0.1", 0, "t", ["/"], str(tmp_path / "agent"), [str(tmp_path)])
    threading.Thread(target=server.serve_forever, daemon=True).start()

    settings = Settings(
        database_url=f"sqlite:///{tmp_path}/t.db", agent_token="t", enable_poller=False, auto_upgrade=False,
        eval_python=sys.executable, eval_script=str(ROOT / "agent" / "evaluate.py"),
        ssh_command=str(ROOT / "tests" / "fake_ssh.py"),
    )
    app = create_app(settings)
    with TestClient(app) as client:
        port = server.server_address[1]
        ids = {}
        for name, ssh_user in (("srv", "alice"), ("eval-srv", "bob"), ("no-ssh", None)):
            body = {"name": name, "host": "127.0.0.1", "port": port, "ssh_user": ssh_user, "ssh_tunnel": False}
            ids[name] = client.post("/api/servers", params={"deploy": False}, json=body).json()["id"]
        asyncio.run(app.state.poller.poll())
        yield client, tmp_path, data, ids
    server.shutdown()


def test_projects_and_tags(env):
    client, _, data, ids = env
    project = client.post("/api/projects", json={"name": " 超分 ", "description": "SR"}).json()
    assert project["name"] == "超分" and project["result_count"] == 0
    assert client.post("/api/projects", json={"name": "超分"}).status_code == 409
    other = client.post("/api/projects", json={"name": "OCR"}).json()

    def register(folder, **extra):
        resp = client.post("/api/results", json={"server_id": ids["srv"], "path": str(data / folder), **extra})
        assert resp.status_code == 201, resp.text
        return resp.json()

    a = register("model-a", project_id=project["id"], tags=["v1", " baseline", "v1"])
    b = register("model-b", project_id=project["id"], tags=["v2"])
    c = register("gt", name="gt-copy")
    assert (a["project_name"], a["tags"]) == ("超分", ["v1", "baseline"])
    resp = client.post("/api/results", json={"server_id": ids["srv"], "path": str(data / "lq"), "project_id": 999})
    assert resp.status_code == 422

    def names(**params):
        return [r["name"] for r in client.get("/api/results", params=params).json()]

    assert names(project_id=project["id"]) == ["model-b", "model-a"]
    assert names(project_id=0) == ["gt-copy"]
    assert names(tag="v1") == ["model-a"]
    assert names(tag=["v1", "v2"]) == []
    assert names(q="MODEL", project_id=project["id"], tag="v2") == ["model-b"]
    assert client.get("/api/results/tags").json() == [
        {"tag": "baseline", "count": 1}, {"tag": "v1", "count": 1}, {"tag": "v2", "count": 1},
    ]

    updated = client.patch(f"/api/results/{c['id']}", json={"project_id": other["id"], "tags": ["v2"]}).json()
    assert (updated["project_name"], updated["tags"]) == ("OCR", ["v2"])
    assert names(tag="v2") == ["gt-copy", "model-b"]
    assert client.patch(f"/api/results/{c['id']}", json={"project_id": 999}).status_code == 422
    projects = {p["name"]: p["result_count"] for p in client.get("/api/projects").json()}
    assert projects == {"OCR": 1, "超分": 2}

    assert client.patch(f"/api/projects/{other['id']}", json={"name": "超分"}).status_code == 409
    assert client.delete(f"/api/projects/{project['id']}").status_code == 204
    assert names(project_id=0) == ["model-b", "model-a"]
    assert client.get(f"/api/results/{a['id']}").json()["project_id"] is None


def test_eval_configs(env):
    client, tmp_path, data, ids = env
    body = {"name": "sr-test", "metrics": ["psnr", "ocr_a", "psnr"], "gt_dir": str(data / "gt") + "/",
            "label_file": str(data / "label.txt"), "lq_dir": str(data / "lq"), "server_id": ids["eval-srv"],
            "server_path": str(tmp_path / "eval-host"), "num_devices": 0}
    for bad in ({"server_path": None}, {"server_id": ids["no-ssh"]}, {"gt_dir": "rel/gt"}, {"server_id": 999}):
        assert client.post("/api/eval-configs", json={**body, **bad}).status_code == 422, bad
    config = client.post("/api/eval-configs", json=body).json()
    assert config["metrics"] == ["psnr", "ocr_a"] and config["gt_dir"] == str(data / "gt")
    assert config["server_name"] == "eval-srv" and config["python"] is None
    assert client.post("/api/eval-configs", json=body).status_code == 409
    resp = client.patch(f"/api/eval-configs/{config['id']}", json={"python": " /opt/venv/bin/python "})
    assert resp.json()["python"] == "/opt/venv/bin/python"
    assert client.patch(f"/api/eval-configs/{config['id']}", json={"python": ""}).json()["python"] is None
    resp = client.patch(f"/api/eval-configs/{config['id']}", json={"note": "x", "server_id": None})
    assert resp.json()["server_id"] is None and resp.json()["note"] == "x"
    assert client.patch(f"/api/eval-configs/{config['id']}", json={"metrics": None}).status_code == 422
    assert [c["name"] for c in client.get("/api/eval-configs").json()] == ["sr-test"]
    assert client.delete(f"/api/eval-configs/{config['id']}").status_code == 204
    assert client.get("/api/eval-configs").json() == []


def wait_status(client, evaluation_id, statuses, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        ev = client.get(f"/api/evaluations/{evaluation_id}").json()
        if ev["status"] in statuses:
            return ev
        time.sleep(0.1)
    raise AssertionError(f"evaluation {evaluation_id} stuck in {ev['status']}")


def test_evaluate_on_eval_server(env):
    client, tmp_path, data, ids = env
    eval_host = tmp_path / "eval-host"
    # 评测配置指定的 python：包一层脚本，确认任务用的是它
    python = tmp_path / "bin" / "eval python"
    python.parent.mkdir()
    python.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
    python.chmod(0o755)
    config = client.post("/api/eval-configs", json={
        "name": "sr", "metrics": ["psnr", "ocr_a"], "gt_dir": str(data / "gt"), "label_file": str(data / "label.txt"),
        "lq_dir": str(data / "lq"), "server_id": ids["eval-srv"], "server_path": str(eval_host), "python": str(python),
    }).json()
    results = [client.post("/api/results", json={"server_id": ids["srv"], "path": str(data / name)}).json()
               for name in ("model-a", "model-b")]

    first = client.post("/api/evaluations", json={"result_set_id": results[0]["id"], "config_id": config["id"]}).json()
    assert first["status"] == "copying" and first["compute_lq"] is True
    copied = eval_host / f"result_{results[0]['id']}"
    assert (first["server_name"], first["data_path"]) == ("eval-srv", str(copied))
    assert first["output_dir"] == str(copied / "eval" / str(first["id"]))
    first = wait_status(client, first["id"], ("pending", "failed"))
    assert first["status"] == "pending", first["error"]
    assert sorted(p.name for p in copied.iterdir()) == sorted(p.name for p in (data / "model-a").iterdir() if p.name != "eval")
    run_until(client, lambda: client.get(f"/api/evaluations/{first['id']}").json()["status"] in ("succeeded", "failed"))
    first = client.get(f"/api/evaluations/{first['id']}").json()
    assert first["status"] == "succeeded", first["error"]
    assert first["python"] == str(python)
    assert client.get(f"/api/jobs/{first['job_id']}").json()["command"].startswith(shlex.quote(str(python)) + " ")
    # 文字参考值来自标注文件，图像参考值来自 GT 目录
    assert first["values"]["ocr_a"] == 1.0 and first["counts"] == {"psnr": 3, "ocr_a": 3}
    assert first["lq_values"]["psnr"] < first["values"]["psnr"]
    assert first["lq_source_id"] == first["id"]
    # LQ 没有识别文本，需要 OCR，测试环境没有安装 paddleocr
    assert "ocr_a" in first["lq_errors"]

    # 同一配置的第二次评测引用第一次的 LQ 基线，不再计算
    second = client.post("/api/evaluations", json={"result_set_id": results[1]["id"], "config_id": config["id"],
                                                   "metrics": ["psnr"], "python": sys.executable}).json()
    assert second["python"] == sys.executable  # 提交时可以覆盖配置中的 python
    assert second["compute_lq"] is False and second["lq_source_id"] == first["id"]
    wait_status(client, second["id"], ("pending",))
    run_until(client, lambda: client.get(f"/api/evaluations/{second['id']}").json()["status"] == "succeeded")
    second = client.get(f"/api/evaluations/{second['id']}").json()
    assert second["lq_values"] == first["lq_values"]
    assert not (Path(second["output_dir"]) / "lq").exists() and (Path(first["output_dir"]) / "lq").is_dir()
    job = client.get(f"/api/jobs/{second['job_id']}").json()
    assert "--lq-baseline" not in job["command"] and job["assigned_server_id"] == ids["eval-srv"]
    assert job["env"] == {"PYTHONUNBUFFERED": "1"}

    listed = {r["id"]: r for r in client.get("/api/results").json()}
    assert listed[results[1]["id"]]["lq_metrics"] == {"psnr": first["lq_values"]["psnr"]}
    cmp = client.get("/api/compare/metrics", params=[("ids", results[0]["id"]), ("ids", results[1]["id"])]).json()
    assert cmp["lq_values"][str(results[0]["id"])]["psnr"] == pytest.approx(first["lq_values"]["psnr"])

    # 样本浏览：LQ 图片和参考值来自评测服务器
    item = client.get(f"/api/results/{results[0]['id']}/samples", params={"limit": 1}).json()["items"][0]
    assert item["lq_image"] == str(data / "lq" / "0.png") and item["ref_text"] == "hello"
    assert item["media"] == {"ref_image": first["id"], "lq_image": first["id"]}
    img = client.get(f"/api/results/{results[0]['id']}/file",
                     params={"path": item["lq_image"], "evaluation_id": first["id"]})
    assert img.status_code == 200 and img.headers["content-type"] == "image/png"
    cmp = client.get("/api/compare/samples", params=[("ids", results[0]["id"]), ("ids", results[1]["id"])]).json()
    assert cmp["items"][0]["results"][str(results[1]["id"])]["lq_image"] == str(data / "lq" / "0.png")


def test_lq_baseline_recompute_and_delete(env):
    client, tmp_path, data, ids = env
    config = client.post("/api/eval-configs", json={
        "name": "sr-local", "metrics": ["psnr", "ocr_a"], "gt_dir": str(data / "gt"),
        "label_file": str(data / "label.txt"), "lq_dir": str(data / "lq"),
    }).json()
    result = client.post("/api/results", json={"server_id": ids["srv"], "path": str(data / "model-a")}).json()

    def evaluate(**kw):
        ev = client.post("/api/evaluations", json={"result_set_id": result["id"], "config_id": config["id"], **kw}).json()
        run_until(client, lambda: client.get(f"/api/evaluations/{ev['id']}").json()["status"] in ("succeeded", "failed"))
        return client.get(f"/api/evaluations/{ev['id']}").json()

    first = evaluate()
    # 测试环境没有 paddleocr，LQ 基线的 ocr_a 没算出来
    assert first["compute_lq"] is True and first["lq_values"]["ocr_a"] is None
    only_psnr = evaluate(metrics=["psnr"])
    assert only_psnr["compute_lq"] is False and only_psnr["lq_source_id"] == first["id"]
    # 需要的指标在已有基线里没算出来时重新计算
    again = evaluate()
    assert again["compute_lq"] is True and again["lq_source_id"] == again["id"]
    forced = evaluate(metrics=["psnr"], recompute_lq=True)
    assert forced["compute_lq"] is True

    # 删除基线来源后，引用它的评测改用同配置的其他基线
    assert client.delete(f"/api/evaluations/{first['id']}").status_code == 204
    assert client.get(f"/api/evaluations/{first['id']}").status_code == 404
    moved = client.get(f"/api/evaluations/{only_psnr['id']}").json()
    assert moved["lq_source_id"] == again["id"] and moved["lq_values"] == again["lq_values"]
    for ev in (again, forced, only_psnr):
        assert client.delete(f"/api/evaluations/{ev['id']}").status_code == 204
    assert client.get("/api/evaluations").json() == []
    assert client.get(f"/api/results/{result['id']}").json()["metrics"] == {}

    pending = client.post("/api/evaluations", json={"result_set_id": result["id"], "metrics": ["psnr"]}).json()
    assert client.delete(f"/api/evaluations/{pending['id']}").status_code == 409
    assert client.delete("/api/evaluations/9999").status_code == 404


def test_copy_needs_ssh_on_result_server(env):
    client, tmp_path, data, ids = env
    result = client.post("/api/results", json={"server_id": ids["no-ssh"], "path": str(data / "model-a")}).json()
    resp = client.post("/api/evaluations", json={
        "result_set_id": result["id"], "metrics": ["psnr"], "server_id": ids["eval-srv"],
        "server_path": str(tmp_path / "eval-host"),
    })
    assert resp.status_code == 422 and "SSH" in resp.json()["detail"]
    # 评测服务器就是结果所在服务器时不拷贝
    same = client.post("/api/evaluations", json={
        "result_set_id": result["id"], "metrics": ["psnr"], "reference": str(data / "gt"),
    }).json()
    assert same["status"] == "pending" and same["data_path"] == str(data / "model-a")


def test_copy_failure_marks_evaluation_failed(env):
    client, tmp_path, data, ids = env
    result = client.post("/api/results", json={"server_id": ids["srv"], "path": str(data / "model-a")}).json()
    blocker = tmp_path / "file"
    blocker.write_text("x")
    ev = client.post("/api/evaluations", json={
        "result_set_id": result["id"], "metrics": ["psnr"], "server_id": ids["eval-srv"], "server_path": str(blocker),
    }).json()
    ev = wait_status(client, ev["id"], ("failed", "pending"))
    assert ev["status"] == "failed" and ev["error"].startswith("拷贝失败：评测服务器") and "mkdir" in ev["error"] and ev["job_id"] is None


def test_interrupted_copy_fails_on_restart(tmp_path):
    settings = Settings(database_url=f"sqlite:///{tmp_path}/t.db", enable_poller=False)
    app = create_app(settings)
    from app.models import Evaluation, ResultSet, Server

    with app.state.session_factory() as session:
        server = Server(name="s", host="h")
        result = ResultSet(name="r", server=server, path="/r")
        session.add(Evaluation(result_set=result, metrics=["psnr"], status="copying", output_dir="/x"))
        session.commit()
    app = create_app(settings)
    with app.state.session_factory() as session:
        evaluation = session.query(Evaluation).one()
        assert evaluation.status == "failed" and "重启" in evaluation.error

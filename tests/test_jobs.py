import asyncio
import json
import re

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def gpu(index, used=0, processes=None):
    return {"index": index, "vendor": "nvidia", "model": "A100", "memory_total_mb": 81920,
            "memory_used_mb": used, "health": "OK", "processes": processes or []}


class FakeCluster:
    """模拟多台 Agent：状态、任务启动、查询、终止和日志。"""

    def __init__(self):
        self.hosts = {}

    def add_host(self, host, n_devices, accelerator="gpu"):
        self.hosts[host] = {"accelerator": accelerator, "devices": [gpu(i) for i in range(n_devices)],
                            "down": False, "jobs": {}, "start_error": None, "start_timeout": False}

    def __call__(self, request: httpx.Request):
        host = self.hosts[request.url.host]
        if host["down"]:
            raise httpx.ConnectError("refused", request=request)
        path = request.url.path
        if path == "/v1/status":
            return httpx.Response(200, json={"accelerator": host["accelerator"], "devices": host["devices"],
                                             "host": {}, "errors": []})
        if path == "/v1/jobs" and request.method == "POST":
            if host["start_timeout"]:
                raise httpx.ReadTimeout("timeout", request=request)
            if host["start_error"]:
                return httpx.Response(host["start_error"], json={"error": "boom"})
            body = json.loads(request.content)
            job = {"job_id": body["job_id"], "pid": 1000 + len(host["jobs"]), "state": "running",
                   "exit_code": None, "killed": False, "devices": body["devices"], "env": body["env"],
                   "command": body["command"], "workdir": body["workdir"]}
            host["jobs"].setdefault(body["job_id"], job)
            return httpx.Response(200, json=host["jobs"][body["job_id"]])
        m = re.match(r"^/v1/jobs/([^/]+)(?:/(kill|log))?$", path)
        if m:
            job = host["jobs"].get(m.group(1))
            if job is None:
                return httpx.Response(404, json={"error": "任务不存在"})
            if m.group(2) == "kill":
                job.update(state="exited", killed=True)
            if m.group(2) == "log":
                data = "hello\n"[int(request.url.params["offset"]):]
                return httpx.Response(200, json={"offset": int(request.url.params["offset"]),
                                                 "next_offset": 6, "size": 6, "data": data})
            return httpx.Response(200, json=job)
        return httpx.Response(404, json={"error": "not found"})

    def finish(self, host, job_id, exit_code=0):
        self.hosts[host]["jobs"][f"gnm-{job_id}"].update(state="exited", exit_code=exit_code)


@pytest.fixture
def cluster():
    c = FakeCluster()
    c.add_host("a", 2)
    c.add_host("b", 4)
    c.add_host("n", 8, accelerator="npu")
    return c


def make_client(tmp_path, cluster, **kw):
    settings = Settings(database_url=f"sqlite:///{tmp_path}/t.db", enable_poller=False, offline_after=1, **kw)
    app = create_app(settings, transport=httpx.MockTransport(cluster))
    return TestClient(app)


@pytest.fixture
def client(tmp_path, cluster):
    with make_client(tmp_path, cluster) as c:
        setup_servers(c)
        yield c


def setup_servers(c):
    for name, host, group in (("srv-a", "a", "cv"), ("srv-b", "b", "cv"), ("srv-n", "n", "nlp")):
        assert c.post("/api/servers", json={"name": name, "host": host, "group": group}).status_code == 201


def tick(client, poll=True):
    state = client.app.state
    if poll:
        asyncio.run(state.poller.poll())
    asyncio.run(state.scheduler.run_once())


def submit(client, **kw):
    body = {"command": "python train.py", "num_devices": 1, **kw}
    resp = client.post("/api/jobs", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


def job(client, job_id):
    return client.get(f"/api/jobs/{job_id}").json()


def test_best_fit_and_queue(client, cluster):
    j1 = submit(client, num_devices=2, group="cv", env={"FOO": "1"}, workdir="/data")
    j2 = submit(client, num_devices=2, group="cv")
    j3 = submit(client, num_devices=4, group="cv")
    assert j1["name"] == "python train.py" and j1["queue_position"] == 1
    tick(client)
    # j1 放进空闲卡最少且足够的 srv-a，j2 放进 srv-b，j3 放不下继续排队
    a, b = job(client, j1["id"]), job(client, j2["id"])
    assert (a["status"], a["assigned_server_name"], a["device_indices"], a["pid"]) == ("running", "srv-a", [0, 1], 1000)
    assert (b["status"], b["assigned_server_name"], b["device_indices"]) == ("running", "srv-b", [0, 1])
    assert job(client, j3["id"])["status"] == "queued" and job(client, j3["id"])["queue_position"] == 1
    sent = cluster.hosts["a"]["jobs"][f"gnm-{j1['id']}"]
    assert sent["env"] == {"FOO": "1"} and sent["workdir"] == "/data" and sent["devices"] == [0, 1]

    # 被占用的卡在服务器接口里显示任务 ID，且不算空闲
    srv_b = next(s for s in client.get("/api/servers").json() if s["name"] == "srv-b")
    assert [d["job_id"] for d in srv_b["devices"]] == [j2["id"], j2["id"], None, None]
    assert srv_b["idle_device_count"] == 2
    overview = client.get("/api/overview").json()
    assert overview["jobs_running"] == 2 and overview["jobs_queued"] == 1

    cluster.finish("b", j2["id"], 0)
    tick(client)
    assert job(client, j2["id"])["status"] == "succeeded"
    assert job(client, j2["id"])["finished_at"] is not None
    third = job(client, j3["id"])
    assert (third["status"], third["assigned_server_name"], third["device_indices"]) == ("running", "srv-b", [0, 1, 2, 3])

    cluster.finish("a", j1["id"], 2)
    tick(client)
    assert (job(client, j1["id"])["status"], job(client, j1["id"])["exit_code"]) == ("failed", 2)


def test_priority_backfill_and_strict(tmp_path, cluster):
    for strict, expected in ((False, "running"), (True, "queued")):
        with make_client(tmp_path / str(strict), cluster, schedule_strict=strict) as c:
            setup_servers(c)
            submit(c, num_devices=1, server_id=2)  # 先占用 srv-b 的 1 张卡
            tick(c)
            big = submit(c, num_devices=4, group="cv", priority=5)  # cv 组已没有 4 张空闲卡
            small = submit(c, num_devices=1, group="cv")
            tick(c)
            assert job(c, big["id"])["status"] == "queued"
            # 默认允许回填：小任务越过放不下的大任务先运行；严格模式下继续等待
            assert job(c, small["id"])["status"] == expected


def test_scheduler_mode_and_wait_reason(client):
    assert client.get("/api/scheduler").json() == {"strict_order": False}
    submit(client, num_devices=1, server_id=2)  # 先占用 srv-b 的 1 张卡
    tick(client)
    big = submit(client, num_devices=4, group="cv", priority=5)
    small = submit(client, num_devices=4, group="cv")
    assert job(client, big["id"])["wait_reason"] is None  # 还没经过一轮调度
    assert client.patch("/api/scheduler", json={"strict_order": True}).json() == {"strict_order": True}
    assert client.get("/api/scheduler").json() == {"strict_order": True}
    tick(client)
    assert job(client, big["id"])["wait_reason"] == "组 cv：单台服务器最多空闲 3 张卡，需要 4 张"
    reason = f"严格按序调度，等待前面的任务 #{big['id']}（python train.py）先启动"
    queued = client.get("/api/jobs", params={"status": "queued"}).json()["items"]
    assert [j["wait_reason"] for j in queued] == [job(client, big["id"])["wait_reason"], reason]

    # 运行组下没有在线服务器
    npu = submit(client, num_devices=1, group="nlp")
    client.patch("/api/servers/3", json={"schedulable": False})
    client.patch("/api/scheduler", json={"strict_order": False})
    tick(client)
    assert job(client, small["id"])["wait_reason"] == "组 cv：单台服务器最多空闲 3 张卡，需要 4 张"
    assert job(client, npu["id"])["wait_reason"] == "组 nlp：没有在线且可调度的服务器"


def test_constraints(client):
    j_npu = submit(client, accelerator="npu", num_devices=8)
    j_server = submit(client, server_id=1, num_devices=1)
    j_cpu = submit(client, num_devices=0, group="nlp")
    tick(client)
    assert job(client, j_npu["id"])["assigned_server_name"] == "srv-n"
    assert job(client, j_server["id"])["assigned_server_name"] == "srv-a"
    cpu = job(client, j_cpu["id"])
    assert (cpu["status"], cpu["device_indices"]) == ("running", [])
    assert client.post("/api/jobs", json={"command": "x", "num_devices": 5, "group": "cv"}).status_code == 422
    assert client.post("/api/jobs", json={"command": "x", "server_id": 99}).status_code == 422
    assert client.post("/api/jobs", json={"command": "x", "env": {"A-B": "1"}}).status_code == 422


def test_cancel_requeue_and_priority(client, cluster):
    running = submit(client, num_devices=4, group="cv")
    queued = submit(client, num_devices=4, group="cv")
    tick(client)
    assert client.patch(f"/api/jobs/{running['id']}", json={"priority": 3}).status_code == 409
    assert client.patch(f"/api/jobs/{queued['id']}", json={"priority": 3}).json()["priority"] == 3

    assert client.delete("/api/servers/2").status_code == 409  # srv-b 上还有运行中的任务
    resp = client.post(f"/api/jobs/{queued['id']}/cancel").json()
    assert resp["status"] == "cancelled"
    resp = client.post(f"/api/jobs/{running['id']}/cancel").json()
    assert resp["status"] == "cancelled"
    assert cluster.hosts["b"]["jobs"][f"gnm-{running['id']}"]["killed"] is True
    assert client.post(f"/api/jobs/{running['id']}/cancel").status_code == 409

    again = client.post(f"/api/jobs/{running['id']}/requeue")
    assert again.status_code == 201
    again = again.json()
    assert again["requeued_from"] == running["id"] and again["status"] == "queued"
    assert again["num_devices"] == 4 and again["group"] == "cv"
    assert client.post(f"/api/jobs/{again['id']}/requeue").status_code == 409

    page = client.get("/api/jobs", params={"status": "cancelled"}).json()
    assert page["total"] == 2
    assert client.get("/api/jobs", params={"q": "train"}).json()["total"] == 3


def test_delete_job(client, cluster):
    running = submit(client, num_devices=4, group="cv")
    queued = submit(client, num_devices=4, group="cv")
    tick(client)
    assert job(client, running["id"])["status"] == "running"
    assert client.delete(f"/api/jobs/{running['id']}").status_code == 409
    assert client.delete(f"/api/jobs/{queued['id']}").status_code == 409
    client.post(f"/api/jobs/{queued['id']}/cancel")
    assert client.delete(f"/api/jobs/{queued['id']}").status_code == 204
    assert client.get(f"/api/jobs/{queued['id']}").status_code == 404
    assert client.delete(f"/api/jobs/{queued['id']}").status_code == 404
    assert [j["id"] for j in client.get("/api/jobs").json()["items"]] == [running["id"]]


def test_cancel_when_agent_unreachable(client, cluster):
    j = submit(client, num_devices=4, group="cv")
    tick(client)
    cluster.hosts["b"]["down"] = True
    assert client.post(f"/api/jobs/{j['id']}/cancel").status_code == 502
    resp = client.post(f"/api/jobs/{j['id']}/cancel", params={"force": "true"}).json()
    assert resp["status"] == "cancelled" and "强制取消" in resp["error"]


def test_lost_and_recover(client, cluster):
    j = submit(client, num_devices=4, group="cv")
    tick(client)
    cluster.hosts["b"]["down"] = True
    tick(client)
    lost = job(client, j["id"])
    assert lost["status"] == "lost"
    # 失联任务仍占着卡，不会被重复分配
    other = submit(client, num_devices=4, group="cv")
    cluster.hosts["b"]["down"] = False
    tick(client)
    assert job(client, j["id"])["status"] == "running"
    assert job(client, other["id"])["status"] == "queued"


def test_launch_failures(client, cluster):
    cluster.hosts["n"]["start_error"] = 400
    bad = submit(client, accelerator="npu")
    tick(client)
    failed = job(client, bad["id"])
    assert failed["status"] == "failed" and "boom" in failed["error"]

    cluster.hosts["n"]["start_error"] = None
    cluster.hosts["n"]["start_timeout"] = True
    slow = submit(client, accelerator="npu")
    tick(client)
    # 超时不确定进程是否启动，保持 starting 等待对账；Agent 没有记录则重新排队
    assert job(client, slow["id"])["status"] == "starting"
    cluster.hosts["n"]["start_timeout"] = False
    asyncio.run(client.app.state.scheduler.sync_active())
    assert job(client, slow["id"])["status"] == "queued"
    tick(client)
    assert job(client, slow["id"])["status"] == "running"


def test_job_log(client):
    j = submit(client, num_devices=1, group="cv")
    assert client.get(f"/api/jobs/{j['id']}/log").json() == {"offset": 0, "next_offset": 0, "size": 0, "data": ""}
    tick(client)
    assert client.get(f"/api/jobs/{j['id']}/log", params={"offset": 2}).json()["data"] == "llo\n"


def test_preempt_and_resume(client, cluster):
    # srv-b 4 张卡：两个可被抢占的低优先级任务各占 2 张
    low1 = submit(client, num_devices=2, server_id=2, preemptible=True, priority=-1)
    low2 = submit(client, num_devices=2, server_id=2, preemptible=True)
    normal = submit(client, num_devices=2, server_id=1)  # srv-a 被不可抢占的任务占满
    tick(client)
    assert job(client, low1["id"])["preemptible"] is True and job(client, normal["id"])["preemptible"] is False
    assert all(job(client, j["id"])["status"] == "running" for j in (low1, low2, normal))

    # 同优先级不抢占
    same = submit(client, num_devices=2, group="cv", priority=-1)
    tick(client)
    assert job(client, same["id"])["status"] == "queued"
    client.post(f"/api/jobs/{same['id']}/cancel")

    # 高优先级任务需要 2 张卡：只抢占优先级最低的 low1，不动 normal
    high = submit(client, num_devices=2, group="cv", priority=5)
    # 被终止的进程退出前卡仍被占用
    low1_devices = job(client, low1["id"])["device_indices"]
    for i in low1_devices:
        cluster.hosts["b"]["devices"][i]["processes"] = [{"pid": 1, "name": "python", "memory_mb": 100}]
    tick(client)
    victim = job(client, low1["id"])
    assert victim["status"] == "queued" and victim["preempt_count"] == 1 and f"#{high['id']}" in victim["error"]
    assert cluster.hosts["b"]["jobs"][f"gnm-{low1['id']}"]["killed"] is True
    assert job(client, low2["id"])["status"] == "running"
    assert job(client, high["id"])["wait_reason"] == f"正在抢占任务 #{low1['id']}，等待其退出释放卡"

    # 卡还没空出来时只等待，不再抢占别的任务
    tick(client)
    assert job(client, low2["id"])["status"] == "running"
    assert job(client, high["id"])["wait_reason"] == f"已抢占任务 #{low1['id']}，等待其退出释放卡"

    for i in low1_devices:
        cluster.hosts["b"]["devices"][i]["processes"] = []
    tick(client)
    h = job(client, high["id"])
    assert (h["status"], h["assigned_server_name"], h["device_indices"]) == ("running", "srv-b", low1_devices)
    assert job(client, low1["id"])["status"] == "queued"

    # 高优先级任务结束后，被抢占的任务以新的 Agent 任务 ID 从头重新运行
    cluster.finish("b", high["id"])
    tick(client)
    tick(client)
    resumed = job(client, low1["id"])
    assert (resumed["status"], resumed["error"], resumed["device_indices"]) == ("running", None, low1_devices)
    assert cluster.hosts["b"]["jobs"][f"gnm-{low1['id']}-r1"]["state"] == "running"
    assert client.get(f"/api/jobs/{low1['id']}/log").json()["data"] == "hello\n"
    assert client.post(f"/api/jobs/{low1['id']}/cancel").json()["status"] == "cancelled"
    assert cluster.hosts["b"]["jobs"][f"gnm-{low1['id']}-r1"]["killed"] is True

    # 复制参数重新排队时保留“可被抢占”
    assert client.post(f"/api/jobs/{low1['id']}/requeue").json()["preemptible"] is True


def test_job_search(client):
    a = submit(client, name="sr-train", submitter="alice")
    submit(client, name="ocr-eval", submitter="bob", command="python eval.py")
    assert [j["id"] for j in client.get("/api/jobs", params={"q": "ALICE"}).json()["items"]] == [a["id"]]
    assert [j["id"] for j in client.get("/api/jobs", params={"q": f"#{a['id']}"}).json()["items"]] == [a["id"]]
    assert client.get("/api/jobs", params={"q": "eval"}).json()["total"] == 1

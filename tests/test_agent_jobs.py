import time

import pytest

import agent


def wait_exited(jobs, job_id, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        info = jobs.get(job_id)
        if info["state"] == "exited":
            return info
        time.sleep(0.05)
    raise AssertionError("job did not exit")


@pytest.fixture
def jobs(tmp_path):
    return agent.JobManager(str(tmp_path / "data"), "gpu")


def test_job_env_exit_code_and_log(jobs, tmp_path):
    info = jobs.start(
        {
            "job_id": "j1",
            "command": 'echo "dev=$CUDA_VISIBLE_DEVICES id=$GNM_JOB_ID foo=$FOO 中文"; pwd; exit 3',
            "workdir": str(tmp_path),
            "env": {"FOO": "bar"},
            "devices": [2, 5],
        }
    )
    assert info["pid"] > 0
    info = wait_exited(jobs, "j1")
    assert info["exit_code"] == 3
    log = jobs.read_log("j1", 0, 1024)
    assert log["data"] == "dev=2,5 id=j1 foo=bar 中文\n{}\n".format(tmp_path)
    assert log["next_offset"] == log["size"]
    # 从末尾继续读，没有新内容
    assert jobs.read_log("j1", log["next_offset"], 1024)["data"] == ""


def test_job_success_and_idempotent_start(jobs, tmp_path):
    spec = {"job_id": "j2", "command": "true", "workdir": str(tmp_path), "devices": []}
    first = jobs.start(spec)
    second = jobs.start(spec)
    assert first["pid"] == second["pid"]
    assert wait_exited(jobs, "j2")["exit_code"] == 0
    assert [j["job_id"] for j in jobs.list()] == ["j2"]


def test_npu_visible_devices(tmp_path):
    jobs = agent.JobManager(str(tmp_path / "data"), "npu")
    jobs.start({"job_id": "n1", "command": 'echo "$ASCEND_RT_VISIBLE_DEVICES"', "devices": [0, 1], "workdir": str(tmp_path)})
    wait_exited(jobs, "n1")
    assert jobs.read_log("n1", 0, 100)["data"] == "0,1\n"


def test_kill_whole_process_group(jobs, tmp_path):
    jobs.start({"job_id": "k1", "command": "sleep 30 & sleep 30; wait", "workdir": str(tmp_path)})
    time.sleep(0.3)
    assert jobs.get("k1")["state"] == "running"
    jobs.kill("k1")
    info = wait_exited(jobs, "k1")
    assert info["killed"] is True
    assert info["exit_code"] is None


def test_log_does_not_split_utf8(jobs, tmp_path):
    jobs.start({"job_id": "u1", "command": "printf '中文'", "workdir": str(tmp_path)})
    wait_exited(jobs, "u1")
    part = jobs.read_log("u1", 0, 4)  # “中”占 3 字节，第 4 字节是“文”的一部分
    assert part["data"] == "中" and part["next_offset"] == 3
    assert jobs.read_log("u1", 3, 100)["data"] == "文"


def test_log_tail_and_end_utf8(jobs, tmp_path):
    jobs.start({"job_id": "t1", "command": "printf 'abc中文xyz'", "workdir": str(tmp_path)})
    wait_exited(jobs, "t1")
    # 字节布局：abc(0..2) 中(3..5) 文(6..8) xyz(9..11)，共 12 字节
    # tail=8 -> raw start=4（落在“中”的续字节），应前移到 6（“文”的首字节）
    tail = jobs.read_log("t1", tail=8)
    assert tail == {"offset": 6, "next_offset": 12, "size": 12, "data": "文xyz"}
    # 再按 end=tail["offset"] 向前读，能无损拼回全文
    head = jobs.read_log("t1", tail=100, end=tail["offset"])
    assert head == {"offset": 0, "next_offset": 6, "size": 12, "data": "abc中"}
    assert head["data"] + tail["data"] == "abc中文xyz"

    # 极端情况：tail=2 且正好落在 3 字节汉字的续字节里，应退回该字符首字节，不会死循环
    jobs.start({"job_id": "t2", "command": "printf 'ab中文'", "workdir": str(tmp_path)})
    wait_exited(jobs, "t2")
    p1 = jobs.read_log("t2", tail=2)
    assert p1 == {"offset": 5, "next_offset": 8, "size": 8, "data": "文"}
    p2 = jobs.read_log("t2", tail=2, end=p1["offset"])
    assert p2 == {"offset": 2, "next_offset": 5, "size": 8, "data": "中"}
    p3 = jobs.read_log("t2", tail=2, end=p2["offset"])
    assert p3 == {"offset": 0, "next_offset": 2, "size": 8, "data": "ab"}
    assert p3["data"] + p2["data"] + p1["data"] == "ab中文"
    # end=0 返回空
    assert jobs.read_log("t2", tail=10, end=0) == {"offset": 0, "next_offset": 0, "size": 8, "data": ""}


def test_log_supports_multi_mb_chunk(jobs, tmp_path):
    jobs.start({"job_id": "big1", "command": "true", "workdir": str(tmp_path)})
    wait_exited(jobs, "big1")
    log_path = tmp_path / "data" / "jobs" / "big1.log"
    payload = ("line-0123456789\n" * (128 * 1024)).encode("utf-8")  # 2 MB
    log_path.write_bytes(payload)
    chunk = jobs.read_log("big1", 0, 2 * 1024 * 1024)
    assert chunk["offset"] == 0 and chunk["next_offset"] == len(payload) and chunk["size"] == len(payload)
    tail = jobs.read_log("big1", tail=1536 * 1024)
    assert tail["next_offset"] == len(payload) and tail["offset"] == len(payload) - 1536 * 1024


def test_invalid_requests(jobs, tmp_path):
    with pytest.raises(agent.JobError) as exc:
        jobs.start({"job_id": "../x", "command": "true"})
    assert exc.value.code == 400
    with pytest.raises(agent.JobError):
        jobs.start({"job_id": "a", "command": "true", "workdir": str(tmp_path / "missing")})
    with pytest.raises(agent.JobError):
        jobs.start({"job_id": "a", "command": "true", "env": {"BAD-KEY": "1"}})
    with pytest.raises(agent.JobError) as exc:
        jobs.get("nope")
    assert exc.value.code == 404

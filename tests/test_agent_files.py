import json
import os

import pytest

import agent


@pytest.fixture
def store(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "sub").mkdir()
    (root / "a.json").write_text('{"x": 1}')
    lines = [json.dumps({"id": i}) for i in range(7)]
    (root / "p.jsonl").write_text("\n".join(lines[:3]) + "\n\n" + "\n".join(lines[3:]) + "\nnot json\n")
    (tmp_path / "secret.txt").write_text("no")
    os.symlink(tmp_path / "secret.txt", root / "link.txt")
    return agent.FileStore([str(root)]), root


def test_list_and_raw(store):
    files, root = store
    listing = files.list_dir(str(root))
    assert [(e["name"], e["type"]) for e in listing["entries"]] == [
        ("a.json", "file"), ("link.txt", "file"), ("p.jsonl", "file"), ("sub", "dir")
    ]
    data, content_type = files.read_raw(str(root / "a.json"))
    assert json.loads(data) == {"x": 1} and content_type == "application/json"


def test_paths_outside_roots_are_rejected(store):
    files, root = store
    for path in (str(root / ".." / "secret.txt"), str(root / "link.txt"), "/etc/passwd"):
        with pytest.raises(agent.JobError) as exc:
            files.read_raw(path)
        assert exc.value.code == 403


def test_jsonl_paging(store):
    files, root = store
    page = files.read_jsonl(str(root / "p.jsonl"), 2, 3)
    assert page["total"] == 8  # 空行不计
    assert [item["id"] for item in page["items"]] == [2, 3, 4]
    last = files.read_jsonl(str(root / "p.jsonl"), 6, 10)["items"]
    assert last[0] == {"id": 6} and "第 8 条记录" in last[1]["_error"]
    # 文件变化后索引失效
    (root / "p.jsonl").write_text('{"id": "only"}\n')
    assert files.read_jsonl(str(root / "p.jsonl"), 0, 10)["items"] == [{"id": "only"}]

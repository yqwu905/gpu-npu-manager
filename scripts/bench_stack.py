"""启动对比页的基准测试环境：本机 Agent（--allow-root DIR）+ 中心服务（uvicorn，托管 web/dist），登记 DIR 下的各组结果集。

    python scripts/bench_stack.py --data DIR [--port 8765] [--agent-port 9765] [--no-pillow] [--build]

DIR 由 scripts/make_compare_dataset.py 生成（含 predictions.jsonl 的子目录都登记为结果集）。启动后写出 DIR/ids.json：
{"url", "ids", "server_id", "agent_pid", "server_pid", "agent_cache", "central_cache", "no_pillow"}，
打开 <url>/ui/#/compare?ids=... 即可，Ctrl+C 退出。--no-pillow 以 GNM_AGENT_NO_PILLOW=1 启动 Agent（中心服务生成）。
scripts/bench_images.py 也把它当模块用：Stack(...).start() / .stop()。
"""

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
TOKEN = "bench"


def wait_http(url: str, headers=None, timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            if httpx.get(url, headers=headers, timeout=2).status_code < 500:
                return
        except httpx.HTTPError:
            pass
        if time.monotonic() > deadline:
            raise RuntimeError(f"{url} 没有启动")
        time.sleep(0.2)


class Stack:
    def __init__(self, data, port=8765, agent_port=9765, no_pillow=False, work=None):
        self.data = Path(data).resolve()
        self.port, self.agent_port, self.no_pillow = port, agent_port, no_pillow
        self.work = Path(work or tempfile.mkdtemp(prefix="gnm-bench-"))
        self.url = f"http://127.0.0.1:{port}"
        self.procs: list[subprocess.Popen] = []
        self.info: dict = {}

    def start(self) -> dict:
        agent_env = dict(os.environ, GNM_AGENT_TOKEN=TOKEN, GNM_NVIDIA_SMI="", GNM_NPU_SMI="",
                         GNM_AGENT_CACHE_DIR=str(self.work / "agent-cache"))
        agent_env.pop("GNM_AGENT_NO_PILLOW", None)
        if self.no_pillow:
            agent_env["GNM_AGENT_NO_PILLOW"] = "1"
        python = os.environ.get("GNM_AGENT_PYTHON") or sys.executable
        agent = subprocess.Popen(
            [python, str(ROOT / "agent" / "agent.py"), "--host", "127.0.0.1", "--port", str(self.agent_port),
             "--allow-root", str(self.data), "--data-dir", str(self.work / "agent")],
            env=agent_env,
        )
        self.procs.append(agent)
        server_env = dict(os.environ, GNM_DATABASE_URL=f"sqlite:///{self.work}/bench.db", GNM_AGENT_TOKEN=TOKEN,
                          GNM_IMAGE_CACHE_DIR=str(self.work / "central-cache"), GNM_WEB_DIR=str(ROOT / "web" / "dist"),
                          GNM_AUTO_UPGRADE="0")
        server_env.pop("GNM_AGENT_NO_PILLOW", None)
        server = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:create_app", "--factory", "--host", "127.0.0.1",
             "--port", str(self.port), "--log-level", "warning"],
            cwd=ROOT / "server", env=server_env,
        )
        self.procs.append(server)
        wait_http(f"http://127.0.0.1:{self.agent_port}/v1/health", {"X-Agent-Token": TOKEN})
        wait_http(f"{self.url}/api/evaluators")
        with httpx.Client(base_url=self.url, timeout=120) as client:
            resp = client.post("/api/servers", json={"name": "bench", "host": "127.0.0.1", "port": self.agent_port})
            resp.raise_for_status()
            server_id = resp.json()["id"]
            ids = []
            for folder in sorted(p for p in self.data.iterdir() if (p / "predictions.jsonl").is_file()):
                resp = client.post("/api/results", json={"server_id": server_id, "path": str(folder)})
                resp.raise_for_status()
                ids.append(resp.json()["id"])
        self.info = {
            "url": self.url, "ids": ids, "server_id": server_id, "agent_pid": agent.pid, "server_pid": server.pid,
            "agent_cache": str(self.work / "agent-cache"), "central_cache": str(self.work / "central-cache"),
            "no_pillow": self.no_pillow,
        }
        return self.info

    def stop(self) -> None:
        for proc in self.procs:
            if proc.poll() is None:
                proc.terminate()  # uvicorn 收到 SIGTERM 正常退出，Agent 直接结束（SIGINT 会打印 KeyboardInterrupt）
        for proc in self.procs:
            try:
                proc.wait(10)
            except subprocess.TimeoutExpired:
                proc.kill()
        self.procs.clear()
        shutil.rmtree(self.work, ignore_errors=True)

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.stop()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--data", required=True)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--agent-port", type=int, default=9765)
    parser.add_argument("--no-pillow", action="store_true", help="Agent 不用 Pillow（GNM_AGENT_NO_PILLOW=1），由中心服务生成")
    parser.add_argument("--build", action="store_true", help="先构建前端（cd web && npm run build）")
    args = parser.parse_args()
    if args.build:
        subprocess.run(["npm", "run", "build"], cwd=ROOT / "web", check=True)
    if not (ROOT / "web" / "dist" / "index.html").is_file():
        print("提示：web/dist 不存在，只提供接口；需要页面时加 --build 或先 cd web && npm run build")
    stack = Stack(args.data, args.port, args.agent_port, args.no_pillow)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))  # 被 kill / timeout 结束时也清理子进程
    try:
        info = stack.start()
        (stack.data / "ids.json").write_text(json.dumps(info, indent=2))
        ids = ",".join(map(str, info["ids"]))
        print(f"已启动：{info['url']}/ui/#/compare?ids={ids}（{len(info['ids'])} 组，ids.json 写在 {stack.data}），Ctrl+C 退出")
        while all(p.poll() is None for p in stack.procs):
            time.sleep(0.5)
        print("有进程已退出")
    except KeyboardInterrupt:
        pass
    finally:
        stack.stop()


if __name__ == "__main__":
    main()

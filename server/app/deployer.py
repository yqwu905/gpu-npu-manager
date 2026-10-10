"""通过 SSH 在服务器上安装和升级 Agent。

中心服务用本机的 ssh 命令（密钥免密登录）连到服务器，把 agent.py、evaluate.py、imaging.py 写到登录用户的
~/.gnm-agent/bin/，生成配置和守护脚本后启动。不需要 root：Agent 以登录用户运行，用 crontab 的
@reboot 实现开机自启。升级时重启 Agent 进程，任务进程在独立的会话中运行，不受影响。
"""

import asyncio
import base64
import hashlib
import logging
import re
import shlex
import shutil
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

from sqlalchemy import select

from .agent_client import AgentClient, AgentError
from .config import Settings
from .models import Server
from .tunnels import ssh_args

log = logging.getLogger(__name__)

PACKAGE_FILES = ("agent.py", "evaluate.py", "imaging.py")
MAX_LOG = 20000
# 安装后等待 Agent 响应的时间（秒）
HEALTH_WAIT = 20


def package_version(package_dir: str) -> str:
    """与 agent.py 中 build_version() 的算法一致。"""
    directory = Path(package_dir)
    mtimes = tuple((directory / name).stat().st_mtime_ns if (directory / name).is_file() else 0 for name in PACKAGE_FILES)
    return _package_version(str(directory), mtimes)


@lru_cache(maxsize=8)
def _package_version(package_dir: str, mtimes: tuple) -> str:
    directory = Path(package_dir)
    source = (directory / "agent.py").read_text(encoding="utf-8")
    match = re.search(r'^AGENT_VERSION = "([^"]+)"', source, re.M)
    digest = hashlib.sha256()
    for name in PACKAGE_FILES:
        path = directory / name
        if path.is_file():
            digest.update(path.read_bytes())
        digest.update(b"\0")
    return "{}+{}".format(match.group(1) if match else "0", digest.hexdigest()[:10])


def public_key() -> tuple[str | None, str | None]:
    """中心主机的 SSH 公钥，需要加到各服务器的 ~/.ssh/authorized_keys。"""
    for name in ("id_ed25519.pub", "id_ecdsa.pub", "id_rsa.pub"):
        path = Path.home() / ".ssh" / name
        if path.is_file():
            return str(path), path.read_text(encoding="utf-8").strip()
    return None, None


def install_script(package_dir: str, env: dict[str, str]) -> str:
    """生成在服务器上执行的安装脚本（通过 ssh 的标准输入传给 bash -s）。"""
    lines = [
        "set -e",
        'D="$HOME/.gnm-agent"',
        'B="$D/bin"',
        'PY=$(command -v python3 || true)',
        '[ -n "$PY" ] || { echo "找不到 python3"; exit 10; }',
        '"$PY" -c "import sys; sys.exit(0 if sys.version_info >= (3, 7) else 1)" '
        '|| { echo "python3 版本低于 3.7"; exit 11; }',
        'mkdir -p "$B"',
        "umask 077",
    ]
    for name in PACKAGE_FILES:
        data = base64.encodebytes((Path(package_dir) / name).read_bytes()).decode()
        lines += [f'base64 -d > "$B/{name}.new" <<\'GNM_EOF\'', data.rstrip("\n"), "GNM_EOF"]
    lines += [f'mv "$B/{name}.new" "$B/{name}"' for name in PACKAGE_FILES]
    lines += ["cat > \"$B/agent.env\" <<'GNM_EOF'"]
    lines += [f"export {key}={shlex.quote(value)}" for key, value in env.items()]
    lines += ["GNM_EOF"]
    # 守护脚本：Agent 意外退出后 5 秒重启
    lines += [
        'cat > "$B/run.sh" <<GNM_EOF',
        "#!/bin/sh",
        'cd "\\$HOME"',
        # 用安装时登录会话的 PATH，开机自启（cron）时也能找到 nvidia-smi / npu-smi
        'export PATH="$PATH:/usr/local/sbin:/usr/local/bin:/usr/sbin:/sbin"',
        '. "$B/agent.env"',
        # 本机自定义配置（如 GNM_NPU_SMI），升级时不会覆盖
        'if [ -f "$D/agent.local.env" ]; then . "$D/agent.local.env"; fi',
        "while true; do",
        # agent.local.env 里可以用 GNM_AGENT_PYTHON 指定装了 Pillow 的 python3
        '    "\\${GNM_AGENT_PYTHON:-$PY}" "$B/agent.py"',
        "    sleep 5",
        "done",
        "GNM_EOF",
    ]
    # 启动脚本：停掉旧的守护进程（整个进程组），再在新会话中启动
    lines += [
        'cat > "$B/start.sh" <<GNM_EOF',
        "#!/bin/sh",
        'if [ -f "$D/agent.pid" ]; then',
        '    OLD=\\$(cat "$D/agent.pid")',
        '    if [ -n "\\$OLD" ] && grep -q gnm-agent "/proc/\\$OLD/cmdline" 2>/dev/null; then',
        '        kill -TERM -"\\$OLD" 2>/dev/null || kill -TERM "\\$OLD" 2>/dev/null || true',
        "        for i in 1 2 3 4 5 6 7 8 9 10; do",
        '            kill -0 "\\$OLD" 2>/dev/null || break',
        "            sleep 1",
        "        done",
        "    fi",
        "fi",
        'if [ -f "$D/agent.log" ] && [ \\$(wc -c < "$D/agent.log") -gt 10485760 ]; then : > "$D/agent.log"; fi',
        'cd "\\$HOME"',
        "if command -v setsid >/dev/null 2>&1; then",
        '    nohup setsid "$B/run.sh" >> "$D/agent.log" 2>&1 < /dev/null &',
        "else",
        '    nohup "$B/run.sh" >> "$D/agent.log" 2>&1 < /dev/null &',
        "fi",
        'echo \\$! > "$D/agent.pid"',
        "GNM_EOF",
        'chmod 700 "$B/run.sh" "$B/start.sh"',
        '"$B/start.sh"',
        # 开机自启
        "if command -v crontab >/dev/null 2>&1; then",
        '    (crontab -l 2>/dev/null | grep -v "gnm-agent/bin/start.sh" || true; echo "@reboot $B/start.sh") | crontab -',
        "else",
        '    echo "警告：没有 crontab，服务器重启后需要在页面上重新安装 Agent"',
        "fi",
        'echo "Agent 已安装到 $B，进程 $(cat "$D/agent.pid")"',
        '( if [ -f "$D/agent.local.env" ]; then . "$D/agent.local.env"; fi; "${GNM_AGENT_PYTHON:-$PY}" -c "import PIL" ) '
        '2>/dev/null || echo "提示：python3 没有 Pillow，缩略图将由中心服务生成（需传输原图，较慢）；'
        '可在 ~/.gnm-agent/agent.local.env 写 export GNM_AGENT_PYTHON=/path/to/python3"',
    ]
    return "\n".join(lines) + "\n"


class Deployer:
    def __init__(self, settings: Settings, session_factory, agent: AgentClient, poller=None):
        self.settings = settings
        self.session_factory = session_factory
        self.agent = agent
        self.poller = poller
        self._semaphore = asyncio.Semaphore(max(1, settings.deploy_concurrency))
        self._tasks: dict[int, asyncio.Task] = {}

    @property
    def version(self) -> str:
        return package_version(self.settings.agent_package_dir)

    def ssh_available(self) -> bool:
        return shutil.which(shlex.split(self.settings.ssh_command)[0]) is not None

    def recover(self) -> None:
        """中心服务重启时，把中断的部署标记为失败。"""
        with self.session_factory() as session:
            for server in session.scalars(select(Server).where(Server.deploy_status.in_(("pending", "running")))):
                server.deploy_status = "failed"
                server.deploy_error = "中心服务重启，部署中断，请重试"
            session.commit()

    def running(self, server_id: int) -> bool:
        task = self._tasks.get(server_id)
        return task is not None and not task.done()

    def submit(self, server_ids: list[int]) -> list[int]:
        """把服务器标记为待部署并在后台执行，返回实际提交的服务器（跳过没有 SSH 信息或正在部署的）。"""
        version = self.version
        accepted = []
        with self.session_factory() as session:
            for server_id in server_ids:
                server = session.get(Server, server_id)
                if server is None or not server.ssh_user or self.running(server_id):
                    continue
                server.deploy_status = "pending"
                server.deploy_action = "upgrade" if server.agent_version else "install"
                server.deploy_version = version
                server.deploy_error = None
                server.deploy_log = None
                server.deploy_started_at = None
                server.deploy_finished_at = None
                accepted.append(server_id)
            session.commit()
        for server_id in accepted:
            self._tasks[server_id] = asyncio.create_task(self._run(server_id))
        return accepted

    async def wait(self) -> None:
        """等待所有部署结束（测试用）。"""
        tasks = [t for t in self._tasks.values() if not t.done()]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def auto_upgrade(self) -> None:
        """版本落后的在线服务器各自动升级一次；失败后不再自动重试，避免反复重启。"""
        if not self.settings.auto_upgrade:
            return
        version = self.version
        with self.session_factory() as session:
            ids = list(
                session.scalars(
                    select(Server.id).where(
                        Server.ssh_user.is_not(None),
                        Server.status == "online",
                        Server.agent_version.is_not(None),
                        Server.agent_version != version,
                        # 每个版本只自动尝试一次
                        (Server.deploy_version.is_(None)) | (Server.deploy_version != version),
                    )
                )
            )
        ids = [i for i in ids if not self.running(i)]
        if ids:
            log.info("auto upgrading agents on servers %s to %s", ids, version)
            self.submit(ids)

    # ------------------------------------------------------------------

    def _update(self, server_id: int, **fields) -> None:
        with self.session_factory() as session:
            server = session.get(Server, server_id)
            if server is None:
                return
            for key, value in fields.items():
                setattr(server, key, value)
            session.commit()

    def _target(self, server_id: int):
        with self.session_factory() as session:
            server = session.get(Server, server_id)
            if server is None:
                return None
            return (
                server.host,
                server.ssh_host or server.host,
                server.port,
                server.ssh_user,
                server.ssh_port or 22,
                server.deploy_version,
                server.ssh_tunnel is not False,
            )

    async def _run(self, server_id: int) -> None:
        async with self._semaphore:
            try:
                await self._deploy(server_id)
            except Exception as exc:  # 部署失败不能影响其他服务器
                log.exception("deploy to server %s failed", server_id)
                self._finish(server_id, f"部署异常：{exc}")

    def _finish(self, server_id: int, error: str | None, output: str | None = None) -> None:
        fields = {
            "deploy_status": "failed" if error else "succeeded",
            "deploy_error": error,
            "deploy_finished_at": datetime.now(timezone.utc),
        }
        if output is not None:
            fields["deploy_log"] = output[-MAX_LOG:]
        self._update(server_id, **fields)

    async def _deploy(self, server_id: int) -> None:
        target = await asyncio.to_thread(self._target, server_id)
        if target is None:
            return
        host, ssh_host, port, ssh_user, ssh_port, version, tunnel = target
        self._update(server_id, deploy_status="running", deploy_started_at=datetime.now(timezone.utc))

        env = {"GNM_AGENT_TOKEN": self.settings.agent_token, "GNM_AGENT_PORT": str(port)}
        if tunnel:
            # 只经 SSH 转发访问，不对外监听
            env["GNM_AGENT_HOST"] = "127.0.0.1"
        script = install_script(self.settings.agent_package_dir, env)
        command = ssh_args(self.settings, ssh_user, ssh_host, ssh_port) + ["bash -s"]
        try:
            proc = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except OSError as exc:
            self._finish(server_id, f"无法执行 ssh：{exc}")
            return
        try:
            out, _ = await asyncio.wait_for(proc.communicate(script.encode()), self.settings.deploy_timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            self._finish(server_id, f"SSH 执行超过 {self.settings.deploy_timeout} 秒未完成")
            return
        output = out.decode("utf-8", "replace")
        if proc.returncode != 0:
            last = next((line for line in reversed(output.strip().splitlines()) if line.strip()), "")
            if proc.returncode == 255:
                error = f"SSH 连接失败：{last}（请确认中心主机的公钥已加入该用户的 authorized_keys）"
            else:
                error = f"安装脚本失败（退出码 {proc.returncode}）：{last}"
            self._finish(server_id, error, output)
            return

        # 等新 Agent 启动并确认版本
        seen, last_error = None, None
        loop = asyncio.get_running_loop()
        deadline = loop.time() + HEALTH_WAIT
        while loop.time() < deadline:
            try:
                seen = (await self.agent.health(host, port)).get("agent_version")
                if seen == version:
                    break
            except AgentError as exc:
                last_error = str(exc)
            await asyncio.sleep(1)
        if seen != version:
            if seen is None:
                where = "通过 SSH 转发" if tunnel else f"直接（请检查防火墙是否放通端口 {port}）"
                error = f"安装完成，但 {HEALTH_WAIT} 秒内{where}连不上 Agent：{last_error}；可查看服务器上的 ~/.gnm-agent/agent.log"
            else:
                error = (
                    f"端口 {port} 上运行的 Agent 版本是 {seen}，不是刚安装的 {version}，"
                    "可能有用 install.sh 安装的 systemd 服务占用了端口"
                )
            self._finish(server_id, error, output)
            return
        self._finish(server_id, None, output)
        if self.poller is not None:
            await self.poller.poll([server_id])

"""通过 SSH 本地端口转发访问 Agent，服务器防火墙不需要放通 Agent 端口。

每台开启了转发的服务器维持一个 `ssh -N -L 127.0.0.1:<本地端口>:127.0.0.1:<Agent 端口>` 进程，
中心服务访问该服务器的 Agent 时改为连接本地端口。进程退出后，下次访问时自动重建。
"""

import asyncio
import collections
import logging
import shlex
import socket
import time
from dataclasses import dataclass, field

from sqlalchemy import select

from .config import Settings
from .models import Server

log = logging.getLogger(__name__)

# 等待转发建立的时间（秒）
READY_TIMEOUT = 15
# 建立失败后多久内不再重试，避免每次请求都发起 SSH 连接
RETRY_AFTER = 10


class TunnelError(Exception):
    pass


def ssh_args(settings: Settings, user: str, host: str, port: int) -> list[str]:
    """中心服务调用 ssh 的公共参数（密钥免密登录）。"""
    return shlex.split(settings.ssh_command) + [
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "ConnectTimeout=10",
        "-p", str(port),
        f"{user}@{host}",
    ]


@dataclass(frozen=True)
class Target:
    ssh_user: str
    ssh_host: str
    ssh_port: int
    agent_port: int


@dataclass
class Tunnel:
    target: Target
    local_port: int = 0
    proc: asyncio.subprocess.Process | None = None
    stderr: collections.deque = field(default_factory=lambda: collections.deque(maxlen=20))
    failed_at: float = 0.0
    error: str | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.returncode is None


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _port_open(port: int) -> bool:
    try:
        _, writer = await asyncio.open_connection("127.0.0.1", port)
    except OSError:
        return False
    writer.close()
    return True


class Tunnels:
    def __init__(self, settings: Settings, session_factory):
        self.settings = settings
        self.session_factory = session_factory
        # (Agent 地址, Agent 端口) -> 转发目标
        self._targets: dict[tuple[str, int], Target] = {}
        self._tunnels: dict[tuple[str, int], Tunnel] = {}

    def refresh(self) -> None:
        """从数据库读取需要转发的服务器；配置变化或删除的服务器关闭旧的转发。"""
        with self.session_factory() as session:
            rows = session.execute(
                select(Server.host, Server.port, Server.ssh_user, Server.ssh_host, Server.ssh_port).where(
                    Server.ssh_user.is_not(None), Server.ssh_tunnel.is_not(False)
                )
            ).all()
        self._targets = {
            (r.host, r.port): Target(r.ssh_user, r.ssh_host or r.host, r.ssh_port or 22, r.port) for r in rows
        }
        for key, tunnel in list(self._tunnels.items()):
            if self._targets.get(key) != tunnel.target:
                self._close(self._tunnels.pop(key))

    async def endpoint(self, host: str, port: int) -> tuple[str, int]:
        """返回实际要连接的地址：开启转发的服务器是本地端口，其余原样返回。"""
        key = (host, port)
        if key not in self._targets:
            await asyncio.to_thread(self.refresh)
        target = self._targets.get(key)
        if target is None:
            return host, port
        tunnel = self._tunnels.get(key)
        if tunnel is None:
            tunnel = self._tunnels[key] = Tunnel(target)
        async with tunnel.lock:
            if not tunnel.alive:
                if tunnel.error and time.monotonic() - tunnel.failed_at < RETRY_AFTER:
                    raise TunnelError(tunnel.error)
                await self._start(tunnel)
        return "127.0.0.1", tunnel.local_port

    async def _start(self, tunnel: Tunnel) -> None:
        t = tunnel.target
        tunnel.local_port = _free_port()
        command = ssh_args(self.settings, t.ssh_user, t.ssh_host, t.ssh_port) + [
            "-N",
            "-o", "ExitOnForwardFailure=yes",
            "-o", "ServerAliveInterval=15",
            "-o", "ServerAliveCountMax=3",
            "-L", f"127.0.0.1:{tunnel.local_port}:127.0.0.1:{t.agent_port}",
        ]
        tunnel.stderr.clear()
        try:
            tunnel.proc = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as exc:
            self._fail(tunnel, f"无法执行 ssh：{exc}")
        asyncio.create_task(self._drain(tunnel, tunnel.proc))

        deadline = time.monotonic() + READY_TIMEOUT
        while time.monotonic() < deadline:
            if not tunnel.alive:
                await asyncio.sleep(0.1)  # 让 stderr 读完
                last = next((line for line in reversed(tunnel.stderr) if line), "ssh 已退出")
                self._fail(tunnel, f"SSH 转发失败：{last}")
            if await _port_open(tunnel.local_port):
                tunnel.error = None
                log.info("ssh tunnel to %s@%s ready on port %s", t.ssh_user, t.ssh_host, tunnel.local_port)
                return
            await asyncio.sleep(0.2)
        self._close(tunnel)
        self._fail(tunnel, f"SSH 转发 {READY_TIMEOUT} 秒内没有建立")

    @staticmethod
    async def _drain(tunnel: Tunnel, proc) -> None:
        while True:
            line = await proc.stderr.readline()
            if not line:
                break
            tunnel.stderr.append(line.decode("utf-8", "replace").strip())

    @staticmethod
    def _fail(tunnel: Tunnel, error: str):
        tunnel.error = error
        tunnel.failed_at = time.monotonic()
        raise TunnelError(error)

    @staticmethod
    def _close(tunnel: Tunnel) -> None:
        if tunnel.alive:
            tunnel.proc.terminate()

    async def stop(self) -> None:
        for tunnel in self._tunnels.values():
            self._close(tunnel)
            if tunnel.proc is not None:
                try:
                    await asyncio.wait_for(tunnel.proc.wait(), 5)
                except asyncio.TimeoutError:
                    tunnel.proc.kill()
        self._tunnels.clear()

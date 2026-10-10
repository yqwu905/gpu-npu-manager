"""通过 SSH 本地端口转发访问 Agent，服务器防火墙不需要放通 Agent 端口。

每台开启了转发的服务器维持一个 `ssh -N -L 127.0.0.1:<本地端口>:127.0.0.1:<Agent 端口>` 进程，
中心服务访问该服务器的 Agent 时改为连接本地端口。进程退出后，下次访问时自动重建。

有的服务器 sshd 设置了 AllowTcpForwarding no（如 openEuler 默认），端口转发被拒绝。这时改为中继：
本地监听端口，每个连接执行一次 `ssh <服务器> python3 -c <中继脚本>`，由服务器上的 python3 连接 Agent，
数据经 ssh 的标准输入输出传递。用 ControlMaster 复用 SSH 连接，避免每次都重新登录。
"""

import asyncio
import collections
import logging
import shlex
import shutil
import socket
import tempfile
import threading
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
# 中继模式下 SSH 主连接空闲多久后退出（秒）
CONTROL_PERSIST = 60
# 遇到不在转发列表里的服务器时，距上次读取数据库超过这么久（秒）才重新读取
REFRESH_INTERVAL = 5

# 在服务器上运行：连接本机 Agent 端口，与标准输入输出互相转发
RELAY_SCRIPT = """\
import socket, sys, threading
s = socket.create_connection(("127.0.0.1", int(sys.argv[1])))
def up():
    while True:
        data = sys.stdin.buffer.read1(65536)
        if not data:
            break
        s.sendall(data)
    s.shutdown(socket.SHUT_WR)
threading.Thread(target=up, daemon=True).start()
while True:
    data = s.recv(65536)
    if not data:
        break
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()
"""


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
    # 服务器禁止端口转发时改用中继
    relay: bool = False
    server: asyncio.Server | None = None

    @property
    def alive(self) -> bool:
        if self.relay:
            return self.server is not None and self.server.is_serving()
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


def _forward_prohibited(tunnel: Tunnel) -> bool:
    return any("administratively prohibited" in line for line in tunnel.stderr)


class Tunnels:
    def __init__(self, settings: Settings, session_factory):
        self.settings = settings
        self.session_factory = session_factory
        # (Agent 地址, Agent 端口) -> 转发目标
        self._targets: dict[tuple[str, int], Target] = {}
        self._tunnels: dict[tuple[str, int], Tunnel] = {}
        self._control_dir: str | None = None
        self._refreshed = 0.0
        self._stale = True
        # 失效后并发的请求可能同时触发 refresh（在线程中执行），串行执行
        self._refresh_lock = threading.Lock()

    def invalidate(self) -> None:
        """服务器增删改后调用：下次访问时重新读取数据库。"""
        self._stale = True

    def refresh(self) -> None:
        """从数据库读取需要转发的服务器；配置变化或删除的服务器关闭旧的转发。"""
        with self._refresh_lock:
            self._stale = False
            self._refreshed = time.monotonic()
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
                if self._targets.get(key) != tunnel.target and self._tunnels.get(key) is tunnel:
                    self._close(self._tunnels.pop(key))

    async def endpoint(self, host: str, port: int) -> tuple[str, int]:
        """返回实际要连接的地址：开启转发的服务器是本地端口，其余原样返回。"""
        key = (host, port)
        # 不需要转发的服务器每次都不在列表里，限制读取数据库的频率；增删改服务器时由 invalidate 触发重新读取
        if self._stale or (key not in self._targets and time.monotonic() - self._refreshed > REFRESH_INTERVAL):
            await asyncio.to_thread(self.refresh)
        target = self._targets.get(key)
        if target is None:
            return host, port
        tunnel = self._tunnels.get(key)
        if tunnel is None:
            tunnel = self._tunnels[key] = Tunnel(target)
        async with tunnel.lock:
            if tunnel.alive and not tunnel.relay and _forward_prohibited(tunnel):
                # 试连时没来得及看到原因，之后的连接暴露出转发被拒绝
                self._close(tunnel)
                tunnel.relay = True
            if not tunnel.alive:
                if tunnel.error and time.monotonic() - tunnel.failed_at < RETRY_AFTER:
                    raise TunnelError(tunnel.error)
                await self._start(tunnel)
        return "127.0.0.1", tunnel.local_port

    def hint(self, host: str, port: int) -> str | None:
        """连接出错时用来说明原因：端口转发取 ssh 最近一次打开通道失败的提示，中继取最近输出的一行。"""
        tunnel = self._tunnels.get((host, port))
        if tunnel is None:
            return None
        for line in reversed(tunnel.stderr):
            if line and (tunnel.relay or "open failed" in line):
                return line
        return None

    async def _start(self, tunnel: Tunnel) -> None:
        if tunnel.relay:
            await self._start_relay(tunnel)
            return
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
                if await self._prohibited(tunnel):
                    log.info("port forwarding to %s@%s is prohibited, using relay", t.ssh_user, t.ssh_host)
                    self._close(tunnel)
                    tunnel.relay = True
                    await self._start_relay(tunnel)
                    return
                log.info("ssh tunnel to %s@%s ready on port %s", t.ssh_user, t.ssh_host, tunnel.local_port)
                return
            await asyncio.sleep(0.2)
        self._close(tunnel)
        self._fail(tunnel, f"SSH 转发 {READY_TIMEOUT} 秒内没有建立")

    @staticmethod
    async def _prohibited(tunnel: Tunnel) -> bool:
        """试连一次：sshd 拒绝转发时本地连接会被直接关闭，ssh 输出 administratively prohibited。"""
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", tunnel.local_port)
        except OSError:
            return False
        try:
            writer.write(b"GET /v1/health HTTP/1.0\r\n\r\n")
            await writer.drain()
            if await asyncio.wait_for(reader.read(1), 5):
                return False
        except (OSError, asyncio.TimeoutError):
            return False
        finally:
            writer.close()
        # 等 ssh 把原因写到 stderr
        for _ in range(20):
            if _forward_prohibited(tunnel):
                return True
            await asyncio.sleep(0.1)
        return False

    def _control_options(self) -> list[str]:
        if self._control_dir is None:
            # 套接字路径有长度限制，放在 /tmp 下
            self._control_dir = tempfile.mkdtemp(prefix="gnm-ssh-", dir="/tmp")
        return [
            "-o", "ControlMaster=auto",
            "-o", f"ControlPath={self._control_dir}/%C",
            "-o", f"ControlPersist={CONTROL_PERSIST}",
        ]

    async def _start_relay(self, tunnel: Tunnel) -> None:
        tunnel.server = await asyncio.start_server(
            lambda reader, writer: self._relay(tunnel, reader, writer), "127.0.0.1", 0
        )
        tunnel.local_port = tunnel.server.sockets[0].getsockname()[1]
        tunnel.error = None

    async def _relay(self, tunnel: Tunnel, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        t = tunnel.target
        # LogLevel=ERROR 不输出登录提示（Banner），stderr 只剩中继脚本的报错
        command = ssh_args(self.settings, t.ssh_user, t.ssh_host, t.ssh_port) + self._control_options() + [
            "-o", "LogLevel=ERROR",
            f"exec python3 -c {shlex.quote(RELAY_SCRIPT)} {t.agent_port}"
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as exc:
            tunnel.stderr.append(f"无法执行 ssh：{exc}")
            writer.close()
            return
        drain = asyncio.create_task(self._drain(tunnel, proc))

        async def copy(src, dst, close):
            try:
                while data := await src.read(65536):
                    dst.write(data)
                    await dst.drain()
            except (OSError, RuntimeError):
                pass
            finally:
                close()

        try:
            await asyncio.gather(
                copy(reader, proc.stdin, proc.stdin.close),
                copy(proc.stdout, writer, writer.close),
            )
        finally:
            writer.close()
            if proc.returncode is None:
                proc.terminate()
            await proc.wait()
            await drain

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
        if tunnel.server is not None:
            tunnel.server.close()
        if tunnel.proc is not None and tunnel.proc.returncode is None:
            tunnel.proc.terminate()

    async def stop(self) -> None:
        for tunnel in self._tunnels.values():
            self._close(tunnel)
            if tunnel.proc is not None:
                try:
                    await asyncio.wait_for(tunnel.proc.wait(), 5)
                except asyncio.TimeoutError:
                    tunnel.proc.kill()
            if tunnel.relay:
                # 关闭复用的 SSH 主连接
                t = tunnel.target
                command = ssh_args(self.settings, t.ssh_user, t.ssh_host, t.ssh_port) + self._control_options()
                proc = await asyncio.create_subprocess_exec(
                    *command, "-O", "exit",
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                try:
                    await asyncio.wait_for(proc.wait(), 5)
                except asyncio.TimeoutError:
                    proc.kill()
        self._tunnels.clear()
        if self._control_dir is not None:
            shutil.rmtree(self._control_dir, ignore_errors=True)
            self._control_dir = None

"""后台轮询：定时向各节点 Agent 拉取状态并写入数据库。"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import delete, select

from .config import Settings
from .models import Device, DeviceMetric, Server
from .timeutil import as_utc

log = logging.getLogger(__name__)

DEVICE_FIELDS = (
    "vendor",
    "model",
    "uuid",
    "bus_id",
    "npu_id",
    "chip_id",
    "memory_total_mb",
    "memory_used_mb",
    "utilization",
    "temperature",
    "power_w",
    "power_limit_w",
    "health",
    "processes",
)


def apply_status(session, server: Server, payload: dict, settings: Settings, now: datetime) -> None:
    """把 Agent 返回的 /v1/status 写入服务器和卡记录。"""
    server.status = "online"
    server.fail_count = 0
    server.last_seen_at = now
    server.last_error = "; ".join(payload.get("errors") or []) or None
    server.hostname = payload.get("hostname")
    server.agent_version = payload.get("agent_version")
    server.host_info = payload.get("host")
    if payload.get("accelerator"):
        server.accelerator = payload["accelerator"]

    existing = {d.index: d for d in server.devices}
    seen = set()
    for item in payload.get("devices") or []:
        index = item.get("index")
        if index is None or index in seen:
            continue
        seen.add(index)
        device = existing.get(index)
        if device is None:
            device = Device(index=index, vendor=item.get("vendor") or "unknown")
            server.devices.append(device)
        for field in DEVICE_FIELDS:
            if field in item:
                setattr(device, field, item[field] if field != "processes" else item[field] or [])
        device.updated_at = now
    # 采集失败时（errors 非空且没有设备）保留旧的卡记录，避免卡片闪烁消失
    if seen or not payload.get("errors"):
        for index, device in existing.items():
            if index not in seen:
                server.devices.remove(device)

    _record_history(session, server, now, settings)


def _record_history(session, server: Server, now: datetime, settings: Settings) -> None:
    last_ts = session.scalar(
        select(DeviceMetric.ts)
        .where(DeviceMetric.server_id == server.id)
        .order_by(DeviceMetric.ts.desc())
        .limit(1)
    )
    if last_ts is not None and now - as_utc(last_ts) < timedelta(seconds=settings.history_interval):
        return
    for device in server.devices:
        session.add(
            DeviceMetric(
                server_id=server.id,
                device_index=device.index,
                ts=now,
                utilization=device.utilization,
                memory_used_mb=device.memory_used_mb,
                temperature=device.temperature,
                power_w=device.power_w,
            )
        )


def apply_failure(server: Server, error: str, settings: Settings) -> None:
    server.fail_count = (server.fail_count or 0) + 1
    server.last_error = error
    if server.fail_count >= settings.offline_after:
        server.status = "offline"


class Poller:
    def __init__(self, settings: Settings, session_factory, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        self.session_factory = session_factory
        self.transport = transport
        self._task: asyncio.Task | None = None
        self._last_prune: datetime | None = None

    def _client(self) -> httpx.AsyncClient:
        headers = {"X-Agent-Token": self.settings.agent_token} if self.settings.agent_token else {}
        return httpx.AsyncClient(timeout=self.settings.agent_timeout, headers=headers, transport=self.transport)

    async def _fetch(self, client: httpx.AsyncClient, host: str, port: int) -> dict:
        resp = await client.get(f"http://{host}:{port}/v1/status")
        resp.raise_for_status()
        return resp.json()

    def _targets(self, server_ids=None):
        with self.session_factory() as session:
            query = select(Server.id, Server.host, Server.port)
            if server_ids is not None:
                query = query.where(Server.id.in_(server_ids))
            return session.execute(query).all()

    def _save(self, server_id: int, payload: dict | None, error: str | None) -> None:
        now = datetime.now(timezone.utc)
        with self.session_factory() as session:
            server = session.get(Server, server_id)
            if server is None:  # 轮询期间被删除
                return
            if payload is not None:
                apply_status(session, server, payload, self.settings, now)
            else:
                apply_failure(server, error or "unknown error", self.settings)
            session.commit()

    async def poll(self, server_ids=None) -> None:
        """拉取一次指定服务器（默认全部）的状态。"""
        targets = await asyncio.to_thread(self._targets, server_ids)
        if not targets:
            return
        async with self._client() as client:
            results = await asyncio.gather(
                *(self._fetch(client, t.host, t.port) for t in targets), return_exceptions=True
            )
        for target, result in zip(targets, results):
            if isinstance(result, Exception):
                error = f"{type(result).__name__}: {result}" if str(result) else type(result).__name__
                await asyncio.to_thread(self._save, target.id, None, error)
            else:
                await asyncio.to_thread(self._save, target.id, result, None)

    def _prune(self) -> None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=self.settings.history_days)
        with self.session_factory() as session:
            session.execute(delete(DeviceMetric).where(DeviceMetric.ts < cutoff))
            session.commit()

    async def _loop(self) -> None:
        while True:
            try:
                await self.poll()
                now = datetime.now(timezone.utc)
                if self._last_prune is None or now - self._last_prune > timedelta(hours=1):
                    await asyncio.to_thread(self._prune)
                    self._last_prune = now
            except Exception:  # 单轮失败不能让轮询停掉
                log.exception("poll round failed")
            await asyncio.sleep(self.settings.poll_interval)

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

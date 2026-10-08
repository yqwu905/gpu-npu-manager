"""任务调度：同步运行中任务的状态，并为排队任务分配空闲卡。"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from .agent_client import AgentClient, AgentError
from .config import Settings
from .models import JOB_ACTIVE_STATUSES, Job, Server
from .views import device_is_idle, occupied_devices

log = logging.getLogger(__name__)


def queue_order(query):
    """排队顺序：优先级从高到低，同优先级先提交先调度。"""
    return query.order_by(Job.priority.desc(), Job.created_at, Job.id)


def apply_agent_state(job: Job, info: dict, now: datetime) -> None:
    """根据 Agent 返回的进程状态更新任务。"""
    job.pid = info.get("pid", job.pid)
    if info.get("state") == "running":
        if job.status in ("starting", "lost"):
            job.status = "running"
            job.error = None
        return
    job.exit_code = info.get("exit_code")
    job.finished_at = job.finished_at or now
    if info.get("killed"):
        job.status = "cancelled"
    elif job.exit_code == 0:
        job.status = "succeeded"
    else:
        job.status = "failed"
        if job.exit_code is None:
            job.error = "进程被信号终止"


def _requeue(job: Job, error: str) -> None:
    job.status = "queued"
    job.error = error
    job.assigned_server_id = None
    job.device_indices = []
    job.pid = None


def _wait_reason(job: Job, matching: list[Server], free: dict[int, list[int]]) -> str:
    scope = []
    if job.server_id is not None:
        scope.append(f"指定服务器 #{job.server_id}")
    if job.group is not None:
        scope.append(f"组 {job.group}")
    if job.accelerator is not None:
        scope.append(job.accelerator.upper())
    scope = " ".join(scope) or "所有服务器"
    if not matching:
        return f"{scope}：没有在线且可调度的服务器"
    most = max(len(free[s.id]) for s in matching)
    return f"{scope}：单台服务器最多空闲 {most} 张卡，需要 {job.num_devices} 张"


@dataclass
class Launch:
    job_id: int
    host: str
    port: int
    command: str
    workdir: str | None
    env: dict
    devices: list[int]


@dataclass
class ActiveJob:
    job_id: int
    status: str
    host: str
    port: int
    server_status: str


class Scheduler:
    def __init__(self, settings: Settings, session_factory, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        self.session_factory = session_factory
        self.agent = AgentClient(settings, transport)
        self._task: asyncio.Task | None = None
        # 保证同一时刻只有一轮调度在运行
        self._lock = asyncio.Lock()
        # 每轮调度后执行的回调（如回收评测结果）
        self.after_round: list = []
        # 调度模式，运行期可通过接口切换，重启后恢复为环境变量的值
        self.strict_order = settings.schedule_strict
        # 排队任务没被调度的原因，每轮调度重新计算
        self.wait_reasons: dict[int, str] = {}

    # ------------------------------------------------------------------
    # 同步运行中任务
    # ------------------------------------------------------------------

    def _active_jobs(self) -> list[ActiveJob]:
        with self.session_factory() as session:
            rows = session.execute(
                select(Job.id, Job.status, Server.host, Server.port, Server.status)
                .join(Server, Job.assigned_server_id == Server.id)
                .where(Job.status.in_(JOB_ACTIVE_STATUSES))
            ).all()
            return [ActiveJob(*row) for row in rows]

    def _update_job(self, job_id: int, info: dict | None = None, *, lost: bool = False, missing: bool = False) -> None:
        now = datetime.now(timezone.utc)
        with self.session_factory() as session:
            job = session.get(Job, job_id)
            if job is None or job.status not in JOB_ACTIVE_STATUSES:
                return
            if lost:
                job.status = "lost"
                job.error = "服务器离线，等待恢复后对账"
            elif missing and job.status == "starting":
                # 启动请求没有到达 Agent（例如中心服务在启动途中重启），重新排队
                _requeue(job, "启动请求未送达 Agent，已重新排队")
            elif missing:
                job.status = "failed"
                job.error = "Agent 中没有该任务的记录"
                job.finished_at = now
            else:
                apply_agent_state(job, info, now)
            session.commit()

    async def sync_active(self) -> None:
        for active in await asyncio.to_thread(self._active_jobs):
            if active.server_status == "offline":
                # 启动中的任务保持 starting：不确定进程是否已启动，等服务器恢复后再对账
                if active.status == "running":
                    await asyncio.to_thread(self._update_job, active.job_id, lost=True)
                continue
            try:
                info = await self.agent.get_job(active.host, active.port, active.job_id)
            except AgentError as exc:
                if exc.status == 404:
                    await asyncio.to_thread(self._update_job, active.job_id, missing=True)
                # 网络错误交给状态轮询判定离线
                continue
            await asyncio.to_thread(self._update_job, active.job_id, info)

    # ------------------------------------------------------------------
    # 分配排队任务
    # ------------------------------------------------------------------

    def _plan(self) -> list[Launch]:
        """为排队任务选择服务器和卡，并把选中的任务标记为 starting。"""
        with self.session_factory() as session:
            servers = list(
                session.scalars(
                    select(Server)
                    .options(selectinload(Server.devices))
                    .where(Server.status == "online", Server.schedulable.is_(True))
                    .order_by(Server.name)
                )
            )
            occupied = occupied_devices(session)
            free = {
                s.id: [d.index for d in s.devices if device_is_idle(s, d, self.settings, occupied)] for s in servers
            }
            launches = []
            reasons = {}
            blocker = None
            for job in session.scalars(queue_order(select(Job).where(Job.status == "queued"))):
                if blocker is not None:
                    reasons[job.id] = f"严格按序调度，等待前面的任务 #{blocker.id}（{blocker.name}）先启动"
                    continue
                matching = [
                    s
                    for s in servers
                    if (job.group is None or s.group == job.group)
                    and (job.accelerator is None or s.accelerator == job.accelerator)
                    and (job.server_id is None or s.id == job.server_id)
                ]
                candidates = [s for s in matching if len(free[s.id]) >= job.num_devices]
                if not candidates:
                    reasons[job.id] = _wait_reason(job, matching, free)
                    if self.strict_order:
                        blocker = job
                    continue
                # 选空闲卡最少但足够的服务器，减少碎片
                server = min(candidates, key=lambda s: (len(free[s.id]), s.name))
                devices = sorted(free[server.id])[: job.num_devices]
                free[server.id] = [i for i in free[server.id] if i not in devices]
                job.status = "starting"
                job.assigned_server_id = server.id
                job.device_indices = devices
                job.error = None
                launches.append(
                    Launch(job.id, server.host, server.port, job.command, job.workdir, job.env or {}, devices)
                )
            session.commit()
            self.wait_reasons = reasons
            return launches

    def _launched(self, job_id: int, info: dict) -> bool:
        """记录启动成功；任务在启动途中被取消时返回 False。"""
        with self.session_factory() as session:
            job = session.get(Job, job_id)
            if job is None or job.status != "starting":
                return False
            job.status = "running"
            job.pid = info.get("pid")
            job.started_at = datetime.now(timezone.utc)
            session.commit()
            return True

    def _launch_failed(self, job_id: int, error: str, mode: str) -> None:
        with self.session_factory() as session:
            job = session.get(Job, job_id)
            if job is None or job.status != "starting":
                return
            if mode == "requeue":
                _requeue(job, error)
            elif mode == "fail":
                job.status = "failed"
                job.error = error
                job.finished_at = datetime.now(timezone.utc)
            else:
                # 网络错误：进程可能已经启动，保持 starting，下一轮同步时向 Agent 对账
                job.error = error
            session.commit()

    async def schedule(self) -> None:
        for launch in await asyncio.to_thread(self._plan):
            try:
                info = await self.agent.start_job(
                    launch.host, launch.port, launch.job_id, launch.command, launch.workdir, launch.env, launch.devices
                )
            except AgentError as exc:
                if exc.status is None:
                    mode = "keep"
                elif exc.status >= 500:
                    mode = "requeue"  # Agent 启动进程失败，换个时机或机器重试
                else:
                    mode = "fail"  # 参数被拒绝（如工作目录不存在），重试也不会成功
                log.warning("job %s launch failed: %s", launch.job_id, exc)
                await asyncio.to_thread(self._launch_failed, launch.job_id, str(exc), mode)
                continue
            if not await asyncio.to_thread(self._launched, launch.job_id, info):
                # 启动途中任务被取消，终止刚启动的进程
                try:
                    await self.agent.kill_job(launch.host, launch.port, launch.job_id)
                except AgentError as exc:
                    log.warning("job %s cancelled during launch, kill failed: %s", launch.job_id, exc)

    # ------------------------------------------------------------------

    async def run_once(self) -> None:
        async with self._lock:
            await self.sync_active()
            await self.schedule()
            for callback in self.after_round:
                await callback()

    async def _loop(self) -> None:
        while True:
            try:
                await self.run_once()
            except Exception:  # 单轮失败不能让调度停掉
                log.exception("schedule round failed")
            await asyncio.sleep(self.settings.schedule_interval)

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

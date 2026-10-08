from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from ..agent_client import AgentError
from ..deps import get_session
from ..models import JOB_ACTIVE_STATUSES, JOB_FINISHED_STATUSES, Job, Server, utcnow
from ..scheduler import queue_order
from ..schemas import JobCreate, JobLog, JobOut, JobPage, JobStatus, JobUpdate, SchedulerSettings
from ..timeutil import as_utc

router = APIRouter(prefix="/api", tags=["jobs"])

COPY_FIELDS = ("name", "command", "workdir", "env", "num_devices", "group", "accelerator", "server_id", "priority", "submitter")


def _queue_positions(session: Session) -> dict[int, int]:
    ids = session.scalars(queue_order(select(Job.id).where(Job.status == "queued")))
    return {job_id: pos for pos, job_id in enumerate(ids, start=1)}


def job_out(job: Job, positions: dict[int, int] | None = None, reasons: dict[int, str] | None = None) -> JobOut:
    out = JobOut.model_validate(job)
    out.assigned_server_name = job.assigned_server.name if job.assigned_server else None
    out.queue_position = (positions or {}).get(job.id)
    if job.status == "queued":
        out.wait_reason = (reasons or {}).get(job.id)
    out.created_at = as_utc(job.created_at)
    out.started_at = as_utc(job.started_at)
    out.finished_at = as_utc(job.finished_at)
    return out


def _get_job(session: Session, job_id: int) -> Job:
    job = session.get(Job, job_id, options=[selectinload(Job.assigned_server)])
    if job is None:
        raise HTTPException(404, "任务不存在")
    return job


def _check_placeable(session: Session, body: JobCreate) -> None:
    """提交时检查是否存在可能满足条件的服务器，避免任务永远排不上。"""
    query = select(Server).options(selectinload(Server.devices)).where(Server.schedulable.is_(True))
    if body.group is not None:
        query = query.where(Server.group == body.group)
    if body.accelerator is not None:
        query = query.where(Server.accelerator == body.accelerator)
    if body.server_id is not None:
        query = query.where(Server.id == body.server_id)
    if not any(len(s.devices) >= body.num_devices for s in session.scalars(query)):
        raise HTTPException(422, "没有满足运行组、类型和卡数要求的服务器")


@router.post("/jobs", response_model=JobOut, status_code=201, summary="提交任务")
def create_job(body: JobCreate, session: Session = Depends(get_session)):
    if body.server_id is not None and session.get(Server, body.server_id) is None:
        raise HTTPException(422, "指定的服务器不存在")
    _check_placeable(session, body)
    data = body.model_dump()
    data["name"] = body.name or body.command.strip().splitlines()[0][:80]
    job = Job(**data, status="queued")
    session.add(job)
    session.commit()
    return job_out(_get_job(session, job.id), _queue_positions(session))


@router.get("/jobs", response_model=JobPage, summary="任务列表")
def list_jobs(
    request: Request,
    status: list[JobStatus] | None = Query(None, description="状态，可多个"),
    group: str | None = Query(None, description="运行组"),
    submitter: str | None = Query(None, description="提交人"),
    server_id: int | None = Query(None, description="实际运行的服务器"),
    q: str | None = Query(None, description="关键字，匹配名称和命令"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    session: Session = Depends(get_session),
):
    query = select(Job)
    if status:
        query = query.where(Job.status.in_(status))
    if group is not None:
        query = query.where(Job.group == group)
    if submitter is not None:
        query = query.where(Job.submitter == submitter)
    if server_id is not None:
        query = query.where(Job.assigned_server_id == server_id)
    if q:
        pattern = f"%{q}%"
        query = query.where(or_(Job.name.ilike(pattern), Job.command.ilike(pattern)))
    total = session.scalar(select(func.count()).select_from(query.subquery()))
    if status and set(status) == {"queued"}:
        query = queue_order(query)  # 只看排队任务时按调度顺序返回
    else:
        query = query.order_by(Job.id.desc())
    jobs = session.scalars(query.options(selectinload(Job.assigned_server)).limit(limit).offset(offset))
    positions = _queue_positions(session)
    reasons = request.app.state.scheduler.wait_reasons
    return JobPage(total=total, items=[job_out(j, positions, reasons) for j in jobs])


@router.get("/jobs/{job_id}", response_model=JobOut, summary="任务详情")
def get_job(job_id: int, request: Request, session: Session = Depends(get_session)):
    return job_out(_get_job(session, job_id), _queue_positions(session), request.app.state.scheduler.wait_reasons)


@router.patch("/jobs/{job_id}", response_model=JobOut, summary="修改任务名称或优先级")
def update_job(job_id: int, body: JobUpdate, session: Session = Depends(get_session)):
    job = _get_job(session, job_id)
    changes = body.model_dump(exclude_unset=True)
    if "priority" in changes and job.status != "queued":
        raise HTTPException(409, "只有排队中的任务可以修改优先级")
    for field, value in changes.items():
        if value is None:
            raise HTTPException(422, f"{field} 不能为空")
        setattr(job, field, value)
    session.commit()
    return job_out(job, _queue_positions(session))


@router.post("/jobs/{job_id}/cancel", response_model=JobOut, summary="取消任务")
async def cancel_job(
    job_id: int,
    request: Request,
    force: bool = Query(False, description="Agent 无法连接时也标记为已取消（不保证进程已停止）"),
):
    session_factory = request.app.state.session_factory
    with session_factory() as session:
        job = _get_job(session, job_id)
        if job.status in JOB_FINISHED_STATUSES:
            raise HTTPException(409, "任务已结束")
        target = None
        if job.status in JOB_ACTIVE_STATUSES and job.assigned_server is not None:
            target = (job.assigned_server.host, job.assigned_server.port)
    error = None
    if target is not None:
        try:
            await request.app.state.scheduler.agent.kill_job(target[0], target[1], job_id)
        except AgentError as exc:
            # 404：进程从未启动，直接视为取消成功
            if exc.status != 404:
                if not force:
                    raise HTTPException(502, f"终止进程失败：{exc}")
                error = f"强制取消，进程可能仍在运行：{exc}"
    with session_factory() as session:
        job = _get_job(session, job_id)
        if job.status not in JOB_FINISHED_STATUSES:
            job.status = "cancelled"
            job.finished_at = utcnow()
            job.error = error
            session.commit()
        return job_out(job)


@router.post("/jobs/{job_id}/requeue", response_model=JobOut, status_code=201, summary="以相同参数重新排队")
def requeue_job(job_id: int, session: Session = Depends(get_session)):
    old = _get_job(session, job_id)
    if old.status not in JOB_FINISHED_STATUSES:
        raise HTTPException(409, "只有已结束的任务可以重新排队")
    job = Job(**{f: getattr(old, f) for f in COPY_FIELDS}, status="queued", requeued_from=old.id)
    session.add(job)
    session.commit()
    return job_out(_get_job(session, job.id), _queue_positions(session))


@router.get("/jobs/{job_id}/log", response_model=JobLog, summary="增量读取任务日志")
async def job_log(
    job_id: int,
    request: Request,
    offset: int = Query(0, ge=0, description="从第几个字节开始读，首次传 0，之后传上次返回的 next_offset"),
    limit: int = Query(65536, ge=1, le=1048576, description="最多读取的字节数"),
):
    with request.app.state.session_factory() as session:
        job = _get_job(session, job_id)
        server = job.assigned_server
        if server is None or job.status == "queued":
            return JobLog(offset=0, next_offset=0, size=0, data="")
        host, port = server.host, server.port
    try:
        return await request.app.state.scheduler.agent.read_log(host, port, job_id, offset, limit)
    except AgentError as exc:
        if exc.status == 404:
            return JobLog(offset=0, next_offset=0, size=0, data="")
        raise HTTPException(502, f"读取日志失败：{exc}")


@router.get("/scheduler", response_model=SchedulerSettings, summary="查询调度模式")
def get_scheduler(request: Request):
    return SchedulerSettings(strict_order=request.app.state.scheduler.strict_order)


@router.patch("/scheduler", response_model=SchedulerSettings, summary="切换调度模式")
def update_scheduler(body: SchedulerSettings, request: Request):
    # 只保存在内存中，重启后恢复为 GNM_SCHEDULE_STRICT 的值
    request.app.state.scheduler.strict_order = body.strict_order
    return SchedulerSettings(strict_order=body.strict_order)

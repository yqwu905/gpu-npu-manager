from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, Response
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from ..config import Settings
from ..deps import get_session, get_settings
from ..models import JOB_ACTIVE_STATUSES, DeviceMetric, Job, Server
from ..schemas import (
    AgentPackage,
    BatchError,
    DeployDetail,
    DeployRequest,
    DeviceHistory,
    FilterOptions,
    MetricPoint,
    ServerBatchCreate,
    ServerBatchResult,
    ServerCreate,
    ServerGroup,
    ServerOut,
    ServerUpdate,
)
from ..timeutil import as_utc
from ..deployer import public_key
from ..views import deploy_state, occupied_devices, server_out

router = APIRouter(prefix="/api", tags=["servers"])

GroupBy = Literal["group", "owner", "tag", "accelerator", "model", "status"]


class ServerFilters:
    """服务器列表的筛选条件，多值参数可重复传入，例如 ?tag=a&tag=b。"""

    def __init__(
        self,
        q: str | None = Query(None, description="关键字，匹配名称、地址、主机名、备注"),
        group: list[str] | None = Query(None, description="运行组，多个为“或”"),
        owner: list[str] | None = Query(None, description="使用人，多个为“或”"),
        tag: list[str] | None = Query(None, description="标签，多个为“且”，即必须同时包含"),
        accelerator: Literal["gpu", "npu"] | None = Query(None, description="加速卡类型"),
        model: list[str] | None = Query(None, description="加速卡型号，多个为“或”"),
        status: Literal["unknown", "online", "offline"] | None = Query(None, description="在线状态"),
        schedulable: bool | None = Query(None, description="是否参与调度"),
        has_idle: bool | None = Query(None, description="true 只返回有空闲卡的服务器"),
    ):
        self.q = q.strip().lower() if q else None
        self.group = group
        self.owner = owner
        self.tag = tag
        self.accelerator = accelerator
        self.model = model
        self.status = status
        self.schedulable = schedulable
        self.has_idle = has_idle

    def match(self, out: ServerOut) -> bool:
        if self.q:
            haystack = " ".join(filter(None, [out.name, out.host, out.hostname, out.note])).lower()
            if self.q not in haystack:
                return False
        if self.group and out.group not in self.group:
            return False
        if self.owner and out.owner not in self.owner:
            return False
        if self.tag and not set(self.tag).issubset(out.tags):
            return False
        if self.accelerator and out.accelerator != self.accelerator:
            return False
        if self.model and not set(self.model) & set(out.models):
            return False
        if self.status and out.status != self.status:
            return False
        if self.schedulable is not None and out.schedulable != self.schedulable:
            return False
        if self.has_idle is not None and (out.idle_device_count > 0) != self.has_idle:
            return False
        return True


def _load_servers(session: Session) -> list[Server]:
    return list(session.scalars(select(Server).options(selectinload(Server.devices)).order_by(Server.name)))


def _get_server(session: Session, server_id: int) -> Server:
    server = session.get(Server, server_id, options=[selectinload(Server.devices)])
    if server is None:
        raise HTTPException(404, "服务器不存在")
    return server


def _commit(session: Session) -> None:
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise HTTPException(409, "服务器名称已存在")


def _group_keys(out: ServerOut, by: GroupBy) -> list[str | None]:
    if by == "tag":
        return list(out.tags) or [None]
    if by == "model":
        return list(out.models) or [None]
    return [getattr(out, by)]


@router.get("/servers", response_model=list[ServerOut], summary="服务器列表（含卡状态，支持筛选）")
def list_servers(
    filters: ServerFilters = Depends(),
    include_devices: bool = Query(True, description="是否返回每张卡的详情"),
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
):
    result = []
    occupied = occupied_devices(session)
    for server in _load_servers(session):
        out = server_out(server, settings, include_devices, occupied)
        if filters.match(out):
            result.append(out)
    return result


@router.get("/servers/grouped", response_model=list[ServerGroup], summary="按属性分组的服务器列表")
def grouped_servers(
    by: GroupBy = Query(..., description="分组属性；按标签或型号分组时，一台服务器可出现在多个分组中"),
    filters: ServerFilters = Depends(),
    include_devices: bool = Query(True),
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
):
    groups: dict[str | None, list[ServerOut]] = {}
    occupied = occupied_devices(session)
    for server in _load_servers(session):
        out = server_out(server, settings, include_devices, occupied)
        if not filters.match(out):
            continue
        for key in _group_keys(out, by):
            groups.setdefault(key, []).append(out)
    # 有值的分组按名称排序，未设置（null）的分组放最后
    ordered = sorted(groups.items(), key=lambda kv: (kv[0] is None, kv[0] or ""))
    return [
        ServerGroup(
            key=key,
            server_count=len(items),
            online_count=sum(1 for s in items if s.status == "online"),
            device_count=sum(s.device_count for s in items),
            idle_device_count=sum(s.idle_device_count for s in items),
            servers=items,
        )
        for key, items in ordered
    ]


@router.get("/meta/filters", response_model=FilterOptions, summary="筛选项候选值")
def filter_options(session: Session = Depends(get_session)):
    servers = _load_servers(session)
    return FilterOptions(
        groups=sorted({s.group for s in servers if s.group}),
        owners=sorted({s.owner for s in servers if s.owner}),
        tags=sorted({t for s in servers for t in (s.tags or [])}),
        models=sorted({d.model for s in servers for d in s.devices if d.model}),
        accelerators=sorted({s.accelerator for s in servers if s.accelerator}),
    )


def _new_server(body: ServerCreate) -> Server:
    data = body.model_dump()
    data["name"] = (body.name or body.host).strip()
    data["ssh_user"] = (body.ssh_user or "").strip() or None
    server = Server(**data)
    server.devices = []
    return server


# 会创建后台部署任务，必须在事件循环中调用，所以调用它的接口都定义为 async
def _after_create(request: Request, background: BackgroundTasks, servers: list[Server], deploy: bool) -> None:
    to_deploy = [s.id for s in servers if deploy and s.ssh_user]
    if to_deploy:
        request.app.state.deployer.submit(to_deploy)
    # 已经装好 Agent 的服务器直接拉取一次状态
    to_poll = [s.id for s in servers if s.id not in to_deploy]
    poller = getattr(request.app.state, "poller", None)
    if poller is not None and to_poll:
        background.add_task(poller.poll, to_poll)


@router.post("/servers", response_model=ServerOut, status_code=201, summary="添加服务器")
async def create_server(
    body: ServerCreate,
    request: Request,
    background: BackgroundTasks,
    deploy: bool = Query(True, description="填写了 ssh_user 时，添加后立即通过 SSH 安装 Agent"),
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
):
    server = _new_server(body)
    session.add(server)
    _commit(session)
    _after_create(request, background, [server], deploy)
    session.refresh(server)
    return server_out(server, settings)


@router.post("/servers/batch", response_model=ServerBatchResult, status_code=201, summary="批量添加服务器")
async def batch_create_servers(
    body: ServerBatchCreate,
    request: Request,
    background: BackgroundTasks,
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
):
    existing = set(session.scalars(select(Server.name)))
    created, errors = [], []
    for index, item in enumerate(body.servers):
        server = _new_server(item)
        if server.name in existing:
            errors.append(BatchError(index=index, host=item.host, error=f"名称 {server.name} 已存在"))
            continue
        existing.add(server.name)
        session.add(server)
        created.append(server)
    session.commit()
    _after_create(request, background, created, body.deploy)
    for server in created:
        session.refresh(server)
    return ServerBatchResult(created=[server_out(s, settings) for s in created], errors=errors)


@router.get("/agent-package", response_model=AgentPackage, summary="Agent 安装包版本与 SSH 公钥")
def agent_package(request: Request, settings: Settings = Depends(get_settings)):
    deployer = request.app.state.deployer
    key_path, key = public_key()
    return AgentPackage(
        version=deployer.version,
        ssh_available=deployer.ssh_available(),
        public_key=key,
        public_key_path=key_path,
        auto_upgrade=settings.auto_upgrade,
    )


@router.post("/servers/deploy", response_model=list[ServerOut], summary="批量安装或升级 Agent")
async def deploy_servers(
    body: DeployRequest,
    request: Request,
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
):
    if body.server_ids is not None:
        ids = body.server_ids
    elif body.outdated:
        outs = [server_out(s, settings, include_devices=False) for s in _load_servers(session)]
        ids = [o.id for o in outs if o.managed and (o.agent_outdated or o.agent_version is None)]
    else:
        raise HTTPException(422, "请指定 server_ids 或 outdated=true")
    accepted = request.app.state.deployer.submit(ids)
    session.expire_all()
    return [server_out(_get_server(session, i), settings) for i in accepted]


@router.post("/servers/{server_id}/deploy", response_model=ServerOut, summary="安装或升级该服务器的 Agent")
async def deploy_server(
    server_id: int,
    request: Request,
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
):
    server = _get_server(session, server_id)
    if not server.ssh_user:
        raise HTTPException(409, "没有填写 SSH 用户，无法自动安装")
    if not request.app.state.deployer.submit([server_id]):
        raise HTTPException(409, "正在安装中")
    session.refresh(server)
    return server_out(server, settings, occupied=occupied_devices(session))


@router.get("/servers/{server_id}/deploy", response_model=DeployDetail | None, summary="最近一次安装或升级的详情与输出")
def deploy_detail(server_id: int, session: Session = Depends(get_session)):
    return deploy_state(_get_server(session, server_id), with_log=True)


@router.get("/servers/{server_id}", response_model=ServerOut, summary="服务器详情")
def get_server(server_id: int, session: Session = Depends(get_session), settings: Settings = Depends(get_settings)):
    return server_out(_get_server(session, server_id), settings, occupied=occupied_devices(session))


@router.patch("/servers/{server_id}", response_model=ServerOut, summary="修改服务器属性")
def update_server(
    server_id: int,
    body: ServerUpdate,
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
):
    server = _get_server(session, server_id)
    for field, value in body.model_dump(exclude_unset=True).items():
        if field in ("name", "host", "port", "tags", "schedulable", "ssh_port", "allow_roots") and value is None:
            raise HTTPException(422, f"{field} 不能为空")
        if field == "ssh_user":
            value = (value or "").strip() or None
        setattr(server, field, value)
    _commit(session)
    return server_out(server, settings, occupied=occupied_devices(session))


@router.delete("/servers/{server_id}", status_code=204, summary="删除服务器")
def delete_server(server_id: int, session: Session = Depends(get_session)):
    server = _get_server(session, server_id)
    active = session.scalar(
        select(func.count()).where(Job.assigned_server_id == server_id, Job.status.in_(JOB_ACTIVE_STATUSES))
    )
    if active:
        raise HTTPException(409, "该服务器上还有运行中的任务，请先取消")
    session.query(DeviceMetric).filter(DeviceMetric.server_id == server_id).delete()
    session.delete(server)
    session.commit()
    return Response(status_code=204)


@router.post("/servers/{server_id}/refresh", response_model=ServerOut, summary="立即拉取一次该服务器状态")
async def refresh_server(server_id: int, request: Request):
    poller = request.app.state.poller
    settings = request.app.state.settings
    await poller.poll([server_id])
    with request.app.state.session_factory() as session:
        return server_out(_get_server(session, server_id), settings, occupied=occupied_devices(session))


@router.get("/servers/{server_id}/history", response_model=list[DeviceHistory], summary="卡的历史趋势")
def server_history(
    server_id: int,
    hours: int = Query(24, ge=1, le=24 * 7, description="最近多少小时"),
    session: Session = Depends(get_session),
):
    _get_server(session, server_id)
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    rows = session.scalars(
        select(DeviceMetric)
        .where(DeviceMetric.server_id == server_id, DeviceMetric.ts >= since)
        .order_by(DeviceMetric.device_index, DeviceMetric.ts)
    )
    history: dict[int, list[MetricPoint]] = {}
    for row in rows:
        history.setdefault(row.device_index, []).append(
            MetricPoint(
                ts=as_utc(row.ts),
                utilization=row.utilization,
                memory_used_mb=row.memory_used_mb,
                temperature=row.temperature,
                power_w=row.power_w,
            )
        )
    return [DeviceHistory(device_index=k, points=v) for k, v in sorted(history.items())]

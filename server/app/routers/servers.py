from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, Response
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from ..config import Settings
from ..deps import get_session, get_settings
from ..models import DeviceMetric, Server
from ..schemas import (
    DeviceHistory,
    FilterOptions,
    MetricPoint,
    ServerCreate,
    ServerGroup,
    ServerOut,
    ServerUpdate,
)
from ..timeutil import as_utc
from ..views import server_out

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
    for server in _load_servers(session):
        out = server_out(server, settings, include_devices)
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
    for server in _load_servers(session):
        out = server_out(server, settings, include_devices)
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


@router.post("/servers", response_model=ServerOut, status_code=201, summary="添加服务器")
def create_server(
    body: ServerCreate,
    request: Request,
    background: BackgroundTasks,
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
):
    server = Server(**body.model_dump())
    server.devices = []
    session.add(server)
    _commit(session)
    poller = getattr(request.app.state, "poller", None)
    if poller is not None:
        background.add_task(poller.poll, [server.id])
    return server_out(server, settings)


@router.get("/servers/{server_id}", response_model=ServerOut, summary="服务器详情")
def get_server(server_id: int, session: Session = Depends(get_session), settings: Settings = Depends(get_settings)):
    return server_out(_get_server(session, server_id), settings)


@router.patch("/servers/{server_id}", response_model=ServerOut, summary="修改服务器属性")
def update_server(
    server_id: int,
    body: ServerUpdate,
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
):
    server = _get_server(session, server_id)
    for field, value in body.model_dump(exclude_unset=True).items():
        if field in ("name", "host", "port", "tags", "schedulable") and value is None:
            raise HTTPException(422, f"{field} 不能为空")
        setattr(server, field, value)
    _commit(session)
    return server_out(server, settings)


@router.delete("/servers/{server_id}", status_code=204, summary="删除服务器")
def delete_server(server_id: int, session: Session = Depends(get_session)):
    server = _get_server(session, server_id)
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
        return server_out(_get_server(session, server_id), settings)


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

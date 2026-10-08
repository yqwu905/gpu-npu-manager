from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..config import Settings
from ..deps import get_session, get_settings
from ..models import Server
from ..schemas import AcceleratorSummary, Overview
from ..views import server_out

router = APIRouter(prefix="/api", tags=["overview"])


@router.get("/overview", response_model=Overview, summary="首页总览数字")
def overview(session: Session = Depends(get_session), settings: Settings = Depends(get_settings)):
    servers = [
        server_out(s, settings)
        for s in session.scalars(select(Server).options(selectinload(Server.devices)))
    ]
    by_accelerator: dict[str, AcceleratorSummary] = {}
    for s in servers:
        if not s.accelerator:
            continue
        summary = by_accelerator.setdefault(s.accelerator, AcceleratorSummary(servers=0, devices=0, idle_devices=0))
        summary.servers += 1
        summary.devices += s.device_count
        summary.idle_devices += s.idle_device_count
    devices_total = sum(s.device_count for s in servers)
    devices_idle = sum(s.idle_device_count for s in servers)
    return Overview(
        servers_total=len(servers),
        servers_online=sum(1 for s in servers if s.status == "online"),
        servers_offline=sum(1 for s in servers if s.status == "offline"),
        devices_total=devices_total,
        devices_idle=devices_idle,
        devices_busy=devices_total - devices_idle,
        by_accelerator=by_accelerator,
    )

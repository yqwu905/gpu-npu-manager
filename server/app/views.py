"""把 ORM 对象转换成接口返回结构，并计算空闲卡等派生字段。"""

from sqlalchemy import select

from .config import Settings
from .deployer import package_version
from .models import JOB_ACTIVE_STATUSES, Device, Job, Server
from .schemas import DeployDetail, DeployState, DeviceOut, ServerOut
from .timeutil import as_utc


Occupied = dict[tuple[int, int], int]


def occupied_devices(session) -> Occupied:
    """平台任务正在占用的卡：(服务器 ID, 卡号) -> 任务 ID。"""
    occupied: Occupied = {}
    rows = session.execute(
        select(Job.id, Job.assigned_server_id, Job.device_indices).where(Job.status.in_(JOB_ACTIVE_STATUSES))
    )
    for job_id, server_id, indices in rows:
        for index in indices or []:
            occupied[(server_id, index)] = job_id
    return occupied


def device_is_idle(server: Server, device: Device, settings: Settings, occupied: Occupied | None = None) -> bool:
    if occupied and (server.id, device.index) in occupied:
        return False
    if server.status != "online":
        return False
    if device.health and device.health.upper() != "OK":
        return False
    if device.processes:
        return False
    threshold = settings.idle_memory_mb_npu if device.vendor == "ascend" else settings.idle_memory_mb_gpu
    if device.memory_used_mb is not None and device.memory_used_mb >= threshold:
        return False
    return True


def _package_version(settings: Settings) -> str | None:
    try:
        return package_version(settings.agent_package_dir)
    except OSError:  # 找不到 Agent 安装包时不判断是否需要升级
        return None


def _outdated(server: Server, settings: Settings) -> bool:
    current = _package_version(settings)
    return bool(server.agent_version and current and server.agent_version != current)


def deploy_state(server: Server, with_log: bool = False) -> DeployState | None:
    if not server.deploy_status:
        return None
    cls = DeployDetail if with_log else DeployState
    out = cls(
        status=server.deploy_status,
        action=server.deploy_action,
        version=server.deploy_version,
        error=server.deploy_error,
        started_at=as_utc(server.deploy_started_at),
        finished_at=as_utc(server.deploy_finished_at),
    )
    if with_log:
        out.log = server.deploy_log
    return out


def device_out(server: Server, device: Device, settings: Settings, occupied: Occupied | None = None) -> DeviceOut:
    out = DeviceOut.model_validate(device)
    out.idle = device_is_idle(server, device, settings, occupied)
    out.job_id = (occupied or {}).get((server.id, device.index))
    out.updated_at = as_utc(device.updated_at)
    return out


def server_out(
    server: Server, settings: Settings, include_devices: bool = True, occupied: Occupied | None = None
) -> ServerOut:
    devices = [device_out(server, d, settings, occupied) for d in server.devices]
    models = sorted({d.model for d in server.devices if d.model})
    return ServerOut(
        id=server.id,
        name=server.name,
        host=server.host,
        port=server.port,
        group=server.group,
        owner=server.owner,
        tags=server.tags or [],
        note=server.note,
        schedulable=server.schedulable,
        accelerator=server.accelerator,
        status=server.status,
        hostname=server.hostname,
        agent_version=server.agent_version,
        agent_outdated=_outdated(server, settings),
        managed=bool(server.ssh_user),
        deploy=deploy_state(server),
        ssh_user=server.ssh_user,
        ssh_port=server.ssh_port or 22,
        allow_roots=server.allow_roots or [],
        host_info=server.host_info,
        last_seen_at=as_utc(server.last_seen_at),
        last_error=server.last_error,
        models=models,
        device_count=len(devices),
        idle_device_count=sum(1 for d in devices if d.idle),
        devices=devices if include_devices else [],
        created_at=as_utc(server.created_at),
        updated_at=as_utc(server.updated_at),
    )

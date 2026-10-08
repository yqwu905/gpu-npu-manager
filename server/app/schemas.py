import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ProcessOut(BaseModel):
    pid: int | None = None
    name: str | None = None
    user: str | None = None
    memory_mb: float | None = None


class DeviceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    index: int = Field(description="逻辑卡号，即 CUDA_VISIBLE_DEVICES / ASCEND_RT_VISIBLE_DEVICES 使用的编号")
    vendor: str = Field(description="nvidia / ascend")
    model: str | None = None
    uuid: str | None = None
    bus_id: str | None = None
    npu_id: int | None = Field(default=None, description="昇腾 NPU ID（npu-smi 中的 NPU 列）")
    chip_id: int | None = None
    memory_total_mb: float | None = None
    memory_used_mb: float | None = None
    utilization: float | None = Field(default=None, description="GPU 利用率或 NPU AICore 利用率，百分比")
    temperature: float | None = Field(default=None, description="摄氏度")
    power_w: float | None = None
    power_limit_w: float | None = None
    health: str | None = None
    processes: list[ProcessOut] = []
    idle: bool = Field(
        default=False, description="是否空闲：服务器在线、健康、无进程、显存占用低于阈值，且未被平台任务占用"
    )
    job_id: int | None = Field(default=None, description="占用该卡的平台任务 ID")
    updated_at: datetime | None = None


class DiskOut(BaseModel):
    path: str
    total_gb: float | None = None
    used_gb: float | None = None


class HostInfo(BaseModel):
    cpu_count: int | None = None
    cpu_percent: float | None = None
    load1: float | None = None
    memory_total_mb: float | None = None
    memory_used_mb: float | None = None
    disks: list[DiskOut] = []


class ServerBase(BaseModel):
    group: str | None = Field(default=None, description="运行组，任务调度按运行组选择服务器")
    owner: str | None = Field(default=None, description="使用人")
    tags: list[str] = Field(default=[], description="标签，可多个")
    note: str | None = Field(default=None, description="备注")
    schedulable: bool = Field(default=True, description="是否参与任务调度")

    @field_validator("tags")
    @classmethod
    def _clean_tags(cls, value):
        seen = []
        for tag in value:
            tag = tag.strip()
            if tag and tag not in seen:
                seen.append(tag)
        return seen


class ServerCreate(ServerBase):
    name: str = Field(min_length=1, max_length=128)
    host: str = Field(min_length=1, max_length=255, description="Agent 地址（IP 或域名）")
    port: int = Field(default=9100, ge=1, le=65535, description="Agent 端口")


class ServerUpdate(BaseModel):
    """只提交需要修改的字段。"""

    name: str | None = Field(default=None, min_length=1, max_length=128)
    host: str | None = Field(default=None, min_length=1, max_length=255)
    port: int | None = Field(default=None, ge=1, le=65535)
    group: str | None = None
    owner: str | None = None
    tags: list[str] | None = None
    note: str | None = None
    schedulable: bool | None = None

    @field_validator("tags")
    @classmethod
    def _clean_tags(cls, value):
        return None if value is None else ServerBase._clean_tags(value)


class ServerOut(ServerBase):
    id: int
    name: str
    host: str
    port: int
    accelerator: str | None = Field(default=None, description="gpu / npu，首次连通后自动识别")
    status: Literal["unknown", "online", "offline"]
    hostname: str | None = None
    agent_version: str | None = None
    host_info: HostInfo | None = None
    last_seen_at: datetime | None = None
    last_error: str | None = None
    models: list[str] = Field(default=[], description="该服务器上的加速卡型号")
    device_count: int = 0
    idle_device_count: int = 0
    devices: list[DeviceOut] = []
    created_at: datetime
    updated_at: datetime


class ServerGroup(BaseModel):
    key: str | None = Field(description="分组值，为 null 表示未设置该属性")
    server_count: int
    online_count: int
    device_count: int
    idle_device_count: int
    servers: list[ServerOut]


class MetricPoint(BaseModel):
    ts: datetime
    utilization: float | None = None
    memory_used_mb: float | None = None
    temperature: float | None = None
    power_w: float | None = None


class DeviceHistory(BaseModel):
    device_index: int
    points: list[MetricPoint]


class FilterOptions(BaseModel):
    groups: list[str]
    owners: list[str]
    tags: list[str]
    models: list[str]
    accelerators: list[str]


class AcceleratorSummary(BaseModel):
    servers: int
    devices: int
    idle_devices: int


class Overview(BaseModel):
    servers_total: int
    servers_online: int
    servers_offline: int
    devices_total: int
    devices_idle: int
    devices_busy: int
    by_accelerator: dict[str, AcceleratorSummary] = Field(description="按 gpu / npu 汇总")
    jobs_queued: int = Field(description="排队中的任务数")
    jobs_running: int = Field(description="启动中和运行中的任务数")
    jobs_lost: int = Field(description="失联的任务数")


JobStatus = Literal["queued", "starting", "running", "lost", "succeeded", "failed", "cancelled"]
_ENV_KEY = r"^[A-Za-z_][A-Za-z0-9_]*$"


class JobCreate(BaseModel):
    name: str | None = Field(default=None, max_length=255, description="任务名称，不填时取命令开头")
    command: str = Field(min_length=1, description="要执行的 shell 命令，以 bash -l 运行")
    workdir: str | None = Field(default=None, description="工作目录，默认为 Agent 运行用户的家目录")
    env: dict[str, str] = Field(default={}, description="额外的环境变量")
    num_devices: int = Field(default=1, ge=0, le=64, description="需要的卡数，0 表示不需要卡（如 CPU 评测）")
    group: str | None = Field(default=None, description="运行组，为空表示不限")
    accelerator: Literal["gpu", "npu"] | None = Field(default=None, description="加速卡类型，为空表示不限")
    server_id: int | None = Field(default=None, description="指定服务器，为空表示由调度器选择")
    priority: int = Field(default=0, ge=-100, le=100, description="优先级，越大越先调度")
    submitter: str | None = Field(default=None, max_length=128, description="提交人")

    @field_validator("env")
    @classmethod
    def _check_env(cls, value):
        for key in value:
            if not re.match(_ENV_KEY, key):
                raise ValueError(f"非法的环境变量名: {key}")
        return value


class JobUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    priority: int | None = Field(default=None, ge=-100, le=100, description="只有排队中的任务可以修改优先级")


class JobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    command: str
    workdir: str | None = None
    env: dict[str, str] = {}
    num_devices: int
    group: str | None = None
    accelerator: str | None = None
    server_id: int | None = None
    priority: int
    submitter: str | None = None
    status: JobStatus = Field(
        description="queued 排队中 / starting 启动中 / running 运行中 / lost 失联 / "
        "succeeded 成功 / failed 失败 / cancelled 已取消"
    )
    queue_position: int | None = Field(default=None, description="排队中的任务在队列中的位置，从 1 开始")
    assigned_server_id: int | None = None
    assigned_server_name: str | None = None
    device_indices: list[int] = Field(default=[], description="分配到的卡号")
    pid: int | None = None
    exit_code: int | None = None
    error: str | None = None
    requeued_from: int | None = Field(default=None, description="由哪个任务重新排队而来")
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None


class JobPage(BaseModel):
    total: int
    items: list[JobOut]


class JobLog(BaseModel):
    offset: int = Field(description="本次读取的起始字节位置")
    next_offset: int = Field(description="下次增量读取时传入的 offset")
    size: int = Field(description="日志文件当前总字节数")
    data: str

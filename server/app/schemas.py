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
    idle: bool = Field(default=False, description="是否空闲：服务器在线、健康、无进程、显存占用低于阈值")
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

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
    ssh_user: str | None = Field(
        default=None, max_length=64, description="SSH 登录用户，填写后由中心服务安装和升级 Agent，任务以该用户运行"
    )
    ssh_port: int = Field(default=22, ge=1, le=65535, description="SSH 端口")
    allow_roots: list[str] = Field(default=[], description="允许读取的目录（推理结果所在位置），为空时为登录用户家目录")
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
    name: str | None = Field(default=None, min_length=1, max_length=128, description="名称，不填时使用地址")
    host: str = Field(min_length=1, max_length=255, description="Agent 地址（IP 或域名）")
    port: int = Field(default=9100, ge=1, le=65535, description="Agent 端口")


class ServerUpdate(BaseModel):
    """只提交需要修改的字段。"""

    name: str | None = Field(default=None, min_length=1, max_length=128)
    host: str | None = Field(default=None, min_length=1, max_length=255)
    port: int | None = Field(default=None, ge=1, le=65535)
    ssh_user: str | None = Field(default=None, max_length=64)
    ssh_port: int | None = Field(default=None, ge=1, le=65535)
    allow_roots: list[str] | None = None
    group: str | None = None
    owner: str | None = None
    tags: list[str] | None = None
    note: str | None = None
    schedulable: bool | None = None

    @field_validator("tags")
    @classmethod
    def _clean_tags(cls, value):
        return None if value is None else ServerBase._clean_tags(value)


class DeployState(BaseModel):
    status: Literal["pending", "running", "succeeded", "failed"] = Field(
        description="pending 等待中 / running 执行中 / succeeded 成功 / failed 失败"
    )
    action: Literal["install", "upgrade"] | None = Field(default=None, description="install 首次安装 / upgrade 升级")
    version: str | None = Field(default=None, description="部署的 Agent 版本")
    error: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None


class DeployDetail(DeployState):
    log: str | None = Field(default=None, description="SSH 执行输出（最后 20000 个字符）")


class ServerOut(ServerBase):
    id: int
    name: str
    host: str
    port: int
    accelerator: str | None = Field(default=None, description="gpu / npu，首次连通后自动识别")
    status: Literal["unknown", "online", "offline"]
    hostname: str | None = None
    agent_version: str | None = None
    agent_outdated: bool = Field(default=False, description="Agent 版本与中心服务自带的版本不一致，需要升级")
    managed: bool = Field(default=False, description="是否由中心服务通过 SSH 安装和升级 Agent")
    deploy: DeployState | None = Field(default=None, description="最近一次安装或升级，从未部署过时为 null")
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
    wait_reason: str | None = Field(default=None, description="排队中的任务为什么还没被调度，由每轮调度更新")
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


MetricName = Literal["psnr", "ssim", "lpips", "ocr_a", "cer", "ned"]


class EvaluatorOut(BaseModel):
    name: MetricName
    label: str
    kind: Literal["image", "text"]
    unit: str | None = None
    higher_is_better: bool
    description: str


class ResultSetCreate(BaseModel):
    server_id: int
    path: str = Field(min_length=1, description="结果集目录在服务器上的绝对路径，目录内需有 predictions.jsonl")
    name: str | None = Field(default=None, max_length=255, description="不填时取 meta.json 的 name 或目录名")
    note: str | None = None
    job_id: int | None = Field(default=None, description="产生该结果的任务")


class ResultSetUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    note: str | None = None


class ResultSetOut(BaseModel):
    id: int
    name: str
    server_id: int
    server_name: str
    path: str
    meta: dict = Field(description="meta.json 的内容（模型、数据集、参数等）")
    sample_count: int | None = None
    note: str | None = None
    job_id: int | None = None
    metrics: dict[str, float] = Field(default={}, description="每个指标最近一次成功评测的值")
    evaluating: bool = Field(default=False, description="是否有进行中的评测")
    created_at: datetime


class SamplePage(BaseModel):
    total: int
    offset: int
    items: list[dict] = Field(
        description="predictions.jsonl 的原始记录，另加 metrics 字段（该样本的逐样本指标）；"
        "图片字段为相对路径，用 /api/results/{id}/file?path= 读取"
    )


class EvaluationCreate(BaseModel):
    result_set_id: int
    metrics: list[MetricName] = Field(min_length=1)
    reference: str | None = Field(default=None, description="可选，参考值 jsonl 在服务器上的路径，按 id 合并 ref_image / ref_text")
    num_devices: int = Field(default=0, ge=0, le=8, description="LPIPS 可用 1 张卡加速，其他指标用 0 即可")
    priority: int = Field(default=0, ge=-100, le=100)
    submitter: str | None = None


class EvaluationOut(BaseModel):
    id: int
    result_set_id: int
    result_set_name: str
    metrics: list[str]
    reference: str | None = None
    job_id: int | None = None
    job_status: str | None = None
    status: Literal["pending", "running", "succeeded", "failed"]
    values: dict[str, float | None] | None = Field(default=None, description="整体指标，无法计算的为 null")
    counts: dict[str, int] | None = Field(default=None, description="每个指标参与计算的样本数")
    errors: dict[str, str] | None = Field(default=None, description="未能计算的指标及原因")
    num_skipped: int | None = Field(default=None, description="读取失败或尺寸不一致而跳过的样本数")
    error: str | None = None
    created_at: datetime
    finished_at: datetime | None = None


class CompareResultSet(BaseModel):
    id: int
    name: str
    server_name: str
    meta: dict


class MetricCompare(BaseModel):
    result_sets: list[CompareResultSet]
    metrics: list[EvaluatorOut] = Field(description="至少一个结果集有值的指标")
    values: dict[str, dict[str, float]] = Field(description="结果集 ID -> 指标名 -> 值")


class SampleCompareItem(BaseModel):
    id: str
    results: dict[str, dict | None] = Field(description="结果集 ID -> 该样本的记录（含 metrics），缺失为 null")
    spread: float | None = Field(default=None, description="排序指标在各结果集之间的最大差值")


class SampleCompare(BaseModel):
    total: int
    offset: int
    items: list[SampleCompareItem]


class SchedulerSettings(BaseModel):
    strict_order: bool = Field(
        description="true 严格按队列顺序调度；false 允许后面的任务在前面的任务等卡时先运行"
    )


class ServerBatchCreate(BaseModel):
    servers: list[ServerCreate] = Field(min_length=1, max_length=200)
    deploy: bool = Field(default=True, description="添加后立即通过 SSH 安装 Agent（只对填写了 ssh_user 的服务器）")


class BatchError(BaseModel):
    index: int = Field(description="在请求 servers 中的位置，从 0 开始")
    host: str
    error: str


class ServerBatchResult(BaseModel):
    created: list[ServerOut]
    errors: list[BatchError]


class DeployRequest(BaseModel):
    server_ids: list[int] | None = Field(default=None, description="要安装或升级的服务器，为空时按 outdated 选择")
    outdated: bool = Field(default=False, description="server_ids 为空时，选择所有 Agent 版本落后或未安装的托管服务器")


class AgentPackage(BaseModel):
    version: str = Field(description="中心服务自带的 Agent 版本")
    ssh_available: bool = Field(description="中心主机上是否有 ssh 命令")
    public_key: str | None = Field(default=None, description="中心主机的 SSH 公钥，需要加入各服务器的 authorized_keys")
    public_key_path: str | None = None
    auto_upgrade: bool = Field(description="是否自动升级版本落后的 Agent")

from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utcnow():
    return datetime.now(timezone.utc)


class Server(Base):
    __tablename__ = "servers"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    host: Mapped[str] = mapped_column(String(255))
    port: Mapped[int] = mapped_column(Integer, default=9100)
    group: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    owner: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    tags: Mapped[list] = mapped_column(JSON, default=list)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    schedulable: Mapped[bool] = mapped_column(Boolean, default=True)

    # SSH 登录信息，填写后由中心服务安装和升级 Agent；任务以该用户运行
    ssh_user: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # SSH 连接目标，可以是中心主机 ~/.ssh/config 中的别名（沿用其中的密钥、跳板机等设置），为空时用 host
    ssh_host: Mapped[str | None] = mapped_column(String(255), nullable=True)
    ssh_port: Mapped[int | None] = mapped_column(Integer, nullable=True, default=22)
    allow_roots: Mapped[list | None] = mapped_column(JSON, nullable=True, default=list)
    # 最近一次部署：pending / running / succeeded / failed
    deploy_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    deploy_action: Mapped[str | None] = mapped_column(String(16), nullable=True)  # install / upgrade
    deploy_version: Mapped[str | None] = mapped_column(String(32), nullable=True)  # 部署的目标版本
    deploy_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    deploy_log: Mapped[str | None] = mapped_column(Text, nullable=True)
    deploy_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deploy_finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # 以下字段由轮询维护
    accelerator: Mapped[str | None] = mapped_column(String(16), nullable=True)  # gpu / npu
    status: Mapped[str] = mapped_column(String(16), default="unknown")  # unknown / online / offline
    hostname: Mapped[str | None] = mapped_column(String(255), nullable=True)
    agent_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    host_info: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    fail_count: Mapped[int] = mapped_column(Integer, default=0)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    devices: Mapped[list["Device"]] = relationship(
        back_populates="server", cascade="all, delete-orphan", order_by="Device.index"
    )


class Device(Base):
    __tablename__ = "devices"
    __table_args__ = (UniqueConstraint("server_id", "index"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    server_id: Mapped[int] = mapped_column(ForeignKey("servers.id", ondelete="CASCADE"))
    index: Mapped[int] = mapped_column(Integer)  # 逻辑卡号，即 *_VISIBLE_DEVICES 使用的编号
    vendor: Mapped[str] = mapped_column(String(16))  # nvidia / ascend
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    uuid: Mapped[str | None] = mapped_column(String(128), nullable=True)
    bus_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    npu_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    chip_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    memory_total_mb: Mapped[float | None] = mapped_column(Float, nullable=True)
    memory_used_mb: Mapped[float | None] = mapped_column(Float, nullable=True)
    utilization: Mapped[float | None] = mapped_column(Float, nullable=True)
    temperature: Mapped[float | None] = mapped_column(Float, nullable=True)
    power_w: Mapped[float | None] = mapped_column(Float, nullable=True)
    power_limit_w: Mapped[float | None] = mapped_column(Float, nullable=True)
    health: Mapped[str | None] = mapped_column(String(32), nullable=True)
    processes: Mapped[list] = mapped_column(JSON, default=list)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    server: Mapped[Server] = relationship(back_populates="devices")


class DeviceMetric(Base):
    """每张卡的历史采样点，用于趋势曲线。"""

    __tablename__ = "device_metrics"

    id: Mapped[int] = mapped_column(primary_key=True)
    server_id: Mapped[int] = mapped_column(ForeignKey("servers.id", ondelete="CASCADE"), index=True)
    device_index: Mapped[int] = mapped_column(Integer)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    utilization: Mapped[float | None] = mapped_column(Float, nullable=True)
    memory_used_mb: Mapped[float | None] = mapped_column(Float, nullable=True)
    temperature: Mapped[float | None] = mapped_column(Float, nullable=True)
    power_w: Mapped[float | None] = mapped_column(Float, nullable=True)


JOB_ACTIVE_STATUSES = ("starting", "running", "lost")
JOB_FINISHED_STATUSES = ("succeeded", "failed", "cancelled")


class Job(Base):
    """任务：在单台服务器上运行的一条命令。"""

    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    command: Mapped[str] = mapped_column(Text)
    workdir: Mapped[str | None] = mapped_column(Text, nullable=True)
    env: Mapped[dict] = mapped_column(JSON, default=dict)
    num_devices: Mapped[int] = mapped_column(Integer, default=1)
    # 调度约束：运行组、加速卡类型、指定服务器，均可为空表示不限
    group: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    accelerator: Mapped[str | None] = mapped_column(String(16), nullable=True)
    server_id: Mapped[int | None] = mapped_column(ForeignKey("servers.id", ondelete="SET NULL"), nullable=True)
    priority: Mapped[int] = mapped_column(Integer, default=0)
    submitter: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)

    # queued / starting / running / lost / succeeded / failed / cancelled
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)
    assigned_server_id: Mapped[int | None] = mapped_column(
        ForeignKey("servers.id", ondelete="SET NULL"), nullable=True, index=True
    )
    device_indices: Mapped[list] = mapped_column(JSON, default=list)
    pid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    requeued_from: Mapped[int | None] = mapped_column(Integer, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    assigned_server: Mapped[Server | None] = relationship(foreign_keys=[assigned_server_id])


class ResultSet(Base):
    """推理结果集：某台服务器上的一个目录，内含 meta.json 和 predictions.jsonl。"""

    __tablename__ = "result_sets"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    server_id: Mapped[int] = mapped_column(ForeignKey("servers.id"), index=True)
    path: Mapped[str] = mapped_column(Text)
    meta: Mapped[dict] = mapped_column(JSON, default=dict)
    sample_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    job_id: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 产生该结果的任务
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    server: Mapped[Server] = relationship()
    evaluations: Mapped[list["Evaluation"]] = relationship(
        back_populates="result_set", cascade="all, delete-orphan", order_by="Evaluation.id"
    )


class Evaluation(Base):
    """一次评测：对一个结果集计算若干指标，作为任务运行。"""

    __tablename__ = "evaluations"

    id: Mapped[int] = mapped_column(primary_key=True)
    result_set_id: Mapped[int] = mapped_column(ForeignKey("result_sets.id", ondelete="CASCADE"), index=True)
    metrics: Mapped[list] = mapped_column(JSON, default=list)  # 请求的指标名
    reference: Mapped[str | None] = mapped_column(Text, nullable=True)
    job_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending / running / succeeded / failed
    output_dir: Mapped[str] = mapped_column(Text)
    values: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # 指标名 -> 数值
    counts: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    errors: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # 指标名 -> 未能计算的原因
    num_skipped: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    result_set: Mapped[ResultSet] = relationship(back_populates="evaluations")
    job: Mapped[Job | None] = relationship()

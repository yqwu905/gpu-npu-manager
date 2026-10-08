import os
from dataclasses import dataclass, field


def _env_int(name, default):
    return int(os.environ.get(name, default))


@dataclass
class Settings:
    """中心服务配置，全部可通过环境变量覆盖。"""

    database_url: str = field(
        default_factory=lambda: os.environ.get("GNM_DATABASE_URL", "sqlite:///./data/gnm.db")
    )
    # 与各节点 Agent 共享的访问令牌
    agent_token: str = field(default_factory=lambda: os.environ.get("GNM_AGENT_TOKEN", ""))
    agent_timeout: float = field(default_factory=lambda: float(os.environ.get("GNM_AGENT_TIMEOUT", "8")))
    # 状态轮询间隔（秒）
    poll_interval: int = field(default_factory=lambda: _env_int("GNM_POLL_INTERVAL", 10))
    # 连续失败多少次判定离线
    offline_after: int = field(default_factory=lambda: _env_int("GNM_OFFLINE_AFTER", 3))
    # 历史曲线采样间隔（秒）与保留天数
    history_interval: int = field(default_factory=lambda: _env_int("GNM_HISTORY_INTERVAL", 60))
    history_days: int = field(default_factory=lambda: _env_int("GNM_HISTORY_DAYS", 7))
    # 空闲卡判定：无进程且已用显存低于阈值（MB）。昇腾卡空载时 HBM 也有 3~4 GB 占用。
    idle_memory_mb_gpu: int = field(default_factory=lambda: _env_int("GNM_IDLE_MEMORY_MB_GPU", 1024))
    idle_memory_mb_npu: int = field(default_factory=lambda: _env_int("GNM_IDLE_MEMORY_MB_NPU", 6144))
    # 是否启动后台轮询（测试时关闭）
    enable_poller: bool = field(
        default_factory=lambda: os.environ.get("GNM_ENABLE_POLLER", "1") not in ("0", "false", "False")
    )

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env_int(name, default):
    return int(os.environ.get(name, default))


@dataclass
class Settings:
    """中心服务配置，全部可通过环境变量覆盖。"""

    database_url: str = field(
        default_factory=lambda: os.environ.get("GNM_DATABASE_URL", "sqlite:///./data/gnm.db")
    )
    # 前端构建产物目录（cd web && npm run build 生成），不存在时只提供接口
    web_dir: str = field(
        default_factory=lambda: os.environ.get("GNM_WEB_DIR", str(Path(__file__).resolve().parents[2] / "web" / "dist"))
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
    # 调度器扫描队列的间隔（秒）
    schedule_interval: int = field(default_factory=lambda: _env_int("GNM_SCHEDULE_INTERVAL", 5))
    # 严格按顺序调度：排在前面的任务放不下时，后面的任务也不调度（默认允许回填）
    schedule_strict: bool = field(
        default_factory=lambda: os.environ.get("GNM_SCHEDULE_STRICT", "0") in ("1", "true", "True")
    )
    # 评测脚本在各服务器上的解释器和路径，路径为空时用 Agent 所在目录下的 evaluate.py
    eval_python: str = field(default_factory=lambda: os.environ.get("GNM_EVAL_PYTHON", "python3"))
    eval_script: str = field(default_factory=lambda: os.environ.get("GNM_EVAL_SCRIPT", ""))
    # 通过 SSH 安装和升级 Agent：安装包目录（agent.py、evaluate.py 所在目录）与 ssh 命令
    agent_package_dir: str = field(
        default_factory=lambda: os.environ.get("GNM_AGENT_PACKAGE_DIR", str(Path(__file__).resolve().parents[2] / "agent"))
    )
    ssh_command: str = field(default_factory=lambda: os.environ.get("GNM_SSH_COMMAND", "ssh"))
    deploy_timeout: int = field(default_factory=lambda: _env_int("GNM_DEPLOY_TIMEOUT", 180))
    deploy_concurrency: int = field(default_factory=lambda: _env_int("GNM_DEPLOY_CONCURRENCY", 4))
    # 评测前把结果目录拷贝到评测服务器的超时（秒）
    copy_timeout: int = field(default_factory=lambda: _env_int("GNM_COPY_TIMEOUT", 6 * 3600))
    # 中心服务升级后，自动把 Agent 版本落后的服务器升级到当前版本
    auto_upgrade: bool = field(
        default_factory=lambda: os.environ.get("GNM_AUTO_UPGRADE", "1") not in ("0", "false", "False")
    )
    # 是否启动后台轮询和调度（测试时关闭）
    enable_poller: bool = field(
        default_factory=lambda: os.environ.get("GNM_ENABLE_POLLER", "1") not in ("0", "false", "False")
    )
    # 对比页图片：派生图缓存（L2）与原图暂存目录、容量（MB）
    image_cache_dir: str = field(default_factory=lambda: os.environ.get("GNM_IMAGE_CACHE_DIR", "./data/image-cache"))
    image_cache_mb: int = field(default_factory=lambda: _env_int("GNM_IMAGE_CACHE_MB", 4096))
    image_spool_mb: int = field(default_factory=lambda: _env_int("GNM_IMAGE_SPOOL_MB", 4096))
    # 读取图片、图片列表的超时（秒）
    image_timeout: float = field(default_factory=lambda: float(os.environ.get("GNM_IMAGE_TIMEOUT", "60")))
    image_list_timeout: float = field(default_factory=lambda: float(os.environ.get("GNM_IMAGE_LIST_TIMEOUT", "120")))
    # 每个 Agent 同时进行的图片请求数：列表、缩略图、预览和瓦片、原图和文件流；排队超过 image_queue_wait 秒返回 503
    image_list_concurrency: int = field(default_factory=lambda: _env_int("GNM_IMAGE_LIST_CONCURRENCY", 2))
    image_thumb_concurrency: int = field(default_factory=lambda: _env_int("GNM_IMAGE_THUMB_CONCURRENCY", 6))
    image_main_concurrency: int = field(default_factory=lambda: _env_int("GNM_IMAGE_MAIN_CONCURRENCY", 6))
    image_stream_concurrency: int = field(default_factory=lambda: _env_int("GNM_IMAGE_STREAM_CONCURRENCY", 4))
    image_queue_wait: float = field(default_factory=lambda: float(os.environ.get("GNM_IMAGE_QUEUE_WAIT", "20")))
    # Agent 没有 Pillow 时由中心服务生成：线程数、解码内存预算、已解码整图的缓存（MB）、像素上限
    image_workers: int = field(default_factory=lambda: _env_int("GNM_IMAGE_WORKERS", min(4, os.cpu_count() or 1)))
    image_mem_mb: int = field(default_factory=lambda: _env_int("GNM_IMAGE_MEM_MB", 1024))
    image_decoded_mb: int = field(default_factory=lambda: _env_int("GNM_IMAGE_DECODED_MB", 2048))
    image_max_pixels: int = field(default_factory=lambda: int(float(os.environ.get("GNM_IMAGE_MAX_PIXELS", "3e8"))))

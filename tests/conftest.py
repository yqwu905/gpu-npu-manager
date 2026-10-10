import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "agent"))
sys.path.insert(0, str(ROOT / "server"))


@pytest.fixture(autouse=True)
def image_cache_has_room(monkeypatch):
    """图片磁盘缓存在剩余空间不足（不到 1 GB 或 5%）时不写入；测试不受本机磁盘占用的影响。
    Agent 的 imaging 与中心服务按文件加载的同一份代码是两个模块，都要改。"""
    import imaging
    from app.images import load_imaging

    for module in {imaging, load_imaging(str(ROOT / "agent"))} - {None}:
        def init(self, *args, _init=module.DiskCache.__init__, **kwargs):
            _init(self, *args, **kwargs)
            self.min_free = self.min_free_ratio = 0

        monkeypatch.setattr(module.DiskCache, "__init__", init)

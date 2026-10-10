from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

from .config import Settings
from .deployer import Deployer
from .db import Base, add_missing_columns, make_engine, make_session_factory
from .poller import Poller
from .evaluations import EvaluationCollector, EvaluationCopier
from .images import MediaService
from .routers import eval_configs, jobs, overview, projects, results, servers
from .scheduler import Scheduler
from .tunnels import Tunnels
from .web import mount_web


def create_app(settings: Settings | None = None, transport: httpx.AsyncBaseTransport | None = None) -> FastAPI:
    settings = settings or Settings()
    engine = make_engine(settings.database_url)
    Base.metadata.create_all(engine)
    add_missing_columns(engine)
    session_factory = make_session_factory(engine)
    poller = Poller(settings, session_factory, transport=transport)
    scheduler = Scheduler(settings, session_factory, transport=transport)
    collector = EvaluationCollector(settings, session_factory, scheduler.agent)
    scheduler.after_round.append(collector.run_once)
    copier = EvaluationCopier(settings, session_factory)
    copier.recover()
    tunnels = Tunnels(settings, session_factory)
    poller.tunnels = tunnels
    scheduler.agent.tunnels = tunnels
    deployer = Deployer(settings, session_factory, scheduler.agent, poller)
    deployer.recover()
    scheduler.after_round.append(deployer.auto_upgrade)
    media = MediaService(settings, scheduler.agent)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if settings.enable_poller:
            poller.start()
            scheduler.start()
        yield
        await scheduler.stop()
        await poller.stop()
        await tunnels.stop()
        await scheduler.agent.aclose()
        media.close()

    app = FastAPI(
        title="GPU/NPU 服务器管理平台",
        version="0.1.0",
        description="管理昇腾 NPU 与英伟达 GPU 服务器：状态查看、任务调度、推理结果评测。",
        lifespan=lifespan,
    )
    mount_web(app, settings.web_dir)
    app.state.settings = settings
    app.state.session_factory = session_factory
    app.state.poller = poller
    app.state.scheduler = scheduler
    app.state.deployer = deployer
    app.state.copier = copier
    app.state.media = media
    # (结果集 ID, 评测 ID) -> (时间, (Agent 地址, 端口, 基准目录))，读取文件和图片时用，见 results._media_target
    app.state.media_targets = {}
    app.include_router(overview.router)
    app.include_router(servers.router)
    app.include_router(jobs.router)
    app.include_router(results.router)
    app.include_router(projects.router)
    app.include_router(eval_configs.router)
    return app


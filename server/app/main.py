from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

from .config import Settings
from .db import Base, make_engine, make_session_factory
from .poller import Poller
from .evaluations import EvaluationCollector
from .routers import jobs, overview, results, servers
from .scheduler import Scheduler
from .web import mount_web


def create_app(settings: Settings | None = None, transport: httpx.AsyncBaseTransport | None = None) -> FastAPI:
    settings = settings or Settings()
    engine = make_engine(settings.database_url)
    Base.metadata.create_all(engine)
    session_factory = make_session_factory(engine)
    poller = Poller(settings, session_factory, transport=transport)
    scheduler = Scheduler(settings, session_factory, transport=transport)
    collector = EvaluationCollector(settings, session_factory, scheduler.agent)
    scheduler.after_round.append(collector.run_once)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if settings.enable_poller:
            poller.start()
            scheduler.start()
        yield
        await scheduler.stop()
        await poller.stop()

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
    app.include_router(overview.router)
    app.include_router(servers.router)
    app.include_router(jobs.router)
    app.include_router(results.router)
    return app


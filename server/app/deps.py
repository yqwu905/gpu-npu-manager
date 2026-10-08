from collections.abc import Iterator

from fastapi import Request
from sqlalchemy.orm import Session

from .config import Settings


def get_session(request: Request) -> Iterator[Session]:
    with request.app.state.session_factory() as session:
        yield session


def get_settings(request: Request) -> Settings:
    return request.app.state.settings

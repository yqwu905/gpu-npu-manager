import posixpath

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..deps import get_session
from ..models import EvalConfig, Evaluation, Server
from ..schemas import EvalConfigCreate, EvalConfigOut, EvalConfigUpdate
from ..timeutil import as_utc

router = APIRouter(prefix="/api/eval-configs", tags=["results"])

PATH_FIELDS = ("label_file", "gt_dir", "lq_dir", "server_path")


def config_out(config: EvalConfig) -> EvalConfigOut:
    return EvalConfigOut(
        id=config.id,
        name=config.name,
        metrics=config.metrics,
        label_file=config.label_file,
        gt_dir=config.gt_dir,
        lq_dir=config.lq_dir,
        server_id=config.server_id,
        server_name=config.server.name if config.server else None,
        server_path=config.server_path,
        num_devices=config.num_devices or 0,
        note=config.note,
        created_at=as_utc(config.created_at),
        updated_at=as_utc(config.updated_at),
    )


def normalize_paths(fields: dict) -> dict:
    """路径字段：去掉首尾空白，空串视为未填，必须是绝对路径。"""
    for field in PATH_FIELDS:
        if field not in fields:
            continue
        value = (fields[field] or "").strip()
        if value and not posixpath.isabs(value):
            raise HTTPException(422, f"{field} 必须是绝对路径")
        fields[field] = posixpath.normpath(value) if value else None
    return fields


def check_eval_server(session: Session, server_id: int | None, server_path: str | None) -> None:
    if server_id is None:
        return
    server = session.get(Server, server_id)
    if server is None:
        raise HTTPException(422, "评测服务器不存在")
    if not server_path:
        raise HTTPException(422, "指定评测服务器时需要填写评测服务器上的路径")
    if not server.ssh_user:
        raise HTTPException(422, f"评测服务器 {server.name} 没有填写 SSH 用户，无法把数据拷贝过去")


def _load(session: Session, config_id: int) -> EvalConfig:
    config = session.get(EvalConfig, config_id, options=[selectinload(EvalConfig.server)])
    if config is None:
        raise HTTPException(404, "评测配置不存在")
    return config


def _check_name(session: Session, name: str, config_id: int | None = None) -> None:
    existing = session.scalar(select(EvalConfig).where(EvalConfig.name == name))
    if existing is not None and existing.id != config_id:
        raise HTTPException(409, f"评测配置 {name} 已存在")


@router.get("", response_model=list[EvalConfigOut], summary="评测配置列表")
def list_configs(session: Session = Depends(get_session)):
    query = select(EvalConfig).options(selectinload(EvalConfig.server)).order_by(EvalConfig.name)
    return [config_out(c) for c in session.scalars(query)]


@router.post("", response_model=EvalConfigOut, status_code=201, summary="保存评测配置")
def create_config(body: EvalConfigCreate, session: Session = Depends(get_session)):
    fields = normalize_paths(body.model_dump())
    fields["name"] = fields["name"].strip()
    fields["metrics"] = list(dict.fromkeys(fields["metrics"]))
    _check_name(session, fields["name"])
    check_eval_server(session, fields["server_id"], fields["server_path"])
    config = EvalConfig(**fields)
    session.add(config)
    session.commit()
    return config_out(_load(session, config.id))


@router.patch("/{config_id}", response_model=EvalConfigOut, summary="修改评测配置（不影响已有评测）")
def update_config(config_id: int, body: EvalConfigUpdate, session: Session = Depends(get_session)):
    config = _load(session, config_id)
    fields = normalize_paths(body.model_dump(exclude_unset=True))
    for field in ("name", "metrics", "num_devices"):
        if field in fields and fields[field] is None:
            raise HTTPException(422, f"{field} 不能为空")
    if "name" in fields:
        fields["name"] = fields["name"].strip()
        _check_name(session, fields["name"], config_id)
    if "metrics" in fields:
        fields["metrics"] = list(dict.fromkeys(fields["metrics"]))
    check_eval_server(
        session, fields.get("server_id", config.server_id), fields.get("server_path", config.server_path)
    )
    for field, value in fields.items():
        setattr(config, field, value)
    session.commit()
    return config_out(_load(session, config_id))


@router.delete("/{config_id}", status_code=204, summary="删除评测配置（已有评测保留）")
def delete_config(config_id: int, session: Session = Depends(get_session)):
    config = _load(session, config_id)
    for evaluation in session.scalars(select(Evaluation).where(Evaluation.config_id == config_id)):
        evaluation.config_id = None
    session.delete(config)
    session.commit()
    return Response(status_code=204)

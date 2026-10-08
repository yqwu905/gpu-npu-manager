import posixpath

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..agent_client import AgentError
from ..deps import get_session, get_settings
from ..config import Settings
from ..evaluations import (
    META_FILE,
    METRICS,
    evaluation_job,
    latest_metric_sources,
    latest_metrics,
    predictions_path,
)
from ..models import Evaluation, ResultSet, Server
from ..schemas import (
    CompareResultSet,
    EvaluationCreate,
    EvaluationOut,
    EvaluatorOut,
    MetricCompare,
    ResultSetCreate,
    ResultSetOut,
    ResultSetUpdate,
    SampleCompare,
    SampleCompareItem,
    SamplePage,
)
from ..timeutil import as_utc

router = APIRouter(prefix="/api", tags=["results"])

JSONL_PAGE = 5000
MAX_COMPARE_SAMPLES = 50000


def _agent(request: Request):
    return request.app.state.scheduler.agent


def _load_result(session: Session, result_id: int) -> ResultSet:
    result = session.get(
        ResultSet,
        result_id,
        options=[selectinload(ResultSet.server), selectinload(ResultSet.evaluations)],
    )
    if result is None:
        raise HTTPException(404, "结果集不存在")
    return result


def result_out(result: ResultSet) -> ResultSetOut:
    return ResultSetOut(
        id=result.id,
        name=result.name,
        server_id=result.server_id,
        server_name=result.server.name,
        path=result.path,
        meta=result.meta or {},
        sample_count=result.sample_count,
        note=result.note,
        job_id=result.job_id,
        metrics=latest_metrics(result),
        evaluating=any(e.status in ("pending", "running") for e in result.evaluations),
        created_at=as_utc(result.created_at),
    )


def evaluation_out(evaluation: Evaluation) -> EvaluationOut:
    return EvaluationOut(
        id=evaluation.id,
        result_set_id=evaluation.result_set_id,
        result_set_name=evaluation.result_set.name,
        metrics=evaluation.metrics,
        reference=evaluation.reference,
        job_id=evaluation.job_id,
        job_status=evaluation.job.status if evaluation.job else None,
        status=evaluation.status,
        values=evaluation.values,
        counts=evaluation.counts,
        errors=evaluation.errors,
        num_skipped=evaluation.num_skipped,
        error=evaluation.error,
        created_at=as_utc(evaluation.created_at),
        finished_at=as_utc(evaluation.finished_at),
    )


def _agent_error(exc: AgentError, what: str) -> HTTPException:
    if exc.status in (403, 404):
        return HTTPException(422, f"{what}：{exc}")
    return HTTPException(502, f"{what}：{exc}")


# ----------------------------------------------------------------------------
# 指标
# ----------------------------------------------------------------------------


@router.get("/evaluators", response_model=list[EvaluatorOut], summary="可用的评测指标")
def list_evaluators():
    return [EvaluatorOut(name=name, **info) for name, info in METRICS.items()]


# ----------------------------------------------------------------------------
# 结果集
# ----------------------------------------------------------------------------


@router.post("/results", response_model=ResultSetOut, status_code=201, summary="登记推理结果集")
async def create_result(body: ResultSetCreate, request: Request):
    with request.app.state.session_factory() as session:
        server = session.get(Server, body.server_id)
        if server is None:
            raise HTTPException(422, "服务器不存在")
        host, port = server.host, server.port
    path = posixpath.normpath(body.path)
    if not posixpath.isabs(path):
        raise HTTPException(422, "path 必须是绝对路径")
    agent = _agent(request)
    try:
        page = await agent.read_jsonl(host, port, posixpath.join(path, "predictions.jsonl"), 0, 0)
    except AgentError as exc:
        raise _agent_error(exc, "读取 predictions.jsonl 失败")
    try:
        meta = await agent.read_json(host, port, posixpath.join(path, META_FILE))
    except AgentError as exc:
        if exc.status != 404:
            raise _agent_error(exc, "读取 meta.json 失败")
        meta = {}
    if not isinstance(meta, dict):
        meta = {"value": meta}
    with request.app.state.session_factory() as session:
        result = ResultSet(
            name=body.name or str(meta.get("name") or posixpath.basename(path)),
            server_id=body.server_id,
            path=path,
            meta=meta,
            sample_count=page["total"],
            note=body.note,
            job_id=body.job_id,
        )
        session.add(result)
        session.commit()
        return result_out(_load_result(session, result.id))


@router.get("/results", response_model=list[ResultSetOut], summary="结果集列表")
def list_results(
    server_id: int | None = Query(None),
    q: str | None = Query(None, description="关键字，匹配名称、路径、备注"),
    session: Session = Depends(get_session),
):
    query = select(ResultSet).options(selectinload(ResultSet.server), selectinload(ResultSet.evaluations))
    if server_id is not None:
        query = query.where(ResultSet.server_id == server_id)
    results = [result_out(r) for r in session.scalars(query.order_by(ResultSet.id.desc()))]
    if q:
        needle = q.lower()
        results = [r for r in results if needle in " ".join(filter(None, [r.name, r.path, r.note])).lower()]
    return results


@router.get("/results/{result_id}", response_model=ResultSetOut, summary="结果集详情")
def get_result(result_id: int, session: Session = Depends(get_session)):
    return result_out(_load_result(session, result_id))


@router.patch("/results/{result_id}", response_model=ResultSetOut, summary="修改名称或备注")
def update_result(result_id: int, body: ResultSetUpdate, session: Session = Depends(get_session)):
    result = _load_result(session, result_id)
    for field, value in body.model_dump(exclude_unset=True).items():
        if field == "name" and value is None:
            raise HTTPException(422, "name 不能为空")
        setattr(result, field, value)
    session.commit()
    return result_out(result)


@router.delete("/results/{result_id}", status_code=204, summary="删除结果集登记（不删除服务器上的文件）")
def delete_result(result_id: int, session: Session = Depends(get_session)):
    session.delete(_load_result(session, result_id))
    session.commit()
    return Response(status_code=204)


async def _per_sample_page(agent, host, port, result: ResultSet, offset: int, limit: int) -> dict[str, dict]:
    """读取逐样本指标：样本 ID -> {指标名: 值}，每个指标取最近一次成功评测。"""
    by_evaluation: dict[int, tuple[Evaluation, list[str]]] = {}
    for metric, evaluation in latest_metric_sources(result).items():
        by_evaluation.setdefault(evaluation.id, (evaluation, []))[1].append(metric)
    merged: dict[str, dict] = {}
    for evaluation, metrics in by_evaluation.values():
        try:
            page = await agent.read_jsonl(
                host, port, posixpath.join(evaluation.output_dir, "per_sample.jsonl"), offset, limit
            )
        except AgentError:
            continue  # 逐样本文件被删除等情况下只显示整体指标
        for row in page["items"]:
            values = merged.setdefault(str(row.get("id")), {})
            for metric in metrics:
                if metric in row:
                    values[metric] = row[metric]
    return merged


@router.get("/results/{result_id}/samples", response_model=SamplePage, summary="分页浏览样本")
async def result_samples(
    result_id: int,
    request: Request,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=500),
):
    with request.app.state.session_factory() as session:
        result = _load_result(session, result_id)
        host, port = result.server.host, result.server.port
    agent = _agent(request)
    try:
        page = await agent.read_jsonl(host, port, predictions_path(result), offset, limit)
    except AgentError as exc:
        raise _agent_error(exc, "读取 predictions.jsonl 失败")
    per_sample = await _per_sample_page(agent, host, port, result, offset, limit)
    items = []
    for index, record in enumerate(page["items"], start=offset):
        sample_id = str(record.get("id", index))
        items.append({**record, "metrics": per_sample.get(sample_id, {})})
    return SamplePage(total=page["total"], offset=offset, items=items)


@router.get("/results/{result_id}/file", summary="读取结果集中的文件（如图片）")
async def result_file(
    result_id: int,
    request: Request,
    path: str = Query(..., description="相对结果集目录的路径，或服务器上的绝对路径"),
):
    with request.app.state.session_factory() as session:
        result = _load_result(session, result_id)
        host, port, base = result.server.host, result.server.port, result.path
    full = path if posixpath.isabs(path) else posixpath.normpath(posixpath.join(base, path))
    try:
        data, content_type = await _agent(request).read_raw(host, port, full)
    except AgentError as exc:
        if exc.status in (403, 404):
            raise HTTPException(404, str(exc))
        raise HTTPException(502, str(exc))
    return Response(content=data, media_type=content_type, headers={"Cache-Control": "max-age=300"})


# ----------------------------------------------------------------------------
# 评测
# ----------------------------------------------------------------------------


def _load_evaluation(session: Session, evaluation_id: int) -> Evaluation:
    evaluation = session.get(
        Evaluation, evaluation_id, options=[selectinload(Evaluation.job), selectinload(Evaluation.result_set)]
    )
    if evaluation is None:
        raise HTTPException(404, "评测不存在")
    return evaluation


@router.post("/evaluations", response_model=EvaluationOut, status_code=201, summary="对结果集发起评测")
def create_evaluation(
    body: EvaluationCreate,
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
):
    result = _load_result(session, body.result_set_id)
    evaluation = Evaluation(
        result_set_id=result.id,
        metrics=list(dict.fromkeys(body.metrics)),
        reference=body.reference,
        status="pending",
        output_dir="",
    )
    session.add(evaluation)
    session.flush()
    evaluation.output_dir = posixpath.join(result.path, "eval", str(evaluation.id))
    job = evaluation_job(settings, result, evaluation, body.num_devices, body.priority, body.submitter)
    session.add(job)
    session.flush()
    evaluation.job_id = job.id
    session.commit()
    return evaluation_out(_load_evaluation(session, evaluation.id))


@router.get("/evaluations", response_model=list[EvaluationOut], summary="评测列表")
def list_evaluations(
    result_set_id: int | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
    session: Session = Depends(get_session),
):
    query = select(Evaluation).options(selectinload(Evaluation.job), selectinload(Evaluation.result_set))
    if result_set_id is not None:
        query = query.where(Evaluation.result_set_id == result_set_id)
    return [evaluation_out(e) for e in session.scalars(query.order_by(Evaluation.id.desc()).limit(limit))]


@router.get("/evaluations/{evaluation_id}", response_model=EvaluationOut, summary="评测详情")
def get_evaluation(evaluation_id: int, session: Session = Depends(get_session)):
    return evaluation_out(_load_evaluation(session, evaluation_id))


# ----------------------------------------------------------------------------
# 对比
# ----------------------------------------------------------------------------


def _load_results(session: Session, ids: list[int]) -> list[ResultSet]:
    results = []
    for result_id in dict.fromkeys(ids):
        results.append(_load_result(session, result_id))
    return results


@router.get("/compare/metrics", response_model=MetricCompare, summary="多组结果的指标对比")
def compare_metrics(
    ids: list[int] = Query(..., description="结果集 ID，可多个"),
    session: Session = Depends(get_session),
):
    results = _load_results(session, ids)
    values = {str(r.id): latest_metrics(r) for r in results}
    present = {m for v in values.values() for m in v}
    return MetricCompare(
        result_sets=[CompareResultSet(id=r.id, name=r.name, server_name=r.server.name, meta=r.meta or {}) for r in results],
        metrics=[EvaluatorOut(name=name, **info) for name, info in METRICS.items() if name in present],
        values=values,
    )


async def _load_all(agent, host, port, path: str) -> list[dict]:
    first = await agent.read_jsonl(host, port, path, 0, JSONL_PAGE)
    if first["total"] > MAX_COMPARE_SAMPLES:
        raise HTTPException(422, f"样本数超过 {MAX_COMPARE_SAMPLES}，暂不支持对比")
    items = list(first["items"])
    while len(items) < first["total"]:
        page = await agent.read_jsonl(host, port, path, len(items), JSONL_PAGE)
        if not page["items"]:
            break
        items.extend(page["items"])
    return items


async def _load_samples(agent, result: ResultSet) -> dict[str, dict]:
    host, port = result.server.host, result.server.port
    try:
        records = await _load_all(agent, host, port, predictions_path(result))
    except AgentError as exc:
        raise _agent_error(exc, f"读取 {result.name} 的 predictions.jsonl 失败")
    samples = {}
    for index, record in enumerate(records):
        samples[str(record.get("id", index))] = {**record, "metrics": {}}
    by_evaluation: dict[int, tuple[Evaluation, list[str]]] = {}
    for metric, evaluation in latest_metric_sources(result).items():
        by_evaluation.setdefault(evaluation.id, (evaluation, []))[1].append(metric)
    for evaluation, metrics in by_evaluation.values():
        try:
            rows = await _load_all(agent, host, port, posixpath.join(evaluation.output_dir, "per_sample.jsonl"))
        except AgentError:
            continue
        for row in rows:
            sample = samples.get(str(row.get("id")))
            if sample is not None:
                sample["metrics"].update({m: row[m] for m in metrics if m in row})
    return samples


@router.get("/compare/samples", response_model=SampleCompare, summary="多组结果的样本并排对比")
async def compare_samples(
    request: Request,
    ids: list[int] = Query(..., description="结果集 ID，可多个；样本顺序以第一个结果集为准"),
    sort_metric: str | None = Query(None, description="按该指标排序"),
    sort: str = Query("spread", pattern="^(spread|asc|desc)$",
                      description="spread：各结果集差值从大到小；asc/desc：按第一个结果集的值排序"),
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=200),
):
    if sort_metric is not None and sort_metric not in METRICS:
        raise HTTPException(422, f"未知指标: {sort_metric}")
    with request.app.state.session_factory() as session:
        results = _load_results(session, ids)
    agent = _agent(request)
    loaded = [(r, await _load_samples(agent, r)) for r in results]
    order = list(loaded[0][1].keys())

    spreads: dict[str, float | None] = {}
    if sort_metric is not None:
        for sample_id in order:
            values = [s[sample_id]["metrics"].get(sort_metric) for _, s in loaded if sample_id in s]
            values = [v for v in values if v is not None]
            spreads[sample_id] = max(values) - min(values) if len(values) >= 2 else None
        if sort == "spread":
            order.sort(key=lambda i: (spreads[i] is None, -(spreads[i] or 0)))
        else:
            first = loaded[0][1]
            missing = [i for i in order if first[i]["metrics"].get(sort_metric) is None]
            present = [i for i in order if first[i]["metrics"].get(sort_metric) is not None]
            present.sort(key=lambda i: first[i]["metrics"][sort_metric], reverse=(sort == "desc"))
            order = present + missing

    items = [
        SampleCompareItem(
            id=sample_id,
            results={str(r.id): samples.get(sample_id) for r, samples in loaded},
            spread=spreads.get(sample_id),
        )
        for sample_id in order[offset : offset + limit]
    ]
    return SampleCompare(total=len(order), offset=offset, items=items)

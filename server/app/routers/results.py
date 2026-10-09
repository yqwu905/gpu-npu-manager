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
    eval_server,
    evaluation_job,
    find_lq_baseline,
    latest_lq_metrics,
    latest_metric_sources,
    latest_metrics,
    lq_baseline,
    ssh_target,
)
from ..models import EvalConfig, Evaluation, Project, ResultSet, Server
from .eval_configs import check_eval_server, normalize_paths
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
    TagCount,
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
        options=[selectinload(ResultSet.server), selectinload(ResultSet.project), selectinload(ResultSet.evaluations)],
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
        project_id=result.project_id,
        project_name=result.project.name if result.project else None,
        tags=result.tags or [],
        metrics=latest_metrics(result),
        lq_metrics=latest_lq_metrics(result),
        evaluating=any(e.status in ("copying", "pending", "running") for e in result.evaluations),
        created_at=as_utc(result.created_at),
    )


def evaluation_out(evaluation: Evaluation) -> EvaluationOut:
    server = eval_server(evaluation)
    source = lq_baseline(evaluation)
    return EvaluationOut(
        id=evaluation.id,
        result_set_id=evaluation.result_set_id,
        result_set_name=evaluation.result_set.name,
        metrics=evaluation.metrics,
        reference=evaluation.reference,
        config_id=evaluation.config_id,
        config_name=evaluation.config.name if evaluation.config else None,
        label_file=evaluation.label_file,
        gt_dir=evaluation.gt_dir,
        lq_dir=evaluation.lq_dir,
        server_id=server.id,
        server_name=server.name,
        data_path=evaluation.data_path or evaluation.result_set.path,
        output_dir=evaluation.output_dir,
        compute_lq=bool(evaluation.compute_lq),
        lq_source_id=source.id if source is not None else None,
        lq_values=source.lq_values if source is not None else None,
        lq_counts=source.lq_counts if source is not None else None,
        lq_errors=source.lq_errors if source is not None else None,
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
    if body.project_id is not None:
        with request.app.state.session_factory() as session:
            _check_project(session, body.project_id)
    agent = _agent(request)
    try:
        page = await agent.read_samples(host, port, path, 0, 0)
    except AgentError as exc:
        raise _agent_error(exc, "读取结果目录失败")
    if not page["total"]:
        raise HTTPException(422, "目录里没有 predictions.jsonl，也没有图片或 .txt 文件")
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
            project_id=body.project_id,
            tags=body.tags,
        )
        session.add(result)
        session.commit()
        return result_out(_load_result(session, result.id))


def _check_project(session: Session, project_id: int | None) -> None:
    if project_id is not None and session.get(Project, project_id) is None:
        raise HTTPException(422, "项目不存在")


@router.get("/results", response_model=list[ResultSetOut], summary="结果集列表")
def list_results(
    server_id: int | None = Query(None),
    project_id: int | None = Query(None, description="只看该项目的结果集；0 表示未归档到任何项目的"),
    tag: list[str] = Query([], description="标签，可多个，需全部包含"),
    q: str | None = Query(None, description="关键字，匹配名称、路径、备注"),
    session: Session = Depends(get_session),
):
    query = select(ResultSet).options(
        selectinload(ResultSet.server), selectinload(ResultSet.project), selectinload(ResultSet.evaluations)
    )
    if server_id is not None:
        query = query.where(ResultSet.server_id == server_id)
    if project_id == 0:
        query = query.where(ResultSet.project_id.is_(None))
    elif project_id is not None:
        query = query.where(ResultSet.project_id == project_id)
    results = [result_out(r) for r in session.scalars(query.order_by(ResultSet.id.desc()))]
    wanted = {t.strip() for t in tag if t.strip()}
    if wanted:
        results = [r for r in results if wanted <= set(r.tags)]
    if q:
        needle = q.lower()
        results = [r for r in results if needle in " ".join(filter(None, [r.name, r.path, r.note])).lower()]
    return results


@router.get("/results/tags", response_model=list[TagCount], summary="结果集用到的所有标签")
def result_tags(session: Session = Depends(get_session)):
    counts: dict[str, int] = {}
    for tags in session.scalars(select(ResultSet.tags)):
        for tag in tags or []:
            counts[tag] = counts.get(tag, 0) + 1
    return [TagCount(tag=t, count=c) for t, c in sorted(counts.items(), key=lambda x: (-x[1], x[0]))]


@router.get("/results/{result_id}", response_model=ResultSetOut, summary="结果集详情")
def get_result(result_id: int, session: Session = Depends(get_session)):
    return result_out(_load_result(session, result_id))


@router.patch("/results/{result_id}", response_model=ResultSetOut, summary="修改名称、备注、所属项目或标签")
def update_result(result_id: int, body: ResultSetUpdate, session: Session = Depends(get_session)):
    result = _load_result(session, result_id)
    for field, value in body.model_dump(exclude_unset=True).items():
        if field == "name" and value is None:
            raise HTTPException(422, "name 不能为空")
        if field == "project_id":
            _check_project(session, value)
        if field == "tags" and value is None:
            value = []
        setattr(result, field, value)
    session.commit()
    session.refresh(result)
    return result_out(result)


@router.delete("/results/{result_id}", status_code=204, summary="删除结果集登记（不删除服务器上的文件）")
def delete_result(result_id: int, session: Session = Depends(get_session)):
    session.delete(_load_result(session, result_id))
    session.commit()
    return Response(status_code=204)


# 评测在逐样本结果里补充的字段（配对到的参考值、LQ 图片、OCR 识别文本）；图片在评测服务器上
SAMPLE_FIELDS = ("ref_image", "ref_text", "lq_image", "ocr_text")
SAMPLE_IMAGE_FIELDS = ("ref_image", "lq_image")


def _sample_sources(result: ResultSet) -> list[tuple[Evaluation, list[str], bool]]:
    """需要读取逐样本结果的评测：(评测, 取哪些指标, 是否取补充字段)。补充字段取最近一次成功评测。"""
    by_evaluation: dict[int, tuple[Evaluation, list[str]]] = {}
    for metric, evaluation in latest_metric_sources(result).items():
        by_evaluation.setdefault(evaluation.id, (evaluation, []))[1].append(metric)
    latest = next((e for e in reversed(result.evaluations) if e.status == "succeeded"), None)
    if latest is not None:
        by_evaluation.setdefault(latest.id, (latest, []))
    return [(e, metrics, latest is not None and e.id == latest.id) for e, metrics in by_evaluation.values()]


def _merge_row(record: dict, row: dict, evaluation: Evaluation, metrics: list[str], fields: bool) -> None:
    """把逐样本结果合并进样本记录：metrics 里放指标，记录没有的补充字段补上，图片字段的来源记到 media。"""
    record["metrics"].update({m: row[m] for m in metrics if m in row})
    if not fields:
        return
    for field in SAMPLE_FIELDS:
        if row.get(field) is not None and record.get(field) is None:
            record[field] = row[field]
            if field in SAMPLE_IMAGE_FIELDS:
                record.setdefault("media", {})[field] = evaluation.id


async def _per_sample_page(agent, result: ResultSet, records: list[dict], offset: int, limit: int) -> None:
    """读取逐样本结果合并进本页样本记录，每个指标取最近一次成功评测。"""
    by_id = {str(r.get("id", i)): r for i, r in enumerate(records, start=offset)}
    for evaluation, metrics, fields in _sample_sources(result):
        server = eval_server(evaluation, result)
        try:
            page = await agent.read_jsonl(
                server.host, server.port, posixpath.join(evaluation.output_dir, "per_sample.jsonl"), offset, limit
            )
        except AgentError:
            continue  # 逐样本文件被删除等情况下只显示整体指标
        for row in page["items"]:
            record = by_id.get(str(row.get("id")))
            if record is not None:
                _merge_row(record, row, evaluation, metrics, fields)


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
        page = await agent.read_samples(host, port, result.path, offset, limit)
    except AgentError as exc:
        raise _agent_error(exc, "读取样本失败")
    items = [{**record, "metrics": {}} for record in page["items"]]
    await _per_sample_page(agent, result, items, offset, limit)
    return SamplePage(total=page["total"], offset=offset, items=items)


@router.get("/results/{result_id}/file", summary="读取结果集中的文件（如图片）")
async def result_file(
    result_id: int,
    request: Request,
    path: str = Query(..., description="相对结果集目录的路径，或服务器上的绝对路径"),
    evaluation_id: int | None = Query(None, description="从该评测的评测服务器读取（样本的 media 字段给出）"),
):
    with request.app.state.session_factory() as session:
        result = _load_result(session, result_id)
        host, port, base = result.server.host, result.server.port, result.path
        if evaluation_id is not None:
            evaluation = next((e for e in result.evaluations if e.id == evaluation_id), None)
            if evaluation is None:
                raise HTTPException(404, "评测不存在")
            server = eval_server(evaluation, result)
            host, port, base = server.host, server.port, evaluation.data_path or result.path
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
        Evaluation,
        evaluation_id,
        options=[selectinload(Evaluation.job), selectinload(Evaluation.result_set).selectinload(ResultSet.server)],
    )
    if evaluation is None:
        raise HTTPException(404, "评测不存在")
    return evaluation


CONFIG_FIELDS = ("metrics", "label_file", "gt_dir", "lq_dir", "server_id", "server_path", "num_devices")


def _evaluation_fields(session: Session, body: EvaluationCreate) -> dict:
    """评测参数：先取评测配置中的值，再用请求里给出的字段覆盖。"""
    fields: dict = {"config_id": body.config_id}
    if body.config_id is not None:
        config = session.get(EvalConfig, body.config_id)
        if config is None:
            raise HTTPException(422, "评测配置不存在")
        fields.update({f: getattr(config, f) for f in CONFIG_FIELDS})
    given = body.model_dump(exclude_unset=True)
    fields.update({f: given[f] for f in CONFIG_FIELDS if f in given})
    fields["reference"] = body.reference
    if not fields.get("metrics"):
        raise HTTPException(422, "请选择评测指标或评测配置")
    fields["metrics"] = list(dict.fromkeys(fields["metrics"]))
    fields["num_devices"] = fields.get("num_devices") or 0
    normalize_paths(fields)
    if fields.get("server_id") is not None:
        check_eval_server(session, fields["server_id"], fields.get("server_path"))
    return fields


@router.post("/evaluations", response_model=EvaluationOut, status_code=201, summary="对结果集发起评测")
async def create_evaluation(
    body: EvaluationCreate,
    request: Request,
    settings: Settings = Depends(get_settings),
):
    with request.app.state.session_factory() as session:
        result = _load_result(session, body.result_set_id)
        fields = _evaluation_fields(session, body)
        server = session.get(Server, fields["server_id"]) if fields.get("server_id") is not None else None
        # 评测服务器就是结果所在服务器时不需要拷贝
        copy = server is not None and server.id != result.server_id
        if copy and ssh_target(result.server) is None:
            raise HTTPException(
                422, f"结果所在服务器 {result.server.name} 没有填写 SSH 用户，无法把数据拷贝到评测服务器"
            )
        evaluation = Evaluation(
            result_set=result,
            metrics=fields["metrics"],
            reference=fields["reference"],
            config_id=fields["config_id"],
            label_file=fields.get("label_file"),
            gt_dir=fields.get("gt_dir"),
            lq_dir=fields.get("lq_dir"),
            server=server if copy else None,
            status="copying" if copy else "pending",
            output_dir="",
        )
        session.add(evaluation)
        session.flush()
        if copy:
            evaluation.data_path = posixpath.join(fields["server_path"], f"result_{result.id}")
        base = evaluation.data_path or result.path
        evaluation.output_dir = posixpath.join(base, "eval", str(evaluation.id))
        if evaluation.lq_dir:
            source = find_lq_baseline(session, evaluation)
            evaluation.lq_source_id = source.id if source is not None else None
            evaluation.compute_lq = source is None
        if not copy:
            job = evaluation_job(settings, result, evaluation, fields["num_devices"], body.priority, body.submitter)
            session.add(job)
            session.flush()
            evaluation.job_id = job.id
        session.commit()
        evaluation_id = evaluation.id
    if copy:
        request.app.state.copier.submit(evaluation_id, fields["num_devices"], body.priority, body.submitter)
    with request.app.state.session_factory() as session:
        return evaluation_out(_load_evaluation(session, evaluation_id))


@router.get("/evaluations", response_model=list[EvaluationOut], summary="评测列表")
def list_evaluations(
    result_set_id: int | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
    session: Session = Depends(get_session),
):
    query = select(Evaluation).options(
        selectinload(Evaluation.job), selectinload(Evaluation.result_set).selectinload(ResultSet.server)
    )
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
    lq_values = {str(r.id): v for r in results if (v := latest_lq_metrics(r))}
    present = {m for v in values.values() for m in v}
    return MetricCompare(
        result_sets=[CompareResultSet(id=r.id, name=r.name, server_name=r.server.name, meta=r.meta or {}) for r in results],
        metrics=[EvaluatorOut(name=name, **info) for name, info in METRICS.items() if name in present],
        values=values,
        lq_values=lq_values,
    )


async def _load_all(agent, host, port, path: str, samples: bool = False) -> list[dict]:
    """读取 jsonl 文件的全部记录；samples 为 True 时 path 是结果集目录，读取它的样本。"""
    read = agent.read_samples if samples else agent.read_jsonl
    first = await read(host, port, path, 0, JSONL_PAGE)
    if first["total"] > MAX_COMPARE_SAMPLES:
        raise HTTPException(422, f"样本数超过 {MAX_COMPARE_SAMPLES}，暂不支持对比")
    items = list(first["items"])
    while len(items) < first["total"]:
        page = await read(host, port, path, len(items), JSONL_PAGE)
        if not page["items"]:
            break
        items.extend(page["items"])
    return items


async def _load_samples(agent, result: ResultSet) -> dict[str, dict]:
    host, port = result.server.host, result.server.port
    try:
        records = await _load_all(agent, host, port, result.path, samples=True)
    except AgentError as exc:
        raise _agent_error(exc, f"读取 {result.name} 的样本失败")
    samples = {}
    for index, record in enumerate(records):
        samples[str(record.get("id", index))] = {**record, "metrics": {}}
    for evaluation, metrics, fields in _sample_sources(result):
        server = eval_server(evaluation, result)
        try:
            rows = await _load_all(
                agent, server.host, server.port, posixpath.join(evaluation.output_dir, "per_sample.jsonl")
            )
        except AgentError:
            continue
        for row in rows:
            sample = samples.get(str(row.get("id")))
            if sample is not None:
                _merge_row(sample, row, evaluation, metrics, fields)
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

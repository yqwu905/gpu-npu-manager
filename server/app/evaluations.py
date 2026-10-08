"""评测：指标定义、评测命令生成，以及评测任务结束后回收指标。"""

import asyncio
import logging
import posixpath
import shlex
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from .agent_client import AgentClient, AgentError
from .config import Settings
from .models import JOB_FINISHED_STATUSES, Evaluation, Job, ResultSet

log = logging.getLogger(__name__)

# 与 agent/evaluate.py 中的指标名一致
METRICS = {
    "psnr": {"label": "PSNR", "kind": "image", "unit": "dB", "higher_is_better": True,
             "description": "峰值信噪比，按 RGB 8 位计算，完全相同时记为 100"},
    "ssim": {"label": "SSIM", "kind": "image", "unit": None, "higher_is_better": True,
             "description": "结构相似度，11x11 高斯窗，RGB 三通道平均"},
    "lpips": {"label": "LPIPS", "kind": "image", "unit": None, "higher_is_better": False,
              "description": "感知距离（AlexNet），需要服务器上安装 torch 和 lpips"},
    "ocr_a": {"label": "OCR-A", "kind": "text", "unit": None, "higher_is_better": True,
              "description": "识别文本与参考文本完全一致的样本比例"},
    "cer": {"label": "CER", "kind": "text", "unit": None, "higher_is_better": False,
            "description": "字符错误率，总编辑距离 / 参考文本总字符数"},
    "ned": {"label": "1-NED", "kind": "text", "unit": None, "higher_is_better": True,
            "description": "1 - 编辑距离 / max(预测长度, 参考长度)，各样本平均"},
}

PREDICTIONS_FILE = "predictions.jsonl"
META_FILE = "meta.json"


def predictions_path(result: ResultSet) -> str:
    return posixpath.join(result.path, PREDICTIONS_FILE)


def build_command(settings: Settings, result: ResultSet, evaluation: Evaluation, device: str) -> str:
    parts = [
        settings.eval_python,
        settings.eval_script,
        "--predictions", predictions_path(result),
        "--output", evaluation.output_dir,
        "--metrics", ",".join(evaluation.metrics),
        "--device", device,
    ]
    if evaluation.reference:
        parts += ["--reference", evaluation.reference]
    return " ".join(shlex.quote(p) for p in parts)


def latest_metric_sources(result: ResultSet) -> dict[str, Evaluation]:
    """每个指标取最近一次成功评测的值：指标名 -> 评测。"""
    sources: dict[str, Evaluation] = {}
    for evaluation in result.evaluations:  # 按 id 升序，后面的覆盖前面的
        if evaluation.status != "succeeded":
            continue
        for metric, value in (evaluation.values or {}).items():
            if value is not None:
                sources[metric] = evaluation
    return sources


def latest_metrics(result: ResultSet) -> dict[str, float]:
    return {m: e.values[m] for m, e in latest_metric_sources(result).items()}


class EvaluationCollector:
    """跟随评测任务的状态；任务成功后从服务器读取 metrics.json 入库。"""

    def __init__(self, settings: Settings, session_factory, agent: AgentClient):
        self.settings = settings
        self.session_factory = session_factory
        self.agent = agent

    def _pending(self):
        with self.session_factory() as session:
            rows = session.scalars(
                select(Evaluation)
                .options(selectinload(Evaluation.job), selectinload(Evaluation.result_set).selectinload(ResultSet.server))
                .where(Evaluation.status.in_(("pending", "running")))
            ).all()
            return [
                (e.id, e.job.status if e.job else None, e.job.error if e.job else None,
                 e.result_set.server.host, e.result_set.server.port, e.output_dir)
                for e in rows
            ]

    def _update(self, evaluation_id: int, **fields) -> None:
        with self.session_factory() as session:
            evaluation = session.get(Evaluation, evaluation_id)
            if evaluation is None:
                return
            for key, value in fields.items():
                setattr(evaluation, key, value)
            session.commit()

    async def run_once(self) -> None:
        now = datetime.now(timezone.utc)
        for evaluation_id, job_status, job_error, host, port, output_dir in await asyncio.to_thread(self._pending):
            if job_status is None:
                await asyncio.to_thread(self._update, evaluation_id, status="failed", error="评测任务不存在", finished_at=now)
            elif job_status in ("queued", "starting"):
                continue
            elif job_status in ("running", "lost"):
                await asyncio.to_thread(self._update, evaluation_id, status="running")
            elif job_status == "succeeded":
                try:
                    data = await self.agent.read_json(host, port, posixpath.join(output_dir, "metrics.json"))
                except AgentError as exc:
                    if exc.status is None:
                        continue  # 网络问题，下一轮再试
                    await asyncio.to_thread(
                        self._update, evaluation_id, status="failed", error=f"读取评测结果失败：{exc}", finished_at=now
                    )
                    continue
                await asyncio.to_thread(
                    self._update,
                    evaluation_id,
                    status="succeeded",
                    values=data.get("metrics") or {},
                    counts=data.get("counts"),
                    errors=data.get("errors") or None,
                    num_skipped=data.get("num_skipped"),
                    finished_at=now,
                )
            elif job_status in JOB_FINISHED_STATUSES:
                error = "评测任务已取消" if job_status == "cancelled" else f"评测任务失败：{job_error or '退出码非 0，详见任务日志'}"
                await asyncio.to_thread(self._update, evaluation_id, status="failed", error=error, finished_at=now)


def evaluation_job(settings: Settings, result: ResultSet, evaluation: Evaluation, num_devices: int,
                   priority: int, submitter: str | None) -> Job:
    accelerator = result.server.accelerator
    if num_devices == 0:
        device = "cpu"
    else:
        device = "npu:0" if accelerator == "npu" else "cuda:0"
    return Job(
        name=f"评测 {result.name}",
        command=build_command(settings, result, evaluation, device),
        workdir=result.path,
        env={},
        num_devices=num_devices,
        server_id=result.server_id,
        priority=priority,
        submitter=submitter,
        status="queued",
    )

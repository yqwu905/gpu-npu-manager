"""评测：指标定义、评测命令生成，以及评测任务结束后回收指标。"""

import asyncio
import logging
import os
import posixpath
import shlex
from datetime import datetime, timezone

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from .agent_client import AgentClient, AgentError
from .config import Settings
from .models import JOB_FINISHED_STATUSES, Evaluation, Job, ResultSet, Server
from .schemas import EvaluationProgress
from .tunnels import ssh_args

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
              "description": "识别文本与参考文本完全一致的区域占比（标注文件的每个框是一个区域，否则每个样本是一个区域）"},
    "cer": {"label": "CER", "kind": "text", "unit": None, "higher_is_better": False,
            "description": "字符错误率，总编辑距离 / 参考文本总字符数"},
    "ned": {"label": "1-NED", "kind": "text", "unit": None, "higher_is_better": True,
            "description": "1 - 编辑距离 / max(预测长度, 参考长度)，各区域平均"},
}

META_FILE = "meta.json"


def eval_server(evaluation: Evaluation, result: ResultSet | None = None) -> Server:
    """运行评测、存放评测输出的服务器；result 为评测所属的结果集（已加载时传入，避免再次加载）。"""
    return evaluation.server or (result or evaluation.result_set).server


def build_command(settings: Settings, result: ResultSet, evaluation: Evaluation, device: str) -> str:
    parts = [
        "--predictions", evaluation.data_path or result.path,
        "--output", evaluation.output_dir,
        "--metrics", ",".join(evaluation.metrics),
        "--device", device,
    ]
    for reference in (evaluation.reference, evaluation.gt_dir, evaluation.label_file):
        if reference:
            parts += ["--reference", reference]
    if evaluation.lq_dir:
        parts += ["--lq", evaluation.lq_dir]
        if evaluation.compute_lq:
            parts.append("--lq-baseline")
    # Agent 启动任务时设置 GNM_AGENT_DIR，evaluate.py 与 agent.py 安装在同一目录
    script = shlex.quote(settings.eval_script) if settings.eval_script else '"$GNM_AGENT_DIR/evaluate.py"'
    python = evaluation.python or settings.eval_python
    return " ".join([shlex.quote(python), script] + [shlex.quote(p) for p in parts])


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


def lq_baseline(evaluation: Evaluation) -> Evaluation | None:
    """提供该评测 LQ 基线指标的评测（自己或同配置第一次评测）。"""
    if not evaluation.lq_dir:
        return None
    return evaluation if evaluation.compute_lq else evaluation.lq_source


def latest_lq_metrics(result: ResultSet) -> dict[str, float]:
    """最近一次带 LQ 基线的成功评测对应的 LQ 指标。"""
    for evaluation in reversed(result.evaluations):
        source = lq_baseline(evaluation) if evaluation.status == "succeeded" else None
        if source is not None and source.status == "succeeded" and source.lq_values:
            return {m: v for m, v in source.lq_values.items() if v is not None}
    return {}


def find_lq_baseline(session, evaluation: Evaluation) -> Evaluation | None:
    """同一配置下 LQ、参考数据和评测服务器都相同、指标覆盖本次评测的已有基线（进行中或已成功）。"""
    if evaluation.config_id is None or not evaluation.lq_dir:
        return None
    candidates = session.scalars(
        select(Evaluation)
        .where(
            Evaluation.config_id == evaluation.config_id,
            Evaluation.compute_lq.is_(True),
            Evaluation.status.in_(("copying", "pending", "running", "succeeded")),
            Evaluation.id != evaluation.id,
        )
        .order_by(Evaluation.id)
    )
    for candidate in candidates:
        same = all(
            getattr(candidate, field) == getattr(evaluation, field)
            for field in ("lq_dir", "gt_dir", "label_file", "reference")
        ) and eval_server(candidate).id == eval_server(evaluation).id
        if same and set(evaluation.metrics) <= set(candidate.metrics):
            if candidate.status != "succeeded" or candidate.lq_values is not None:
                return candidate
    return None


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
                 eval_server(e).host, eval_server(e).port, e.output_dir)
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
                fields = {"status": "running"}
                if job_status == "running":
                    try:
                        progress = await self.agent.read_json(host, port, posixpath.join(output_dir, "progress.json"))
                    except AgentError:
                        progress = None  # 还没开始写进度，或读取失败，保留上一次的进度
                    try:
                        fields["progress"] = EvaluationProgress.model_validate(progress).model_dump()
                    except ValidationError:
                        pass
                await asyncio.to_thread(self._update, evaluation_id, **fields)
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
                    lq_values=(data.get("lq") or {}).get("metrics"),
                    lq_counts=(data.get("lq") or {}).get("counts"),
                    lq_errors=(data.get("lq") or {}).get("errors") or None,
                    finished_at=now,
                )
            elif job_status in JOB_FINISHED_STATUSES:
                error = "评测任务已取消" if job_status == "cancelled" else f"评测任务失败：{job_error or '退出码非 0，详见任务日志'}"
                await asyncio.to_thread(self._update, evaluation_id, status="failed", error=error, finished_at=now)


def evaluation_job(settings: Settings, result: ResultSet, evaluation: Evaluation, num_devices: int,
                   priority: int, submitter: str | None) -> Job:
    server = evaluation.server or result.server
    accelerator = server.accelerator
    if num_devices == 0:
        device = "cpu"
    else:
        device = "npu:0" if accelerator == "npu" else "cuda:0"
    return Job(
        name=f"评测 {result.name}",
        command=build_command(settings, result, evaluation, device),
        workdir=evaluation.data_path or result.path,
        # 任务日志是文件，不加时 Python 的输出按块缓冲，进度和第三方库的日志要很久才出现
        env={"PYTHONUNBUFFERED": "1"},
        num_devices=num_devices,
        server_id=server.id,
        priority=priority,
        submitter=submitter,
        status="queued",
    )


def ssh_target(server: Server) -> tuple[str, str, int] | None:
    """服务器的 SSH 登录信息 (user, host, port)，没有填写 ssh_user 时为 None。"""
    if not server.ssh_user:
        return None
    return server.ssh_user, server.ssh_host or server.host, server.ssh_port or 22


def copy_commands(settings: Settings, src: tuple[str, str, int], src_path: str,
                  dst: tuple[str, str, int], dst_path: str) -> tuple[list[str], list[str]]:
    """经中心主机中转拷贝目录的两条 ssh 命令：源端打包输出到 stdout，目标端从 stdin 解包。"""
    # 不拷贝结果目录下已有的评测输出
    pack = f"tar -C {shlex.quote(src_path)} --exclude=./eval -cf - ."
    unpack = f"mkdir -p {shlex.quote(dst_path)} && tar -C {shlex.quote(dst_path)} -xf -"
    return ssh_args(settings, *src) + [pack], ssh_args(settings, *dst) + [unpack]


class EvaluationCopier:
    """评测服务器与结果所在服务器不同时，先把结果目录拷贝过去，再提交评测任务。"""

    def __init__(self, settings: Settings, session_factory):
        self.settings = settings
        self.session_factory = session_factory
        self._tasks: dict[int, asyncio.Task] = {}

    def recover(self) -> None:
        """中心服务重启时，未完成的拷贝已经中断。"""
        with self.session_factory() as session:
            for evaluation in session.scalars(select(Evaluation).where(Evaluation.status == "copying")):
                evaluation.status = "failed"
                evaluation.error = "拷贝过程中中心服务重启，请重新发起评测"
                evaluation.finished_at = datetime.now(timezone.utc)
            session.commit()

    def submit(self, evaluation_id: int, num_devices: int, priority: int, submitter: str | None) -> None:
        task = asyncio.create_task(self._run(evaluation_id, num_devices, priority, submitter))
        self._tasks[evaluation_id] = task
        task.add_done_callback(lambda _: self._tasks.pop(evaluation_id, None))

    async def wait(self) -> None:
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)

    def _plan(self, evaluation_id: int):
        with self.session_factory() as session:
            evaluation = session.get(
                Evaluation, evaluation_id, options=[selectinload(Evaluation.result_set).selectinload(ResultSet.server)]
            )
            if evaluation is None or evaluation.server is None:
                return None
            result = evaluation.result_set
            return ssh_target(result.server), result.path, ssh_target(evaluation.server), evaluation.data_path

    def _fail(self, evaluation_id: int, error: str) -> None:
        with self.session_factory() as session:
            evaluation = session.get(Evaluation, evaluation_id)
            if evaluation is not None:
                evaluation.status = "failed"
                evaluation.error = error
                evaluation.finished_at = datetime.now(timezone.utc)
                session.commit()

    def _start_job(self, evaluation_id: int, num_devices: int, priority: int, submitter: str | None) -> None:
        with self.session_factory() as session:
            evaluation = session.get(
                Evaluation, evaluation_id, options=[selectinload(Evaluation.result_set).selectinload(ResultSet.server)]
            )
            if evaluation is None:
                return
            job = evaluation_job(self.settings, evaluation.result_set, evaluation, num_devices, priority, submitter)
            session.add(job)
            session.flush()
            evaluation.job_id = job.id
            evaluation.status = "pending"
            session.commit()

    async def _run(self, evaluation_id: int, num_devices: int, priority: int, submitter: str | None) -> None:
        try:
            plan = await asyncio.to_thread(self._plan, evaluation_id)
            if plan is None:
                return
            src, src_path, dst, dst_path = plan
            if src is None or dst is None:
                await asyncio.to_thread(self._fail, evaluation_id, "拷贝需要结果所在服务器和评测服务器都填写了 SSH 用户")
                return
            error = await self._copy(src, src_path, dst, dst_path)
            if error:
                await asyncio.to_thread(self._fail, evaluation_id, error)
                return
            await asyncio.to_thread(self._start_job, evaluation_id, num_devices, priority, submitter)
        except Exception as exc:  # 单个评测失败不能影响其他评测
            log.exception("copy for evaluation %s failed", evaluation_id)
            await asyncio.to_thread(self._fail, evaluation_id, f"拷贝异常：{exc}")

    async def _copy(self, src, src_path: str, dst, dst_path: str) -> str | None:
        pack, unpack = copy_commands(self.settings, src, src_path, dst, dst_path)
        read_fd, write_fd = os.pipe()
        try:
            sender = await asyncio.create_subprocess_exec(
                *pack, stdin=asyncio.subprocess.DEVNULL, stdout=write_fd, stderr=asyncio.subprocess.PIPE
            )
            try:
                receiver = await asyncio.create_subprocess_exec(
                    *unpack, stdin=read_fd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
                )
            except OSError:
                sender.kill()
                await sender.wait()
                raise
        except OSError as exc:
            return f"无法执行 ssh：{exc}"
        finally:
            # 两端子进程各自持有管道的一端，父进程关闭自己的副本，否则接收端读不到 EOF
            os.close(read_fd)
            os.close(write_fd)
        try:
            (_, send_err), (_, recv_err) = await asyncio.wait_for(
                asyncio.gather(sender.communicate(), receiver.communicate()), self.settings.copy_timeout
            )
        except asyncio.TimeoutError:
            for proc in (sender, receiver):
                if proc.returncode is None:
                    proc.kill()
                    await proc.wait()
            return f"拷贝超过 {self.settings.copy_timeout} 秒未完成"
        # 一端失败常导致另一端也失败（管道断开或收到空数据），两端的错误都列出，评测服务器在前
        failures = []
        for proc, err, side in ((receiver, recv_err, "评测服务器"), (sender, send_err, "结果所在服务器")):
            if proc.returncode != 0:
                last = next((x for x in reversed(err.decode("utf-8", "replace").splitlines()) if x.strip()), "")
                failures.append(f"{side}（退出码 {proc.returncode}）：{last}")
        return "拷贝失败：" + "；".join(failures) if failures else None

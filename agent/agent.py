#!/usr/bin/env python3
"""GPU/NPU 节点 Agent。

部署在每台服务器上，只依赖 Python 标准库（3.7+），通过 HTTP 向中心服务提供：
- GET /v1/health   存活检查
- GET /v1/status   主机概况与所有加速卡状态
- POST /v1/jobs                启动任务进程
- GET  /v1/jobs                本机所有任务记录
- GET  /v1/jobs/{id}           任务状态（running / exited）与退出码
- POST /v1/jobs/{id}/kill      终止任务的整个进程组
- GET  /v1/jobs/{id}/log       按字节偏移量读取任务日志
- GET  /v1/files/list?path=    列出白名单目录内的文件
- GET  /v1/files/raw?path=     流式读取文件原始内容（图片、json 等），带 ETag，支持 If-None-Match
- GET  /v1/files/jsonl?path=   分页读取 jsonl 文件
- GET  /v1/files/samples?path= 分页读取结果集目录的样本（predictions.jsonl，没有时扫描图片和 .txt）
- GET  /v1/files/images?path=  结果集目录的全部图片 [路径, 大小, 版本号]（排序、去重，可 gzip）
- GET  /v1/files/image?path=&kind=thumb|preview|full|tile  缩略图、预览、无损原图和瓦片（需要 imaging.py 和 Pillow）

所有请求需带请求头 X-Agent-Token，与启动参数 --token（或环境变量 GNM_AGENT_TOKEN）一致。
HTTP/1.1 长连接；图片的缓存、并发等配置见 imaging.py（GNM_AGENT_* 环境变量）。
"""
import argparse
import gzip
import hashlib
import json
import logging
import mimetypes
import os
import pwd
import re
import select
import shutil
import signal
import socket
import stat
import subprocess
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from email.utils import formatdate
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from urllib.parse import parse_qs, urlparse

from evaluate import PREDICTIONS_FILE, fill_text, scan_samples

try:
    import imaging
except Exception:  # 只拷贝了 agent.py 的手动部署仍能提供图片列表和原图
    imaging = None

AGENT_VERSION = "0.2.0"
# Agent 所在目录，evaluate.py 与它放在一起
AGENT_DIR = os.path.dirname(os.path.abspath(__file__))
SMI_TIMEOUT = 15

NVIDIA_GPU_FIELDS = [
    "index",
    "uuid",
    "name",
    "memory.total",
    "memory.used",
    "utilization.gpu",
    "temperature.gpu",
    "power.draw",
    "power.limit",
    "pci.bus_id",
]
NVIDIA_APP_FIELDS = ["gpu_uuid", "pid", "process_name", "used_memory"]

log = logging.getLogger("gnm-agent")


def build_version(directory=AGENT_DIR):
    """版本号带上 agent.py、evaluate.py 和 imaging.py 的内容摘要，中心服务据此判断是否需要升级。"""
    digest = hashlib.sha256()
    for name in ("agent.py", "evaluate.py", "imaging.py"):
        try:
            with open(os.path.join(directory, name), "rb") as f:
                digest.update(f.read())
        except OSError:
            pass
        digest.update(b"\0")
    return "{}+{}".format(AGENT_VERSION, digest.hexdigest()[:10])


VERSION = build_version()


# ---------------------------------------------------------------------------
# 通用工具
# ---------------------------------------------------------------------------


def _to_float(value):
    """把 smi 输出的单元格转成 float，NA / [N/A] / [Not Supported] 等返回 None。"""
    if value is None:
        return None
    value = value.strip()
    match = re.match(r"^-?\d+(\.\d+)?$", value)
    if not match:
        return None
    return float(value)


def _to_int(value):
    number = _to_float(value)
    return None if number is None else int(number)


def _run(cmd):
    """执行命令并返回 stdout，失败时抛出 RuntimeError。"""
    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=SMI_TIMEOUT,
            universal_newlines=True,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("执行 {} 失败: {}".format(cmd[0], exc))
    if proc.returncode != 0:
        raise RuntimeError(
            "{} 返回码 {}: {}".format(cmd[0], proc.returncode, proc.stderr.strip()[:500])
        )
    return proc.stdout


def process_user(pid):
    """读取进程所属用户名，进程不存在或无权限时返回 None。"""
    try:
        with open("/proc/{}/status".format(pid)) as f:
            for line in f:
                if line.startswith("Uid:"):
                    uid = int(line.split()[1])
                    try:
                        return pwd.getpwuid(uid).pw_name
                    except KeyError:
                        return str(uid)
    except (OSError, ValueError):
        return None
    return None


# ---------------------------------------------------------------------------
# NVIDIA GPU
# ---------------------------------------------------------------------------


def _split_csv_line(line):
    return [cell.strip() for cell in line.split(",")]


def parse_nvidia_gpus(gpu_csv, apps_csv):
    """解析 nvidia-smi --query-gpu 与 --query-compute-apps 的 csv,noheader,nounits 输出。"""
    processes_by_uuid = {}
    for line in apps_csv.splitlines():
        if not line.strip():
            continue
        cells = _split_csv_line(line)
        if len(cells) < len(NVIDIA_APP_FIELDS):
            continue
        uuid, pid, name, memory = cells[: len(NVIDIA_APP_FIELDS)]
        pid_int = _to_int(pid)
        processes_by_uuid.setdefault(uuid, []).append(
            {
                "pid": pid_int,
                "name": name,
                "user": process_user(pid_int) if pid_int is not None else None,
                "memory_mb": _to_float(memory),
            }
        )

    devices = []
    for line in gpu_csv.splitlines():
        if not line.strip():
            continue
        cells = _split_csv_line(line)
        if len(cells) < len(NVIDIA_GPU_FIELDS):
            continue
        row = dict(zip(NVIDIA_GPU_FIELDS, cells))
        devices.append(
            {
                "index": _to_int(row["index"]),
                "vendor": "nvidia",
                "model": row["name"],
                "uuid": row["uuid"],
                "bus_id": row["pci.bus_id"],
                "memory_total_mb": _to_float(row["memory.total"]),
                "memory_used_mb": _to_float(row["memory.used"]),
                "utilization": _to_float(row["utilization.gpu"]),
                "temperature": _to_float(row["temperature.gpu"]),
                "power_w": _to_float(row["power.draw"]),
                "power_limit_w": _to_float(row["power.limit"]),
                "health": "OK",
                "processes": processes_by_uuid.get(row["uuid"], []),
            }
        )
    devices.sort(key=lambda d: d["index"])
    return devices


def collect_nvidia(smi):
    gpu_csv = _run(
        [smi, "--query-gpu=" + ",".join(NVIDIA_GPU_FIELDS), "--format=csv,noheader,nounits"]
    )
    apps_csv = _run(
        [
            smi,
            "--query-compute-apps=" + ",".join(NVIDIA_APP_FIELDS),
            "--format=csv,noheader,nounits",
        ]
    )
    return parse_nvidia_gpus(gpu_csv, apps_csv)


# ---------------------------------------------------------------------------
# 昇腾 NPU
# ---------------------------------------------------------------------------

_USAGE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)")
_BUS_ID_RE = re.compile(r"^[0-9A-Fa-f]{4}:[0-9A-Fa-f]{2}:[0-9A-Fa-f]{2}\.[0-9A-Fa-f]$")


def _table_cells(line):
    """'| a | b | c |' -> ['a', 'b', 'c']。"""
    line = line.strip()
    if not (line.startswith("|") and line.endswith("|")):
        return None
    return [cell.strip() for cell in line[1:-1].split("|")]


def parse_npu_smi_info(text):
    """解析 `npu-smi info` 的表格输出。

    设备表每张卡由两行组成：
      | NPU  Name | Health | Power(W)  Temp(C)  Hugepages-Usage(page) |
      | Chip [Device] | Bus-Id | AICore(%)  Memory-Usage(MB)  [HBM-Usage(MB)] |
    910B 系列第二行带 HBM-Usage，310P 系列第二行只有 Memory-Usage 且首列多一个 Device 号。
    进程表每行为 | NPU Chip | Process id | Process name | Process memory(MB) |。
    """
    devices = []
    processes = {}
    current_npu = None
    in_process_table = False

    for raw in text.splitlines():
        cells = _table_cells(raw)
        if cells is None:
            continue
        if len(cells) >= 4 and cells[1].startswith("Process id"):
            in_process_table = True
            continue
        if cells[0].startswith("NPU") or cells[0].startswith("Chip") or cells[0].startswith("npu-smi"):
            continue

        if in_process_table:
            if len(cells) < 4 or cells[0].startswith("No running"):
                continue
            ids = cells[0].split()
            if len(ids) < 2:
                continue
            key = (_to_int(ids[0]), _to_int(ids[1]))
            pid = _to_int(cells[1])
            processes.setdefault(key, []).append(
                {
                    "pid": pid,
                    "name": cells[2],
                    "user": process_user(pid) if pid is not None else None,
                    "memory_mb": _to_float(cells[3]),
                }
            )
            continue

        if len(cells) < 3:
            continue
        if _BUS_ID_RE.match(cells[1]):
            # 芯片行
            if current_npu is None:
                continue
            ids = cells[0].split()
            chip_id = _to_int(ids[0]) if ids else None
            logic_id = _to_int(ids[1]) if len(ids) > 1 else None
            right = cells[2].split()
            aicore = _to_float(right[0]) if right else None
            usages = _USAGE_RE.findall(cells[2])
            memory = usages[-1] if usages else None  # 有 HBM 时取 HBM，否则取 Memory-Usage
            devices.append(
                {
                    "npu_id": current_npu["npu_id"],
                    "chip_id": chip_id,
                    "logic_id": logic_id,
                    "vendor": "ascend",
                    "model": current_npu["model"],
                    "uuid": None,
                    "bus_id": cells[1],
                    "memory_total_mb": float(memory[1]) if memory else None,
                    "memory_used_mb": float(memory[0]) if memory else None,
                    "utilization": aicore,
                    "temperature": current_npu["temperature"],
                    "power_w": current_npu["power_w"],
                    "power_limit_w": None,
                    "health": current_npu["health"],
                    "processes": [],
                }
            )
        else:
            # NPU 行
            ids = cells[0].split()
            if not ids or _to_int(ids[0]) is None:
                continue
            right = cells[2].split()
            current_npu = {
                "npu_id": _to_int(ids[0]),
                "model": " ".join(ids[1:]),
                "health": cells[1],
                "power_w": _to_float(right[0]) if len(right) > 0 else None,
                "temperature": _to_float(right[1]) if len(right) > 1 else None,
            }

    # 逻辑卡号：310P 输出里有 Device 列时直接使用；否则按 (NPU, Chip) 顺序编号，
    # 与 ASCEND_RT_VISIBLE_DEVICES 使用的逻辑 ID 一致（未在真实机器验证）。
    devices.sort(key=lambda d: (d["npu_id"], d["chip_id"] if d["chip_id"] is not None else 0))
    for ordinal, device in enumerate(devices):
        logic_id = device.pop("logic_id")
        device["index"] = logic_id if logic_id is not None else ordinal
        device["processes"] = processes.get((device["npu_id"], device["chip_id"]), [])
    return devices


def parse_npu_health(text):
    """解析 `npu-smi info -t health -i <NPU> -c <Chip>`，返回告警码和说明，没有告警时返回 None。

    输出形如：
      Health Status                  : Warning
      Error Code                     : 80E01801
      Error Information              : ...
    """
    fields = {}
    for line in text.splitlines():
        key, sep, value = line.partition(":")
        if sep:
            fields[key.strip().lower()] = value.strip()
    parts = [v for v in (fields.get("error code"), fields.get("error information")) if v and v.upper() != "NA"]
    return " ".join(parts) or None


def collect_npu(smi):
    devices = parse_npu_smi_info(_run([smi, "info"]))
    # 只对健康状态不是 OK 的卡查询告警详情，正常情况下不增加开销
    for device in devices:
        device["health_detail"] = None
        if device["health"] and device["health"].upper() != "OK" and device["chip_id"] is not None:
            try:
                device["health_detail"] = parse_npu_health(
                    _run([smi, "info", "-t", "health", "-i", str(device["npu_id"]), "-c", str(device["chip_id"])])
                )
            except RuntimeError as exc:
                device["health_detail"] = str(exc)[:500]
    return devices


# ---------------------------------------------------------------------------
# 主机信息
# ---------------------------------------------------------------------------


class CpuSampler(object):
    """后台线程每隔几秒采样 /proc/stat，计算 CPU 使用率。"""

    def __init__(self, interval=3.0):
        self.interval = interval
        self.percent = None
        self._last = None
        thread = threading.Thread(target=self._loop, daemon=True)
        thread.start()

    @staticmethod
    def _read():
        with open("/proc/stat") as f:
            values = [int(v) for v in f.readline().split()[1:]]
        idle = values[3] + (values[4] if len(values) > 4 else 0)
        return idle, sum(values)

    def _loop(self):
        while True:
            try:
                current = self._read()
                if self._last is not None:
                    idle = current[0] - self._last[0]
                    total = current[1] - self._last[1]
                    if total > 0:
                        self.percent = round(100.0 * (total - idle) / total, 1)
                self._last = current
            except (OSError, ValueError, IndexError):
                pass
            time.sleep(self.interval)


def read_meminfo():
    info = {}
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                key, value = line.split(":", 1)
                info[key] = int(value.split()[0])  # kB
    except (OSError, ValueError):
        return None, None
    total = info.get("MemTotal")
    available = info.get("MemAvailable", info.get("MemFree"))
    if total is None or available is None:
        return None, None
    return round(total / 1024.0, 1), round((total - available) / 1024.0, 1)


def read_disks(paths):
    disks = []
    for path in paths:
        try:
            usage = shutil.disk_usage(path)
        except OSError:
            continue
        disks.append(
            {
                "path": path,
                "total_gb": round(usage.total / 1024.0 ** 3, 1),
                "used_gb": round(usage.used / 1024.0 ** 3, 1),
            }
        )
    return disks


# ---------------------------------------------------------------------------
# 状态汇总
# ---------------------------------------------------------------------------


class Collector(object):
    def __init__(self, disk_paths):
        self.disk_paths = disk_paths
        self.cpu = CpuSampler()
        self.nvidia_smi = os.environ.get("GNM_NVIDIA_SMI") or shutil.which("nvidia-smi")
        self.npu_smi = os.environ.get("GNM_NPU_SMI") or shutil.which("npu-smi")

    @property
    def accelerator(self):
        if self.nvidia_smi:
            return "gpu"
        if self.npu_smi:
            return "npu"
        return None

    def status(self):
        errors = []
        devices = []
        accelerator = None
        if self.nvidia_smi:
            accelerator = "gpu"
            try:
                devices.extend(collect_nvidia(self.nvidia_smi))
            except RuntimeError as exc:
                errors.append(str(exc))
        if self.npu_smi:
            accelerator = "npu" if accelerator is None else accelerator
            try:
                devices.extend(collect_npu(self.npu_smi))
            except RuntimeError as exc:
                errors.append(str(exc))

        mem_total, mem_used = read_meminfo()
        try:
            load1 = round(os.getloadavg()[0], 2)
        except OSError:
            load1 = None
        return {
            "agent_version": VERSION,
            "hostname": socket.gethostname(),
            "time": time.time(),
            "accelerator": accelerator,
            "host": {
                "cpu_count": os.cpu_count(),
                "cpu_percent": self.cpu.percent,
                "load1": load1,
                "memory_total_mb": mem_total,
                "memory_used_mb": mem_used,
                "disks": read_disks(self.disk_paths),
            },
            "devices": devices,
            "errors": errors,
        }


# ---------------------------------------------------------------------------
# 任务进程管理
# ---------------------------------------------------------------------------

_JOB_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
KILL_GRACE_SECONDS = 10
MAX_LOG_CHUNK = 1024 * 1024

# 外层 sh 负责在命令结束后把退出码写入文件，这样 Agent 重启后仍能拿到退出码。
# 命令用 bash -l 执行，以加载登录用户的环境（如 conda）。
_WRAPPER = 'bash -lc "$1"; echo $? > "$2"'


class JobError(Exception):
    def __init__(self, code, message):
        Exception.__init__(self, message)
        self.code = code
        self.message = message


def _proc_start_ticks(pid):
    """读取 /proc/<pid>/stat 中的进程启动时间，用于防止 PID 复用误判；进程不存在或已僵死返回 None。"""
    try:
        with open("/proc/{}/stat".format(pid)) as f:
            fields = f.read().rsplit(")", 1)[1].split()
    except (OSError, IndexError):
        return None
    if fields[0] == "Z":
        return None
    return int(fields[19])


def _decode_utf8_prefix(data):
    """解码字节串；结尾被截断的多字节字符留到下一次读取。返回 (文本, 实际消费的字节数)。"""
    for cut in range(0, 4):
        end = len(data) - cut
        if end < 0:
            break
        try:
            return data[:end].decode("utf-8"), end
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace"), len(data)


class JobManager(object):
    def __init__(self, data_dir, accelerator):
        self.dir = os.path.join(data_dir, "jobs")
        if not os.path.isdir(self.dir):
            os.makedirs(self.dir)
        self.accelerator = accelerator
        self.lock = threading.Lock()

    def _path(self, job_id, suffix):
        return os.path.join(self.dir, job_id + suffix)

    def _load(self, job_id):
        if not _JOB_ID_RE.match(job_id or ""):
            raise JobError(400, "非法的任务 ID")
        try:
            with open(self._path(job_id, ".json")) as f:
                return json.load(f)
        except (OSError, ValueError):
            raise JobError(404, "任务不存在")

    def _save(self, meta):
        tmp = self._path(meta["job_id"], ".json.tmp")
        with open(tmp, "w") as f:
            json.dump(meta, f, ensure_ascii=False)
        os.rename(tmp, self._path(meta["job_id"], ".json"))

    def _alive(self, meta):
        ticks = _proc_start_ticks(meta["pid"])
        return ticks is not None and ticks == meta.get("start_ticks")

    def _info(self, meta):
        alive = self._alive(meta)
        exit_code = None
        if not alive:
            try:
                with open(meta["exit_path"]) as f:
                    exit_code = int(f.read().strip())
            except (OSError, ValueError):
                exit_code = None  # 被信号终止时没有退出码文件
        return {
            "job_id": meta["job_id"],
            "pid": meta["pid"],
            "state": "running" if alive else "exited",
            "exit_code": exit_code,
            "killed": meta.get("killed", False),
            "devices": meta["devices"],
            "started_at": meta["started_at"],
            "log_size": os.path.getsize(meta["log_path"]) if os.path.exists(meta["log_path"]) else 0,
        }

    def start(self, spec):
        job_id = str(spec.get("job_id") or "")
        if not _JOB_ID_RE.match(job_id):
            raise JobError(400, "非法的任务 ID")
        command = spec.get("command")
        if not isinstance(command, str) or not command.strip():
            raise JobError(400, "command 不能为空")
        workdir = os.path.expanduser(spec.get("workdir") or "~")
        if not os.path.isdir(workdir):
            raise JobError(400, "工作目录不存在: {}".format(workdir))
        devices = [int(d) for d in spec.get("devices") or []]
        extra_env = spec.get("env") or {}
        for key in extra_env:
            if not _ENV_KEY_RE.match(key):
                raise JobError(400, "非法的环境变量名: {}".format(key))

        with self.lock:
            # 幂等：同一个任务 ID 重复提交时直接返回已有记录
            if os.path.exists(self._path(job_id, ".json")):
                return self._info(self._load(job_id))

            env = os.environ.copy()
            env.update({k: str(v) for k, v in extra_env.items()})
            visible = ",".join(str(d) for d in devices)
            if self.accelerator == "npu":
                if devices:
                    env["ASCEND_RT_VISIBLE_DEVICES"] = visible
            else:
                # 不分配卡时设为空字符串，进程看不到任何 GPU
                env["CUDA_VISIBLE_DEVICES"] = visible
            env["GNM_JOB_ID"] = job_id
            env["GNM_AGENT_DIR"] = AGENT_DIR

            log_path = self._path(job_id, ".log")
            exit_path = self._path(job_id, ".exit")
            with open(log_path, "ab") as log_file:
                try:
                    proc = subprocess.Popen(
                        ["/bin/sh", "-c", _WRAPPER, "sh", command, exit_path],
                        cwd=workdir,
                        env=env,
                        stdin=subprocess.DEVNULL,
                        stdout=log_file,
                        stderr=subprocess.STDOUT,
                        start_new_session=True,
                    )
                except OSError as exc:
                    raise JobError(500, "启动失败: {}".format(exc))
            # 回收子进程，避免产生僵尸进程
            threading.Thread(target=proc.wait, daemon=True).start()
            meta = {
                "job_id": job_id,
                "pid": proc.pid,
                "start_ticks": _proc_start_ticks(proc.pid),
                "command": command,
                "workdir": workdir,
                "devices": devices,
                "log_path": log_path,
                "exit_path": exit_path,
                "started_at": time.time(),
            }
            self._save(meta)
            log.info("job %s started, pid=%s devices=%s", job_id, proc.pid, visible)
            return self._info(meta)

    def get(self, job_id):
        return self._info(self._load(job_id))

    def list(self):
        jobs = []
        for name in sorted(os.listdir(self.dir)):
            if name.endswith(".json"):
                try:
                    jobs.append(self.get(name[: -len(".json")]))
                except JobError:
                    continue
        return jobs

    def kill(self, job_id):
        meta = self._load(job_id)
        if self._alive(meta):
            meta["killed"] = True
            self._save(meta)
            self._signal(meta, signal.SIGTERM)

            def force():
                time.sleep(KILL_GRACE_SECONDS)
                if self._alive(meta):
                    self._signal(meta, signal.SIGKILL)

            threading.Thread(target=force, daemon=True).start()
        return self._info(meta)

    @staticmethod
    def _signal(meta, sig):
        try:
            os.killpg(meta["pid"], sig)
        except OSError:
            pass

    def read_log(self, job_id, offset, limit):
        meta = self._load(job_id)
        limit = max(1, min(limit, MAX_LOG_CHUNK))
        try:
            with open(meta["log_path"], "rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                offset = max(0, min(offset, size))
                f.seek(offset)
                chunk = f.read(limit)
        except OSError:
            size, chunk, offset = 0, b"", 0
        text, used = _decode_utf8_prefix(chunk)
        return {"offset": offset, "next_offset": offset + used, "size": size, "data": text}


# ---------------------------------------------------------------------------
# 结果文件读取
# ---------------------------------------------------------------------------

MAX_RAW_BYTES = 64 * 1024 * 1024
MAX_JSONL_PAGE = 5000
# 扫描结果集目录的结果缓存多久（秒），翻页时不必每次都重新扫描
SCAN_CACHE_SECONDS = 30
# 图片列表最多返回的条数、缓存的目录数、并行 stat 的线程数和每批的文件数
MAX_IMAGE_LIST = 200000
LIST_CACHE_SIZE = 16
STAT_WORKERS = 16
STAT_CHUNK = 256
# 等别的目录构建图片列表的最长时间（秒），超过后并发构建
BUILD_WAIT = 1


def source_version(st):
    """文件版本号（与 imaging.version 相同）：修改时间和大小的摘要，中心服务和浏览器据此长期缓存。"""
    return hashlib.sha1(b"%d:%d" % (st.st_mtime_ns, st.st_size)).hexdigest()[:10]


def _basename(path):
    return path[max(path.rfind("/"), path.rfind("\\")) + 1 :]


class FileStore(object):
    """读取文件，供结果浏览和评测结果回读。

    默认不限制路径（仍受运行用户的文件权限约束）；指定了 --allow-root 时只允许读取这些目录。
    """

    def __init__(self, roots):
        self.roots = [os.path.realpath(os.path.expanduser(r)) for r in roots]
        self._index_cache = {}
        self._scan_cache = {}
        self._list_cache = OrderedDict()
        # 正在构建的图片列表：目录 -> [锁, 引用数]，同一目录只构建一次，其余请求排在后面拿缓存
        self._building = {}
        # 不同目录的列表尽量一个一个构建：构建是 CPU 密集的 Python 代码，并发时每次 stat 都要交接 GIL，两三个一起反而慢 3–6 倍
        # （还会让并行 stat 的判断误以为磁盘慢、再开 16 个线程更慢）；最多等 BUILD_WAIT 秒，某个目录在冷的或卡住的 NFS 上时不拖住其他目录
        self._build_lock = threading.Lock()
        self._lock = threading.Lock()

    def resolve(self, path):
        if not path:
            raise JobError(400, "缺少 path 参数")
        real = os.path.realpath(os.path.expanduser(path))
        if not self.roots:
            return real
        for root in self.roots:
            if real == root or real.startswith(root.rstrip(os.sep) + os.sep):
                return real
        raise JobError(403, "路径不在允许访问的目录内: {}".format(path))

    def list_dir(self, path):
        real = self.resolve(path)
        if not os.path.isdir(real):
            raise JobError(404, "目录不存在")
        entries = []
        for name in sorted(os.listdir(real)):
            full = os.path.join(real, name)
            try:
                st = os.stat(full)
            except OSError:
                continue
            entries.append(
                {
                    "name": name,
                    "type": "dir" if os.path.isdir(full) else "file",
                    "size": st.st_size,
                    "mtime": st.st_mtime,
                }
            )
        return {"path": real, "entries": entries}

    def read_raw(self, path):
        real = self.resolve(path)
        if not os.path.isfile(real):
            raise JobError(404, "文件不存在")
        if os.path.getsize(real) > MAX_RAW_BYTES:
            raise JobError(413, "文件过大")
        with open(real, "rb") as f:
            data = f.read()
        content_type = mimetypes.guess_type(real)[0] or "application/octet-stream"
        return data, content_type

    def open_file(self, path):
        """打开普通文件，返回 (真实路径, 文件, stat)。"""
        real = self.resolve(path)
        if not os.path.isfile(real):
            raise JobError(404, "文件不存在")
        try:
            f = open(real, "rb")
        except OSError:
            raise JobError(404, "文件不存在")
        return real, f, os.fstat(f.fileno())

    def _line_index(self, real):
        """返回每个非空行的起始字节位置；按 (大小, 修改时间) 缓存。"""
        st = os.stat(real)
        key = (st.st_size, st.st_mtime)
        with self._lock:
            cached = self._index_cache.get(real)
            if cached and cached[0] == key:
                return cached[1]
        offsets = []
        pos = 0
        with open(real, "rb") as f:
            for line in f:
                if line.strip():
                    offsets.append(pos)
                pos += len(line)
        with self._lock:
            self._index_cache[real] = (key, offsets)
        return offsets

    def read_jsonl(self, path, offset, limit):
        real = self.resolve(path)
        if not os.path.isfile(real):
            raise JobError(404, "文件不存在")
        offsets = self._line_index(real)
        offset = max(0, offset)
        limit = max(0, min(limit, MAX_JSONL_PAGE))
        items = []
        with open(real, "rb") as f:
            for number, start in enumerate(offsets[offset : offset + limit], start=offset + 1):
                f.seek(start)
                line = f.readline().decode("utf-8", errors="replace")
                try:
                    items.append(json.loads(line))
                except ValueError as exc:
                    items.append({"_error": "第 {} 条记录不是合法 JSON: {}".format(number, exc)})
        return {"total": len(offsets), "offset": offset, "items": items}

    def read_samples(self, path, offset, limit):
        """结果集目录的样本：有 predictions.jsonl 时读取它，否则扫描目录里的图片和 .txt。"""
        real = self.resolve(path)
        if not os.path.isdir(real):
            raise JobError(404, "目录不存在")
        jsonl = os.path.join(real, PREDICTIONS_FILE)
        if os.path.isfile(jsonl):
            return self.read_jsonl(jsonl, offset, limit)
        samples = self._scan(real)
        offset = max(0, offset)
        limit = max(0, min(limit, MAX_JSONL_PAGE))
        items = [fill_text(real, dict(s)) for s in samples[offset : offset + limit]]
        return {"total": len(samples), "offset": offset, "items": items}

    def _scan(self, real):
        """扫描目录得到的样本，缓存 SCAN_CACHE_SECONDS 秒。"""
        now = time.monotonic()
        with self._lock:
            cached = self._scan_cache.get(real)
        if cached and now - cached[0] < SCAN_CACHE_SECONDS:
            return cached[1]
        samples = scan_samples(real)
        with self._lock:
            self._scan_cache[real] = (now, samples)
        return samples

    def image_list(self, path, limit=MAX_IMAGE_LIST, gone=None):
        """结果集目录的全部图片，返回 (ETag, JSON, gzip 压缩的 JSON)。

        路径取自 predictions.jsonl 每条记录的 image 字段（没有 jsonl 时扫描目录），去重后按 (文件名, 路径) 排序，
        每项为 [路径, 字节数（-1 为不存在或不可读）, 版本号]。按 jsonl 的大小和修改时间缓存 SCAN_CACHE_SECONDS 秒。
        gone() 为真时（客户端已断开）不再等同一目录正在进行的构建，抛出 ConnectionError。
        """
        real = self.resolve(path)
        if not os.path.isdir(real):
            raise JobError(404, "目录不存在")
        jsonl = os.path.join(real, PREDICTIONS_FILE)
        try:
            st = os.stat(jsonl)
            sig = ("j", st.st_size, st.st_mtime_ns, limit) if stat.S_ISREG(st.st_mode) else ("s", limit)
        except OSError:
            sig = ("s", limit)
        if sig[0] == "j":
            jsonl = self.resolve(jsonl)  # 与 read_samples 相同：符号链接指向允许的目录外时 403
        cached = self._cached_list(real, sig)
        if cached:
            return cached
        with self._lock:
            slot = self._building.setdefault(real, [threading.Lock(), 0])
            slot[1] += 1
        try:
            while not slot[0].acquire(timeout=1):  # 同一目录的请求排在后面，拿到刚建好的缓存
                if gone and gone():
                    raise ConnectionError("客户端已断开")
            try:
                cached = self._cached_list(real, sig)
                if cached:
                    return cached
                got = self._build_lock.acquire(timeout=BUILD_WAIT)
                try:
                    built = self._build_list(real, jsonl if sig[0] == "j" else None, limit)
                finally:
                    if got:
                        self._build_lock.release()
                entry = (sig, time.monotonic()) + built  # 建好后才计时，构建很慢时缓存也能用满 SCAN_CACHE_SECONDS
                with self._lock:
                    self._list_cache[real] = entry
                    self._list_cache.move_to_end(real)
                    while len(self._list_cache) > LIST_CACHE_SIZE:
                        self._list_cache.popitem(last=False)
            finally:
                slot[0].release()
        finally:
            with self._lock:
                slot[1] -= 1
                if not slot[1]:
                    del self._building[real]
        return entry[2:]

    def _cached_list(self, real, sig):
        with self._lock:
            cached = self._list_cache.get(real)
            if cached and cached[0] == sig and time.monotonic() - cached[1] < SCAN_CACHE_SECONDS:
                self._list_cache.move_to_end(real)
                return cached[2:]
        return None

    def _build_list(self, real, jsonl, limit):
        paths, skipped = [], 0

        def take(record):
            image = record.get("image") if isinstance(record, dict) else None
            if isinstance(image, str) and "\0" not in image:
                try:
                    image.encode("utf-8", "surrogateescape")  # 单独的代理字符既不能 stat 也不能编码进响应
                except UnicodeEncodeError:
                    return 1
                paths.append(image)
                return 0
            return 1

        if jsonl:
            with open(jsonl, "rb") as f:
                for line in f:
                    if line.strip():
                        try:
                            skipped += take(json.loads(line.decode("utf-8", errors="replace")))
                        except ValueError:
                            skipped += 1
        else:
            for sample in self._scan(real):
                skipped += take(sample)
        seen = set()
        paths = [p for p in paths if not (p in seen or seen.add(p))]
        paths.sort(key=lambda p: (_basename(p), p))
        truncated = len(paths) > limit
        files = self._stat_images(real, paths[:limit])
        body = json.dumps(
            {
                "total": len(files),
                "missing": sum(1 for item in files if item[1] == -1),
                "skipped": skipped,
                "truncated": truncated,
                "source": "agent",
                "files": files,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8", "surrogateescape")
        return '"%s"' % hashlib.sha1(body).hexdigest()[:16], body, gzip.compress(body, 6)

    def _stat_images(self, real, paths):
        """并行 stat；不在允许的目录内、不存在或不是普通文件时大小为 -1、版本号为空。"""
        folders = {}

        def one(p):
            target = p if os.path.isabs(p) else os.path.join(real, p)
            try:
                if self.roots:
                    # 目录的真实路径按目录缓存，文件本身是符号链接时再整体解析
                    folder, name = os.path.split(target)
                    resolved = folders.get(folder)
                    if resolved is None:
                        try:
                            resolved = self.resolve(folder)
                        except JobError:
                            resolved = ""
                        folders[folder] = resolved
                    if not resolved:
                        return [p, -1, ""]
                    target = os.path.join(resolved, name)
                    st = os.lstat(target)
                    if stat.S_ISLNK(st.st_mode):
                        st = os.stat(self.resolve(target))
                else:
                    st = os.stat(target)
            except (OSError, ValueError, JobError):
                return [p, -1, ""]
            if not stat.S_ISREG(st.st_mode):
                return [p, -1, ""]
            return [p, st.st_size, source_version(st)]

        def chunk(part):
            return [one(p) for p in part]

        # 先单线程 stat 一段：本地盘或属性已缓存时很快，多线程争 GIL 反而更慢；慢（如冷的 NFS）时再并行
        started = time.monotonic()
        head = chunk(paths[:STAT_CHUNK])
        rest = paths[STAT_CHUNK:]
        if not rest or time.monotonic() - started < STAT_CHUNK * 20e-6:
            return head + chunk(rest)
        parts = [rest[i : i + STAT_CHUNK] for i in range(0, len(rest), STAT_CHUNK)]
        with ThreadPoolExecutor(STAT_WORKERS) as pool:
            return head + [item for part in pool.map(chunk, parts) for item in part]


# ---------------------------------------------------------------------------
# HTTP 服务
# ---------------------------------------------------------------------------


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    # 中心服务会同时建立几十个长连接
    request_queue_size = 128


# 请求体上限，超过时不读，回复后断开连接
MAX_BODY = 16 * 1024 * 1024
IMAGE_KINDS = ("thumb", "preview", "full", "tile")


def make_handler(collector, jobs, files, token, images=None):
    features = {
        "list": 1,
        "stream": 1,
        "image": imaging.PIL_VERSION if images is not None else None,
        "webp": bool(images is not None and imaging.WEBP),
        "tiles": images is not None,
    }
    if imaging is not None:
        image_errors = (
            (imaging.Unsupported, 501),
            (imaging.TooLarge, 413),
            (imaging.BadImage, 415),
        )

    class Handler(BaseHTTPRequestHandler):
        server_version = "gnm-agent/" + AGENT_VERSION
        # 长连接：中心服务复用连接请求大量缩略图，空闲 120 秒后断开
        protocol_version = "HTTP/1.1"
        timeout = 120
        # 响应头和响应体分两次写出，长连接上不关 Nagle 会与对端的延迟确认叠加，每个响应多等约 40 ms
        disable_nagle_algorithm = True

        def log_message(self, fmt, *args):
            log.debug("%s - %s", self.address_string(), fmt % args)

        def _end_headers(self):
            if self.close_connection:
                self.send_header("Connection", "close")
            self.end_headers()

        def _send(self, code, payload, headers=()):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            for key, value in headers:
                self.send_header(key, value)
            self._end_headers()
            self.wfile.write(body)

        def _send_bytes(self, data, headers):
            self.send_response(200)
            for key, value in headers:
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(data)))
            self._end_headers()
            self.wfile.write(data)

        def _send_file(self, f, offset, length, headers):
            """用 sendfile 发送文件的 [offset, offset + length)；文件在发送时变短时断开连接。"""
            self.send_response(200)
            for key, value in headers:
                self.send_header(key, value)
            self.send_header("Content-Length", str(length))
            self._end_headers()
            if not length:
                return
            try:
                sent = self.connection.sendfile(f, offset, length)
            except AttributeError:
                f.seek(offset)
                sent = 0
                while sent < length:
                    chunk = f.read(min(1 << 20, length - sent))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    sent += len(chunk)
            if sent < length:
                self.close_connection = True

        def _not_modified(self, etag, version=None):
            """If-None-Match 与 etag 一致时回复 304。"""
            tags = [t.strip() for t in (self.headers.get("If-None-Match") or "").split(",")]
            if etag not in [t[2:] if t.startswith("W/") else t for t in tags] and "*" not in tags:
                return False
            self.send_response(304)
            self.send_header("ETag", etag)
            if version:
                self.send_header("X-Source-Version", version)
            self._end_headers()
            return True

        def _client_gone(self):
            """客户端是否已断开（对端关闭或复位），排队中的图片请求据此放弃。"""
            try:
                poller = select.poll()
                poller.register(self.connection, select.POLLIN)
                if not poller.poll(0):
                    return False
                return self.connection.recv(1, socket.MSG_PEEK) == b""
            except (OSError, ValueError):
                return True

        def _authorized(self):
            if token and self.headers.get("X-Agent-Token") != token:
                # 不读请求体（免得没有 token 也能让 Agent 读入大块数据），回复后断开，请求体不会留在长连接上
                self.close_connection = True
                self._send(401, {"error": "invalid token"})
                return False
            return True

        def _read_body(self):
            """先读完请求体，这样路由不存在、请求体用不上时也不会留在长连接上打乱下一个请求。"""
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if length < 0 or length > MAX_BODY or self.headers.get("Transfer-Encoding"):
                self.close_connection = True
                return b""
            return self.rfile.read(length) if length else b""

        def _read_json(self):
            if not self._body:
                return {}
            try:
                return json.loads(self._body.decode("utf-8"))
            except ValueError:
                raise JobError(400, "请求体不是合法 JSON")

        def _dispatch(self, method):
            try:
                if self._authorized():
                    self._body = self._read_body()
                    self._route(method)
            except (ConnectionError, socket.timeout):  # 客户端已断开
                self.close_connection = True

        def _route(self, method):
            url = urlparse(self.path)
            parts = [p for p in url.path.split("/") if p]
            query = parse_qs(url.query)
            try:
                if method == "GET" and parts == ["v1", "health"]:
                    self._send(200, {"ok": True, "agent_version": VERSION, "features": features})
                elif method == "GET" and parts == ["v1", "status"]:
                    self._send(200, collector.status())
                elif method == "GET" and parts == ["v1", "jobs"]:
                    self._send(200, {"jobs": jobs.list()})
                elif method == "POST" and parts == ["v1", "jobs"]:
                    self._send(200, jobs.start(self._read_json()))
                elif method == "GET" and len(parts) == 3 and parts[:2] == ["v1", "jobs"]:
                    self._send(200, jobs.get(parts[2]))
                elif method == "POST" and len(parts) == 4 and parts[:2] == ["v1", "jobs"] and parts[3] == "kill":
                    self._send(200, jobs.kill(parts[2]))
                elif method == "GET" and len(parts) == 4 and parts[:2] == ["v1", "jobs"] and parts[3] == "log":
                    offset = int(query.get("offset", ["0"])[0])
                    limit = int(query.get("limit", [str(64 * 1024)])[0])
                    self._send(200, jobs.read_log(parts[2], offset, limit))
                elif method == "GET" and parts == ["v1", "files", "list"]:
                    self._send(200, files.list_dir(query.get("path", [""])[0]))
                elif method == "GET" and parts == ["v1", "files", "raw"]:
                    self._raw(query.get("path", [""])[0])
                elif method == "GET" and parts == ["v1", "files", "jsonl"]:
                    offset = int(query.get("offset", ["0"])[0])
                    limit = int(query.get("limit", ["100"])[0])
                    self._send(200, files.read_jsonl(query.get("path", [""])[0], offset, limit))
                elif method == "GET" and parts == ["v1", "files", "samples"]:
                    offset = int(query.get("offset", ["0"])[0])
                    limit = int(query.get("limit", ["100"])[0])
                    self._send(200, files.read_samples(query.get("path", [""])[0], offset, limit))
                elif method == "GET" and parts == ["v1", "files", "images"]:
                    limit = int(query.get("limit", [str(MAX_IMAGE_LIST)])[0])
                    self._image_list(query.get("path", [""])[0], max(0, min(limit, MAX_IMAGE_LIST)))
                elif method == "GET" and parts == ["v1", "files", "image"]:
                    self._image({k: v[0] for k, v in query.items()})
                else:
                    self._send(404, {"error": "not found"})
            except JobError as exc:
                self._send(exc.code, {"error": exc.message})
            except (ValueError, TypeError) as exc:
                self._send(400, {"error": str(exc)})

        def _raw(self, path):
            real, f, st = files.open_file(path)
            with f:
                version = source_version(st)
                tag = '"%s"' % version
                if self._not_modified(tag, version):
                    return
                self._send_file(f, 0, st.st_size, [
                    ("Content-Type", mimetypes.guess_type(real)[0] or "application/octet-stream"),
                    ("ETag", tag),
                    ("X-Source-Version", version),
                    ("Last-Modified", formatdate(st.st_mtime, usegmt=True)),
                ])

        def _image_list(self, path, limit):
            tag, body, gz = files.image_list(path, limit, gone=self._client_gone)
            if self._not_modified(tag):
                return
            headers = [("Content-Type", "application/json; charset=utf-8"), ("ETag", tag), ("Vary", "Accept-Encoding")]
            if "gzip" in (self.headers.get("Accept-Encoding") or ""):
                body = gz
                headers.append(("Content-Encoding", "gzip"))
            self._send_bytes(body, headers)

        def _image(self, query):
            """缩略图、预览、无损原图和瓦片（参数与返回见 imaging.py 和 docs/api.md）。"""
            if imaging is None:
                raise JobError(501, "Agent 缺少 imaging.py，不能生成图片")
            kind = query.get("kind") or "thumb"
            if kind not in IMAGE_KINDS:
                raise ValueError("kind 只能是 thumb、preview、full 或 tile")
            params = {}
            if kind == "preview":
                params["size"] = imaging.bucket(int(query["size"]) if query.get("size") else None)
            elif kind == "tile":
                try:
                    params = {k: int(query[k]) for k in ("l", "x", "y")}
                except (KeyError, ValueError):
                    raise ValueError("瓦片需要整数参数 l、x、y")
                if min(params.values()) < 0:
                    raise ValueError("瓦片参数不能为负数")
            real, f, st = files.open_file(query.get("path") or "")
            with f:
                version = source_version(st)
                fmt = imaging.sniff(f)
                headers = [
                    ("X-Source-Version", version),
                    ("X-Image-Format", fmt),
                    ("X-Image-Native", str(int(imaging.is_native(fmt, st.st_size)))),
                ]
                if kind == "full" and imaging.is_native(fmt, st.st_size):
                    # 浏览器能直接显示的原图原样发送，不需要 Pillow
                    tag = imaging.etag(version, "raw")
                    if self._not_modified(tag, version):
                        return
                    headers += [("Content-Type", imaging.CONTENT_TYPES[fmt]), ("ETag", tag), ("X-Lossless", "1")]
                    if images is not None:
                        try:
                            info = imaging.probe(real, 1 << 62)
                            headers += [("X-Image-Width", str(info.w)), ("X-Image-Height", str(info.h))]
                        except Exception:
                            pass
                    self._send_file(f, 0, st.st_size, headers)
                    return
            tag = imaging.etag(version, kind, **params)
            if self._not_modified(tag, version):
                return
            if images is None:
                raise JobError(501, "节点没有可用的 Pillow（>=7.0），不能生成缩略图")
            key_base = "%s\0%d\0%d" % (real, st.st_size, st.st_mtime_ns)
            try:
                res = images.render(real, key_base, kind, params, gone=self._client_gone)
            except imaging.Gone:
                self.close_connection = True
                return
            except imaging.Busy as exc:
                self._send(503, {"error": str(exc)}, [("Retry-After", str(exc.retry_after))])
                return
            except (imaging.Unsupported, imaging.TooLarge, imaging.BadImage) as exc:
                self._send(next(c for t, c in image_errors if isinstance(exc, t)), {"error": str(exc)})
                return
            except (ValueError, ConnectionError, socket.timeout):
                raise
            except Exception as exc:
                log.exception("生成图片失败: %s %s", real, kind)
                self._send(500, {"error": "生成图片失败: {}".format(exc)})
                return
            with res:
                hdr = res.hdr
                headers += [
                    ("Content-Type", hdr["ct"]),
                    ("ETag", tag),
                    ("X-Image-Width", str(hdr["w"])),
                    ("X-Image-Height", str(hdr["h"])),
                    ("X-Lossless", str(int(hdr["lossless"]))),
                    ("X-Cache", res.cache),
                    ("X-Gen-Ms", "%.1f" % res.ms),
                ]
                if hdr.get("normalized"):
                    headers.append(("X-Normalized", "1"))
                if kind == "tile":
                    headers.append(("X-Tile-Size", str(imaging.TILE)))
                if res.file is not None:
                    self._send_file(res.file, res.offset, res.length, headers)
                else:
                    self._send_bytes(res.data, headers)

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

    return Handler


def build_server(host, port, token, disk_paths, data_dir, allow_roots=None, image_opts=None):
    """image_opts 为 imaging.Service 的参数，缺省时取 GNM_AGENT_* 环境变量。"""
    collector = Collector(disk_paths)
    jobs = JobManager(data_dir, collector.accelerator)
    files = FileStore(allow_roots or [])
    images = None
    if imaging is not None and imaging.Image is not None:
        images = imaging.Service(**(image_opts or {}))
    return ThreadingHTTPServer((host, port), make_handler(collector, jobs, files, token, images))


def main():
    parser = argparse.ArgumentParser(description="GPU/NPU 节点 Agent")
    parser.add_argument("--host", default=os.environ.get("GNM_AGENT_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("GNM_AGENT_PORT", "9100")))
    parser.add_argument("--token", default=os.environ.get("GNM_AGENT_TOKEN", ""))
    parser.add_argument(
        "--disk",
        action="append",
        default=None,
        help="需要上报使用率的磁盘挂载点，可重复，默认 /",
    )
    parser.add_argument(
        "--data-dir",
        default=os.environ.get("GNM_AGENT_DATA_DIR", os.path.expanduser("~/.gnm-agent")),
        help="任务记录和日志的存放目录，默认 ~/.gnm-agent",
    )
    parser.add_argument(
        "--allow-root",
        action="append",
        default=None,
        help="只允许中心服务读取这些目录，可重复，默认不限制（受运行用户的文件权限约束）；"
        "也可用环境变量 GNM_AGENT_ALLOW_ROOTS 设置，多个用冒号分隔",
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    if not args.token:
        log.warning("未设置 token，任何人都可以访问本 Agent")
    allow_roots = args.allow_root
    if not allow_roots and os.environ.get("GNM_AGENT_ALLOW_ROOTS"):
        allow_roots = [r for r in os.environ["GNM_AGENT_ALLOW_ROOTS"].split(os.pathsep) if r]
    server = build_server(args.host, args.port, args.token, args.disk or ["/"], args.data_dir, allow_roots)
    log.info("gnm-agent %s 监听 %s:%s", VERSION, args.host, args.port)
    if imaging is None:
        log.warning("缺少 imaging.py，不能生成缩略图")
    elif imaging.Image is None:
        log.info("没有可用的 Pillow（>=7.0），缩略图由中心服务生成")
    server.serve_forever()


if __name__ == "__main__":
    main()

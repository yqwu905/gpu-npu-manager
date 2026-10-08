#!/usr/bin/env python3
"""GPU/NPU 节点 Agent。

部署在每台服务器上，只依赖 Python 标准库（3.7+），通过 HTTP 向中心服务提供：
- GET /v1/health   存活检查
- GET /v1/status   主机概况与所有加速卡状态

所有请求需带请求头 X-Agent-Token，与启动参数 --token（或环境变量 GNM_AGENT_TOKEN）一致。
"""
import argparse
import json
import logging
import os
import pwd
import re
import shutil
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn

AGENT_VERSION = "0.1.0"
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


def collect_npu(smi):
    return parse_npu_smi_info(_run([smi, "info"]))


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
            "agent_version": AGENT_VERSION,
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
# HTTP 服务
# ---------------------------------------------------------------------------


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


def make_handler(collector, token):
    class Handler(BaseHTTPRequestHandler):
        server_version = "gnm-agent/" + AGENT_VERSION

        def log_message(self, fmt, *args):
            log.debug("%s - %s", self.address_string(), fmt % args)

        def _send(self, code, payload):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if token and self.headers.get("X-Agent-Token") != token:
                self._send(401, {"error": "invalid token"})
                return
            path = self.path.split("?", 1)[0]
            if path == "/v1/health":
                self._send(200, {"ok": True, "agent_version": AGENT_VERSION})
            elif path == "/v1/status":
                self._send(200, collector.status())
            else:
                self._send(404, {"error": "not found"})

    return Handler


def build_server(host, port, token, disk_paths):
    collector = Collector(disk_paths)
    return ThreadingHTTPServer((host, port), make_handler(collector, token))


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
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    if not args.token:
        log.warning("未设置 token，任何人都可以访问本 Agent")
    server = build_server(args.host, args.port, args.token, args.disk or ["/"])
    log.info("gnm-agent %s 监听 %s:%s", AGENT_VERSION, args.host, args.port)
    server.serve_forever()


if __name__ == "__main__":
    main()

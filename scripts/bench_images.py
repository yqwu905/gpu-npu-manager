"""对比页图片接口的基准测试，默认不随 pytest 运行。

    python scripts/bench_images.py --data DIR [--quick] [--out result.json]
        用 scripts/bench_stack.py 依次启动两套环境（Agent 有 Pillow、GNM_AGENT_NO_PILLOW=1 由中心服务生成）分别测量；
        DIR 由 scripts/make_compare_dataset.py 生成
    python scripts/bench_images.py --url http://host:8000 --ids 3,4 [--ssh-baseline user@node:/path/to/big.png]
        测已经在运行的服务（如经 SSH 转发的真实节点上的结果集），基线为 ssh 节点 cat 文件的速度

输出各项的 p50 / p95 / p99（毫秒）、Server-Timing 的分解（agent / gen 的平均值）、与性能目标的对比，
本地环境另测中断请求后 Agent 停止发送的时间、模拟用户大量中断时的生成次数、并发原图流时中心服务的 RSS。
"""

import argparse
import asyncio
import json
import os
import random
import subprocess
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bench_stack import Stack  # noqa: E402

# 性能目标（4 核，本机或局域网；4K PNG 约 16–20 MB，8K 约 64 MB）：项目 -> (统计量, 上限, 单位)
TARGETS = {
    "list_warm": ("p50", 150, "ms"),
    "list_cold": ("p50", 1500, "ms"),
    "list_gzip_kb": ("max", 400, "KB"),
    "thumb_warm_c6": ("p95", 25, "ms"),
    "thumb_warm_c16": ("p95", 25, "ms"),
    "thumb_cold_c6": ("p50", 350, "ms"),
    "thumb_cold_8k": ("p50", 1200, "ms"),
    "preview_warm": ("p95", 60, "ms"),
    "preview_cold": ("p50", 400, "ms"),
    "preview_cold_8k": ("p50", 1300, "ms"),
    "tile_warm": ("p95", 40, "ms"),
    "tile_first_8k": ("p50", 1300, "ms"),
    "full_ttfb": ("p50", 100, "ms"),
    "abort_stop_ms": ("max", 200, "ms"),
    "streams_rss_growth_mb": ("max", 100, "MB"),
    "renders_over_budget": ("max", 0, ""),
}


def pct(values: list[float]) -> dict:
    if not values:
        return {"n": 0}
    s = sorted(values)
    at = lambda q: s[min(len(s) - 1, int(round(q * (len(s) - 1))))]  # noqa: E731
    return {"n": len(s), "p50": at(0.5), "p95": at(0.95), "p99": at(0.99), "max": s[-1], "mean": sum(s) / len(s)}


def server_timing(resp: httpx.Response) -> dict:
    out = {}
    for item in resp.headers.get("Server-Timing", "").split(","):
        name, *fields = item.strip().split(";")
        for f in fields:
            k, _, val = f.partition("=")
            if k == "dur":
                out[name] = float(val)
            elif k == "desc":
                out[name + "_desc"] = val.strip('"')
    return out


def proc_status(pid: int | None) -> dict:
    """VmRSS / VmHWM（MB）。"""
    if not pid:
        return {}
    try:
        lines = Path(f"/proc/{pid}/status").read_text().splitlines()
    except OSError:
        return {}
    return {l.split(":")[0]: int(l.split()[1]) / 1024 for l in lines if l.startswith(("VmRSS", "VmHWM"))}


def proc_wchar(pid: int) -> int:
    for line in Path(f"/proc/{pid}/io").read_text().splitlines():
        if line.startswith("wchar:"):
            return int(line.split()[1])
    return 0


def count_bins(*dirs) -> int:
    return sum(1 for d in dirs if d for _, _, names in os.walk(d) for n in names if n.endswith(".bin"))


class Bench:
    def __init__(self, url: str, ids: list[int], info: dict | None = None, quick: bool = False):
        self.url, self.ids, self.info, self.quick = url, ids, info or {}, quick
        self.results: dict = {}
        self.used: set = set()

    def record(self, name: str, values: list[float], timings: list[dict] = (), **extra) -> None:
        entry = pct(values)
        for key in ("agent", "gen"):
            durs = [t[key] for t in timings if key in t]
            if durs:
                entry[f"{key}_mean"] = sum(durs) / len(durs)
        gens = {t.get("gen_desc") for t in timings if t.get("gen_desc")}
        if gens:
            entry["gen_by"] = sorted(gens)
        entry.update(extra)
        self.results[name] = entry

    def url_for(self, rid: int, entry: list, kind: str, **params) -> str:
        path, _, v = entry
        q = httpx.QueryParams({"path": path, **({"v": v} if v else {}), "kind": kind, **params})
        return f"/api/results/{rid}/image?{q}"

    def pick(self, pool: list, n: int) -> list:
        """从 (结果集, 条目) 中均匀挑 n 个没用过的（冷）。"""
        fresh = [item for item in pool if (item[0], item[1][0]) not in self.used]
        step = max(1, len(fresh) // max(1, n))
        chosen = fresh[::step][:n]
        self.used.update((rid, e[0]) for rid, e in chosen)
        return chosen

    async def fetch_all(self, client: httpx.AsyncClient, urls: list[str], concurrency: int, dims=None) -> tuple[list, list]:
        """并发请求，返回 (耗时列表, Server-Timing 列表)；dims 为字典时记下每个 URL 的原图尺寸。"""
        sem = asyncio.Semaphore(concurrency)
        times, timings = [], []

        async def one(url):
            async with sem:
                t = time.perf_counter()
                resp = await client.get(url)
                if resp.status_code != 200:
                    raise RuntimeError(f"{url}: {resp.status_code} {resp.text[:200]}")
                times.append((time.perf_counter() - t) * 1000)
                timings.append(server_timing(resp))
                if dims is not None:
                    dims[url] = int(resp.headers["X-Image-Width"]), int(resp.headers["X-Image-Height"])

        await asyncio.gather(*(one(u) for u in urls))
        return times, timings

    async def run(self) -> dict:
        n = 12 if self.quick else 48
        limits = httpx.Limits(max_connections=64, max_keepalive_connections=64)
        async with httpx.AsyncClient(base_url=self.url, timeout=120, limits=limits) as client:
            # 图片列表：冷（Agent 第一次读）与热
            lists, cold, sizes = {}, [], []
            for rid in self.ids:
                t = time.perf_counter()
                resp = await client.get(f"/api/results/{rid}/images")
                resp.raise_for_status()
                cold.append((time.perf_counter() - t) * 1000)
                sizes.append(resp.num_bytes_downloaded / 1024)
                lists[rid] = resp.json()["files"]
            self.record("list_cold", cold, total=[len(v) for v in lists.values()])
            self.record("list_gzip_kb", sizes)
            warm = []
            for _ in range(3 if self.quick else 10):
                for rid in self.ids:
                    t = time.perf_counter()
                    (await client.get(f"/api/results/{rid}/images")).raise_for_status()
                    warm.append((time.perf_counter() - t) * 1000)
            self.record("list_warm", warm)

            entries = [(rid, e) for rid, files in lists.items() for e in files if e[1] is None or e[1] > 0]
            by_size = sorted((e for e in entries if e[1]), key=lambda x: -x[1][1])
            big_cut = by_size[0][1][1] * 0.6 if by_size else 0
            bigs = [e for e in by_size if e[1][1] >= big_cut and big_cut > 32 << 20]
            regular = [e for e in entries if e not in bigs and not e[1][0].endswith(".tif")]
            random.Random(0).shuffle(regular)

            # 缩略图：冷 / 热，并发 6 和 16；8K 冷图
            for c in (6, 16):
                urls = [self.url_for(rid, e, "thumb") for rid, e in self.pick(regular, n)]
                self.record(f"thumb_cold_c{c}", *await self.fetch_all(client, urls, c))
                self.record(f"thumb_warm_c{c}", *await self.fetch_all(client, urls * 3, c))
            if bigs:
                urls = [self.url_for(rid, e, "thumb") for rid, e in self.pick(bigs, 3 if self.quick else 6)]
                self.record("thumb_cold_8k", *await self.fetch_all(client, urls, 1))

            # 预览 2048：冷（逐个请求）/ 热
            urls = [self.url_for(rid, e, "preview", size=2048) for rid, e in self.pick(regular, n // 4)]
            self.record("preview_cold", *await self.fetch_all(client, urls, 1))
            self.record("preview_warm", *await self.fetch_all(client, urls * 3, 1))
            if bigs:
                urls = [self.url_for(rid, e, "preview", size=2048) for rid, e in self.pick(bigs, 3 if self.quick else 6)]
                self.record("preview_cold_8k", *await self.fetch_all(client, urls, 1))

            # 瓦片：第一块要整图解码，之后的从已解码的整图裁
            for name, pool in (("", regular), ("_8k", bigs)):
                chosen = self.pick(pool, 3 if self.quick else 6)
                if not chosen:
                    continue
                first, dims = [self.url_for(rid, e, "tile", l=0, x=0, y=0) for rid, e in chosen], {}
                self.record(f"tile_first{name}", *await self.fetch_all(client, first, 1, dims))
                # 同一张图的其他瓦片（第 0 层前两行的前 4 列，不超出范围）
                rest = [self.url_for(rid, e, "tile", l=0, x=x, y=y) for (rid, e), url in zip(chosen, first)
                        for x in range(min(4, -(-dims[url][0] // 512))) for y in range(min(2, -(-dims[url][1] // 512)))
                        if x or y]
                self.record(f"tile_warm{name}", *await self.fetch_all(client, rest, 4))

            # 原图：首字节时间与吞吐
            ttfb, rates = [], []
            for rid, e in (bigs or by_size)[:2 if self.quick else 4]:
                t = time.perf_counter()
                async with client.stream("GET", self.url_for(rid, e, "full")) as resp:
                    first, total = None, 0
                    async for chunk in resp.aiter_raw():
                        first = first or time.perf_counter()
                        total += len(chunk)
                ttfb.append((first - t) * 1000)
                rates.append(total / (1 << 20) / (time.perf_counter() - t))
            self.record("full_ttfb", ttfb, mb_per_s=pct(rates))

            if self.info.get("agent_pid"):
                await self.local_only(client, regular, by_size)
        self.results["rss"] = {"central": proc_status(self.info.get("server_pid")), "agent": proc_status(self.info.get("agent_pid"))}
        return self.results

    async def local_only(self, client: httpx.AsyncClient, regular: list, by_size: list) -> None:
        """需要 Agent / 中心服务进程信息的测量（bench_stack 启动的本地环境）。"""
        agent_pid, server_pid = self.info["agent_pid"], self.info["server_pid"]
        # 中断原图请求后 Agent 多久停止发送（sendfile 计入 /proc/<pid>/io 的 wchar）
        stops = []
        for rid, e in by_size[:3]:
            async with client.stream("GET", self.url_for(rid, e, "full")) as resp:
                async for _ in resp.aiter_raw():
                    break
            aborted = time.perf_counter()
            last, last_change = proc_wchar(agent_pid), aborted
            while time.perf_counter() - last_change < 0.3 and time.perf_counter() - aborted < 5:
                await asyncio.sleep(0.005)
                now = proc_wchar(agent_pid)
                if now != last:
                    last, last_change = now, time.perf_counter()
            stops.append((last_change - aborted) * 1000)
        self.record("abort_stop_ms", stops)

        # 6 个用户各每秒 60 个缩略图，一半在 5 ms 内中断：生成次数不应超过没中断的请求数 + 槽位数
        # 只数生成的一方的磁盘缓存（中心服务的 L2 也会存一份 Agent 生成的派生图）
        caches = (self.info["central_cache"] if self.info.get("no_pillow") else self.info["agent_cache"],)
        before = count_bins(*caches)
        pool = self.pick(regular, 6 * 60 * (1 if self.quick else 3))
        completed = 0

        async def user(items):
            nonlocal completed
            for rid, e in items:
                started = time.perf_counter()
                abort = random.random() < 0.5
                try:
                    resp = await asyncio.wait_for(client.get(self.url_for(rid, e, "thumb")), 0.005 if abort else 60)
                    completed += resp.status_code == 200
                except asyncio.TimeoutError:
                    pass
                await asyncio.sleep(max(0.0, 1 / 60 - (time.perf_counter() - started)))

        await asyncio.gather(*(user(pool[i::6]) for i in range(6)))
        await asyncio.sleep(2)  # 等已经开始的生成写完缓存
        renders = count_bins(*caches) - before
        workers = int(os.environ.get("GNM_AGENT_IMAGE_WORKERS") or max(2, min(6, (os.cpu_count() or 1) // 4)))
        self.results["renders_over_budget"] = {"max": max(0, renders - completed - workers), "renders": renders,
                                               "completed": completed, "requests": len(pool), "workers": workers}

        # 20 个并发原图流时中心服务的 RSS 增长
        base = proc_status(server_pid).get("VmRSS", 0)
        peak = base
        files = (by_size * 20)[:20]

        async def stream(rid, e):
            async with client.stream("GET", self.url_for(rid, e, "full")) as resp:
                async for _ in resp.aiter_raw():
                    pass

        task = asyncio.gather(*(stream(rid, e) for rid, e in files))
        while not task.done():
            peak = max(peak, proc_status(server_pid).get("VmRSS", 0))
            await asyncio.sleep(0.05)
        await task
        self.results["streams_rss_growth_mb"] = {"max": peak - base, "before": base, "peak": peak}


def ssh_baseline(target: str) -> dict:
    """ssh 节点 cat 文件的速度（MB/s），作为原图吞吐的基线。"""
    host, _, path = target.partition(":")
    size = int(subprocess.run(["ssh", host, "stat", "-c", "%s", path], capture_output=True, text=True, check=True).stdout)
    t = time.perf_counter()
    subprocess.run(["ssh", host, "cat", path], stdout=subprocess.DEVNULL, check=True)
    return {"mb_per_s": size / (1 << 20) / (time.perf_counter() - t), "bytes": size}


def report(title: str, results: dict) -> None:
    print(f"\n== {title}")
    print(f"{'项目':<24}{'n':>5}{'p50':>10}{'p95':>10}{'p99':>10}  目标 / 备注")
    for name, r in results.items():
        if name == "rss":
            continue
        target = TARGETS.get(name)
        verdict = ""
        if target and target[0] in r:
            ok = r[target[0]] <= target[1]
            verdict = f"{target[0]} ≤ {target[1]}{target[2]}：{'达标' if ok else '未达标'}"
        notes = {k: v for k, v in r.items() if k not in ("n", "p50", "p95", "p99", "max", "mean")}
        cells = "".join(f"{r[k]:>10.1f}" if isinstance(r.get(k), (int, float)) else f"{'-':>10}" for k in ("p50", "p95", "p99"))
        print(f"{name:<24}{r.get('n', '-'):>5}{cells}  {verdict} {json.dumps(notes, ensure_ascii=False, default=str) if notes else ''}")
    print("RSS（MB）：", json.dumps(results.get("rss"), ensure_ascii=False))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--data", help="数据集目录：自动启动两套环境")
    parser.add_argument("--url", help="已在运行的中心服务")
    parser.add_argument("--ids", help="结果集 ID，逗号分隔（配合 --url）")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--agent-port", type=int, default=9765)
    parser.add_argument("--quick", action="store_true", help="少量请求，快速跑一遍")
    parser.add_argument("--only", choices=["pillow", "no-pillow"], help="只测一套环境")
    parser.add_argument("--ssh-baseline", help="user@host:/path，ssh cat 该文件作为原图吞吐的基线")
    parser.add_argument("--out", help="结果另存为 JSON")
    args = parser.parse_args()
    if not args.data and not (args.url and args.ids):
        parser.error("需要 --data，或 --url 和 --ids")
    results = {}
    if args.data:
        for mode in ("pillow", "no-pillow"):
            if args.only and args.only != mode:
                continue
            with Stack(args.data, args.port, args.agent_port, no_pillow=mode == "no-pillow") as stack:
                results[mode] = asyncio.run(Bench(stack.url, stack.info["ids"], stack.info, args.quick).run())
            report(f"Agent {'没有' if mode == 'no-pillow' else '有'} Pillow", results[mode])
    else:
        ids = [int(i) for i in args.ids.split(",")]
        results["remote"] = asyncio.run(Bench(args.url.rstrip("/"), ids, quick=args.quick).run())
        report(args.url, results["remote"])
    if args.ssh_baseline:
        results["ssh_baseline"] = ssh_baseline(args.ssh_baseline)
        print("ssh cat 基线：", results["ssh_baseline"])
    if args.out:
        Path(args.out).write_text(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

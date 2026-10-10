"""对比页的图片：图片列表、缩略图 / 预览 / 无损原图 / 瓦片，以及流式读取结果集文件。

派生图优先由节点 Agent 生成（imaging.py + Pillow）。Agent 没有 Pillow 或是旧版本时，中心服务把原图拉到本地暂存（spool），
用同一份 imaging.py 生成，两边的派生图逐字节一致。按版本号生成的派生图缓存在中心服务的磁盘上（L2），
URL 带版本号 v 且与文件当前版本一致时浏览器可永久缓存。约定见 docs/api.md。
"""

import asyncio
import gzip
import hashlib
import importlib.util
import io
import json
import logging
import os
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor

import httpx
from fastapi import HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from starlette.background import BackgroundTask

from .agent_client import AgentClient, AgentError, ClientGone, Slot
from .config import Settings

log = logging.getLogger(__name__)

IMMUTABLE = "private, max-age=31536000, immutable"
NO_CACHE = "private, no-cache"
SHORT = "private, max-age=300"
NOSNIFF = {"X-Content-Type-Options": "nosniff"}
# 列表最多的条数；旧版 Agent 时分页读取样本的每页条数与列表缓存时间（秒）、个数
MAX_LIST = 200000
SAMPLES_PAGE = 5000
COMPAT_TTL = 30
COMPAT_CACHE_SIZE = 16
# 读入内存转发的派生图上限（原图走流式转发）
MAX_DERIVATIVE = 16 << 20
# 原图暂存的有效期（秒，按最近使用）；旧版 Agent 没有版本号，暂存只用 5 分钟
SPOOL_TTL = 1800
SPOOL_UNVERSIONED_TTL = 300
# 最近这么久（秒）用过的暂存原图不因超出容量而删除；先读这么多字节判断是不是图片
SPOOL_GRACE = 120
PEEK_BYTES = 256 << 10
# 单个暂存原图的上限（同时不超过暂存容量）：再大的文件像素数早已超过上限，不必下载完再拒绝
SPOOL_FILE_MAX = 1 << 30
# 每种图片占用的 Agent 请求类别
SLOT_OF = {"thumb": "thumb", "preview": "main", "tile": "main", "full": "stream"}
# Agent 生成的派生图透传给浏览器的响应头
PASS_HEADERS = ("ETag", "X-Image-Width", "X-Image-Height", "X-Image-Format", "X-Image-Native", "X-Lossless",
                "X-Normalized", "X-Tile-Size")
# 浏览器里可能执行脚本的类型：作为附件下载（所有文件都禁止脚本，xhtml、xml 等也可能执行脚本）
ACTIVE_TYPES = ("text/html", "image/svg+xml")

_modules: dict = {}


def load_imaging(package_dir: str):
    """加载 Agent 安装包里的 imaging.py（与节点用同一份代码），失败时返回 None。"""
    path = os.path.join(package_dir, "imaging.py")
    if path not in _modules:
        try:
            spec = importlib.util.spec_from_file_location("gnm_imaging", path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        except Exception:
            log.exception("加载 %s 失败，对比页不能生成图片", path)
            module = None
        _modules[path] = module
    return _modules[path]


def key_base(host: str, port: int, full: str, version: str) -> str:
    """派生图缓存键的源文件部分：某个 Agent 上某个文件的某个版本。"""
    return f"{host}\0{port}\0{full}\0{version}"


def cache_control(v: str | None, version: str) -> str:
    """URL 带的版本号与文件当前版本一致时永久缓存，不一致时每次验证，不带时缓存 5 分钟。"""
    if not v:
        return SHORT
    return IMMUTABLE if v == version else NO_CACHE


def etag_match(if_none_match: str | None, tags) -> str | None:
    """If-None-Match 包含 tags 之一时返回它。"""
    if not if_none_match:
        return None
    given = {t.strip().removeprefix("W/") for t in if_none_match.split(",")}
    return next((t for t in tags if t in given), None)


def server_timing(agent_ms=None, gen_ms=None, gen=None, cache=None) -> str:
    parts = []
    if agent_ms is not None:
        parts.append(f"agent;dur={agent_ms:.1f}")
    if gen is not None:
        parts.append(f'gen;dur={gen_ms or 0:.1f};desc="{gen}"')
    if cache:
        parts.append(f'cache;desc="{cache}"')
    return ", ".join(parts)


def error(status: int, detail: str, retry_after=None) -> HTTPException:
    headers = {"Cache-Control": "no-store", **NOSNIFF}
    if retry_after is not None:
        headers["Retry-After"] = str(retry_after)
    return HTTPException(status, detail, headers=headers)


def agent_error(exc: AgentError) -> HTTPException:
    """Agent 的错误对应到浏览器：403 / 404 为 404，400 / 413 / 415 / 501 原样，503 保留 Retry-After，超时 504，其余 502。"""
    if exc.timeout:
        return error(504, f"读取超时：{exc}")
    if exc.status in (403, 404):
        return error(404, str(exc))
    if exc.status in (400, 413, 415, 501):
        return error(exc.status, str(exc))
    if exc.status == 503:
        return error(503, str(exc), exc.retry_after or 1)
    return error(502, str(exc))


def gone_response() -> Response:
    """浏览器已断开，不再生成，随便回一个响应。"""
    return Response(status_code=499, headers={"Cache-Control": "no-store"})


async def read_body(resp: httpx.Response, limit: int) -> bytes:
    """读取原始响应体（不解压），超过 limit 字节时报错；读完关闭响应。"""
    chunks, size = [], 0
    try:
        async for chunk in resp.aiter_raw():
            size += len(chunk)
            if size > limit:
                raise AgentError(f"Agent 返回的内容超过 {limit >> 20} MB")
            chunks.append(chunk)
    except httpx.HTTPError as exc:
        raise AgentError(f"读取 Agent 响应失败: {type(exc).__name__} {exc}",
                         timeout=isinstance(exc, httpx.TimeoutException)) from exc
    finally:
        await resp.aclose()
    return b"".join(chunks)


def build_list(paths: list[str], skipped: int, source: str) -> tuple[str, bytes, bytes]:
    """去重、按 (文件名, 路径) 排序、截断，返回 (ETag, JSON, gzip 压缩的 JSON)；大小未知为 null，版本号为空。"""
    seen: set = set()
    paths = [p for p in paths if not (p in seen or seen.add(p))]
    paths.sort(key=lambda p: (p[max(p.rfind("/"), p.rfind("\\")) + 1 :], p))
    files = [[p, None, ""] for p in paths[:MAX_LIST]]
    body = json.dumps(
        {"total": len(files), "missing": 0, "skipped": skipped, "truncated": len(paths) > MAX_LIST,
         "source": source, "files": files},
        ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8", "surrogateescape")
    return f'"{hashlib.sha1(body).hexdigest()[:16]}"', body, gzip.compress(body, 5)


async def _chain(first: bytes, rest):
    if first:
        yield first
    async for chunk in rest:
        yield chunk


def _file_chunks(f, offset: int, length: int):
    with f:
        f.seek(offset)
        while length > 0:
            chunk = f.read(min(1 << 20, length))
            if not chunk:
                break
            length -= len(chunk)
            yield chunk


async def _disconnected(request: Request) -> None:
    """等到浏览器断开（ASGI 的 http.disconnect 消息）。"""
    while (await request.receive())["type"] != "http.disconnect":
        pass


async def _watch(request: Request, event: threading.Event) -> None:
    """浏览器断开时设置 event，生成线程里的 gone() 据此放弃还没开始解码的请求。"""
    while not event.is_set():
        if await request.is_disconnected():
            event.set()
            return
        await asyncio.sleep(0.5)


class ProxyStream(StreamingResponse):
    """把 Agent 的响应体原样转发给浏览器，中心服务不缓冲；结束或浏览器断开时关闭上游连接、释放请求名额。"""

    def __init__(self, upstream: httpx.Response, slot: Slot | None = None, body=None, status_code: int = 200,
                 headers: dict | None = None):
        super().__init__(upstream.aiter_raw() if body is None else body, status_code, headers)
        self.upstream = upstream
        self.slot = slot

    async def __call__(self, scope, receive, send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            await self.upstream.aclose()
            if self.slot is not None:
                self.slot.release()


class MediaService:
    """对比页图片请求的处理，挂在 app.state.media。"""

    def __init__(self, settings: Settings, agent: AgentClient):
        self.settings = settings
        self.agent = agent
        self.imaging = im = load_imaging(settings.agent_package_dir)
        if im is not None and im.Image is not None:
            # imaging 加载时按 GNM_AGENT_MAX_PIXELS 设置了 Pillow 的全局上限，中心服务以 GNM_IMAGE_MAX_PIXELS 为准
            im.Image.MAX_IMAGE_PIXELS = settings.image_max_pixels
        # 中心服务自己生成派生图的 imaging.Service；它的磁盘缓存同时是 Agent 生成的派生图的 L2 缓存
        self.service = None if im is None else im.Service(
            cache_dir=settings.image_cache_dir, cache_mb=settings.image_cache_mb, workers=settings.image_workers,
            mem_mb=settings.image_mem_mb, decoded_mb=settings.image_decoded_mb, max_pixels=settings.image_max_pixels,
        )
        self.cache = None if self.service is None else self.service.disk
        self.spool_dir = os.path.join(settings.image_cache_dir, "spool")
        self._pool: ThreadPoolExecutor | None = None
        self._flights: dict = {}
        self._lists: OrderedDict = OrderedDict()

    def _executor(self) -> ThreadPoolExecutor:
        if self._pool is None:
            # 生成任务在 imaging.Service 里按 CPU 槽位和内存预算排队（能感知浏览器断开，排队太久返回 503），线程比槽位多
            self._pool = ThreadPoolExecutor(max(4, self.settings.image_workers * 4), thread_name_prefix="gnm-image")
        return self._pool

    def close(self) -> None:
        if self._pool is not None:
            self._pool.shutdown(wait=False, cancel_futures=True)
            self._pool = None

    def params(self, kind: str, size: int | None, l: int | None, x: int | None, y: int | None) -> dict:
        """规范化参数：缩略图没有参数，预览尺寸取档位，瓦片必须有 l、x、y。"""
        if kind == "tile":
            if l is None or x is None or y is None:
                raise error(422, "瓦片需要参数 l、x、y")
            return {"l": l, "x": x, "y": y}
        if kind == "preview":
            return {"size": self.imaging.bucket(size) if self.imaging is not None else size or 2048}
        return {}

    @staticmethod
    async def _until_gone(request: Request, coro):
        """等待 coro；浏览器先断开时取消它（关闭到 Agent 的连接，Agent 据此放弃还没开始的生成）并抛出 ClientGone。
        Starlette 不会因为浏览器断开而取消普通接口，所以要自己等断开消息。"""
        task = asyncio.ensure_future(coro)
        watcher = asyncio.ensure_future(_disconnected(request))
        try:
            await asyncio.wait({task, watcher}, return_when=asyncio.FIRST_COMPLETED)
        except BaseException:
            task.cancel()
            raise
        finally:
            watcher.cancel()
        if task.done():
            return task.result()
        task.cancel()
        try:
            result = await task
        except BaseException:
            pass
        else:  # 收到断开消息的同时已经完成
            if isinstance(result, httpx.Response):
                await result.aclose()
        raise ClientGone()

    async def _once(self, key, factory):
        """同一 key 同时只执行一次 factory()，其余请求等待同一结果；发起的请求断开时由等待的请求接着做。"""
        loop = asyncio.get_running_loop()
        while True:
            fut = self._flights.get(key)
            if fut is None or fut.get_loop() is not loop:
                break
            try:
                return await asyncio.shield(fut)
            except (ClientGone, asyncio.CancelledError):
                # 自己被取消时照常抛出；发起的请求被取消或断开时重新来过
                if not fut.done() or not (fut.cancelled() or isinstance(fut.exception(), ClientGone)):
                    raise
        fut = self._flights[key] = loop.create_future()
        try:
            result = await factory()
        except BaseException as exc:
            if isinstance(exc, asyncio.CancelledError):
                fut.cancel()
            else:
                fut.set_exception(exc)
                fut.exception()  # 没有等待者时不报 “exception was never retrieved”
            raise
        else:
            fut.set_result(result)
            return result
        finally:
            if self._flights.get(key) is fut:
                del self._flights[key]

    # ------------------------------------------------------------------
    # 图片列表
    # ------------------------------------------------------------------

    async def image_list(self, request: Request, host: str, port: int, base: str) -> Response:
        t0 = time.perf_counter()
        inm = request.headers.get("If-None-Match")
        gzip_ok = "gzip" in request.headers.get("Accept-Encoding", "")
        try:
            features = await self.agent.features(host, port)
            slot = await self.agent.slot(host, port, "list", request.is_disconnected)
            try:
                if features.get("list"):
                    headers = {"Accept-Encoding": "gzip", **({"If-None-Match": inm} if inm else {})}
                    resp = await self._until_gone(request, self.agent.open_stream(
                        host, port, "/v1/files/images", {"path": base}, headers, self.settings.image_list_timeout
                    ))
                    etag, status = resp.headers.get("ETag"), resp.status_code
                    encoded = resp.headers.get("Content-Encoding") == "gzip"
                    body = await self._until_gone(request, read_body(resp, 256 << 20))
                else:
                    etag, plain, packed = await self._compat_list(host, port, base)
                    status = 304 if etag_match(inm, [etag]) else 200
                    body, encoded = (packed, True) if gzip_ok else (plain, False)
            finally:
                slot.release()
        except ClientGone:
            return gone_response()
        except AgentError as exc:
            if exc.timeout:
                raise error(504, f"读取图片列表超时：{exc}")
            if exc.status in (403, 404):
                raise error(422, f"读取图片列表失败：{exc}")
            if exc.status == 503:
                raise error(503, str(exc), exc.retry_after or 1)
            raise error(502, f"读取图片列表失败：{exc}")
        headers = {"Cache-Control": NO_CACHE, "Vary": "Accept-Encoding", **NOSNIFF,
                   "Server-Timing": server_timing((time.perf_counter() - t0) * 1000)}
        if etag:
            headers["ETag"] = etag
        if status == 304:
            return Response(status_code=304, headers=headers)
        if encoded and not gzip_ok:
            body, encoded = await asyncio.to_thread(gzip.decompress, body), False
        if encoded:
            headers["Content-Encoding"] = "gzip"
        return Response(body, media_type="application/json; charset=utf-8", headers=headers)

    async def _compat_list(self, host: str, port: int, base: str) -> tuple[str, bytes, bytes]:
        """旧版 Agent：分页读取样本拼出列表（没有大小和版本号），缓存 30 秒。"""
        key = (host, port, base)
        cached = self._lists.get(key)
        if cached is not None and time.monotonic() - cached[0] < COMPAT_TTL:
            self._lists.move_to_end(key)
            return cached[1]
        return await self._once(("list", *key), lambda: self._build_compat(host, port, base))

    async def _build_compat(self, host: str, port: int, base: str) -> tuple[str, bytes, bytes]:
        key = (host, port, base)
        paths, skipped, total = [], 0, None
        offset = 0
        while total is None or offset < total:
            page = await self.agent.read_samples(host, port, base, offset, SAMPLES_PAGE)
            total = page["total"]
            if not page["items"]:
                break
            for record in page["items"]:
                image = record.get("image") if isinstance(record, dict) else None
                if isinstance(image, str):
                    paths.append(image)
                else:
                    skipped += 1
            offset += len(page["items"])
        entry = await asyncio.to_thread(build_list, paths, skipped, "samples")
        self._lists[key] = (time.monotonic(), entry)
        self._lists.move_to_end(key)
        while len(self._lists) > COMPAT_CACHE_SIZE:
            self._lists.popitem(last=False)
        return entry

    # ------------------------------------------------------------------
    # 文件
    # ------------------------------------------------------------------

    async def file(self, request: Request, host: str, port: int, full: str, v: str | None) -> Response:
        """流式转发原文件，带 ETag / Last-Modified，If-None-Match 交给 Agent 判断。"""
        t0 = time.perf_counter()
        inm = request.headers.get("If-None-Match")
        if v and etag_match(inm, [f'"{v}"']):
            return Response(status_code=304, headers={
                "ETag": f'"{v}"', "Cache-Control": IMMUTABLE, **NOSNIFF, "Server-Timing": server_timing(cache="hit"),
            })
        try:
            slot = await self.agent.slot(host, port, "stream", request.is_disconnected)
            try:
                if await request.is_disconnected():
                    raise ClientGone()
                resp = await self.agent.open_stream(
                    host, port, "/v1/files/raw", {"path": full}, {"If-None-Match": inm} if inm else None
                )
            except BaseException:
                slot.release()
                raise
        except ClientGone:
            return gone_response()
        except AgentError as exc:
            raise agent_error(exc)
        h = resp.headers
        headers = {"Cache-Control": cache_control(v, h.get("X-Source-Version", "")), **NOSNIFF,
                   "Server-Timing": server_timing((time.perf_counter() - t0) * 1000)}
        for name in ("ETag", "Last-Modified"):
            if name in h:
                headers[name] = h[name]
        if resp.status_code == 304:
            await resp.aclose()
            slot.release()
            return Response(status_code=304, headers=headers)
        content_type = h.get("Content-Type", "application/octet-stream")
        headers["Content-Type"] = content_type
        if "Content-Length" in h:
            headers["Content-Length"] = h["Content-Length"]
        headers["Content-Security-Policy"] = "sandbox"  # 不影响 <img> 显示
        if content_type.split(";")[0].strip().lower() in ACTIVE_TYPES:
            headers["Content-Disposition"] = "attachment"
        return ProxyStream(resp, slot, headers=headers)

    # ------------------------------------------------------------------
    # 缩略图、预览、原图、瓦片
    # ------------------------------------------------------------------

    async def image(self, request: Request, host: str, port: int, base: str, full: str, v: str | None, kind: str,
                    params: dict) -> Response:
        im = self.imaging
        if im is None:
            raise error(501, "中心服务缺少 imaging.py，无法生成预览")
        t0 = time.perf_counter()
        inm = request.headers.get("If-None-Match")
        if v:
            # 浏览器手里就是这个版本：不用问 Agent
            tags = [im.etag(v, kind, **params)] + ([im.etag(v, "raw")] if kind == "full" else [])
            tag = etag_match(inm, tags)
            if tag:
                return Response(status_code=304, headers={
                    "ETag": tag, "Cache-Control": IMMUTABLE, **NOSNIFF, "Server-Timing": server_timing(cache="hit"),
                })
        try:
            if await request.is_disconnected():
                raise ClientGone()
            if v and self.cache is not None:
                hit = await asyncio.to_thread(self._l2_get, key_base(host, port, full, v), kind, params)
                if hit is not None:
                    return self._respond(hit[0], hit[1], v, v, kind, params, server_timing(cache="l2"))
            features = await self.agent.features(host, port)
            if features.get("image") or (kind == "full" and features.get("stream")):
                try:
                    return await self._from_agent(request, host, port, base, full, v, kind, params, inm, t0)
                except AgentError as exc:
                    if exc.status != 501:
                        raise
            elif kind == "full":
                # 旧版 Agent：浏览器能直接显示的原图直接转发，其余接着这次读取写入暂存，由中心服务转码
                got = await self._peek_native(request, host, port, full, v, t0)
                if isinstance(got, Response):
                    return got
                return await self._central(request, host, port, full, v, kind, params, got)
            return await self._central(request, host, port, full, v, kind, params)
        except ClientGone:
            return gone_response()
        except AgentError as exc:
            raise agent_error(exc)

    def _l2_get(self, kb: str, kind: str, params: dict):
        """L2 命中时返回 (头信息, 内容)，大的内容为 (文件, 偏移, 长度)，在线程中调用。"""
        got = self.cache.open(self.cache.key(kb, kind, self.imaging.code(kind, **params)))
        if got is None:
            return None
        hdr, f, offset, length = got
        if length > MAX_DERIVATIVE:
            return hdr, (f, offset, length)
        with f:
            f.seek(offset)
            return hdr, f.read(length)

    def _respond(self, hdr: dict, body, version: str, v: str | None, kind: str, params: dict, timing: str,
                 background=None) -> Response:
        """由 imaging 的头信息（L2 缓存、中心服务生成）构造响应。body 为内容或 (文件, 偏移, 长度)。"""
        im = self.imaging
        headers = {
            "Content-Type": hdr["ct"],
            "Cache-Control": cache_control(v, version),
            "X-Image-Width": str(hdr["w"]),
            "X-Image-Height": str(hdr["h"]),
            "X-Image-Format": hdr["fmt"],
            "X-Image-Native": str(int(hdr["native"])),
            "X-Lossless": str(int(hdr["lossless"])),
            **NOSNIFF,
            "Server-Timing": timing,
        }
        if version:
            # 原图原样返回的 full 用原图的 ETag
            headers["ETag"] = im.etag(version, "raw" if kind == "full" and hdr["native"] else kind, **params)
        if hdr.get("normalized"):
            headers["X-Normalized"] = "1"
        if kind == "tile":
            headers["X-Tile-Size"] = str(im.TILE)
        if isinstance(body, tuple):
            headers["Content-Length"] = str(body[2])
            return StreamingResponse(_file_chunks(*body), headers=headers)
        return Response(body, headers=headers, background=background)

    async def _from_agent(self, request: Request, host: str, port: int, base: str, full: str, v: str | None,
                          kind: str, params: dict, inm: str | None, t0: float) -> Response:
        slot = await self.agent.slot(host, port, SLOT_OF[kind], request.is_disconnected)
        try:
            if await request.is_disconnected():
                raise ClientGone()
            resp = await self._until_gone(request, self.agent.open_stream(
                host, port, "/v1/files/image", {"path": full, "kind": kind, **params, "ctx": base},
                {"If-None-Match": inm} if inm else None,
            ))
            h = resp.headers
            version = h.get("X-Source-Version", "")
            headers = {"Cache-Control": cache_control(v, version), **NOSNIFF}
            headers.update((name, h[name]) for name in PASS_HEADERS if name in h)
            if resp.status_code == 304:
                await resp.aclose()
                headers["Server-Timing"] = server_timing((time.perf_counter() - t0) * 1000)
                return Response(status_code=304, headers=headers)
            headers["Content-Type"] = h.get("Content-Type", "application/octet-stream")
            gen_ms = float(h.get("X-Gen-Ms") or 0)
            if kind == "full" or int(h.get("Content-Length") or 0) > MAX_DERIVATIVE:
                # 原图和很大的派生图（如 3072 档的无损预览）流式转发，不进 L2（Agent 有自己的磁盘缓存）
                if "Content-Length" in h:
                    headers["Content-Length"] = h["Content-Length"]
                headers["Server-Timing"] = server_timing((time.perf_counter() - t0) * 1000, gen_ms, "agent", h.get("X-Cache"))
                stream, slot = ProxyStream(resp, slot, headers=headers), None
                return stream
            data = await self._until_gone(request, read_body(resp, MAX_DERIVATIVE))
        finally:
            if slot is not None:
                slot.release()
        headers["Server-Timing"] = server_timing((time.perf_counter() - t0) * 1000, gen_ms, "agent", h.get("X-Cache"))
        background = None
        if v and version == v and self.cache is not None:
            # 与 URL 的版本一致才写入 L2，之后同一版本的请求不再访问 Agent
            try:
                hdr = {"ct": headers["Content-Type"], "w": int(h["X-Image-Width"]), "h": int(h["X-Image-Height"]),
                       "fmt": h["X-Image-Format"], "native": int(h["X-Image-Native"]), "lossless": int(h["X-Lossless"]),
                       "normalized": int(h.get("X-Normalized") or 0), "ms": gen_ms}
                key = self.cache.key(key_base(host, port, full, v), kind, self.imaging.code(kind, **params))
                background = BackgroundTask(self.cache.put, key, hdr, data)
            except (KeyError, ValueError):
                pass
        return Response(data, headers=headers, background=background)

    async def _peek_native(self, request: Request, host: str, port: int, full: str, v: str | None,
                           t0: float) -> Response | tuple:
        """旧版 Agent 的 full：读原图的第一块判断格式，浏览器能直接显示的原样转发；否则返回读到一半的
        (名额, 响应, 第一块, 其余内容)，由 _spool 接着写入暂存，不必再拉取一次。"""
        im = self.imaging
        slot = await self.agent.slot(host, port, "stream", request.is_disconnected)
        resp = None
        try:
            if await request.is_disconnected():
                raise ClientGone()
            resp = await self.agent.open_stream(host, port, "/v1/files/raw", {"path": full})
            rest = resp.aiter_raw()
            try:
                first = await anext(rest, b"")
            except httpx.HTTPError as exc:
                raise AgentError(f"读取 Agent 响应失败: {type(exc).__name__} {exc}",
                                 timeout=isinstance(exc, httpx.TimeoutException)) from exc
            h = resp.headers
            size = int(h["Content-Length"]) if h.get("Content-Length", "").isdigit() else None
            fmt = im.sniff(io.BytesIO(first[:16]))
            if not im.is_native(fmt, size):
                opened = (slot, resp, first, rest)
                resp = slot = None
                return opened
            version = h.get("X-Source-Version", "")
            headers = {
                "Content-Type": im.CONTENT_TYPES[fmt], "Cache-Control": cache_control(v, version),
                "X-Image-Format": fmt, "X-Image-Native": "1", "X-Lossless": "1", **NOSNIFF,
                "Server-Timing": server_timing((time.perf_counter() - t0) * 1000, gen="agent"),
            }
            if version:
                headers["ETag"] = im.etag(version, "raw")
            if size is not None:
                headers["Content-Length"] = str(size)
            stream = ProxyStream(resp, slot, body=_chain(first, rest), headers=headers)
            resp = slot = None
            return stream
        finally:
            if resp is not None:
                await resp.aclose()
            if slot is not None:
                slot.release()

    async def _central(self, request: Request, host: str, port: int, full: str, v: str | None, kind: str,
                       params: dict, opened: tuple | None = None) -> Response:
        """中心服务生成：原图拉到本地暂存，用 imaging.Service 在线程池中生成（写入同一个磁盘缓存）。"""
        im = self.imaging
        if self.service is None or im.Image is None:
            if opened is not None:
                await self._discard(opened)
            raise error(501, "节点和中心服务都没有可用的 Pillow，无法生成预览")
        path, version = await self._spool(request, host, port, full, v, opened)
        kb = key_base(host, port, full, version or "spool:%d" % os.stat(path).st_mtime_ns)
        gone = threading.Event()
        watch = asyncio.create_task(_watch(request, gone))
        try:
            hdr, data, cache, ms = await asyncio.get_running_loop().run_in_executor(
                self._executor(), self._render, path, kb, kind, params, gone.is_set
            )
        except im.Gone:
            raise ClientGone()
        except im.Busy as exc:
            raise error(503, str(exc), exc.retry_after)
        except im.Unsupported as exc:
            raise error(501, str(exc))
        except im.TooLarge as exc:
            raise error(413, str(exc))
        except im.BadImage as exc:
            raise error(415, str(exc))
        except ValueError as exc:
            raise error(400, str(exc))
        finally:
            watch.cancel()
        timing = server_timing(gen_ms=ms, gen="central", cache="l2" if cache == "hit" else cache)
        return self._respond(hdr, data, version, v, kind, params, timing)

    def _render(self, path: str, kb: str, kind: str, params: dict, gone) -> tuple[dict, object, str, float]:
        """在线程中生成；内容为 bytes，很大的缓存命中（如转码的 8K 原图）为 (文件, 偏移, 长度)，不读入内存。"""
        with self.service.render(path, kb, kind, params, gone) as res:
            if res.file is not None and res.length > MAX_DERIVATIVE:
                body, res.file = (res.file, res.offset, res.length), None
                return res.hdr, body, res.cache, res.ms
            return res.hdr, res.read(), res.cache, res.ms

    # ------------------------------------------------------------------
    # 原图暂存
    # ------------------------------------------------------------------

    def _spool_path(self, host: str, port: int, full: str, version: str) -> str:
        name = hashlib.sha1(key_base(host, port, full, version).encode("utf-8", "surrogateescape")).hexdigest()
        return os.path.join(self.spool_dir, name)

    @staticmethod
    def _spool_fresh(path: str, versioned: bool) -> bool:
        """暂存文件可用：有版本号的按最近使用计时（使用时更新修改时间），没有版本号的拉取 5 分钟内有效。"""
        try:
            age = time.time() - os.stat(path).st_mtime
        except OSError:
            return False
        if not versioned:
            return age < SPOOL_UNVERSIONED_TTL
        if age > 60:
            try:
                os.utime(path)
            except OSError:
                pass
        return True

    async def _spool(self, request: Request, host: str, port: int, full: str, v: str | None,
                     opened: tuple | None = None) -> tuple[str, str]:
        """把原图拉到本地，返回 (路径, 版本号)；版本号为空表示旧版 Agent。同一文件同时只拉取一次。
        opened 为已经开始读取的原图（见 _peek_native），用不上时关闭。"""
        try:
            path = self._spool_path(host, port, full, v or "")
            if await asyncio.to_thread(self._spool_fresh, path, bool(v)):
                return path, v or ""
            return await self._once(("spool", host, port, full, v or ""),
                                    lambda: self._fetch(request, host, port, full, v, opened))
        finally:
            if opened is not None:
                await self._discard(opened)

    @staticmethod
    async def _discard(opened: tuple) -> None:
        """关闭读到一半的原图并释放名额（可重复调用）。"""
        await opened[1].aclose()
        opened[0].release()

    async def _fetch(self, request: Request, host: str, port: int, full: str, v: str | None,
                     opened: tuple | None = None) -> tuple[str, str]:
        if opened is not None:
            slot, resp, head, rest = opened
        else:
            slot = await self.agent.slot(host, port, "stream", request.is_disconnected)
            resp = None
        try:
            if resp is None:
                if await request.is_disconnected():
                    raise ClientGone()
                resp = await self.agent.open_stream(host, port, "/v1/files/raw", {"path": full})
                head, rest = b"", resp.aiter_raw()
            try:
                version = resp.headers.get("X-Source-Version", "")
                path = self._spool_path(host, port, full, version)
                if version != (v or "") and await asyncio.to_thread(self._spool_fresh, path, bool(version)):
                    return path, version  # 文件已改写，新版本已经暂存过
                length = resp.headers.get("Content-Length", "")
                if length.isdigit() and int(length) > self._spool_limit():
                    raise self._too_big()
                await self._write_spool(head, rest, path)
            finally:
                await resp.aclose()
        finally:
            slot.release()
        asyncio.get_running_loop().run_in_executor(None, self._evict_spool)
        return path, version

    def _spool_limit(self) -> int:
        return min(self.settings.image_spool_mb << 20, SPOOL_FILE_MAX)

    def _too_big(self) -> AgentError:
        return AgentError(f"原图超过 {self._spool_limit() >> 20} MB，无法生成预览", 413)

    def _maybe_image(self, head: bytes) -> bool:
        """文件开头像不像图片：认得的魔数，或 Pillow 能按 imaging.FORMATS 里的格式识别出来。
        文件头里的像素数超过上限时抛出 413，不必下载完再拒绝。"""
        im = self.imaging
        try:
            with im.open_image(io.BytesIO(head)) as img:
                w, h = img.size
        except im.Image.DecompressionBombError as exc:
            raise AgentError(f"图片像素过多：{exc}", 413) from exc
        except (im.Image.UnidentifiedImageError, im.BadImage):
            # 认得魔数而文件头不完整的（如 IFD 在文件末尾的 TIFF）交给生成时判断
            return im.sniff(io.BytesIO(head)) != "other"
        except Exception:  # 认出了格式（文件头不完整等），交给生成时判断
            return True
        if w * h > self.settings.image_max_pixels:
            raise AgentError(f"图片有 {w}×{h} 像素，超过上限 {self.settings.image_max_pixels}", 413)
        return True

    async def _write_spool(self, head: bytes, rest, path: str) -> None:
        """边收边写入暂存；先读够 PEEK_BYTES 判断是不是图片，不是时不必下载整个文件（如误点的模型权重）。
        超过单个文件的上限、磁盘剩余空间不足时放弃。"""
        tmp = f"{path}.{os.getpid()}.{id(rest)}.tmp"
        f = None
        limit = self._spool_limit()
        try:
            buf, size, total, checked = [head], len(head), len(head), False
            try:
                async for chunk in rest:
                    buf.append(chunk)
                    size += len(chunk)
                    total += len(chunk)
                    if total > limit:
                        raise self._too_big()
                    if not checked and size >= PEEK_BYTES:
                        checked = True
                        if not await asyncio.to_thread(self._maybe_image, b"".join(buf)):
                            raise AgentError("无法解码：不是图片文件", 415)
                    if checked and size >= 1 << 20:
                        if self.cache.low_space():
                            raise AgentError("中心服务磁盘空间不足，暂时不能生成预览", 503, "30")
                        if f is None:
                            f = await asyncio.to_thread(self._open_tmp, tmp)
                        await asyncio.to_thread(f.write, b"".join(buf))
                        buf, size = [], 0
            except httpx.HTTPError as exc:
                raise AgentError(f"读取原图失败: {type(exc).__name__} {exc}",
                                 timeout=isinstance(exc, httpx.TimeoutException)) from exc
            if self.cache.low_space():
                raise AgentError("中心服务磁盘空间不足，暂时不能生成预览", 503, "30")
            if f is None:
                f = await asyncio.to_thread(self._open_tmp, tmp)
            await asyncio.to_thread(f.write, b"".join(buf))
            await asyncio.to_thread(f.close)
            os.replace(tmp, path)
        except BaseException:
            if f is not None:
                f.close()
                try:
                    os.remove(tmp)
                except OSError:
                    pass
            raise

    def _open_tmp(self, tmp: str):
        os.makedirs(self.spool_dir, exist_ok=True)
        return open(tmp, "wb")

    def _evict_spool(self) -> None:
        """删除过期和超出容量的暂存原图（按最近使用从旧到新，最近 2 分钟用过的超出两倍容量时才删），以及 1 小时前的临时文件。"""
        cap, now, total, entries = self.settings.image_spool_mb << 20, time.time(), 0, []
        try:
            names = os.listdir(self.spool_dir)
        except OSError:
            return
        for name in names:
            p = os.path.join(self.spool_dir, name)
            try:
                st = os.stat(p)
                if now - st.st_mtime > (3600 if name.endswith(".tmp") else SPOOL_TTL):
                    os.remove(p)
                elif not name.endswith(".tmp"):
                    entries.append((st.st_mtime, st.st_size, p))
                    total += st.st_size
            except OSError:
                continue
        if total > cap:
            entries.sort()
            for mtime, size, p in entries:
                # 最近用过的可能正在生成（使用时刷新修改时间），宁可暂时超出容量也不删，但最多超出一倍
                if total <= cap * 0.8 or (now - mtime < SPOOL_GRACE and total <= cap * 2):
                    break
                try:
                    os.remove(p)
                    total -= size
                except OSError:
                    pass

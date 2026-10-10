"""调用节点 Agent 的任务和文件接口。

任务、状态等控制面请求每次新建连接（超时 agent_timeout）；对比页的图片请求走按事件循环共享的长连接池，
并按 (Agent, 类别) 限制同时进行的请求数。
"""

import asyncio
import json
import time
import weakref
from collections.abc import Awaitable, Callable

import httpx

from .config import Settings
from .tunnels import TunnelError

# 浏览样本、读取评测结果时整读的文件上限；图片和大文件用 open_stream 流式转发
MAX_RAW_BYTES = 64 << 20
# Agent 能力的缓存时间（秒）：成功、失败
FEATURES_TTL = 60
FEATURES_FAIL_TTL = 10


class AgentError(Exception):
    """Agent 返回错误或无法连接。status 为 None 表示网络错误，timeout 表示超时。"""

    def __init__(self, message: str, status: int | None = None, retry_after: str | None = None, timeout: bool = False):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after
        self.timeout = timeout


class ClientGone(Exception):
    """浏览器已断开，不再继续处理。"""


def agent_job_id(job_id: int) -> str:
    return f"gnm-{job_id}"


class Slot:
    """占用的一个请求名额，release 可重复调用。"""

    def __init__(self, sem: asyncio.Semaphore):
        self._sem = sem

    def release(self) -> None:
        if self._sem is not None:
            self._sem.release()
            self._sem = None


class AgentClient:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        self.transport = transport
        # SSH 端口转发（Tunnels），为 None 时直接连接 Agent
        self.tunnels = None
        # 事件循环 -> 图片请求的连接池 / {(地址, 端口, 类别): 信号量}；测试会用到多个事件循环
        self._media: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
        self._slots: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
        # (地址, 端口) -> (过期时间, 能力或 AgentError)
        self._features: dict[tuple[str, int], tuple[float, dict | AgentError]] = {}

    def _headers(self) -> dict:
        return {"X-Agent-Token": self.settings.agent_token} if self.settings.agent_token else {}

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=self.settings.agent_timeout, headers=self._headers(), transport=self.transport)

    async def _endpoint(self, host: str, port: int) -> tuple[str, int]:
        if self.tunnels is None:
            return host, port
        try:
            return await self.tunnels.endpoint(host, port)
        except TunnelError as exc:
            raise AgentError(str(exc)) from exc

    def _network_error(self, exc: httpx.HTTPError, key: tuple[str, int]) -> AgentError:
        message = f"无法连接 Agent: {type(exc).__name__} {exc}".strip()
        hint = self.tunnels.hint(*key) if self.tunnels is not None else None
        if hint:
            message += f"（SSH：{hint}）"
        return AgentError(message, timeout=isinstance(exc, httpx.TimeoutException))

    @staticmethod
    def _status_error(resp: httpx.Response) -> AgentError:
        try:
            message = resp.json().get("error") or resp.text
        except ValueError:
            message = resp.text
        except httpx.StreamError:  # 响应体没读完连接就断了
            message = resp.reason_phrase
        return AgentError(f"Agent 返回 {resp.status_code}: {message}", resp.status_code, resp.headers.get("Retry-After"))

    async def _send(self, method: str, host: str, port: int, path: str, limit: int | None = None, **kwargs) -> httpx.Response:
        key = (host, port)
        host, port = await self._endpoint(host, port)
        try:
            async with self._client() as client:
                async with client.stream(method, f"http://{host}:{port}{path}", **kwargs) as resp:
                    if limit is not None and resp.status_code < 400 and int(resp.headers.get("Content-Length") or 0) > limit:
                        raise AgentError(f"文件超过 {limit >> 20} MB", 413)
                    await resp.aread()
        except httpx.HTTPError as exc:
            raise self._network_error(exc, key) from exc
        if resp.status_code >= 400:
            raise self._status_error(resp)
        return resp

    async def _request(self, method: str, host: str, port: int, path: str, **kwargs) -> dict:
        return (await self._send(method, host, port, path, **kwargs)).json()

    async def health(self, host, port) -> dict:
        return await self._request("GET", host, port, "/v1/health")

    async def start_job(self, host, port, job_id, command, workdir, env, devices) -> dict:
        body = {
            "job_id": agent_job_id(job_id),
            "command": command,
            "workdir": workdir,
            "env": env or {},
            "devices": devices,
        }
        return await self._request("POST", host, port, "/v1/jobs", json=body)

    async def get_job(self, host, port, job_id) -> dict:
        return await self._request("GET", host, port, f"/v1/jobs/{agent_job_id(job_id)}")

    async def kill_job(self, host, port, job_id) -> dict:
        return await self._request("POST", host, port, f"/v1/jobs/{agent_job_id(job_id)}/kill")

    async def read_log(self, host, port, job_id, offset: int, limit: int) -> dict:
        return await self._request(
            "GET", host, port, f"/v1/jobs/{agent_job_id(job_id)}/log", params={"offset": offset, "limit": limit}
        )

    async def list_dir(self, host, port, path: str) -> dict:
        return await self._request("GET", host, port, "/v1/files/list", params={"path": path})

    async def read_raw(self, host, port, path: str) -> tuple[bytes, str]:
        """整读一个文件（超过 64 MB 时报 413）；浏览图片等大文件用 open_stream。"""
        resp = await self._send("GET", host, port, "/v1/files/raw", limit=MAX_RAW_BYTES, params={"path": path})
        return resp.content, resp.headers.get("content-type", "application/octet-stream")

    async def read_json(self, host, port, path: str):
        data, _ = await self.read_raw(host, port, path)
        try:
            return json.loads(data)
        except ValueError as exc:
            raise AgentError(f"{path} 不是合法 JSON: {exc}") from exc

    async def read_samples(self, host, port, path: str, offset: int, limit: int) -> dict:
        """结果集目录的样本：predictions.jsonl 的记录，没有时为扫描到的图片和 .txt。"""
        return await self._request(
            "GET", host, port, "/v1/files/samples", params={"path": path, "offset": offset, "limit": limit}
        )

    async def read_jsonl(self, host, port, path: str, offset: int, limit: int) -> dict:
        return await self._request(
            "GET", host, port, "/v1/files/jsonl", params={"path": path, "offset": offset, "limit": limit}
        )

    # ------------------------------------------------------------------
    # 图片（对比页）：共享长连接、流式响应、按类别限流
    # ------------------------------------------------------------------

    def media_client(self) -> httpx.AsyncClient:
        """当前事件循环的图片请求连接池。"""
        loop = asyncio.get_running_loop()
        client = self._media.get(loop)
        if client is None or client.is_closed:
            client = self._media[loop] = httpx.AsyncClient(
                headers=self._headers(),
                transport=self.transport,
                limits=httpx.Limits(max_connections=256, max_keepalive_connections=64, keepalive_expiry=30),
                timeout=httpx.Timeout(connect=10, read=self.settings.image_timeout, write=30, pool=None),
            )
        return client

    async def aclose(self) -> None:
        """关闭当前事件循环的连接池（服务退出时调用）。"""
        loop = asyncio.get_running_loop()
        self._slots.pop(loop, None)
        client = self._media.pop(loop, None)
        if client is not None:
            await client.aclose()

    async def open_stream(
        self, host: str, port: int, path: str, params: dict | None = None, headers: dict | None = None,
        timeout: float | None = None,
    ) -> httpx.Response:
        """发起 GET 并在收到响应头后返回，响应体由调用方读取并 aclose()。状态码 >= 400 时抛出 AgentError；
        复用的空闲连接已被关闭、转发端口变了等连接错误重试一次。"""
        key = (host, port)
        client = self.media_client()
        if timeout is not None:
            timeout = httpx.Timeout(connect=10, read=timeout, write=30, pool=None)
        for attempt in (0, 1):
            h, p = await self._endpoint(host, port)
            request = client.build_request(
                "GET", f"http://{h}:{p}{path}", params=params, headers=headers,
                timeout=timeout if timeout is not None else httpx.USE_CLIENT_DEFAULT,
            )
            try:
                resp = await client.send(request, stream=True)
            except (httpx.RemoteProtocolError, httpx.ReadError, httpx.ConnectError) as exc:
                if attempt == 0:
                    continue
                raise self._network_error(exc, key) from exc
            except httpx.HTTPError as exc:
                raise self._network_error(exc, key) from exc
            if resp.status_code >= 400:
                try:
                    await resp.aread()
                except httpx.HTTPError:
                    pass
                finally:
                    await resp.aclose()
                raise self._status_error(resp)
            return resp
        raise AssertionError("unreachable")

    async def features(self, host: str, port: int) -> dict:
        """Agent 的图片能力（/v1/health 的 features，旧版 Agent 为空字典），缓存 60 秒，失败缓存 10 秒。"""
        key = (host, port)
        cached = self._features.get(key)
        if cached is not None and time.monotonic() < cached[0]:
            if isinstance(cached[1], AgentError):
                exc = cached[1]
                raise AgentError(str(exc), exc.status, exc.retry_after, exc.timeout)
            return cached[1]
        try:
            resp = await self.open_stream(host, port, "/v1/health", timeout=self.settings.agent_timeout)
            try:
                features = json.loads(await resp.aread()).get("features") or {}
            except (ValueError, AttributeError, httpx.HTTPError) as exc:
                raise AgentError(f"Agent 健康检查返回的内容不对: {exc}") from exc
            finally:
                await resp.aclose()
        except AgentError as exc:
            self._features[key] = (time.monotonic() + FEATURES_FAIL_TTL, exc)
            raise
        self._features[key] = (time.monotonic() + FEATURES_TTL, features)
        return features

    def forget(self) -> None:
        """服务器的配置变化后，重新查询 Agent 的能力。"""
        self._features.clear()

    def _semaphore(self, host: str, port: int, cls: str) -> asyncio.Semaphore:
        slots = self._slots.setdefault(asyncio.get_running_loop(), {})
        sem = slots.get((host, port, cls))
        if sem is None:
            sem = slots[(host, port, cls)] = asyncio.Semaphore(getattr(self.settings, f"image_{cls}_concurrency"))
        return sem

    async def slot(self, host: str, port: int, cls: str, gone: Callable[[], Awaitable[bool]] | None = None) -> Slot:
        """排队占用该 Agent 一个 cls 类别的名额（list 列表、thumb 缩略图、main 预览和瓦片、stream 原图和文件流，
        含中心生成时拉取原图）；排队超过 image_queue_wait 秒抛出 503，
        浏览器断开（gone() 为真）时抛出 ClientGone。排队按先来后到。"""
        sem = self._semaphore(host, port, cls)
        if not sem.locked():
            await sem.acquire()
            return Slot(sem)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.settings.image_queue_wait
        task = asyncio.ensure_future(sem.acquire())
        try:
            while True:
                done, _ = await asyncio.wait({task}, timeout=max(0.0, min(0.5, deadline - loop.time())))
                if done:
                    task.result()
                    return Slot(sem)
                if gone is not None and await gone():
                    raise ClientGone()
                if loop.time() >= deadline:
                    raise AgentError("节点上的图片请求太多，请稍后重试", 503, "1")
        except BaseException:
            if not task.cancel() and not task.cancelled() and task.exception() is None:
                sem.release()
            raise

"""调用节点 Agent 的任务和文件接口。"""

import json

import httpx

from .config import Settings
from .tunnels import TunnelError


class AgentError(Exception):
    """Agent 返回错误或无法连接。status 为 None 表示网络错误。"""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def agent_job_id(job_id: int) -> str:
    return f"gnm-{job_id}"


class AgentClient:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        self.transport = transport
        # SSH 端口转发（Tunnels），为 None 时直接连接 Agent
        self.tunnels = None

    def _client(self) -> httpx.AsyncClient:
        headers = {"X-Agent-Token": self.settings.agent_token} if self.settings.agent_token else {}
        return httpx.AsyncClient(timeout=self.settings.agent_timeout, headers=headers, transport=self.transport)

    async def _send(self, method: str, host: str, port: int, path: str, **kwargs) -> httpx.Response:
        key = (host, port)
        if self.tunnels is not None:
            try:
                host, port = await self.tunnels.endpoint(host, port)
            except TunnelError as exc:
                raise AgentError(str(exc)) from exc
        try:
            async with self._client() as client:
                resp = await client.request(method, f"http://{host}:{port}{path}", **kwargs)
        except httpx.HTTPError as exc:
            message = f"无法连接 Agent: {type(exc).__name__} {exc}".strip()
            hint = self.tunnels.hint(*key) if self.tunnels is not None else None
            if hint:
                message += f"（SSH：{hint}）"
            raise AgentError(message) from exc
        if resp.status_code >= 400:
            try:
                message = resp.json().get("error") or resp.text
            except ValueError:
                message = resp.text
            raise AgentError(f"Agent 返回 {resp.status_code}: {message}", resp.status_code)
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
        resp = await self._send("GET", host, port, "/v1/files/raw", params={"path": path})
        return resp.content, resp.headers.get("content-type", "application/octet-stream")

    async def read_json(self, host, port, path: str):
        data, _ = await self.read_raw(host, port, path)
        try:
            return json.loads(data)
        except ValueError as exc:
            raise AgentError(f"{path} 不是合法 JSON: {exc}") from exc

    async def read_jsonl(self, host, port, path: str, offset: int, limit: int) -> dict:
        return await self._request(
            "GET", host, port, "/v1/files/jsonl", params={"path": path, "offset": offset, "limit": limit}
        )

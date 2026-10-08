"""调用节点 Agent 的任务接口。"""

import httpx

from .config import Settings


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

    def _client(self) -> httpx.AsyncClient:
        headers = {"X-Agent-Token": self.settings.agent_token} if self.settings.agent_token else {}
        return httpx.AsyncClient(timeout=self.settings.agent_timeout, headers=headers, transport=self.transport)

    async def _request(self, method: str, host: str, port: int, path: str, **kwargs) -> dict:
        try:
            async with self._client() as client:
                resp = await client.request(method, f"http://{host}:{port}{path}", **kwargs)
        except httpx.HTTPError as exc:
            raise AgentError(f"无法连接 Agent: {type(exc).__name__} {exc}".strip()) from exc
        if resp.status_code >= 400:
            try:
                message = resp.json().get("error") or resp.text
            except ValueError:
                message = resp.text
            raise AgentError(f"Agent 返回 {resp.status_code}: {message}", resp.status_code)
        return resp.json()

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

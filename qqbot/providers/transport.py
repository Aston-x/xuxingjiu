"""HTTP 传输层：客户端缓存 + 代理绕过判定 + 出错时把服务端原话记下来。

**本机端点必须绕过 HTTP_PROXY**：环境变量里的代理解析不到 127.0.0.1，
一旦走代理就会卡死或连不上。重构前这条判断写在 `Chat.client_for` 里，现在搬到这。
"""

from __future__ import annotations

import logging
from typing import Any, Callable

import httpx

from .types import Endpoint, Request

logger = logging.getLogger("providers.transport")

ClientFactory = Callable[[bool], httpx.AsyncClient]


class Transport:
    """按 (是否本机, trust_env) 缓存 AsyncClient。

    重试与业务重试（截断翻倍）不在这里做 —— 这里只管发出去、记日志、抛错。
    """

    def __init__(self, client_factory: ClientFactory | None = None,
                 default_timeout: float = 90.0) -> None:
        self._factory = client_factory or self._default_factory
        self._clients: dict[tuple[bool, bool], httpx.AsyncClient] = {}
        self.default_timeout = default_timeout

    @staticmethod
    def _default_factory(trust_env: bool) -> httpx.AsyncClient:
        return httpx.AsyncClient(trust_env=trust_env)

    def client_for(self, ep: Endpoint) -> httpx.AsyncClient:
        trust_env = ep.effective_trust_env
        key = (ep.is_loopback, trust_env)
        cli = self._clients.get(key)
        if cli is None or cli.is_closed:
            cli = self._factory(trust_env)
            self._clients[key] = cli
        return cli

    async def send(self, ep: Endpoint, req: Request, timeout: float | None = None) -> httpx.Response:
        """发请求。4xx/5xx 先把服务端原话写进日志，再 raise_for_status。"""
        cli = self.client_for(ep)
        kw: dict[str, Any] = {"headers": req.headers or None,
                              "timeout": timeout or ep.timeout or self.default_timeout}
        if req.json_body is not None:
            kw["json"] = req.json_body
        if req.params:
            kw["params"] = req.params
        resp = await cli.request(req.method.upper(), req.url, **kw)
        if resp.status_code >= 400:
            logger.error("模型接口 %s 返回 %s：%s", req.url, resp.status_code,
                         (resp.text or "")[:400])
        resp.raise_for_status()
        return resp

    async def get(self, ep: Endpoint, url: str, *, headers: dict | None = None,
                  params: dict | None = None, timeout: float = 10.0) -> httpx.Response:
        cli = self.client_for(ep)
        return await cli.get(url, headers=headers or None, params=params or None, timeout=timeout)

    async def aclose(self) -> None:
        for cli in list(self._clients.values()):
            try:
                await cli.aclose()
            except Exception:  # noqa: BLE001
                pass
        self._clients.clear()

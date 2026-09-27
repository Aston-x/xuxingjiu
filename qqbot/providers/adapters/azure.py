"""Azure OpenAI 适配器。

和"OpenAI 官方"看着像，其实三处都不一样，塞进 OpenAI 适配器 + params 是塞不下的：

  · URL 里带 **deployment**：`{base}/openai/deployments/{deployment}/chat/completions`；
  · 鉴权头是 **`api-key`**，不是 `Authorization: Bearer`；
  · 必须带 **`?api-version=YYYY-MM-DD`** 查询参；
  · 没有可用的 `/models` 探针。

所以 base_url 要写成 `https://{资源名}.openai.azure.com`（**不带** /openai 前缀），
deployment 与 api_version 写在端点配置里。
"""

from __future__ import annotations

from ..base import register_provider
from ..types import Capability, Endpoint, Request, UsageKeys
from .openai import OpenAIProvider

DEFAULT_API_VERSION = "2024-10-21"


@register_provider("azure")
class AzureOpenAIProvider(OpenAIProvider):
    name = "azure"
    display_name = "Azure OpenAI"

    def default_capabilities(self) -> Capability:
        return Capability(
            vision=True,
            vision_input="url",
            vision_block_style="openai",
            tool_calling=True,
            streaming=True,
            max_tokens_key="max_tokens",
            temperature_range=(0.0, 2.0),
            usage=UsageKeys(prompt="usage.prompt_tokens",
                            completion="usage.completion_tokens",
                            cache_hit="usage.prompt_tokens_details.cached_tokens"),
        )

    def build_request(self, ep: Endpoint, messages: list, *, stream: bool,
                      max_tokens: int, temperature: float,
                      extra: dict | None) -> Request:
        req = super().build_request(ep, messages, stream=stream, max_tokens=max_tokens,
                                    temperature=temperature, extra=extra)
        deployment = str(ep.deployment or ep.model)
        api_version = str(ep.api_version or ep.params.get("_api_version")
                          or DEFAULT_API_VERSION)
        root = ep.base_url.rstrip("/")
        # 用户可能把 /openai 也写进 base_url，兼容一下
        if root.endswith("/openai"):
            root = root[: -len("/openai")]
        req.url = f"{root}/openai/deployments/{deployment}/chat/completions"
        req.headers.pop("Authorization", None)
        req.headers["api-key"] = ep.auth()
        params = dict(req.params or {})
        params["api-version"] = api_version
        req.params = params
        return req

    async def probe(self, ep: Endpoint, http) -> dict:
        # Azure 没有列模型的公开端点，探了也是 404
        return {"unsupported": True}

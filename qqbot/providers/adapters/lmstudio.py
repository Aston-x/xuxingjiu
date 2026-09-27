"""LM Studio 适配器：聊天走 OpenAI 兼容接口，探针走它自己的原生 `/api/v0/models`。

为什么探针不能用 `/v1/models`：那个只列「装了什么」，而面板想知道的是
「现在**加载**的是哪个、上下文开多长」—— 本地 VL 上下文太小正是当年图被换成文字、
她只能编的根因。`/api/v0/models` 的返回里才有 `state` 和 `loaded_context_length`。
"""

from __future__ import annotations

from ..base import register_provider
from ..types import Capability, Endpoint, UsageKeys
from .openai import OpenAIProvider


@register_provider("lmstudio")
class LMStudioProvider(OpenAIProvider):
    name = "lmstudio"
    display_name = "LM Studio（本地）"

    def default_capabilities(self) -> Capability:
        return Capability(
            vision=True,
            vision_input="base64",
            vision_block_style="openai",
            streaming=True,
            tool_calling=True,
            max_tokens_key="max_tokens",
            temperature_range=(0.0, 2.0),
            usage=UsageKeys(),
            max_image_side_default=1024,
        )

    @staticmethod
    def _root(base_url: str) -> str:
        """把 base_url 的 `/v1` 去掉 —— 原生 API 挂在根上，不在 /v1 下面。"""
        b = (base_url or "").rstrip("/")
        return b[:-3].rstrip("/") if b.endswith("/v1") else b

    async def probe(self, ep: Endpoint, http) -> dict:
        root = self._root(ep.base_url)
        if not root:
            return {"error": "base_url 是空的"}
        try:
            resp = await http.get(ep, root + "/api/v0/models", timeout=10)
            resp.raise_for_status()
            items = (resp.json() or {}).get("data") or []
        except Exception as exc:  # noqa: BLE001
            return {"error": str(exc)[:150]}
        picked = next((m for m in items if m.get("state") == "loaded"),
                      items[0] if items else {})
        return {
            "base_url": root,
            "id": str(picked.get("id") or ""),
            "state": str(picked.get("state") or ""),
            "loaded_context_length": picked.get("loaded_context_length"),
            "max_context_length": picked.get("max_context_length"),
            "models": len(items),
        }

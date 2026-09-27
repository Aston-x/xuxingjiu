"""OpenAI 兼容适配器 —— 覆盖面最广的一份，绝大多数厂商都走它。

一次适配，以下全部可用（只改 base_url + model）：
  云端：DeepSeek、Kimi/Moonshot、智谱 GLM、通义 Qwen(DashScope 兼容模式)、硅基流动、
        OpenRouter、Groq、xAI、MiniMax、零一万物、百川、阶跃星辰、Perplexity、Together…
  本地：vLLM、llama.cpp server、text-generation-webui、LocalAI、KoboldCpp、Jan、Xinference…

⚠️ 两条不能踩的规则：
  1. **不自动补 `/v1`**。DeepSeek 的 base_url 是 `https://api.deepseek.com`（不带 /v1），
     LM Studio 是 `http://127.0.0.1:1234/v1`（带 /v1）—— 两种写法都是对的，
     自作聪明地统一补 /v1 会让其中一种行为突变。规则是：base_url 的版本前缀由用户写全，
     这里只拼 `request.path` 的尾段。
  2. **不往统一 body 里塞厂商私有字段**。比如 `thinking` 只有 DeepSeek 认，
     塞给 Groq / OpenRouter / Kimi 会直接 400。私有字段一律走端点级 `params`。
"""

from __future__ import annotations

import random

from ..base import Provider, register_provider
from ..types import Capability, ChatResult, Endpoint, Request, UsageKeys, dget, dset
from ..usage import normalize_usage

# 这些厂商的 usage 带缓存计费字段，用它们做默认更容易命中
DEEPSEEK_USAGE = UsageKeys(prompt="prompt_tokens", completion="completion_tokens",
                           cache_hit="prompt_cache_hit_tokens",
                           cache_miss="prompt_cache_miss_tokens")


@register_provider("openai")
class OpenAIProvider(Provider):
    name = "openai"
    display_name = "OpenAI 兼容 (/chat/completions)"

    # 允许清单覆盖 path 与鉴权方式（有些网关的前缀不一样）
    default_path = "/chat/completions"

    def default_capabilities(self) -> Capability:
        return Capability(
            vision=False,
            vision_input="url",
            vision_block_style="openai",
            streaming=True,
            tool_calling=True,
            max_tokens_key="max_tokens",
            temperature_range=(0.0, 2.0),
            usage=UsageKeys(),          # 最保守的默认；带缓存的厂商在端点里覆盖
        )

    # ── 请求 ──

    def build_request(self, ep: Endpoint, messages: list, *, stream: bool,
                      max_tokens: int, temperature: float,
                      extra: dict | None) -> Request:
        cap = self.capability(ep)
        path = str(ep.params.get("_path") or self.default_path)
        url = ep.base_url.rstrip("/") + path

        body: dict = {
            "model": ep.model,
            "messages": messages,
            "stream": bool(stream),
            "temperature": cap.clamped_temperature(temperature),
        }
        # max_tokens 的字段名各家不同（max_tokens / max_completion_tokens / 嵌套路径）
        if cap.max_tokens_key == "max_tokens":
            body["max_tokens"] = int(max_tokens)
        else:
            dset(body, cap.max_tokens_key, int(max_tokens))

        # 端点私有参数（如 DeepSeek 的 thinking）—— 只加在这一条端点上
        for k, v in (ep.params or {}).items():
            if k.startswith("_"):
                continue
            body[k] = v
        if extra:
            body.update(extra)

        headers = self.bearer_headers(ep)
        headers.update(ep.headers or {})
        return Request(method="POST", url=url, headers=headers, json_body=body,
                       params=dict(ep.query) or None)

    # ── 响应 ──

    def parse_response(self, ep: Endpoint, data: dict) -> ChatResult:
        cap = self.capability(ep)
        choice = dget(data, "choices[0]", {}) or {}
        msg = dget(choice, "message", {}) or {}
        # content 偶尔是 null，直接 .strip() 会炸
        content = msg.get("content")
        text = (content or "").strip() if isinstance(content, str) else ""
        return ChatResult(
            text=text,
            usage=normalize_usage(data, cap),
            endpoint_id=ep.id,
            finish_reason=str(choice.get("finish_reason") or ""),
            raw=data,
        )

    # ── 探针：GET /models ──

    async def probe(self, ep: Endpoint, http) -> dict:
        root = ep.base_url.rstrip("/")
        try:
            resp = await http.get(ep, root + "/models", headers=self.bearer_headers(ep))
            resp.raise_for_status()
            items = (resp.json() or {}).get("data") or []
            ids = [str(m.get("id") or "") for m in items if isinstance(m, dict)]
            return {"models": len(ids), "sample": ids[:5]}
        except Exception as exc:  # noqa: BLE001
            return {"error": str(exc)[:150]}


def jitter_temperature(base: float = 0.8, spread: float = 0.2) -> float:
    """回复要有点变化，不然每句都一个调子。原实现是 0.8 + rand*0.2。"""
    return round(base + random.random() * spread, 2)

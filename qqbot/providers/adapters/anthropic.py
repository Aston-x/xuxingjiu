"""Anthropic Messages 适配器（Claude 系列）。

和 OpenAI 系的差异不止字段名，报文结构本身就不同，所以必须单独写：

  · `system` 是**顶层字段**，不能留在 messages 里（留在里面会 400）；
  · `messages` 必须 user / assistant 交替，且**以 user 开头**；
  · `max_tokens` **必填**（漏了直接报错，不是默认值）；
  · 图片是 `{"type":"image","source":{...}}`，不是 `image_url`；
  · 响应文本在 `content[]` 里，而且可能是多个 block（含 thinking block，要挑掉）；
  · 截断判据是 `stop_reason == "max_tokens"`，不是 `finish_reason == "length"`。
"""

from __future__ import annotations

from ..base import Provider, register_provider
from ..types import Capability, ChatResult, Endpoint, Request, UsageKeys, dget
from ..usage import normalize_usage

ANTHROPIC_VERSION = "2023-06-01"


def split_data_url(url: str) -> tuple[str, str] | None:
    """`data:image/png;base64,AAAA` → ("image/png", "AAAA")；不是 data URL 就返回 None。"""
    if not isinstance(url, str) or not url.startswith("data:"):
        return None
    head, _, payload = url.partition(",")
    if not payload:
        return None
    meta = head[5:]
    media = meta.split(";", 1)[0] or "image/jpeg"
    return media, payload


@register_provider("anthropic")
class AnthropicProvider(Provider):
    name = "anthropic"
    display_name = "Anthropic Messages（Claude）"

    @property
    def default_path(self) -> str:
        return "/messages"

    def default_capabilities(self) -> Capability:
        return Capability(
            vision=True,
            vision_input="both",
            vision_block_style="anthropic",
            thinking=True,
            tool_calling=True,
            streaming=True,
            max_tokens_key="max_tokens",
            temperature_range=(0.0, 1.0),
            system_in_messages=False,
            max_image_side_default=1568,
            usage=UsageKeys(prompt="usage.input_tokens",
                            completion="usage.output_tokens",
                            cache_hit="usage.cache_read_input_tokens",
                            cache_miss="usage.cache_creation_input_tokens"),
        )

    # ── 消息体转换 ──

    @staticmethod
    def _blocks(content) -> list:
        """把 OpenAI 风格的 content 转成 Anthropic 的 content blocks。"""
        if isinstance(content, str):
            return [{"type": "text", "text": content}]
        out: list = []
        for p in content if isinstance(content, list) else []:
            if not isinstance(p, dict):
                continue
            if p.get("type") == "text":
                out.append({"type": "text", "text": str(p.get("text") or "")})
            elif p.get("type") == "image_url":
                url = str(((p.get("image_url") or {}) or {}).get("url") or "")
                got = split_data_url(url)
                if got:
                    media, data = got
                    out.append({"type": "image",
                                "source": {"type": "base64", "media_type": media, "data": data}})
                elif url:
                    # Anthropic 也接受来源为 URL 的图片
                    out.append({"type": "image", "source": {"type": "url", "url": url}})
        return out or [{"type": "text", "text": ""}]

    def build_request(self, ep: Endpoint, messages: list, *, stream: bool,
                      max_tokens: int, temperature: float,
                      extra: dict | None) -> Request:
        cap = self.capability(ep)
        path = str(ep.params.get("_path") or self.default_path)
        url = ep.base_url.rstrip("/") + path

        system_parts: list[str] = []
        conv: list = []
        for m in messages:
            if not isinstance(m, dict):
                continue
            role = str(m.get("role") or "user")
            if role == "system":
                content = m.get("content")
                if isinstance(content, str) and content.strip():
                    system_parts.append(content)
                continue
            conv.append({"role": "assistant" if role == "assistant" else "user",
                         "content": self._blocks(m.get("content"))})

        # Anthropic 要求以 user 开头、且 user/assistant 交替
        if not conv or conv[0]["role"] != "user":
            conv.insert(0, {"role": "user", "content": [{"type": "text", "text": "(开始)"}]})
        merged: list = []
        for item in conv:
            if merged and merged[-1]["role"] == item["role"]:
                merged[-1]["content"] = merged[-1]["content"] + item["content"]
            else:
                merged.append(item)

        body: dict = {
            "model": ep.model,
            "messages": merged,
            "max_tokens": int(max_tokens),
            "temperature": cap.clamped_temperature(temperature),
            "stream": bool(stream),
        }
        if system_parts:
            body["system"] = "\n\n".join(system_parts)
        for k, v in (ep.params or {}).items():
            if not k.startswith("_"):
                body[k] = v
        if extra:
            body.update(extra)

        headers = {
            "x-api-key": ep.auth(),
            "anthropic-version": str(ep.params.get("_version") or ANTHROPIC_VERSION),
            "Content-Type": "application/json",
        }
        headers.update(ep.headers or {})
        return Request(method="POST", url=url, headers=headers, json_body=body,
                       params=dict(ep.query) or None)

    def parse_response(self, ep: Endpoint, data: dict) -> ChatResult:
        cap = self.capability(ep)
        blocks = data.get("content") or []
        texts: list[str] = []
        for b in blocks:
            if isinstance(b, dict) and b.get("type") == "text":
                texts.append(str(b.get("text") or ""))
        return ChatResult(
            text="".join(texts).strip(),
            usage=normalize_usage(data, cap),
            endpoint_id=ep.id,
            finish_reason=str(data.get("stop_reason") or ""),
            raw=data,
        )

    async def probe(self, ep: Endpoint, http) -> dict:
        # Anthropic 没有公开的 /models 列表（有也需额外权限），不探
        return {"unsupported": True}


__all__ = ["AnthropicProvider", "split_data_url", "dget"]

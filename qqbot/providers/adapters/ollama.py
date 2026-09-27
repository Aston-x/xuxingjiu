"""Ollama 原生适配器（`/api/chat`）。

用原生接口而不是它的 `/v1` 兼容层，理由有两个：
  · 原生接口能拿到 `prompt_eval_count` / `eval_count` 的真实用量，兼容层只给个空 usage；
  · 生成参数在 `options` 里（`num_predict` 才是输出上限），兼容层会把它翻译错。

图片在这边是 `messages[].images`（**base64 字符串数组，不带 data: 前缀**），
不是 OpenAI 那种 content 数组 —— 所以必须转换消息体。
"""

from __future__ import annotations

from ..base import Provider, register_provider
from ..types import Capability, ChatResult, Endpoint, Request, UsageKeys, dget
from ..usage import normalize_usage
from .anthropic import split_data_url


@register_provider("ollama")
class OllamaProvider(Provider):
    name = "ollama"
    display_name = "Ollama（本地）"

    def default_capabilities(self) -> Capability:
        return Capability(
            vision=True,
            vision_input="base64",
            vision_block_style="openai",
            thinking=True,
            tool_calling=True,
            streaming=True,
            max_tokens_key="options.num_predict",
            max_tokens_limit=None,
            temperature_range=(0.0, 2.0),
            system_in_messages=True,
            max_image_side_default=1024,
            usage=UsageKeys(prompt="prompt_eval_count", completion="eval_count"),
        )

    # ── 消息体转换：content 数组 -> text + images[] ──

    @staticmethod
    def _to_ollama(messages: list) -> list:
        out: list = []
        for m in messages:
            if not isinstance(m, dict):
                continue
            content = m.get("content")
            if isinstance(content, str):
                out.append({**m, "content": content})
                continue
            texts: list[str] = []
            images: list[str] = []
            for p in content if isinstance(content, list) else []:
                if not isinstance(p, dict):
                    continue
                if p.get("type") == "text":
                    texts.append(str(p.get("text") or ""))
                elif p.get("type") == "image_url":
                    url = str(((p.get("image_url") or {}) or {}).get("url") or "")
                    got = split_data_url(url)
                    if got:
                        images.append(got[1])            # base64，不带前缀
                    elif url:
                        texts.append(f"（图片链接：{url}）")
            item = {**m, "content": "\n".join(texts)}
            if images:
                item["images"] = images
            out.append(item)
        return out

    def build_request(self, ep: Endpoint, messages: list, *, stream: bool,
                      max_tokens: int, temperature: float,
                      extra: dict | None) -> Request:
        cap = self.capability(ep)
        path = str(ep.params.get("_path") or "/api/chat")
        url = ep.base_url.rstrip("/") + path

        options: dict = {"temperature": cap.clamped_temperature(temperature),
                         "num_predict": int(max_tokens)}
        body: dict = {
            "model": ep.model,
            "messages": self._to_ollama(messages),
            "stream": bool(stream),
            "options": options,
        }
        for k, v in (ep.params or {}).items():
            if k.startswith("_"):
                continue
            if k == "options" and isinstance(v, dict):
                options.update(v)
            else:
                body[k] = v
        if extra:
            body.update(extra)

        headers = {"Content-Type": "application/json"}
        if ep.has_key():
            headers["Authorization"] = f"Bearer {ep.auth()}"
        headers.update(ep.headers or {})
        return Request(method="POST", url=url, headers=headers, json_body=body,
                       params=dict(ep.query) or None)

    def parse_response(self, ep: Endpoint, data: dict) -> ChatResult:
        cap = self.capability(ep)
        text = str(dget(data, "message.content", "") or "").strip()
        return ChatResult(text=text,
                          usage=normalize_usage(data, cap),
                          endpoint_id=ep.id,
                          finish_reason=str(data.get("done_reason") or ""),
                          raw=data)

    def needs_truncation_retry(self, result: ChatResult) -> bool:
        return not result.text and result.finish_reason in ("length", "max_tokens")

    async def probe(self, ep: Endpoint, http) -> dict:
        root = ep.base_url.rstrip("/")
        if root.endswith("/api"):
            root = root[: -len("/api")]
        try:
            resp = await http.get(ep, root + "/api/tags", timeout=10)
            resp.raise_for_status()
            items = (resp.json() or {}).get("models") or []
        except Exception as exc:  # noqa: BLE001
            return {"error": str(exc)[:150]}
        names = [str(m.get("name") or "") for m in items if isinstance(m, dict)]
        return {"models": len(names), "sample": names[:5],
                "loaded_context_length": None, "max_context_length": None}

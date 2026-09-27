"""Google Gemini 适配器。

报文结构和 OpenAI **完全不同**，靠字段映射是搞不定的，所以独立适配：

  · 没有 `messages`，是 `contents[].parts[]`；
  · role 只有 `user` / `model`（assistant 要改名）；
  · system 走顶层 `system_instruction`；
  · 生成参数在 `generationConfig` 里（`maxOutputTokens` / `temperature`）；
  · 图片是 `{"inline_data": {"mime_type": ..., "data": ...}}`；
  · 响应文本在 `candidates[0].content.parts[].text`；
  · 用量在 `usageMetadata`（`promptTokenCount` / `candidatesTokenCount` / `cachedContentTokenCount`）；
  · 截断判据是 `finishReason == "MAX_TOKENS"`。
"""

from __future__ import annotations

from ..base import Provider, register_provider
from ..types import Capability, ChatResult, Endpoint, Request, UsageKeys, dget
from ..usage import normalize_usage
from .anthropic import split_data_url


@register_provider("gemini")
class GeminiProvider(Provider):
    name = "gemini"
    display_name = "Google Gemini"

    def default_capabilities(self) -> Capability:
        return Capability(
            vision=True,
            vision_input="both",
            vision_block_style="gemini",
            thinking=True,
            tool_calling=True,
            streaming=True,
            max_tokens_key="generationConfig.maxOutputTokens",
            temperature_range=(0.0, 2.0),
            system_in_messages=False,
            max_image_side_default=1568,
            usage=UsageKeys(prompt="usageMetadata.promptTokenCount",
                            completion="usageMetadata.candidatesTokenCount",
                            cache_hit="usageMetadata.cachedContentTokenCount",
                            cache_miss=""),
        )

    @staticmethod
    def _parts(content) -> list:
        if isinstance(content, str):
            return [{"text": content}]
        out: list = []
        for p in content if isinstance(content, list) else []:
            if not isinstance(p, dict):
                continue
            if p.get("type") == "text":
                out.append({"text": str(p.get("text") or "")})
            elif p.get("type") == "image_url":
                url = str(((p.get("image_url") or {}) or {}).get("url") or "")
                got = split_data_url(url)
                if got:
                    media, data = got
                    out.append({"inline_data": {"mime_type": media, "data": data}})
                elif url:
                    # Gemini 也能直接吃公开 URL，但要走 fileData（需上传），
                    # 这里保守地降级成文字，避免发出去被 400
                    out.append({"text": f"（图片链接：{url}）"})
        return out or [{"text": ""}]

    def build_request(self, ep: Endpoint, messages: list, *, stream: bool,
                      max_tokens: int, temperature: float,
                      extra: dict | None) -> Request:
        cap = self.capability(ep)
        path = str(ep.params.get("_path") or f"/models/{ep.model}:generateContent")
        url = ep.base_url.rstrip("/") + path

        system_parts: list[str] = []
        contents: list = []
        for m in messages:
            if not isinstance(m, dict):
                continue
            role = str(m.get("role") or "user")
            if role == "system":
                if isinstance(m.get("content"), str):
                    system_parts.append(m["content"])
                continue
            contents.append({"role": "model" if role == "assistant" else "user",
                             "parts": self._parts(m.get("content"))})
        if not contents:
            contents = [{"role": "user", "parts": [{"text": ""}]}]

        gen: dict = {"temperature": cap.clamped_temperature(temperature),
                     "maxOutputTokens": int(max_tokens)}
        body: dict = {"contents": contents, "generationConfig": gen}
        if system_parts:
            body["system_instruction"] = {"parts": [{"text": "\n\n".join(system_parts)}]}
        for k, v in (ep.params or {}).items():
            if not k.startswith("_"):
                body[k] = v
        if extra:
            body.update(extra)

        # 鉴权：优先 x-goog-api-key（官方推荐），也支持 ?key= 的老写法
        headers = {"Content-Type": "application/json"}
        params: dict = dict(ep.query or {})
        auth = ep.auth()
        if auth:
            headers["x-goog-api-key"] = auth
        headers.update(ep.headers or {})
        return Request(method="POST", url=url, headers=headers, json_body=body,
                       params=params or None)

    def parse_response(self, ep: Endpoint, data: dict) -> ChatResult:
        cap = self.capability(ep)
        parts = dget(data, "candidates[0].content.parts", []) or []
        texts: list[str] = []
        for p in parts:
            if isinstance(p, dict) and p.get("text") is not None:
                texts.append(str(p["text"]))
        finish = str(dget(data, "candidates[0].finishReason", "") or "")
        return ChatResult(text="".join(texts).strip(),
                          usage=normalize_usage(data, cap),
                          endpoint_id=ep.id,
                          finish_reason=finish,
                          raw=data)

    def needs_truncation_retry(self, result: ChatResult) -> bool:
        return not result.text and result.finish_reason.upper() in ("MAX_TOKENS", "LENGTH")

    async def probe(self, ep: Endpoint, http) -> dict:
        root = ep.base_url.rstrip("/")
        headers = {"x-goog-api-key": ep.auth()} if ep.auth() else {}
        try:
            resp = await http.get(ep, root + "/models", headers=headers)
            resp.raise_for_status()
            items = (resp.json() or {}).get("models") or []
            names = [str(m.get("name") or "").split("/")[-1] for m in items
                     if isinstance(m, dict)]
            return {"models": len(names), "sample": names[:5]}
        except Exception as exc:  # noqa: BLE001
            return {"error": str(exc)[:150]}

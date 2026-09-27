"""声明式 Provider：由 JSON/TOML 清单驱动的通用适配器。

能覆盖的情况：**请求形状和 OpenAI 差不多、只是 URL / 鉴权 / 字段名不一样**的厂商。
写一份清单就能接进来，不用碰 Python：

    {"name": "kimi", "protocol": "declarative",
     "request": {"path": "/chat/completions",
                 "body": {"model": "$model", "messages": "$messages", ...}},
     "response": {"text": "$.choices[0].message.content"}}

字段模板里以 `$` 开头的是变量：
    $model  $messages  $temperature  $max_tokens  $stream  $api_key

覆盖不了的（system 位置不同、报文结构完全不同、需要签名）请写 Python 适配器，
见 `providers/plugins/README.md`。
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from .base import Provider, register_provider
from .types import Capability, ChatResult, Endpoint, Request, UsageKeys, dget, dset
from .usage import normalize_usage

VARS = ("$model", "$messages", "$temperature", "$max_tokens", "$stream", "$api_key")


def render(template: Any, ctx: dict) -> Any:
    """递归渲染字段模板。dict/list 逐层下钻，字符串整体匹配变量则替换。"""
    if isinstance(template, dict):
        return {k: render(v, ctx) for k, v in template.items()}
    if isinstance(template, list):
        return [render(v, ctx) for v in template]
    if isinstance(template, str) and template.startswith("$"):
        if template not in VARS:
            raise ValueError(f"未知模板变量 {template!r}，可用：{' '.join(VARS)}")
        return ctx[template[1:]]
    return template


def contains(template: Any, var: str) -> bool:
    if isinstance(template, dict):
        return any(contains(v, var) for v in template.values())
    if isinstance(template, list):
        return any(contains(v, var) for v in template)
    return template == var


def _collect_vars(template: Any) -> list[str]:
    """把模板里所有 `$xxx` 变量收集出来（给清单校验用）。"""
    out: list[str] = []

    def walk(t: Any) -> None:
        if isinstance(t, dict):
            for v in t.values():
                walk(v)
        elif isinstance(t, list):
            for v in t:
                walk(v)
        elif isinstance(t, str) and t.startswith("$"):
            out.append(t)

    walk(template)
    return out


@register_provider("declarative")
class DeclarativeProvider(Provider):
    """按 spec 走的通用适配器。spec 由 manifest.py 校验后传进来。"""

    name = "declarative"

    def __init__(self, spec: dict) -> None:
        self.spec = spec
        self.name = str(spec.get("name") or "declarative")
        self.display_name = str(spec.get("display_name") or self.name)

    # ── 能力 ──

    def default_capabilities(self) -> Capability:
        cap = Capability.from_dict(self.spec.get("capabilities"))
        usage_spec = self.spec.get("usage")
        if isinstance(usage_spec, dict):
            # 清单里 usage 与 capabilities 平级写更直观，这里合进能力声明
            cap = replace(cap, usage=UsageKeys.from_dict(usage_spec))
        return cap

    # ── 请求 ──

    def _auth(self, ep: Endpoint, headers: dict, params: dict) -> None:
        auth = self.spec.get("auth") or {}
        kind = str(auth.get("type") or "bearer").lower()
        key = ep.auth()
        if kind in ("none", ""):
            return
        if kind == "bearer":
            headers[auth.get("header") or "Authorization"] = f"{auth.get('prefix', 'Bearer ')}{key}"
        elif kind == "header":
            headers[str(auth.get("header") or "Authorization")] = f"{auth.get('prefix', '')}{key}"
        elif kind == "azure-key":
            headers[str(auth.get("header") or "api-key")] = key
        elif kind == "query":
            params[str(auth.get("param") or "key")] = key
        else:
            raise ValueError(f"未知鉴权类型：{kind}")

    def build_request(self, ep: Endpoint, messages: list, *, stream: bool,
                      max_tokens: int, temperature: float,
                      extra: dict | None) -> Request:
        cap = self.capability(ep)
        req_spec = self.spec.get("request") or {}
        path = str(req_spec.get("path") or "/chat/completions")
        url = ep.base_url.rstrip("/") + path
        method = str(req_spec.get("method") or "POST").upper()

        ctx = {
            "model": ep.model,
            "messages": messages,
            "temperature": cap.clamped_temperature(temperature),
            "max_tokens": int(max_tokens),
            "stream": bool(stream),
            "api_key": ep.auth(),
        }

        body = render(req_spec.get("body") or {}, ctx)
        if not contains(req_spec.get("body"), "$max_tokens"):
            # 清单没写 max_tokens 的位置，就按能力声明里的点路径塞进去
            dset(body, cap.max_tokens_key, int(max_tokens))

        # 额外的 query / body（清单里的固定字段 + 端点级 params）
        params: dict = dict(render(req_spec.get("query") or {}, ctx))
        for k, v in (ep.query or {}).items():
            params[k] = v
        for k, v in (req_spec.get("extra_body") or {}).items():
            body[k] = v
        for k, v in (ep.params or {}).items():
            if not k.startswith("_"):
                body[k] = v
        if extra:
            body.update(extra)

        headers = {"Content-Type": "application/json"}
        self._auth(ep, headers, params)
        headers.update(ep.headers or {})
        return Request(method=method, url=url, headers=headers, json_body=body,
                       params=params or None)

    # ── 响应 ──

    def parse_response(self, ep: Endpoint, data: dict) -> ChatResult:
        cap = self.capability(ep)
        resp_spec = self.spec.get("response") or {}
        text_path = str(resp_spec.get("text") or "$.choices[0].message.content")
        finish_path = str(resp_spec.get("finish_reason") or "")
        raw_text = dget(data, text_path, "")
        text = raw_text.strip() if isinstance(raw_text, str) else ("" if raw_text is None else str(raw_text))
        finish = str(dget(data, finish_path, "") or "") if finish_path else ""
        return ChatResult(text=text, usage=normalize_usage(data, cap),
                          endpoint_id=ep.id, finish_reason=finish, raw=data)

    # ── 探针 ──

    async def probe(self, ep: Endpoint, http) -> dict:
        probe = self.spec.get("probe") or {}
        path = probe.get("path")
        if not path:
            return {"unsupported": True}
        url = ep.base_url.rstrip("/") + str(path)
        headers: dict = {}
        params: dict = {}
        self._auth(ep, headers, params)
        try:
            resp = await http.get(ep, url, headers=headers or None, params=params or None)
            resp.raise_for_status()
            data = resp.json() or {}
        except Exception as exc:  # noqa: BLE001
            return {"error": str(exc)[:150]}
        kind = str(probe.get("parse") or "openai_models")
        if kind == "ollama_tags":
            items = data.get("models") or []
            return {"models": len(items), "sample": [str(m.get("name") or "") for m in items[:5]]}
        items = data.get("data") or []
        ids = [str(m.get("id") or "") for m in items if isinstance(m, dict)]
        return {"models": len(ids), "sample": ids[:5]}


def compile_manifest(raw: dict, *, manifest_path: str = "") -> list[DeclarativeProvider]:
    """把一份清单编译成若干 DeclarativeProvider。"""
    out: list[DeclarativeProvider] = []
    for item in raw.get("providers") or []:
        spec = dict(item)
        spec.setdefault("_manifest_path", manifest_path)
        out.append(DeclarativeProvider(spec))
    return out


__all__ = ["DeclarativeProvider", "compile_manifest", "render", "contains", "VARS",
           "UsageKeys", "_collect_vars"]

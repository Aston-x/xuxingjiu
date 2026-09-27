"""声明式清单的加载与校验。

原则：**坏清单不能拖垮启动**。任何一份清单有问题，只把它记进 errors 里跳过，
其余清单照常加载，进程照常起来。开源项目里用户手写 JSON 出错是常态。
"""

from __future__ import annotations

import json
import pathlib
from typing import Any

PROBE_PARSERS = ("openai_models", "ollama_tags", "none")
AUTH_TYPES = ("none", "bearer", "header", "azure-key", "query")


def load_file(path: pathlib.Path) -> tuple[list[dict], list[str]]:
    """读一份清单，返回 (provider 规格列表, 错误列表)。文件不存在也算错误（由调用方决定是否忽略）。"""
    errors: list[str] = []
    if not path.is_file():
        return [], [f"{path}：文件不存在"]
    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return [], [f"{path}：读不了（{exc}）"]
    try:
        if path.suffix.lower() == ".toml":
            import tomllib
            data: Any = tomllib.loads(raw_text)
        else:
            # 顺手容忍 // 注释（手写清单时很常见）
            data = json.loads(_strip_jsonc(raw_text))
    except Exception as exc:  # noqa: BLE001
        return [], [f"{path}：解析失败（{exc}）"]
    if not isinstance(data, dict):
        return [], [f"{path}：顶层必须是对象"]
    version = data.get("manifest_version", 1)
    if version != 1:
        return [], [f"{path}：manifest_version={version} 不认识（本版本只支持 1）"]
    items = data.get("providers")
    if not isinstance(items, list) or not items:
        return [], [f"{path}：providers 必须是非空数组"]

    specs: list[dict] = []
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            errors.append(f"{path}：providers[{i}] 不是对象")
            continue
        errs = validate(item)
        if errs:
            errors += [f"{path}：providers[{i}]（{item.get('name') or '未命名'}）{e}" for e in errs]
            continue
        spec = dict(item)
        spec.setdefault("_manifest_path", str(path))
        specs.append(spec)
    return specs, errors


def validate(item: dict) -> list[str]:
    """返回错误列表，空列表表示合法。"""
    errs: list[str] = []
    name = item.get("name")
    if not isinstance(name, str) or not name.strip():
        errs.append("缺少 name")
    elif not name.replace("_", "").replace("-", "").isalnum():
        errs.append(f"name 只能是字母数字加 -/_：{name!r}")

    proto = item.get("protocol", "declarative")
    if not isinstance(proto, str):
        errs.append("protocol 必须是字符串")

    auth = item.get("auth")
    if auth is not None:
        if not isinstance(auth, dict):
            errs.append("auth 必须是对象")
        elif str(auth.get("type") or "bearer").lower() not in AUTH_TYPES:
            errs.append(f"auth.type 不认识：{auth.get('type')!r}（可选 {'/'.join(AUTH_TYPES)}）")

    req = item.get("request")
    if req is not None:
        if not isinstance(req, dict):
            errs.append("request 必须是对象")
        else:
            if req.get("path") is not None and not isinstance(req.get("path"), str):
                errs.append("request.path 必须是字符串")
            if req.get("body") is not None and not isinstance(req.get("body"), dict):
                errs.append("request.body 必须是对象")

    resp = item.get("response")
    if resp is not None:
        if not isinstance(resp, dict):
            errs.append("response 必须是对象")
        elif resp.get("text") is not None and not isinstance(resp.get("text"), str):
            errs.append("response.text 必须是字符串路径")

    caps = item.get("capabilities")
    if caps is not None and not isinstance(caps, dict):
        errs.append("capabilities 必须是对象")

    probe = item.get("probe")
    if probe is not None:
        if not isinstance(probe, dict):
            errs.append("probe 必须是对象")
        elif str(probe.get("parse") or "openai_models") not in PROBE_PARSERS:
            errs.append(f"probe.parse 不认识：{probe.get('parse')!r}")

    # 模板变量拼错是最常见的坑，这里提前抓出来
    from .declarative import VARS, _collect_vars  # noqa: PLC0415
    try:
        for var in _collect_vars(req.get("body") if isinstance(req, dict) else None):
            if var not in VARS:
                errs.append(f"request.body 里出现未知模板变量 {var}（可用 {' '.join(VARS)}）")
    except Exception:  # noqa: BLE001
        pass
    return errs


def _strip_jsonc(text: str) -> str:
    """去掉 `//` 行注释，但不动字符串里的 //（比如 https://）。"""
    out: list[str] = []
    for line in text.splitlines():
        cut = -1
        in_str = False
        esc = False
        for i, ch in enumerate(line):
            if esc:
                esc = False
                continue
            if ch == "\\":
                esc = True
                continue
            if ch == '"':
                in_str = not in_str
                continue
            if not in_str and ch == "/" and i + 1 < len(line) and line[i + 1] == "/":
                cut = i
                break
        out.append(line[:cut] if cut >= 0 else line)
    return "\n".join(out)


def fixture(**overrides) -> dict:
    """给测试用的一份最小合法清单。"""
    base = {
        "name": "sample",
        "auth": {"type": "bearer"},
        "request": {"path": "/chat/completions", "method": "POST",
                    "body": {"model": "$model", "messages": "$messages",
                             "temperature": "$temperature", "max_tokens": "$max_tokens",
                             "stream": False}},
        "response": {"text": "$.choices[0].message.content",
                     "finish_reason": "$.choices[0].finish_reason"},
        "usage": {"prompt": "$.usage.prompt_tokens",
                  "completion": "$.usage.completion_tokens"},
    }
    base.update(overrides)
    return base

"""Router：选路、回退链、并发闸、用量汇总。

它是 `providers` 包对外的**唯一**执行入口 —— `bot.py` 里的 `Chat` / `Vision`
退化成薄门面，真正的活都在这儿。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import replace
from typing import Any, Callable, Iterator

from . import policy as policy_mod
from .base import Provider
from .registry import EndpointSet, Registry
from .transport import Transport
from .types import ChatResult, Endpoint

logger = logging.getLogger("providers.router")

# 失败原因字面量 —— `bot._reply_model_failure` 只认这几个，不要随意扩展
SOURCE_LOCAL = "local"
SOURCE_CLOUD = "cloud"
SOURCE_NO_KEY = "no-key"
SOURCE_FAILED = "failed"


def _safe_url(url: str) -> str:
    """把 URL 里的密钥抹掉再上屏。

    有些厂商（尤其 Gemini 的老写法）把 key 放在 `?key=` 里，而端点面板/日志会把
    base_url 原样显示 —— 不处理就等于**在控制台上明文展示密钥**。
    这里只保留查询参数的名字，值一律替成 `***`。
    """
    if not url or "?" not in url:
        return url
    head, _, query = url.partition("?")
    safe = []
    for piece in query.split("&"):
        if not piece:
            continue
        name, sep, _value = piece.partition("=")
        safe.append(f"{name}{sep}***" if sep else name)
    return head + "?" + "&".join(safe)


class ConcurrencyGate:
    """按端点限流。泛化自原来那个只盯着 local 的 `Chat.local_sem`。

    信号量**惰性创建**：重构前的实现是在 import 期就 `asyncio.Semaphore(...)`，
    那时还没有事件循环；惰性建可以避开这类坑。
    """

    def __init__(self) -> None:
        self._sems: dict[str, asyncio.Semaphore] = {}

    def sem(self, ep: Endpoint) -> asyncio.Semaphore | None:
        limit = int(ep.concurrency or 0)
        if limit <= 0:
            return None
        sem = self._sems.get(ep.id)
        if sem is None:
            sem = asyncio.Semaphore(limit)
            self._sems[ep.id] = sem
        return sem

    @contextlib.asynccontextmanager
    async def hold(self, ep: Endpoint):
        sem = self.sem(ep)
        if sem is None:
            yield
            return
        async with sem:
            yield

    def forget(self, endpoint_id: str) -> None:
        self._sems.pop(endpoint_id, None)

    def clear(self) -> None:
        self._sems.clear()

    def snapshot(self) -> dict[str, int]:
        return {k: v._value for k, v in self._sems.items()}  # noqa: SLF001


class Router:
    def __init__(self, registry: Registry, endpoints: EndpointSet | None = None,
                 *, transport: Transport | None = None, gate: ConcurrencyGate | None = None,
                 logger_: logging.Logger | None = None,
                 on_usage: Callable[[dict], None] | None = None) -> None:
        self.registry = registry
        self.http = transport or Transport()
        self.gate = gate or ConcurrencyGate()
        self.log = logger_ or logger
        self._on_usage = on_usage
        self._set = endpoints or EndpointSet()
        self.cfg: dict = {}
        self._probe_at: float = 0.0
        self._probe: dict = {}
        self._probe_error: str = ""
        self._last_notices: list[str] = []

    # ── 基础访问 ──

    @property
    def endpoints(self) -> list[Endpoint]:
        return list(self._set.endpoints)

    @property
    def chains(self) -> dict[str, list[str]]:
        return {k: list(v) for k, v in self._set.chains.items()}

    @property
    def notices(self) -> list[str]:
        return list(self._last_notices)

    def endpoint(self, ep_id: str) -> Endpoint | None:
        return self._set.by_id().get(ep_id)

    def provider_for(self, ep: Endpoint) -> Provider | None:
        return self.registry.get(ep.provider)

    def chain(self, name: str = "default") -> list[Endpoint]:
        by_id = self._set.by_id()
        ids = self._set.chains.get(name)
        if ids is None:
            ids = [e.id for e in self._set.endpoints]
        return [by_id[i] for i in ids if i in by_id]

    # ── 可用性 ──

    def skip_reason(self, ep: Endpoint) -> str:
        """空串 = 可用；否则返回跳过原因（`disabled` / `no-key` / `no-provider`）。"""
        if ep.require_enabled and not ep.enabled:
            return "disabled"
        if self.registry.get(ep.provider) is None:
            return "no-provider"
        if ep.tier == "cloud" and not ep.has_key():
            return "no-key"
        return ""

    def usable(self, ep: Endpoint) -> bool:
        return not self.skip_reason(ep)

    def capability_of(self, ep: Endpoint) -> Any:
        prov = self.registry.get(ep.provider)
        if prov is None:
            from .types import Capability
            return ep.capabilities or Capability()
        return prov.capability(ep)

    def vision_of(self, ep: Endpoint) -> bool:
        cap = self.capability_of(ep)
        return bool(cap.vision and cap.vision_input != "none")

    def vision_ok(self, tier: str) -> bool:
        return any(self.usable(e) and self.vision_of(e)
                   for e in self._set.endpoints if e.tier == tier)

    def vision_capable(self, ep: Endpoint) -> bool:
        """严格口径：端点**显式启用**且真的能收图（cloud 还要有密钥）。

        与 `usable()` 的区别是不认 `require_enabled` —— 那条只服务于
        「中继模式下 cloud 不看 cloud.enable」这个历史行为。面板上的
        `VISION.cloud_ok()/local_ok()` 用的是这个严格口径。
        """
        if not ep.enabled:
            return False
        if not self.vision_of(ep):
            return False
        if ep.tier == "cloud" and not ep.has_key():
            return False
        return True

    def answer_endpoint(self) -> Endpoint | None:
        """按 default 链，第一个**真的能答话**的端点。判据与 `answer()` 同源。"""
        for ep in self.chain("default"):
            if not self.skip_reason(ep):
                return ep
        return None

    def describe_endpoint(self) -> Endpoint | None:
        """谁负责把图转成文字（原「本地识图」）。"""
        for ep in self.chain("describe"):
            if not self.skip_reason(ep) and self.vision_of(ep):
                return ep
        return None

    def first_answerer(self) -> str:
        ep = self.answer_endpoint()
        return ep.tier if ep else ""

    def policy(self, cfg: dict | None = None) -> policy_mod.VisionPolicy:
        return policy_mod.build(
            cfg if cfg is not None else self.cfg,
            endpoints=self._set.endpoints,
            vision_capable=self.vision_capable,   # 严格口径，对齐旧的 cloud_ok/local_ok
            vision_of=self.vision_of,
            answer_endpoint=self.answer_endpoint,
            describe_endpoint=self.describe_endpoint,
        )

    def need_vision_relay(self, cfg: dict | None = None) -> bool:
        return self.policy(cfg).relay

    # ── 调用 ──

    def _usage_sink(self, result: ChatResult) -> None:
        if self._on_usage is None:
            return
        u = result.usage or {}
        if not any(u.values()):
            return
        self._on_usage({**u, "calls": 1, "endpoint": result.endpoint_id})

    async def call(self, ep: Endpoint, messages: list, *, max_tokens: int | None = None,
                   temperature: float | None = None, extra: dict | None = None,
                   path: str | None = None) -> ChatResult:
        """调一个端点。含「被 max_tokens 卡断就翻倍重试一次」。"""
        prov = self.registry.get(ep.provider)
        if prov is None:
            raise RuntimeError(f"端点 {ep.id} 的 provider={ep.provider!r} 没有注册")
        cap = prov.capability(ep)
        limit = int(max_tokens if max_tokens is not None else (ep.max_tokens or 1024))
        temp = cap.clamped_temperature(temperature if temperature is not None else 0.9)

        backend_can_see = self._backend_can_see()
        prepared = prov.prepare_messages(messages, ep, cap, backend_can_see=backend_can_see)

        first = await self._send(prov, ep, prepared, limit, temp, extra, path)
        if cap.supports_truncation_retry and prov.needs_truncation_retry(first):
            doubled = limit * 2
            if cap.max_tokens_limit:
                doubled = min(doubled, int(cap.max_tokens_limit))
            if doubled > limit:
                self.log.warning("回复被 max_tokens=%d 截断，翻倍重试一次", limit)
                retry = await self._send(prov, ep, prepared, doubled, temp, extra, path)
                if retry.text:
                    return retry
                return first
        return first

    async def _send(self, prov: Provider, ep: Endpoint, messages: list, max_tokens: int,
                    temperature: float, extra: dict | None, path: str | None) -> ChatResult:
        target = ep
        if path:
            # `_path` 是协议内部用的路径覆盖（以 _ 开头，不会被当成 body 字段发出去）
            target = replace(ep, params={**(ep.params or {}), "_path": path})
        req = prov.build_request(target, messages, stream=False, max_tokens=max_tokens,
                                 temperature=temperature, extra=extra)
        async with self.gate.hold(ep):
            resp = await self.http.send(ep, req, timeout=ep.timeout)
        result = prov.parse_response(ep, resp.json() or {})
        result.endpoint_id = ep.id
        self._usage_sink(result)
        return result

    def _backend_can_see(self) -> bool:
        """给消息预处理用：当前是否**有**端点能看图（决定占位措辞，见 base.VISION_PLACEHOLDER_*）。

        判据与 `Vision.usable()` 同源 —— 措辞说反了她会把刚识图拿到的描述也一起否认掉。
        """
        if self.cfg:
            return self.policy().usable
        return any(self.usable(e) and self.vision_of(e) for e in self._set.endpoints)

    async def answer(self, messages: list, chain: str = "default") -> tuple[str | None, str]:
        """沿回退链依次试。返回 (文本, source)，source ∈ {local, cloud, no-key, failed}。

        `no-key` 会**短路**：只要链上第一个被跳过的原因是「没密钥」，就直接返回 no-key，
        不再往后试 —— 这是重构前 `Chat.answer` 的行为，改动它会让「本地有模型但没配云端 key」
        的用户突然从 no-key 变成 local，管理员收到的故障原因也跟着变。
        """
        for ep in self.chain(chain):
            reason = self.skip_reason(ep)
            if reason == "no-key":
                return None, SOURCE_NO_KEY
            if reason:
                continue
            try:
                result = await self.call(ep, messages)
            except Exception as exc:  # noqa: BLE001
                self.log.warning("%s 端点 %s 失败：%s", ep.tier, ep.id, exc)
                continue
            if result.text:
                if ep.tier not in (SOURCE_LOCAL, SOURCE_CLOUD):
                    self.log.warning("端点 %s 的 tier=%r 不是 local/cloud，按 cloud 记账", ep.id, ep.tier)
                return result.text, (ep.tier if ep.tier in (SOURCE_LOCAL, SOURCE_CLOUD)
                                     else SOURCE_CLOUD)
        return None, SOURCE_FAILED

    async def describe(self, images: list[str], prompt: str, *, chain: str = "vision",
                       max_tokens: int = 300) -> str | None:
        """识图转述：沿链找第一个能看图的端点，把图变成文字。"""
        if not images:
            return None
        content: list = [{"type": "text", "text": prompt}]
        for u in images:
            content.append({"type": "image_url", "image_url": {"url": u}})
        tried: list[str] = []
        for ep in self.chain(chain):
            if self.skip_reason(ep) or not self.vision_of(ep):
                continue
            try:
                result = await self.call(ep, [{"role": "user", "content": content}],
                                         max_tokens=max_tokens)
            except Exception as exc:  # noqa: BLE001
                self.log.warning("识图端 %s 失败：%s", ep.id, exc)
                tried.append(ep.id)
                continue
            text = (result.text or "").strip()
            if text:
                return text
            tried.append(ep.id)
        if tried:
            self.log.warning("识图没成（试过 %s）", "、".join(tried))
        return None

    async def describe_on(self, ep: Endpoint, images: list[str], prompt: str,
                          *, max_tokens: int = 300) -> str | None:
        """指定端点做识图（给 `describe` 之外的特殊路径用）。"""
        content: list = [{"type": "text", "text": prompt}]
        for u in images:
            content.append({"type": "image_url", "image_url": {"url": u}})
        result = await self.call(ep, [{"role": "user", "content": content}],
                                 max_tokens=max_tokens)
        return (result.text or "").strip() or None

    # ── 探针 ──

    def probe_cached(self) -> dict:
        return dict(self._probe)

    async def probe_all(self, cfg: dict, force: bool = False) -> dict:
        """探所有端点。结果缓存在内存里（`status()` 只能读缓存，不许联网）。"""
        ttl = float((cfg.get("vision") or {}).get("local_probe_seconds", 300))
        now = time.time()
        if not force and self._probe_at and now - self._probe_at < ttl:
            return dict(self._probe)

        by_id: dict[str, dict] = {}
        errors: list[str] = []
        for ep in self._set.endpoints:
            prov = self.registry.get(ep.provider)
            if prov is None:
                by_id[ep.id] = {"error": f"provider {ep.provider!r} 未注册"}
                continue
            try:
                got = await prov.probe(ep, self.http)
            except Exception as exc:  # noqa: BLE001
                got = {"error": str(exc)[:150]}
            by_id[ep.id] = dict(got or {})

        legacy = {}
        for ep in self._set.endpoints:
            if ep.tier == "local":
                legacy = dict(by_id.get(ep.id) or {})
                legacy["at"] = now
                break
        err = ""
        for ep in self._set.endpoints:
            if ep.tier == "local":
                err = str((by_id.get(ep.id) or {}).get("error") or "")
                break
        self._probe = {"at": now, "endpoints": by_id, "local": legacy}
        self._probe_at = now
        self._probe_error = err
        if err:
            self.log.warning("探测本地模型失败：%s", err)
        _ = errors
        return dict(self._probe)

    def probe_error(self) -> str:
        return self._probe_error

    # ── 面板快照 ──

    def status(self, cfg: dict) -> dict:
        """给 Web 控制台的只读快照：**同步、不联网**（探针结果用缓存）。"""
        pol = self.policy(cfg)
        eps = []
        for ep in self._set.endpoints:
            cap = self.capability_of(ep)
            eps.append({
                "id": ep.id,
                "provider": ep.provider,
                "display_name": ep.display_name or ep.provider,
                "base_url": ep.base_url,
                "model": ep.model,
                "tier": ep.tier,
                "enabled": bool(ep.enabled),
                "usable": self.usable(ep),
                "skip_reason": self.skip_reason(ep),
                "has_key": ep.has_key(),
                "key_source": ("env:" + ep.api_key_env) if (ep.api_key_env and ep.has_key())
                              else ("plain" if ep.api_key else ""),
                "loopback": ep.is_loopback,
                "concurrency": int(ep.concurrency or 0),
                "base_url": _safe_url(ep.base_url),
                "vision": bool(cap.vision and cap.vision_input != "none"),
                "capabilities": {
                    "streaming": cap.streaming,
                    "tool_calling": cap.tool_calling,
                    "thinking": cap.thinking,
                    "max_tokens_key": cap.max_tokens_key,
                },
                "probe": ((self._probe.get("endpoints") or {}).get(ep.id) or {}),
            })
        return {
            "ok": True,
            "legacy_config": bool(self._set.legacy),
            "chains": self.chains,
            "endpoints": eps,
            "policy": pol.as_dict(),
            "probe_at": self._probe_at,
            "probe_error": self._probe_error,
            "registry": self.registry.names(),
        }

    # ── 重载 ──

    def reload(self, cfg: dict) -> list[str]:
        """配置变了之后**必须**调它 —— 否则缓存的端点 / 信号量 / key 全是陈旧的。

        典型症状：改了 api_key 不生效、改了 concurrency 不生效、删掉的端点还在打旧地址。
        """
        old_ids = {e.id for e in self._set.endpoints}
        self.cfg = cfg
        self._set = self.registry.build_endpoints(cfg)
        new_ids = {e.id for e in self._set.endpoints}
        for gone in old_ids - new_ids:
            self.gate.forget(gone)
        self._last_notices = list(self._set.notices)
        for n in self._last_notices:
            self.log.warning("[providers] %s", n)
        return list(self._last_notices)

    def load_notices(self, cfg: dict) -> list[str]:
        """首次构建用（与 reload 同义，只是语义更清楚的别名）。"""
        return self.reload(cfg)

    async def aclose(self) -> None:
        await self.http.aclose()


def iter_endpoint_ids(endpoints: list[Endpoint]) -> Iterator[str]:
    for ep in endpoints:
        yield ep.id

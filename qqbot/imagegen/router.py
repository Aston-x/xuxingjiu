"""生图路由：回退链、并发闸、负缓存、成本控制、探针、只读快照。

对上层只暴露两件事：
  · `check_ready(purpose)` —— **零请求**回答"现在能不能画"（能不能 + 为什么不能）
  · `generate(chain, req)` —— 沿链试，返回 GenOutcome（含失败原因，供提示与通知）

设计要点：**能不画就提前说**。云端不真探针（可能计费），靠配置判定 + 负缓存兜；
本地用廉价探针（列模型）判断，结果带 TTL。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from providers.router import ConcurrencyGate
from providers.transport import Transport
from .base import ImageProvider
from .registry import ImageEndpointSet, ImageRegistry
from .types import (
    REASON_BACKEND_DOWN,
    REASON_DISABLED,
    REASON_EMPTY,
    REASON_FAILED,
    REASON_HTTP,
    REASON_NO_ENDPOINT,
    REASON_QUOTA,
    REASON_TIMEOUT,
    GenOutcome,
    GenRequest,
    ImageEndpoint,
    ImageResult,
)

logger = logging.getLogger("imagegen.router")

# 负缓存退避：本地后端刚挂了多半是 WebUI 在重载模型，60 秒内别反复撞；
# 云端失败给长一点，避免连续烧钱重试。
BACKOFF_LOCAL = 60.0
BACKOFF_CLOUD = 180.0


@dataclass
class _Down:
    until: float = 0.0
    reason: str = ""
    detail: str = ""


class ImageRouter:
    def __init__(self, registry: ImageRegistry, endpoints: ImageEndpointSet | None = None,
                 *, transport: Transport | None = None, gate: ConcurrencyGate | None = None,
                 cfg: dict | None = None, logger_: logging.Logger | None = None,
                 state_path: Path | None = None) -> None:
        self.registry = registry
        self.http = transport or Transport()
        self.gate = gate or ConcurrencyGate()
        self.log = logger_ or logger
        self._set = endpoints or ImageEndpointSet()
        self.cfg: dict = cfg or {}
        self._down: dict[str, _Down] = {}
        self._probe: dict = {}
        self._probe_at: float = 0.0
        self.state_path = state_path
        self._state: dict = {"day": "", "cloud": {}}
        self._load_state()

    # ── 基础访问 ──

    @property
    def endpoints(self) -> list[ImageEndpoint]:
        return list(self._set.endpoints)

    @property
    def chains(self) -> dict[str, list[str]]:
        return {k: list(v) for k, v in self._set.chains.items()}

    @property
    def legacy(self) -> bool:
        return bool(self._set.legacy)

    def endpoint(self, ep_id: str) -> ImageEndpoint | None:
        return self._set.by_id().get(ep_id)

    def provider_for(self, ep: ImageEndpoint) -> ImageProvider | None:
        return self.registry.get(ep.provider)

    def chain(self, name: str = "image") -> list[ImageEndpoint]:
        by_id = self._set.by_id()
        ids = self._set.chains.get(name)
        if ids is None:
            ids = [e.id for e in self._set.endpoints]
        return [by_id[i] for i in ids if i in by_id]

    def any_enabled(self) -> bool:
        return any(ep.enabled for ep in self._set.endpoints)

    # ── 云端每日额度 ──

    def _today(self) -> str:
        return time.strftime("%Y-%m-%d")

    def _load_state(self) -> None:
        if not self.state_path:
            return
        try:
            import json
            obj = json.loads(self.state_path.read_text(encoding="utf-8"))
            if isinstance(obj, dict):
                self._state = {"day": str(obj.get("day") or ""),
                               "cloud": dict(obj.get("cloud") or {})}
        except Exception:  # noqa: BLE001
            self._state = {"day": "", "cloud": {}}

    def _save_state(self) -> None:
        if not self.state_path:
            return
        try:
            import json  # noqa: PLC0415
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self._state, ensure_ascii=False, indent=2),
                           encoding="utf-8")
            tmp.replace(self.state_path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("生图额度落盘失败：%s", exc)

    def cloud_used(self, ep_id: str) -> int:
        if self._state.get("day") != self._today():
            return 0
        return int((self._state.get("cloud") or {}).get(ep_id) or 0)

    def cloud_mark(self, ep_id: str) -> None:
        day = self._today()
        if self._state.get("day") != day:
            self._state = {"day": day, "cloud": {}}
        cloud = self._state.setdefault("cloud", {})
        cloud[ep_id] = int(cloud.get(ep_id) or 0) + 1
        self._save_state()

    def cloud_unmark(self, ep_id: str) -> None:
        if self._state.get("day") != self._today():
            return
        cloud = self._state.get("cloud") or {}
        if ep_id in cloud:
            cloud[ep_id] = max(0, int(cloud[ep_id]) - 1)
            self._save_state()

    def _cloud_cap(self, ep: ImageEndpoint) -> int:
        """云端每日上限：端点显式 `max_per_day` > 全局 `imagegen.cloud.max_per_day` > 0（不限）。"""
        own = ep.params.get("_max_per_day")
        if own is not None:
            return int(own)
        return int((self.cfg.get("imagegen") or {}).get("cloud", {}).get("max_per_day") or 0)

    # ── 负缓存 ──

    def mark_down(self, ep: ImageEndpoint, reason: str, detail: str = "") -> None:
        backoff = BACKOFF_LOCAL if ep.tier == "local" else BACKOFF_CLOUD
        self._down[ep.id] = _Down(until=time.time() + backoff, reason=reason, detail=detail)

    def mark_up(self, ep: ImageEndpoint) -> None:
        self._down.pop(ep.id, None)

    def down_info(self, ep: ImageEndpoint) -> _Down | None:
        d = self._down.get(ep.id)
        if d and d.until > time.time():
            return d
        return None

    def clear_negative_cache(self) -> None:
        """「重载配置」按钮会调它 —— 用户刚把 SD 重启了，不该再被负缓存挡住。"""
        self._down.clear()

    # ── 可用性 ──

    def skip_reason(self, ep: ImageEndpoint, *, purpose: str = "group",
                    low_spec: bool = False) -> str:
        """空串 = 可用。返回 `disabled` / `no-provider` / `unavailable` /
        `backend-down` / `over-quota` / `cloud-skipped`。"""
        if not ep.enabled:
            return "disabled"
        prov = self.registry.get(ep.provider)
        if prov is None:
            return "no-provider"
        ok, _why = prov.available(ep)
        if not ok:
            return "unavailable"
        if ep.tier == "cloud":
            if check := self._cloud_allowed(ep, low_spec):
                return check
            cap = self._cloud_cap(ep)
            if cap > 0 and self.cloud_used(ep.id) >= cap:
                return "over-quota"
        if self.down_info(ep) is not None:
            return "backend-down"
        return ""

    def _cloud_allowed(self, ep: ImageEndpoint, low_spec: bool) -> str:
        """`allow_when`: always / low-spec / never —— 云端要花钱，默认仍给 always。"""
        policy = str((self.cfg.get("imagegen") or {}).get("cloud", {}).get("allow_when")
                     or "always").lower()
        if policy == "never":
            return "cloud-skipped"
        if policy == "low-spec" and not low_spec:
            return "cloud-skipped"
        return ""

    def usable(self, ep: ImageEndpoint, **kw) -> bool:
        return not self.skip_reason(ep, **kw)

    # ── 零请求就绪检查 ──

    def check_ready(self, purpose: str = "group", *, low_spec: bool = False,
                    enabled: bool = True, has_intent: bool = True) -> tuple[bool, str, str]:
        """**不发任何请求**回答「现在能不能画」。

        返回 (ok, reason, why)。`reason` 用 `types.py` 里的字面量：
        正常态（关着 / 额度满 / 没说要画）上层应静默；故障态（没端点 / 后端刚挂）
        上层应给用户一句失败话 + 通知管理员。
        """
        if not enabled:
            return False, REASON_DISABLED, "生图总闸关着（sd.enable / imagegen.enable）"
        if not has_intent:
            return False, REASON_EMPTY, "没给要画什么"
        eps = [ep for ep in self.chain("image")]
        if not eps:
            return False, REASON_NO_ENDPOINT, "一个生图端点都没配（imagegen.endpoints 是空的）"
        if not any(ep.enabled for ep in eps):
            return False, REASON_DISABLED, "链上所有生图端点都被停用了"
        blocked: list[str] = []
        for ep in eps:
            reason = self.skip_reason(ep, purpose=purpose, low_spec=low_spec)
            if not reason:
                return True, "", ""
            blocked.append(f"{ep.id}:{reason}")
        if any("backend-down" in b for b in blocked):
            ep = eps[0]
            d = self.down_info(ep)
            why = (d.detail if d and d.detail else "后端刚失败过，正在退避")
            return False, REASON_BACKEND_DOWN, f"{ep.id} {why}"
        if any("over-quota" in b for b in blocked):
            return False, REASON_QUOTA, "云端生图今日额度已用完"
        return False, REASON_NO_ENDPOINT, "链上没有可用端点（" + "，".join(blocked) + "）"

    # ── 出图 ──

    async def generate(self, req: GenRequest, chain: str = "image") -> GenOutcome:
        eps = self.chain(chain)
        low_spec = _is_low_spec(req)
        tried: list[str] = []
        last_reason, last_detail = REASON_FAILED, "没有可用端点"
        for ep in eps:
            reason = self.skip_reason(ep, purpose=req.purpose, low_spec=low_spec)
            if reason:
                if reason == "backend-down":
                    last_reason = REASON_BACKEND_DOWN
                    d = self.down_info(ep)
                    last_detail = f"{ep.id} {d.detail if d else '退避中'}"
                elif reason == "over-quota":
                    last_reason, last_detail = REASON_QUOTA, f"{ep.id} 超过今日上限"
                else:
                    last_detail = f"{ep.id} {reason}"
                tried.append(f"{ep.id}({reason})")
                continue
            prov = self.registry.get(ep.provider)
            if prov is None:
                continue
            prepared = prov.prepare(ep, req)
            if prepared.params.get("_note"):
                self.log.info("[imagegen] %s：%s", ep.id, prepared.params["_note"])
            counted = False
            if ep.tier == "cloud":
                self.cloud_mark(ep.id)
                counted = True
            try:
                async with self.gate.hold(ep):
                    result = await prov.run(ep, prepared, self.http)
            except Exception as exc:  # noqa: BLE001
                reason_c, detail = _classify(exc)
                if counted:
                    self.cloud_unmark(ep.id)
                self.mark_down(ep, reason_c, detail)
                self.log.warning("[imagegen] %s 出图失败（%s）：%s", ep.id, reason_c, detail)
                tried.append(f"{ep.id}({reason_c})")
                last_reason, last_detail = reason_c, f"{ep.id} {detail}"
                continue
            if result and result.data:
                self.mark_up(ep)
                if len(result.data) > (prov.capability(ep).max_bytes or 0) > 0:
                    # 理论上适配器已经在下载时就拦了，这里再兜一层
                    if counted:
                        self.cloud_unmark(ep.id)
                    last_reason, last_detail = "too-large", f"{ep.id} 图片超过上限"
                    tried.append(f"{ep.id}(too-large)")
                    continue
                return GenOutcome(ok=True, data=result.data, endpoint_id=ep.id,
                                  tried=tried, meta=result.meta)
            if counted:
                self.cloud_unmark(ep.id)
            self.mark_down(ep, REASON_HTTP, "返回里没有图片字节")
            tried.append(f"{ep.id}(empty)")
            last_reason, last_detail = REASON_HTTP, f"{ep.id} 返回里没有图片字节"
        return GenOutcome(ok=False, reason=last_reason, detail=last_detail, tried=tried)

    # ── 探针 ──

    def probe_cached(self) -> dict:
        return dict(self._probe)

    async def probe_all(self, *, force: bool = False) -> dict:
        ttl = float((self.cfg.get("imagegen") or {}).get("probe_seconds", 300))
        now = time.time()
        if not force and self._probe_at and now - self._probe_at < ttl:
            return dict(self._probe)
        by_id: dict[str, dict] = {}
        for ep in self._set.endpoints:
            prov = self.registry.get(ep.provider)
            if prov is None:
                by_id[ep.id] = {"error": f"provider {ep.provider!r} 未注册"}
                continue
            ok, why = prov.available(ep)
            if not ok:
                by_id[ep.id] = {"error": why}
                continue
            try:
                got = await prov.probe(ep, self.http)
            except Exception as exc:  # noqa: BLE001
                got = {"error": str(exc)[:150]}
            d = self.down_info(ep)
            if d:
                got = {**got, "down": d.reason, "down_until": round(d.until)}
            by_id[ep.id] = dict(got or {})
        self._probe = {"at": now, "endpoints": by_id}
        self._probe_at = now
        return dict(self._probe)

    # ── 面板快照（同步、不联网） ──

    def status(self) -> dict:
        eps = []
        for ep in self._set.endpoints:
            prov = self.registry.get(ep.provider)
            cap = prov.capability(ep) if prov else ep.image
            d = self.down_info(ep)
            eps.append({
                "id": ep.id,
                "provider": ep.provider,
                "display_name": ep.display_name or ep.provider,
                "tier": ep.tier,
                "enabled": bool(ep.enabled),
                "usable": self.usable(ep),
                "skip_reason": self.skip_reason(ep),
                "has_key": ep.has_key(),
                "key_source": ("env:" + ep.api_key_env) if (ep.api_key_env and ep.has_key())
                              else ("plain" if ep.api_key else ""),
                "base_url": ep.base_url,
                "timeout": ep.timeout,
                "cloud_cap": self._cloud_cap(ep),
                "cloud_used": self.cloud_used(ep.id),
                "down": ({"reason": d.reason, "detail": d.detail,
                          "seconds_left": max(0, round(d.until - time.time()))} if d else None),
                "capability": {
                    "free_size": cap.free_size,
                    "negative": cap.negative,
                    "hires": cap.hires,
                    "returns": cap.returns,
                    "moderation": cap.moderation,
                    "note": cap.note,
                },
                "probe": ((self._probe.get("endpoints") or {}).get(ep.id) or {}),
            })
        return {
            "ok": True,
            "legacy_config": self.legacy,
            "chains": self.chains,
            "endpoints": eps,
            "registry": self.registry.names(),
            "probe_at": self._probe_at,
            "cloud_policy": str((self.cfg.get("imagegen") or {}).get("cloud", {})
                                .get("allow_when") or "always"),
            "ready": self.check_ready()[0],
        }

    # ── 重载 ──

    def reload(self, cfg: dict) -> list[str]:
        """配置变了必须调它：端点 / 链 / 密钥 / 信号量 / 负缓存 一起刷新。

        ⚠️ **不清角色字段**（形象 / 数量词 / 尺寸由 SD 门面管）——
        重读它们会让"这次画的尺寸和上一张不一样"，很难查。
        """
        old_ids = {e.id for e in self._set.endpoints}
        self.cfg = cfg
        self._set = self.registry.build_endpoints(cfg)
        for gone in old_ids - {e.id for e in self._set.endpoints}:
            self.gate.forget(gone)
            self._down.pop(gone, None)
        self._load_state()
        for n in self._set.notices:
            self.log.warning("[imagegen] %s", n)
        return list(self._set.notices)

    def load_cfg(self, cfg: dict) -> list[str]:
        return self.reload(cfg)

    async def aclose(self) -> None:
        await self.http.aclose()


def _is_low_spec(req: GenRequest) -> bool:
    """像素数 ≤ 1024×1024 且没开 hires，才算「低规格」（云端省钱策略用它）。"""
    return int(req.width) * int(req.height) <= 1024 * 1024


def _classify(exc: BaseException) -> tuple[str, str]:
    """把异常翻译成可读原因。httpx 的异常文本太长，这里只留关键信息。"""
    name = type(exc).__name__
    text = str(exc)[:200]
    if "Timeout" in name or "timeout" in text.lower():
        return REASON_TIMEOUT, f"请求超时（{text}）"
    if name in ("ConnectError", "ConnectTimeout", "NetworkError", "ReadError"):
        return "backend-down", f"连不上（{text}）"
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status:
        return REASON_HTTP, f"HTTP {status}（{text}）"
    return REASON_FAILED, f"{name}: {text}"


@dataclass
class ReadyInfo:
    ok: bool = False
    reason: str = ""
    why: str = ""
    extra: dict = field(default_factory=dict)


def make_ready_probe(fn: Callable[..., tuple[bool, str, str]]) -> Callable[..., ReadyInfo]:
    """把 `check_ready` 包成 ReadyInfo（给需要结构化返回的调用方）。"""
    def _call(*a, **kw) -> ReadyInfo:
        ok, reason, why = fn(*a, **kw)
        return ReadyInfo(ok=ok, reason=reason, why=why)
    return _call


__all__ = ["ImageRouter", "ReadyInfo", "make_ready_probe", "BACKOFF_LOCAL", "BACKOFF_CLOUD",
           "Any", "ImageResult"]

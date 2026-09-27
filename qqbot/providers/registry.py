"""注册中心：三路装载 + 旧配置迁移。

装载顺序（先来后到，同名不覆盖）：
    1. 内置适配器（`providers/adapters/*`）
    2. 声明式清单（`config.providers.plugins.manifests` 指向的 JSON/TOML）
    3. Python 插件（`providers/plugins/*.py`，白名单控制）

同名冲突默认**跳过并记录**，不静默覆盖、不抛异常 —— 用户装了个名字撞车的插件
不应该把内置协议改坏，也不应该让进程起不来。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from .base import Provider, register_provider, registered_hooks  # noqa: F401
from .types import Capability, Endpoint, UsageKeys

logger = logging.getLogger("providers.registry")

LEGACY_LOCAL_ID = "local-default"
LEGACY_CLOUD_ID = "cloud-default"

# 旧配置的 cloud 端点默认按 DeepSeek 处理（它的 usage 带前缀缓存计费字段）
DEEPSEEK_USAGE = UsageKeys(prompt="usage.prompt_tokens",
                          completion="usage.completion_tokens",
                          cache_hit="usage.prompt_cache_hit_tokens",
                          cache_miss="usage.prompt_cache_miss_tokens")


@dataclass
class LoadReport:
    """一次装载的结果。**任何一条都不该让进程起不来。**"""

    source: str = ""
    loaded: list[str] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    errors: list[tuple[str, str]] = field(default_factory=list)

    def merge(self, other: "LoadReport") -> None:
        self.loaded += other.loaded
        self.skipped += other.skipped
        self.errors += other.errors

    def summary(self) -> str:
        bits = [f"{self.source or '装载'}：{len(self.loaded)} 个"]
        if self.skipped:
            bits.append(f"跳过 {len(self.skipped)}")
        if self.errors:
            bits.append(f"错误 {len(self.errors)}")
        return "｜".join(bits)

    def warnings(self) -> list[str]:
        out = [f"跳过 {n}：{why}" for n, why in self.skipped]
        out += [f"出错 {n}：{why}" for n, why in self.errors]
        return out


@dataclass
class EndpointSet:
    """`build_endpoints` 的产物。"""

    endpoints: list[Endpoint] = field(default_factory=list)
    chains: dict[str, list[str]] = field(default_factory=dict)
    notices: list[str] = field(default_factory=list)
    legacy: bool = False          # 是否是旧版 local/cloud 推导出来的

    def by_id(self) -> dict[str, Endpoint]:
        return {ep.id: ep for ep in self.endpoints}


class Registry:
    def __init__(self) -> None:
        self._providers: dict[str, Provider] = {}
        self.reports: list[LoadReport] = []

    # ── 装载 ──

    def register(self, name: str, provider: Provider, *, override: bool = False) -> bool:
        if name in self._providers and not override:
            return False
        self._providers[name] = provider
        return True

    def load_builtin(self) -> LoadReport:
        """导入 adapters 包触发 @register_provider 副作用。"""
        rep = LoadReport(source="内置适配器")
        try:
            from . import adapters  # noqa: F401,PLC0415  导入即注册
        except Exception as exc:  # noqa: BLE001
            rep.errors.append(("<adapters>", f"导入失败：{exc}"))
            self.reports.append(rep)
            return rep
        for name, cls, _prio in registered_hooks():
            try:
                obj = cls() if not _needs_spec(cls) else None  # type: ignore[call-arg]
            except Exception as exc:  # noqa: BLE001
                rep.errors.append((name, f"实例化失败：{exc}"))
                continue
            if obj is None:
                continue      # 需要清单的适配器（declarative）由 load_manifests 提供
            obj.name = obj.name or name
            if self.register(name, obj):
                rep.loaded.append(name)
            else:
                rep.skipped.append((name, "名字已被占用"))
        self.reports.append(rep)
        return rep

    def load_manifests(self, paths: list[str | Path], *, base_dir: Path | None = None) -> LoadReport:
        from .manifest import load_file  # noqa: PLC0415
        from .declarative import DeclarativeProvider  # noqa: PLC0415

        rep = LoadReport(source="声明式清单")
        for raw in paths:
            p = Path(raw)
            if not p.is_absolute() and base_dir is not None:
                p = base_dir / p
            specs, errors = load_file(p)
            for err in errors:
                rep.errors.append((p.name, err))
            for spec in specs:
                name = str(spec.get("name"))
                provider = DeclarativeProvider(spec)
                if self.register(name, provider):
                    rep.loaded.append(name)
                else:
                    rep.skipped.append((name, "名字已被占用"))
        self.reports.append(rep)
        return rep

    def get(self, name: str) -> Provider | None:
        return self._providers.get(name)

    def names(self) -> list[str]:
        return sorted(self._providers)

    def summary(self) -> str:
        return "、".join(self.names()) or "（空）"

    # ── 端点构建 ──

    def build_endpoints(self, cfg: dict, *, notices: bool = True,
                        base_dir: Path | None = None) -> EndpointSet:
        prov = cfg.get("providers") or {}
        raw = prov.get("endpoints")
        if isinstance(raw, list) and raw:
            out = self._build_modern(raw, prov)
            out.legacy = False
        else:
            out = self._build_legacy(cfg)
            out.legacy = True
        if notices:
            out.notices += self._key_notices(out.endpoints)
        self._validate(out)
        return out

    def _build_modern(self, raw: list, prov: dict) -> EndpointSet:
        out = EndpointSet()
        for i, item in enumerate(raw):
            if not isinstance(item, dict):
                out.notices.append(f"providers.endpoints[{i}] 不是对象，已跳过")
                continue
            ep = Endpoint.from_dict(item, index=i)
            if ep.provider not in self._providers:
                out.notices.append(
                    f"端点 {ep.id} 的 provider={ep.provider!r} 没注册（可用：{self.summary()}），已跳过")
                continue
            out.endpoints.append(ep)
        chains = {k: [str(x) for x in v] for k, v in (prov.get("chains") or {}).items()
                  if isinstance(v, list)}
        if "default" not in chains:
            chains["default"] = [ep.id for ep in out.endpoints if ep.enabled]
        for name, ids in chains.items():
            unknown = [i for i in ids if i not in {e.id for e in out.endpoints}]
            for u in unknown:
                out.notices.append(f"回退链 {name} 里的端点 {u!r} 不存在，已忽略")
            chains[name] = [i for i in ids if i not in unknown]
        out.chains = chains
        return out

    def _build_legacy(self, cfg: dict) -> EndpointSet:
        """旧版顶层 local/cloud 简写 → 端点 + 回退链。语义逐条对齐重构前的 Chat.answer。"""
        local = cfg.get("local") or {}
        cloud = cfg.get("cloud") or {}
        out = EndpointSet()
        notices: list[str] = []

        if local.get("base_url") or local.get("model"):
            ep = Endpoint(
                id=LEGACY_LOCAL_ID,
                provider="lmstudio" if "1234" in str(local.get("base_url") or "") else "openai",
                display_name="本地模型（旧配置 local）",
                base_url=str(local.get("base_url") or ""),
                model=str(local.get("model") or ""),
                api_key=str(local.get("api_key") or ""),
                api_key_env=str(local.get("api_key_env") or ""),
                enabled=bool(local.get("enable", True)),
                tier="local",
                concurrency=int(local.get("concurrency", 0) or 0),
                timeout=float(local.get("timeout_seconds", 120) or 120),
                max_tokens=int(local.get("max_tokens", 1024) or 1024),
                vision=bool(local.get("vision")) if local.get("vision") is not None else None,
            )
            out.endpoints.append(ep)
            notices.append(
                f'local  -> "{ep.id}"（{ep.provider}，{ep.base_url}，tier=local，'
                f"并发 {ep.concurrency or '不限'}）")

        if cloud.get("base_url") or cloud.get("model"):
            caps = Capability(usage=DEEPSEEK_USAGE) if "deepseek" in str(
                cloud.get("base_url") or "").lower() else None
            params = {}
            if cloud.get("thinking"):
                params["thinking"] = {"type": cloud.get("thinking")}
            ep = Endpoint(
                id=LEGACY_CLOUD_ID,
                provider="openai",
                display_name="云端模型（旧配置 cloud）",
                base_url=str(cloud.get("base_url") or ""),
                model=str(cloud.get("model") or ""),
                api_key=str(cloud.get("api_key") or ""),
                api_key_env=str(cloud.get("api_key_env") or ""),
                enabled=bool(cloud.get("enable", True)),
                tier="cloud",
                timeout=float(cloud.get("timeout_seconds", 90) or 90),
                max_tokens=int(cloud.get("max_tokens", 1024) or 1024),
                vision=bool(cloud.get("vision")) if cloud.get("vision") is not None else None,
                params=params,
                capabilities=caps,
            )
            out.endpoints.append(ep)
            notices.append(
                f'cloud  -> "{ep.id}"（{ep.provider}，{ep.base_url}，tier=cloud）')

        out.chains = self._legacy_chains(cfg, out.endpoints)
        out.notices = notices
        if out.endpoints:
            out.notices.append(
                "建议迁移到 providers 段（支持多端点 + 回退链 + 能力声明），见 docs/providers.md"
                "#从旧版-localcloud-迁移；旧键仍会继续生效。")
        return out

    @staticmethod
    def _legacy_chains(cfg: dict, endpoints: list[Endpoint]) -> dict[str, list[str]]:
        """把 vision_relay / vision.backend 翻译成回退链，逐条对齐老行为。"""
        local_ids = [e.id for e in endpoints if e.tier == "local"]
        cloud_ids = [e.id for e in endpoints if e.tier == "cloud"]
        local = cfg.get("local") or {}
        cloud = cfg.get("cloud") or {}

        relay = bool(cfg.get("vision_relay", True))
        if relay:
            # 中继模式：先用 cloud（只看 api_key，不看 cloud.enable），失败才退 local
            for e in endpoints:
                if e.tier == "cloud":
                    e.require_enabled = False
            default = cloud_ids + local_ids
        else:
            default = (local_ids if local.get("enable") else []) + \
                      (cloud_ids if cloud.get("enable") else [])
        if not default:
            default = cloud_ids + local_ids

        backend = str((cfg.get("vision") or {}).get("backend") or "").strip().lower()
        if backend not in ("auto", "cloud", "local", "off"):
            backend = "auto" if relay else "local"
        if backend == "off":
            vision: list[str] = []
        elif backend == "cloud":
            vision = cloud_ids
        elif backend == "local":
            vision = local_ids
        else:
            vision = cloud_ids + local_ids

        chains = {"default": default, "vision": vision}
        # 识图转述固定走本地（原 describe_images 只看 local）
        chains["describe"] = local_ids
        _ = cloud  # 保留变量便于以后扩展
        return chains

    @staticmethod
    def _key_notices(endpoints: list[Endpoint]) -> list[str]:
        plain = [e.id for e in endpoints if e.api_key and not e.api_key_env]
        if not plain:
            return []
        who = "、".join(plain)
        return [f"安全提示：端点 {who} 的 api_key 是明文写进配置的。"
                f"开源 / 分享配置前请改用 api_key_env 指向环境变量"
                f"（例如 api_key_env 设为 XXX_API_KEY），密钥就不会进仓库。"]

    def _validate(self, out: EndpointSet) -> None:
        for ep in out.endpoints:
            if not ep.base_url:
                out.notices.append(f"端点 {ep.id} 没有 base_url，调用时会失败")
            if not ep.model:
                out.notices.append(f"端点 {ep.id} 没有 model，多数服务端会 400")


def _needs_spec(cls: type[Provider]) -> bool:
    """declarative 这类适配器必须由清单实例化，不能在 load_builtin 里裸实例化。"""
    import inspect  # noqa: PLC0415
    try:
        sig = inspect.signature(cls.__init__)
    except (TypeError, ValueError):
        return False
    required = [p for p in sig.parameters.values()
                if p.name != "self" and p.default is inspect.Parameter.empty
                and p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
    return bool(required)

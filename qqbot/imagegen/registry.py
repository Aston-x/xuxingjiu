"""生图端点注册中心：装载 + 旧 `sd.*` 迁移。

迁移原则（与 chat 层一致）：**旧配置不改也能跑**，启动时打印映射提示。
`sd.*` 里与"许杏玖这个人"绑定的字段（形象 / 数量词 / 禁用词 / 尺寸）**留在 sd 段**，
只有"跟后端绑"的字段（steps / cfg / sampler / hires）下沉到端点的 `params`。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from providers.registry import LoadReport
from .base import ImageProvider, registered_image_hooks
from .types import ImageCapability, ImageEndpoint

logger = logging.getLogger("imagegen.registry")

LEGACY_ID = "local-sd"


@dataclass
class ImageEndpointSet:
    endpoints: list[ImageEndpoint] = field(default_factory=list)
    chains: dict[str, list[str]] = field(default_factory=dict)
    notices: list[str] = field(default_factory=list)
    legacy: bool = False

    def by_id(self) -> dict[str, ImageEndpoint]:
        return {ep.id: ep for ep in self.endpoints}


class ImageRegistry:
    def __init__(self) -> None:
        self._providers: dict[str, ImageProvider] = {}

    # ── 装载 ──

    def register(self, name: str, provider: ImageProvider, *, override: bool = False) -> bool:
        if name in self._providers and not override:
            return False
        self._providers[name] = provider
        return True

    def get(self, name: str) -> ImageProvider | None:
        return self._providers.get(name)

    def names(self) -> list[str]:
        return sorted(self._providers)

    def summary(self) -> str:
        return "、".join(self.names()) or "（空）"

    def load_builtin(self) -> LoadReport:
        rep = LoadReport(source="内置生图适配器")
        try:
            from . import adapters  # noqa: F401,PLC0415  导入即注册
        except Exception as exc:  # noqa: BLE001
            rep.errors.append(("<adapters>", f"导入失败：{exc}"))
            return rep
        for name, cls, _prio in registered_image_hooks():
            try:
                obj = cls()
            except Exception as exc:  # noqa: BLE001
                rep.errors.append((name, f"实例化失败：{exc}"))
                continue
            obj.name = obj.name or name
            if self.register(name, obj):
                rep.loaded.append(name)
            else:
                rep.skipped.append((name, "名字已被占用"))
        return rep

    def load_manifests(self, paths: list[str | Path], *,
                       base_dir: Path | None = None) -> LoadReport:
        """清单只用来**声明端点能力**与厂商默认值，不生成代码（生图没法声明式驱动）。"""
        from providers.manifest import load_file  # noqa: PLC0415
        rep = LoadReport(source="生图清单")
        for raw in paths:
            p = Path(raw)
            if not p.is_absolute() and base_dir is not None:
                p = base_dir / p
            specs, errors = load_file(p)
            for err in errors:
                rep.errors.append((p.name, err))
            self._specs = getattr(self, "_specs", {})
            for spec in specs:
                self._specs[str(spec.get("name"))] = spec
                rep.loaded.append(str(spec.get("name")))
        return rep

    def spec(self, name: str) -> dict:
        return (getattr(self, "_specs", {}) or {}).get(name) or {}

    # ── 端点构建 ──

    def build_endpoints(self, cfg: dict, *, notices: bool = True) -> ImageEndpointSet:
        prov = cfg.get("imagegen") or {}
        raw = prov.get("endpoints")
        if isinstance(raw, list) and raw:
            out = self._build_modern(raw, prov)
        else:
            out = self._build_legacy(cfg)
        if notices:
            out.notices += self._key_notices(out.endpoints)
        return out

    def _build_modern(self, raw: list, prov: dict) -> ImageEndpointSet:
        out = ImageEndpointSet(legacy=False)
        for i, item in enumerate(raw):
            if not isinstance(item, dict):
                out.notices.append(f"imagegen.endpoints[{i}] 不是对象，已跳过")
                continue
            ep = ImageEndpoint.from_dict(item, index=i)
            base_provider = self.get(ep.provider)
            spec = self.spec(ep.provider)
            if base_provider is None and not spec:
                out.notices.append(
                    f"生图端点 {ep.id} 的 provider={ep.provider!r} 没注册"
                    f"（可用：{self.summary()}），已跳过")
                continue
            if base_provider is None:
                # 清单声明了能力但没写 Python 适配器：能力可用，但 run() 必然 unsupported
                ep.image = ImageCapability.from_dict(spec.get("capabilities"))
            if "max_per_day" in item:
                ep.params["_max_per_day"] = int(item["max_per_day"] or 0)
            out.endpoints.append(ep)
        chains = {k: [str(x) for x in v] for k, v in (prov.get("chains") or {}).items()
                  if isinstance(v, list)}
        if "image" not in chains:
            chains["image"] = [ep.id for ep in out.endpoints if ep.enabled]
        ids = {ep.id for ep in out.endpoints}
        for name, want in chains.items():
            unknown = [i for i in want if i not in ids]
            for u in unknown:
                out.notices.append(f"生图回退链 {name} 里的端点 {u!r} 不存在，已忽略")
            chains[name] = [i for i in want if i in ids]
        out.chains = chains
        cloud_cfg = prov.get("cloud") or {}
        if cloud_cfg.get("allow_when") and cloud_cfg["allow_when"] != "always":
            out.notices.append(
                f"云端生图策略：allow_when={cloud_cfg['allow_when']}"
                f"（只有低规格请求才会走云端，省成本）")
        return out

    def _build_legacy(self, cfg: dict) -> ImageEndpointSet:
        """旧 `sd.*` → 一个本地端点。语义与重构前的 SD 类逐条对齐。"""
        sd = cfg.get("sd") or {}
        out = ImageEndpointSet(legacy=True)
        if not sd:
            return out
        hires = sd.get("hires") or {}
        params: dict = {
            "steps": int(sd.get("steps", 28)),
            "cfg_scale": float(sd.get("cfg_scale", 6.5)),
            "sampler_name": str(sd.get("sampler", "DPM++ 2M Karras")),
        }
        if bool(hires.get("enable", False)):
            params.update({
                "enable_hr": True,
                "hr_scale": float(hires.get("scale", 1.35)),
                "hr_upscaler": str(hires.get("upscaler", "R-ESRGAN 4x+ Anime6B")),
                "denoising_strength": float(hires.get("denoising_strength", 0.32)),
                "hr_second_pass_steps": max(8, int(params["steps"] * 0.5)),
            })
        ep = ImageEndpoint(
            id=LEGACY_ID,
            provider="sd_webui",
            display_name="本地 SD WebUI（旧配置 sd 段）",
            base_url=str(sd.get("base_url", "http://127.0.0.1:7860")).rstrip("/"),
            model="",
            enabled=bool(sd.get("enable", False)),
            tier="local",
            concurrency=1,
            timeout=float(sd.get("timeout_seconds", 180)),
            params=params,
        )
        out.endpoints.append(ep)
        out.chains = {"image": [ep.id]}
        out.notices = [
            f'sd -> "{ep.id}"（sd_webui，{ep.base_url}，tier=local，'
            f"开={'是' if ep.enabled else '否'}）",
            "建议迁移到 imagegen 段（多后端 + 回退链 + 云端生图），"
            "见 docs/生图.md#从旧版-sd-配置迁移；旧 sd.* 仍会继续生效。",
        ]
        return out

    @staticmethod
    def _key_notices(endpoints: list[ImageEndpoint]) -> list[str]:
        plain = [e.id for e in endpoints if e.api_key and not e.api_key_env]
        if not plain:
            return []
        return [f"安全提示：生图端点 {'、'.join(plain)} 的 api_key 是明文写进配置的，"
                f"开源 / 分享前请改用 api_key_env。"]


def build_image_registry(cfg: dict, *, base_dir: Path | None = None) -> ImageRegistry:
    """建好一个装完内置适配器 + 清单的注册中心（失败只记日志，不抛）。"""
    base = Path(base_dir) if base_dir else Path(__file__).resolve().parent.parent
    reg = ImageRegistry()
    rep = reg.load_builtin()
    for w in rep.warnings():
        logger.warning("[imagegen] 适配器 %s", w)
    prov = cfg.get("imagegen") or {}
    manifests = (prov.get("plugins") or {}).get("manifests") or [
        "imagegen/manifests/local-image.json",
        "imagegen/manifests/cloud-image.json",
    ]
    mrep = reg.load_manifests(manifests, base_dir=base)
    for w in mrep.warnings():
        logger.warning("[imagegen] 清单 %s", w)

    plugins_cfg = prov.get("plugins") or {}
    if not plugins_cfg.get("disable_plugins", False):
        from .plugins import discover  # noqa: PLC0415
        prep = discover(
            base / str(plugins_cfg.get("dir") or "imagegen/plugins"),
            enabled=list(plugins_cfg.get("enabled") or []),
            allow_dir_scan=bool(plugins_cfg.get("allow_dir_scan", False)),
            registry=reg,
        )
        for w in prep.warnings():
            logger.warning("[imagegen] 插件 %s", w)
    return reg

"""Provider 注册中心 —— 「本地模型 / 云端模型」的统一接入层。

一个最小的用法：

    from providers import build_router
    router = build_router(cfg)                       # 读 config.json 里的 providers / local / cloud
    text, source = await router.answer(messages)     # 沿回退链答话
    ok = router.policy(cfg).usable                   # 现在能不能看图
    panel = router.status(cfg)                       # 控制台快照（不联网）

新增一家厂商的三种姿势（由轻到重）：
    1. **改配置**：base_url + model 一换就行（OpenAI 兼容的那几十家都这样）；
    2. **写清单**：`providers/manifests/*.json` 声明字段映射（零代码，见 docs/providers.md）；
    3. **写插件**：`providers/plugins/*.py` 实现 `Provider` 子类（处理签名、特殊报文）。

本包**不 import bot**，可脱离主程序单独测试。
"""

from __future__ import annotations

import logging
from pathlib import Path

from .base import (  # noqa: F401
    PROTOCOLS,
    VISION_PLACEHOLDER_BLIND,
    VISION_PLACEHOLDER_NO_CHANNEL,
    Provider,
    register_provider,
)
from .policy import VISION_BACKENDS, VisionPolicy  # noqa: F401
from .registry import EndpointSet, LoadReport, Registry  # noqa: F401
from .router import SOURCE_CLOUD, SOURCE_FAILED, SOURCE_LOCAL, SOURCE_NO_KEY, ConcurrencyGate, Router  # noqa: F401
from .transport import Transport  # noqa: F401
from .types import Capability, ChatResult, Endpoint, Request, UsageKeys, dget, dset  # noqa: F401
from .usage import normalize_usage  # noqa: F401

__all__ = [
    "PROTOCOLS", "VISION_BACKENDS",
    "Provider", "register_provider",
    "Capability", "Endpoint", "Request", "ChatResult", "UsageKeys", "dget", "dset",
    "Registry", "EndpointSet", "LoadReport", "Router", "ConcurrencyGate", "Transport",
    "VisionPolicy", "normalize_usage",
    "VISION_PLACEHOLDER_BLIND", "VISION_PLACEHOLDER_NO_CHANNEL",
    "SOURCE_LOCAL", "SOURCE_CLOUD", "SOURCE_NO_KEY", "SOURCE_FAILED",
    "build_router",
]

log = logging.getLogger("providers")


def build_router(cfg: dict, *, base_dir: Path | None = None,
                 transport: Transport | None = None,
                 on_usage=None,
                 logger_: logging.Logger | None = None,
                 load_plugins: bool = True) -> Router:
    """从配置建好 Registry + Router。任何装载失败都只记日志，不抛。"""
    base = Path(base_dir) if base_dir else Path(__file__).resolve().parent.parent
    prov_cfg = cfg.get("providers") or {}
    plugins_cfg = prov_cfg.get("plugins") or {}

    registry = Registry()
    registry.load_builtin()

    manifests = plugins_cfg.get("manifests")
    if manifests is None:
        manifests = ["providers/manifests/openai-compat.json",
                     "providers/manifests/local-servers.json",
                     "providers/manifests/cn-vendors.json"]
    if manifests:
        rep = registry.load_manifests(manifests, base_dir=base)
        for w in rep.warnings():
            log.warning("[providers] 清单 %s", w)

    if load_plugins:
        from .plugins import discover  # noqa: PLC0415
        rep = discover(
            base / str(plugins_cfg.get("dir") or "providers/plugins"),
            enabled=list(plugins_cfg.get("enabled") or []),
            allow_dir_scan=bool(plugins_cfg.get("allow_dir_scan", False)),
            registry=registry,
        )
        for w in rep.warnings():
            log.warning("[providers] 插件 %s", w)

    router = Router(registry, transport=transport, logger_=logger_, on_usage=on_usage)
    router.load_notices(cfg)
    log.info("[providers] 可用适配器：%s｜端点 %d 个｜回退链 %s",
             registry.summary(), len(router.endpoints),
             "、".join(f"{k}={len(v)}" for k, v in router.chains.items()) or "（无）")
    return router

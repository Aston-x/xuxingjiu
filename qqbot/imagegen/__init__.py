"""生图层 —— 「把图弄回来」的统一接入层。

和 `providers/`（对话模型）同构、但**独立**：生图后端和对话模型是两回事，
注册表分开，避免名字互相打架。

最小用法：

    from imagegen import build_image_router
    router = build_image_router(cfg)                 # 读 config.json 的 imagegen / sd 段
    ok, reason, why = router.check_ready("group")    # 零请求：现在能不能画
    out = await router.generate(req)                 # 沿回退链试，拿字节或失败原因
    panel = router.status()                          # 同步、不联网的快照

内置后端（都实现了 `ImageProvider`）：
    sd_webui  本地 Stable Diffusion WebUI（实测）
    comfyui   本地 ComfyUI（工作流注入）
    sd_next   本地 SD.Next（接口同 A1111）
    invokeai  本地 InvokeAI（单一路径）
    openai_images / dashscope / cogview / siliconflow / gemini_image   云端

新增一个后端：写一个 `ImageProvider` 子类并 `@register_image_provider("名字")`，
放在 `imagegen/adapters/` 或 `imagegen/plugins/`（白名单加载）。
"""

from __future__ import annotations

import logging
from pathlib import Path

from .base import ImageProvider, register_image_provider  # noqa: F401
from .registry import ImageEndpointSet, ImageRegistry, build_image_registry  # noqa: F401
from .router import ImageRouter  # noqa: F401
from .types import (  # noqa: F401
    REASON_BACKEND_DOWN,
    REASON_BAD_CONFIG,
    REASON_DISABLED,
    REASON_EMPTY,
    REASON_EMPTY_IMAGE,
    REASON_FAILED,
    REASON_HTTP,
    REASON_NO_ENDPOINT,
    REASON_QUOTA,
    REASON_TIMEOUT,
    REASON_TOO_LARGE,
    GenOutcome,
    GenRequest,
    ImageCapability,
    ImageEndpoint,
    ImageResult,
)

__all__ = [
    "ImageProvider", "register_image_provider",
    "ImageRegistry", "ImageEndpointSet", "build_image_registry",
    "ImageRouter", "build_image_router",
    "ImageCapability", "ImageEndpoint", "GenRequest", "ImageResult", "GenOutcome",
    "REASON_DISABLED", "REASON_QUOTA", "REASON_EMPTY", "REASON_NO_ENDPOINT",
    "REASON_BACKEND_DOWN", "REASON_TIMEOUT", "REASON_HTTP", "REASON_EMPTY_IMAGE",
    "REASON_TOO_LARGE", "REASON_BAD_CONFIG", "REASON_FAILED",
]

log = logging.getLogger("imagegen")


def build_image_router(cfg: dict, *, base_dir: Path | None = None,
                       transport=None, logger_=None,
                       state_path: Path | None = None) -> ImageRouter:
    """从配置建好 Registry + Router。任何装载失败都只记日志，不抛。"""
    base = Path(base_dir) if base_dir else Path(__file__).resolve().parent.parent
    registry = build_image_registry(cfg, base_dir=base)
    if state_path is None:
        state_path = base / "state" / "imagegen_state.json"
    router = ImageRouter(registry, transport=transport, cfg=cfg,
                         logger_=logger_, state_path=state_path)
    router.load_cfg(cfg)
    log.info("[imagegen] 可用后端：%s｜端点 %d 个｜链 %s",
             registry.summary(), len(router.endpoints),
             "、".join(f"{k}={len(v)}" for k, v in router.chains.items()) or "（无）")
    return router

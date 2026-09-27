"""SD.Next 适配器 —— 接口与 A1111 基本兼容，所以直接复用 sd_webui 的形状。

差异点都在端点配置里表达（模型目录、采样器名字），代码层面不需要分家。
这里单独留一个注册名，是为了让用户在配置里写 `provider: "sd_next"` 时
意思是明确的，而不是"看起来像但其实是 A1111"。

⚠️ 未实测：手上没有 SD.Next 环境。若它的 `/sdapi/v1/txt2img` 有出入，
`probe()` 会失败并由 doctor 报出来，不会静默画错。
"""

from __future__ import annotations

from ..base import register_image_provider
from ..types import ImageCapability
from .sd_webui import SDWebUIProvider


@register_image_provider("sd_next")
class SDNextProvider(SDWebUIProvider):
    name = "sd_next"
    display_name = "SD.Next（本地）"

    def default_capabilities(self) -> ImageCapability:
        cap = super().default_capabilities()
        return ImageCapability(
            free_size=cap.free_size, size_step=cap.size_step, negative=cap.negative,
            hires=True, returns=cap.returns, formats=cap.formats,
            max_prompt_chars=cap.max_prompt_chars, seed=cap.seed, moderation=False,
            max_bytes=cap.max_bytes,
            note="接口同 A1111/SD.WebUI；未实测",
        )

    async def probe(self, ep, http, *, deep: bool = False) -> dict:
        got = await super().probe(ep, http, deep=deep)
        got["note"] = "SD.Next 未实测；若这里报错请核对它的 API 前缀"
        return got

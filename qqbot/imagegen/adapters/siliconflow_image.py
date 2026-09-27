"""硅基流动（SiliconFlow）生图适配器。

接口：`POST {base_url}/images/generations`，尺寸字段叫 **`image_size`**（不是 `size`），
负面提示词用 `negative_prompt`。它是个聚合平台，能力随背后的模型而变 ——
所以端点里可以用 `capabilities` 逐端点覆盖预设档位。

⚠️ 未实测：按公开文档写，字段以官方文档为准。
"""

from __future__ import annotations

from ..base import ImageProvider, register_image_provider
from providers.types import Request
from ..types import (
    REASON_EMPTY_IMAGE,
    GenRequest,
    ImageCapability,
    ImageEndpoint,
    ImageResult,
)


@register_image_provider("siliconflow_image")
class SiliconFlowImageProvider(ImageProvider):
    name = "siliconflow_image"
    display_name = "硅基流动（生图）"

    def default_capabilities(self) -> ImageCapability:
        return ImageCapability(
            free_size=False,
            presets=((1024, 1024), (768, 1024), (1024, 768), (768, 1344), (1344, 768)),
            negative=True,
            hires=False,
            returns="url",
            formats=("png",),
            max_prompt_chars=1500,
            seed=True,
            moderation=True,
            note="聚合平台，能力随背后模型而变；未实测",
        )

    def available(self, ep: ImageEndpoint) -> tuple[bool, str]:
        if not (ep.base_url or "").strip():
            return False, "没配 base_url（默认 https://api.siliconflow.cn/v1）"
        if not ep.has_key():
            return False, f"没配密钥（环境变量 {ep.api_key_env or 'SILICONFLOW_API_KEY'}）"
        return True, ""

    async def run(self, ep: ImageEndpoint, req: GenRequest, http) -> ImageResult:
        body: dict = {
            "model": ep.model or "stabilityai/stable-diffusion-xl-base-1.0",
            "prompt": req.prompt,
            "image_size": f"{int(req.width)}x{int(req.height)}",
            "batch_size": 1,
        }
        if req.negative:
            body["negative_prompt"] = req.negative
        for k, v in (req.params or {}).items():
            if not k.startswith("_"):
                body[k] = v
        url = ep.base_url.rstrip("/") + "/images/generations"
        resp = await http.send(ep, Request(method="POST", url=url,
                                           headers=self.bearer_headers(ep),
                                           json_body=body), timeout=ep.timeout)
        payload = resp.json() or {}
        items = payload.get("images") or payload.get("data") or []
        if not items:
            raise ValueError(f"{REASON_EMPTY_IMAGE}: 返回里没有图片：{str(payload)[:200]}")
        first = items[0] or {}
        link = str(first.get("url") or "")
        if not link:
            raise ValueError(f"{REASON_EMPTY_IMAGE}: 返回里没有图片 url")
        raw = await self.download(http, ep, link)
        return ImageResult(data=raw, endpoint_id=ep.id, meta={"url": link[:200]})

    async def probe(self, ep: ImageEndpoint, http, *, deep: bool = False) -> dict:
        if not ep.has_key():
            return {"unsupported": True, "error": "没配密钥"}
        return {"unsupported": True, "note": "云端接口，不主动探测（避免计费）"}

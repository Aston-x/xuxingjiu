"""智谱 CogView 适配器。

接口：`POST {base_url}/images/generations`（与 OpenAI 形状接近，但**尺寸档位不同**、
且 CogView-4 **不支持负面提示词** —— 传了会被拒，所以直接不发）。

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


@register_image_provider("cogview")
class CogViewProvider(ImageProvider):
    name = "cogview"
    display_name = "智谱 CogView"

    def default_capabilities(self) -> ImageCapability:
        return ImageCapability(
            free_size=False,
            presets=((1024, 1024), (768, 1344), (1344, 768), (1440, 720), (720, 1440)),
            negative=False,          # CogView-4 不支持
            hires=False,
            returns="url",
            formats=("png",),
            max_prompt_chars=1000,
            seed=True,
            moderation=True,
            note="CogView-4 不支持负面提示词（传了会被拒，所以不发）；未实测",
        )

    def available(self, ep: ImageEndpoint) -> tuple[bool, str]:
        if not (ep.base_url or "").strip():
            return False, "没配 base_url（默认 https://open.bigmodel.cn/api/paas/v4）"
        if not ep.has_key():
            return False, f"没配密钥（环境变量 {ep.api_key_env or 'ZHIPU_API_KEY'}）"
        return True, ""

    async def run(self, ep: ImageEndpoint, req: GenRequest, http) -> ImageResult:
        body: dict = {
            "model": ep.model or "cogview-4",
            "prompt": req.prompt,
            "size": f"{int(req.width)}x{int(req.height)}",
        }
        for k, v in (req.params or {}).items():
            if not k.startswith("_"):
                body[k] = v
        url = ep.base_url.rstrip("/") + "/images/generations"
        resp = await http.send(ep, Request(method="POST", url=url,
                                           headers=self.bearer_headers(ep),
                                           json_body=body), timeout=ep.timeout)
        items = (resp.json() or {}).get("data") or []
        if not items:
            raise ValueError(f"{REASON_EMPTY_IMAGE}: 返回里没有 data")
        link = str((items[0] or {}).get("url") or "")
        if not link:
            raise ValueError(f"{REASON_EMPTY_IMAGE}: 返回里没有图片 url")
        raw = await self.download(http, ep, link)
        return ImageResult(data=raw, endpoint_id=ep.id, meta={"url": link[:200]})

    async def probe(self, ep: ImageEndpoint, http, *, deep: bool = False) -> dict:
        if not ep.has_key():
            return {"unsupported": True, "error": "没配密钥"}
        return {"unsupported": True, "note": "云端接口，不主动探测（避免计费）"}

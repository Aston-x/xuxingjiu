"""Google Gemini / Imagen 适配器。

接口：`POST {base_url}/models/{model}:predict`
（Imagen 系用 `:predict`，不是对话模型的 `:generateContent`）

  body: `{"instances": [{"prompt": "..."}], "parameters": {"sampleCount": 1, "aspectRatio": "1:1"}}`
  返回: `predictions[0].bytesBase64Encoded`

三个必须注意的点：

  · **密钥走 `x-goog-api-key` 头**，绝不能塞进 URL 的 `?key=` ——
    否则 URL 一旦上屏（控制台端点面板 / 日志）密钥就跟着泄漏。
  · **尺寸是"宽高比"不是像素**，所以从预设档反推 aspectRatio。
  · **不支持负面提示词**（Imagen 没有这个参数），传了会被忽略。
"""

from __future__ import annotations

import base64
from math import gcd

from ..base import ImageProvider, register_image_provider
from providers.types import Request
from ..types import (
    REASON_EMPTY_IMAGE,
    GenRequest,
    ImageCapability,
    ImageEndpoint,
    ImageResult,
)


def _aspect(w: int, h: int) -> str:
    """把像素尺寸换算成最简宽高比，如 1024x1536 -> 2:3。"""
    g = gcd(int(w), int(h)) or 1
    return f"{int(w) // g}:{int(h) // g}"


@register_image_provider("gemini_image")
class GeminiImageProvider(ImageProvider):
    name = "gemini_image"
    display_name = "Google Gemini / Imagen"

    def default_capabilities(self) -> ImageCapability:
        return ImageCapability(
            free_size=False,
            presets=((1024, 1024), (896, 1152), (1152, 896), (1344, 768), (768, 1344)),
            negative=False,
            hires=False,
            returns="b64",
            formats=("png",),
            max_prompt_chars=4000,
            seed=False,
            moderation=True,
            note="走 :predict；密钥用 x-goog-api-key 头传（不写进 URL）；未实测",
        )

    def available(self, ep: ImageEndpoint) -> tuple[bool, str]:
        if not (ep.base_url or "").strip():
            return False, "没配 base_url（默认 https://generativelanguage.googleapis.com/v1beta）"
        if not ep.has_key():
            return False, f"没配密钥（环境变量 {ep.api_key_env or 'GEMINI_API_KEY'}）"
        return True, ""

    async def run(self, ep: ImageEndpoint, req: GenRequest, http) -> ImageResult:
        model = ep.model or "imagen-4.0-generate-001"
        body: dict = {
            "instances": [{"prompt": req.prompt}],
            "parameters": {"sampleCount": 1,
                           "aspectRatio": _aspect(req.width, req.height)},
        }
        for k, v in (req.params or {}).items():
            if not k.startswith("_"):
                body["parameters"][k] = v
        url = f"{ep.base_url.rstrip('/')}/models/{model}:predict"
        headers = {"x-goog-api-key": ep.auth(), "Content-Type": "application/json"}
        headers.update(ep.headers or {})
        resp = await http.send(ep, Request(method="POST", url=url, headers=headers,
                                           json_body=body), timeout=ep.timeout)
        payload = resp.json() or {}
        preds = payload.get("predictions") or []
        if not preds:
            raise ValueError(f"{REASON_EMPTY_IMAGE}: 返回里没有 predictions：{str(payload)[:200]}")
        b64 = str((preds[0] or {}).get("bytesBase64Encoded") or "")
        if not b64:
            raise ValueError(f"{REASON_EMPTY_IMAGE}: predictions[0] 里没有图片数据")
        return ImageResult(data=base64.b64decode(b64), mime="image/png", endpoint_id=ep.id,
                           meta={"aspect": body["parameters"]["aspectRatio"]})

    async def probe(self, ep: ImageEndpoint, http, *, deep: bool = False) -> dict:
        if not ep.has_key():
            return {"unsupported": True, "error": "没配密钥"}
        return {"unsupported": True, "note": "云端接口，不主动探测（避免计费）"}

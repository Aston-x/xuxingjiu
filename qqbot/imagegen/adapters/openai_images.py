"""OpenAI Images 适配器（`gpt-image-1` / `dall-e-3`）。

接口：`POST {base_url}/images/generations`

两个容易踩的点：

  · **尺寸只认三个档**（1024x1024 / 1024x1536 / 1536x1024）。发 `832x1216` 会直接 400，
    所以 `fit_size` 必须先吸附（`ImageCapability.presets` 就是干这个的）。
  · **不支持负面提示词**。传了不报错但会被忽略，所以干脆不发、并记一条 note。
  · `gpt-image-1` **不接受 `response_format` 参数**（它永远返 b64），
    只有 `dall-e-*` 才认这个字段 —— 按模型名分流。
"""

from __future__ import annotations

import base64

from ..base import ImageProvider, register_image_provider
from providers.types import Request
from ..types import (
    REASON_EMPTY_IMAGE,
    GenRequest,
    ImageCapability,
    ImageEndpoint,
    ImageResult,
)


@register_image_provider("openai_images")
class OpenAIImagesProvider(ImageProvider):
    name = "openai_images"
    display_name = "OpenAI Images"

    def default_capabilities(self) -> ImageCapability:
        return ImageCapability(
            free_size=False,
            presets=((1024, 1024), (1024, 1536), (1536, 1024)),
            negative=False,          # 不支持负面提示词
            hires=False,
            returns="b64",
            formats=("png",),
            max_prompt_chars=32000,
            seed=False,
            moderation=True,
            note="尺寸只认三个预设；不支持负面提示词；有内容审核",
        )

    def available(self, ep: ImageEndpoint) -> tuple[bool, str]:
        if not (ep.base_url or "").strip():
            return False, "没配 base_url（OpenAI 默认 https://api.openai.com/v1）"
        if not ep.has_key():
            return False, (f"没配密钥（api_key 或环境变量 {ep.api_key_env or 'OPENAI_API_KEY'}）"
                           f"—— 云端生图要花钱，没有密钥就不该被选中")
        return True, ""

    async def run(self, ep: ImageEndpoint, req: GenRequest, http) -> ImageResult:
        model = ep.model or "gpt-image-1"
        body: dict = {
            "model": model,
            "prompt": req.prompt,
            "n": 1,
            "size": f"{int(req.width)}x{int(req.height)}",
        }
        # dall-e 系才认 response_format；gpt-image 系传了会 400
        if model.lower().startswith("dall-e"):
            body["response_format"] = "b64_json"
        for k, v in (req.params or {}).items():
            if not k.startswith("_"):
                body[k] = v

        url = ep.base_url.rstrip("/") + "/images/generations"
        resp = await http.send(ep, Request(method="POST", url=url,
                                           headers=self.bearer_headers(ep),
                                           json_body=body), timeout=ep.timeout)
        data = resp.json() or {}
        items = data.get("data") or []
        if not items:
            raise ValueError(f"{REASON_EMPTY_IMAGE}: 返回里没有 data：{str(data)[:200]}")
        item = items[0] or {}
        b64 = item.get("b64_json")
        if b64:
            return ImageResult(data=base64.b64decode(b64), mime="image/png",
                               endpoint_id=ep.id,
                               meta={"revised_prompt": item.get("revised_prompt") or ""})
        link = item.get("url")
        if not link:
            raise ValueError(f"{REASON_EMPTY_IMAGE}: 既没有 b64_json 也没有 url")
        raw = await self.download(http, ep, str(link))
        return ImageResult(data=raw, endpoint_id=ep.id, meta={"url": str(link)[:200]})

    async def probe(self, ep: ImageEndpoint, http, *, deep: bool = False) -> dict:
        """`GET /models` 是免费的，用它确认密钥有效 + base_url 没写错。"""
        root = (ep.base_url or "").rstrip("/")
        if not root or not ep.has_key():
            return {"unsupported": True, "error": "没配 base_url 或密钥"}
        try:
            r = await http.get(ep, root + "/models",
                               headers={"Authorization": f"Bearer {ep.auth()}"}, timeout=10)
            r.raise_for_status()
            items = (r.json() or {}).get("data") or []
        except Exception as exc:  # noqa: BLE001
            return {"error": str(exc)[:150]}
        return {"models": len(items),
                "sample": [str(m.get("id") or "") for m in items[:3] if isinstance(m, dict)]}

"""Stable Diffusion WebUI（AUTOMATIC1111 / Forge / reForge）适配器。

接口：`POST {base_url}/sdapi/v1/txt2img` → `{"images": ["<base64>", …], "info": "<JSON 字符串>"}`。
必须带 `--api` 启动 WebUI，否则这个路径不存在（404）。

这是本项目**唯一在医院验证过**的生图后端，也是 `sd.webui` 语义的等价实现 ——
搬进来是为了让"换后端"变成改配置，而不是改代码。
"""

from __future__ import annotations

import base64
import json

from ..base import ImageProvider, register_image_provider
from providers.types import Request
from ..types import GenRequest, ImageCapability, ImageEndpoint, ImageResult

# 这些键由适配器自己按请求填，端点 params 里出现同名一律忽略（避免把旧值覆盖回来）
_RESERVED = ("prompt", "negative_prompt", "width", "height", "batch_size", "init_images")


@register_image_provider("sd_webui")
class SDWebUIProvider(ImageProvider):
    name = "sd_webui"
    display_name = "Stable Diffusion WebUI（本地）"

    def default_capabilities(self) -> ImageCapability:
        return ImageCapability(
            free_size=True,          # SD 系尺寸自由，但必须对齐 8
            size_step=8,
            negative=True,
            hires=True,
            returns="b64",
            formats=("png",),
            max_prompt_chars=4000,   # CLIP 77 token × 多个 chunk，这里按字符给个宽松上限
            seed=True,
            moderation=False,
            max_bytes=64 * 1024 * 1024,
        )

    def available(self, ep: ImageEndpoint) -> tuple[bool, str]:
        base = (ep.base_url or "").strip()
        if not base:
            return False, "没配 base_url（SD WebUI 默认 http://127.0.0.1:7860）"
        return True, ""

    async def run(self, ep: ImageEndpoint, req: GenRequest, http) -> ImageResult:
        # 端点 params（steps/cfg/sampler/hires…）打底，请求带的 params 覆盖它
        body: dict = {k: v for k, v in (ep.params or {}).items() if not k.startswith("_")}
        for k, v in (req.params or {}).items():
            if not k.startswith("_"):
                body[k] = v
        for k in _RESERVED:
            body.pop(k, None)
        body.update({
            "prompt": req.prompt,
            "negative_prompt": req.negative,
            "width": int(req.width),
            "height": int(req.height),
            "batch_size": 1,
        })
        url = ep.base_url.rstrip("/") + "/sdapi/v1/txt2img"
        resp = await http.send(ep, Request(method="POST", url=url,
                                           headers={"Content-Type": "application/json"},
                                           json_body=body),
                               timeout=ep.timeout)
        payload = resp.json() or {}
        images = payload.get("images") or []
        raw_b64 = str(images[0]) if images else ""
        if not raw_b64:
            return ImageResult(data=b"", endpoint_id=ep.id)
        data = base64.b64decode(raw_b64.split(",", 1)[-1])
        meta = _parse_info(payload.get("info"))
        meta["bytes"] = len(data)
        return ImageResult(data=data, mime="image/png", endpoint_id=ep.id, meta=meta)

    async def probe(self, ep: ImageEndpoint, http, *, deep: bool = False) -> dict:
        """廉价探针：列一下模型。零成本，能证明 WebUI 活着且开了 --api。"""
        root = (ep.base_url or "").rstrip("/")
        if not root:
            return {"error": "没配 base_url"}
        try:
            resp = await http.get(ep, root + "/sdapi/v1/sd-models", timeout=8)
            resp.raise_for_status()
            items = resp.json() or []
        except Exception as exc:  # noqa: BLE001
            return {"error": str(exc)[:150]}
        titles = [str(m.get("model_name") or m.get("title") or "") for m in items
                  if isinstance(m, dict)]
        return {"models": len(titles), "sample": titles[:3]}

    async def list_samplers(self, ep: ImageEndpoint, http) -> list[str]:
        """可选：拉一下采样器列表（`doctor` 校验 sampler 名字有没有写错时用）。"""
        root = (ep.base_url or "").rstrip("/")
        try:
            resp = await http.get(ep, root + "/sdapi/v1/samplers", timeout=8)
            resp.raise_for_status()
            return [str(s.get("name") or "") for s in (resp.json() or [])
                    if isinstance(s, dict)]
        except Exception:  # noqa: BLE001
            return []


def _parse_info(raw) -> dict:
    """`info` 是个 JSON 字符串（不是对象），拿不到就算了。"""
    if isinstance(raw, dict):
        return {"info": raw}
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        obj = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    if not isinstance(obj, dict):
        return {}
    out = {k: obj.get(k) for k in ("seed", "sampler_name", "steps", "cfg_scale")
           if obj.get(k) is not None}
    return out

"""ComfyUI 适配器：提交工作流 → 轮询 history → 取图（三跳）。

为什么和 SD WebUI 不能共用一套：ComfyUI 是**工作流**驱动的，没有"给个 prompt 就出图"
这种一步到位的接口。所以要：

  1. `POST /prompt` 提交一份工作流 JSON → 拿 `prompt_id`；
  2. 轮询 `GET /history/{prompt_id}` 直到 `outputs` 出现（或超时）；
  3. `GET /view?filename=…&subfolder=…&type=output` 取回图片字节。

⚠️ 两个必须踩住的坑：

  · **`node_errors` 非空就立刻失败**。`POST /prompt` 即使工作流有问题也会返 200，
    只是带一个 `node_errors`；不看它就会一直轮询到超时，表现为"卡到超时"。
  · **轮询必须有上限**（总时长 + 次数），否则 ComfyUI 卡住时这里会永远转。

工作流模板放在 `imagegen/workflows/comfyui_default.json`，可以自己导出替换；
模板里的节点 id 与 checkpoint 名要与你实际装的模型一致，否则会命中上面第一个坑。
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import random
import time

from ..base import ImageProvider, register_image_provider
from providers.types import Request
from ..types import (
    REASON_BAD_CONFIG,
    REASON_EMPTY_IMAGE,
    REASON_TIMEOUT,
    GenRequest,
    ImageCapability,
    ImageEndpoint,
    ImageResult,
)

WORKFLOW_DIR = pathlib.Path(__file__).resolve().parent.parent / "workflows"

# 内置兜底工作流：SDXL/SD1.5 通用的最小链路。
# 节点标题（"_meta.title"）只是给人看的，ComfyUI 忽略它。
DEFAULT_WORKFLOW: dict = {
    "3": {"class_type": "KSampler", "_meta": {"title": "K采样器"},
          "inputs": {"seed": 0, "steps": 28, "cfg": 6.5, "sampler_name": "dpmpp_2m",
                     "scheduler": "karras", "denoise": 1.0, "model": ["4", 0],
                     "positive": ["6", 0], "negative": ["7", 0], "latent_image": ["5", 0]}},
    "4": {"class_type": "CheckpointLoaderSimple", "_meta": {"title": "加载模型"},
          "inputs": {"ckpt_name": "model.safetensors"}},
    "5": {"class_type": "EmptyLatentImage", "_meta": {"title": "空潜空间"},
          "inputs": {"width": 832, "height": 1216, "batch_size": 1}},
    "6": {"class_type": "CLIPTextEncode", "_meta": {"title": "正向提示词"},
          "inputs": {"text": "", "clip": ["4", 1]}},
    "7": {"class_type": "CLIPTextEncode", "_meta": {"title": "负向提示词"},
          "inputs": {"text": "", "clip": ["4", 1]}},
    "8": {"class_type": "VAEDecode", "_meta": {"title": "VAE 解码"},
          "inputs": {"samples": ["3", 0], "vae": ["4", 2]}},
    "9": {"class_type": "SaveImage", "_meta": {"title": "保存图片"},
          "inputs": {"filename_prefix": "xuxingjiu", "images": ["8", 0]}},
}


@register_image_provider("comfyui")
class ComfyUIProvider(ImageProvider):
    name = "comfyui"
    display_name = "ComfyUI（本地）"

    def default_capabilities(self) -> ImageCapability:
        return ImageCapability(
            free_size=True, size_step=8, negative=True, hires=False,
            returns="b64", formats=("png",), max_prompt_chars=4000, seed=True,
            multi_hop=True, max_bytes=64 * 1024 * 1024,
            note="走「提交工作流 → 轮询 history → 取图」三跳；工作流模板见 imagegen/workflows/",
        )

    def available(self, ep: ImageEndpoint) -> tuple[bool, str]:
        if not (ep.base_url or "").strip():
            return False, "没配 base_url（ComfyUI 默认 http://127.0.0.1:8188）"
        return True, ""

    # ── 工作流装配 ──

    def _workflow(self, ep: ImageEndpoint) -> tuple[dict, str]:
        """取工作流模板：端点指定的文件 > 约定的默认文件 > 内置兜底。"""
        want = str(ep.params.get("workflow") or "").strip()
        cands = [WORKFLOW_DIR / want] if want else []
        cands.append(WORKFLOW_DIR / "comfyui_default.json")
        for p in cands:
            try:
                if p.is_file():
                    return json.loads(p.read_text(encoding="utf-8")), str(p)
            except Exception as exc:  # noqa: BLE001
                return {}, f"工作流文件读不了（{p}：{exc}）"
        return json.loads(json.dumps(DEFAULT_WORKFLOW)), "（内置兜底工作流）"

    @staticmethod
    def _inject(wf: dict, req: GenRequest, seed: int, params: dict) -> dict:
        """把提示词/尺寸/参数塞进模板。

        定位规则（按优先级）——因为节点 id 会随模板变，不能写死：
          1. 模板里 `_meta.title` 含"正/negative"之类关键词的节点；
          2. 否则按 class_type：CLIPTextEncode 有两条时，靠前的当正向、
             引用 KSampler.positive 的那条当正向（更可靠）。
        """
        out = json.loads(json.dumps(wf))

        def is_type(node: dict, t: str) -> bool:
            return str(node.get("class_type") or "") == t

        # ① 正向 / 负向
        enc = {k: v for k, v in out.items() if is_type(v, "CLIPTextEncode")}
        ks = next((v for v in out.values() if is_type(v, "KSampler")), None)
        pos_id, neg_id = "", ""
        if ks:
            refs = ks.get("inputs") or {}
            pos_id = str((refs.get("positive") or ["", 0])[0])
            neg_id = str((refs.get("negative") or ["", 0])[0])
        if not pos_id or not neg_id:
            ids = sorted(enc, key=lambda x: int(x) if str(x).isdigit() else 0)
            if ids:
                pos_id = pos_id or ids[0]
                neg_id = neg_id or (ids[1] if len(ids) > 1 else "")
        for nid, text in ((pos_id, req.prompt), (neg_id, req.negative)):
            if nid and nid in out:
                out[nid].setdefault("inputs", {})["text"] = text

        # ② 尺寸
        for node in out.values():
            if is_type(node, "EmptyLatentImage"):
                node.setdefault("inputs", {}).update({"width": req.width,
                                                      "height": req.height})
            elif is_type(node, "EmptySD3LatentImage"):
                node.setdefault("inputs", {}).update({"width": req.width,
                                                      "height": req.height})

        # ③ KSampler 参数（seed 每次都给新值，否则 ComfyUI 会命中缓存返回同一张）
        for node in out.values():
            if is_type(node, "KSampler"):
                ins = node.setdefault("inputs", {})
                ins["seed"] = seed
                for key, src in (("steps", "steps"), ("cfg", "cfg_scale"),
                                 ("sampler_name", "sampler_name"), ("scheduler", "scheduler")):
                    if src in params and params[src] is not None:
                        ins[key] = params[src]
            elif is_type(node, "SaveImage"):
                node.setdefault("inputs", {})["filename_prefix"] = "xuxingjiu"
        return out

    # ── 主流程 ──

    async def run(self, ep: ImageEndpoint, req: GenRequest, http) -> ImageResult:
        params = {k: v for k, v in (ep.params or {}).items() if not k.startswith("_")}
        for k, v in (req.params or {}).items():
            if not k.startswith("_"):
                params[k] = v

        wf, src = self._workflow(ep)
        if not wf:
            raise ValueError(f"{REASON_BAD_CONFIG}: {src}")
        # ckpt 名字可以在端点上覆盖（模板里那个默认名多半对不上你的模型）
        ckpt = str(params.pop("ckpt_name", "") or "")
        if ckpt:
            for node in wf.values():
                if str(node.get("class_type")) == "CheckpointLoaderSimple":
                    node.setdefault("inputs", {})["ckpt_name"] = ckpt
        seed = int(params.pop("seed", 0) or 0) or random.randint(1, 2 ** 31 - 1)
        wf = self._inject(wf, req, seed, params)

        root = ep.base_url.rstrip("/")
        client_id = f"xuxingjiu-{random.randint(1000, 9999)}"

        # ① 提交
        resp = await http.send(ep, Request(
            method="POST", url=root + "/prompt",
            headers={"Content-Type": "application/json"},
            json_body={"prompt": wf, "client_id": client_id}), timeout=ep.timeout)
        payload = resp.json() or {}
        errs = payload.get("node_errors") or {}
        if errs:
            # ★ 这里必须立刻失败：不看它就会一直轮询到超时，表现为"卡死"
            first = next(iter(errs.values()), {})
            raise ValueError(f"工作流节点报错（模板：{src}）：{str(first)[:200]}")
        if payload.get("error"):
            raise ValueError(f"ComfyUI 拒绝了这个工作流：{str(payload['error'])[:200]}")
        prompt_id = str(payload.get("prompt_id") or "")
        if not prompt_id:
            raise ValueError(f"ComfyUI 没返回 prompt_id：{str(payload)[:200]}")

        # ② 轮询 history
        deadline = time.time() + float(ep.timeout or 180)
        outputs: dict = {}
        while time.time() < deadline:
            await asyncio.sleep(1.0)
            h = await http.send(ep, Request(method="GET", url=f"{root}/history/{prompt_id}"),
                                timeout=15)
            hist = h.json() or {}
            entry = (hist or {}).get(prompt_id) or {}
            if entry:
                status = str((entry.get("status") or {}).get("status_str") or "")
                if status == "error":
                    raise ValueError(f"ComfyUI 执行失败：{str(entry.get('status'))[:200]}")
                outputs = entry.get("outputs") or {}
                if outputs:
                    break
        if not outputs:
            raise TimeoutError(f"{REASON_TIMEOUT}: 等 ComfyUI 出图超时（{ep.timeout}s）")

        # ③ 取图
        info = None
        for node in outputs.values():
            for img in (node.get("images") or []):
                if isinstance(img, dict):
                    info = img
                    break
            if info:
                break
        if not info:
            raise ValueError(f"{REASON_EMPTY_IMAGE}: ComfyUI 的 outputs 里没有图片")
        q = {"filename": info.get("filename") or "",
             "subfolder": info.get("subfolder") or "",
             "type": info.get("type") or "output"}
        got = await http.send(ep, Request(method="GET", url=root + "/view", params=q),
                              timeout=60)
        data = got.content or b""
        if not data:
            raise ValueError(f"{REASON_EMPTY_IMAGE}: 取回的图片是空的")
        if len(data) > self.capability(ep).max_bytes:
            raise ValueError(f"{REASON_EMPTY_IMAGE}: 图片超过大小上限")
        return ImageResult(data=data, mime=str(got.headers.get("content-type")
                                               or "image/png").split(";")[0],
                           endpoint_id=ep.id,
                           meta={"seed": seed, "workflow": src, "prompt_id": prompt_id})

    async def probe(self, ep: ImageEndpoint, http, *, deep: bool = False) -> dict:
        """`/object_info` 是懒加载的大 JSON（首次可能几百 KB），所以只在 deep 时拉。"""
        root = (ep.base_url or "").rstrip("/")
        if not root:
            return {"error": "没配 base_url"}
        try:
            r = await http.get(ep, root + "/system_stats", timeout=8)
            r.raise_for_status()
            stats = r.json() or {}
        except Exception as exc:  # noqa: BLE001
            return {"error": str(exc)[:150]}
        out: dict = {"ok": True,
                     "device": str((stats.get("devices") or [{}])[0].get("name") or "")}
        if deep:
            try:
                oi = await http.get(ep, root + "/object_info", timeout=20)
                oi.raise_for_status()
                nodes = oi.json() or {}
                ckpts = (((nodes.get("CheckpointLoaderSimple") or {}).get("input") or {})
                         .get("required") or {}).get("ckpt_name") or [[]]
                out["models"] = len(ckpts[0]) if ckpts and isinstance(ckpts[0], list) else 0
            except Exception as exc:  # noqa: BLE001
                out["object_info_error"] = str(exc)[:120]
        return out

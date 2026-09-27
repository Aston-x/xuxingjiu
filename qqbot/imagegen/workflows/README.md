# 生图工作流模板

这个目录放**需要多跳的后端**要用的模板文件。

## ComfyUI

`comfyui_default.json`（可选）。不提供时适配器会用内置的兜底工作流
（一份最小的 SDXL/SD1.5 链路：CheckpointLoader → 两个 CLIPTextEncode → KSampler → VAEDecode → SaveImage）。

**怎么换成你自己的**：

1. 在 ComfyUI 里搭好工作流，确认能出图；
2. 菜单 → Workflow → **Export (API)**（注意不是普通的 Export，必须是 API 格式）；
3. 存成 `comfyui_default.json` 放进本目录，或另存一个名字，
   然后在端点里指定：

```jsonc
{"id": "comfy", "provider": "comfyui", "base_url": "http://127.0.0.1:8188",
 "tier": "local",
 "params": {"workflow": "my_workflow.json", "ckpt_name": "animagineXL40.safetensors"}}
```

**适配器会自动覆盖的字段**（不需要你手工填）：

| 字段 | 怎么找 |
| --- | --- |
| 正向 / 负向提示词 | 优先看 `KSampler.inputs.positive / negative` 指向谁；找不到就取前两个 `CLIPTextEncode` |
| 尺寸 | 所有 `EmptyLatentImage` / `EmptySD3LatentImage` 节点的 `width` / `height` |
| seed | `KSampler.seed`（**每次都会给新值**，否则 ComfyUI 会命中缓存返回同一张图） |
| steps / cfg / sampler / scheduler | 端点的 `params` 里同名键 |

**两个常见坑**：

- 模板里的 `CheckpointLoaderSimple.ckpt_name` 必须是你机器上真实存在的模型文件名，
  否则 `POST /prompt` 会返 200 但带 `node_errors` —— 适配器会**立刻失败**并把这个原因报出来，
  不会傻等超时。
- 换了模型家族（比如从 SDXL 换到 Flux）通常需要换模板，因为节点链路不一样。

## InvokeAI

`invokeai_graph.json`（**必需**）。InvokeAI 的接口在各版本间差异很大，本适配器
只支持「固定 graph 模板 + 队列两跳」，所以必须给一份 graph。

模板里用这三个占位符，运行时会被替换：

```
__PROMPT__     正向提示词（已做 JSON 转义）
__NEGATIVE__   负向提示词
__WIDTH__ / __HEIGHT__
```

⚠️ 这个后端**未实测**。如果它的接口形状和你的版本对不上，
`probe()` 会自报 `unsupported`，`doctor.py` 也会标出来 —— 请以官方文档为准。

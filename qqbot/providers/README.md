# providers —— 模型接入层

所有「调模型」的活都在这里。`bot.py` 里的 `Chat` / `Vision` 只是门面，
真正的 URL 拼接、鉴权、代理绕过、重试、用量统计都由这一层做。

## 为什么要这一层

重构前这一切写在 `bot.py` 里，而且写死了两个槽位：`config.local`（LM Studio）和
`config.cloud`（DeepSeek）。想换一家厂商得改代码，想同时接两家得复制粘贴。
现在换厂商 = 改配置，特殊协议 = 加一个适配器或插件。

## 目录

```
providers/
├── types.py        Endpoint / Capability / Request / ChatResult（纯数据，无 IO）
├── base.py         Provider 基类 + @register_provider
├── transport.py    HTTP：客户端缓存 + 本机绕过代理 + 出错记原文
├── usage.py        usage 字段归一化（各家字段名不一样）
├── declarative.py  清单驱动的通用适配器
├── manifest.py     清单加载与校验（坏清单只报错，不抛）
├── registry.py     三路装载 + 旧配置迁移 + 回退链构建
├── policy.py       识图策略：谁看图 / 缩到多宽 / 要不要转述
├── router.py       选路、回退链、并发闸、用量汇总、探针
├── plugins.py      Python 插件发现（白名单）
├── adapters/       内置协议适配器
└── manifests/      厂商声明式清单（JSON）
```

## 三条使用路径

```python
from providers import build_router

router = build_router(cfg)                      # 读 config.json
text, source = await router.answer(messages)    # 沿回退链要一句回复
```

### 1. 改配置：接一家 OpenAI 兼容的厂商（0 行代码）

```json
"providers": {
  "endpoints": [
    {"id": "kimi", "provider": "moonshot",
     "base_url": "https://api.moonshot.cn/v1", "model": "moonshot-v1-32k",
     "tier": "cloud", "api_key_env": "MOONSHOT_API_KEY"}
  ]
}
```

清单里已经备好了：DeepSeek、Kimi、智谱 GLM、通义 Qwen、硅基流动、OpenRouter、Groq、
xAI、MiniMax、零一万物、OpenAI 官方、文心千帆、讯飞星火、腾讯混元、百川、阶跃星辰，
以及 vLLM / llama.cpp / text-generation-webui / LocalAI / KoboldCpp / Xinference。

### 2. 写清单：形状像 OpenAI 但字段不同（0 行代码）

往 `providers/manifests/` 加一段 JSON，见 `docs/providers.md`。

### 3. 写插件：报文结构完全不同（几十行 Python）

见 [`plugins/README.md`](plugins/README.md)。已有内置适配器：
`openai` / `azure` / `anthropic` / `gemini` / `ollama` / `lmstudio`。

## 能力声明（Capability）

一个端点「会什么、不会什么」全部声明式表达，调用方不再写 if：

| 字段 | 作用 |
| --- | --- |
| `vision` / `vision_input` | 能不能看图、图片是传 URL 还是 base64 |
| `max_tokens_key` | 输出上限的**字段名**（支持点路径，如 Ollama 的 `options.num_predict`） |
| `max_tokens_limit` | 截断翻倍重试时的硬上限 |
| `temperature_range` | 越界会被**静默夹紧**，不会透传报错 |
| `system_in_messages` | Anthropic 为 `false`（system 提到顶层） |
| `usage` | 各家 usage 字段名的映射（含缓存计费字段） |
| `thinking` | 是否支持推理参数 |

## 回退链

```json
"chains": {
  "default": ["local-lmstudio", "cloud-deepseek"],
  "vision":  ["cloud-deepseek", "local-lmstudio"]
}
```

`answer()` 沿 `default` 依次试：某个端点**没密钥**会立即返回 `no-key` 并停止
（这是历史行为，改了会让「本地有模型但没配云端 key」的用户行为突变）；
全部失败返回 `failed`；有回复则返回文本 + 该端点的 tier（`local` / `cloud`）。

## 本机端点不走代理

`127.0.0.1` / `localhost` / `::1` / `0.0.0.0` 上的端点强制 `trust_env=False`，
否则环境变量里的 `HTTP_PROXY` 会把本机请求打死。局域网推理机（`192.168.x.x`）
请显式写 `"trust_env": false`。

## 配置变更后必须重载

`Router.reload(cfg)` 会重建端点、回收信号量、刷新密钥。
`bot.update_config()` 已经自动调它 —— 自己写代码改配置时别忘了。

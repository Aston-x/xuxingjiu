# 模型 Providers：怎么接一家新厂商

本文是「模型接入层」的使用手册。想看代码结构去 [`qqbot/providers/README.md`](../qqbot/providers/README.md)。

---

## 0. 一分钟决定你该走哪条路

| 你的情况 | 走哪条 | 工作量 |
| --- | --- | --- |
| 厂商提供 **OpenAI 兼容端点** | 改配置（第 2 节） | 改 4 行 JSON |
| 请求像 OpenAI，但 URL / 鉴权 / 字段名不同 | 写清单（第 3 节） | 写 20 行 JSON |
| 报文结构完全不同 / 需要签名 / 要先换 token | 写插件（第 4 节） | 几十行 Python |

先确认第一档：绝大多数厂商（含国内几家）都提供 `/chat/completions` 兼容端点，
翻一下它们的 API 文档里「OpenAI 兼容」那一节就行。

---

## 1. 配置放在哪

`qqbot/config.json` 里的 `providers` 段：

```jsonc
"providers": {
  "endpoints": [ /* 一个端点 = 一个可调用的模型服务 */ ],
  "chains":    { "default": ["端点id", "..."], "vision": ["..."] },
  "plugins":   { "dir": "providers/plugins", "enabled": [], "allow_dir_scan": false }
}
```

> 只写顶层 `local` / `cloud` 的老配置**仍然能用** —— 启动时会自动映射成
> `local-default` / `cloud-default` 两个端点并在日志里提示迁移。两套同时存在时以
> `providers` 段为准。

## 2. 只改配置：接一家 OpenAI 兼容厂商

```jsonc
"endpoints": [
  {
    "id": "kimi",                       // 唯一标识，chains 里用它引用
    "provider": "moonshot",             // 适配器名（见下表）
    "base_url": "https://api.moonshot.cn/v1",
    "model": "moonshot-v1-32k",
    "tier": "cloud",                    // local | cloud —— 决定失败原因字面量与代理策略
    "api_key_env": "MOONSHOT_API_KEY",  // ★ 密钥走环境变量，别写 api_key
    "timeout": 90,
    "max_tokens": 1024,
    "vision": true
  }
]
```

**可用的 `provider` 名**（内置 + 清单）：

| 类别 | 名字 |
| --- | --- |
| 协议适配器 | `openai` `azure` `anthropic` `gemini` `ollama` `lmstudio` |
| OpenAI 兼容云端 | `deepseek` `moonshot` `zhipu` `dashscope` `siliconflow` `openrouter` `groq` `xai` `minimax` `yi` `openai-official` |
| 国内厂商（兼容端点） | `ernie`（文心千帆）`spark`（讯飞星火）`hunyuan`（腾讯混元）`baichuan` `stepfun` |
| 本地推理 | `vllm` `llamacpp` `textgen` `localai` `koboldcpp` `xinference` |

拿不准某家兼容端点长什么样？直接抄 `providers/manifests/openai-compat.json` 里
对应的那条，看它的 `_note` 字段 —— 里面写了 `base_url` 该填什么。

**`base_url` 不自动补 `/v1`**：DeepSeek 是 `https://api.deepseek.com`（不带），
LM Studio 是 `http://127.0.0.1:1234/v1`（带）—— 两种都对，按厂商文档原样写。

### 回退链

```jsonc
"chains": {
  "default": ["local-lmstudio", "cloud-deepseek"],   // 日常答话：本地优先
  "vision":  ["cloud-deepseek", "local-lmstudio"]    // 识图：云端优先
}
```

规则：

- 按顺序试，前一个**抛异常**才轮到下一个；
- 某个端点**没配密钥**会立即返回 `no-key` 并**停止**（这是历史行为，改掉会让
  「本地有模型但没配云端 key」的用户行为突变）；
- 全挂返回 `failed`；成功返回文本 + 该端点的 `tier`。

---

## 3. 写清单：形状像 OpenAI 但字段名不同（零代码）

在 `qqbot/providers/manifests/` 下加一个 `.json`（或 `.toml`），在 `plugins.manifests`
里登记，然后就能在端点里用 `"provider": "你起的名字"`。

```jsonc
{
  "manifest_version": 1,
  "providers": [
    {
      "name": "myvendor",                       // ← 端点里 provider 写这个
      "protocol": "declarative",
      "display_name": "我家大模型",
      "auth": { "type": "bearer" },             // none | bearer | header | azure-key | query
      "request": {
        "path": "/chat/completions",            // 相对 base_url
        "method": "POST",
        "body": {
          "model": "$model",                    // $ 开头的是变量
          "messages": "$messages",
          "stream": false,
          "temperature": "$temperature",
          "max_tokens": "$max_tokens"
        }
      },
      "response": {
        "text": "$.choices[0].message.content", // JSONPath 风格，支持 [下标]
        "finish_reason": "$.choices[0].finish_reason"
      },
      "usage": {
        "prompt": "$.usage.prompt_tokens",
        "completion": "$.usage.completion_tokens",
        "cache_hit": "$.usage.prompt_cache_hit_tokens",
        "cache_miss": "$.usage.prompt_cache_miss_tokens"
      },
      "capabilities": {
        "vision": false, "vision_input": "none",
        "tool_calling": true, "streaming": true,
        "max_tokens_key": "max_tokens",
        "max_tokens_limit": 65536,
        "temperature_range": [0.0, 1.0]
      },
      "probe": { "path": "/models", "method": "GET", "parse": "openai_models" }
    }
  ]
}
```

可用变量：`$model` `$messages` `$temperature` `$max_tokens` `$stream` `$api_key`。
写错会**在加载时**被指出来（日志里报 `未知模板变量`），不会等到运行时才 400。

**`max_tokens_key` 支持点路径** —— 这是清单能覆盖 Ollama 这类嵌套参数的关键：

```jsonc
"body": { "options": { "temperature": "$temperature", "num_predict": "$max_tokens" } }
```

完整范例（含注释）见 `qqbot/providers/manifests/` 下三份清单。

---

## 4. 写插件：报文结构完全不同

见 [`qqbot/providers/plugins/README.md`](../qqbot/providers/plugins/README.md)，
里面有可抄的骨架、可覆盖的方法表，以及**安全边界**（插件是主进程内执行，白名单是唯一信任边界）。

---

## 5. 能力声明速查

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `vision` | `false` | 能不能收图。假的时候会把图换成文字占位（两套措辞，见 `base.py`） |
| `vision_input` | `none` | `url` / `base64` / `both` |
| `max_tokens_key` | `max_tokens` | 输出上限的**字段名**，支持点路径 |
| `max_tokens_limit` | 无 | 截断翻倍重试的硬上限（不夹住会被 400 顶回来） |
| `temperature_range` | `[0,2]` | 越界**静默夹紧**，不透传报错 |
| `system_in_messages` | `true` | Anthropic 是 `false`（system 提到顶层） |
| `thinking` | `false` | 是否支持推理参数（如 DeepSeek 的 `thinking`） |
| `usage.*` | 见上 | usage 字段名的映射，决定面板上的缓存命中率 |

端点里写 `capabilities` 可以**逐端点覆盖**厂商默认值，比如给 GPT-5 系端点写
`"capabilities": {"max_tokens_key": "max_completion_tokens"}`。

## 6. 常见坑

1. **别把厂商私有参数写进通用 body**。`thinking` 只有 DeepSeek 认，塞给 Groq 会 400。
   私有参数写在**端点级** `params` 里：
   `"params": {"thinking": {"type": "disabled"}}`。
2. **本机端点自动绕过代理**（`127.0.0.1` / `localhost` / `::1` / `0.0.0.0`）。
   局域网推理机（`192.168.x.x`）要显式写 `"trust_env": false`，否则会被公司代理劫持。
3. **改完配置要重载**。走控制台改的会自动重载；手工改文件后请重启进程，
   或者调用 `Router.reload(cfg)`。
4. **密钥不要写进配置文件**。用 `api_key_env`，配置里只留变量名。
5. **插件改了要重启**。`provider_reload` 只重读清单与配置，插件模块有 `sys.modules` 缓存。

## 7. 从旧版 `local`/`cloud` 迁移

老配置长这样：

```jsonc
"local": { "enable": true, "base_url": "http://127.0.0.1:1234/v1", "model": "...", "vision": true },
"cloud": { "enable": true, "base_url": "https://api.deepseek.com", "model": "...", "api_key": "sk-..." }
```

映射规则：

| 旧键 | 新位置 |
| --- | --- |
| `local.enable` | `endpoints[].enabled` |
| `local.timeout_seconds` | `endpoints[].timeout` |
| `local.concurrency` | `endpoints[].concurrency` |
| `cloud.thinking` | `endpoints[].params.thinking` |
| `cloud.api_key` | 建议改 `api_key_env` |
| `vision_relay=true` | `chains.default = [云端, 本地]` |
| `vision_relay=false` + `local.enable` | `chains.default = [本地, 云端]` |
| `vision.backend` | 决定 `chains.vision`（`off` 时空链） |

**不迁移也能跑**：顶层 `local`/`cloud` 一直有效，启动日志会打印映射结果。

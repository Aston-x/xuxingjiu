# 许杏玖 · Xuxingjiu

> 一只住在旧书店的**黑猫猫娘 QQ 机器人**，带独立生活轨迹、双层记忆与可插拔的模型接入层。
> Python 主体（`qqbot`）+ TypeScript 的 QQ 空间桥（`qzone-bridge`）。

⚠️ **免责声明**：本项目仅供学习与研究。使用其中的 QQ 空间相关代码可能违反腾讯服务条款，
仓库内不提供任何账号、Cookie 或密钥，请自行承担风险。详见 [NOTICE.md](NOTICE.md)。

[English](README.md) · [中文](README.zh-CN.md)

---

## 它是什么

一个跑在自己电脑上的非官方 QQ 机器人。不是"接个大模型就完事"的套壳，而是围绕一个
**具体角色**做的完整体验：

- **人设一致性优先** —— 身份守卫、反元叙述、反幻觉（看不到图就说看不到，不许编）
- **她有自己的生活** —— 作息表、心情档位、好感度、自己在做什么（翻旧书 / 趴窗台 / 盯虫子）
- **双层记忆** —— 按 QQ 号记人（跨群与私聊共享）+ 群记忆，带冲突消解规则
- **能看图、能生图、能刷 B 站、能发说说** —— 图片理解、SD 生图、B 站浏览与点赞投币收藏、QQ 空间互动
- **模型接入层可插拔** —— 本地 / 云端 / 任意第三方，改配置就能换，特殊协议写成插件

## 架构

```
        ┌──────────────┐   反向 WS     ┌──────────────────────┐
        │   NapCat     │ ───────────▶ │  qqbot (Python)       │
        │  (第三方注入) │   :6199      │  ├─ providers/  模型层 │──▶ 本地推理 / 云端 API
        └──────┬───────┘               │  ├─ 记忆 / 好感 / 作息  │
               │                       │  ├─ 识图 / 生图 / B站   │──▶ SD WebUI
        ┌──────▼───────┐   HTTP :5700  │  └─ Web 控制台 :6200   │
        │ qzone-bridge │ ◀───────────▶ │                        │
        │ (TypeScript) │   说说/评论/赞 └────────────────────────┘
        └──────┬───────┘
               ▼
          QQ 空间 Web API
```

## 快速开始

**一条命令装好**（先探测本机环境、缺什么补什么，再建 venv、装依赖、生成配置、跑体检）：

```bash
# Windows：双击 install.bat，或者
powershell -NoProfile -ExecutionPolicy Bypass -File install.ps1

# Linux / macOS：
bash install.sh
```

它会先认出操作系统 / 发行版 / 包管理器 / Python / Node / git 和网络状况，再补齐缺的：
动系统包管理器之前会问一句（`--yes` 跳过询问，`--no-system` 一律不碰），
官方源连不上时自动切国内镜像。只想看看本机环境、不装任何东西：

```bash
bash install.sh --detect          # Windows：install.ps1 -DetectOnly
```

然后改 `qqbot/config.json` 里三处，就能起来了：

| 键 | 填什么 |
| --- | --- |
| `bot_qq` | 机器人 QQ 号 |
| `admin.user_ids` | 你自己的 QQ 号 |
| `access_token` | 自己编一串随机字符（QQ 接入端要填一样的） |

模型密钥走环境变量（别写进文件）：

```bash
# Windows
set DEEPSEEK_API_KEY=sk-xxxx
# Linux / macOS
export DEEPSEEK_API_KEY=sk-xxxx
```

再装 QQ 接入端并启动：

```bash
# Windows：NapCat（见 NapCat/README.md），然后双击 deploy\start-all.pyw
# Linux / macOS：见 docs/部署-Linux.md，然后 bash deploy/start-all.sh
```

控制台默认在 <http://127.0.0.1:6200/>，令牌见 `qqbot/state/console-token`。

> 装完哪里不对？跑 `python qqbot/tools/doctor.py` —— 它会逐项告诉你
> 「哪一项没配好、为什么、怎么修」，比翻日志快。

## 模型接入层（本项目的重点）

重构后，「调模型」这件事从 `bot.py` 里抽成了独立的 [`qqbot/providers/`](qqbot/providers/README.md)。
换一家厂商通常**只需要改配置**：

```jsonc
"providers": {
  "endpoints": [
    { "id": "local",  "provider": "lmstudio", "tier": "local",
      "base_url": "http://127.0.0.1:1234/v1", "model": "qwen2.5-vl-7b-instruct",
      "concurrency": 1, "vision": true },

    { "id": "cloud",  "provider": "deepseek",  "tier": "cloud",
      "base_url": "https://api.deepseek.com", "model": "deepseek-chat",
      "api_key_env": "DEEPSEEK_API_KEY" }
  ],
  "chains": {
    "default": ["local", "cloud"],     // 本地优先，挂了退云端
    "vision":  ["cloud", "local"]      // 识图反过来
  }
}
```

**已内置**：

| 类别 | 覆盖 |
| --- | --- |
| 协议适配器（Python） | OpenAI 兼容、Azure OpenAI、Anthropic Messages、Google Gemini、Ollama 原生、LM Studio 原生 |
| 声明式清单（JSON，零代码） | DeepSeek、Kimi、智谱 GLM、通义 Qwen、硅基流动、OpenRouter、Groq、xAI、MiniMax、零一万物、OpenAI、文心千帆、讯飞星火、腾讯混元、百川、阶跃星辰；vLLM、llama.cpp、text-generation-webui、LocalAI、KoboldCpp、Xinference |
| 插件（Python，白名单加载） | 任何报文结构不同的厂商，几十行搞定 |

设计要点：**能力声明**（能不能看图、输出上限叫什么字段、温度范围）、**回退链**、
**本机端点自动绕过 HTTP_PROXY**、**usage 归一化**（换厂商不会让缓存命中率静默归零）。

详见 [docs/providers.md](docs/providers.md) 与 [qqbot/providers/README.md](qqbot/providers/README.md)。

## 目录

| 路径 | 说明 |
| --- | --- |
| `qqbot/` | Python 主体：人设、记忆、作息、识图、生图、B 站、Web 控制台、模型接入层 |
| `qqbot/providers/` | 模型接入层（适配器 / 清单 / 插件 / 路由 / 识图策略） |
| `qzone-bridge/` | TypeScript：把 QQ 空间的能力暴露成 OneBot v11 接口 |
| `NapCat/` | 只放配置模板与部署脚本，本体请从上游下载 |
| `docs/` | 部署、配置、Provider 教程、常见坑 |

## 测试

```bash
cd qqbot
.venv\Scripts\python.exe test_regression.py    # 全量回归（含 Provider 段），当前 685 项
.venv\Scripts\python.exe test_providers.py     # 只管模型层，纯离线（MockTransport）
```

两条命令都**不联网、不碰真实状态文件**（测试会把可写路径重定向到临时目录，并在结尾自检）。

## 许可

[MIT](LICENSE)。第三方组件与参考实现的致谢见 [NOTICE.md](NOTICE.md)。

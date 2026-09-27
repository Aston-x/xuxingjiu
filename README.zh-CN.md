# 许杏玖 · Xuxingjiu

许杏玖住在旧书店里，是只黑猫娘。这个仓库就是她本人：一个跑在你自己电脑上的 QQ 机器人，
有作息表、心情档位、双层记忆，模型接入层想指哪儿指哪儿，本机跑个 7B 或者直接连 DeepSeek 都行。

代码分两块。`qqbot/` 是 Python 主体，`qzone-bridge/` 是 TypeScript 写的 QQ 空间桥，
把空间的能力暴露成 OneBot v11 接口。

仅供学习与研究。QQ 空间那部分接口是逆出来的，用起来有违反腾讯服务条款的风险。
仓库里不含任何账号、Cookie 或密钥，详见 [NOTICE.md](NOTICE.md)。

[English](README.md) · [中文](README.zh-CN.md)

---

## 它是什么

这类机器人通常的做法是接个大模型、套个提示词就完事。这个反过来：先有一个具体角色，
别的都是为了让这个角色立得住。

- 人设稳住。有身份守卫，不元叙述，不许编。看不到图就说看不到。
- 她有自己的生活。作息表、心情档位、好感度，还有她手头在干的事
  （翻旧书、趴窗台、盯一只虫子）。
- 记忆分两层：按 QQ 号记人，跨群和私聊共享；再加一层群记忆。两层对不上时有消解规则。
- 能看图、能生图、能刷 B 站、能发说说。
- 模型接入层可插拔。换厂商一般只要改配置；报文结构不一样的，写成插件就行。

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

一条命令装好。它会先看你这台机器，缺什么补什么，然后建 venv、装依赖、生成配置、跑体检。

```bash
# Windows：双击 install.bat，或者
powershell -NoProfile -ExecutionPolicy Bypass -File install.ps1

# Linux / macOS：
bash install.sh
```

探测的范围是系统、发行版、包管理器、Python、Node、git 和网络。动系统包管理器之前会问一句
（`--yes` 跳过询问，`--no-system` 一律不碰），官方源连不上就自动切国内镜像。
只想看看报告、什么都不动：

```bash
bash install.sh --detect          # Windows：install.ps1 -DetectOnly
```

然后改 `qqbot/config.json` 里三处，就能起来了：

| 键 | 填什么 |
| --- | --- |
| `bot_qq` | 机器人 QQ 号 |
| `admin.user_ids` | 你自己的 QQ 号 |
| `access_token` | 自己编一串随机字符（QQ 接入端要填一样的） |

模型密钥走环境变量，别写进这个文件：

```bash
# Windows
set DEEPSEEK_API_KEY=sk-xxxx
# Linux / macOS
export DEEPSEEK_API_KEY=sk-xxxx
```

最后装 QQ 接入端并启动：

```bash
# Windows：NapCat（见 NapCat/README.md），然后双击 deploy\start-all.pyw
# Linux / macOS：见 docs/部署-Linux.md，然后 bash deploy/start-all.sh
```

控制台在 <http://127.0.0.1:6200/>，令牌见 `qqbot/state/console-token`。

哪里不对就跑 `python qqbot/tools/doctor.py`。它会逐项告诉你哪一项没配好、为什么、怎么改，
比翻日志快。

## 模型接入层

「调模型」这件事以前长在 `bot.py` 里，现在抽成了独立的
[`qqbot/providers/`](qqbot/providers/README.md)。换一家厂商通常只要改配置：

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

**已内置：**

| 类别 | 覆盖 |
| --- | --- |
| 协议适配器（Python） | OpenAI 兼容、Azure OpenAI、Anthropic Messages、Google Gemini、Ollama 原生、LM Studio 原生 |
| 声明式清单（JSON，零代码） | DeepSeek、Kimi、智谱 GLM、通义 Qwen、硅基流动、OpenRouter、Groq、xAI、MiniMax、零一万物、OpenAI、文心千帆、讯飞星火、腾讯混元、百川、阶跃星辰；vLLM、llama.cpp、text-generation-webui、LocalAI、KoboldCpp、Xinference |
| 插件（Python，白名单加载） | 任何报文结构不同的厂商，几十行搞定 |

几个当时想了一会儿的地方：端点自己声明能力（能不能看图、输出上限叫什么字段、温度范围）；
回退链负责兜底；本机端点自动绕过 `HTTP_PROXY`；usage 归一化，换厂商不会让缓存命中率静默归零。

详见 [docs/providers.md](docs/providers.md) 与
[qqbot/providers/README.md](qqbot/providers/README.md)。

## 目录

| 路径 | 说明 |
| --- | --- |
| `qqbot/` | Python 主体：人设、记忆、作息、识图、生图、B 站、Web 控制台、模型接入层 |
| `qqbot/providers/` | 模型接入层：适配器、清单、插件、路由、识图策略 |
| `qzone-bridge/` | TypeScript：把 QQ 空间的能力暴露成 OneBot v11 接口 |
| `NapCat/` | 只放配置模板与部署脚本，本体请从上游下载 |
| `docs/` | 部署、配置、Provider 教程、常见坑 |

## 文档

| 文档 | 讲什么 |
| --- | --- |
| [docs/使用说明.md](docs/使用说明.md) | 日常怎么用：命令、记忆、人物 |
| [docs/配置说明.md](docs/配置说明.md) | 配置项逐条过 |
| [docs/部署.md](docs/部署.md) | 部署入口（Windows / Linux） |
| [docs/常见坑.md](docs/常见坑.md) | 实际踩过的坑，以及成因 |
| [docs/生图.md](docs/生图.md) | 生图后端怎么选、怎么配 |
| [docs/providers.md](docs/providers.md) | 模型接入层的细节 |
| [docs/开源指南.md](docs/开源指南.md) | 这个项目怎么发出去 |
| [docs/GitHub-开源教程.md](docs/GitHub-开源教程.md) | 同样的流程，不挑项目 |
| [qzone-bridge/doc/](qzone-bridge/doc/README.md) | QQ 空间接口笔记与逆向流程 |

## 测试

```bash
cd qqbot
.venv\Scripts\python.exe test_regression.py    # 全量回归，818 项
.venv\Scripts\python.exe test_providers.py     # 只管模型层，纯离线
.venv\Scripts\python.exe test_imagegen.py      # 生图各条路径
```

```bash
cd qzone-bridge
npm run typecheck
npm test                                       # 单测，196 项
```

都不联网，也不碰你的真实状态文件：测试会把可写路径重定向到临时目录，跑完再自检一遍有没有漏出去。

## 许可

[MIT](LICENSE)。第三方组件与参考实现的致谢见 [NOTICE.md](NOTICE.md)。

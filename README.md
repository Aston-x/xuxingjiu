# Xuxingjiu · 许杏玖

许杏玖 is a black cat-girl who lives in a second-hand bookshop. This repository is her:
a QQ bot that runs on your own machine, with a daily routine, a mood that shifts, a
two-layer memory, and a model layer you can point at anything from a local 7B to DeepSeek.

There are two halves. `qqbot/` is the Python core. `qzone-bridge/` is the TypeScript side
that turns QQ空间 into OneBot v11 actions.

For study and research only. The QZone code is reverse-engineered, so using it may violate
Tencent's terms of service. No accounts, cookies or API keys are included here. See
[NOTICE.md](NOTICE.md).

[English](README.md) · [中文](README.zh-CN.md)

---

## What it is

Most bots like this are an LLM API with a prompt glued on. This one is built the other way
round: there is a specific character first, and everything else exists to keep her
consistent.

- She stays in character. An identity guard, no meta-narration, and no making things up.
  If she cannot see an image, she says she cannot see it.
- She has her own life. A schedule, mood tiers, affinity scores, and things she is doing
  (browsing the old books, sitting on the windowsill, watching a bug).
- Memory has two layers: per person, shared across groups and DMs, and per group. There are
  rules for when the two disagree.
- She can look at pictures, draw, browse Bilibili, and post to QQ空间.
- The model layer is pluggable. Switching vendors is usually a config edit. A vendor with
  its own wire format becomes a small plugin.

## Architecture

```
        ┌──────────────┐  reverse WS  ┌──────────────────────┐
        │   NapCat     │ ───────────▶ │  qqbot (Python)       │
        │  (3rd party) │   :6199      │  ├─ providers/  models │──▶ local / cloud APIs
        └──────┬───────┘              │  ├─ memory / affinity  │
               │                      │  ├─ vision / SD / B站  │──▶ SD WebUI
        ┌──────▼───────┐  HTTP :5700  │  └─ Web console :6200  │
        │ qzone-bridge │ ◀──────────▶ │                        │
        │ (TypeScript) │   feeds/likes └───────────────────────┘
        └──────┬───────┘
               ▼
        QZone Web API
```

## Quick start

One command sets everything up. It looks at your machine first, installs whatever is
missing, creates the venv, installs dependencies, writes a config, and runs a health check.

```bash
# Windows: double-click install.bat, or
powershell -NoProfile -ExecutionPolicy Bypass -File install.ps1

# Linux / macOS:
bash install.sh
```

The probe covers the OS, distro, package manager, Python, Node, git and the network. It
asks before it touches a system package manager (`--yes` skips the question, `--no-system`
never touches it at all), and falls back to a Chinese mirror when the official index is
unreachable. If you only want to see the report and change nothing:

```bash
bash install.sh --detect          # Windows: install.ps1 -DetectOnly
```

Then fill in three keys in `qqbot/config.json`:

| Key | Value |
| --- | --- |
| `bot_qq` | the bot's QQ number |
| `admin.user_ids` | your own QQ number |
| `access_token` | any random string (the QQ front-end must use the same one) |

API keys live in environment variables, not in that file:

```bash
# Windows
set DEEPSEEK_API_KEY=sk-xxxx
# Linux / macOS
export DEEPSEEK_API_KEY=sk-xxxx
```

Last step is a QQ front-end plus the launcher:

```bash
# Windows: NapCat (see NapCat/README.md), then double-click deploy\start-all.pyw
# Linux / macOS: see docs/部署-Linux.md, then bash deploy/start-all.sh
```

The web console is at <http://127.0.0.1:6200/>; its token is in
`qqbot/state/console-token`.

If something is off, run `python qqbot/tools/doctor.py`. It tells you which piece is
unconfigured, why, and what to do about it, which beats reading the logs.

## The model-access layer

Calling a model used to live inside `bot.py`. It is now its own package,
[`qqbot/providers/`](qqbot/providers/README.md), and switching vendors is usually a config
edit:

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
    "default": ["local", "cloud"],     // local first, fall back to cloud
    "vision":  ["cloud", "local"]      // the other way round for images
  }
}
```

**What ships in the box:**

| Kind | Coverage |
| --- | --- |
| Python protocol adapters | OpenAI-compatible, Azure OpenAI, Anthropic Messages, Google Gemini, native Ollama, native LM Studio |
| Declarative JSON manifests, no code needed | DeepSeek, Kimi, Zhipu GLM, Qwen, SiliconFlow, OpenRouter, Groq, xAI, MiniMax, Yi, OpenAI, Baidu Qianfan, iFlytek Spark, Tencent Hunyuan, Baichuan, StepFun; vLLM, llama.cpp, text-generation-webui, LocalAI, KoboldCpp, Xinference |
| Python plugins, allow-listed | anything with a different wire format, usually a few dozen lines |

A few details that took thinking: endpoints declare what they can do (vision support, the
name of the output-cap field, the temperature range they accept); chains give you fallback;
loopback endpoints bypass `HTTP_PROXY` automatically; and token usage is normalised so
switching vendors does not silently zero your cache-hit rate.

More in [docs/providers.md](docs/providers.md) and
[qqbot/providers/README.md](qqbot/providers/README.md).

## Layout

| Path | What |
| --- | --- |
| `qqbot/` | Python core: persona, memory, routine, vision, image generation, Bilibili, web console, model layer |
| `qqbot/providers/` | Model layer: adapters, manifests, plugins, router, vision policy |
| `qzone-bridge/` | TypeScript: exposes QZone features as OneBot v11 actions |
| `NapCat/` | Config templates and deploy scripts only. Get the runtime from upstream |
| `docs/` | Deployment, configuration, provider tutorial, pitfalls (Chinese) |

## Docs

Everything below is written in Chinese.

| Doc | What |
| --- | --- |
| [docs/使用说明.md](docs/使用说明.md) | Day-to-day use: commands, memory, people |
| [docs/配置说明.md](docs/配置说明.md) | Every key in the config, one by one |
| [docs/部署.md](docs/部署.md) | Deployment index (Windows / Linux) |
| [docs/常见坑.md](docs/常见坑.md) | The failures we actually hit, and their causes |
| [docs/生图.md](docs/生图.md) | Image generation backends |
| [docs/providers.md](docs/providers.md) | The model layer in depth |
| [qzone-bridge/doc/](qzone-bridge/doc/README.md) | QZone API notes and the reverse-engineering workflow |

## Tests

```bash
cd qqbot
.venv\Scripts\python.exe test_regression.py    # full regression, 818 checks
.venv\Scripts\python.exe test_providers.py     # model layer only, offline
.venv\Scripts\python.exe test_imagegen.py      # image generation paths
```

```bash
cd qzone-bridge
npm run typecheck
npm test                                       # unit tests, 196 checks
```

All of it runs offline and never touches your real state files. The tests redirect every
writable path into a temp directory and check afterwards that it stayed there.

## License

[MIT](LICENSE). Third-party credits are in [NOTICE.md](NOTICE.md).

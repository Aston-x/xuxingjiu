# Xuxingjiu · 许杏玖

> A **black cat-girl QQ chatbot** who lives in a second-hand bookshop — with her own daily
> routine, a two-layer memory system, and a **pluggable model-access layer**.
> Python core (`qqbot`) + a TypeScript QZone↔OneBot bridge (`qzone-bridge`).

⚠️ **Disclaimer**: this project is for **study and research only**. Using the QZone-related
code may violate Tencent's Terms of Service. No accounts, cookies, or API keys are included.
See [NOTICE.md](NOTICE.md).

[English](README.md) · [中文](README.zh-CN.md)

---

## What it is

An unofficial QQ chatbot that runs on your own machine. It is not a thin wrapper around an
LLM API — it is built around one specific character:

- **Persona consistency first** — identity guard, no meta-narration, no hallucination
  (if she cannot see an image, she says so instead of making things up)
- **She has her own life** — a schedule, mood tiers, affinity scores, and her own activities
- **Two-layer memory** — per-person (shared across groups and DMs) + per-group, with
  conflict-resolution rules
- **Can see images, draw, browse Bilibili, post to QZone**
- **Pluggable model layer** — local / cloud / any third party; usually just a config change,
  exotic protocols become plugins

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

```bash
# 1) bot core
cd qqbot
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt     # Windows
cp config.example.json config.json                              # then edit it
#    At minimum: bot_qq (the bot account) and admin.user_ids (your account)
#    Keep API keys in environment variables, never in config.json
set DEEPSEEK_API_KEY=sk-xxxx                                    # PowerShell: $env:DEEPSEEK_API_KEY="sk-xxxx"

# 2) QZone bridge (optional)
cd ../qzone-bridge
npm install
cp .env.example .env

# 3) NapCat (third party — download it yourself)
#    See docs/部署.md (Chinese)

# 4) launch — double-click, no console window
double-click qqbot\启动.pyw      # or: python qqbot/launcher.py start
```

The web console is at <http://127.0.0.1:6200/>; the token lives in `qqbot/state/console-token`.

## The model-access layer

After the refactor, "calling a model" no longer lives inside `bot.py` — it is its own package,
[`qqbot/providers/`](qqbot/providers/README.md). Switching vendors is usually **a config change**:

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

**Built in:**

| Kind | Coverage |
| --- | --- |
| Python protocol adapters | OpenAI-compatible, Azure OpenAI, Anthropic Messages, Google Gemini, native Ollama, native LM Studio |
| Declarative JSON manifests (no code) | DeepSeek, Kimi, Zhipu GLM, Qwen, SiliconFlow, OpenRouter, Groq, xAI, MiniMax, Yi, OpenAI, Baidu Qianfan, iFlytek Spark, Tencent Hunyuan, Baichuan, StepFun; vLLM, llama.cpp, text-generation-webui, LocalAI, KoboldCpp, Xinference |
| Python plugins (allow-listed) | anything with a different wire format — a few dozen lines |

Design notes: **capability declarations** (vision support, the field name for the output cap,
temperature range), **fallback chains**, **loopback endpoints automatically bypass
`HTTP_PROXY`**, and **usage normalisation** (so switching vendors does not silently zero your
cache-hit rate).

See [docs/providers.md](docs/providers.md) (Chinese) and
[qqbot/providers/README.md](qqbot/providers/README.md).

## Layout

| Path | What |
| --- | --- |
| `qqbot/` | Python core: persona, memory, routine, vision, image generation, Bilibili, web console, model layer |
| `qqbot/providers/` | Model layer (adapters / manifests / plugins / router / vision policy) |
| `qzone-bridge/` | TypeScript: exposes QZone features as OneBot v11 actions |
| `NapCat/` | Config templates and deploy scripts only — download the runtime from upstream |
| `docs/` | Deployment, configuration, provider tutorial, pitfalls (Chinese) |

## Tests

```bash
cd qqbot
.venv\Scripts\python.exe test_regression.py    # full regression incl. provider section — 685 checks
.venv\Scripts\python.exe test_providers.py     # model layer only, fully offline (MockTransport)
```

Both run **offline** and never touch real state files (writable paths are redirected to a temp
directory and verified afterwards).

## License

[MIT](LICENSE). Third-party credits in [NOTICE.md](NOTICE.md).

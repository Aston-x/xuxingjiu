# qqbot —— 许杏玖的 Python 主体

完整介绍见仓库根目录的 [README.zh-CN.md](../README.zh-CN.md)。

## 这里有什么

| 文件 / 目录 | 说明 |
| --- | --- |
| `bot.py` | 主体：WebSocket 接入、人设、记忆、作息、识图、生图、B 站、命令处理 |
| `providers/` | **模型接入层** —— 本地 / 云端 / 第三方的统一入口，见其 [README](providers/README.md) |
| `console_server.py` | 本地 Web 控制台后端（只读面板 + 少量写操作，带密钥脱敏） |
| `public/console.html` | 控制台前端（单文件，无构建步骤） |
| `launcher.py` | 静默启动 / 停止 / 状态（不弹黑窗口，绝不杀 QQ 进程） |
| `启动.pyw` `停止.pyw` `打开控制台.pyw` | 双击即用的入口 |
| `config.example.json` | 配置模板 —— 复制成 `config.json` 再改 |
| `persona.example.json` | 人设模板 —— 想换成自己的角色时改这里（可选） |
| `test_regression.py` | 全量回归（含模型层），离线、不碰真实状态文件 |
| `test_providers.py` | 模型层单测，纯离线（MockTransport） |
| `tools/` | 辅助脚本：探模型、采样回复、控制台冒烟测试等 |

## 最小启动

```bash
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
cp config.example.json config.json      # 改 bot_qq / admin.user_ids，密钥走环境变量
.venv\Scripts\python.exe bot.py         # 或双击 启动.pyw（无窗口 + 自动守护）
```

控制台：<http://127.0.0.1:6200/>（令牌在 `state/console-token`）。

## 两条硬规则

1. **不要 `taskkill /IM QQ.exe`** —— 你的日常 QQ 与机器人号是同一个 `QQ.exe`，
   按镜像名杀会把大号一起带走。启动器只按端口定位进程，QQ 永不在候选里。
2. **`config.json` 不进版本库** —— 里面有明文密钥。`.gitignore` 已经挡住，
   但你自己新建分支 / 换机器时留意一下。

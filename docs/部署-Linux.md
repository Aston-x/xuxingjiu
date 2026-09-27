# 部署：从零跑起来（Linux / macOS）

> Windows 看 [部署-Windows.md](部署-Windows.md)。
> **模型 / 生图 / 配置**这三块两平台完全一样，不重复写，只在本文里说清
> 「路径写法」和「后台运行」的差异。

---

## 0. 先明确一件事：QQ 接入端

`qqbot` 只实现 **OneBot v11 的反向 WebSocket 服务端**（监听 `ws_port`，默认 6199），
它跟具体接入端无关。而 **NapCat 在 Windows 上是注入式的**（hook QQ 客户端进程），
**在 Linux/macOS 上不能按那套方式跑**。

所以这一篇的重点是：**你在这两个系统上用什么来接 QQ**。

### 接入端推荐表

| 平台 | 推荐 | 启动形态 | 备注 |
| --- | --- | --- | --- |
| Linux | **NapCat（Docker）** | `docker run …` + `network_mode: host` | 最省事；**需核实**上游镜像的当前标签与配置方式 |
| Linux | Lagrange.Core / Lagrange.OneBot | `./Lagrange.OneBot`（独立进程） | 独立进程、不是注入式，配置字段名与 NapCat 不同；**需核实**其当前维护状态与 `Message/ReverseWebSocket` 字段 |
| macOS | Docker Desktop 跑上面的镜像，或 Lagrange | 同上 | **需核实** |
| 任意 | 任何实现了 OneBot v11 的端 | 你自己启动 | 只要它能反向连过来就行 |

### 对接口径（所有接入端都一样）

```
接入端  --反向 WS-->  ws://<ws_host>:<ws_port>/ws      （默认 127.0.0.1:6199）
                      token == config.json 里的 access_token
```

**字段名映射（同一个语义，各家叫法不同）**：

| 接入端 | 配置位置 | 字段 |
| --- | --- | --- |
| NapCat | `NapCat/config/onebot11_<QQ>.json` | `network.websocketClients[].{url, token}` |
| Lagrange | `appsettings.json` | `Message.ReverseWebSocket.{Host, Port, AccessToken}` |
| LLOneBot | 它的 WebUI | 「反向 WS 地址 / 令牌」 |
| 其它 | — | 找 `ws url` + `access token` 这两个概念即可 |

> 本项目的启动器会读 `config.json` 的 `onebot.adapter`：
> `auto`（默认，按目录探测）、`napcat`（强制 NapCat）、`none`（**你自己管接入端，启动器完全不碰它**）。
> 用 Docker 或 Lagrange 时建议写 `none` —— 让启动器只管 bot 与空间桥。

### Docker 的一个坑

容器里的 `127.0.0.1` **不是**宿主机的 `127.0.0.1`。两种解法：

1. `network_mode: host`（Linux 上最省事）；
2. 用 bridge 网络 + 把 qqbot 的 `ws_host` 改成 `0.0.0.0`，接入端连宿主机的实际 IP。

⚠️ 改成 `0.0.0.0` 意味着 **6199 对局域网开放**，一定要同时把 `access_token`
设成一个足够长的随机串（它就是这个 WebSocket 的唯一鉴权）。

---

## 1. 环境

- Linux 发行版 / macOS 12+
- Python **3.11+**（`python3 --version`；Ubuntu 22.04 自带的 3.10 不够）
- Node.js 18+（可选，只在用空间桥接时需要）

```bash
# Debian/Ubuntu
sudo apt update && sudo apt install -y python3 python3-venv python3-pip git

# macOS（Homebrew）
brew install python@3.12 node git
```

---

## 2. 装机器人主体

```bash
cd qqbot
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt     # ← 注意是 bin/ 不是 Scripts/
cp config.example.json config.json
```

改 `config.json` 里至少这几项（和 Windows 完全一样）：

```jsonc
{
  "bot_qq": 10001,                          // 机器人 QQ 号
  "access_token": "自己编的随机串",           // 接入端也要填一样的
  "admin": { "user_ids": [10002] },         // 你自己的 QQ
  "ban":   { "protected_ids": [10002] },
  "onebot": { "adapter": "none" }           // 用 Docker/Lagrange 时写 none
}
```

模型端点配在 `providers` 段，密钥走环境变量：

```bash
export DEEPSEEK_API_KEY="sk-..."
# 长期生效就写进 ~/.bashrc 或 ~/.zshrc；用 systemd 则写进服务的 Environment=
```

### 先单独验证她能说话

```bash
.venv/bin/python bot.py
# 看到「服务已启动 ws://127.0.0.1:6199 (等待 NapCat 反向连接)」就对了
```

服务启动时会打印一段**自检结果**（对话模型 / 识图 / 生图 / 空间桥 / 接入端各一项，
带 ✅⚠️❌）。有 ❌ 就照后面的提示修，别急着接 QQ。

---

## 3. 装空间桥接（可选）

```bash
cd ../qzone-bridge
npm ci                  # 有 package-lock.json，比 npm install 可复现
cp .env.example .env    # 然后填 QQ 空间 Cookie
npm run build           # 产出 dist/main.js（启动器优先跑它，省掉 tsx）
```

`.env` 里的 Cookie 是**账号级凭据**，等同于账号密码。别提交、别外传。

---

## 4. 启动

```bash
bash deploy/start-all.sh
```

它会依次补启 qqbot → 空间桥 → （如果是 NapCat）接入端。
关掉终端不会把 bot 带走（`launcher.py` 里用了 `start_new_session`）。

### 让它开机自启（推荐）

```bash
mkdir -p ~/.config/systemd/user
cp deploy/qqbot.service.example ~/.config/systemd/user/qqbot.service
# 编辑里面的路径（%h/xuxingjiu/... 换成你自己的）
systemctl --user daemon-reload
systemctl --user enable --now qqbot
journalctl --user -u qqbot -f        # 看日志
sudo loginctl enable-linger "$USER"  # 注销后也让服务继续跑
```

> `qqbot.service.example` 里直接前台跑 `bot.py`，**不用** launcher 的那套守护 ——
> 监督交给 systemd（`Restart=on-failure`）比自己的循环更可靠。
> 接入端（Docker / Lagrange）建议单独做一个服务或容器。

---

## 5. 无头环境（没有显示器）

- **空间桥接**如果需要扫码登录会用到 Playwright + 浏览器：`npx playwright install chromium`；
  没有 `$DISPLAY` 时它会自动切 headless（`QZONE_PLAYWRIGHT_HEADLESS` 可以强制）。
- **生图后端**（SD WebUI / ComfyUI）通常是独立服务，按它们自己的文档部署即可。
- 本地大模型建议跑在别的机器或容器上，把 `providers.endpoints[].base_url` 指过去，
  并且**显式写 `"trust_env": false`** —— 局域网地址不在"本机自动绕过代理"的名单里。

---

## 6. 常见问题

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `launcher.py` 报找不到 netstat/tasklist | 旧版本的行为 | 已修：探测层现在按平台选 `ss`/`lsof`，装个 `psutil` 会更准（可选） |
| `单实例锁` 报"已有实例在运行" | 上一次进程没退干净 | 锁是 `flock`，进程真死了会自动释放；确认没有残留进程再启动 |
| 接入端连不上 | `access_token` 或端口不一致 | 两边对齐；容器场景注意 127.0.0.1 的问题（见 §0） |
| 面板打不开 | 只监听 127.0.0.1 | 这是刻意的；要远程访问请用 SSH 端口转发 |
| 生图一直失败 | 后端没起 / 没配密钥 | 跑 `python tools/doctor.py` 看具体是哪一项 |

---

## 7. 还是不行

```bash
.venv/bin/python tools/doctor.py           # 逐项体检（❌/⚠️ + 修复建议）
.venv/bin/python tools/doctor.py --json    # 给脚本消费
.venv/bin/python tools/doctor.py --deep    # 额外探一次本地后端（可能耗时/计费）
```

`doctor` 会把「哪一项没配、哪里连不上」直接写出来，比翻日志快。

# NapCat 骨架（第三方组件，本体需自行下载）

这里**只有配置模板与部署说明**，不含 NapCat 本体。原因是 NapCat 是第三方项目，
有自己的发行节奏与许可，仓库里塞一份二进制既不合适（100MB+，含 DLL 注入组件），
也没法跟上游保持同步。

## 1. 下载

从 [NapCatQQ 的 Release](https://github.com/NapNeko/NapCatQQ/releases) 下载
**Windows 一键包（Shell / Framework 版均可）**，解压到本目录，最终结构应该像这样：

```
NapCat/
├── napcat.mjs                 ← 本体（上游提供）
├── NapCatWinBootMain.exe      ← 注入器（上游提供）
├── NapCatWinBootHook.dll      ← 钩子（上游提供）
├── qqnt.json                  ← 上游提供（QQ 内容缓存）
├── launcher-user.bat          ← 上游提供
├── native/  node_modules/  static/  plugins/   ← 上游提供
├── config/                    ← ★ 本仓库只提供这里的模板
├── loadNapCat.js              ← ★ 由启动器自动生成（内容是本目录的绝对路径）
├── logs/  cache/              ← 运行时生成
└── README.md                  ← 本文件
```

## 2. 配置

把 `config/` 下的模板复制成正式文件（**文件名里的 `<QQ号>` 要换成你自己的机器人 QQ**）：

```bat
cd NapCat\config
copy onebot11.example.json onebot11_<你的机器人QQ>.json
copy napcat.example.json   napcat_<你的机器人QQ>.json
copy webui.example.json    webui.json
```

### `onebot11_<QQ>.json` —— 与 qqbot 对接的关键

```jsonc
"websocketClients": [
  {
    "enable": true,
    "name": "xuxingjiu",
    "url": "ws://127.0.0.1:6199/ws",     // ← 必须与 qqbot config.json 的 ws_port 一致
    "token": "",                          // ← 与 qqbot config.json 的 access_token 一致
    "reportSelfMessage": false,
    "messagePostFormat": "array",
    "heartInterval": 30000,
    "reconnectInterval": 5000
  }
]
```

这是**反向 WebSocket**：NapCat 主动连到 qqbot，而不是 qqbot 去连 NapCat。
所以要先启动 qqbot、再启动 NapCat —— qqbot 的启动器已经帮你排好了顺序。

> `token` 两处必须一致（NapCat 的 `onebot11_<QQ>.json` 与 qqbot 的 `config.json`）。
> 改一处不改另一处 = 连不上，而且日志里只会说"鉴权失败"。

### `webui.example.json` —— 注意安全默认值

模板里 `host` 已经改成 **`127.0.0.1`**（上游默认是 `::`，也就是监听所有网卡，
在没有防火墙的环境下等于把 WebUI 令牌暴露给整个局域网）。
`token` 留空让 NapCat 自己生成一个，**不要**用弱口令。

## 3. 启动

推荐用仓库根的 `deploy\start-all.pyw`（它会按正确顺序拉起 qqbot → NapCat，且不弹黑窗口）。

手工启动则是：

```bat
cd NapCat
launcher-user.bat
```

它会去注册表找 QQ 安装路径，然后注入。如果你的环境里 `reg.exe` 被安全策略拦了，
脚本会失败 —— NapCat 的启动器**硬依赖 reg.exe 读注册表**，而我们自己的启动器
（`qqbot/launcher.py`）是用 Python 的 `winreg`，不受这个限制。这种情况请用
`deploy\start-all.pyw`，或设环境变量 `QQ_EXE` 后走 `launcher.example.bat`。

## 4. 常见问题

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| QQ 起来又自己退出 | 需要登录机器人号 | 手动双击 `launcher-user.bat`（有窗口），在 QQ 界面完成登录 |
| bot 收不到消息 | token 不一致 / 端口不一致 / 顺序错了 | 核对两处 token 与端口，先起 qqbot 再起 NapCat |
| 提示 `provided QQ path is invalid` | 注册表里没有 QQ 的卸载信息 | 设 `QQ_EXE` 环境变量指向 `QQ.exe` |
| 每次都要重新登录 | 启动脚本杀了 QQ 进程 | **不要** `taskkill /IM QQ.exe`（见下） |

## ⚠️ 一条必须记住的规则

**绝不要用 `taskkill /IM QQ.exe`。** 你的日常 QQ 和机器人号用的是**同一个 QQ.exe**
（同一份安装、同一个进程），按镜像名杀会把你自己的号一起带走 —— 这就是"每次都要
重新登录"的根因。需要停进程时一律**按端口定位**（本仓库的 `launcher.py` 就是这么做的，
而且 QQ 永不在候选列表里）。

## 许可

NapCatQQ 版权归其作者所有，本项目不包含其源码或二进制，仅提供配置模板与说明。

# 第三方组件与参考实现

本项目（许杏玖 / Xuxingjiu）以 MIT 许可发布，见 [`LICENSE`](LICENSE)。
仓库内**不包含**下列第三方组件的源码或二进制，仅在文档中说明如何获取与对接。
各组件版权归其各自作者所有。

## 运行时依赖（使用者自行安装）

| 组件 | 许可 | 用途 | 获取方式 |
| --- | --- | --- | --- |
| [NapCatQQ](https://github.com/NapNeko/NapCatQQ) | 见上游 | QQ 客户端注入框架，提供 OneBot v11 接口 | 自行从上游 Release 下载，仓库内只提供配置模板与部署脚本 |
| [OneBot v11](https://github.com/botuniverse/onebot-11) | 见上游 | 机器人应用层通信标准 | 协议规范 |
| [astrbot_plugin_qzone](https://github.com/Zhalslar/astrbot_plugin_qzone) | 见上游 | `qzone-bridge` 参考了其点赞接口实现、图片 URL 优先策略、feeds3 HTML 解析模式 | 参考实现，未复制代码 |
| [OpenCamwall](https://github.com/Cles4Zhalslar/OpenCamwall) | 见上游 | `qzone-bridge` 参考了其流量统计接口、说说隐私设置、头像/昵称获取（GBK 解码）与 Cookie 保活思路 | 参考实现，未复制代码 |
| [nonebot-adapter-qzone](https://github.com/qzqzcsclub/nonebot-adapter-qzone) | 见上游 | `qzone-bridge` 参考了其扫码登录流程、Cookie 维护机制与 Session 架构设计 | 参考实现，未复制代码 |
| [OpenClaw](https://github.com/openclaw/openclaw) / [openclaw-napcat-qq](https://github.com/Gu-Heping/openclaw-napcat-qq) | 见上游 | 可选的消费方，提供 Agent 侧工具调用；`qzone-bridge` 的联调文档与其对接 | 相关项目，非上游依赖 |

## Python / Node 依赖

`qqbot` 与 `qzone-bridge` 的依赖全部通过 `requirements.txt` / `package.json` 声明，
不随仓库分发。许可信息见各包自身。

## 人设参考图

`qqbot/assets/xuxingjiu_01.jpg` 为本项目角色「许杏玖」的参考设定图，
由项目方生成/持有，随本仓库以 MIT 许可发布。

## 免责声明

本项目仅供**学习与研究**用途：

- 其中的 QQ 空间相关代码基于对公开 Web 接口的观察与逆向验证，**使用它可能违反腾讯的服务条款**；
- 仓库内不提供任何账号、Cookie、密钥或代理服务，使用者需自行承担账号风险与合规责任；
- 请自行控制请求频率，避免对第三方服务造成压力；
- 由此产生的一切后果由使用者自行承担，项目作者不承担任何责任。

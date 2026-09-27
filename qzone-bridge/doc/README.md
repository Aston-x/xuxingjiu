# QQ空间接口分析文档

本目录收录了 qzone-bridge 项目在逆向分析 QQ 空间 Web API 过程中积累的技术文档。

## 文档索引

### 接口文档

| 文档 | 内容 |
|------|------|
| [api-overview.md](api-overview.md) | API 总览：域名体系、通用参数、请求模式、怎么判断成功 |
| [auth.md](auth.md) | 认证机制：Cookie 登录、二维码登录、g_tk / qzonetoken 算法 |
| [emotion-api.md](emotion-api.md) | 说说相关接口：列表、详情、发布、删除、转发 |
| [social-api.md](social-api.md) | 社交互动接口：点赞、取消点赞、评论、删除评论 |
| [feeds3-parser.md](feeds3-parser.md) | feeds3_html_more 接口及 HTML 解析方案 |
| [photo-api.md](photo-api.md) | 相册/照片接口：列表、上传、创建、删除 |
| [user-api.md](user-api.md) | 用户信息接口：好友列表、访客列表、个人资料 |
| [board-api.md](board-api.md) | 留言板：**还没抓包**，目前只有状态、判断依据与抓包计划 |

### 策略与矩阵

| 文档 | 内容 |
|------|------|
| [api-probe-results.md](api-probe-results.md) | 各端点的实机探测结果汇总（-10000 / 500 / 404 等），每条标出处 |
| [fallback-strategy.md](fallback-strategy.md) | 多级降级策略与容错机制 |
| [compatibility-matrix.md](compatibility-matrix.md) | 接口可用性矩阵与常见业务码 |
| [qzone-feature-matrix.md](qzone-feature-matrix.md) | QZone 功能支持矩阵 |

### 工程参考

| 文档 | 内容 |
|------|------|
| [reverse-engineering-guide.md](reverse-engineering-guide.md) | 一个新端点从抓包到上线的完整顺序与工具链 |
| [feature-backlog.md](feature-backlog.md) | 功能待办与规划 |
| [openclaw-acceptance.md](openclaw-acceptance.md) | 按 OpenClaw 工具逐项验收的记录 |

### 流程与排障

| 文档 | 内容 |
|------|------|
| [获取说说流程说明.md](获取说说流程说明.md) | 一条说说从请求到落库走过的链路 |
| [排障-Cookie失效与取不到说说.md](排障-Cookie失效与取不到说说.md) | Cookie 失效、拿到空壳页时的定位顺序 |

## 约定

- 接口结论以各文档里标注的实测日期为准（`compatibility-matrix.md` 是 2026-02 那批，
  `emotion-api.md` / `feeds3-parser.md` 是 2026-03-22 那批），汇总在 [api-probe-results.md](api-probe-results.md)
- PC 端指 `user.qzone.qq.com/proxy/domain/` 系列端点
- Mobile 端指 `mobile.qzone.qq.com` 系列端点
- 业务码含义与账号状态、风控策略相关，需结合具体场景判断
- 文档里的 QQ 号一律是合成值（`1100000011` 这种）或 `oXXXX`，不要替换成真实账号再提交

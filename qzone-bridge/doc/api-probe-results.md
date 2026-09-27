# 接口探测结果汇总

这篇把散在各文档里的实机结论收拢成一份。原始结论仍然以各文档为准，这里只做汇总和交叉核对，每条都标出处。第四、五节之后是几个对不上的地方，以及没测到的部分。

## 一、这些数字是怎么来的

主要是三条途径。

`npm run probe:doc`（`scripts/probe-doc-endpoints.ts`）是 2026-03-22 那批结论的出处。它按文档里列出的 URL 逐个发请求，只读 GET 加一个空 POST 试探，输出四列 `HTTP_STATUS \t BODY_LEN \t LABEL \t PARSED_SUMMARY`。三点注意：

- 只认环境变量 `QZONE_COOKIE`（`scripts/probe-doc-endpoints.ts:68`）。只配 `QZONE_COOKIE_STRING` 会直接报「缺少 QZONE_COOKIE」退出。
- 结果只打 stdout，脚本不写任何文件。`README.md:410` 那句「探测接口可用性并写入文档」不准确，抄结论是人工动作。
- 探测日期硬编码在脚本里打出来（`probe-doc-endpoints.ts:84` 打 `# probe_date=2026-03-22`）。将来重跑要顺手改，否则新结果会挂着旧日期。

`npm run verify` 和 `verify:readonly`（`scripts/verify-endpoints.ts`）是端点健康检查，覆盖 8 个读接口加 2 个写接口，输出 PASS/FAIL/SKIP，`--skip-write` 跳过写探针。`npm run verify:http`（`scripts/verify-bridge-http.ts`）与 `npm run verify:tools`（`scripts/verify-all-qzone-tools-http.ts`）打的是已经启动的桥接 HTTP 服务：前者抽检 `check_cookie` / `get_login_info` / `get_stranger_info`，后者按 OpenClaw 工具对应的 action 逐项请求，加 `--write` 会真发数据。

原始报文由 `src/qzone/rawLogger.ts` 的 `dumpRawResponse()` 落盘到 `logs/raw_responses/{label}_{时间戳}.log`，单文件上限 5 MB，目录最多 500 个。触发点有四处：HTTP 状态码 >= 400、命中反爬正则、JSONP 解析失败（都在 `src/qzone/requestLayer.ts`），以及 Zod 校验不通过（`src/qzone/validate.ts:50`）。开 `QZONE_DEBUG_DUMP=1` 会另写一份到 `{QZONE_CACHE_PATH}/debug/`（`client.ts:1031`）。下面每条结论，在跑过的那台机器上都能在 `logs/raw_responses/` 或 `test_cache/debug/` 里找到对应的原始响应。

```bash
npm run probe:doc      # 原始探测，需 QZONE_COOKIE
npm run verify         # 端点健康检查（verify:readonly 为只读）
npm run verify:http    # 桥接 HTTP 抽检，需 bridge 已启动
npm run verify:tools   # 全量 action 对齐
```

还有一条口径要说明：`doc/README.md` 写「所有接口分析基于 2026-02 实测结论」，但实际存在两个批次，2026-02 那批（[compatibility-matrix.md](compatibility-matrix.md)、[photo-api.md](photo-api.md)）和 2026-03-22 那批（`probe:doc`）。两批之间有些结论已经变了，见第六节。

## 二、PC 端（user.qzone.qq.com/proxy/…）

| 端点 | 方法 | 域名 | 探测结果 | 日期 | 出处 |
|---|---|---|---|---|---|
| `emotion_cgi_msglist_v6` | GET | taotao.qzone.qq.com | HTTP 200，JSONP 体，`code=-10000`，message「使用人数过多，请稍后再试」 | 2026-03-22 | `doc/emotion-api.md:57` |
| `emotion_cgi_getdetailv6` | POST | taotao.qzone.qq.com | HTTP 500 且空体 | 2026-03-22 | `doc/emotion-api.md:115` |
| `emotion_cgi_getdetailv6` | GET | taotao.qzone.qq.com | HTTP 500 且空体 | 2026-03-22 | `doc/emotion-api.md:115` |
| `emotion_cgi_getcmtreply_v6` | POST/GET | taotao.qzone.qq.com | 多已不可用，无逐条状态码 | 2026-03 | `doc/fallback-strategy.md:8` |
| `get_like_list` | GET | taotao.qzone.qq.com | 常为空或 500，无逐条状态码与日期 | 无 | `CLAUDE.md:42` |
| `emotion_cgi_msglist_v6` 限流 | - | - | 线上仍被限流，`code=-10000` | 无 | `CLAUDE.md:40` |
| 一批 PC/mobile JSON 接口 | - | - | 实测返回 `-10000`、HTTP 500 空体或 404 | 2026-03 | `CLAUDE.md:74` |
| `feeds3_html_more` | GET | ic2.qzone.qq.com | 可靠，是当前主数据源 | 无 | `doc/feeds3-parser.md:5`、`doc/compatibility-matrix.md:11` |

`feeds3_html_more` 那行没写日期，因为文档里找不到一句带日期的单独判定，它是从「当前主数据源」这个结论反推出来的（`doc/feeds3-parser.md:9`）。

## 三、移动端（mobile.qzone.qq.com）

| 端点 | 方法 | 探测结果 | 日期 | 出处 |
|---|---|---|---|---|
| `/get_mood_list` | GET | HTTP 404、空体 | 2026-03-22 | `doc/emotion-api.md:75` |
| `/detail` | GET | HTTP 404、空体 | 2026-03-22 | `doc/emotion-api.md:130` |
| `/detail` | GET | 0 字节空响应 | 2026-02 | `doc/compatibility-matrix.md:51` |
| `/like` | POST | 404 | 2026-02 | `doc/compatibility-matrix.md:52` |
| `/list` | GET | `code=-4003`（业务受限） | 2026-02 | `doc/compatibility-matrix.md:53`、`doc/user-api.md:114` |
| `/get_mood_list` | GET | 404 | 2026-02 | `doc/compatibility-matrix.md:54` |
| `/get_comment_list` | GET | 不稳定，作备选 | 2026-02 | `doc/compatibility-matrix.md:55` |
| `/del_comment` | POST | 不稳定，作备选 | 2026-02 | `doc/compatibility-matrix.md:56` |

「多条 mobile.qzone.qq.com 路径 404」的总述在 `doc/feeds3-parser.md:9`。

## 四、相册（photo.qzone.qq.com）

| 端点 | 方法 | 探测结果 | 日期 | 出处 |
|---|---|---|---|---|
| `cgi_list_album` | GET | HTTP 500 | 2026-02 | `doc/compatibility-matrix.md:40`、`doc/photo-api.md:3` |
| `cgi_list_photo` | GET | HTTP 500 | 2026-02 | `doc/compatibility-matrix.md:41` |
| `cgi_floatview_photo_list_v2` | GET | HTTP 500 | 2026-02 | `doc/compatibility-matrix.md:42` |
| `cgi_create_album` | POST | HTTP 500 | 2026-02 | `doc/compatibility-matrix.md:43` |
| `cgi_del_album` | POST | HTTP 500 | 2026-02 | `doc/compatibility-matrix.md:44` |
| `cgi_del_photo` | POST | HTTP 500 | 2026-02 | `doc/compatibility-matrix.md:45` |
| `cgi_upload_image` | POST | 可靠，走 up.qzone.qq.com | 2026-02 | `doc/compatibility-matrix.md:26` |

[photo-api.md](photo-api.md) 补充了一条口径：已经试过 7 种以上变体（含 H5 proxy 与直连域名），全部失败，属于服务端限制。上传是这批里唯一的例外。

## 五、用户与认证

| 端点/项 | 探测结果 | 日期 | 出处 |
|---|---|---|---|
| `cgi_get_friend_list` | 可用 | 2026-02 | `doc/compatibility-matrix.md:32` |
| `cgi_right_get_visitor_more` | 可用 | 2026-02 | `doc/compatibility-matrix.md:33` |
| `cgi_personal_card` | 可用 | 2026-02 | `doc/compatibility-matrix.md:34` |
| Cookie 字符串登录 | 最可靠，推荐方式 | 2026-02 | `doc/compatibility-matrix.md:64` |
| `ptqrlogin` 扫码轮询 | 可能 403，IP/设备风控 | 2026-02 | `doc/compatibility-matrix.md:63` |
| Cookie 有效期 | 约 14 天 | 无 | `doc/auth.md:20` |

`cgi_personal_card` 在探测脚本里也有对应的一行（`scripts/probe-doc-endpoints.ts:200`），但仓库里没留下那次的逐行输出。

另外两条来自排障记录，不是探针脚本出的：Cookie 无效时 feeds3 返回约 113 字节的空壳、解析 0 条（`doc/排障-Cookie失效与取不到说说.md:34`）；bot 身份下 `scope=1` 请求他人个人页、`scope=0` 加 `uinlist` 限定好友，两种都返回空（同文件 79-82 行）。

## 六、按代码看，现在实际走哪条路

把探测结果只当「哪些接口挂了」看会漏掉一半信息，当前实现已经按这些结果改过路：

- 说说列表：`src/qzone/client.ts:2636` 的注释写明 PC/mobile 接口已从 `getEmotionList` 移除，现在只走 feeds3。也就是说 `emotion_cgi_msglist_v6` 的 `-10000` 已经不影响这个功能。
- 评论：`getCommentsBestEffort` 只从 feeds3 取数，未命中时翻 `feeds_html_act_all`（`doc/fallback-strategy.md:76`）。
- 说说详情：PC 的 POST 与 GET 都会试，失败后走 mobile，再失败从列表按 tid 匹配（`doc/fallback-strategy.md:61` 到 `:72`）。
- 点赞列表：feeds3 解析为主，可能为空而 `traffic.like` 不为 0，验收时不算回归（`doc/openclaw-acceptance.md:34`）。

所以「接口全挂了但功能还在」不矛盾，是设计如此。

## 七、对不上的地方

不取平均，原样列出来。

1. `getdetailv6` 的 POST 结论变了。`compatibility-matrix.md:13` 写「GET空/POST可用，POST 优先策略」，`emotion-api.md:115` 的 2026-03-22 实测是 POST 和 GET 都 500 空体。时间在后、结果更差的那条更可信。
2. 空壳响应体的字节数不一致。排障 doc 记「约 113 字节」（`doc/排障-Cookie失效与取不到说说.md:34`、`:80`），流程说明记「约 115 字节」（`doc/获取说说流程说明.md:72`）。两次请求不同，都不算错。
3. 脚本只认一个环境变量名。`probe-doc-endpoints.ts:68` 用 `QZONE_COOKIE`，桥接本体读环境变量时两个名都认（`src/qzone/config/env.ts:43`）。于是「桥接登录正常、probe:doc 报缺少变量」是很常见的组合。
4. 文档写了脚本没有的能力。`README.md:410` 说 `probe-doc-endpoints.ts`「探测接口可用性并写入文档」，源码里只有 `console.log`。
5. 流程说明与实现脱节。`doc/获取说说流程说明.md:8` 到 `:11` 描述的仍是 `emotion_cgi_msglist_v6` 主用、feeds3 兜底的两级链，`client.ts:2636` 已经改成 feeds3 单路。
6. 矩阵的日期口径。`doc/README.md:37` 说所有分析基于 2026-02，但 `probe:doc` 那批是 2026-03-22，且部分结论与 2026-02 相反。

## 八、没实测到的

按「本来以为有、仓库里其实没有」列。

- 评论接口的逐条状态码。`getcmtreply_v6` 与 mobile `get_comment_list` 只有「实机 2026-03 多已不可用」一句总述（`doc/fallback-strategy.md:8`），没有 HTTP 码，没有 body 长度，没有具体日期。
- `get_like_list` 的探测记录。只有「常为空或 500」（`CLAUDE.md:42`），没有日期，没有 `code`。
- `feeds_html_act_all` 的独立探测结果。它在降级链里被提到多次（`doc/fallback-strategy.md:8`、`:76`），但没有一条带状态码的实测。
- mobile `/get_comment_list` 与 `/del_comment` 的「不稳定」。`compatibility-matrix.md:55`、`:56` 只给了两个字，没有可复现的判定条件。
- 留言板。一次包都没抓过，见 [board-api.md](board-api.md)。
- 日志、签到、礼物、表情评论。矩阵里都还是「待抓包」，没有任何探测动作。

## 九、要补测的话怎么补

上面缺的那些，补起来不难，但要按同一套口径做，不然又会出现第七节那种对不上的情况。建议这么走：

1. 找一台能稳定登录的机器，用测试账号，先确认 `npm run verify:readonly` 是绿的，证明登录态没问题再开始，否则后面所有失败都分不清是接口挂了还是 Cookie 废了。
2. 一条一条跑，别一把梭。同一批探测尽量落在同一天、同一个账号、同一种网络出口，这样结论之间才有可比性。
3. 每条都记三样东西：HTTP 状态码、body 长度、解析后的业务码或 message。`probe:doc` 的那套 `HTTP_STATUS \t BODY_LEN \t LABEL \t PARSED_SUMMARY` 直接照抄就行，不用另发明格式。
4. 顺手看一眼 `logs/raw_responses/` 有没有新文件。有就说明这次响应触发了异常落盘，那条的原文值得整段留着，别只存一行摘要。
5. 回来后按第二节的模板加行，日期写探测当天，出处写清楚是哪篇文档的哪一行。原来那批 2026-02 的结论不要删，两批并存，谁更接近现在一眼就能看出来。
6. 如果新结果和老结论冲突，不要就地改掉旧的，在第七节那种「对不上的地方」里写清楚两边的日期和结果，让读的人自己判断。

再跑 `probe:doc` 之前记得两件事：把脚本里硬编码的 `probe_date` 改成当天（`scripts/probe-doc-endpoints.ts:84`），以及确认环境里设的是 `QZONE_COOKIE`（脚本只认这个名）。这两条忘了，出来的报告会挂着错的日期，或者干脆在第一步就退出。

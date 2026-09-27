# 逆向与实现指南

这篇给的是一个新端点从零落地的顺序。跟 QQ 空间这类页面接口打交道，顺序错了会反复返工：抓包时没存原始报文，回头解析对不上就得重抓；解析写完没先写单测，改一条正则就可能把别的模板弄坏。下面按「抓包 → 存原始报文 → 写解析 → 写单测 → 接降级 → 更新矩阵」走一遍，每一步都指到仓库里的具体文件和命令。

## 一、先定目标

待办在 [feature-backlog.md](feature-backlog.md) 和 [qzone-feature-matrix.md](qzone-feature-matrix.md)。矩阵里状态是「待抓包」的，才需要走去浏览器抓包这一步；状态是「未实现」的，先确认还有没有开放接口。当前排在前面的是留言板，它单独占一篇 [board-api.md](board-api.md)。

一个新端点落地后，下面这几样要齐，缺一项都不算完：

- `src/qzone/client.ts` 里的请求方法
- `src/bridge/actions.ts` 里的 action，对外暴露成 OneBot action
- 至少一级降级或兜底
- `test/unit/` 下的解析单测
- 矩阵、待办表、探测结果表的状态同步

## 二、先拿到能用的登录态

两条路。

Cookie 字符串最快，用 `client.loginWithCookieString()`（`src/qzone/client.ts:1153`）。字符串里 `uin` / `p_skey` / `skey` / `p_uin` 至少得有一个，否则直接抛错；带 `o` 前缀的 uin 会被剥掉。之后它会 GET 一次 `https://user.qzone.qq.com/{uin}` 做校验，但校验失败只打 WARNING，不会拦住你（`client.ts:1188`）。所以日志里出现「Cookie 登录成功」不等于 Cookie 真有效，还得看后续接口的返回。

Cookie 的读取优先级是 缓存 `cookies.json` → 环境变量 `QZONE_COOKIE_STRING`（兼容 `QZONE_COOKIE`，`src/qzone/config/env.ts:43`）→ 二维码。缓存文件的优先级很高，碰到「明明更新了 .env 还是失败」，先删 `test_cache/cookies.json` 再来一遍。

扫码走 `client.loginWithPlaywright()`（`client.ts:1201`），底层是 `src/qzone/playwrightHelper.ts` 的 `launchPlaywright()`。找浏览器的顺序：`QZONE_PLAYWRIGHT_EXECUTABLE` → `QZONE_PLAYWRIGHT_CHANNEL`（默认 chrome）→ 系统 Chrome（`/usr/bin/google-chrome-stable` 那批）→ Playwright 自带的 Chromium。`headless` 默认 true，服务器上没有 `$DISPLAY` 就走无头。Playwright 没装时它默认返回 null 而不抛错，只有传 `throwOnMissing` 才报。

拿到 Cookie 之后要算两个令牌。

**g_tk** 由 `client.getGtk()`（`client.ts:964`）经 `calcGtk()` 算出，是 hash33 的变种：初值 5381，逐字符做 `hsh = hsh + (hsh << 5) + charCode`，最后 `& 0x7fffffff`。来源优先 `p_skey`，其次 `skey`，两个都空就是 `calcGtk('')`，结果 5381，这种状态下基本什么接口都过不去。算法与 Cookie 字段说明见 [auth.md](auth.md)。

**qzonetoken** 从空间主页 HTML 里正则提取，`client.getQzonetoken()`（`client.ts:2164`）。5 条正则按优先级依次试，还有 iframe 备选（提取 iframe src 后附加 g_tk 再请求）和 Playwright 兜底（注入 Cookie 后读 `window.g_qzonetoken`）。成功缓存 5 分钟，失败冻结 10 分钟。这里要记牢：大多数接口不需要 qzonetoken，别一上来就被它卡住。

Cookie 失效、p_skey 换了设备就废、非本人拿不到数据这些症状，[排障-Cookie失效与取不到说说.md](排障-Cookie失效与取不到说说.md) 已经按「现象 → 根因 → 处置」写全了，出问题先看那篇。

## 三、抓包

浏览器登录空间，F12 → Network，把「保留日志」打开，然后手动做一次要逆向的操作，再去翻那条请求。要抄下来的东西：完整 URL、方法、query 和 body 参数、Content-Type、Referer，以及完整响应体。

判断抓到的算不算成功，只看 HTTP 状态码会被骗。这个仓库里反复出现的失败模式就是 HTTP 200 加空体，[compatibility-matrix.md](compatibility-matrix.md) 专门为它留了一条警告。JSONP 里的 `code` 不为 0 同样是失败。真正可用的判据在 `src/qzone/requestLayer.ts` 的 `isGenuineSuccess()`：不触发反爬、不是认证失败、不是限流、body 非空、`code` 为 0、Zod 校验通过。

批量摸底可以用现成脚本：

```bash
npm run probe:doc          # scripts/probe-doc-endpoints.ts
```

它按文档里列出的 URL 做只读 GET 加一个空 POST 试探，输出四列 `HTTP_STATUS \t BODY_LEN \t LABEL \t PARSED_SUMMARY`，结果直接打 stdout。两个坑先说清楚。第一，脚本只认环境变量 `QZONE_COOKIE`（`scripts/probe-doc-endpoints.ts:68`），你只配了 `QZONE_COOKIE_STRING` 它会直接报缺少变量退出。第二，它并不会写任何文件，`README.md:410` 那句「探测接口可用性并写入文档」跟源码对不上，抄结论是人工动作，抄到 [api-probe-results.md](api-probe-results.md)。另外脚本里的探测日期是硬编码的（`probe-doc-endpoints.ts:84` 打 `# probe_date=2026-03-22`），将来重跑记得顺手改掉，否则新结果会挂着旧日期。

抓包时别用浏览器地址栏直接开 `qpic` / `photo.store.qq.com` 的图。那些是防盗链资源，缺 Cookie 和 Referer 就是 403/404，而且 URL 必须整段保留，`&bo=`、`&t=` 这些查询串少一个就失效。要看图走桥接的 `POST /fetch_image`。

## 四、把原始报文留下来

解析正则写了又改是常态，每次改都要有原始报文对照。仓库里有两条落盘通道。

`src/qzone/rawLogger.ts` 是主通道，核心是 `dumpRawResponse(apiLabel, rawText, reasons)`：写到 `logs/raw_responses/{apiLabel}_{ISO时间戳}.log`，文件头记 API 名、时间、落盘原因、体长；单文件超过 5 MB 截断，目录超过 500 个就按 mtime 删最老的。它被这几处触发：

- `src/qzone/requestLayer.ts:51`，HTTP 状态码 >= 400
- `requestLayer.ts:67`，命中反爬正则
- `requestLayer.ts:81`，JSONP 解析失败
- `src/qzone/validate.ts:50`，Zod 校验不通过

也就是说，异常响应不用你手动存，凡是走统一请求层的都会自己躺进 `logs/raw_responses/`。

另一条通道是 `QZONE_DEBUG_DUMP=1`。打开后 `client.dumpDebugPayload()`（`client.ts:1031`）会把响应写到 `{QZONE_CACHE_PATH}/debug/{name}_{时间戳}.txt`，评论那几条链路默认用它，文件名形如 `comments_pc_post_*.txt`、`comments_mobile_*.txt`。

两条通道的产物都可能带账号信息。`logs/` 和 `test_cache/` 都在 `.gitignore` 里，别手动往里加文件。

## 五、写解析

请求统一走 `client.request` / `requestParsed`，最终落到 `requestLayer.parseRawResponse()`：先做 HTTP 判断，再扫反爬正则，再 JSONP 剥壳，最后提业务码。反爬正则在 `src/qzone/config/constants.ts:34-44`，一共 9 条（`系统繁忙`、`访问过于频繁`、`安全验证`、`请输入验证码`、`tcaptcha`、`需要登录`、`请先登录`、`g_isSurvey`，加一条主页脚本特征）。业务码集合也是常量：认证失败 `{-3, -100, -3000, -10001, -10006}`，限流 `{-10000, -2}`。新端点如果带自己的错误码，先想清楚能不能并进这两个集合，而不是就地写一堆 if。

JSON cgi 和 HTML 解析怎么选，由实测决定，不看喜好。PC 端那批 JSON 接口大量返回限流码、500 空体或 404，而 `feeds3_html_more` 只要 Cookie 加 g_tk 就能出数据，所以现在的说说列表、内嵌评论、内嵌点赞实际都挂在 feeds3 上（[feeds3-parser.md](feeds3-parser.md)、[CLAUDE.md](../CLAUDE.md)）。选路顺序是：先试 JSON cgi，能稳定返回就用它，字段全、解析简单；一被限流或空体，切 HTML 那条路。别反过来，HTML 解析的代价高得多。

HTML 解析的成型套路，实现都在 `src/qzone/feeds3/`，对外由 `src/qzone/feeds3Parser.ts` 这个 barrel 统一导出：

1. 解转义。feeds3 返回的 HTML 片段里 `\x22` 是 `"`、`\x3C` 是 `<`、`\/` 是 `/`，不解开正则一定匹配不到。预处理在 `preprocess.ts`。
2. 切块。按 `id="feed_{uin}_{appid}_0_{timestamp}_0_1"` 或 `name="feed_data"` 把整页切成一条条。
3. 归一 tid。统一走 `feedDataCanonical.ts` 的 `canonicalPostTidFromFeedAttrs()`，items、评论、meta、点赞共用同一套，不然同一帖在不同子解析器里会得到不同的 tid。
4. 认发表者。以 `feed_data` 的 `data-uin` 为准；`id="feed_*"` 块缺失时这条仍然要保留，新模板会缺，整条丢掉就会出现「多页 HTML 只解析出一条」。app 分享（appid=202 那类）要取 `feed_data` 上方最近的 `f-nick` 里的 `nameCard`，直接抓全文第一个 `f-name` 会先撞上正文里的播放量行。这几条各有夹具在 `test/unit/feeds3-items-parse.test.ts`，改正则前先读它。
5. 参数串整段提取。`data-param` 里的 `t1_tid` 可能是 16 位以上的 hex，也可能是 `d{uin}_{时间戳}_…` 这种含下划线的复合串，正则必须是 `[^&"'<>\s]+`，用 `[a-z0-9]+` 会在第一个下划线处截断。规则集中在 `feeds3/tidParams.ts`。
6. 打来源标记。评论带 `_source: 'feeds3_html'`，列表项带 `_source: 'feeds3'`，下游靠它区分数据可信度。
7. 时间解析给全格式。`HH:mm`、`昨天 HH:mm`、`前天 HH:mm`、`MM-DD`、`M月D日`、`YYYY-MM-DD`。解析不出来就写 0，同时把本页顺序写进 `_feeds3_seq`（归一到 `feeds3ParseSeq`），去重时当兜底键。

## 六、写单测

测试不上 jest/vitest。`test/unit/` 里每套自己导出一个 `run()`，用 `test/test-helpers.ts` 的 `assert` 和 `runSuite`。夹具直接内联在测试文件里，用合成号（`1100000011` 这一套），不落磁盘。可以直接抄的两个范例：

- `test/unit/feeds3-items-parse.test.ts`，三条夹具覆盖「`feed_` 内部 uin 与 `data-uin` 不一致」「app 分享的发表者识别」「外层 f-nick 与正文主人不同的转帖」。
- `test/unit/feeds3-comments.test.ts`，一级评论加二级回复的嵌套结构，外加纯图评论、`data-pickey`、`style` 里的 `background-image`。

新套件写完后要到 `test/run-all.ts` 的 `suites` 数组里注册，否则 `npm test` 不会跑它。单跑一套：

```bash
npx tsx test/unit/feeds3-items-parse.test.ts
npm test            # 全部单元测试，不需要登录
npm run typecheck
```

单测该覆盖的是边界，不是 happy path：缺失外层块、uin 字段互相矛盾、tid 是短序号还是长 hex、评论无图时 `pic` 给 `[]`、认证失败时返回错误码而不是空成功（`test/unit/feeds3-auth.test.ts` 就是干这个的）。凡是浏览器里抓到过的畸形响应，都值得固化成一个夹具。

## 七、接降级

降级链路在 [fallback-strategy.md](fallback-strategy.md) 有全景，写新端点时照着摆：

- 每一级独立 try/catch，单级失败不中断整条链。
- POST 优先于 GET。taotao proxy 下 POST 的成功率明显高一截。
- PC 优先于 mobile。移动端那批接口现状更差（见 [api-probe-results.md](api-probe-results.md)），只当兜底。
- 记住成功变体。详情接口有 5 种参数变体，成功一次就把它排到最前面，POST 成功记 `-1`。
- 全失败后进冷却，不要每次请求都从头遍历：评论和详情各 5 分钟，qzonetoken 10 分钟，Playwright 30 分钟。

重试用 `src/qzone/infra/retry.ts` 的 `withRetry()`：默认重试 2 次，退避从 1 秒起、封顶 30 秒，带 ±25% 抖动；错误是 `RateLimitError` 就用它自己的 `retryAfterMs`。能不能重试由 `isRetryable()` 判定（`src/qzone/infra/errors.ts`）：网络错误和限流可以，认证失败和反爬不重试。错误类型层级也在那两个文件里，新端点抛错尽量复用 `AuthError` / `RateLimitError` / `ApiBusinessError`，别抛裸 `Error`，否则重试和上报都会失准。

缓存别设太激进。feeds3 页缓存 TTL 30 秒、上限 50 个 uin；熔断是 2 次失败进 30 分钟冷却。这么设是为了避免一个端点挂了拖死整个轮询。

风控对抗那层是自动的：User-Agent 从 8 个里随机取、Accept-Language 从 5 个里随机取、所有轮询定时器加 ±20% 抖动。

## 八、接进 bridge

请求方法写完后，在 `src/bridge/actions.ts` 加一个 `action_xxx`，参照现有的 `action_get_comment_list`、`action_get_visitor_list` 的写法，返回 OneBot 标准响应结构，出错走统一错误分支。对外 action 名和参数要记到 `README.md` 的 action 表里，否则没人知道它存在。

如果这个功能还要进事件推送，得在 `src/bridge/poller.ts` 里加轮询和去重。去重键这里有个坑值得单独说：feeds3 解析出的评论 id 是帖内序号（1、2、3 这种），不能当全局稳定键。`src/bridge/commentDedup.ts` 的做法是短纯数字走内容指纹 `h:tid:…`，长数字或含 `_r_` 的合成 id 走 `id:tid:…`。

## 九、更新矩阵与文档

活干完了状态要同步，不然下次有人照着矩阵干活会白跑：

- [qzone-feature-matrix.md](qzone-feature-matrix.md)，把对应行的状态从「待抓包」改成「已实现」或「部分」，接口列填真实 cgi 名。
- [feature-backlog.md](feature-backlog.md)，状态列和优先级。
- [compatibility-matrix.md](compatibility-matrix.md)，加端点行，写清方法、域名、状态、备注。
- [fallback-strategy.md](fallback-strategy.md)，新端点若进了某条降级链，更新那条链。
- [api-probe-results.md](api-probe-results.md)，把这次探测到的状态码、业务码、日期按出处补进去。
- `README.md` 的功能特性和 doc 目录树。

## 十、验收命令

改动提交前跑一遍，从便宜到贵：

```bash
npm run typecheck          # 类型检查
npm test                   # 单元测试，纯本地
npm run verify:readonly    # 端点健康检查，只读（verify-endpoints.ts --skip-write）
npm run verify             # 含写探针，会真实发数据
npm run verify:http        # 需 bridge 已启动；check_cookie / get_login_info / get_stranger_info
npm run verify:tools       # OpenClaw 工具全量对齐；加 --write 会真发说说和点赞
npm run probe:doc          # 原始探测，需 QZONE_COOKIE
```

`verify:tools --write` 不是纯本地测试。它会真的发说说、点赞，轮询大概率把事件推给接在 WebSocket 上的 bot。生产上验收前先关掉上报，或者换个没有订阅者的实例。

## 十一、踩过的坑

- HTTP 200 空体。最常见的假成功，`isGenuineSuccess()` 里的 `_empty` 判断就是为它写的。
- 113 还是 115 字节。[排障-Cookie失效与取不到说说.md](排障-Cookie失效与取不到说说.md) 记的是「约 113 字节的空壳」，[获取说说流程说明.md](获取说说流程说明.md) 记的是「约 115 字节」。两处都是实测记录，量的是不同次请求，谁也没错，别去凑一个平均数。
- `QZONE_COOKIE` 和 `QZONE_COOKIE_STRING`。桥接两个都认，但 `probe:doc` 只认前者，`scripts/verify-endpoints.ts:119` 也只报「缺少 QZONE_COOKIE」。只配一个的时候脚本会挑食。
- 非本人拿不到说说。`scope=1` 请求别人的个人页、用 `uinlist` 限定好友，这两条在当前环境下后端都不支持。唯一稳定的是当前登录号加 `scope=0` 的无 uinlist 好友动态流，再在内存里按 uin 过滤，[排障-Cookie失效与取不到说说.md](排障-Cookie失效与取不到说说.md) 有对比表。
- 文档里的流程可能已经过期。[获取说说流程说明.md](获取说说流程说明.md) 写的是 `emotion_cgi_msglist_v6` 主用、feeds3 兜底，而 `src/qzone/client.ts:2636` 的注释和实现在说明 PC/mobile 那条链已经从 `getEmotionList` 里移除了。读文档时以代码为准。
- Playwright 冷却 30 分钟。调试时反复重启容易连撞几次进冷却，`resetApiCaches()` 能清掉，别干等。
- 图片防盗链。见第三节，预览一律走 `fetch_image`。

## 十二、一页清单

真上手时按这张表打勾，比来回翻上面十节快。

| 步骤 | 落点 | 完成标志 |
|---|---|---|
| 定目标 | 功能矩阵、待办表 | 选中的行状态是「待抓包」 |
| 拿登录态 | `loginWithCookieString` / `loginWithPlaywright` | 能算出 g_tk，接口不再是认证失败码 |
| 抓包 | 浏览器 Network | 抄全 URL、方法、参数、Referer、完整响应体 |
| 存原始报文 | `logs/raw_responses/`、`{cache}/debug/` | 失败响应有文件可查 |
| 写请求 | `client.ts` | 走统一请求层，业务码能被正确分类 |
| 写解析 | `src/qzone/feeds3/` 或就地 | 归一 tid，打 `_source`，时间给全格式 |
| 写单测 | `test/unit/` 加 `run-all.ts` 注册 | `npm test` 能跑过新套件 |
| 接降级 | `fallback-strategy.md` 那条链 | 单级失败不中断，有冷却 |
| 接重试 | `infra/retry.ts` | 只对网络与限流重试 |
| 加 action | `src/bridge/actions.ts` | 进 `README.md` 的 action 表 |
| 跑验收 | 第十节那七条命令 | typecheck 与单元测试全绿 |
| 更新文档 | 矩阵、待办、探测结果 | 状态和接口名都换成真的 |

清单里唯一容易被跳过的是最后一步，但它最省别人的时间。矩阵里一行还写着「待抓包」，下一个人就会把已经做完的事再做一遍。

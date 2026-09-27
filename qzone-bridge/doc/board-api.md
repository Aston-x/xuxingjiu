# 留言板接口（未抓包）

写在前头：这篇没有接口。留言板目前一次包都没抓，下面写的全是状态、判断依据和抓包计划。等抓到包，这篇再从头改成正式接口文档。[qzone-feature-matrix.md](qzone-feature-matrix.md) 第六节的三行现在还是「待抓包」。

## 一、当前状态

| 功能 | 状态 | 接口 |
|---|---|---|
| 留言列表 | 待抓包 | 无 |
| 发表留言 | 待抓包 | 无 |
| 删除留言 | 待抓包 | 无 |

出处是 [qzone-feature-matrix.md](qzone-feature-matrix.md) 第 69 到 71 行。优先级在 [feature-backlog.md](feature-backlog.md) 里标的是 P1，排在评论点赞前面。

有一处要纠正：`feature-backlog.md:28` 的备注写「client/action 已预留」，源码里没这回事。`src/qzone/client.ts` 里没有留言板相关方法，`src/bridge/actions.ts` 的 `action_*` 列表里也没有对应项。整个 src 下跟留言板沾边的只有一处，是访客列表的来源名称映射 `{ 4: '留言板' }`（`src/bridge/actions.ts:717`），意思是访客来源字段里有个值叫「留言板」，跟接口实现没关系。抓包时按从零写起准备，别去找预留的桩。

## 二、能确定的，和只能是猜的

能确定的：

- 留言挂在某个人的个人档上，入口页面是 `https://user.qzone.qq.com/{uin}` 这一层。仓库里 `getQzreferrer()` 用的就是 `https://user.qzone.qq.com/{qqNumber}/main`（`src/qzone/client.ts:973`），抓包从这里进就行。
- 如果走 PC 端 proxy，需要 `g_tk`。这是 PC 端点位的通行做法（见 [auth.md](auth.md)），但留言板到底需不需要，没验证过。
- 留言行为会被计进访客来源，取值就是上面那个 4。

只能是猜的，或者干脆不知道：

- cgi 名字、请求方法、参数名、响应结构，一律未知。仓库里搜不到任何线索，别去别处抄一个名字填进来，那种文档比空着更坏。
- 域名未知。PC 端可能在 `user.qzone.qq.com/proxy/domain/` 下面，也可能在 `qzs` / `ic2` 那类域；移动端可能在 `mobile.qzone.qq.com`。都只是按同类接口的惯例推的，没证据。
- 列表是分页还是游标、发表是不是纯 POST form、删除是按留言 id 还是帖内序号，全未知。

## 三、怎么抓

按 [reverse-engineering-guide.md](reverse-engineering-guide.md) 第三、四节的流程走，具体到留言板是这样：

1. 浏览器登录 QQ 空间，进自己的个人档，找到留言板模块。
2. F12 → Network，打开「保留日志」，勾上「禁用缓存」，先清空记录。
3. 一次只做一个动作，然后停下来看那条请求。先看列表那次（通常进页面就发了），再单独做一次发表，再做一次删除。动作分开，请求才归属得清。
4. 每条都要抄全：完整 URL、方法、query 和 body、Content-Type、Referer、完整响应体。响应体整段存下来，别在 DevTools 里看一眼就算，回头写正则要用。
5. 判断成功与否不能只看状态码，用 [api-probe-results.md](api-probe-results.md) 第一节那套判据：HTTP 200 加空体、JSONP 里 `code` 非 0，都算失败。
6. 用测试账号先发一条能认出来的内容（比如带固定前缀），抓完随手删掉。写操作别在自己的正式账号上反复试。

几个已知会干扰抓包的：空间隐私设成「仅好友可见」时，非好友身份怎么都拿不到数据；短时间内高频请求会被限流，表现是返回空或 500；移动端那批接口现状普遍比 PC 差（[api-probe-results.md](api-probe-results.md) 第三节），优先抓 PC 端。

## 四、抓完写什么

1. 这篇按 [emotion-api.md](emotion-api.md) 或 [user-api.md](user-api.md) 的结构补全：每个动作一节，写 URL、方法、参数表、响应示例。示例里的 QQ 号用合成号（`1100000011`）或 `oXXXX`，不要贴真实账号。
2. [compatibility-matrix.md](compatibility-matrix.md) 加端点行，写清方法、域名、状态（可靠 / 限流 / 500 / 404）、以及备注里的日期。
3. [qzone-feature-matrix.md](qzone-feature-matrix.md) 第 69 到 71 行改状态、填真实接口名，[feature-backlog.md](feature-backlog.md) 同步。
4. 探测结果和日期进 [api-probe-results.md](api-probe-results.md)，标清出处。
5. 代码侧按 [reverse-engineering-guide.md](reverse-engineering-guide.md) 第五到第八节：写请求方法、写解析、补单测（`test/unit/`）、接降级、加 action。
6. 如果发表留言要进事件推送，还要在 `src/bridge/poller.ts` 里做轮询和去重。

## 五、边界与风险

抓之前先把边界想清楚，省得抓完发现不能用。

空间隐私是第一道坎。留言板的可见范围跟着个人档的隐私设置走，设成「仅好友可见」或「仅自己可见」时，非好友身份拿不到数据。这意味着就算接口抓通了，桥接在一个 bot 账号下能读到的范围也受主客关系限制，跟好友说说那条路遇到的情况一样。

写操作要谨慎。发表留言和删除留言都是真动作，频率高了容易触发风控，表现通常不是明确的错误码，而是返回空体或者 500，很难判断到底成没成。所以抓包阶段一次只做一个动作，做完立刻核对结果，别连着发十条再看。

还有一条合规提醒：QQ 空间的接口是观察出来的，不是公开文档，用它有违反服务条款的风险。仓库根目录的 `NOTICE.md` 写了这一条，留言板这种带写操作的模块更要注意，别拿别人账号试。

抓完包如果发现接口本身就不开放（比如返回固定的业务受限码），那也没什么可做的，把结论如实写进这篇，矩阵里标成「未实现」而不是继续挂着「待抓包」，这样路线图才是准的。

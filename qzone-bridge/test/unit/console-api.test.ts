/**
 * 控制台只读接口单元测试
 *
 * 覆盖三层：
 *   1. EventHub 的最近事件环形缓冲（供 /console/api/events）
 *   2. EventPoller 的运行统计结构（供 /console/api/status、/console/api/pollers）
 *   3. createApp 的实际 HTTP 行为 —— 路由、独立令牌鉴权、只读约束
 *
 * 第 3 层用真实 HTTP 往返（监听 127.0.0.1:0 由内核分配空闲端口），
 * 目的是守住一个容易踩的坑：/console/* 若落到 OneBot 的 action 分发，
 * 会被拼成 action_console/api/xxx 而返回 1404。
 */
import type { AddressInfo } from 'node:net';
import { EventHub } from '../../src/bridge/hub.js';
import { EventPoller } from '../../src/bridge/poller.js';
import { fromEnv, type BridgeConfig } from '../../src/bridge/config.js';
import { createApp } from '../../src/bridge/server.js';
import type { ActionHandler } from '../../src/bridge/actions.js';
import { assert, runSuite, type TestCase } from '../test-helpers.js';

/** 构造一份完整的 BridgeConfig，供不需要真实网络的用例使用 */
function makeConfig(overrides: Partial<BridgeConfig> = {}): BridgeConfig {
  return {
    host: '127.0.0.1',
    port: 0, // 0 = 交给内核分配空闲端口
    accessToken: '',
    consoleToken: 'test-token',
    httpPostUrls: [],
    wsReverseUrls: [],
    wsReverseApiUrls: [],
    wsReverseEventUrls: [],
    wsReverseReconnectInterval: 5,
    pollInterval: 60,
    commentPollInterval: 120,
    likePollInterval: 180,
    friendFeedPollInterval: 120,
    enableQr: false,
    emitMessageEvents: true,
    emitCommentEvents: true,
    emitLikeEvents: true,
    emitFriendFeedEvents: false,
    emitHeartbeatEvents: true,
    eventDebug: false,
    eventPollSource: 'auto',
    attachImageDataInEvents: false,
    cachePath: './test_cache',
    ...overrides,
  };
}

/** 无指纹事件：post_type 不属于 message/notice，Hub 不会去重 */
function plainEvent(n: number): Record<string, unknown> {
  return { post_type: 'meta_event', _seq: n };
}

/** 有指纹的说说事件：同 (self_id, author, tid) 会被去重 */
function postEvent(tid: string): Record<string, unknown> {
  return { post_type: 'message', self_id: 10001, _tid: tid, sender: { user_id: 888 } };
}

const cases: TestCase[] = [
  // ── 1. EventHub 环形缓冲 ──────────────────────────────
  {
    name: 'getRecentEvents 保持下发顺序',
    fn: async () => {
      const hub = new EventHub();
      for (let i = 1; i <= 3; i++) await hub.publish(plainEvent(i) as never);
      const got = hub.getRecentEvents();
      assert(got.length === 3, `应缓冲 3 条，实际 ${got.length}`);
      assert((got[0] as Record<string, unknown>)['_seq'] === 1, '首条应为 1');
      assert((got[2] as Record<string, unknown>)['_seq'] === 3, '末条应为 3');
    },
  },
  {
    name: 'getRecentEvents 不含被去重的事件',
    fn: async () => {
      const hub = new EventHub();
      await hub.publish(postEvent('tid-A') as never);
      await hub.publish(postEvent('tid-A') as never); // 重复，应被去重
      await hub.publish(postEvent('tid-B') as never);
      const got = hub.getRecentEvents();
      assert(got.length === 2, `去重事件不应入缓冲，实际 ${got.length}`);
    },
  },
  {
    name: 'getRecentEvents 超过上限淘汰最旧',
    fn: async () => {
      const hub = new EventHub();
      for (let i = 1; i <= 250; i++) await hub.publish(plainEvent(i) as never);
      const stats = hub.hubStats();
      assert(stats['recent'] === 200, `上限应为 200，实际 ${String(stats['recent'])}`);
      assert(stats['recentMax'] === 200, 'recentMax 应为 200');
      const got = hub.getRecentEvents(200);
      assert(got.length === 200, '应返回 200 条');
      assert((got[0] as Record<string, unknown>)['_seq'] === 51, '最旧的 1~50 应被淘汰');
      assert((got[199] as Record<string, unknown>)['_seq'] === 250, '末条应为 250');
    },
  },
  {
    name: 'getRecentEvents 返回浅拷贝，外部改动不影响内部',
    fn: async () => {
      const hub = new EventHub();
      await hub.publish(plainEvent(1) as never);
      const got = hub.getRecentEvents();
      got.length = 0;
      assert(hub.getRecentEvents().length === 1, '内部缓冲不应被外部清空影响');
    },
  },
  {
    name: 'getRecentEvents limit 边界处理',
    fn: async () => {
      const hub = new EventHub();
      for (let i = 1; i <= 5; i++) await hub.publish(plainEvent(i) as never);
      assert(hub.getRecentEvents(0).length === 0, 'limit=0 应返回空数组');
      assert(hub.getRecentEvents(-3).length === 0, '负 limit 应返回空数组');
      const two = hub.getRecentEvents(2);
      assert(two.length === 2, 'limit=2 应返回 2 条');
      assert((two[1] as Record<string, unknown>)['_seq'] === 5, '应取最新的 2 条');
      assert(hub.getRecentEvents(9999).length === 5, 'limit 超上限时按已有条数返回');
    },
  },
  {
    name: 'hubStats 计数正确',
    fn: async () => {
      const hub = new EventHub();
      hub.subscribe(() => {});
      hub.subscribe(() => {});
      hub.addSeedTid('t1');
      await hub.publish(plainEvent(1) as never);
      const s = hub.hubStats();
      assert(s['subscribers'] === 2, `subscribers 应为 2，实际 ${String(s['subscribers'])}`);
      assert(s['recent'] === 1, 'recent 应为 1');
      assert(s['seedTids'] === 1, 'seedTids 应为 1');
    },
  },

  // ── 2. EventPoller 统计 ──────────────────────────────
  {
    name: 'getStats 初始结构（不启动，避免真实网络与定时器）',
    fn: () => {
      const hub = new EventHub();
      const poller = new EventPoller({} as never, hub, makeConfig());
      const s = poller.getStats();
      assert(s['running'] === false, '初始不应在运行');
      assert(s['startedAt'] === 0, 'startedAt 初始为 0');
      assert(s['uptimeMs'] === 0, 'uptimeMs 初始为 0');
      assert(s['trackTids'] === 0, 'trackTids 初始为 0');
      const pollers = s['pollers'] as Record<string, { runCount: number; ok: boolean }>;
      assert(!!pollers, 'pollers 段应存在');
      for (const k of ['main', 'comments', 'likes', 'friendFeeds']) {
        assert(!!pollers[k], `pollers.${k} 应存在`);
        assert(pollers[k]!.runCount === 0, `pollers.${k}.runCount 初始为 0`);
        assert(pollers[k]!.ok === false, `pollers.${k}.ok 初始为 false`);
      }
    },
  },
  {
    name: 'getRecentPosts 初始为空且只接受合法 limit',
    fn: () => {
      const hub = new EventHub();
      const poller = new EventPoller({} as never, hub, makeConfig());
      assert(poller.getRecentPosts().length === 0, '初始应为空');
      assert(poller.getRecentPosts(0).length === 0, 'limit=0 应为空');
      assert(poller.getRecentPosts(-1).length === 0, '负 limit 应为空');
    },
  },

  // ── 3. 配置 ──────────────────────────────────────────
  {
    name: 'fromEnv 读取 QZONE_CONSOLE_TOKEN 并 trim',
    fn: () => {
      const prev = process.env['QZONE_CONSOLE_TOKEN'];
      try {
        process.env['QZONE_CONSOLE_TOKEN'] = '  abc123  ';
        assert(fromEnv().consoleToken === 'abc123', '首尾空格应被 trim');
        process.env['QZONE_CONSOLE_TOKEN'] = '   ';
        assert(fromEnv().consoleToken === '', '全空白应归一为空串');
      } finally {
        if (prev === undefined) delete process.env['QZONE_CONSOLE_TOKEN'];
        else process.env['QZONE_CONSOLE_TOKEN'] = prev;
      }
    },
  },
  {
    name: 'fromEnv 的控制台令牌为空时，accessToken 仍独立生效',
    fn: () => {
      const prevTok = process.env['QZONE_CONSOLE_TOKEN'];
      const prevAccess = process.env['ONEBOT_ACCESS_TOKEN'];
      try {
        delete process.env['QZONE_CONSOLE_TOKEN'];
        process.env['ONEBOT_ACCESS_TOKEN'] = 'onebot-token';
        const cfg = fromEnv();
        assert(cfg.consoleToken === '', '控制台令牌应为空');
        assert(cfg.accessToken === 'onebot-token', 'OneBot 令牌应独立保留');
      } finally {
        if (prevTok === undefined) delete process.env['QZONE_CONSOLE_TOKEN'];
        else process.env['QZONE_CONSOLE_TOKEN'] = prevTok;
        if (prevAccess === undefined) delete process.env['ONEBOT_ACCESS_TOKEN'];
        else process.env['ONEBOT_ACCESS_TOKEN'] = prevAccess;
      }
    },
  },

  // ── 4. 真实 HTTP 往返 ────────────────────────────────
  {
    name: '/console/* 路由、令牌鉴权、只读约束（真实 HTTP）',
    fn: async () => {
      const hub = new EventHub();
      const config = makeConfig();
      const handlerStub = {
        handle: async (action: string) => ({ status: 'ok', retcode: 0, data: { handled: action } }),
      } as unknown as ActionHandler;

      const app = createApp(config, handlerStub, hub, undefined, undefined);
      await new Promise<void>((resolve) => {
        app.server.once('listening', () => resolve());
        app.start();
      });
      const addr = app.server.address() as AddressInfo | null;
      assert(!!addr && typeof addr === 'object', '应能拿到监听地址');
      const base = `http://127.0.0.1:${addr!.port}`;

      await hub.publish(plainEvent(1) as never);
      await hub.publish(plainEvent(2) as never);

      try {
        // 鉴权
        const noTok = await fetch(`${base}/console/api/status`);
        assert(noTok.status === 401, `无令牌应 401，实际 ${noTok.status}`);
        const badTok = await fetch(`${base}/console/api/status?token=wrong`);
        assert(badTok.status === 401, `错误令牌应 401，实际 ${badTok.status}`);

        // 合法令牌（query 与 header 两种传法都应通过）
        const viaQuery = await fetch(`${base}/console/api/status?token=test-token`);
        assert(viaQuery.status === 200, `query 令牌应 200，实际 ${viaQuery.status}`);
        const body = (await viaQuery.json()) as Record<string, unknown>;
        assert(body['ok'] === true, 'ok 应为 true');
        assert(typeof body['bridge'] === 'object' && body['bridge'] !== null, '应含 bridge 段');
        assert(typeof body['hub'] === 'object' && body['hub'] !== null, '应含 hub 段');

        const viaHeader = await fetch(`${base}/console/api/status`, {
          headers: { 'x-console-token': 'test-token' },
        });
        assert(viaHeader.status === 200, `header 令牌应 200，实际 ${viaHeader.status}`);

        // 关键回归：/console/api/* 必须走控制台而不是 OneBot action 分发（否则会 1404）
        const events = (await (await fetch(`${base}/console/api/events?token=test-token`)).json()) as Record<string, unknown>;
        assert(Array.isArray(events['events']), 'events 应为数组');
        assert((events['events'] as unknown[]).length === 2, '应返回刚发布的 2 条事件');

        const posts = (await (await fetch(`${base}/console/api/posts?token=test-token`)).json()) as Record<string, unknown>;
        assert(Array.isArray(posts['posts']), 'posts 应为数组');

        const pollers = (await (await fetch(`${base}/console/api/pollers?token=test-token`)).json()) as Record<string, unknown>;
        assert(pollers['ok'] === true, 'pollers ok');

        const health = (await (await fetch(`${base}/console/api/health?token=test-token`)).json()) as Record<string, unknown>;
        assert(health['ok'] === true, 'health ok');

        // 未知控制台路径
        const unknown = await fetch(`${base}/console/api/nope?token=test-token`);
        assert(unknown.status === 404, `未知控制台路径应 404，实际 ${unknown.status}`);

        // 只读：非 GET 一律 405
        const notGet = await fetch(`${base}/console/api/status?token=test-token`, { method: 'POST' });
        assert(notGet.status === 405, `POST 应 405，实际 ${notGet.status}`);

        // 非 console 路径仍走 OneBot action 分发，未被控制台吞掉
        const actResp = await fetch(`${base}/get_version_info`, { method: 'POST' });
        const actBody = (await actResp.json()) as Record<string, unknown>;
        assert(actBody['retcode'] === 0, `OneBot action 应仍可用，实际 ${JSON.stringify(actBody)}`);
        const actData = actBody['data'] as Record<string, unknown>;
        assert(actData['handled'] === 'get_version_info', 'action 应被原样分发');
      } finally {
        await app.stop();
      }
    },
  },
];

export async function run() {
  return runSuite('console/只读控制台', cases);
}

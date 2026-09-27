/**
 * 好友列表从 feeds3 提取逻辑单元测试（extractFriendsFromFeeds3FromText）
 */
import { QzoneClient } from '../../src/qzone/client.js';
import { assert, runSuite, type TestCase } from '../test-helpers.js';

// 最小 feeds3 片段：JS 含 opuin/uin/nickname/logimg，HTML 含 f-nick
const FEEDS3_FIXTURE = `
data:[{opuin:'1100000016',uin:'1100000016',nickname:'用户丙',logimg:'http://qlogo4.store.qq.com/qzone/1100000016/50'},{opuin:'1100000015',uin:'1100000015',nickname:'用户乙',logimg:'http://qlogo2.store.qq.com/qzone/1100000015/50'},{opuin:'1100000021',uin:'1100000021',nickname:'用户己',logimg:''}
,{opuin:'0',uin:'0',nickname:''}
]
<div class="f-nick"><a href="http://user.qzone.qq.com/1100000016" class="f-name">用户丙</a></div>
<div class="f-nick"><a href="http://user.qzone.qq.com/888888888">HTMLOnly</a></div>
`;

function createClientWithSelf(selfUin: string): QzoneClient {
  const client = new QzoneClient({ cachePath: './test_cache' });
  (client as unknown as { qqNumber: string | null }).qqNumber = selfUin;
  return client;
}

const cases: TestCase[] = [
  {
    name: '从 JS 提取 opuin/uin/nickname/logimg',
    fn: () => {
      const client = createClientWithSelf('999999999');
      const list = client.extractFriendsFromFeeds3FromText(FEEDS3_FIXTURE);
      const uins = list.map((f) => f.uin).sort();
      assert(uins.includes('1100000016'), '应包含 1100000016');
      assert(uins.includes('1100000015'), '应包含 1100000015');
      assert(uins.includes('1100000021'), '应包含 1100000021');
      const a = list.find((f) => f.uin === '1100000016');
      assert(a != null && a.nickname.includes('用户丙'), '昵称应来自 JS');
      assert(a != null && a.avatar.includes('qlogo4'), '头像应来自 logimg');
    },
  },
  {
    name: '排除自身 UIN',
    fn: () => {
      const client = createClientWithSelf('1100000016');
      const list = client.extractFriendsFromFeeds3FromText(FEEDS3_FIXTURE);
      assert(!list.some((f) => f.uin === '1100000016'), '应排除自身 1100000016');
      assert(list.some((f) => f.uin === '1100000015'), '应保留他人');
    },
  },
  {
    name: '排除 uin=0',
    fn: () => {
      const client = createClientWithSelf('999999999');
      const list = client.extractFriendsFromFeeds3FromText(FEEDS3_FIXTURE);
      assert(!list.some((f) => f.uin === '0'), '不应包含 uin=0');
    },
  },
  {
    name: 'f-nick HTML 兜底昵称',
    fn: () => {
      const client = createClientWithSelf('999999999');
      const list = client.extractFriendsFromFeeds3FromText(FEEDS3_FIXTURE);
      const htmlOnly = list.find((f) => f.uin === '888888888');
      assert(htmlOnly != null && htmlOnly.nickname === 'HTMLOnly', '应从 f-nick 得到 888888888 昵称');
    },
  },
  {
    name: '空文本返回空数组',
    fn: () => {
      const client = createClientWithSelf('1');
      const list = client.extractFriendsFromFeeds3FromText('');
      assert(Array.isArray(list) && list.length === 0, '空文本应返回 []');
    },
  },
];

export async function run(): Promise<{ name: string; passed: number; failed: number; errors: Array<{ test: string; error: string }> }> {
  return runSuite('client/extractFriendsFromFeeds3FromText', cases);
}

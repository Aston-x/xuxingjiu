/**
 * 真实浏览器渲染 + 交互验证 + 截图（Playwright / 系统 Chrome）。
 *
 * 用法：
 *     node tools\shoot_console.js
 * 依赖：同级的 qzone-bridge 里有 playwright（自动按兄弟目录查找）。
 *
 * 为什么要用真浏览器跑一遍：接口全 200 + JS 语法通过 ≠ 界面正确。
 * 之前就靠它抓到过两个只看接口发现不了的问题 ——
 * CSS 优先级把 display:grid 顶成 block（卡片全被撑成全宽）、favicon 404。
 *
 * 交互部分会真的点开关、并**直接读临时 config 文件校验值变了**，
 * 从而证明「DOM 点击 → POST → 改 CFG → 原子落盘」这条链路是通的。
 * config 已由 serve_console.py 隔离到临时副本，不会动真实配置。
 */
const { spawn } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');

const TOOLS = __dirname;
const BASE_DIR = path.resolve(TOOLS, '..');
const SIBLING = path.resolve(BASE_DIR, '..');

const PW = require(path.join(SIBLING, 'qzone-bridge', 'node_modules', 'playwright'));
const PY = path.join(BASE_DIR, '.venv', 'Scripts', 'python.exe');
const SERVE = path.join(TOOLS, 'serve_console.py');
const TOKEN_FILE = path.join(BASE_DIR, 'state', 'console-token');
const TMP_CFG = path.join(BASE_DIR, '_tmp_shoot_config.json');
const SHOTS = path.join(TOOLS, '_shots');

let PORT = 0;
let BASE = '';

const sleep = ms => new Promise(r => setTimeout(r, ms));
const results = [];
function note(name, ok, detail) {
  results.push({ name, ok, detail });
  console.log(`  ${ok ? '✅' : '❌'} ${name}${detail ? '   ' + detail : ''}`);
}
async function waitFor(fn, timeoutMs, label) {
  const t0 = Date.now();
  for (;;) {
    if (await fn()) return true;
    if (Date.now() - t0 > timeoutMs) throw new Error('超时等待：' + label);
    await sleep(250);
  }
}

async function main() {
  fs.mkdirSync(SHOTS, { recursive: true });
  const child = spawn(PY, [SERVE], { cwd: BASE_DIR, stdio: ['ignore', 'pipe', 'pipe'] });
  let out = '';
  child.stdout.on('data', d => { out += d.toString(); });
  child.stderr.on('data', d => { out += d.toString(); });

  let browser;
  try {
    await waitFor(() => /CONSOLE_READY \d+/.test(out), 40000, '控制台就绪');
    PORT = Number(out.match(/CONSOLE_READY (\d+)/)[1]);
    BASE = `http://127.0.0.1:${PORT}`;
    console.log(`✅ 控制台已就绪（端口 ${PORT}，config 已隔离）\n`);

    const token = fs.readFileSync(TOKEN_FILE, 'utf8').trim();
    browser = await PW.chromium.launch({ channel: 'chrome', headless: true });
    const page = await browser.newPage({ viewport: { width: 1560, height: 1180 } });

    const errors = [];
    page.on('pageerror', e => errors.push('pageerror: ' + e.message));
    page.on('console', m => { if (m.type() === 'error') errors.push('console: ' + m.text()); });

    await page.goto(`${BASE}/?token=${encodeURIComponent(token)}`, { waitUntil: 'domcontentloaded' });
    await page.waitForFunction(
      () => { const e = document.getElementById('p-status'); return e && e.textContent.includes('Bot QQ'); },
      { timeout: 25000 });
    await sleep(2600);

    const probe = await page.evaluate(() => {
      const g = id => { const e = document.getElementById(id); return e ? e.innerText.replace(/\s+/g, ' ').trim() : null; };
      // 面板 body 之外还有卡片标题与底部说明 —— 想断言它们就得取整张卡片
      const cardTextOf = id => {
        const e = document.getElementById(id);
        const c = e && e.closest ? e.closest('.card') : null;
        return c ? c.innerText.replace(/\s+/g, ' ').trim() : null;
      };
      const cs = getComputedStyle(document.getElementById('tab-bot'));
      return {
        banner: document.getElementById('offline-banner').style.display,
        display: cs.display,
        cols: cs.gridTemplateColumns.split(' ').length,
        scrollW: document.documentElement.scrollWidth,
        stats: g('p-status-stats'), life: g('p-life'), memory: g('p-memory'),
        access: g('p-access'), sd: g('p-sd'),
        switches: document.querySelectorAll('#p-access .switch input').length,
        hasAdminInput: !!document.getElementById('ac-admin'),
        logSources: [...document.querySelectorAll('#log-source option')].map(o => o.value),
        accessNotes: [...document.querySelectorAll('#p-access .note')]
          .map(e => e.innerText.replace(/\s+/g, ' ').trim()),
        // 出图预览
        sdImgs: document.querySelectorAll('#p-sd .shot img').length,
        sdImgSrc: (document.querySelector('#p-sd .shot img') || {}).getAttribute
                    ? document.querySelector('#p-sd .shot img').getAttribute('src') : '',
        // 受限功能的中文名
        restrictLabels: [...document.querySelectorAll('#p-access .chip')]
          .map(e => e.innerText.trim()).filter(t => t && !/^\d/.test(t)),
        // 表情包管理
        stkSwitches: document.querySelectorAll('#p-stickers .switch input').length,
        stkHasUrlInput: !!document.getElementById('stk-url'),
        stkHasFileInput: !!document.getElementById('stk-file'),
        stkText: g('p-stickers'),
        // 阈值进度条 与 好感/心情
        sliders: document.querySelectorAll('#p-tunables input[type=range]').length,
        sliderSample: (function(){
          const el = document.querySelector('#p-tunables input[type=range]');
          return el ? el.getAttribute('min') + '~' + el.getAttribute('max') +
                      ' step=' + el.getAttribute('step') + ' value=' + el.value : '';
        })(),
        moodText: g('p-mood'),
        // 布局与视觉
        groupTitles: document.querySelectorAll('.group-title').length,
        firstCardAnim: (function(){
          const c = document.querySelector('#tab-bot .card');
          return c ? getComputedStyle(c).animationName : '';
        })(),
        bodyFont: getComputedStyle(document.body).fontFamily.slice(0, 36),
        cardRadius: (function(){
          const c = document.querySelector('#tab-bot .card');
          return c ? getComputedStyle(c).borderRadius : '';
        })(),
        // 记忆三分。注意：标题(h2) 和底部说明(note) 在**卡片**里、不在面板 body 里，
        // 所以要往上找 .card 取整张卡的文字，否则断言会假失败（踩过）。
        memoryText: g('p-memory'),
        groupMemText: g('p-groupmem'),
        memoryCard: cardTextOf('p-memory'),
        groupMemCard: cardTextOf('p-groupmem'),
      };
    });

    note('离线提示条未显示', probe.banner === 'none');
    note('栅格生效', probe.display === 'grid' && probe.cols >= 3,
         `display=${probe.display} cols=${probe.cols}`);
    note('无横向溢出', probe.scrollW <= 1560, `scrollWidth=${probe.scrollW}`);
    // 生活作息是横向三栏面板：卡高必须远低于原来的 1069px 长列（1560 实测 ~444px）
    const lifeH = await page.evaluate(() =>
      Math.round(document.querySelector('#p-life').closest('.card').getBoundingClientRect().height));
    note('生活作息已横向化（卡高 ≤600px，原来 1069px）', lifeH > 0 && lifeH <= 600, `B03=${lifeH}px`);
    note('B01 统计块已渲染', String(probe.stats).includes('NapCat'));
    note('B06 记忆已渲染', /特点|记着/.test(String(probe.memory)));
    note('B13 SD 配额已渲染', String(probe.sd).includes('群聊'));
    note('B11 渲染出可编辑开关', probe.switches > 0, `${probe.switches} 个`);
    note('B11 渲染出名单输入框', probe.hasAdminInput);
    note('B11 显示管理员昵称', probe.accessNotes.some(t => t.includes('示例用户1')),
         probe.accessNotes.filter(t => t.includes('（')).join(' | ').slice(0, 80));
    note('日志页有三个来源', probe.logSources.join(',') === 'bot,qzone,napcat',
         probe.logSources.join(','));

    // ── 新功能：出图预览 / 中文开关 / 表情包管理 ──
    note('B13 出图渲染成缩略图', probe.sdImgs > 0, probe.sdImgs + ' 张');
    note('缩略图 URL 带令牌（<img> 带不了请求头）',
         String(probe.sdImgSrc).includes('token='), String(probe.sdImgSrc).slice(0, 70));
    note('受限开关显示中文名',
         probe.restrictLabels.some(t => /发空间说说|聊天里生图|禁言群成员/.test(t)),
         probe.restrictLabels.slice(0, 6).join(' / '));
    note('B14 有表情包总开关', probe.stkSwitches >= 4, probe.stkSwitches + ' 个');
    note('B14 有链接添加入口', probe.stkHasUrlInput);
    note('B14 有上传入口', probe.stkHasFileInput);
    note('B14 说明了「不发图片」',
         /原生表情|不会用它|允许以图片/.test(String(probe.stkText)));

    // ── 生图模式 ──
    note('B13 显示三种出图模式',
         /只有她/.test(String(probe.sd)) && /纯景物/.test(String(probe.sd)));

    // ── B17 阈值进度条 / B04 好感心情 ──
    note('B17 渲染出可拖动进度条', probe.sliders >= 15, probe.sliders + ' 条');
    note('进度条带范围与步长', /^\d.*~\d/.test(String(probe.sliderSample)),
         String(probe.sliderSample));
    note('B04 好感和心情分开展示',
         /好感/.test(String(probe.moodText)) && /心情/.test(String(probe.moodText))
         && /100/.test(String(probe.moodText)),
         String(probe.moodText).slice(0, 90));
    note('B04 说明了「好感影响心情」',
         /好感[\s\S]{0,40}心情|往好感/.test(String(probe.moodText)));

    // ── 布局与视觉 ──
    note('有分区标题（布局重排）', probe.groupTitles >= 8, probe.groupTitles + ' 个');
    note('卡片入场动效已应用', probe.firstCardAnim === 'cardIn', probe.firstCardAnim);
    note('字体栈用了自定义 sans', /Inter|PingFang|Segoe UI/.test(String(probe.bodyFont)),
         String(probe.bodyFont));
    note('卡片圆角用新令牌', /14px|13|15/.test(String(probe.cardRadius)), String(probe.cardRadius));
    note('B03 有「她自己在做什么」', /她自己在做什么/.test(String(probe.life)),
         String(probe.life).slice(0, 60));
    note('B03 显示活动池与节奏',
         /活动池/.test(String(probe.life)) && /节奏/.test(String(probe.life)));

    // ── 记忆三分：人物 / 群聊 / 每轮刷新 ──
    note('B06 标题已改成「人物记忆」',
         /人物记忆/.test(String(probe.memoryCard)),
         String(probe.memoryCard).slice(0, 46));
    note('B06 说明了「留不留由你决定」',
         /留不留由你决定|你点批准/.test(String(probe.memoryCard)));
    note('B06 显示「每轮刷新（不落盘）」块',
         /每轮刷新/.test(String(probe.memoryText)) && /不落盘/.test(String(probe.memoryText)),
         String(probe.memoryText).slice(0, 60));
    note('B07 群聊记忆面板已渲染',
         /每群上限|总条数/.test(String(probe.groupMemCard)),
         String(probe.groupMemCard).slice(0, 60));
    note('B07 说明了它跟群走不跟人走',
         /跟.*群.*走|不跟人走|跟群走/.test(String(probe.groupMemCard)));

    await page.selectOption('#log-source', 'napcat');
    await sleep(1800);
    const napRows = await page.evaluate(() => document.querySelectorAll('#p-logs tbody tr').length);
    note('切到 NapCat 有日志行', napRows > 0, napRows + ' 行');
    await page.selectOption('#log-source', 'bot');
    await sleep(1500);
    note('切回 Bot 有日志行',
         (await page.evaluate(() => document.querySelectorAll('#p-logs tbody tr').length)) > 0);

    // 交互：点开关 → 校验落盘 → 点回去
    const readRestrict = k => JSON.parse(fs.readFileSync(TMP_CFG, 'utf8')).admin.restrict[k];
    const info = await page.evaluate(() => {
      const inp = document.querySelector('#p-access .switch input');
      const label = inp.closest('label');
      // 从 data-feature 取配置键 —— 界面文字现在是中文名（发空间说说），
      // 拿它去查 config 会查不到（这里踩过一次）
      return { feature: (label && label.dataset.feature) || '', checked: inp.checked,
               label: label ? label.innerText.trim() : '' };
    });
    note('开关能用 data-feature 定位到配置键',
         /^[a-z_]+$/.test(info.feature), `${info.label} → ${info.feature}`);
    await page.evaluate(() => document.querySelector('#p-access .switch input').click());
    await waitFor(() => readRestrict(info.feature) === !info.checked, 6000, '开关落盘');
    note(`点开关后 config 已落盘（${info.feature}: ${info.checked} → ${!info.checked}）`, true);
    note('toast 提示已出现', (await page.locator('.toast').count()) > 0);
    await page.screenshot({ path: path.join(SHOTS, 'toast.png') });
    await sleep(1200);
    await page.evaluate(() => document.querySelector('#p-access .switch input').click());
    await waitFor(() => readRestrict(info.feature) === info.checked, 6000, '开关还原');
    note('再点一次已还原', true);

    // 交互：确认模态
    await page.evaluate(() => {
      const b = [...document.querySelectorAll('#p-access button')]
        .find(x => x.textContent.includes('保存两份名单'));
      if (b) b.click();
    });
    await page.waitForSelector('#modal:not([hidden])', { timeout: 5000 });
    note('确认模态已弹出', true);
    await page.screenshot({ path: path.join(SHOTS, 'modal.png') });
    await page.click('#modal-cancel');
    await waitFor(async () => (await page.locator('#modal[hidden]').count()) > 0, 4000, '模态关闭');
    note('模态可取消关闭', true);

    await page.screenshot({ path: path.join(SHOTS, 'bot-dark.png'), fullPage: true });
    await page.evaluate(() => document.getElementById('theme-btn').click());
    await sleep(700);
    note('浅色主题可切换',
         (await page.evaluate(() => document.documentElement.getAttribute('data-theme'))) === 'light');
    await page.screenshot({ path: path.join(SHOTS, 'bot-light.png'), fullPage: true });
    await page.evaluate(() => document.getElementById('theme-btn').click());
    await sleep(400);

    await page.click('#btn-qzone');
    await sleep(2600);
    await page.screenshot({ path: path.join(SHOTS, 'qzone.png'), fullPage: true });
    const qz = await page.evaluate(() => {
      const g = id => { const e = document.getElementById(id); return e ? e.innerText.replace(/\s+/g, ' ').trim() : null; };
      return { status: g('q-status'), pollers: g('q-pollers'),
               features: g('q-features'), actions: g('q-actions') };
    });
    // 桥接可能被用户主动停掉（面板会显示离线提示，这是对的行为），
    // 这时不把"空间面板没内容"算成失败，只记一条说明。
    const qzText = String(qz.status) + ' ' + String(qz.pollers);
    if (/离线|不存在|重启|未监听|旧版本/.test(qzText)) {
      note('空间面板显示离线提示（桥接没在跑）', true, String(qz.status).slice(0, 46));
    } else {
      note('空间 Q01 已渲染', /已登录|未登录/.test(String(qz.status)),
           String(qz.status).slice(0, 50));
      note('空间 Q02 已渲染', String(qz.pollers).includes('说说'),
           String(qz.pollers).slice(0, 50));
    }
    // Q03/Q04 是本地数据，桥接在不在都该有内容
    note('Q03 列出三个动作与各自额度',
         /发说说/.test(String(qz.features)) && /评论/.test(String(qz.features))
         && /点赞/.test(String(qz.features)),
         String(qz.features).slice(0, 70));
    note('Q03 说明了点赞与评论相互独立',
         /两个独立动作|都会执行/.test(String(qz.features)));
    note('Q03 显示桥接连接信息', /地址/.test(String(qz.features)));
    note('Q03 有自主互动设置',
         /自主互动/.test(String(qz.features)) && /冷却/.test(String(qz.features)),
         String(qz.features).slice(120, 200));
    note('Q03 显示她对每个人的印象（心情决定互不互动）',
         /她对每个人的印象/.test(String(qz.features)));
    note('Q04 今日空间动作面板已渲染',
         qz.actions != null && /点赞|评论|说说|还没在空间/.test(String(qz.actions)),
         String(qz.actions).slice(0, 60));

    // ── 窄屏（420×900）：不得有横向溢出，卡片必须塌成单列 ──
    {
      const narrow = await browser.newPage({ viewport: { width: 420, height: 900 } });
      await narrow.goto(`${BASE}/?token=${encodeURIComponent(token)}`, { waitUntil: 'domcontentloaded' });
      await narrow.waitForFunction(
        () => { const e = document.getElementById('p-status'); return e && e.textContent.includes('Bot QQ'); },
        { timeout: 25000 });
      await sleep(1500);
      const np = await narrow.evaluate(() => {
        const cs = getComputedStyle(document.getElementById('tab-bot'));
        return { scrollW: document.documentElement.scrollWidth,
                 cols: cs.gridTemplateColumns.split(' ').length };
      });
      note('窄屏（420px）无横向溢出', np.scrollW <= 420, `scrollWidth=${np.scrollW}`);
      note('窄屏（420px）卡片单列', np.cols === 1, `cols=${np.cols}`);
      await narrow.screenshot({ path: path.join(SHOTS, 'narrow.png'), fullPage: true });
      await narrow.close();
    }

    // ── 卡片编号必须按页面顺序（B01,B02,… 连续递增），防止新面板又直接追加 B18 ──
    for (const tabName of ['bot', 'qzone']) {
      await page.evaluate(t => { const b = document.getElementById('btn-' + t); if (b) b.click(); }, tabName);
      await sleep(1500);
      const seq = await page.evaluate(t =>
        [...document.getElementById('tab-' + t).querySelectorAll(':scope > .card > h2 .n')]
          .map(e => e.textContent.trim()), tabName);
      const nums = seq.map(s => Number(String(s).replace(/^[BQ]/, '')));
      const ok = seq.length > 0 && /^[BQ]01$/.test(seq[0]) &&
                 nums.every((n, i) => i === 0 || n === nums[i - 1] + 1);
      note(`${tabName} 页卡片编号 = 页面顺序且连续递增`, ok, seq.join(' '));
    }

    // ── 所有角标（状态胶囊 .pill / 列表标签 .chip / 卡头编号 .n）必须是同一套外形 ──
    for (const tabName of ['bot', 'qzone']) {
      await page.evaluate(t => { const b = document.getElementById('btn-' + t); if (b) b.click(); }, tabName);
      await sleep(2200);
      const rep = await page.evaluate(t => {
        const els = [...document.querySelectorAll(`#tab-${t} .pill, #tab-${t} .chip, #tab-${t} .card>h2 .n`)]
          .filter(e => !e.querySelector('.switch'));   // 含开关的那几个本来就更宽，不比高度
        const props = ['borderRadius', 'fontSize', 'fontFamily', 'paddingTop', 'paddingRight',
                       'paddingBottom', 'paddingLeft'];
        const rows = els.map((e, i) => {
          const cs = getComputedStyle(e);
          return { i, who: (e.className || e.tagName) + '', v: props.map(p => cs[p]).join(' | ') };
        });
        const map = new Map();
        rows.forEach(r => { if (!map.has(r.v)) map.set(r.v, []); map.get(r.v).push(r); });
        return {
          n: rows.length,
          groups: [...map.entries()].map(([v, arr]) => ({ v, idx: arr.slice(0, 4).map(r => r.i),
                                                          who: arr.slice(0, 4).map(r => r.who) }))
        };
      }, tabName);
      note(`${tabName} 页所有角标同一套外形`, rep.n > 0 && rep.groups.length === 1,
           rep.n === 0 ? '没采到样本'
             : rep.groups.length === 1 ? `${rep.n} 个 · ${rep.groups[0].v}`
             : `${rep.n} 个却有 ${rep.groups.length} 种取值：` +
               JSON.stringify(rep.groups.map(g => ({ idx: g.idx, who: g.who, v: g.v }))).slice(0, 320));
    }

    // ── 大屏 / 中屏：列数按断点定死，每一行都不许空列 ──
    // 用户真实环境是 2560×1440 最大化（视口 ≈2545），旧版 auto-fit 会算出 4 列，
    // 而每个分区只有 3 张卡 → 每行右边空一整列。1560 与 420 都测不到这个宽度。
    for (const vp of [{ w: 2560, h: 1400, cols: 3, shot: 'wide.png' },
                      { w: 1024, h: 900,  cols: 2, shot: 'mid.png' }]) {
      const pg = await browser.newPage({ viewport: { width: vp.w, height: vp.h } });
      await pg.goto(`${BASE}/?token=${encodeURIComponent(token)}`, { waitUntil: 'domcontentloaded' });
      await pg.waitForFunction(
        () => { const e = document.getElementById('p-status'); return e && e.textContent.includes('Bot QQ'); },
        { timeout: 25000 });
      await sleep(1800);
      for (const tabName of ['bot', 'qzone']) {
        await pg.evaluate(t => { const b = document.getElementById('btn-' + t); if (b) b.click(); }, tabName);
        await sleep(2200);
        const rep = await pg.evaluate(t => {
          const sec = document.getElementById('tab-' + t);
          const cs = getComputedStyle(sec);
          const tracks = cs.gridTemplateColumns.split(' ').filter(Boolean).map(parseFloat);
          const gap = parseFloat(cs.columnGap) || 0;
          const container = sec.clientWidth;
          const rows = new Map();
          for (const c of sec.querySelectorAll(':scope > .card')) {
            const r = c.getBoundingClientRect();
            const key = Math.round(r.top);
            if (!rows.has(key)) rows.set(key, []);
            rows.get(key).push({ badge: (c.querySelector('h2 .n') || {}).textContent || '?',
                                 w: Math.round(r.width) });
          }
          const holes = [];
          for (const [top, arr] of rows) {
            const used = arr.reduce((s, c) => s + c.w, 0) + gap * (arr.length - 1);
            const left = Math.round(container - used);
            if (left >= tracks[0] * 0.9) {         // 空了大半列以上就算空列
              holes.push({ cards: arr.map(c => c.badge).join('+'), left });
            }
          }
          return { cols: tracks.length, trackW: Math.round(tracks[0]), container, holes,
                   lifeH: Math.round(document.querySelector('#p-life').closest('.card')
                             .getBoundingClientRect().height) };
        }, tabName);
        const tag = `${vp.w}px ${tabName}`;
        note(`${tag} 列数正确（按断点定死）`, rep.cols === vp.cols,
             `cols=${rep.cols} 列宽=${rep.trackW} 容器=${rep.container}`);
        note(`${tag} 每行都不空列`, rep.holes.length === 0,
             rep.holes.length ? '空列：' + rep.holes.map(h => `${h.cards} 空 ${h.left}px`).join('，')
                              : '每行都填满');
        if (tabName === 'bot') {
          note(`${vp.w}px 生活作息仍是横向面板（卡高 ≤600px）`,
               rep.lifeH > 0 && rep.lifeH <= 600, `B03=${rep.lifeH}px`);
        }
      }
      const sw = await pg.evaluate(() => document.documentElement.scrollWidth);
      note(`${vp.w}px 无横向溢出`, sw <= vp.w, `scrollWidth=${sw}`);
      // 截图前切回 Bot 页（上面两栏检查把 tab 切到了空间页，截图要取主 tab）
      await pg.evaluate(() => { const b = document.getElementById('btn-bot'); if (b) b.click(); });
      await sleep(2200);
      await pg.screenshot({ path: path.join(SHOTS, vp.shot), fullPage: true });
      await pg.close();
    }

    console.log('📸 截图目录：' + SHOTS);
    if (errors.length) {
      console.log('\n⚠ 浏览器报错 ' + errors.length + ' 条：');
      [...new Set(errors)].slice(0, 8).forEach(e => console.log('   ' + e));
    } else {
      console.log('\n✅ 浏览器控制台无报错');
    }

    const bad = results.filter(r => !r.ok);
    console.log('\n' + '='.repeat(52));
    console.log(`视觉 + 交互验证：通过 ${results.length - bad.length} 项，失败 ${bad.length} 项`);
    bad.forEach(b => console.log(`  ✗ ${b.name}  ${b.detail || ''}`));
    console.log('='.repeat(52));
    if (bad.length) process.exitCode = 1;
  } finally {
    if (browser) await browser.close().catch(() => {});
    child.kill('SIGKILL');
    await sleep(400);
    try { fs.unlinkSync(TMP_CFG); } catch (e) { /* ignore */ }
  }
}

main().catch(e => { console.error('失败:', e.message); process.exit(1); });

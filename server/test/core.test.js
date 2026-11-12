'use strict';
/** 端到端测试：通过真实 HTTP 接口验证核心规则与禁止项 */
process.env.TZ = 'Asia/Shanghai';
delete process.env.ANTHROPIC_API_KEY;
const test = require('node:test');
// Node 19 的 describe/before 钩子不稳定：收集用例，在一个顶层 test 中顺序执行
const cases = [];
const it = (name, fn) => cases.push([name, fn]);
const assert = require('node:assert/strict');
const bcrypt = require('bcryptjs');
const { Database } = require('../src/db');
const { seedDefaults, ensureAdmin } = require('../src/seed');
const { createApp } = require('../src/app');
const clock = require('../src/clock');
const L = require('../src/leads');
const { extractByRules } = require('../src/extract');
const { DAY } = clock;

let db; let server; let base;
const tokens = {};
const users = {};

async function call(who, method, url, body) {
  const res = await fetch(base + url, {
    method,
    headers: { 'content-type': 'application/json', ...(who ? { authorization: `Bearer ${tokens[who]}` } : {}) },
    body: body ? JSON.stringify(body) : undefined,
  });
  const text = await res.text();
  let data; try { data = JSON.parse(text); } catch (_) { data = text; }
  return { status: res.status, data };
}

async function setup() {
  db = await Database.open(null);
  seedDefaults(db);
  ensureAdmin(db, 'admin-pass-123');
  const now = Date.now();
  for (const [u, name, role] of [['dir', '周总监', 'director'], ['a', '销售甲', 'sales'], ['b', '销售乙', 'sales']]) {
    users[u] = db.run('INSERT INTO users (username, password_hash, name, role, created_at, updated_at) VALUES (?,?,?,?,?,?)',
      [u, bcrypt.hashSync('password1', 4), name, role, now, now]).lastInsertRowid;
  }
  const app = createApp(db, { jwtSecret: 'test-secret' });
  await new Promise((r) => { server = app.listen(0, r); });
  base = `http://127.0.0.1:${server.address().port}/api`;
  for (const u of ['dir', 'a', 'b']) tokens[u] = (await call(null, 'POST', '/auth/login', { username: u, password: 'password1' })).data.token;
  tokens.admin = (await call(null, 'POST', '/auth/login', { username: 'admin', password: 'admin-pass-123' })).data.token;
}

async function newLead(name, extra = {}) {
  const r = await call('dir', 'POST', '/leads', { company_name: name, contact_name: '张三', contact_phone: '13812345678', industry_id: 1, ...extra });
  assert.equal(r.status, 201, JSON.stringify(r.data));
  return r.data.id;
}

it('登录失败与未登录访问被拒绝', async () => {
  assert.equal((await call(null, 'POST', '/auth/login', { username: 'a', password: 'wrong' })).status, 401);
  assert.equal((await call(null, 'GET', '/leads/pool')).status, 401);
});

it('认领：一条线索只能被一人认领，并从公海可认领列表消失', async () => {
  const id = await newLead('甲乙丙饮料有限公司');
  const [r1, r2] = await Promise.all([call('a', 'POST', `/leads/${id}/claim`), call('b', 'POST', `/leads/${id}/claim`)]);
  const ok = [r1, r2].filter((r) => r.status === 200);
  assert.equal(ok.length, 1, '并发认领只能成功一个');
  assert.equal([r1, r2].find((r) => r.status !== 200).status, 409);
  const pool = await call('a', 'GET', '/leads/pool?size=200');
  assert.ok(!pool.data.items.some((l) => l.id === id), '已认领线索不应出现在可认领列表');
  // 台账仍保留，且标记认领人
  const ledger = await call('dir', 'GET', '/leads/ledger?size=200');
  const row = ledger.data.items.find((l) => l.id === id);
  assert.equal(row.status, 'owned');
  assert.ok(row.owner_name && row.claimed_at);
});

it('权限隔离：销售看不到同事私池线索、联系方式与跟进记录', async () => {
  const id = await newLead('隔离测试科技有限公司');
  await call('a', 'POST', `/leads/${id}/claim`);
  await call('a', 'POST', `/leads/${id}/followups`, { content: '甲的机密跟进' });
  const asB = await call('b', 'GET', `/leads/${id}`);
  assert.equal(asB.status, 403);
  assert.ok(!JSON.stringify(asB.data).includes('13812345678'));
  assert.equal((await call('b', 'POST', `/leads/${id}/followups`, { content: 'x' })).status, 403);
  assert.equal((await call('b', 'PUT', `/leads/${id}`, { contact_dept: 'x' })).status, 403);
  assert.equal((await call('b', 'POST', `/leads/${id}/release`, { note: 'x' })).status, 403);
  const mineB = await call('b', 'GET', '/leads/mine?size=200');
  assert.ok(!mineB.data.items.some((l) => l.id === id));
  assert.equal((await call('b', 'GET', '/leads/ledger')).status, 403);
  assert.equal((await call('b', 'GET', '/dashboard/summary')).status, 403);
  // 总监穿透查看
  const asDir = await call('dir', 'GET', `/leads/${id}`);
  assert.equal(asDir.status, 200);
  assert.equal(asDir.data.followups[0].content, '甲的机密跟进');
  assert.equal(asDir.data.contact_phone, '13812345678');
});

it('公海中对销售脱敏联系方式，认领后可见完整信息', async () => {
  const id = await newLead('脱敏测试传媒有限公司');
  const p = await call('a', 'GET', `/leads/${id}`);
  assert.equal(p.data.contact_phone, '138****5678');
  const c = await call('a', 'POST', `/leads/${id}/claim`);
  assert.equal(c.data.contact_phone, '13812345678');
});

it('普通销售禁止导出', async () => {
  assert.equal((await call('a', 'GET', '/export/leads')).status, 403);
  assert.equal((await call('a', 'GET', '/export/sales-stats')).status, 403);
  assert.equal((await call('dir', 'GET', '/export/leads?scope=all')).status, 200);
});

it('自动回流：30 天无有效更新即回收，历史记录全部保留', async () => {
  const id = await newLead('回流测试汽车销售有限公司');
  await call('a', 'POST', `/leads/${id}/claim`);
  await call('a', 'POST', `/leads/${id}/followups`, { content: '第一次拜访' });

  // 第 29 天：未到期，不回收
  clock.offset = 29 * DAY;
  L.recycleExpired(db);
  assert.equal((await call('a', 'GET', `/leads/${id}`)).data.status, 'owned');
  // 第 29 天做一次有效更新（修改信息）→ 计时重置
  const up = await call('a', 'PUT', `/leads/${id}`, { contact_title: '市场总监' });
  assert.equal(up.status, 200);
  assert.equal(up.data.changed, true);
  // 第 31 天：距上次有效更新只有 2 天，不回收
  clock.offset = 31 * DAY;
  L.recycleExpired(db); // 其它用例的线索此时会到期，这里只关心本条
  assert.equal((await call('a', 'GET', `/leads/${id}`)).data.status, 'owned');
  // 第 59 天 + 1 分钟：距上次有效更新满 30 天，回收
  clock.offset = 59 * DAY + 60000;
  assert.ok(L.recycleExpired(db) >= 1);

  const d = await call('dir', 'GET', `/leads/${id}`);
  assert.equal(d.data.status, 'pool');
  assert.equal(d.data.owner_id, null);
  assert.equal(d.data.last_release_type, 'auto_recycle');
  assert.equal(d.data.last_owner_name, '销售甲');
  assert.equal(d.data.followups.length, 1, '历史跟进必须保留');
  assert.equal(d.data.claim_history[0].end_reason, 'auto_recycle');
  assert.ok(d.data.logs.some((l) => l.action === 'auto_recycle'));

  // 其他销售可再次认领，并能看到历史跟进（继承历史开关默认开启）
  const c = await call('b', 'POST', `/leads/${id}/claim`);
  assert.equal(c.status, 200);
  assert.equal(c.data.followups.length, 1);
  assert.equal(c.data.claim_history.length, 2);
});

it('无变化的保存不算有效更新，不重置计时', async () => {
  const id = await newLead('无效更新测试有限公司');
  const c = await call('a', 'POST', `/leads/${id}/claim`);
  clock.offset = 5 * DAY;
  const up = await call('a', 'PUT', `/leads/${id}`, { company_name: '无效更新测试有限公司' });
  assert.equal(up.data.changed, false);
  assert.equal(up.data.lead.expire_at, c.data.expire_at);
});

it('手动退回需填写原因；总监可强制回收，销售不可', async () => {
  const id = await newLead('退回测试保险代理有限公司');
  await call('a', 'POST', `/leads/${id}/claim`);
  assert.equal((await call('a', 'POST', `/leads/${id}/release`, {})).status, 400);
  assert.equal((await call('b', 'POST', `/leads/${id}/recycle`, { note: 'x' })).status, 403);
  assert.equal((await call('a', 'POST', `/leads/${id}/release`, { note: '客户已有合作方' })).status, 200);
  await call('b', 'POST', `/leads/${id}/claim`);
  assert.equal((await call('dir', 'POST', `/leads/${id}/recycle`, { note: '分配调整' })).status, 200);
  const d = await call('dir', 'GET', `/leads/${id}`);
  assert.deepEqual(d.data.claim_history.map((c) => c.end_reason), ['force_recycle', 'release']);
  // 公海中可看到“曾被认领”标记
  const pool = await call('a', 'GET', `/leads/pool?q=${encodeURIComponent('退回测试')}`);
  assert.equal(pool.data.items[0].last_owner_name, '销售乙');
});

it('总监也有个人池，遵守同样的回流规则', async () => {
  const id = await newLead('总监私池测试有限公司');
  await call('dir', 'POST', `/leads/${id}/claim`);
  assert.equal((await call('a', 'GET', `/leads/${id}`)).status, 403);
  clock.offset = 30 * DAY + 1000;
  L.recycleExpired(db);
  assert.equal((await call('dir', 'GET', `/leads/${id}`)).data.status, 'pool');
});

it('日志、跟进记录、线索台账不可删除', async () => {
  assert.throws(() => db.run('DELETE FROM lead_logs'));
  assert.throws(() => db.run("UPDATE lead_logs SET action = 'x'"));
  assert.throws(() => db.run('DELETE FROM followups'));
  assert.throws(() => db.run('DELETE FROM leads'));
});

it('重复公司录入被拦截', async () => {
  await newLead('重复测试金融服务有限公司');
  const r = await call('a', 'POST', '/leads', { company_name: '重复测试金融服务有限公司', contact_name: '李四' });
  assert.equal(r.status, 409);
});

it('修改回流周期后，私池到期时间按新周期重算', async () => {
  const id = await newLead('周期测试食品有限公司');
  const c = await call('a', 'POST', `/leads/${id}/claim`);
  assert.equal((await call('admin', 'PUT', '/admin/settings', { recycle_days: 15 })).status, 200);
  const d = await call('a', 'GET', `/leads/${id}`);
  assert.equal(d.data.expire_at, c.data.last_effective_at + 15 * DAY);
  await call('admin', 'PUT', '/admin/settings', { recycle_days: 30 });
  assert.equal((await call('dir', 'PUT', '/admin/settings', { recycle_days: 5 })).status, 403);
});

it('停用销售账号时其名下线索统一回收公海', async () => {
  const now = Date.now();
  const uid = db.run('INSERT INTO users (username, password_hash, name, role, created_at, updated_at) VALUES (?,?,?,?,?,?)',
    ['leaver', bcrypt.hashSync('password1', 4), '离职销售', 'sales', now, now]).lastInsertRowid;
  tokens.leaver = (await call(null, 'POST', '/auth/login', { username: 'leaver', password: 'password1' })).data.token;
  const id = await newLead('离职测试网络科技有限公司');
  await call('leaver', 'POST', `/leads/${id}/claim`);
  const r = await call('admin', 'PUT', `/admin/users/${uid}`, { active: false });
  assert.equal(r.data.recycled, 1);
  assert.equal((await call('leaver', 'GET', '/leads/mine')).status, 401);
  assert.equal((await call('dir', 'GET', `/leads/${id}`)).data.status, 'pool');
});

it('CSV 批量导入：校验行业、跳过重复', async () => {
  const csv = '公司全称,对接人姓名,联系电话,所属行业\n导入一号有限公司,王五,13900000001,快消\n导入一号有限公司,王五,,快消\n导入二号有限公司,赵六,,不存在行业\n,缺公司,,\n';
  const r = await fetch(base + '/leads/import', { method: 'POST', headers: { 'content-type': 'text/csv', authorization: `Bearer ${tokens.dir}` }, body: csv });
  const d = await r.json();
  assert.equal(d.created, 1);
  assert.equal(d.skipped.length, 1);
  assert.equal(d.errors.length, 2);
});

it('看板统计可用', async () => {
  const r = await call('dir', 'GET', '/dashboard/summary');
  assert.equal(r.status, 200);
  assert.ok(r.data.totals.total > 0);
  assert.ok(r.data.perSales.find((p) => p.name === '销售甲'));
});

it('语音文本本地规则识别', () => {
  const ctx = { industries: ['快消', '互联网/网络', '汽车', '金融', '保险'], stages: ['初次联系', '待报价', '方案沟通', '已成交', '暂无意向'],
    intents: ['高意向', '中等意向', '低意向'], sources: ['渠道A', '渠道B', '线下收集', '线上'] };
  const f = extractByRules('今天拜访了蓝湾饮料有限公司，对接人叫王小明，市场部的品牌经理，电话13912345678，微信wxm_2024，地址在杭州市西湖区文三路100号，客户很感兴趣，要我们报价，展会上认识的', ctx);
  assert.equal(f.company_name, '蓝湾饮料有限公司');
  assert.equal(f.contact_name, '王小明');
  assert.equal(f.contact_phone, '13912345678');
  assert.equal(f.contact_wechat, 'wxm_2024');
  assert.equal(f.contact_title, '品牌经理');
  assert.equal(f.contact_dept, '市场部');
  assert.equal(f.industry, '快消');
  assert.equal(f.intent_level, '高意向');
  assert.equal(f.stage, '待报价');
  assert.equal(f.source, '线下收集');
  assert.ok(f.address && f.address.includes('文三路'));
});

test('地标传媒线索公海系统', async (t) => {
  await setup();
  try {
    for (const [name, fn] of cases) {
      await t.test(name, async () => { try { await fn(); } finally { clock.offset = 0; } });
    }
  } finally {
    server.close();
  }
});

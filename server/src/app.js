'use strict';
const path = require('path');
const express = require('express');
const jwt = require('jsonwebtoken');
const bcrypt = require('bcryptjs');
const clock = require('./clock');
const L = require('./leads');
const R = require('./reports');
const { extract } = require('./extract');
const { AppError, isManager } = L;

function createApp(db, { jwtSecret, webRoot }) {
  const app = express();
  app.disable('x-powered-by');
  app.set('trust proxy', 1);
  app.use(express.json({ limit: '10mb' }));
  app.use(express.text({ type: ['text/csv', 'text/plain'], limit: '10mb' }));

  // 安全响应头
  app.use((req, res, next) => {
    res.set('X-Content-Type-Options', 'nosniff');
    res.set('Referrer-Policy', 'same-origin');
    if (req.path.startsWith('/api/')) res.set('Cache-Control', 'no-store');
    next();
  });

  // ---------- 登录限流（简单内存版：同一账号 / IP 10 分钟内失败 10 次即锁定） ----------
  const fails = new Map();
  const failKey = (req) => `${req.ip}|${String(req.body && req.body.username || '').toLowerCase()}`;

  const sign = (u) => jwt.sign({ uid: u.id, v: u.token_version }, jwtSecret, { expiresIn: '7d' });
  const publicUser = (u) => ({ id: u.id, username: u.username, name: u.name, role: u.role, phone: u.phone, active: u.active });

  function auth(req, res, next) {
    const h = req.get('authorization') || '';
    const token = h.startsWith('Bearer ') ? h.slice(7) : null;
    if (!token) throw new AppError(401, '请先登录');
    let payload;
    try { payload = jwt.verify(token, jwtSecret); } catch (_) { throw new AppError(401, '登录已过期，请重新登录'); }
    const u = db.get('SELECT * FROM users WHERE id = ?', [payload.uid]);
    if (!u || !u.active || u.token_version !== payload.v) throw new AppError(401, '账号已停用或密码已修改，请重新登录');
    req.user = u;
    next();
  }
  const manager = (req, res, next) => { if (!isManager(req.user)) throw new AppError(403, '需要销售总监权限'); next(); };
  const admin = (req, res, next) => { if (req.user.role !== 'admin') throw new AppError(403, '需要超级管理员权限'); next(); };

  const api = express.Router();

  // ---------- 认证 ----------
  api.post('/auth/login', (req, res) => {
    const key = failKey(req);
    const f = fails.get(key);
    if (f && f.count >= 10 && clock.now() - f.at < 10 * 60 * 1000) throw new AppError(429, '失败次数过多，请 10 分钟后再试');
    const { username, password } = req.body || {};
    const u = db.get('SELECT * FROM users WHERE username = ?', [String(username || '').trim()]);
    if (!u || !bcrypt.compareSync(String(password || ''), u.password_hash)) {
      fails.set(key, { count: (f && clock.now() - f.at < 10 * 60 * 1000 ? f.count : 0) + 1, at: clock.now() });
      throw new AppError(401, '账号或密码错误');
    }
    if (!u.active) throw new AppError(403, '账号已停用，请联系管理员');
    fails.delete(key);
    res.json({ token: sign(u), user: publicUser(u) });
  });

  api.use(auth);

  api.get('/auth/me', (req, res) => res.json({ user: publicUser(req.user) }));
  api.post('/auth/password', (req, res) => {
    const { old_password, new_password } = req.body || {};
    if (!bcrypt.compareSync(String(old_password || ''), req.user.password_hash)) throw new AppError(400, '原密码不正确');
    if (String(new_password || '').length < 8) throw new AppError(400, '新密码至少 8 位');
    db.run('UPDATE users SET password_hash = ?, token_version = token_version + 1, updated_at = ? WHERE id = ?',
      [bcrypt.hashSync(String(new_password), 10), clock.now(), req.user.id]);
    const u = db.get('SELECT * FROM users WHERE id = ?', [req.user.id]);
    res.json({ token: sign(u) });
  });

  // ---------- 基础数据 ----------
  api.get('/meta', (req, res) => {
    const s = L.getSettings(db);
    const opts = (type) => db.all('SELECT id, value, is_won FROM options WHERE type = ? AND active = 1 ORDER BY sort, id', [type]);
    res.json({
      industries: db.all('SELECT id, name FROM industries WHERE active = 1 ORDER BY sort, id'),
      all_industries: db.all('SELECT id, name, active FROM industries ORDER BY sort, id'),
      stages: opts('stage'), intents: opts('intent'), sources: opts('source'),
      settings: { recycle_days: s.recycle_days, warn_days: s.warn_days, sales_can_create: s.sales_can_create, ai_enabled: !!process.env.ANTHROPIC_API_KEY },
      sales: isManager(req.user)
        ? db.all("SELECT id, name, role, active FROM users WHERE role IN ('sales','director') ORDER BY role DESC, id") : [],
    });
  });

  // ---------- 线索 ----------
  api.get('/leads/pool', (req, res) => res.json(L.listPool(db, req.user, req.query)));
  api.get('/leads/mine', (req, res) => res.json(L.listMine(db, req.user, req.query)));
  api.get('/leads/ledger', manager, (req, res) => res.json(L.listLedger(db, req.user, req.query)));
  api.get('/leads/import/template', manager, (req, res) => {
    res.type('text/csv').attachment('线索导入模板.csv').send(R.importTemplate());
  });
  api.post('/leads/import', manager, (req, res) => {
    const text = typeof req.body === 'string' ? req.body : req.body && req.body.csv;
    res.json(R.importCsv(db, req.user, text));
  });
  api.post('/leads/check-duplicate', (req, res) => {
    const name = String((req.body && req.body.company_name) || '').trim();
    const dup = name ? L.findDuplicate(db, name) : null;
    if (!dup) return res.json({ duplicate: false });
    const visible = isManager(req.user) || dup.status === 'pool' || dup.owner_id === req.user.id;
    res.json({ duplicate: true, status: dup.status, mine: dup.owner_id === req.user.id, id: visible ? dup.id : undefined });
  });
  api.post('/leads', (req, res) => {
    const id = L.createLead(db, req.user, req.body || {}, { claim: !!(req.body && req.body.claim) });
    res.status(201).json(L.getLead(db, req.user, id));
  });
  api.get('/leads/:id', (req, res) => res.json(L.getLead(db, req.user, req.params.id)));
  api.put('/leads/:id', (req, res) => res.json(L.updateLead(db, req.user, req.params.id, req.body || {}, req.body && req.body._via)));
  api.post('/leads/:id/claim', (req, res) => res.json(L.claim(db, req.user, req.params.id)));
  api.post('/leads/:id/release', (req, res) => res.json(L.release(db, req.user, req.params.id, req.body && req.body.note)));
  api.post('/leads/:id/recycle', manager, (req, res) => res.json(L.forceRecycle(db, req.user, req.params.id, req.body && req.body.note)));
  api.post('/leads/:id/followups', (req, res) => res.status(201).json(L.addFollowup(db, req.user, req.params.id, req.body || {})));

  // ---------- AI 语音识别填充 ----------
  api.post('/ai/extract', async (req, res) => {
    const rows = db.all('SELECT id, name FROM industries WHERE active = 1');
    const opts = (type) => db.all('SELECT value FROM options WHERE type = ? AND active = 1 ORDER BY sort', [type]).map((r) => r.value);
    const result = await extract(req.body && req.body.text, {
      industryRows: rows, industries: rows.map((r) => r.name),
      stages: opts('stage'), intents: opts('intent'), sources: opts('source'),
    });
    if (req.body && req.body.lead_id) {
      // 已有线索上口述跟进时，公司名/对接人已存在，不再提示必填缺失
      result.missing = [];
      if (Object.keys(result.fields).length || result.followup) result.message = '识别完成，请核对后保存';
      L.log(db, { lead_id: Number(req.body.lead_id) || null, action: 'voice_parse', actor: req.user, owner_id: req.user.id, detail: { engine: result.engine, fields: Object.keys(result.fields) } });
    }
    res.json(result);
  });

  // ---------- 总监看板 ----------
  api.get('/dashboard/summary', manager, (req, res) => res.json(R.summary(db, req.user)));
  api.get('/dashboard/warnings', manager, (req, res) => res.json(R.warnings(db, req.user, req.query)));
  api.get('/dashboard/flows', manager, (req, res) => res.json(R.flows(db, req.user, req.query)));

  // ---------- 导出（普通销售无权） ----------
  const stamp = () => new Date(clock.now()).toISOString().slice(0, 10);
  api.get('/export/leads', manager, (req, res) => {
    const scope = String(req.query.scope || 'all');
    res.type('text/csv').attachment(`线索_${scope}_${stamp()}.csv`).send(R.exportLeads(db, req.user, scope));
  });
  api.get('/export/sales-stats', manager, (req, res) => {
    res.type('text/csv').attachment(`销售统计_${stamp()}.csv`).send(R.exportSalesStats(db, req.user));
  });

  // ---------- 超级管理员 ----------
  const ad = express.Router();
  ad.use(admin);
  ad.get('/users', (req, res) => res.json(db.all(`SELECT u.id, u.username, u.name, u.role, u.phone, u.active, u.created_at,
      (SELECT COUNT(*) FROM leads l WHERE l.owner_id = u.id) AS holding FROM users u ORDER BY u.active DESC, u.role, u.id`)));
  ad.post('/users', (req, res) => {
    const { username, name, role, phone, password } = req.body || {};
    if (!/^[A-Za-z0-9_.-]{3,32}$/.test(String(username || ''))) throw new AppError(400, '账号需为 3-32 位字母/数字/下划线');
    if (!String(name || '').trim()) throw new AppError(400, '请填写姓名');
    if (!['sales', 'director', 'admin'].includes(role)) throw new AppError(400, '角色无效');
    if (String(password || '').length < 8) throw new AppError(400, '初始密码至少 8 位');
    if (db.get('SELECT 1 FROM users WHERE username = ?', [username])) throw new AppError(409, '账号已存在');
    const now = clock.now();
    const r = db.run('INSERT INTO users (username, password_hash, name, role, phone, created_at, updated_at) VALUES (?,?,?,?,?,?,?)',
      [username, bcrypt.hashSync(String(password), 10), String(name).trim(), role, phone || null, now, now]);
    L.log(db, { action: 'user_create', actor: req.user, detail: { user_id: r.lastInsertRowid, username, role } });
    res.status(201).json({ id: r.lastInsertRowid });
  });
  ad.put('/users/:id', (req, res) => {
    const u = db.get('SELECT * FROM users WHERE id = ?', [Number(req.params.id)]);
    if (!u) throw new AppError(404, '用户不存在');
    const b = req.body || {};
    const name = b.name != null ? String(b.name).trim() : u.name;
    const role = b.role || u.role;
    const active = b.active != null ? (b.active ? 1 : 0) : u.active;
    if (!['sales', 'director', 'admin'].includes(role)) throw new AppError(400, '角色无效');
    if (u.id === req.user.id && (role !== 'admin' || !active)) throw new AppError(400, '不能停用自己或取消自己的管理员身份');
    let recycled = 0;
    db.tx(() => {
      db.run('UPDATE users SET name = ?, role = ?, phone = ?, active = ?, token_version = token_version + ?, updated_at = ? WHERE id = ?',
        [name, role, b.phone !== undefined ? b.phone || null : u.phone, active, active !== u.active || role !== u.role ? 1 : 0, clock.now(), u.id]);
      if (u.active && !active) recycled = L.recycleAllOf(db, req.user, u.id, `账号停用（${u.name}），线索统一回收公海`);
      L.log(db, { action: 'user_update', actor: req.user, detail: { user_id: u.id, name, role, active, recycled } });
    });
    res.json({ ok: true, recycled });
  });
  ad.post('/users/:id/reset-password', (req, res) => {
    const pwd = String((req.body && req.body.password) || '');
    if (pwd.length < 8) throw new AppError(400, '新密码至少 8 位');
    const r = db.run('UPDATE users SET password_hash = ?, token_version = token_version + 1, updated_at = ? WHERE id = ?',
      [bcrypt.hashSync(pwd, 10), clock.now(), Number(req.params.id)]);
    if (!r.changes) throw new AppError(404, '用户不存在');
    L.log(db, { action: 'user_reset_password', actor: req.user, detail: { user_id: Number(req.params.id) } });
    res.json({ ok: true });
  });

  // 行业标签
  ad.post('/industries', (req, res) => {
    const name = String((req.body && req.body.name) || '').trim();
    if (!name) throw new AppError(400, '请填写行业名称');
    if (db.get('SELECT 1 FROM industries WHERE name = ?', [name])) throw new AppError(409, '行业已存在');
    const max = db.get('SELECT COALESCE(MAX(sort), 0) AS m FROM industries').m;
    const r = db.run('INSERT INTO industries (name, sort, created_at) VALUES (?,?,?)', [name, max + 1, clock.now()]);
    res.status(201).json({ id: r.lastInsertRowid });
  });
  ad.put('/industries/:id', (req, res) => {
    const b = req.body || {};
    const row = db.get('SELECT * FROM industries WHERE id = ?', [Number(req.params.id)]);
    if (!row) throw new AppError(404, '行业不存在');
    const name = b.name != null ? String(b.name).trim() : row.name;
    if (!name) throw new AppError(400, '名称不能为空');
    if (db.get('SELECT 1 FROM industries WHERE name = ? AND id != ?', [name, row.id])) throw new AppError(409, '行业已存在');
    db.run('UPDATE industries SET name = ?, sort = ?, active = ? WHERE id = ?',
      [name, b.sort != null ? Number(b.sort) : row.sort, b.active != null ? (b.active ? 1 : 0) : row.active, row.id]);
    res.json({ ok: true });
  });
  ad.delete('/industries/:id', (req, res) => {
    const id = Number(req.params.id);
    const used = db.get('SELECT COUNT(*) AS c FROM leads WHERE industry_id = ?', [id]).c;
    if (used) {
      db.run('UPDATE industries SET active = 0 WHERE id = ?', [id]);
      return res.json({ ok: true, soft: true, message: `已有 ${used} 条线索使用该标签，已改为停用（历史数据保留）` });
    }
    db.run('DELETE FROM industries WHERE id = ?', [id]);
    res.json({ ok: true });
  });

  // 下拉选项
  ad.get('/options', (req, res) => res.json(db.all('SELECT * FROM options ORDER BY type, sort, id')));
  ad.post('/options', (req, res) => {
    const { type, value, is_won } = req.body || {};
    const v = String(value || '').trim();
    if (!['stage', 'intent', 'source'].includes(type) || !v) throw new AppError(400, '参数无效');
    if (db.get('SELECT 1 FROM options WHERE type = ? AND value = ?', [type, v])) throw new AppError(409, '选项已存在');
    const max = db.get('SELECT COALESCE(MAX(sort), 0) AS m FROM options WHERE type = ?', [type]).m;
    const r = db.run('INSERT INTO options (type, value, sort, is_won, created_at) VALUES (?,?,?,?,?)', [type, v, max + 1, is_won ? 1 : 0, clock.now()]);
    res.status(201).json({ id: r.lastInsertRowid });
  });
  ad.put('/options/:id', (req, res) => {
    const o = db.get('SELECT * FROM options WHERE id = ?', [Number(req.params.id)]);
    if (!o) throw new AppError(404, '选项不存在');
    const b = req.body || {};
    const v = b.value != null ? String(b.value).trim() : o.value;
    if (!v) throw new AppError(400, '名称不能为空');
    if (v !== o.value && db.get('SELECT 1 FROM options WHERE type = ? AND value = ?', [o.type, v])) throw new AppError(409, '选项已存在');
    const col = o.type === 'intent' ? 'intent_level' : o.type;
    db.tx(() => {
      db.run('UPDATE options SET value = ?, sort = ?, is_won = ?, active = ? WHERE id = ?',
        [v, b.sort != null ? Number(b.sort) : o.sort, b.is_won != null ? (b.is_won ? 1 : 0) : o.is_won, b.active != null ? (b.active ? 1 : 0) : o.active, o.id]);
      if (v !== o.value) db.run(`UPDATE leads SET ${col} = ? WHERE ${col} = ?`, [v, o.value]); // 改名同步到线索
    });
    res.json({ ok: true });
  });
  ad.delete('/options/:id', (req, res) => {
    const o = db.get('SELECT * FROM options WHERE id = ?', [Number(req.params.id)]);
    if (!o) throw new AppError(404, '选项不存在');
    const col = o.type === 'intent' ? 'intent_level' : o.type;
    const used = db.get(`SELECT COUNT(*) AS c FROM leads WHERE ${col} = ?`, [o.value]).c;
    if (used) {
      db.run('UPDATE options SET active = 0 WHERE id = ?', [o.id]);
      return res.json({ ok: true, soft: true, message: `已有 ${used} 条线索使用该选项，已改为停用` });
    }
    db.run('DELETE FROM options WHERE id = ?', [o.id]);
    res.json({ ok: true });
  });

  // 系统参数
  ad.get('/settings', (req, res) => res.json(L.getSettings(db)));
  ad.put('/settings', (req, res) => {
    const b = req.body || {};
    const before = L.getSettings(db);
    const set = (k, v) => db.run('INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value', [k, String(v)]);
    db.tx(() => {
      if (b.recycle_days != null) {
        const d = parseInt(b.recycle_days, 10);
        if (!(d >= 1 && d <= 365)) throw new AppError(400, '回流周期需在 1-365 天之间');
        set('recycle_days', d);
      }
      if (b.warn_days != null) {
        const d = parseInt(b.warn_days, 10);
        if (!(d >= 0 && d <= 60)) throw new AppError(400, '预警天数需在 0-60 天之间');
        set('warn_days', d);
      }
      if (b.sales_max_leads != null) set('sales_max_leads', Math.max(0, parseInt(b.sales_max_leads, 10) || 0));
      for (const k of ['pool_mask_phone', 'inherit_history', 'sales_can_create']) if (b[k] != null) set(k, b[k] ? 1 : 0);
      const after = L.getSettings(db);
      if (after.recycle_days !== before.recycle_days) L.recomputeExpiry(db);
      L.log(db, { action: 'settings_update', actor: req.user, detail: { before, after } });
    });
    res.json(L.getSettings(db));
  });
  ad.post('/recycle/run', (req, res) => res.json({ recycled: L.recycleExpired(db) }));
  ad.get('/logs', (req, res) => {
    const size = Math.min(500, parseInt(req.query.size, 10) || 100);
    res.json(db.all(`SELECT g.*, l.company_name FROM lead_logs g LEFT JOIN leads l ON l.id = g.lead_id ORDER BY g.id DESC LIMIT ?`, [size])
      .map((r) => ({ ...r, detail: r.detail ? JSON.parse(r.detail) : null })));
  });

  api.use('/admin', ad);
  app.use('/api', api);
  app.use('/api', (req, res) => res.status(404).json({ error: '接口不存在' }));

  // ---------- 前端静态资源（H5 / 管理端同一套 SPA） ----------
  if (webRoot) {
    app.use(express.static(webRoot, {
      setHeaders(res, p) { if (/(index\.html|sw\.js)$/.test(p)) res.set('Cache-Control', 'no-cache'); },
    }));
    app.get('/vendor/vue.global.prod.js', (req, res) => res.sendFile(require.resolve('vue/dist/vue.global.prod.js')));
    app.use((req, res, next) => (req.method === 'GET' ? res.sendFile(path.join(webRoot, 'index.html')) : next()));
  }

  // ---------- 统一错误处理 ----------
  // eslint-disable-next-line no-unused-vars
  app.use((err, req, res, next) => {
    if (err instanceof AppError) return res.status(err.status).json({ error: err.message, ...(err.extra || {}) });
    if (err.type === 'entity.too.large') return res.status(413).json({ error: '上传内容过大' });
    if (err.type === 'entity.parse.failed') return res.status(400).json({ error: '请求格式错误' });
    if (/RAISE|不可/.test(err.message || '')) return res.status(403).json({ error: err.message });
    console.error(err);
    res.status(500).json({ error: '服务器内部错误' });
  });
  return app;
}

module.exports = { createApp };

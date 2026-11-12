'use strict';
/**
 * 线索核心业务：公海 / 私池 / 认领 / 退回 / 自动回流 / 强制回收 / 跟进 / 日志。
 * 所有权限判断集中在本文件，路由层只做参数解析。
 */
const clock = require('./clock');
const { DAY } = clock;

class AppError extends Error {
  constructor(status, message, extra) {
    super(message);
    this.status = status;
    this.extra = extra;
  }
}

const isManager = (u) => u && (u.role === 'director' || u.role === 'admin');

// 销售可编辑的线索字段（系统字段不在此列，用户不可编辑）
const EDITABLE = ['company_name', 'contact_name', 'contact_dept', 'contact_title', 'contact_phone',
  'contact_wechat', 'address', 'industry_id', 'source', 'intent_level', 'stage', 'remark'];
const FIELD_LABELS = {
  company_name: '公司全称', contact_name: '对接人姓名', contact_dept: '对接人部门', contact_title: '对接人岗位',
  contact_phone: '联系电话', contact_wechat: '微信', address: '办公地址', industry_id: '所属行业',
  source: '线索来源', intent_level: '意向等级', stage: '跟进阶段', remark: '备注',
};

// ---------- 配置 ----------
function getSettings(db) {
  const s = {};
  for (const r of db.all('SELECT key, value FROM settings')) s[r.key] = r.value;
  return {
    recycle_days: Math.max(1, parseInt(s.recycle_days, 10) || 30),
    warn_days: Math.max(0, parseInt(s.warn_days, 10) || 7),
    pool_mask_phone: s.pool_mask_phone !== '0',
    inherit_history: s.inherit_history !== '0',
    sales_can_create: s.sales_can_create !== '0',
    sales_max_leads: Math.max(0, parseInt(s.sales_max_leads, 10) || 0),
  };
}

// ---------- 日志（只增不改） ----------
function log(db, { lead_id = null, action, actor, owner_id = null, detail = null }) {
  db.run('INSERT INTO lead_logs (lead_id, action, actor_id, actor_name, owner_id, detail, created_at) VALUES (?,?,?,?,?,?,?)',
    [lead_id, action, actor ? actor.id : null, actor ? actor.name : '系统', owner_id,
      detail == null ? null : JSON.stringify(detail), clock.now()]);
}

// ---------- 查询辅助 ----------
const LEAD_SELECT = `
  SELECT l.*, i.name AS industry_name, o.name AS owner_name, lo.name AS last_owner_name
  FROM leads l
  LEFT JOIN industries i ON i.id = l.industry_id
  LEFT JOIN users o ON o.id = l.owner_id
  LEFT JOIN users lo ON lo.id = l.last_owner_id`;

function maskPhone(v) {
  if (!v) return v;
  const s = String(v);
  if (s.length >= 7) return s.slice(0, 3) + '****' + s.slice(-4);
  return s.slice(0, 1) + '***';
}

/** 按查看人身份输出线索：普通销售查看公海线索时按配置脱敏 */
function present(lead, user, settings) {
  if (!lead) return lead;
  const out = { ...lead };
  const now = clock.now();
  if (out.status === 'owned' && out.expire_at) {
    out.days_left = Math.ceil((out.expire_at - now) / DAY);
    out.expiring = out.expire_at - now <= settings.warn_days * DAY;
  }
  const mine = out.owner_id === user.id;
  if (!isManager(user) && !mine) {
    if (settings.pool_mask_phone) {
      out.contact_phone = maskPhone(out.contact_phone);
      out.contact_wechat = maskPhone(out.contact_wechat);
      out.masked = true;
    }
    delete out.remark;
  }
  out.can_edit = mine;
  return out;
}

function buildFilters(f, where, params) {
  if (f.industry_id) { where.push('l.industry_id = ?'); params.push(Number(f.industry_id)); }
  if (f.intent_level) { where.push('l.intent_level = ?'); params.push(f.intent_level); }
  if (f.stage) { where.push('l.stage = ?'); params.push(f.stage); }
  if (f.source) { where.push('l.source = ?'); params.push(f.source); }
  if (f.q) {
    where.push('(l.company_name LIKE ? OR l.contact_name LIKE ? OR l.address LIKE ?)');
    const q = `%${String(f.q).trim()}%`;
    params.push(q, q, q);
  }
}

function buildExpireFilter(f, where, params, settings) {
  const now = clock.now();
  if (f.expire === 'warn') { where.push('l.expire_at <= ?'); params.push(now + settings.warn_days * DAY); }
  else if (/^\d+$/.test(String(f.expire || ''))) { where.push('l.expire_at <= ?'); params.push(now + Number(f.expire) * DAY); }
}

function paginate(db, baseSql, where, params, f, order) {
  const page = Math.max(1, parseInt(f.page, 10) || 1);
  const size = Math.min(200, Math.max(1, parseInt(f.size, 10) || 20));
  const w = where.length ? ' WHERE ' + where.join(' AND ') : '';
  const total = db.get(`SELECT COUNT(*) AS c FROM (${baseSql}${w})`, params).c;
  const items = db.all(`${baseSql}${w} ORDER BY ${order} LIMIT ? OFFSET ?`, [...params, size, (page - 1) * size]);
  return { total, page, size, items };
}

// ---------- 列表 ----------
/** 公海可认领列表：所有角色可见，已认领线索不出现 */
function listPool(db, user, f = {}) {
  const settings = getSettings(db);
  const where = ["l.status = 'pool'"];
  const params = [];
  buildFilters(f, where, params);
  if (f.returned === '1') where.push('l.claim_count > 0');
  if (f.returned === '0') where.push('l.claim_count = 0');
  const r = paginate(db, LEAD_SELECT, where, params, f, 'COALESCE(l.last_released_at, l.created_at) DESC, l.id DESC');
  r.items = r.items.map((l) => present(l, user, settings));
  return r;
}

/** 公海原始台账（含已认领）：仅总监 / 管理员 */
function listLedger(db, user, f = {}) {
  if (!isManager(user)) throw new AppError(403, '无权查看公海台账');
  const settings = getSettings(db);
  const where = [];
  const params = [];
  buildFilters(f, where, params);
  if (f.status === 'pool' || f.status === 'owned') { where.push('l.status = ?'); params.push(f.status); }
  if (f.owner_id) { where.push('l.owner_id = ?'); params.push(Number(f.owner_id)); }
  buildExpireFilter(f, where, params, settings);
  const r = paginate(db, LEAD_SELECT, where, params, f,
    f.sort === 'expire' ? 'l.expire_at IS NULL, l.expire_at ASC' : 'l.updated_at DESC, l.id DESC');
  r.items = r.items.map((l) => present(l, user, settings));
  return r;
}

/** 我的个人线索池 */
function listMine(db, user, f = {}) {
  const settings = getSettings(db);
  const where = ["l.status = 'owned'", 'l.owner_id = ?'];
  const params = [user.id];
  buildFilters(f, where, params);
  buildExpireFilter(f, where, params, settings);
  const order = f.sort === 'recent' ? 'l.last_effective_at DESC' : 'l.expire_at ASC';
  const r = paginate(db, LEAD_SELECT, where, params, f, order);
  r.items = r.items.map((l) => present(l, user, settings));
  return r;
}

// ---------- 详情 ----------
function loadLead(db, id) {
  const lead = db.get(LEAD_SELECT + ' WHERE l.id = ?', [Number(id)]);
  if (!lead) throw new AppError(404, '线索不存在');
  return lead;
}

/** 普通销售：只能看公海线索或自己名下线索 */
function assertCanView(user, lead) {
  if (isManager(user)) return;
  if (lead.status === 'pool') return;
  if (lead.owner_id === user.id) return;
  throw new AppError(403, '该线索已被其他销售认领，无权查看');
}

function getLead(db, user, id) {
  const settings = getSettings(db);
  const lead = loadLead(db, id);
  assertCanView(user, lead);
  const out = present(lead, user, settings);
  const mine = lead.owner_id === user.id;
  const claims = db.all(`SELECT c.*, u.name AS user_name FROM claims c JOIN users u ON u.id = c.user_id
                          WHERE c.lead_id = ? ORDER BY c.claimed_at DESC`, [lead.id]);
  out.claim_history = claims.map((c) => ({
    user_name: c.user_name, claimed_at: c.claimed_at, ended_at: c.ended_at, end_reason: c.end_reason,
    ...(isManager(user) || mine ? { end_note: c.end_note, user_id: c.user_id } : {}),
  }));

  if (isManager(user) || mine) {
    let fsql = `SELECT f.*, u.name AS user_name FROM followups f JOIN users u ON u.id = f.user_id WHERE f.lead_id = ?`;
    let lsql = 'SELECT * FROM lead_logs WHERE lead_id = ?';
    const fp = [lead.id];
    const lp = [lead.id];
    if (!isManager(user) && !settings.inherit_history) {
      // 不继承历史：只能看本次认领以来的记录
      fsql += ' AND f.created_at >= ?'; fp.push(lead.claimed_at);
      lsql += ' AND created_at >= ?'; lp.push(lead.claimed_at);
    }
    out.followups = db.all(fsql + ' ORDER BY f.follow_at DESC, f.id DESC', fp);
    out.logs = db.all(lsql + ' ORDER BY id DESC', lp).map((l) => ({ ...l, detail: l.detail ? JSON.parse(l.detail) : null }));
  } else {
    out.followups = [];
    out.logs = [];
  }
  return out;
}

// ---------- 校验 ----------
function normalize(db, data, partial) {
  const out = {};
  for (const k of EDITABLE) {
    if (!(k in data)) continue;
    let v = data[k];
    if (v === undefined) continue;
    if (k === 'industry_id') {
      v = v === '' || v == null ? null : Number(v);
      if (v != null && !db.get('SELECT 1 FROM industries WHERE id = ?', [v])) throw new AppError(400, '行业标签不存在');
    } else {
      v = v == null ? null : String(v).trim();
      if (v === '') v = null;
      if (v && v.length > (k === 'remark' ? 2000 : 200)) throw new AppError(400, `${FIELD_LABELS[k]}过长`);
      if (v && ['source', 'intent_level', 'stage'].includes(k)) {
        const type = k === 'intent_level' ? 'intent' : k;
        if (!db.get('SELECT 1 FROM options WHERE type = ? AND value = ?', [type, v])) throw new AppError(400, `${FIELD_LABELS[k]}选项无效：${v}`);
      }
    }
    out[k] = v;
  }
  if (!partial || 'company_name' in out) if (!out.company_name) throw new AppError(400, '公司全称为必填');
  if (!partial || 'contact_name' in out) if (!out.contact_name) throw new AppError(400, '对接人姓名为必填');
  return out;
}

function findDuplicate(db, company, excludeId) {
  return db.get(`SELECT id, status, owner_id FROM leads WHERE company_name = ? ${excludeId ? 'AND id != ?' : ''} LIMIT 1`,
    excludeId ? [company, excludeId] : [company]);
}

// ---------- 认领核心 ----------
function doClaim(db, user, leadId, via) {
  const settings = getSettings(db);
  const now = clock.now();
  if (settings.sales_max_leads > 0) {
    const c = db.get("SELECT COUNT(*) AS c FROM leads WHERE status = 'owned' AND owner_id = ?", [user.id]).c;
    if (c >= settings.sales_max_leads) throw new AppError(409, `个人池已达上限 ${settings.sales_max_leads} 条，请先跟进或退回部分线索`);
  }
  const expire = now + settings.recycle_days * DAY;
  // 原子条件更新：只有 status='pool' 时才能成功，杜绝一条线索被多人同时认领
  const r = db.run(`UPDATE leads SET status = 'owned', owner_id = ?, claimed_at = ?, last_effective_at = ?, expire_at = ?,
                    claim_count = claim_count + 1, updated_at = ? WHERE id = ? AND status = 'pool'`,
    [user.id, now, now, expire, now, leadId]);
  if (r.changes !== 1) throw new AppError(409, '手慢了，该线索已被其他人认领');
  const claim = db.run('INSERT INTO claims (lead_id, user_id, claimed_at) VALUES (?,?,?)', [leadId, user.id, now]);
  log(db, { lead_id: leadId, action: 'claim', actor: user, owner_id: user.id, detail: { expire_at: expire, via } });
  return claim.lastInsertRowid;
}

function claim(db, user, id) {
  return db.tx(() => {
    const lead = loadLead(db, id);
    if (lead.status !== 'pool') throw new AppError(409, '手慢了，该线索已被其他人认领');
    doClaim(db, user, lead.id, 'pool');
    return getLead(db, user, lead.id);
  });
}

/** 结束当前归属，线索回到公海（退回 / 自动回流 / 强制回收共用） */
function returnToPool(db, lead, type, actor, note) {
  const now = clock.now();
  const r = db.run(`UPDATE leads SET status = 'pool', owner_id = NULL, claimed_at = NULL, last_effective_at = NULL,
                    expire_at = NULL, last_owner_id = ?, last_released_at = ?, last_release_type = ?, updated_at = ?
                    WHERE id = ? AND status = 'owned' AND owner_id = ?`,
    [lead.owner_id, now, type, now, lead.id, lead.owner_id]);
  if (r.changes !== 1) throw new AppError(409, '线索状态已变化，请刷新后重试');
  db.run(`UPDATE claims SET ended_at = ?, end_reason = ?, end_note = ?
          WHERE lead_id = ? AND user_id = ? AND ended_at IS NULL`, [now, type, note || null, lead.id, lead.owner_id]);
  log(db, {
    lead_id: lead.id, action: type, actor, owner_id: lead.owner_id,
    detail: { note: note || null, from_owner: lead.owner_name || null, last_effective_at: lead.last_effective_at },
  });
}

function release(db, user, id, note) {
  note = String(note || '').trim();
  if (!note) throw new AppError(400, '请填写退回原因');
  return db.tx(() => {
    const lead = loadLead(db, id);
    if (lead.status !== 'owned' || lead.owner_id !== user.id) throw new AppError(403, '只能退回自己名下的线索');
    returnToPool(db, lead, 'release', user, note);
    return { ok: true };
  });
}

function forceRecycle(db, user, id, note) {
  if (!isManager(user)) throw new AppError(403, '仅销售总监可强制回收');
  return db.tx(() => {
    const lead = loadLead(db, id);
    if (lead.status !== 'owned') throw new AppError(409, '该线索当前在公海中，无需回收');
    returnToPool(db, lead, 'force_recycle', user, String(note || '').trim() || '总监强制回收');
    return { ok: true };
  });
}

/** 定时任务：回收所有超过回流周期未有效更新的线索 */
function recycleExpired(db) {
  const now = clock.now();
  const due = db.all(LEAD_SELECT + " WHERE l.status = 'owned' AND l.expire_at <= ?", [now]);
  let n = 0;
  for (const lead of due) {
    try {
      db.tx(() => returnToPool(db, lead, 'auto_recycle', null, `超过回流周期未有效更新（上次有效更新：${new Date(lead.last_effective_at).toLocaleString('zh-CN')}）`));
      n++;
    } catch (e) {
      if (e.status !== 409) throw e; // 状态已变化（被同时退回等），忽略
    }
  }
  return n;
}

/** 回流周期变更后，按新周期重算所有私池线索的到期时间 */
function recomputeExpiry(db) {
  const { recycle_days } = getSettings(db);
  db.run("UPDATE leads SET expire_at = last_effective_at + ? WHERE status = 'owned'", [recycle_days * DAY]);
}

/** 有效更新：重置回流倒计时 */
function touchEffective(db, leadId) {
  const { recycle_days } = getSettings(db);
  const now = clock.now();
  db.run('UPDATE leads SET last_effective_at = ?, expire_at = ?, updated_at = ? WHERE id = ?',
    [now, now + recycle_days * DAY, now, leadId]);
}

// ---------- 新增 / 修改 / 跟进 ----------
function createLead(db, user, data, opts = {}) {
  const settings = getSettings(db);
  if (!isManager(user) && !settings.sales_can_create) throw new AppError(403, '当前不允许销售录入线索');
  const v = normalize(db, data, false);
  return db.tx(() => {
    const dup = findDuplicate(db, v.company_name);
    if (dup) {
      const where = dup.status === 'pool' ? '已在公海中，可直接认领'
        : dup.owner_id === user.id ? '已在你的个人池中' : '已被其他销售跟进中';
      throw new AppError(409, `该公司${where}，请勿重复录入`, { duplicate_id: dup.status === 'pool' || dup.owner_id === user.id || isManager(user) ? dup.id : undefined });
    }
    const now = clock.now();
    const cols = Object.keys(v);
    const r = db.run(`INSERT INTO leads (${cols.join(',')}, status, created_by, created_at, updated_at)
                      VALUES (${cols.map(() => '?').join(',')}, 'pool', ?, ?, ?)`,
      [...cols.map((k) => v[k]), user.id, now, now]);
    const id = r.lastInsertRowid;
    log(db, { lead_id: id, action: opts.importBatch ? 'import' : 'create', actor: user, detail: opts.importBatch ? { batch: opts.importBatch } : null });
    if (opts.claim) doClaim(db, user, id, 'create');
    return id;
  });
}

function assertOwner(user, lead) {
  if (lead.status !== 'owned' || lead.owner_id !== user.id) {
    throw new AppError(403, lead.status === 'pool' ? '请先认领该线索再编辑' : '只能编辑自己名下的线索');
  }
}

function updateLead(db, user, id, data, via) {
  return db.tx(() => {
    const lead = loadLead(db, id);
    assertOwner(user, lead);
    const v = normalize(db, data, true);
    const changes = {};
    for (const [k, val] of Object.entries(v)) {
      if ((lead[k] ?? null) !== (val ?? null)) changes[k] = { from: lead[k] ?? null, to: val ?? null };
    }
    if (changes.company_name) {
      const dup = findDuplicate(db, v.company_name, lead.id);
      if (dup) throw new AppError(409, '已存在同名公司线索，请核对公司全称');
    }
    const keys = Object.keys(changes);
    if (!keys.length) return { changed: false, lead: getLead(db, user, lead.id) };
    db.run(`UPDATE leads SET ${keys.map((k) => `${k} = ?`).join(', ')} WHERE id = ?`, [...keys.map((k) => changes[k].to), lead.id]);
    touchEffective(db, lead.id);
    log(db, {
      lead_id: lead.id, action: changes.stage && keys.length === 1 ? 'stage_change' : 'update', actor: user, owner_id: user.id,
      detail: { via: via || 'form', changes: Object.fromEntries(keys.map((k) => [FIELD_LABELS[k], changes[k]])) },
    });
    return { changed: true, lead: getLead(db, user, lead.id) };
  });
}

function addFollowup(db, user, id, data) {
  const content = String(data.content || '').trim();
  if (!content) throw new AppError(400, '请填写跟进内容');
  if (content.length > 5000) throw new AppError(400, '跟进内容过长');
  const now = clock.now();
  const followAt = data.follow_at ? Number(new Date(data.follow_at)) : now;
  const nextAt = data.next_follow_at ? Number(new Date(data.next_follow_at)) : null;
  if (Number.isNaN(followAt) || (nextAt !== null && Number.isNaN(nextAt))) throw new AppError(400, '时间格式不正确');
  return db.tx(() => {
    const lead = loadLead(db, id);
    assertOwner(user, lead);
    let stage = data.stage ? String(data.stage) : null;
    if (stage && !db.get("SELECT 1 FROM options WHERE type = 'stage' AND value = ?", [stage])) throw new AppError(400, '跟进阶段无效');
    const claimRow = db.get('SELECT id FROM claims WHERE lead_id = ? AND user_id = ? AND ended_at IS NULL', [lead.id, user.id]);
    const r = db.run(`INSERT INTO followups (lead_id, user_id, claim_id, content, follow_at, next_follow_at, stage, created_at)
                      VALUES (?,?,?,?,?,?,?,?)`, [lead.id, user.id, claimRow && claimRow.id, content, followAt, nextAt, stage, now]);
    if (stage && stage !== lead.stage) {
      db.run('UPDATE leads SET stage = ? WHERE id = ?', [stage, lead.id]);
      log(db, { lead_id: lead.id, action: 'stage_change', actor: user, owner_id: user.id, detail: { changes: { 跟进阶段: { from: lead.stage, to: stage } } } });
    }
    touchEffective(db, lead.id);
    log(db, { lead_id: lead.id, action: 'followup', actor: user, owner_id: user.id, detail: { followup_id: r.lastInsertRowid, via: data.via || 'form' } });
    return getLead(db, user, lead.id);
  });
}

// ---------- 账号停用：名下线索全部回收 ----------
function recycleAllOf(db, actor, userId, note) {
  const leads = db.all(LEAD_SELECT + " WHERE l.status = 'owned' AND l.owner_id = ?", [userId]);
  db.tx(() => leads.forEach((l) => returnToPool(db, l, 'force_recycle', actor, note)));
  return leads.length;
}

module.exports = {
  AppError, isManager, EDITABLE, FIELD_LABELS, LEAD_SELECT,
  getSettings, log, present, listPool, listLedger, listMine, getLead,
  createLead, updateLead, addFollowup, claim, release, forceRecycle,
  recycleExpired, recomputeExpiry, recycleAllOf, findDuplicate, normalize,
};

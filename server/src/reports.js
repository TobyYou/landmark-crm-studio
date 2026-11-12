'use strict';
/** 总监看板、导入、导出 */
const clock = require('./clock');
const { DAY } = clock;
const csv = require('./csv');
const L = require('./leads');

const fmt = (t) => (t ? new Date(t).toLocaleString('zh-CN', { hour12: false }) : '');

function weekStart(now) {
  const d = new Date(now);
  const day = (d.getDay() + 6) % 7; // 周一 = 0
  d.setHours(0, 0, 0, 0);
  return d.getTime() - day * DAY;
}

function summary(db, user) {
  if (!L.isManager(user)) throw new L.AppError(403, '无权查看看板');
  const s = L.getSettings(db);
  const now = clock.now();
  const ws = weekStart(now);
  const warnAt = now + s.warn_days * DAY;
  const won = db.all("SELECT value FROM options WHERE type = 'stage' AND is_won = 1").map((r) => r.value);
  const wonIn = won.length ? `l.stage IN (${won.map(() => '?').join(',')})` : '0';

  const totals = db.get(`SELECT
      COUNT(*) AS total,
      SUM(status = 'pool') AS pool,
      SUM(status = 'owned') AS owned,
      SUM(status = 'owned' AND expire_at <= ?) AS expiring,
      SUM(created_at >= ?) AS new_this_week
    FROM leads`, [warnAt, ws]);
  totals.won = db.get(`SELECT COUNT(*) AS c FROM leads l WHERE ${wonIn}`, won).c;

  const byIndustry = db.all(`SELECT COALESCE(i.name, '未分类') AS name,
      SUM(l.status = 'pool') AS pool, SUM(l.status = 'owned') AS owned, COUNT(*) AS total
    FROM leads l LEFT JOIN industries i ON i.id = l.industry_id
    GROUP BY COALESCE(i.name, '未分类') ORDER BY total DESC`);

  const users = db.all("SELECT id, name, role, active FROM users WHERE role IN ('sales','director') ORDER BY role DESC, id");
  const perSales = users.map((u) => {
    const one = (sql, p) => db.get(sql, p).c;
    return {
      user_id: u.id, name: u.name, role: u.role, active: u.active,
      holding: one("SELECT COUNT(*) AS c FROM leads WHERE status = 'owned' AND owner_id = ?", [u.id]),
      claimed_week: one('SELECT COUNT(*) AS c FROM claims WHERE user_id = ? AND claimed_at >= ?', [u.id, ws]),
      released_week: one("SELECT COUNT(*) AS c FROM claims WHERE user_id = ? AND end_reason = 'release' AND ended_at >= ?", [u.id, ws]),
      expiring: one("SELECT COUNT(*) AS c FROM leads WHERE status = 'owned' AND owner_id = ? AND expire_at <= ?", [u.id, warnAt]),
      recycled_total: one("SELECT COUNT(*) AS c FROM claims WHERE user_id = ? AND end_reason IN ('auto_recycle','force_recycle')", [u.id]),
      auto_recycled_total: one("SELECT COUNT(*) AS c FROM claims WHERE user_id = ? AND end_reason = 'auto_recycle'", [u.id]),
      won: one(`SELECT COUNT(*) AS c FROM leads l WHERE l.status = 'owned' AND l.owner_id = ? AND ${wonIn}`, [u.id, ...won]),
      followups_week: one('SELECT COUNT(*) AS c FROM followups WHERE user_id = ? AND created_at >= ?', [u.id, ws]),
      followups_30d: one('SELECT COUNT(*) AS c FROM followups WHERE user_id = ? AND created_at >= ?', [u.id, now - 30 * DAY]),
    };
  });

  const flowWeek = db.get(`SELECT
      SUM(action = 'claim') AS claim, SUM(action = 'release') AS release,
      SUM(action = 'auto_recycle') AS auto_recycle, SUM(action = 'force_recycle') AS force_recycle,
      SUM(action = 'followup') AS followup
    FROM lead_logs WHERE created_at >= ?`, [ws]);

  return { settings: { recycle_days: s.recycle_days, warn_days: s.warn_days }, week_start: ws, totals, byIndustry, perSales, flowWeek };
}

/** 即将回流预警清单 */
function warnings(db, user, f = {}) {
  if (!L.isManager(user)) throw new L.AppError(403, '无权查看');
  return L.listLedger(db, user, { ...f, status: 'owned', expire: f.expire || 'warn', sort: 'expire', size: f.size || 200 });
}

/** 线索流转台账：认领 / 退回 / 回流 / 强制回收 */
function flows(db, user, f = {}) {
  if (!L.isManager(user)) throw new L.AppError(403, '无权查看');
  const where = ["g.action IN ('claim','release','auto_recycle','force_recycle','create','import')"];
  const params = [];
  if (f.lead_id) { where.push('g.lead_id = ?'); params.push(Number(f.lead_id)); }
  if (f.action) { where.push('g.action = ?'); params.push(f.action); }
  if (f.user_id) { where.push('(g.owner_id = ? OR g.actor_id = ?)'); params.push(Number(f.user_id), Number(f.user_id)); }
  const size = Math.min(500, parseInt(f.size, 10) || 100);
  const page = Math.max(1, parseInt(f.page, 10) || 1);
  const sql = `SELECT g.*, l.company_name, u.name AS owner_name FROM lead_logs g
               LEFT JOIN leads l ON l.id = g.lead_id LEFT JOIN users u ON u.id = g.owner_id
               WHERE ${where.join(' AND ')}`;
  const total = db.get(`SELECT COUNT(*) AS c FROM (${sql})`, params).c;
  const items = db.all(sql + ' ORDER BY g.id DESC LIMIT ? OFFSET ?', [...params, size, (page - 1) * size])
    .map((r) => ({ ...r, detail: r.detail ? JSON.parse(r.detail) : null }));
  return { total, page, size, items };
}

// ---------- 导入 ----------
const IMPORT_HEADER = ['公司全称', '对接人姓名', '对接人部门', '对接人岗位', '联系电话', '微信', '办公地址', '所属行业', '线索来源', '意向等级', '备注'];
const IMPORT_KEYS = ['company_name', 'contact_name', 'contact_dept', 'contact_title', 'contact_phone', 'contact_wechat', 'address', 'industry', 'source', 'intent_level', 'remark'];

function importTemplate() {
  return csv.stringify(IMPORT_HEADER, [['示例：XX饮料有限公司', '张三', '市场部', '品牌经理', '13800000000', 'zhangsan01', 'XX市XX区XX路1号', '快消', '线下收集', '中等意向', '']]);
}

function importCsv(db, user, text) {
  if (!L.isManager(user)) throw new L.AppError(403, '仅总监/管理员可批量导入');
  const rows = csv.parse(text);
  if (!rows.length) throw new L.AppError(400, '文件为空');
  const head = rows[0].map((h) => String(h).trim());
  const idx = IMPORT_HEADER.map((h) => head.indexOf(h));
  if (idx[0] < 0 || idx[1] < 0) throw new L.AppError(400, '表头需包含“公司全称”“对接人姓名”，请使用导入模板');
  const industries = db.all('SELECT id, name FROM industries');
  const batch = `B${clock.now()}`;
  const result = { batch, created: 0, skipped: [], errors: [] };
  if (rows.length > 5001) throw new L.AppError(400, '单次最多导入 5000 行');
  // 整批在一个事务中写入，结束后统一落盘（逐行校验失败只跳过该行，不影响其它行）
  db.tx(() => importRows(db, user, rows, idx, industries, batch, result));
  return result;
}

function importRows(db, user, rows, idx, industries, batch, result) {
  for (let r = 1; r < rows.length; r++) {
    const data = {};
    IMPORT_KEYS.forEach((k, i) => { if (idx[i] >= 0) data[k] = String(rows[r][idx[i]] ?? '').trim(); });
    if (data.industry) {
      const ind = industries.find((x) => x.name === data.industry);
      if (!ind) { result.errors.push({ row: r + 1, message: `行业“${data.industry}”不存在` }); continue; }
      data.industry_id = ind.id;
    }
    delete data.industry;
    try {
      L.createLead(db, user, data, { importBatch: batch });
      result.created++;
    } catch (e) {
      if (e.status === 409) result.skipped.push({ row: r + 1, company: data.company_name, message: '重复，已跳过' });
      else if (e.status) result.errors.push({ row: r + 1, message: e.message });
      else throw e;
    }
  }
}

// ---------- 导出（仅总监/管理员） ----------
const STATUS_TEXT = { pool: '公海待认领', owned: '已认领' };
const ACTION_TEXT = { release: '主动退回', auto_recycle: '超时自动回流', force_recycle: '总监强制回收' };

function exportLeads(db, user, scope) {
  if (!L.isManager(user)) throw new L.AppError(403, '无权导出');
  const s = L.getSettings(db);
  const now = clock.now();
  let where = '1';
  const params = [];
  if (scope === 'pool') where = "l.status = 'pool'";
  else if (scope === 'owned') where = "l.status = 'owned'";
  else if (scope === 'expiring') { where = "l.status = 'owned' AND l.expire_at <= ?"; params.push(now + s.warn_days * DAY); }
  else if (scope === 'recycled') where = "l.last_release_type IN ('auto_recycle','force_recycle')";
  else if (scope === 'won') where = "l.stage IN (SELECT value FROM options WHERE type = 'stage' AND is_won = 1)";
  const rows = db.all(`${L.LEAD_SELECT} WHERE ${where} ORDER BY l.id`, params);
  L.log(db, { action: 'export', actor: user, detail: { scope: scope || 'all', count: rows.length } });
  return csv.stringify(
    ['ID', '公司全称', '对接人', '部门', '岗位', '电话', '微信', '地址', '行业', '来源', '意向等级', '跟进阶段', '状态', '当前认领人', '认领时间', '上次有效更新', '到期回流时间', '累计认领次数', '上一任认领人', '最近回流方式', '最近回流时间', '录入时间'],
    rows.map((l) => [l.id, l.company_name, l.contact_name, l.contact_dept, l.contact_title, l.contact_phone, l.contact_wechat, l.address,
      l.industry_name, l.source, l.intent_level, l.stage, STATUS_TEXT[l.status], l.owner_name, fmt(l.claimed_at), fmt(l.last_effective_at),
      fmt(l.expire_at), l.claim_count, l.last_owner_name, ACTION_TEXT[l.last_release_type] || '', fmt(l.last_released_at), fmt(l.created_at)]));
}

function exportSalesStats(db, user) {
  const d = summary(db, user);
  L.log(db, { action: 'export', actor: user, detail: { scope: 'sales_stats' } });
  return csv.stringify(
    ['销售', '角色', '持有线索', '本周认领', '本周退回', '即将回流', '累计被回收', '其中超时回流', '已成交', '本周跟进次数', '近30天跟进次数'],
    d.perSales.map((p) => [p.name, p.role === 'director' ? '销售总监' : '销售', p.holding, p.claimed_week, p.released_week, p.expiring,
      p.recycled_total, p.auto_recycled_total, p.won, p.followups_week, p.followups_30d]));
}

module.exports = { summary, warnings, flows, importTemplate, importCsv, exportLeads, exportSalesStats };

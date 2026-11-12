'use strict';
/** 初始化默认配置（幂等）：行业标签、下拉选项、系统参数、超级管理员 */
const crypto = require('crypto');
const bcrypt = require('bcryptjs');
const clock = require('./clock');

const DEFAULT_INDUSTRIES = ['快消', '互联网/网络', '汽车', '金融', '保险'];
const DEFAULT_OPTIONS = {
  stage: ['初次联系', '待报价', '方案沟通', '已成交', '暂无意向'],
  intent: ['高意向', '中等意向', '低意向'],
  source: ['渠道A', '渠道B', '线下收集', '线上'],
};
const DEFAULT_SETTINGS = {
  recycle_days: '30',          // 自动回流周期（天）
  warn_days: '7',              // 到期预警阈值（天）
  pool_mask_phone: '1',        // 公海中对普通销售隐藏完整联系方式
  inherit_history: '1',        // 重新认领的人可查看该线索的历史跟进记录
  sales_can_create: '1',       // 普通销售可在前端录入新线索
  sales_max_leads: '0',        // 个人池上限，0 = 不限
};

function seedDefaults(db) {
  const now = clock.now();
  db.tx(() => {
    if (!db.get('SELECT 1 FROM industries LIMIT 1')) {
      DEFAULT_INDUSTRIES.forEach((name, i) =>
        db.run('INSERT INTO industries (name, sort, created_at) VALUES (?,?,?)', [name, i, now]));
    }
    for (const [type, values] of Object.entries(DEFAULT_OPTIONS)) {
      if (!db.get('SELECT 1 FROM options WHERE type = ? LIMIT 1', [type])) {
        values.forEach((v, i) =>
          db.run('INSERT INTO options (type, value, sort, is_won, created_at) VALUES (?,?,?,?,?)',
            [type, v, i, type === 'stage' && v === '已成交' ? 1 : 0, now]));
      }
    }
    for (const [k, v] of Object.entries(DEFAULT_SETTINGS)) {
      db.run('INSERT OR IGNORE INTO settings (key, value) VALUES (?,?)', [k, v]);
    }
  });
}

/** 若系统内没有任何超级管理员，则创建 admin 账号，返回初始密码（仅首次） */
function ensureAdmin(db, presetPassword) {
  if (db.get("SELECT 1 FROM users WHERE role = 'admin' LIMIT 1")) return null;
  const password = presetPassword || crypto.randomBytes(6).toString('base64url');
  const now = clock.now();
  db.run('INSERT INTO users (username, password_hash, name, role, created_at, updated_at) VALUES (?,?,?,?,?,?)',
    ['admin', bcrypt.hashSync(password, 10), '超级管理员', 'admin', now, now]);
  return password;
}

module.exports = { seedDefaults, ensureAdmin, DEFAULT_SETTINGS };

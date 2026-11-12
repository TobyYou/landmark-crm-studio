'use strict';
/**
 * 地标传媒 · 线索公海管理系统 — 服务入口
 * 环境变量见 .env.example
 */
process.env.TZ = process.env.TZ || 'Asia/Shanghai';
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');

// 读取同目录 .env（不引入额外依赖）
const envFile = path.join(__dirname, '.env');
if (fs.existsSync(envFile)) {
  for (const line of fs.readFileSync(envFile, 'utf8').split(/\r?\n/)) {
    const m = line.match(/^\s*([A-Z0-9_]+)\s*=\s*(.*)\s*$/);
    const val = m ? m[2].replace(/^["']|["']$/g, '') : '';
    if (m && val !== '' && process.env[m[1]] === undefined) process.env[m[1]] = val; // 空值视为未配置
  }
}

const { Database } = require('./src/db');
const { seedDefaults, ensureAdmin } = require('./src/seed');
const { createApp } = require('./src/app');
const L = require('./src/leads');

const DATA_DIR = path.resolve(__dirname, process.env.DATA_DIR || './data');
const PORT = Number(process.env.PORT || 3000);
const SCAN_MINUTES = Number(process.env.RECYCLE_SCAN_MINUTES || 10);

function loadSecret() {
  if (process.env.JWT_SECRET) return process.env.JWT_SECRET;
  const f = path.join(DATA_DIR, '.jwt_secret');
  if (!fs.existsSync(f)) fs.writeFileSync(f, crypto.randomBytes(48).toString('hex'), { mode: 0o600 });
  return fs.readFileSync(f, 'utf8').trim();
}

(async () => {
  fs.mkdirSync(DATA_DIR, { recursive: true });
  const db = await Database.open(path.join(DATA_DIR, 'leads.sqlite'));
  seedDefaults(db);
  const pwd = ensureAdmin(db, process.env.ADMIN_INITIAL_PASSWORD);
  if (pwd) {
    const f = path.join(DATA_DIR, 'INITIAL_ADMIN_PASSWORD.txt');
    fs.writeFileSync(f, `超级管理员账号：admin\n初始密码：${pwd}\n请登录后立即修改密码，并删除本文件。\n`, { mode: 0o600 });
    console.log(`[init] 已创建超级管理员 admin，初始密码已写入 ${f}`);
  }

  // 自动回流扫描：启动时执行一次，之后每 N 分钟执行
  const scan = () => {
    try {
      const n = L.recycleExpired(db);
      if (n) console.log(`[recycle] ${new Date().toLocaleString('zh-CN')} 自动回流 ${n} 条线索`);
    } catch (e) { console.error('[recycle] 扫描失败', e); }
  };
  scan();
  setInterval(scan, SCAN_MINUTES * 60 * 1000).unref();

  // 每日备份，保留 30 份
  const backup = () => {
    try {
      const dir = path.join(DATA_DIR, 'backups');
      db.backup(path.join(dir, `leads-${new Date().toISOString().slice(0, 10)}.sqlite`));
      const files = fs.readdirSync(dir).filter((f) => f.endsWith('.sqlite')).sort();
      files.slice(0, Math.max(0, files.length - 30)).forEach((f) => fs.unlinkSync(path.join(dir, f)));
    } catch (e) { console.error('[backup] 失败', e); }
  };
  backup();
  setInterval(backup, 24 * 3600 * 1000).unref();

  const app = createApp(db, { jwtSecret: loadSecret(), webRoot: path.join(__dirname, '..', 'web') });
  const server = app.listen(PORT, () => {
    console.log(`地标传媒线索公海系统已启动： http://localhost:${PORT}`);
    console.log(`AI 识别引擎：${process.env.ANTHROPIC_API_KEY ? 'Claude（失败自动降级为本地规则）' : '本地规则（未配置 ANTHROPIC_API_KEY）'}`);
  });
  const shutdown = () => { try { db.persist(); } finally { server.close(() => process.exit(0)); } };
  process.on('SIGINT', shutdown);
  process.on('SIGTERM', shutdown);
})();

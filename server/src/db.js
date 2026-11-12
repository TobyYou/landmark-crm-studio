'use strict';
/**
 * 数据层：基于 sql.js（纯 JS / WASM 版 SQLite，无需本地编译）。
 * - 内存中运行，每次写事务提交后原子落盘（写临时文件再 rename）。
 * - 单进程部署；数据量增长后可平滑迁移到 MySQL / PostgreSQL（SQL 均为标准写法）。
 */
const fs = require('fs');
const path = require('path');
const initSqlJs = require('sql.js');

const SCHEMA = `
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  username      TEXT NOT NULL UNIQUE,
  password_hash TEXT NOT NULL,
  name          TEXT NOT NULL,
  role          TEXT NOT NULL CHECK (role IN ('sales','director','admin')),
  phone         TEXT,
  active        INTEGER NOT NULL DEFAULT 1,
  token_version INTEGER NOT NULL DEFAULT 0,
  created_at    INTEGER NOT NULL,
  updated_at    INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS industries (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  name       TEXT NOT NULL UNIQUE,
  sort       INTEGER NOT NULL DEFAULT 0,
  active     INTEGER NOT NULL DEFAULT 1,
  created_at INTEGER NOT NULL
);

-- 下拉选项：stage 跟进阶段 / intent 意向等级 / source 线索来源
CREATE TABLE IF NOT EXISTS options (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  type       TEXT NOT NULL CHECK (type IN ('stage','intent','source')),
  value      TEXT NOT NULL,
  sort       INTEGER NOT NULL DEFAULT 0,
  is_won     INTEGER NOT NULL DEFAULT 0,   -- 仅 stage：是否代表“已成交”
  active     INTEGER NOT NULL DEFAULT 1,
  created_at INTEGER NOT NULL,
  UNIQUE (type, value)
);

CREATE TABLE IF NOT EXISTS settings (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS leads (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  company_name      TEXT NOT NULL,
  contact_name      TEXT NOT NULL,
  contact_dept      TEXT,
  contact_title     TEXT,
  contact_phone     TEXT,
  contact_wechat    TEXT,
  address           TEXT,
  industry_id       INTEGER REFERENCES industries(id),
  source            TEXT,
  intent_level      TEXT,
  stage             TEXT,
  remark            TEXT,
  status            TEXT NOT NULL DEFAULT 'pool' CHECK (status IN ('pool','owned')),
  owner_id          INTEGER REFERENCES users(id),
  claimed_at        INTEGER,
  last_effective_at INTEGER,
  expire_at         INTEGER,
  claim_count       INTEGER NOT NULL DEFAULT 0,
  last_owner_id     INTEGER REFERENCES users(id),
  last_released_at  INTEGER,
  last_release_type TEXT,               -- release / auto_recycle / force_recycle
  created_by        INTEGER REFERENCES users(id),
  created_at        INTEGER NOT NULL,
  updated_at        INTEGER NOT NULL,
  CHECK ((status = 'pool' AND owner_id IS NULL) OR (status = 'owned' AND owner_id IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS idx_leads_status   ON leads(status);
CREATE INDEX IF NOT EXISTS idx_leads_owner    ON leads(owner_id);
CREATE INDEX IF NOT EXISTS idx_leads_expire   ON leads(expire_at);
CREATE INDEX IF NOT EXISTS idx_leads_industry ON leads(industry_id);
CREATE INDEX IF NOT EXISTS idx_leads_company  ON leads(company_name);

-- 认领历史：每次认领一行，结束（退回/回流/强制回收）时写 ended_at
CREATE TABLE IF NOT EXISTS claims (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  lead_id     INTEGER NOT NULL REFERENCES leads(id),
  user_id     INTEGER NOT NULL REFERENCES users(id),
  claimed_at  INTEGER NOT NULL,
  ended_at    INTEGER,
  end_reason  TEXT,                     -- release / auto_recycle / force_recycle
  end_note    TEXT
);
CREATE INDEX IF NOT EXISTS idx_claims_lead ON claims(lead_id);
CREATE INDEX IF NOT EXISTS idx_claims_user ON claims(user_id);

CREATE TABLE IF NOT EXISTS followups (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  lead_id        INTEGER NOT NULL REFERENCES leads(id),
  user_id        INTEGER NOT NULL REFERENCES users(id),
  claim_id       INTEGER REFERENCES claims(id),
  content        TEXT NOT NULL,
  follow_at      INTEGER NOT NULL,
  next_follow_at INTEGER,
  stage          TEXT,
  created_at     INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_followups_lead ON followups(lead_id);
CREATE INDEX IF NOT EXISTS idx_followups_user ON followups(user_id, created_at);

-- 操作日志：只增不改不删
CREATE TABLE IF NOT EXISTS lead_logs (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  lead_id    INTEGER REFERENCES leads(id),
  action     TEXT NOT NULL,
  actor_id   INTEGER REFERENCES users(id),
  actor_name TEXT NOT NULL,
  owner_id   INTEGER,
  detail     TEXT,
  created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_logs_lead   ON lead_logs(lead_id);
CREATE INDEX IF NOT EXISTS idx_logs_action ON lead_logs(action, created_at);

-- 硬约束：日志、跟进记录、线索台账、认领历史不可删除；日志不可修改
CREATE TRIGGER IF NOT EXISTS trg_logs_no_update BEFORE UPDATE ON lead_logs
BEGIN SELECT RAISE(ABORT, '操作日志不可修改'); END;
CREATE TRIGGER IF NOT EXISTS trg_logs_no_delete BEFORE DELETE ON lead_logs
BEGIN SELECT RAISE(ABORT, '操作日志不可删除'); END;
CREATE TRIGGER IF NOT EXISTS trg_followups_no_delete BEFORE DELETE ON followups
BEGIN SELECT RAISE(ABORT, '跟进记录不可删除'); END;
CREATE TRIGGER IF NOT EXISTS trg_followups_no_update BEFORE UPDATE ON followups
BEGIN SELECT RAISE(ABORT, '跟进记录不可修改'); END;
CREATE TRIGGER IF NOT EXISTS trg_leads_no_delete BEFORE DELETE ON leads
BEGIN SELECT RAISE(ABORT, '线索台账不可删除'); END;
CREATE TRIGGER IF NOT EXISTS trg_claims_no_delete BEFORE DELETE ON claims
BEGIN SELECT RAISE(ABORT, '认领历史不可删除'); END;
`;

class Database {
  constructor(SQL, file) {
    this.file = file;
    this.inTx = 0;
    if (file && fs.existsSync(file)) {
      this.db = new SQL.Database(fs.readFileSync(file));
    } else {
      this.db = new SQL.Database();
    }
    this.db.exec(SCHEMA);
    this.persist();
  }

  static async open(file) {
    const SQL = await initSqlJs();
    if (file) fs.mkdirSync(path.dirname(file), { recursive: true });
    return new Database(SQL, file);
  }

  _stmt(sql, params) {
    const stmt = this.db.prepare(sql);
    if (params && params.length) stmt.bind(params.map((v) => (v === undefined ? null : typeof v === 'boolean' ? Number(v) : v)));
    return stmt;
  }

  get(sql, params = []) {
    const stmt = this._stmt(sql, params);
    try {
      return stmt.step() ? stmt.getAsObject() : undefined;
    } finally {
      stmt.free();
    }
  }

  all(sql, params = []) {
    const stmt = this._stmt(sql, params);
    const rows = [];
    try {
      while (stmt.step()) rows.push(stmt.getAsObject());
      return rows;
    } finally {
      stmt.free();
    }
  }

  run(sql, params = []) {
    const stmt = this._stmt(sql, params);
    try {
      stmt.step();
    } finally {
      stmt.free();
    }
    const changes = this.db.getRowsModified();
    const lastInsertRowid = this.db.exec('SELECT last_insert_rowid()')[0].values[0][0];
    if (!this.inTx) this.persist();
    return { changes, lastInsertRowid };
  }

  /** 同步事务：fn 内抛错则整体回滚 */
  tx(fn) {
    if (this.inTx) return fn();
    this.db.exec('BEGIN IMMEDIATE');
    this.inTx++;
    try {
      const r = fn();
      this.db.exec('COMMIT');
      this.inTx--;
      this.persist();
      return r;
    } catch (e) {
      this.inTx--;
      try { this.db.exec('ROLLBACK'); } catch (_) { /* ignore */ }
      throw e;
    }
  }

  persist() {
    if (!this.file) return;
    const tmp = this.file + '.tmp';
    fs.writeFileSync(tmp, Buffer.from(this.db.export()));
    fs.renameSync(tmp, this.file);
    // sql.js 的 export() 会重开数据库并重置 PRAGMA，需重新开启外键约束
    this.db.exec('PRAGMA foreign_keys = ON;');
  }

  backup(target) {
    fs.mkdirSync(path.dirname(target), { recursive: true });
    fs.writeFileSync(target, Buffer.from(this.db.export()));
    this.db.exec('PRAGMA foreign_keys = ON;');
  }
}

module.exports = { Database };

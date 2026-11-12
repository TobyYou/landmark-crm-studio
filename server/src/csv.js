'use strict';
/** 简易 CSV 读写（支持引号、逗号、换行转义；导出带 BOM，Excel 打开不乱码） */

function parse(text) {
  text = String(text || '').replace(/^﻿/, '');
  const rows = [];
  let row = [];
  let cell = '';
  let q = false;
  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    if (q) {
      if (c === '"') {
        if (text[i + 1] === '"') { cell += '"'; i++; } else q = false;
      } else cell += c;
    } else if (c === '"') q = true;
    else if (c === ',' || c === '\t') { row.push(cell); cell = ''; }
    else if (c === '\n' || c === '\r') {
      if (c === '\r' && text[i + 1] === '\n') i++;
      row.push(cell); rows.push(row); row = []; cell = '';
    } else cell += c;
  }
  if (cell !== '' || row.length) { row.push(cell); rows.push(row); }
  return rows.filter((r) => r.some((x) => String(x).trim() !== ''));
}

function esc(v) {
  if (v == null) return '';
  let s = String(v);
  // 防 CSV 公式注入
  if (/^[=+\-@]/.test(s)) s = "'" + s;
  return /[",\n\r]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
}

function stringify(header, rows) {
  return '﻿' + [header, ...rows].map((r) => r.map(esc).join(',')).join('\r\n');
}

module.exports = { parse, stringify };

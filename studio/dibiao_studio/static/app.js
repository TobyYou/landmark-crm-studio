/* 地标传媒 · 上刊素材工作台（原生 JS，无构建步骤） */
'use strict';

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const api = async (url, opt = {}) => {
  const r = await fetch(url, { headers: opt.body && !(opt.body instanceof FormData) ? { 'Content-Type': 'application/json' } : {}, ...opt });
  const t = await r.text();
  let d; try { d = JSON.parse(t); } catch { d = t; }
  if (!r.ok) throw new Error((d && d.detail) || t || r.statusText);
  return d;
};
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

// ------------------------------------------------------------------ 参数定义（对应系统可调参数）
const PARAMS = [
  ['精修增强', [
    ['enhance.strength', '精修强度', 'range', 0, 1, 0.05, 0.6],
    ['enhance.tone_mode', '色调模式', 'select', [['natural', '自然'], ['cinematic', '电影感'], ['night_boost', '夜景增强'], ['commercial', '商圈大片']], 'cinematic'],
    ['enhance.scene', '场景', 'select', [['auto', '自动识别'], ['day', '白天'], ['dusk', '黄昏'], ['night', '夜景']], 'auto'],
    ['enhance.denoise', '去噪强度', 'range', 0, 1, 0.05, 0.6],
    ['enhance.sharpen', '锐化强度', 'range', 0, 1, 0.05, 0.45],
    ['enhance.screen_boost', '大屏增强强度', 'range', 0, 1, 0.05, 0.5],
    ['enhance.atmosphere', '大片感氛围', 'range', 0, 1, 0.05, 0.5],
    ['enhance.color_temp', '色温偏移', 'range', -1, 1, 0.05, 0],
    ['enhance.grain', '胶片颗粒', 'range', 0, 1, 0.05, 0.35],
  ]],
  ['人流车流', [
    ['crowd.level', '增强等级', 'select', [['off', '关闭'], ['light', '轻微'], ['medium', '中等'], ['strong', '明显']], 'medium'],
    ['crowd.trails', '车流光轨', 'select', [['auto', '自动(夜景/黄昏)'], ['on', '开'], ['off', '关']], 'auto'],
    ['crowd.sprites', '人车素材', 'select', [['auto', '自动(有素材时)'], ['on', '开'], ['off', '关']], 'auto'],
    ['crowd.light_angle', '光源方向(°)', 'range', 0, 360, 5, 135],
    ['crowd.shadow', '阴影强度', 'range', 0, 1, 0.05, 0.5],
  ]],
  ['画面替换与融合', [
    ['fusion.fit', '画面适配', 'select', [['cover', '等比铺满(裁切)'], ['contain', '等比完整(留黑)'], ['stretch', '拉伸(素材已按屏比例制作)']], 'cover'],
    ['fusion.brightness_match', '亮度匹配', 'range', 0, 1, 0.05, 0.8],
    ['fusion.glow', '发光强度', 'range', 0, 1, 0.05, 0.5],
    ['fusion.reflection', '反光强度', 'range', 0, 1, 0.05, 0.35],
    ['fusion.wb_match', '色温匹配', 'range', 0, 1, 0.05, 0.6],
    ['fusion.color_temp', '画面色温偏移', 'range', -1, 1, 0.05, 0],
    ['fusion.edge_feather', '边缘融合强度', 'range', 0, 2, 0.05, 0.5],
    ['fusion.edge_expand', '边缘外扩(px)', 'range', 0, 4, 0.25, 1],
    ['fusion.face_shading', '各面明暗匹配', 'range', 0, 1, 0.05, 0.8],
    ['fusion.noise_match', '噪点匹配', 'range', 0, 1.5, 0.05, 1],
    ['fusion.softness', '镜头柔化', 'range', 0, 1.5, 0.05, 0.5],
    ['fusion.led_grid', 'LED 像素格', 'range', 0, 1, 0.05, 0],
    ['fusion.lens_k1', '镜头畸变 k1', 'range', -0.3, 0.3, 0.005, 0],
  ]],
  ['视频', [
    ['video.smooth', '轨迹平滑', 'range', 0, 3, 0.25, 0],
    ['video.loop_ad', '客户视频循环', 'check', true],
    ['video.keep_audio', '保留原声', 'check', true],
    ['video.crf', '视频质量 CRF', 'range', 14, 28, 1, 18],
  ]],
  ['输出', [
    ['output.max_side', '输出分辨率(长边)', 'select', [['0', '原尺寸'], ['1920', '1920'], ['2560', '2560'], ['3840', '3840 (4K)'], ['5120', '5120'], ['7680', '7680 (8K)']], '0'],
    ['output.format', '图片格式', 'select', [['jpg', 'JPG'], ['png', 'PNG']], 'jpg'],
    ['output.watermark_text', '水印文字', 'text', '地标传媒 · 样片'],
  ]],
];

// ------------------------------------------------------------------ 状态
const S = {
  base: null, ad: null, img: null, W: 0, H: 0,
  type: 'flat', tool: 'screen',
  kfs: { 0: { top: [], bottom: [] } }, cur: 0, frame: 0, phase: 'top',
  occluders: [], occDraft: [],
  lanes: [], laneDraft: null,
  view: { s: 1, ox: 0, oy: 0 },
  geo: null, drag: null, pan: null, space: false,
  params: {}, tplId: '', parentId: null,
};

const cv = $('#cv');
const ctx = cv.getContext('2d');

// ------------------------------------------------------------------ 参数面板
function buildParams() {
  const root = $('#params');
  root.innerHTML = '';
  for (const [group, items] of PARAMS) {
    const fs = document.createElement('fieldset');
    fs.innerHTML = `<legend>${group}</legend>`;
    for (const it of items) {
      const [key, label, type] = it;
      const row = document.createElement('div');
      row.className = 'row';
      let def;
      if (type === 'range') {
        const [, , , min, max, step, d] = it; def = d;
        row.innerHTML = `<label>${label}</label><input type="range" min="${min}" max="${max}" step="${step}"><span class="val"></span>`;
        const inp = $('input', row), val = $('.val', row);
        inp.oninput = () => { S.params[key] = parseFloat(inp.value); val.textContent = inp.value; };
        row.set = (v) => { inp.value = v; val.textContent = v; S.params[key] = parseFloat(v); };
      } else if (type === 'select') {
        const [, , , opts, d] = it; def = d;
        row.innerHTML = `<label>${label}</label><select>${opts.map(([v, t]) => `<option value="${v}">${t}</option>`).join('')}</select>`;
        const sel = $('select', row);
        sel.onchange = () => { S.params[key] = sel.value; };
        row.set = (v) => { sel.value = String(v); S.params[key] = sel.value; };
      } else if (type === 'check') {
        const [, , , d] = it; def = d;
        row.innerHTML = `<label>${label}</label><input type="checkbox">`;
        const cb = $('input', row);
        cb.onchange = () => { S.params[key] = cb.checked; };
        row.set = (v) => { cb.checked = !!v; S.params[key] = !!v; };
      } else {
        const [, , , d] = it; def = d;
        row.innerHTML = `<label>${label}</label><input type="text">`;
        const t = $('input', row);
        t.oninput = () => { S.params[key] = t.value; };
        row.set = (v) => { t.value = v; S.params[key] = v; };
      }
      row.dataset.key = key;
      row.set(def);
      fs.appendChild(row);
    }
    root.appendChild(fs);
  }
}
function setParam(key, v) { const r = $(`#params .row[data-key="${key}"]`); if (r && v !== undefined && v !== null) r.set(v); }
function paramsObject() {
  const o = { enhance: {}, crowd: {}, fusion: {}, video: {}, output: {} };
  for (const [k, v] of Object.entries(S.params)) {
    const [g, f] = k.split('.');
    o[g][f] = k === 'output.max_side' ? parseInt(v, 10) : v;
  }
  return o;
}

// ------------------------------------------------------------------ 上传
function setupDrop(el, which) {
  const inp = $('input', el);
  el.onclick = () => inp.click();
  inp.onchange = () => inp.files[0] && doUpload(inp.files[0], which);
  el.ondragover = (e) => { e.preventDefault(); el.classList.add('over'); };
  el.ondragleave = () => el.classList.remove('over');
  el.ondrop = (e) => { e.preventDefault(); el.classList.remove('over'); e.dataTransfer.files[0] && doUpload(e.dataTransfer.files[0], which); };
}
async function doUpload(file, which) {
  const box = $(`#drop-${which} .meta`);
  box.textContent = `上传中… ${(file.size / 1048576).toFixed(1)} MB`;
  const fd = new FormData();
  fd.append('file', file);
  try {
    const info = await api('/api/uploads', { method: 'POST', body: fd });
    await setUpload(which, info);
  } catch (e) { box.innerHTML = `<span class="err">${esc(e.message)}</span>`; }
}
async function setUpload(which, info) {
  S[which] = info;
  const box = $(`#drop-${which} .meta`);
  const desc = info.kind === 'video' ? `视频 ${info.width}×${info.height} · ${info.fps.toFixed(2)}fps · ${info.frames} 帧` : `图片 ${info.width}×${info.height}`;
  box.innerHTML = `${esc(info.name)}<br>${desc}<img src="/api/uploads/${info.id}/preview?max_side=480">`;
  if (which === 'base') {
    S.W = info.width; S.H = info.height;
    $('#video-ctl').hidden = info.kind !== 'video';
    if (info.kind === 'video') { $('#frame').max = Math.max(0, info.frames - 1); }
    await loadFrame(0);
    fitView();
  }
}
function loadFrame(f) {
  S.frame = f;
  $('#frame-no').textContent = f;
  return new Promise((res) => {
    const im = new Image();
    im.onload = () => { S.img = im; $('#empty').hidden = true; draw(); res(); };
    im.onerror = () => res();
    im.src = `/api/uploads/${S.base.id}/preview?frame=${f}&max_side=2400`;
  });
}

// ------------------------------------------------------------------ 视图
function resize() {
  const r = cv.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  cv.width = Math.round(r.width * dpr); cv.height = Math.round(r.height * dpr);
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  draw();
}
function fitView() {
  if (!S.W) return;
  const r = cv.getBoundingClientRect();
  const s = Math.min(r.width / S.W, r.height / S.H) * 0.94;
  S.view = { s, ox: (r.width - S.W * s) / 2, oy: (r.height - S.H * s) / 2 };
  draw();
}
const toScr = (p) => [p[0] * S.view.s + S.view.ox, p[1] * S.view.s + S.view.oy];
const toImg = (x, y) => [(x - S.view.ox) / S.view.s, (y - S.view.oy) / S.view.s];

// ------------------------------------------------------------------ 关键帧 / 点位
const K = () => S.kfs[S.cur];
function isKeyframe() { return S.base?.kind !== 'video' || S.kfs[S.frame] !== undefined; }
function screenComplete(k = K()) {
  if (!k) return false;
  const n = k.top.length;
  if (n < 2 || n !== k.bottom.length) return false;
  if (S.type === 'flat') return n === 2;
  return n >= 3;
}
function nearestKf(f) {
  const ks = Object.keys(S.kfs).map(Number);
  return ks.reduce((a, b) => (Math.abs(b - f) < Math.abs(a - f) ? b : a), ks[0]);
}
function templateFor(k) {
  const pts = S.kfs[k];
  const pw = parseFloat($('#phys-w').value), ph = parseFloat($('#phys-h').value);
  const seg = $('#seg-w').value.split(/[,，\s]+/).map(parseFloat).filter((x) => x > 0);
  const umode = $('#u-mode').value;
  const t = {
    type: S.type, top: pts.top, bottom: pts.bottom, image_size: [S.W, S.H], u_mode: umode,
    occluders: S.occluders, name: $('#tpl-name').value.trim(),
  };
  if (pw > 0 && ph > 0) t.physical_size = [pw, ph];
  if (seg.length === pts.top.length - 1) t.segment_widths = seg;
  else if (umode === 'physical') t.u_mode = 'auto';
  const f = parseFloat($('#focal').value);
  if (f > 0) t.focal_px = f;
  const ta = parseFloat($('#turn-angle').value);
  if (S.type !== 'flat' && ta >= 5 && ta <= 175) t.turn_angle = ta;
  if (S.tplId) t.id = S.tplId;
  return t;
}

function updateHint() {
  const k = K();
  let h = '';
  if (!S.base) h = '先上传原始素材';
  else if (S.tool === 'screen') {
    if (!isKeyframe()) h = '当前帧不是关键帧（虚线为最近关键帧点位）。需要修正时点“在当前帧加关键帧”。';
    else if (S.type === 'flat') {
      const order = ['左上角', '右上角', '左下角', '右下角'];
      const n = k.top.length + k.bottom.length;
      h = n < 4 ? `点击屏幕 ${order[n]}（${n + 1}/4）` : '四角已标定，可拖动微调；滚轮放大看边缘';
    } else if (S.phase === 'top') {
      h = `沿屏幕【上边缘】从左到右点击：${S.type === 'folded' ? '左端 → 每个折角 → 右端' : '建议 7–11 个点，尽量落在 LED 箱体竖缝上'}（已 ${k.top.length} 点）。完成后点“下一步”或按回车。`;
    } else if (S.phase === 'bottom') {
      h = `沿【下边缘】从左到右，与上边缘逐列对应（${k.bottom.length}/${k.top.length}）`;
    } else h = '标定完成，可拖动点位微调；“加一列点”在最宽的一段中间插入一列';
  } else if (S.tool === 'occluder') {
    h = '圈出挡在屏幕前的物体（路灯杆、树、招牌），单击加点，回车或点回起点闭合。替换时保留原图。';
  } else {
    const d = S.laneDraft;
    const nm = S.tool === 'lane-road' ? '车道' : '人行道';
    if (!d) h = `沿${nm}一侧边线点击（远→近或近→远均可），回车后再画另一侧边线`;
    else h = d.phase === 'left' ? `${nm}第一条边线（${d.left.length} 点），回车进入另一侧` : `${nm}另一侧边线（${d.right.length} 点），回车完成`;
  }
  $('#hint').textContent = h;
  $('#btn-next-edge').hidden = !(S.tool === 'screen' && S.type !== 'flat' && S.phase === 'top');
  $('#btn-insert').hidden = S.type === 'flat';
}

function addScreenPoint(p) {
  if (!isKeyframe()) return;
  const k = K();
  if (S.type === 'flat') {
    if (k.top.length < 2) k.top.push(p);
    else if (k.bottom.length < 2) k.bottom.push(p);
    S.phase = k.bottom.length === 2 ? 'done' : (k.top.length === 2 ? 'bottom' : 'top');
  } else if (S.phase === 'top') k.top.push(p);
  else if (S.phase === 'bottom') {
    k.bottom.push(p);
    if (k.bottom.length === k.top.length) S.phase = 'done';
  } else return;
  changed();
}
function nextEdge() {
  const k = K();
  if (S.tool === 'screen' && S.phase === 'top' && k.top.length >= (S.type === 'flat' ? 2 : 3)) { S.phase = 'bottom'; changed(); }
  else if (S.tool === 'occluder' && S.occDraft.length >= 3) { S.occluders.push(S.occDraft); S.occDraft = []; changed(); }
  else if (S.tool.startsWith('lane') && S.laneDraft) {
    const d = S.laneDraft;
    if (d.phase === 'left' && d.left.length >= 2) d.phase = 'right';
    else if (d.phase === 'right' && d.right.length >= 2) {
      S.lanes.push({ kind: d.kind, left: d.left, right: d.right, width_m: d.kind === 'road' ? 7 : 3, direction: 'both' });
      S.laneDraft = null;
      renderLanes();
    }
    changed();
  }
}
function undo() {
  if (S.tool === 'screen') {
    const k = K();
    if (k.bottom.length) k.bottom.pop(); else k.top.pop();
    S.phase = S.type === 'flat' ? (k.top.length < 2 ? 'top' : 'bottom') : (k.bottom.length ? 'bottom' : 'top');
  } else if (S.tool === 'occluder') {
    if (S.occDraft.length) S.occDraft.pop(); else S.occluders.pop();
  } else if (S.laneDraft) {
    const d = S.laneDraft;
    if (d.phase === 'right' && d.right.length) d.right.pop();
    else if (d.phase === 'right') d.phase = 'left';
    else d.left.pop();
    if (!d.left.length) S.laneDraft = null;
  } else { S.lanes.pop(); renderLanes(); }
  changed();
}
function insertColumn() {
  const k = K();
  if (!screenComplete(k) || S.type === 'flat') return;
  let best = 0, bl = -1;
  for (let i = 0; i < k.top.length - 1; i++) {
    const l = Math.hypot(k.top[i + 1][0] - k.top[i][0], k.top[i + 1][1] - k.top[i][1]);
    if (l > bl) { bl = l; best = i; }
  }
  const mid = (a, b) => [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2];
  k.top.splice(best + 1, 0, mid(k.top[best], k.top[best + 1]));
  k.bottom.splice(best + 1, 0, mid(k.bottom[best], k.bottom[best + 1]));
  changed();
}
function clearAll() {
  if (S.tool === 'screen') { S.kfs[S.cur] = { top: [], bottom: [] }; S.phase = 'top'; S.geo = null; }
  else if (S.tool === 'occluder') { S.occluders = []; S.occDraft = []; }
  else { S.lanes = []; S.laneDraft = null; renderLanes(); }
  changed();
}

let geoTimer = null;
function changed() {
  updateHint();
  renderKfs();
  draw();
  clearTimeout(geoTimer);
  geoTimer = setTimeout(fetchGeo, 180);
}
async function fetchGeo() {
  if (!screenComplete() || !isKeyframe()) { S.geo = null; $('#geo-info').textContent = ''; draw(); return; }
  try {
    const g = await api('/api/geometry/preview', { method: 'POST', body: JSON.stringify({ template: templateFor(S.cur), width: S.W, height: S.H, lens_k1: S.params['fusion.lens_k1'] || 0 }) });
    S.geo = g;
    const ad = S.ad ? (S.ad.width / S.ad.height) : null;
    let txt = `屏幕宽高比 ${g.aspect.toFixed(3)}（${g.strips} 个映射面片） · 焦距估计 ${Math.round(g.focal)}px`;
    if (g.segment_widths.length > 1) txt += `<br>各段相对宽度 ${g.segment_widths.map((x) => x.toFixed(2)).join(' : ')}`;
    if (ad) {
      const diff = Math.abs(ad - g.aspect) / g.aspect;
      txt += `<br>客户素材宽高比 ${ad.toFixed(3)}${diff > 0.02 ? ` — 与屏幕不一致，将按“画面适配”${{ cover: '等比裁切', contain: '等比留黑', stretch: '拉伸' }[S.params['fusion.fit']]}` : ' — 与屏幕一致'}`;
    }
    $('#geo-info').innerHTML = txt;
  } catch (e) { S.geo = null; $('#geo-info').innerHTML = `<span class="err">${esc(e.message)}</span>`; }
  draw();
}

// ------------------------------------------------------------------ 绘制
function poly(pts, close = false) {
  if (!pts.length) return;
  ctx.beginPath();
  pts.forEach((p, i) => { const [x, y] = toScr(p); i ? ctx.lineTo(x, y) : ctx.moveTo(x, y); });
  if (close) ctx.closePath();
}
function dot(p, color, label) {
  const [x, y] = toScr(p);
  ctx.beginPath(); ctx.arc(x, y, 5, 0, Math.PI * 2);
  ctx.fillStyle = color; ctx.fill();
  ctx.lineWidth = 1.5; ctx.strokeStyle = '#000'; ctx.stroke();
  if (label !== undefined) { ctx.fillStyle = '#fff'; ctx.font = '11px sans-serif'; ctx.fillText(label, x + 7, y - 7); }
}
function draw() {
  const r = cv.getBoundingClientRect();
  ctx.clearRect(0, 0, r.width, r.height);
  if (!S.img) return;
  ctx.imageSmoothingQuality = 'high';
  ctx.drawImage(S.img, S.view.ox, S.view.oy, S.W * S.view.s, S.H * S.view.s);

  // 车道 / 人行道
  const lanes = [...S.lanes, ...(S.laneDraft ? [S.laneDraft] : [])];
  for (const l of lanes) {
    const col = l.kind === 'road' ? 'rgba(255,178,63,' : 'rgba(76,209,138,';
    if (l.left.length >= 2 && l.right.length >= 2) {
      poly([...l.left, ...[...l.right].reverse()], true);
      ctx.fillStyle = col + '0.18)'; ctx.fill();
    }
    ctx.lineWidth = 2; ctx.strokeStyle = col + '0.9)';
    poly(l.left); ctx.stroke(); poly(l.right); ctx.stroke();
    l.left.forEach((p) => dot(p, col + '1)')); l.right.forEach((p) => dot(p, col + '1)'));
  }
  // 遮挡物
  for (const o of [...S.occluders, S.occDraft]) {
    if (!o.length) continue;
    poly(o, o !== S.occDraft); ctx.fillStyle = 'rgba(255,100,100,0.25)'; if (o !== S.occDraft) ctx.fill();
    ctx.strokeStyle = '#ff6464'; ctx.lineWidth = 1.5; ctx.stroke();
    o.forEach((p) => dot(p, '#ff6464'));
  }
  // 屏幕
  const keyNow = isKeyframe();
  const k = keyNow ? K() : S.kfs[nearestKf(S.frame)];
  if (!k) return;
  ctx.setLineDash(keyNow ? [] : [6, 5]);
  if (S.geo && keyNow) {
    poly(S.geo.outline, true);
    ctx.fillStyle = 'rgba(63,167,255,0.12)'; ctx.fill();
    ctx.strokeStyle = '#3fa7ff'; ctx.lineWidth = 2; ctx.stroke();
    ctx.lineWidth = 1; ctx.strokeStyle = 'rgba(63,167,255,0.55)';
    for (const c of S.geo.columns) { poly(c); ctx.stroke(); }
  } else {
    ctx.strokeStyle = '#3fa7ff'; ctx.lineWidth = 2;
    poly(k.top); ctx.stroke(); poly(k.bottom); ctx.stroke();
    const n = Math.min(k.top.length, k.bottom.length);
    for (let i = 0; i < n; i++) { poly([k.top[i], k.bottom[i]]); ctx.stroke(); }
  }
  ctx.setLineDash([]);
  k.top.forEach((p, i) => dot(p, '#3fa7ff', S.type === 'flat' ? ['左上', '右上'][i] : `上${i + 1}`));
  k.bottom.forEach((p, i) => dot(p, '#ffb23f', S.type === 'flat' ? ['左下', '右下'][i] : `下${i + 1}`));
}

// ------------------------------------------------------------------ 交互
function allHandles() {
  const hs = [];
  if (isKeyframe()) {
    const k = K();
    k.top.forEach((p) => hs.push(p)); k.bottom.forEach((p) => hs.push(p));
  }
  S.occluders.forEach((o) => o.forEach((p) => hs.push(p)));
  S.lanes.forEach((l) => { l.left.forEach((p) => hs.push(p)); l.right.forEach((p) => hs.push(p)); });
  return hs;
}
function hit(x, y) {
  let best = null, bd = 10;
  for (const p of allHandles()) {
    const [sx, sy] = toScr(p);
    const d = Math.hypot(sx - x, sy - y);
    if (d < bd) { bd = d; best = p; }
  }
  return best;
}
cv.addEventListener('contextmenu', (e) => e.preventDefault());
cv.addEventListener('mousedown', (e) => {
  if (!S.img) return;
  const r = cv.getBoundingClientRect();
  const x = e.clientX - r.left, y = e.clientY - r.top;
  if (e.button === 2 || e.button === 1 || S.space) { S.pan = { x, y, ox: S.view.ox, oy: S.view.oy }; return; }
  const h = hit(x, y);
  if (h) { S.drag = h; return; }
  const p = toImg(x, y);
  if (p[0] < 0 || p[1] < 0 || p[0] > S.W || p[1] > S.H) return;
  if (S.tool === 'screen') addScreenPoint(p);
  else if (S.tool === 'occluder') {
    if (S.occDraft.length >= 3) {
      const [fx, fy] = toScr(S.occDraft[0]);
      if (Math.hypot(fx - x, fy - y) < 12) { nextEdge(); return; }
    }
    S.occDraft.push(p); changed();
  } else {
    const kind = S.tool === 'lane-road' ? 'road' : 'sidewalk';
    if (!S.laneDraft) S.laneDraft = { kind, left: [], right: [], phase: 'left' };
    S.laneDraft[S.laneDraft.phase].push(p);
    changed();
  }
});
window.addEventListener('mousemove', (e) => {
  const r = cv.getBoundingClientRect();
  const x = e.clientX - r.left, y = e.clientY - r.top;
  if (S.pan) { S.view.ox = S.pan.ox + x - S.pan.x; S.view.oy = S.pan.oy + y - S.pan.y; draw(); }
  else if (S.drag) {
    const p = toImg(x, y);
    S.drag[0] = Math.max(0, Math.min(S.W, p[0])); S.drag[1] = Math.max(0, Math.min(S.H, p[1]));
    draw();
  } else if (S.img) cv.style.cursor = hit(x, y) ? 'move' : (S.space ? 'grab' : 'crosshair');
});
window.addEventListener('mouseup', () => { if (S.drag) changed(); S.drag = null; S.pan = null; });
cv.addEventListener('wheel', (e) => {
  e.preventDefault();
  const r = cv.getBoundingClientRect();
  const x = e.clientX - r.left, y = e.clientY - r.top;
  const f = Math.exp(-e.deltaY * 0.0015);
  const ns = Math.max(0.02, Math.min(40, S.view.s * f));
  S.view.ox = x - (x - S.view.ox) * (ns / S.view.s);
  S.view.oy = y - (y - S.view.oy) * (ns / S.view.s);
  S.view.s = ns;
  draw();
}, { passive: false });
cv.addEventListener('dblclick', () => nextEdge());
window.addEventListener('keydown', (e) => {
  if (e.target.matches('input, select, textarea')) return;
  if (e.key === ' ') { S.space = true; e.preventDefault(); }
  if (e.key === 'Enter') nextEdge();
  if ((e.key === 'z' && (e.metaKey || e.ctrlKey)) || e.key === 'Backspace') { e.preventDefault(); undo(); }
});
window.addEventListener('keyup', (e) => { if (e.key === ' ') S.space = false; });
window.addEventListener('resize', resize);

$('#zoom-fit').onclick = fitView;
$('#zoom-100').onclick = () => {
  const r = cv.getBoundingClientRect();
  const cx = r.width / 2, cy = r.height / 2;
  const ns = 1 / (window.devicePixelRatio || 1);
  S.view.ox = cx - (cx - S.view.ox) * (ns / S.view.s); S.view.oy = cy - (cy - S.view.oy) * (ns / S.view.s); S.view.s = ns; draw();
};
$$('#tools button').forEach((b) => b.onclick = () => {
  $$('#tools button').forEach((x) => x.classList.toggle('active', x === b));
  S.tool = b.dataset.tool; S.laneDraft = null; S.occDraft = []; changed();
});
$('#scr-type').onchange = (e) => {
  S.type = e.target.value;
  for (const k of Object.keys(S.kfs)) S.kfs[k] = { top: [], bottom: [] };
  S.phase = 'top'; S.geo = null;
  $('#u-mode').value = S.type === 'curved' ? 'equal' : 'auto';
  syncTurnRow(true);
  changed();
};
function syncTurnRow(reset) {
  $('#turn-row').hidden = S.type === 'flat';
  // 拐角屏绝大多数是 90°；弧形屏只有“圆角 L 型”才是 90°，纯弧形按实际弧度填或留空
  if (reset) $('#turn-angle').value = S.type === 'folded' ? 90 : '';
}
['#u-mode', '#phys-w', '#phys-h', '#seg-w', '#focal', '#turn-angle'].forEach((s) => $(s).addEventListener('input', changed));
$('#btn-next-edge').onclick = nextEdge;
$('#btn-undo').onclick = undo;
$('#btn-insert').onclick = insertColumn;
$('#btn-clear').onclick = clearAll;
$('#btn-detect').onclick = async () => {
  if (!S.base) return;
  $('#hint').textContent = '识别中…';
  try {
    const d = await api(`/api/uploads/${S.base.id}/detect?frame=${S.frame}`, { method: 'POST' });
    if (d.scene) setParam('enhance.scene', 'auto');
    if (!d.candidates.length) { $('#hint').textContent = '没有找到明显的发光屏幕，请手动标定'; return; }
    S.type = 'flat'; $('#scr-type').value = 'flat';
    const c = d.candidates[0];
    if (!isKeyframe()) S.kfs[S.frame] = { top: [], bottom: [] }, S.cur = S.frame;
    S.kfs[S.cur] = { top: c.top, bottom: c.bottom };
    S.phase = 'done';
    changed();
    $('#hint').textContent = `自动识别到 ${d.candidates.length} 个候选（场景：${{ day: '白天', dusk: '黄昏', night: '夜景' }[d.scene.scene]}），已取最可能的一个，请放大检查四角。弧形 / 拐角屏请切换类型手动标定。`;
  } catch (e) { $('#hint').textContent = e.message; }
};
$('#btn-snap').onclick = async () => {
  if (!S.base || !screenComplete() || !isKeyframe()) { $('#hint').textContent = '请先完成屏幕标定'; return; }
  $('#hint').textContent = '吸附中…';
  try {
    const r = await api(`/api/uploads/${S.base.id}/snap`, { method: 'POST', body: JSON.stringify({ template: templateFor(S.cur), frame: S.frame }) });
    S.kfs[S.cur] = { top: r.top, bottom: r.bottom };
    const mv = r.moved.filter((x) => x > 0.3);
    changed();
    $('#hint').textContent = mv.length ? `已吸附 ${mv.length} 个点（最大移动 ${Math.max(...r.moved).toFixed(1)}px）。放大检查边角；不对可撤销后手动拖动。` : '点位已在边缘上，无需调整';
  } catch (e) { $('#hint').textContent = e.message; }
};
$('#btn-lens').onclick = async () => {
  if (!S.base || !screenComplete()) { $('#hint').textContent = '请先完成屏幕标定'; return; }
  $('#hint').textContent = '估计中…';
  try {
    const r = await api(`/api/uploads/${S.base.id}/lens`, { method: 'POST', body: JSON.stringify({ template: templateFor(S.cur), frame: S.frame }) });
    if (r.k1) { setParam('fusion.lens_k1', r.k1); changed(); }
    $('#hint').textContent = r.k1 ? `镜头畸变 k1=${r.k1}（边缘直线度 ${r.rms_before}px → ${r.rms_after}px），已填入参数` : `未检测到明显畸变（边缘直线度 ${r.rms_before ?? '-'}px），保持 0`;
  } catch (e) { $('#hint').textContent = e.message; }
};
$('#btn-unwrap').onclick = async () => {
  if (!S.base || !screenComplete() || !isKeyframe()) { $('#hint').textContent = '请先完成屏幕标定'; return; }
  try {
    const r = await fetch(`/api/uploads/${S.base.id}/unwrap`, { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ template: templateFor(S.cur), frame: S.frame, lens_k1: S.params['fusion.lens_k1'] || 0, height: 420 }) });
    if (!r.ok) throw new Error((await r.json()).detail);
    const url = URL.createObjectURL(await r.blob());
    $('#unwrap-img').src = url;
    $('#unwrap-box').hidden = false;
  } catch (e) { $('#hint').textContent = e.message; }
};

let frameTimer;
$('#frame').oninput = (e) => {
  const f = parseInt(e.target.value, 10);
  $('#frame-no').textContent = f;
  clearTimeout(frameTimer);
  frameTimer = setTimeout(async () => {
    await loadFrame(f);
    if (S.kfs[f]) S.cur = f;
    changed();
  }, 120);
};
$('#btn-add-kf').onclick = () => {
  if (S.kfs[S.frame]) return;
  const src = S.kfs[nearestKf(S.frame)];
  S.kfs[S.frame] = { top: src.top.map((p) => [...p]), bottom: src.bottom.map((p) => [...p]) };
  S.cur = S.frame;
  S.phase = screenComplete() ? 'done' : 'top';
  changed();
};
$('#btn-del-kf').onclick = () => {
  if (Object.keys(S.kfs).length <= 1 || !S.kfs[S.frame]) return;
  delete S.kfs[S.frame];
  S.cur = nearestKf(S.frame);
  changed();
};
function renderKfs() {
  const el = $('#kf-list');
  if (S.base?.kind !== 'video') { el.innerHTML = ''; return; }
  el.innerHTML = Object.keys(S.kfs).map(Number).sort((a, b) => a - b).map((k) =>
    `<span class="chip ${k === S.frame ? 'on' : ''}" data-k="${k}">关键帧 ${k}${screenComplete(S.kfs[k]) ? '' : ' (未完成)'}</span>`).join('');
  $$('.chip', el).forEach((c) => c.onclick = async () => {
    const k = parseInt(c.dataset.k, 10);
    $('#frame').value = k; await loadFrame(k); S.cur = k; changed();
  });
}
function renderLanes() {
  const el = $('#lane-list');
  el.innerHTML = S.lanes.map((l, i) => `<div class="row"><label>${l.kind === 'road' ? '车道' : '人行道'} ${i + 1}</label>
    <input type="number" step="0.5" value="${l.width_m}" data-i="${i}" data-f="width_m" title="物理宽度（米）" style="width:60px;flex:none">米
    <select data-i="${i}" data-f="direction"><option value="both">双向</option><option value="forward">沿画线方向</option><option value="backward">逆画线方向</option></select>
    <button data-del="${i}" class="danger">×</button></div>`).join('');
  $$('[data-f]', el).forEach((inp) => {
    const l = S.lanes[inp.dataset.i];
    if (inp.dataset.f === 'direction') inp.value = l.direction;
    inp.onchange = () => { l[inp.dataset.f] = inp.dataset.f === 'width_m' ? parseFloat(inp.value) : inp.value; };
  });
  $$('[data-del]', el).forEach((b) => b.onclick = () => { S.lanes.splice(+b.dataset.del, 1); renderLanes(); draw(); });
}

// ------------------------------------------------------------------ 模板
async function refreshTemplates() {
  const list = await api('/api/templates');
  $('#tpl-select').innerHTML = '<option value="">— 选择已保存点位 —</option>' + list.map((t) =>
    `<option value="${esc(t.id)}">${esc(t.name)}（${{ flat: '平面', curved: '弧形', folded: '折角' }[t.type]}）</option>`).join('');
  $('#tpl-select')._list = list;
  $('#templates').innerHTML = list.length ? list.map((t) => `<div class="card"><div class="body">
    <b>${esc(t.name)}</b><br><small>${{ flat: '平面矩形屏', curved: '弧形曲面屏', folded: '拐角 / 折角屏' }[t.type]} · ${t.top.length} 列点 ·
    标定尺寸 ${t.image_size ? t.image_size.join('×') : '-'} · 宽度分配 ${t.u_mode}${t.physical_size ? ` · ${t.physical_size.join('×')}m` : ''}<br>更新 ${esc(t.updated || '')}</small>
    <div class="btns"><button data-del-tpl="${esc(t.id)}" class="danger">删除</button></div></div></div>`).join('')
    : '<p style="color:var(--muted)">还没有保存的点位模板。标定完成后在左侧“保存模板”，同一机位以后直接载入复用。</p>';
  $$('[data-del-tpl]').forEach((b) => b.onclick = async () => {
    if (!confirm('删除该模板？')) return;
    await api(`/api/templates/${b.dataset.delTpl}`, { method: 'DELETE' });
    refreshTemplates();
  });
}
$('#tpl-select').onchange = (e) => {
  const t = (e.target._list || []).find((x) => x.id === e.target.value);
  if (!t) return;
  applyTemplate(t);
};
function applyTemplate(t) {
  S.type = t.type; $('#scr-type').value = t.type;
  const sx = S.W && t.image_size ? S.W / t.image_size[0] : 1, sy = S.H && t.image_size ? S.H / t.image_size[1] : 1;
  const sc = (pts) => pts.map(([x, y]) => [x * sx, y * sy]);
  S.kfs[S.cur] = { top: sc(t.top), bottom: sc(t.bottom) };
  S.occluders = (t.occluders || []).map(sc);
  $('#u-mode').value = t.u_mode || 'auto';
  $('#phys-w').value = t.physical_size ? t.physical_size[0] : '';
  $('#phys-h').value = t.physical_size ? t.physical_size[1] : '';
  $('#seg-w').value = t.segment_widths ? t.segment_widths.join(',') : '';
  $('#focal').value = t.focal_px || '';
  syncTurnRow(false);
  $('#turn-angle').value = t.turn_angle || '';
  $('#tpl-name').value = t.name || '';
  S.tplId = t.id || '';
  S.phase = 'done';
  changed();
}
$('#btn-save-tpl').onclick = async () => {
  if (!screenComplete()) { alert('请先完成屏幕标定'); return; }
  try {
    const t = await api('/api/templates', { method: 'POST', body: JSON.stringify(templateFor(S.cur)) });
    S.tplId = t.id;
    await refreshTemplates();
    $('#tpl-select').value = t.id;
    $('#hint').textContent = `模板“${t.name}”已保存，同一机位素材可直接载入复用`;
  } catch (e) { alert(e.message); }
};

// ------------------------------------------------------------------ 任务
function buildJob(preview) {
  const mode = $('input[name=mode]:checked').value;
  if (!S.base) throw new Error('请先上传原始素材');
  if (mode !== 'enhance' && !S.ad) throw new Error('替换模式需要上传客户广告素材');
  const p = paramsObject();
  const body = { base_id: S.base.id, ad_id: S.ad?.id || null, mode, preview, title: $('#title').value.trim(), parent_id: S.parentId, ...p, lanes: S.lanes };
  const done = Object.keys(S.kfs).filter((k) => screenComplete(S.kfs[k]));
  if (mode !== 'enhance' && !done.length) throw new Error('请先完成屏幕标定（大屏增强在“只精修”模式下也需要标定屏幕）');
  if (done.length) {
    if (S.base.kind === 'video') {
      body.keyframes = Object.fromEntries(done.map((k) => [k, templateFor(k)]));
      body.template = templateFor(done[0]);
      body.video.keyframe = parseInt(done[0], 10);
    } else body.template = templateFor(done[0]);
  }
  return body;
}
async function submit(preview) {
  let body;
  try { body = buildJob(preview); } catch (e) { $('#job-status').innerHTML = `<span class="err">${esc(e.message)}</span>`; return; }
  try {
    const job = await api('/api/jobs', { method: 'POST', body: JSON.stringify(body) });
    poll(job.id);
  } catch (e) { $('#job-status').innerHTML = `<span class="err">${esc(e.message)}</span>`; }
}
$('#btn-preview').onclick = () => submit(true);
$('#btn-run').onclick = () => submit(false);
let pollTimer;
async function poll(id) {
  clearTimeout(pollTimer);
  const j = await api(`/api/jobs/${id}`);
  $('#job-status').innerHTML = `<div>${j.preview ? '预览' : '任务'} ${esc(j.id)} · <span class="status-${j.status}">${{ queued: '排队中', running: '处理中', done: '完成', failed: '失败' }[j.status]}</span> ${esc(j.message || '')}</div>
    <div class="bar"><div style="width:${Math.round((j.progress || 0) * 100)}%"></div></div>${j.status === 'failed' ? `<div class="err">${esc(j.error || '')}</div>` : ''}`;
  if (j.status === 'done') showResult(j);
  else if (j.status !== 'failed') pollTimer = setTimeout(() => poll(id), 1000);
}
function showResult(j) {
  $('#result').hidden = false;
  const o = j.outputs || {};
  const names = { final: '最终成片', client: '客户交付版', watermark: '带水印版', refined: '精修原图', poster: '关键帧截图', original: '原图', track: '跟踪数据' };
  $('#result-links').innerHTML = Object.entries(o).filter(([k]) => k !== 'original').map(([k, v]) => `<a href="${v}" download target="_blank">${names[k] || k}</a>`).join('');
  const isVideo = String(o.final || '').endsWith('.mp4');
  const after = isVideo ? o.poster : o.final;
  $('#compare').hidden = !after || !o.original;
  if (after && o.original) {
    const t = `?t=${Date.now()}`;
    $('#cmp-a').src = after + t;
    $('#cmp-b').src = o.original + t;
    $('#cmp-a').onload = () => { $('#cmp-b').style.width = $('#cmp-a').clientWidth + 'px'; };
  }
  $('#video-out').innerHTML = isVideo ? `<video src="${o.final}" controls loop></video>` : '';
  $('#report').textContent = JSON.stringify(j.report, null, 2);
}
$('#btn-close-result').onclick = () => { $('#result').hidden = true; };
$('#compare').addEventListener('mousemove', (e) => {
  const r = $('#compare').getBoundingClientRect();
  $('#cmp-top').style.width = `${Math.max(0, Math.min(100, ((e.clientX - r.left) / r.width) * 100))}%`;
});

// ------------------------------------------------------------------ 历史版本
async function refreshHistory() {
  const list = await api('/api/jobs');
  const modeName = { enhance: 'A 只精修', enhance_then_replace: 'B 先精修再替换', replace_then_enhance: 'C 先替换再精修' };
  $('#history').innerHTML = list.length ? list.map((j) => {
    const o = j.outputs || {};
    const thumb = o.poster || (String(o.final || '').endsWith('.mp4') ? '' : o.final) || '';
    return `<div class="card"><div class="thumb" style="background-image:url('${thumb}')"></div><div class="body">
      <b>${esc(j.title || j.id)}</b> <span class="status-${j.status}">${{ queued: '排队中', running: '处理中', done: '完成', failed: '失败' }[j.status]}</span><br>
      <small>${modeName[j.mode] || j.mode} · ${j.kind === 'video' ? '视频' : '图片'} · ${esc(j.created)}${j.parent_id ? ` · 基于 ${esc(j.parent_id)}` : ''}</small>
      <div class="btns">${j.status === 'done' ? `<button data-view="${j.id}">查看</button>` : ''}<button data-reuse="${j.id}">以此再处理</button><button data-del-job="${j.id}" class="danger">删除</button></div></div></div>`;
  }).join('') : '<p style="color:var(--muted)">还没有正式处理过的任务。</p>';
  $$('[data-view]').forEach((b) => b.onclick = async () => { const j = await api(`/api/jobs/${b.dataset.view}`); switchTab('editor'); showResult(j); });
  $$('[data-del-job]').forEach((b) => b.onclick = async () => { if (confirm('删除该版本及其输出文件？')) { await api(`/api/jobs/${b.dataset.delJob}`, { method: 'DELETE' }).catch((e) => alert(e.message)); refreshHistory(); } });
  $$('[data-reuse]').forEach((b) => b.onclick = () => reuseJob(b.dataset.reuse));
}
async function reuseJob(id) {
  const j = await api(`/api/jobs/${id}`);
  const rq = j.request || {};
  switchTab('editor');
  $('#result').hidden = true;
  await setUpload('base', await api(`/api/uploads/${rq.base_id}`));
  if (rq.ad_id) await setUpload('ad', await api(`/api/uploads/${rq.ad_id}`));
  $(`input[name=mode][value="${rq.mode}"]`).checked = true;
  for (const g of ['enhance', 'crowd', 'fusion', 'video', 'output']) for (const [k, v] of Object.entries(rq[g] || {})) setParam(`${g}.${k}`, v);
  S.lanes = rq.lanes || []; renderLanes();
  S.kfs = {};
  if (rq.keyframes && Object.keys(rq.keyframes).length) {
    for (const [k, t] of Object.entries(rq.keyframes)) S.kfs[k] = { top: t.top, bottom: t.bottom };
    S.cur = Math.min(...Object.keys(S.kfs).map(Number));
  } else S.cur = 0;
  if (rq.template) { if (!S.kfs[S.cur]) S.kfs[S.cur] = { top: [], bottom: [] }; applyTemplate(rq.template); }
  else S.kfs[S.cur] = S.kfs[S.cur] || { top: [], bottom: [] };
  $('#title').value = rq.title || '';
  S.parentId = id;
  if (S.base.kind === 'video') { $('#frame').value = S.cur; await loadFrame(S.cur); }
  changed();
}

// ------------------------------------------------------------------ 标签页
function switchTab(name) {
  $$('nav button').forEach((b) => b.classList.toggle('active', b.dataset.tab === name));
  $$('.tab').forEach((t) => t.classList.toggle('active', t.id === `tab-${name}`));
  if (name === 'history') refreshHistory();
  if (name === 'templates') refreshTemplates();
  if (name === 'editor') setTimeout(resize, 0);
}
$$('nav button').forEach((b) => b.onclick = () => switchTab(b.dataset.tab));

// ------------------------------------------------------------------ 启动
buildParams();
setupDrop($('#drop-base'), 'base');
setupDrop($('#drop-ad'), 'ad');
refreshTemplates();
resize();
updateHint();
api('/api/meta').then((m) => {
  if (!m.sprites.people && !m.sprites.vehicles) {
    const r = $('#params .row[data-key="crowd.sprites"] label');
    if (r) r.title = '素材库为空：在 studio/assets/sprites/people 与 vehicles 放入透明底 PNG';
  }
}).catch(() => {});

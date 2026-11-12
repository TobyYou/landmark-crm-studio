/* 地标传媒 · 线索公海管理系统 — H5 / 管理端（同一套 SPA，按角色与屏宽自适应） */
(function () {
  'use strict';
  const { createApp, reactive, computed, watch, onMounted, onUnmounted } = Vue;

  // ======================= 全局状态 =======================
  const LS = {
    get(k, d) { try { const v = localStorage.getItem(k); return v == null ? d : JSON.parse(v); } catch (_) { return d; } },
    set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (_) { /* 存储不可用时忽略 */ } },
    del(k) { try { localStorage.removeItem(k); } catch (_) { /* ignore */ } },
  };
  const store = reactive({
    token: LS.get('token', null),
    user: LS.get('user', null),
    meta: LS.get('meta', null),
    route: { path: '/', params: {}, query: {} },
    toast: '',
    dialog: null,
    offline: !navigator.onLine,
    queue: LS.get('offlineQueue', []),
  });
  const isManager = () => store.user && (store.user.role === 'director' || store.user.role === 'admin');
  const ROLE = { sales: '销售', director: '销售总监', admin: '超级管理员' };
  const ACTION = {
    create: '录入线索', import: '批量导入', claim: '认领', release: '主动退回公海', auto_recycle: '超时自动回流',
    force_recycle: '总监强制回收', update: '修改信息', stage_change: '更新阶段', followup: '新增跟进', voice_parse: '语音识别',
    export: '导出数据', settings_update: '修改系统配置', user_create: '新增账号', user_update: '修改账号', user_reset_password: '重置密码',
  };
  const RELEASE = { release: '主动退回', auto_recycle: '超时自动回流', force_recycle: '总监强制回收' };

  // ======================= 工具 =======================
  const pad = (n) => String(n).padStart(2, '0');
  const fmt = (t, withTime = true) => {
    if (!t) return '—';
    const d = new Date(t);
    const s = `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
    return withTime ? `${s} ${pad(d.getHours())}:${pad(d.getMinutes())}` : s;
  };
  const ago = (t) => {
    if (!t) return '';
    const m = Math.round((Date.now() - t) / 60000);
    if (m < 1) return '刚刚';
    if (m < 60) return `${m} 分钟前`;
    if (m < 1440) return `${Math.round(m / 60)} 小时前`;
    return `${Math.round(m / 1440)} 天前`;
  };
  const toLocalInput = (t) => { if (!t) return ''; const d = new Date(t); return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`; };
  let toastTimer;
  function toast(msg, ms = 2200) { store.toast = msg; clearTimeout(toastTimer); toastTimer = setTimeout(() => { store.toast = ''; }, ms); }
  /** 通用确认 / 输入弹窗：返回 Promise<false | true | string> */
  function ask(opts) {
    return new Promise((resolve) => { store.dialog = { okText: '确定', ...opts, value: opts.input && opts.input.value || '', resolve }; });
  }
  function go(path) { if (location.hash !== '#' + path) location.hash = '#' + path; }

  // ======================= 接口 =======================
  async function api(method, url, body, { cache = false, raw = false, silent = false } = {}) {
    const headers = {};
    if (store.token) headers.authorization = 'Bearer ' + store.token;
    let payload;
    if (body !== undefined) {
      if (typeof body === 'string') { headers['content-type'] = 'text/csv'; payload = body; }
      else { headers['content-type'] = 'application/json'; payload = JSON.stringify(body); }
    }
    let res;
    try {
      res = await fetch('/api' + url, { method, headers, body: payload });
    } catch (e) {
      store.offline = true;
      if (cache && method === 'GET') {
        const c = LS.get('c:' + store.user.id + ':' + url, null);
        if (c) return { ...c, _cached: true };
      }
      const err = new Error('网络不可用，请检查网络后重试');
      err.network = true;
      throw err;
    }
    store.offline = false;
    if (res.status === 401 && url !== '/auth/login') { logout(true); throw new Error('登录已过期，请重新登录'); }
    if (raw) {
      if (!res.ok) throw new Error((await res.json().catch(() => ({}))).error || '请求失败');
      return res;
    }
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      const err = new Error(data.error || `请求失败（${res.status}）`);
      err.status = res.status; err.data = data;
      if (!silent) toast(err.message);
      throw err;
    }
    if (cache && method === 'GET' && store.user) LS.set('c:' + store.user.id + ':' + url, data);
    return data;
  }
  const qs = (o) => {
    const p = Object.entries(o).filter(([, v]) => v !== '' && v != null).map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(v)}`);
    return p.length ? '?' + p.join('&') : '';
  };
  async function download(url, fallbackName) {
    try {
      const res = await api('GET', url, undefined, { raw: true });
      const blob = await res.blob();
      const cd = res.headers.get('content-disposition') || '';
      const m = cd.match(/filename\*=UTF-8''([^;]+)/i);
      const name = m ? decodeURIComponent(m[1]) : fallbackName;
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob); a.download = name;
      document.body.appendChild(a); a.click(); a.remove();
      setTimeout(() => URL.revokeObjectURL(a.href), 4000);
    } catch (e) { toast(e.message); }
  }
  async function loadMeta() {
    try { store.meta = await api('GET', '/meta', undefined, { cache: true, silent: true }); LS.set('meta', store.meta); } catch (_) { /* 离线时沿用缓存 */ }
  }
  function logout(expired) {
    store.token = null; store.user = null; LS.del('token'); LS.del('user');
    if (expired) toast('登录已过期，请重新登录');
    go('/login');
  }

  // 离线跟进队列：联网后自动补交
  async function flushQueue() {
    if (!store.queue.length || !store.token) return;
    const rest = [];
    let ok = 0;
    for (const item of store.queue) {
      try { await api('POST', `/leads/${item.leadId}/followups`, item.body, { silent: true }); ok++; } catch (e) {
        if (e.network) rest.push(item); // 仍然离线，保留
        else toast(`离线跟进同步失败：${item.company}（${e.message}）`, 4000);
      }
    }
    store.queue = rest; LS.set('offlineQueue', rest);
    if (ok) toast(`已同步 ${ok} 条离线跟进记录`);
  }
  window.addEventListener('online', () => { store.offline = false; flushQueue(); });
  window.addEventListener('offline', () => { store.offline = true; });

  // ======================= 路由 =======================
  const ROUTES = [
    ['/login', 'login-view', '登录'],
    ['/pool', 'lead-list', '公海线索', { mode: 'pool' }],
    ['/mine', 'lead-list', '我的线索', { mode: 'mine' }],
    ['/ledger', 'lead-list', '全员线索台账', { mode: 'ledger' }, 'manager'],
    ['/lead/:id', 'lead-detail', '线索详情'],
    ['/new', 'lead-form', '新增线索'],
    ['/dashboard', 'dashboard-view', '总监看板', {}, 'manager'],
    ['/import', 'import-view', '批量导入', {}, 'manager'],
    ['/admin', 'admin-view', '系统设置', {}, 'admin'],
    ['/me', 'me-view', '我的'],
  ];
  function parseHash() {
    const h = location.hash.replace(/^#/, '') || '/';
    const [path, q] = h.split('?');
    const query = Object.fromEntries(new URLSearchParams(q || ''));
    for (const [pat, comp, title, props, need] of ROUTES) {
      const keys = [];
      const re = new RegExp('^' + pat.replace(/:(\w+)/g, (_, k) => { keys.push(k); return '([^/]+)'; }) + '$');
      const m = path.match(re);
      if (m) {
        const params = {};
        keys.forEach((k, i) => { params[k] = decodeURIComponent(m[i + 1]); });
        return { path, comp, title, props: { ...(props || {}), ...params }, need, query };
      }
    }
    return null;
  }
  function homePath() { return isManager() ? '/dashboard' : '/mine'; }

  // ======================= 组件：语音录入 =======================
  const FIELD_NAMES = {
    company_name: '公司全称', contact_name: '对接人', contact_dept: '部门', contact_title: '岗位', contact_phone: '电话',
    contact_wechat: '微信', address: '地址', industry_id: '行业', source: '来源', intent_level: '意向等级', stage: '跟进阶段',
  };
  const VoicePanel = {
    props: { leadId: [String, Number], compact: Boolean },
    emits: ['apply'],
    setup(props, { emit }) {
      const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
      const s = reactive({ text: '', listening: false, interim: '', loading: false, result: null, picked: {}, pickFollow: true, error: '' });
      let rec = null;
      function start() {
        if (!SR) { s.error = '当前浏览器不支持网页语音识别：请点击下方输入框，使用手机键盘上的🎤语音输入，说完后点“AI 识别填充”。'; return; }
        rec = new SR();
        rec.lang = 'zh-CN'; rec.continuous = true; rec.interimResults = true;
        rec.onresult = (e) => {
          let fin = ''; let tmp = '';
          for (let i = e.resultIndex; i < e.results.length; i++) {
            if (e.results[i].isFinal) fin += e.results[i][0].transcript; else tmp += e.results[i][0].transcript;
          }
          if (fin) s.text += (s.text && !/[，。,.]$/.test(s.text) ? '，' : '') + fin;
          s.interim = tmp;
        };
        rec.onerror = (e) => { s.error = e.error === 'not-allowed' ? '没有麦克风权限，请在浏览器设置中允许' : '语音识别中断：' + e.error; s.listening = false; };
        rec.onend = () => { s.listening = false; s.interim = ''; };
        s.error = ''; s.listening = true;
        try { rec.start(); } catch (_) { s.listening = false; }
      }
      function stop() { if (rec) rec.stop(); s.listening = false; }
      async function parse() {
        if (s.listening) stop();
        if (!s.text.trim()) { s.error = '请先口述或输入客户信息'; return; }
        s.loading = true; s.error = '';
        try {
          const r = await api('POST', '/ai/extract', { text: s.text, lead_id: props.leadId || undefined });
          s.result = r;
          s.picked = Object.fromEntries(Object.keys(r.fields).map((k) => [k, true]));
          s.pickFollow = !!r.followup;
          if (!Object.keys(r.fields).length && !r.followup) s.error = r.message;
        } catch (e) { s.error = e.message; } finally { s.loading = false; }
      }
      function display(k, v) {
        if (k === 'industry_id') { const i = (store.meta.industries || []).find((x) => x.id === v); return i ? i.name : v; }
        return v;
      }
      function apply() {
        const fields = {};
        for (const [k, v] of Object.entries(s.result.fields)) if (s.picked[k]) fields[k] = v;
        emit('apply', { fields, followup: s.pickFollow ? s.result.followup : null, text: s.text });
        s.result = null; s.text = '';
      }
      onUnmounted(stop);
      return { s, SR, start, stop, parse, apply, display, FIELD_NAMES, fmt };
    },
    template: `
    <div class="voice">
      <div class="row" style="align-items:flex-start">
        <button type="button" class="mic" :class="{on:s.listening}" @click="s.listening ? stop() : start()" :title="s.listening?'停止':'开始语音录入'">{{ s.listening ? '■' : '🎤' }}</button>
        <div class="grow">
          <div style="font-weight:600">语音录入 <span class="faint">口述客户信息，AI 自动拆分到字段</span></div>
          <div class="faint">例：“今天拜访了蓝湾饮料有限公司，对接人王小明，市场部品牌经理，电话139…，客户想要报价，下周三再联系”</div>
        </div>
      </div>
      <textarea class="input" style="margin-top:10px" v-model="s.text" :placeholder="s.listening ? '正在聆听…' : '点击🎤口述，或在此输入/使用键盘语音输入'"></textarea>
      <div v-if="s.interim" class="faint">… {{ s.interim }}</div>
      <div class="row" style="margin-top:8px">
        <button type="button" class="btn accent" :disabled="s.loading" @click="parse">{{ s.loading ? '识别中…' : 'AI 识别填充' }}</button>
        <button type="button" class="btn ghost sm" v-if="s.text" @click="s.text='';s.result=null">清空</button>
      </div>
      <div v-if="s.error" class="faint" style="color:var(--warn);margin-top:6px">{{ s.error }}</div>
      <div v-if="s.result" class="suggest">
        <div class="row between"><span class="faint">识别引擎：{{ s.result.engine === 'claude' ? 'Claude AI' : '本地规则' }} · {{ s.result.message }}</span></div>
        <div v-if="s.result.warning" class="faint" style="color:var(--warn)">{{ s.result.warning }}</div>
        <label v-for="(v,k) in s.result.fields" :key="k"><input type="checkbox" v-model="s.picked[k]"><b>{{ FIELD_NAMES[k] || k }}</b><span>{{ display(k, v) }}</span></label>
        <label v-if="s.result.followup"><input type="checkbox" v-model="s.pickFollow"><b>跟进记录</b><span>{{ s.result.followup.content }}<template v-if="s.result.followup.next_follow_at"><br><span class="faint">下次跟进：{{ fmt(s.result.followup.next_follow_at) }}</span></template></span></label>
        <div v-if="s.result.missing.length" class="faint" style="color:var(--warn);margin-top:6px">未识别到：{{ s.result.missing.map(k=>FIELD_NAMES[k]).join('、') }}，请手动补充</div>
        <div class="row" style="margin-top:8px"><button type="button" class="btn primary" @click="apply">填入表单</button><span class="faint">填入后请核对，点“保存”才会生效</span></div>
      </div>
    </div>`,
  };

  // ======================= 组件：线索字段表单 =======================
  const LeadFields = {
    props: { form: Object, hl: Object },
    setup() { return { store }; },
    template: `
    <div class="grid2">
      <div class="field"><label>公司全称<span class="req">*</span></label><input class="input" :class="{hl:hl&&hl.company_name}" v-model.trim="form.company_name" maxlength="200" placeholder="营业执照上的全称"></div>
      <div class="field"><label>对接人姓名<span class="req">*</span></label><input class="input" :class="{hl:hl&&hl.contact_name}" v-model.trim="form.contact_name" maxlength="50"></div>
      <div class="field"><label>对接人部门</label><input class="input" :class="{hl:hl&&hl.contact_dept}" v-model.trim="form.contact_dept"></div>
      <div class="field"><label>对接人岗位</label><input class="input" :class="{hl:hl&&hl.contact_title}" v-model.trim="form.contact_title"></div>
      <div class="field"><label>联系电话</label><input class="input" :class="{hl:hl&&hl.contact_phone}" v-model.trim="form.contact_phone" inputmode="tel"></div>
      <div class="field"><label>微信</label><input class="input" :class="{hl:hl&&hl.contact_wechat}" v-model.trim="form.contact_wechat"></div>
      <div class="field" style="grid-column:1/-1"><label>办公地址 / 公司位置</label><input class="input" :class="{hl:hl&&hl.address}" v-model.trim="form.address"></div>
      <div class="field"><label>所属行业</label>
        <select class="input" :class="{hl:hl&&hl.industry_id}" v-model="form.industry_id"><option :value="null">请选择</option><option v-for="i in store.meta.industries" :key="i.id" :value="i.id">{{ i.name }}</option></select></div>
      <div class="field"><label>线索来源</label>
        <select class="input" :class="{hl:hl&&hl.source}" v-model="form.source"><option :value="null">请选择</option><option v-for="o in store.meta.sources" :key="o.id" :value="o.value">{{ o.value }}</option></select></div>
      <div class="field"><label>意向等级</label>
        <select class="input" :class="{hl:hl&&hl.intent_level}" v-model="form.intent_level"><option :value="null">请选择</option><option v-for="o in store.meta.intents" :key="o.id" :value="o.value">{{ o.value }}</option></select></div>
      <div class="field"><label>当前跟进阶段</label>
        <select class="input" :class="{hl:hl&&hl.stage}" v-model="form.stage"><option :value="null">请选择</option><option v-for="o in store.meta.stages" :key="o.id" :value="o.value">{{ o.value }}</option></select></div>
      <div class="field" style="grid-column:1/-1"><label>备注</label><textarea class="input" v-model="form.remark" maxlength="2000" style="min-height:60px"></textarea></div>
    </div>`,
  };
  const FORM_KEYS = ['company_name', 'contact_name', 'contact_dept', 'contact_title', 'contact_phone', 'contact_wechat', 'address', 'industry_id', 'source', 'intent_level', 'stage', 'remark'];
  const emptyForm = () => Object.fromEntries(FORM_KEYS.map((k) => [k, k === 'company_name' || k === 'contact_name' ? '' : null]));

  // ======================= 组件：登录 =======================
  const LoginView = {
    setup() {
      const f = reactive({ username: '', password: '', loading: false, err: '' });
      async function submit() {
        f.err = ''; f.loading = true;
        try {
          const r = await api('POST', '/auth/login', { username: f.username.trim(), password: f.password }, { silent: true });
          store.token = r.token; store.user = r.user; LS.set('token', r.token); LS.set('user', r.user);
          await loadMeta();
          flushQueue();
          go(homePath());
        } catch (e) { f.err = e.message; } finally { f.loading = false; }
      }
      return { f, submit };
    },
    template: `
    <div class="login"><form class="box" @submit.prevent="submit">
      <img class="logo" src="/icon.svg" alt="">
      <h2>地标传媒 · 线索公海</h2>
      <div class="faint" style="margin-bottom:18px">户外广告销售线索管理系统</div>
      <div class="field" style="margin-bottom:12px"><label>账号</label><input class="input" v-model="f.username" autocomplete="username" autocapitalize="off" required></div>
      <div class="field" style="margin-bottom:16px"><label>密码</label><input class="input" type="password" v-model="f.password" autocomplete="current-password" required></div>
      <div v-if="f.err" style="color:var(--danger);font-size:13px;margin-bottom:10px">{{ f.err }}</div>
      <button class="btn primary block" :disabled="f.loading">{{ f.loading ? '登录中…' : '登录' }}</button>
      <div class="faint" style="margin-top:14px;text-align:center">账号由管理员分配，忘记密码请联系管理员重置</div>
    </form></div>`,
  };

  // ======================= 组件：线索列表（公海 / 我的 / 台账） =======================
  const LeadList = {
    props: { mode: String },
    setup(props) {
      const q0 = store.route.query;
      const f = reactive({ industry_id: '', intent_level: '', stage: '', q: '', expire: q0.expire || '', status: q0.status || '', owner_id: q0.owner_id || '', returned: '', sort: q0.sort || '' });
      const s = reactive({ items: [], total: 0, page: 1, loading: false, cached: false });
      const url = { pool: '/leads/pool', mine: '/leads/mine', ledger: '/leads/ledger' }[props.mode];
      async function load(reset = true) {
        if (reset) s.page = 1;
        s.loading = true;
        try {
          const r = await api('GET', url + qs({ ...f, page: s.page, size: 30 }), undefined, { cache: s.page === 1 });
          s.items = reset ? r.items : s.items.concat(r.items);
          s.total = r.total; s.cached = !!r._cached;
        } catch (e) { if (!e.status) toast(e.message); } finally { s.loading = false; }
      }
      let t;
      watch(() => ({ ...f }), (nv, ov) => { clearTimeout(t); t = setTimeout(() => load(), nv.q !== ov.q ? 350 : 0); });
      onMounted(() => load());
      async function claim(l, ev) {
        ev.stopPropagation();
        const ok = await ask({ title: '认领线索', message: `确认认领「${l.company_name}」？认领后 ${store.meta.settings.recycle_days} 天内无有效跟进将自动回流公海。`, okText: '认领' });
        if (!ok) return;
        try {
          await api('POST', `/leads/${l.id}/claim`);
          s.items = s.items.filter((x) => x.id !== l.id); s.total--;
          toast('认领成功，已进入你的个人池');
          go('/lead/' + l.id);
        } catch (e) { if (e.status === 409) load(); }
      }
      function cls(l) {
        if (l.status !== 'owned') return '';
        if (l.days_left <= 1) return 'lead overdue';
        return l.expiring ? 'lead expiring' : 'lead';
      }
      const warnDays = computed(() => (store.meta && store.meta.settings.warn_days) || 7);
      return { f, s, load, claim, cls, store, fmt, ago, RELEASE, warnDays, isManager };
    },
    template: `
    <div>
      <div class="chips" v-if="mode==='mine'">
        <button class="chip" :class="{on:!f.expire}" @click="f.expire=''">全部</button>
        <button class="chip" :class="{on:f.expire==='warn'}" @click="f.expire='warn'">即将回流（≤{{ warnDays }}天）</button>
        <button class="chip" :class="{on:f.expire==='3'}" @click="f.expire='3'">3 天内回流</button>
        <button class="chip" :class="{on:f.sort==='recent'}" @click="f.sort = f.sort==='recent' ? '' : 'recent'">按最近更新</button>
      </div>
      <div class="chips" v-if="mode==='ledger'">
        <button class="chip" :class="{on:!f.status && !f.expire}" @click="f.status='';f.expire=''">全部</button>
        <button class="chip" :class="{on:f.status==='pool'}" @click="f.status='pool';f.expire=''">公海待认领</button>
        <button class="chip" :class="{on:f.status==='owned' && !f.expire}" @click="f.status='owned';f.expire=''">已认领</button>
        <button class="chip" :class="{on:f.expire==='warn'}" @click="f.status='owned';f.expire='warn';f.sort='expire'">即将回流</button>
      </div>
      <div class="chips" v-if="mode==='pool'">
        <button class="chip" :class="{on:!f.returned}" @click="f.returned=''">全部</button>
        <button class="chip" :class="{on:f.returned==='0'}" @click="f.returned='0'">新线索</button>
        <button class="chip" :class="{on:f.returned==='1'}" @click="f.returned='1'">回流线索</button>
      </div>
      <div class="filters">
        <input class="input search" v-model="f.q" placeholder="搜索公司 / 对接人 / 地址" type="search">
        <select class="input" v-model="f.industry_id"><option value="">全部行业</option><option v-for="i in store.meta.industries" :key="i.id" :value="i.id">{{ i.name }}</option></select>
        <select class="input" v-model="f.intent_level"><option value="">意向等级</option><option v-for="o in store.meta.intents" :key="o.id" :value="o.value">{{ o.value }}</option></select>
        <select class="input" v-model="f.stage"><option value="">跟进阶段</option><option v-for="o in store.meta.stages" :key="o.id" :value="o.value">{{ o.value }}</option></select>
        <select class="input" v-if="mode==='ledger'" v-model="f.owner_id"><option value="">全部销售</option><option v-for="u in store.meta.sales" :key="u.id" :value="u.id">{{ u.name }}{{ u.active ? '' : '（停用）' }}</option></select>
      </div>
      <div class="row between" style="margin-bottom:8px">
        <span class="faint">共 {{ s.total }} 条<span v-if="s.cached">（离线缓存）</span></span>
        <a v-if="mode!=='ledger' && (store.meta.settings.sales_can_create || isManager())" href="#/new" class="btn sm">＋ 新增线索</a>
      </div>
      <div class="listgrid">
        <div v-for="l in s.items" :key="l.id" :class="cls(l) || 'lead'" @click="location.hash='#/lead/'+l.id">
          <div class="t">
            <span class="name">{{ l.company_name }}</span>
            <span v-if="l.status==='owned'" class="days" :style="{color: l.expiring ? 'var(--warn)' : 'var(--ink-3)'}">{{ l.days_left > 0 ? l.days_left + '天' : '即将回流' }}</span>
          </div>
          <div class="meta">
            <span>👤 {{ l.contact_name }}<template v-if="l.contact_title"> · {{ l.contact_title }}</template></span>
            <span v-if="l.contact_phone">📞 {{ l.contact_phone }}</span>
          </div>
          <div class="tags">
            <span class="tag info" v-if="l.industry_name">{{ l.industry_name }}</span>
            <span class="tag accent" v-if="l.intent_level">{{ l.intent_level }}</span>
            <span class="tag" v-if="l.stage">{{ l.stage }}</span>
            <template v-if="mode==='ledger'">
              <span class="tag ok" v-if="l.status==='owned'">已认领｜{{ l.owner_name }}｜{{ fmt(l.claimed_at,false) }}</span>
              <span class="tag" v-else>公海待认领</span>
            </template>
            <span class="tag warn" v-if="l.status==='pool' && l.last_owner_name">曾被 {{ l.last_owner_name }} 认领 · {{ RELEASE[l.last_release_type] }}</span>
            <span class="faint" v-if="mode==='mine'">更新于 {{ ago(l.last_effective_at) }}</span>
            <button v-if="l.status==='pool'" class="btn accent sm act" @click="claim(l,$event)">认领</button>
          </div>
        </div>
      </div>
      <div v-if="!s.loading && !s.items.length" class="empty">{{ mode==='mine' ? '个人池暂无线索，去公海认领吧' : '暂无线索' }}<div v-if="mode==='mine'" style="margin-top:10px"><a class="btn primary" href="#/pool">去公海</a></div></div>
      <div class="more" v-if="s.items.length < s.total"><button class="btn" :disabled="s.loading" @click="s.page++;load(false)">{{ s.loading ? '加载中…' : '加载更多' }}</button></div>
      <div class="more faint" v-if="s.loading && !s.items.length">加载中…</div>
    </div>`,
  };

  // ======================= 组件：线索详情 =======================
  const LeadDetail = {
    props: { id: String },
    components: { VoicePanel, LeadFields },
    setup(props) {
      const s = reactive({ lead: null, err: '', editing: false, form: null, hl: {}, saving: false, logsOpen: false });
      const fu = reactive({ content: '', next_follow_at: '', stage: '', saving: false });
      async function load() {
        try { s.lead = await api('GET', '/leads/' + props.id, undefined, { cache: true, silent: true }); s.err = ''; }
        catch (e) { s.err = e.message; }
      }
      onMounted(load);
      function edit() { s.form = Object.fromEntries(FORM_KEYS.map((k) => [k, s.lead[k] ?? null])); s.hl = {}; s.editing = true; }
      async function save(via) {
        if (!s.form.company_name || !s.form.contact_name) { toast('公司全称、对接人姓名为必填'); return; }
        s.saving = true;
        try {
          const r = await api('PUT', '/leads/' + props.id, { ...s.form, _via: via });
          s.lead = r.lead; s.editing = false;
          toast(r.changed ? '已保存，回流倒计时已重置' : '内容无变化');
        } catch (_) { /* toast 已提示 */ } finally { s.saving = false; }
      }
      function onVoice({ fields, followup }) {
        if (!s.editing) edit();
        Object.assign(s.form, fields);
        s.hl = Object.fromEntries(Object.keys(fields).map((k) => [k, true]));
        if (followup) { fu.content = followup.content; if (followup.next_follow_at) fu.next_follow_at = toLocalInput(followup.next_follow_at); }
        if (fields.stage) fu.stage = fields.stage;
        toast('已填入，请核对后点击保存');
      }
      async function addFollow() {
        if (!fu.content.trim()) { toast('请填写跟进内容'); return; }
        const body = { content: fu.content.trim(), stage: fu.stage || undefined, next_follow_at: fu.next_follow_at ? new Date(fu.next_follow_at).toISOString() : undefined, follow_at: new Date().toISOString() };
        fu.saving = true;
        try {
          s.lead = await api('POST', `/leads/${props.id}/followups`, body, { silent: true });
          fu.content = ''; fu.next_follow_at = ''; fu.stage = '';
          toast('跟进已记录，回流倒计时已重置');
        } catch (e) {
          if (e.network) {
            store.queue.push({ leadId: props.id, company: s.lead.company_name, body });
            LS.set('offlineQueue', store.queue);
            fu.content = ''; fu.next_follow_at = ''; fu.stage = '';
            toast('当前离线，跟进已暂存，联网后自动同步', 3500);
          } else toast(e.message);
        } finally { fu.saving = false; }
      }
      async function claim() {
        if (!await ask({ title: '认领线索', message: `认领后 ${store.meta.settings.recycle_days} 天内无有效跟进将自动回流公海。`, okText: '认领' })) return;
        try { s.lead = await api('POST', `/leads/${props.id}/claim`); toast('认领成功'); } catch (e) { if (e.status === 409) load(); }
      }
      async function release() {
        const note = await ask({ title: '退回公海', message: '退回后其他销售可认领该线索，你的跟进记录会随线索保留。', input: { label: '退回原因', placeholder: '如：客户暂无预算 / 联系方式无效', required: true }, okText: '确认退回', danger: true });
        if (!note) return;
        try { await api('POST', `/leads/${props.id}/release`, { note }); toast('已退回公海'); go('/mine'); } catch (_) { /* toast */ }
      }
      async function recycle() {
        const note = await ask({ title: '强制回收', message: `将「${s.lead.company_name}」从 ${s.lead.owner_name} 的个人池回收至公海。`, input: { label: '回收原因', placeholder: '可选' }, okText: '强制回收', danger: true });
        if (note === false) return;
        try { await api('POST', `/leads/${props.id}/recycle`, { note }); toast('已回收至公海'); load(); } catch (_) { /* toast */ }
      }
      const cd = computed(() => {
        const l = s.lead;
        if (!l || l.status !== 'owned') return null;
        const warn = store.meta.settings.warn_days;
        return { cls: l.days_left <= 2 ? 'danger' : l.days_left <= warn ? 'warn' : '', text: `距自动回流还剩 ${Math.max(0, l.days_left)} 天（${fmt(l.expire_at)}）` };
      });
      const pendingQ = computed(() => store.queue.filter((q) => String(q.leadId) === String(props.id)));
      const logText = (g) => {
        const d = g.detail || {};
        if (d.changes) return Object.entries(d.changes).map(([k, v]) => `${k}：${v.from ?? '空'} → ${v.to ?? '空'}`).join('；');
        if (d.note) return d.note;
        if (d.fields) return '识别字段：' + d.fields.map((k) => FIELD_NAMES[k] || k).join('、');
        return '';
      };
      return { s, fu, store, edit, save, onVoice, addFollow, claim, release, recycle, cd, fmt, ago, RELEASE, ACTION, logText, isManager, pendingQ };
    },
    template: `
    <div v-if="s.err" class="empty">{{ s.err }}<div style="margin-top:10px"><a class="btn" href="#/pool">返回公海</a></div></div>
    <div v-else-if="!s.lead" class="empty">加载中…</div>
    <div v-else>
      <div class="card">
        <div class="row between" style="align-items:flex-start">
          <div class="grow"><div style="font-size:18px;font-weight:700;word-break:break-all">{{ s.lead.company_name }}</div>
            <div class="row" style="margin-top:6px">
              <span class="tag ok" v-if="s.lead.status==='owned'">已认领｜{{ s.lead.owner_name }}｜{{ fmt(s.lead.claimed_at) }}</span>
              <span class="tag" v-else>公海待认领</span>
              <span class="tag info" v-if="s.lead.industry_name">{{ s.lead.industry_name }}</span>
              <span class="tag accent" v-if="s.lead.intent_level">{{ s.lead.intent_level }}</span>
              <span class="tag" v-if="s.lead.stage">{{ s.lead.stage }}</span>
            </div>
          </div>
        </div>
        <div v-if="cd" class="countdown" :class="cd.cls" style="margin-top:10px">⏱ {{ cd.text }}<span v-if="s.lead.can_edit"> · 修改信息或新增跟进即可重置</span></div>
        <div v-if="s.lead.status==='pool' && s.lead.last_owner_name" class="countdown warn" style="margin-top:10px">
          曾被 {{ s.lead.last_owner_name }} 认领过（累计认领 {{ s.lead.claim_count }} 次），{{ fmt(s.lead.last_released_at) }} {{ RELEASE[s.lead.last_release_type] }}；历史跟进已归档，认领后可查看
        </div>
        <div class="faint" style="margin-top:8px">上次有效更新：{{ fmt(s.lead.last_effective_at) }} · 录入：{{ fmt(s.lead.created_at) }}</div>
      </div>

      <div v-if="s.lead.status==='pool'" class="card">
        <button class="btn accent block" @click="claim">认领到我的个人池</button>
        <div class="faint" style="margin-top:6px" v-if="s.lead.masked">认领后可查看完整联系方式</div>
      </div>

      <div class="card">
        <h3>客户信息<span class="sp"></span><button v-if="s.lead.can_edit && !s.editing" class="btn sm" @click="edit">编辑</button></h3>
        <template v-if="s.editing">
          <voice-panel :lead-id="s.lead.id" @apply="onVoice" style="margin-bottom:12px"></voice-panel>
          <lead-fields :form="s.form" :hl="s.hl"></lead-fields>
          <div class="row" style="margin-top:12px"><button class="btn primary" :disabled="s.saving" @click="save(Object.keys(s.hl).length?'voice':'form')">{{ s.saving ? '保存中…' : '保存' }}</button><button class="btn" @click="s.editing=false">取消</button></div>
        </template>
        <dl v-else class="kv">
          <dt>对接人</dt><dd>{{ s.lead.contact_name }}</dd>
          <dt>部门 / 岗位</dt><dd>{{ s.lead.contact_dept || '—' }} / {{ s.lead.contact_title || '—' }}</dd>
          <dt>联系电话</dt><dd><a v-if="s.lead.contact_phone && !s.lead.masked" :href="'tel:'+s.lead.contact_phone">{{ s.lead.contact_phone }}</a><span v-else>{{ s.lead.contact_phone || '—' }}</span></dd>
          <dt>微信</dt><dd>{{ s.lead.contact_wechat || '—' }}</dd>
          <dt>办公地址</dt><dd>{{ s.lead.address || '—' }}</dd>
          <dt>线索来源</dt><dd>{{ s.lead.source || '—' }}</dd>
          <dt v-if="s.lead.remark">备注</dt><dd v-if="s.lead.remark" style="white-space:pre-wrap">{{ s.lead.remark }}</dd>
        </dl>
      </div>

      <div v-if="s.lead.can_edit" class="card">
        <h3>新增跟进</h3>
        <voice-panel v-if="!s.editing" :lead-id="s.lead.id" @apply="onVoice" style="margin-bottom:10px"></voice-panel>
        <div class="field"><label>跟进内容</label><textarea class="input" v-model="fu.content" placeholder="本次沟通内容、客户反馈…"></textarea></div>
        <div class="grid2" style="margin-top:8px">
          <div class="field"><label>更新阶段</label><select class="input" v-model="fu.stage"><option value="">不变（{{ s.lead.stage || '未设置' }}）</option><option v-for="o in store.meta.stages" :key="o.id" :value="o.value">{{ o.value }}</option></select></div>
          <div class="field"><label>下次跟进时间</label><input class="input" type="datetime-local" v-model="fu.next_follow_at"></div>
        </div>
        <button class="btn primary" style="margin-top:10px" :disabled="fu.saving" @click="addFollow">{{ fu.saving ? '提交中…' : '提交跟进' }}</button>
        <div v-if="pendingQ.length" class="faint" style="color:var(--warn);margin-top:6px">有 {{ pendingQ.length }} 条离线跟进待同步</div>
      </div>

      <div class="card" v-if="s.lead.followups.length">
        <h3>跟进记录 <span class="faint">{{ s.lead.followups.length }} 条</span></h3>
        <ul class="timeline">
          <li v-for="f in s.lead.followups" :key="f.id">
            <div class="h">{{ fmt(f.follow_at) }} · {{ f.user_name }}<span v-if="f.stage"> · 阶段→{{ f.stage }}</span><span v-if="f.user_id !== s.lead.owner_id" class="tag" style="margin-left:6px">历史归档</span></div>
            <div class="c">{{ f.content }}</div>
            <div class="faint" v-if="f.next_follow_at">下次跟进：{{ fmt(f.next_follow_at) }}</div>
          </li>
        </ul>
      </div>

      <div class="card" v-if="s.lead.claim_history.length">
        <h3>认领历史</h3>
        <ul class="timeline">
          <li v-for="(c,i) in s.lead.claim_history" :key="i" :class="{bad: c.end_reason==='auto_recycle' || c.end_reason==='force_recycle'}">
            <div class="h">{{ fmt(c.claimed_at) }} 认领 → {{ c.ended_at ? fmt(c.ended_at) + ' ' + RELEASE[c.end_reason] : '至今' }}</div>
            <div>{{ c.user_name }}<span v-if="c.end_note" class="faint">（{{ c.end_note }}）</span></div>
          </li>
        </ul>
      </div>

      <div class="card" v-if="s.lead.logs.length">
        <h3>操作日志<span class="sp"></span><button class="btn ghost sm" @click="s.logsOpen=!s.logsOpen">{{ s.logsOpen ? '收起' : '展开 ' + s.lead.logs.length + ' 条' }}</button></h3>
        <ul class="timeline" v-if="s.logsOpen">
          <li v-for="g in s.lead.logs" :key="g.id" :class="{sys: !g.actor_id, bad: g.action==='auto_recycle'||g.action==='force_recycle'}">
            <div class="h">{{ fmt(g.created_at) }} · {{ g.actor_name }}</div>
            <div>{{ ACTION[g.action] || g.action }} <span class="faint">{{ logText(g) }}</span></div>
          </li>
        </ul>
      </div>

      <div class="sticky-actions" v-if="s.lead.status==='owned' && (s.lead.can_edit || isManager())">
        <button v-if="s.lead.can_edit" class="btn danger" @click="release">退回公海</button>
        <button v-if="isManager() && !s.lead.can_edit" class="btn danger" @click="recycle">强制回收至公海</button>
      </div>
    </div>`,
  };

  // ======================= 组件：新增线索 =======================
  const LeadForm = {
    components: { VoicePanel, LeadFields },
    setup() {
      const form = reactive(emptyForm());
      const s = reactive({ hl: {}, claim: !isManager(), saving: false, dup: null, followup: null });
      async function checkDup() {
        if (!form.company_name) { s.dup = null; return; }
        try { const r = await api('POST', '/leads/check-duplicate', { company_name: form.company_name }, { silent: true }); s.dup = r.duplicate ? r : null; } catch (_) { /* ignore */ }
      }
      function onVoice({ fields, followup }) {
        Object.assign(form, fields); s.hl = Object.fromEntries(Object.keys(fields).map((k) => [k, true])); s.followup = followup;
        checkDup();
      }
      async function submit() {
        if (!form.company_name || !form.contact_name) { toast('公司全称、对接人姓名为必填'); return; }
        s.saving = true;
        try {
          const lead = await api('POST', '/leads', { ...form, claim: s.claim }, { silent: true });
          if (s.claim && s.followup && s.followup.content) {
            await api('POST', `/leads/${lead.id}/followups`, { content: s.followup.content, next_follow_at: s.followup.next_follow_at || undefined, via: 'voice' }).catch(() => {});
          }
          toast(s.claim ? '已录入并认领到个人池' : '已录入公海');
          go('/lead/' + lead.id);
        } catch (e) {
          toast(e.message, 3000);
          if (e.data && e.data.duplicate_id) s.dup = { id: e.data.duplicate_id };
        } finally { s.saving = false; }
      }
      return { form, s, submit, onVoice, checkDup, isManager };
    },
    template: `
    <div>
      <div class="card"><voice-panel @apply="onVoice"></voice-panel></div>
      <div class="card">
        <lead-fields :form="form" :hl="s.hl" @focusout="checkDup"></lead-fields>
        <div v-if="s.dup" class="countdown warn" style="margin-top:10px">该公司已存在：{{ s.dup.status==='pool' ? '在公海中' : s.dup.mine ? '在你的个人池中' : '已被其他销售跟进' }}<a v-if="s.dup.id" :href="'#/lead/'+s.dup.id" style="margin-left:8px">查看</a></div>
        <div v-if="s.followup" class="faint" style="margin-top:8px">语音识别的跟进内容将在认领后自动记录：{{ s.followup.content }}</div>
        <label class="switch"><input type="checkbox" v-model="s.claim"> 录入后直接认领到我的个人池</label>
        <button class="btn primary block" :disabled="s.saving" @click="submit">{{ s.saving ? '提交中…' : (s.claim ? '录入并认领' : '录入到公海') }}</button>
      </div>
    </div>`,
  };

  // ======================= 组件：总监看板 =======================
  const DashboardView = {
    setup() {
      const s = reactive({ tab: 'overview', d: null, warn: null, flows: null, flowAction: '' });
      async function load() {
        try { s.d = await api('GET', '/dashboard/summary', undefined, { cache: true }); } catch (_) { /* toast */ }
      }
      async function loadWarn() { try { s.warn = await api('GET', '/dashboard/warnings', undefined, { cache: true }); } catch (_) { /* toast */ } }
      async function loadFlows() { try { s.flows = await api('GET', '/dashboard/flows' + qs({ action: s.flowAction, size: 200 }), undefined, { cache: true }); } catch (_) { /* toast */ } }
      onMounted(() => { load(); loadWarn(); });
      watch(() => s.tab, (t) => { if (t === 'flows' && !s.flows) loadFlows(); });
      watch(() => s.flowAction, loadFlows);
      async function recycle(l) {
        const note = await ask({ title: '强制回收', message: `将「${l.company_name}」从 ${l.owner_name} 处回收至公海。`, input: { label: '回收原因', placeholder: '可选' }, okText: '强制回收', danger: true });
        if (note === false) return;
        try { await api('POST', `/leads/${l.id}/recycle`, { note }); toast('已回收'); loadWarn(); load(); } catch (_) { /* toast */ }
      }
      const maxInd = computed(() => Math.max(1, ...((s.d && s.d.byIndustry) || []).map((x) => x.total)));
      return { s, recycle, maxInd, download, fmt, ACTION, store };
    },
    template: `
    <div>
      <div class="tabs">
        <button :class="{on:s.tab==='overview'}" @click="s.tab='overview'">总览</button>
        <button :class="{on:s.tab==='warn'}" @click="s.tab='warn'">到期预警 <span class="tag warn" v-if="s.warn">{{ s.warn.total }}</span></button>
        <button :class="{on:s.tab==='flows'}" @click="s.tab='flows'">流转台账</button>
        <button :class="{on:s.tab==='export'}" @click="s.tab='export'">导出报表</button>
      </div>
      <template v-if="s.tab==='overview'">
        <div v-if="!s.d" class="empty">加载中…</div>
        <template v-else>
          <div class="kpis">
            <div class="kpi"><div class="v">{{ s.d.totals.pool || 0 }}</div><div class="l">公海存量</div></div>
            <div class="kpi"><div class="v">{{ s.d.totals.owned || 0 }}</div><div class="l">私池跟进中</div></div>
            <div class="kpi warn"><div class="v">{{ s.d.totals.expiring || 0 }}</div><div class="l">{{ s.d.settings.warn_days }} 天内将回流</div></div>
            <div class="kpi"><div class="v">{{ s.d.totals.new_this_week || 0 }}</div><div class="l">本周新增线索</div></div>
            <div class="kpi"><div class="v">{{ s.d.totals.won || 0 }}</div><div class="l">已成交</div></div>
          </div>
          <div class="card">
            <h3>本周流转</h3>
            <div class="row" style="gap:18px">
              <span>认领 <b>{{ s.d.flowWeek.claim || 0 }}</b></span><span>主动退回 <b>{{ s.d.flowWeek.release || 0 }}</b></span>
              <span>超时回流 <b style="color:var(--danger)">{{ s.d.flowWeek.auto_recycle || 0 }}</b></span><span>强制回收 <b>{{ s.d.flowWeek.force_recycle || 0 }}</b></span>
              <span>跟进 <b>{{ s.d.flowWeek.followup || 0 }}</b></span>
            </div>
          </div>
          <div class="card">
            <h3>各销售线索情况 <span class="faint">本周自周一起算</span></h3>
            <div class="tablewrap"><table class="tbl">
              <thead><tr><th>销售</th><th class="n">持有</th><th class="n">本周认领</th><th class="n">本周退回</th><th class="n">即将回流</th><th class="n">累计被回收</th><th class="n">已成交</th><th class="n">本周跟进</th><th class="n">近30天跟进</th><th></th></tr></thead>
              <tbody><tr v-for="p in s.d.perSales" :key="p.user_id" :style="{opacity: p.active ? 1 : .5}">
                <td>{{ p.name }} <span class="tag" v-if="p.role==='director'">总监</span><span class="tag" v-if="!p.active">停用</span></td>
                <td class="n">{{ p.holding }}</td><td class="n">{{ p.claimed_week }}</td><td class="n">{{ p.released_week }}</td>
                <td class="n" :style="{color: p.expiring ? 'var(--warn)' : ''}">{{ p.expiring }}</td><td class="n">{{ p.recycled_total }}</td>
                <td class="n">{{ p.won }}</td><td class="n">{{ p.followups_week }}</td><td class="n">{{ p.followups_30d }}</td>
                <td><a :href="'#/ledger?status=owned&owner_id='+p.user_id">查看线索</a></td>
              </tr></tbody>
            </table></div>
          </div>
          <div class="card bars">
            <h3>按行业分布</h3>
            <div class="legend"><span><i style="background:var(--navy-2)"></i>公海</span><span><i style="background:var(--accent)"></i>私池</span></div>
            <div class="b" v-for="x in s.d.byIndustry" :key="x.name">
              <span>{{ x.name }}</span>
              <div class="track"><div class="seg1" :style="{width: (x.pool/maxInd*100)+'%'}"></div><div class="seg2" :style="{width: (x.owned/maxInd*100)+'%'}"></div></div>
              <span class="faint" style="text-align:right">{{ x.total }}</span>
            </div>
          </div>
        </template>
      </template>
      <template v-if="s.tab==='warn'">
        <div v-if="!s.warn" class="empty">加载中…</div>
        <div v-else-if="!s.warn.items.length" class="empty">暂无即将回流的线索 👍</div>
        <div class="listgrid" v-else>
          <div v-for="l in s.warn.items" :key="l.id" class="lead expiring" :class="{overdue: l.days_left<=1}">
            <div class="t"><a class="name" :href="'#/lead/'+l.id">{{ l.company_name }}</a><span class="days" style="color:var(--warn)">{{ l.days_left > 0 ? l.days_left + '天' : '即将回流' }}</span></div>
            <div class="meta"><span>持有人：{{ l.owner_name }}</span><span>上次更新：{{ fmt(l.last_effective_at) }}</span></div>
            <div class="tags"><span class="tag info" v-if="l.industry_name">{{ l.industry_name }}</span><span class="tag" v-if="l.stage">{{ l.stage }}</span><button class="btn danger sm act" @click="recycle(l)">强制回收</button></div>
          </div>
        </div>
      </template>
      <template v-if="s.tab==='flows'">
        <div class="filters"><select class="input" v-model="s.flowAction"><option value="">全部动作</option><option value="claim">认领</option><option value="release">主动退回</option><option value="auto_recycle">超时自动回流</option><option value="force_recycle">强制回收</option><option value="create">录入</option><option value="import">导入</option></select></div>
        <div class="card" v-if="s.flows">
          <div class="tablewrap"><table class="tbl">
            <thead><tr><th>时间</th><th>动作</th><th>线索</th><th>归属销售</th><th>操作人</th><th>说明</th></tr></thead>
            <tbody><tr v-for="g in s.flows.items" :key="g.id">
              <td>{{ fmt(g.created_at) }}</td><td><span class="tag" :class="{danger:g.action==='auto_recycle', warn:g.action==='force_recycle', ok:g.action==='claim'}">{{ ACTION[g.action] }}</span></td>
              <td><a :href="'#/lead/'+g.lead_id">{{ g.company_name }}</a></td><td>{{ g.owner_name || '—' }}</td><td>{{ g.actor_name }}</td><td class="faint">{{ (g.detail && g.detail.note) || '' }}</td>
            </tr></tbody>
          </table></div>
          <div class="faint" style="margin-top:8px">共 {{ s.flows.total }} 条，显示最近 {{ s.flows.items.length }} 条</div>
        </div>
      </template>
      <template v-if="s.tab==='export'">
        <div class="card">
          <h3>导出报表（CSV，可用 Excel 打开）</h3>
          <div class="row" style="gap:10px">
            <button class="btn" @click="download('/export/leads?scope=pool','公海线索.csv')">公海存量</button>
            <button class="btn" @click="download('/export/leads?scope=owned','私池线索.csv')">各销售私池线索</button>
            <button class="btn" @click="download('/export/leads?scope=expiring','即将回流.csv')">即将回流线索</button>
            <button class="btn" @click="download('/export/leads?scope=recycled','已回流线索.csv')">已回流线索</button>
            <button class="btn" @click="download('/export/leads?scope=won','成交线索.csv')">成交线索</button>
            <button class="btn" @click="download('/export/leads?scope=all','全部线索台账.csv')">全部台账</button>
            <button class="btn primary" @click="download('/export/sales-stats','销售统计.csv')">销售统计</button>
          </div>
          <div class="faint" style="margin-top:10px">每次导出都会记录在操作日志中。普通销售账号无导出权限。</div>
        </div>
      </template>
    </div>`,
  };

  // ======================= 组件：批量导入 =======================
  const ImportView = {
    setup() {
      const s = reactive({ text: '', name: '', rows: 0, loading: false, result: null });
      function decode(buf) {
        const utf8 = new TextDecoder('utf-8').decode(buf);
        if (!utf8.includes('�')) return utf8;
        try { return new TextDecoder('gbk').decode(buf); } catch (_) { return utf8; } // Excel 另存的 CSV 常为 GBK 编码
      }
      async function pick(e) {
        const file = e.target.files[0];
        if (!file) return;
        if (!/\.(csv|txt)$/i.test(file.name)) { toast('请上传 CSV 文件（Excel 可“另存为 CSV”）'); return; }
        s.name = file.name; s.text = decode(await file.arrayBuffer());
        s.rows = s.text.split(/\r?\n/).filter((x) => x.trim()).length - 1; s.result = null;
      }
      async function submit() {
        s.loading = true;
        try { s.result = await api('POST', '/leads/import', s.text); toast(`成功导入 ${s.result.created} 条`); } catch (_) { /* toast */ } finally { s.loading = false; }
      }
      return { s, pick, submit, download };
    },
    template: `
    <div class="card">
      <h3>批量导入线索到公海</h3>
      <ol class="muted" style="padding-left:18px;margin:0 0 12px">
        <li>下载导入模板，按列填写（公司全称、对接人姓名必填；行业需与系统行业标签一致）</li>
        <li>在 Excel 中“另存为 CSV”，然后上传</li>
        <li>同名公司会自动跳过，避免重复</li>
      </ol>
      <div class="row"><button class="btn" @click="download('/leads/import/template','线索导入模板.csv')">下载模板</button>
        <label class="btn primary">选择 CSV 文件<input type="file" accept=".csv,.txt,text/csv" @change="pick" style="display:none"></label></div>
      <div v-if="s.name" style="margin-top:12px">已选择 <b>{{ s.name }}</b>，约 {{ s.rows }} 行数据
        <button class="btn accent" style="margin-left:8px" :disabled="s.loading" @click="submit">{{ s.loading ? '导入中…' : '开始导入' }}</button></div>
      <div v-if="s.result" style="margin-top:14px">
        <div class="row" style="gap:16px"><span>成功 <b style="color:var(--ok)">{{ s.result.created }}</b></span><span>重复跳过 <b>{{ s.result.skipped.length }}</b></span><span>失败 <b style="color:var(--danger)">{{ s.result.errors.length }}</b></span></div>
        <div class="tablewrap" v-if="s.result.errors.length || s.result.skipped.length" style="margin-top:8px"><table class="tbl"><thead><tr><th>行号</th><th>说明</th></tr></thead>
          <tbody><tr v-for="e in s.result.errors.concat(s.result.skipped)" :key="e.row"><td>{{ e.row }}</td><td>{{ e.company ? e.company + '：' : '' }}{{ e.message }}</td></tr></tbody></table></div>
      </div>
    </div>`,
  };

  // ======================= 组件：系统设置（超级管理员） =======================
  const AdminView = {
    setup() {
      const s = reactive({ tab: 'users', users: [], options: [], settings: null, logs: [], nu: { username: '', name: '', role: 'sales', phone: '', password: '' }, newInd: '', newOpt: { stage: '', intent: '', source: '' } });
      const load = {
        async users() { s.users = await api('GET', '/admin/users'); },
        async options() { s.options = await api('GET', '/admin/options'); await loadMeta(); },
        async settings() { s.settings = await api('GET', '/admin/settings'); },
        async logs() { s.logs = await api('GET', '/admin/logs?size=200'); },
      };
      const safe = (fn) => async (...a) => { try { await fn(...a); } catch (_) { /* toast 已提示 */ } };
      onMounted(safe(() => load.users()));
      watch(() => s.tab, safe(async (t) => { if (t === 'users') await load.users(); if (t === 'dict') { await load.options(); } if (t === 'settings') await load.settings(); if (t === 'logs') await load.logs(); }));
      const addUser = safe(async () => {
        await api('POST', '/admin/users', s.nu);
        toast('账号已创建'); s.nu = { username: '', name: '', role: 'sales', phone: '', password: '' }; await load.users(); await loadMeta();
      });
      const toggleUser = safe(async (u) => {
        if (u.active && !await ask({ title: '停用账号', message: `停用「${u.name}」后其无法登录，名下 ${u.holding} 条线索将全部回收至公海。`, okText: '停用', danger: true })) return;
        const r = await api('PUT', `/admin/users/${u.id}`, { active: !u.active });
        toast(u.active ? `已停用，回收线索 ${r.recycled} 条` : '已启用'); await load.users(); await loadMeta();
      });
      const changeRole = safe(async (u, role) => { await api('PUT', `/admin/users/${u.id}`, { role }); toast('角色已更新'); await load.users(); await loadMeta(); });
      const resetPwd = safe(async (u) => {
        const pwd = await ask({ title: `重置「${u.name}」的密码`, input: { label: '新密码（至少 8 位）', required: true, type: 'password' }, okText: '重置' });
        if (!pwd) return;
        await api('POST', `/admin/users/${u.id}/reset-password`, { password: pwd }); toast('密码已重置，对方需重新登录');
      });
      const addInd = safe(async () => { if (!s.newInd.trim()) return; await api('POST', '/admin/industries', { name: s.newInd.trim() }); s.newInd = ''; await loadMeta(); toast('已添加'); });
      const renameInd = safe(async (i) => { const name = await ask({ title: '修改行业名称', input: { label: '名称', value: i.name, required: true } }); if (!name) return; await api('PUT', `/admin/industries/${i.id}`, { name }); await loadMeta(); });
      const toggleInd = safe(async (i) => { await api('PUT', `/admin/industries/${i.id}`, { active: !i.active }); await loadMeta(); });
      const delInd = safe(async (i) => { if (!await ask({ title: '删除行业标签', message: `删除「${i.name}」？若已有线索使用，将改为停用。`, danger: true, okText: '删除' })) return; const r = await api('DELETE', `/admin/industries/${i.id}`); toast(r.message || '已删除'); await loadMeta(); });
      const addOpt = safe(async (type) => { const v = s.newOpt[type].trim(); if (!v) return; await api('POST', '/admin/options', { type, value: v }); s.newOpt[type] = ''; await load.options(); });
      const renameOpt = safe(async (o) => { const v = await ask({ title: '修改选项', message: '改名会同步更新已使用该选项的线索。', input: { label: '名称', value: o.value, required: true } }); if (!v) return; await api('PUT', `/admin/options/${o.id}`, { value: v }); await load.options(); });
      const patchOpt = safe(async (o, body) => { await api('PUT', `/admin/options/${o.id}`, body); await load.options(); });
      const delOpt = safe(async (o) => { if (!await ask({ title: '删除选项', message: `删除「${o.value}」？若已被使用将改为停用。`, danger: true, okText: '删除' })) return; const r = await api('DELETE', `/admin/options/${o.id}`); toast(r.message || '已删除'); await load.options(); });
      const saveSettings = safe(async () => { s.settings = await api('PUT', '/admin/settings', s.settings); await loadMeta(); toast('配置已保存'); });
      const runRecycle = safe(async () => { const r = await api('POST', '/admin/recycle/run'); toast(`扫描完成，回流 ${r.recycled} 条`); });
      const optsOf = (type) => s.options.filter((o) => o.type === type);
      return { s, store, ROLE, ACTION, fmt, addUser, toggleUser, changeRole, resetPwd, addInd, renameInd, toggleInd, delInd, addOpt, renameOpt, patchOpt, delOpt, saveSettings, runRecycle, optsOf };
    },
    template: `
    <div>
      <div class="tabs">
        <button :class="{on:s.tab==='users'}" @click="s.tab='users'">账号管理</button>
        <button :class="{on:s.tab==='dict'}" @click="s.tab='dict'">行业与选项</button>
        <button :class="{on:s.tab==='settings'}" @click="s.tab='settings'">回流与权限</button>
        <button :class="{on:s.tab==='logs'}" @click="s.tab='logs'">系统日志</button>
      </div>
      <template v-if="s.tab==='users'">
        <div class="card">
          <h3>新增账号</h3>
          <div class="grid2">
            <div class="field"><label>登录账号</label><input class="input" v-model.trim="s.nu.username" placeholder="字母/数字，如 sales08" autocapitalize="off"></div>
            <div class="field"><label>姓名</label><input class="input" v-model.trim="s.nu.name"></div>
            <div class="field"><label>角色</label><select class="input" v-model="s.nu.role"><option value="sales">普通销售</option><option value="director">销售总监</option><option value="admin">超级管理员</option></select></div>
            <div class="field"><label>手机号</label><input class="input" v-model.trim="s.nu.phone"></div>
            <div class="field"><label>初始密码（至少 8 位）</label><input class="input" type="password" v-model="s.nu.password" autocomplete="new-password"></div>
          </div>
          <button class="btn primary" style="margin-top:10px" @click="addUser">创建账号</button>
        </div>
        <div class="card"><div class="tablewrap"><table class="tbl">
          <thead><tr><th>姓名</th><th>账号</th><th>角色</th><th class="n">持有线索</th><th>状态</th><th>操作</th></tr></thead>
          <tbody><tr v-for="u in s.users" :key="u.id">
            <td>{{ u.name }}</td><td>{{ u.username }}</td>
            <td><select class="input" style="min-height:30px;padding:2px 6px;width:auto" :value="u.role" @change="changeRole(u,$event.target.value)" :disabled="u.id===store.user.id"><option value="sales">普通销售</option><option value="director">销售总监</option><option value="admin">超级管理员</option></select></td>
            <td class="n">{{ u.holding }}</td>
            <td><span class="tag" :class="u.active?'ok':''">{{ u.active ? '正常' : '已停用' }}</span></td>
            <td><button class="btn sm" @click="resetPwd(u)">重置密码</button> <button v-if="u.id!==store.user.id" class="btn sm" :class="{danger:u.active}" @click="toggleUser(u)">{{ u.active ? '停用' : '启用' }}</button></td>
          </tr></tbody>
        </table></div></div>
      </template>
      <template v-if="s.tab==='dict'">
        <div class="card">
          <h3>行业标签</h3>
          <div class="row" v-for="i in store.meta.all_industries" :key="i.id" style="padding:6px 0;border-bottom:1px dashed var(--line)">
            <span class="grow" :style="{opacity:i.active?1:.45}">{{ i.name }}<span v-if="!i.active" class="faint">（已停用）</span></span>
            <button class="btn sm" @click="renameInd(i)">改名</button><button class="btn sm" @click="toggleInd(i)">{{ i.active ? '停用' : '启用' }}</button><button class="btn sm danger" @click="delInd(i)">删除</button>
          </div>
          <div class="row" style="margin-top:10px"><input class="input grow" v-model="s.newInd" placeholder="新行业名称，如：房地产" @keyup.enter="addInd"><button class="btn primary" @click="addInd">添加</button></div>
        </div>
        <div class="card" v-for="t in [['stage','跟进阶段'],['intent','意向等级'],['source','线索来源']]" :key="t[0]">
          <h3>{{ t[1] }}</h3>
          <div class="row" v-for="o in optsOf(t[0])" :key="o.id" style="padding:6px 0;border-bottom:1px dashed var(--line)">
            <span class="grow" :style="{opacity:o.active?1:.45}">{{ o.value }}<span v-if="!o.active" class="faint">（已停用）</span></span>
            <label v-if="t[0]==='stage'" class="faint" style="display:flex;gap:4px;align-items:center"><input type="checkbox" :checked="!!o.is_won" @change="patchOpt(o,{is_won:$event.target.checked})">计为成交</label>
            <button class="btn sm" @click="renameOpt(o)">改名</button><button class="btn sm" @click="patchOpt(o,{active:!o.active})">{{ o.active ? '停用' : '启用' }}</button><button class="btn sm danger" @click="delOpt(o)">删除</button>
          </div>
          <div class="row" style="margin-top:10px"><input class="input grow" v-model="s.newOpt[t[0]]" :placeholder="'新增' + t[1]" @keyup.enter="addOpt(t[0])"><button class="btn primary" @click="addOpt(t[0])">添加</button></div>
        </div>
      </template>
      <template v-if="s.tab==='settings' && s.settings">
        <div class="card">
          <h3>自动回流</h3>
          <div class="grid2">
            <div class="field"><label>回流周期（天）：认领或上次有效更新后满该天数自动回收</label><input class="input" type="number" min="1" max="365" v-model.number="s.settings.recycle_days"></div>
            <div class="field"><label>到期预警（天）：剩余天数小于等于该值时高亮提醒</label><input class="input" type="number" min="0" max="60" v-model.number="s.settings.warn_days"></div>
            <div class="field"><label>个人池上限（条，0 = 不限）</label><input class="input" type="number" min="0" v-model.number="s.settings.sales_max_leads"></div>
          </div>
          <div class="faint" style="margin-top:6px">修改回流周期后，所有私池线索的到期时间会按新周期重新计算。</div>
        </div>
        <div class="card">
          <h3>权限开关</h3>
          <label class="switch"><input type="checkbox" v-model="s.settings.pool_mask_phone"> 公海中对普通销售隐藏完整电话/微信（认领后可见）</label>
          <label class="switch"><input type="checkbox" v-model="s.settings.inherit_history"> 重新认领的销售可查看该线索的历史跟进记录</label>
          <label class="switch"><input type="checkbox" v-model="s.settings.sales_can_create"> 允许普通销售在前端录入新线索</label>
        </div>
        <div class="row"><button class="btn primary" @click="saveSettings">保存配置</button><button class="btn" @click="runRecycle">立即执行一次回流扫描</button></div>
      </template>
      <template v-if="s.tab==='logs'">
        <div class="card"><div class="tablewrap"><table class="tbl">
          <thead><tr><th>时间</th><th>操作人</th><th>动作</th><th>线索</th></tr></thead>
          <tbody><tr v-for="g in s.logs" :key="g.id"><td>{{ fmt(g.created_at) }}</td><td>{{ g.actor_name }}</td><td>{{ ACTION[g.action] || g.action }}</td><td><a v-if="g.lead_id" :href="'#/lead/'+g.lead_id">{{ g.company_name }}</a></td></tr></tbody>
        </table></div><div class="faint" style="margin-top:8px">日志只增不改，任何角色都无法删除。</div></div>
      </template>
    </div>`,
  };

  // ======================= 组件：我的 =======================
  const MeView = {
    setup() {
      const p = reactive({ old_password: '', new_password: '' });
      async function changePwd() {
        try {
          const r = await api('POST', '/auth/password', p);
          store.token = r.token; LS.set('token', r.token); p.old_password = ''; p.new_password = ''; toast('密码已修改');
        } catch (_) { /* toast */ }
      }
      return { store, p, changePwd, logout, ROLE, flushQueue, isManager };
    },
    template: `
    <div>
      <div class="card"><div style="font-size:18px;font-weight:700">{{ store.user.name }}</div><div class="muted">{{ ROLE[store.user.role] }} · {{ store.user.username }}</div></div>
      <div class="card">
        <h3>规则提醒</h3>
        <div class="muted" style="font-size:14px">认领后 <b>{{ store.meta.settings.recycle_days }}</b> 天内没有“有效更新”（修改客户信息 / 新增跟进 / 更新阶段），线索会自动回流公海；剩余不足 <b>{{ store.meta.settings.warn_days }}</b> 天会高亮提醒。</div>
        <div class="faint" style="margin-top:6px">AI 语音识别：{{ store.meta.settings.ai_enabled ? 'Claude AI（失败自动降级为本地规则）' : '本地规则识别' }}</div>
      </div>
      <div class="card" v-if="store.queue.length">
        <h3>离线待同步</h3><div class="muted">{{ store.queue.length }} 条跟进记录等待联网同步</div>
        <button class="btn" style="margin-top:8px" @click="flushQueue">立即同步</button>
      </div>
      <div class="card" v-if="isManager()"><h3>管理入口</h3><div class="row"><a class="btn" href="#/ledger">全员线索台账</a><a class="btn" href="#/import">批量导入</a><a class="btn" v-if="store.user.role==='admin'" href="#/admin">系统设置</a></div></div>
      <div class="card">
        <h3>修改密码</h3>
        <div class="grid2">
          <div class="field"><label>原密码</label><input class="input" type="password" v-model="p.old_password" autocomplete="current-password"></div>
          <div class="field"><label>新密码（至少 8 位）</label><input class="input" type="password" v-model="p.new_password" autocomplete="new-password"></div>
        </div>
        <button class="btn" style="margin-top:10px" @click="changePwd">修改密码</button>
      </div>
      <button class="btn danger block" @click="logout()">退出登录</button>
    </div>`,
  };

  // ======================= 根组件 =======================
  const App = {
    components: { LoginView, LeadList, LeadDetail, LeadForm, DashboardView, ImportView, AdminView, MeView },
    setup() {
      const r = reactive({ view: null, key: 0 });
      function route() {
        if (!store.user) { if (!location.hash.startsWith('#/login')) go('/login'); r.view = parseHash(); return; }
        const v = parseHash();
        if (!v || v.path === '/login' || (v.need === 'manager' && !isManager()) || (v.need === 'admin' && store.user.role !== 'admin')) { go(homePath()); return; }
        store.route = { path: v.path, params: v.props, query: v.query };
        r.view = v; r.key++;
        window.scrollTo(0, 0);
      }
      window.addEventListener('hashchange', route);
      onMounted(async () => {
        if (store.user) {
          await loadMeta();
          api('GET', '/auth/me', undefined, { silent: true }).then((m) => { store.user = m.user; LS.set('user', m.user); }).catch(() => {});
          flushQueue();
        }
        route();
      });
      const nav = computed(() => {
        if (!store.user) return [];
        const items = [
          { to: '/pool', ico: '🌊', label: '公海' },
          { to: '/mine', ico: '📋', label: '我的线索' },
        ];
        if (isManager()) items.push({ to: '/dashboard', ico: '📊', label: '看板' }, { to: '/ledger', ico: '🗂', label: '台账' });
        else if (store.meta && store.meta.settings.sales_can_create) items.push({ to: '/new', ico: '➕', label: '新增' });
        items.push({ to: '/me', ico: '👤', label: '我的' });
        return items;
      });
      const sideExtra = computed(() => {
        if (!isManager()) return [];
        const x = [{ to: '/new', ico: '➕', label: '新增线索' }, { to: '/import', ico: '📥', label: '批量导入' }];
        if (store.user.role === 'admin') x.push({ to: '/admin', ico: '⚙️', label: '系统设置' });
        return x;
      });
      const isTop = computed(() => r.view && ['/pool', '/mine', '/dashboard', '/ledger', '/me', '/admin'].includes(r.view.path));
      function back() { if (history.length > 1) history.back(); else go(homePath()); }
      function closeDialog(ok) {
        const d = store.dialog;
        if (!d) return;
        if (ok && d.input) {
          if (d.input.required && !String(d.value).trim()) { toast('请填写' + d.input.label); return; }
          d.resolve(String(d.value).trim() || (d.input.required ? false : ''));
        } else d.resolve(ok ? true : false);
        store.dialog = null;
      }
      return { store, r, nav, sideExtra, isTop, back, closeDialog, ROLE };
    },
    template: `
    <login-view v-if="!store.user"></login-view>
    <div v-else class="shell">
      <aside class="sidebar">
        <div class="brand"><img src="/icon.svg" alt="">地标传媒 · 线索公海</div>
        <a v-for="n in nav" :key="n.to" :href="'#'+n.to" :class="{on: r.view && r.view.path===n.to}"><span>{{ n.ico }}</span>{{ n.label }}</a>
        <template v-if="sideExtra.length"><div class="grp">管理</div><a v-for="n in sideExtra" :key="n.to" :href="'#'+n.to" :class="{on: r.view && r.view.path===n.to}"><span>{{ n.ico }}</span>{{ n.label }}</a></template>
        <div class="foot">{{ store.user.name }} · {{ ROLE[store.user.role] }}</div>
      </aside>
      <div style="min-width:0">
        <header class="topbar">
          <button v-if="!isTop" class="back" @click="back" aria-label="返回">‹</button>
          <h1>{{ r.view ? r.view.title : '' }}</h1>
          <span class="who">{{ store.user.name }}</span>
        </header>
        <div class="banner" v-if="store.offline">⚠️ 当前离线：显示的是缓存数据<span v-if="store.queue.length">，{{ store.queue.length }} 条跟进待同步</span></div>
        <main class="main" v-if="r.view && store.meta"><component :is="r.view.comp" :key="r.key" v-bind="r.view.props"></component></main>
        <div v-else class="empty">加载中…</div>
      </div>
      <nav class="tabbar"><a v-for="n in nav" :key="n.to" :href="'#'+n.to" :class="{on: r.view && r.view.path===n.to}"><span class="ico">{{ n.ico }}</span>{{ n.label }}</a></nav>
    </div>
    <div class="mask" v-if="store.dialog" @click.self="closeDialog(false)">
      <div class="dialog">
        <h4>{{ store.dialog.title }}</h4>
        <div class="muted" v-if="store.dialog.message" style="font-size:14px">{{ store.dialog.message }}</div>
        <div class="field" v-if="store.dialog.input" style="margin-top:10px"><label>{{ store.dialog.input.label }}<span class="req" v-if="store.dialog.input.required">*</span></label>
          <input class="input" :type="store.dialog.input.type || 'text'" v-model="store.dialog.value" :placeholder="store.dialog.input.placeholder || ''" @keyup.enter="closeDialog(true)" autofocus></div>
        <div class="btns"><button class="btn" @click="closeDialog(false)">取消</button><button class="btn" :class="store.dialog.danger ? 'danger' : 'primary'" @click="closeDialog(true)">{{ store.dialog.okText }}</button></div>
      </div>
    </div>
    <div class="toast" v-if="store.toast">{{ store.toast }}</div>`,
  };

  const app = createApp(App);
  app.config.globalProperties.location = window.location;
  app.component('login-view', LoginView);
  app.mount('#app');

  if ('serviceWorker' in navigator && location.protocol !== 'file:') {
    navigator.serviceWorker.register('/sw.js').catch(() => {});
  }
})();

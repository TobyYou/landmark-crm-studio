'use strict';
/**
 * 语音口述 → 结构化字段。
 * 1. 配置了 ANTHROPIC_API_KEY 时调用 Claude 做语义抽取（结构化 JSON 输出）；
 * 2. 未配置或调用失败时，降级为本地规则抽取（正则 + 关键词），保证离线/内网也可用。
 * 结果只作为“建议填充”，由销售确认后保存，不会直接写库。
 */
const clock = require('./clock');
const { DAY } = clock;

const FIELDS = ['company_name', 'contact_name', 'contact_dept', 'contact_title', 'contact_phone', 'contact_wechat',
  'address', 'industry', 'source', 'intent_level', 'stage'];

const INDUSTRY_KEYWORDS = {
  快消: ['快消', '饮料', '食品', '日化', '零食', '啤酒', '白酒', '乳业', '牛奶', '奶茶', '餐饮', '化妆品', '美妆', '超市'],
  '互联网/网络': ['互联网', '网络', 'APP', 'app', '平台', '电商', '游戏', '软件', '科技公司', '直播', '短视频'],
  汽车: ['汽车', '4S', '车企', '新能源车', '汽贸', '车行', '电动车'],
  金融: ['银行', '证券', '基金', '金融', '信贷', '支付', '理财', '信托'],
  保险: ['保险', '人寿', '财险', '寿险'],
};

const pick = (re, text, g = 1) => { const m = text.match(re); return m ? m[g].trim() : ''; };

function matchOption(options, text, rules) {
  // rules: [[关键词正则, 目标选项中包含的字]]
  for (const [re, hint] of rules) {
    if (re.test(text)) {
      const o = options.find((v) => v.includes(hint));
      if (o) return o;
    }
  }
  return '';
}

function parseDate(text, now) {
  const d = new Date(now);
  d.setHours(10, 0, 0, 0);
  let m;
  if (/大后天/.test(text)) return d.getTime() + 3 * DAY;
  if (/后天/.test(text)) return d.getTime() + 2 * DAY;
  if (/明天/.test(text)) return d.getTime() + DAY;
  if ((m = text.match(/(\d{1,2})\s*天(之)?后/))) return d.getTime() + Number(m[1]) * DAY;
  if ((m = text.match(/下(个)?(周|星期|礼拜)([一二三四五六日天])/))) {
    const map = { 一: 1, 二: 2, 三: 3, 四: 4, 五: 5, 六: 6, 日: 7, 天: 7 };
    const today = (d.getDay() + 6) % 7 + 1;
    return d.getTime() + (7 - today + map[m[3]]) * DAY;
  }
  if (/下(个)?(周|星期|礼拜)/.test(text)) return d.getTime() + 7 * DAY;
  if ((m = text.match(/(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]/))) {
    const t = new Date(d);
    t.setMonth(Number(m[1]) - 1, Number(m[2]));
    if (t.getTime() < now - DAY) t.setFullYear(t.getFullYear() + 1);
    return t.getTime();
  }
  return null;
}

/** 本地规则抽取 */
function extractByRules(text, ctx) {
  const t = String(text).replace(/\s+/g, ' ');
  const f = {};
  f.contact_phone = pick(/(?<!\d)(1[3-9]\d{9})(?!\d)/, t) || pick(/(?<!\d)(0\d{2,3}-?\d{7,8})(?!\d)/, t);
  f.contact_wechat = pick(/(?:微信号?|vx|wx|v信|威信)\s*(?:是|为|号|：|:)?\s*([a-zA-Z][-_a-zA-Z0-9]{5,19})/i, t);
  if (!f.contact_wechat && /微信(就是|同|和)?(手机|电话)(号)?/.test(t) && f.contact_phone) f.contact_wechat = f.contact_phone;

  f.company_name =
    pick(/(?:公司(?:名|名称|全称)?(?:是|叫|为|：|:))\s*([一-龥A-Za-z0-9（）()]{2,40}?(?:有限责任公司|股份有限公司|有限公司|集团|公司|银行|保险|传媒))/, t) ||
    pick(/([一-龥A-Za-z0-9（）()]{2,30}?(?:有限责任公司|股份有限公司|有限公司|集团|分公司|公司|银行|传媒))/, t);
  f.company_name = f.company_name.replace(/^(客户是|客户|这家|一家|来自|是|在|跟|和|对接|拜访了?|今天|刚才)+/, '');

  f.contact_name =
    pick(/(?:对接人|联系人|负责人|姓名|名字)(?:是|叫|为|：|:)?\s*([一-龥]{2,4}?)(?=[，,。\s]|经理|总|主管|女士|先生|$|电话|手机|微信|在|是|负责)/, t) ||
    pick(/(?:叫|找)\s*([一-龥]{2,3}?)(?=[，,。\s]|经理|总|$|电话|手机|微信|是)/, t);
  if (!f.contact_name) {
    const m = t.match(/(?:姓)([一-龥])/) || t.match(/([一-龥])(总|经理|主任|总监)(?![一-龥]{0,1}部)/);
    if (m) f.contact_name = m[1] + (m[2] ? m[2] : '先生/女士');
  }

  f.contact_dept = pick(/(?:部门(?:是|为|：|:)?)\s*([一-龥]{2,10}?(?:部|中心|事业部|组))/, t) ||
    pick(/([一-龥]{2,6}(?:部|中心|事业部))(?:的|负责)/, t);
  f.contact_title = pick(/(?:岗位|职位|职务|title)(?:是|为|：|:)?\s*([一-龥A-Za-z]{2,10})/i, t) ||
    pick(/(市场总监|品牌总监|营销总监|销售总监|总经理|副总经理|品牌经理|市场经理|营销经理|采购经理|媒介经理|广告主管|市场专员|媒介专员|董事长|老板|CEO|CMO|COO)/i, t);

  const addr = pick(/(?:地址|位于|办公地点|办公地址|公司在|在)(?:是|为|：|:)?\s*([一-龥A-Za-z0-9\-#]{3,40}?(?:号楼|号|大厦|广场|中心|园区?|栋|座|层|室|楼))/, t) ||
    pick(/((?:[一-龥]{2,6}[省市])?[一-龥]{2,6}[区县][一-龥A-Za-z0-9]{2,20}(?:路|街|道|大道)[一-龥A-Za-z0-9\-]{0,15}号?)/, t);
  f.address = addr;

  // 行业：先匹配已配置的标签名，再走关键词
  let industry = (ctx.industries || []).find((n) => t.includes(n)) || '';
  if (!industry) {
    for (const [name, kws] of Object.entries(INDUSTRY_KEYWORDS)) {
      if (kws.some((k) => t.includes(k)) && (ctx.industries || []).includes(name)) { industry = name; break; }
    }
  }
  f.industry = industry;

  f.intent_level = matchOption(ctx.intents || [], t, [
    [/没(有)?兴趣|不感兴趣|暂不考虑|低意向|没预算|以后再说/, '低'],
    [/高意向|很感兴趣|非常感兴趣|意向(很)?强|尽快|马上|预算(已经)?批|确定要投|想尽快/, '高'],
    [/中等意向|有点兴趣|考虑一下|了解一下|再看看|对比一下/, '中'],
  ]);
  f.stage = matchOption(ctx.stages || [], t, [
    [/签(了|约|合同)|成交|已经下单|付款了/, '成交'],
    [/不需要|无意向|暂无意向|拒绝/, '无意向'],
    [/报价|要价格|发价格|价目表/, '报价'],
    [/方案|提案|排期|点位/, '方案'],
    [/第一次|初次|首次|刚加上|刚联系/, '初次'],
  ]);
  f.source = matchOption(ctx.sources || [], t, [
    [/渠道\s*A/i, 'A'], [/渠道\s*B/i, 'B'],
    [/线下|展会|地推|扫楼|陌拜|活动现场/, '线下'],
    [/线上|官网|网上|公众号|抖音|小红书/, '线上'],
  ]);

  for (const k of FIELDS) if (!f[k]) delete f[k];
  return f;
}

function isFollowupLike(t) {
  return /(拜访|沟通|聊了|打电话|电话|见面|约了|反馈|跟进|介绍了|报价|方案|下次|明天|后天|下周|回复)/.test(t);
}

/** 调用 Claude 做抽取（结构化输出） */
async function extractByClaude(text, ctx) {
  const Anthropic = require('@anthropic-ai/sdk');
  const client = new Anthropic({ timeout: 30000, maxRetries: 1 });
  const strField = { type: 'string' };
  const enumOr = (arr) => ({ type: 'string', enum: ['', ...arr] });
  const schema = {
    type: 'object',
    additionalProperties: false,
    required: [...FIELDS, 'followup_content', 'next_follow_date'],
    properties: {
      company_name: strField, contact_name: strField, contact_dept: strField, contact_title: strField,
      contact_phone: strField, contact_wechat: strField, address: strField,
      industry: enumOr(ctx.industries), source: enumOr(ctx.sources),
      intent_level: enumOr(ctx.intents), stage: enumOr(ctx.stages),
      followup_content: strField,
      next_follow_date: { type: 'string', description: 'YYYY-MM-DD，未提及则为空字符串' },
    },
  };
  const today = new Date(clock.now()).toLocaleDateString('zh-CN', { year: 'numeric', month: '2-digit', day: '2-digit', weekday: 'long' });
  const response = await client.beta.messages.create({
    model: process.env.AI_MODEL || 'claude-opus-5-5',
    max_tokens: 2000,
    betas: ['server-side-fallback-2026-07-01'],
    fallbacks: 'default',
    output_config: { effort: 'low', format: { type: 'json_schema', schema } },
    system: '你是户外广告公司的销售助理。把销售口述的客户信息拆分到 CRM 字段。只填写口述里明确提到的信息，没有提到的字段一律返回空字符串，不要编造。'
      + '行业、来源、意向等级、跟进阶段只能从给定枚举中选择。followup_content 用一两句话概括本次沟通内容（若口述只是客户资料而没有沟通过程则为空）。'
      + `今天是 ${today}，用于换算“明天”“下周三”等相对日期。`,
    messages: [{ role: 'user', content: `销售口述内容：\n${text}` }],
  });
  if (response.stop_reason === 'refusal') throw new Error('AI 拒绝处理');
  const block = response.content.find((b) => b.type === 'text');
  if (!block) throw new Error('AI 未返回内容');
  const data = JSON.parse(block.text);
  const fields = {};
  for (const k of FIELDS) if (data[k]) fields[k] = String(data[k]).trim();
  let next = null;
  if (/^\d{4}-\d{2}-\d{2}$/.test(data.next_follow_date || '')) next = new Date(data.next_follow_date + 'T10:00:00').getTime();
  return { fields, followup: data.followup_content ? { content: data.followup_content, next_follow_at: next } : null };
}

async function extract(text, ctx) {
  text = String(text || '').trim();
  if (!text) return { engine: 'none', fields: {}, followup: null, missing: ['company_name', 'contact_name'], message: '没有识别到内容，请重新口述或手动填写' };
  if (text.length > 4000) text = text.slice(0, 4000);
  let engine = 'rules';
  let fields;
  let followup = null;
  let warning;
  if (process.env.ANTHROPIC_API_KEY && process.env.AI_DISABLED !== '1') {
    try {
      ({ fields, followup } = await extractByClaude(text, ctx));
      engine = 'claude';
    } catch (e) {
      warning = 'AI 服务暂不可用，已使用本地规则识别：' + (e.message || '').slice(0, 80);
    }
  }
  if (engine === 'rules') {
    fields = extractByRules(text, ctx);
    if (isFollowupLike(text)) followup = { content: text, next_follow_at: parseDate(text, clock.now()) };
  }
  // 行业名称 → id
  if (fields.industry) {
    const ind = (ctx.industryRows || []).find((r) => r.name === fields.industry);
    if (ind) fields.industry_id = ind.id;
    delete fields.industry;
  }
  const missing = ['company_name', 'contact_name'].filter((k) => !fields[k]);
  const recognized = Object.keys(fields).length;
  return {
    engine, fields, followup, missing, warning,
    message: recognized === 0
      ? '未能识别出客户字段，请手动补充'
      : missing.length ? '部分必填信息未识别到，请手动补充' : '识别完成，请核对后保存',
  };
}

module.exports = { extract, extractByRules, parseDate };

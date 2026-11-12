'use strict';
/**
 * 生成演示数据：1 名总监 + 7 名销售 + 60 条线索（部分已认领、部分临近回流）。
 * 仅用于本地体验，生产环境请勿执行。用法：npm run seed:demo
 */
process.env.TZ = process.env.TZ || 'Asia/Shanghai';
const path = require('path');
const fs = require('fs');
const bcrypt = require('bcryptjs');
const { Database } = require('../src/db');
const { seedDefaults, ensureAdmin } = require('../src/seed');
const clock = require('../src/clock');
const L = require('../src/leads');
const { DAY } = clock;

const DEMO_PASSWORD = 'demo1234';

(async () => {
  const dataDir = path.resolve(__dirname, '..', process.env.DATA_DIR || './data');
  fs.mkdirSync(dataDir, { recursive: true });
  const db = await Database.open(path.join(dataDir, 'leads.sqlite'));
  seedDefaults(db);
  ensureAdmin(db, DEMO_PASSWORD);
  if (db.get("SELECT 1 FROM users WHERE username = 'director'")) {
    console.log('演示数据已存在，跳过。');
    return;
  }
  const hash = bcrypt.hashSync(DEMO_PASSWORD, 10);
  const now = Date.now();
  const mk = (username, name, role) => db.run('INSERT INTO users (username, password_hash, name, role, created_at, updated_at) VALUES (?,?,?,?,?,?)',
    [username, hash, name, role, now, now]).lastInsertRowid;
  const director = { id: mk('director', '周总监', 'director'), name: '周总监', role: 'director' };
  const names = ['李明', '王芳', '张伟', '刘洋', '陈静', '杨磊', '赵敏'];
  const sales = names.map((n, i) => ({ id: mk(`sales${i + 1}`, n, 'sales'), name: n, role: 'sales' }));

  const inds = db.all('SELECT id, name FROM industries');
  const brands = {
    快消: ['蓝湾饮料', '禾丰食品', '鲜语乳业', '悦颜日化', '麦田烘焙', '晨光啤酒'],
    '互联网/网络': ['云帆网络科技', '速达电商', '乐游互娱', '知行软件', '优选直播'],
    汽车: ['驰远汽车销售', '星驰新能源汽车', '安途汽贸', '恒达4S'],
    金融: ['城市商业银行', '鼎信证券', '汇金基金', '安融支付'],
    保险: ['平安喜乐保险代理', '长青人寿', '泰和财险'],
  };
  const stages = ['初次联系', '待报价', '方案沟通', '已成交', '暂无意向'];
  const intents = ['高意向', '中等意向', '低意向'];
  const sources = ['渠道A', '渠道B', '线下收集', '线上'];
  const depts = ['市场部', '品牌部', '媒介中心', '总经办'];
  const titles = ['市场总监', '品牌经理', '媒介经理', '总经理'];
  const surnames = '赵钱孙李周吴郑王冯陈褚卫蒋沈韩杨朱秦许何吕施张孔曹严华金魏陶姜'.split('');
  const districts = ['朝阳区', '海淀区', '浦东新区', '天河区', '南山区', '武侯区', '江干区'];
  let n = 0;
  const pickOf = (a) => a[Math.floor(Math.random() * a.length)];

  for (const ind of inds) {
    for (const b of brands[ind.name] || []) {
      for (const suffix of ['有限公司', '（华东）有限公司']) {
        if (n >= 60) break;
        n++;
        const phone = '13' + String(100000000 + Math.floor(Math.random() * 899999999));
        const id = L.createLead(db, director, {
          company_name: b + suffix, contact_name: pickOf(surnames) + pickOf(['先生', '女士', '经理', '总']),
          contact_dept: pickOf(depts), contact_title: pickOf(titles), contact_phone: phone,
          address: `${pickOf(districts)}${pickOf(['建国路', '科技园路', '世纪大道', '天河路'])}${Math.ceil(Math.random() * 300)}号`,
          industry_id: ind.id, source: pickOf(sources), intent_level: pickOf(intents),
        });
        // 约一半线索被认领，制造不同的“剩余天数”
        if (n % 2 === 0) {
          const owner = n % 10 === 0 ? director : sales[n % sales.length];
          const daysAgo = [1, 5, 12, 20, 24, 26, 28][Math.floor(n / 2) % 7];
          clock.offset = -daysAgo * DAY;
          L.claim(db, owner, id);
          if (n % 4 === 0) {
            clock.offset = -Math.max(0, daysAgo - 2) * DAY;
            L.addFollowup(db, owner, id, { content: '电话沟通，客户对商圈大屏感兴趣，需要发点位和刊例价。', stage: pickOf(stages.slice(0, 4)) });
          }
          clock.offset = 0;
        }
      }
    }
  }
  console.log(`演示数据已生成：总监 director、销售 sales1~sales7，管理员 admin，密码均为 ${DEMO_PASSWORD}；线索 ${n} 条。`);
})();

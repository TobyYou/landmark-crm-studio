# 地标传媒 · 广告销售线索公海管理系统

公海池 → 销售认领进入个人私池 → 30 天无有效跟进自动回流公海 → 其他销售再次认领。
手机 H5 + 电脑管理端（同一套前端自适应）+ 微信小程序壳，支持语音口述 AI 自动填表。

## 快速开始

```bash
cd server
npm install
npm run seed:demo   # 可选：演示数据（账号 admin / director / sales1~7，密码 demo1234）
npm start           # 打开 http://localhost:3000
npm test            # 运行核心规则测试
```

## 文档

| 文档 | 内容 |
|---|---|
| [docs/01-产品需求文档PRD.md](docs/01-产品需求文档PRD.md) | 角色权限、流程、字段、规则、禁止项落实 |
| [docs/02-数据库设计.md](docs/02-数据库设计.md) | 表结构、约束、迁移建议 |
| [docs/03-页面清单与原型说明.md](docs/03-页面清单与原型说明.md) | 每个页面的布局与交互 |
| [docs/04-后端接口清单.md](docs/04-后端接口清单.md) | 全部 API |
| [docs/05-部署说明.md](docs/05-部署说明.md) | 本地试用、云服务器、HTTPS、小程序、AI 配置、运维 |
| [docs/06-上刊素材精修与大屏替换系统.md](docs/06-上刊素材精修与大屏替换系统.md) | 独立模块 `studio/`：上刊照片精修、大屏客户画面替换（平面/弧形/拐角屏）、视频贴合 |

## 上刊素材工作台（独立模块）

```bash
cd studio
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python run.py   # 打开 http://localhost:8600
```

## 代码结构

- `server/src/leads.js` — 认领 / 退回 / 自动回流 / 强制回收 / 跟进 / 权限隔离（核心规则都在这里）
- `server/src/reports.js` — 总监看板、导入、导出
- `server/src/extract.js` — 语音文本 → 字段（Claude AI，失败自动降级本地规则）
- `server/src/db.js` — 表结构与“不可删除”触发器
- `web/app.js` — 前端全部页面
- `miniprogram/` — 微信小程序 web-view 壳

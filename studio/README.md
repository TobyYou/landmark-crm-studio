# 地标传媒 · 上刊照片精修与大屏素材合成（Studio）

真实上刊照片 / 视频 → 精修增强（画质、色调、大屏、人流车流、大片感）→ 按客户需求把大屏换成客户广告图 / 广告视频（平面屏、弧形屏、L 型拐角屏、多折面屏），并做真实融合。

完整说明（功能、流程图、标定方法、技术方案、测试用例与实测结果、能力边界）：[docs/06-上刊素材精修与大屏替换系统.md](../docs/06-上刊素材精修与大屏替换系统.md)

## 快速开始

```bash
cd studio
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python run.py            # http://localhost:8600
.venv/bin/python -m pytest         # 39 项测试
.venv/bin/python scripts/make_demo.py   # 演示素材 → data/demo（合成场景，含点位真值）
```

## 目录

| 路径 | 内容 |
|---|---|
| `dibiao_studio/geometry.py` | 屏幕几何：多面片映射、单视图宽高比 / 焦距估计、弧形屏母线三维重建、镜头畸变 |
| `dibiao_studio/enhance.py` | 精修增强 |
| `dibiao_studio/replace.py` | 客户画面替换与真实融合 |
| `dibiao_studio/crowd.py` | 人流车流（光轨 + 素材贴入） |
| `dibiao_studio/video.py` | 视频跟踪与客户视频同步 |
| `dibiao_studio/pipeline.py` | 流程 A/B/C、输出版本、水印 |
| `dibiao_studio/server.py` + `static/` | HTTP 接口与网页工作台 |
| `dibiao_studio/synth.py` | 合成测试场景（已知 3D 几何的精确真值渲染） |
| `assets/sprites/{people,vehicles}/` | 人物 / 车辆透明底素材库（**需自行放入**，白天“增加人流车流”依赖它） |
| `data/` | 上传、任务输出、模板（不入库） |

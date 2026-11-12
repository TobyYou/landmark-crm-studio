"""视频跟踪精度、视频合成输出、HTTP 接口端到端。"""
import time

import numpy as np
import pytest

from dibiao_studio import synth, video as V, geometry as G, pipeline as P
from dibiao_studio.imgio import video_info, write_image


@pytest.fixture(scope="module")
def clip(tmp_path_factory):
    d = tmp_path_factory.mktemp("clip")
    path = d / "aerial.mp4"
    gts = synth.make_test_video(str(path), w=960, h=540, frames=60)
    return path, gts


def tpl_from(g):
    return G.ScreenTemplate.from_dict({"type": "flat", "top": [g[0].tolist(), g[1].tolist()],
                                       "bottom": [g[3].tolist(), g[2].tolist()], "image_size": [960, 540]})


def corners(tr, i):
    return np.stack([tr["top"][i][0], tr["top"][i][1], tr["bottom"][i][1], tr["bottom"][i][0]])


@pytest.mark.parametrize("kfs", [[0], [30], [0, 59]])
def test_tracking_no_drift(clip, kfs):
    path, gts = clip
    tr = V.track_video(str(path), {k: tpl_from(gts[k]) for k in kfs}, V.VideoParams())
    err = np.array([np.linalg.norm(corners(tr, i) - gts[i], axis=1) for i in range(tr["frames"])])
    assert err.mean() < 0.15
    assert err.max() < 0.6
    assert err[-5:].mean() < 0.2  # 末尾不漂移


def test_tracking_jitter_low(clip):
    path, gts = clip
    tr = V.track_video(str(path), {0: tpl_from(gts[0])}, V.VideoParams())
    est = np.stack([corners(tr, i) for i in range(tr["frames"])])
    resid = est - np.stack(gts)
    assert np.abs(np.diff(resid, axis=0)).mean() < 0.1  # 帧间误差变化（抖动）


def test_smoothing_has_no_lag():
    t = np.arange(100, dtype=float)
    x = np.stack([t * 2.5, t * -1.0], -1)[:, None, :]
    y = V.gaussian_smooth(x, 2.0)
    assert np.abs(y - x).max() < 1e-6  # 线性运动零相位平滑后完全不变（无延迟、端点不回拉）


def test_video_pipeline_outputs(clip, tmp_path):
    path, gts = clip
    ad = tmp_path / "ad.png"
    write_image(ad, synth.test_pattern(1280, 720))
    g = gts[0]
    spec = {"mode": "enhance_then_replace", "base": str(path), "ad": str(ad),
            "template": tpl_from(g).to_dict(), "video": {"max_frames": 20}}
    res = P.run_job(spec, tmp_path / "out")
    vi = video_info(tmp_path / "out" / "final.mp4")
    assert vi["frames"] == 20 and (vi["width"], vi["height"]) == (960, 540)
    assert (tmp_path / "out" / "watermark.mp4").exists()
    assert res["report"]["tracking"]["median_reproj_err_px"] < 1.0


def test_ad_source_timing(clip):
    path, _ = clip
    src = V.AdSource(str(path), loop=True)
    a = src.get(0.0)
    b = src.get(1.0)        # 25fps → 第 25 帧
    c = src.get(0.0)        # 回到开头（重新打开）
    assert a.shape == b.shape and not np.array_equal(a, b) and np.array_equal(a, c)
    src.close()


def test_http_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setenv("STUDIO_DATA_DIR", str(tmp_path / "data"))
    import importlib
    from dibiao_studio import server
    server = importlib.reload(server)
    from fastapi.testclient import TestClient

    c = TestClient(server.app)
    w, h = 960, 540
    cam, faces, ub, tpl, _ = synth.folded_scene(w, h, f=800)
    img, _ = synth.render_planar_faces(synth.city_background(w, h, night=True), synth.test_pattern(800, 240), cam, faces, ub)
    write_image(tmp_path / "base.jpg", img)
    write_image(tmp_path / "ad.jpg", synth.test_pattern(1280, 720))
    base = c.post("/api/uploads", files={"file": ("base.jpg", (tmp_path / "base.jpg").read_bytes(), "image/jpeg")}).json()
    ad = c.post("/api/uploads", files={"file": ("ad.jpg", (tmp_path / "ad.jpg").read_bytes(), "image/jpeg")}).json()
    assert base["width"] == w
    assert c.post("/api/uploads", files={"file": ("x.txt", b"hi", "text/plain")}).status_code == 400

    g = c.post("/api/geometry/preview", json={"template": tpl}).json()
    assert g["aspect"] == pytest.approx(10 / 3, rel=0.02)
    t = c.post("/api/templates", json={**tpl, "name": "测试拐角屏"}).json()
    assert any(x["id"] == t["id"] for x in c.get("/api/templates").json())

    r = c.post("/api/jobs", json={"base_id": base["id"], "mode": "enhance_then_replace", "template": tpl})
    assert r.status_code == 400  # 缺少客户素材
    job = c.post("/api/jobs", json={"base_id": base["id"], "ad_id": ad["id"], "mode": "enhance_then_replace",
                                    "template": tpl, "output": {"max_side": 1280}, "title": "测试"}).json()
    for _ in range(120):
        j = c.get(f"/api/jobs/{job['id']}").json()
        if j["status"] in ("done", "failed"):
            break
        time.sleep(0.25)
    assert j["status"] == "done", j.get("error")
    for k in ("final", "client", "watermark", "refined"):
        assert c.get(j["outputs"][k]).status_code == 200
    assert j["report"]["client_size"] == [1280, 720]
    assert c.delete(f"/api/jobs/{job['id']}").status_code == 200


def test_http_calibration_tools(tmp_path, monkeypatch):
    monkeypatch.setenv("STUDIO_DATA_DIR", str(tmp_path / "data"))
    import importlib
    from dibiao_studio import server
    server = importlib.reload(server)
    from fastapi.testclient import TestClient

    c = TestClient(server.app)
    w, h = 960, 540
    cam, faces, ub, tpl, _ = synth.folded_scene(w, h, f=800)
    img, _ = synth.render_planar_faces(synth.city_background(w, h, night=True), synth.test_pattern(800, 240), cam, faces, ub)
    write_image(tmp_path / "base.png", img)
    base = c.post("/api/uploads", files={"file": ("base.png", (tmp_path / "base.png").read_bytes(), "image/png")}).json()
    r = c.post(f"/api/uploads/{base['id']}/snap", json={"template": tpl}).json()
    assert len(r["top"]) == 3 and max(r["moved"]) < 5
    r = c.post(f"/api/uploads/{base['id']}/unwrap", json={"template": tpl, "height": 200})
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    assert c.post(f"/api/uploads/{base['id']}/lens", json={"template": tpl}).json()["k1"] == 0.0
    assert c.post(f"/api/uploads/{base['id']}/snap", json={"template": {"type": "flat"}}).status_code == 400

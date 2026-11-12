"""精修 / 融合 / 人流车流：安全性与真实感约束测试。"""
import cv2
import numpy as np
import pytest

from dibiao_studio import analysis as A, enhance as E, replace as R, geometry as G, crowd as C, synth


def noisy(img, sigma, seed=0):
    return np.clip(img + np.random.default_rng(seed).normal(0, sigma, img.shape).astype(np.float32), 0, 1)


@pytest.mark.parametrize("sigma", [0.005, 0.01, 0.02, 0.04])
def test_noise_estimate(sigma):
    img = np.full((600, 900, 3), 0.4, np.float32)
    assert A.estimate_noise(noisy(img, sigma)) == pytest.approx(sigma, rel=0.08)


def test_scene_classification():
    assert A.classify_scene(synth.city_background(800, 450, night=True))["scene"] == "night"
    assert A.classify_scene(synth.city_background(800, 450, night=False))["scene"] == "day"


def test_hot_pixels_only_isolated():
    img = synth.city_background(400, 300, night=True, seed=3)
    out, n = E.remove_hot_pixels(img, 0.01)
    assert n == 0  # 真实的小灯点、窗格角点不能被当成坏点
    bad = img.copy()
    bad[100, 100] = 1.0
    bad[50, 200] = 0.0 if img[50, 200].mean() > 0.3 else 1.0
    out, n = E.remove_hot_pixels(bad, 0.01)
    assert n >= 1 and abs(out[100, 100].mean() - img[100, 100].mean()) < 0.2


def test_enhance_preserves_structure():
    """精修不能改变结构：边缘位置与原图高度一致，尺寸不变，不大面积过曝。"""
    w, h = 1200, 800
    cam, faces, ub, tpl, _ = synth.flat_scene(w, h, f=900)
    img, _ = synth.render_planar_faces(synth.city_background(w, h, night=True), synth.test_pattern(800, 300), cam, faces, ub)
    src = noisy(img, 0.015)
    geom = G.build_geometry(G.ScreenTemplate.from_dict(tpl), w, h)
    out, log = E.enhance(src, E.EnhanceParams(), screen=G.screen_mask(geom, w, h))
    assert out.shape == src.shape and out.dtype == np.float32
    assert log["scene"] == "night"
    e1 = cv2.Canny((img * 255).astype(np.uint8), 50, 150) > 0
    e2 = cv2.Canny((out * 255).astype(np.uint8), 50, 150) > 0
    near = cv2.dilate(e2.astype(np.uint8), np.ones((3, 3), np.uint8)) > 0
    assert (e1 & near).sum() / e1.sum() > 0.8
    assert (out.max(axis=2) > 0.995).mean() < 0.02


def test_enhance_video_safe_is_deterministic():
    img = noisy(synth.city_background(640, 360, night=False, seed=1), 0.01)
    p = E.EnhanceParams(video_safe=True, grain=0)
    st = E.analyze(img, p)
    a, _ = E.enhance(img, p, stats=st)
    b, _ = E.enhance(img, p, stats=st)
    assert np.array_equal(a, b)


def _night_scene():
    w, h = 1280, 720
    cam, faces, ub, tpl, phys = synth.folded_scene(w, h, f=1000)
    old = np.zeros((300, 1000, 3), np.float32)
    old[:] = (0.2, 0.6, 0.9)
    img, _ = synth.render_planar_faces(synth.city_background(w, h, night=True, seed=2), old, cam, faces, ub)
    return noisy(img, 0.012), G.ScreenTemplate.from_dict(tpl)


def test_fusion_matches_noise_and_levels():
    img, tpl = _night_scene()
    ad = synth.test_pattern(1920, 1080)
    ad[:, :500] = 0  # 纯黑区域：检验 LED 黑位
    out, info = R.replace_screen(img, ad, tpl, R.FusionParams())
    assert info["aspect_used"] == pytest.approx(10 / 3, rel=0.01)
    geom = G.build_geometry(tpl, 1280, 720)
    m = G.screen_mask(geom, 1280, 720)
    inner = cv2.erode((m > 0.99).astype(np.uint8), np.ones((9, 9), np.uint8)) > 0
    # 屏幕内噪声与原图噪声同量级（不“过于干净”）
    n_out = A.estimate_noise(out, region=inner.astype(np.float32))
    assert 0.5 * 0.012 < n_out < 2.5 * 0.012
    # 屏幕黑位被环境光抬起但不发灰（夜景）
    L = out[inner] @ A.LUMA
    assert 0.01 < np.percentile(L, 1) < 0.2
    # 屏幕外的光晕：紧贴屏幕外侧比远处亮
    ring = A.ring_mask(m, 2, 12) > 0
    far = A.ring_mask(m, 120, 160) > 0
    gain_ring = (out[ring] @ A.LUMA).mean() - (img[ring] @ A.LUMA).mean()
    gain_far = (out[far] @ A.LUMA).mean() - (img[far] @ A.LUMA).mean()
    assert gain_ring > gain_far + 0.005


def test_occluders_preserved():
    img, tpl = _night_scene()
    d = tpl.to_dict()
    c = np.mean(np.array(d["top"] + d["bottom"]), axis=0)
    d["occluders"] = [[[c[0] - 8, c[1] - 200], [c[0] + 8, c[1] - 200], [c[0] + 8, c[1] + 200], [c[0] - 8, c[1] + 200]]]
    out, _ = R.replace_screen(img, synth.test_pattern(1920, 1080), G.ScreenTemplate.from_dict(d), R.FusionParams(glow=0))
    y, x = int(c[1]), int(c[0])
    assert np.abs(out[y - 50:y + 50, x - 3:x + 3] - img[y - 50:y + 50, x - 3:x + 3]).max() < 1e-5


def test_light_trails_stay_in_lane_and_off_screen():
    w, h = 960, 540
    img = synth.city_background(w, h, night=True, seed=4) * 0.3
    lanes = [{"kind": "road", "left": [[100, 539], [430, 300]], "right": [[860, 539], [530, 300]], "width_m": 14}]
    scr = np.zeros((h, w), np.float32)
    scr[380:420, 440:520] = 1
    out, info = C.augment(img, lanes, C.CrowdParams(level="strong"), "night", screen_mask=scr)
    assert info["trails"] > 10
    diff = (out - img).max(axis=2)
    lane = C._region_mask(C.LaneModel(C.Lane.from_dict(lanes[0])), h, w)
    glow_margin = cv2.dilate((lane > 0).astype(np.uint8), np.ones((41, 41), np.uint8)) > 0
    assert diff[~glow_margin].max() < 0.02          # 光轨不出车道（仅允许光晕外溢）
    assert diff[scr > 0].max() < 1e-6                # 不覆盖大屏


def test_sprites_scale_with_perspective(tmp_path):
    """素材按落脚点处的透视比例尺缩放：近处的人比远处高。"""
    rgba = np.zeros((170, 60, 4), np.float32)
    rgba[:, :, :3] = 0.3
    rgba[10:, 10:50, 3] = 1
    sprites = {"people": [C.Sprite(rgba, "people", "test")], "vehicles": []}
    w, h = 800, 600
    img = np.full((h, w, 3), 0.6, np.float32)
    lanes = [{"kind": "sidewalk", "left": [[100, 590], [380, 250]], "right": [[400, 590], [420, 250]], "width_m": 3}]
    out, info = C.augment(img, lanes, C.CrowdParams(level="strong", trails="off", shadow=0), "day", sprites=sprites)
    assert info["sprites"] >= 3
    boxes = sorted(info["placements"], key=lambda b: b[3])          # 按落脚点 y 排序
    heights = [b[3] - b[1] for b in boxes if b[1] > 0]
    feet = [b[3] for b in boxes if b[1] > 0]
    assert np.corrcoef(feet, heights)[0, 1] > 0.9                   # 越近（越靠下）越高
    lm = C.LaneModel(C.Lane.from_dict(lanes[0]))
    b = boxes[-1]
    s = float(np.clip(np.interp(b[3], [250, 590], [1, 0]), 0, 1))  # 该车道 y 与弧长参数单调对应（近似）
    expect = 1.7 * float(lm.at(s, lm.ppm)) * (160 / 170)            # 素材内人物占 160/170 高度
    assert heights[-1] == pytest.approx(expect, rel=0.2)


# ---------------------------------------------------------------- 真实照片测试中发现并修复的问题（回归测试）

def test_bright_overcast_day_not_darkened():
    """明亮阴天：精修不能把天空 / 整体压暗压灰（真实样片曾出现）。"""
    w, h = 1200, 800
    img = synth.city_background(w, h, night=False, seed=5)
    img[: h // 2] = 0.86 + np.random.default_rng(0).normal(0, 0.01, (h // 2, w, 3)).astype(np.float32)  # 灰白天空
    out, log = E.enhance(img, E.EnhanceParams(scene="day"))
    L0, L1 = A.luminance(img), A.luminance(out)
    assert np.median(L1) >= np.median(L0) * 0.98
    assert L1[: h // 3].mean() >= L0[: h // 3].mean() * 0.95


def test_snap_template_recovers_corners():
    """点位吸附：±5px 随机误差，多次试验平均误差显著下降（测试图案的网格线紧贴屏幕边，属于困难情形）。"""
    for fn, limit in ((synth.flat_scene, 1.5), (synth.folded_scene, 2.2)):
        cam, faces, ub, tpl, phys = fn(1600, 900, f=1100)
        img, _ = synth.render_planar_faces(synth.city_background(1600, 900, night=True), synth.test_pattern(1200, 450), cam, faces, ub)
        T, B = np.array(tpl["top"]), np.array(tpl["bottom"])
        before, after = [], []
        for seed in range(8):
            rng = np.random.default_rng(seed)
            pT, pB = T + rng.uniform(-5, 5, T.shape), B + rng.uniform(-5, 5, B.shape)
            r = A.snap_template(img, {**tpl, "top": pT.tolist(), "bottom": pB.tolist()})
            after.append(np.linalg.norm(np.r_[r["top"], r["bottom"]] - np.r_[T, B], axis=1))
            before.append(np.linalg.norm(np.r_[pT, pB] - np.r_[T, B], axis=1))
        before, after = np.array(before), np.array(after)
        assert after.mean() < limit and after.mean() < 0.55 * before.mean(), (fn.__name__, after.mean())


def test_lens_k1_zero_when_edges_straight():
    cam, faces, ub, tpl, phys = synth.flat_scene(1600, 900, f=1100)
    img, _ = synth.render_planar_faces(synth.city_background(1600, 900, night=True), synth.test_pattern(1200, 450), cam, faces, ub)
    assert A.estimate_lens_k1(img, tpl)["k1"] == 0.0


def test_face_shading_follows_original_faces():
    """拐角屏两面在原片里白电平不同（LED 视角衰减）→ 新画面也应一面亮一面暗。"""
    w, h = 1280, 720
    cam, faces, ub, tpl, phys = synth.folded_scene(w, h, f=1000)
    old = synth.test_pattern(1000, 300)
    img, _ = synth.render_planar_faces(synth.city_background(w, h, night=True, seed=2), old, cam, faces[:1], ub[:1])
    img, _ = synth.render_planar_faces(img, old * 0.7, cam, faces[1:], ub[1:])   # 第二面整体暗 30%
    geom = G.build_geometry(G.ScreenTemplate.from_dict(tpl), w, h)
    st = R.prepare_state(img, geom, R.FusionParams(face_shading=1.0), "night")
    assert st.face_gain is not None
    assert st.face_gain[0] == pytest.approx(1.0, abs=0.02) and st.face_gain[1] == pytest.approx(0.7, abs=0.06)

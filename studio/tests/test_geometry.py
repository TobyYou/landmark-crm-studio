"""屏幕几何测试：用已知 3D 场景的精确渲染作真值，度量贴合误差（像素）。"""
import math

import numpy as np
import pytest

from dibiao_studio import geometry as G, synth

W, H = 1280, 720


def uv_content(cw, ch):
    u = np.linspace(0, 1, cw, dtype=np.float32)[None, :].repeat(ch, 0)
    v = np.linspace(0, 1, ch, dtype=np.float32)[:, None].repeat(cw, 1)
    return np.stack([u, v, np.zeros_like(u)], -1)


def uv_error(render_gt, tpl_dict, phys, extra=None):
    """返回 (u 方向误差 px 数组, v 方向误差 px 数组, geom)。"""
    d = dict(tpl_dict)
    d.update(extra or {})
    tpl = G.ScreenTemplate.from_dict(d)
    geom = G.build_geometry(tpl, W, H)
    base = np.zeros((H, W, 3), np.float32)
    gt, ga = render_gt(base, uv_content(2000, int(2000 * phys[1] / phys[0])))
    layer, a, (x0, y0, x1, y1) = G.warp_content(uv_content(2000, int(2000 / geom.aspect)), geom, W, H)
    res = np.zeros_like(base)
    res[y0:y1, x0:x1] = layer
    import cv2
    m = cv2.erode((ga > 0.99).astype(np.uint8), np.ones((5, 5), np.uint8)) > 0
    ys, xs = np.nonzero(m)
    sw, sh = np.ptp(xs), np.ptp(ys)
    return np.abs(res[..., 0] - gt[..., 0])[m] * sw, np.abs(res[..., 1] - gt[..., 1])[m] * sh, geom, (a, m[y0:y1, x0:x1])


def test_flat_exact():
    cam, faces, ub, tpl, phys = synth.flat_scene(W, H)
    du, dv, geom, _ = uv_error(lambda b, c: synth.render_planar_faces(b, c, cam, faces, ub), tpl, phys)
    assert np.percentile(du, 99) < 0.2 and np.percentile(dv, 99) < 0.2
    assert geom.aspect == pytest.approx(phys[0] / phys[1], rel=0.005)  # 单视图宽高比恢复
    assert geom.focal == pytest.approx(cam.f, rel=0.01)                # 焦距估计


def test_folded_auto_recovers_face_widths():
    """L 型拐角屏：不填任何物理尺寸，仅凭透视恢复两个面 6m:4m 的宽度比与整体比例。"""
    cam, faces, ub, tpl, phys = synth.folded_scene(W, H)
    du, dv, geom, (a, inner) = uv_error(lambda b, c: synth.render_planar_faces(b, c, cam, faces, ub), tpl, phys)
    w = np.array(geom.segment_widths)
    assert w[0] / w[1] == pytest.approx(6 / 4, rel=0.01)
    assert geom.aspect == pytest.approx(10 / 3, rel=0.01)
    assert np.percentile(du, 99) < 0.3 and np.percentile(dv, 99) < 0.3
    # 折缝处无撕裂（屏幕内部 alpha 全为 1）
    assert (a[inner] > 0.999).all()


def test_folded_physical_widths():
    cam, faces, ub, tpl, phys = synth.folded_scene(W, H)
    du, dv, geom, _ = uv_error(lambda b, c: synth.render_planar_faces(b, c, cam, faces, ub), tpl, phys,
                               {"u_mode": "physical", "segment_widths": [6, 4], "physical_size": [10, 3]})
    assert np.percentile(du, 99) < 0.3


def test_folded_equal_split_is_wrong_and_detectable():
    """反例：把两面按等宽硬分（相当于“整体硬拉伸”）误差很大 —— 说明按物理宽度分配是必要的。"""
    cam, faces, ub, tpl, phys = synth.folded_scene(W, H)
    du, _, _, _ = uv_error(lambda b, c: synth.render_planar_faces(b, c, cam, faces, ub), tpl, phys, {"u_mode": "equal"})
    assert du.mean() > 10


@pytest.mark.parametrize("n_ctrl,max_mean", [(7, 0.8), (9, 0.4)])
def test_curved_cylinder(n_ctrl, max_mean):
    cam, g, tpl, phys = synth.curved_scene(W, H, n_ctrl=n_ctrl)
    for mode in ("equal", "auto"):
        du, dv, geom, (a, inner) = uv_error(lambda b, c: synth.render_cylinder(b, c, cam, g), tpl, phys, {"u_mode": mode})
        assert du.mean() < max_mean, (mode, du.mean())
        assert dv.mean() < max_mean
        assert geom.aspect == pytest.approx(phys[0] / phys[1], rel=0.01)
        assert (a[inner] > 0.999).all()  # 面片之间无缝


def test_curved_beats_naive_rectangle():
    """弧形屏若当作平面四点透视处理，误差远大于曲面映射。"""
    cam, g, tpl, phys = synth.curved_scene(W, H)
    du_c, _, _, _ = uv_error(lambda b, c: synth.render_cylinder(b, c, cam, g), tpl, phys)
    flat = {"type": "flat", "top": [tpl["top"][0], tpl["top"][-1]], "bottom": [tpl["bottom"][0], tpl["bottom"][-1]],
            "image_size": tpl["image_size"]}
    du_f, dv_f, _, _ = uv_error(lambda b, c: synth.render_cylinder(b, c, cam, g), flat, phys)
    assert du_c.mean() * 5 < du_f.mean()


def test_lens_distortion_roundtrip():
    d = G.Distortion(-0.12, 0.02, 1920, 1080)
    pts = np.random.default_rng(0).uniform([0, 0], [1920, 1080], (200, 2))
    assert np.abs(d.distort(d.undistort(pts)) - pts).max() < 1e-3


def test_template_rescale_and_validation():
    t = G.ScreenTemplate.from_dict({"type": "flat", "top": [[10, 10], [110, 12]], "bottom": [[10, 60], [110, 58]],
                                    "image_size": [200, 100]})
    s = t.scaled_to(400, 200)
    assert s.top[1] == [220, 24]
    with pytest.raises(ValueError):
        G.ScreenTemplate.from_dict({"type": "curved", "top": [[0, 0], [1, 0]], "bottom": [[0, 1], [1, 1]]})
    with pytest.raises(ValueError):
        G.ScreenTemplate.from_dict({"type": "folded", "top": [[0, 0], [1, 0], [2, 0]], "bottom": [[0, 1], [1, 1]]})


def test_fit_content_keeps_proportions():
    c = np.zeros((1080, 1920, 3), np.float32)
    out = G.fit_content(c, 10 / 3, "cover")
    assert out.shape[1] / out.shape[0] == pytest.approx(10 / 3, rel=0.01)
    out = G.fit_content(c, 10 / 3, "contain")
    assert out.shape[1] / out.shape[0] == pytest.approx(10 / 3, rel=0.01) and out.shape[0] == 1080


# ---------------------------------------------------------------- 真实照片测试中发现并修复的问题（回归测试）

def test_turn_angle_fixes_focal():
    """拐角屏：已知两面夹角 90° → 由消失点直接求焦距（真实照片中单矩形估计偏差可达 2 倍）。"""
    cam, faces, ub, tpl, phys = synth.folded_scene(W, H)
    g = G.build_geometry(G.ScreenTemplate.from_dict({**tpl, "turn_angle": 90}), W, H)
    assert g.focal == pytest.approx(cam.f, rel=0.01)
    w = np.array(g.segment_widths)
    assert w[0] / w[1] == pytest.approx(6 / 4, rel=0.01)
    with pytest.raises(ValueError):
        G.ScreenTemplate.from_dict({**tpl, "type": "flat", "top": tpl["top"][:2], "bottom": tpl["bottom"][:2], "turn_angle": 90})


def test_curved_uneven_columns_no_vertical_jog():
    """弧形屏控制点间距不均（真实标定的常态）时，相邻细面片的竖向透视必须一致，否则斜线 / 文字会错位。"""
    cam, g, tpl, phys = synth.curved_scene(W, H)
    ths = np.array([-50, -38, -30, -5, 12, 33, 50]) * math.pi / 180   # 不均匀
    P = lambda th, y: (g["radius"] * math.sin(th), y, g["cz"] - g["radius"] * math.cos(th))
    t2 = dict(tpl, top=cam.project(np.array([P(t, 0) for t in ths])).tolist(),
              bottom=cam.project(np.array([P(t, g["H"]) for t in ths])).tolist(), u_mode="auto")
    du, dv, geom, _ = uv_error(lambda b, c: synth.render_cylinder(b, c, cam, g), t2, phys)
    assert dv.mean() < 0.6 and np.percentile(dv, 99) < 3.0
    assert du.mean() < 1.5


def test_unwrap_inverts_mapping():
    """展开检查：把真值渲染的屏幕展开，应还原客户画面（检验标定的工具本身要准）。"""
    import cv2
    cam, faces, ub, tpl, phys = synth.folded_scene(W, H)
    content = synth.test_pattern(1000, 300)
    img, _ = synth.render_planar_faces(np.zeros((H, W, 3), np.float32), content, cam, faces, ub)
    geom = G.build_geometry(G.ScreenTemplate.from_dict(tpl), W, H)
    u = G.unwrap(img, geom, 300)
    ref = cv2.resize(content, (u.shape[1], u.shape[0]), interpolation=cv2.INTER_AREA)
    err = np.abs(u[10:-10, 10:-10] - ref[10:-10, 10:-10]).mean()
    assert err < 0.06


def test_edge_expand_covers_calibration_slack():
    cam, faces, ub, tpl, phys = synth.flat_scene(W, H)
    geom = G.build_geometry(G.ScreenTemplate.from_dict(tpl), W, H)
    _, _, a0, b0 = G.compute_maps(geom, W, H, 10, 10, need_maps=False)
    _, _, a2, b2 = G.compute_maps(geom, W, H, 10, 10, need_maps=False, expand=2.0)
    full0 = np.zeros((H, W)); full0[b0[1]:b0[3], b0[0]:b0[2]] = a0
    full2 = np.zeros((H, W)); full2[b2[1]:b2[3], b2[0]:b2[2]] = a2
    grow = (full2 > 0.5).sum() - (full0 > 0.5).sum()
    perim = sum(np.linalg.norm(np.subtract(a, b)) for a, b in
                [(tpl["top"][0], tpl["top"][1]), (tpl["top"][1], tpl["bottom"][1]), (tpl["bottom"][1], tpl["bottom"][0]), (tpl["bottom"][0], tpl["top"][0])])
    assert grow == pytest.approx(2.0 * perim, rel=0.15)

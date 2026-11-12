"""合成测试场景：已知 3D 几何 + 相机 → 精确的“真值”渲染。

用于测试用例与演示：平面屏、L 型拐角屏、圆柱弧形屏，在给定相机下的投影与理想贴图结果。
真实案例测试仍需真实上刊照片，见 docs/06。
"""
from __future__ import annotations

import math

import cv2
import numpy as np


def look_at(cam_pos, target, up=(0, -1, 0)):
    """返回 R, t（世界→相机），相机坐标：x 右、y 下、z 前。世界坐标 y 向下。"""
    C = np.asarray(cam_pos, float)
    z = np.asarray(target, float) - C
    z /= np.linalg.norm(z)
    x = np.cross(np.asarray(up, float) * -1, z)  # up 为 -y
    x /= np.linalg.norm(x)
    y = np.cross(z, x)
    R = np.stack([x, y, z])
    return R, -R @ C


class Camera:
    def __init__(self, width, height, f, cam_pos, target):
        self.w, self.h, self.f = width, height, f
        self.K = np.array([[f, 0, (width - 1) / 2], [0, f, (height - 1) / 2], [0, 0, 1.0]])
        self.R, self.t = look_at(cam_pos, target)
        self.C = np.asarray(cam_pos, float)

    def project(self, X):
        X = np.atleast_2d(np.asarray(X, float))
        Xc = (self.R @ X.T).T + self.t
        p = (self.K @ Xc.T).T
        return p[:, :2] / p[:, 2:3]

    def rays(self):
        gx, gy = np.meshgrid(np.arange(self.w, dtype=np.float64), np.arange(self.h, dtype=np.float64))
        pix = np.stack([gx, gy, np.ones_like(gx)], -1)
        d = pix @ np.linalg.inv(self.K).T @ self.R  # 相机→世界：R^T * v，等价 v @ R
        return d


def test_pattern(w=1600, h=480, label="DIBIAO 地标传媒 TEST") -> np.ndarray:
    """棋盘格 + 色块 + 圆 + 文字：便于肉眼和数值检查拉伸 / 错位 / 撕裂。"""
    img = np.zeros((h, w, 3), np.uint8)
    cell = h // 8
    for yy in range(0, h, cell):
        for xx in range(0, w, cell):
            if ((yy // cell) + (xx // cell)) % 2 == 0:
                img[yy:yy + cell, xx:xx + cell] = 217
            else:
                img[yy:yy + cell, xx:xx + cell] = 38
    cols = [(25, 50, 242), (25, 204, 50), (242, 128, 25), (230, 25, 204)]
    for i, c in enumerate(cols):
        x = int((i + 0.5) * w / 4)
        cv2.circle(img, (x, h // 2), int(h * 0.28), c, -1, cv2.LINE_AA)
        cv2.circle(img, (x, h // 2), int(h * 0.28), (255, 255, 255), max(2, h // 80), cv2.LINE_AA)
    cv2.putText(img, "DIBIAO MEDIA", (int(w * 0.05), int(h * 0.62)), cv2.FONT_HERSHEY_DUPLEX,
                h / 110, (255, 255, 255), max(2, h // 50), cv2.LINE_AA)
    for x in np.linspace(0, w - 1, 11):
        cv2.line(img, (int(x), 0), (int(x), h - 1), (0, 255, 255), max(1, h // 200), cv2.LINE_AA)
    return img.astype(np.float32) / 255.0


def city_background(w, h, night=True, seed=0) -> np.ndarray:
    """有纹理的城市背景（窗格 + 噪声），供跟踪测试与演示使用。"""
    rng = np.random.default_rng(seed)
    base = np.full((h, w, 3), 0.06 if night else 0.55, np.float32)
    if not night:
        sky = np.linspace(0.85, 0.6, h // 2)[:, None, None] * np.array([1.0, 0.85, 0.65], np.float32)
        base[: h // 2] = sky
    for _ in range(int(w * h / 900)):
        x, y = rng.integers(0, w), rng.integers(0, h)
        s = int(rng.integers(3, 12))
        v = rng.uniform(0.2, 0.9) if night else rng.uniform(0.2, 0.7)
        col = np.array([v * rng.uniform(0.5, 0.8), v * rng.uniform(0.7, 0.95), v], np.float32)
        cv2.rectangle(base, (int(x), int(y)), (int(x + s), int(y + s * 1.5)), col.tolist(), -1)
    base += rng.normal(0, 0.01, base.shape).astype(np.float32)
    return np.clip(base, 0, 1)


def render_planar_faces(base, content, cam: Camera, faces, u_bounds):
    """faces: [(TL,TR,BR,BL) 3D]；u_bounds: [(u0,u1)]。用精确单应渲染真值。"""
    out = base.copy()
    h, w = base.shape[:2]
    ch, cw = content.shape[:2]
    alpha_total = np.zeros((h, w), np.float32)
    for face, (u0, u1) in zip(faces, u_bounds):
        img_pts = cam.project(np.array(face)).astype(np.float32)
        src = np.array([[u0 * cw - 0.5, -0.5], [u1 * cw - 0.5, -0.5], [u1 * cw - 0.5, ch - 0.5], [u0 * cw - 0.5, ch - 0.5]], np.float32)
        H = cv2.getPerspectiveTransform(src, img_pts)
        warped = cv2.warpPerspective(content, H, (w, h), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT)
        m = np.zeros((h, w), np.uint8)
        cv2.fillPoly(m, [np.round(img_pts * 16).astype(np.int32)], 255, cv2.LINE_AA, shift=4)
        a = m.astype(np.float32) / 255.0
        a = np.clip(a, 0, 1 - alpha_total)
        out = out * (1 - a[..., None]) + warped * a[..., None]
        alpha_total += a
    return out, alpha_total


def flat_scene(w=1280, h=720, f=1000.0):
    cam = Camera(w, h, f, cam_pos=(-4, -6, -14), target=(1.5, 2.0, 0))
    W, H = 8.0, 3.0
    face = [(-1, 0, 0), (-1 + W, 0, 0), (-1 + W, H, 0), (-1, H, 0)]
    pts = cam.project(np.array(face))
    tpl = {"type": "flat", "top": pts[[0, 1]].tolist(), "bottom": pts[[3, 2]].tolist(), "image_size": [w, h]}
    return cam, [face], [(0.0, 1.0)], tpl, (W, H)


def folded_scene(w=1280, h=720, f=950.0, wa=6.0, wb=4.0, H=3.0):
    """L 型外拐角屏：面 A 在 z=0（x∈[-wa,0]），面 B 在 x=0（z∈[0,wb]）。"""
    cam = Camera(w, h, f, cam_pos=(7, -4, -13), target=(-1.2, 1.5, 1.0))
    A = [(-wa, 0, 0), (0, 0, 0), (0, H, 0), (-wa, H, 0)]
    B = [(0, 0, 0), (0, 0, wb), (0, H, wb), (0, H, 0)]
    ua = wa / (wa + wb)
    top = cam.project(np.array([(-wa, 0, 0), (0, 0, 0), (0, 0, wb)]))
    bot = cam.project(np.array([(-wa, H, 0), (0, H, 0), (0, H, wb)]))
    tpl = {"type": "folded", "top": top.tolist(), "bottom": bot.tolist(), "image_size": [w, h]}
    return cam, [A, B], [(0.0, ua), (ua, 1.0)], tpl, (wa + wb, H)


def curved_scene(w=1280, h=720, f=1000.0, radius=6.0, arc_deg=100.0, H=3.0, n_ctrl=7):
    """外凸圆柱弧形屏，轴为竖直方向。控制点在弧上等角（等物理间距）分布。"""
    cam = Camera(w, h, f, cam_pos=(3, -5, -16), target=(0.5, 1.5, 2.0))
    th0, th1 = -math.radians(arc_deg) / 2, math.radians(arc_deg) / 2
    cz = radius  # 圆心在 z=radius，弧最近点在 z=0

    def P(th, y):
        return (radius * math.sin(th), y, cz - radius * math.cos(th))

    ths = np.linspace(th0, th1, n_ctrl)
    top = cam.project(np.array([P(t, 0) for t in ths]))
    bot = cam.project(np.array([P(t, H) for t in ths]))
    tpl = {"type": "curved", "top": top.tolist(), "bottom": bot.tolist(), "image_size": [w, h], "u_mode": "equal"}
    geom = dict(radius=radius, th0=th0, th1=th1, cz=cz, H=H)
    return cam, geom, tpl, (radius * (th1 - th0), H)


def render_cylinder(base, content, cam: Camera, g):
    """光线-圆柱求交渲染弧形屏真值（4x 超采样边缘）。"""
    h, w = base.shape[:2]
    ss = 2
    big = Camera(w * ss, h * ss, cam.f * ss, cam.C, (0, 0, 1))
    big.R, big.t = cam.R, cam.t
    big.K = np.array([[cam.f * ss, 0, (w * ss - 1) / 2], [0, cam.f * ss, (h * ss - 1) / 2], [0, 0, 1]])
    d = big.rays()
    C = cam.C
    ox, oz = C[0], C[2] - g["cz"]
    dx, dz = d[..., 0], d[..., 2]
    a = dx * dx + dz * dz
    b = 2 * (ox * dx + oz * dz)
    c = ox * ox + oz * oz - g["radius"] ** 2
    disc = b * b - 4 * a * c
    ok = disc > 0
    sq = np.sqrt(np.where(ok, disc, 0))
    t = (-b - sq) / (2 * a)
    X = C[0] + t * dx
    Y = C[1] + t * d[..., 1]
    Z = C[2] + t * dz
    th = np.arctan2(X, g["cz"] - Z)
    u = (th - g["th0"]) / (g["th1"] - g["th0"])
    v = Y / g["H"]
    ok &= (t > 0) & (u >= 0) & (u <= 1) & (v >= 0) & (v <= 1)
    ch, cw = content.shape[:2]
    mx = (u * cw - 0.5).astype(np.float32)
    my = (v * ch - 0.5).astype(np.float32)
    layer = cv2.remap(content, mx, my, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
    a_big = ok.astype(np.float32)
    layer = cv2.resize(layer * a_big[..., None], (w, h), interpolation=cv2.INTER_AREA)
    alpha = cv2.resize(a_big, (w, h), interpolation=cv2.INTER_AREA)
    with np.errstate(invalid="ignore", divide="ignore"):
        layer = np.where(alpha[..., None] > 1e-6, layer / np.maximum(alpha[..., None], 1e-6), 0)
    out = base * (1 - alpha[..., None]) + layer * alpha[..., None]
    return out.astype(np.float32), alpha


def make_test_video(path, w=960, h=540, frames=60, fps=25, seed=0, night=True):
    """合成航拍式镜头：大幅城市背景 + 平面屏（屏内内容逐帧变化），相机做平移 + 推近 + 轻微旋转。

    返回每帧屏幕四角的真值 [(TL,TR,BR,BL)]。
    """
    from .imgio import VideoWriter
    rng = np.random.default_rng(seed)
    BW, BH = int(w * 1.8), int(h * 1.8)
    canvas = city_background(BW, BH, night=night, seed=seed)
    # 屏幕框（亮边框便于识别）
    q = np.array([[BW * 0.42, BH * 0.36], [BW * 0.62, BH * 0.38], [BW * 0.62, BH * 0.52], [BW * 0.42, BH * 0.53]], np.float32)
    cv2.polylines(canvas, [np.round(q).astype(np.int32)], True, (0.35, 0.35, 0.38), 6, cv2.LINE_AA)
    writer = VideoWriter(path, w, h, fps, crf=16)
    gts = []
    for t in range(frames):
        a = t / max(frames - 1, 1)
        zoom = 1.0 + 0.25 * a
        ang = math.radians(3 * math.sin(a * math.pi))
        cx = BW * (0.45 + 0.1 * a) + 2 * math.sin(t * 0.7)
        cy = BH * (0.47 - 0.04 * a) + 1.5 * math.cos(t * 0.9)
        S = np.array([[zoom * math.cos(ang), -zoom * math.sin(ang), 0], [zoom * math.sin(ang), zoom * math.cos(ang), 0], [0, 0, 1]])
        Tm = np.array([[1, 0, w / 2], [0, 1, h / 2], [0, 0, 1]]) @ S @ np.array([[1, 0, -cx], [0, 1, -cy], [0, 0, 1]])
        P = np.array([[1, 0, 0], [0, 1, 0], [2e-5 * math.sin(a * 3), 1e-5 * a, 1]])
        Hm = P @ Tm
        frame = canvas.copy()
        # 屏幕内部：逐帧变化的内容（跟踪不能依赖它）
        content = np.zeros((200, 400, 3), np.float32)
        content[:] = rng.uniform(0.2, 0.9, 3)
        cv2.circle(content, (int(200 + 150 * math.sin(t * 0.3)), 100), 60, rng.uniform(0, 1, 3).tolist(), -1)
        src = np.array([[0, 0], [400, 0], [400, 200], [0, 200]], np.float32)
        Hc = cv2.getPerspectiveTransform(src, q)
        warped = cv2.warpPerspective(content, Hc, (BW, BH))
        m = np.zeros((BH, BW), np.float32)
        cv2.fillPoly(m, [np.round(q * 16).astype(np.int32)], 1.0, cv2.LINE_AA, shift=4)
        frame = frame * (1 - m[..., None]) + warped * m[..., None]
        out = cv2.warpPerspective(frame, Hm, (w, h), flags=cv2.INTER_LINEAR)
        out = np.clip(out + rng.normal(0, 0.008, out.shape).astype(np.float32), 0, 1)
        writer.write(out)
        gts.append(cv2.perspectiveTransform(q.reshape(-1, 1, 2).astype(np.float64), Hm).reshape(-1, 2))
    writer.close()
    return gts

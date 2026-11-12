"""画面分析：场景识别、噪声估计、环境光估计、屏幕候选检测。"""
from __future__ import annotations

from typing import Optional

import math

import cv2
import numpy as np

LUMA = np.array([0.114, 0.587, 0.299], np.float32)  # BGR


def luminance(img: np.ndarray) -> np.ndarray:
    return img @ LUMA


def estimate_noise(img: np.ndarray, region: np.ndarray | None = None) -> float:
    """Immerkær 快速噪声估计：返回单通道噪声 sigma（[0,1] 尺度，三通道平均）。

    只在平坦区（平滑后梯度低）且非过曝 / 非死黑处统计；region 可限定统计区域（整图大小掩码）。
    """
    img = img.astype(np.float64)
    if region is not None:
        region = region > 0.5
    if max(img.shape[:2]) > 2000 and region is None:
        # 噪声估计需要原始像素，不缩放；只裁中心区域以控制耗时
        h, w = img.shape[:2]
        img = img[max(0, h // 2 - 700): h // 2 + 700, max(0, w // 2 - 1000): w // 2 + 1000]
    g = img @ LUMA.astype(np.float64)
    gb = cv2.GaussianBlur(g, (0, 0), 2.0)  # 在平滑图上找“平坦区”，否则会偏向挑出噪声小的像素
    gx = cv2.Sobel(gb, cv2.CV_64F, 1, 0)[1:-1, 1:-1]
    gy = cv2.Sobel(gb, cv2.CV_64F, 0, 1)[1:-1, 1:-1]
    grad = np.hypot(gx, gy)
    inner = g[1:-1, 1:-1]
    m = (inner > 0.06) & (inner < 0.94)
    if region is not None:
        m &= region[1:-1, 1:-1]
    if m.sum() < 500:
        m = np.ones_like(m)
    m &= grad <= np.percentile(grad[m], 50)
    k = np.array([[1, -2, 1], [-2, 4, -2], [1, -2, 1]], np.float64)
    sig = []
    for c in range(3):
        conv = cv2.filter2D(img[..., c], -1, k)[1:-1, 1:-1]
        sig.append(math.sqrt(math.pi / 2) / 6 * np.abs(conv[m]).mean())
    return float(np.mean(sig))


def classify_scene(img: np.ndarray) -> dict:
    """白天 / 黄昏 / 夜景。返回 {scene, scores, stats}。"""
    small = cv2.resize(img, (256, max(1, int(256 * img.shape[0] / img.shape[1]))), interpolation=cv2.INTER_AREA)
    L = luminance(small)
    h = small.shape[0]
    top = small[: h // 3]
    hsv = cv2.cvtColor(top, cv2.COLOR_BGR2HSV)  # float: H∈[0,360], S∈[0,1], V∈[0,1]
    med = float(np.median(L))
    top_med = float(np.median(luminance(top)))
    p95 = float(np.percentile(L, 95))
    # 天空暖色（橙/粉/紫）占比
    H, S, V = hsv[..., 0], hsv[..., 1], hsv[..., 2]
    warm = ((H < 50) | (H > 280)) & (S > 0.25) & (V > 0.25)
    blue = (H > 190) & (H < 250) & (S > 0.2)
    warm_ratio = float(warm.mean())
    blue_ratio = float(blue.mean())
    bright_spots = float((L > 0.85).mean())

    night = np.clip((0.28 - med) / 0.18, 0, 1) * 0.7 + np.clip((0.3 - top_med) / 0.2, 0, 1) * 0.3
    dusk = np.clip(warm_ratio / 0.18, 0, 1) * 0.6 + np.clip(1 - abs(top_med - 0.35) / 0.25, 0, 1) * 0.25 + \
        np.clip((p95 - med) / 0.6, 0, 1) * 0.15
    day = np.clip((med - 0.25) / 0.2, 0, 1) * 0.7 + np.clip((top_med - 0.45) / 0.25, 0, 1) * 0.3
    if night > 0.6:
        dusk *= 0.6
    scores = {"night": float(night), "dusk": float(dusk), "day": float(day)}
    scene = max(scores, key=scores.get)
    return {
        "scene": scene,
        "scores": scores,
        "stats": {"median_luma": med, "sky_luma": top_med, "p95": p95, "warm_sky": warm_ratio,
                  "blue_sky": blue_ratio, "highlights": bright_spots},
    }


def ambient_color(img: np.ndarray, mask: np.ndarray | None = None) -> np.ndarray:
    """估计环境光颜色（BGR，归一化到亮度 1），用于贴入画面的色温匹配。

    在给定区域（通常是屏幕外围环带）用 shades-of-gray（p=6）估计，排除过曝和极暗像素。
    """
    px = img.reshape(-1, 3)
    L = px @ LUMA
    sel = (L > 0.04) & (L < 0.95)
    if mask is not None:
        sel &= mask.reshape(-1) > 0.5
    if sel.sum() < 50:
        return np.ones(3, np.float32)
    p = 6
    c = np.power(np.mean(np.power(px[sel], p), axis=0), 1 / p)
    c = c / max(float(c @ LUMA), 1e-6)
    return c.astype(np.float32)


def ring_mask(mask: np.ndarray, inner: int, outer: int) -> np.ndarray:
    d = cv2.distanceTransform((mask <= 0.5).astype(np.uint8), cv2.DIST_L2, 3)
    return ((d > inner) & (d <= outer)).astype(np.float32)


def _profile_grad(img: np.ndarray, p: np.ndarray, n: np.ndarray, t: np.ndarray, R: int, half_w: int = 3,
                  inside_aware: bool = False):
    """沿法线 n 在 p 两侧 ±R 采样（沿切线 t 方向 ±half_w 平均），返回 (偏移数组, 颜色梯度幅值)。

    inside_aware=True 时 n 须指向屏幕内部：梯度再乘以“该位置内侧颜色与屏幕内部颜色的相似度”，
    这样屏幕边框外沿（内侧是边框而不是屏幕）不会被当成屏幕边缘。
    """
    offs = np.arange(-R, R + 1, dtype=np.float32)
    lat = np.arange(-half_w, half_w + 1, dtype=np.float32)
    xs = p[0] + offs[:, None] * n[0] + lat[None, :] * t[0]
    ys = p[1] + offs[:, None] * n[1] + lat[None, :] * t[1]
    samp = cv2.remap(img, xs.astype(np.float32), ys.astype(np.float32), cv2.INTER_LINEAR,
                     borderMode=cv2.BORDER_REPLICATE).mean(axis=1)  # (2R+1, 3)
    samp = cv2.GaussianBlur(samp.reshape(-1, 1, 3), (1, 5), 0).reshape(-1, 3)
    g = np.abs(np.gradient(samp, axis=0)).sum(axis=1)
    if inside_aware:
        ref = np.median(samp[offs >= R * 0.5], axis=0)          # 屏幕内部参考色
        inner = np.roll(samp, -3, axis=0)                       # 每个位置向内 3px 的颜色
        inner[-3:] = samp[-1]
        outer = np.roll(samp, 3, axis=0)
        outer[:3] = samp[0]
        d_in = np.abs(inner - ref).sum(axis=1)
        d_out = np.abs(outer - ref).sum(axis=1)
        g = g * np.clip((d_out - d_in) / (d_out + d_in + 1e-4), 0.05, 1.0)
    return offs, g


def snap_points(img: np.ndarray, top: list, bottom: list, radius: int | None = None) -> dict:
    """标定点吸附到屏幕真实边缘（亮度 / 颜色突变处）。

    每个点沿所在边线的法线方向搜索 ±radius 像素，取“梯度 × 距离先验”最大的位置；
    边缘不明显（峰值不突出）时保持原位。两端列的点同时沿左 / 右边线法线吸附（角点两条边）。
    返回 {"top", "bottom", "moved": [每点移动像素], "confidence": [每点峰值显著度]}。
    """
    h, w = img.shape[:2]
    # 只做“微调”：用户点击误差通常在几像素内。范围太大会跳到边框外沿、旁边灯箱等更强的边
    R = int(radius or max(6, round(0.0025 * math.hypot(w, h))))
    T = np.asarray(top, np.float64).copy()
    B = np.asarray(bottom, np.float64).copy()
    n = len(T)
    moved_t, moved_b, conf_t, conf_b = [0.0] * n, [0.0] * n, [0.0] * n, [0.0] * n

    def snap_one(P, tangent, inward):
        t = tangent / (np.linalg.norm(tangent) + 1e-9)
        nn = np.array([-t[1], t[0]])
        if np.dot(nn, inward) < 0:
            nn = -nn
        offs, g = _profile_grad(img, P, nn, t, R)
        prior = np.exp(-0.5 * (offs / (0.5 * R)) ** 2)
        score = g * prior
        i = int(np.argmax(score))
        base = float(np.median(g)) + 1e-6
        sig = float(g[i] / base)
        if sig < 3.0:
            return P, 0.0, sig
        # 亚像素抛物线
        off = offs[i]
        if 0 < i < len(g) - 1:
            a, b_, c = score[i - 1], score[i], score[i + 1]
            den = a - 2 * b_ + c
            if abs(den) > 1e-12:
                off += 0.5 * (a - c) / den
        return P + nn * off, float(abs(off)), sig

    for _ in range(2):
        for i in range(n):
            for arr, other, moved, conf in ((T, B, moved_t, conf_t), (B, T, moved_b, conf_b)):
                j0, j1 = max(0, i - 1), min(n - 1, i + 1)
                tangent = arr[j1] - arr[j0]
                inward = other[i] - arr[i]
                p_new, mv, sig = snap_one(arr[i], tangent, inward)
                if i in (0, n - 1):
                    side = other[i] - arr[i]
                    inward_side = arr[1] - arr[0] if i == 0 else arr[n - 2] - arr[n - 1]
                    p_new, mv2, sig2 = snap_one(p_new, side, inward_side)
                    mv = math.hypot(*(p_new - arr[i]))
                    sig = min(sig, sig2) if sig2 > 0 else sig
                moved[i] = float(math.hypot(*(p_new - np.asarray(arr[i])))) + moved[i]
                conf[i] = sig
                arr[i] = p_new
    return {"top": T.round(2).tolist(), "bottom": B.round(2).tolist(),
            "moved": [round(a, 2) for a in moved_t + moved_b],
            "confidence": [round(a, 2) for a in conf_t + conf_b]}


def _track_edge(img: np.ndarray, a: np.ndarray, b: np.ndarray, R: int, samples: int = 13,
                inside: Optional[np.ndarray] = None, carry: bool = True) -> Optional[np.ndarray]:
    """沿边 a→b 取边缘点（不足 5 个时 None）。inside 为屏幕内部一点。

    carry=True：从两端向中间跟踪（沿用上一点偏移），能跟上镜头畸变造成的弯曲边；
    carry=False：每点独立搜索，不会因某段低对比度而累积漂移（直线边吸附用）。
    """
    L = np.linalg.norm(b - a)
    if L < 30:
        return None
    t = (b - a) / L
    nrm = np.array([-t[1], t[0]])
    if inside is not None and np.dot(inside - (a + b) / 2, nrm) < 0:
        nrm = -nrm
    fs = np.linspace(0.1, 0.9, samples)
    found: dict = {}
    for order in (range(len(fs)), range(len(fs) - 1, -1, -1)):
        off_prev = 0.0
        for k in order:
            p = a + (b - a) * fs[k] + nrm * off_prev
            offs, g = _profile_grad(img, p, nrm, t, R, inside_aware=inside is not None)
            prior = np.exp(-0.5 * (offs / (0.6 * R)) ** 2)
            i = int(np.argmax(g * prior))
            if g[i] / (np.median(g) + 1e-6) < 3.0:
                continue
            o = float(offs[i])
            if 0 < i < len(g) - 1:
                sc = g * prior
                den = sc[i - 1] - 2 * sc[i] + sc[i + 1]
                if abs(den) > 1e-12:
                    o += 0.5 * (sc[i - 1] - sc[i + 1]) / den
            if carry:
                off_prev += o
                found.setdefault(k, []).append(off_prev)
            else:
                found.setdefault(k, []).append(o)
    if len(found) < 5:
        return None
    return np.array([a + (b - a) * fs[k] + nrm * float(np.median(v)) for k, v in sorted(found.items())])


def _fit_line(P: np.ndarray, tol: float = 1.5):
    """RANSAC 直线拟合 + 内点精化，返回 (点, 方向)；内点不足一半时返回 None。"""
    best = None
    n = len(P)
    for i in range(n):
        for j in range(i + 1, n):
            d = P[j] - P[i]
            L = np.linalg.norm(d)
            if L < 1e-6:
                continue
            nrm = np.array([-d[1], d[0]]) / L
            inl = np.abs((P - P[i]) @ nrm) <= tol
            if best is None or inl.sum() > best.sum():
                best = inl
    if best is None or best.sum() < max(3, n // 2):
        return None
    Q = P[best]
    c = Q.mean(0)
    _, _, vt = np.linalg.svd(Q - c)
    return c, vt[0]


def _intersect(l1, l2) -> Optional[np.ndarray]:
    (p, d), (q, e) = l1, l2
    A = np.array([d, -e]).T
    if abs(np.linalg.det(A)) < 1e-9:
        return None
    s = np.linalg.solve(A, q - p)
    return p + d * s[0]


def snap_template(img: np.ndarray, tpl_dict: dict, radius: int | None = None) -> dict:
    """标定吸附（推荐）：直线边用“多点跟踪 + 直线拟合”，角点 / 折点取相邻边线的交点（亚像素、不受角落杂物影响）；
    弧形屏的上下弧线中间点逐点吸附。返回新的 top / bottom 及每个点的移动量。"""
    h, w = img.shape[:2]
    R = int(radius or max(6, round(0.0025 * math.hypot(w, h))))
    typ = tpl_dict.get("type", "flat")
    T = np.asarray(tpl_dict["top"], float)
    B = np.asarray(tpl_dict["bottom"], float)
    n = len(T)
    T0, B0 = T.copy(), B.copy()

    center = np.r_[T, B].mean(0)

    def line_or_none(a, b):
        P = _track_edge(img, a, b, R, inside=center, carry=False)
        return _fit_line(P) if P is not None else None

    side_l, side_r = line_or_none(T[0], B[0]), line_or_none(T[-1], B[-1])
    if typ in ("flat", "folded"):
        tops = [line_or_none(T[i], T[i + 1]) for i in range(n - 1)]
        bots = [line_or_none(B[i], B[i + 1]) for i in range(n - 1)]
        for arr, lines in ((T, tops), (B, bots)):
            new = arr.copy()
            for i in range(n):
                if i == 0:
                    pair = (side_l, lines[0])
                elif i == n - 1:
                    pair = (lines[-1], side_r)
                else:
                    pair = (lines[i - 1], lines[i])
                if pair[0] is None or pair[1] is None:
                    continue
                (c1, d1), (c2, d2) = pair
                ang = math.degrees(math.acos(min(1.0, abs(float(d1 @ d2)))))
                if ang < 25:
                    # 两条边几乎共线（折角屏顶边在图中常见）：交点对误差极敏感 → 保留用户在边方向上的位置，
                    # 只把点投影到两条拟合边线上（取平均），即只修正垂直于边的方向
                    proj = [c + d * float((arr[i] - c) @ d) for c, d in pair]
                    p = (proj[0] + proj[1]) / 2
                else:
                    p = _intersect(*pair)
                if p is not None and np.linalg.norm(p - arr[i]) <= 2.5 * R:
                    new[i] = p
            arr[:] = new
    else:
        r = snap_points(img, T.tolist(), B.tolist(), R)
        T, B = np.asarray(r["top"], float), np.asarray(r["bottom"], float)
        # 两端点：弧线端部切线与左右竖边的交点
        for arr in (T, B):
            for i, j, side in ((0, 1, side_l), (n - 1, n - 2, side_r)):
                if side is None:
                    continue
                tang = (arr[i], (arr[i] - arr[j]) / (np.linalg.norm(arr[i] - arr[j]) + 1e-9))
                p = _intersect(side, tang)
                if p is not None and np.linalg.norm(p - arr[i]) <= 2.5 * R:
                    arr[i] = p
    moved = np.r_[np.linalg.norm(T - T0, axis=1), np.linalg.norm(B - B0, axis=1)]
    return {"top": T.round(2).tolist(), "bottom": B.round(2).tolist(), "moved": moved.round(2).tolist()}


def estimate_lens_k1(img: np.ndarray, tpl_dict: dict, samples: int = 11) -> dict:
    """由“物理上是直线”的屏幕边估计镜头径向畸变 k1（与 geometry.Distortion 同一定义）。

    直线边：平面屏四条边；折角屏每个面的上 / 下边与所有竖向折缝；弧形屏只有竖向母线（每一列）。
    每条边均匀取点并吸附到真实边缘，再一维搜索 k1 使去畸变后各边最直。
    返回 {"k1", "rms_before", "rms_after", "edges"}；改善不明显时 k1=0。
    """
    from .geometry import Distortion

    h, w = img.shape[:2]
    top = np.asarray(tpl_dict["top"], float)
    bot = np.asarray(tpl_dict["bottom"], float)
    typ = tpl_dict.get("type", "flat")
    edges = [(top[i], bot[i]) for i in range(len(top))]                      # 竖边 / 折缝
    if typ in ("flat", "folded"):
        edges += [(top[i], top[i + 1]) for i in range(len(top) - 1)]
        edges += [(bot[i], bot[i + 1]) for i in range(len(bot) - 1)]
    R = max(5, round(0.002 * math.hypot(w, h)))
    pts_per_edge = []
    fs = np.linspace(0.06, 0.94, samples)
    for a, b in edges:
        L = np.linalg.norm(b - a)
        if L < 40:
            continue
        t = (b - a) / L
        nrm = np.array([-t[1], t[0]])
        # 从两端向中间“跟踪”：端点附近弦≈边，逐点沿用上一点的法向偏移作为预测，弯曲的边也能跟上
        found = {}
        for order in (range(len(fs)), range(len(fs) - 1, -1, -1)):
            off_prev = 0.0
            for k in order:
                p = a + (b - a) * fs[k] + nrm * off_prev
                offs, g = _profile_grad(img, p, nrm, t, R)
                prior = np.exp(-0.5 * (offs / (0.6 * R)) ** 2)
                i = int(np.argmax(g * prior))
                if g[i] / (np.median(g) + 1e-6) < 3.0:
                    continue
                off_prev += float(offs[i])
                found.setdefault(k, []).append(off_prev)
        pts = [a + (b - a) * fs[k] + nrm * float(np.mean(v)) for k, v in sorted(found.items())]
        if len(pts) >= 5:
            pts_per_edge.append(np.array(pts))
    if len(pts_per_edge) < 2:
        return {"k1": 0.0, "rms_before": None, "rms_after": None, "edges": len(pts_per_edge)}

    def rms(k1):
        d = Distortion(k1, 0.0, w, h)
        r = []
        for P in pts_per_edge:
            U = d.undistort(P)
            c = U - U.mean(0)
            _, s, _ = np.linalg.svd(c, full_matrices=False)
            r.append(s[-1] ** 2 / len(U))
        return math.sqrt(float(np.mean(r)))

    ks = np.linspace(-0.3, 0.3, 121)
    vals = [rms(k) for k in ks]
    i = int(np.argmin(vals))
    lo, hi = ks[max(0, i - 1)], ks[min(len(ks) - 1, i + 1)]
    for _ in range(30):  # 黄金分割细化
        m1, m2 = lo + (hi - lo) * 0.382, lo + (hi - lo) * 0.618
        if rms(m1) < rms(m2):
            hi = m2
        else:
            lo = m1
    k = (lo + hi) / 2
    before, after = rms(0.0), rms(k)
    if not (after < before * 0.8 and before - after > 0.3):
        k = 0.0
    return {"k1": round(float(k), 4), "rms_before": round(before, 2), "rms_after": round(rms(k), 2),
            "edges": len(pts_per_edge)}


def detect_screen_candidates(img: np.ndarray, max_candidates: int = 5) -> list[dict]:
    """自动框选：寻找“比周围亮、色彩丰富、近似四边形”的发光区域，给出四角初值。

    这是标定的起点，不替代人工确认；结果按得分排序。
    """
    small, s = img, 1.0
    if max(img.shape[:2]) > 1400:
        s = 1400 / max(img.shape[:2])
        small = cv2.resize(img, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
    L = luminance(small)
    hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)
    local = cv2.GaussianBlur(L, (0, 0), max(small.shape[:2]) / 25)
    emissive = np.clip((L - local) * 3 + L * 0.5 + hsv[..., 1] * 0.3, 0, 1)
    thr = max(0.35, float(np.percentile(emissive, 92)))
    m = (emissive > thr).astype(np.uint8) * 255
    k = max(3, int(max(small.shape[:2]) / 150) | 1)
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (k * 3, k * 3)))
    m = cv2.morphologyEx(m, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (k, k)))
    contours, _ = cv2.findContours(m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    area_img = small.shape[0] * small.shape[1]
    out = []
    for c in contours:
        a = cv2.contourArea(c)
        if a < area_img * 0.004:
            continue
        hull = cv2.convexHull(c)
        peri = cv2.arcLength(hull, True)
        quad = None
        for eps in np.linspace(0.01, 0.08, 15):
            ap = cv2.approxPolyDP(hull, eps * peri, True)
            if len(ap) == 4:
                quad = ap.reshape(4, 2).astype(np.float64)
                break
        if quad is None:
            rect = cv2.minAreaRect(c)
            quad = cv2.boxPoints(rect).astype(np.float64)
        rectangularity = a / max(cv2.contourArea(quad.astype(np.float32)), 1)
        # 排序为 TL, TR, BR, BL
        ctr = quad.mean(0)
        ang = np.arctan2(quad[:, 1] - ctr[1], quad[:, 0] - ctr[0])
        quad = quad[np.argsort(ang)]  # 从左上方逆时针（图像坐标下为顺时针）
        idx = int(np.argmin(quad.sum(1)))
        quad = np.roll(quad, -idx, axis=0)
        score = float(min(rectangularity, 1.0) * math.sqrt(a / area_img))
        q = (quad / s).tolist()
        out.append({"top": [q[0], q[1]], "bottom": [q[3], q[2]], "score": score})
    out.sort(key=lambda d: -d["score"])
    return out[:max_candidates]

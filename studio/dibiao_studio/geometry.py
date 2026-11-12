"""屏幕几何：把任意形态的大屏统一表达为「一串共享边的平面面片（strip）」。

标定方式（三种屏幕共用一套数据结构）：
    top    = 屏幕上边缘的点列（从左到右）
    bottom = 屏幕下边缘的点列（与 top 一一对应，同一列的上下点在屏幕的同一条竖直拼缝上）

- flat   平面屏：top=[左上, 右上]，bottom=[左下, 右下] → 1 个面片，四点透视变换
- folded 拐角 / 折角 / 多折面屏：每个折点在 top/bottom 各标一个点 → K 个平面面片，
         每个面片独立做透视映射，折缝两侧共享同一条边，并在折缝处做坐标级融合
- curved 弧形屏：沿弧线标若干控制点（建议按 LED 箱体拼缝等物理间距标）→ 中心向心
         Catmull-Rom 样条拟合上下弧线 → 加密为几十到上百条细面片（曲面 UV 映射）

客户画面横向坐标 u 按「物理宽度」而不是图像像素长度分配，避免透视近大远小、
折面朝向不同造成的文字压扁 / 拉伸：
- physical  使用标定模板里填写的每段物理宽度（米）
- equal     控制点等物理间距（弧形屏按箱体拼缝标点时最准确）
- auto      单视图矩形宽高比估计（Zhang & He 2007），由画面透视反推每个面的真实宽高比；
            同一张照片的所有面共用一个焦距估计，保证各面一致

每个面片内部用独立单应矩阵（homography）映射，所以面片内的透视（包括竖直方向的近大远小）
都是精确的；不存在“整体矩形硬拉伸”。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, asdict
from typing import Optional

import cv2
import numpy as np

SCREEN_TYPES = ("flat", "curved", "folded")
U_MODES = ("auto", "equal", "physical")


# ------------------------------------------------------------------ template

@dataclass
class ScreenTemplate:
    type: str = "flat"
    top: list = field(default_factory=list)
    bottom: list = field(default_factory=list)
    image_size: Optional[list] = None          # 标定时底图的 [宽, 高]，用于换分辨率复用
    u_mode: str = "auto"
    segment_widths: Optional[list] = None      # 每段物理宽度（米），len = 点数-1
    physical_size: Optional[list] = None       # 屏幕整体物理 [宽, 高]（米），决定客户画面宽高比
    focal_px: Optional[float] = None           # 已知焦距（像素，按 image_size 计）；不填则自动估计
    turn_angle: Optional[float] = None         # 整屏转角（度）：最左一段与最右一段的水平夹角，L 型拐角屏 = 90
    occluders: list = field(default_factory=list)  # 屏幕前遮挡物多边形（路灯杆、树等），替换时保留原图
    name: str = ""
    id: str = ""
    notes: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> "ScreenTemplate":
        known = {k: d[k] for k in cls.__dataclass_fields__ if k in d}
        t = cls(**known)
        t.validate()
        return t

    def to_dict(self) -> dict:
        return asdict(self)

    def validate(self) -> None:
        if self.type not in SCREEN_TYPES:
            raise ValueError(f"屏幕类型必须是 {SCREEN_TYPES}")
        if self.u_mode not in U_MODES:
            raise ValueError(f"u_mode 必须是 {U_MODES}")
        if len(self.top) != len(self.bottom):
            raise ValueError("上边缘与下边缘的点数必须相同（每个折点/控制点上下各一个）")
        if len(self.top) < 2:
            raise ValueError("至少需要 2 列点（左右两侧）")
        if self.type == "flat" and len(self.top) != 2:
            raise ValueError("平面屏只需四个角：上边 2 点、下边 2 点")
        if self.type == "curved" and len(self.top) < 3:
            raise ValueError("弧形屏每条边至少 3 个控制点")
        if self.type == "folded" and len(self.top) < 3:
            raise ValueError("拐角/折角屏至少 3 列点（含一个折点）")
        n = len(self.top) - 1
        if self.segment_widths is not None:
            if len(self.segment_widths) != n or min(self.segment_widths) <= 0:
                raise ValueError(f"segment_widths 需要 {n} 个正数")
        if self.u_mode == "physical" and not self.segment_widths:
            raise ValueError("physical 模式需要填写每段物理宽度")
        if self.turn_angle is not None and not (5 <= float(self.turn_angle) <= 175):
            raise ValueError("整屏转角应在 5°–175° 之间（L 型拐角屏为 90°）")
        if self.turn_angle is not None and self.type == "flat":
            raise ValueError("平面屏没有转角")

    def scaled_to(self, w: int, h: int) -> "ScreenTemplate":
        """把模板点位换算到实际图像尺寸（同一机位不同分辨率素材复用模板）。"""
        if not self.image_size:
            return self
        sx, sy = w / float(self.image_size[0]), h / float(self.image_size[1])
        if abs(sx - 1) < 1e-9 and abs(sy - 1) < 1e-9:
            return self
        d = self.to_dict()
        d["top"] = [[x * sx, y * sy] for x, y in self.top]
        d["bottom"] = [[x * sx, y * sy] for x, y in self.bottom]
        d["occluders"] = [[[x * sx, y * sy] for x, y in poly] for poly in self.occluders]
        d["image_size"] = [w, h]
        if self.focal_px:
            d["focal_px"] = self.focal_px * math.sqrt(sx * sy)
        return ScreenTemplate.from_dict(d)

    def with_points(self, top, bottom) -> "ScreenTemplate":
        d = self.to_dict()
        d["top"], d["bottom"] = [list(map(float, p)) for p in top], [list(map(float, p)) for p in bottom]
        return ScreenTemplate.from_dict(d)


# ------------------------------------------------------------------ lens distortion

@dataclass
class Distortion:
    """径向畸变（与 OpenCV k1/k2 同形式），坐标按半对角线归一化，k1∈[-0.3,0.3] 较直观。

    标定点、输出像素都在「实际（有畸变）」图像坐标；几何计算在「理想（无畸变）」坐标。
    k1<0 桶形（广角 / 航拍常见），k1>0 枕形。
    """

    k1: float = 0.0
    k2: float = 0.0
    width: int = 1
    height: int = 1

    @property
    def active(self) -> bool:
        return abs(self.k1) > 1e-6 or abs(self.k2) > 1e-6

    def _norm(self):
        cx, cy = (self.width - 1) / 2.0, (self.height - 1) / 2.0
        s = 0.5 * math.hypot(self.width, self.height)
        return cx, cy, s

    def distort(self, pts: np.ndarray) -> np.ndarray:
        if not self.active:
            return pts
        cx, cy, s = self._norm()
        x, y = (pts[..., 0] - cx) / s, (pts[..., 1] - cy) / s
        r2 = x * x + y * y
        f = 1 + self.k1 * r2 + self.k2 * r2 * r2
        return np.stack([x * f * s + cx, y * f * s + cy], axis=-1)

    def undistort(self, pts: np.ndarray, iters: int = 10) -> np.ndarray:
        if not self.active:
            return pts
        cx, cy, s = self._norm()
        xd, yd = (pts[..., 0] - cx) / s, (pts[..., 1] - cy) / s
        x, y = xd.copy(), yd.copy()
        for _ in range(iters):
            r2 = x * x + y * y
            f = 1 + self.k1 * r2 + self.k2 * r2 * r2
            x, y = xd / f, yd / f
        return np.stack([x * s + cx, y * s + cy], axis=-1)


# ------------------------------------------------------------------ curves

def catmull_rom(points: np.ndarray, ts_per_seg: list[np.ndarray]) -> list[np.ndarray]:
    """中心向心 Catmull-Rom（alpha=0.5），无尖点、无自交，穿过每个控制点。

    ts_per_seg[i] 为第 i 段内的参数 t∈[0,1]。返回每段采样点。
    """
    P = np.asarray(points, dtype=np.float64)
    n = len(P)
    ext = np.vstack([2 * P[0] - P[1], P, 2 * P[-1] - P[-2]])
    out = []
    for i in range(n - 1):
        p0, p1, p2, p3 = ext[i], ext[i + 1], ext[i + 2], ext[i + 3]

        def tj(ti, a, b):
            return ti + max(np.linalg.norm(b - a), 1e-6) ** 0.5

        t0 = 0.0
        t1 = tj(t0, p0, p1)
        t2 = tj(t1, p1, p2)
        t3 = tj(t2, p2, p3)
        t = t1 + np.asarray(ts_per_seg[i])[:, None] * (t2 - t1)
        a1 = (t1 - t) / (t1 - t0) * p0 + (t - t0) / (t1 - t0) * p1
        a2 = (t2 - t) / (t2 - t1) * p1 + (t - t1) / (t2 - t1) * p2
        a3 = (t3 - t) / (t3 - t2) * p2 + (t - t2) / (t3 - t2) * p3
        b1 = (t2 - t) / (t2 - t0) * a1 + (t - t0) / (t2 - t0) * a2
        b2 = (t3 - t) / (t3 - t1) * a2 + (t - t1) / (t3 - t1) * a3
        c = (t2 - t) / (t2 - t1) * b1 + (t - t1) / (t2 - t1) * b2
        out.append(c)
    return out


# ------------------------------------------------------------------ single-view rectangle aspect

def _zhang_terms(quad: np.ndarray, center: tuple[float, float]):
    """quad 顺序 TL,TR,BR,BL（理想坐标）。返回 (n2, n3)，坐标以主点为原点。"""
    cx, cy = center
    tl, tr, br, bl = [np.array([p[0] - cx, p[1] - cy, 1.0]) for p in quad]
    m1, m2, m3, m4 = tl, tr, bl, br
    c14 = np.cross(m1, m4)
    d2 = np.dot(np.cross(m2, m4), m3)
    d3 = np.dot(np.cross(m3, m4), m2)
    if abs(d2) < 1e-12 or abs(d3) < 1e-12:
        return None
    k2 = np.dot(c14, m3) / d2
    k3 = np.dot(c14, m2) / d3
    return k2 * m2 - m1, k3 * m3 - m1


def estimate_focal(quads: list[np.ndarray], center, diag: float) -> Optional[float]:
    """从若干个“物理上是矩形”的四边形估计焦距（像素）。透视不足时返回 None。"""
    fs, ws = [], []
    for q in quads:
        terms = _zhang_terms(q, center)
        if terms is None:
            continue
        n2, n3 = terms
        den = n2[2] * n3[2]
        # 透视太弱（对边接近平行）时焦距不可观测
        rel = abs(n2[2]) * diag / (np.linalg.norm(n2[:2]) + 1e-9) + abs(n3[2]) * diag / (np.linalg.norm(n3[:2]) + 1e-9)
        if abs(den) < 1e-15 or rel < 0.05:
            continue
        f2 = -(n2[0] * n3[0] + n2[1] * n3[1]) / den
        if f2 <= 0:
            continue
        f = math.sqrt(f2)
        if 0.2 * diag < f < 8 * diag:
            fs.append(f)
            ws.append(rel)
    if not fs:
        return None
    order = np.argsort(fs)
    fs, ws = np.array(fs)[order], np.array(ws)[order]
    cw = np.cumsum(ws)
    return float(fs[np.searchsorted(cw, cw[-1] / 2)])  # 按透视强度加权的中位数


def _vanishing_point(quad: np.ndarray) -> Optional[np.ndarray]:
    """面片上、下边线的交点（齐次坐标，可能在无穷远）。quad 顺序 TL,TR,BR,BL。"""
    tl, tr, br, bl = [np.array([p[0], p[1], 1.0]) for p in quad]
    v = np.cross(np.cross(tl, tr), np.cross(bl, br))
    n = np.linalg.norm(v)
    return v / n if n > 1e-12 else None


def focal_from_turn_angle(q_first: np.ndarray, q_last: np.ndarray, center, diag: float,
                          turn_deg: float) -> Optional[float]:
    """两端面夹角已知（L 型拐角屏 = 90°）时，由两端面水平消失点反推焦距。

    两个面的水平方向 d_i ∝ K⁻¹·v_i，夹角已知 → 关于 f 的一元方程，二分求解。
    比单个矩形的焦距估计稳定得多（尤其当某一面几乎被侧着看时）。
    """
    v1, v2 = _vanishing_point(q_first), _vanishing_point(q_last)
    if v1 is None or v2 is None:
        return None
    cx, cy = center
    c0 = math.cos(math.radians(turn_deg))

    def cosang(f):
        # 齐次消失点的符号任意但固定 → 带符号余弦随 f 连续变化，可二分
        d1 = np.array([v1[0] - cx * v1[2], v1[1] - cy * v1[2], f * v1[2]])
        d2 = np.array([v2[0] - cx * v2[2], v2[1] - cy * v2[2], f * v2[2]])
        return float(d1 @ d2) / (np.linalg.norm(d1) * np.linalg.norm(d2) + 1e-18)

    lo0, hi0 = 0.15 * diag, 20 * diag
    for target in ({c0, -c0}):
        lo, hi = lo0, hi0
        glo, ghi = cosang(lo) - target, cosang(hi) - target
        if glo * ghi > 0:
            continue
        for _ in range(80):
            mid = math.sqrt(lo * hi)
            gm = cosang(mid) - target
            if (gm > 0) == (ghi > 0):
                hi, ghi = mid, gm
            else:
                lo, glo = mid, gm
        return math.sqrt(lo * hi)
    return None


def rect_aspect(quad: np.ndarray, center, focal: float) -> float:
    """单视图矩形宽高比（宽/高）。"""
    terms = _zhang_terms(quad, center)
    tl, tr, br, bl = [np.asarray(p, float) for p in quad]
    fallback = (np.linalg.norm(tr - tl) + np.linalg.norm(br - bl)) / (np.linalg.norm(bl - tl) + np.linalg.norm(br - tr) + 1e-9)
    if terms is None:
        return float(fallback)
    n2, n3 = terms
    Ainv = np.diag([1.0 / focal, 1.0 / focal, 1.0])
    a, b = Ainv @ n2, Ainv @ n3
    r = float(np.linalg.norm(a) / (np.linalg.norm(b) + 1e-12))
    if not np.isfinite(r) or r <= 0 or r > 50 * max(fallback, 1e-3):
        return float(fallback)
    return r


def vertical_vp(top: np.ndarray, bot: np.ndarray) -> Optional[np.ndarray]:
    """各列（上点→下点）连线的最小二乘公共交点 = 竖直消失点（齐次坐标，可在无穷远）。"""
    L = []
    for t, b in zip(top, bot):
        l = np.cross([t[0], t[1], 1.0], [b[0], b[1], 1.0])
        n = math.hypot(l[0], l[1])
        if n > 1e-9:
            L.append(l / n)
    if len(L) < 2:
        return None
    _, _, vt = np.linalg.svd(np.array(L))
    return vt[-1]


def match_bottom_along_verticals(top_segs: list, bot_segs: list, vp: np.ndarray) -> list:
    """弧形屏：让每个上边缘采样点的“下端对应点”落在指向竖直消失点的直线上（真实的竖直母线），
    而不是按样条参数机械对应。否则相邻细面片在共享边上的竖向透视不一致，斜线 / 文字会出现错位台阶。

    标定列（控制点）本身保持用户给定的对应关系：每段内对弧长参数做线性校正，首尾严格对齐。
    """
    # 稠密下边缘折线 + 累计弧长
    dense = np.concatenate([bot_segs[0]] + [b[1:] for b in bot_segs[1:]])
    fine = []
    for i in range(len(dense) - 1):
        fine.append(np.linspace(dense[i], dense[i + 1], 5)[:-1])
    fine.append(dense[-1:])
    Bd = np.concatenate(fine)
    sd = np.concatenate([[0.0], np.cumsum(np.linalg.norm(np.diff(Bd, axis=0), axis=1))])
    if sd[-1] <= 0:
        return bot_segs
    Bh = np.c_[Bd, np.ones(len(Bd))]

    def s_at_bottom_point(p):
        return float(sd[int(np.argmin(np.linalg.norm(Bd - p, axis=1)))])

    def intersect(T, guess):
        l = np.cross([T[0], T[1], 1.0], vp)
        val = Bh @ l
        idx = np.nonzero(np.sign(val[:-1]) * np.sign(val[1:]) <= 0)[0]
        if len(idx) == 0:
            return None
        cands = []
        for i in idx:
            a, b = val[i], val[i + 1]
            f = 0.0 if a == b else a / (a - b)
            cands.append(sd[i] + f * (sd[i + 1] - sd[i]))
        return min(cands, key=lambda c: abs(c - guess))

    def eval_s(s):
        s = float(np.clip(s, 0, sd[-1]))
        return np.array([np.interp(s, sd, Bd[:, 0]), np.interp(s, sd, Bd[:, 1])])

    out = []
    for T, B in zip(top_segs, bot_segs):
        s_user0, s_user1 = s_at_bottom_point(B[0]), s_at_bottom_point(B[-1])
        guesses = [s_at_bottom_point(b) for b in B]
        s_vp = [intersect(t, g) for t, g in zip(T, guesses)]
        if any(v is None for v in s_vp):
            out.append(B)
            continue
        k = len(T) - 1
        frac = np.linspace(0, 1, k + 1)
        corr = (1 - frac) * (s_user0 - s_vp[0]) + frac * (s_user1 - s_vp[-1])
        s_new = np.maximum.accumulate(np.array(s_vp) + corr)
        s_new[0], s_new[-1] = s_user0, s_user1
        out.append(np.array([eval_s(s) for s in s_new]))
    return out


def ruled_surface_arclen(top: np.ndarray, bot: np.ndarray, outer_quad: np.ndarray,
                         center, focal: float) -> Optional[np.ndarray]:
    """弧形屏单视图三维重建：屏幕由竖直、等高的母线（LED 箱体竖缝）构成。

    1) 外弦四边形（最左母线 + 最右母线的四个端点）是物理矩形 → 由单应分解得到竖直方向 r2 与尺度；
    2) 每条母线：上端点在射线 rT 上、下端点在射线 rB 上，且 下端 - 上端 = r2（屏高为 1 单位）
       → 最小二乘求两点深度，得到每条母线的三维位置；
    3) 上边缘三维弧长即客户画面横向物理坐标（以屏高为单位）。
    返回每个采样点的累计弧长；退化时返回 None（调用方回退到弦面投影法）。
    """
    cx, cy = center
    K = np.array([[focal, 0, cx], [0, focal, cy], [0, 0, 1.0]])
    Kinv = np.linalg.inv(K)
    dst = outer_quad.astype(np.float32)
    src = np.array([[0, 0], [1, 0], [1, 1], [0, 1]], np.float32)
    H = cv2.getPerspectiveTransform(src, dst).astype(np.float64)
    M = Kinv @ H
    s = 1.0 / (np.linalg.norm(M[:, 1]) + 1e-12)
    r2, t = M[:, 1] * s, M[:, 2] * s
    if t[2] < 0:
        r2, t = -r2, -t
    if t[2] <= 0:
        return None
    rT = (Kinv @ np.c_[top, np.ones(len(top))].T).T
    rB = (Kinv @ np.c_[bot, np.ones(len(bot))].T).T
    X = np.empty((len(top), 3))
    for k in range(len(top)):
        A = np.stack([-rT[k], rB[k]], axis=1)
        lam, *_ = np.linalg.lstsq(A, r2, rcond=None)
        if lam[0] <= 0 or lam[1] <= 0:
            return None
        X[k] = lam[0] * rT[k]
    d = np.linalg.norm(np.diff(X, axis=0), axis=1)
    if not np.all(np.isfinite(d)):
        return None
    return np.concatenate([[0.0], np.cumsum(d)])


# ------------------------------------------------------------------ geometry

@dataclass
class Strip:
    quad: np.ndarray        # (4,2) 理想坐标，顺序 TL, TR, BR, BL
    u0: float
    u1: float
    seam_left: bool
    seam_right: bool
    seg: int = 0            # 所属标定段（折角屏的第几个面 / 弧形屏的第几段）


@dataclass
class ScreenGeometry:
    strips: list
    aspect: float               # 屏幕物理宽 / 高（客户画面应有的宽高比）
    segment_widths: list        # 每段物理宽度（以屏高为 1 的单位，供视频冻结）
    focal: float
    seam_px: float
    distortion: Distortion

    def outline(self) -> np.ndarray:
        """屏幕外轮廓（实际图像坐标）。"""
        top = [s.quad[0] for s in self.strips] + [self.strips[-1].quad[1]]
        bot = [s.quad[3] for s in self.strips] + [self.strips[-1].quad[2]]
        poly = np.array(top + bot[::-1])
        return self.distortion.distort(poly)

    def column_lines(self) -> list:
        """各面片竖向分界线（实际坐标），用于预览。"""
        lines = [np.array([s.quad[0], s.quad[3]]) for s in self.strips] + [np.array([self.strips[-1].quad[1], self.strips[-1].quad[2]])]
        return [self.distortion.distort(l) for l in lines]


def _poly_len(p: np.ndarray) -> float:
    return float(np.sum(np.linalg.norm(np.diff(p, axis=0), axis=1)))


def build_geometry(tpl: ScreenTemplate, width: int, height: int,
                   distortion: Optional[Distortion] = None,
                   frozen_widths: Optional[list] = None,
                   frozen_aspect: Optional[float] = None,
                   frozen_focal: Optional[float] = None,
                   strip_px: float = 10.0) -> ScreenGeometry:
    tpl = tpl.scaled_to(width, height)
    dist = distortion or Distortion(0, 0, width, height)
    dist.width, dist.height = width, height
    top_d = np.asarray(tpl.top, dtype=np.float64)
    bot_d = np.asarray(tpl.bottom, dtype=np.float64)
    top = dist.undistort(top_d)
    bot = dist.undistort(bot_d)
    nseg = len(top) - 1
    center = ((width - 1) / 2.0, (height - 1) / 2.0)
    diag = math.hypot(width, height)

    # ---- 每段的上下边采样
    if tpl.type == "curved":
        seg_samples = []
        for i in range(nseg):
            L = max(np.linalg.norm(top[i + 1] - top[i]), np.linalg.norm(bot[i + 1] - bot[i]))
            m = int(np.clip(math.ceil(L / strip_px), 4, 96))
            seg_samples.append(np.linspace(0, 1, m + 1))
        top_segs = catmull_rom(top, seg_samples)
        bot_segs = catmull_rom(bot, seg_samples)
        vp = vertical_vp(top, bot)
        if vp is not None:
            bot_segs = match_bottom_along_verticals(top_segs, bot_segs, vp)
    else:
        top_segs = [np.array([top[i], top[i + 1]]) for i in range(nseg)]
        bot_segs = [np.array([bot[i], bot[i + 1]]) for i in range(nseg)]

    # ---- 每段弦四边形（物理上是矩形），用于焦距与宽高比估计
    chord_quads = [np.array([top[i], top[i + 1], bot[i + 1], bot[i]]) for i in range(nseg)]
    if frozen_focal:
        focal = frozen_focal
    elif tpl.focal_px:
        focal = float(tpl.focal_px)
    else:
        focal = None
        if tpl.turn_angle:
            # 弧形屏：用两端第一小段（直段）求消失点；折角屏：用首尾两个面
            # 弧形屏 / 折角屏都用首尾两段的弦面（端面通常是直的）
            qa, qb = chord_quads[0], chord_quads[-1]
            focal = focal_from_turn_angle(qa, qb, center, diag, float(tpl.turn_angle))
        if focal is None:
            focal = estimate_focal(chord_quads, center, diag) or 0.6 * diag

    # 弧形屏：优先用母线三维重建得到真实弧长
    ruled_seg = None
    if tpl.type == "curved":
        allT = np.concatenate([top_segs[0]] + [t_[1:] for t_ in top_segs[1:]])
        allB = np.concatenate([bot_segs[0]] + [b_[1:] for b_ in bot_segs[1:]])
        outer = np.array([top[0], top[-1], bot[-1], bot[0]])
        arcl = ruled_surface_arclen(allT, allB, outer, center, focal)
        if arcl is not None:
            ruled_seg, pos = [], 0
            for t_ in top_segs:
                k_ = len(t_) - 1
                ruled_seg.append(arcl[pos:pos + k_ + 1] - arcl[pos])
                pos += k_

    auto_w = []
    for i, q in enumerate(chord_quads):
        if ruled_seg is not None:
            auto_w.append(float(ruled_seg[i][-1]))
            continue
        a = rect_aspect(q, center, focal)
        if tpl.type == "curved":
            arc = _poly_len(top_segs[i]) + _poly_len(bot_segs[i])
            chord = np.linalg.norm(top[i + 1] - top[i]) + np.linalg.norm(bot[i + 1] - bot[i])
            a *= arc / max(chord, 1e-9)
        auto_w.append(a)
    auto_w = np.array(auto_w)

    if frozen_widths is not None:
        widths = np.asarray(frozen_widths, float)
    elif tpl.u_mode == "physical" and tpl.segment_widths:
        widths = np.asarray(tpl.segment_widths, float)
    elif tpl.u_mode == "equal":
        widths = np.ones(nseg)
    else:
        widths = auto_w.copy()

    if frozen_aspect is not None:
        aspect = frozen_aspect
    elif tpl.physical_size:
        aspect = tpl.physical_size[0] / tpl.physical_size[1]
    elif tpl.u_mode == "physical" and tpl.segment_widths:
        # 只给了各段宽度没给高：用 auto 的整体比例换算高度
        aspect = float(auto_w.sum())
    else:
        aspect = float(auto_w.sum())
    widths_norm = widths / widths.sum()

    # ---- 生成面片与 u 坐标
    strips: list[Strip] = []
    u = 0.0
    for i in range(nseg):
        T, B = top_segs[i], bot_segs[i]
        k = len(T) - 1
        if k == 1:
            wts = np.array([1.0])
        elif ruled_seg is not None and ruled_seg[i][-1] > 0:
            wts = np.diff(ruled_seg[i]) / ruled_seg[i][-1]
        else:
            # 段内：把弧上采样点经该段弦面的单应矩阵投回“展平”的矩形，得到其物理横向位置。
            # 同时考虑了距离（近大远小）与朝向（斜看压缩），比按像素弧长分配准确得多。
            Hq = cv2.getPerspectiveTransform(chord_quads[i].astype(np.float32),
                                             np.array([[0, 0], [1, 0], [1, 1], [0, 1]], np.float32))
            st_ = cv2.perspectiveTransform(T[None].astype(np.float64), Hq.astype(np.float64))[0, :, 0]
            sb_ = cv2.perspectiveTransform(B[None].astype(np.float64), Hq.astype(np.float64))[0, :, 0]
            s_ = (st_ + sb_) / 2
            s_ = np.maximum.accumulate(np.clip(s_, 0, 1))
            s_[0], s_[-1] = 0.0, 1.0
            wts = np.diff(s_)
            if wts.sum() <= 1e-9 or np.any(~np.isfinite(wts)):
                wts = np.full(k, 1.0 / k)
            wts = wts / wts.sum()
        for j in range(k):
            du = widths_norm[i] * wts[j]
            q = np.array([T[j], T[j + 1], B[j + 1], B[j]])
            strips.append(Strip(q, u, u + du, seam_left=bool(strips), seam_right=False, seg=i))
            u += du
    for s in strips[:-1]:
        s.seam_right = True
    strips[-1].u1 = 1.0

    seam_px = 2.0 if tpl.type == "folded" else 0.75
    return ScreenGeometry(strips, float(aspect), list(map(float, widths)), float(focal), seam_px, dist)


# ------------------------------------------------------------------ inverse maps

def _edge_dist(P: np.ndarray, a: np.ndarray, b: np.ndarray, orient: float) -> np.ndarray:
    d = b - a
    n = math.hypot(d[0], d[1]) + 1e-12
    return ((d[0] * (P[..., 1] - a[1]) - d[1] * (P[..., 0] - a[0])) / n) * orient


def compute_maps(geom: ScreenGeometry, width: int, height: int, content_w: int, content_h: int,
                 seam_px: Optional[float] = None, need_maps: bool = True, expand: float = 0.0):
    """计算屏幕区域的逆映射（输出像素 → 客户画面坐标）与抗锯齿覆盖率。

    返回 (map_x, map_y, alpha, bbox)，bbox=(x0,y0,x1,y1) 为输出图像中的区域。
    折缝处：相邻面片的映射坐标按到折缝距离做线性加权，消除折缝错位、撕裂。
    expand：屏幕外轮廓向外扩的像素数（折缝不扩）。标定差一两个像素时，避免原广告从边缘漏出来。
    """
    dist = geom.distortion
    s = float(seam_px if seam_px is not None else geom.seam_px)
    e = max(0.0, float(expand))
    outline = geom.outline()
    margin = int(math.ceil(s + e)) + 3
    x0 = max(int(math.floor(outline[:, 0].min())) - margin, 0)
    y0 = max(int(math.floor(outline[:, 1].min())) - margin, 0)
    x1 = min(int(math.ceil(outline[:, 0].max())) + margin + 1, width)
    y1 = min(int(math.ceil(outline[:, 1].max())) + margin + 1, height)
    if x1 <= x0 or y1 <= y0:
        raise ValueError("屏幕区域不在画面内")
    bw, bh = x1 - x0, y1 - y0

    gx, gy = np.meshgrid(np.arange(x0, x1, dtype=np.float32), np.arange(y0, y1, dtype=np.float32))
    P = np.stack([gx, gy], axis=-1)
    if dist.active:
        P = dist.undistort(P.astype(np.float64)).astype(np.float32)

    alpha = np.zeros((bh, bw), np.float32)
    if need_maps:
        sw = np.zeros((bh, bw), np.float32)
        sx = np.zeros((bh, bw), np.float32)
        sy = np.zeros((bh, bw), np.float32)

    Wc, Hc = content_w, content_h
    for st in geom.strips:
        q = st.quad
        # 面片在输出图中的包围盒（考虑畸变：沿边采样后再畸变）
        edge_pts = np.concatenate([np.linspace(q[i], q[(i + 1) % 4], 9) for i in range(4)])
        ed = dist.distort(edge_pts)
        bx0 = max(int(math.floor(ed[:, 0].min())) - margin, x0)
        by0 = max(int(math.floor(ed[:, 1].min())) - margin, y0)
        bx1 = min(int(math.ceil(ed[:, 0].max())) + margin + 1, x1)
        by1 = min(int(math.ceil(ed[:, 1].max())) + margin + 1, y1)
        if bx1 <= bx0 or by1 <= by0:
            continue
        sl = (slice(by0 - y0, by1 - y0), slice(bx0 - x0, bx1 - x0))
        Ps = P[sl]
        area = 0.0
        for i in range(4):
            a, b = q[i], q[(i + 1) % 4]
            area += a[0] * b[1] - b[0] * a[1]
        orient = 1.0 if area > 0 else -1.0
        d_top = _edge_dist(Ps, q[0], q[1], orient)
        d_right = _edge_dist(Ps, q[1], q[2], orient)
        d_bot = _edge_dist(Ps, q[2], q[3], orient)
        d_left = _edge_dist(Ps, q[3], q[0], orient)
        dmin = np.minimum(np.minimum(d_top + e, d_bot + e),
                          np.minimum(d_left + (0 if st.seam_left else e), d_right + (0 if st.seam_right else e)))
        alpha[sl] += np.clip(dmin + 0.5, 0, 1)
        if not need_maps:
            continue
        outer = [d_top, d_bot] + ([] if st.seam_left else [d_left]) + ([] if st.seam_right else [d_right])
        seams = ([d_left] if st.seam_left else []) + ([d_right] if st.seam_right else [])
        dout = outer[0]
        for d in outer[1:]:
            dout = np.minimum(dout, d)
        if seams:
            dse = seams[0] if len(seams) == 1 else np.minimum(seams[0], seams[1])
            w = np.clip((dse + s) / (2 * s), 0, 1)
        else:
            w = np.ones_like(dout)
        w = w * (dout > -(s + 1.5 + e))
        src = q.astype(np.float32)
        dst = np.array([[st.u0 * Wc - 0.5, -0.5], [st.u1 * Wc - 0.5, -0.5],
                        [st.u1 * Wc - 0.5, Hc - 0.5], [st.u0 * Wc - 0.5, Hc - 0.5]], np.float32)
        Hm = cv2.getPerspectiveTransform(src, dst).astype(np.float64)
        X, Y = Ps[..., 0].astype(np.float64), Ps[..., 1].astype(np.float64)
        den = Hm[2, 0] * X + Hm[2, 1] * Y + Hm[2, 2]
        den = np.where(np.abs(den) < 1e-12, 1e-12, den)
        cx = (Hm[0, 0] * X + Hm[0, 1] * Y + Hm[0, 2]) / den
        cy = (Hm[1, 0] * X + Hm[1, 1] * Y + Hm[1, 2]) / den
        sw[sl] += w
        sx[sl] += (w * cx).astype(np.float32)
        sy[sl] += (w * cy).astype(np.float32)

    alpha = np.clip(alpha, 0, 1)
    if not need_maps:
        return None, None, alpha, (x0, y0, x1, y1)
    valid = sw > 1e-6
    map_x = np.where(valid, sx / np.maximum(sw, 1e-6), -10).astype(np.float32)
    map_y = np.where(valid, sy / np.maximum(sw, 1e-6), -10).astype(np.float32)
    alpha[~valid] = 0
    return map_x, map_y, alpha, (x0, y0, x1, y1)


def screen_mask(geom: ScreenGeometry, width: int, height: int) -> np.ndarray:
    """整幅图大小的抗锯齿屏幕掩码 [0,1]。"""
    _, _, a, (x0, y0, x1, y1) = compute_maps(geom, width, height, 2, 2, need_maps=False)
    m = np.zeros((height, width), np.float32)
    m[y0:y1, x0:x1] = a
    return m


def occluder_mask(tpl: ScreenTemplate, width: int, height: int, feather: float = 1.0) -> np.ndarray:
    tpl = tpl.scaled_to(width, height)
    m = np.zeros((height, width), np.uint8)
    for poly in tpl.occluders or []:
        if len(poly) >= 3:
            cv2.fillPoly(m, [np.round(np.asarray(poly) * 16).astype(np.int32)], 255, cv2.LINE_AA, shift=4)
    m = m.astype(np.float32) / 255.0
    if feather > 0 and m.any():
        m = cv2.GaussianBlur(m, (0, 0), feather)
    return m


# ------------------------------------------------------------------ content fit

def fit_content(content: np.ndarray, aspect: float, mode: str = "cover",
                focus: tuple[float, float] = (0.5, 0.5)) -> np.ndarray:
    """把客户画面调整为屏幕物理宽高比，保证 LOGO / 文字不变形。

    cover   等比铺满并裁切（默认，焦点 focus 决定保留区域）
    contain 等比完整显示，空白处为黑（LED 不亮）
    stretch 拉伸铺满（仅当客户素材本就是按屏幕比例制作时使用）
    """
    h, w = content.shape[:2]
    ca = w / h
    if mode == "stretch" or abs(ca - aspect) / aspect < 0.005:
        return content
    if mode == "contain":
        if ca > aspect:
            nh = int(round(w / aspect))
            out = np.zeros((nh, w, 3), content.dtype)
            y = (nh - h) // 2
            out[y:y + h] = content
        else:
            nw = int(round(h * aspect))
            out = np.zeros((h, nw, 3), content.dtype)
            x = (nw - w) // 2
            out[:, x:x + w] = content
        return out
    if ca > aspect:
        nw = max(1, int(round(h * aspect)))
        x = int(round((w - nw) * focus[0]))
        return content[:, x:x + nw]
    nh = max(1, int(round(w / aspect)))
    y = int(round((h - nh) * focus[1]))
    return content[y:y + nh]


def unwrap(img: np.ndarray, geom: ScreenGeometry, out_h: int = 600) -> np.ndarray:
    """把照片里的屏幕“展开”成物理比例的平面图（替换的逆过程）。

    用于检查标定：展开后原屏画面里的方形元素（汉字、LOGO、圆）应保持方 / 圆；
    若某一面的字被压扁或拉宽，说明该段宽度分配或点位有误。
    """
    out_w = max(2, int(round(out_h * geom.aspect)))
    out = np.zeros((out_h, out_w, img.shape[2]), img.dtype)
    dist = geom.distortion
    for st in geom.strips:
        x0, x1 = st.u0 * out_w, st.u1 * out_w
        xa, xb = int(math.floor(x0)), int(math.ceil(x1))
        if xb <= xa:
            continue
        xs = np.arange(xa, xb, dtype=np.float32) + 0.5
        ys = np.arange(out_h, dtype=np.float32) + 0.5
        gx, gy = np.meshgrid(xs, ys)
        src = np.array([[x0, 0], [x1, 0], [x1, out_h], [x0, out_h]], np.float32)
        Hm = cv2.getPerspectiveTransform(src, st.quad.astype(np.float32))
        pts = cv2.perspectiveTransform(np.stack([gx, gy], -1).reshape(-1, 1, 2).astype(np.float64), Hm.astype(np.float64))
        pts = dist.distort(pts.reshape(-1, 2)).reshape(out_h, xb - xa, 2).astype(np.float32)
        patch = cv2.remap(img, pts[..., 0], pts[..., 1], cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT)
        if patch.ndim == 2:
            patch = patch[..., None]
        sel = (xs >= x0 - 0.5) & (xs < x1 + 0.5)
        out[:, xa:xb][:, sel] = patch[:, sel]
    return out


def warp_content(content: np.ndarray, geom: ScreenGeometry, width: int, height: int,
                 seam_px: Optional[float] = None, expand: float = 0.0):
    """把（已按宽高比调整好的）客户画面映射到屏幕上。返回 (layer, alpha, bbox)，layer/alpha 为 bbox 大小。"""
    ch, cw = content.shape[:2]
    # 内容远大于屏幕在图中的像素尺寸时先做面积下采样，避免摩尔纹/锯齿
    outline = geom.outline()
    approx_w = max(_poly_len(np.array([s.quad[0] for s in geom.strips] + [geom.strips[-1].quad[1]])),
                   _poly_len(np.array([s.quad[3] for s in geom.strips] + [geom.strips[-1].quad[2]])))
    approx_h = max(np.ptp(outline[:, 1]), 1.0)
    scale = min(1.0, 2.0 * max(approx_w / cw, approx_h / ch))
    if scale < 0.999:
        content = cv2.resize(content, (max(2, int(cw * scale)), max(2, int(ch * scale))), interpolation=cv2.INTER_AREA)
    ch, cw = content.shape[:2]
    mx, my, alpha, bbox = compute_maps(geom, width, height, cw, ch, seam_px, expand=expand)
    layer = cv2.remap(content, mx, my, interpolation=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
    if layer.ndim == 2:
        layer = layer[..., None]
    return layer, alpha, bbox

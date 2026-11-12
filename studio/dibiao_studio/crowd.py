"""人流车流增强。

两种真实摄影手法，都严格受“车道 / 人行道”标定区域约束，并避开大屏：

1. 车流光轨（夜景 / 黄昏默认）
   商业城市夜景的标准做法（长曝光车灯轨迹）。完全程序化生成：
   - 沿标定车道走向，横向位置、轨迹宽度按车道在图中的宽度做透视缩放（近粗远细）
   - 驶近镜头一侧为白黄色前灯，驶离一侧为红色尾灯（由车道宽度沿线变化自动判断）
   - 亮度沿轨迹有起伏（刹车 / 车距），带核心 + 光晕两层，滤色叠加不溢出

2. 人物 / 车辆素材贴入（白天默认，夜景可叠加）
   从素材库 assets/sprites/{people,vehicles}/*.png（透明底）取素材：
   - 透视：按落脚点处车道像素宽度 / 车道物理宽度 换算比例尺，人高 1.7m、车高 1.5m
   - 光影：按光源方向投射柔和阴影；亮度、色温向落脚点周边环境靠拢；噪点匹配
   - 遮挡：按远近排序绘制（远处先画），不覆盖大屏，素材之间重叠受控
   - 去“复制粘贴感”：随机水平翻转、±6% 尺寸抖动、色彩微调、同一素材不相邻

更强的“生成式”增强（AI 局部重绘生成行人车辆）可作为扩展后端接入，见 docs/06。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

from .analysis import LUMA, luminance, estimate_noise
from .imgio import read_rgba

LEVELS = {"off": 0.0, "light": 0.35, "medium": 0.65, "strong": 1.0}
ASSET_DIR = Path(__file__).resolve().parent.parent / "assets" / "sprites"


@dataclass
class Lane:
    kind: str = "road"              # road 车道 / sidewalk 人行道
    left: list = field(default_factory=list)    # 区域一侧边线（点列）
    right: list = field(default_factory=list)   # 另一侧边线（点数可不同，按弧长对应）
    width_m: float = 0.0            # 物理宽度（米）；0=默认（车道 7m，人行道 3m）
    direction: str = "both"         # both 双向 / forward 沿点列方向 / backward

    @classmethod
    def from_dict(cls, d):
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})


@dataclass
class CrowdParams:
    level: str = "medium"           # 人流车流增强等级：off / light / medium / strong
    trails: str = "auto"            # 车流光轨：auto（夜景/黄昏开）/ on / off
    sprites: str = "auto"           # 素材贴入：auto（有素材就用）/ on / off
    light_angle: float = 135.0      # 光源方位（度，图像坐标，0=右，90=下）；阴影投向反方向
    shadow: float = 0.5             # 阴影强度
    seed: int = 1

    @classmethod
    def from_dict(cls, d):
        d = d or {}
        p = cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})
        if p.level not in LEVELS:
            raise ValueError(f"level 必须是 {tuple(LEVELS)}")
        return p

    def to_dict(self):
        return asdict(self)


def _resample(poly: np.ndarray, n: int) -> np.ndarray:
    poly = np.asarray(poly, np.float64)
    seg = np.linalg.norm(np.diff(poly, axis=0), axis=1)
    s = np.concatenate([[0], np.cumsum(seg)])
    if s[-1] <= 0:
        return np.repeat(poly[:1], n, axis=0)
    t = np.linspace(0, s[-1], n)
    return np.stack([np.interp(t, s, poly[:, 0]), np.interp(t, s, poly[:, 1])], -1)


class LaneModel:
    def __init__(self, lane: Lane, n: int = 400):
        self.lane = lane
        L = _resample(lane.left, n)
        R = _resample(lane.right, n)
        # 左右边线方向不一致时自动翻转
        if np.linalg.norm(L[0] - R[0]) + np.linalg.norm(L[-1] - R[-1]) > np.linalg.norm(L[0] - R[-1]) + np.linalg.norm(L[-1] - R[0]):
            R = R[::-1]
        self.L, self.R = L, R
        self.width_px = np.linalg.norm(L - R, axis=1)
        self.width_m = lane.width_m or (7.0 if lane.kind == "road" else 3.0)
        self.ppm = self.width_px / self.width_m  # 每米像素（透视比例尺）
        mid = (L + R) / 2
        d = np.gradient(mid, axis=0)
        self.dir = d / np.maximum(np.linalg.norm(d, axis=1, keepdims=True), 1e-9)
        # 物理长度（米）：像素弧长 / 当地比例尺
        seg = np.linalg.norm(np.diff(mid, axis=0), axis=1)
        self.length_m = float(np.sum(seg / np.maximum((self.ppm[:-1] + self.ppm[1:]) / 2, 1e-6)))
        self.approaching_forward = self.width_px[-1] > self.width_px[0]  # 沿点列方向变宽 = 驶向镜头

    def point(self, s: float, o: float) -> np.ndarray:
        i = s * (len(self.L) - 1)
        i0 = int(np.clip(math.floor(i), 0, len(self.L) - 2))
        f = i - i0
        a = self.L[i0] * (1 - o) + self.R[i0] * o
        b = self.L[i0 + 1] * (1 - o) + self.R[i0 + 1] * o
        return a * (1 - f) + b * f

    def at(self, s: float, arr: np.ndarray):
        i = s * (len(arr) - 1)
        i0 = int(np.clip(math.floor(i), 0, len(arr) - 2))
        f = i - i0
        return arr[i0] * (1 - f) + arr[i0 + 1] * f

    def polygon(self) -> np.ndarray:
        return np.vstack([self.L, self.R[::-1]])


def _region_mask(lm: LaneModel, h: int, w: int) -> np.ndarray:
    m = np.zeros((h, w), np.uint8)
    cv2.fillPoly(m, [np.round(lm.polygon() * 16).astype(np.int32)], 255, cv2.LINE_AA, shift=4)
    return m.astype(np.float32) / 255


# ---------------------------------------------------------------- light trails

def render_trails(img: np.ndarray, lanes: list[LaneModel], density: float, scene: str,
                  avoid: Optional[np.ndarray], rng: np.random.Generator) -> tuple[np.ndarray, int]:
    h, w = img.shape[:2]
    canvas = np.zeros((h, w, 3), np.float32)
    head = np.array([0.72, 0.9, 1.0], np.float32)
    tail = np.array([0.12, 0.16, 1.0], np.float32)
    amber = np.array([0.15, 0.6, 1.0], np.float32)
    count = 0
    allowed = np.zeros((h, w), np.float32)
    for lm in lanes:
        if lm.lane.kind != "road":
            continue
        allowed = np.maximum(allowed, cv2.dilate(_region_mask(lm, h, w), np.ones((3, 3), np.uint8)))
        lanes_n = max(1.0, lm.width_m / 3.5)
        n_tr = int(round(density * 5 * lanes_n))  # 中等：每条车道约 3 条光轨（太密会糊成一片白）
        for k in range(n_tr):
            o = rng.uniform(0.08, 0.92)
            if lm.lane.direction == "both":
                forward = o < 0.5  # 左半幅、右半幅相向行驶
            else:
                forward = lm.lane.direction == "forward"
            toward_cam = forward == lm.approaching_forward
            col = head if toward_cam else tail
            if rng.random() < 0.08:
                col = amber
            s0 = rng.uniform(0, 0.25) if rng.random() < 0.6 else 0.0
            s1 = rng.uniform(0.75, 1.0) if rng.random() < 0.6 else 1.0
            ss = np.linspace(s0, s1, 120)
            # 变道 / 车身轻微摆动：低频平滑偏移（长曝光光轨是顺滑的，不能有随机抖动）
            ph = rng.uniform(0, 2 * np.pi, 2)
            fr = rng.uniform(0.5, 2.0, 2)
            wob = 0.004 * np.sin(fr[0] * np.pi * ss + ph[0]) + 0.002 * np.sin(fr[1] * 2 * np.pi * ss + ph[1])
            if rng.random() < 0.15:  # 少量车辆变道
                wob = wob + np.clip((ss - rng.uniform(0.3, 0.7)) * 6, 0, 1) * rng.choice([-1, 1]) * (3.5 / lm.width_m) * 0.8
            amp = rng.uniform(0.4, 0.85)
            flick = 0.8 + 0.2 * np.sin(np.linspace(0, rng.uniform(1, 4), len(ss)) + rng.uniform(0, 6))
            if not toward_cam:
                amp *= 0.75  # 尾灯比前灯暗
            sep = rng.uniform(0.16, 0.22) * 1.8 / lm.width_m  # 一辆车的两盏灯，约 1.6–1.8m 间距
            for side in (-0.5, 0.5):
                pts = np.array([lm.point(s, np.clip(o + wob[i] + side * sep, 0, 1)) for i, s in enumerate(ss)])
                for i in range(len(pts) - 1):
                    ppm = lm.at(ss[i], lm.ppm)
                    th = max(1, int(round(ppm * 0.07)))  # 长曝光光轨的亮芯约 7cm（车灯光斑的核心）
                    c = (col * amp * flick[i]).tolist()
                    p0 = np.round(pts[i] * 16).astype(int)
                    p1 = np.round(pts[i + 1] * 16).astype(int)
                    cv2.line(canvas, tuple(p0), tuple(p1), c, th, cv2.LINE_AA, shift=4)
            count += 1
    if count == 0:
        return img, 0
    canvas *= allowed[..., None]
    if avoid is not None:
        canvas *= (1 - avoid)[..., None]
    diag = math.hypot(h, w)
    glow = cv2.GaussianBlur(canvas, (0, 0), max(1.0, diag * 0.0012)) * 0.6 + cv2.GaussianBlur(canvas, (0, 0), max(2.0, diag * 0.005)) * 0.3
    core = cv2.GaussianBlur(canvas, (0, 0), 0.6)
    # 核心发白（过曝的灯光中心），外圈保留颜色
    core_w = np.clip(core @ LUMA, 0, 1)[..., None]
    core = core * 0.8 + core_w * np.clip(core_w * 2 - 0.6, 0, 0.5)  # 只有很亮的灯芯才发白
    k = {"night": 1.0, "dusk": 0.75, "day": 0.35}[scene]
    add = np.clip((core + glow) * k, 0, 1)
    if avoid is not None:
        add *= (1 - avoid)[..., None]  # 光晕也不能盖到大屏上
    return 1 - (1 - img) * (1 - add), count


# ---------------------------------------------------------------- sprites

@dataclass
class Sprite:
    rgba: np.ndarray
    kind: str
    name: str


def load_sprites(asset_dir: Path = ASSET_DIR) -> dict[str, list[Sprite]]:
    out = {"people": [], "vehicles": []}
    for kind in out:
        d = asset_dir / kind
        if not d.exists():
            continue
        for f in sorted(d.glob("*.png")):
            try:
                rgba = read_rgba(f)
            except ValueError:
                continue
            a = rgba[..., 3]
            ys, xs = np.nonzero(a > 0.05)
            if ys.size == 0:
                continue
            rgba = rgba[ys.min(): ys.max() + 1, xs.min(): xs.max() + 1]
            out[kind].append(Sprite(rgba, kind, f.stem))
    return out


def _place_sprite(img, sp: Sprite, foot: np.ndarray, height_px: float, flip: bool, motion: Optional[np.ndarray],
                  light_angle: float, shadow: float, noise: float, rng) -> Optional[tuple]:
    h, w = img.shape[:2]
    rgba = sp.rgba[:, ::-1] if flip else sp.rgba
    sh, sw = rgba.shape[:2]
    s = height_px / sh
    nh, nw = max(2, int(round(sh * s))), max(1, int(round(sw * s)))
    if nh < 6:
        return None
    spr = cv2.resize(rgba, (nw, nh), interpolation=cv2.INTER_AREA if s < 1 else cv2.INTER_LINEAR)
    if motion is not None and np.linalg.norm(motion) > 1.5:
        L = int(min(np.linalg.norm(motion), nw))
        k = np.zeros((L * 2 + 1, L * 2 + 1), np.float32)
        d = motion / np.linalg.norm(motion)
        cv2.line(k, (L, L), (int(L + d[0] * L), int(L + d[1] * L)), 1, 1)
        k /= max(k.sum(), 1)
        spr = cv2.filter2D(spr, -1, k)
    x0 = int(round(foot[0] - nw / 2))
    y0 = int(round(foot[1] - nh))
    if x0 >= w or y0 >= h or x0 + nw <= 0 or y0 + nh <= 0:
        return None
    # 裁到画面内
    cx0, cy0 = max(0, -x0), max(0, -y0)
    cx1, cy1 = nw - max(0, x0 + nw - w), nh - max(0, y0 + nh - h)
    spr = spr[cy0:cy1, cx0:cx1]
    X0, Y0 = x0 + cx0, y0 + cy0
    a = spr[..., 3:4]
    rgb = spr[..., :3]
    region = img[Y0:Y0 + spr.shape[0], X0:X0 + spr.shape[1]]
    # 光影匹配：素材亮度 / 色温向落脚点环境靠拢
    pad = int(nh * 0.6)
    env = img[max(0, Y0 - pad): min(h, Y0 + spr.shape[0] + pad // 3), max(0, X0 - pad): min(w, X0 + spr.shape[1] + pad)]
    env_m = env.reshape(-1, 3).mean(0)
    env_L = float(env_m @ LUMA)
    spr_m = (rgb * a).reshape(-1, 3).sum(0) / max(float(a.sum()), 1e-6)
    spr_L = float(spr_m @ LUMA)
    gain = float(np.clip((env_L / max(spr_L, 1e-3)) ** 0.6, 0.15, 1.6))
    tint = env_m / max(env_L, 1e-4)
    tint = 1 + (tint - 1) * 0.35
    rgb = np.clip(rgb * gain * tint * rng.uniform(0.92, 1.08, 3), 0, 1)
    if noise > 0:
        rgb = rgb + rng.normal(0, noise, rgb.shape).astype(np.float32)
    # 阴影：落脚点处压扁并按光源反方向剪切的剪影
    if shadow > 0:
        ang = math.radians(light_angle + 180)
        sx_ = math.cos(ang)
        sil = (sp.rgba[:, ::-1] if flip else sp.rgba)[..., 3]
        sil = cv2.resize(sil, (nw, max(2, int(nh * 0.25))), interpolation=cv2.INTER_AREA)[::-1]
        M = np.float32([[1, -sx_ * 1.6, sx_ * 1.6 * sil.shape[0]], [0, 1, 0]])
        sw2 = nw + int(abs(sx_) * 1.6 * sil.shape[0]) + 2
        sil = cv2.warpAffine(sil, M, (sw2, sil.shape[0]))
        sil = cv2.GaussianBlur(sil, (0, 0), max(0.8, nh * 0.02))
        bx = int(round(foot[0] - nw / 2 - (sw2 - nw if sx_ < 0 else 0)))
        by = int(round(foot[1] - 1))
        bx0, by0 = max(0, bx), max(0, by)
        bx1, by1 = min(w, bx + sw2), min(h, by + sil.shape[0])
        if bx1 > bx0 and by1 > by0:
            ss = sil[by0 - by: by1 - by, bx0 - bx: bx1 - bx]
            img[by0:by1, bx0:bx1] *= (1 - ss * 0.55 * shadow)[..., None]
        region = img[Y0:Y0 + spr.shape[0], X0:X0 + spr.shape[1]]
    img[Y0:Y0 + spr.shape[0], X0:X0 + spr.shape[1]] = region * (1 - a) + rgb * a
    return (X0, Y0, X0 + spr.shape[1], Y0 + spr.shape[0])


def place_sprites(img: np.ndarray, lanes: list[LaneModel], sprites: dict, density: float,
                  params: CrowdParams, avoid: Optional[np.ndarray], scene: str,
                  rng: np.random.Generator) -> tuple[np.ndarray, list]:
    h, w = img.shape[:2]
    noise = estimate_noise(img)
    jobs = []
    for lm in lanes:
        kind = "vehicles" if lm.lane.kind == "road" else "people"
        pool = sprites.get(kind) or []
        if not pool:
            continue
        spacing = 14.0 if kind == "vehicles" else 2.6       # 中等密度下平均间距（米）
        lanes_n = max(1, lm.width_m / (3.5 if kind == "vehicles" else 1.2))
        n = int(round(density * lm.length_m / spacing * lanes_n))
        for _ in range(n):
            s = rng.uniform(0.02, 0.98)
            o = rng.uniform(0.15, 0.85)
            foot = lm.point(s, o)
            ppm = float(lm.at(s, lm.ppm))
            real_h = 1.7 if kind == "people" else 1.5
            hp = real_h * ppm * rng.uniform(0.94, 1.06)
            jobs.append((foot[1], foot, hp, kind, lm, s, o))
    jobs.sort(key=lambda j: j[0])  # 远处（画面上方）先画
    boxes, last = [], {}
    placed = 0
    for _, foot, hp, kind, lm, s, o in jobs:
        pool = sprites[kind]
        idx = int(rng.integers(len(pool)))
        if len(pool) > 1 and last.get(kind) == idx:
            idx = (idx + 1) % len(pool)
        sp = pool[idx]
        aspect = sp.rgba.shape[1] / sp.rgba.shape[0]
        bx = (foot[0] - hp * aspect / 2, foot[1] - hp, foot[0] + hp * aspect / 2, foot[1])
        if avoid is not None:
            x0, y0 = int(max(0, bx[0])), int(max(0, bx[1]))
            x1, y1 = int(min(w, bx[2])), int(min(h, bx[3]))
            if x1 <= x0 or y1 <= y0 or avoid[y0:y1, x0:x1].mean() > 0.01:
                continue
        bad = False
        for b in boxes:
            ix = max(0, min(bx[2], b[2]) - max(bx[0], b[0]))
            iy = max(0, min(bx[3], b[3]) - max(bx[1], b[1]))
            small = min((bx[2] - bx[0]) * (bx[3] - bx[1]), (b[2] - b[0]) * (b[3] - b[1]))
            if ix * iy > 0.35 * small:
                bad = True
                break
        if bad:
            continue
        d = lm.at(s, lm.dir)
        motion = d * hp * 0.25 if kind == "vehicles" else None
        r = _place_sprite(img, sp, foot, hp, bool(rng.random() < 0.5), motion, params.light_angle,
                          params.shadow if scene != "night" else params.shadow * 0.3, noise * 0.8, rng)
        if r:
            boxes.append(r)
            last[kind] = idx
            placed += 1
    return img, [list(map(int, b)) for b in boxes]


def augment(img: np.ndarray, lanes: list[dict], params: CrowdParams, scene: str,
            screen_mask: Optional[np.ndarray] = None, sprites: Optional[dict] = None) -> tuple[np.ndarray, dict]:
    info = {"trails": 0, "sprites": 0, "placements": [], "warnings": []}
    density = LEVELS[params.level]
    if density <= 0 or not lanes:
        return img, info
    rng = np.random.default_rng(params.seed)
    lms = [LaneModel(Lane.from_dict(l)) for l in lanes if len(l.get("left", [])) >= 2 and len(l.get("right", [])) >= 2]
    avoid = None
    if screen_mask is not None:
        avoid = cv2.dilate((screen_mask > 0.05).astype(np.uint8), np.ones((9, 9), np.uint8)).astype(np.float32)
    out = img.copy()
    use_sprites = params.sprites == "on" or (params.sprites == "auto")
    if use_sprites:
        sprites = sprites if sprites is not None else load_sprites()
        if not any(sprites.values()):
            if params.sprites == "on" or scene == "day":
                info["warnings"].append("素材库为空：请在 studio/assets/sprites/people 与 vehicles 放入透明底 PNG 人物 / 车辆素材")
        else:
            out, info["placements"] = place_sprites(out, lms, sprites, density, params, avoid, scene, rng)
            info["sprites"] = len(info["placements"])
    use_trails = params.trails == "on" or (params.trails == "auto" and scene in ("night", "dusk"))
    if use_trails:
        out, info["trails"] = render_trails(out, lms, density, scene, avoid, rng)
    return out, info

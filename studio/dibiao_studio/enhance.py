"""第一层：真实上刊照片精修增强。

原则：只改“质感”，不改“内容”。所有操作都是逐像素 / 局部滤波类的确定性处理，
不使用生成式重绘，因此不会改变建筑结构、道路结构、屏幕位置和主体内容。

处理顺序（每步都可单独调强度）：
 1. 坏点 / 脏点修复     孤立异常像素中值替换
 2. 去噪 / 去压缩伪影    亮度 NLM（视频模式用双边滤波，避免闪烁），色度单独平滑
 3. 动态范围优化         导向滤波 base/detail 分解：压缩大尺度明暗、保留并增强细节
 4. 去雾（白天/黄昏）    暗通道先验，轻度，提升通透感
 5. 场景色调             夜景 / 黄昏 / 白天 × 自然 / 电影感 / 夜景增强 / 商圈大片
 6. 局部优化             天空（渐变 / 偏振感 / 防色带）、高光（玻璃反光、灯光）回收
 7. 大屏强化             （有屏幕掩码时）提亮、色彩、文字清晰度、LED 发光感、边缘
 8. 大片感氛围           光晕、空气透视、暗角、胶片颗粒（防“塑料感”）
 9. 输出锐化             自适应半径 + 防光晕钳制
"""
from __future__ import annotations

import math
from dataclasses import dataclass, asdict, field
from typing import Optional

import cv2
import numpy as np

from .analysis import LUMA, luminance, estimate_noise, classify_scene

TONE_MODES = ("natural", "cinematic", "night_boost", "commercial")
SCENES = ("auto", "day", "dusk", "night")


@dataclass
class EnhanceParams:
    strength: float = 0.6          # 精修强度（总控）
    tone_mode: str = "cinematic"   # 色调模式：自然 / 电影感 / 夜景增强 / 商圈大片
    scene: str = "auto"            # 场景：自动 / 白天 / 黄昏 / 夜景
    denoise: float = 0.6           # 去噪强度（结合噪声估计自动换算）
    sharpen: float = 0.45          # 锐化强度
    screen_boost: float = 0.5      # 大屏增强强度
    atmosphere: float = 0.5        # 大片感氛围强度（光晕 / 空气感 / 暗角）
    color_temp: float = 0.0        # 色温偏移 -1 冷 … +1 暖
    grain: float = 0.35            # 颗粒（保持真实摄影质感）
    video_safe: bool = False       # 视频模式：不用随帧变化的自适应统计，避免闪烁

    @classmethod
    def from_dict(cls, d: dict | None) -> "EnhanceParams":
        d = d or {}
        p = cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})
        if p.tone_mode not in TONE_MODES:
            raise ValueError(f"tone_mode 必须是 {TONE_MODES}")
        if p.scene not in SCENES:
            raise ValueError(f"scene 必须是 {SCENES}")
        return p

    def to_dict(self):
        return asdict(self)


# ---------------------------------------------------------------- helpers

def box(x, r):
    return cv2.boxFilter(x, -1, (2 * r + 1, 2 * r + 1), borderType=cv2.BORDER_REFLECT)


def guided_filter(I: np.ndarray, p: np.ndarray, r: int, eps: float, sub: int = 4) -> np.ndarray:
    """快速导向滤波（He 2015）：在 1/sub 尺度求系数后上采样。I、p 为单通道 float32。"""
    h, w = I.shape
    sub = max(1, min(sub, r // 2 if r >= 2 else 1))
    Is = cv2.resize(I, (max(1, w // sub), max(1, h // sub)), interpolation=cv2.INTER_AREA) if sub > 1 else I
    ps = cv2.resize(p, (Is.shape[1], Is.shape[0]), interpolation=cv2.INTER_AREA) if sub > 1 else p
    rs = max(1, r // sub)
    mI, mp = box(Is, rs), box(ps, rs)
    cov = box(Is * ps, rs) - mI * mp
    var = box(Is * Is, rs) - mI * mI
    a = cov / (var + eps)
    b = mp - a * mI
    ma, mb = box(a, rs), box(b, rs)
    if sub > 1:
        ma = cv2.resize(ma, (w, h), interpolation=cv2.INTER_LINEAR)
        mb = cv2.resize(mb, (w, h), interpolation=cv2.INTER_LINEAR)
    return ma * I + mb


def apply_luma_ratio(img: np.ndarray, L_old: np.ndarray, L_new: np.ndarray, sat_comp: float = 0.6) -> np.ndarray:
    """按亮度比例调整 RGB（保持色相）；sat_comp<1 时在提亮处略降饱和避免发艳。"""
    ratio = L_new / np.maximum(L_old, 1e-4)
    ratio = np.clip(ratio, 0, 8)[..., None]
    out = img * ratio
    if sat_comp < 1:
        Ln = L_new[..., None]
        out = Ln + (out - Ln) * (sat_comp + (1 - sat_comp) * np.clip(1 / np.maximum(ratio, 1e-3), 0, 1))
    return out


def soft_clip(x: np.ndarray, knee: float = 0.85) -> np.ndarray:
    """高光软压缩：knee 以上平滑逼近 1，避免死白。"""
    over = np.maximum(x - knee, 0)
    span = 1 - knee
    return np.where(x > knee, knee + span * (1 - np.exp(-over / span)), x)


def s_curve(x: np.ndarray, k: float) -> np.ndarray:
    """f(x) = x - k·sin(2πx)/(2π)：端点与中灰不动，中间调对比 ×(1+k)。"""
    xc = np.clip(x, 0, 1)
    return x - k * np.sin(2 * math.pi * xc) / (2 * math.pi)


def to_lab(img):
    return cv2.cvtColor(np.clip(img, 0, 1).astype(np.float32), cv2.COLOR_BGR2Lab)


def from_lab(lab):
    return cv2.cvtColor(lab.astype(np.float32), cv2.COLOR_Lab2BGR)


def vibrance(img: np.ndarray, amount: float, mask: Optional[np.ndarray] = None) -> np.ndarray:
    """自然饱和度：低饱和区域提得多、高饱和区域提得少；保护肤色（橙色色相减半）。"""
    if abs(amount) < 1e-3:
        return img
    lab = to_lab(img)
    a, b = lab[..., 1], lab[..., 2]
    C = np.sqrt(a * a + b * b)
    k = amount * (1 - np.clip(C / 70.0, 0, 1))
    hue = np.degrees(np.arctan2(b, a)) % 360
    skin = np.exp(-((hue - 55) / 20) ** 2) * (C > 8) * (C < 40)
    k = k * (1 - 0.5 * skin)
    if mask is not None:
        k = k * mask
    lab[..., 1] = a * (1 + k)
    lab[..., 2] = b * (1 + k)
    return from_lab(lab)


def sky_mask(img: np.ndarray) -> np.ndarray:
    """与画面上边缘连通的低纹理区域 → 天空。返回柔和掩码 [0,1]。"""
    h, w = img.shape[:2]
    s = 384 / max(h, w)
    small = cv2.resize(img, (max(8, int(w * s)), max(8, int(h * s))), interpolation=cv2.INTER_AREA)
    L = luminance(small)
    gx = cv2.Sobel(L, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(L, cv2.CV_32F, 0, 1, ksize=3)
    g = cv2.GaussianBlur(np.hypot(gx, gy), (0, 0), 2)
    yy = np.linspace(0, 1, small.shape[0])[:, None]
    cand = (g < max(0.04, float(np.percentile(g, 35)))) & (yy < 0.7)
    cand = cv2.morphologyEx(cand.astype(np.uint8), cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    n, lab = cv2.connectedComponents(cand)
    keep = np.zeros_like(cand)
    top_labels = set(np.unique(lab[0])) - {0}
    for t in top_labels:
        comp = lab == t
        if comp.sum() > 0.01 * comp.size:
            keep |= comp.astype(np.uint8)
    m = cv2.GaussianBlur(keep.astype(np.float32), (0, 0), 3)
    m = cv2.resize(m, (w, h), interpolation=cv2.INTER_LINEAR)
    return np.clip(m * 1.2, 0, 1)


def remove_hot_pixels(img: np.ndarray, sigma: float) -> tuple[np.ndarray, int]:
    """坏点 / 热像素：比 8 邻域中的最大值还亮（或比最小值还暗）一个明显阈值的单个像素。

    只看 8 邻域极值而不是中值，真实的小灯点、窗格角点不会被误修。
    """
    ring = np.ones((3, 3), np.uint8)
    ring[1, 1] = 0
    nmax = cv2.dilate(img, ring)
    nmin = cv2.erode(img, ring)
    thr = max(0.15, 6 * sigma)
    hot = ((img - nmax).max(axis=2) > thr) | ((nmin - img).max(axis=2) > thr)
    out = img.copy()
    if hot.any():
        u8 = np.clip(img * 255, 0, 255).astype(np.uint8)
        med = cv2.medianBlur(u8, 3).astype(np.float32) / 255
        out[hot] = med[hot]
    return out, int(hot.sum())


def denoise(img: np.ndarray, sigma: float, strength: float, video_safe: bool) -> np.ndarray:
    if strength <= 0 or sigma * 255 < 0.8:
        return img
    ycc = cv2.cvtColor(np.clip(img, 0, 1), cv2.COLOR_BGR2YCrCb)
    Y = ycc[..., 0]
    h_ = float(np.clip(sigma * 255 * 1.15 * strength, 1.5, 18))
    if video_safe:
        Yd = cv2.bilateralFilter(Y, 5, sigma * 2.5 * strength + 1e-3, 3)
    else:
        Yd = cv2.fastNlMeansDenoising(np.clip(Y * 255 + 0.5, 0, 255).astype(np.uint8), None, h=h_,
                                      templateWindowSize=7, searchWindowSize=15).astype(np.float32) / 255
        # 保留少量原始细节，防止“油画感”
        Yd = Yd + (Y - Yd) * 0.18
    rc = max(1.0, 1.5 * strength * min(3, sigma * 255 / 3))
    Cr = guided_filter(Yd, ycc[..., 1], int(rc * 2) + 1, 1e-3, sub=1)
    Cb = guided_filter(Yd, ycc[..., 2], int(rc * 2) + 1, 1e-3, sub=1)
    out = cv2.cvtColor(np.stack([Yd, Cr, Cb], -1).astype(np.float32), cv2.COLOR_YCrCb2BGR)
    return out


def dehaze(img: np.ndarray, amount: float, sky: Optional[np.ndarray] = None) -> np.ndarray:
    """暗通道先验去雾（轻度）。天空不去雾：阴天 / 薄雾天空本身就是亮的，去雾只会把它压成脏灰色。"""
    if amount <= 0:
        return img
    h, w = img.shape[:2]
    s = 512 / max(h, w)
    small = cv2.resize(img, (max(8, int(w * s)), max(8, int(h * s))), interpolation=cv2.INTER_AREA)
    dark = cv2.erode(small.min(axis=2), np.ones((15, 15), np.uint8))
    flat = dark.reshape(-1)
    idx = np.argsort(flat)[-max(1, flat.size // 1000):]
    A = small.reshape(-1, 3)[idx].mean(0)
    A = np.maximum(A, 0.5)
    t = 1 - amount * dark / float(A.max())
    t = cv2.resize(t, (w, h), interpolation=cv2.INTER_LINEAR)
    t = guided_filter(luminance(img).astype(np.float32), t.astype(np.float32), int(max(h, w) / 80) + 2, 1e-3)
    t = np.clip(t, 0.55, 1)
    if sky is not None:
        t = t + (1 - t) * np.clip(sky * 1.2, 0, 1)
    t = t[..., None]
    return (img - A) / t + A


# ---------------------------------------------------------------- scene presets

SCENE_PRESETS = {
    #           目标中位亮度  base 压缩  细节增强  去雾  S 曲线  自然饱和  阴影色         高光色
    "night": dict(target=0.17, compress=0.78, detail=0.35, haze=0.0, curve=0.22, vib=0.28,
                  shadow=(0.018, 0.006, -0.008), high=(-0.012, 0.004, 0.02), bloom=0.30, bloom_thr=0.62),
    "dusk": dict(target=0.30, compress=0.82, detail=0.30, haze=0.12, curve=0.20, vib=0.30,
                 shadow=(0.012, 0.0, -0.004), high=(-0.02, 0.006, 0.03), bloom=0.18, bloom_thr=0.75),
    "day": dict(target=0.44, compress=0.85, detail=0.28, haze=0.22, curve=0.16, vib=0.22,
                shadow=(0.006, 0.0, -0.004), high=(-0.006, 0.0, 0.008), bloom=0.06, bloom_thr=0.88),
}

TONE_MODIFIERS = {
    "natural": dict(curve=0.5, vib=0.6, split=0.3, bloom=0.5, detail=0.8),
    "cinematic": dict(curve=1.0, vib=0.9, split=1.0, bloom=1.0, detail=1.0),
    "night_boost": dict(curve=1.1, vib=1.2, split=0.8, bloom=1.4, detail=1.1, lift=1.25),
    "commercial": dict(curve=1.2, vib=1.35, split=0.7, bloom=1.2, detail=1.2, lift=1.1),
}


# ---------------------------------------------------------------- screen boost

def boost_screen(img: np.ndarray, mask: np.ndarray, amount: float, scene: str) -> np.ndarray:
    """大屏区域：提亮（高光软压缩防发白）、饱和、文字清晰度、LED 发光感。只在屏幕邻域内计算。"""
    if amount <= 0 or mask is None or mask.max() < 0.5:
        return img
    ys, xs = np.nonzero(mask > 0.02)
    h, w = mask.shape
    size = max(int(np.ptp(xs)) if xs.size else 1, int(np.ptp(ys)) if ys.size else 1)
    sig = max(2.0, size * 0.03)
    mg = int(sig * 3) + 4
    x0, x1 = max(0, xs.min() - mg), min(w, xs.max() + mg + 1)
    y0, y1 = max(0, ys.min() - mg), min(h, ys.max() + mg + 1)
    sub = img[y0:y1, x0:x1]
    m = mask[y0:y1, x0:x1, None]
    L = luminance(sub)
    gain = 1 + 0.35 * amount * (1.2 if scene == "night" else 1.0)
    Lb = soft_clip(L * gain, 0.82)
    scr = apply_luma_ratio(sub, L, Lb, sat_comp=1.0)
    scr = vibrance(scr, 0.35 * amount)
    # 文字 / 画面清晰度：小半径局部对比
    blur = cv2.GaussianBlur(scr, (0, 0), 1.6)
    scr = scr + (scr - blur) * (0.6 * amount)
    o = sub * (1 - m) + np.clip(scr, 0, 1.2) * m
    # LED 发光：屏幕内容向外的柔和溢光
    glow = cv2.GaussianBlur(o * m, (0, 0), sig)
    k = {"night": 0.45, "dusk": 0.28, "day": 0.12}[scene] * amount
    out = img.copy()
    out[y0:y1, x0:x1] = o + glow * k * (1 - m * 0.7)
    return out


# ---------------------------------------------------------------- atmosphere

def add_bloom(img: np.ndarray, amount: float, thr: float) -> np.ndarray:
    if amount <= 0:
        return img
    h, w = img.shape[:2]
    diag = math.hypot(h, w)
    L = luminance(img)
    bright = img * np.clip((L - thr) / (1 - thr + 1e-3), 0, 1)[..., None]
    s = 1024 / max(h, w)
    small = cv2.resize(bright, (max(8, int(w * s)), max(8, int(h * s))), interpolation=cv2.INTER_AREA) if s < 1 else bright
    acc = np.zeros_like(small)
    for frac, wt in ((0.004, 0.5), (0.012, 0.3), (0.035, 0.2)):
        acc += cv2.GaussianBlur(small, (0, 0), max(0.8, frac * diag * min(s, 1))) * wt
    if s < 1:
        acc = cv2.resize(acc, (w, h), interpolation=cv2.INTER_LINEAR)
    return 1 - (1 - img) * (1 - np.clip(acc * amount, 0, 1))  # 滤色叠加，不溢出


_ATMO_CACHE: dict = {}


def _atmo_maps(h: int, w: int):
    """空气透视的纵向深度权重与暗角形状只与尺寸有关，缓存复用（视频逐帧加速）。"""
    key = (h, w)
    if key not in _ATMO_CACHE:
        yy = np.linspace(0, 1, h, dtype=np.float32)[:, None]
        depth = np.clip(1 - yy / 0.65, 0, 1) ** 1.5
        yv, xv = np.ogrid[:h, :w]
        r2 = ((xv - w / 2) / (w / 2)) ** 2 + ((yv - h / 2) / (h / 2)) ** 2
        vig = (np.clip(r2 / 2, 0, 1) ** 1.1).astype(np.float32)
        if len(_ATMO_CACHE) > 4:
            _ATMO_CACHE.clear()
        _ATMO_CACHE[key] = (depth.astype(np.float32), vig)
    return _ATMO_CACHE[key]


def add_atmosphere(img: np.ndarray, scene: str, amount: float, sky: np.ndarray, seed: int = 7,
                   grain: float = 0.3) -> np.ndarray:
    h, w = img.shape[:2]
    if amount > 0:
        # 空气透视：画面上部（远处）轻微雾化，颜色取天空/环境
        yy_d, vig0 = _atmo_maps(h, w)
        depth = yy_d * (1 - sky)
        if sky.mean() > 0.02:
            hz = (img * sky[..., None]).reshape(-1, 3).sum(0) / max(sky.sum(), 1)
        else:
            hz = np.percentile(img.reshape(-1, 3), 75, axis=0)
        k = {"night": 0.03, "dusk": 0.06, "day": 0.07}[scene] * amount
        img = img * (1 - depth[..., None] * k) + hz.astype(np.float32) * depth[..., None] * k
        # 暗角
        img = img * (1 - 0.16 * amount * vig0)[..., None]
    if grain > 0:
        rng = np.random.default_rng(seed)
        L = luminance(np.clip(img, 0, 1))
        # 颗粒：中间调最明显，亮部暗部弱
        sig = 0.0065 * grain * (4 * L * (1 - L) + 0.15)
        n = rng.standard_normal((h, w)).astype(np.float32)
        n = cv2.GaussianBlur(n, (0, 0), 0.6) * 1.6
        img = img + (n * sig)[..., None]
    return img


def sharpen(img: np.ndarray, amount: float) -> np.ndarray:
    """亮度通道 USM，半径随分辨率自适应；用局部极值钳制防止白边黑边（过度锐化）。"""
    if amount <= 0:
        return img
    h, w = img.shape[:2]
    r = float(np.clip(max(h, w) / 3200, 0.6, 2.0))
    L = luminance(img)
    blur = cv2.GaussianBlur(L, (0, 0), r)
    detail = L - blur
    thr = 0.006
    detail = np.sign(detail) * np.maximum(np.abs(detail) - thr, 0)  # 软阈值：不放大噪点
    Ls = L + detail * (1.4 * amount)
    k = np.ones((3, 3), np.uint8)
    lo, hi = cv2.erode(L, k), cv2.dilate(L, k)
    span = (hi - lo) * 0.12
    Ls = np.clip(Ls, lo - span, hi + span)
    return apply_luma_ratio(img, L, Ls, sat_comp=1.0)


# ---------------------------------------------------------------- main

@dataclass
class SceneStats:
    """视频模式：首帧统计后冻结，逐帧复用，避免亮度 / 色调随帧跳变。"""
    scene: str
    gain: float
    noise: float
    info: dict = field(default_factory=dict)


def analyze(img: np.ndarray, params: EnhanceParams) -> SceneStats:
    cls = classify_scene(img)
    scene = cls["scene"] if params.scene == "auto" else params.scene
    pre = SCENE_PRESETS[scene]
    mod = TONE_MODIFIERS[params.tone_mode]
    med = max(float(np.median(luminance(cv2.resize(img, (256, 256), interpolation=cv2.INTER_AREA)))), 1e-3)
    target = pre["target"] * mod.get("lift", 1.0)
    gain = float(np.clip((target / med) ** (0.6 * params.strength), 0.75, 1.9))
    # 白天 / 黄昏原片已正常曝光时不往下压（明亮的阴天、白墙压暗只会显得发灰发闷）；夜景允许轻微回收过曝
    gain = max(gain, 1.0 if scene in ("day", "dusk") else 0.85)
    return SceneStats(scene=scene, gain=gain, noise=estimate_noise(img),
                      info={"classify": cls, "target_median": target, "median_luma": med})


def enhance(img: np.ndarray, params: EnhanceParams, screen: Optional[np.ndarray] = None,
            stats: Optional[SceneStats] = None, seed: int = 7) -> tuple[np.ndarray, dict]:
    """返回 (精修图, 处理信息)。screen 为可选的屏幕掩码（整图大小，[0,1]）。"""
    p = params
    st = stats or analyze(img, p)
    scene = st.scene
    pre = SCENE_PRESETS[scene]
    mod = TONE_MODIFIERS[p.tone_mode]
    S = float(np.clip(p.strength, 0, 1))
    log = {"scene": scene, "noise_sigma_255": round(st.noise * 255, 2), "steps": []}
    out = img.astype(np.float32)

    # 1. 坏点
    if not p.video_safe:
        out, n_hot = remove_hot_pixels(out, st.noise)
        log["steps"].append(f"坏点修复 {n_hot} 处")
    # 2. 去噪
    out = denoise(out, st.noise, p.denoise, p.video_safe)
    log["steps"].append("去噪/去压缩伪影")

    # 3. 动态范围 + 细节（导向滤波 base/detail）
    L = luminance(np.clip(out, 0, None)) + 1e-4
    logL = np.log(L).astype(np.float32)
    h, w = L.shape
    r = int(max(h, w) / 60) + 2
    base = guided_filter(logL, logL, r, 0.04)
    detail = logL - base
    mid = float(np.log(np.median(L) + 1e-4))
    c = 1 - (1 - pre["compress"]) * S
    d_gain = 1 + pre["detail"] * mod.get("detail", 1.0) * S
    # 细节增强只作用于纹理区（建筑），天空等平滑区不加（防噪点 / 色带）
    sky = sky_mask(out)
    tex = np.clip(np.abs(detail) / 0.08, 0, 1)
    d_gain_map = 1 + (d_gain - 1) * tex * (1 - 0.8 * sky)
    delta = base - mid
    delta = np.where(delta < 0, delta * c, delta)  # 只把暗部往中间调拉（提阴影），不把亮天空、白墙往下压
    newlog = mid + delta + detail * d_gain_map + math.log(st.gain)
    Ln = soft_clip(np.exp(newlog), 0.88)
    out = apply_luma_ratio(out, L, Ln)
    log["steps"].append(f"动态范围优化（曝光×{st.gain:.2f}，大尺度压缩 {c:.2f}，细节 ×{d_gain:.2f}）")

    # 4. 去雾
    if pre["haze"] > 0:
        out = dehaze(np.clip(out, 0, 1), pre["haze"] * S, sky)
        log["steps"].append("通透感（去雾）")

    # 5. 色调
    Lc = luminance(np.clip(out, 0, 1))
    k_curve = pre["curve"] * mod["curve"] * S
    Lk = s_curve(Lc, k_curve)
    # 夜景防止暗部死黑：抬一点黑位
    if scene == "night":
        Lk = 0.012 + Lk * (1 - 0.012)
    out = apply_luma_ratio(out, Lc, Lk, sat_comp=1.0)
    sh = np.array(pre["shadow"], np.float32) * mod["split"] * S
    hi = np.array(pre["high"], np.float32) * mod["split"] * S
    Lc = luminance(np.clip(out, 0, 1))[..., None]
    out = out + sh * (1 - Lc) ** 2 + hi * Lc ** 2
    out = vibrance(out, pre["vib"] * mod["vib"] * S)
    if abs(p.color_temp) > 1e-3:
        t = float(np.clip(p.color_temp, -1, 1)) * 0.08
        out = out * np.array([1 - t, 1 + t * 0.1, 1 + t], np.float32)
    log["steps"].append(f"{scene} 场景调色（{p.tone_mode}）")

    # 6. 局部：天空
    if sky.mean() > 0.01:
        sm = sky[..., None]
        sky_img = cv2.GaussianBlur(out, (0, 0), 1.2)  # 天空再平滑一次防色带
        if scene == "day":
            lab = to_lab(sky_img)
            chroma = float((np.hypot(lab[..., 1], lab[..., 2]) * sky).sum() / max(sky.sum(), 1))
            if chroma > 12:  # 蓝天：偏振镜效果
                lab[..., 0] *= 1 - 0.06 * S
                lab[..., 1:] *= 1 + 0.25 * S
            else:            # 阴天：保持亮度，只拉一点云层层次
                Lsky = lab[..., 0]
                Lb = cv2.GaussianBlur(Lsky, (0, 0), max(4.0, max(h, w) / 200))
                lab[..., 0] = np.clip(Lsky + (Lsky - Lb) * 0.6 * S, 0, 100)
            sky_img = from_lab(lab)
        elif scene == "dusk":
            lab = to_lab(sky_img)
            yy = np.linspace(-1, 1, h, dtype=np.float32)[:, None]
            lab[..., 0] = lab[..., 0] + yy * 6 * S  # 天空渐变层次（上暗下亮）
            lab[..., 1:] *= 1 + 0.35 * S
            sky_img = from_lab(lab)
        out = out * (1 - sm) + sky_img * sm
        log["steps"].append("天空局部优化")
    # 高光回收（玻璃反光、灯箱）
    Lh = luminance(np.clip(out, 0, None))
    out = apply_luma_ratio(out, Lh, soft_clip(Lh, 0.9), sat_comp=1.0)

    # 7. 大屏强化
    if screen is not None:
        out = boost_screen(np.clip(out, 0, 1), screen, p.screen_boost * (0.5 + 0.5 * S) * 1.0, scene)
        log["steps"].append("大屏强化")

    # 8. 大片感
    A = float(np.clip(p.atmosphere, 0, 1))
    out = add_bloom(np.clip(out, 0, 1), pre["bloom"] * mod["bloom"] * A, pre["bloom_thr"])
    out = add_atmosphere(out, scene, A, sky, seed=seed, grain=p.grain)
    log["steps"].append("大片感氛围")

    # 9. 锐化
    out = sharpen(np.clip(out, 0, None), p.sharpen)
    log["steps"].append("输出锐化")
    # 10. 高光保护：前面各步（夜景提亮、大屏增强、光晕、锐化）叠加后统一软压缩，避免屏幕 / 灯光死白
    Lf = luminance(np.clip(out, 0, None))
    out = apply_luma_ratio(out, Lf, soft_clip(Lf, 0.93), sat_comp=1.0)
    out = gamut_compress(np.clip(out, 0, None))
    return np.clip(out, 0, 1).astype(np.float32), log


def gamut_compress(img: np.ndarray, knee: float = 0.92) -> np.ndarray:
    """饱和色单通道溢出（亮度没过曝但某个通道 > 1）时，把色度向亮度收拢，保持色相与亮度，避免色块死掉。"""
    m = img.max(axis=2)
    over = m > knee
    if not over.any():
        return img
    L = luminance(img)[..., None]
    tm = soft_clip(m, knee)[..., None]
    mm = m[..., None]
    k = np.where(over[..., None], np.clip((tm - L) / np.maximum(mm - L, 1e-6), 0, 1), 1.0)
    return L + (img - L) * k

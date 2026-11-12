"""第二层：客户画面替换 + 真实融合（去“贴纸感”）。

流程：
  客户画面 → 按屏幕物理宽高比适配（不变形）
          → 屏幕内容空间：亮度/黑位/白点匹配（LED 实拍响应）、色温匹配、
            玻璃高光与环境反射（在内容空间生成，随曲面 / 折面一起形变）、LED 像素格（可选）
          → 多面片映射（geometry.compute_maps，含镜头畸变）
          → 图像空间：镜头柔化、噪点匹配、边缘羽化、LED 溢光 / 光晕、遮挡物还原

所有“匹配”参数都从原图的屏幕区域与周边环带测得；视频任务在关键帧测一次后冻结（FusionState），
逐帧复用，保证不闪烁。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, asdict, field
from typing import Optional

import cv2
import numpy as np

from . import geometry as G
from .analysis import LUMA, luminance, estimate_noise, ring_mask, classify_scene

FIT_MODES = ("cover", "contain", "stretch")


@dataclass
class FusionParams:
    fit: str = "cover"               # 画面适配：cover 等比裁切 / contain 等比留黑 / stretch 拉伸
    focus: list = field(default_factory=lambda: [0.5, 0.5])  # cover 裁切焦点
    brightness_match: float = 0.8    # 亮度 / 对比度匹配强度
    glow: float = 0.5                # 发光强度（LED 溢光、夜景光晕）
    reflection: float = 0.35         # 反光强度（玻璃高光 + 环境反射）
    wb_match: float = 0.6            # 色温匹配强度
    color_temp: float = 0.0          # 额外色温偏移 -1 冷 … +1 暖
    edge_feather: float = 0.5        # 边缘融合强度
    edge_expand: float = 1.0         # 边缘外扩（像素，按 3000px 长边计）：盖住标定误差，防止原广告从边缘漏出
    face_shading: float = 0.8        # 各面明暗匹配：按原片各面的白电平还原 LED 视角衰减（折角两侧明暗不同才有立体感）
    noise_match: float = 1.0         # 噪点匹配强度
    softness: float = 0.5            # 镜头柔化（贴入画面不能比原片更锐）
    led_grid: float = 0.0            # LED 像素格强度（近景大屏可开）
    led_columns: int = 0             # LED 横向像素数（0=按 physical_size 宽 / 10mm 估算）
    lens_k1: float = 0.0             # 镜头径向畸变 k1（桶形 <0）
    lens_k2: float = 0.0

    @classmethod
    def from_dict(cls, d: dict | None) -> "FusionParams":
        d = d or {}
        p = cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})
        if p.fit not in FIT_MODES:
            raise ValueError(f"fit 必须是 {FIT_MODES}")
        return p

    def to_dict(self):
        return asdict(self)


@dataclass
class FusionState:
    scene: str
    white: np.ndarray        # 屏幕“满白”在本照片中的 BGR 值
    black: np.ndarray        # 屏幕“全黑”在本照片中的 BGR 值（环境光 + LED 黑位）
    noise: float
    env: Optional[np.ndarray]  # 环境反射纹理（内容空间，延后按内容尺寸缩放）
    info: dict = field(default_factory=dict)
    face_u: Optional[np.ndarray] = None     # 各段的 u 边界（len = 段数 + 1）
    face_gain: Optional[np.ndarray] = None  # 各段相对亮度（LED 视角衰减），最亮的面 = 1
    smooth_faces: bool = False              # 弧形屏：段间平滑过渡；折角屏：折缝处突变


SCENE_WHITE = {"night": 0.97, "dusk": 0.93, "day": 0.86}
SCENE_GLOW = {"night": 0.55, "dusk": 0.32, "day": 0.12}
SCENE_REFL = {"night": 0.35, "dusk": 0.7, "day": 1.0}


def make_distortion(p: FusionParams, w: int, h: int) -> G.Distortion:
    return G.Distortion(p.lens_k1, p.lens_k2, w, h)


def prepare_state(base: np.ndarray, geom: G.ScreenGeometry, params: FusionParams,
                  scene: Optional[str] = None, reference: Optional[np.ndarray] = None) -> FusionState:
    """从原图测量：屏幕白点 / 黑位、噪声、环境反射纹理。

    reference：未经精修的原片（流程 B 时 base 是精修后的底图）。各面明暗比例是屏体的物理属性，
    必须在原片上测；精修里的大屏增强会把各面白电平都推到接近饱和，抹掉这个差异。
    """
    h, w = base.shape[:2]
    scene = scene or classify_scene(base)["scene"]
    mask = G.screen_mask(geom, w, h)
    inner = cv2.erode((mask > 0.99).astype(np.uint8), np.ones((5, 5), np.uint8)) > 0
    if inner.sum() < 50:
        inner = mask > 0.5
    px = base[inner]
    L = px @ LUMA
    info = {"scene": scene, "screen_pixels": int(inner.sum())}

    # 白点：原屏最亮像素（若接近中性色，用它的颜色做色温参考）
    hi = px[L >= np.percentile(L, 98.5)] if len(px) > 20 else px
    white_obs = hi.mean(0) if len(hi) else np.ones(3, np.float32)
    neutral = float(white_obs.max() / max(white_obs.min(), 1e-4)) < 1.35
    # 环境（屏幕外环带）色温
    size = max(np.ptp(np.nonzero(mask > 0.5)[1]), 10)
    ring = ring_mask(mask, max(3, int(size * 0.02)), max(8, int(size * 0.25)))
    amb = base[ring > 0.5]
    ring_luma = float(np.median(amb @ LUMA)) if len(amb) > 50 else 0.1
    if len(amb) > 50:
        al = amb @ LUMA
        sel = (al > 0.03) & (al < 0.95)
        amb_c = np.power(np.mean(np.power(amb[sel] if sel.sum() > 50 else amb, 6), 0), 1 / 6)
    else:
        amb_c = np.ones(3)
    amb_c = amb_c / max(float(amb_c @ LUMA), 1e-6)
    if neutral:
        tint = white_obs / max(float(white_obs @ LUMA), 1e-6)
        tint_src = "原屏白点"
        tint_w = params.wb_match
    else:
        tint = amb_c
        tint_src = "环境光"
        tint_w = params.wb_match * 0.35
    tint = 1 + (tint - 1) * tint_w
    if abs(params.color_temp) > 1e-3:
        t = float(np.clip(params.color_temp, -1, 1)) * 0.1
        tint = tint * np.array([1 - t, 1, 1 + t])
    tint = tint / max(float(tint @ LUMA), 1e-6)

    m = float(np.clip(params.brightness_match, 0, 1))
    w_obs = float(np.percentile(L, 99)) if len(L) else 1.0
    w_lvl = (1 - m) * 1.0 + m * max(w_obs, SCENE_WHITE[scene] * 0.85)
    w_lvl = float(np.clip(w_lvl, 0.5, 1.0))
    b_obs = float(np.percentile(L, 1.5)) if len(L) else 0.0
    # 黑位 = 屏面对环境光的反射，不会比周围立面亮多少；原广告若没有暗部，不能用它的最暗值
    b_lvl = float(np.clip(m * min(b_obs, ring_luma * 1.2 + 0.01, 0.25), 0, 0.3))
    white = np.clip(tint * w_lvl, 0, 1.15).astype(np.float32)
    black = (np.clip(tint * 0.5 + amb_c * 0.5, 0, 2) * b_lvl).astype(np.float32)

    # 噪声：屏幕周边环带的噪声水平
    noise = estimate_noise(base, region=ring) if ring.sum() > 500 else estimate_noise(base)

    # 环境反射：屏幕上方区域上下翻转 + 强模糊（只在内容空间以很低强度叠加）
    ol = geom.outline()
    x0, x1 = int(max(ol[:, 0].min(), 0)), int(min(ol[:, 0].max(), w))
    y0 = int(max(ol[:, 1].min(), 0))
    hh = max(int(np.ptp(ol[:, 1])), 8)
    env = None
    if y0 > 8 and x1 - x0 > 8:
        src = base[max(0, y0 - hh): y0, x0:x1]
        env = cv2.GaussianBlur(src[::-1].copy(), (0, 0), max(2.0, (x1 - x0) * 0.02))
    # 各面白电平（LED 视角衰减）：每段取高分位亮度（原广告里的白字、高光），与最亮的段比较
    face_u = face_gain = None
    nseg = max(st_.seg for st_ in geom.strips) + 1
    if nseg > 1 and params.face_shading > 0:
        segs = np.zeros((h, w), np.int16) - 1
        for st_ in geom.strips:
            q = geom.distortion.distort(st_.quad)
            cv2.fillPoly(segs, [np.round(q).astype(np.int32)], int(st_.seg))
        segs[~inner] = -1
        p98 = []
        for i in range(nseg):
            px_i = (reference if reference is not None else base)[segs == i]
            p98.append(float(np.percentile(px_i @ LUMA, 98)) if len(px_i) > 200 else np.nan)
        p98 = np.array(p98)
        if np.isfinite(p98).sum() >= 2:
            p98 = np.where(np.isfinite(p98), p98, np.nanmax(p98))
            ratio = np.clip(p98 / np.nanmax(p98), 0.55, 1.0) ** float(params.face_shading)
            face_gain = ratio.astype(np.float32)
            bounds = [0.0] * (nseg + 1)
            for st_ in geom.strips:
                bounds[st_.seg + 1] = st_.u1
            face_u = np.array(bounds, np.float32)
            info["face_gain"] = [round(float(x), 3) for x in face_gain]
    info.update({"white": white.round(3).tolist(), "black": black.round(3).tolist(),
                 "tint_source": tint_src, "noise_sigma_255": round(noise * 255, 2)})
    smooth = len(geom.strips) > nseg  # 弧形屏每段有多条细面片 → 段间平滑；折角屏每面一片 → 折缝处明暗突变
    return FusionState(scene, white, black, noise, env, info, face_u, face_gain, smooth)


def _content_space(content: np.ndarray, st: FusionState, p: FusionParams, physical_w: Optional[float]) -> np.ndarray:
    """在内容空间完成：LED 响应、色温、反光、LED 像素格。"""
    ch, cw = content.shape[:2]
    c = np.clip(content, 0, 1)
    # LED 实拍响应：黑位被环境光抬起，白点按本照片曝光
    em = st.black + (st.white - st.black) * c
    if st.face_gain is not None and st.face_u is not None:
        uc = (np.arange(cw, dtype=np.float32) + 0.5) / cw
        seg = np.clip(np.searchsorted(st.face_u, uc, side="right") - 1, 0, len(st.face_gain) - 1)
        gcol = st.face_gain[seg]
        if st.smooth_faces:  # 弧形屏：亮度随曲率连续变化
            gcol = cv2.GaussianBlur(gcol.reshape(1, -1), (0, 0), max(1.0, cw * 0.03)).ravel()
        emissive = (st.white - st.black) * c * gcol[None, :, None]
        em = st.black + emissive
    # 高光肩部柔和，避免贴入画面比周围更“数码”
    Lm = em @ LUMA
    over = np.maximum(Lm - 0.85, 0)
    em = em * (np.where(Lm > 0.85, (0.85 + 0.15 * (1 - np.exp(-over / 0.15))) / np.maximum(Lm, 1e-4), 1))[..., None]

    k_refl = float(p.reflection) * SCENE_REFL[st.scene]
    if k_refl > 0:
        yy, xx = np.mgrid[0:ch, 0:cw].astype(np.float32)
        u, v = xx / cw, yy / ch
        band = np.exp(-(((u * 0.85 + v * 0.5) - 0.42) / 0.22) ** 2) * 0.05 + (1 - v) * 0.02
        refl = np.repeat(band[..., None], 3, axis=2) * k_refl
        if st.env is not None:
            env = cv2.resize(st.env, (cw, ch), interpolation=cv2.INTER_LINEAR)
            refl = refl + env * (0.10 * k_refl)
        em = 1 - (1 - em) * (1 - np.clip(refl, 0, 1))

    if p.led_grid > 0:
        cols = p.led_columns or (int(physical_w * 100) if physical_w else 0)
        if cols > 0:
            pitch = cw / cols
            if pitch >= 3:  # 内容分辨率下每颗灯至少 3px 才画，否则只会产生摩尔纹
                xx = (np.arange(cw) % pitch) / pitch
                yy = (np.arange(ch) % pitch) / pitch
                gx = 1 - p.led_grid * 0.35 * (np.abs(xx - 0.5) * 2) ** 4
                gy = 1 - p.led_grid * 0.35 * (np.abs(yy - 0.5) * 2) ** 4
                em = em * (gy[:, None] * gx[None, :])[..., None]
    return em.astype(np.float32)


def composite(base: np.ndarray, content: np.ndarray, geom: G.ScreenGeometry, params: FusionParams,
              st: FusionState, occluder: Optional[np.ndarray] = None, seed: int = 0,
              physical_w: Optional[float] = None) -> tuple[np.ndarray, np.ndarray]:
    """把客户画面合成进 base。返回 (结果图, 整图屏幕 alpha)。content 需已按 geom.aspect 适配。"""
    h, w = base.shape[:2]
    tex = _content_space(content, st, params, physical_w)
    res_scale = max(1.0, max(h, w) / 3000)
    layer, alpha, (x0, y0, x1, y1) = G.warp_content(tex, geom, w, h, expand=float(params.edge_expand) * res_scale)
    if layer.shape[2] == 1:
        layer = np.repeat(layer, 3, axis=2)

    # 镜头柔化：分辨率越高半径越大
    sig = 0.25 + 0.6 * float(params.softness) * max(1.0, max(h, w) / 3000)
    if sig > 0.3:
        layer = cv2.GaussianBlur(layer, (0, 0), sig)
    # 边缘羽化
    fe = float(params.edge_feather) * max(0.6, max(h, w) / 3000)
    if fe > 0.05:
        alpha = cv2.GaussianBlur(alpha, (0, 0), fe)
    # 噪点匹配：亮度相关的散粒噪声 + 轻微空间相关（去马赛克后的真实噪声形态）
    if params.noise_match > 0 and st.noise > 1e-4:
        rng = np.random.default_rng(seed)
        n = rng.standard_normal(layer.shape).astype(np.float32)
        n = cv2.GaussianBlur(n, (0, 0), 0.5) * 1.9
        Ll = (layer @ LUMA)[..., None]
        layer = layer + n * (st.noise * params.noise_match * (0.55 + 0.9 * np.sqrt(np.clip(Ll, 0, 1))))

    out = base.copy()
    sl = (slice(y0, y1), slice(x0, x1))
    a3 = alpha[..., None]
    out[sl] = base[sl] * (1 - a3) + layer * a3

    # LED 溢光 / 光晕：在扩展区域内模糊屏幕发光层，滤色叠加
    k_glow = float(params.glow) * SCENE_GLOW[st.scene]
    full_alpha = np.zeros((h, w), np.float32)
    full_alpha[sl] = alpha
    if k_glow > 0:
        size = max(x1 - x0, y1 - y0)
        gs = max(2.0, size * 0.035)
        mg = int(gs * 3)
        ex0, ey0, ex1, ey1 = max(0, x0 - mg), max(0, y0 - mg), min(w, x1 + mg), min(h, y1 + mg)
        src = np.zeros((ey1 - ey0, ex1 - ex0, 3), np.float32)
        src[y0 - ey0:y1 - ey0, x0 - ex0:x1 - ex0] = layer * a3
        glow = cv2.GaussianBlur(src, (0, 0), gs) * 0.7 + cv2.GaussianBlur(src, (0, 0), gs * 0.25) * 0.3
        ea = full_alpha[ey0:ey1, ex0:ex1, None]
        glow = glow * k_glow * (1 - 0.65 * ea)
        reg = out[ey0:ey1, ex0:ex1]
        out[ey0:ey1, ex0:ex1] = 1 - (1 - reg) * (1 - np.clip(glow, 0, 1))

    if occluder is not None and occluder.max() > 0:
        o = occluder[..., None]
        out = out * (1 - o) + base * o
        full_alpha = full_alpha * (1 - occluder)
    return np.clip(out, 0, 1), full_alpha


def replace_screen(base: np.ndarray, content: np.ndarray, tpl: G.ScreenTemplate,
                   params: FusionParams, scene: Optional[str] = None, seed: int = 0,
                   reference: Optional[np.ndarray] = None) -> tuple[np.ndarray, dict]:
    """单张图片：客户画面替换的完整入口。reference 为未精修原片（用于各面明暗测量）。"""
    h, w = base.shape[:2]
    geom = G.build_geometry(tpl, w, h, make_distortion(params, w, h))
    st = prepare_state(base, geom, params, scene, reference)
    fitted = G.fit_content(content, geom.aspect, params.fit, tuple(params.focus))
    occ = G.occluder_mask(tpl, w, h) if tpl.occluders else None
    out, a = composite(base, fitted, geom, params, st, occ, seed,
                       physical_w=(tpl.physical_size or [None])[0])
    info = {
        "screen_type": tpl.type,
        "aspect_used": round(geom.aspect, 4),
        "content_aspect": round(content.shape[1] / content.shape[0], 4),
        "fit": params.fit,
        "focal_px": round(geom.focal, 1),
        "strips": len(geom.strips),
        "segment_widths": [round(x, 4) for x in geom.segment_widths],
        "fusion": st.info,
    }
    return out, info

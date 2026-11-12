"""视频：屏幕锚点跟踪 + 逐帧贴合。

稳定性设计（对应“不漂移、不抖动、不闪烁、不延迟”）：

不漂移   特征点保持“关键帧身份”：每个跟踪点都记得它在关键帧中的坐标，单应矩阵始终是
         关键帧→当前帧直接估计（不是逐帧连乘），误差不累积；每帧再把当前帧反变换回关键帧
         做一次直接对齐（KLT 到关键帧）消除残余漂移。支持多个关键帧修正，相邻关键帧双向
         跟踪并按时间加权融合。
不抖动   前后向光流一致性检查 + RANSAC 去外点；点位轨迹做零相位高斯平滑（离线处理，前后帧都可见）
不延迟   零相位平滑（不是因果滤波），贴图与原片同帧对齐
不闪烁   融合参数（白点/黑位/色温/噪声）与调色参数在关键帧测一次后冻结；屏幕 u 分布（折面宽度、
         焦距、宽高比）在关键帧计算后冻结，逐帧只改变点位
锚点区域 只用屏幕外框及周边一圈的特征（屏幕内部内容在变，不能用来跟踪）
"""
from __future__ import annotations

import math
from dataclasses import dataclass, asdict
from typing import Callable, Optional

import cv2
import numpy as np

from . import geometry as G
from . import replace as R
from . import enhance as E
from .imgio import video_info, iter_video, VideoWriter, read_image, kind_of


@dataclass
class VideoParams:
    keyframe: int = 0           # 标定所在帧
    smooth: float = 0.0         # 轨迹平滑（帧，高斯 sigma）。默认关闭：跟踪噪声本身 <0.1px，
                                # 平滑会抹掉真实的镜头抖动，使屏幕相对建筑“漂浮”；仅跟踪质量差时开 0.5–1
    loop_ad: bool = True        # 客户视频比原片短时循环播放（否则停在最后一帧）
    keep_audio: bool = True
    crf: int = 18               # H.264 质量（越小越好）
    max_frames: int = 0         # 0=全部
    track_budget_mb: int = 450  # 跟踪用灰度帧缓存上限

    @classmethod
    def from_dict(cls, d):
        d = d or {}
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})

    def to_dict(self):
        return asdict(self)


# ---------------------------------------------------------------- tracking

def _anchor_mask(shape, outline: np.ndarray, grow: float) -> np.ndarray:
    """屏幕外框 + 周边一圈，去掉屏幕内部（内容会变化）。"""
    h, w = shape
    m_scr = np.zeros((h, w), np.uint8)
    cv2.fillPoly(m_scr, [np.round(outline).astype(np.int32)], 255)
    size = max(np.ptp(outline[:, 0]), np.ptp(outline[:, 1]), 8)
    # 距离变换代替超大核膨胀（后者在大屏时极慢）
    dist = cv2.distanceTransform((m_scr == 0).astype(np.uint8), cv2.DIST_L2, 3)
    big = dist <= size * grow / 2
    inner = cv2.erode(m_scr, np.ones((7, 7), np.uint8))
    return (big & (inner == 0)).astype(np.uint8) * 255


def _detect(gray, mask, n):
    pts = cv2.goodFeaturesToTrack(gray, maxCorners=n, qualityLevel=0.005, minDistance=6, mask=mask, blockSize=7)
    return np.zeros((0, 2), np.float32) if pts is None else pts.reshape(-1, 2).astype(np.float32)


LK = dict(winSize=(21, 21), maxLevel=4, criteria=(cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 30, 0.01))


def _flow(a, b, pts):
    if len(pts) == 0:
        return pts, np.zeros(0, bool)
    p1, st1, _ = cv2.calcOpticalFlowPyrLK(a, b, pts.reshape(-1, 1, 2), None, **LK)
    p0, st2, _ = cv2.calcOpticalFlowPyrLK(b, a, p1, None, **LK)
    fb = np.linalg.norm(p0.reshape(-1, 2) - pts, axis=1)
    ok = (st1.ravel() == 1) & (st2.ravel() == 1) & (fb < 0.8)
    return p1.reshape(-1, 2), ok


def track_segment(frames: list, k: int, end: int, outline_small: np.ndarray,
                  progress: Optional[Callable] = None) -> dict:
    """从关键帧 k 跟踪到 end（含），返回 {帧号: (H_k→t, 质量)}。坐标为跟踪分辨率。"""
    step = 1 if end >= k else -1
    ref = frames[k]
    h, w = ref.shape
    mask = _anchor_mask((h, w), outline_small, 1.5)
    ref_pts = _detect(ref, mask, 500)
    if len(ref_pts) < 40:
        mask = np.full((h, w), 255, np.uint8)
        m_scr = np.zeros((h, w), np.uint8)
        cv2.fillPoly(m_scr, [np.round(outline_small).astype(np.int32)], 255)
        mask[cv2.erode(m_scr, np.ones((7, 7), np.uint8)) > 0] = 0
        ref_pts = _detect(ref, mask, 800)
    ref_feat = ref_pts.copy()
    cur_pts = ref_pts.copy()
    H = np.eye(3)
    out = {k: (H.copy(), {"inliers": len(ref_pts), "err": 0.0})}
    prev = ref
    t = k
    while t != end:
        t += step
        cur = frames[t]
        nxt, ok = _flow(prev, cur, cur_pts)
        rp, cp = ref_pts[ok], nxt[ok]
        quality = {"inliers": 0, "err": float("nan")}
        if len(rp) >= 8:
            Hn, inl = cv2.findHomography(rp, cp, cv2.RANSAC, 1.5, maxIters=3000, confidence=0.999)
            if Hn is not None:
                inl = inl.ravel().astype(bool)
                rp, cp = rp[inl], cp[inl]
                H = Hn
        # 直接对齐到关键帧：消除残余漂移
        try:
            Hinv = np.linalg.inv(H)
            warped = cv2.warpPerspective(cur, Hinv, (w, h), flags=cv2.INTER_LINEAR | cv2.WARP_FILL_OUTLIERS)
            sc = math.sqrt(abs(np.linalg.det(H[:2, :2])))
            if 0.6 < sc < 1.7 and len(ref_feat) >= 8:
                q, okq = _flow(ref, warped, ref_feat)
                if okq.sum() >= 12:
                    Q, iq = cv2.findHomography(ref_feat[okq], q[okq], cv2.RANSAC, 1.0)
                    if Q is not None and iq.sum() >= 10:
                        corr = H @ Q
                        corr /= corr[2, 2]
                        # 只接受小幅修正，防止错误匹配把结果带偏
                        test = np.array([[0, 0], [w, 0], [w, h], [0, h]], np.float32).reshape(-1, 1, 2)
                        dlt = np.abs(cv2.perspectiveTransform(test, corr) - cv2.perspectiveTransform(test, H)).max()
                        if dlt < 0.03 * max(w, h):
                            H = corr
        except np.linalg.LinAlgError:
            pass
        if len(rp) >= 8:
            proj = cv2.perspectiveTransform(rp.reshape(-1, 1, 2).astype(np.float64), H).reshape(-1, 2)
            quality = {"inliers": int(len(rp)), "err": float(np.median(np.linalg.norm(proj - cp, axis=1)))}
        out[t] = (H.copy(), quality)
        # 补点：当前帧新检测的点，反推其关键帧坐标（保持“关键帧身份”）
        if len(rp) < 250:
            outline_t = cv2.perspectiveTransform(outline_small.reshape(-1, 1, 2).astype(np.float64), H).reshape(-1, 2)
            m_t = _anchor_mask((h, w), outline_t, 1.5)
            for p in cp:
                cv2.circle(m_t, (int(p[0]), int(p[1])), 6, 0, -1)
            newp = _detect(cur, m_t, 400 - len(rp))
            if len(newp):
                back = cv2.perspectiveTransform(newp.reshape(-1, 1, 2).astype(np.float64), np.linalg.inv(H)).reshape(-1, 2)
                rp = np.vstack([rp, back.astype(np.float32)])
                cp = np.vstack([cp, newp])
        ref_pts, cur_pts = rp.astype(np.float32), cp.astype(np.float32)
        prev = cur
        if progress:
            progress(t)
    return out


def gaussian_smooth(series: np.ndarray, sigma: float) -> np.ndarray:
    """零相位高斯平滑（沿时间轴，镜像边界）。"""
    if sigma <= 0 or len(series) < 3:
        return series
    r = int(math.ceil(sigma * 3))
    x = np.arange(-r, r + 1)
    k = np.exp(-0.5 * (x / sigma) ** 2)
    k /= k.sum()
    if len(series) > r + 1:
        # 点对称延拓（2·端点 − 镜像），保持端点处的线性运动趋势，不会把首尾帧往回拉
        pad = np.concatenate([2 * series[:1] - series[r:0:-1], series, 2 * series[-1:] - series[-2:-r - 2:-1]], axis=0)
    else:
        pad = np.pad(series, ((r, r),) + ((0, 0),) * (series.ndim - 1), mode="edge")
    out = np.empty_like(series)
    flat = pad.reshape(pad.shape[0], -1)
    res = np.stack([np.convolve(flat[:, j], k, mode="valid") for j in range(flat.shape[1])], axis=1)
    out[:] = res.reshape(series.shape)
    return out


def track_video(path: str, keyframes: dict[int, G.ScreenTemplate], params: VideoParams,
                progress: Optional[Callable[[float, str], None]] = None) -> dict:
    """返回 {"top": (T,n,2), "bottom": (T,n,2), "quality": [...], "frames": T}（原始分辨率坐标）。"""
    info = video_info(path)
    W, H = info["width"], info["height"]
    total = info["frames"] if info["frames"] > 0 else 10 ** 9
    if params.max_frames:
        total = min(total, params.max_frames)
    # 跟踪分辨率：在内存预算内尽量高
    est = max(1, min(total, 100000))
    max_side = 1280
    while max_side > 480 and (max_side * max_side * min(H, W) / max(H, W)) * est / 1e6 > params.track_budget_mb:
        max_side -= 160
    s = min(1.0, max_side / max(W, H))
    frames = []
    for i, f in enumerate(iter_video(path)):
        if i >= total:
            break
        g = cv2.cvtColor(f, cv2.COLOR_BGR2GRAY)
        if s < 1:
            g = cv2.resize(g, (int(round(W * s)), int(round(H * s))), interpolation=cv2.INTER_AREA)
        frames.append(g)
        if progress and i % 20 == 0:
            progress(0.15 * i / max(1, min(total, info["frames"] or 1)), f"读取帧 {i}")
    T = len(frames)
    if T == 0:
        raise ValueError("视频没有可读取的帧")
    kfs = sorted(k for k in keyframes if 0 <= k < T)
    if not kfs:
        raise ValueError("关键帧超出视频范围")
    n = len(keyframes[kfs[0]].top)
    for k in kfs:
        if len(keyframes[k].top) != n or keyframes[k].type != keyframes[kfs[0]].type:
            raise ValueError("各关键帧的标定点数与屏幕类型必须一致")

    est_top = [[] for _ in range(T)]   # [(weight, top, bottom)]
    quality = [None] * T
    done = [0]

    def prog(_t):
        done[0] += 1
        if progress and done[0] % 10 == 0:
            progress(0.15 + 0.25 * done[0] / (T * 1.0 + len(kfs)), f"跟踪 {done[0]}/{T}")

    for idx, k in enumerate(kfs):
        tpl = keyframes[k].scaled_to(W, H)
        top = np.asarray(tpl.top, np.float64)
        bot = np.asarray(tpl.bottom, np.float64)
        geom = G.build_geometry(tpl, W, H)
        outline_small = geom.outline() * s
        prev_k = kfs[idx - 1] if idx > 0 else 0
        next_k = kfs[idx + 1] if idx + 1 < len(kfs) else T - 1
        segs = [track_segment(frames, k, next_k, outline_small, prog)]
        if k > prev_k or idx == 0:
            segs.append(track_segment(frames, k, prev_k, outline_small, prog))
        for seg in segs:
            for t, (Hs, q) in seg.items():
                Hf = np.diag([1 / s, 1 / s, 1]) @ Hs @ np.diag([s, s, 1])
                tt = cv2.perspectiveTransform(top.reshape(-1, 1, 2), Hf).reshape(-1, 2)
                bb = cv2.perspectiveTransform(bot.reshape(-1, 1, 2), Hf).reshape(-1, 2)
                # 权重：离本关键帧越近越可信（相邻关键帧之间线性交叉淡化）
                span = abs((next_k if t >= k else prev_k) - k) or 1
                wgt = max(1e-3, 1 - abs(t - k) / (span + 1e-9)) if t != k else 1e6
                est_top[t].append((wgt, tt, bb))
                if quality[t] is None or t == k:
                    quality[t] = q
    tops = np.zeros((T, n, 2))
    bots = np.zeros((T, n, 2))
    for t in range(T):
        if not est_top[t]:
            raise ValueError(f"第 {t} 帧跟踪失败")
        ws = np.array([e[0] for e in est_top[t]])
        ws = ws / ws.sum()
        tops[t] = sum(wi * e[1] for wi, e in zip(ws, est_top[t]))
        bots[t] = sum(wi * e[2] for wi, e in zip(ws, est_top[t]))
    if params.smooth > 0:
        # 关键帧处保持标定值不被平滑改动
        sm_t, sm_b = gaussian_smooth(tops, params.smooth), gaussian_smooth(bots, params.smooth)
        for k in kfs:
            sm_t[k], sm_b[k] = tops[k], bots[k]
        tops, bots = sm_t, sm_b
    return {"top": tops, "bottom": bots, "quality": quality, "frames": T, "track_scale": s, "info": info}


# ---------------------------------------------------------------- ad source

class AdSource:
    """客户素材：图片或视频；按原片时间取对应帧，保持客户视频正常播放速度。"""

    def __init__(self, path: Optional[str], loop: bool = True):
        self.path = path
        self.loop = loop
        self.kind = kind_of(path) if path else None
        self.cap = None
        self.cur_idx = -1
        self.cur = None
        if self.kind == "image":
            self.cur = read_image(path)
        elif self.kind == "video":
            vi = video_info(path)
            self.fps, self.n = vi["fps"], max(1, vi["frames"])
            self._open()

    def _open(self):
        if self.cap is not None:
            self.cap.release()
        self.cap = cv2.VideoCapture(str(self.path))
        self.cur_idx = -1

    def get(self, t: float) -> np.ndarray:
        if self.kind != "video":
            return self.cur
        idx = int(math.floor(t * self.fps + 1e-6))
        if idx >= self.n:
            idx = idx % self.n if self.loop else self.n - 1
        if idx < self.cur_idx:
            self._open()
        while self.cur_idx < idx:
            ok, f = self.cap.read()
            if not ok:
                self.n = max(1, self.cur_idx + 1)  # 帧数元数据不准时以实际为准
                if self.cur is None:
                    raise ValueError("客户视频无法读取")
                break
            self.cur_idx += 1
            if self.cur_idx >= idx - 0:
                self.cur = f.astype(np.float32) / 255.0
        return self.cur

    def close(self):
        if self.cap is not None:
            self.cap.release()

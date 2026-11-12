"""流程编排：A 只精修 / B 先精修再替换（默认推荐）/ C 先替换再精修；图片与视频共用。

输出版本：
  refined     精修增强照片（不含客户画面替换）
  final       最终成片（有客户素材时 = 替换后的照片 / 视频）
  client      客户交付版：最终成片按交付分辨率导出，无水印
  watermark   带醒目水印版：用于未付款预览 / 方案提案
"""
from __future__ import annotations

import json
import math
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import cv2
import numpy as np

from . import geometry as G
from . import enhance as E
from . import replace as R
from . import crowd as C
from . import video as V
from .imgio import read_image, write_image, kind_of, video_info, read_video_frame, VideoWriter, iter_video, resize_max, to_u8

MODES = ("enhance", "enhance_then_replace", "replace_then_enhance")
MODE_LABEL = {"enhance": "流程A 只精修", "enhance_then_replace": "流程B 先精修再替换",
              "replace_then_enhance": "流程C 先替换再精修"}

FONT_CANDIDATES = [
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "C:/Windows/Fonts/msyhbd.ttc",
    "C:/Windows/Fonts/msyh.ttc",
]


# ---------------------------------------------------------------- watermark

_wm_cache: dict = {}


def watermark_layer(w: int, h: int, text: str) -> np.ndarray:
    """RGBA 水印层（float32）：斜向平铺文字 + 右下角大字，醒目但不完全遮挡画面。"""
    key = (w, h, text)
    if key in _wm_cache:
        return _wm_cache[key]
    from PIL import Image, ImageDraw, ImageFont

    font_path = next((f for f in FONT_CANDIDATES if Path(f).exists()), None)
    size = max(18, int(min(w, h) * 0.045))

    def font(sz):
        if font_path:
            try:
                return ImageFont.truetype(font_path, sz)
            except OSError:
                pass
        return ImageFont.load_default()

    if not font_path:
        text = "DIBIAO MEDIA PREVIEW"
    diag = int(math.hypot(w, h))
    tile = Image.new("RGBA", (diag, diag), (0, 0, 0, 0))
    d = ImageDraw.Draw(tile)
    f = font(size)
    tw = d.textlength(text, font=f)
    step_x, step_y = int(tw + size * 3), int(size * 4.5)
    for row, y in enumerate(range(0, diag, step_y)):
        off = (row % 2) * step_x // 2
        for x in range(-step_x, diag, step_x):
            d.text((x + off, y), text, font=f, fill=(255, 255, 255, 70), stroke_width=max(1, size // 18),
                   stroke_fill=(0, 0, 0, 40))
    tile = tile.rotate(28, resample=Image.BICUBIC)
    left, top = (diag - w) // 2, (diag - h) // 2
    layer = tile.crop((left, top, left + w, top + h))
    d2 = ImageDraw.Draw(layer)
    big = font(int(size * 1.6))
    bw = d2.textlength(text, font=big)
    d2.text((w - bw - size, h - size * 3), text, font=big, fill=(255, 255, 255, 190),
            stroke_width=max(2, size // 10), stroke_fill=(0, 0, 0, 120))
    arr = np.asarray(layer).astype(np.float32) / 255.0
    arr = arr[..., [2, 1, 0, 3]]  # RGBA → BGRA
    _wm_cache.clear()
    _wm_cache[key] = arr
    return arr


def apply_watermark(img: np.ndarray, text: str) -> np.ndarray:
    h, w = img.shape[:2]
    wm = watermark_layer(w, h, text)
    a = wm[..., 3:4]
    return img * (1 - a) + wm[..., :3] * a


def resize_output(img: np.ndarray, max_side: int) -> np.ndarray:
    """交付分辨率：缩小用面积插值；放大用 Lanczos + 轻锐化（非 AI 超分，见文档）。"""
    if not max_side:
        return img
    h, w = img.shape[:2]
    s = max_side / max(h, w)
    if abs(s - 1) < 1e-3:
        return img
    size = (int(round(w * s)), int(round(h * s)))
    if s < 1:
        return cv2.resize(img, size, interpolation=cv2.INTER_AREA)
    up = cv2.resize(img, size, interpolation=cv2.INTER_LANCZOS4)
    return np.clip(E.sharpen(np.clip(up, 0, 1), 0.35), 0, 1)


# ---------------------------------------------------------------- job spec

@dataclass
class JobSpec:
    mode: str
    base: str
    ad: Optional[str]
    template: Optional[dict]
    keyframes: dict
    enhance: E.EnhanceParams
    fusion: R.FusionParams
    crowd: C.CrowdParams
    lanes: list
    video: V.VideoParams
    output: dict
    preview: bool

    @classmethod
    def from_dict(cls, d: dict) -> "JobSpec":
        mode = d.get("mode", "enhance_then_replace")
        if mode not in MODES:
            raise ValueError(f"mode 必须是 {MODES}")
        ad = d.get("ad")
        tpl = d.get("template")
        kfs = {int(k): v for k, v in (d.get("keyframes") or {}).items()}
        if mode != "enhance" and not ad:
            raise ValueError("替换模式需要上传客户广告图 / 广告视频")
        if mode != "enhance" and not tpl and not kfs:
            raise ValueError("替换模式需要先完成屏幕标定")
        if tpl:
            G.ScreenTemplate.from_dict(tpl)
        for v in kfs.values():
            G.ScreenTemplate.from_dict(v)
        out = {"max_side": 0, "format": "jpg", "quality": 95, "watermark_text": "地标传媒 · 样片"}
        out.update(d.get("output") or {})
        if out["format"] not in ("jpg", "png"):
            raise ValueError("图片输出格式只支持 jpg / png")
        return cls(mode, d["base"], ad, tpl, kfs,
                   E.EnhanceParams.from_dict(d.get("enhance")), R.FusionParams.from_dict(d.get("fusion")),
                   C.CrowdParams.from_dict(d.get("crowd")), d.get("lanes") or [],
                   V.VideoParams.from_dict(d.get("video")), out, bool(d.get("preview")))


Progress = Callable[[float, str], None]


def _noop(p, m):
    pass


# ---------------------------------------------------------------- image

def run_image(spec: JobSpec, out_dir: Path, progress: Progress = _noop) -> dict:
    t0 = time.time()
    img = read_image(spec.base)
    oh, ow = img.shape[:2]
    scale = 1.0
    if spec.preview:
        img, scale = resize_max(img, 1400)
    h, w = img.shape[:2]
    report: dict = {"mode": MODE_LABEL[spec.mode], "size": [w, h]}
    tpl = None
    if spec.template:
        td = dict(spec.template)
        td["image_size"] = td.get("image_size") or [ow, oh]  # 标定点默认按原图坐标
        tpl = G.ScreenTemplate.from_dict(td)
    lanes = spec.lanes
    if scale != 1.0:
        lanes = [{**l, "left": [[x * scale, y * scale] for x, y in l.get("left", [])],
                  "right": [[x * scale, y * scale] for x, y in l.get("right", [])]} for l in lanes]
    geom = None
    mask = None
    if tpl:
        geom = G.build_geometry(tpl, w, h, R.make_distortion(spec.fusion, w, h))
        mask = G.screen_mask(geom, w, h)

    stats = E.analyze(img, spec.enhance)
    report["scene"] = stats.scene
    report["scene_scores"] = stats.info["classify"]["scores"]
    ad = read_image(spec.ad) if spec.ad and kind_of(spec.ad) == "image" else None
    if spec.ad and kind_of(spec.ad) == "video":
        ad = read_video_frame(spec.ad, 0)

    def do_enhance(x, msg):
        progress(0.1, msg)
        y, log = E.enhance(x, spec.enhance, screen=mask, stats=stats)
        report["enhance"] = log
        progress(0.45, "人流车流增强")
        y, cinfo = C.augment(y, lanes, spec.crowd, stats.scene, screen_mask=mask)
        report["crowd"] = cinfo
        return y

    def do_replace(x, msg):
        progress(0.55, msg)
        y, rinfo = R.replace_screen(x, ad, tpl, spec.fusion, scene=stats.scene, reference=img)
        report["replace"] = rinfo
        return y

    outputs = {}
    ext = spec.output["format"]
    if spec.mode == "enhance":
        refined = do_enhance(img, "精修增强")
        final = refined
    elif spec.mode == "enhance_then_replace":
        refined = do_enhance(img, "精修底图")
        final = do_replace(refined, "客户画面贴合")
    else:
        replaced = do_replace(img, "客户画面贴合")
        # 先替换再精修：屏幕已是客户画面，大屏强化照常作用
        refined = None
        final = do_enhance(replaced, "全局精修")

    progress(0.85, "导出")
    if refined is not None and spec.mode != "enhance":
        write_image(out_dir / f"refined.{ext}", refined, spec.output["quality"])
        outputs["refined"] = f"refined.{ext}"
    write_image(out_dir / f"final.{ext}", final, spec.output["quality"])
    outputs["final"] = f"final.{ext}"
    client = resize_output(final, int(spec.output.get("max_side") or 0))
    write_image(out_dir / f"client.{ext}", client, spec.output["quality"])
    outputs["client"] = f"client.{ext}"
    write_image(out_dir / "watermark.jpg", apply_watermark(client, spec.output["watermark_text"]), 92)
    outputs["watermark"] = "watermark.jpg"
    write_image(out_dir / "original.jpg", img, 92)
    outputs["original"] = "original.jpg"
    report["client_size"] = [client.shape[1], client.shape[0]]
    report["seconds"] = round(time.time() - t0, 2)
    return {"outputs": outputs, "report": report}


# ---------------------------------------------------------------- video

def run_video(spec: JobSpec, out_dir: Path, progress: Progress = _noop) -> dict:
    t0 = time.time()
    info = video_info(spec.base)
    W, H = info["width"], info["height"]
    report: dict = {"mode": MODE_LABEL[spec.mode], "size": [W, H], "fps": info["fps"]}
    kfs = dict(spec.keyframes)
    if spec.template and not kfs:
        kfs[spec.video.keyframe] = spec.template
    templates = {k: G.ScreenTemplate.from_dict(v) for k, v in kfs.items()}
    replacing = spec.mode != "enhance"

    track = None
    if templates:
        progress(0.02, "屏幕跟踪")
        track = V.track_video(spec.base, templates, spec.video, progress)
        q = [x for x in track["quality"] if x]
        errs = [x["err"] for x in q if x["err"] == x["err"]]
        report["tracking"] = {
            "frames": track["frames"],
            "median_reproj_err_px": round(float(np.median(errs)) / track["track_scale"], 3) if errs else None,
            "min_inliers": int(min(x["inliers"] for x in q)) if q else 0,
            "keyframes": sorted(kfs),
        }

    k0 = min(templates) if templates else spec.video.keyframe
    key_frame = read_video_frame(spec.base, k0)
    vp = E.EnhanceParams.from_dict({**spec.enhance.to_dict(), "video_safe": True})
    stats = E.analyze(key_frame, vp)
    report["scene"] = stats.scene

    # 关键帧上冻结：几何（宽高比 / 折面宽度 / 焦距）与融合参数
    frozen = None
    st = None
    tpl0 = templates.get(k0)
    if tpl0 is not None:
        g0 = G.build_geometry(tpl0, W, H, R.make_distortion(spec.fusion, W, H))
        frozen = dict(frozen_widths=g0.segment_widths, frozen_aspect=g0.aspect, frozen_focal=g0.focal)
        if replacing:
            kb = key_frame
            if spec.mode == "enhance_then_replace":
                m0 = G.screen_mask(g0, W, H)
                kb, _ = E.enhance(key_frame, vp, screen=m0, stats=stats)
            st = R.prepare_state(kb, g0, spec.fusion, stats.scene, reference=key_frame)
            report["fusion"] = st.info
        report["aspect_used"] = round(g0.aspect, 4)

    ad = V.AdSource(spec.ad, spec.video.loop_ad) if replacing else None
    T = track["frames"] if track else (info["frames"] if not spec.video.max_frames else min(info["frames"], spec.video.max_frames))
    writer = VideoWriter(out_dir / "final.mp4", W, H, info["fps"], spec.video.crf)
    wm_writer = VideoWriter(out_dir / "watermark.mp4", W, H, info["fps"], 23)
    poster = None
    physical_w = (tpl0.physical_size or [None])[0] if tpl0 is not None else None
    wm_text = spec.output["watermark_text"]

    def process(t: int, frame: np.ndarray, ad_frame):
        img = frame.astype(np.float32) / 255.0
        geom = mask = occ = content = None
        if track is not None:
            tpl_t = tpl0.with_points(track["top"][t], track["bottom"][t])
            tpl_t.image_size = [W, H]
            geom = G.build_geometry(tpl_t, W, H, R.make_distortion(spec.fusion, W, H), **frozen)
            mask = G.screen_mask(geom, W, H)
            if tpl0.occluders:
                occ = G.occluder_mask(tpl_t, W, H)
        if replacing:
            content = G.fit_content(ad_frame, geom.aspect, spec.fusion.fit, tuple(spec.fusion.focus))
        if spec.mode == "enhance":
            out, _ = E.enhance(img, vp, screen=mask, stats=stats, seed=t)
        elif spec.mode == "enhance_then_replace":
            base, _ = E.enhance(img, vp, screen=mask, stats=stats, seed=t)
            out, _ = R.composite(base, content, geom, spec.fusion, st, occ, seed=t, physical_w=physical_w)
        else:
            rep, _ = R.composite(img, content, geom, spec.fusion, st, occ, seed=t, physical_w=physical_w)
            out, _ = E.enhance(rep, vp, screen=mask, stats=stats, seed=t)
        return out, to_u8(out), to_u8(apply_watermark(out, wm_text))

    # 帧间相互独立（参数已冻结）→ 多线程并行（OpenCV / NumPy 运算释放 GIL），按顺序写出
    import os
    from collections import deque
    from concurrent.futures import ThreadPoolExecutor

    workers = max(1, min(4, (os.cpu_count() or 2) - 1))
    pending: deque = deque()

    def drain(limit):
        nonlocal poster
        while len(pending) > limit:
            t_, fut = pending.popleft()
            out, u8, wm = fut.result()
            writer.write(u8)
            wm_writer.write(wm)
            if t_ == k0:
                poster = out
            if t_ % 5 == 0:
                progress(0.4 + 0.58 * t_ / max(T, 1), f"合成 {t_}/{T}")

    try:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for t, frame in enumerate(iter_video(spec.base)):
                if t >= T:
                    break
                ad_frame = ad.get(t / info["fps"]).copy() if replacing else None  # 主线程顺序取客户画面帧
                pending.append((t, ex.submit(process, t, frame, ad_frame)))
                drain(workers * 2)
            drain(0)
    finally:
        if ad:
            ad.close()
        audio = spec.base if spec.video.keep_audio else None
        writer.close(audio_from=audio)
        wm_writer.close(audio_from=audio)
    outputs = {"final": "final.mp4", "client": "final.mp4", "watermark": "watermark.mp4"}
    if poster is not None:
        write_image(out_dir / "poster.jpg", poster, 92)
        outputs["poster"] = "poster.jpg"
        write_image(out_dir / "original.jpg", key_frame, 92)
        outputs["original"] = "original.jpg"
    if track is not None:
        (out_dir / "track.json").write_text(json.dumps({
            "top": np.round(track["top"], 2).tolist(), "bottom": np.round(track["bottom"], 2).tolist(),
            "quality": track["quality"]}, ensure_ascii=False))
        outputs["track"] = "track.json"
    report["frames_written"] = writer.count
    report["seconds"] = round(time.time() - t0, 2)
    return {"outputs": outputs, "report": report}


def run_video_keyframe_preview(spec: JobSpec, out_dir: Path, progress: Progress = _noop) -> dict:
    """视频的快速预览：只处理关键帧（调参用）。"""
    k = min(spec.keyframes) if spec.keyframes else spec.video.keyframe
    frame = read_video_frame(spec.base, k)
    tmp = out_dir / "keyframe_src.png"
    write_image(tmp, frame)
    d = {"mode": spec.mode, "base": str(tmp), "ad": None, "template": spec.keyframes.get(k, spec.template),
         "enhance": {**spec.enhance.to_dict(), "video_safe": True}, "fusion": spec.fusion.to_dict(),
         "crowd": {"level": "off"}, "output": spec.output, "preview": True}
    if spec.ad:
        ad_img = read_video_frame(spec.ad, 0) if kind_of(spec.ad) == "video" else read_image(spec.ad)
        ad_tmp = out_dir / "ad_src.png"
        write_image(ad_tmp, ad_img)
        d["ad"] = str(ad_tmp)
    res = run_image(JobSpec.from_dict(d), out_dir, progress)
    res["report"]["note"] = f"视频预览：仅处理第 {k} 帧"
    return res


def run_job(spec_dict: dict, out_dir: Path, progress: Progress = _noop) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    spec = JobSpec.from_dict(spec_dict)
    if kind_of(spec.base) == "video":
        if spec.preview:
            return run_video_keyframe_preview(spec, out_dir, progress)
        return run_video(spec, out_dir, progress)
    return run_image(spec, out_dir, progress)

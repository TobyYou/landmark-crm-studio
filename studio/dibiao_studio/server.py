"""FastAPI 服务：素材上传、屏幕标定模板、任务（精修 / 替换 / 视频）、历史版本、网页工作台。

启动：  cd studio && .venv/bin/python -m dibiao_studio.server     （默认 http://localhost:8600）
环境变量：
  STUDIO_DATA_DIR   数据目录（默认 studio/data）
  STUDIO_PORT       端口（默认 8600）
  STUDIO_HOST       监听地址（默认 127.0.0.1；局域网共享用 0.0.0.0）
  STUDIO_WORKERS    同时处理的任务数（默认 1，视频任务内部已多线程）
"""
from __future__ import annotations

import json
import os
import shutil
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

import cv2
import numpy as np
from fastapi import FastAPI, File, HTTPException, UploadFile, Body
from fastapi.responses import FileResponse, Response, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from . import analysis as A
from . import geometry as G
from . import pipeline as P
from . import crowd as C
from .imgio import kind_of, read_image, video_info, read_video_frame, resize_max, to_u8, IMAGE_EXTS, VIDEO_EXTS

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get("STUDIO_DATA_DIR", ROOT / "data")).resolve()
UPLOADS, JOBS, TEMPLATES = DATA / "uploads", DATA / "jobs", DATA / "templates"
for d in (UPLOADS, JOBS, TEMPLATES):
    d.mkdir(parents=True, exist_ok=True)
STATIC = Path(__file__).resolve().parent / "static"

app = FastAPI(title="地标传媒 · 上刊素材精修与大屏合成", version=__version__)
_executor = ThreadPoolExecutor(max_workers=int(os.environ.get("STUDIO_WORKERS", "1")))
_lock = threading.Lock()
MAX_UPLOAD = 4 * 1024 ** 3


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _safe_id(x: str) -> str:
    if not x or not all(c.isalnum() or c in "-_" for c in x):
        raise HTTPException(400, "非法 ID")
    return x


# ---------------------------------------------------------------- uploads

def _upload_path(uid: str) -> Path:
    uid = _safe_id(uid)
    meta = UPLOADS / f"{uid}.json"
    if not meta.exists():
        raise HTTPException(404, "素材不存在")
    return UPLOADS / json.loads(meta.read_text())["file"]


@app.post("/api/uploads")
async def upload(file: UploadFile = File(...)):
    ext = Path(file.filename or "").suffix.lower()
    if ext not in IMAGE_EXTS | VIDEO_EXTS:
        raise HTTPException(400, f"不支持的文件类型 {ext}（图片：jpg/png/tif/webp，视频：mp4/mov）")
    uid = uuid.uuid4().hex[:12]
    dst = UPLOADS / f"{uid}{ext}"
    size = 0
    with dst.open("wb") as f:
        while chunk := await file.read(8 * 1024 * 1024):
            size += len(chunk)
            if size > MAX_UPLOAD:
                f.close()
                dst.unlink(missing_ok=True)
                raise HTTPException(413, "文件过大（上限 4GB）")
            f.write(chunk)
    kind = kind_of(dst)
    try:
        if kind == "image":
            img = read_image(dst)
            meta = {"width": img.shape[1], "height": img.shape[0]}
        else:
            meta = video_info(dst)
            img = read_video_frame(dst, 0)
    except Exception as e:  # noqa: BLE001
        dst.unlink(missing_ok=True)
        raise HTTPException(400, f"文件无法解码：{e}")
    prev, _ = resize_max(img, 1600)
    ok, buf = cv2.imencode(".jpg", to_u8(prev), [cv2.IMWRITE_JPEG_QUALITY, 88])
    (UPLOADS / f"{uid}_preview.jpg").write_bytes(buf.tobytes())
    info = {"id": uid, "kind": kind, "name": file.filename, "file": dst.name, "size": size,
            "created": _now(), **meta}
    (UPLOADS / f"{uid}.json").write_text(json.dumps(info, ensure_ascii=False))
    return info


@app.get("/api/uploads/{uid}")
def upload_info(uid: str):
    _upload_path(uid)
    return json.loads((UPLOADS / f"{uid}.json").read_text())


@app.get("/api/uploads/{uid}/preview")
def upload_preview(uid: str, frame: int = 0, max_side: int = 1600):
    p = _upload_path(uid)
    if kind_of(p) == "image" and frame == 0 and max_side == 1600:
        return FileResponse(UPLOADS / f"{uid}_preview.jpg", media_type="image/jpeg")
    img = read_image(p) if kind_of(p) == "image" else read_video_frame(p, frame)
    img, _ = resize_max(img, max(64, min(max_side, 4096)))
    ok, buf = cv2.imencode(".jpg", to_u8(img), [cv2.IMWRITE_JPEG_QUALITY, 88])
    return Response(buf.tobytes(), media_type="image/jpeg")


@app.post("/api/uploads/{uid}/detect")
def detect(uid: str, frame: int = 0):
    """自动框选屏幕候选（返回原图坐标的四角，供人工确认 / 微调）。"""
    p = _upload_path(uid)
    img = read_image(p) if kind_of(p) == "image" else read_video_frame(p, frame)
    return {"candidates": A.detect_screen_candidates(img), "scene": A.classify_scene(img)}


def _frame_img(uid: str, frame: int = 0):
    p = _upload_path(uid)
    return read_image(p) if kind_of(p) == "image" else read_video_frame(p, frame)


@app.post("/api/uploads/{uid}/snap")
def snap(uid: str, body: dict = Body(...)):
    """标定点吸附到真实屏幕边缘（直线边多点拟合、角点取边线交点）。body: {template, frame}"""
    tpl = dict(body.get("template") or {})
    try:
        G.ScreenTemplate.from_dict(tpl)
        img = _frame_img(uid, int(body.get("frame") or 0))
        if tpl.get("image_size") and list(tpl["image_size"]) != [img.shape[1], img.shape[0]]:
            raise ValueError("模板尺寸与素材不一致，请先在当前素材上标定")
        r = A.snap_template(img, tpl)
    except (ValueError, KeyError, TypeError) as e:
        raise HTTPException(400, str(e))
    return r


@app.post("/api/uploads/{uid}/lens")
def lens(uid: str, body: dict = Body(...)):
    """由屏幕直边估计镜头畸变 k1。body: {template, frame}"""
    tpl = dict(body.get("template") or {})
    try:
        G.ScreenTemplate.from_dict(tpl)
        return A.estimate_lens_k1(_frame_img(uid, int(body.get("frame") or 0)), tpl)
    except (ValueError, KeyError, TypeError) as e:
        raise HTTPException(400, str(e))


@app.post("/api/uploads/{uid}/unwrap")
def unwrap(uid: str, body: dict = Body(...)):
    """展开检查：把照片里的屏幕按物理比例展开成平面图（JPEG）。原屏画面里的字、圆、二维码应保持方 / 圆。"""
    tpl = dict(body.get("template") or {})
    try:
        t = G.ScreenTemplate.from_dict(tpl)
        img = _frame_img(uid, int(body.get("frame") or 0))
        h, w = img.shape[:2]
        geom = G.build_geometry(t, w, h, G.Distortion(float(body.get("lens_k1") or 0), 0, w, h))
        out = G.unwrap(img, geom, int(body.get("height") or 420))
    except (ValueError, KeyError, TypeError) as e:
        raise HTTPException(400, str(e))
    ok, buf = cv2.imencode(".jpg", to_u8(out), [cv2.IMWRITE_JPEG_QUALITY, 88])
    return Response(buf.tobytes(), media_type="image/jpeg")


# ---------------------------------------------------------------- templates

@app.get("/api/templates")
def list_templates():
    out = []
    for f in sorted(TEMPLATES.glob("*.json"), key=lambda x: -x.stat().st_mtime):
        try:
            out.append(json.loads(f.read_text()))
        except json.JSONDecodeError:
            continue
    return out


@app.post("/api/templates")
def save_template(tpl: dict = Body(...)):
    try:
        t = G.ScreenTemplate.from_dict(tpl)
    except (ValueError, TypeError) as e:
        raise HTTPException(400, str(e))
    if not t.name:
        raise HTTPException(400, "请填写模板名称（建议：点位名 + 机位，如“国贸弧形屏-东南航拍”）")
    t.id = _safe_id(t.id) if t.id else uuid.uuid4().hex[:10]
    d = t.to_dict()
    d["updated"] = _now()
    (TEMPLATES / f"{t.id}.json").write_text(json.dumps(d, ensure_ascii=False, indent=1))
    return d


@app.delete("/api/templates/{tid}")
def delete_template(tid: str):
    f = TEMPLATES / f"{_safe_id(tid)}.json"
    if not f.exists():
        raise HTTPException(404, "模板不存在")
    f.unlink()
    return {"ok": True}


@app.post("/api/geometry/preview")
def geometry_preview(body: dict = Body(...)):
    """标定实时预览：返回面片分界线与估计的屏幕宽高比、焦距（不渲染图像）。"""
    try:
        t = G.ScreenTemplate.from_dict(body["template"])
        w, h = body.get("width") or t.image_size[0], body.get("height") or t.image_size[1]
        k1 = float(body.get("lens_k1") or 0)
        geom = G.build_geometry(t, int(w), int(h), G.Distortion(k1, 0, int(w), int(h)))
    except (ValueError, KeyError, TypeError, IndexError) as e:
        raise HTTPException(400, str(e))
    cols = geom.column_lines()
    step = max(1, len(cols) // 40)
    return {
        "aspect": geom.aspect, "focal": geom.focal, "strips": len(geom.strips),
        "segment_widths": geom.segment_widths,
        "outline": geom.outline().tolist(),
        "columns": [c.tolist() for c in cols[::step]] + ([cols[-1].tolist()] if (len(cols) - 1) % step else []),
    }


# ---------------------------------------------------------------- jobs

def _job_dir(jid: str) -> Path:
    d = JOBS / _safe_id(jid)
    if not (d / "job.json").exists():
        raise HTTPException(404, "任务不存在")
    return d


def _save_job(job: dict):
    d = JOBS / job["id"]
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / "job.json.tmp"
    tmp.write_text(json.dumps(job, ensure_ascii=False, indent=1))
    os.replace(tmp, d / "job.json")


def _run(job: dict, spec: dict):
    d = JOBS / job["id"]
    last = [0.0]

    def progress(p, msg):
        now = time.time()
        if now - last[0] < 0.4 and p < 0.99:
            return
        last[0] = now
        with _lock:
            job.update(progress=round(float(p), 3), message=msg)
            _save_job(job)

    with _lock:
        job.update(status="running", started=_now(), message="开始处理")
        _save_job(job)
    try:
        res = P.run_job(spec, d / "out", progress)
        with _lock:
            job.update(status="done", progress=1.0, message="完成", finished=_now(),
                       outputs={k: f"/files/jobs/{job['id']}/out/{v}" for k, v in res["outputs"].items()},
                       report=res["report"])
            _save_job(job)
    except Exception as e:  # noqa: BLE001
        with _lock:
            job.update(status="failed", message=str(e), error=traceback.format_exc()[-3000:], finished=_now())
            _save_job(job)


@app.post("/api/jobs")
def create_job(body: dict = Body(...)):
    """body: {base_id, ad_id?, mode, template?, keyframes?, enhance, fusion, crowd, lanes, video, output,
    preview, title?, parent_id?}"""
    base = _upload_path(body.get("base_id", ""))
    ad = _upload_path(body["ad_id"]) if body.get("ad_id") else None
    spec = {k: body.get(k) for k in ("mode", "template", "keyframes", "enhance", "fusion", "crowd", "lanes",
                                     "video", "output", "preview")}
    spec["base"], spec["ad"] = str(base), (str(ad) if ad else None)
    if spec.get("template") and not spec["template"].get("image_size"):
        bi = json.loads((UPLOADS / f"{body['base_id']}.json").read_text())
        spec["template"]["image_size"] = [bi["width"], bi["height"]]
    try:
        P.JobSpec.from_dict(spec)
    except (ValueError, TypeError, KeyError) as e:
        raise HTTPException(400, str(e))
    jid = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
    job = {"id": jid, "title": body.get("title") or "", "status": "queued", "progress": 0.0, "message": "排队中",
           "created": _now(), "preview": bool(body.get("preview")), "mode": spec["mode"] or "enhance_then_replace",
           "base_id": body["base_id"], "ad_id": body.get("ad_id"), "parent_id": body.get("parent_id"),
           "kind": kind_of(base), "request": body}
    _save_job(job)
    _executor.submit(_run, job, spec)
    return job


@app.get("/api/jobs")
def list_jobs(limit: int = 100, include_preview: bool = False):
    out = []
    for d in sorted(JOBS.iterdir(), key=lambda x: x.name, reverse=True):
        f = d / "job.json"
        if not f.exists():
            continue
        try:
            j = json.loads(f.read_text())
        except json.JSONDecodeError:
            continue
        if j.get("preview") and not include_preview:
            continue
        j.pop("request", None)
        j.pop("error", None)
        out.append(j)
        if len(out) >= limit:
            break
    return out


@app.get("/api/jobs/{jid}")
def get_job(jid: str):
    return json.loads((_job_dir(jid) / "job.json").read_text())


@app.delete("/api/jobs/{jid}")
def delete_job(jid: str):
    d = _job_dir(jid)
    j = json.loads((d / "job.json").read_text())
    if j["status"] in ("queued", "running"):
        raise HTTPException(409, "任务处理中，不能删除")
    shutil.rmtree(d)
    return {"ok": True}


@app.get("/api/meta")
def meta():
    sprites = C.load_sprites()
    return {
        "version": __version__,
        "modes": P.MODE_LABEL,
        "sprites": {k: len(v) for k, v in sprites.items()},
        "data_dir": str(DATA),
    }


# 输出与上传文件（只读）
app.mount("/files/jobs", StaticFiles(directory=str(JOBS)), name="jobs")
app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


def main():
    import uvicorn

    uvicorn.run(app, host=os.environ.get("STUDIO_HOST", "127.0.0.1"), port=int(os.environ.get("STUDIO_PORT", "8600")))


if __name__ == "__main__":
    main()

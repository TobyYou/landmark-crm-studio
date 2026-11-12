"""图片 / 视频读写。内部统一使用 float32 BGR，取值 [0,1]。

cv2.imread 在部分平台不支持中文路径，这里统一走 np.fromfile + imdecode。
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import cv2
import numpy as np

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp"}
VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm"}


def kind_of(path: str | os.PathLike) -> str:
    ext = Path(path).suffix.lower()
    if ext in IMAGE_EXTS:
        return "image"
    if ext in VIDEO_EXTS:
        return "video"
    raise ValueError(f"不支持的文件类型: {ext}")


def read_image(path: str | os.PathLike) -> np.ndarray:
    data = np.fromfile(str(path), dtype=np.uint8)
    img = cv2.imdecode(data, cv2.IMREAD_UNCHANGED)
    if img is None:
        raise ValueError(f"无法读取图片: {path}")
    if img.ndim == 2:
        img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
    elif img.shape[2] == 4:
        img = img[:, :, :3]
    if img.dtype == np.uint16:
        return img.astype(np.float32) / 65535.0
    return img.astype(np.float32) / 255.0


def read_rgba(path: str | os.PathLike) -> np.ndarray:
    """读取带透明通道的素材（人/车贴图），返回 float32 BGRA。"""
    data = np.fromfile(str(path), dtype=np.uint8)
    img = cv2.imdecode(data, cv2.IMREAD_UNCHANGED)
    if img is None:
        raise ValueError(f"无法读取图片: {path}")
    if img.ndim == 2:
        img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGRA)
    elif img.shape[2] == 3:
        img = cv2.cvtColor(img, cv2.COLOR_BGR2BGRA)
    return img.astype(np.float32) / (65535.0 if img.dtype == np.uint16 else 255.0)


def to_u8(img: np.ndarray) -> np.ndarray:
    return np.clip(img * 255.0 + 0.5, 0, 255).astype(np.uint8)


def write_image(path: str | os.PathLike, img: np.ndarray, quality: int = 95) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    ext = path.suffix.lower()
    u8 = to_u8(img)
    if ext in (".jpg", ".jpeg"):
        ok, buf = cv2.imencode(".jpg", u8, [cv2.IMWRITE_JPEG_QUALITY, int(quality)])
    elif ext == ".png":
        ok, buf = cv2.imencode(".png", u8, [cv2.IMWRITE_PNG_COMPRESSION, 3])
    else:
        ok, buf = cv2.imencode(ext, u8)
    if not ok:
        raise ValueError(f"无法编码图片: {path}")
    buf.tofile(str(path))


def resize_max(img: np.ndarray, max_side: int) -> tuple[np.ndarray, float]:
    h, w = img.shape[:2]
    s = max_side / max(h, w)
    if s >= 1:
        return img, 1.0
    out = cv2.resize(img, (round(w * s), round(h * s)), interpolation=cv2.INTER_AREA)
    return out, s


# ---------------------------------------------------------------- video

def ffmpeg_exe() -> str:
    import imageio_ffmpeg

    return imageio_ffmpeg.get_ffmpeg_exe()


def video_info(path: str | os.PathLike) -> dict:
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        raise ValueError(f"无法打开视频: {path}")
    info = {
        "width": int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
        "height": int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)),
        "fps": float(cap.get(cv2.CAP_PROP_FPS) or 25.0),
        "frames": int(cap.get(cv2.CAP_PROP_FRAME_COUNT)),
    }
    cap.release()
    if info["fps"] <= 0 or info["fps"] > 240:
        info["fps"] = 25.0
    return info


def read_video_frame(path: str | os.PathLike, index: int) -> np.ndarray:
    cap = cv2.VideoCapture(str(path))
    cap.set(cv2.CAP_PROP_POS_FRAMES, max(0, int(index)))
    ok, frame = cap.read()
    cap.release()
    if not ok:
        raise ValueError(f"无法读取第 {index} 帧")
    return frame.astype(np.float32) / 255.0


def iter_video(path: str | os.PathLike):
    """逐帧产出 uint8 BGR。"""
    cap = cv2.VideoCapture(str(path))
    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            yield frame
    finally:
        cap.release()


class VideoWriter:
    """H.264 (libx264, yuv420p) 写出；结束后可从原片混入音轨。"""

    def __init__(self, path: str | os.PathLike, width: int, height: int, fps: float, crf: int = 18):
        import imageio_ffmpeg

        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # yuv420p 要求偶数宽高
        self.width, self.height = width - width % 2, height - height % 2
        self._tmp = self.path.with_suffix(".video_only.mp4")
        self._gen = imageio_ffmpeg.write_frames(
            str(self._tmp),
            (self.width, self.height),
            fps=fps,
            codec="libx264",
            pix_fmt_in="rgb24",
            pix_fmt_out="yuv420p",
            quality=None,
            macro_block_size=1,
            output_params=["-crf", str(crf), "-preset", "medium", "-movflags", "+faststart"],
        )
        self._gen.send(None)
        self.count = 0

    def write(self, frame_bgr: np.ndarray) -> None:
        if frame_bgr.dtype != np.uint8:
            frame_bgr = to_u8(frame_bgr)
        frame = frame_bgr[: self.height, : self.width]
        self._gen.send(np.ascontiguousarray(frame[:, :, ::-1]))
        self.count += 1

    def close(self, audio_from: str | os.PathLike | None = None) -> None:
        self._gen.close()
        if audio_from is not None:
            cmd = [
                ffmpeg_exe(), "-y", "-loglevel", "error",
                "-i", str(self._tmp), "-i", str(audio_from),
                "-map", "0:v:0", "-map", "1:a:0?", "-c:v", "copy", "-c:a", "aac",
                "-shortest", "-movflags", "+faststart", str(self.path),
            ]
            r = subprocess.run(cmd, capture_output=True)
            if r.returncode == 0:
                self._tmp.unlink(missing_ok=True)
                return
        os.replace(self._tmp, self.path)

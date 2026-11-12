"""生成演示 / 测试素材（合成场景，非真实照片）：
    python scripts/make_demo.py [输出目录，默认 data/demo]
生成：平面屏、L 型拐角屏、弧形屏夜景照片，客户广告图，航拍式测试视频及其点位真值。
"""
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dibiao_studio import synth  # noqa: E402
from dibiao_studio.imgio import write_image  # noqa: E402


def main(out: Path):
    out.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(0)
    w, h = 1920, 1080
    old_ad = np.zeros((400, 1300, 3), np.float32)
    old_ad[:] = (0.75, 0.45, 0.2)
    truths = {}
    for name, fn in (("flat", synth.flat_scene), ("folded", synth.folded_scene)):
        cam, faces, ub, tpl, phys = fn(w, h, f=1400)
        base = synth.city_background(w, h, night=True, seed=len(name))
        img, _ = synth.render_planar_faces(base, old_ad, cam, faces, ub)
        img = np.clip(img + rng.normal(0, 0.012, img.shape).astype(np.float32), 0, 1)
        write_image(out / f"{name}_screen_night.jpg", img, 92)
        truths[name] = {**tpl, "physical_size": list(phys)}
    cam, g, tpl, phys = synth.curved_scene(w, h, f=1500)
    base = synth.city_background(w, h, night=True, seed=9)
    img, _ = synth.render_cylinder(base, old_ad, cam, g)
    write_image(out / "curved_screen_night.jpg", np.clip(img + rng.normal(0, 0.012, img.shape).astype(np.float32), 0, 1), 92)
    truths["curved"] = {**tpl, "physical_size": list(phys)}
    write_image(out / "client_ad_16x9.jpg", synth.test_pattern(1920, 1080), 95)
    gts = synth.make_test_video(str(out / "aerial_test.mp4"), w=1280, h=720, frames=75)
    g0 = gts[0]
    truths["video"] = {"type": "flat", "top": [g0[0].tolist(), g0[1].tolist()],
                       "bottom": [g0[3].tolist(), g0[2].tolist()], "image_size": [1280, 720]}
    (out / "truth_templates.json").write_text(json.dumps(truths, ensure_ascii=False, indent=1))
    print("已生成：", ", ".join(sorted(p.name for p in out.iterdir())))


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent / "data" / "demo")

# Prompt for Codex: build per-screen profiles for Landmark's 11 LED screens

> Copy everything below the line into Codex. It is written for the Codex session that works on the Landmark website project.

---

## Who we are and why this request exists

We are 地标传媒 (Landmark Media), a Wuhan outdoor LED media company. You have been working with us on the Landmark website project at:

`/Users/tobysmacbookpro/Documents/ChatGPT/Landmark地标传媒-website/`

That project holds most of what we know about our 11 screens: `site/city_resources.json`, the March 2026 commercial deck extraction (`sources/extracted/2026-10-19-commercial-deck/`), the Feishu photo imports with per-photo screen verification (`sources/extracted/2026-10-19-feishu-media/photo-review.json`), campaign photos (`site/dist/images/campaigns/`), location photos (`site/dist/images/locations/`), and videos (`site/dist/video/`).

In a separate project we are building an internal image tool, **Studio (上刊素材精修与大屏合成系统)**, at:

`/Users/tobysmacbookpro/地标公司内部销售管理系统/studio/`
(full design doc: `/Users/tobysmacbookpro/地标公司内部销售管理系统/docs/06-上刊素材精修与大屏替换系统.md`)

The same code and docs are on GitHub (public): https://github.com/TobyYou/landmark-crm-studio
(`studio/` = the tool, `docs/06` = design and real-photo test results, `docs/07` = this brief). Read it if the local path isn't available to you. Don't push to that repo; hand results back as described in section 8.

Studio is currently generic: it can handle "any flat / curved / corner screen". We now want to **customize it to our 11 specific screens**, because generic geometry estimation is where it fails on real photos. You know these screens from the website work: what each one looks like, which photos show it in daylight and at night, which are corner or curved screens, what surrounds them. **We need you to turn that knowledge into one structured, evidence-backed profile per screen** that Studio can import.

Please read this whole brief first. The task itself is in section 5, and the output format is in section 6.

---

## 1. What Studio does

**Layer 1: refine a real 上刊 (on-air) photo.** Denoise, dynamic range, scene-aware grading (day / dusk / night), screen boost (brighter, clearer LED), optional traffic light trails or people/vehicle cut-outs, subtle "cinematic" atmosphere. All deterministic. It must never change buildings, roads, or the screen's position.

**Layer 2: replace what is on the screen with a new client ad** (image or video), so it looks like the client's ad really played there. This is used for client proof-of-play delivery and for sales proposals ("this is how your ad would look on 三镇之眼").

**Video.** The screen is tracked through drone or handheld footage, and the client video plays on it frame by frame.

How replacement works (this is why we need geometry data):

- An operator **calibrates** the screen in the photo by clicking a top-edge point row and a matching bottom-edge point row. Each top/bottom pair must sit on the same vertical seam of the physical screen.
  - `flat`: 4 corners.
  - `folded` (L-corner / multi-face): one column per face edge or fold.
  - `curved`: 7–11 columns along the arc, ideally on LED cabinet seams.
- The screen is split into planar strips. Each strip gets its own perspective mapping. Content is distributed horizontally by **physical width**, not by pixel length in the photo. That is what keeps text and logos from being stretched or squashed on corner and curved screens.
- Realism layer: match the photo's LED white and black level, colour temperature, noise, lens softness, glow and halo, glass reflection, and per-face brightness. It also keeps objects in front of the screen (lamp posts, trees) in front.

A calibration is saved as a **template** (JSON), reusable for every photo taken from the same camera position (机位):

```json
{
  "type": "flat | curved | folded",
  "top": [[x, y], ...], "bottom": [[x, y], ...],
  "image_size": [w, h],
  "u_mode": "auto | equal | physical",
  "segment_widths": [12.0, 8.0],
  "physical_size": [20.0, 6.0],
  "turn_angle": 90,
  "occluders": [[[x, y], ...]],
  "name": "", "notes": ""
}
```

`segment_widths` are the physical widths in metres of each face or segment, left to right as seen from the camera. `turn_angle` is the horizontal angle between the leftmost and rightmost face (90 for an L-corner). Lanes for traffic trails are polygons `{kind: road|sidewalk, left: [...], right: [...], width_m, direction}`.

Built-in calibration helpers:
- **吸附边缘:** snaps points to the real screen edge.
- **展开检查 (flatten check):** shows the screen flattened. Square glyphs and round circles mean the calibration is right.
- **估计畸变:** estimates lens distortion from straight edges.

---

## 2. What we learned testing on real photos (2026-11-12)

Three full-resolution Feishu originals were tested, using real ads from other photos as stand-in client content:

| Photo | Screen (from `photo-review.json`) | Light | Shape seen | Official aspect (m / px) | Studio's calibrated aspect |
|---|---|---|---|---|---|
| `record-007-09.jpg` (6144×4096, drone) | `mark-mall` | dusk / blue hour | L-corner with a **rounded corner**; left face seen almost edge-on | 3.068 / 3.000 | 3.012 |
| `record-142-01.jpg` (6144×4096, DJI 2026-10-15 19:48) | `sanzhen-eye` | night | sharp **90° L-corner** | 4.642 / 4.643 | 4.545 |
| `record-026-01.jpg` (4963×3171) | `xudong-ouyada` | overcast day | flat | 1.898 / 1.905 | 1.835 |

Problems found and already fixed in Studio:

1. **Corner screens: the edge-on face was computed 1.5× too wide.** Single-face perspective cannot pin down the focal length when a face is seen at a grazing angle. Feishu strips EXIF, so focal length is unknown. Fixed by a `turn_angle` field (90° for L-corners), which recovers focal length from the two faces' vanishing points. **This only works if we know each screen's real turn angle.**
2. **Curved screens: text "stepped" at calibration points.** Fixed by matching top and bottom points along true vertical lines.
3. **A sliver of the old ad stayed visible at the edge** (the camera wasn't level). Fixed by edge snapping and a 1 px edge expansion.
4. **Overcast daytime photos came out greyer and darker.** Fixed.
5. **Saturated colours clipped.** Fixed with gamut compression.
6. **Traffic light trails were too dense** and merged into a white smear. Toned down.
7. **The replaced screen looked like a bent sticker** because both faces had equal brightness. Fixed by measuring each face's white level in the original photo (LED viewing-angle falloff).

Estimates from our calibrations. **These are not confirmed facts; please check them:**

- `mark-mall`: left face ≈ 14.0 m, rounded corner ≈ 1.9 m along the surface, right face ≈ 19.7 m. Total ≈ 35.5 m against the official 36.2 m.
  - The ad on screen in `record-007-09` appears to be two separately designed panels: roughly 4:3 on the left face (including the corner) and roughly 16:9 on the right.
  - In the 4800×1600 px canvas that would be roughly 2128 + 2672 px.
- `sanzhen-eye`: left face ≈ 30.3 m, right face ≈ 36.4 m. Total ≈ 66.7 m against the official 68.1 m.
  - So the deck's "宽" (width) for corner screens appears to be the **unfolded length along the surface**, not the straight-line width (which would be about 47 m).
  - In `record-142-01`, the same China Mobile creative is shown on both faces (framed differently).

---

## 3. What we already know about the 11 screens (from `site/city_resources.json`, deck dated 2026-03)

| slug (city_resources) | slug in photo-review.json | Name | Pixels | Metres (宽×高) | px/m W vs H | Shape (what we know) |
|---|---|---|---|---|---|---|
| sanzhen-eye | sanzhen-eye | 三镇之眼 (钟家村祁遇) | 7800×1680 | 68.1×14.67 | consistent | **L-corner, ~90°, confirmed in photo 142-01** |
| wuhan-light | wuhan-light | 武汉之光 (光谷世界城) | 5616×2052 | 46.8×17.1 | consistent | deck says "**270°双视角裸眼3D**": a wrap / corner screen; geometry unknown |
| yangtze-star | yangtze-star | 长江之星 (徐东中商世界里) | 7280×2080 | 48×16 | **mismatch 16.7%** (px aspect 3.50 vs m aspect 3.00) | possibly a curved / rounded corner (street-view photo hint); unconfirmed |
| mark-mall | mark-mall | Mark Mall (江汉路马客茂) | 4800×1600 | 36.2×11.8 | −2.2% | **L-corner with rounded corner, confirmed in photo 007-09** |
| hankou-station-ouyada | hankou-ouyada | 汉口火车站欧亚达 | 1280×720 | 33×18.5 | consistent (≈25.7 mm pitch) | unknown |
| wuguang-suning | (no verified photos) | 武广苏宁易购 | 1472×1056 | 16.7×12 | consistent | unknown |
| jianghan-yanhuo | jianghan-smoke-lane | 江汉路烟火巷 | 3600×1680 | 18.25×8.32 | −2.3% | **rounded corner** seen in `record-054-01` and the location detail photo; unconfirmed geometry |
| xudong-ouyada | xudong-ouyada | 徐东欧亚达 | 1280×672 | 26×13.7 | consistent | **flat, confirmed in photo 026-01** |
| zhongnan-zhongshang | zhongnan-zhongshang | 中南中商广场 | 1080×1660 (portrait) | 12.5×19.2 | consistent | unknown |
| guanggu-capital | optics-capital | 光谷资本大厦 | 3840×1760 | 24×11 | consistent | unknown |
| moore-city | morr-city | 王家湾摩尔城 | 1216×702 | 19.45×11.26 | consistent | unknown |

Known data issues:
- **Mismatched slugs.** Four points use different slugs in `city_resources.json` and `photo-review.json` (see table).
- **Swapped filenames.** For `sanzhen-eye`, `sanzhen-eye-map.webp` is actually the screen photo and `sanzhen-eye-detail.webp` is the map. The JSON asset keys compensate for this.
- **Unparseable traffic figure.** `guanggu-capital` traffic "114,1340" doesn't parse (already flagged in the file).
- **Interesting pattern, unconfirmed.** Both rounded-corner screens (`mark-mall`, `jianghan-yanhuo`) have about −2.2% px/m mismatch, which may relate to the corner modules or to how width was measured.
- **iCloud offloading.** Many files in the website project are offloaded to iCloud (`ls -lO` shows `compressed,dataless`) and reading them can hang. Check the flag first. **Don't trigger iCloud downloads without asking us.** Say which files you couldn't read.

---

## 4. Open problems this data should solve

1. **Shape is missing.** No field says flat / curved / corner / multi-face, how many faces, the width of each face, the turn angle, or the corner radius. This matters most: a wrong face split visibly stretches the client's text.
2. **We don't know what "宽" (width) means.** Is it the unfolded length or the straight-line width? And do the deck pixels mean the content canvas the client must deliver, or the physical LED count? (1280×720 on a 33 m screen suggests a canvas.)
3. **We don't know how pixels map to faces.** For a 4800 px wide canvas, which pixel columns land on which face? Without this, a client's native-resolution creative can't be placed exactly.
4. **We don't know how each screen looks in the morning or daytime versus at night.** Studio's realism defaults are generic. We want per-screen presets:
   - Daytime: sun direction and glare, reflections, how washed-out the screen looks, shadows from nearby buildings.
   - Night: how bright the screen is relative to its surroundings, glow and halo, nearby signage colours, whether the screen is off outside its hours.
5. **We don't know the standard camera positions (机位).** Each screen is photographed from a few recurring spots (drone positions, street corners). We want one calibration template per camera position, so we need to know them and have a representative full-resolution photo for each.
6. **We don't know what's in front and around.** Lamp posts, trees, signs, a pedestrian bridge or railings in front of the screen become occluders. Roads and sidewalks in the usual frame become places for light trails and crowd cut-outs.
7. **We don't know the content conventions.** Does each face run its own layout (as on Mark Mall) or one continuous creative? Does it use 裸眼3D content (武汉之光)? Are there safe areas, so that text isn't placed across a fold?
8. **We have no real video test.** We need to know which videos (`site/dist/video/*.mp4`, `record-*-video.mp4`) show which screen, from what kind of camera move.

---

## 5. Your task

For **each of the 11 screens**, research the website project's data, deck text, photo-review records and images. Produce one profile per screen in the JSON format in section 6. Specifically:

1. **Map every photo and video to its screen.**
   - Use `photo-review.json` `verified_screen`, `downloads.json`, campaign records and location images.
   - List the best references per screen. Aim for **at least one daytime/morning and one night** full-resolution photo per screen, plus any video.
   - For each reference give: path, local or iCloud-offloaded, resolution, time of day, camera type (drone or ground), roughly where the camera stands, which faces are visible, and whether the screen is lit.
   - Flag the 10 photos whose `verified_screen` is `null` if you can identify them from what's in the picture, and say what evidence you used.
2. **Describe the physical shape.** Give: type, face count, face order left to right as seen from the most common camera position, and each face's approximate width.
   - Also give corner type (sharp or rounded) and an approximate radius, the turn angle, any curvature, and any non-rectangular outline or cut-outs.
   - Use deck text, any spec documents in `sources/`, and what the photos show. If you estimate from photos, say how.
3. **Clear up what width and pixels mean** where any source says so (deck notes, rate card `rate-card.internal.json`, coworker materials). Explain the 长江之星 16.7% mismatch if you can find why.
4. **Describe how pixels map to faces**, and the content conventions: one continuous creative or one per face, 裸眼3D, safe areas. Use any source, or infer from campaign photos (say so if you infer).
5. **Describe day and night appearance.** For each screen, describe in a few concrete sentences how it looks in daylight or morning and at night. Cover:
   - brightness relative to surroundings, glare or reflections, whether it washes out in sun;
   - shadows, sky visible behind, colour of surrounding signage;
   - typical traffic or crowd in frame, and anything in front of the screen.
   Point to the photo each observation comes from.
6. **List the camera positions** you can identify, with one representative full-resolution photo each (so we can pre-calibrate a template per position).
7. **Note occluders, roads and sidewalks** for each camera position: what's in front of the screen and where the traffic and pedestrian areas are in the frame (a description is enough; no pixel coordinates needed).
8. **List data problems** you find (inconsistencies, mismatched slugs, swapped files, missing photos) without fixing them.

### Rules

- **Keep evidence levels separate.** Mark every value as one of:
  - `confirmed`: stated in a source document; cite the path and slide or line.
  - `observed`: clearly visible in a named photo.
  - `estimated`: inferred or measured from photos; explain how.
  - `unknown`.
  Never present an estimate as a fact. Use `null` plus `"unknown"` rather than guessing.
- **Don't modify existing website files** (`city_resources.json`, `photo-review.json`, images, pages). Write your output as **new files only** (see section 6).
- Don't download from iCloud or the internet without asking. List what you couldn't read.
- Brands seen on screens are not automatically our clients. Don't add client claims.
- Where our estimates in section 2 conflict with what you find, say which is better supported and why.

---

## 6. Output

Create two new files in the website project:

1. `data/screen-profiles-2026-11.json`: an array of 11 objects in this shape (values below are illustrative, partly from our Mark Mall test):

```json
{
  "slug": "mark-mall",
  "slug_aliases": ["mark-mall"],
  "name": "Mark Mall（江汉路马客茂）",
  "official": {
    "pixels": [4800, 1600],
    "dimensions_m": [36.2, 11.8],
    "source": "sources/extracted/2026-10-19-commercial-deck/all-slides-text.txt slide 10 (2026-03)",
    "evidence": "confirmed"
  },
  "dimension_semantics": {
    "width_is": "unfolded_surface | straight_line | unknown",
    "pixels_are": "content_canvas | physical_led | unknown",
    "evidence": "estimated",
    "note": "Our calibration: faces sum to ≈35.5 m vs 36.2 m official, so width looks like the unfolded length"
  },
  "shape": {
    "type": "flat | curved | folded | rounded_corner | wrap_270 | irregular",
    "face_count": 2,
    "faces_left_to_right": [
      {"label": "江汉路侧（左）", "width_m": 14.0, "pixel_columns": [0, 2128], "facing": "unknown", "evidence": "estimated", "how": "Studio calibration of record-007-09 + layout of the ad on screen"},
      {"label": "corner", "kind": "rounded", "arc_length_m": 1.9, "radius_m": null, "evidence": "estimated"},
      {"label": "右", "width_m": 19.7, "pixel_columns": [2128, 4800], "evidence": "estimated"}
    ],
    "turn_angle_deg": {"value": 90, "evidence": "estimated", "how": "two façades meet at a right angle in drone photos"},
    "outline_irregularities": [],
    "cabinet_or_seam_spacing_m": {"value": null, "evidence": "unknown"}
  },
  "content_conventions": {
    "layout": "per_face_panels | continuous | unknown",
    "naked_eye_3d": false,
    "safe_area_notes": "",
    "evidence": "observed",
    "examples": ["site/dist/images/campaigns/record-007-01.webp"]
  },
  "appearance": {
    "day": {"description": "", "glare_or_reflection": "", "washout": "low|medium|high|unknown", "photos": []},
    "dusk": {"description": "", "photos": ["sources/extracted/2026-10-19-feishu-media/record-007-09.jpg"]},
    "night": {"description": "", "screen_vs_surroundings": "", "halo": "", "surrounding_signage": "", "photos": []},
    "operating_hours": {"value": "08:00-23:00", "evidence": "confirmed"}
  },
  "camera_positions": [
    {
      "id": "drone-ne-high",
      "description": "drone, east of the screen, roughly at screen height, both faces visible, left face nearly edge-on",
      "representative_photo": "sources/extracted/2026-10-19-feishu-media/record-007-09.jpg",
      "photo_local": true,
      "resolution": [6144, 4096],
      "time_of_day": "dusk",
      "occluders": ["none in front of screen"],
      "roads_and_sidewalks": "江汉路 pedestrian street below, crowds at bottom-left",
      "evidence": "observed"
    }
  ],
  "references": {
    "photos": [{"path": "", "local": true, "resolution": null, "time_of_day": "day|dusk|night", "camera": "drone|ground", "verified_screen": "mark-mall", "notes": ""}],
    "videos": [{"path": "", "local": true, "screen_visible": true, "camera_move": "", "notes": ""}]
  },
  "data_issues": [],
  "open_questions": []
}
```

2. `docs/screen-profiles-2026-11.md`: a readable summary with, for each screen:
   - a one-paragraph description;
   - a shape sketch in words;
   - a day vs night appearance comparison;
   - its camera positions with the best photo for each;
   - what's still unknown.

   Then end with a **consolidated list of questions for the screen operator / 德仁天下传媒**, for example: per-face widths, turn angles, corner radii, the per-face pixel layout of the content canvas, the 长江之星 dimensions, and what "宽" means.

When you're done, reply with:
- file paths;
- a table of the 11 screens with shape type, evidence level, and count of day / night / video references;
- the top data problems;
- anything you couldn't read because of iCloud.

---

## 7. How we'll use your output (so you can judge what matters)

- Studio will import `screen-profiles-2026-11.json`. Choosing a screen in the workbench will pre-fill screen type, `physical_size`, `segment_widths` (per-face widths), `turn_angle` and occluder hints.
- After calibration, Studio checks the result against the official dimensions and warns if it's more than about 5% off. That check alone would have caught our 1.5× Mark Mall error.
- Pixel columns per face let us place a client's native-resolution creative exactly, and warn when the creative's size doesn't match the screen.
- Day and night descriptions and reference photos become per-screen realism presets (white level, glow, reflection strength) and test cases.
- Camera positions become pre-built calibration templates, one per screen per position.

The most valuable fields, in order:
1. shape type and face count;
2. per-face widths and turn angle;
3. what width and pixels mean;
4. pixel columns per face;
5. a representative full-resolution day and night photo per camera position.

Accuracy matters more than completeness: an honest `unknown` is better than a plausible guess.

---

## 8. Handing results back

Your output goes back to the Studio side (Claude Code), which imports it into the tool. Please:

1. Write the two files from section 6 into the website project (`data/screen-profiles-2026-11.json` and `docs/screen-profiles-2026-11.md`). Don't edit anything in the Studio project.
2. Also create a hand-off folder `handoff/studio-2026-11/` in the website project containing:
   - `screen-profiles-2026-11.json` (same file);
   - `reference-photos.csv`, one row per reference photo or video: `slug, camera_position_id, path, local(yes/no), time_of_day, camera(drone/ground), width, height, faces_visible, screen_lit, verified_screen_source`. Studio will use the local full-resolution files on this list to pre-build calibration templates and per-screen day/night test cases. **List paths only; don't copy the image files.**
   - `questions-for-operator.md`, the consolidated question list for the screen operator.
3. If you find any screen spec document, LED cabinet or pixel-layout drawing, or content-production guideline (素材规格 / 制作规范) in the project, list its path in `reference-photos.csv` with `kind=spec_document` (or in a separate `spec-documents.csv`).
4. Reply with the file paths and the summary table described in section 6.

On the Studio side we will then:
- import the profiles;
- pre-fill each screen's shape, face widths and turn angle;
- calibrate one template per camera position from your reference photos;
- add the official-dimension check;
- build day/night presets per screen.

Anything still `unknown` goes on the operator question list.

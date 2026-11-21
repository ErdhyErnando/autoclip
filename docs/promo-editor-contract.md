# Promo editor — shared contract

Authoritative interface between the backend and frontend workstreams of the
promo-editor epic ([#16](https://github.com/ErdhyErnando/autoclip/issues/16)).
Both executors build against this document. Changing it requires the
reviewer's approval. Where an issue body and this document disagree, this
document wins.

## 1. Effective caption style

Resolution order, highest wins:

1. Per-clip `style_overrides` (JSON on `clip_edits`)
2. Base preset — the clip's `caption_style` key
3. Legacy global knobs `export.caption_size_scale` / `export.caption_position_ratio`
   (transition layer only — #10 removes it and seeds the default preset from
   current values, so nothing visually changes for an existing user)
4. `PRESETS[key]` defaults

Rules:

- Every numeric input is **clamped, never rejected** — a stale stored value
  must not break a render (the existing `resolve_style` philosophy).
- `resolve_style` becomes a pure function of (base key, overrides, legacy
  globals). The API exposes its per-clip result (`effective_caption`) so the
  UI never reimplements resolution.

### StyleOverrides schema

| field          | type                      | clamp / notes                                      |
|----------------|---------------------------|----------------------------------------------------|
| `font`         | str (registry key)        | must exist in the font registry, else HTTP 400     |
| `size_ratio`   | float                     | 0.02–0.12, fraction of frame height                |
| `primary`      | `"#RRGGBB"`               | text colour                                        |
| `accent`       | `"#RRGGBB"` or `null`     | `null` disables per-word highlight                 |
| `outline`      | `"#RRGGBB"`               |                                                    |
| `outline_width`| float                     | 0.0–8.0                                            |
| `all_caps`     | bool                      |                                                    |
| `animation`    | `"scale" \| "karaoke" \| "none"` |                                              |
| `max_words`    | int                       | 1–8, words per caption group — the pacing control  |
| `boxed`        | bool                      |                                                    |
| `box_colour`   | `"#RRGGBB"`               |                                                    |
| `box_alpha`    | int                       | 0–255, ASS alpha (0 = opaque)                      |

All fields optional; a missing field falls through to the layer below.

## 2. Caption geometry (per clip)

- `caption_margin_ratio` — float or null, clamp 0.0–0.8. Distance from the
  bottom of the frame as a fraction of frame height — same semantics as
  today's global `caption_position_ratio`. `null` = the base preset's default.
- `caption_x_ratio` — float or null, clamp 0.0–1.0. `null` = horizontally
  centered (today's behaviour).

### Horizontal alignment maths — both sides implement identically

Given `caption_x_ratio = x` and output width `W` (PlayRes X units):

- `x` is null (or snapped to 0.5) → `BOTTOM_CENTER`, no margins.
- `x < 0.5` → `BOTTOM_LEFT`, `MarginL = round(x * W)`.
- `x > 0.5` → `BOTTOM_RIGHT`, `MarginR = round((1 - x) * W)`.

The UI snaps to centered (`null`) when a drag is released within ±0.04 of
0.5. The caption is anchored by its **edge**, not its center, on both sides —
CSS mirror: `text-align: left/right/center` with inset padding of the same
fraction. Preview and burn-in agree by construction.

## 3. API additions

`PATCH /api/clips/{id}/captions` request gains:

- `style_overrides: StyleOverrides | null` — key present ⇒ replaces the stored
  blob wholesale; value `null` ⇒ clears it; key absent ⇒ unchanged.
- `caption_margin_ratio: float | null` — present ⇒ set (`null` clears to the
  preset default); absent ⇒ unchanged.
- `caption_x_ratio: float | null` — same rule.

`ClipOut` gains:

- `style_overrides: StyleOverrides | null`
- `caption_margin_ratio: float | null`
- `caption_x_ratio: float | null`
- `effective_caption` — the resolved snapshot for the preview to render
  directly: every field the `caption-styles` preview payload carries today,
  plus `alignment` (`"left" | "center" | "right"`) and `margin_ratio`.
  Populated by the same function the exporter uses, so drift is impossible.

## 4. Presets

Table `presets`: `id` (text PK), `name` (text, unique), `base_style` (text,
preset key), `style_overrides` (JSON, nullable), `layout` (JSON, nullable),
`is_default` (int, at most one row = 1), `created_at`.

`layout` JSON: `{ "caption_margin_ratio": float | null,
"caption_x_ratio": float | null }` — Phase 2 appends the `pip_*` placement
fields here, so a preset reproduces the whole on-screen layout (planning
decision: presets capture **look and layout**).

Endpoints:

- `GET /api/presets` — list, the default first.
- `POST /api/presets` — `{name, base_style, style_overrides?, layout?}` →
  201; duplicate name → 409; unknown base_style or font key → 400.
- `PUT /api/presets/{id}` — partial update of the same fields → 200.
- `PUT /api/presets/{id}/default` → 200; clears any other default row.
- `DELETE /api/presets/{id}` → 204. Deleting the default leaves none.
  Deleting never mutates clips — applied copies live on the clip edits.
- `POST /api/presets/{id}/apply` — `{clip_id}` → 200 with `ClipOut`.
  Server-side, atomic: sets `caption_style = base_style`, deep-copies
  `style_overrides`, and writes both layout fields onto the clip edit.

New clips: the runner applies the default preset (if one exists) when clips
are persisted after highlight detection — a fresh job's clips come out
on-brand with no client round-trip. No default ⇒ behaviour exactly as today.

## 5. Font registry

`captions.py` grows a registry: display name → file in
`backend/autoclip/assets/fonts/` (all SIL OFL, committed to git; runtime
never fetches). Baseline: Anton, Inter. #7 adds at least Archivo Black,
Bebas Neue, and one more display face (candidates: Lilita One, Passion One
Bold — static weights, not variable-font instances, so libass renders the
intended look). `CaptionStyle.font` must exist in the registry; the captions
PATCH and presets POST reject unknown font keys with 400.

Endpoint: `GET /api/fonts` → `[{"key": str, "label": str}]` listing the
registry — this drives the style editor's font dropdown.

## 6. Phase 2 — PiP placement and crop

Storage (on `clip_edits`):

- `pip_x_ratio`, `pip_y_ratio` — float or null, 0.0–1.0, the **center** of the
  inset in output space. Both null ⇒ fall back to legacy `pip_corner`
  mapping, so corner-era clips render identically.
- `pip_crop` — JSON `{x, y, width, height}` in **source pixels**, nullable.
  `null` ⇒ the detected `FacecamBox`.

`PATCH /api/clips/{id}/captions` and `ExportRequestIn` gain all three, with
the same present/absent merge rules as §3. `PipLayout` carries `x_ratio`,
`y_ratio`, `size_ratio` (existing, 0.2–0.6) and `crop`; `_pip_chain` positions
the inset center at `(x_ratio·W, y_ratio·H)`, clamped so the inset stays
fully inside the frame. The corner dict remains only to map legacy data.

Snap targets (UI): the four corners' inset-center positions computed from
`size_ratio` and the existing 2.2% margin; snap when a release lands within
6% of frame width/height of a target.

The crop marquee (#13) writes `pip_crop` from a rectangle drawn over the raw
source; `Reset to detected` writes `null`.

## 7. Phase 3 — watermark

`ExportSettings` gains: `watermark_text` (str, default `""`),
`watermark_png` (path under the AutoClip home, nullable), `watermark_corner`
(`"tl" | "tr" | "bl" | "br"`, default `"br"`), `watermark_scale` (fraction of
output width, 0.02–0.12, default 0.06), `watermark_opacity` (0.0–1.0,
default 0.9). `ExportRequestIn` gains `watermark: bool | null` — present ⇒
override; absent ⇒ on when configured, off when not. Text renders with a
bundled font via `drawtext`; PNG via `overlay`; composed **before** captions
so captions win z-order. SRT output unaffected.

## 8. Invariants — the reviewer gates on these

1. **The preview never lies.** `effective_caption` and the CSS approximation
   derive from the same resolution the exporter burns.
2. Migrations are nullable and additive; the reviewer's existing job
   (`852cc6fe…`, corner-era PiP data) must load and re-export unchanged.
3. No-override output is byte-identical to current `main` — a regression
   test asserts it.
4. Every stored value is clamped, never rejected — except unknown
   font/preset/style keys, which are a 400.
5. Tests stay hermetic (conftest isolates `AUTOCLIP_HOME`); no golden-media
   dependency in the default suite.

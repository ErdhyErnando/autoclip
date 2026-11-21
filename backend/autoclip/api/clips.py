"""Clip review, editing, preview media, and export."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from .. import paths
from ..config import load as load_settings
from ..db import store
from ..db.models import ClipEdit, Export, new_id
from ..pipeline import captions as captions_module
from ..pipeline import export as export_module
from ..pipeline.reframe.croppath import CropPath, centre_crop, shift_crop_path
from ..pipeline.reframe.facecam import FacecamBox, search_facecam
from ..pipeline.runner import JobWorkspace
from ..pipeline.transcript import Transcript, Word
from .schemas import (
    CaptionPatchIn,
    CaptionStyleOut,
    ClipOut,
    ClipPatchIn,
    ExportOut,
    ExportRequestIn,
    FacecamOut,
    WordOut,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["clips"])


def _clip_out(clip, settings=None) -> ClipOut:
    edit = store.get_clip_edit(clip.id)
    exports = store.list_exports(clip.id)
    return ClipOut.of(
        clip,
        edit=edit,
        exports=exports,
        effective_caption=_effective_caption(edit, settings or load_settings()),
    )


def _effective_caption(edit: ClipEdit | None, settings) -> dict[str, Any]:
    """The resolved caption snapshot for one clip (contract §1-§3).

    Resolved by the same function, from the same inputs, that the exporter
    calls. That shared call is the entire reason the review player can be
    trusted: preview and burn-in cannot describe different captions.
    """
    style = captions_module.resolve_style(
        edit.caption_style if edit else captions_module.DEFAULT_STYLE,
        overrides=edit.style_overrides if edit else None,
        size_scale=settings.export.caption_size_scale,
        position_ratio=settings.export.caption_position_ratio,
        caption_margin_ratio=edit.caption_margin_ratio if edit else None,
        caption_x_ratio=edit.caption_x_ratio if edit else None,
    )
    return captions_module.preview_payload(style)


async def list_clips_for_job(job_id: str) -> list[ClipOut]:
    job = await asyncio.to_thread(store.get_job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    clips = await asyncio.to_thread(store.list_clips, job_id)
    # One settings read for the whole page; resolution itself is pure.
    settings = await asyncio.to_thread(load_settings)
    return [await asyncio.to_thread(_clip_out, clip, settings) for clip in clips]


@router.get("/clips/{clip_id}", response_model=ClipOut)
async def get_clip(clip_id: str) -> ClipOut:
    clip = await asyncio.to_thread(store.get_clip, clip_id)
    if clip is None:
        raise HTTPException(status_code=404, detail="Clip not found.")
    return await asyncio.to_thread(_clip_out, clip)


@router.get("/clips/{clip_id}/words", response_model=list[WordOut])
async def clip_words(clip_id: str) -> list[WordOut]:
    """Words for a clip — the caption editor's source of truth.

    Returns the user's edited words when they exist, falling back to the
    transcript's originals.
    """
    clip = await asyncio.to_thread(store.get_clip, clip_id)
    if clip is None:
        raise HTTPException(status_code=404, detail="Clip not found.")

    edit = await asyncio.to_thread(store.get_clip_edit, clip_id)
    if edit is not None and edit.edited_words:
        return [WordOut(**word) for word in edit.edited_words]

    transcript = await asyncio.to_thread(_load_transcript, clip.job_id)
    words = transcript.slice(clip.start_word, clip.end_word)
    return [WordOut(text=w.text, start=w.start, end=w.end, speaker=w.speaker) for w in words]


@router.get("/clips/{clip_id}/crop-path")
async def clip_crop_path(clip_id: str) -> dict:
    """The computed crop path for a clip, so the preview can match the export.

    Without this the review player can only centre-crop, which on a tracked shot
    shows different framing from the file that gets rendered — sometimes with
    the speaker half out of frame. Reviewing framing against the wrong framing
    is worse than not previewing it at all.
    """
    clip = await asyncio.to_thread(store.get_clip, clip_id)
    if clip is None:
        raise HTTPException(status_code=404, detail="Clip not found.")

    cached = JobWorkspace(clip.job_id).crop_path(clip.id)
    if not cached.exists():
        # Audio-only sources and pre-reframe jobs legitimately have none; the
        # client falls back to a centre crop.
        raise HTTPException(status_code=404, detail="No crop path for this clip yet.")

    return (await asyncio.to_thread(CropPath.load, cached)).to_dict()


@router.get("/clips/{clip_id}/facecam", response_model=FacecamOut)
async def get_facecam(clip_id: str) -> FacecamOut:
    """The cached facecam box for a clip, if detection has run."""
    clip = await asyncio.to_thread(store.get_clip, clip_id)
    if clip is None:
        raise HTTPException(status_code=404, detail="Clip not found.")

    cached = JobWorkspace(clip.job_id).facecam(clip.id)
    if not cached.exists():
        raise HTTPException(
            status_code=404, detail="No facecam detected yet — run detection first."
        )

    return FacecamOut(**(await asyncio.to_thread(FacecamBox.load, cached)).to_dict())


@router.post("/clips/{clip_id}/facecam/detect", response_model=FacecamOut)
async def detect_facecam(clip_id: str) -> FacecamOut:
    """Search a clip's range for a persistent corner face and cache the box.

    Full-frame pass first, then close-up corner passes. Takes on the order of
    a minute of CPU per minute of clip; the result is cached per clip.
    """
    clip = await asyncio.to_thread(store.get_clip, clip_id)
    if clip is None:
        raise HTTPException(status_code=404, detail="Clip not found.")

    job = await asyncio.to_thread(store.get_job, clip.job_id)
    source = await asyncio.to_thread(store.get_source, job.source_id) if job else None
    if job is None or source is None:
        raise HTTPException(status_code=404, detail="The clip's source is missing.")

    box = await asyncio.to_thread(
        search_facecam,
        Path(source.path),
        source.width or 1920,
        source.height or 1080,
        start_s=clip.start_s,
        end_s=clip.end_s,
    )
    if box is None:
        raise HTTPException(
            status_code=404,
            detail="No persistent corner face found in this clip's range.",
        )

    workspace = JobWorkspace(clip.job_id)
    await asyncio.to_thread(box.save, workspace.facecam(clip.id))
    return FacecamOut(**box.to_dict())


@router.patch("/clips/{clip_id}", response_model=ClipOut)
async def patch_clip(clip_id: str, payload: ClipPatchIn) -> ClipOut:
    """Trim, retitle, or keep/discard a clip.

    A trim re-derives the word range from the new times, so captions stay in
    step with the boundaries without the client having to compute indices.
    """
    clip = await asyncio.to_thread(store.get_clip, clip_id)
    if clip is None:
        raise HTTPException(status_code=404, detail="Clip not found.")

    start_s = payload.start_s if payload.start_s is not None else clip.start_s
    end_s = payload.end_s if payload.end_s is not None else clip.end_s
    if end_s <= start_s:
        raise HTTPException(status_code=400, detail="End must come after start.")

    trimmed = payload.start_s is not None or payload.end_s is not None
    start_word = end_word = None

    if trimmed:
        transcript = await asyncio.to_thread(_load_transcript, clip.job_id)
        first, last = transcript.indices_in_range(start_s, end_s)
        start_word, end_word = first, max(first, last - 1)

    await asyncio.to_thread(
        store.update_clip,
        clip_id,
        start_s=start_s if trimmed else None,
        end_s=end_s if trimmed else None,
        start_word=start_word,
        end_word=end_word,
        title=payload.title,
        status=payload.status,
        user_trimmed=True if trimmed else None,
    )

    updated = await asyncio.to_thread(store.get_clip, clip_id)
    return await asyncio.to_thread(_clip_out, updated)


@router.patch("/clips/{clip_id}/captions", response_model=ClipOut)
async def patch_captions(clip_id: str, payload: CaptionPatchIn) -> ClipOut:
    """Store word-level caption edits, style choices, and caption geometry.

    Style fields merge the way the framing corrections do: a present key wins
    (an explicit null clears it), an absent key leaves the stored value alone.
    ``model_fields_set`` is what makes that distinction available — pydantic
    hands an omitted field its default, which is indistinguishable from a client
    sending null unless we ask which keys actually arrived.
    """
    clip = await asyncio.to_thread(store.get_clip, clip_id)
    if clip is None:
        raise HTTPException(status_code=404, detail="Clip not found.")

    if payload.caption_style is not None:
        try:
            captions_module.get_style(payload.caption_style)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    if payload.style_overrides is not None and payload.style_overrides.font is not None:
        try:
            captions_module.get_font(payload.style_overrides.font)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    existing = await asyncio.to_thread(store.get_clip_edit, clip_id)
    supplied = payload.model_fields_set

    edit = ClipEdit(
        clip_id=clip_id,
        edited_words=(
            [w.model_dump() for w in payload.words]
            if payload.words is not None
            else (existing.edited_words if existing else None)
        ),
        caption_style=payload.caption_style or (existing.caption_style if existing else "bold_pop"),
        ratio=payload.ratio or (existing.ratio if existing else "9:16"),
        crop_nudge=(
            payload.crop_nudge
            if payload.crop_nudge is not None
            else (existing.crop_nudge if existing else 0.0)
        ),
        centre_crop=(
            payload.centre_crop
            if payload.centre_crop is not None
            else (existing.centre_crop if existing else False)
        ),
        pip_enabled=(
            payload.pip_enabled
            if payload.pip_enabled is not None
            else (existing.pip_enabled if existing else False)
        ),
        pip_corner=payload.pip_corner or (existing.pip_corner if existing else "auto"),
        pip_size=(
            payload.pip_size
            if payload.pip_size is not None
            else (existing.pip_size if existing else 0.35)
        ),
        style_overrides=_merged_overrides(payload, existing, supplied),
        caption_margin_ratio=_merged_ratio(
            payload, existing, supplied, "caption_margin_ratio", captions_module.MARGIN_RATIO_RANGE
        ),
        caption_x_ratio=_merged_ratio(
            payload, existing, supplied, "caption_x_ratio", captions_module.X_RATIO_RANGE
        ),
    )
    await asyncio.to_thread(store.upsert_clip_edit, edit)

    return await asyncio.to_thread(_clip_out, clip)


@router.post("/clips/{clip_id}/export", response_model=ExportOut, status_code=201)
async def export_clip(clip_id: str, payload: ExportRequestIn) -> ExportOut:
    """Render one clip and return its download link."""
    clip = await asyncio.to_thread(store.get_clip, clip_id)
    if clip is None:
        raise HTTPException(status_code=404, detail="Clip not found.")

    job = await asyncio.to_thread(store.get_job, clip.job_id)
    source = await asyncio.to_thread(store.get_source, job.source_id) if job else None
    if job is None or source is None:
        raise HTTPException(status_code=404, detail="The clip's source is missing.")

    try:
        style = captions_module.get_style(payload.style)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    words = [
        Word(text=w.text, start=w.start, end=w.end, speaker=w.speaker)
        for w in await clip_words(clip_id)
    ]

    settings = load_settings()
    settings.export.write_srt = payload.write_srt

    workspace = JobWorkspace(clip.job_id)
    crop_path = await asyncio.to_thread(_crop_path_for, workspace, clip, source, payload.ratio)

    # Framing corrections: an explicit centre lock wins, otherwise the stored or
    # requested nudge shifts the tracked window. Both re-derive from the cached
    # path, so the correction never touches the stored reframe data.
    stored_edit = await asyncio.to_thread(store.get_clip_edit, clip_id)
    nudge = (
        payload.crop_nudge
        if payload.crop_nudge is not None
        else (stored_edit.crop_nudge if stored_edit else 0.0)
    )
    locked = (
        payload.centre_crop
        if payload.centre_crop is not None
        else (stored_edit.centre_crop if stored_edit else False)
    )
    if locked:
        aspect = {"9:16": (9, 16), "1:1": (1, 1), "16:9": (16, 9)}[payload.ratio]
        crop_path = centre_crop(
            source.width or 1920,
            source.height or 1080,
            clip.end_s - clip.start_s,
            aspect_w=aspect[0],
            aspect_h=aspect[1],
        )
    elif nudge:
        crop_path = shift_crop_path(crop_path, nudge)

    pip = _pip_for_export(workspace, clip, source, payload, stored_edit, payload.ratio)

    destination = _export_destination(clip, payload.ratio)

    request = export_module.ExportRequest(
        source=Path(source.path),
        destination=destination,
        start_s=clip.start_s,
        end_s=clip.end_s,
        crop_path=crop_path,
        words=words,
        style=style,
        ratio=payload.ratio,
        pip=pip,
    )

    try:
        await asyncio.to_thread(
            export_module.export_clip,
            request,
            work_dir=workspace.captions_dir,
            settings=settings.export,
        )
    except export_module.ExportError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    record = Export(
        id=new_id(),
        clip_id=clip_id,
        path=str(destination),
        ratio=payload.ratio,
        style=style.key,
        size_bytes=destination.stat().st_size,
    )
    await asyncio.to_thread(store.create_export, record)
    await asyncio.to_thread(store.update_clip, clip_id, status="exported")

    return ExportOut.of(record)


@router.get("/exports/{export_id}/download")
async def download_export(export_id: str) -> FileResponse:
    record = await asyncio.to_thread(store.get_export, export_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Export not found.")

    path = Path(record.path)
    if not path.exists():
        raise HTTPException(status_code=410, detail="The exported file has been moved or deleted.")

    return FileResponse(path, media_type="video/mp4", filename=path.name)


@router.get("/jobs/{job_id}/media")
async def job_media(job_id: str) -> FileResponse:
    """Serve the source video, so the review player can scrub the original.

    Range requests are handled by FileResponse, which is what makes seeking in
    the preview player usable on a long source.
    """
    job = await asyncio.to_thread(store.get_job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")

    source = await asyncio.to_thread(store.get_source, job.source_id)
    if source is None or not Path(source.path).exists():
        raise HTTPException(status_code=404, detail="Source media not found.")

    return FileResponse(Path(source.path))


@router.get("/caption-styles", response_model=list[CaptionStyleOut])
async def caption_styles() -> list[CaptionStyleOut]:
    """Style presets, with enough detail for the UI to approximate them in CSS.

    The preview carries the user's global size/position overrides, so what the
    review player draws matches what the renderer burns in. Per-clip styling is
    carried by ``ClipOut.effective_caption``, which resolves through the same
    function.
    """
    settings = load_settings()
    return [
        CaptionStyleOut(
            key=style.key,
            label=style.label,
            description=style.description,
            preview=captions_module.preview_payload(
                captions_module.resolve_style(
                    style.key,
                    size_scale=settings.export.caption_size_scale,
                    position_ratio=settings.export.caption_position_ratio,
                )
            ),
        )
        for style in captions_module.PRESETS.values()
    ]


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _merged_overrides(
    payload: CaptionPatchIn, existing: ClipEdit | None, supplied: set[str]
) -> dict[str, Any] | None:
    """Merge the style blob: present replaces wholesale, null clears, absent keeps."""
    if "style_overrides" not in supplied:
        return existing.style_overrides if existing else None
    if payload.style_overrides is None:
        return None

    # Only the keys the client actually sent, after clamping. An empty result is
    # stored as NULL: "no overrides" gets one representation, not two.
    clamped = captions_module.clamp_overrides(
        payload.style_overrides.model_dump(exclude_unset=True)
    )
    return clamped or None


def _merged_ratio(
    payload: CaptionPatchIn,
    existing: ClipEdit | None,
    supplied: set[str],
    name: str,
    bounds: tuple[float, float],
) -> float | None:
    """Merge one geometry ratio, clamping rather than rejecting (contract §8.4)."""
    if name not in supplied:
        return getattr(existing, name) if existing else None
    value = getattr(payload, name)
    return None if value is None else captions_module.clamp(value, bounds)


def _export_destination(clip, ratio: str) -> Path:
    """Where this clip's render lands, disambiguated against its siblings.

    Mirrors the pipeline's used-name logic (``pipeline/runner.py``): a title
    alone is not unique, an LLM is free to reuse one, and two clips writing the
    same filename means the second export silently clobbers the first while the
    first clip's ``Export`` row keeps pointing at the clobbered path.

    The seed for disambiguation is the job's *persisted* exports for *other*
    clips, not an in-memory set, because each API export stands alone — there is
    no batch to accumulate names across. That also gives us the semantics the
    endpoint needs: re-exporting the same clip sees only its siblings' names, so
    it keeps overwriting its own file.
    """
    owned: set[str] = set()
    for sibling in store.list_clips(clip.job_id):
        if sibling.id == clip.id:
            continue
        owned.update(Path(record.path).name for record in store.list_exports(sibling.id))

    title = clip.title or f"clip-{clip.rank}"
    filename = export_module.output_filename(title, ratio)
    # The rank is unique within a job. The loop closes the pathological case
    # where a rank-suffixed slug happens to equal another clip's plain slug.
    variant = clip.rank if filename in owned else None
    while filename in owned:
        variant = (variant or 0) + 1
        filename = export_module.output_filename(title, ratio, variant=variant)

    return paths.exports_dir() / clip.job_id / filename


def _load_transcript(job_id: str) -> Transcript:
    workspace = JobWorkspace(job_id)
    if not workspace.transcript.exists():
        raise HTTPException(status_code=404, detail="This job has no transcript yet.")
    return Transcript.load(workspace.transcript)


def _pip_for_export(workspace, clip, source, payload, stored_edit, ratio: str):
    """Resolve the PiP layout for an export, or None for a normal render.

    Payload values win when set; otherwise the clip's stored corrections apply.
    "auto" corner follows the detected facecam box.
    """
    enabled = (
        payload.pip_enabled
        if payload.pip_enabled is not None
        else (stored_edit.pip_enabled if stored_edit else False)
    )
    if not enabled:
        return None

    cached = workspace.facecam(clip.id)
    if not cached.exists():
        raise HTTPException(
            status_code=400,
            detail="Picture-in-picture is on but no facecam was detected — run detection first.",
        )
    box = FacecamBox.load(cached)

    corner = (
        payload.pip_corner
        if payload.pip_corner is not None
        else (stored_edit.pip_corner if stored_edit else "auto")
    )
    size = (
        payload.pip_size
        if payload.pip_size is not None
        else (stored_edit.pip_size if stored_edit else 0.35)
    )
    return export_module.PipLayout(
        x=box.x,
        y=box.y,
        width=box.width,
        height=box.height,
        corner=corner if corner != "auto" else box.corner,
        size_ratio=min(0.6, max(0.2, size)),
    )


def _crop_path_for(workspace: JobWorkspace, clip, source, ratio: str) -> CropPath:
    """Reuse the job's cached crop path, falling back to a centre crop.

    A re-export at a different ratio can't reuse a path computed for the
    original one, so the geometry is recomputed rather than stretched.
    """
    cached = workspace.crop_path(clip.id)
    if cached.exists() and ratio == "9:16":
        return CropPath.load(cached)

    aspect = {"9:16": (9, 16), "1:1": (1, 1), "16:9": (16, 9)}[ratio]
    return centre_crop(
        source.width or 1920,
        source.height or 1080,
        clip.end_s - clip.start_s,
        aspect_w=aspect[0],
        aspect_h=aspect[1],
    )

"""Reusable caption presets — CRUD, apply, and the default for new jobs.

A preset is a *template*, not a shared reference: applying one copies its values
onto a clip's edit row. That is what makes deleting a preset harmless — clips
that were already styled keep the look they were given — and it is why apply is a
single transaction over one clip rather than a join at render time.
"""

from __future__ import annotations

import asyncio
import sqlite3
from typing import Any

from fastapi import APIRouter, HTTPException

from ..db import store
from ..db.models import Preset, new_id
from ..pipeline import captions as captions_module
from .clips import clip_out
from .schemas import ClipOut, PresetApplyIn, PresetIn, PresetLayoutIn, PresetOut, PresetPatchIn

router = APIRouter(prefix="/api", tags=["presets"])


def _validate_style(base_style: str | None, overrides) -> None:
    """Reject an unknown preset key or font key with 400 (contract §8.4).

    Everything numeric is clamped; these two are the exceptions that must fail
    loudly, because silently substituting a preset or a font would render
    something the user never chose.
    """
    if base_style is not None:
        try:
            captions_module.get_style(base_style)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    font = overrides.font if overrides is not None else None
    if font is not None:
        try:
            captions_module.get_font(font)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc


def _clamped_overrides(overrides) -> dict[str, Any] | None:
    """Clamp the blob, keeping only the keys the client sent. Empty means none."""
    if overrides is None:
        return None
    return captions_module.clamp_overrides(overrides.model_dump(exclude_unset=True)) or None


def _clamped_layout(layout: PresetLayoutIn | None) -> dict[str, Any] | None:
    """Clamp the layout, always carrying both keys — the shape §4 specifies."""
    if layout is None:
        return None
    return {
        "caption_margin_ratio": (
            None
            if layout.caption_margin_ratio is None
            else captions_module.clamp(
                layout.caption_margin_ratio, captions_module.MARGIN_RATIO_RANGE
            )
        ),
        "caption_x_ratio": (
            None
            if layout.caption_x_ratio is None
            else captions_module.clamp(layout.caption_x_ratio, captions_module.X_RATIO_RANGE)
        ),
    }


def _cleaned_name(raw: str | None) -> str | None:
    """Trim the name, treating whitespace-only as absent.

    Presets are picked by name out of a list; ``"Bold  "`` and ``"Bold"`` being
    two different rows would look like a bug and would not collide to a user.
    """
    if raw is None:
        return None
    name = raw.strip()
    if not name:
        raise HTTPException(status_code=400, detail="A preset needs a name.")
    return name


@router.get("/presets", response_model=list[PresetOut])
async def list_presets() -> list[PresetOut]:
    """Every preset, the default first — the order the picker shows them in."""
    presets = await asyncio.to_thread(store.list_presets)
    return [PresetOut.of(preset) for preset in presets]


@router.post("/presets", response_model=PresetOut, status_code=201)
async def create_preset(payload: PresetIn) -> PresetOut:
    name = _cleaned_name(payload.name)
    _validate_style(payload.base_style, payload.style_overrides)

    preset = Preset(
        id=new_id(),
        name=name or "",
        base_style=payload.base_style,
        style_overrides=_clamped_overrides(payload.style_overrides),
        layout=_clamped_layout(payload.layout),
    )
    try:
        await asyncio.to_thread(store.create_preset, preset)
    except sqlite3.IntegrityError as exc:
        raise HTTPException(
            status_code=409, detail=f"A preset named {name!r} already exists."
        ) from exc

    return PresetOut.of(preset)


@router.put("/presets/{preset_id}", response_model=PresetOut)
async def update_preset(preset_id: str, payload: PresetPatchIn) -> PresetOut:
    """Patch a preset. Absent fields are left alone; ``null`` blobs are cleared."""
    existing = await asyncio.to_thread(store.get_preset, preset_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Preset not found.")

    supplied = payload.model_fields_set
    _validate_style(
        payload.base_style if "base_style" in supplied else None,
        payload.style_overrides,
    )

    fields: dict[str, Any] = {
        "name": _cleaned_name(payload.name) if "name" in supplied else None,
        "base_style": payload.base_style if "base_style" in supplied else None,
    }
    if "style_overrides" in supplied:
        fields["style_overrides"] = _clamped_overrides(payload.style_overrides)
    if "layout" in supplied:
        fields["layout"] = _clamped_layout(payload.layout)

    try:
        await asyncio.to_thread(store.update_preset, preset_id, **fields)
    except sqlite3.IntegrityError as exc:
        raise HTTPException(
            status_code=409, detail=f"A preset named {fields.get('name')!r} already exists."
        ) from exc

    updated = await asyncio.to_thread(store.get_preset, preset_id)
    if updated is None:  # pragma: no cover - deleted between the two calls
        raise HTTPException(status_code=404, detail="Preset not found.")
    return PresetOut.of(updated)


@router.put("/presets/{preset_id}/default", response_model=PresetOut)
async def set_default(preset_id: str) -> PresetOut:
    """Make one preset the default applied to new jobs; clears any other."""
    if await asyncio.to_thread(store.get_preset, preset_id) is None:
        raise HTTPException(status_code=404, detail="Preset not found.")

    await asyncio.to_thread(store.set_default_preset, preset_id)

    updated = await asyncio.to_thread(store.get_preset, preset_id)
    if updated is None:  # pragma: no cover - deleted between the two calls
        raise HTTPException(status_code=404, detail="Preset not found.")
    return PresetOut.of(updated)


@router.delete("/presets/{preset_id}", status_code=204)
async def delete_preset(preset_id: str) -> None:
    """Delete a preset. Clips keep the copies they were given, so none change.

    Deleting the default simply leaves no default; new jobs then behave exactly
    as they did before presets existed.
    """
    if not await asyncio.to_thread(store.delete_preset, preset_id):
        raise HTTPException(status_code=404, detail="Preset not found.")


@router.post("/presets/{preset_id}/apply", response_model=ClipOut)
async def apply_preset(preset_id: str, payload: PresetApplyIn) -> ClipOut:
    """Copy a preset onto one clip and return the clip as it now reads."""
    preset = await asyncio.to_thread(store.get_preset, preset_id)
    if preset is None:
        raise HTTPException(status_code=404, detail="Preset not found.")

    clip = await asyncio.to_thread(store.get_clip, payload.clip_id)
    if clip is None:
        raise HTTPException(status_code=404, detail="Clip not found.")

    await asyncio.to_thread(store.apply_preset_to_clips, [clip.id], preset)
    return await asyncio.to_thread(clip_out, clip)

"""Pydantic request and response models for the REST API.

Kept separate from the dataclasses in ``db.models``: those mirror SQLite rows,
these are the wire contract. Splitting them means a schema change doesn't
silently reshape the API, and the API can expose derived fields (durations,
export URLs) that don't belong in storage.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ..db import models
from ..pipeline.captions import COLOUR_PATTERN


class SourceOut(BaseModel):
    id: str
    type: str
    title: str
    url: str | None = None
    filename: str | None = None
    channel: str | None = None
    duration_s: float
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    has_audio: bool
    has_video: bool
    created_at: str

    @classmethod
    def of(cls, source: models.Source) -> SourceOut:
        # Built field by field rather than from vars(): the row carries an
        # absolute filesystem path that has no business leaving the process.
        return cls(
            id=source.id,
            type=source.type,
            title=source.title,
            url=source.url,
            filename=source.filename,
            channel=source.channel,
            duration_s=source.duration_s,
            width=source.width,
            height=source.height,
            fps=source.fps,
            has_audio=source.has_audio,
            has_video=source.has_video,
            created_at=source.created_at,
        )


class YouTubeIngestIn(BaseModel):
    url: str
    cookies_from_browser: str | None = None


class JobSettingsIn(BaseModel):
    """Per-job overrides. Anything omitted falls back to saved settings."""

    provider: str | None = None
    whisper_model: str | None = None
    language: str | None = None
    diarization: bool | None = None
    min_duration_s: float | None = Field(default=None, gt=0)
    max_duration_s: float | None = Field(default=None, gt=0)
    max_clips: int | None = Field(default=None, ge=1, le=50)
    caption_style: str | None = None
    ratio: Literal["9:16", "1:1", "16:9"] | None = None


class JobCreateIn(BaseModel):
    source_id: str
    settings: JobSettingsIn = Field(default_factory=JobSettingsIn)


class JobOut(BaseModel):
    id: str
    source_id: str
    status: str
    current_stage: str
    progress: float
    error: str | None = None
    provider: str
    created_at: str
    updated_at: str
    started_at: str | None = None
    finished_at: str | None = None
    source: SourceOut | None = None

    @classmethod
    def of(cls, job: models.Job, source: models.Source | None = None) -> JobOut:
        return cls(
            id=job.id,
            source_id=job.source_id,
            status=job.status,
            current_stage=job.current_stage,
            progress=job.progress,
            error=job.error,
            provider=job.provider,
            created_at=job.created_at,
            updated_at=job.updated_at,
            started_at=job.started_at,
            finished_at=job.finished_at,
            source=SourceOut.of(source) if source else None,
        )


class WordOut(BaseModel):
    text: str
    start: float
    end: float
    speaker: str | None = None


class ExportOut(BaseModel):
    id: str
    clip_id: str
    ratio: str
    style: str
    size_bytes: int
    created_at: str
    download_url: str

    @classmethod
    def of(cls, export: models.Export) -> ExportOut:
        return cls(
            id=export.id,
            clip_id=export.clip_id,
            ratio=export.ratio,
            style=export.style,
            size_bytes=export.size_bytes,
            created_at=export.created_at,
            download_url=f"/api/exports/{export.id}/download",
        )


class StyleOverrides(BaseModel):
    """Per-clip caption styling, layered over the base preset (contract §1).

    Every field is optional and a missing field falls through to the layer
    below. Numeric inputs carry no range constraint here on purpose: the
    contract says clamp them, so out-of-range values are pulled into range by
    ``captions.clamp_overrides`` rather than rejected.

    ``extra="forbid"`` is the one strictness that pays for itself — a typo'd
    field name would otherwise be dropped silently and the user would see no
    change from a setting the UI said it saved.
    """

    model_config = ConfigDict(extra="forbid")

    font: str | None = None
    size_ratio: float | None = None
    primary: str | None = Field(default=None, pattern=COLOUR_PATTERN)
    #: None disables per-word highlighting; an absent key keeps the preset's.
    accent: str | None = Field(default=None, pattern=COLOUR_PATTERN)
    outline: str | None = Field(default=None, pattern=COLOUR_PATTERN)
    outline_width: float | None = None
    all_caps: bool | None = None
    animation: Literal["scale", "karaoke", "none"] | None = None
    max_words: int | None = None
    boxed: bool | None = None
    box_colour: str | None = Field(default=None, pattern=COLOUR_PATTERN)
    box_alpha: int | None = None


class ClipOut(BaseModel):
    id: str
    job_id: str
    rank: int
    start_s: float
    end_s: float
    duration_s: float
    start_word: int
    end_word: int
    title: str
    hook: str
    score: int
    reason: str
    status: str
    user_trimmed: bool
    caption_style: str = "bold_pop"
    ratio: str = "9:16"
    crop_nudge: float = 0.0
    centre_crop: bool = False
    pip_enabled: bool = False
    pip_corner: str = "auto"
    pip_size: float = 0.35
    #: A plain dict on the way out so a blob written by a newer build still
    #: serialises; ``StyleOverrides`` is the input model, which is where
    #: strictness belongs. The JSON shape is identical either way.
    style_overrides: dict[str, Any] | None = None
    caption_margin_ratio: float | None = None
    caption_x_ratio: float | None = None
    #: The resolved caption snapshot — the same resolution the exporter burns.
    effective_caption: dict[str, Any] | None = None
    exports: list[ExportOut] = Field(default_factory=list)

    @classmethod
    def of(
        cls,
        clip: models.Clip,
        *,
        edit: models.ClipEdit | None = None,
        exports: list[models.Export] | None = None,
        effective_caption: dict[str, Any] | None = None,
    ) -> ClipOut:
        return cls(
            id=clip.id,
            job_id=clip.job_id,
            rank=clip.rank,
            start_s=clip.start_s,
            end_s=clip.end_s,
            duration_s=clip.duration_s,
            start_word=clip.start_word,
            end_word=clip.end_word,
            title=clip.title,
            hook=clip.hook,
            score=clip.score,
            reason=clip.reason,
            status=clip.status,
            user_trimmed=clip.user_trimmed,
            caption_style=edit.caption_style if edit else "bold_pop",
            ratio=edit.ratio if edit else "9:16",
            crop_nudge=edit.crop_nudge if edit else 0.0,
            centre_crop=edit.centre_crop if edit else False,
            pip_enabled=edit.pip_enabled if edit else False,
            pip_corner=edit.pip_corner if edit else "auto",
            pip_size=edit.pip_size if edit else 0.35,
            style_overrides=edit.style_overrides if edit else None,
            caption_margin_ratio=edit.caption_margin_ratio if edit else None,
            caption_x_ratio=edit.caption_x_ratio if edit else None,
            effective_caption=effective_caption,
            exports=[ExportOut.of(e) for e in (exports or [])],
        )


class ClipPatchIn(BaseModel):
    start_s: float | None = Field(default=None, ge=0)
    end_s: float | None = Field(default=None, gt=0)
    title: str | None = None
    status: Literal["candidate", "kept", "discarded", "exported"] | None = None


class CaptionPatchIn(BaseModel):
    """Word-level caption edits for one clip."""

    words: list[WordOut] | None = None
    caption_style: str | None = None
    ratio: Literal["9:16", "1:1", "16:9"] | None = None
    #: -1 (full left) to +1 (full right); None leaves the stored value alone.
    crop_nudge: float | None = Field(default=None, ge=-1.0, le=1.0)
    centre_crop: bool | None = None
    pip_enabled: bool | None = None
    pip_corner: Literal["auto", "tl", "tr", "bl", "br"] | None = None
    pip_size: float | None = Field(default=None, ge=0.2, le=0.6)
    #: Per-clip style. Key present ⇒ replaces the stored blob wholesale; value
    #: null ⇒ clears it; key absent ⇒ unchanged (contract §3).
    style_overrides: StyleOverrides | None = None
    #: Clamped, not rejected, so a stale client can't wedge the editor
    #: (contract §8.4). Present ⇒ set, explicit null ⇒ back to the default,
    #: absent ⇒ unchanged.
    caption_margin_ratio: float | None = None
    caption_x_ratio: float | None = None


class ExportRequestIn(BaseModel):
    ratio: Literal["9:16", "1:1", "16:9"] = "9:16"
    style: str = "bold_pop"
    write_srt: bool = False
    #: Overrides the clip's stored nudge when set; otherwise the stored value wins.
    crop_nudge: float | None = Field(default=None, ge=-1.0, le=1.0)
    centre_crop: bool | None = None
    pip_enabled: bool | None = None
    pip_corner: Literal["auto", "tl", "tr", "bl", "br"] | None = None
    pip_size: float | None = Field(default=None, ge=0.2, le=0.6)


class FacecamOut(BaseModel):
    x: float
    y: float
    width: float
    height: float
    corner: str
    coverage: float


class CaptionStyleOut(BaseModel):
    key: str
    label: str
    description: str
    #: Enough for the UI to approximate the look in HTML/CSS before rendering.
    preview: dict[str, Any]


class ProviderStatusOut(BaseModel):
    name: str
    available: bool
    detail: str = ""
    models: list[str] = Field(default_factory=list)
    requires_key: bool = True
    has_key: bool = False


class SettingsOut(BaseModel):
    active_provider: str
    providers: dict[str, dict[str, Any]]
    whisper: dict[str, Any]
    clips: dict[str, Any]
    ingest: dict[str, Any]
    export: dict[str, Any]
    insecure_secret_storage: bool
    #: Which providers have a key stored. The keys themselves never leave the
    #: keyring, so the UI shows presence, not value.
    keys_present: dict[str, bool] = Field(default_factory=dict)


class SettingsIn(BaseModel):
    active_provider: str | None = None
    providers: dict[str, dict[str, Any]] | None = None
    whisper: dict[str, Any] | None = None
    clips: dict[str, Any] | None = None
    ingest: dict[str, Any] | None = None
    export: dict[str, Any] | None = None


class SecretIn(BaseModel):
    key: str
    value: str


class SystemOut(BaseModel):
    ready: bool
    python_version: str
    platform: str
    ffmpeg_version: str | None
    has_libass: bool
    nvenc_works: bool
    accel: str
    gpu_name: str | None
    compute_type: str
    diarization_available: bool

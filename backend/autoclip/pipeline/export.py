"""Export stage — crop path plus captions plus audio normalisation to an MP4.

The whole render is a single ffmpeg pass. Per-shot crop segments are trimmed out
of the source, cropped independently, scaled to a common output size, and
concatenated in the filtergraph — which is what allows a wide two-shot and a
tight single to sit in one clip without a mid-stream frame-size change.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config import ExportSettings
from ..system import report
from . import captions as captions_module
from . import ffmpeg
from .captions import CaptionStyle
from .reframe.croppath import CropPath, segment_crop_filter
from .transcript import Word

log = logging.getLogger(__name__)

#: Output dimensions per aspect ratio. 1080-wide is the platform sweet spot:
#: high enough to avoid re-encode mush, low enough to upload quickly.
RATIOS: dict[str, tuple[int, int]] = {
    "9:16": (1080, 1920),
    "1:1": (1080, 1080),
    "16:9": (1920, 1080),
}

#: Loudness targets. -14 LUFS integrated with -1.5 dBTP is what every major
#: platform normalises toward, so hitting it ourselves avoids their processing.
LOUDNESS_TRUE_PEAK = -1.5
LOUDNESS_RANGE = 11.0

AUDIO_BITRATE = "192k"
AUDIO_SAMPLE_RATE = 48_000


class ExportError(RuntimeError):
    """Rendering a clip failed."""


@dataclass
class PipLayout:
    """Picture-in-picture: full-screen background plus a facecam inset.

    The box is source pixels (from facecam detection); the corner picks the
    inset position and ``size_ratio`` its width as a fraction of output width.
    """

    x: float
    y: float
    width: float
    height: float
    corner: str = "tr"
    size_ratio: float = 0.35


@dataclass
class ExportRequest:
    source: Path
    destination: Path
    start_s: float
    end_s: float
    crop_path: CropPath
    words: list[Word]
    style: CaptionStyle
    ratio: str = "9:16"
    burn_captions: bool = True
    pip: PipLayout | None = None
    #: The clip's own styling, layered over ``style`` at render time. Kept apart
    #: from ``style`` rather than pre-merged because the legacy global knobs live
    #: in settings, and that layer has to be applied in one place — here.
    style_overrides: dict[str, Any] | None = None
    caption_margin_ratio: float | None = None
    caption_x_ratio: float | None = None

    @property
    def duration_s(self) -> float:
        return self.end_s - self.start_s


def ratio_dimensions(ratio: str) -> tuple[int, int]:
    if ratio not in RATIOS:
        raise ExportError(f"Unknown ratio {ratio!r}. Available: {', '.join(RATIOS)}")
    return RATIOS[ratio]


def slugify_title(title: str, *, max_length: int = 50) -> str:
    """Filename-safe slug for a clip title."""
    slug = re.sub(r"[^\w\s-]", "", title, flags=re.UNICODE).strip().lower()
    slug = re.sub(r"[\s_-]+", "-", slug).strip("-")
    return slug[:max_length] or "clip"


def output_filename(title: str, ratio: str, *, variant: int | None = None) -> str:
    """The export filename for a clip title.

    ``variant`` disambiguates clips that share a title — the rank, in practice,
    which is unique per job. Without it two same-titled clips write one file
    and the second export silently clobbers the first.
    """
    slug = slugify_title(title)
    if variant is not None:
        slug = f"{slug}-{variant}"
    return f"{slug}_{ratio.replace(':', 'x')}.mp4"


# --------------------------------------------------------------------------
# Filtergraph construction
# --------------------------------------------------------------------------


def build_video_filtergraph(
    request: ExportRequest,
    *,
    subtitle_name: str | None,
    fonts_name: str = "fonts",
) -> str:
    """Build the ``-filter_complex`` video chain for one clip."""
    out_w, out_h = ratio_dimensions(request.ratio)

    if request.pip is not None:
        parts = _pip_chain(request.pip, out_w, out_h)
        current = "[pip]"
    else:
        parts, current = _crop_chain(request, out_w, out_h)

    if request.burn_captions and subtitle_name is not None:
        # Bare relative names — ffmpeg runs with its cwd set to the render
        # workspace, so there is nothing here that needs escaping.
        parts.append(f"{current}ass=filename={subtitle_name}:fontsdir={fonts_name}[vout]")
    else:
        parts.append(f"{current}null[vout]")

    return ";".join(parts)


def _pip_chain(pip: PipLayout, out_w: int, out_h: int) -> list[str]:
    """Full-screen background plus a facecam inset.

    The background fits the whole source (blurred fill, like a too-wide shot)
    so no screen content is cropped away; the inset is the detected facecam box
    scaled to ``size_ratio`` of the output width in the chosen corner.
    """
    inset_w = max(2, round(out_w * pip.size_ratio) // 2 * 2)
    inset_h = max(2, round(inset_w * pip.height / max(1.0, pip.width)) // 2 * 2)
    margin = round(out_w * 0.022)
    corners = {
        "tl": (margin, margin),
        "tr": (out_w - inset_w - margin, margin),
        "bl": (margin, out_h - inset_h - margin),
        "br": (out_w - inset_w - margin, out_h - inset_h - margin),
    }
    x, y = corners.get(pip.corner, corners["tr"])

    face = (
        f"[0:v]crop=w={round(pip.width)}:h={round(pip.height)}"
        f":x={round(pip.x)}:y={round(pip.y)},"
        f"scale={inset_w}:{inset_h}:flags=lanczos,setsar=1[face]"
    )
    return [
        *_fit_chain("[0:v]", 9, out_w, out_h),
        face,
        f"[v9][face]overlay={x}:{y},setsar=1,format=yuv420p[pip]",
    ]


def _crop_chain(request: ExportRequest, out_w: int, out_h: int) -> tuple[list[str], str]:
    """The standard tracked-crop chain; returns (parts, current label)."""
    segments = request.crop_path.segments
    if not segments:
        raise ExportError("The crop path has no segments.")

    parts: list[str] = []
    labels: list[str] = []

    for index, segment in enumerate(segments):
        label = f"v{index}"
        labels.append(f"[{label}]")

        if len(segments) == 1:
            # No trim needed; the input is already seeked to the clip.
            source_label = "[0:v]"
            prefix = ""
        else:
            source_label = f"[s{index}]"
            prefix = (
                f"[0:v]trim=start={segment.start_s:.4f}:end={segment.end_s:.4f},"
                f"setpts=PTS-STARTPTS{source_label}"
            )
            parts.append(prefix)

        if segment.fit:
            parts.extend(_fit_chain(source_label, index, out_w, out_h))
            continue

        chain = [segment_crop_filter(segment)]
        if segment.zoom > 0:
            chain.append(_zoom_filter(segment.zoom, out_w, out_h))
        chain.append(f"scale={out_w}:{out_h}:flags=lanczos")
        chain.append("setsar=1,format=yuv420p")

        parts.append(f"{source_label}{','.join(chain)}[{label}]")

    if len(segments) == 1:
        current = "[v0]"
    else:
        parts.append(f"{''.join(labels)}concat=n={len(segments)}:v=1:a=0[vcat]")
        current = "[vcat]"

    return parts, current


def _fit_chain(source_label: str, index: int, out_w: int, out_h: int) -> list[str]:
    """Fit the whole frame into the output over a blurred copy of itself.

    Used when the subjects are spread wider than any crop can hold. Plain black
    bars would be honest but look cheap on a phone; a blurred fill is what
    viewers are used to seeing and keeps the frame full-bleed.
    """
    return [
        f"{source_label}split=2[fitbg{index}][fitfg{index}]",
        (
            f"[fitbg{index}]scale={out_w}:{out_h}:force_original_aspect_ratio=increase,"
            f"crop={out_w}:{out_h},boxblur=24:2[bg{index}]"
        ),
        (
            f"[fitfg{index}]scale={out_w}:{out_h}:force_original_aspect_ratio=decrease:"
            f"flags=lanczos[fg{index}]"
        ),
        (f"[bg{index}][fg{index}]overlay=(W-w)/2:(H-h)/2,setsar=1,format=yuv420p[v{index}]"),
    ]


def _zoom_filter(zoom: float, out_w: int, out_h: int) -> str:
    """A slow push-in over the segment.

    Uses ``zoompan``, which is the only filter that can change apparent scale
    while holding a fixed output size. Off by default — at very small
    increments it can step visibly, and a locked frame beats a stuttering one.
    """
    end_zoom = 1.0 + zoom
    return f"zoompan=z='min(zoom+{zoom / 240:.6f},{end_zoom:.4f})':d=1:s={out_w}x{out_h}:fps=30"


def build_audio_filtergraph(settings: ExportSettings) -> str:
    return f"loudnorm=I={settings.loudness_lufs}:TP={LOUDNESS_TRUE_PEAK}:LRA={LOUDNESS_RANGE}"


def encoder_args(settings: ExportSettings) -> list[str]:
    """Pick the video encoder and its quality settings.

    NVENC is materially faster where available. Its quality knob is ``-cq``
    rather than ``-crf``, and the two scales are close enough that reusing the
    configured value keeps output consistent between machines.
    """
    if settings.prefer_hardware_encoder and report().ffmpeg.nvenc_works:
        return [
            "-c:v",
            "h264_nvenc",
            "-preset",
            "p5",
            "-rc",
            "vbr",
            "-cq",
            str(settings.crf),
            "-b:v",
            "0",
            "-profile:v",
            "high",
        ]
    return [
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        str(settings.crf),
        "-profile:v",
        "high",
    ]


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


def export_clip(
    request: ExportRequest,
    *,
    work_dir: Path,
    settings: ExportSettings | None = None,
    on_progress: Callable[[float], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> Path:
    """Render one clip to an MP4 and return the output path.

    Preconditions:
        request.source exists; request.crop_path segments span the clip duration
        in clip-relative seconds.
    """
    settings = settings or ExportSettings()
    out_w, out_h = ratio_dimensions(request.ratio)

    # The request carries the preset and the clip's own styling; the user's
    # global knobs live in settings so one change applies to pipeline exports and
    # re-exports alike. All of it resolves through the one function the API's
    # ``effective_caption`` also calls, so preview and burn-in cannot drift.
    style = captions_module.resolve_style(
        request.style.key,
        size_scale=settings.caption_size_scale,
        position_ratio=settings.caption_position_ratio,
        overrides=request.style_overrides,
        caption_margin_ratio=request.caption_margin_ratio,
        caption_x_ratio=request.caption_x_ratio,
    )

    # Each clip renders in its own workspace so concurrent exports can't collide
    # on the shared `captions.ass` name that the relative-path scheme requires.
    workspace = work_dir / request.destination.stem
    workspace.mkdir(parents=True, exist_ok=True)

    subtitle_name: str | None = None
    fonts_name = "fonts"
    render_cwd: Path | None = None

    if request.burn_captions and request.words:
        ass_path = captions_module.write_ass(
            workspace / "captions.ass",
            request.words,
            style,
            width=out_w,
            height=out_h,
            time_offset_s=request.start_s,
        )
        render_cwd, subtitle_name, fonts_name = ffmpeg.relative_filter_workspace(
            ass_path, captions_module.FONT_DIR
        )

    filtergraph = build_video_filtergraph(
        request, subtitle_name=subtitle_name, fonts_name=fonts_name
    )

    request.destination.parent.mkdir(parents=True, exist_ok=True)

    # Input and output paths are ordinary argv arguments, so they stay absolute
    # and need no escaping regardless of the working directory.
    args = [
        # Seeking before -i decodes from the nearest keyframe and is far faster
        # than an output-side seek; modern ffmpeg keeps it frame-accurate.
        "-ss",
        f"{request.start_s:.4f}",
        "-t",
        f"{request.duration_s:.4f}",
        "-i",
        str(request.source),
        "-filter_complex",
        filtergraph,
        "-map",
        "[vout]",
        "-map",
        "0:a?",
        "-af",
        build_audio_filtergraph(settings),
        *encoder_args(settings),
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        AUDIO_BITRATE,
        "-ar",
        str(AUDIO_SAMPLE_RATE),
        "-movflags",
        "+faststart",
        str(request.destination),
    ]

    try:
        ffmpeg.run(
            args,
            total_duration_s=request.duration_s,
            on_progress=on_progress,
            cancelled=cancelled,
            cwd=render_cwd,
        )
    except ffmpeg.FFmpegError as exc:
        raise ExportError(f"Could not render {request.destination.name}.\n{exc}") from exc

    if not request.destination.exists() or request.destination.stat().st_size == 0:
        raise ExportError(f"ffmpeg reported success but {request.destination.name} is empty.")

    if settings.write_srt and request.words:
        captions_module.write_srt(
            request.destination.with_suffix(".srt"),
            request.words,
            time_offset_s=request.start_s,
        )

    log.info(
        "Exported %s (%.1fs, %s)",
        request.destination.name,
        request.duration_s,
        request.ratio,
    )
    return request.destination

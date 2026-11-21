"""Caption generation — word timings to burned-in ASS subtitles.

Word grouping decides readability; ASS styling decides whether it looks like a
real short or like a subtitle track. Four presets cover the range from
attention-grabbing to professional.

Per-word animation (Bold Pop, Boxed) is done by emitting one Dialogue event per
word, each showing the whole caption group with the active word overridden.
That's how libass does word highlighting — there's no per-glyph timeline — and
it's why event counts look high relative to word counts.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import pysubs2

from .transcript import Word

log = logging.getLogger(__name__)

FONT_DIR = Path(__file__).resolve().parent.parent / "assets" / "fonts"

#: ``#RRGGBB``. Shared with the API schemas so what the request layer accepts and
#: what the resolver is willing to store can never disagree.
COLOUR_PATTERN = r"^#[0-9A-Fa-f]{6}$"
_COLOUR_RE = re.compile(COLOUR_PATTERN)


@dataclass(frozen=True)
class FontSpec:
    """One font bundled with the app.

    ``key`` is the ASS family name, and is also the key the API's ``font`` field
    carries. libass resolves a family from the TTF, so introducing a separate
    registry key would only add a mapping that can drift from the file it points
    at.
    """

    key: str
    label: str
    file: str

    @property
    def path(self) -> Path:
        return FONT_DIR / self.file


#: Every font the renderer may name (contract §5). All SIL OFL, committed to
#: git — nothing is fetched at runtime, so exporting works offline.
FONTS: dict[str, FontSpec] = {
    "Anton": FontSpec(key="Anton", label="Anton", file="Anton-Regular.ttf"),
    "Inter": FontSpec(key="Inter", label="Inter", file="Inter-Variable.ttf"),
}


def get_font(key: str) -> FontSpec:
    spec = FONTS.get(key)
    if spec is None:
        raise ValueError(f"Unknown font {key!r}. Available: {', '.join(FONTS)}")
    return spec

#: Words are grouped into caption events; more than this is hard to read at a
#: glance on a phone.
DEFAULT_MAX_WORDS = 4
#: A pause longer than this ends a caption group regardless of word count.
DEFAULT_MAX_GAP_S = 0.4

#: Reference canvas. Styles are authored against this and scaled to the real
#: output size, so a preset looks identical at 9:16, 1:1, and 16:9.
REFERENCE_HEIGHT = 1920

#: Guard rails for per-clip inputs (contract §1-§2). Values outside these are
#: clamped, never rejected: a stored override written by another build must not
#: be able to break a render.
SIZE_RATIO_RANGE = (0.02, 0.12)
OUTLINE_WIDTH_RANGE = (0.0, 8.0)
MAX_WORDS_RANGE = (1, 8)
BOX_ALPHA_RANGE = (0, 255)
MARGIN_RATIO_RANGE = (0.0, 0.8)
X_RATIO_RANGE = (0.0, 1.0)
#: A position within this of centre counts as centred. The review UI applies the
#: same tolerance before it sends, so a drag that lands "centred enough" renders
#: centred instead of left-aligned against a hair-thin margin.
X_SNAP_TOLERANCE = 0.04

#: The animation modes a style may name.
ANIMATIONS = ("scale", "karaoke", "none")


def clamp(value: float, bounds: tuple[float, float]) -> float:
    low, high = bounds
    return min(high, max(low, value))


def _number(value: Any) -> float | None:
    """Read an override value as a float, or None if it isn't one.

    ``bool`` is excluded on purpose: JSON booleans are ints in Python, and a
    ``true`` in a numeric field is a mistake rather than a size of one.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _colour(value: Any) -> str | None:
    return value if isinstance(value, str) and _COLOUR_RE.match(value) else None


def clamp_overrides(raw: Mapping[str, Any] | None) -> dict[str, Any]:
    """Clamp a style-override blob to the ranges in contract §1.

    Used on write, so what lands in the database is always in range, and again
    on read, so a blob written by another build still renders. Unknown keys and
    unparseable values are dropped rather than raised: the API turns bad input
    into a 400, but resolution must never fail part-way through a render.
    """
    if not raw:
        return {}

    out: dict[str, Any] = {}

    for name, bounds, whole in (
        ("size_ratio", SIZE_RATIO_RANGE, False),
        ("outline_width", OUTLINE_WIDTH_RANGE, False),
        ("box_alpha", BOX_ALPHA_RANGE, True),
        ("max_words", MAX_WORDS_RANGE, True),
    ):
        number = _number(raw.get(name))
        if number is not None:
            bounded = clamp(number, bounds)
            out[name] = int(bounded) if whole else bounded

    if raw.get("font") in FONTS:
        out["font"] = raw["font"]

    for name in ("primary", "outline", "box_colour"):
        colour = _colour(raw.get(name))
        if colour is not None:
            out[name] = colour

    # ``accent`` accepts an explicit null: that is how per-word highlighting gets
    # switched off, and it has to stay distinguishable from "leave the preset
    # alone", which is an absent key.
    if "accent" in raw:
        accent = raw["accent"]
        colour = _colour(accent)
        if colour is not None:
            out["accent"] = colour
        elif accent is None:
            out["accent"] = None

    for name in ("all_caps", "boxed"):
        if isinstance(raw.get(name), bool):
            out[name] = raw[name]

    if raw.get("animation") in ANIMATIONS:
        out["animation"] = raw["animation"]

    return out


def hex_to_ass(colour: str, alpha: int = 0) -> pysubs2.Color:
    """Convert ``#RRGGBB`` to a pysubs2 colour.

    ASS stores colours as ``&HAABBGGRR`` — alpha first, then reversed RGB.
    pysubs2 handles the byte order; we only translate from hex.
    """
    r, g, b = _rgb(colour)
    return pysubs2.Color(r, g, b, alpha)


def _rgb(colour: str) -> tuple[int, int, int]:
    value = colour.lstrip("#")
    if len(value) != 6:
        raise ValueError(f"Expected #RRGGBB, got {colour!r}")
    return tuple(int(value[i : i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def ass_colour_override(colour: str) -> str:
    r"""Format ``#RRGGBB`` as an inline ASS colour value, e.g. ``&H00E5FF&``.

    Inline ``\c`` overrides take the raw ``&HBBGGRR&`` form — reversed byte
    order and no alpha component, unlike the style-level colour fields.
    """
    r, g, b = _rgb(colour)
    return f"&H{b:02X}{g:02X}{r:02X}&"


@dataclass
class CaptionStyle:
    """A caption preset."""

    key: str
    label: str
    description: str

    font: str = "Anton"
    #: Font size as a fraction of frame height, so presets scale across ratios.
    size_ratio: float = 0.058
    primary: str = "#FFFFFF"
    #: Colour of the currently-spoken word. None disables per-word highlighting.
    accent: str | None = "#FFE500"
    outline: str = "#000000"
    outline_width: float = 4.0
    shadow: float = 0.0
    bold: bool = True
    all_caps: bool = True
    #: Distance from the bottom of the frame, as a fraction of height.
    margin_v_ratio: float = 0.24
    max_words: int = DEFAULT_MAX_WORDS
    #: "none" | "scale" | "karaoke"
    animation: str = "scale"
    #: Percentage the active word grows to. Only used when animation is "scale".
    scale_percent: int = 118
    #: Opaque background box behind the text (ASS BorderStyle 3).
    boxed: bool = False
    box_colour: str = "#000000"
    box_alpha: int = 40
    #: Horizontal anchor, 0 at the left edge and 1 at the right. None is
    #: horizontally centred, which is all that existed before x-axis placement.
    x_ratio: float | None = None

    @property
    def font_path(self) -> Path:
        return get_font(self.font).path

    @property
    def alignment(self) -> str:
        """Which edge the caption block is anchored to (contract §2).

        Derived rather than stored so the preview payload and the ASS style can
        never disagree about it: both read this.
        """
        if self.x_ratio is None:
            return "center"
        return "left" if self.x_ratio < 0.5 else "right"


PRESETS: dict[str, CaptionStyle] = {
    "bold_pop": CaptionStyle(
        key="bold_pop",
        label="Bold Pop",
        description="Chunky white with a heavy outline; the spoken word grows and turns yellow.",
        font="Anton",
        size_ratio=0.062,
        primary="#FFFFFF",
        accent="#FFE500",
        outline_width=5.0,
        all_caps=True,
        animation="scale",
        scale_percent=118,
        max_words=4,
    ),
    "karaoke_fill": CaptionStyle(
        key="karaoke_fill",
        label="Karaoke Fill",
        description="Words fill with colour exactly as they're spoken.",
        font="Anton",
        size_ratio=0.058,
        primary="#FFFFFF",
        accent="#31E981",
        outline_width=4.0,
        all_caps=True,
        animation="karaoke",
        max_words=5,
    ),
    "clean_lower": CaptionStyle(
        key="clean_lower",
        label="Clean Lower",
        description="Minimal lower third, no animation. For talks and professional cuts.",
        font="Inter",
        size_ratio=0.034,
        primary="#FFFFFF",
        accent=None,
        outline_width=2.0,
        shadow=1.0,
        bold=False,
        all_caps=False,
        margin_v_ratio=0.10,
        animation="none",
        max_words=8,
    ),
    "boxed": CaptionStyle(
        key="boxed",
        label="Boxed",
        description="High-contrast text on a solid block. Readable on any footage.",
        font="Anton",
        size_ratio=0.052,
        primary="#FFFFFF",
        accent="#FF4D4D",
        outline_width=0.0,
        all_caps=True,
        animation="scale",
        scale_percent=112,
        max_words=4,
        boxed=True,
        box_colour="#000000",
        box_alpha=30,
    ),
}

DEFAULT_STYLE = "bold_pop"


def get_style(key: str) -> CaptionStyle:
    style = PRESETS.get(key)
    if style is None:
        raise ValueError(f"Unknown caption style {key!r}. Available: {', '.join(PRESETS)}")
    return style


def resolve_style(
    key: str,
    *,
    size_scale: float = 1.0,
    position_ratio: float | None = None,
    overrides: Mapping[str, Any] | None = None,
    caption_margin_ratio: float | None = None,
    caption_x_ratio: float | None = None,
) -> CaptionStyle:
    """Resolve a preset plus everything the user layered on top of it.

    Highest wins (contract §1):

    1. ``overrides`` — the clip's own style blob
    2. ``caption_margin_ratio`` / ``caption_x_ratio`` — the clip's own geometry
    3. ``size_scale`` / ``position_ratio`` — the legacy global knobs, kept as a
       transition layer until #10 seeds a default preset from their values
    4. the preset itself

    Layer 3 sitting above the preset is not a misreading: it is today's
    behaviour, and no-override output has to stay byte-identical for an existing
    user (contract §8.3).

    Every numeric input is clamped rather than rejected, so a stale stored value
    can never break a render. Unknown values are ignored for the same reason —
    turning bad input into a 400 is the API layer's job.
    """
    style = get_style(key)
    changes: dict[str, Any] = {}

    scale = clamp(size_scale, (0.5, 2.0))
    if scale != 1.0:
        changes["size_ratio"] = style.size_ratio * scale

    margin = style.margin_v_ratio
    if position_ratio is not None:
        margin = clamp(position_ratio, MARGIN_RATIO_RANGE)
    if caption_margin_ratio is not None:
        margin = clamp(caption_margin_ratio, MARGIN_RATIO_RANGE)
    if margin != style.margin_v_ratio:
        changes["margin_v_ratio"] = margin

    x_ratio = caption_x_ratio
    if x_ratio is not None:
        x_ratio = clamp(x_ratio, X_RATIO_RANGE)
        # Centring here as well as in the UI costs one comparison and closes the
        # band where a "centred" caption would render left-aligned with a
        # margin thinner than its own outline.
        if abs(x_ratio - 0.5) <= X_SNAP_TOLERANCE:
            x_ratio = None
    if x_ratio is not None:
        changes["x_ratio"] = x_ratio

    clamped = clamp_overrides(overrides)
    for name in (
        "font",
        "size_ratio",
        "primary",
        "accent",
        "outline",
        "outline_width",
        "all_caps",
        "animation",
        "max_words",
        "boxed",
        "box_colour",
        "box_alpha",
    ):
        if name in clamped:
            changes[name] = clamped[name]

    return replace(style, **changes) if changes else style


def preview_payload(style: CaptionStyle) -> dict[str, Any]:
    """The resolved snapshot, for the preview and the exporter alike.

    One builder for ``/api/caption-styles`` and each clip's ``effective_caption``
    so the review player and the renderer cannot drift (contract §8.1). Keys are
    camelCase where the UI already reads them; the two geometry keys stay
    snake_case because §3 names them that way.
    """
    return {
        "font": style.font,
        "primary": style.primary,
        "accent": style.accent,
        "outline": style.outline,
        "outlineWidth": style.outline_width,
        "allCaps": style.all_caps,
        "sizeRatio": style.size_ratio,
        "marginRatio": style.margin_v_ratio,
        "boxed": style.boxed,
        "boxColour": style.box_colour,
        "boxAlpha": style.box_alpha,
        "animation": style.animation,
        "maxWords": style.max_words,
        "alignment": style.alignment,
        "margin_ratio": style.margin_v_ratio,
        "xRatio": style.x_ratio,
    }


# --------------------------------------------------------------------------
# Grouping
# --------------------------------------------------------------------------


@dataclass
class CaptionGroup:
    words: list[Word] = field(default_factory=list)

    @property
    def start(self) -> float:
        return self.words[0].start if self.words else 0.0

    @property
    def end(self) -> float:
        return self.words[-1].end if self.words else 0.0

    @property
    def text(self) -> str:
        return " ".join(w.text.strip() for w in self.words)


def group_words(
    words: list[Word],
    *,
    max_words: int = DEFAULT_MAX_WORDS,
    max_gap_s: float = DEFAULT_MAX_GAP_S,
) -> list[CaptionGroup]:
    """Split words into caption-sized groups.

    Breaks on three signals, in priority order: sentence-ending punctuation, a
    pause longer than ``max_gap_s``, and the word-count ceiling. Punctuation
    first means a caption rarely spans two sentences, which reads badly even
    when the timing is correct.
    """
    groups: list[CaptionGroup] = []
    current: list[Word] = []

    for index, word in enumerate(words):
        if current:
            gap = word.start - current[-1].end
            if gap > max_gap_s or len(current) >= max_words:
                groups.append(CaptionGroup(words=current))
                current = []

        current.append(word)

        is_last = index == len(words) - 1
        if word.ends_sentence and not is_last:
            groups.append(CaptionGroup(words=current))
            current = []

    if current:
        groups.append(CaptionGroup(words=current))

    return [g for g in groups if g.words]


# --------------------------------------------------------------------------
# ASS generation
# --------------------------------------------------------------------------

STYLE_NAME = "AutoClip"

#: The horizontal inset every caption carried before x-axis placement existed,
#: as a fraction of frame *height* — libass measures these margins in PlayRes
#: units but the legacy value was derived from height. Kept verbatim so a clip
#: with no x-axis override emits byte-identical ASS to the pre-styling renderer
#: (contract §8.3).
DEFAULT_HORIZONTAL_INSET = 0.03

#: ASS alignment per horizontal anchor.
ALIGNMENTS: dict[str, pysubs2.Alignment] = {
    "left": pysubs2.Alignment.BOTTOM_LEFT,
    "center": pysubs2.Alignment.BOTTOM_CENTER,
    "right": pysubs2.Alignment.BOTTOM_RIGHT,
}


def horizontal_margins(style: CaptionStyle, *, width: int, height: int) -> tuple[int, int]:
    """``(MarginL, MarginR)`` in PlayRes units — contract §2.

    The caption is anchored by its **edge**, not its centre, so the margin on the
    anchor side is simply how far that edge sits from the frame edge. The CSS
    mirror draws the same thing with ``text-align`` plus an inset of the same
    fraction, which is what keeps preview and burn-in from disagreeing.

    Centred is the pre-styling behaviour right down to the odd height-derived
    inset, and stays that way on purpose: no-override output must not shift.
    """
    inset = round(height * DEFAULT_HORIZONTAL_INSET)
    if style.x_ratio is None:
        return inset, inset
    if style.alignment == "left":
        return round(style.x_ratio * width), inset
    if style.alignment == "right":
        return inset, round((1.0 - style.x_ratio) * width)
    return inset, inset


def build_ass(
    words: list[Word],
    style: CaptionStyle,
    *,
    width: int,
    height: int,
    time_offset_s: float = 0.0,
) -> pysubs2.SSAFile:
    """Build an ASS subtitle file for a clip.

    ``time_offset_s`` is subtracted from every timestamp, so absolute transcript
    times become clip-relative ones.

    Preconditions:
        words are in chronological order and carry start/end timings.
    """
    subs = pysubs2.SSAFile()
    subs.info["PlayResX"] = str(width)
    subs.info["PlayResY"] = str(height)
    subs.info["ScaledBorderAndShadow"] = "yes"
    subs.info["WrapStyle"] = "0"

    scale = height / REFERENCE_HEIGHT
    subs.styles[STYLE_NAME] = _build_ass_style(style, width=width, height=height, scale=scale)

    groups = group_words(words, max_words=style.max_words)

    for group in groups:
        if style.animation == "karaoke":
            subs.events.append(_karaoke_event(group, style, time_offset_s))
        elif style.animation == "scale" and style.accent:
            subs.events.extend(_per_word_events(group, style, time_offset_s))
        else:
            subs.events.append(_static_event(group, style, time_offset_s))

    return subs


def _build_ass_style(
    style: CaptionStyle, *, width: int, height: int, scale: float
) -> pysubs2.SSAStyle:
    ass_style = pysubs2.SSAStyle()
    ass_style.fontname = style.font
    ass_style.fontsize = round(height * style.size_ratio)
    ass_style.primarycolor = hex_to_ass(style.primary)
    ass_style.secondarycolor = hex_to_ass(style.accent or style.primary)
    ass_style.outlinecolor = hex_to_ass(style.outline)
    ass_style.backcolor = hex_to_ass(style.box_colour, alpha=style.box_alpha)
    ass_style.bold = style.bold
    ass_style.outline = style.outline_width * scale
    ass_style.shadow = style.shadow * scale
    # BorderStyle 3 draws an opaque box using backcolor instead of an outline.
    ass_style.borderstyle = 3 if style.boxed else 1
    ass_style.alignment = ALIGNMENTS[style.alignment]
    ass_style.marginv = round(height * style.margin_v_ratio)
    ass_style.marginl, ass_style.marginr = horizontal_margins(style, width=width, height=height)
    return ass_style


def _text_of(word: Word, style: CaptionStyle) -> str:
    text = word.text.strip()
    return text.upper() if style.all_caps else text


def _event(start_s: float, end_s: float, text: str, offset: float) -> pysubs2.SSAEvent:
    return pysubs2.SSAEvent(
        start=pysubs2.make_time(s=max(0.0, start_s - offset)),
        end=pysubs2.make_time(s=max(0.0, end_s - offset)),
        text=text,
        style=STYLE_NAME,
    )


def _static_event(group: CaptionGroup, style: CaptionStyle, offset: float) -> pysubs2.SSAEvent:
    text = " ".join(_text_of(w, style) for w in group.words)
    return _event(group.start, group.end, text, offset)


def _karaoke_event(group: CaptionGroup, style: CaptionStyle, offset: float) -> pysubs2.SSAEvent:
    r"""Build a ``\kf`` karaoke line.

    ``\kf`` sweeps the fill across each word over its own duration; durations
    are in centiseconds, which is the unit ASS karaoke tags use.
    """
    parts: list[str] = []
    for index, word in enumerate(group.words):
        duration_cs = max(1, round(word.duration * 100))
        # Roll the inter-word gap into the preceding word so the sweep stays
        # locked to the audio instead of drifting late across the line.
        if index + 1 < len(group.words):
            gap = group.words[index + 1].start - word.end
            if 0 < gap < 0.5:
                duration_cs += round(gap * 100)
        parts.append(f"{{\\kf{duration_cs}}}{_text_of(word, style)}")

    return _event(group.start, group.end, " ".join(parts), offset)


def _per_word_events(
    group: CaptionGroup, style: CaptionStyle, offset: float
) -> list[pysubs2.SSAEvent]:
    """Emit one event per word, highlighting the active one.

    Every event renders the full group so the line stays stable on screen; only
    the emphasised word changes between them.
    """
    accent_tag = f"\\c{ass_colour_override(style.accent or style.primary)}"
    scale = style.scale_percent
    events: list[pysubs2.SSAEvent] = []

    for active, word in enumerate(group.words):
        rendered: list[str] = []
        for index, other in enumerate(group.words):
            text = _text_of(other, style)
            if index == active:
                rendered.append(f"{{{accent_tag}\\fscx{scale}\\fscy{scale}}}{text}{{\\r}}")
            else:
                rendered.append(text)

        # Hold the last word until the group ends so the caption doesn't blink
        # out early when a word's measured end precedes the next word's start.
        end = word.end if active + 1 < len(group.words) else group.end
        next_start = group.words[active + 1].start if active + 1 < len(group.words) else end
        events.append(_event(word.start, max(end, next_start), " ".join(rendered), offset))

    return events


def write_ass(
    path: Path,
    words: list[Word],
    style: CaptionStyle,
    *,
    width: int,
    height: int,
    time_offset_s: float = 0.0,
) -> Path:
    """Render captions to an .ass file and return its path."""
    subs = build_ass(words, style, width=width, height=height, time_offset_s=time_offset_s)
    path.parent.mkdir(parents=True, exist_ok=True)
    subs.save(str(path), encoding="utf-8")
    return path


def write_srt(path: Path, words: list[Word], *, time_offset_s: float = 0.0) -> Path:
    """Write a plain .srt sidecar — for platforms that want an upload-time file."""
    subs = pysubs2.SSAFile()
    for group in group_words(words, max_words=8):
        subs.events.append(
            pysubs2.SSAEvent(
                start=pysubs2.make_time(s=max(0.0, group.start - time_offset_s)),
                end=pysubs2.make_time(s=max(0.0, group.end - time_offset_s)),
                text=group.text,
            )
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    subs.save(str(path), encoding="utf-8", format_="srt")
    return path

"""Golden-set machinery for the reframe acceptance bar.

The first tagged release is gated on the reframe quality bar being validated
against a fixed golden video set — see README's "What has and hasn't been
verified" and docs/goldens.md. The golden media is real footage a human picks,
acquired once and pinned by hash, living outside the repo; this module is the
contract and the tooling around it:

- a manifest format (``manifest.json`` in the golden directory) recording where
  each file came from, when, through which yt-dlp format selector, and its
  sha256;
- :func:`acquire` — download a golden once and record that provenance;
- render sanity checks — probe plus decoded-frame statistics, catching renders
  that "succeed" but are blank or uniform.

The validation run itself is ``tests/test_golden_validation.py``, driven by
``pytest -m golden`` and the ``AUTOCLIP_GOLDEN_MEDIA`` env var. What a human
must judge with their own eyes — tracking smoothness, caption sync, whether
the PiP inset lands somewhere tasteful — is the checklist in docs/goldens.md;
automation catches regressions, it never decides the bar is met.

Media lives outside the repo on purpose: real footage is large and licensed,
so it is referenced by directory, the same way the e2e test points at a local
file rather than committing one.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .config import IngestSettings
from .pipeline import ffmpeg
from .pipeline.export import ratio_dimensions

#: Manifest lives in the golden directory alongside the media.
MANIFEST_NAME = "manifest.json"

#: Pinned selector used by acquire when none is given. The ingest default is
#: reused deliberately: goldens should be downloaded exactly like pipeline
#: sources, so any selector YouTube breaks surfaces in both places at once.
DEFAULT_FORMAT_SELECTOR = IngestSettings().ytdlp_format


class GoldenSetError(RuntimeError):
    """The golden set is unusable: malformed manifest, missing or changed media."""


# --------------------------------------------------------------------------
# Scenarios
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ScenarioSpec:
    """What the automated run asserts for one scenario.

    ``expect_facecam`` is the detection verdict the run enforces: a persistent
    corner face must be found for the stream VOD, and must *not* be found for
    the others — a talking-head close-up fails the corner test, and finding a
    "facecam" in pure screen content would mean detection is hallucinating.

    ``requires_tracked_subject`` guards the silent-failure case on footage
    that must track: a solo or multi-speaker golden that produces only
    GENERAL segments means face detection quietly did nothing.
    """

    key: str
    description: str
    expect_facecam: bool
    requires_tracked_subject: bool


SCENARIOS: dict[str, ScenarioSpec] = {
    "solo-speaker": ScenarioSpec(
        key="solo-speaker",
        description="One person talking to camera for the whole video.",
        expect_facecam=False,
        requires_tracked_subject=True,
    ),
    "multi-speaker": ScenarioSpec(
        key="multi-speaker",
        description="Two or more people in conversation.",
        expect_facecam=False,
        requires_tracked_subject=True,
    ),
    "facecam-stream-vod": ScenarioSpec(
        key="facecam-stream-vod",
        description="Screen content with the streamer's face parked in a corner.",
        expect_facecam=True,
        requires_tracked_subject=False,
    ),
    "screen-content": ScenarioSpec(
        key="screen-content",
        description="Pure screen content — slides, code, gameplay — no face.",
        expect_facecam=False,
        requires_tracked_subject=False,
    ),
}


# --------------------------------------------------------------------------
# Manifest
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Golden:
    """One pinned media file and the scenario it speaks for.

    ``file`` is relative to the golden directory; ``source_url`` is the
    acquisition origin — a link is a recipe for re-acquisition, never the
    golden itself (takedowns and re-encodes make raw URLs unstable).
    """

    file: str
    scenario: str
    sha256: str
    source_url: str | None = None
    acquired_date: str | None = None
    format_selector: str | None = None

    @property
    def spec(self) -> ScenarioSpec:
        try:
            return SCENARIOS[self.scenario]
        except KeyError:
            known = ", ".join(sorted(SCENARIOS))
            raise GoldenSetError(
                f"{self.file}: unknown scenario {self.scenario!r}. Known: {known}."
            ) from None

    def path(self, golden_dir: Path) -> Path:
        return golden_dir / self.file

    def to_dict(self) -> dict:
        return {
            "file": self.file,
            "scenario": self.scenario,
            "source_url": self.source_url,
            "acquired_date": self.acquired_date,
            "format_selector": self.format_selector,
            "sha256": self.sha256,
        }

    @classmethod
    def from_dict(cls, data: dict) -> Golden:
        if not isinstance(data, dict):
            raise GoldenSetError(f"Manifest entries must be objects; got {data!r}.")
        for key in ("file", "scenario", "sha256"):
            if not data.get(key):
                raise GoldenSetError(f"Manifest entry is missing {key!r}: {data!r}.")
        return cls(
            file=str(data["file"]),
            scenario=str(data["scenario"]),
            sha256=str(data["sha256"]).lower(),
            source_url=data.get("source_url"),
            acquired_date=data.get("acquired_date"),
            format_selector=data.get("format_selector"),
        )


def sha256_file(path: Path) -> str:
    """Streaming hash — golden files are hundreds of megabytes."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_manifest(golden_dir: Path, *, verify_hashes: bool = False) -> list[Golden]:
    """Load and validate the golden set.

    With ``verify_hashes`` the media is re-hashed, which is the point of
    pinning: a golden that changed on disk is a different video wearing the
    same name, and every past validation result is void.
    """
    manifest = golden_dir / MANIFEST_NAME
    if not manifest.exists():
        raise GoldenSetError(f"No {MANIFEST_NAME} in {golden_dir}.")
    try:
        raw = json.loads(manifest.read_text())
    except json.JSONDecodeError as exc:
        raise GoldenSetError(f"{manifest} is not valid JSON: {exc}") from exc
    if not isinstance(raw, list):
        raise GoldenSetError(f"{manifest} must hold a JSON array of entries.")

    goldens = [Golden.from_dict(entry) for entry in raw]
    if not goldens:
        raise GoldenSetError(f"{manifest} holds no entries.")
    for golden in goldens:
        if golden.scenario not in SCENARIOS:
            known = ", ".join(sorted(SCENARIOS))
            raise GoldenSetError(
                f"{golden.file}: unknown scenario {golden.scenario!r}. Known: {known}."
            )
        media = golden.path(golden_dir)
        if not media.exists():
            raise GoldenSetError(f"{golden.file} is in the manifest but not on disk.")
        if verify_hashes and sha256_file(media) != golden.sha256:
            raise GoldenSetError(
                f"{golden.file} has changed since acquisition (sha256 mismatch). "
                "A golden must stay fixed — re-acquire and update the manifest, "
                "or the validation history is meaningless."
            )
    return goldens


def upsert_manifest(golden_dir: Path, golden: Golden) -> Path:
    """Add or replace one entry, preserving the others; returns the manifest path."""
    manifest = golden_dir / MANIFEST_NAME
    entries: list[dict] = []
    if manifest.exists():
        entries = json.loads(manifest.read_text())
    entries = [e for e in entries if e.get("file") != golden.file]
    entries.append(golden.to_dict())
    entries.sort(key=lambda e: (e.get("scenario", ""), e.get("file", "")))
    manifest.write_text(json.dumps(entries, indent=2) + "\n")
    return manifest


# --------------------------------------------------------------------------
# Acquisition
# --------------------------------------------------------------------------


def acquire(
    url: str,
    scenario: str,
    golden_dir: Path,
    *,
    name: str | None = None,
    format_selector: str | None = None,
    cookies_from_browser: str | None = None,
    on_progress: Callable[[float], None] | None = None,
) -> Golden:
    """Download a golden once, pin it by hash, and record its provenance.

    Refuses to overwrite an existing file of the same name — a fixed golden
    must never be silently replaced. A failed download leaves the golden
    directory exactly as it was.
    """
    if scenario not in SCENARIOS:
        known = ", ".join(sorted(SCENARIOS))
        raise GoldenSetError(f"Unknown scenario {scenario!r}. Known: {known}.")
    if name and not all(c.isalnum() or c in "-_" for c in name):
        raise GoldenSetError(f"Name {name!r} may only use letters, digits, '-' and '_'.")

    import yt_dlp

    golden_dir.mkdir(parents=True, exist_ok=True)
    stem = name or scenario
    final = golden_dir / f"{stem}.mp4"
    if final.exists():
        raise GoldenSetError(
            f"{final.name} already exists — a golden is fixed once acquired. "
            "Pass a different --name or remove the old file on purpose."
        )

    selector = format_selector or DEFAULT_FORMAT_SELECTOR
    scratch = golden_dir / ".incoming"
    scratch.mkdir(exist_ok=True)
    options: dict = {
        "format": selector,
        "outtmpl": str(scratch / f"{stem}.%(ext)s"),
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "retries": 3,
        "fragment_retries": 3,
    }
    if cookies_from_browser:
        # yt-dlp expects a tuple; only the browser name is required.
        options["cookiesfrombrowser"] = (cookies_from_browser,)
    if on_progress:

        def hook(status: dict) -> None:
            if status.get("status") != "downloading":
                return
            total = status.get("total_bytes") or status.get("total_bytes_estimate")
            done = status.get("downloaded_bytes")
            if total and done:
                on_progress(min(1.0, done / total))

        options["progress_hooks"] = [hook]

    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            ydl.extract_info(url, download=True)
    except Exception as exc:
        shutil.rmtree(scratch, ignore_errors=True)
        raise GoldenSetError(f"Could not download {url}: {exc}") from exc

    downloaded = _single_file(scratch)
    if downloaded is None:
        shutil.rmtree(scratch, ignore_errors=True)
        raise GoldenSetError(f"yt-dlp reported success but {scratch} holds no media file.")
    downloaded.replace(final)
    shutil.rmtree(scratch, ignore_errors=True)

    golden = Golden(
        file=final.name,
        scenario=scenario,
        sha256=sha256_file(final),
        source_url=url,
        acquired_date=date.today().isoformat(),
        format_selector=selector,
    )
    upsert_manifest(golden_dir, golden)
    return golden


def _single_file(directory: Path) -> Path | None:
    files = [p for p in directory.iterdir() if p.is_file() and not p.name.startswith(".")]
    return files[0] if len(files) == 1 else None


# --------------------------------------------------------------------------
# Render sanity
# --------------------------------------------------------------------------


#: An 8-bit frame with a standard deviation below this is one flat colour.
UNIFORM_FRAME_STD = 3.0

#: How many frames, spread over the render, the sanity check decodes.
SANITY_FRAME_COUNT = 5


def render_report(render: Path) -> dict:
    """Probe a render and decode a few frames.

    Returns the probe facts plus per-frame brightness statistics — enough to
    catch the failure mode no unit test can: an export that encodes fine and
    plays back blank. Assertions belong to the caller; this never raises on
    degenerate output, it reports it.
    """
    info = ffmpeg.probe(render)
    return {
        "width": info.width,
        "height": info.height,
        "duration_s": info.duration_s,
        "has_audio": info.has_audio,
        "video_codec": info.video_codec,
        "frames": _frame_stats(render, duration_s=info.duration_s),
    }


def assert_render_sane(
    render: Path,
    *,
    ratio: str | None = None,
    expected_duration_s: float | None = None,
) -> dict:
    """Raise :class:`GoldenSetError` unless the render is structurally sound.

    Checks aspect (via ``ratio``), duration, audio presence, and that sampled
    frames carry actual picture. Returns the :func:`render_report` facts.
    """
    report = render_report(render)
    if ratio is not None and (report["width"], report["height"]) != ratio_dimensions(ratio):
        raise GoldenSetError(
            f"{render.name} is {report['width']}x{report['height']}, "
            f"expected {ratio_dimensions(ratio)} for {ratio}."
        )
    if not report["has_audio"]:
        raise GoldenSetError(f"{render.name} has no audio stream.")
    if expected_duration_s is not None and abs(report["duration_s"] - expected_duration_s) > 0.6:
        raise GoldenSetError(
            f"{render.name} runs {report['duration_s']:.1f}s, "
            f"expected about {expected_duration_s:.1f}s."
        )
    for frame in report["frames"]:
        if frame["std"] < UNIFORM_FRAME_STD:
            raise GoldenSetError(
                f"{render.name} has blank or uniform frames around t={frame['t']:.1f}s "
                f"(std {frame['std']:.2f})."
            )
    return report


def _frame_stats(render: Path, *, duration_s: float) -> list[dict]:
    """Decode frames spread over the file and summarise their pixel values.

    PyAV and NumPy are core dependencies (transcription and reframe need
    them), so this adds nothing to an install. Seeks in microseconds on the
    container — keyframe granularity is fine, the frames only need to be
    *somewhere near* the target times.
    """
    import av

    duration = max(duration_s, 0.1)
    targets = [duration * (i + 0.5) / SANITY_FRAME_COUNT for i in range(SANITY_FRAME_COUNT)]
    stats: list[dict] = []
    with av.open(str(render)) as container:
        video = container.streams.video[0]
        for target in targets:
            container.seek(int(target * 1_000_000))
            for frame in container.decode(video):
                pixels = frame.to_ndarray(format="rgb24")
                stats.append(
                    {
                        "t": target,
                        "mean": float(pixels.mean()),
                        "std": float(pixels.std()),
                    }
                )
                break
    return stats

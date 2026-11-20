"""Reframe acceptance bar: the golden validation run.

One run = the whole pipeline, for real — ffmpeg, faster-whisper, MediaPipe,
libass — against every pinned golden in the set, plus the framing features
landed alongside it: crop paths from tracking, facecam detection, exports at
all three ratios, and a picture-in-picture render when a facecam is found.
Only the language model's *answer* is scripted, exactly like the e2e pipeline
test: an API key must not gate the acceptance bar, and clip selection is a
judgement call that would make the run non-deterministic.

The golden media lives outside the repo, pinned by hash; point the run at it::

    AUTOCLIP_GOLDEN_MEDIA=/path/to/goldens pytest -m golden -q

Acquire goldens with ``autoclip goldens acquire`` (see docs/goldens.md).
Transcription dominates: expect several minutes per golden. A malformed
manifest or a changed file fails the collection loudly rather than skipping —
a broken golden set is a red result, not a silent pass.

What this run asserts is structural: stages complete, crops stay inside the
frame and tile their clips, the facecam verdict matches the scenario, renders
have the right shape and carry actual picture. Whether the output is *good*
is the manual checklist in docs/goldens.md — automation catches regressions,
it never decides the bar is met.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest
from autoclip.config import Settings
from autoclip.db import store
from autoclip.db.models import Job, new_id
from autoclip.goldens import (
    UNIFORM_FRAME_STD,
    Golden,
    GoldenSetError,
    load_manifest,
    render_report,
)
from autoclip.pipeline import captions, ingest
from autoclip.pipeline import export as export_module
from autoclip.pipeline.export import ExportRequest, PipLayout, ratio_dimensions
from autoclip.pipeline.ffmpeg import probe
from autoclip.pipeline.reframe.croppath import CropPath, Strategy, centre_crop
from autoclip.pipeline.reframe.facecam import FacecamBox, search_facecam
from autoclip.pipeline.runner import JobWorkspace, PipelineRunner
from autoclip.pipeline.transcript import Transcript
from autoclip.providers import ClipCandidates, DetectionConfig, LLMProvider, ProviderStatus

pytestmark = [pytest.mark.slow, pytest.mark.golden]

ENV_GOLDENS = "AUTOCLIP_GOLDEN_MEDIA"

#: Facecam search window: a persistent corner face shows up in any stretch of
#: video, so the first stretch is enough — at a fraction of the cost of
#: scanning the whole source.
FACECAM_WINDOW_S = 90.0
FACECAM_SAMPLE_FPS = 0.5


def _golden_dir() -> Path:
    raw = os.environ.get(ENV_GOLDENS)
    if not raw:
        pytest.skip(
            f"Set {ENV_GOLDENS} to a golden directory to run the acceptance pass.",
            allow_module_level=True,
        )
    path = Path(raw)
    if not path.is_dir():
        pytest.skip(
            f"{ENV_GOLDENS} points at {path}, which is not a directory.",
            allow_module_level=True,
        )
    return path


GOLDEN_DIR = _golden_dir()

try:
    GOLDENS: list[Golden] = load_manifest(GOLDEN_DIR, verify_hashes=True)
except GoldenSetError as exc:
    # A broken golden set is a red result, not a silent pass: let the error
    # surface as a collection error with the message, rather than skipping.
    raise GoldenSetError(f"The golden set is broken: {exc}") from exc

GOLDENS_BY_FILE = {golden.file: golden for golden in GOLDENS}


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


@pytest.fixture(scope="module", autouse=True)
def autoclip_home(tmp_path_factory):
    """One throwaway home for the whole module.

    Shadows the function-scoped autouse fixture in conftest by name, exactly
    like the e2e module does: without this the pipeline runs once and every
    assertion then looks at a fresh empty database.
    """
    from _pytest.monkeypatch import MonkeyPatch
    from autoclip import db, paths

    patcher = MonkeyPatch()
    home = tmp_path_factory.mktemp("goldens_home")
    patcher.setenv(paths.ENV_HOME, str(home))
    db.reset_connections()

    yield home

    patcher.undo()
    db.reset_connections()


@pytest.fixture(scope="module", params=[g.file for g in GOLDENS])
def golden_run(request, autoclip_home: Path) -> dict:
    """Run the full validation once per golden; the tests below read the report."""
    golden = GOLDENS_BY_FILE[request.param]
    return run_golden(golden, GOLDEN_DIR, home=autoclip_home)


# --------------------------------------------------------------------------
# The scripted provider and the run itself
# --------------------------------------------------------------------------


class ScriptedProvider(LLMProvider):
    """Stands in for the LLM, returning spans anchored to the real transcript.

    The acceptance bar must not depend on an API key or a model's mood, but
    everything downstream of the answer — dedupe, duration clamping, reframe,
    caption burn-in, export — runs for real on real footage.
    """

    name = "scripted"
    requires_key = False

    def __init__(self) -> None:
        super().__init__("scripted-model")

    async def _complete(self, system: str, user: str, config: DetectionConfig) -> str:
        raise AssertionError("detect_highlights is overridden; _complete is unused")

    async def health_check(self) -> ProviderStatus:
        return ProviderStatus(name=self.name, available=True)

    async def detect_highlights(self, window, config) -> ClipCandidates:
        first, last = window.first_word, window.last_word
        span = max(1, (last - first) // 4)
        return ClipCandidates.model_validate(
            {
                "clips": [
                    {
                        "start_word_index": first,
                        "end_word_index": min(last, first + span),
                        "title": "Opening stretch",
                        "hook": "first words",
                        "score": 91,
                        "reason": "Scripted candidate covering the start of the window.",
                    },
                    {
                        # Overlaps the first by design — dedupe must drop it.
                        "start_word_index": first + span // 2,
                        "end_word_index": min(last, first + span),
                        "title": "Overlapping duplicate",
                        "score": 60,
                        "reason": "Should be removed by dedupe.",
                    },
                    {
                        "start_word_index": min(last - 1, first + 2 * span),
                        "end_word_index": min(last, first + 3 * span),
                        "title": "Later stretch",
                        "hook": "second span",
                        "score": 78,
                        "reason": "Scripted candidate from later in the window.",
                    },
                ]
            }
        )


def _crop_path_for(workspace: JobWorkspace, clip, source, ratio: str) -> CropPath:
    """The job's cached path, or a centre crop — the same shape the API uses."""
    cached = workspace.crop_path(clip.id)
    if cached.exists():
        return CropPath.load(cached)
    aspect = {"9:16": (9, 16), "1:1": (1, 1), "16:9": (16, 9)}[ratio]
    return centre_crop(
        source.width or 1920,
        source.height or 1080,
        clip.end_s - clip.start_s,
        aspect_w=aspect[0],
        aspect_h=aspect[1],
    )


def run_golden(golden: Golden, golden_dir: Path, *, home: Path) -> dict:
    """Run everything once for one golden and return the facts, not verdicts.

    Assertions live in the tests so a failure names the check that failed
    instead of the whole run.
    """
    from autoclip import db
    from autoclip.pipeline import runner as runner_module

    db.init()

    media = golden.path(golden_dir)
    source = store.create_source(ingest.ingest_file(media, title=golden.file))

    settings = Settings()
    settings.whisper.model = "tiny"  # transcription dominates runtime; tiny is enough
    settings.clips.max_clips = 3
    settings.clips.min_duration_s = 12.0
    settings.clips.max_duration_s = 45.0
    settings.export.ratio = "9:16"
    settings.export.caption_style = "bold_pop"

    job = store.create_job(Job(id=new_id(), source_id=source.id, provider="scripted", settings={}))

    provider = ScriptedProvider()
    original = runner_module.build_provider
    runner_module.build_provider = lambda *args, **kwargs: provider
    try:
        runner = PipelineRunner(job, source, settings=settings)
        clips = asyncio.run(runner.run())
    finally:
        runner_module.build_provider = original

    if not clips:
        pytest.fail(
            f"{golden.file}: the scripted provider produced no clips — the golden is "
            "too short or too quiet for the 12–45s clip window. Pick longer media."
        )

    workspace = JobWorkspace(job.id)

    # Facecam verdict over a fixed window at the top of the source.
    info = probe(media)
    window_end = min(FACECAM_WINDOW_S, info.duration_s or FACECAM_WINDOW_S)
    facecam: FacecamBox | None = search_facecam(
        Path(source.path),
        info.width or 1920,
        info.height or 1080,
        start_s=0.0,
        end_s=window_end,
        sample_fps=FACECAM_SAMPLE_FPS,
    )

    # Extra renders for the first clip: the other two ratios, plus a PiP
    # export through the same request shape the API's export endpoint builds —
    # the never-slow-gated path the review UI exercises in production.
    first = clips[0]
    transcript = Transcript.load(workspace.transcript)
    words = transcript.slice(first.start_word, first.end_word)
    style = captions.get_style(settings.export.caption_style)

    renders: dict[str, Path] = {}
    reports: dict[str, dict] = {}
    render_dir = home / "golden_renders" / Path(golden.file).stem
    render_dir.mkdir(parents=True, exist_ok=True)

    def render(ratio: str, *, pip: PipLayout | None, stem: str) -> None:
        request = ExportRequest(
            source=Path(source.path),
            destination=render_dir / f"{stem}.mp4",
            start_s=first.start_s,
            end_s=first.end_s,
            crop_path=_crop_path_for(workspace, first, source, ratio),
            words=words,
            style=style,
            ratio=ratio,
            pip=pip,
        )
        path = export_module.export_clip(request, work_dir=render_dir, settings=settings.export)
        renders[stem] = path
        reports[stem] = render_report(path)

    for ratio in ("1:1", "16:9"):
        render(ratio, pip=None, stem=ratio.replace(":", "x"))
    if facecam is not None:
        pip = PipLayout(
            x=facecam.x,
            y=facecam.y,
            width=facecam.width,
            height=facecam.height,
            corner=facecam.corner,
            size_ratio=0.35,
        )
        render("9:16", pip=pip, stem="pip_9x16")

    # The pipeline's own 9:16 exports, probed from the store.
    pipeline_reports = {}
    for clip in clips:
        export_row = store.list_exports(clip.id)[0]
        pipeline_reports[clip.id] = render_report(Path(export_row.path))

    return {
        "golden": golden,
        "spec": golden.spec,
        "source": source,
        "job": job,
        "clips": clips,
        "workspace": workspace,
        "transcript": transcript,
        "facecam": facecam,
        "renders": renders,
        "reports": reports,
        "pipeline_reports": pipeline_reports,
    }


# --------------------------------------------------------------------------
# The acceptance checks
# --------------------------------------------------------------------------


class TestGoldenSet:
    def test_the_set_covers_every_scenario(self) -> None:
        """The bar is defined over four scenarios; a partial set is a partial bar."""
        from autoclip.goldens import SCENARIOS

        have = {golden.scenario for golden in GOLDENS}
        missing = sorted(set(SCENARIOS) - have)
        assert not missing, (
            f"The golden set is missing scenarios: {missing}. The acceptance bar "
            "is defined over all of them — acquire the missing ones with "
            "`autoclip goldens acquire <url> --scenario <key> --dest <dir>`."
        )

    def test_pins_were_verified_at_load(self) -> None:
        """The manifest was loaded with verify_hashes=True; reaching here means the pins held."""
        assert GOLDENS  # the load itself is the assertion


class TestPipelineCompletes:
    def test_job_reaches_done(self, golden_run) -> None:
        job = store.get_job(golden_run["job"].id)

        assert job is not None
        assert job.status == "done"
        assert job.error is None
        assert job.progress == pytest.approx(1.0)

    def test_clips_were_produced(self, golden_run) -> None:
        assert len(golden_run["clips"]) > 0

    def test_transcript_has_word_timings(self, golden_run) -> None:
        transcript = golden_run["transcript"]

        assert len(transcript.words) > 50
        assert all(word.end >= word.start for word in transcript.words)


class TestReframe:
    def test_crop_paths_tile_their_clips(self, golden_run) -> None:
        workspace = golden_run["workspace"]

        for clip in golden_run["clips"]:
            path = CropPath.load(workspace.crop_path(clip.id))
            segments = path.segments

            assert segments
            assert segments[0].start_s == pytest.approx(0.0, abs=0.05)
            assert segments[-1].end_s == pytest.approx(clip.duration_s, abs=0.05)
            for earlier, later in zip(segments, segments[1:], strict=False):
                # A gap or overlap desyncs the concatenated render from audio.
                assert earlier.end_s == pytest.approx(later.start_s, abs=0.01)

    def test_crops_stay_inside_the_frame(self, golden_run) -> None:
        workspace = golden_run["workspace"]

        for clip in golden_run["clips"]:
            path = CropPath.load(workspace.crop_path(clip.id))
            for segment in path.segments:
                assert segment.width <= path.source_width
                assert segment.height <= path.source_height
                for keyframe in segment.keyframes:
                    assert -1 <= keyframe.x <= path.source_width - segment.width + 1
                    assert -1 <= keyframe.y <= path.source_height - segment.height + 1

    def test_crop_dimensions_are_even(self, golden_run) -> None:
        # Odd dimensions fail an h264 yuv420p encode.
        workspace = golden_run["workspace"]

        for clip in golden_run["clips"]:
            for segment in CropPath.load(workspace.crop_path(clip.id)).segments:
                assert segment.width % 2 == 0
                assert segment.height % 2 == 0

    def test_a_subject_is_tracked_when_the_scenario_requires_it(self, golden_run) -> None:
        """Only GENERAL segments on footage that must track means detection did nothing."""
        if not golden_run["spec"].requires_tracked_subject:
            pytest.skip(f"{golden_run['golden'].scenario} does not require a tracked subject.")

        workspace = golden_run["workspace"]
        strategies = {
            segment.strategy
            for clip in golden_run["clips"]
            for segment in CropPath.load(workspace.crop_path(clip.id)).segments
        }

        assert strategies & {Strategy.TRACK, Strategy.WIDE}, (
            f"No subject was tracked (strategies: {strategies}) — face detection "
            "produced nothing on footage that must track."
        )


class TestFacecam:
    def test_the_verdict_matches_the_scenario(self, golden_run) -> None:
        found = golden_run["facecam"] is not None

        assert found == golden_run["spec"].expect_facecam, (
            f"{golden_run['golden'].scenario}: facecam detection returned "
            f"{'a box' if found else 'nothing'}, the scenario expects "
            f"{'one' if golden_run['spec'].expect_facecam else 'none'}."
        )

    def test_a_found_box_is_a_persistent_corner(self, golden_run) -> None:
        box = golden_run["facecam"]
        if box is None:
            pytest.skip("No facecam expected for this scenario.")

        source = golden_run["source"]

        assert box.corner in {"tl", "tr", "bl", "br"}
        assert box.coverage >= 0.5, f"Coverage {box.coverage:.2f} is not a persistent facecam."
        assert 0 <= box.x <= (source.width or 1920)
        assert 0 <= box.y <= (source.height or 1080)
        assert box.width > 0 and box.height > 0


class TestRenders:
    def test_pipeline_exported_every_clip_at_9x16(self, golden_run) -> None:
        for clip in golden_run["clips"]:
            exports = store.list_exports(clip.id)
            assert len(exports) == 1
            assert Path(exports[0].path).exists()

    def test_extra_renders_exist_for_the_other_ratios(self, golden_run) -> None:
        assert set(golden_run["renders"]) >= {"1x1", "16x9"}

    def test_a_pip_render_exists_exactly_when_a_facecam_was_found(self, golden_run) -> None:
        """PiP renders whenever detection fires — the path gets exercised on
        any golden with a persistent corner face, not just the stream VOD."""
        found = golden_run["facecam"] is not None

        assert ("pip_9x16" in golden_run["renders"]) == found

    @staticmethod
    def _all_reports(golden_run) -> dict[str, dict]:
        reports = dict(golden_run["reports"])
        reports.update(golden_run["pipeline_reports"])
        return reports

    def test_render_dimensions_match_their_ratios(self, golden_run) -> None:
        expected = {"1x1": "1:1", "16x9": "16:9", "pip_9x16": "9:16"}
        for stem, report in golden_run["reports"].items():
            ratio = expected[stem]
            assert (report["width"], report["height"]) == ratio_dimensions(ratio), stem
        for clip_id, report in golden_run["pipeline_reports"].items():
            assert (report["width"], report["height"]) == ratio_dimensions("9:16"), clip_id

    def test_renders_have_audio_and_the_clip_duration(self, golden_run) -> None:
        # Extra renders come from clips[0]; the pipeline's own 9:16 exports
        # are keyed by clip id and must each match their own clip.
        first = golden_run["clips"][0]
        durations = dict.fromkeys(golden_run["reports"], first.duration_s)
        by_id = {clip.id: clip for clip in golden_run["clips"]}
        durations.update(
            {clip_id: by_id[clip_id].duration_s for clip_id in golden_run["pipeline_reports"]}
        )
        reports = dict(golden_run["reports"])
        reports.update(golden_run["pipeline_reports"])

        for stem, report in reports.items():
            expected = durations[stem]
            assert report["has_audio"], stem
            assert report["duration_s"] == pytest.approx(expected, abs=0.6), stem

    def test_renders_carry_actual_picture(self, golden_run) -> None:
        for stem, report in self._all_reports(golden_run).items():
            assert report["frames"], stem
            for frame in report["frames"]:
                assert frame["std"] >= UNIFORM_FRAME_STD, (
                    f"{stem}: frame at t={frame['t']:.1f}s is blank or uniform."
                )

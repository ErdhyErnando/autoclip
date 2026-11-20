"""Golden-set machinery self-tests.

The manifest contract, hash pinning, and render sanity checks are exercised
here on synthetic files so the harness itself is tested without real media
and without the network. The full validation run lives in
test_golden_validation.py and is env-gated on the real golden set.
"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import pytest
from autoclip.goldens import (
    SANITY_FRAME_COUNT,
    SCENARIOS,
    UNIFORM_FRAME_STD,
    Golden,
    GoldenSetError,
    assert_render_sane,
    load_manifest,
    sha256_file,
    upsert_manifest,
)
from autoclip.pipeline import ffmpeg

W, H = 320, 240
DURATION = 2.0


def write_media(directory: Path, name: str, content: bytes = b"golden media bytes") -> Path:
    path = directory / name
    path.write_bytes(content)
    return path


def golden_for(directory: Path, file: str, scenario: str = "solo-speaker") -> Golden:
    """A valid Golden pinning whatever bytes are actually on disk."""
    return Golden(
        file=file,
        scenario=scenario,
        sha256=sha256_file(directory / file),
        source_url="https://example.com/watch?v=abc",
        acquired_date="2026-10-26",
        format_selector="bestvideo[height<=1080]+bestaudio/best",
    )


class TestScenarios:
    def test_the_four_scenarios_are_defined(self) -> None:
        assert set(SCENARIOS) == {
            "solo-speaker",
            "multi-speaker",
            "facecam-stream-vod",
            "screen-content",
        }

    def test_only_the_stream_vod_expects_a_facecam(self) -> None:
        expecting = [key for key, spec in SCENARIOS.items() if spec.expect_facecam]

        assert expecting == ["facecam-stream-vod"]

    def test_talking_scenarios_require_a_tracked_subject(self) -> None:
        assert SCENARIOS["solo-speaker"].requires_tracked_subject
        assert SCENARIOS["multi-speaker"].requires_tracked_subject
        assert not SCENARIOS["facecam-stream-vod"].requires_tracked_subject
        assert not SCENARIOS["screen-content"].requires_tracked_subject


class TestManifest:
    def test_round_trips_through_upsert_and_load(self, tmp_path: Path) -> None:
        write_media(tmp_path, "solo-speaker.mp4")
        golden = golden_for(tmp_path, "solo-speaker.mp4")

        upsert_manifest(tmp_path, golden)

        loaded = load_manifest(tmp_path)
        assert [g.to_dict() for g in loaded] == [golden.to_dict()]
        assert loaded[0].spec is SCENARIOS["solo-speaker"]

    def test_hash_verification_passes_while_the_file_is_unchanged(self, tmp_path: Path) -> None:
        write_media(tmp_path, "solo-speaker.mp4")
        upsert_manifest(tmp_path, golden_for(tmp_path, "solo-speaker.mp4"))

        assert len(load_manifest(tmp_path, verify_hashes=True)) == 1

    def test_a_changed_file_fails_hash_verification(self, tmp_path: Path) -> None:
        media = write_media(tmp_path, "solo-speaker.mp4")
        upsert_manifest(tmp_path, golden_for(tmp_path, "solo-speaker.mp4"))

        media.write_bytes(b"different bytes: the file was re-encoded or replaced")

        with pytest.raises(GoldenSetError, match="changed since acquisition"):
            load_manifest(tmp_path, verify_hashes=True)

    def test_an_unknown_scenario_is_rejected(self, tmp_path: Path) -> None:
        write_media(tmp_path, "mystery.mp4")
        upsert_manifest(tmp_path, golden_for(tmp_path, "mystery.mp4"))
        manifest = tmp_path / "manifest.json"
        manifest.write_text(manifest.read_text().replace('"solo-speaker"', '"made-up-scenario"'))

        with pytest.raises(GoldenSetError, match="unknown scenario"):
            load_manifest(tmp_path)

    def test_a_missing_media_file_is_rejected(self, tmp_path: Path) -> None:
        upsert_manifest(
            tmp_path,
            Golden(file="never-downloaded.mp4", scenario="solo-speaker", sha256="0" * 64),
        )

        with pytest.raises(GoldenSetError, match="not on disk"):
            load_manifest(tmp_path)

    def test_a_missing_manifest_is_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(GoldenSetError, match="No manifest.json"):
            load_manifest(tmp_path)

    def test_malformed_manifests_are_rejected(self, tmp_path: Path) -> None:
        manifest = tmp_path / "manifest.json"

        manifest.write_text("{ not json")
        with pytest.raises(GoldenSetError, match="not valid JSON"):
            load_manifest(tmp_path)

        manifest.write_text('{"file": "not-a-list"}')
        with pytest.raises(GoldenSetError, match="must hold a JSON array"):
            load_manifest(tmp_path)

        manifest.write_text("[]")
        with pytest.raises(GoldenSetError, match="no entries"):
            load_manifest(tmp_path)

        manifest.write_text('[{"file": "x.mp4"}]')
        with pytest.raises(GoldenSetError, match="missing 'scenario'"):
            load_manifest(tmp_path)

    def test_upsert_replaces_the_same_file_and_preserves_others(self, tmp_path: Path) -> None:
        write_media(tmp_path, "solo-speaker.mp4", b"v1")
        write_media(tmp_path, "screen-content.mp4", b"other")
        first = golden_for(tmp_path, "solo-speaker.mp4")
        upsert_manifest(tmp_path, first)
        upsert_manifest(tmp_path, golden_for(tmp_path, "screen-content.mp4", "screen-content"))

        media = tmp_path / "solo-speaker.mp4"
        media.write_bytes(b"v2, a deliberate re-acquisition")
        upsert_manifest(tmp_path, golden_for(tmp_path, "solo-speaker.mp4"))

        loaded = {g.file: g for g in load_manifest(tmp_path)}
        assert set(loaded) == {"solo-speaker.mp4", "screen-content.mp4"}
        assert loaded["solo-speaker.mp4"].sha256 == sha256_file(media)
        assert loaded["screen-content.mp4"].scenario == "screen-content"

    def test_sha256_file_matches_hashlib(self, tmp_path: Path) -> None:
        media = write_media(tmp_path, "x.mp4", b"known bytes")

        expected = hashlib.sha256(b"known bytes").hexdigest()
        assert sha256_file(media) == expected


# --------------------------------------------------------------------------
# Render sanity — these shell out to ffmpeg, so they are slow-marked like
# every other test that encodes real video.
# --------------------------------------------------------------------------


def encode(path: Path, video_source: str, *, with_audio: bool = True) -> Path:
    command = [
        ffmpeg.ffmpeg_path(),
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        video_source,
    ]
    if with_audio:
        command += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={DURATION}"]
    command += ["-c:v", "libx264", "-pix_fmt", "yuv420p"]
    if with_audio:
        command += ["-c:a", "aac", "-shortest"]
    command += [str(path)]
    subprocess.run(command, check=True, capture_output=True)
    return path


@pytest.fixture(scope="module")
def real_render(tmp_path_factory) -> Path:
    """A genuine 320x240 render with moving picture and a tone."""
    return encode(
        tmp_path_factory.mktemp("goldens_render") / "real.mp4",
        f"testsrc2=size={W}x{H}:rate=30:duration={DURATION}",
    )


@pytest.fixture(scope="module")
def blank_render(tmp_path_factory) -> Path:
    """Encodes fine, plays back black — the failure the frame check exists for."""
    return encode(
        tmp_path_factory.mktemp("goldens_blank") / "blank.mp4",
        f"color=c=black:s={W}x{H}:r=30:duration={DURATION}",
    )


@pytest.fixture(scope="module")
def silent_render(tmp_path_factory) -> Path:
    """Correct picture, but no audio stream."""
    return encode(
        tmp_path_factory.mktemp("goldens_silent") / "silent.mp4",
        f"testsrc2=size={W}x{H}:rate=30:duration={DURATION}",
        with_audio=False,
    )


@pytest.mark.slow
class TestRenderSanity:
    def test_a_real_render_passes_every_check(self, real_render: Path) -> None:
        report = assert_render_sane(real_render, expected_duration_s=DURATION)

        assert report["has_audio"] is True
        assert report["duration_s"] == pytest.approx(DURATION, abs=0.5)
        assert len(report["frames"]) == SANITY_FRAME_COUNT
        assert all(frame["std"] > UNIFORM_FRAME_STD for frame in report["frames"])

    def test_a_blank_render_is_caught(self, blank_render: Path) -> None:
        with pytest.raises(GoldenSetError, match="blank or uniform"):
            assert_render_sane(blank_render)

    def test_the_wrong_dimensions_are_caught(self, real_render: Path) -> None:
        with pytest.raises(GoldenSetError, match="expected"):
            assert_render_sane(real_render, ratio="9:16")

    def test_missing_audio_is_caught(self, silent_render: Path) -> None:
        with pytest.raises(GoldenSetError, match="no audio"):
            assert_render_sane(silent_render)

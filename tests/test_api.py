"""REST API contract tests.

Exercised through the real app with its lifespan running, so the database
migration, event broker binding, and job queue startup are all covered too.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from autoclip import app as app_module
from autoclip import config
from autoclip.api import clips as clips_module
from autoclip.app import create_app
from autoclip.db import store
from autoclip.db.models import Clip, Job, Source, new_id
from fastapi.testclient import TestClient


@pytest.fixture
def client(autoclip_home, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    # The background worker is off here so queued jobs stay queued: these tests
    # are about the HTTP contract, and a worker racing to pick jobs up would
    # make every status assertion flaky. The worker has its own tests.
    monkeypatch.setenv(app_module.ENV_NO_WORKER, "1")
    with TestClient(create_app()) as test_client:
        yield test_client


@pytest.fixture
def source() -> Source:
    return store.create_source(
        Source(
            id=new_id(),
            type="upload",
            path="C:/media/video.mp4",
            title="Test source",
            duration_s=600.0,
            width=1920,
            height=1080,
            fps=30.0,
        )
    )


def _write_transcript(job_id: str, *, count: int) -> None:
    """Drop a transcript where the job workspace expects one.

    The clip word endpoints (and therefore the export path, which reads its
    words through them) 404 without this.
    """
    from autoclip.pipeline.runner import JobWorkspace
    from autoclip.pipeline.transcript import Transcript, Word

    words = [Word(text=f"word{i}", start=i * 0.4, end=i * 0.4 + 0.3) for i in range(count)]
    Transcript(words=words).save(JobWorkspace(job_id).transcript)


class TestHealthAndSystem:
    def test_health(self, client: TestClient) -> None:
        response = client.get("/api/health")

        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    def test_system_reports_this_machine(self, client: TestClient) -> None:
        body = client.get("/api/system").json()

        # Compared against the running interpreter, not a hardcoded version:
        # the project supports 3.11 and 3.12, and pinning the assertion to one
        # of them made the test contradict the CI matrix it runs under.
        running = f"{sys.version_info.major}.{sys.version_info.minor}"
        assert body["python_version"].startswith(running)
        assert (3, 11) <= sys.version_info[:2] < (3, 13)

        assert body["accel"] in ("cuda", "mps", "cpu")
        assert isinstance(body["nvenc_works"], bool)

    def test_unknown_api_route_is_404_not_the_spa(self, client: TestClient) -> None:
        # The SPA catch-all must never swallow a mistyped API path — that turns
        # a clear 404 into an HTML page the client can't parse.
        assert client.get("/api/does-not-exist").status_code == 404


@pytest.fixture
def job_with_clips(source: Source) -> Job:
    job = store.create_job(Job(id=new_id(), source_id=source.id, status="done"))
    store.replace_clips(
        job.id,
        [
            Clip(
                id=new_id(),
                job_id=job.id,
                rank=rank,
                start_s=rank * 60.0,
                end_s=rank * 60.0 + 40.0,
                start_word=rank * 100,
                end_word=rank * 100 + 80,
                title=f"Clip {rank}",
                score=90 - rank,
            )
            for rank in (1, 2, 3)
        ],
    )
    return job


class TestCaptionStyles:
    def test_lists_all_four(self, client: TestClient) -> None:
        styles = client.get("/api/caption-styles").json()

        assert {s["key"] for s in styles} == {
            "bold_pop",
            "karaoke_fill",
            "clean_lower",
            "boxed",
        }

    def test_includes_preview_metadata(self, client: TestClient) -> None:
        styles = client.get("/api/caption-styles").json()
        preview = next(s for s in styles if s["key"] == "bold_pop")["preview"]

        assert preview["accent"]
        assert preview["allCaps"] is True
        assert preview["maxWords"] > 0


class TestSettings:
    def test_get_returns_defaults(self, client: TestClient) -> None:
        body = client.get("/api/settings").json()

        assert body["active_provider"] == "anthropic"
        assert body["whisper"]["model"] == "small"

    def test_partial_update_leaves_other_sections_alone(self, client: TestClient) -> None:
        client.put("/api/settings", json={"whisper": {"model": "large-v3"}})

        body = client.get("/api/settings").json()
        assert body["whisper"]["model"] == "large-v3"
        assert body["clips"]["max_clips"] == 10

    def test_provider_switch(self, client: TestClient) -> None:
        response = client.put("/api/settings", json={"active_provider": "ollama"})

        assert response.status_code == 200
        assert response.json()["active_provider"] == "ollama"

    def test_unknown_provider_is_rejected(self, client: TestClient) -> None:
        response = client.put("/api/settings", json={"active_provider": "skynet"})

        assert response.status_code == 400

    def test_inverted_clip_lengths_are_rejected(self, client: TestClient) -> None:
        response = client.put(
            "/api/settings", json={"clips": {"min_duration_s": 90, "max_duration_s": 20}}
        )

        assert response.status_code == 400

    def test_secrets_are_never_returned(self, client: TestClient, fake_keyring) -> None:
        client.put("/api/settings/secrets", json={"key": "anthropic", "value": "sk-secret-value"})

        body = client.get("/api/settings").json()

        assert "sk-secret-value" not in str(body)
        assert body["keys_present"]["anthropic"] is True

    def test_empty_secret_is_rejected(self, client: TestClient, fake_keyring) -> None:
        response = client.put("/api/settings/secrets", json={"key": "anthropic", "value": "   "})

        assert response.status_code == 400

    def test_unknown_secret_key_is_rejected(self, client: TestClient) -> None:
        response = client.put("/api/settings/secrets", json={"key": "aws", "value": "x"})

        assert response.status_code == 400

    def test_secret_can_be_deleted(self, client: TestClient, fake_keyring) -> None:
        client.put("/api/settings/secrets", json={"key": "openai", "value": "sk-x"})
        client.delete("/api/settings/secrets/openai")

        assert client.get("/api/settings").json()["keys_present"]["openai"] is False


class TestSources:
    def test_empty_initially(self, client: TestClient) -> None:
        assert client.get("/api/sources").json() == []

    def test_lists_a_created_source(self, client: TestClient, source: Source) -> None:
        body = client.get("/api/sources").json()

        assert len(body) == 1
        assert body[0]["title"] == "Test source"

    def test_never_exposes_the_filesystem_path(self, client: TestClient, source: Source) -> None:
        body = client.get(f"/api/sources/{source.id}").json()

        assert "path" not in body
        assert "C:/media" not in str(body)

    def test_missing_source_is_404(self, client: TestClient) -> None:
        assert client.get("/api/sources/nope").status_code == 404

    def test_non_youtube_url_is_rejected(self, client: TestClient) -> None:
        response = client.post("/api/sources/youtube", json={"url": "https://vimeo.com/12345"})

        assert response.status_code == 400

    def test_unsupported_upload_type_is_rejected(self, client: TestClient) -> None:
        response = client.post(
            "/api/sources/upload",
            files={"file": ("notes.txt", b"hello", "text/plain")},
        )

        assert response.status_code == 415
        assert "supported" in response.json()["detail"]["message"]


class TestJobs:
    def test_create_and_fetch(self, client: TestClient, source: Source) -> None:
        created = client.post("/api/jobs", json={"source_id": source.id})

        assert created.status_code == 201
        job_id = created.json()["id"]

        fetched = client.get(f"/api/jobs/{job_id}").json()
        assert fetched["status"] == "queued"
        assert fetched["source"]["title"] == "Test source"

    def test_overrides_are_applied(self, client: TestClient, source: Source) -> None:
        response = client.post(
            "/api/jobs",
            json={
                "source_id": source.id,
                "settings": {"provider": "ollama", "max_clips": 3},
            },
        )

        assert response.json()["provider"] == "ollama"

    def test_missing_source_is_404(self, client: TestClient) -> None:
        response = client.post("/api/jobs", json={"source_id": "nope"})

        assert response.status_code == 404

    def test_inverted_durations_are_rejected(self, client: TestClient, source: Source) -> None:
        response = client.post(
            "/api/jobs",
            json={
                "source_id": source.id,
                "settings": {"min_duration_s": 90, "max_duration_s": 20},
            },
        )

        assert response.status_code == 400

    def test_cancel_a_queued_job(self, client: TestClient, source: Source) -> None:
        job_id = client.post("/api/jobs", json={"source_id": source.id}).json()["id"]

        response = client.post(f"/api/jobs/{job_id}/cancel")

        assert response.status_code == 200
        assert client.get(f"/api/jobs/{job_id}").json()["status"] == "cancelled"

    def test_cancelling_a_finished_job_conflicts(self, client: TestClient, source: Source) -> None:
        job = store.create_job(Job(id=new_id(), source_id=source.id, status="done"))

        assert client.post(f"/api/jobs/{job.id}/cancel").status_code == 409

    def test_retry_requeues_a_failed_job(self, client: TestClient, source: Source) -> None:
        job = store.create_job(Job(id=new_id(), source_id=source.id, status="failed", error="boom"))

        response = client.post(f"/api/jobs/{job.id}/retry")

        assert response.status_code == 200
        assert response.json()["status"] == "queued"
        assert response.json()["error"] is None

    def test_retrying_a_running_job_conflicts(self, client: TestClient, source: Source) -> None:
        job = store.create_job(Job(id=new_id(), source_id=source.id, status="running"))

        assert client.post(f"/api/jobs/{job.id}/retry").status_code == 409

    def test_missing_job_is_404(self, client: TestClient) -> None:
        assert client.get("/api/jobs/nope").status_code == 404
        assert client.post("/api/jobs/nope/cancel").status_code == 404
        assert client.post("/api/jobs/nope/retry").status_code == 404


class TestClips:
    def test_list_clips_for_a_job(self, client: TestClient, job_with_clips: Job) -> None:
        clips = client.get(f"/api/jobs/{job_with_clips.id}/clips").json()

        assert len(clips) == 3
        assert [c["rank"] for c in clips] == [1, 2, 3]
        assert clips[0]["duration_s"] == pytest.approx(40.0)

    def test_patch_title_and_status(self, client: TestClient, job_with_clips: Job) -> None:
        clip_id = client.get(f"/api/jobs/{job_with_clips.id}/clips").json()[0]["id"]

        response = client.patch(
            f"/api/clips/{clip_id}", json={"title": "Renamed", "status": "kept"}
        )

        assert response.status_code == 200
        assert response.json()["title"] == "Renamed"
        assert response.json()["status"] == "kept"

    def test_inverted_trim_is_rejected(self, client: TestClient, job_with_clips: Job) -> None:
        clip_id = client.get(f"/api/jobs/{job_with_clips.id}/clips").json()[0]["id"]

        response = client.patch(f"/api/clips/{clip_id}", json={"start_s": 90.0, "end_s": 30.0})

        assert response.status_code == 400

    def test_caption_style_is_persisted(self, client: TestClient, job_with_clips: Job) -> None:
        clip_id = client.get(f"/api/jobs/{job_with_clips.id}/clips").json()[0]["id"]

        response = client.patch(
            f"/api/clips/{clip_id}/captions", json={"caption_style": "karaoke_fill"}
        )

        assert response.status_code == 200
        assert response.json()["caption_style"] == "karaoke_fill"

    def test_framing_corrections_are_persisted(
        self, client: TestClient, job_with_clips: Job
    ) -> None:
        clip_id = client.get(f"/api/jobs/{job_with_clips.id}/clips").json()[0]["id"]

        response = client.patch(
            f"/api/clips/{clip_id}/captions",
            json={
                "crop_nudge": 0.5,
                "centre_crop": True,
                "pip_enabled": True,
                "pip_corner": "bl",
                "pip_size": 0.4,
            },
        )

        assert response.status_code == 200
        body = response.json()
        assert body["crop_nudge"] == pytest.approx(0.5)
        assert body["centre_crop"] is True
        assert body["pip_enabled"] is True
        assert body["pip_corner"] == "bl"
        assert body["pip_size"] == pytest.approx(0.4)

    def test_out_of_range_nudge_is_rejected(self, client: TestClient, job_with_clips: Job) -> None:
        clip_id = client.get(f"/api/jobs/{job_with_clips.id}/clips").json()[0]["id"]

        response = client.patch(f"/api/clips/{clip_id}/captions", json={"crop_nudge": 2.0})

        assert response.status_code == 422

    def test_facecam_is_404_before_detection(self, client: TestClient, job_with_clips: Job) -> None:
        clip_id = client.get(f"/api/jobs/{job_with_clips.id}/clips").json()[0]["id"]

        assert client.get(f"/api/clips/{clip_id}/facecam").status_code == 404

    def test_unknown_caption_style_is_rejected(
        self, client: TestClient, job_with_clips: Job
    ) -> None:
        clip_id = client.get(f"/api/jobs/{job_with_clips.id}/clips").json()[0]["id"]

        response = client.patch(
            f"/api/clips/{clip_id}/captions", json={"caption_style": "explosion"}
        )

        assert response.status_code == 400

    def test_missing_clip_is_404(self, client: TestClient) -> None:
        assert client.get("/api/clips/nope").status_code == 404
        assert client.patch("/api/clips/nope", json={"title": "x"}).status_code == 404
        assert client.get("/api/clips/nope/crop-path").status_code == 404

    def test_crop_path_is_404_before_reframe(self, client: TestClient, job_with_clips: Job) -> None:
        # The review player treats this as "fall back to a centre crop", which
        # is what the renderer does for these clips too.
        clip_id = client.get(f"/api/jobs/{job_with_clips.id}/clips").json()[0]["id"]

        assert client.get(f"/api/clips/{clip_id}/crop-path").status_code == 404

    def test_crop_path_is_served_when_present(
        self, client: TestClient, job_with_clips: Job, autoclip_home
    ) -> None:
        from autoclip.pipeline.reframe.croppath import centre_crop
        from autoclip.pipeline.runner import JobWorkspace

        clip = client.get(f"/api/jobs/{job_with_clips.id}/clips").json()[0]
        path = centre_crop(1920, 1080, clip["duration_s"])
        path.save(JobWorkspace(job_with_clips.id).crop_path(clip["id"]))

        body = client.get(f"/api/clips/{clip['id']}/crop-path").json()

        assert body["source_width"] == 1920
        assert body["source_height"] == 1080
        assert len(body["segments"]) == 1
        # The client needs these to position the video; a missing field means a
        # silently centre-cropped preview that disagrees with the export.
        segment = body["segments"][0]
        assert {"start_s", "end_s", "width", "height", "keyframes", "fit"} <= segment.keys()

    def test_words_need_a_transcript(self, client: TestClient, job_with_clips: Job) -> None:
        # The job has clips but no transcript on disk, which is exactly the
        # state a partially-restored workspace would be in.
        clip_id = client.get(f"/api/jobs/{job_with_clips.id}/clips").json()[0]["id"]

        assert client.get(f"/api/clips/{clip_id}/words").status_code == 404


class TestExportFilenames:
    """Two clips may share a title; their renders may not share a file."""

    TITLE = "Opening stretch"

    @pytest.fixture
    def same_titled_clips(self, source: Source) -> Job:
        job = store.create_job(Job(id=new_id(), source_id=source.id, status="done"))
        store.replace_clips(
            job.id,
            [
                Clip(
                    id=new_id(),
                    job_id=job.id,
                    rank=rank,
                    start_s=(rank - 1) * 10.0,
                    end_s=(rank - 1) * 10.0 + 5.0,
                    start_word=(rank - 1) * 10,
                    end_word=(rank - 1) * 10 + 9,
                    title=self.TITLE,
                )
                for rank in (1, 2)
            ],
        )
        _write_transcript(job.id, count=40)
        return job

    @pytest.fixture
    def rendered(self, monkeypatch: pytest.MonkeyPatch) -> list[Path]:
        """Replace the renderer with one that just writes a byte.

        This class is about where the file lands, not what is inside it. A real
        encode here would add minutes to the default suite and test ffmpeg
        rather than the filename derivation these tests exist for.
        """
        written: list[Path] = []

        def fake_render(request, **_kwargs):
            request.destination.parent.mkdir(parents=True, exist_ok=True)
            request.destination.write_bytes(b"fake render")
            written.append(request.destination)
            return request.destination

        monkeypatch.setattr(clips_module.export_module, "export_clip", fake_render)
        return written

    def test_two_same_titled_clips_write_two_files(
        self, client: TestClient, same_titled_clips: Job, rendered: list[Path]
    ) -> None:
        clips = client.get(f"/api/jobs/{same_titled_clips.id}/clips").json()

        responses = [
            client.post(f"/api/clips/{clip['id']}/export", json={"ratio": "9:16"})
            for clip in clips
        ]

        assert [r.status_code for r in responses] == [201, 201]
        assert len(rendered) == 2
        assert len(set(rendered)) == 2, "both clips wrote the same file"
        assert all(path.exists() for path in rendered)

    def test_each_clip_row_points_at_its_own_render(
        self, client: TestClient, same_titled_clips: Job, rendered: list[Path]
    ) -> None:
        clips = client.get(f"/api/jobs/{same_titled_clips.id}/clips").json()
        responses = [
            client.post(f"/api/clips/{clip['id']}/export", json={"ratio": "9:16"})
            for clip in clips
        ]

        for response, path in zip(responses, rendered, strict=True):
            record = store.get_export(response.json()["id"])

            assert record is not None
            # The bug this closes: B's render overwrote A's file while A's row
            # kept pointing at a path that now held someone else's video.
            assert Path(record.path) == path

        assert [store.get_export(r.json()["id"]).clip_id for r in responses] == [
            clip["id"] for clip in clips
        ]

    def test_re_exporting_a_clip_keeps_overwriting_itself(
        self, client: TestClient, same_titled_clips: Job, rendered: list[Path]
    ) -> None:
        clip = client.get(f"/api/jobs/{same_titled_clips.id}/clips").json()[0]

        first = client.post(f"/api/clips/{clip['id']}/export", json={"ratio": "9:16"})
        second = client.post(f"/api/clips/{clip['id']}/export", json={"ratio": "9:16"})

        assert Path(store.get_export(first.json()["id"]).path) == Path(
            store.get_export(second.json()["id"]).path
        )

    def test_a_disambiguated_clip_is_stable_across_re_exports(
        self, client: TestClient, same_titled_clips: Job, rendered: list[Path]
    ) -> None:
        # The sibling was rendered first, so this clip is the one that gets a
        # variant. It must keep that variant on every later export rather than
        # drifting between names.
        clips = client.get(f"/api/jobs/{same_titled_clips.id}/clips").json()
        client.post(f"/api/clips/{clips[0]['id']}/export", json={"ratio": "9:16"})

        first = client.post(f"/api/clips/{clips[1]['id']}/export", json={"ratio": "9:16"})
        second = client.post(f"/api/clips/{clips[1]['id']}/export", json={"ratio": "9:16"})

        assert Path(store.get_export(first.json()["id"]).path) == Path(
            store.get_export(second.json()["id"]).path
        )
        assert len(set(rendered)) == 2

    def test_a_different_ratio_still_gets_its_own_file(
        self, client: TestClient, same_titled_clips: Job, rendered: list[Path]
    ) -> None:
        clip_id = client.get(f"/api/jobs/{same_titled_clips.id}/clips").json()[0]["id"]

        client.post(f"/api/clips/{clip_id}/export", json={"ratio": "9:16"})
        client.post(f"/api/clips/{clip_id}/export", json={"ratio": "1:1"})

        assert len(set(rendered)) == 2


class TestCaptionOverrides:
    """Per-clip styling: storage, merge semantics, and the resolved snapshot."""

    @pytest.fixture
    def clip_id(self, client: TestClient, job_with_clips: Job) -> str:
        return client.get(f"/api/jobs/{job_with_clips.id}/clips").json()[0]["id"]

    def test_overrides_are_persisted_and_returned(
        self, client: TestClient, clip_id: str
    ) -> None:
        response = client.patch(
            f"/api/clips/{clip_id}/captions",
            json={
                "style_overrides": {
                    "font": "Inter",
                    "size_ratio": 0.08,
                    "primary": "#112233",
                    "max_words": 2,
                    "animation": "none",
                }
            },
        )

        assert response.status_code == 200
        overrides = response.json()["style_overrides"]
        assert overrides["font"] == "Inter"
        assert overrides["primary"] == "#112233"
        assert overrides["max_words"] == 2

    def test_a_partial_patch_leaves_other_overrides_alone(
        self, client: TestClient, clip_id: str
    ) -> None:
        client.patch(
            f"/api/clips/{clip_id}/captions",
            json={"style_overrides": {"font": "Inter", "max_words": 2}},
        )

        response = client.patch(
            f"/api/clips/{clip_id}/captions", json={"caption_x_ratio": 0.2}
        )

        assert response.json()["style_overrides"] == {"font": "Inter", "max_words": 2}
        assert response.json()["caption_x_ratio"] == pytest.approx(0.2)

    def test_an_absent_key_leaves_the_stored_blob_unchanged(
        self, client: TestClient, clip_id: str
    ) -> None:
        client.patch(
            f"/api/clips/{clip_id}/captions",
            json={"style_overrides": {"font": "Inter", "boxed": True}},
        )

        response = client.patch(f"/api/clips/{clip_id}/captions", json={"ratio": "1:1"})

        assert response.json()["style_overrides"] == {"font": "Inter", "boxed": True}

    def test_null_clears_the_stored_blob(self, client: TestClient, clip_id: str) -> None:
        client.patch(
            f"/api/clips/{clip_id}/captions", json={"style_overrides": {"font": "Inter"}}
        )

        response = client.patch(
            f"/api/clips/{clip_id}/captions", json={"style_overrides": None}
        )

        assert response.json()["style_overrides"] is None

    def test_an_empty_blob_is_the_same_as_clearing(self, client: TestClient, clip_id: str) -> None:
        client.patch(
            f"/api/clips/{clip_id}/captions", json={"style_overrides": {"font": "Inter"}}
        )

        response = client.patch(f"/api/clips/{clip_id}/captions", json={"style_overrides": {}})

        assert response.json()["style_overrides"] is None

    def test_null_clears_a_geometry_ratio_to_the_preset_default(
        self, client: TestClient, clip_id: str
    ) -> None:
        client.patch(
            f"/api/clips/{clip_id}/captions",
            json={"caption_margin_ratio": 0.1, "caption_x_ratio": 0.2},
        )

        response = client.patch(
            f"/api/clips/{clip_id}/captions",
            json={"caption_margin_ratio": None, "caption_x_ratio": None},
        )

        assert response.json()["caption_margin_ratio"] is None
        assert response.json()["caption_x_ratio"] is None

    def test_an_unknown_font_is_a_400(self, client: TestClient, clip_id: str) -> None:
        response = client.patch(
            f"/api/clips/{clip_id}/captions",
            json={"style_overrides": {"font": "Comic Sans MS"}},
        )

        assert response.status_code == 400
        assert "Unknown font" in response.json()["detail"]

    def test_out_of_range_numbers_are_clamped_not_rejected(
        self, client: TestClient, clip_id: str
    ) -> None:
        # Contract §8.4: every stored value is clamped. A stale client must be
        # corrected, never wedged out of the editor with a 422.
        response = client.patch(
            f"/api/clips/{clip_id}/captions",
            json={
                "style_overrides": {
                    "size_ratio": 0.9,
                    "max_words": 99,
                    "outline_width": -3,
                    "box_alpha": 900,
                },
                "caption_x_ratio": 5.0,
                "caption_margin_ratio": -1.0,
            },
        )

        assert response.status_code == 200
        body = response.json()
        assert body["style_overrides"]["size_ratio"] == pytest.approx(0.12)
        assert body["style_overrides"]["max_words"] == 8
        assert body["style_overrides"]["outline_width"] == pytest.approx(0.0)
        assert body["style_overrides"]["box_alpha"] == 255
        assert body["caption_x_ratio"] == pytest.approx(1.0)
        assert body["caption_margin_ratio"] == pytest.approx(0.0)

    def test_effective_caption_carries_the_overrides(
        self, client: TestClient, clip_id: str
    ) -> None:
        client.patch(
            f"/api/clips/{clip_id}/captions",
            json={
                "style_overrides": {
                    "font": "Inter",
                    "primary": "#112233",
                    "accent": None,
                    "max_words": 1,
                }
            },
        )

        effective = client.get(f"/api/clips/{clip_id}").json()["effective_caption"]

        assert effective["font"] == "Inter"
        assert effective["primary"] == "#112233"
        # An explicit null disables per-word highlighting rather than restoring
        # the preset's accent.
        assert effective["accent"] is None
        assert effective["maxWords"] == 1

    def test_effective_caption_reports_horizontal_alignment(
        self, client: TestClient, clip_id: str
    ) -> None:
        for x_ratio, expected in ((0.2, "left"), (0.8, "right"), (0.5, "center")):
            client.patch(
                f"/api/clips/{clip_id}/captions", json={"caption_x_ratio": x_ratio}
            )

            effective = client.get(f"/api/clips/{clip_id}").json()["effective_caption"]

            assert effective["alignment"] == expected

    def test_a_clip_without_overrides_matches_the_preset_preview(
        self, client: TestClient, clip_id: str
    ) -> None:
        # Contract §8.1: the clip snapshot and the style catalogue are the same
        # resolution, so an untouched clip must agree with the preset exactly.
        preview = next(
            s for s in client.get("/api/caption-styles").json() if s["key"] == "bold_pop"
        )["preview"]

        effective = client.get(f"/api/clips/{clip_id}").json()["effective_caption"]

        assert effective == preview

    def test_effective_caption_tracks_the_global_knobs(
        self, client: TestClient, clip_id: str
    ) -> None:
        baseline = client.get(f"/api/clips/{clip_id}").json()["effective_caption"]
        client.put("/api/settings", json={"export": {"caption_size_scale": 1.5}})

        scaled = client.get(f"/api/clips/{clip_id}").json()["effective_caption"]

        assert scaled["sizeRatio"] > baseline["sizeRatio"]


class TestProviderStatus:
    def test_reports_every_provider(self, client: TestClient, fake_keyring) -> None:
        statuses = client.get("/api/providers/status").json()

        assert {s["name"] for s in statuses} == {
            "anthropic",
            "openai",
            "gemini",
            "ollama",
        }

    def test_missing_key_is_reported_as_unavailable(self, client: TestClient, fake_keyring) -> None:
        statuses = {s["name"]: s for s in client.get("/api/providers/status").json()}

        assert statuses["anthropic"]["has_key"] is False
        assert statuses["anthropic"]["available"] is False

    def test_ollama_never_requires_a_key(self, client: TestClient) -> None:
        statuses = {s["name"]: s for s in client.get("/api/providers/status").json()}

        assert statuses["ollama"]["requires_key"] is False


class TestEventStream:
    def test_finished_job_streams_a_snapshot_and_closes(
        self, client: TestClient, source: Source
    ) -> None:
        job = store.create_job(Job(id=new_id(), source_id=source.id, status="done", progress=1.0))

        with client.stream("GET", f"/api/jobs/{job.id}/events") as response:
            assert response.status_code == 200
            body = "".join(response.iter_text())

        # A client opening the stream after completion must still learn the
        # final state rather than hanging on an empty connection.
        assert "snapshot" in body
        assert "done" in body

    def test_missing_job_is_404(self, client: TestClient) -> None:
        assert client.get("/api/jobs/nope/events").status_code == 404


class TestSecretsNeverLeak:
    def test_settings_response_has_no_secret_values(self, client: TestClient, fake_keyring) -> None:
        for key in ("anthropic", "openai", "gemini"):
            client.put("/api/settings/secrets", json={"key": key, "value": f"sk-{key}-DEADBEEF"})

        payload = str(client.get("/api/settings").json())

        assert "DEADBEEF" not in payload

    def test_config_file_has_no_secrets_when_keyring_works(
        self, client: TestClient, fake_keyring, autoclip_home
    ) -> None:
        client.put("/api/settings/secrets", json={"key": "anthropic", "value": "sk-DEADBEEF"})
        client.put("/api/settings", json={"whisper": {"model": "medium"}})

        from autoclip import paths

        assert "DEADBEEF" not in paths.config_path().read_text(encoding="utf-8")
        assert config.get_secret("anthropic") == "sk-DEADBEEF"


class TestFonts:
    def test_lists_the_registry(self, client: TestClient) -> None:
        fonts = client.get("/api/fonts").json()

        assert len(fonts) >= 5
        assert all({"key", "label"} == set(f.keys()) for f in fonts)

    def test_includes_the_display_faces(self, client: TestClient) -> None:
        keys = {f["key"] for f in client.get("/api/fonts").json()}

        assert {"Anton", "Inter", "Archivo Black", "Bebas Neue", "Lilita One"} <= keys

    def test_every_listed_font_is_usable(self, client: TestClient, job_with_clips: Job) -> None:
        clip_id = client.get(f"/api/jobs/{job_with_clips.id}/clips").json()[0]["id"]

        for font in client.get("/api/fonts").json():
            response = client.patch(
                f"/api/clips/{clip_id}/captions",
                json={"style_overrides": {"font": font["key"]}},
            )

            assert response.status_code == 200, font
            assert response.json()["style_overrides"]["font"] == font["key"]


class TestPresets:
    """Contract §4: CRUD, apply, and the single default."""

    @staticmethod
    def _create(client: TestClient, **overrides):
        payload = {
            "name": "On brand",
            "base_style": "bold_pop",
            "style_overrides": {"font": "Bebas Neue", "max_words": 2},
            "layout": {"caption_margin_ratio": 0.15, "caption_x_ratio": 0.25},
        }
        payload.update(overrides)
        return client.post("/api/presets", json=payload)

    def test_empty_initially(self, client: TestClient) -> None:
        assert client.get("/api/presets").json() == []

    def test_create_returns_201_with_the_preset(self, client: TestClient) -> None:
        response = self._create(client)

        assert response.status_code == 201
        body = response.json()
        assert body["name"] == "On brand"
        assert body["base_style"] == "bold_pop"
        assert body["style_overrides"]["font"] == "Bebas Neue"
        assert body["layout"]["caption_x_ratio"] == pytest.approx(0.25)
        assert body["is_default"] is False

    def test_a_duplicate_name_conflicts(self, client: TestClient) -> None:
        self._create(client)

        assert self._create(client).status_code == 409

    def test_names_are_trimmed_so_whitespace_does_not_make_a_twin(
        self, client: TestClient
    ) -> None:
        self._create(client, name="On brand")

        assert self._create(client, name="  On brand  ").status_code == 409

    def test_a_blank_name_is_rejected(self, client: TestClient) -> None:
        assert self._create(client, name="   ").status_code in (400, 422)

    def test_an_unknown_base_style_is_a_400(self, client: TestClient) -> None:
        response = self._create(client, base_style="neon-explosion")

        assert response.status_code == 400
        assert "Unknown caption style" in response.json()["detail"]

    def test_an_unknown_font_is_a_400(self, client: TestClient) -> None:
        response = self._create(client, style_overrides={"font": "Comic Sans MS"})

        assert response.status_code == 400
        assert "Unknown font" in response.json()["detail"]

    def test_out_of_range_geometry_is_clamped_not_rejected(self, client: TestClient) -> None:
        response = self._create(
            client, layout={"caption_margin_ratio": 9.0, "caption_x_ratio": -2.0}
        )

        assert response.status_code == 201
        layout = response.json()["layout"]
        assert layout["caption_margin_ratio"] == pytest.approx(0.8)
        assert layout["caption_x_ratio"] == pytest.approx(0.0)

    def test_the_default_lists_first(self, client: TestClient) -> None:
        self._create(client, name="Alpha").json()
        second = self._create(client, name="Beta").json()
        client.put(f"/api/presets/{second['id']}/default")

        listed = client.get("/api/presets").json()

        assert [p["name"] for p in listed] == ["Beta", "Alpha"]
        assert listed[0]["is_default"] is True

    def test_setting_a_default_clears_the_previous_one(self, client: TestClient) -> None:
        first = self._create(client, name="Alpha").json()
        second = self._create(client, name="Beta").json()

        client.put(f"/api/presets/{first['id']}/default")
        client.put(f"/api/presets/{second['id']}/default")

        presets = {p["id"]: p for p in client.get("/api/presets").json()}
        assert presets[first["id"]]["is_default"] is False
        assert presets[second["id"]]["is_default"] is True

    def test_defaulting_a_missing_preset_is_404(self, client: TestClient) -> None:
        assert client.put("/api/presets/nope/default").status_code == 404

    def test_partial_update_leaves_other_fields_alone(self, client: TestClient) -> None:
        created = self._create(client).json()

        response = client.put(f"/api/presets/{created['id']}", json={"name": "Renamed"})

        assert response.status_code == 200
        body = response.json()
        assert body["name"] == "Renamed"
        assert body["style_overrides"]["font"] == "Bebas Neue"
        assert body["layout"]["caption_x_ratio"] == pytest.approx(0.25)

    def test_update_can_clear_a_blob(self, client: TestClient) -> None:
        created = self._create(client).json()

        response = client.put(f"/api/presets/{created['id']}", json={"style_overrides": None})

        assert response.json()["style_overrides"] is None
        assert response.json()["layout"] is not None

    def test_renaming_onto_a_taken_name_conflicts(self, client: TestClient) -> None:
        self._create(client, name="Alpha")
        beta = self._create(client, name="Beta").json()

        response = client.put(f"/api/presets/{beta['id']}", json={"name": "Alpha"})

        assert response.status_code == 409

    def test_update_rejects_an_unknown_base_style(self, client: TestClient) -> None:
        created = self._create(client).json()

        response = client.put(
            f"/api/presets/{created['id']}", json={"base_style": "neon-explosion"}
        )

        assert response.status_code == 400

    def test_updating_a_missing_preset_is_404(self, client: TestClient) -> None:
        assert client.put("/api/presets/nope", json={"name": "x"}).status_code == 404

    def test_delete_is_204_then_404(self, client: TestClient) -> None:
        created = self._create(client).json()

        assert client.delete(f"/api/presets/{created['id']}").status_code == 204
        assert client.delete(f"/api/presets/{created['id']}").status_code == 404

    def test_apply_reproduces_the_style_and_layout(
        self, client: TestClient, job_with_clips: Job
    ) -> None:
        preset = self._create(client).json()
        clip_id = client.get(f"/api/jobs/{job_with_clips.id}/clips").json()[0]["id"]

        response = client.post(f"/api/presets/{preset['id']}/apply", json={"clip_id": clip_id})

        assert response.status_code == 200
        body = response.json()
        assert body["caption_style"] == "bold_pop"
        assert body["style_overrides"] == {"font": "Bebas Neue", "max_words": 2}
        assert body["caption_margin_ratio"] == pytest.approx(0.15)
        assert body["caption_x_ratio"] == pytest.approx(0.25)

    def test_apply_updates_the_published_snapshot(
        self, client: TestClient, job_with_clips: Job
    ) -> None:
        # Contract §8.1: what the preview draws must be what the renderer burns,
        # and applying a preset is exactly the moment they could diverge.
        preset = self._create(client).json()
        clip_id = client.get(f"/api/jobs/{job_with_clips.id}/clips").json()[0]["id"]

        client.post(f"/api/presets/{preset['id']}/apply", json={"clip_id": clip_id})

        effective = client.get(f"/api/clips/{clip_id}").json()["effective_caption"]
        assert effective["font"] == "Bebas Neue"
        assert effective["maxWords"] == 2
        assert effective["alignment"] == "left"
        assert effective["xRatio"] == pytest.approx(0.25)
        assert effective["margin_ratio"] == pytest.approx(0.15)

    def test_apply_to_a_missing_clip_is_404(
        self, client: TestClient, job_with_clips: Job
    ) -> None:
        preset = self._create(client).json()

        response = client.post(f"/api/presets/{preset['id']}/apply", json={"clip_id": "nope"})

        assert response.status_code == 404

    def test_applying_a_missing_preset_is_404(
        self, client: TestClient, job_with_clips: Job
    ) -> None:
        clip_id = client.get(f"/api/jobs/{job_with_clips.id}/clips").json()[0]["id"]

        response = client.post("/api/presets/nope/apply", json={"clip_id": clip_id})

        assert response.status_code == 404

    def test_deleting_a_preset_leaves_the_clip_styled(
        self, client: TestClient, job_with_clips: Job
    ) -> None:
        preset = self._create(client).json()
        clip_id = client.get(f"/api/jobs/{job_with_clips.id}/clips").json()[0]["id"]
        client.post(f"/api/presets/{preset['id']}/apply", json={"clip_id": clip_id})

        client.delete(f"/api/presets/{preset['id']}")

        clip = client.get(f"/api/clips/{clip_id}").json()
        assert clip["style_overrides"] == {"font": "Bebas Neue", "max_words": 2}
        assert clip["caption_x_ratio"] == pytest.approx(0.25)

    def test_editing_a_preset_leaves_an_applied_clip_alone(
        self, client: TestClient, job_with_clips: Job
    ) -> None:
        preset = self._create(client).json()
        clip_id = client.get(f"/api/jobs/{job_with_clips.id}/clips").json()[0]["id"]
        client.post(f"/api/presets/{preset['id']}/apply", json={"clip_id": clip_id})

        client.put(
            f"/api/presets/{preset['id']}",
            json={"style_overrides": {"font": "Inter", "max_words": 8}},
        )

        clip = client.get(f"/api/clips/{clip_id}").json()
        assert clip["style_overrides"] == {"font": "Bebas Neue", "max_words": 2}

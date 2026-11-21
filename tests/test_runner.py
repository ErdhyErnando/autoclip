"""Runner behaviour that does not need the real pipeline.

The full pipeline lives in ``test_pipeline_e2e.py`` behind the ``slow`` and
``e2e`` markers. What is tested here is the wiring that decides what a *fresh*
job's clips look like before anything is rendered — the part a user notices the
moment they open the review page.
"""

from __future__ import annotations

import pytest
from autoclip.db import store
from autoclip.db.models import Clip, Job, Preset, Source, new_id
from autoclip.pipeline.runner import PipelineRunner


@pytest.fixture
def job_and_source(initialised_db: int) -> tuple[Job, Source]:
    source = store.create_source(
        Source(
            id=new_id(),
            type="upload",
            path="C:/media/video.mp4",
            title="Test source",
            duration_s=600.0,
            width=1920,
            height=1080,
        )
    )
    job = store.create_job(Job(id=new_id(), source_id=source.id, status="running"))
    return job, source


@pytest.fixture
def clips(job_and_source: tuple[Job, Source]) -> list[Clip]:
    job, _ = job_and_source
    rows = [
        Clip(id=new_id(), job_id=job.id, rank=rank, start_s=rank * 10.0, end_s=rank * 10.0 + 5.0)
        for rank in (1, 2)
    ]
    store.replace_clips(job.id, rows)
    return rows


class TestDefaultPreset:
    """Contract §4: a default preset styles new clips; no default changes nothing."""

    @staticmethod
    def _runner(job_and_source) -> PipelineRunner:
        job, source = job_and_source
        return PipelineRunner(job, source)

    def test_without_a_default_no_edit_is_written(
        self, job_and_source, clips: list[Clip]
    ) -> None:
        runner = self._runner(job_and_source)

        runner._apply_default_preset(clips)

        # No row at all, rather than a row full of defaults: behaviour has to be
        # byte-identical to the pre-preset pipeline.
        assert all(store.get_clip_edit(clip.id) is None for clip in clips)

    def test_a_default_preset_styles_every_new_clip(
        self, job_and_source, clips: list[Clip]
    ) -> None:
        store.create_preset(
            Preset(
                id=new_id(),
                name="On brand",
                base_style="karaoke_fill",
                style_overrides={"font": "Bebas Neue", "max_words": 2},
                layout={"caption_margin_ratio": 0.12, "caption_x_ratio": 0.2},
            )
        )
        preset = store.list_presets()[0]
        store.set_default_preset(preset.id)

        self._runner(job_and_source)._apply_default_preset(clips)

        for clip in clips:
            edit = store.get_clip_edit(clip.id)
            assert edit is not None
            assert edit.caption_style == "karaoke_fill"
            assert edit.style_overrides == {"font": "Bebas Neue", "max_words": 2}
            assert edit.caption_margin_ratio == pytest.approx(0.12)
            assert edit.caption_x_ratio == pytest.approx(0.2)

    def test_a_non_default_preset_is_not_applied(
        self, job_and_source, clips: list[Clip]
    ) -> None:
        store.create_preset(Preset(id=new_id(), name="Unused", style_overrides={"font": "Inter"}))

        self._runner(job_and_source)._apply_default_preset(clips)

        assert all(store.get_clip_edit(clip.id) is None for clip in clips)

    def test_applying_twice_is_idempotent(self, job_and_source, clips: list[Clip]) -> None:
        store.create_preset(Preset(id=new_id(), name="On brand", base_style="boxed"))
        store.set_default_preset(store.list_presets()[0].id)
        runner = self._runner(job_and_source)

        runner._apply_default_preset(clips)
        runner._apply_default_preset(clips)

        assert store.get_clip_edit(clips[0].id).caption_style == "boxed"

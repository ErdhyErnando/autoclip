"""Schema migrations and the data layer."""

from __future__ import annotations

import sqlite3

import pytest
from autoclip import db, paths
from autoclip.db import store
from autoclip.db.models import Clip, ClipEdit, Export, Job, Preset, Source, Transcript, new_id


def test_init_creates_database_at_schema_version() -> None:
    version = db.init()

    assert version == db.SCHEMA_VERSION
    assert paths.db_path().exists()


def test_init_is_idempotent() -> None:
    first = db.init()
    second = db.init()

    assert first == second == db.SCHEMA_VERSION


def test_all_tables_exist(initialised_db: int) -> None:
    with db.connection() as conn:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    names = {r["name"] for r in rows}

    assert {"sources", "jobs", "transcripts", "clips", "clip_edits", "exports", "presets"} <= names


def test_foreign_keys_are_enforced(initialised_db: int) -> None:
    job = Job(id=new_id(), source_id="does-not-exist")

    with pytest.raises(sqlite3.IntegrityError):
        store.create_job(job)


def test_newer_schema_version_is_refused(initialised_db: int) -> None:
    with db.connection() as conn:
        conn.execute(f"PRAGMA user_version = {db.SCHEMA_VERSION + 5}")

    with pytest.raises(RuntimeError, match="newer than this AutoClip build"):
        db.init()


def _make_source(**overrides) -> Source:
    defaults = {
        "id": new_id(),
        "type": "youtube",
        "path": "C:/media/video.mp4",
        "title": "Test video",
        "url": "https://youtube.com/watch?v=abc",
        "duration_s": 1800.0,
        "width": 1920,
        "height": 1080,
        "fps": 30.0,
    }
    return Source(**{**defaults, **overrides})


class TestSources:
    def test_create_and_get(self, initialised_db: int) -> None:
        source = store.create_source(_make_source())

        loaded = store.get_source(source.id)

        assert loaded is not None
        assert loaded.title == "Test video"
        assert loaded.duration_s == 1800.0
        assert loaded.has_audio is True

    def test_get_missing_returns_none(self, initialised_db: int) -> None:
        assert store.get_source("nope") is None

    def test_list_is_newest_first(self, initialised_db: int) -> None:
        old = store.create_source(
            _make_source(title="older", created_at="2026-01-01T00:00:00+00:00")
        )
        new = store.create_source(
            _make_source(title="newer", created_at="2026-06-01T00:00:00+00:00")
        )

        listed = store.list_sources()

        assert [s.id for s in listed] == [new.id, old.id]

    def test_type_is_constrained(self, initialised_db: int) -> None:
        with pytest.raises(sqlite3.IntegrityError):
            store.create_source(_make_source(type="vimeo"))


class TestJobs:
    @pytest.fixture
    def source(self, initialised_db: int) -> Source:
        return store.create_source(_make_source())

    def test_create_and_get_with_settings(self, source: Source) -> None:
        job = store.create_job(
            Job(id=new_id(), source_id=source.id, provider="anthropic", settings={"max_clips": 5})
        )

        loaded = store.get_job(job.id)

        assert loaded is not None
        assert loaded.status == "queued"
        assert loaded.settings == {"max_clips": 5}

    def test_update_patches_only_supplied_fields(self, source: Source) -> None:
        job = store.create_job(Job(id=new_id(), source_id=source.id, provider="ollama"))

        store.update_job(job.id, status="running", current_stage="transcribe", progress=0.4)

        loaded = store.get_job(job.id)
        assert loaded is not None
        assert loaded.status == "running"
        assert loaded.current_stage == "transcribe"
        assert loaded.progress == pytest.approx(0.4)
        assert loaded.provider == "ollama"  # untouched

    def test_update_refreshes_the_timestamp(self, source: Source) -> None:
        job = store.create_job(
            Job(id=new_id(), source_id=source.id, updated_at="2020-01-01T00:00:00+00:00")
        )

        store.update_job(job.id, progress=0.1)

        loaded = store.get_job(job.id)
        assert loaded is not None
        assert loaded.updated_at != "2020-01-01T00:00:00+00:00"

    def test_next_queued_is_fifo(self, source: Source) -> None:
        first = store.create_job(
            Job(id=new_id(), source_id=source.id, created_at="2026-01-01T00:00:00+00:00")
        )
        store.create_job(
            Job(id=new_id(), source_id=source.id, created_at="2026-02-01T00:00:00+00:00")
        )

        assert store.next_queued_job().id == first.id

        store.update_job(first.id, status="running")
        assert store.next_queued_job().id != first.id

    def test_deleting_a_source_cascades_to_jobs(self, source: Source) -> None:
        job = store.create_job(Job(id=new_id(), source_id=source.id))

        with db.connection() as conn:
            conn.execute("DELETE FROM sources WHERE id = ?", (source.id,))

        assert store.get_job(job.id) is None

    def test_status_is_constrained(self, source: Source) -> None:
        with pytest.raises(sqlite3.IntegrityError):
            store.create_job(Job(id=new_id(), source_id=source.id, status="banana"))


class TestClips:
    @pytest.fixture
    def job(self, initialised_db: int) -> Job:
        source = store.create_source(_make_source())
        return store.create_job(Job(id=new_id(), source_id=source.id))

    def _clip(self, job: Job, rank: int, **overrides) -> Clip:
        defaults = {
            "id": new_id(),
            "job_id": job.id,
            "rank": rank,
            "start_s": 10.0 * rank,
            "end_s": 10.0 * rank + 45.0,
            "start_word": 100 * rank,
            "end_word": 100 * rank + 80,
            "title": f"Clip {rank}",
            "score": 90 - rank,
        }
        return Clip(**{**defaults, **overrides})

    def test_replace_clips_orders_by_rank(self, job: Job) -> None:
        store.replace_clips(job.id, [self._clip(job, 2), self._clip(job, 1), self._clip(job, 3)])

        listed = store.list_clips(job.id)

        assert [c.rank for c in listed] == [1, 2, 3]

    def test_replace_clips_discards_the_previous_run(self, job: Job) -> None:
        store.replace_clips(job.id, [self._clip(job, 1), self._clip(job, 2)])
        store.replace_clips(job.id, [self._clip(job, 1, title="Only survivor")])

        listed = store.list_clips(job.id)

        assert len(listed) == 1
        assert listed[0].title == "Only survivor"

    def test_duration_is_derived(self, job: Job) -> None:
        store.replace_clips(job.id, [self._clip(job, 1, start_s=12.5, end_s=57.5)])

        assert store.list_clips(job.id)[0].duration_s == pytest.approx(45.0)

    def test_update_clip_records_a_trim(self, job: Job) -> None:
        clip = self._clip(job, 1)
        store.replace_clips(job.id, [clip])

        store.update_clip(clip.id, start_s=15.0, end_s=50.0, user_trimmed=True, status="kept")

        loaded = store.get_clip(clip.id)
        assert loaded is not None
        assert loaded.start_s == 15.0
        assert loaded.user_trimmed is True
        assert loaded.status == "kept"

    def test_update_clip_with_no_fields_is_a_noop(self, job: Job) -> None:
        clip = self._clip(job, 1)
        store.replace_clips(job.id, [clip])

        store.update_clip(clip.id)

        assert store.get_clip(clip.id).title == "Clip 1"

    def test_clip_status_is_constrained(self, job: Job) -> None:
        with pytest.raises(sqlite3.IntegrityError):
            store.replace_clips(job.id, [self._clip(job, 1, status="maybe")])


class TestTranscriptsEditsAndExports:
    @pytest.fixture
    def job(self, initialised_db: int) -> Job:
        source = store.create_source(_make_source())
        return store.create_job(Job(id=new_id(), source_id=source.id))

    def test_transcript_upsert_overwrites(self, job: Job) -> None:
        store.upsert_transcript(
            Transcript(job_id=job.id, json_path="a.json", model="small", word_count=100)
        )
        store.upsert_transcript(
            Transcript(
                job_id=job.id,
                json_path="b.json",
                model="large-v3",
                word_count=250,
                has_diarization=True,
            )
        )

        loaded = store.get_transcript(job.id)

        assert loaded is not None
        assert loaded.json_path == "b.json"
        assert loaded.model == "large-v3"
        assert loaded.has_diarization is True

    def test_clip_edit_round_trips_word_edits(self, job: Job) -> None:
        clip = Clip(id=new_id(), job_id=job.id, start_s=0.0, end_s=30.0)
        store.replace_clips(job.id, [clip])

        words = [{"word": "Hello", "start": 0.0, "end": 0.4}]
        store.upsert_clip_edit(
            ClipEdit(clip_id=clip.id, edited_words=words, caption_style="karaoke_fill")
        )

        loaded = store.get_clip_edit(clip.id)
        assert loaded is not None
        assert loaded.edited_words == words
        assert loaded.caption_style == "karaoke_fill"

    def test_clip_edit_round_trips_framing_corrections(self, job: Job) -> None:
        clip = Clip(id=new_id(), job_id=job.id, start_s=0.0, end_s=30.0)
        store.replace_clips(job.id, [clip])

        store.upsert_clip_edit(ClipEdit(clip_id=clip.id, crop_nudge=0.5, centre_crop=True))

        loaded = store.get_clip_edit(clip.id)
        assert loaded is not None
        assert loaded.crop_nudge == pytest.approx(0.5)
        assert loaded.centre_crop is True

        # A later edit that only touches words must preserve the framing.
        store.upsert_clip_edit(
            ClipEdit(
                clip_id=clip.id,
                edited_words=[{"word": "Hi", "start": 0.0, "end": 0.4}],
                crop_nudge=loaded.crop_nudge,
                centre_crop=loaded.centre_crop,
            )
        )
        reloaded = store.get_clip_edit(clip.id)
        assert reloaded is not None
        assert reloaded.crop_nudge == pytest.approx(0.5)
        assert reloaded.centre_crop is True

    def test_clip_edit_round_trips_style_overrides(self, job: Job) -> None:
        clip = Clip(id=new_id(), job_id=job.id, start_s=0.0, end_s=30.0)
        store.replace_clips(job.id, [clip])
        overrides = {"font": "Anton", "size_ratio": 0.08, "accent": None, "max_words": 2}

        store.upsert_clip_edit(ClipEdit(clip_id=clip.id, style_overrides=overrides))

        loaded = store.get_clip_edit(clip.id)
        assert loaded is not None
        assert loaded.style_overrides == overrides

    def test_clip_edit_round_trips_caption_geometry(self, job: Job) -> None:
        clip = Clip(id=new_id(), job_id=job.id, start_s=0.0, end_s=30.0)
        store.replace_clips(job.id, [clip])

        store.upsert_clip_edit(
            ClipEdit(clip_id=clip.id, caption_margin_ratio=0.12, caption_x_ratio=0.2)
        )

        loaded = store.get_clip_edit(clip.id)
        assert loaded is not None
        assert loaded.caption_margin_ratio == pytest.approx(0.12)
        assert loaded.caption_x_ratio == pytest.approx(0.2)

    def test_an_edit_without_styling_reads_back_as_null(self, job: Job) -> None:
        clip = Clip(id=new_id(), job_id=job.id, start_s=0.0, end_s=30.0)
        store.replace_clips(job.id, [clip])

        store.upsert_clip_edit(ClipEdit(clip_id=clip.id))

        loaded = store.get_clip_edit(clip.id)
        assert loaded is not None
        # Null, not an empty blob: there is one representation of "no overrides",
        # and the resolver treats both the same way.
        assert loaded.style_overrides is None
        assert loaded.caption_margin_ratio is None
        assert loaded.caption_x_ratio is None

    def test_clearing_styling_persists_the_clear(self, job: Job) -> None:
        clip = Clip(id=new_id(), job_id=job.id, start_s=0.0, end_s=30.0)
        store.replace_clips(job.id, [clip])
        store.upsert_clip_edit(
            ClipEdit(
                clip_id=clip.id,
                style_overrides={"font": "Anton"},
                caption_x_ratio=0.3,
            )
        )

        store.upsert_clip_edit(
            ClipEdit(clip_id=clip.id, style_overrides=None, caption_x_ratio=None)
        )

        loaded = store.get_clip_edit(clip.id)
        assert loaded is not None
        assert loaded.style_overrides is None
        assert loaded.caption_x_ratio is None

    def test_a_pre_styling_row_still_loads(self, job: Job) -> None:
        # Contract §8.2: the reviewer's existing job — written before these
        # columns existed — must load and re-export unchanged.
        clip = Clip(id=new_id(), job_id=job.id, start_s=0.0, end_s=30.0)
        store.replace_clips(job.id, [clip])
        store.upsert_clip_edit(ClipEdit(clip_id=clip.id, caption_style="boxed"))

        with db.connection() as conn:
            conn.execute(
                """
                UPDATE clip_edits
                   SET style_overrides_json = NULL,
                       caption_margin_ratio = NULL,
                       caption_x_ratio = NULL
                 WHERE clip_id = ?
                """,
                (clip.id,),
            )

        loaded = store.get_clip_edit(clip.id)
        assert loaded is not None
        assert loaded.caption_style == "boxed"
        assert loaded.style_overrides is None

    def test_clip_edit_round_trips_pip_settings(self, job: Job) -> None:
        clip = Clip(id=new_id(), job_id=job.id, start_s=0.0, end_s=30.0)
        store.replace_clips(job.id, [clip])

        store.upsert_clip_edit(
            ClipEdit(clip_id=clip.id, pip_enabled=True, pip_corner="bl", pip_size=0.4)
        )

        loaded = store.get_clip_edit(clip.id)
        assert loaded is not None
        assert loaded.pip_enabled is True
        assert loaded.pip_corner == "bl"
        assert loaded.pip_size == pytest.approx(0.4)

    def test_exports_are_listed_for_a_clip(self, job: Job) -> None:
        clip = Clip(id=new_id(), job_id=job.id, start_s=0.0, end_s=30.0)
        store.replace_clips(job.id, [clip])

        store.create_export(
            Export(id=new_id(), clip_id=clip.id, path="out_9x16.mp4", size_bytes=1234)
        )
        store.create_export(Export(id=new_id(), clip_id=clip.id, path="out_1x1.mp4", ratio="1:1"))

        assert len(store.list_exports(clip.id)) == 2


class TestPresets:
    """Contract §4: presets are templates, and the default is unique."""

    @pytest.fixture(autouse=True)
    def _schema(self, initialised_db: int) -> None:
        """A migrated database — the table is new, so nothing else creates it."""

    @pytest.fixture
    def job(self, initialised_db: int) -> Job:
        source = store.create_source(_make_source())
        return store.create_job(Job(id=new_id(), source_id=source.id))

    @pytest.fixture
    def clip(self, job: Job) -> Clip:
        clip = Clip(id=new_id(), job_id=job.id, start_s=0.0, end_s=30.0)
        store.replace_clips(job.id, [clip])
        return clip

    def _preset(self, name: str = "On brand", **overrides) -> Preset:
        return Preset(id=new_id(), name=name, **overrides)

    def test_create_and_get(self) -> None:
        preset = self._preset(base_style="karaoke_fill", style_overrides={"font": "Inter"})

        store.create_preset(preset)

        loaded = store.get_preset(preset.id)
        assert loaded is not None
        assert loaded.name == "On brand"
        assert loaded.base_style == "karaoke_fill"
        assert loaded.style_overrides == {"font": "Inter"}

    def test_a_duplicate_name_is_refused(self) -> None:
        store.create_preset(self._preset(name="Same"))

        with pytest.raises(sqlite3.IntegrityError):
            store.create_preset(self._preset(name="Same"))

    def test_the_default_lists_first(self) -> None:
        first = self._preset(name="Alpha")
        second = self._preset(name="Beta")
        store.create_preset(first)
        store.create_preset(second)
        store.set_default_preset(second.id)

        listed = store.list_presets()

        assert [p.name for p in listed] == ["Beta", "Alpha"]
        assert listed[0].is_default is True

    def test_only_one_default_at_a_time(self) -> None:
        first = self._preset(name="Alpha")
        second = self._preset(name="Beta")
        store.create_preset(first)
        store.create_preset(second)

        store.set_default_preset(first.id)
        store.set_default_preset(second.id)

        assert store.default_preset().id == second.id
        assert store.get_preset(first.id).is_default is False

    def test_a_second_default_row_is_impossible(self) -> None:
        # The partial unique index is the backstop: nothing can leave two
        # defaults behind even if the clearing step were ever skipped.
        store.create_preset(self._preset(name="Alpha", is_default=True))

        with pytest.raises(sqlite3.IntegrityError):
            store.create_preset(self._preset(name="Beta", is_default=True))

    def test_update_patches_only_supplied_fields(self) -> None:
        preset = self._preset(style_overrides={"font": "Anton"}, layout={"caption_x_ratio": 0.2})
        store.create_preset(preset)

        store.update_preset(preset.id, name="Renamed")

        loaded = store.get_preset(preset.id)
        assert loaded is not None
        assert loaded.name == "Renamed"
        assert loaded.style_overrides == {"font": "Anton"}
        assert loaded.layout == {"caption_x_ratio": 0.2}

    def test_update_can_clear_a_blob(self) -> None:
        preset = self._preset(style_overrides={"font": "Anton"})
        store.create_preset(preset)

        store.update_preset(preset.id, style_overrides=None)

        assert store.get_preset(preset.id).style_overrides is None

    def test_delete_reports_whether_it_found_one(self) -> None:
        preset = self._preset()
        store.create_preset(preset)

        assert store.delete_preset(preset.id) is True
        assert store.delete_preset(preset.id) is False

    def test_deleting_the_default_leaves_none(self) -> None:
        preset = self._preset()
        store.create_preset(preset)
        store.set_default_preset(preset.id)

        store.delete_preset(preset.id)

        assert store.default_preset() is None

    def test_apply_copies_the_style_and_layout_onto_the_clip(self, clip: Clip) -> None:
        preset = self._preset(
            base_style="boxed",
            style_overrides={"font": "Bebas Neue", "max_words": 2},
            layout={"caption_margin_ratio": 0.15, "caption_x_ratio": 0.25},
        )

        store.apply_preset_to_clips([clip.id], preset)

        edit = store.get_clip_edit(clip.id)
        assert edit is not None
        assert edit.caption_style == "boxed"
        assert edit.style_overrides == {"font": "Bebas Neue", "max_words": 2}
        assert edit.caption_margin_ratio == pytest.approx(0.15)
        assert edit.caption_x_ratio == pytest.approx(0.25)

    def test_apply_keeps_the_clip_own_edits(self, clip: Clip) -> None:
        store.upsert_clip_edit(
            ClipEdit(clip_id=clip.id, crop_nudge=0.4, pip_enabled=True, pip_corner="bl")
        )
        preset = self._preset(style_overrides={"font": "Inter"})

        store.apply_preset_to_clips([clip.id], preset)

        edit = store.get_clip_edit(clip.id)
        assert edit is not None
        assert edit.crop_nudge == pytest.approx(0.4)
        assert edit.pip_enabled is True
        assert edit.pip_corner == "bl"
        assert edit.style_overrides == {"font": "Inter"}

    def test_clips_carry_copies_so_deleting_the_preset_changes_nothing(
        self, clip: Clip
    ) -> None:
        preset = self._preset(style_overrides={"font": "Inter"})
        store.create_preset(preset)
        store.apply_preset_to_clips([clip.id], preset)

        store.delete_preset(preset.id)

        edit = store.get_clip_edit(clip.id)
        assert edit is not None
        assert edit.style_overrides == {"font": "Inter"}

    def test_the_copy_is_independent_of_the_preset_object(self, clip: Clip) -> None:
        overrides = {"font": "Inter"}
        preset = self._preset(style_overrides=overrides)
        store.apply_preset_to_clips([clip.id], preset)

        # Mutating the preset's blob in memory must not reach the clip: it was
        # deep-copied, not aliased.
        preset.style_overrides["font"] = "Anton"

        assert store.get_clip_edit(clip.id).style_overrides == {"font": "Inter"}

    def test_applying_to_no_clips_is_a_noop(self) -> None:
        store.apply_preset_to_clips([], self._preset())

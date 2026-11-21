"""Caption grouping and ASS generation."""

from __future__ import annotations

from pathlib import Path

import pysubs2
import pytest
from autoclip.pipeline import captions
from autoclip.pipeline.transcript import Word


def words_from(spec: list[tuple[str, float, float]]) -> list[Word]:
    return [Word(text=t, start=s, end=e) for t, s, e in spec]


def evenly_spaced(texts: list[str], *, step: float = 0.4) -> list[Word]:
    return [
        Word(text=text, start=i * step, end=i * step + step * 0.8) for i, text in enumerate(texts)
    ]


class TestGrouping:
    def test_respects_the_word_ceiling(self) -> None:
        groups = captions.group_words(evenly_spaced([f"w{i}" for i in range(12)]), max_words=4)

        assert all(len(g.words) <= 4 for g in groups)
        assert sum(len(g.words) for g in groups) == 12

    def test_breaks_on_a_long_pause(self) -> None:
        words = words_from([("one", 0.0, 0.3), ("two", 0.3, 0.6), ("three", 2.0, 2.3)])

        groups = captions.group_words(words, max_words=10, max_gap_s=0.4)

        assert len(groups) == 2
        assert groups[1].words[0].text == "three"

    def test_breaks_after_sentence_punctuation(self) -> None:
        words = words_from([("Hello.", 0.0, 0.3), ("Next", 0.4, 0.7), ("thing", 0.8, 1.0)])

        groups = captions.group_words(words, max_words=10)

        assert len(groups) == 2
        assert groups[0].text == "Hello."

    def test_trailing_sentence_end_does_not_make_an_empty_group(self) -> None:
        words = words_from([("Hello", 0.0, 0.3), ("world.", 0.4, 0.7)])

        groups = captions.group_words(words, max_words=10)

        assert len(groups) == 1
        assert all(g.words for g in groups)

    def test_every_word_appears_exactly_once(self) -> None:
        words = evenly_spaced([f"w{i}" for i in range(37)])

        grouped = [w for g in captions.group_words(words, max_words=4) for w in g.words]

        assert [w.text for w in grouped] == [w.text for w in words]

    def test_empty_input_gives_no_groups(self) -> None:
        assert captions.group_words([]) == []

    def test_group_times_span_its_words(self) -> None:
        groups = captions.group_words(evenly_spaced(["a", "b", "c"]), max_words=3)

        assert groups[0].start == 0.0
        assert groups[0].end == pytest.approx(0.8 + 0.32, abs=0.01)


class TestColour:
    def test_hex_converts_to_rgb(self) -> None:
        colour = captions.hex_to_ass("#FFE500")

        assert (colour.r, colour.g, colour.b) == (255, 229, 0)

    def test_leading_hash_is_optional(self) -> None:
        assert captions.hex_to_ass("FFFFFF").r == 255

    def test_bad_hex_raises(self) -> None:
        with pytest.raises(ValueError):
            captions.hex_to_ass("#FFF")


class TestPresets:
    def test_all_four_presets_exist(self) -> None:
        assert set(captions.PRESETS) == {
            "bold_pop",
            "karaoke_fill",
            "clean_lower",
            "boxed",
        }

    @pytest.mark.parametrize("key", list(captions.PRESETS))
    def test_every_preset_ships_its_font(self, key: str) -> None:
        # A missing font file means libass silently substitutes, and the caption
        # looks nothing like the preview.
        assert captions.get_style(key).font_path.exists()

    def test_unknown_style_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown caption style"):
            captions.get_style("neon-explosion")


class TestAssGeneration:
    @pytest.fixture
    def words(self) -> list[Word]:
        return evenly_spaced(["the", "quick", "brown", "fox", "jumps", "over."])

    @pytest.mark.parametrize("key", list(captions.PRESETS))
    def test_every_preset_produces_events(self, key: str, words: list[Word]) -> None:
        subs = captions.build_ass(words, captions.get_style(key), width=1080, height=1920)

        assert len(subs.events) > 0
        assert captions.STYLE_NAME in subs.styles

    def test_resolution_is_recorded(self, words: list[Word]) -> None:
        subs = captions.build_ass(words, captions.get_style("bold_pop"), width=1080, height=1920)

        assert subs.info["PlayResX"] == "1080"
        assert subs.info["PlayResY"] == "1920"

    def test_font_size_scales_with_height(self, words: list[Word]) -> None:
        tall = captions.build_ass(words, captions.get_style("bold_pop"), width=1080, height=1920)
        square = captions.build_ass(words, captions.get_style("bold_pop"), width=1080, height=1080)

        assert (
            tall.styles[captions.STYLE_NAME].fontsize > square.styles[captions.STYLE_NAME].fontsize
        )

    def test_time_offset_rebases_to_clip_relative(self, words: list[Word]) -> None:
        offset = captions.build_ass(
            words,
            captions.get_style("clean_lower"),
            width=1080,
            height=1920,
            time_offset_s=100.0,
        )

        # Absolute times were 0-2.7s, so everything clamps to zero rather than
        # going negative.
        assert offset.events[0].start == 0

    def test_offset_preserves_relative_spacing(self) -> None:
        words = [Word(text=t, start=100.0 + i, end=100.5 + i) for i, t in enumerate("abc")]

        subs = captions.build_ass(
            words,
            captions.get_style("clean_lower"),
            width=1080,
            height=1920,
            time_offset_s=100.0,
        )

        assert subs.events[0].start == pytest.approx(0, abs=20)

    def test_all_caps_is_applied(self, words: list[Word]) -> None:
        subs = captions.build_ass(words, captions.get_style("bold_pop"), width=1080, height=1920)

        assert "QUICK" in " ".join(e.text for e in subs.events)

    def test_clean_lower_preserves_case(self, words: list[Word]) -> None:
        subs = captions.build_ass(words, captions.get_style("clean_lower"), width=1080, height=1920)

        assert "quick" in " ".join(e.text for e in subs.events)

    def test_karaoke_emits_kf_tags(self, words: list[Word]) -> None:
        subs = captions.build_ass(
            words, captions.get_style("karaoke_fill"), width=1080, height=1920
        )

        assert any("\\kf" in event.text for event in subs.events)

    def test_bold_pop_emits_one_event_per_word(self, words: list[Word]) -> None:
        # Per-word highlighting has no per-glyph timeline in libass, so each
        # word needs its own event showing the whole group.
        subs = captions.build_ass(words, captions.get_style("bold_pop"), width=1080, height=1920)

        assert len(subs.events) == len(words)

    def test_clean_lower_emits_one_event_per_group(self, words: list[Word]) -> None:
        style = captions.get_style("clean_lower")
        groups = captions.group_words(words, max_words=style.max_words)

        subs = captions.build_ass(words, style, width=1080, height=1920)

        assert len(subs.events) == len(groups)

    def test_boxed_uses_an_opaque_border_style(self, words: list[Word]) -> None:
        subs = captions.build_ass(words, captions.get_style("boxed"), width=1080, height=1920)

        assert subs.styles[captions.STYLE_NAME].borderstyle == 3

    def test_events_never_start_before_zero(self, words: list[Word]) -> None:
        subs = captions.build_ass(
            words,
            captions.get_style("bold_pop"),
            width=1080,
            height=1920,
            time_offset_s=1.0,
        )

        assert all(event.start >= 0 for event in subs.events)

    def test_written_file_is_valid_ass(self, words: list[Word], tmp_path) -> None:
        path = captions.write_ass(
            tmp_path / "out.ass",
            words,
            captions.get_style("bold_pop"),
            width=1080,
            height=1920,
        )

        reloaded = pysubs2.load(str(path), encoding="utf-8")

        assert len(reloaded.events) > 0

    def test_srt_sidecar_is_written(self, words: list[Word], tmp_path) -> None:
        path = captions.write_srt(tmp_path / "out.srt", words)

        assert path.exists()
        assert len(pysubs2.load(str(path), encoding="utf-8").events) > 0


class TestResolveStyle:
    def test_defaults_return_the_preset_untouched(self) -> None:
        preset = captions.get_style("bold_pop")

        assert captions.resolve_style("bold_pop") is preset

    def test_size_scale_multiplies_font_size_only(self) -> None:
        preset = captions.get_style("bold_pop")

        resolved = captions.resolve_style("bold_pop", size_scale=1.5)

        assert resolved.size_ratio == pytest.approx(preset.size_ratio * 1.5)
        assert resolved.margin_v_ratio == preset.margin_v_ratio
        # The shared preset must not be mutated.
        assert preset.size_ratio == pytest.approx(0.062)

    def test_position_override_replaces_preset_margin(self) -> None:
        resolved = captions.resolve_style("karaoke_fill", position_ratio=0.1)

        assert resolved.margin_v_ratio == pytest.approx(0.1)

    def test_out_of_range_inputs_are_clamped(self) -> None:
        preset = captions.get_style("boxed")

        assert captions.resolve_style("boxed", size_scale=5.0).size_ratio == pytest.approx(
            preset.size_ratio * 2.0
        )
        assert captions.resolve_style("boxed", size_scale=0.1).size_ratio == pytest.approx(
            preset.size_ratio * 0.5
        )
        assert captions.resolve_style("boxed", position_ratio=0.99).margin_v_ratio == pytest.approx(
            0.8
        )
        assert captions.resolve_style("boxed", position_ratio=-0.5).margin_v_ratio == pytest.approx(
            0.0
        )


class TestNoOverrideRegression:
    """Contract §8.3: no overrides must burn byte-identical to the old renderer.

    The fixtures under ``data/ass_main`` were generated from ``main`` at 3f66b38
    — the commit this work branched from — so this is a diff against the real
    previous output, not against a reimplementation of it.
    """

    WORDS = ["the", "quick", "brown", "fox", "jumps", "over."]

    @staticmethod
    def _words() -> list[Word]:
        return [
            Word(text=text, start=index * 0.4, end=index * 0.4 + 0.32)
            for index, text in enumerate(TestNoOverrideRegression.WORDS)
        ]

    @pytest.mark.parametrize("key", list(captions.PRESETS))
    def test_every_preset_matches_the_pre_styling_output(self, key: str) -> None:
        expected = (Path(__file__).parent / "data" / "ass_main" / f"{key}.ass").read_text(
            encoding="utf-8"
        )

        subs = captions.build_ass(self._words(), captions.get_style(key), width=1080, height=1920)

        assert subs.to_string(format_="ass") == expected

    def test_resolving_with_no_overrides_returns_the_preset_itself(self) -> None:
        # Identity, not equality: nothing to layer means nothing is rebuilt, which
        # is why the bytes cannot move.
        assert captions.resolve_style("boxed") is captions.PRESETS["boxed"]


class TestOverrideResolution:
    def test_font_override_reaches_the_ass_style(self) -> None:
        style = captions.resolve_style("bold_pop", overrides={"font": "Inter"})

        assert style.font == "Inter"
        assert style.font_path.name == "Inter-Variable.ttf"

    def test_colour_overrides_replace_the_preset_values(self) -> None:
        style = captions.resolve_style(
            "bold_pop",
            overrides={"primary": "#112233", "accent": "#445566", "outline": "#778899"},
        )

        assert style.primary == "#112233"
        assert style.accent == "#445566"
        assert style.outline == "#778899"

    def test_a_null_accent_disables_per_word_highlighting(self) -> None:
        style = captions.resolve_style("bold_pop", overrides={"accent": None})
        events = captions.build_ass(
            self._words(), style, width=1080, height=1920
        ).events

        assert style.accent is None
        # Without an accent there is nothing to highlight, so one event per group
        # instead of one per word.
        assert len(events) == len(captions.group_words(self._words(), max_words=style.max_words))
        assert not any("\\c&H" in event.text for event in events)

    def test_an_absent_accent_keeps_the_preset_accent(self) -> None:
        style = captions.resolve_style("bold_pop", overrides={"primary": "#112233"})

        assert style.accent == captions.PRESETS["bold_pop"].accent

    def test_size_ratio_replaces_rather_than_multiplies(self) -> None:
        # The override is an absolute fraction of frame height, not a multiplier
        # on the preset, so it must win outright over the global knob too.
        style = captions.resolve_style(
            "bold_pop", size_scale=2.0, overrides={"size_ratio": 0.03}
        )

        assert style.size_ratio == pytest.approx(0.03)

    @pytest.mark.parametrize("mode", captions.ANIMATIONS)
    def test_animation_override_is_honoured(self, mode: str) -> None:
        style = captions.resolve_style("bold_pop", overrides={"animation": mode})

        assert style.animation == mode

    def test_all_caps_and_boxed_overrides_apply(self) -> None:
        style = captions.resolve_style(
            "clean_lower", overrides={"all_caps": True, "boxed": True}
        )
        subs = captions.build_ass(self._words(), style, width=1080, height=1920)

        assert subs.styles[captions.STYLE_NAME].borderstyle == 3
        assert "ONE" in " ".join(e.text for e in subs.events)

    def test_box_colour_and_alpha_reach_the_style(self) -> None:
        style = captions.resolve_style(
            "boxed", overrides={"box_colour": "#102030", "box_alpha": 200}
        )

        assert style.box_colour == "#102030"
        assert style.box_alpha == 200

    def test_outline_width_override_applies(self) -> None:
        style = captions.resolve_style("bold_pop", overrides={"outline_width": 7.0})

        assert style.outline_width == pytest.approx(7.0)

    @staticmethod
    def _words() -> list[Word]:
        return evenly_spaced(["one", "two", "three", "four", "five"])


class TestWordsPerScreen:
    """The pacing knob: the override's max_words feeds grouping (contract §1)."""

    @pytest.mark.parametrize("max_words", range(1, 9))
    def test_each_setting_caps_the_group_size(self, max_words: int) -> None:
        words = evenly_spaced([f"w{i}" for i in range(16)])

        style = captions.resolve_style("bold_pop", overrides={"max_words": max_words})
        groups = captions.group_words(words, max_words=style.max_words)

        assert all(len(g.words) <= max_words for g in groups)
        assert style.max_words == max_words

    def test_one_word_per_group_is_the_snappy_look(self) -> None:
        words = evenly_spaced([f"w{i}" for i in range(6)])

        style = captions.resolve_style("bold_pop", overrides={"max_words": 1})
        groups = captions.group_words(words, max_words=style.max_words)

        assert [len(g.words) for g in groups] == [1] * 6

    def test_out_of_range_pacing_is_clamped_not_raised(self) -> None:
        assert captions.resolve_style("bold_pop", overrides={"max_words": 99}).max_words == 8
        assert captions.resolve_style("bold_pop", overrides={"max_words": 0}).max_words == 1
        assert captions.resolve_style("bold_pop", overrides={"max_words": -4}).max_words == 1


class TestHorizontalAlignment:
    """Contract §2: the block is anchored by its edge, on both sides."""

    WIDTH = 1080
    HEIGHT = 1920
    #: The inset every caption carried before x-axis placement.
    LEGACY_INSET = round(HEIGHT * 0.03)

    def _margins(self, x_ratio):
        style = captions.resolve_style("bold_pop", caption_x_ratio=x_ratio)
        return captions.horizontal_margins(style, width=self.WIDTH, height=self.HEIGHT)

    def test_centred_is_the_legacy_geometry(self) -> None:
        assert self._margins(None) == (self.LEGACY_INSET, self.LEGACY_INSET)

    def test_left_of_centre_anchors_the_left_margin(self) -> None:
        assert self._margins(0.25) == (round(0.25 * self.WIDTH), self.LEGACY_INSET)

    def test_right_of_centre_anchors_the_right_margin(self) -> None:
        assert self._margins(0.75) == (self.LEGACY_INSET, round(0.25 * self.WIDTH))

    def test_alignment_follows_the_side(self) -> None:
        assert captions.resolve_style("bold_pop", caption_x_ratio=0.2).alignment == "left"
        assert captions.resolve_style("bold_pop", caption_x_ratio=0.8).alignment == "right"
        assert captions.resolve_style("bold_pop", caption_x_ratio=None).alignment == "center"

    def test_near_centre_snaps_to_centred(self) -> None:
        # A drag that lands "centred enough" renders centred rather than
        # left-aligned against a margin thinner than the outline.
        for x_ratio in (0.5, 0.47, 0.53, 0.5 - captions.X_SNAP_TOLERANCE):
            style = captions.resolve_style("bold_pop", caption_x_ratio=x_ratio)

            assert style.x_ratio is None, x_ratio
            assert style.alignment == "center"

    def test_just_past_the_snap_tolerance_stays_anchored(self) -> None:
        x_ratio = 0.5 - captions.X_SNAP_TOLERANCE - 0.01

        style = captions.resolve_style("bold_pop", caption_x_ratio=x_ratio)

        assert style.alignment == "left"
        assert style.x_ratio == pytest.approx(x_ratio)

    def test_out_of_range_positions_are_clamped(self) -> None:
        assert captions.resolve_style("bold_pop", caption_x_ratio=4.0).x_ratio == 1.0
        assert captions.resolve_style("bold_pop", caption_x_ratio=-4.0).x_ratio == 0.0

    def test_ass_style_carries_the_alignment_and_margins(self) -> None:
        style = captions.resolve_style("bold_pop", caption_x_ratio=0.25)

        subs = captions.build_ass(
            evenly_spaced(["hello"]), style, width=self.WIDTH, height=self.HEIGHT
        )
        emitted = subs.styles[captions.STYLE_NAME]

        assert emitted.alignment == pysubs2.Alignment.BOTTOM_LEFT
        assert emitted.marginl == round(0.25 * self.WIDTH)
        assert emitted.marginr == self.LEGACY_INSET

    def test_right_alignment_reaches_the_ass_style(self) -> None:
        style = captions.resolve_style("bold_pop", caption_x_ratio=0.8)

        subs = captions.build_ass(
            evenly_spaced(["hello"]), style, width=self.WIDTH, height=self.HEIGHT
        )
        emitted = subs.styles[captions.STYLE_NAME]

        assert emitted.alignment == pysubs2.Alignment.BOTTOM_RIGHT
        assert emitted.marginr == round(0.2 * self.WIDTH)

    def test_vertical_margin_is_clamped(self) -> None:
        assert captions.resolve_style(
            "bold_pop", caption_margin_ratio=5.0
        ).margin_v_ratio == pytest.approx(0.8)
        assert captions.resolve_style(
            "bold_pop", caption_margin_ratio=-1.0
        ).margin_v_ratio == pytest.approx(0.0)


class TestLayering:
    """Contract §1: overrides > per-clip geometry > legacy globals > preset."""

    def test_a_per_clip_margin_beats_the_global_knob(self) -> None:
        style = captions.resolve_style(
            "bold_pop", position_ratio=0.30, caption_margin_ratio=0.10
        )

        assert style.margin_v_ratio == pytest.approx(0.10)

    def test_without_a_per_clip_margin_the_global_knob_still_wins(self) -> None:
        # The transition layer has to keep working, or an existing user's
        # captions move when they open the new editor (contract §8.3).
        style = captions.resolve_style("bold_pop", position_ratio=0.30)

        assert style.margin_v_ratio == pytest.approx(0.30)

    def test_without_any_override_the_preset_value_stands(self) -> None:
        style = captions.resolve_style("bold_pop")

        assert style.margin_v_ratio == captions.PRESETS["bold_pop"].margin_v_ratio

    def test_a_stale_blob_never_raises(self) -> None:
        # Resolution must survive whatever is in the database, including a blob
        # written by a build that knew things this one doesn't.
        style = captions.resolve_style(
            "bold_pop",
            overrides={
                "font": "Comic Sans MS",
                "primary": "not-a-colour",
                "animation": "explode",
                "max_words": "lots",
                "size_ratio": None,
                "wobble": 3,
            },
        )

        assert style.font == "Anton"
        assert style.primary == captions.PRESETS["bold_pop"].primary
        assert style.animation == captions.PRESETS["bold_pop"].animation
        assert style.max_words == captions.PRESETS["bold_pop"].max_words

    def test_clamped_overrides_are_idempotent(self) -> None:
        once = captions.clamp_overrides({"size_ratio": 9.0, "max_words": 99})

        assert captions.clamp_overrides(once) == once
        assert once == {"size_ratio": 0.12, "max_words": 8}


class TestPreviewPayload:
    """Contract §8.1: the payload and the burned style come from one resolution."""

    def test_carries_the_alignment_and_geometry(self) -> None:
        style = captions.resolve_style("bold_pop", caption_x_ratio=0.2)

        payload = captions.preview_payload(style)

        assert payload["alignment"] == "left"
        assert payload["xRatio"] == pytest.approx(0.2)
        assert payload["x_ratio"] == pytest.approx(0.2)
        assert payload["margin_ratio"] == pytest.approx(style.margin_v_ratio)

    def test_carries_the_box_style_the_css_needs(self) -> None:
        payload = captions.preview_payload(captions.get_style("boxed"))

        assert payload["boxColour"] == "#000000"
        assert payload["box_colour"] == "#000000"
        assert payload["boxAlpha"] == captions.PRESETS["boxed"].box_alpha
        assert payload["box_alpha"] == captions.PRESETS["boxed"].box_alpha

    def test_reflects_overrides(self) -> None:
        style = captions.resolve_style(
            "bold_pop", overrides={"font": "Inter", "max_words": 2, "outline_width": 1.0}
        )

        payload = captions.preview_payload(style)

        assert payload["font"] == "Inter"
        assert payload["maxWords"] == 2
        assert payload["outlineWidth"] == pytest.approx(1.0)


class TestFontRegistry:
    """Contract §5: everything the renderer can name is bundled and usable."""

    #: TrueType outlines. ``true``/``ttcf`` cover the rarer wrappers, and an HTML
    #: error page or a Git LFS pointer fails all of them — which is the failure
    #: this catches, because both look like a file on disk.
    TTF_MAGIC = (b"\x00\x01\x00\x00", b"true", b"ttcf")

    def test_offers_at_least_five_faces(self) -> None:
        assert len(captions.FONTS) >= 5

    def test_keys_match_the_family_the_ass_style_emits(self) -> None:
        for key, spec in captions.FONTS.items():
            assert spec.key == key

    def test_labels_are_not_empty(self) -> None:
        assert all(spec.label.strip() for spec in captions.FONTS.values())

    @pytest.mark.parametrize("key", list(captions.FONTS))
    def test_every_registered_font_ships_its_file(self, key: str) -> None:
        assert captions.get_font(key).path.exists()

    @pytest.mark.parametrize("key", list(captions.FONTS))
    def test_every_registered_font_is_a_real_ttf(self, key: str) -> None:
        raw = captions.get_font(key).path.read_bytes()

        assert raw[:4] in self.TTF_MAGIC
        # An LFS pointer is a ~130-byte text file. This is a floor for "has
        # outlines", not a beauty contest.
        assert len(raw) > 5_000

    @pytest.mark.parametrize("key", list(captions.FONTS))
    def test_every_registered_font_renders(self, key: str) -> None:
        style = captions.resolve_style("bold_pop", overrides={"font": key})

        subs = captions.build_ass(
            evenly_spaced(["hello", "world"]), style, width=1080, height=1920
        )

        assert subs.styles[captions.STYLE_NAME].fontname == key
        assert len(subs.events) > 0

    def test_unknown_font_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown font"):
            captions.get_font("Comic Sans MS")

    def test_a_font_outside_the_registry_is_ignored_by_resolution(self) -> None:
        style = captions.resolve_style("bold_pop", overrides={"font": "Comic Sans MS"})

        assert style.font == captions.PRESETS["bold_pop"].font

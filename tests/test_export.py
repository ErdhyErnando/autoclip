"""Export filtergraph construction (no rendering).

Rendering itself is covered by the slow tests in test_export_render.py; these
assert on the filtergraph strings, which is where framing decisions land.
"""

from __future__ import annotations

from pathlib import Path

from autoclip.pipeline import captions, export
from autoclip.pipeline.export import ExportRequest, PipLayout
from autoclip.pipeline.reframe.croppath import centre_crop
from autoclip.pipeline.transcript import Word


def words() -> list[Word]:
    return [Word(text=f"w{i}", start=i * 0.4, end=i * 0.4 + 0.3) for i in range(8)]


def request(**overrides) -> ExportRequest:
    path = centre_crop(1920, 1080, 30.0)
    params = {
        "source": Path("source.mp4"),
        "destination": Path("out.mp4"),
        "start_s": 0.0,
        "end_s": 30.0,
        "crop_path": path,
        "words": words(),
        "style": captions.get_style("bold_pop"),
        "ratio": "9:16",
    }
    params.update(overrides)
    return ExportRequest(**params)


class TestPipFiltergraph:
    def test_pip_chain_has_face_crop_and_overlay(self) -> None:
        graph = export.build_video_filtergraph(
            request(pip=PipLayout(x=1694.0, y=22.0, width=138.0, height=252.0)),
            subtitle_name=None,
        )

        assert "crop=w=138:h=252:x=1694:y=22" in graph
        assert "overlay=" in graph
        # The background fits the whole frame over a blurred copy so no screen
        # content is cropped away.
        assert "boxblur" in graph

    def test_inset_size_tracks_the_size_ratio(self) -> None:
        small = export.build_video_filtergraph(
            request(pip=PipLayout(x=0.0, y=0.0, width=100.0, height=100.0, size_ratio=0.2)),
            subtitle_name=None,
        )
        large = export.build_video_filtergraph(
            request(pip=PipLayout(x=0.0, y=0.0, width=100.0, height=100.0, size_ratio=0.5)),
            subtitle_name=None,
        )

        # 0.2 * 1080 = 216 wide; 0.5 * 1080 = 540 wide.
        assert "scale=216:216" in small
        assert "scale=540:540" in large

    def test_inset_parks_in_the_chosen_corner(self) -> None:
        box = {"x": 0.0, "y": 0.0, "width": 100.0, "height": 100.0}
        tl = export.build_video_filtergraph(
            request(pip=PipLayout(**box, corner="tl")), subtitle_name=None
        )
        br = export.build_video_filtergraph(
            request(pip=PipLayout(**box, corner="br")), subtitle_name=None
        )

        assert "overlay=24:24" in tl
        assert "overlay=" in br and "overlay=24:24" not in br

    def test_captions_still_burn_in_after_the_overlay(self) -> None:
        graph = export.build_video_filtergraph(
            request(pip=PipLayout(x=0.0, y=0.0, width=100.0, height=100.0)),
            subtitle_name="captions.ass",
        )

        assert graph.index("overlay=") < graph.index("ass=filename=captions.ass")

    def test_tracked_chain_is_unchanged_without_pip(self) -> None:
        graph = export.build_video_filtergraph(request(), subtitle_name=None)

        assert "overlay=" not in graph
        assert "[vout]" in graph

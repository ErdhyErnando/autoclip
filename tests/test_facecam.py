"""Facecam detection for picture-in-picture stream clips."""

from __future__ import annotations

from autoclip.pipeline.reframe.facecam import detect_facecam
from autoclip.pipeline.reframe.faces import FaceObservation

W, H = 1920, 1080


def face(t: float, cx: float, cy: float, w: float = 90.0, h: float = 110.0) -> FaceObservation:
    return FaceObservation(t=t, cx=cx, cy=cy, width=w, height=h, eye_y=cy - 20.0, mar=0.2)


def test_finds_a_static_corner_face() -> None:
    # A facecam parked top-right for the whole video, with detection jitter.
    observations = [face(t, 1650.0 + (t % 3), 150.0 - (t % 2)) for t in range(0, 600, 2)]

    box = detect_facecam(observations, W, H)

    assert box is not None
    assert box.corner == "tr"
    assert box.coverage > 0.9
    # The inset must actually contain the face it was built from.
    assert box.x <= 1650.0 - 90.0 / 2
    assert box.right >= 1650.0 + 90.0 / 2
    assert box.y <= 150.0 - 110.0 / 2
    assert box.bottom >= 150.0 + 110.0 / 2


def test_rejects_a_centre_close_up() -> None:
    observations = [face(t, 960.0, 540.0, w=300.0, h=360.0) for t in range(0, 600, 2)]

    assert detect_facecam(observations, W, H) is None


def test_rejects_a_guest_who_joins_late() -> None:
    observations = [face(t, 200.0, 150.0) for t in range(400, 600, 2)]

    assert detect_facecam(observations, W, H, duration_s=600.0) is None


def test_rejects_a_moving_person() -> None:
    # Same face size as a facecam, but walking across the frame.
    observations = [face(t, 200.0 + t * 2.0, 800.0) for t in range(0, 600, 2)]

    assert detect_facecam(observations, W, H) is None


def test_picks_the_persistent_facecam_over_a_passer_by() -> None:
    observations = [face(t, 1650.0, 150.0) for t in range(0, 600, 2)]
    observations += [face(t, 400.0 + t, 700.0) for t in range(100, 160, 2)]

    box = detect_facecam(observations, W, H)

    assert box is not None
    assert box.corner == "tr"


def test_empty_observations_find_nothing() -> None:
    assert detect_facecam([], W, H) is None

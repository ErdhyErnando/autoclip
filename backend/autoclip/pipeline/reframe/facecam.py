"""Facecam detection for picture-in-picture stream clips.

A stream VOD is screen content plus a small box with the streamer's face, parked
in a corner for the whole video. When such a box exists, a 9:16 export can show
the full screen behind a face inset instead of cropping one of them away.

Detection works purely from :func:`faces.sample_faces` observations: a facecam
is a cluster of overlapping face boxes that barely moves, persists across most
of the video, and sits in a corner. A talking-head close-up fails the corner
test; a person walking through the frame fails the stability test; a guest who
joins halfway fails the coverage test.
"""

from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass
from pathlib import Path

from .faces import FaceObservation, sample_faces

log = logging.getLogger(__name__)

#: Two observations belong to the same face when their boxes overlap this much.
CLUSTER_IOU = 0.4
#: A facecam must be visible across this fraction of the observed span.
MIN_COVERAGE = 0.5
#: Minimum observations in a cluster, so a few stray detections can't qualify.
MIN_OBSERVATIONS = 10
#: Maximum drift of the box centre, as a fraction of frame width. A facecam is
#: static; a person moving through the frame drifts far more than this.
MAX_DRIFT_RATIO = 0.08
#: Maximum size change within a cluster, measured as the ratio of the 90th to
#: the 10th percentile width/height. Percentiles (not min/max) so a few noisy
#: boxes on a small face can't disqualify a static webcam; a real zoom cut or
#: someone walking at the camera sails straight past this.
MAX_SIZE_VARIATION = 0.5
#: A facecam face is small. Anything bigger is the main subject, not an inset.
MAX_FACE_AREA_RATIO = 0.12
#: Boxes below this are detection noise, not a face anyone could watch.
MIN_FACE_WIDTH_PX = 24.0
#: How far the expanded head-and-shoulders box sits from the frame edges.
CORNER_MARGIN_RATIO = 0.02


@dataclass
class FacecamBox:
    """Source-pixel region to use as the PiP inset, head and shoulders."""

    x: float
    y: float
    width: float
    height: float
    #: Which corner the face lives in: "tl" | "tr" | "bl" | "br".
    corner: str
    #: Fraction of the observed span the facecam was visible for.
    coverage: float

    @property
    def right(self) -> float:
        return self.x + self.width

    @property
    def bottom(self) -> float:
        return self.y + self.height

    def to_dict(self) -> dict[str, float | str]:
        return {
            "x": self.x,
            "y": self.y,
            "width": self.width,
            "height": self.height,
            "corner": self.corner,
            "coverage": self.coverage,
        }

    @classmethod
    def from_dict(cls, data: dict) -> FacecamBox:
        return cls(
            x=float(data["x"]),
            y=float(data["y"]),
            width=float(data["width"]),
            height=float(data["height"]),
            corner=str(data["corner"]),
            coverage=float(data.get("coverage", 0.0)),
        )

    def save(self, path: Path) -> Path:
        import json

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict()), encoding="utf-8")
        return path

    @classmethod
    def load(cls, path: Path) -> FacecamBox:
        import json

        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


def detect_facecam(
    observations: list[FaceObservation],
    source_width: int,
    source_height: int,
    *,
    duration_s: float | None = None,
) -> FacecamBox | None:
    """Find a persistent corner face among sampled observations, if any.

    ``duration_s`` is the video (or window) the observations were sampled from;
    coverage is measured against it. Without it, coverage falls back to the
    observed span, which flatters a face that only appears for part of the video.
    """
    if not observations or source_width <= 0 or source_height <= 0:
        return None

    observed_span = max(o.t for o in observations) - min(o.t for o in observations)
    total_span = duration_s if duration_s else observed_span
    if total_span <= 0:
        return None

    frame_area = source_width * source_height
    usable = [
        o
        for o in observations
        if o.width >= MIN_FACE_WIDTH_PX and o.area / frame_area <= MAX_FACE_AREA_RATIO
    ]

    best: FacecamBox | None = None
    for cluster in _cluster(usable):
        if len(cluster) < MIN_OBSERVATIONS:
            continue
        box = _to_facecam(cluster, total_span, source_width, source_height)
        if box is not None and (best is None or box.coverage > best.coverage):
            best = box
    return best


def detect_facecam_in_video(
    video: Path,
    source_width: int,
    source_height: int,
    *,
    start_s: float = 0.0,
    end_s: float | None = None,
    sample_fps: float = 1.0,
) -> FacecamBox | None:
    """Sample a video sparsely and look for a facecam box.

    1 fps is plenty: a facecam persists for minutes, and MediaPipe is the most
    expensive call in the reframe stage, so this stays cheap.
    """
    observations = sample_faces(video, start_s=start_s, end_s=end_s, sample_fps=sample_fps)
    duration = (end_s if end_s is not None else start_s) - start_s
    return detect_facecam(
        observations, source_width, source_height, duration_s=duration if duration > 0 else None
    )


#: Corner search regions as (width, height, x-offset, y-offset) fractions of the
#: frame. A small facecam is invisible to the landmarker at full-frame 1080p
#: (proven on real footage: 0 detections full-frame, 24/60 in a corner crop),
#: so each corner is re-scanned up close. The crops stay tight on purpose: on
#: the same footage an 864px-wide crop found nothing where a 730px one found
#: 11 — the face must stay large in the landmaker's input. Overlapping regions
#: are fine — the same face seen twice merges in clustering by IoU.
CORNER_CROPS: dict[str, tuple[float, float, float, float]] = {
    "tl": (0.38, 0.38, 0.0, 0.0),
    "tr": (0.38, 0.38, 0.62, 0.0),
    "bl": (0.38, 0.38, 0.0, 0.62),
    "br": (0.38, 0.38, 0.62, 0.62),
}


def search_facecam(
    video: Path,
    source_width: int,
    source_height: int,
    *,
    start_s: float = 0.0,
    end_s: float | None = None,
    sample_fps: float = 1.0,
) -> FacecamBox | None:
    """Look for a facecam box anywhere in the frame.

    Full-frame pass first (catches large facecams cheaply), then a close-up
    pass over each corner (catches the small ones the landmarker misses at
    full-frame scale). Corner detections are mapped back to source pixels and
    pooled, so a face straddling two regions still clusters as one.
    """
    direct = detect_facecam_in_video(
        video,
        source_width,
        source_height,
        start_s=start_s,
        end_s=end_s,
        sample_fps=sample_fps,
    )
    if direct is not None:
        return direct

    try:
        import cv2
    except ImportError:
        return None

    pooled: list[FaceObservation] = []
    for frac_w, frac_h, frac_x, frac_y in CORNER_CROPS.values():
        pooled.extend(
            _sample_crop(
                cv2,
                video,
                frac_x * source_width,
                frac_y * source_height,
                frac_w * source_width,
                frac_h * source_height,
                start_s=start_s,
                end_s=end_s,
                sample_fps=sample_fps,
            )
        )

    duration = (end_s if end_s is not None else start_s) - start_s
    return detect_facecam(
        pooled, source_width, source_height, duration_s=duration if duration > 0 else None
    )


def _sample_crop(
    cv2,
    video: Path,
    origin_x: float,
    origin_y: float,
    crop_w: float,
    crop_h: float,
    *,
    start_s: float,
    end_s: float | None,
    sample_fps: float,
) -> list[FaceObservation]:
    """Run the landmarker on one corner crop, mapped back to source pixels."""
    import mediapipe as mp
    from mediapipe.tasks.python import BaseOptions
    from mediapipe.tasks.python.vision import (
        FaceLandmarker,
        FaceLandmarkerOptions,
        RunningMode,
    )

    from ... import models
    from .faces import _iter_frames, _to_observation

    model_file = models.ensure("face_landmarker")
    options = FaceLandmarkerOptions(
        base_options=BaseOptions(model_asset_path=str(model_file)),
        running_mode=RunningMode.VIDEO,
        num_faces=2,
        min_face_detection_confidence=0.4,
        min_tracking_confidence=0.4,
    )

    # Integer crop window; the landmarker never sees the rest of the frame.
    x0, y0 = int(origin_x), int(origin_y)
    x1, y1 = int(origin_x + crop_w), int(origin_y + crop_h)

    observations: list[FaceObservation] = []
    with FaceLandmarker.create_from_options(options) as landmarker:
        for timestamp, frame, _, _ in _iter_frames(cv2, video, start_s, end_s, sample_fps):
            # A slice is a strided view, which mp.Image silently misreads as
            # garbage (zero detections, no error). The copy keeps C-contiguity.
            cropped = frame[y0:y1, x0:x1].copy()
            if cropped.size == 0:
                continue
            image = mp.Image(image_format=mp.ImageFormat.SRGB, data=cropped)
            result = landmarker.detect_for_video(image, int(timestamp * 1000))
            for landmarks in result.face_landmarks or []:
                observation = _to_observation(landmarks, timestamp, x1 - x0, y1 - y0)
                if observation is not None:
                    observation.cx += x0
                    observation.cy += y0
                    observation.eye_y += y0
                    observations.append(observation)
    return observations


def _cluster(observations: list[FaceObservation]) -> list[list[FaceObservation]]:
    """Greedy IoU clustering in time order."""
    clusters: list[list[FaceObservation]] = []
    for observation in observations:
        placed = False
        for cluster in clusters:
            if observation.iou(cluster[-1]) >= CLUSTER_IOU:
                cluster.append(observation)
                placed = True
                break
        if not placed:
            clusters.append([observation])
    return clusters


def _to_facecam(
    cluster: list[FaceObservation],
    total_span: float,
    source_width: int,
    source_height: int,
) -> FacecamBox | None:
    span = cluster[-1].t - cluster[0].t
    coverage = span / total_span
    if coverage < MIN_COVERAGE:
        return None

    median_cx = statistics.median(o.cx for o in cluster)
    median_cy = statistics.median(o.cy for o in cluster)
    median_w = statistics.median(o.width for o in cluster)
    median_h = statistics.median(o.height for o in cluster)

    drift_x = max(o.cx for o in cluster) - min(o.cx for o in cluster)
    drift_y = max(o.cy for o in cluster) - min(o.cy for o in cluster)
    if drift_x > MAX_DRIFT_RATIO * source_width or drift_y > MAX_DRIFT_RATIO * source_height:
        return None

    widths = sorted(o.width for o in cluster)
    heights = sorted(o.height for o in cluster)
    # Percentile ratio: robust to a few wild boxes on small faces.
    lo, hi = len(widths) // 10, max(len(widths) - 1 - len(widths) // 10, 1)
    if widths[hi] / max(1.0, widths[lo]) > 1.0 + MAX_SIZE_VARIATION:
        return None
    if heights[hi] / max(1.0, heights[lo]) > 1.0 + MAX_SIZE_VARIATION:
        return None

    corner = _corner_of(median_cx, median_cy, source_width, source_height)
    if corner is None:
        return None

    return _expand_to_shoulders(
        median_cx, median_cy, median_w, median_h, corner, source_width, source_height, coverage
    )


def _corner_of(cx: float, cy: float, width: int, height: int) -> str | None:
    """Which corner a face centre sits in, or None when it is not cornered."""
    nx, ny = cx / width, cy / height
    horizontal = "l" if nx < 0.35 else ("r" if nx > 0.65 else None)
    vertical = "t" if ny < 0.35 else ("b" if ny > 0.65 else None)
    if horizontal is None or vertical is None:
        return None
    return vertical + horizontal


def _expand_to_shoulders(
    cx: float,
    cy: float,
    face_w: float,
    face_h: float,
    corner: str,
    source_width: int,
    source_height: int,
    coverage: float,
) -> FacecamBox:
    """Grow a face box into a head-and-shoulders inset, snapped to its corner.

    The face sits in the upper part of the box with headroom above; the outer
    edges snap to the frame corner with a small margin so the inset reads as
    deliberately placed rather than floating.
    """
    margin = CORNER_MARGIN_RATIO * min(source_width, source_height)
    width = min(source_width, face_w * 2.0)
    height = min(source_height, face_h * 2.8)
    pad = 8.0

    # Anchor the box on the face, then slide it toward its corner as far as
    # containment allows: among positions that still hold the whole face (plus
    # padding), nearest-to-corner wins. Snapping blindly would park a small box
    # beside a face tucked tight into the corner.
    face_left, face_top = cx - face_w / 2, cy - face_h / 2
    face_right, face_bottom = cx + face_w / 2, cy + face_h / 2

    if corner.endswith("l"):
        x = max(margin, face_right + pad - width)
    else:
        x = min(source_width - margin - width, face_left - pad)
    if corner.startswith("t"):
        y = max(margin, face_bottom + pad - height)
    else:
        y = min(source_height - margin - height, face_top - pad)

    # Even dimensions: the box feeds a yuv420p crop downstream.
    width = max(2, round(width) // 2 * 2)
    height = max(2, round(height) // 2 * 2)
    x = min(source_width - width, max(0.0, x))
    y = min(source_height - height, max(0.0, y))

    return FacecamBox(
        x=x, y=y, width=float(width), height=float(height), corner=corner, coverage=coverage
    )

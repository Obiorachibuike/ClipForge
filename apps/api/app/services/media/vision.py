"""Face detection backends and visual saliency.

Detectors are pluggable: the bundled OpenCV Haar cascades work with no model
download, MediaPipe and YuNet are used when present. Every detector reports
whether it is actually available, so the UI can state what is running.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from app.core.config import settings
from app.core.logging import get_logger

log = get_logger(__name__)


@dataclass
class FaceBox:
    x: int
    y: int
    w: int
    h: int
    confidence: float = 1.0

    @property
    def center(self) -> tuple[float, float]:
        return (self.x + self.w / 2.0, self.y + self.h / 2.0)

    @property
    def area(self) -> int:
        return max(0, self.w) * max(0, self.h)

    def iou(self, other: FaceBox) -> float:
        left = max(self.x, other.x)
        top = max(self.y, other.y)
        right = min(self.x + self.w, other.x + other.w)
        bottom = min(self.y + self.h, other.y + other.h)
        if right <= left or bottom <= top:
            return 0.0
        intersection = (right - left) * (bottom - top)
        union = self.area + other.area - intersection
        return intersection / union if union > 0 else 0.0


class FaceDetector(ABC):
    name = "none"
    available = False
    note = ""

    @abstractmethod
    def detect(self, frame_bgr) -> list[FaceBox]: ...

    def close(self) -> None:  # pragma: no cover - optional cleanup
        return None


class NoopDetector(FaceDetector):
    name = "none"
    available = False
    note = "no face detector available"

    def detect(self, frame_bgr) -> list[FaceBox]:
        return []


class HaarFaceDetector(FaceDetector):
    """Bundled OpenCV cascades: frontal + profile. No model download required."""

    name = "haar"
    available = False
    note = "frontal and profile Haar cascades"

    def __init__(self, scale_factor: float = 1.12, min_neighbors: int = 6, min_size_ratio: float = 0.05) -> None:
        import cv2

        cascade_root = Path(cv2.data.haarcascades)
        self.cv2 = cv2
        self.frontal = cv2.CascadeClassifier(str(cascade_root / "haarcascade_frontalface_default.xml"))
        self.profile = cv2.CascadeClassifier(str(cascade_root / "haarcascade_profileface.xml"))
        self.scale_factor = scale_factor
        self.min_neighbors = min_neighbors
        self.min_size_ratio = min_size_ratio
        self.available = not self.frontal.empty()
        if not self.available:
            self.note = "cascade files missing from the OpenCV installation"

    def detect(self, frame_bgr) -> list[FaceBox]:
        cv2 = self.cv2
        if frame_bgr is None or frame_bgr.size == 0:
            return []
        height, width = frame_bgr.shape[:2]
        gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)
        gray = cv2.equalizeHist(gray)
        min_size = max(24, int(min(height, width) * self.min_size_ratio))
        boxes: list[FaceBox] = []
        for cascade, mirrored in ((self.frontal, False), (self.profile, False), (self.profile, True)):
            target = cv2.flip(gray, 1) if mirrored else gray
            found = cascade.detectMultiScale(
                target,
                scaleFactor=self.scale_factor,
                minNeighbors=self.min_neighbors,
                minSize=(min_size, min_size),
                flags=cv2.CASCADE_SCALE_IMAGE,
            )
            for x, y, w, h in found:
                if mirrored:
                    x = width - x - w
                boxes.append(FaceBox(int(x), int(y), int(w), int(h), 1.0))
        return _merge_overlapping(boxes)


class MediaPipeFaceDetector(FaceDetector):
    name = "mediapipe"
    available = False
    note = "MediaPipe short-range face detection"

    def __init__(self, model_selection: int = 1, min_confidence: float = 0.4) -> None:
        import mediapipe as mp

        self.mp_face = mp.solutions.face_detection
        self.detector = self.mp_face.FaceDetection(model_selection=model_selection, min_detection_confidence=min_confidence)
        self.available = True

    def detect(self, frame_bgr) -> list[FaceBox]:
        import cv2

        height, width = frame_bgr.shape[:2]
        rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
        result = self.detector.process(rgb)
        boxes: list[FaceBox] = []
        for detection in result.detections or []:
            relative = detection.location_data.relative_bounding_box
            boxes.append(
                FaceBox(
                    int(relative.xmin * width),
                    int(relative.ymin * height),
                    int(relative.width * width),
                    int(relative.height * height),
                    float(detection.score[0]) if detection.score else 1.0,
                )
            )
        return _merge_overlapping(boxes)

    def close(self) -> None:
        try:
            self.detector.close()
        except Exception:
            pass


class YuNetDetector(FaceDetector):
    """OpenCV DNN face detection (YuNet ONNX). Enabled by supplying a model path."""

    name = "yunet"
    available = False
    note = "OpenCV YuNet DNN detector"

    def __init__(self, model_path: str, score_threshold: float = 0.6) -> None:
        import cv2

        if not model_path or not Path(model_path).exists():
            raise FileNotFoundError(model_path or "no model path")
        self.cv2 = cv2
        self.detector = cv2.FaceDetectorYN.create(model_path, "", (320, 320), score_threshold)
        self.available = True

    def detect(self, frame_bgr) -> list[FaceBox]:
        height, width = frame_bgr.shape[:2]
        self.detector.setInputSize((width, height))
        _, faces = self.detector.detect(frame_bgr)
        boxes: list[FaceBox] = []
        if faces is not None:
            for face in faces:
                x, y, w, h = (float(v) for v in face[:4])
                score = float(face[-1]) if len(face) > 14 else 1.0
                boxes.append(FaceBox(int(x), int(y), int(w), int(h), score))
        return _merge_overlapping(boxes)


def _merge_overlapping(boxes: list[FaceBox], threshold: float = 0.45) -> list[FaceBox]:
    merged: list[FaceBox] = []
    for box in sorted(boxes, key=lambda b: -b.area):
        duplicate = False
        for kept in merged:
            if box.iou(kept) > threshold:
                duplicate = True
                break
        if not duplicate:
            merged.append(box)
    return merged


def build_detector(preference: str | None = None) -> FaceDetector:
    """Choose the best available detector without ever pretending one works."""
    preference = (preference or settings.vision_face_detector or "auto").lower()
    candidates: list[tuple[str, callable]] = []
    if preference in ("auto", "yunet"):
        candidates.append(("yunet", lambda: YuNetDetector(settings.vision_yunet_model)))
    if preference in ("auto", "mediapipe"):
        candidates.append(("mediapipe", MediaPipeFaceDetector))
    if preference in ("auto", "haar"):
        candidates.append(("haar", HaarFaceDetector))
    for label, factory in candidates:
        try:
            detector = factory()
            if detector.available:
                log.info("vision.detector_selected", detector=label)
                return detector
        except Exception as exc:
            log.info("vision.detector_unavailable", detector=label, error=str(exc))
    log.warning("vision.no_detector")
    return NoopDetector()


# ---------------------------------------------------------------- saliency ---
class SaliencyAnalyzer:
    """Locates the main visual subject when no face is present."""

    def __init__(self) -> None:
        self.cv2 = None
        self.spectral = None
        try:
            import cv2

            self.cv2 = cv2
            if hasattr(cv2, "saliency"):
                self.spectral = cv2.saliency.StaticSaliencySpectralResidual_create()
        except Exception:
            self.spectral = None

    def subject_center(self, frame_bgr) -> tuple[float, float, float]:
        """Return (x_ratio, y_ratio, confidence) in 0..1 coordinates."""
        if frame_bgr is None or frame_bgr.size == 0:
            return 0.5, 0.45, 0.0
        cv2 = self.cv2
        if cv2 is None:
            return 0.5, 0.45, 0.0
        height, width = frame_bgr.shape[:2]
        try:
            if self.spectral is not None:
                success, saliency_map = self.spectral.computeSaliency(frame_bgr)
                if success:
                    import numpy as np

                    small = cv2.resize(saliency_map, (64, 64))
                    total = float(small.sum())
                    if total > 1e-6:
                        ys, xs = np.mgrid[0:64, 0:64]
                        cx = float((small * xs).sum() / total) / 64.0
                        cy = float((small * ys).sum() / total) / 64.0
                        confidence = min(1.0, float(small.std()) * 6.0)
                        return cx, cy, confidence
        except Exception:
            pass
        # Fallback: weighted centroid of edge density (a stable proxy for the
        # busiest region of the frame, usually the subject).
        try:
            import numpy as np

            gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)
            edges = cv2.Laplacian(cv2.resize(gray, (96, 96)), cv2.CV_32F)
            magnitude = np.abs(edges)
            magnitude = cv2.GaussianBlur(magnitude, (15, 15), 0)
            total = float(magnitude.sum())
            if total > 1e-6:
                ys, xs = np.mgrid[0:96, 0:96]
                cx = float((magnitude * xs).sum() / total) / 96.0
                cy = float((magnitude * ys).sum() / total) / 96.0
                return cx, cy, 0.35
        except Exception:
            pass
        return 0.5, 0.45, 0.0

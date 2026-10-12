"""Lazy PaddleOCR adapter with a stable application-level response shape."""

from __future__ import annotations

import inspect
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from app.config import Settings, get_settings
from app.extractor import extract_identity
from app.models import OCRDetection, OCRResult
from app.preprocessing import preprocess_image, rotate_image

logger = logging.getLogger(__name__)


class AlgerianIDOCR:
    """CPU-only OCR facade for Algerian identity-card images."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._ocr: dict[str, Any] = {}

    def _load_model(self, language: str) -> Any:
        """Lazily load one PaddleOCR language model and force CPU execution."""

        if language in self._ocr:
            return self._ocr[language]
        os.environ.setdefault("FLAGS_use_mkldnn", "0")
        try:
            from paddleocr import PaddleOCR
        except ImportError as exc:
            raise RuntimeError("PaddleOCR is not installed; run `uv sync` first") from exc

        signature = inspect.signature(PaddleOCR)
        parameters = signature.parameters
        accepts_extra = any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()
        )
        kwargs: dict[str, Any] = {}
        if "lang" in parameters:
            kwargs["lang"] = language
        mobile_recognition_models = {
            "fr": "latin_PP-OCRv5_mobile_rec",
            "ar": "arabic_PP-OCRv5_mobile_rec",
            "en": "en_PP-OCRv5_mobile_rec",
        }
        if (
            self.settings.ocr_model_profile == "mobile"
            and language in mobile_recognition_models
            and "text_detection_model_name" in parameters
            and "text_recognition_model_name" in parameters
        ):
            kwargs.pop("lang", None)
            kwargs["text_detection_model_name"] = "PP-OCRv5_mobile_det"
            kwargs["text_recognition_model_name"] = mobile_recognition_models[language]
        if "device" in parameters:
            kwargs["device"] = "cpu"
        elif "use_gpu" in parameters:
            kwargs["use_gpu"] = False
        for key in (
            "use_doc_orientation_classify",
            "use_doc_unwarping",
            "use_textline_orientation",
        ):
            if key in parameters:
                kwargs[key] = False
        if "use_angle_cls" in parameters:
            kwargs["use_angle_cls"] = False
        if "show_log" in parameters:
            kwargs["show_log"] = False
        if "enable_mkldnn" in parameters or accepts_extra:
            kwargs["enable_mkldnn"] = False
        self._ocr[language] = PaddleOCR(**kwargs)
        logger.info("Loaded PaddleOCR language=%s on CPU", language)
        return self._ocr[language]

    def _languages(self) -> list[str]:
        """Return unique, configured OCR languages while preserving their order."""

        languages = [language.strip() for language in self.settings.ocr_languages.split(",")]
        return list(dict.fromkeys(language for language in languages if language)) or ["fr"]

    def _ocr_image(self, image: np.ndarray) -> list[OCRDetection]:
        """Run all configured OCR languages on one already-preprocessed image."""

        detections: list[OCRDetection] = []
        for language in self._languages():
            engine = self._load_model(language)
            output = (
                engine.predict(image)
                if hasattr(engine, "predict")
                else engine.ocr(image, cls=False)
            )
            for result in output or []:
                detections = _merge_detections(detections, self._parse_result(result))
        return detections

    @staticmethod
    def _orientation_score(detections: list[OCRDetection]) -> float:
        """Score an orientation using OCR confidence and identity-document evidence."""

        text = " ".join(detection.text.upper() for detection in detections)
        digits = "".join(character for character in text if character.isdigit())
        average_confidence = (
            sum(detection.confidence for detection in detections) / len(detections)
            if detections
            else 0.0
        )
        score = average_confidence + min(len(detections), 30) / 150
        if len(digits) >= 18:
            score += 0.45
        if "بطاقة" in text or "التعريف" in text or "NATIONAL" in text or "PASSEPORT" in text:
            score += 0.35
        if any(separator in text for separator in (".", "/", "-")):
            score += 0.10
        return score

    @classmethod
    def _needs_orientation_retry(cls, detections: list[OCRDetection]) -> bool:
        """Retry rotations when the first pass lacks strong identity-document evidence."""

        text = " ".join(detection.text.upper() for detection in detections)
        digits = "".join(character for character in text if character.isdigit())
        has_document_marker = any(
            marker in text for marker in ("بطاقة", "التعريف", "NATIONAL", "PASSPORT", "PASSEPORT")
        )
        return len(detections) < 8 or len(digits) < 18 or not has_document_marker

    @staticmethod
    def _json_mapping(value: Any) -> dict[str, Any] | None:
        if isinstance(value, dict):
            return value
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError:
                return None
            return parsed if isinstance(parsed, dict) else None
        return None

    @classmethod
    def _parse_result(cls, result: Any) -> list[OCRDetection]:
        """Parse PaddleOCR 2.x and 3.x result formats."""

        mapping = cls._json_mapping(result)
        if mapping is None and hasattr(result, "json"):
            raw_json = result.json() if callable(result.json) else result.json
            mapping = cls._json_mapping(raw_json)
        if mapping is not None:
            texts = mapping.get("rec_texts", mapping.get("texts", []))
            scores = mapping.get("rec_scores", mapping.get("scores", []))
            boxes = mapping.get("rec_polys", mapping.get("dt_polys", mapping.get("rec_boxes", [])))
            return [
                OCRDetection(
                    text=str(text),
                    confidence=float(scores[index]) if index < len(scores) else 0.0,
                    bbox=_to_list(boxes[index]) if index < len(boxes) else [],
                )
                for index, text in enumerate(texts)
                if str(text).strip()
            ]
        if isinstance(result, (list, tuple)):
            detections: list[OCRDetection] = []
            for item in result:
                if (
                    isinstance(item, (list, tuple))
                    and len(item) == 2
                    and isinstance(item[1], (list, tuple))
                ):
                    box, text_score = item
                    if len(text_score) >= 2:
                        detections.append(
                            OCRDetection(
                                text=str(text_score[0]),
                                confidence=float(text_score[1]),
                                bbox=_to_list(box),
                            )
                        )
                else:
                    detections.extend(cls._parse_result(item))
            return detections
        return []

    def scan(self, image_path: str) -> OCRResult:
        """Preprocess and OCR an image path, returning structured identity fields."""

        started = time.perf_counter()
        try:
            debug_dir = self._debug_dir(image_path) if self.settings.ocr_debug else None
            processed = preprocess_image(image_path, debug_dir=debug_dir)
            detections = self._ocr_image(processed.image)
            selected_image = processed.image
            selected_angle = 0
            orientation_scores = {"0": self._orientation_score(detections)}
            if self._needs_orientation_retry(detections):
                for angle in (90, 180, 270):
                    candidate_image = rotate_image(processed.image, angle)
                    candidate_detections = self._ocr_image(candidate_image)
                    candidate_score = self._orientation_score(candidate_detections)
                    orientation_scores[str(angle)] = candidate_score
                    if candidate_score > self._orientation_score(detections):
                        detections = candidate_detections
                        selected_image = candidate_image
                        selected_angle = angle
            raw_text = [detection.text for detection in detections]
            extracted = extract_identity(raw_text, detections)
            if debug_dir is not None:
                self._save_ocr_debug(
                    selected_image, detections, extracted.fields.model_dump(), debug_dir
                )
                (debug_dir / "09_orientation.json").write_text(
                    json.dumps(
                        {"selected_angle_clockwise": selected_angle, "scores": orientation_scores},
                        indent=2,
                    ),
                    encoding="utf-8",
                )
            confidence = (
                sum(d.confidence for d in detections) / len(detections) if detections else 0.0
            )
            return OCRResult(
                success=bool(detections),
                confidence=round(confidence, 4),
                fields=extracted.fields,
                raw_text=raw_text,
                detections=detections,
                processing_time_ms=round((time.perf_counter() - started) * 1000, 2),
            )
        except (OSError, ValueError, RuntimeError) as exc:
            logger.exception("OCR scan failed for %s", image_path)
            return OCRResult(
                success=False,
                error=str(exc),
                processing_time_ms=round((time.perf_counter() - started) * 1000, 2),
            )

    def _debug_dir(self, image_path: str) -> Path:
        """Return a stable, per-image debug directory to avoid overwriting scans."""

        return Path(self.settings.ocr_debug_dir) / Path(image_path).stem

    @staticmethod
    def _save_ocr_debug(
        image: np.ndarray,
        detections: list[OCRDetection],
        fields: dict[str, Any],
        debug_dir: Path,
    ) -> None:
        """Save an OCR overlay and JSON payload beside preprocessing stages."""

        overlay = image.copy()
        for index, detection in enumerate(detections, start=1):
            if len(detection.bbox) < 4:
                continue
            points: Any = np.asarray(detection.bbox, dtype=np.int32).reshape((-1, 1, 2))
            cv2.polylines(overlay, [points], isClosed=True, color=(0, 255, 0), thickness=2)
            x, y = points[0, 0]
            label = f"{index}: {detection.text[:36]} ({detection.confidence:.2f})"
            cv2.putText(
                overlay,
                label,
                (int(x), max(int(y) - 6, 16)),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.45,
                (0, 0, 255),
                1,
                cv2.LINE_AA,
            )
        try:
            if not cv2.imwrite(str(debug_dir / "07_ocr_detections.jpg"), overlay):
                logger.warning("Could not save OCR overlay in %s", debug_dir)
            payload = {
                "fields": fields,
                "detections": [detection.model_dump() for detection in detections],
            }
            (debug_dir / "08_ocr_result.json").write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError:
            logger.exception("Could not save OCR debug output in %s", debug_dir)


def _to_list(value: Any) -> list[list[float]]:
    """Convert NumPy Paddle output into JSON-compatible box coordinates."""

    if hasattr(value, "tolist"):
        value = value.tolist()
    if not isinstance(value, list):
        return []
    if value and not isinstance(value[0], list):
        return [[float(number) for number in value]]
    return [[float(number) for number in point] for point in value]


def _merge_detections(
    existing: list[OCRDetection], new_detections: list[OCRDetection]
) -> list[OCRDetection]:
    """Keep the highest-confidence text for substantially overlapping OCR boxes."""

    merged = list(existing)
    for candidate in new_detections:
        overlap_index = next(
            (
                index
                for index, current in enumerate(merged)
                if _bounding_box_iou(current.bbox, candidate.bbox) >= 0.75
            ),
            None,
        )
        if overlap_index is None:
            merged.append(candidate)
        elif candidate.confidence > merged[overlap_index].confidence:
            merged[overlap_index] = candidate
    return merged


def _bounding_box_iou(left: list[list[float]], right: list[list[float]]) -> float:
    """Return axis-aligned intersection-over-union for two OCR polygons."""

    if not left or not right:
        return 0.0
    left_x = [point[0] for point in left]
    left_y = [point[1] for point in left]
    right_x = [point[0] for point in right]
    right_y = [point[1] for point in right]
    intersection_width = max(0.0, min(max(left_x), max(right_x)) - max(min(left_x), min(right_x)))
    intersection_height = max(0.0, min(max(left_y), max(right_y)) - max(min(left_y), min(right_y)))
    intersection = intersection_width * intersection_height
    left_area = (max(left_x) - min(left_x)) * (max(left_y) - min(left_y))
    right_area = (max(right_x) - min(right_x)) * (max(right_y) - min(right_y))
    union = left_area + right_area - intersection
    return intersection / union if union > 0 else 0.0

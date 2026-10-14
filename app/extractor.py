"""Heuristic field extraction for French and bilingual Algerian ID cards."""

from __future__ import annotations

import re
from dataclasses import dataclass

from rapidfuzz.fuzz import ratio

from app.models import DocumentType, IdentityFields, OCRDetection
from app.utils import compact_digits, normalize_label, normalize_text

FRENCH_LABELS = {
    "last_name": ("NOM", "SURNAME", "FAMILY NAME"),
    "first_name": ("PRENOM", "GIVEN NAME", "FIRST NAME"),
    "birth_date": ("NE LE", "DATE DE NAISSANCE", "BORN", "DATE OF BIRTH"),
    "birth_place": ("LIEU DE NAISSANCE", "BIRTH PLACE", "BORN IN"),
    "gender": ("SEXE", "SEX", "GENRE"),
    "nin": ("NIN", "NUMERO D IDENTIFICATION NATIONALE", "NATIONAL ID NUMBER"),
    "document_number": ("NO", "NUMERO", "DOCUMENT NUMBER", "CARD NUMBER"),
}

DATE_PATTERN = re.compile(r"\b(\d{1,2})\s*[/.-]\s*(\d{1,2})\s*[/.-]\s*(\d{2,4})\b")
YEAR_FIRST_DATE_PATTERN = re.compile(r"\b(\d{4})\s*[/.-]\s*(\d{1,2})\s*[/.-]\s*(\d{1,2})\b")
DIGIT_GROUP_PATTERN = re.compile(r"(?<!\d)(?:\d[\s.-]*){6,20}\d(?!\d)")
ARABIC_PATTERN = re.compile(r"[\u0600-\u06ff]")
ARABIC_NAME_LABELS = ("الإسم", "الاسم", "اللقب", "اسم ولقب")
ARABIC_LABELS = {
    "first_name": ("الإسم", "الاسم", "الاسم الشخصي"),
    "last_name": ("اللقب", "اسم العائلة", "الاسم العائلي"),
    "birth_date": ("تاريخ الميلاد", "تاريخ الازدياد", "تاريخ الولادة"),
    "birth_place": ("مكان الميلاد", "مكان الازدياد", "مكان الولادة"),
    "gender": ("الجنس", "النوع"),
    "nin": ("الرقم الوطني", "رقم التعريف الوطني", "رقم التعريف"),
    "document_number": ("رقم البطاقة", "رقم الوثيقة", "رقم البطاقة الوطنية"),
}
ARABIC_NON_NAME_MARKERS = (
    "بطاقة",
    "التعريف",
    "الوطنية",
    "الجمهورية",
    "الشعبية",
    "الجزائر",
    "سلطة",
    "الإصدار",
    "الإصداو",
    "دالي",
    "مكان الميلاد",
    "الجنس",
    "الإسم",
    "الاسم",
    "اللقب",
    "ذكر",
    "أنثى",
    "تاريخ",
    "فصيلة",
    "الدم",
    "الرقم",
)
PASSPORT_MARKERS = ("PASSPORT", "PASSEPORT", "P<DZA")
DRIVING_LICENSE_MARKERS = (
    "PERMIS DE CONDUIRE",
    "DRIVING LICENCE",
    "DRIVING LICENSE",
    "DRIVER LICENSE",
    "رخصة السياقة",
)
NATIONAL_ID_MARKERS = (
    "CARTE NATIONALE D IDENTITE",
    "CARTE NATIONALE",
    "NATIONAL IDENTITY CARD",
    "CNI",
    "بطاقة التعريف الوطني",
    "بطاقة التعريف الوطنية",
)


@dataclass(frozen=True)
class ExtractedIdentity:
    """Extraction output with a confidence estimate for the field parser."""

    fields: IdentityFields
    confidence: float
    field_confidence: dict[str, float]


def _is_label(line: str, candidates: tuple[str, ...]) -> bool:
    normalized = normalize_label(line)
    return any(ratio(normalized, normalize_label(candidate)) >= 72 for candidate in candidates)


def _value_after_label(line: str, candidates: tuple[str, ...]) -> str | None:
    normalized = normalize_text(line)
    before, separator, after = normalized.partition(":")
    if separator and _is_label(before, candidates):
        return after.strip() or None
    normalized_label = normalize_label(normalized)
    for candidate in candidates:
        label = normalize_label(candidate)
        if not label:
            continue
        position = normalized_label.find(label)
        if position >= 0:
            value = normalized[position + len(label) :].lstrip(" :-")
            if value:
                return value
    return None


def _find_labeled_value(lines: list[str], key: str) -> str | None:
    candidates = FRENCH_LABELS[key]
    for index, line in enumerate(lines):
        value = _value_after_label(line, candidates)
        if value:
            return value
        if _is_label(line, candidates) and index + 1 < len(lines):
            next_line = lines[index + 1]
            if not _is_label(next_line, candidates):
                return next_line
            if index > 0 and not _is_label(lines[index - 1], candidates):
                return lines[index - 1]
        if _is_label(line, candidates) and index > 0:
            previous_line = lines[index - 1]
            if not _is_label(previous_line, candidates):
                return previous_line
    return None


def _clean_name(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = re.sub(r"[^\wÀ-ÿ' -]", " ", value, flags=re.UNICODE)
    cleaned = normalize_text(cleaned).upper()
    return cleaned or None


def _arabic_label_value(lines: list[str], key: str) -> str | None:
    """Find an Arabic value printed beside or near a recognized Arabic label."""

    labels = ARABIC_LABELS[key]
    for index, line in enumerate(lines):
        label = next((candidate for candidate in labels if candidate in line), None)
        if not label:
            continue
        inline = line.split(label, 1)[1].lstrip(" :-")
        if inline:
            return inline
        indexes = (index - 1, index + 1) if key == "birth_place" else (index + 1, index - 1)
        for candidate_index in indexes:
            if 0 <= candidate_index < len(lines):
                candidate = lines[candidate_index]
                if (
                    key == "birth_place"
                    and ARABIC_PATTERN.search(candidate)
                    and candidate
                    not in {item for values in ARABIC_LABELS.values() for item in values}
                ):
                    return candidate
                if _arabic_name_candidate(candidate) or key in {"nin", "document_number"}:
                    return candidate
    return None


def _arabic_gender(lines: list[str]) -> str | None:
    """Read common Arabic gender values, allowing small OCR substitutions."""

    for line in lines:
        if "أنثى" in line or "انثى" in line:
            return "F"
        if any(ratio(token, "ذكر") >= 65 for token in line.split()):
            return "M"
    return None


def _arabic_name_from_detections(
    lines: list[str],
    detections: list[OCRDetection] | None,
) -> str | None:
    """Prefer Arabic text spatially closest to the Arabic name label."""

    if not detections:
        return None
    label = next((item for item in detections if item.text in ARABIC_NAME_LABELS), None)
    if label is None or not label.bbox:
        return None
    label_y = sum(point[1] for point in label.bbox) / len(label.bbox)
    candidates = [
        item
        for item in detections
        if _arabic_name_candidate(item.text) and not any(char.isdigit() for char in item.text)
    ]
    if not candidates:
        return None
    candidate = min(
        candidates,
        key=lambda item: (
            abs(sum(point[1] for point in item.bbox) / len(item.bbox) - label_y)
            if item.bbox
            else float("inf")
        ),
    )
    return candidate.text


def _normalize_date(value: str | None) -> str | None:
    if not value:
        return None
    year_first_match = YEAR_FIRST_DATE_PATTERN.search(value)
    if year_first_match:
        year, month, day = (int(part) for part in year_first_match.groups())
    else:
        match = DATE_PATTERN.search(value)
        if not match:
            return None
        day, month, year = (int(part) for part in match.groups())
    if year < 100:
        year += 2000 if year < 30 else 1900
    if not 1 <= month <= 12 or not 1 <= day <= 31:
        return None
    return f"{year:04d}-{month:02d}-{day:02d}"


def _digits_from_labeled(lines: list[str], key: str) -> str | None:
    value = _find_labeled_value(lines, key)
    digits = compact_digits(value) if value else ""
    return digits or None


def _find_numbers(lines: list[str]) -> tuple[str | None, str | None]:
    candidates = [
        compact_digits(match.group())
        for line in lines
        for match in DIGIT_GROUP_PATTERN.finditer(line)
        if not _normalize_date(line)
    ]
    candidates = [candidate for candidate in candidates if 6 <= len(candidate) <= 20]
    nin = next((candidate for candidate in candidates if len(candidate) == 18), None)
    document = next(
        (candidate for candidate in candidates if candidate != nin and 8 <= len(candidate) <= 12),
        None,
    )
    return nin, document


def _mrz_names(lines: list[str]) -> tuple[str | None, str | None]:
    """Extract surname and given names from a passport MRZ name line."""

    for line in lines:
        compact = re.sub(r"\s+", "", line.upper())
        if not compact.startswith("P<") or "<<" not in compact:
            continue
        surname_part, given_part = compact[5:].split("<<", 1)
        surname = surname_part.replace("<", " ").strip(" <")
        given = given_part.replace("<", " ").strip(" <")
        return surname or None, given or None
    return None, None


def _detect_document_type(lines: list[str]) -> tuple[DocumentType, float]:
    """Classify a document using strong OCR labels and MRZ evidence.

    A photograph cannot prove that a document contains a biometric chip. The
    classifier therefore reports its visible document category only.
    """

    combined = " ".join(lines).upper()
    normalized = normalize_label(combined)
    if any(marker in combined for marker in PASSPORT_MARKERS) or re.search(
        r"\bP<[A-Z]{3}", combined
    ):
        return "passport", 0.98
    if any(marker in combined for marker in DRIVING_LICENSE_MARKERS) or all(
        token in combined for token in ("رخصة", "السياقة")
    ):
        return "driving_license", 0.95
    if any(
        marker in combined for marker in ("بطاقة التعريف الوطني", "بطاقة التعريف الوطنية")
    ) or all(token in combined for token in ("بطاقة", "التعريف", "الوطنية")):
        return "national_id", 0.98
    if any(
        normalized_marker and normalized_marker in normalized
        for marker in NATIONAL_ID_MARKERS
        if (normalized_marker := normalize_label(marker))
    ):
        return "national_id", 0.95
    return "unknown", 0.0


def _arabic_name_candidate(line: str) -> bool:
    """Return whether an Arabic OCR line looks like a person name, not a label."""

    if len(ARABIC_PATTERN.findall(line)) < 3 or any(char.isdigit() for char in line):
        return False
    return not any(marker in line for marker in ARABIC_NON_NAME_MARKERS)


def extract_identity(
    raw_text: list[str] | str, detections: list[OCRDetection] | None = None
) -> ExtractedIdentity:
    """Extract identity fields using labels, Arabic-line detection, and numeric fallbacks."""

    text_lines = raw_text.splitlines() if isinstance(raw_text, str) else raw_text
    lines = [normalize_text(line) for line in text_lines if normalize_text(line)]
    arabic_name = _arabic_name_from_detections(lines, detections) or next(
        (line for line in lines if _arabic_name_candidate(line) and len(line.split()) >= 2),
        None,
    )
    arabic_name = arabic_name or next(
        (line for line in lines if _arabic_name_candidate(line)),
        None,
    )
    arabic_first_name = _arabic_label_value(lines, "first_name")
    arabic_last_name = _arabic_label_value(lines, "last_name")
    arabic_name = arabic_name or arabic_first_name or arabic_last_name
    gender_value = _find_labeled_value(lines, "gender")
    gender: str | None = None
    if gender_value:
        upper = normalize_label(gender_value)
        if "FEM" in upper or upper in {"F", "H"}:
            gender = "F"
        elif "MASC" in upper or upper in {"M", "HOMME"}:
            gender = "M"
    gender = gender or _arabic_gender(lines)
    nin = (
        _digits_from_labeled(lines, "nin")
        or compact_digits(_arabic_label_value(lines, "nin") or "")
        or None
    )
    document_number = (
        _digits_from_labeled(lines, "document_number")
        or compact_digits(_arabic_label_value(lines, "document_number") or "")
        or None
    )
    fallback_nin, fallback_document = _find_numbers(lines)
    nin = nin if len(nin or "") == 18 else fallback_nin
    document_number = (
        document_number if document_number and document_number != nin else fallback_document
    )
    document_type, document_type_confidence = _detect_document_type(lines)
    mrz_last_name, mrz_first_name = _mrz_names(lines)

    fields = IdentityFields(
        document_type=document_type,
        document_type_confidence=document_type_confidence,
        first_name=_clean_name(_find_labeled_value(lines, "first_name")) or mrz_first_name,
        last_name=_clean_name(_find_labeled_value(lines, "last_name")) or mrz_last_name,
        first_name_ar=_arabic_label_value(lines, "first_name"),
        last_name_ar=_arabic_label_value(lines, "last_name"),
        arabic_name=arabic_name,
        birth_date=_normalize_date(_find_labeled_value(lines, "birth_date")) or _oldest_date(lines),
        birth_place=_clean_name(_find_labeled_value(lines, "birth_place"))
        or _arabic_label_value(lines, "birth_place"),
        gender=gender,
        nin=nin,
        document_number=document_number,
    )
    extractable = fields.model_dump(exclude={"document_type", "document_type_confidence"})
    detections_by_name: dict[str, float] = {}
    for key, value in extractable.items():
        if value is None:
            detections_by_name[key] = 0.0
            continue
        value_text = normalize_text(str(value)).casefold()
        normalized_value = normalize_label(str(value))
        numeric_value = compact_digits(str(value))
        supporting = []
        for detection in detections or []:
            detection_text = normalize_text(detection.text).casefold()
            if (numeric_value and numeric_value in compact_digits(detection.text)) or (
                value_text and value_text in detection_text
            ) or (normalized_value and normalized_value in normalize_label(detection.text)):
                supporting.append(detection.confidence)
        # A parsed value may come from a multi-token fallback. Keep its score
        # conservative when no OCR box directly supports it.
        detections_by_name[key] = round(max(supporting) if supporting else 0.0, 4)
    populated = sum(value is not None for value in extractable.values())
    return ExtractedIdentity(
        fields=fields,
        confidence=populated / len(extractable),
        field_confidence={"document_type": document_type_confidence, **detections_by_name},
    )


def _oldest_date(lines: list[str]) -> str | None:
    """Use the oldest detected date as a birth-date fallback when labels are lost."""

    dates = [date for line in lines if (date := _normalize_date(line))]
    return min(dates) if dates else None

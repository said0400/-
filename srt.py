"""
📄 SRT Subtitle Generator v2.0 — Final Production Edition

Features:
  ✅ Sentence-level SRT (for upload as captions)
  ✅ Word-level SRT (for karaoke effects)
  ✅ Absolute paths
  ✅ Min duration enforcement
  ✅ UTF-8 encoding
  ✅ round() instead of int() for timestamp precision
  ✅ Auto-create output directory
  ✅ %s logging (no f-string)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# ═════════════════════════════════════════════════════════════════════════════
# CONSTANTS
# ═════════════════════════════════════════════════════════════════════════════

# Minimum durations (seconds)
MIN_SENTENCE_DURATION = 0.5
MIN_WORD_DURATION     = 0.08

# Default fallback durations
DEFAULT_SENTENCE_DURATION = 3.0
DEFAULT_WORD_DURATION     = 0.4

# Time conversion
MS_PER_SECOND = 1000
MS_PER_MINUTE = 60_000
MS_PER_HOUR   = 3_600_000


# ═════════════════════════════════════════════════════════════════════════════
# DATA CLASSES
# ═════════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class SubtitleEntry:
    """مدخل واحد في ملف SRT."""
    index: int
    start: float
    end:   float
    text:  str

    def to_srt_block(self) -> list[str]:
        """تحويل لـ SRT block (4 lines)."""
        return [
            str(self.index),
            f"{_format_timestamp(self.start)} --> "
            f"{_format_timestamp(self.end)}",
            self.text,
            "",
        ]


# ═════════════════════════════════════════════════════════════════════════════
# TIMESTAMP FORMATTING
# ═════════════════════════════════════════════════════════════════════════════

def _format_timestamp(seconds: float) -> str:
    """
    تحويل ثوانٍ إلى SRT timestamp.

    Format: HH:MM:SS,mmm

    Uses round() instead of int() for floating-point precision.

    Examples:
        >>> _format_timestamp(3.5)
        "00:00:03,500"
        >>> _format_timestamp(3661.25)
        "01:01:01,250"
        >>> _format_timestamp(3.1)
        "00:00:03,100"
    """
    safe_seconds = max(0.0, seconds)
    total_ms     = round(safe_seconds * MS_PER_SECOND)

    hours        = total_ms // MS_PER_HOUR
    minutes      = (total_ms % MS_PER_HOUR) // MS_PER_MINUTE
    secs         = (total_ms % MS_PER_MINUTE) // MS_PER_SECOND
    milliseconds = total_ms % MS_PER_SECOND

    return (
        f"{hours:02d}:{minutes:02d}:"
        f"{secs:02d},{milliseconds:03d}"
    )


# ═════════════════════════════════════════════════════════════════════════════
# DURATION HELPERS
# ═════════════════════════════════════════════════════════════════════════════

def _ensure_min_duration(
    start:        float,
    end:          float,
    min_duration: float,
) -> tuple[float, float]:
    """
    ضمان أن المدة لا تقل عن الحد الأدنى.

    Returns:
        (start, end) معدلة
    """
    safe_start = max(0.0, start)

    # إذا end <= start أو المدة قصيرة جداً
    if end <= safe_start or (end - safe_start) < min_duration:
        return safe_start, safe_start + min_duration

    return safe_start, end


# ═════════════════════════════════════════════════════════════════════════════
# FILE WRITING
# ═════════════════════════════════════════════════════════════════════════════

def _write_srt_file(
    entries:     list[SubtitleEntry],
    output_path: str,
) -> Optional[Path]:
    """
    كتابة entries إلى ملف SRT.

    Returns:
        Path للملف أو None إذا entries فارغة
    """
    if not entries:
        return None

    path = Path(output_path).resolve()

    # تأكد من وجود المجلد
    path.parent.mkdir(parents=True, exist_ok=True)

    lines = []
    for entry in entries:
        lines.extend(entry.to_srt_block())

    path.write_text(
        "\n".join(lines),
        encoding = "utf-8",
    )

    return path


# ═════════════════════════════════════════════════════════════════════════════
# SENTENCE-LEVEL SRT
# ═════════════════════════════════════════════════════════════════════════════

def _extract_sentence_entries(
    aligned: list[dict],
) -> list[SubtitleEntry]:
    """استخراج subtitle entries من aligned (مستوى الجملة)."""
    entries: list[SubtitleEntry] = []

    for item in aligned:
        sentence = item.get("sentence", "").strip()
        if not sentence:
            continue

        raw_start = float(item.get("start", 0.0))
        raw_end   = float(item.get(
            "end",
            raw_start + DEFAULT_SENTENCE_DURATION,
        ))

        start, end = _ensure_min_duration(
            raw_start, raw_end,
            MIN_SENTENCE_DURATION,
        )

        entries.append(SubtitleEntry(
            index = len(entries) + 1,
            start = start,
            end   = end,
            text  = sentence,
        ))

    return entries


def generate_srt(
    aligned:     list[dict],
    output_path: str,
) -> Optional[Path]:
    """
    توليد SRT على مستوى الجملة (للرفع كترجمة).

    Args:
        aligned:     [{sentence, start, end, words:[...]}, ...]
        output_path: مسار ملف SRT

    Returns:
        Path للملف أو None إذا فشل
    """
    if not aligned:
        return None

    entries = _extract_sentence_entries(aligned)

    if not entries:
        return None

    path = _write_srt_file(entries, output_path)

    if path:
        log.info(
            "  📄 SRT (%d subtitles) → %s",
            len(entries), path.name
        )

    return path


# ═════════════════════════════════════════════════════════════════════════════
# WORD-LEVEL SRT
# ═════════════════════════════════════════════════════════════════════════════

def _extract_word_entries(
    aligned: list[dict],
) -> list[SubtitleEntry]:
    """استخراج subtitle entries من aligned (مستوى الكلمة)."""
    entries: list[SubtitleEntry] = []

    for item in aligned:
        for word_data in item.get("words", []):
            word = word_data.get("word", "").strip()
            if not word:
                continue

            raw_start = float(word_data.get("start", 0.0))
            raw_end   = float(word_data.get(
                "end",
                raw_start + DEFAULT_WORD_DURATION,
            ))

            start, end = _ensure_min_duration(
                raw_start, raw_end,
                MIN_WORD_DURATION,
            )

            entries.append(SubtitleEntry(
                index = len(entries) + 1,
                start = start,
                end   = end,
                text  = word,
            ))

    return entries


def generate_word_srt(
    aligned:     list[dict],
    output_path: str,
) -> Optional[Path]:
    """
    توليد SRT على مستوى الكلمة (Karaoke style).

    استخدم للـ closed captions للحصول على تفاعل أكبر.

    Args:
        aligned:     [{sentence, start, end, words:[...]}, ...]
        output_path: مسار ملف SRT

    Returns:
        Path للملف أو None إذا فشل
    """
    if not aligned:
        return None

    entries = _extract_word_entries(aligned)

    if not entries:
        return None

    path = _write_srt_file(entries, output_path)

    if path:
        log.info(
            "  📄 Word SRT (%d words) → %s",
            len(entries), path.name
        )

    return path

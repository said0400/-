"""
🎤 Word-level Alignment via stable-ts v2.0 — Final Production Edition

Features:
  ✅ Powered by stable-ts (latest, maintained)
  ✅ Built-in word-level alignment (no pyannote needed)
  ✅ No HF_TOKEN required
  ✅ Thread-safe singleton model loading
  ✅ Backward compatible API
  ✅ Same output format as old WhisperX-based sync.py
  ✅ Fallback to minimal options on TypeError
  ✅ Global word counter for visible_word_count
  ✅ MIN_WORD_DURATION enforced in fallback too
  ✅ Better FPS parsing (avg_frame_rate priority)
  ✅ MIN_REMAP_WORDS_RATIO = 0.15 (more flexible)
  ✅ Word timestamps sorted before remap
  ✅ total_duration=0 fallback protection
  ✅ ffprobe not found → -1.0 (distinct from 0.0)

IMPORTANT:
  Audio passed = Audio that will play in final video
"""

from __future__ import annotations

import logging
import subprocess
import threading
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

# ═════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ═════════════════════════════════════════════════════════════════════════════

# Model settings
WHISPER_MODEL  = "medium"
WHISPER_DEVICE = "cpu"
COMPUTE_TYPE   = "int8"

# Cache directory (created on first use, not on import)
MODEL_CACHE_DIR = Path.home() / ".cache" / "stable-ts"

# Language mapping
LANG_MAP: dict[str, str] = {
    "ar": "ar",
    "fr": "fr",
    "en": "en",
}

# Timeouts
FFPROBE_TIMEOUT = 15

# Validation thresholds
MIN_REMAP_WORDS_RATIO = 0.15   # 15% tolerance (was 0.05)
MIN_WORD_DURATION     = 0.05   # 50ms minimum

# stable-ts options
TRANSCRIBE_OPTIONS = {
    "word_timestamps":   True,
    "regroup":           True,
    "suppress_silence":  True,
    "suppress_word_ts":  False,
    "use_word_position": True,
    "vad":               False,
    "verbose":           None,
}


# ═════════════════════════════════════════════════════════════════════════════
# GLOBAL STATE (Thread-safe)
# ═════════════════════════════════════════════════════════════════════════════

_MODEL:      Any            = None
_MODEL_LOCK: threading.Lock = threading.Lock()


# ═════════════════════════════════════════════════════════════════════════════
# DATA CLASSES
# ═════════════════════════════════════════════════════════════════════════════

@dataclass
class WordEntry:
    """كلمة مع timestamps."""
    word:  str
    start: float
    end:   float
    s_idx: int
    w_idx: int

    def to_dict(self) -> dict:
        return {
            "word":  self.word,
            "start": self.start,
            "end":   self.end,
            "s_idx": self.s_idx,
            "w_idx": self.w_idx,
        }


# ═════════════════════════════════════════════════════════════════════════════
# AUDIO DURATION
# ═════════════════════════════════════════════════════════════════════════════

def get_audio_duration(path: str) -> float:
    """
    الحصول على مدة ملف صوتي بالثواني.

    Returns:
        المدة (موجبة) أو 0.0 عند الفشل
        -1.0 إذا ffprobe غير موجود
    """
    try:
        r = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                path,
            ],
            capture_output = True,
            text           = True,
            timeout        = FFPROBE_TIMEOUT,
        )

        output = r.stdout.strip()
        try:
            return float(output) if output else 0.0
        except ValueError:
            return 0.0

    except FileNotFoundError:
        log.error("  ❌ ffprobe not found — install FFmpeg")
        return -1.0
    except subprocess.TimeoutExpired:
        log.warning("  ⚠️  ffprobe timeout")
        return 0.0
    except Exception as e:
        log.debug("  ffprobe error: %s", e)
        return 0.0


# ═════════════════════════════════════════════════════════════════════════════
# MODEL LOADING (Thread-safe Singleton)
# ═════════════════════════════════════════════════════════════════════════════

def _load_model() -> Any:
    """
    تحميل stable-ts model (singleton, thread-safe).

    Returns:
        Model instance أو None
    """
    global _MODEL

    # Fast path (no lock)
    if _MODEL is not None:
        return _MODEL

    with _MODEL_LOCK:
        # Double-check inside lock
        if _MODEL is not None:
            return _MODEL

        try:
            import stable_whisper
        except ImportError as e:
            log.error("  ❌ stable-ts not installed: %s", e)
            log.error("     Install: pip install stable-ts")
            return None

        try:
            # Create cache dir on first use
            MODEL_CACHE_DIR.mkdir(parents=True, exist_ok=True)

            log.info(
                "  📥 Loading stable-ts '%s'...",
                WHISPER_MODEL
            )
            t0 = time.time()

            _MODEL = stable_whisper.load_faster_whisper(
                WHISPER_MODEL,
                device        = WHISPER_DEVICE,
                compute_type  = COMPUTE_TYPE,
                download_root = str(MODEL_CACHE_DIR),
            )

            elapsed = time.time() - t0
            log.info("  ✅ Loaded in %.1fs", elapsed)

        except Exception as e:
            log.error("  ❌ Model load failed: %s", e)
            traceback.print_exc()
            return None

    return _MODEL


# ═════════════════════════════════════════════════════════════════════════════
# TRANSCRIPTION
# ═════════════════════════════════════════════════════════════════════════════

def _transcribe_with_alignment(
    model:      Any,
    audio_path: str,
    lang:       str,
) -> Optional[Any]:
    """
    تشغيل stable-ts transcribe مع alignment مدمج.

    Returns:
        WhisperResult object أو None
    """
    log.info("  📝 Transcribing + Aligning (%s)...", lang)
    t0 = time.time()

    try:
        result = model.transcribe_stable(
            audio_path,
            language = lang,
            **TRANSCRIBE_OPTIONS,
        )

        elapsed = time.time() - t0
        n_segs  = len(result.segments) if result else 0

        log.info(
            "  ✅ %d segments in %.1fs", n_segs, elapsed
        )

        return result

    except TypeError as e:
        # Fallback for unknown options in different stable-ts versions
        log.warning(
            "  ⚠️  transcribe option error: %s — retrying minimal",
            e
        )
        try:
            result = model.transcribe_stable(
                audio_path,
                language        = lang,
                word_timestamps = True,
            )
            elapsed = time.time() - t0
            n_segs  = len(result.segments) if result else 0
            log.info(
                "  ✅ %d segments in %.1fs (minimal options)",
                n_segs, elapsed
            )
            return result
        except Exception as e2:
            log.error("  ❌ Transcribe fallback failed: %s", e2)
            return None

    except Exception as e:
        log.error("  ❌ Transcribe failed: %s", e)
        traceback.print_exc()
        return None


# ═════════════════════════════════════════════════════════════════════════════
# DATA EXTRACTION FROM stable-ts
# ═════════════════════════════════════════════════════════════════════════════

def _is_valid_timestamp(start: float, end: float) -> bool:
    """التحقق من صحة timestamps."""
    return (
        start >= 0 and
        end > start and
        (end - start) >= MIN_WORD_DURATION
    )


def _extract_words_from_segment(
    segment: Any,
    s_idx:   int,
) -> list[WordEntry]:
    """
    استخراج الكلمات من segment.

    stable-ts WordTiming has:
        .word  → str
        .start → float
        .end   → float
    """
    seg_words: list[WordEntry] = []

    # Get words from segment
    words = getattr(segment, "words", None) or []

    for w_idx, word_obj in enumerate(words):
        wtext = (
            getattr(word_obj, "word", "") or ""
        ).strip()

        if not wtext:
            continue

        wstart = getattr(word_obj, "start", None)
        wend   = getattr(word_obj, "end",   None)

        if wstart is None or wend is None:
            continue

        ws = float(wstart)
        we = float(wend)

        if not _is_valid_timestamp(ws, we):
            continue

        seg_words.append(WordEntry(
            word  = wtext,
            start = round(ws, 4),
            end   = round(we, 4),
            s_idx = s_idx,
            w_idx = w_idx,
        ))

    # Fallback: equal distribution if no valid words found
    if not seg_words:
        seg_text  = (
            getattr(segment, "text", "") or ""
        ).strip()
        seg_start = float(
            getattr(segment, "start", 0) or 0
        )
        seg_end   = float(
            getattr(segment, "end",   0) or 0
        )

        if seg_text and seg_end > seg_start:
            tokens   = seg_text.split()
            word_dur = (seg_end - seg_start) / len(tokens)

            for w_idx, tok in enumerate(tokens):
                ws = round(seg_start + w_idx * word_dur, 4)
                we = round(
                    seg_start + (w_idx + 1) * word_dur, 4
                )

                # Enforce MIN_WORD_DURATION in fallback too
                if not _is_valid_timestamp(ws, we):
                    continue

                seg_words.append(WordEntry(
                    word  = tok,
                    start = ws,
                    end   = we,
                    s_idx = s_idx,
                    w_idx = w_idx,
                ))

    return seg_words


def _extract_all_data(
    result: Any,
) -> tuple[list[str], list[WordEntry]]:
    """
    استخراج جميع الجمل والكلمات من نتيجة stable-ts.

    Returns:
        (sentences, all_words)
    """
    sentences: list[str]       = []
    all_words: list[WordEntry] = []

    segments = getattr(result, "segments", None) or []

    for s_idx, segment in enumerate(segments):
        seg_text = (
            getattr(segment, "text", "") or ""
        ).strip()

        if not seg_text:
            continue

        sentences.append(seg_text)

        seg_words = _extract_words_from_segment(
            segment, s_idx,
        )
        all_words.extend(seg_words)

    return sentences, all_words


# ═════════════════════════════════════════════════════════════════════════════
# OUTPUT BUILDERS
# ═════════════════════════════════════════════════════════════════════════════

def _build_aligned_output(
    sentences: list[str],
    all_words: list[WordEntry],
) -> list[dict]:
    """بناء aligned output."""
    aligned_out = []

    for s_idx, sent in enumerate(sentences):
        sw = [w for w in all_words if w.s_idx == s_idx]

        if not sw:
            log.debug(
                "  ⚠️  No words for sentence %d: %r",
                s_idx, sent[:40]
            )
            continue

        aligned_out.append({
            "sentence": sent,
            "start":    sw[0].start,
            "end":      sw[-1].end,
            "words": [
                {
                    "word":  w.word,
                    "start": w.start,
                    "end":   w.end,
                }
                for w in sw
            ],
        })

    return aligned_out


def _build_timeline(all_words: list[WordEntry]) -> list[dict]:
    """بناء timeline من الكلمات (global word counter)."""
    timeline   = []
    global_idx = 0

    for w in all_words:
        timeline.append({
            "time":               w.start,
            "sentence_idx":       w.s_idx,
            "visible_word_count": global_idx + 1,
        })
        global_idx += 1

    timeline.sort(key=lambda x: x["time"])
    return timeline


# ═════════════════════════════════════════════════════════════════════════════
# REPORTING
# ═════════════════════════════════════════════════════════════════════════════

def _print_extraction_report(
    sentences: list[str],
    all_words: list[WordEntry],
    aud_dur:   float,
) -> None:
    """طباعة تقرير الاستخراج."""
    log.info(
        "  ✅ %d sentences, %d words",
        len(sentences), len(all_words)
    )

    if not all_words:
        return

    # أول 8 كلمات
    log.info("  🔍 First 8:")
    for w in all_words[:8]:
        log.info(
            "     %.3fs → %.3fs  '%s'",
            w.start, w.end, w.word
        )

    # نسبة التغطية
    if aud_dur > 0:
        coverage = (
            (all_words[-1].end - all_words[0].start)
            / aud_dur * 100
        )
        log.info(
            "  📊 %.3fs → %.3fs (%.0f%%)",
            all_words[0].start,
            all_words[-1].end,
            coverage
        )


# ═════════════════════════════════════════════════════════════════════════════
# MAIN: EXTRACT TRANSCRIPT
# ═════════════════════════════════════════════════════════════════════════════

def _empty_result(aud_dur: float = 0.0) -> dict:
    """نتيجة فارغة."""
    return {
        "sentences":      [],
        "aligned":        [],
        "timeline":       [],
        "total_duration": aud_dur,
        "success":        False,
    }


def extract_transcript_from_audio(
    audio_path: str,
    lang:       str = "ar",
) -> dict:
    """
    استخراج النص مع timestamps من stable-ts.

    Args:
        audio_path: مسار الصوت
        lang:       ar | fr | en

    Returns:
        dict مع:
            - sentences:      list[str]
            - aligned:        list[dict] (sentence + words)
            - timeline:       list[dict]
            - total_duration: float
            - success:        bool

    Note:
        الصوت المُمرَّر يجب أن يكون نفس الصوت في الفيديو النهائي.
    """
    wlang   = LANG_MAP.get(lang, lang)
    aud_dur = get_audio_duration(audio_path)

    log.info(
        "\n  🎤 %s | lang=%s | %.3fs",
        Path(audio_path).name, lang, aud_dur
    )

    try:
        # تحميل model
        model = _load_model()
        if model is None:
            return _empty_result(aud_dur)

        # Transcribe + Align
        result = _transcribe_with_alignment(
            model, audio_path, wlang,
        )

        if result is None:
            return _empty_result(aud_dur)

        # استخراج البيانات
        sentences, all_words = _extract_all_data(result)

        if not sentences:
            return _empty_result(aud_dur)

        # ترتيب الكلمات حسب الوقت
        all_words.sort(key=lambda w: w.start)

        # بناء output
        aligned_out = _build_aligned_output(
            sentences, all_words,
        )
        timeline = _build_timeline(all_words)

        # تقرير
        _print_extraction_report(
            sentences, all_words, aud_dur,
        )

        return {
            "sentences":      sentences,
            "aligned":        aligned_out,
            "timeline":       timeline,
            "total_duration": aud_dur,
            "success":        True,
        }

    except Exception:
        traceback.print_exc()
        return _empty_result(aud_dur)


# ═════════════════════════════════════════════════════════════════════════════
# BACKWARD COMPATIBILITY (Same API as old sync.py)
# ═════════════════════════════════════════════════════════════════════════════

def get_word_timestamps(
    audio_path: str,
    lang:       str = "ar",
) -> list[dict]:
    """
    استخراج timestamps الكلمات فقط.

    Returns:
        list of {"word", "start", "end"}
    """
    result = extract_transcript_from_audio(audio_path, lang)

    if not result["success"]:
        return []

    return [
        w
        for seg in result["aligned"]
        for w in seg.get("words", [])
    ]


def _try_remap_timestamps(
    sentences:       list[str],
    word_timestamps: list[dict],
) -> Optional[list[dict]]:
    """
    محاولة re-map timestamps على جمل السكريبت الأصلية.

    MIN_REMAP_WORDS_RATIO = 0.15 (15% tolerance)

    Returns:
        list of word entries أو None
    """
    total_w = sum(len(s.split()) for s in sentences)

    if (
        not word_timestamps or
        len(word_timestamps) < 5 or
        total_w == 0
    ):
        return None

    # Sort word_timestamps by start time
    word_timestamps = sorted(
        word_timestamps,
        key=lambda x: x.get("start", 0)
    )

    # Check ratio
    diff_ratio = (
        abs(len(word_timestamps) - total_w) / total_w
    )

    if diff_ratio > MIN_REMAP_WORDS_RATIO:
        return None

    # Re-map
    wt:  list[dict] = []
    idx = 0

    for s_idx, sent in enumerate(sentences):
        for w_idx, word in enumerate(sent.split()):
            if idx >= len(word_timestamps):
                break

            ts = word_timestamps[idx]
            wt.append({
                "word":  word,
                "start": ts["start"],
                "end":   ts["end"],
                "s_idx": s_idx,
                "w_idx": w_idx,
            })
            idx += 1

    return wt if wt else None


def build_word_timeline(
    sentences:       list[str],
    word_timestamps: list[dict],
    total_duration:  float,
) -> tuple[list[dict], list[dict]]:
    """
    بناء word timeline من جمل السكريبت + timestamps.

    Strategy:
        1. محاولة re-map إذا الكلمات قريبة
        2. وإلا: equal split

    Returns:
        (timeline, aligned)
    """
    if not sentences:
        return [], []

    # Try re-map
    wt = _try_remap_timestamps(sentences, word_timestamps)

    if wt:
        return _build_output(sentences, wt, total_duration)

    # Fallback: equal split
    return _equal_split(sentences, total_duration)


def _equal_split(
    sentences:      list[str],
    total_duration: float,
) -> tuple[list[dict], list[dict]]:
    """
    توزيع متساوٍ للكلمات على total_duration.

    Handles total_duration=0 safely.
    """
    # Protect against total_duration=0
    if total_duration <= 0:
        log.warning(
            "  ⚠️  total_duration=0 — using fallback (%.1fs)",
            max(1.0, len(sentences) * 0.3)
        )
        total_duration = max(1.0, len(sentences) * 0.3)

    # Build word list
    all_w: list[tuple[int, int, str]] = []

    for s_idx, sentence in enumerate(sentences):
        for w_idx, word in enumerate(sentence.split()):
            all_w.append((s_idx, w_idx, word))

    if not all_w:
        return [], []

    # Equal distribution
    word_duration = total_duration / len(all_w)
    wt: list[dict] = []

    for i, (s_idx, w_idx, word) in enumerate(all_w):
        wt.append({
            "word":  word,
            "start": round(i * word_duration, 4),
            "end":   round((i + 1) * word_duration, 4),
            "s_idx": s_idx,
            "w_idx": w_idx,
        })

    return _build_output(sentences, wt, total_duration)


def _build_output(
    sentences:      list[str],
    word_times:     list[dict],
    total_duration: float,
) -> tuple[list[dict], list[dict]]:
    """بناء (timeline, aligned) من sentences + word_times."""
    aligned = []

    for s_idx, sent in enumerate(sentences):
        sw = [w for w in word_times if w["s_idx"] == s_idx]

        if not sw:
            continue

        aligned.append({
            "sentence": sent,
            "start":    sw[0]["start"],
            "end":      sw[-1]["end"],
            "words": [
                {
                    "word":  w["word"],
                    "start": w["start"],
                    "end":   w["end"],
                }
                for w in sw
            ],
        })

    # Build timeline with global word counter
    timeline   = []
    global_idx = 0

    for w in word_times:
        timeline.append({
            "time":               w["start"],
            "sentence_idx":       w["s_idx"],
            "visible_word_count": global_idx + 1,
        })
        global_idx += 1

    timeline.sort(key=lambda x: x["time"])
    return timeline, aligned

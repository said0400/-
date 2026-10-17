"""
📦 Video Format Exporter v2.0 — Final Production Edition

Features:
  ✅ Multi-format export (9:16, 1:1, 16:9, 4:5)
  ✅ Smart aspect ratio handling (scale + crop)
  ✅ Audio codec fallback (copy → aac on failure)
  ✅ FFmpeg via subprocess
  ✅ Skip 9x16 (it's the source)
  ✅ Output directory auto-creation
  ✅ Output file size validation
  ✅ Failed file cleanup
  ✅ Failures reported in return
  ✅ Progress reporting
  ✅ Cross-platform compatible
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# ═════════════════════════════════════════════════════════════════════════════
# CONSTANTS
# ═════════════════════════════════════════════════════════════════════════════

# Encoding
VIDEO_CODEC  = "libx264"
AUDIO_CODEC  = "copy"     # passthrough (no re-encoding)
FALLBACK_AUDIO_CODEC = "aac"  # fallback if copy fails
PRESET       = "fast"     # Balance: speed over quality (CI/CD friendly)
CRF          = 20         # Quality (lower = better, 18-23 recommended)

# Timeouts
FFMPEG_TIMEOUT = 600  # 10 minutes per format

# Source format (will be skipped)
SOURCE_FORMAT = "9x16"

# Default export formats
DEFAULT_EXPORT_FORMATS = ["1x1", "16x9"]

# Minimum output size
MIN_OUTPUT_BYTES = 10_000  # 10 KB


# ═════════════════════════════════════════════════════════════════════════════
# DATA CLASSES
# ═════════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class VideoFormat:
    """تعريف format فيديو."""
    name:        str   # مثل "1x1"
    width:       int
    height:      int
    description: str   # مثل "Instagram Square"

    @property
    def file_suffix(self) -> str:
        """اللاحقة للاسم (1x1 → 1_1)."""
        return self.name.replace("x", "_")


# ═════════════════════════════════════════════════════════════════════════════
# FORMATS REGISTRY
# ═════════════════════════════════════════════════════════════════════════════

FORMATS: dict[str, VideoFormat] = {
    "9x16": VideoFormat(
        name        = "9x16",
        width       = 1080,
        height      = 1920,
        description = "Vertical (Reels/Shorts)",
    ),
    "1x1": VideoFormat(
        name        = "1x1",
        width       = 1080,
        height      = 1080,
        description = "Instagram Square",
    ),
    "16x9": VideoFormat(
        name        = "16x9",
        width       = 1920,
        height      = 1080,
        description = "YouTube Horizontal",
    ),
    "4x5": VideoFormat(
        name        = "4x5",
        width       = 1080,
        height      = 1350,
        description = "Instagram Portrait",
    ),
}


# ═════════════════════════════════════════════════════════════════════════════
# HELPERS
# ═════════════════════════════════════════════════════════════════════════════

def _file_size_mb(path: Path) -> float:
    """حجم الملف بالـ MB."""
    try:
        return path.stat().st_size / 1_048_576
    except OSError:
        return 0.0


def _build_video_filter(
    width:  int,
    height: int,
) -> str:
    """
    بناء video filter للـ ffmpeg.

    Strategy:
        1. scale     → تكبير حسب أكبر بُعد
        2. crop      → قص للأبعاد المطلوبة
        3. setsar=1  → ضبط Sample Aspect Ratio
    """
    return (
        f"scale={width}:{height}:"
        f"force_original_aspect_ratio=increase,"
        f"crop={width}:{height},"
        f"setsar=1"
    )


def _run_ffmpeg(
    source:      Path,
    output:      Path,
    width:       int,
    height:      int,
    audio_codec: str = AUDIO_CODEC,
) -> tuple[bool, str]:
    """
    تشغيل ffmpeg.

    Returns:
        (success, error_message)
    """
    vf_filter = _build_video_filter(width, height)

    try:
        r = subprocess.run(
            [
                "ffmpeg", "-y",
                "-i",        str(source),
                "-vf",       vf_filter,
                "-c:v",      VIDEO_CODEC,
                "-preset",   PRESET,
                "-crf",      str(CRF),
                "-c:a",      audio_codec,
                str(output),
            ],
            capture_output = True,
            text           = True,
            timeout        = FFMPEG_TIMEOUT,
        )

        if r.returncode != 0:
            # Show both start and end of stderr
            stderr = r.stderr or "no stderr"
            if len(stderr) > 300:
                error_msg = (
                    stderr[:150] + "\n...\n" +
                    stderr[-150:]
                )
            else:
                error_msg = stderr
            return False, error_msg

        # Validate output file
        if not output.exists():
            return False, "Output file not created"

        if output.stat().st_size < MIN_OUTPUT_BYTES:
            output.unlink(missing_ok=True)
            return False, (
                "Output file too small "
                "(encoding may have failed)"
            )

        return True, ""

    except subprocess.TimeoutExpired:
        # Cleanup partial file
        output.unlink(missing_ok=True)
        return False, "ffmpeg timeout"

    except FileNotFoundError:
        return False, "ffmpeg not found — install FFmpeg"

    except Exception as e:
        output.unlink(missing_ok=True)
        return False, str(e)[:150]


# ═════════════════════════════════════════════════════════════════════════════
# SINGLE FORMAT EXPORT
# ═════════════════════════════════════════════════════════════════════════════

def export_format(
    source:      str,
    output_path: str,
    width:       int,
    height:      int,
) -> Optional[Path]:
    """
    تصدير فيديو بأبعاد محددة.

    Tries audio copy first, falls back to aac if needed.

    Args:
        source:      مسار الفيديو الأصلي
        output_path: مسار الإخراج
        width:       العرض المطلوب
        height:      الارتفاع المطلوب

    Returns:
        Path للملف أو None عند الفشل
    """
    src = Path(source).resolve()
    out = Path(output_path).resolve()

    if not src.exists():
        log.error("  ❌ Source not found: %s", src)
        return None

    # Ensure output directory exists
    out.parent.mkdir(parents=True, exist_ok=True)

    # Try 1: audio copy (fastest)
    success, error = _run_ffmpeg(
        src, out, width, height,
        audio_codec=AUDIO_CODEC,
    )

    if success:
        return out

    # Try 2: re-encode audio (fallback)
    if "codec" in error.lower() or "copy" in error.lower():
        log.warning(
            "  ⚠️  Audio copy failed — re-encoding"
        )
        success, error = _run_ffmpeg(
            src, out, width, height,
            audio_codec=FALLBACK_AUDIO_CODEC,
        )

        if success:
            return out

    log.warning(
        "  ⚠️  Export %dx%d failed: %s",
        width, height, error[:100]
    )

    # Cleanup failed file
    out.unlink(missing_ok=True)
    return None


# ═════════════════════════════════════════════════════════════════════════════
# EXPORT ALL FORMATS
# ═════════════════════════════════════════════════════════════════════════════

def _filter_export_formats(
    formats: list[str],
) -> list[str]:
    """
    فلترة الـ formats:
        - إزالة source format (9x16)
        - إزالة المكررات (keep order)

    Returns:
        list of formats to export
    """
    seen:   set[str]  = set()
    result: list[str] = []

    for fmt in formats:
        if fmt == SOURCE_FORMAT:
            continue
        if fmt in seen:
            continue
        seen.add(fmt)
        result.append(fmt)

    return result


def _export_single(
    source:      str,
    output_base: str,
    fmt_name:    str,
) -> Optional[Path]:
    """
    تصدير format واحد مع logging.

    Handles output_base with or without extension.
    """
    fmt = FORMATS.get(fmt_name)

    if not fmt:
        log.warning(
            "  ⚠️  Unknown format: %s — skipping",
            fmt_name
        )
        return None

    # Handle output_base with extension
    base_path   = Path(output_base).with_suffix("")
    output_path = f"{base_path}_{fmt.file_suffix}.mp4"

    result = export_format(
        source      = source,
        output_path = output_path,
        width       = fmt.width,
        height      = fmt.height,
    )

    if result and result.exists():
        size_mb = _file_size_mb(result)
        log.info(
            "     ✅ %s (%s) → %s (%.1f MB)",
            fmt.description,
            fmt.name,
            result.name,
            size_mb,
        )
        return result

    log.error(
        "     ❌ %s (%s) → failed",
        fmt.description, fmt.name,
    )
    return None


def export_all(
    source:      str,
    output_base: str,
    formats:     Optional[list[str]] = None,
) -> dict[str, Path]:
    """
    تصدير صيغ إضافية من الفيديو الأساسي.

    Args:
        source:      مسار الفيديو الأصلي (9:16)
        output_base: المسار الأساسي (بدون/مع امتداد)
        formats:     قائمة الصيغ (default: ["1x1", "16x9"])

    Returns:
        dict من اسم الصيغة إلى Path

    Examples:
        >>> export_all(
        ...     source="video.mp4",
        ...     output_base="output/video_1",
        ...     formats=["1x1", "16x9"],
        ... )
        {
            "1x1":  Path("output/video_1_1_1.mp4"),
            "16x9": Path("output/video_1_16_9.mp4"),
        }
    """
    # Default formats
    if formats is None:
        formats = list(DEFAULT_EXPORT_FORMATS)

    if not formats:
        return {}

    # Filter (remove 9x16 and duplicates)
    formats_to_export = _filter_export_formats(formats)

    if not formats_to_export:
        return {}

    # Validate source
    src = Path(source).resolve()
    if not src.exists():
        log.error(
            "  ❌ Export source not found: %s", src
        )
        return {}

    log.info(
        "  📦 Exporting formats: %s",
        formats_to_export
    )

    # Export each format
    results:  dict[str, Path] = {}
    failures: list[str]       = []

    for fmt_name in formats_to_export:
        result = _export_single(
            source      = source,
            output_base = output_base,
            fmt_name    = fmt_name,
        )

        if result:
            results[fmt_name] = result
        else:
            failures.append(fmt_name)

    # Report failures
    if failures:
        log.warning(
            "  ⚠️  Failed formats: %s",
            ", ".join(failures)
        )

    # Summary
    log.info(
        "  📦 Export: %d/%d formats successful",
        len(results),
        len(formats_to_export),
    )

    return results

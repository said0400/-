"""
🎬 pre_generate.py — Daily Video Pre-Generator v2.0

Features:
  ✅ Calculate actual video duration (no more 0.0)
  ✅ Absolute paths (works from any CWD)
  ✅ Skip if file exists (avoid re-rendering)
  ✅ Loop support (reset when all published)
  ✅ Smart output file detection (multiple patterns)
  ✅ Conditional --force (not always forced)
  ✅ Correct total_failed counting
  ✅ Per-language daily quotas

يعمل كل ليلة تلقائياً لتوليد فيديوهات اليوم التالي.

Pipeline لكل لغة:
  1. يقرأ xx_short.xlsx → يختار 5 غير منشورة → يولّدها
  2. يقرأ xx_long.xlsx  → يختار 1 غير منشور  → يولّده
  3. يحفظ الفيديوهات في DB كـ ready to publish

لا ينشر أي شيء — فقط يولّد ويحفظ.

Usage:
  python pre_generate.py --lang ar
  python pre_generate.py --lang fr
  python pre_generate.py --lang en
  python pre_generate.py --all
  python pre_generate.py --all --dry-run
"""

from __future__ import annotations

import argparse
import logging
import subprocess
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from db import (
    init_db,
    get_next_video_number,
    get_daily_remaining_generate,
    is_daily_generate_quota_reached,
    save_pre_generated,
    get_pre_generated_count,
    reset_published_for_lang,
    print_db_summary,
    make_cache_key,
    has_ai_cache,
)
from script_reader import read_scripts, validate_scripts

log = logging.getLogger(__name__)

# ═════════════════════════════════════════════════════════════════════════════
# CONSTANTS
# ═════════════════════════════════════════════════════════════════════════════

BASE_DIR    = Path(__file__).parent.resolve()
SCRIPTS_DIR = BASE_DIR / "scripts"
OUTPUT_DIR  = BASE_DIR / "output"

# Content files (absolute paths — matches retry_publisher.py)
CONTENT_FILES: dict[str, dict[str, Path]] = {
    "ar": {
        "short": SCRIPTS_DIR / "ar_short.xlsx",
        "long":  SCRIPTS_DIR / "ar_long.xlsx",
    },
    "fr": {
        "short": SCRIPTS_DIR / "fr_short.xlsx",
        "long":  SCRIPTS_DIR / "fr_long.xlsx",
    },
    "en": {
        "short": SCRIPTS_DIR / "en_short.xlsx",
        "long":  SCRIPTS_DIR / "en_long.xlsx",
    },
}

# Daily quotas
DAILY_QUOTAS: dict[str, int] = {
    "short": 5,
    "long":  1,
}

VALID_LANGS = ("ar", "fr", "en")
VALID_MODES = ("short", "long")

# Timeouts
FFPROBE_TIMEOUT     = 15
MAIN_PY_TIMEOUT     = 7200    # 2 hours per video
MIN_VALID_FILE_SIZE = 100_000  # 100 KB


# ═════════════════════════════════════════════════════════════════════════════
# CLI
# ═════════════════════════════════════════════════════════════════════════════

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description     = "🎬 Daily Video Pre-Generator",
        formatter_class = argparse.RawTextHelpFormatter,
    )
    p.add_argument(
        "--lang",
        choices = list(VALID_LANGS),
        default = None,
        help    = "اللغة المطلوبة (ar/fr/en)",
    )
    p.add_argument(
        "--all",
        action = "store_true",
        help   = "توليد فيديوهات كل اللغات",
    )
    p.add_argument(
        "--mode",
        choices = list(VALID_MODES) + ["both"],
        default = "both",
        help    = "short / long / both",
    )
    p.add_argument(
        "--output-dir",
        type    = str,
        default = str(OUTPUT_DIR),
        help    = "مجلد الحفظ",
    )
    p.add_argument(
        "--dry-run",
        action = "store_true",
        help   = "معاينة فقط بدون توليد",
    )
    p.add_argument(
        "--force",
        action = "store_true",
        help   = "تجاهل الـ quota اليومي وإعادة التوليد",
    )
    p.add_argument(
        "--force-ai",
        action = "store_true",
        help   = "تجديد الـ AI cache",
    )
    p.add_argument(
        "--platform",
        choices = ["yt", "fb", "both"],
        default = "yt",
        help    = "المنصة المستهدفة",
    )
    p.add_argument(
        "--skip-existing",
        action  = "store_true",
        default = True,
        help    = "تخطي الفيديوهات الموجودة بالفعل",
    )
    return p.parse_args()


# ═════════════════════════════════════════════════════════════════════════════
# HELPERS
# ═════════════════════════════════════════════════════════════════════════════

def _now_utc() -> str:
    """UTC timestamp as ISO string."""
    return datetime.now(timezone.utc).isoformat()


def _log_separator(
    char: str = "─",
    width: int = 65,
) -> None:
    log.info(char * width)


def _get_content_file(
    lang: str,
    mode: str,
) -> Optional[Path]:
    """Get content file path with existence check."""
    path = CONTENT_FILES.get(lang, {}).get(mode)
    if path and path.exists():
        return path
    if path:
        log.warning("  ⚠️  File not found: %s", path)
    return None


def _get_video_output_path(
    num:          str,
    lang:         str,
    content_mode: str,
    platform:     str,
    output_dir:   str,
) -> Path:
    """Build expected video output path."""
    suffix = (
        f"_{content_mode}_{platform}_published.mp4"
    )
    return Path(output_dir) / f"video_{num}_{lang}{suffix}"


def _get_expected_output(
    num:          str,
    lang:         str,
    content_mode: str,
    platform:     str,
    output_dir:   str,
) -> Optional[Path]:
    """
    البحث عن الفيديو المُولَّد بصيغ متعددة.

    Searches by content_mode + platform accurately.
    Falls back to generic patterns if exact match not found.
    """
    out_dir = Path(output_dir)

    # Primary: exact name
    exact = out_dir / (
        f"video_{num}_{lang}_{content_mode}"
        f"_{platform}_published.mp4"
    )
    if (
        exact.exists() and
        exact.stat().st_size > MIN_VALID_FILE_SIZE
    ):
        return exact

    # Secondary: glob by content_mode
    pattern = (
        f"video_{num}_{lang}_{content_mode}_*.mp4"
    )
    candidates = sorted(
        out_dir.glob(pattern),
        key     = lambda p: p.stat().st_size,
        reverse = True,
    )

    for c in candidates:
        if c.stat().st_size > MIN_VALID_FILE_SIZE:
            return c

    # Final fallback: any published video for this num
    pattern_generic = (
        f"video_{num}_{lang}_*_published.mp4"
    )
    candidates_g = sorted(
        out_dir.glob(pattern_generic),
        key     = lambda p: p.stat().st_size,
        reverse = True,
    )

    for c in candidates_g:
        if c.stat().st_size > MIN_VALID_FILE_SIZE:
            return c

    return None


# ═════════════════════════════════════════════════════════════════════════════
# VIDEO DURATION
# ═════════════════════════════════════════════════════════════════════════════

def _get_video_duration(path: Path) -> float:
    """
    حساب مدة الفيديو بـ ffprobe.

    Returns:
        المدة بالثواني (0.0 لو فشل)
    """
    if not path.exists():
        return 0.0

    try:
        r = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            capture_output = True,
            text           = True,
            timeout        = FFPROBE_TIMEOUT,
        )
        output = r.stdout.strip()
        if output:
            duration = float(output)
            return max(0.0, duration)
        return 0.0

    except FileNotFoundError:
        log.warning(
            "  ⚠️  ffprobe not found — install FFmpeg"
        )
        return 0.0
    except subprocess.TimeoutExpired:
        log.warning("  ⚠️  ffprobe timeout")
        return 0.0
    except (ValueError, Exception) as e:
        log.debug("  Duration error: %s", e)
        return 0.0


# ═════════════════════════════════════════════════════════════════════════════
# RUN main.py
# ═════════════════════════════════════════════════════════════════════════════

def _run_main_py(
    num:          str,
    lang:         str,
    content_mode: str,
    platform:     str,
    input_file:   str,
    output_dir:   str,
    force_ai:     bool = False,
    force:        bool = False,
) -> bool:
    """
    تشغيل main.py لتوليد فيديو واحد.

    --force is conditional (not always forced).
    """
    cmd = [
        sys.executable,
        str(BASE_DIR / "main.py"),
        str(input_file),
        "--video-number", str(num),
        "--lang",         lang,
        "--content-mode", content_mode,
        "--platform",     platform,
        "--output-dir",   output_dir,
        "--no-publish",
    ]

    if force:
        cmd.append("--force")

    if force_ai:
        cmd.append("--force-ai")

    log.info(
        "  🚀 Running: python main.py #%s [%s/%s/%s]",
        num, lang, content_mode, platform
    )
    log.info(
        "     Command tail: %s",
        ' '.join(cmd[-6:])
    )

    try:
        result = subprocess.run(
            cmd,
            cwd            = str(BASE_DIR),
            timeout        = MAIN_PY_TIMEOUT,
            text           = True,
            capture_output = False,
        )
        return result.returncode == 0

    except subprocess.TimeoutExpired:
        log.error(
            "  ❌ Timeout (%dh): video #%s [%s/%s]",
            MAIN_PY_TIMEOUT // 3600,
            num, lang, content_mode
        )
        return False
    except Exception as e:
        log.error("  ❌ Error running main.py: %s", e)
        return False


# ═════════════════════════════════════════════════════════════════════════════
# PICK NEXT VIDEOS
# ═════════════════════════════════════════════════════════════════════════════

def _pick_next_videos(
    lang:         str,
    content_mode: str,
    count:        int,
    output_dir:   str,
    platform:     str = "yt",
) -> list[dict]:
    """
    اختيار الفيديوهات التالية غير المولّدة والمنشورة.

    Returns:
        list of {number, title, record, file_path}
    """
    file_path = _get_content_file(lang, content_mode)
    if not file_path:
        log.warning(
            "  ⚠️  No content file for %s/%s",
            lang.upper(), content_mode.upper()
        )
        return []

    try:
        all_scripts = read_scripts(str(file_path))
    except Exception as e:
        log.error(
            "  ❌ Cannot read %s: %s",
            file_path.name, e
        )
        return []

    valid, errors = validate_scripts(all_scripts)
    for err in errors:
        if "❌" in err:
            log.warning(err)

    if not valid:
        log.warning(
            "  ⚠️  No valid scripts in %s",
            file_path.name
        )
        return []

    available_numbers = [
        str(s["number"]) for s in valid
    ]
    scripts_map = {
        str(s["number"]): s for s in valid
    }

    selected:       list[dict] = []
    used_numbers:   set[str]   = set()
    loop_attempted: bool       = False

    for _ in range(count):
        remaining = [
            n for n in available_numbers
            if n not in used_numbers
        ]

        if not remaining:
            log.warning(
                "  ⚠️  No more available numbers in %s",
                file_path.name
            )
            break

        next_num = get_next_video_number(
            lang              = lang,
            available_numbers = remaining,
            content_mode      = content_mode,
            platforms         = (platform,),
        )

        if next_num is None and not loop_attempted:
            # Loop only once
            log.warning(
                "  🔄 ALL %s %s videos published — "
                "starting new cycle",
                lang.upper(), content_mode.upper()
            )
            log.warning(
                "  ⚠️  reset_published_for_lang: videos "
                "will be eligible for RE-PUBLISHING"
            )
            reset_published_for_lang(lang, content_mode)
            loop_attempted = True

            next_num = get_next_video_number(
                lang              = lang,
                available_numbers = remaining,
                content_mode      = content_mode,
                platforms         = (platform,),
            )

        if next_num is None:
            break

        used_numbers.add(next_num)
        record = scripts_map.get(next_num, {})

        selected.append({
            "number":    next_num,
            "title":     record.get(
                "title", f"Video #{next_num}"
            ),
            "record":    record,
            "file_path": str(file_path),
        })

    return selected


# ═════════════════════════════════════════════════════════════════════════════
# GENERATE ONE VIDEO
# ═════════════════════════════════════════════════════════════════════════════

def _generate_one(
    video_info:    dict,
    lang:          str,
    content_mode:  str,
    platform:      str,
    output_dir:    str,
    force_ai:      bool = False,
    dry_run:       bool = False,
    skip_existing: bool = True,
) -> bool:
    """
    توليد فيديو واحد وحفظه في DB.

    ✅ Calculates actual duration_s
    ✅ Skips existing files (skip_existing)
    ✅ Conditional --force

    Returns:
        True إذا نجح
    """
    num   = video_info["number"]
    title = video_info["title"]

    log.info(
        "\n  🎬 Generating #%s: %s",
        num, title[:50]
    )
    log.info(
        "     Lang: %s | Mode: %s | Platform: %s",
        lang.upper(),
        content_mode.upper(),
        platform.upper()
    )

    if dry_run:
        log.info(
            "  🔍 [DRY RUN] — skipping actual generation"
        )
        return True

    # Check for existing file (skip re-rendering)
    if skip_existing:
        existing = _get_expected_output(
            num, lang, content_mode,
            platform, output_dir
        )
        if existing:
            file_size_mb = (
                existing.stat().st_size / 1_048_576
            )
            duration = _get_video_duration(existing)

            log.info(
                "  ♻️  Found existing file: %s "
                "(%.1f MB, %.1fs) — saving to DB only",
                existing.name, file_size_mb, duration
            )

            yt_path = (
                str(existing)
                if platform == "yt" else ""
            )
            fb_path = (
                str(existing)
                if platform == "fb" else ""
            )

            save_pre_generated(
                video_number = num,
                lang         = lang,
                content_mode = content_mode,
                title        = title,
                output_path  = str(existing),
                duration_s   = duration,
                yt_path      = yt_path,
                fb_path      = fb_path,
                scheduled_at = _now_utc(),
            )

            log.info(
                "  💾 Saved to DB: #%s [%s/%s] "
                "(skip-existing)",
                num, lang, content_mode
            )
            return True

    # Generate video via main.py
    success = _run_main_py(
        num          = num,
        lang         = lang,
        content_mode = content_mode,
        platform     = platform,
        input_file   = video_info["file_path"],
        output_dir   = output_dir,
        force_ai     = force_ai,
        force        = False,  # Not always forced
    )

    if not success:
        log.error("  ❌ Generation failed: #%s", num)
        return False

    # Find generated file
    output_path = _get_expected_output(
        num, lang, content_mode,
        platform, output_dir
    )

    if not output_path:
        log.error(
            "  ❌ Output file not found for "
            "#%s [%s/%s/%s]",
            num, lang, content_mode, platform
        )
        return False

    # Calculate actual duration
    file_size = output_path.stat().st_size / 1_048_576
    duration  = _get_video_duration(output_path)

    log.info(
        "  ✅ Generated: %s (%.1f MB, %.1fs)",
        output_path.name, file_size, duration
    )

    if duration <= 0:
        log.warning(
            "  ⚠️  Cannot determine duration — "
            "saved as 0.0"
        )

    # Save to DB with actual duration
    yt_path = (
        str(output_path)
        if platform == "yt" else ""
    )
    fb_path = (
        str(output_path)
        if platform == "fb" else ""
    )

    save_pre_generated(
        video_number = num,
        lang         = lang,
        content_mode = content_mode,
        title        = title,
        output_path  = str(output_path),
        duration_s   = duration,
        yt_path      = yt_path,
        fb_path      = fb_path,
        scheduled_at = _now_utc(),
    )

    log.info(
        "  💾 Saved to DB: #%s [%s/%s]",
        num, lang, content_mode
    )
    return True


# ═════════════════════════════════════════════════════════════════════════════
# GENERATE FOR ONE LANGUAGE
# ═════════════════════════════════════════════════════════════════════════════

def generate_for_lang(
    lang:          str,
    modes:         list[str],
    output_dir:    str,
    platform:      str  = "yt",
    force:         bool = False,
    force_ai:      bool = False,
    dry_run:       bool = False,
    skip_existing: bool = True,
) -> dict[str, int]:
    """
    توليد فيديوهات لغة واحدة.

    Returns:
        {"short": n_success, "long": n_success}
    """
    results: dict[str, int] = {}

    for mode in modes:
        _log_separator()
        log.info(
            "  📋 %s %s — Processing...",
            lang.upper(), mode.upper()
        )

        quota = DAILY_QUOTAS.get(mode, 5)

        # Check quota
        if (
            not force and
            is_daily_generate_quota_reached(lang, mode)
        ):
            remaining = get_daily_remaining_generate(
                lang, mode
            )
            log.info(
                "  ✅ Quota reached for %s/%s "
                "(remaining: %d)",
                lang.upper(), mode.upper(), remaining
            )
            results[mode] = 0
            continue

        remaining_quota = (
            quota
            if force
            else get_daily_remaining_generate(lang, mode)
        )

        log.info(
            "  🎯 Need to generate: %d %s videos",
            remaining_quota, mode.upper()
        )

        # Pick videos
        videos_to_generate = _pick_next_videos(
            lang         = lang,
            content_mode = mode,
            count        = remaining_quota,
            output_dir   = output_dir,
            platform     = platform,
        )

        if not videos_to_generate:
            log.warning(
                "  ⚠️  No videos to generate for %s/%s",
                lang.upper(), mode.upper()
            )
            results[mode] = 0
            continue

        log.info(
            "  📝 Selected %d videos:",
            len(videos_to_generate)
        )
        for v in videos_to_generate:
            log.info(
                "     #%s: %s",
                v['number'], v['title'][:40]
            )

        # Generate each video
        success_count = 0
        for video_info in videos_to_generate:
            ok = _generate_one(
                video_info    = video_info,
                lang          = lang,
                content_mode  = mode,
                platform      = platform,
                output_dir    = output_dir,
                force_ai      = force_ai,
                dry_run       = dry_run,
                skip_existing = skip_existing,
            )
            if ok:
                success_count += 1

        results[mode] = success_count
        log.info(
            "  ✅ %s %s: %d/%d generated",
            lang.upper(), mode.upper(),
            success_count, len(videos_to_generate)
        )

    return results


# ═════════════════════════════════════════════════════════════════════════════
# MAIN
# ═════════════════════════════════════════════════════════════════════════════

def main() -> None:
    # Logging — entry point only
    logging.basicConfig(
        level   = logging.INFO,
        format  = "%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt = "%H:%M:%S",
    )

    args = parse_args()

    # Validate inputs
    if not args.lang and not args.all:
        log.error("❌ Must specify --lang or --all")
        sys.exit(1)

    langs = (
        list(VALID_LANGS)
        if args.all
        else [args.lang]
    )

    modes = (
        list(VALID_MODES)
        if args.mode == "both"
        else [args.mode]
    )

    output_dir = args.output_dir
    Path(output_dir).mkdir(parents=True, exist_ok=True)

    # Initialize DB
    init_db()

    # Header
    _log_separator("═")
    log.info(
        "  🎬  Pre-Generator v2.0 — Daily Video Factory"
    )
    _log_separator("═")
    log.info(
        "  Languages : %s",
        ', '.join(l.upper() for l in langs)
    )
    log.info(
        "  Modes     : %s",
        ', '.join(m.upper() for m in modes)
    )
    log.info("  Platform  : %s", args.platform.upper())
    log.info("  Scripts   : %s", SCRIPTS_DIR)
    log.info("  Output    : %s", output_dir)
    log.info(
        "  Dry Run   : %s",
        'YES ⚠️' if args.dry_run else 'NO'
    )
    log.info(
        "  Force     : %s",
        'YES' if args.force else 'NO'
    )
    log.info(
        "  Skip Exist: %s",
        'YES ♻️' if args.skip_existing else 'NO'
    )
    log.info("")

    print_db_summary()

    # Generate for each language
    total_success = 0
    total_failed  = 0
    grand_results : dict[str, dict[str, int]] = {}

    for lang in langs:
        _log_separator("═")
        log.info(
            "\n  🌐 Processing: %s", lang.upper()
        )
        _log_separator("═")

        try:
            results = generate_for_lang(
                lang          = lang,
                modes         = modes,
                output_dir    = output_dir,
                platform      = args.platform,
                force         = args.force,
                force_ai      = args.force_ai,
                dry_run       = args.dry_run,
                skip_existing = args.skip_existing,
            )

            grand_results[lang] = results

            lang_success = sum(results.values())
            lang_quota   = sum(
                DAILY_QUOTAS.get(m, 5) for m in modes
            )
            total_success += lang_success

            # Count failed correctly
            if (
                lang_success == 0 and
                not args.dry_run
            ):
                total_failed += 1
                log.warning(
                    "  ⚠️  %s: 0/%d videos generated",
                    lang.upper(), lang_quota
                )

            log.info(
                "\n  ✅ %s done: %d/%d videos generated",
                lang.upper(), lang_success, lang_quota
            )

        except KeyboardInterrupt:
            log.warning("\n⛔ Interrupted by user")
            break
        except Exception as e:
            log.error(
                "\n  ❌ %s failed: %s",
                lang.upper(), e
            )
            traceback.print_exc()
            total_failed += 1

    # Final Summary
    _log_separator("═")
    log.info("  📊 Pre-Generation Summary")
    _log_separator("═")

    for lang, results in grand_results.items():
        for mode, count in results.items():
            quota  = DAILY_QUOTAS.get(mode, 5)
            status = "✅" if count >= quota else "⚠️"
            log.info(
                "  %s %s %s: %d/%d generated",
                status, lang.upper(), mode.upper(),
                count, quota
            )

    log.info("")
    log.info(
        "  🎯 Total generated : %d videos",
        total_success
    )
    if total_failed > 0:
        log.info(
            "  ❌ Failed languages: %d", total_failed
        )

    log.info("")
    print_db_summary()
    _log_separator("═")

    # Exit code
    if total_failed > 0 and total_success == 0:
        sys.exit(1)


if __name__ == "__main__":
    main()

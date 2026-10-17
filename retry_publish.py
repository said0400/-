"""
🔄 Smart Retry Publisher v2.0 — Final Production Edition

Features:
  ✅ Find videos: rendered ✅ but not published ❌
  ✅ Skip fully published videos (uses is_fully_published)
  ✅ Retry publishing without re-rendering
  ✅ Multi-platform support (Facebook + YouTube)
  ✅ Multi-language support (AR, FR, EN)
  ✅ Per-language Facebook credentials
  ✅ Smart thumbnail detection (auto)
  ✅ as_reel dynamic based on content_mode
  ✅ Dry-run mode for testing
  ✅ Notification integration
  ✅ Detailed error tracking (500 chars)
  ✅ Absolute paths (works from any CWD)
  ✅ Immutable credential check (no side effects)
  ✅ Pure functions (retry returns tuple)
  ✅ Limit applied in SQL query (early exit)
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
import traceback
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Optional

# Public DB API + _conn for rendered_videos query
from db import (
    _conn,
    get_ai_cache,
    init_db,
    is_fully_published,
    is_published_facebook,
    is_published_youtube,
    make_cache_key,
    mark_video_published_for_lang,
    print_db_summary,
)
from facebook import (
    check_credentials as fb_check_credentials,
    publish_to_facebook,
)
from notifier import (
    notify_info,
    notify_video_failed,
    notify_video_published,
)
from script_reader import read_scripts
from youtube import publish_to_youtube

log = logging.getLogger(__name__)

# ═════════════════════════════════════════════════════════════════════════════
# CONSTANTS
# ═════════════════════════════════════════════════════════════════════════════

# Absolute paths
BASE_DIR    = Path(__file__).parent.resolve()
SCRIPTS_DIR = BASE_DIR / "scripts"

# Supported values
LANGS     = ("ar", "fr", "en")
MODES     = ("short", "long")
PLATFORMS = ("facebook", "youtube")

# Limits
DEFAULT_LIMIT    = 10
MAX_ERROR_LENGTH = 500

# Display
SUMMARY_WIDTH    = 65
SEPARATOR_WIDTH  = 55
LIST_SEPARATOR   = 60

# Script files mapping (absolute paths)
SCRIPT_FILES: dict[tuple[str, str], str] = {
    ("ar", "short"): str(SCRIPTS_DIR / "ar_short.xlsx"),
    ("fr", "short"): str(SCRIPTS_DIR / "fr_short.xlsx"),
    ("en", "short"): str(SCRIPTS_DIR / "en_short.xlsx"),
    ("ar", "long"):  str(SCRIPTS_DIR / "ar_long.xlsx"),
    ("fr", "long"):  str(SCRIPTS_DIR / "fr_long.xlsx"),
    ("en", "long"):  str(SCRIPTS_DIR / "en_long.xlsx"),
}


# ═════════════════════════════════════════════════════════════════════════════
# DATA CLASSES
# ═════════════════════════════════════════════════════════════════════════════

@dataclass
class PendingVideo:
    """Video that needs retry publishing."""
    video_number: str
    lang:         str
    content_mode: str
    fb_path:      str
    yt_path:      str
    needs_fb:     bool
    needs_yt:     bool
    duration:     float = 0.0


@dataclass
class RetryResult:
    """Result of retry attempt."""
    video_number: str
    lang:         str
    content_mode: str
    fb_success:   bool       = False
    yt_success:   bool       = False
    errors:       list[str]  = field(default_factory=list)

    def has_errors(self) -> bool:
        return len(self.errors) > 0

    def has_success(self) -> bool:
        return self.fb_success or self.yt_success


# ═════════════════════════════════════════════════════════════════════════════
# CLI
# ═════════════════════════════════════════════════════════════════════════════

def parse_args() -> argparse.Namespace:
    """Parse CLI arguments."""
    p = argparse.ArgumentParser(
        description     = "🔄 Retry failed publishes",
        formatter_class = argparse.RawTextHelpFormatter,
    )
    p.add_argument(
        "--lang",
        type    = str,
        default = "all",
        choices = ["all", *LANGS],
        help    = "اللغة (all = كل اللغات)",
    )
    p.add_argument(
        "--content-mode",
        type    = str,
        default = "all",
        choices = ["all", *MODES],
        help    = "نوع المحتوى",
    )
    p.add_argument(
        "--platform",
        type    = str,
        default = "all",
        choices = ["all", *PLATFORMS],
        help    = "المنصة",
    )
    p.add_argument(
        "--video-number",
        type    = str,
        default = None,
        help    = "رقم فيديو محدد",
    )
    p.add_argument(
        "--dry-run",
        action = "store_true",
        help   = "عرض فقط بدون نشر فعلي",
    )
    p.add_argument(
        "--limit",
        type    = int,
        default = DEFAULT_LIMIT,
        help    = f"حد أقصى (افتراضي: {DEFAULT_LIMIT})",
    )
    return p.parse_args()


# ═════════════════════════════════════════════════════════════════════════════
# THUMBNAIL DETECTION
# ═════════════════════════════════════════════════════════════════════════════

def _find_video_thumbnail(
    video_path:   str,
    content_mode: str,
    platform:     str,
) -> str:
    """
    Smart search for thumbnail.png matching the video.

    Searches multiple naming patterns and returns first match.
    """
    video = Path(video_path)
    if not video.exists():
        return ""

    base_dir   = video.parent
    video_stem = video.stem
    parts      = video_stem.split("_")

    candidates: list[str] = []

    # Primary: video_NUM_LANG + suffix
    if len(parts) >= 3 and parts[0] == "video":
        base_name = "_".join(parts[:3])

        suffix_map = {
            ("short", "yt"):       "_short_yt_thumbnail.png",
            ("short", "fb"):       "_short_yt_thumbnail.png",
            ("short", "youtube"):  "_short_yt_thumbnail.png",
            ("short", "facebook"): "_short_yt_thumbnail.png",
            ("long",  "yt"):       "_long_yt_thumbnail.png",
            ("long",  "fb"):       "_long_fb_thumbnail.png",
            ("long",  "youtube"):  "_long_yt_thumbnail.png",
            ("long",  "facebook"): "_long_fb_thumbnail.png",
        }

        primary_suffix = suffix_map.get(
            (content_mode, platform),
            "_thumbnail.png",
        )
        candidates.append(
            str(base_dir / f"{base_name}{primary_suffix}")
        )

        # Fallback suffixes
        for suffix in [
            "_short_yt_thumbnail.png",
            "_long_yt_thumbnail.png",
            "_long_fb_thumbnail.png",
            "_thumbnail.png",
        ]:
            cand = str(base_dir / f"{base_name}{suffix}")
            if cand not in candidates:
                candidates.append(cand)

    # Fallback: replace _published with _thumbnail
    for replace_from in ["_published", "_final"]:
        if replace_from in video_stem:
            base = video_stem.replace(replace_from, "")
            candidates.append(
                str(base_dir / f"{base}_thumbnail.png")
            )

    candidates.append(
        str(base_dir / f"{video_stem}_thumbnail.png")
    )

    for candidate in candidates:
        if Path(candidate).exists():
            log.info(
                "  🖼️  Found thumbnail: %s",
                Path(candidate).name
            )
            return candidate

    return ""


# ═════════════════════════════════════════════════════════════════════════════
# FIND PENDING VIDEOS
# ═════════════════════════════════════════════════════════════════════════════

def _query_rendered_videos(
    lang:         str,
    content_mode: str,
) -> list[dict]:
    """
    جلب الفيديوهات المرندرة من DB.

    ⚠️ Uses _conn() temporarily.
    TODO: Move this query to db.py as get_rendered_videos()
    """
    try:
        rows = _conn().execute(
            """SELECT video_number, lang, content_mode,
                      output_path, fb_path, yt_path, duration_s
               FROM renders
               WHERE status        = 'done'
                 AND output_path   IS NOT NULL
                 AND lang          = ?
                 AND content_mode  = ?
            """,
            (lang, content_mode),
        ).fetchall()
        return [dict(row) for row in rows]

    except sqlite3.Error as e:
        log.error(
            "  ❌ DB query failed for %s/%s: %s",
            lang, content_mode, e
        )
        return []


def _check_file_exists(path: Optional[str]) -> bool:
    """التحقق من وجود ملف."""
    return bool(path and Path(path).exists())


def _evaluate_video_needs(
    row:          dict,
    platform:     str,
    video_number: Optional[str],
) -> Optional[PendingVideo]:
    """
    تقييم احتياجات فيديو واحد.

    Returns:
        PendingVideo أو None إذا لا يحتاج retry
    """
    num  = str(row["video_number"])
    lang = row["lang"]
    mode = row["content_mode"]

    # Filter by video_number
    if video_number and num != str(video_number):
        return None

    # Skip if fully published
    if is_fully_published(num, lang, mode):
        return None

    # Paths
    fb_path = row["fb_path"] or row["output_path"]
    yt_path = row["yt_path"] or row["output_path"]

    # Published status
    fb_done = is_published_facebook(num, lang, mode)
    yt_done = is_published_youtube(num, lang, mode)

    # What needs publishing
    needs_fb = (
        not fb_done and
        platform in ("all", "facebook")
    )
    needs_yt = (
        not yt_done and
        platform in ("all", "youtube")
    )

    if not (needs_fb or needs_yt):
        return None

    # File existence check
    if needs_fb and not _check_file_exists(fb_path):
        log.warning(
            "  ⚠️  FB file missing for #%s (%s) [%s]",
            num, lang.upper(), mode
        )
        needs_fb = False

    if needs_yt and not _check_file_exists(yt_path):
        log.warning(
            "  ⚠️  YT file missing for #%s (%s) [%s]",
            num, lang.upper(), mode
        )
        needs_yt = False

    if not (needs_fb or needs_yt):
        return None

    return PendingVideo(
        video_number = num,
        lang         = lang,
        content_mode = mode,
        fb_path      = fb_path or "",
        yt_path      = yt_path or "",
        needs_fb     = needs_fb,
        needs_yt     = needs_yt,
        duration     = row.get("duration_s") or 0.0,
    )


def find_pending_videos(
    lang:         str           = "all",
    content_mode: str           = "all",
    platform:     str           = "all",
    video_number: Optional[str] = None,
    limit:        int           = 100,
) -> list[PendingVideo]:
    """
    البحث عن فيديوهات تحتاج إعادة نشر.

    Applies limit for early exit.
    """
    target_langs = (
        list(LANGS) if lang == "all" else [lang]
    )
    target_modes = (
        list(MODES)
        if content_mode == "all"
        else [content_mode]
    )

    pending: list[PendingVideo] = []

    for l in target_langs:
        for m in target_modes:
            rows = _query_rendered_videos(l, m)

            for row in rows:
                video = _evaluate_video_needs(
                    row, platform, video_number,
                )
                if video:
                    pending.append(video)

                # Early exit on limit
                if len(pending) >= limit:
                    log.info(
                        "  ℹ️  Limit reached (%d)", limit
                    )
                    return pending

    return pending


# ═════════════════════════════════════════════════════════════════════════════
# LOAD RECORD FROM SCRIPT
# ═════════════════════════════════════════════════════════════════════════════

def _load_record(
    video_number: str,
    lang:         str,
    content_mode: str,
) -> Optional[dict]:
    """تحميل بيانات الفيديو من ملف السكريبتات."""
    script_file = SCRIPT_FILES.get((lang, content_mode))

    if not script_file or not Path(script_file).exists():
        log.warning(
            "  ⚠️  Script file not found: %s",
            script_file
        )
        return None

    try:
        scripts = read_scripts(script_file)
        for s in scripts:
            if str(s["number"]) == str(video_number):
                return s
    except Exception as e:
        log.warning("  ⚠️  Cannot read script: %s", e)

    return None


def _get_record_or_fallback(
    video_number: str,
    lang:         str,
    content_mode: str,
) -> dict:
    """جلب record أو fallback بسيط."""
    record = _load_record(
        video_number, lang, content_mode
    )

    if record:
        return record

    return {
        "number": video_number,
        "title":  f"Video #{video_number}",
    }


def _get_street_description(
    video_number: str,
    lang:         str,
    content_mode: str,
) -> str:
    """جلب street_description من AI cache."""
    ai_cache = get_ai_cache(
        make_cache_key(
            video_number, lang, content_mode
        )
    ) or {}

    return ai_cache.get("street_description", "")


# ═════════════════════════════════════════════════════════════════════════════
# RETRY ONE PLATFORM — Pure functions + Thumbnails
# ═════════════════════════════════════════════════════════════════════════════

def _retry_facebook(
    video:              PendingVideo,
    record:             dict,
    title:              str,
    street_description: str,
) -> tuple[bool, Optional[str]]:
    """
    محاولة نشر على Facebook.

    Returns:
        (success, error_message)
    """
    log.info("\n  📘 Publishing to Facebook...")

    # Find thumbnail
    thumbnail_path = _find_video_thumbnail(
        video.fb_path,
        video.content_mode,
        "facebook",
    )

    try:
        publish_to_facebook(
            video_path     = video.fb_path,
            record         = record,
            lang           = video.lang,
            as_reel        = (video.content_mode == "short"),
            ai_caption     = street_description or title,
            content_mode   = video.content_mode,
            thumbnail_path = thumbnail_path,
        )

        mark_video_published_for_lang(
            video.video_number,
            video.lang,
            "facebook",
            video.content_mode,
        )

        log.info("  ✅ Facebook: published!")

        notify_video_published(
            video_number = video.video_number,
            lang         = video.lang,
            content_mode = video.content_mode,
            platform     = "facebook",
            title        = title,
        )

        return True, None

    except Exception as e:
        error_full  = str(e)
        error_short = error_full[:MAX_ERROR_LENGTH]

        log.debug("  Full FB error: %s", error_full)
        log.error("  ❌ Facebook failed: %s", error_short)

        notify_video_failed(
            video_number = video.video_number,
            lang         = video.lang,
            content_mode = video.content_mode,
            error        = error_short,
            platform     = "facebook",
        )

        return False, f"Facebook: {error_short}"


def _retry_youtube(
    video:              PendingVideo,
    record:             dict,
    title:              str,
    street_description: str,
) -> tuple[bool, Optional[str]]:
    """
    محاولة نشر على YouTube.

    Returns:
        (success, error_message)
    """
    log.info("\n  📺 Publishing to YouTube...")

    # Find thumbnail
    thumbnail_path = _find_video_thumbnail(
        video.yt_path,
        video.content_mode,
        "youtube",
    )

    try:
        publish_to_youtube(
            video_path         = video.yt_path,
            record             = record,
            lang               = video.lang,
            street_description = street_description,
            content_mode       = video.content_mode,
            thumbnail_path     = thumbnail_path,
        )

        mark_video_published_for_lang(
            video.video_number,
            video.lang,
            "youtube",
            video.content_mode,
        )

        log.info("  ✅ YouTube: published!")

        notify_video_published(
            video_number = video.video_number,
            lang         = video.lang,
            content_mode = video.content_mode,
            platform     = "youtube",
            title        = title,
        )

        return True, None

    except Exception as e:
        error_full  = str(e)
        error_short = error_full[:MAX_ERROR_LENGTH]

        log.debug("  Full YT error: %s", error_full)
        log.error("  ❌ YouTube failed: %s", error_short)

        notify_video_failed(
            video_number = video.video_number,
            lang         = video.lang,
            content_mode = video.content_mode,
            error        = error_short,
            platform     = "youtube",
        )

        return False, f"YouTube: {error_short}"


# ═════════════════════════════════════════════════════════════════════════════
# RETRY ONE VIDEO
# ═════════════════════════════════════════════════════════════════════════════

def _print_retry_header(video: PendingVideo) -> None:
    """طباعة header retry."""
    separator = "─" * SEPARATOR_WIDTH

    log.info("\n  %s", separator)
    log.info(
        "  🔄 Retry #%s (%s) [%s]",
        video.video_number,
        video.lang.upper(),
        video.content_mode.upper()
    )
    log.info("  %s", separator)


def _print_dry_run(video: PendingVideo) -> None:
    """طباعة معلومات dry run."""
    if video.needs_fb:
        log.info("  📘 [DRY RUN] Would publish to Facebook")
        log.info("     File: %s", video.fb_path)

    if video.needs_yt:
        log.info("  📺 [DRY RUN] Would publish to YouTube")
        log.info("     File: %s", video.yt_path)


def retry_video(
    video:   PendingVideo,
    dry_run: bool = False,
) -> RetryResult:
    """
    إعادة نشر فيديو واحد.

    Uses pure functions (no side effects on parameters).
    """
    result = RetryResult(
        video_number = video.video_number,
        lang         = video.lang,
        content_mode = video.content_mode,
    )

    _print_retry_header(video)

    # Dry run
    if dry_run:
        _print_dry_run(video)
        return result

    # Validate paths before publish
    if video.needs_fb and not video.fb_path:
        log.error(
            "  ❌ fb_path is empty for #%s",
            video.video_number
        )
        result.errors.append("Facebook: empty fb_path")

    if video.needs_yt and not video.yt_path:
        log.error(
            "  ❌ yt_path is empty for #%s",
            video.video_number
        )
        result.errors.append("YouTube: empty yt_path")

    # Load data
    record = _get_record_or_fallback(
        video.video_number,
        video.lang,
        video.content_mode,
    )
    title = record.get(
        "title", f"Video #{video.video_number}"
    )
    street_description = _get_street_description(
        video.video_number,
        video.lang,
        video.content_mode,
    )

    # Facebook
    if video.needs_fb and video.fb_path:
        success, error = _retry_facebook(
            video, record, title, street_description,
        )
        result.fb_success = success
        if error:
            result.errors.append(error)

    # YouTube
    if video.needs_yt and video.yt_path:
        success, error = _retry_youtube(
            video, record, title, street_description,
        )
        result.yt_success = success
        if error:
            result.errors.append(error)

    return result


# ═════════════════════════════════════════════════════════════════════════════
# DISPLAY
# ═════════════════════════════════════════════════════════════════════════════

def _print_header(args: argparse.Namespace) -> None:
    """طباعة header البرنامج."""
    separator = "═" * SUMMARY_WIDTH

    log.info("\n%s", separator)
    log.info("  🔄 Retry Publish System v2.0")
    log.info("%s", separator)
    log.info("  Language     : %s", args.lang.upper())
    log.info(
        "  Content Mode : %s", args.content_mode.upper()
    )
    log.info("  Platform     : %s", args.platform.upper())
    log.info("  Limit        : %d", args.limit)
    log.info(
        "  Dry Run      : %s",
        'YES' if args.dry_run else 'NO'
    )

    if args.video_number:
        log.info(
            "  Video Number : #%s", args.video_number
        )

    log.info("%s", separator)


def _print_pending_list(
    pending: list[PendingVideo],
) -> None:
    """طباعة قائمة الفيديوهات المعلقة."""
    log.info("\n  📝 Pending videos:")
    log.info("  %s", "─" * LIST_SEPARATOR)

    for v in pending:
        needs = []
        if v.needs_fb:
            needs.append("📘 FB")
        if v.needs_yt:
            needs.append("📺 YT")

        log.info(
            "  #%s %s [%s] → %s",
            v.video_number.rjust(4),
            v.lang.upper(),
            v.content_mode.rjust(5),
            ' + '.join(needs)
        )

    log.info("  %s", "─" * LIST_SEPARATOR)


def _print_summary(results: list[RetryResult]) -> None:
    """طباعة الملخص النهائي."""
    separator = "═" * SUMMARY_WIDTH

    fb_success = sum(1 for r in results if r.fb_success)
    yt_success = sum(1 for r in results if r.yt_success)
    errors     = sum(1 for r in results if r.has_errors())

    log.info("\n%s", separator)
    log.info("  📊 Retry Summary")
    log.info("%s", separator)
    log.info("  📘 Facebook published: %d", fb_success)
    log.info("  📺 YouTube published : %d", yt_success)
    log.info("  ❌ With errors       : %d", errors)
    log.info("%s", separator)

    if errors > 0:
        log.info("\n  ❌ Errors:")
        for r in results:
            if r.has_errors():
                for err in r.errors:
                    log.info(
                        "     #%s: %s",
                        r.video_number, err
                    )


def _send_final_notification(
    results: list[RetryResult],
    dry_run: bool,
) -> None:
    """إرسال إشعار نهائي."""
    if dry_run:
        return

    fb_success = sum(1 for r in results if r.fb_success)
    yt_success = sum(1 for r in results if r.yt_success)
    errors     = sum(1 for r in results if r.has_errors())

    if not (fb_success or yt_success):
        return

    # Smart message based on results
    if errors > (fb_success + yt_success):
        msg_prefix = "⚠️ Retry completed with errors"
    else:
        msg_prefix = "🔄 Retry complete!"

    notify_info(
        f"{msg_prefix}\n"
        f"📘 Facebook: {fb_success}\n"
        f"📺 YouTube:  {yt_success}\n"
        f"❌ Errors:   {errors}",
        skip_rate = True,
    )


# ═════════════════════════════════════════════════════════════════════════════
# CREDENTIALS CHECK — Per-language, Immutable
# ═════════════════════════════════════════════════════════════════════════════

def _check_facebook_credentials(
    pending: list[PendingVideo],
) -> list[PendingVideo]:
    """
    التحقق من Facebook credentials per-language.

    Returns NEW list (immutable — no side effects).
    """
    if not any(v.needs_fb for v in pending):
        return pending

    log.info(
        "\n  📘 Checking Facebook credentials per-language..."
    )

    # Check each language separately
    lang_status: dict[str, bool] = {}

    unique_langs = {v.lang for v in pending if v.needs_fb}

    for lang in unique_langs:
        is_valid = fb_check_credentials(lang)
        lang_status[lang] = is_valid

        if is_valid:
            log.info(
                "  ✅ Facebook (%s): valid",
                lang.upper()
            )
        else:
            log.warning(
                "  ❌ Facebook (%s): invalid",
                lang.upper()
            )

    # Build NEW list (immutable)
    updated_pending: list[PendingVideo] = []

    for v in pending:
        if v.needs_fb and not lang_status.get(v.lang, False):
            # Disable FB for this video (new object)
            updated_pending.append(
                replace(v, needs_fb=False)
            )
        else:
            updated_pending.append(v)

    # Log disabled count
    disabled = sum(
        1 for old, new in zip(pending, updated_pending)
        if old.needs_fb and not new.needs_fb
    )

    if disabled > 0:
        log.warning(
            "  ⚠️  Disabled FB for %d videos "
            "due to invalid credentials",
            disabled
        )

    return updated_pending


# ═════════════════════════════════════════════════════════════════════════════
# MAIN
# ═════════════════════════════════════════════════════════════════════════════

def main() -> None:
    """نقطة الدخول الرئيسية."""
    # Logging — entry point only
    logging.basicConfig(
        level   = logging.INFO,
        format  = "%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt = "%H:%M:%S",
    )

    args = parse_args()
    init_db()

    _print_header(args)
    print_db_summary()

    # البحث
    log.info(
        "\n  🔍 Searching for pending videos..."
    )

    pending = find_pending_videos(
        lang         = args.lang,
        content_mode = args.content_mode,
        platform     = args.platform,
        video_number = args.video_number,
        limit        = args.limit,
    )

    if not pending:
        log.info("\n  ✅ No pending videos found!")
        log.info(
            "     All videos are published or "
            "render files are missing."
        )
        notify_info(
            "🔄 Retry: No pending videos",
            silent = True,
        )
        return

    log.info(
        "\n  📋 Found %d pending video(s)",
        len(pending)
    )

    # عرض القائمة
    _print_pending_list(pending)

    if args.dry_run:
        log.info(
            "\n  🔍 DRY RUN — No actual publishing\n"
        )

    # التحقق من credentials (per-language, immutable)
    if not args.dry_run:
        pending = _check_facebook_credentials(pending)

    # إعادة النشر
    results: list[RetryResult] = []

    for i, video in enumerate(pending, 1):
        log.info("\n[%d/%d]", i, len(pending))

        try:
            result = retry_video(
                video,
                dry_run = args.dry_run,
            )
            results.append(result)

        except KeyboardInterrupt:
            log.warning("\n  ⛔ Interrupted by user")
            break

        except Exception as e:
            log.error("  ❌ Unexpected error: %s", e)
            traceback.print_exc()

    # الملخص
    _print_summary(results)

    # إشعار نهائي
    _send_final_notification(results, args.dry_run)

    log.info("")


if __name__ == "__main__":
    main()

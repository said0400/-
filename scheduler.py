"""
📅 scheduler.py — Daily Video Publisher v2.0

Features:
  ✅ DST-aware scheduling (ZoneInfo)
  ✅ Smart thumbnail detection
  ✅ Per-language Facebook credentials
  ✅ as_reel dynamic based on content_mode
  ✅ Per-platform independent video fetching
  ✅ Anti-duplicate publishing (MIN_MINUTES_BETWEEN > tolerance)
  ✅ Per-platform quota tracking
  ✅ Timezone-safe datetime comparisons
  ✅ Next slot calculation (skips active slots)

Timezones:
  🇸🇦 Arabic   (Asia/Riyadh   — no DST)
  🇫🇷 French   (Europe/Paris  — DST aware)
  🇺🇸 English  (America/New_York — DST aware)

Usage:
  python scheduler.py --lang ar --mode short
  python scheduler.py --lang fr --mode long
  python scheduler.py --lang en --mode short --platform both
  python scheduler.py --check
"""

from __future__ import annotations

import argparse
import logging
import sys
import traceback
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

try:
    from zoneinfo import ZoneInfo
except ImportError:
    try:
        from backports.zoneinfo import ZoneInfo  # type: ignore
    except ImportError:
        raise ImportError(
            "zoneinfo not available.\n"
            "Install: pip install backports.zoneinfo\n"
            "Or upgrade to Python 3.9+"
        )

from db import (
    init_db,
    get_ready_to_publish,
    mark_published,
    mark_pre_generated_published,
    is_published_youtube,
    is_published_facebook,
    get_today_published_count,
    get_daily_quota,
    get_daily_remaining_publish,
    get_last_publish_time,
    print_db_summary,
    make_cache_key,
    get_ai_cache,
)
from youtube import (
    publish_to_youtube,
    credentials_available as yt_credentials_available,
    check_credentials    as yt_check_credentials,
)
from facebook import (
    publish_to_facebook,
    credentials_available as fb_credentials_available,
    check_credentials    as fb_check_credentials,
)

log = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════
# CONSTANTS
# ═══════════════════════════════════════════════════════════════════

BASE_DIR = Path(__file__).parent.resolve()

VALID_LANGS     = ("ar", "fr", "en")
VALID_MODES     = ("short", "long")
VALID_PLATFORMS = ("yt", "fb", "both")

LANG_TIMEZONES: dict[str, str] = {
    "ar": "Asia/Riyadh",
    "fr": "Europe/Paris",
    "en": "America/New_York",
}

PUBLISH_SCHEDULE: dict[str, dict[str, list[tuple[int, int]]]] = {
    "ar": {
        "short": [(8, 0), (12, 0), (15, 0), (18, 0), (21, 0)],
        "long":  [(22, 0)],
    },
    "fr": {
        "short": [(8, 0), (12, 0), (15, 0), (18, 0), (21, 0)],
        "long":  [(22, 0)],
    },
    "en": {
        "short": [(8, 0), (12, 0), (15, 0), (18, 0), (21, 0)],
        "long":  [(22, 0)],
    },
}

# Tolerance (minutes after slot to still consider active)
SCHEDULE_TOLERANCE_MINUTES = 25

# Anti-duplicate: must be > tolerance to prevent overlap
MIN_MINUTES_BETWEEN_PUBLISH = 30


# ═══════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description     = "📅 Daily Video Scheduler",
        formatter_class = argparse.RawTextHelpFormatter,
    )
    p.add_argument(
        "--lang",
        choices = list(VALID_LANGS),
        default = None,
    )
    p.add_argument(
        "--mode",
        choices = list(VALID_MODES),
        default = None,
    )
    p.add_argument(
        "--platform",
        choices = list(VALID_PLATFORMS),
        default = "yt",
    )
    p.add_argument("--check",   action="store_true")
    p.add_argument("--force",   action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument(
        "--output-dir",
        type    = str,
        default = str(BASE_DIR / "output"),
    )
    return p.parse_args()


# ═══════════════════════════════════════════════════════════════════
# TIME HELPERS (DST-safe)
# ═══════════════════════════════════════════════════════════════════

def _now_utc() -> datetime:
    """Current time in UTC (aware)."""
    return datetime.now(timezone.utc)


def _local_time(lang: str) -> datetime:
    """Current time in language's timezone (KeyError-safe)."""
    tz_name = LANG_TIMEZONES.get(lang, "UTC")
    return datetime.now(ZoneInfo(tz_name))


def _get_utc_slots(lang: str, mode: str) -> list[datetime]:
    """
    UTC datetimes for today's slots.

    Uses datetime() with ZoneInfo directly (DST-safe).
    """
    tz_name     = LANG_TIMEZONES.get(lang, "UTC")
    tz          = ZoneInfo(tz_name)
    today       = datetime.now(tz).date()
    local_slots = PUBLISH_SCHEDULE.get(lang, {}).get(mode, [])
    utc_slots   : list[datetime] = []

    for (h, m) in local_slots:
        try:
            # datetime() with ZoneInfo handles DST correctly
            local_dt = datetime(
                today.year, today.month, today.day,
                h, m, 0,
                tzinfo=tz,
            )
            utc_dt = local_dt.astimezone(timezone.utc)
            utc_slots.append(utc_dt)
        except Exception as e:
            log.warning(
                "  ⚠️  Cannot build slot %02d:%02d for %s: %s",
                h, m, lang, e
            )

    return utc_slots


def _is_slot_active(
    slot_dt:   datetime,
    now_utc:   datetime,
    tolerance: int = SCHEDULE_TOLERANCE_MINUTES,
) -> bool:
    """Is this slot currently active (within tolerance)?"""
    diff = (now_utc - slot_dt).total_seconds()
    return 0 <= diff <= tolerance * 60


def _is_publish_time(
    lang:      str,
    mode:      str,
    tolerance: int = SCHEDULE_TOLERANCE_MINUTES,
) -> bool:
    """Is any slot active right now?"""
    now_utc   = _now_utc()
    utc_slots = _get_utc_slots(lang, mode)
    return any(
        _is_slot_active(slot, now_utc, tolerance)
        for slot in utc_slots
    )


def _was_recently_published(
    lang:        str,
    mode:        str,
    platform:    str,
    min_minutes: int = MIN_MINUTES_BETWEEN_PUBLISH,
) -> bool:
    """
    Check if recently published (prevent double-publish).

    Handles both naive and aware datetimes safely.
    """
    last = get_last_publish_time(lang, mode, platform)
    if not last:
        return False

    # Make aware if naive
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)

    minutes_ago = (_now_utc() - last).total_seconds() / 60
    return minutes_ago < min_minutes


def _next_slot_in_minutes(lang: str, mode: str) -> Optional[int]:
    """
    Minutes until next slot.

    Skips currently active slots (to avoid +1day error).
    """
    now_utc   = _now_utc()
    utc_slots = _get_utc_slots(lang, mode)
    min_wait  : Optional[int] = None

    for slot_dt in utc_slots:
        # Skip active slots
        if _is_slot_active(slot_dt, now_utc):
            continue

        target = slot_dt if slot_dt > now_utc \
                 else slot_dt + timedelta(days=1)
        wait = int((target - now_utc).total_seconds() / 60)
        if min_wait is None or wait < min_wait:
            min_wait = wait

    return min_wait


# ═══════════════════════════════════════════════════════════════════
# SMART THUMBNAIL DETECTION
# ═══════════════════════════════════════════════════════════════════

def _find_thumbnail(
    video_path:   str,
    content_mode: str,
    platform:     str,
) -> str:
    """
    Smart search for thumbnail.png matching the video.

    Searches multiple naming patterns:
        - video_NUM_LANG_short_yt_thumbnail.png
        - video_NUM_LANG_long_yt_thumbnail.png
        - video_NUM_LANG_long_fb_thumbnail.png
        - video_NUM_LANG_thumbnail.png (fallback)
    """
    video = Path(video_path)
    if not video.exists():
        return ""

    base_dir   = video.parent
    video_stem = video.stem
    parts      = video_stem.split("_")

    candidates: list[str] = []

    # Primary: video_NUM_LANG + content/platform suffix
    if len(parts) >= 3 and parts[0] == "video":
        base_name = "_".join(parts[:3])  # video_NUM_LANG

        suffix_map = {
            ("short", "yt"):  "_short_yt_thumbnail.png",
            ("short", "fb"):  "_short_yt_thumbnail.png",
            ("long",  "yt"):  "_long_yt_thumbnail.png",
            ("long",  "fb"):  "_long_fb_thumbnail.png",
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

    # Last fallback
    candidates.append(
        str(base_dir / f"{video_stem}_thumbnail.png")
    )

    # Find first existing
    for candidate in candidates:
        if Path(candidate).exists():
            log.info(
                "  🖼️  Found thumbnail: %s",
                Path(candidate).name
            )
            return candidate

    log.debug(
        "  ℹ️  No thumbnail found for: %s",
        video.name
    )
    return ""


# ═══════════════════════════════════════════════════════════════════
# SCHEDULE DISPLAY
# ═══════════════════════════════════════════════════════════════════

def _display_schedule() -> None:
    """Display full schedule with current status."""
    now_utc = _now_utc()

    log.info("\n%s", "═" * 65)
    log.info("  📅 Publishing Schedule")
    log.info(
        "  🕐 UTC: %s",
        now_utc.strftime('%Y-%m-%d %H:%M:%S')
    )
    log.info("%s", "═" * 65)

    lang_labels = {
        "ar": "🇸🇦 Arabic   (Asia/Riyadh)",
        "fr": "🇫🇷 French   (Europe/Paris)",
        "en": "🇺🇸 English  (America/New_York)",
    }

    for lang in VALID_LANGS:
        local_now = _local_time(lang)
        tz_name   = local_now.strftime("%Z %z")
        label     = lang_labels[lang]

        log.info("\n  %s", label)
        log.info(
            "  Local: %s (%s) | UTC: %s",
            local_now.strftime('%H:%M'),
            tz_name,
            now_utc.strftime('%H:%M')
        )

        for mode in VALID_MODES:
            local_slots = PUBLISH_SCHEDULE[lang][mode]
            utc_slots   = _get_utc_slots(lang, mode)
            quota       = get_daily_quota(mode)
            pub_yt      = get_today_published_count(
                lang, mode, "youtube"
            )
            pub_fb      = get_today_published_count(
                lang, mode, "facebook"
            )
            next_min    = _next_slot_in_minutes(lang, mode)
            next_str    = f"{next_min}min" if next_min else "—"

            log.info(
                "    %s: YT: %d/%d | FB: %d/%d | Next: %s",
                mode.upper().ljust(6),
                pub_yt, quota,
                pub_fb, quota,
                next_str,
            )

            for (h, m), utc_dt in zip(local_slots, utc_slots):
                is_now = _is_slot_active(utc_dt, now_utc)
                log.info(
                    "      Local %02d:%02d → UTC %s %s",
                    h, m,
                    utc_dt.strftime('%H:%M'),
                    '← NOW 🟢' if is_now else ''
                )

    log.info("\n%s", "═" * 65)


# ═══════════════════════════════════════════════════════════════════
# STREET DESCRIPTION
# ═══════════════════════════════════════════════════════════════════

def _get_street_description(
    num:          str,
    lang:         str,
    content_mode: str,
) -> str:
    """Get street_description from AI cache."""
    try:
        cache_key = make_cache_key(
            str(num), lang, content_mode
        )
        cached = get_ai_cache(cache_key)
        if cached:
            return cached.get("street_description", "") or ""
    except Exception as e:
        log.warning("  ⚠️  AI cache error: %s", e)
    return ""


# ═══════════════════════════════════════════════════════════════════
# PUBLISH HELPERS — with Thumbnails + Per-language FB
# ═══════════════════════════════════════════════════════════════════

def _publish_to_youtube_safe(
    video_path: str,
    record:     dict,
    lang:       str,
    mode:       str,
    dry_run:    bool = False,
) -> bool:
    """Publish to YouTube with thumbnail."""
    num = str(record.get("number", "?"))

    if is_published_youtube(num, lang, mode):
        log.info("  ⏭️  #%s already on YouTube", num)
        return True

    if not yt_credentials_available(lang):
        log.warning(
            "  ⚠️  No YouTube credentials (%s)",
            lang.upper()
        )
        return False

    if not Path(video_path).exists():
        log.error("  ❌ Video not found: %s", video_path)
        return False

    if dry_run:
        log.info("  🔍 [DRY RUN] #%s → YouTube", num)
        return True

    # Find thumbnail
    thumbnail_path = _find_thumbnail(video_path, mode, "yt")

    try:
        result = publish_to_youtube(
            video_path         = video_path,
            record             = record,
            lang               = lang,
            street_description = _get_street_description(
                num, lang, mode
            ),
            content_mode       = mode,
            thumbnail_path     = thumbnail_path,
        )
        log.info(
            "  📺 YouTube: #%s → %s",
            num, result.get('url', '')
        )
        mark_published(num, lang, "youtube", mode)
        return True
    except Exception as e:
        log.error("  ❌ YouTube failed #%s: %s", num, e)
        return False


def _publish_to_facebook_safe(
    video_path: str,
    record:     dict,
    lang:       str,
    mode:       str,
    dry_run:    bool = False,
) -> bool:
    """
    Publish to Facebook with thumbnail.

    as_reel dynamic: short=Reel, long=Video
    Per-language credentials.
    """
    num = str(record.get("number", "?"))

    if is_published_facebook(num, lang, mode):
        log.info("  ⏭️  #%s already on Facebook", num)
        return True

    # Per-language credentials check
    if not fb_credentials_available(lang):
        log.warning(
            "  ⚠️  No Facebook credentials (%s)",
            lang.upper()
        )
        return False

    if not Path(video_path).exists():
        log.error("  ❌ Video not found: %s", video_path)
        return False

    if dry_run:
        log.info("  🔍 [DRY RUN] #%s → Facebook", num)
        return True

    # Find thumbnail
    thumbnail_path = _find_thumbnail(video_path, mode, "fb")

    try:
        street_desc = _get_street_description(num, lang, mode)
        publish_to_facebook(
            video_path     = video_path,
            record         = record,
            lang           = lang,
            as_reel        = (mode == "short"),  # Dynamic!
            ai_caption     = (
                street_desc or record.get("title", "")
            ),
            content_mode   = mode,
            thumbnail_path = thumbnail_path,
        )
        log.info("  📘 Facebook: #%s published", num)
        mark_published(num, lang, "facebook", mode)
        return True
    except Exception as e:
        log.error("  ❌ Facebook failed #%s: %s", num, e)
        return False


# ═══════════════════════════════════════════════════════════════════
# PUBLISH ONE VIDEO
# ═══════════════════════════════════════════════════════════════════

def _publish_one(
    video_info_yt: Optional[dict],
    video_info_fb: Optional[dict],
    lang:          str,
    mode:          str,
    platforms:     list[str],
    dry_run:       bool = False,
) -> bool:
    """
    Publish one video (independent video per platform).

    mark_pre_generated_published only if ALL needed platforms succeed.
    """
    primary_info = video_info_yt or video_info_fb
    if not primary_info:
        log.error("  ❌ No video info provided")
        return False

    num   = str(primary_info.get("video_number", "?"))
    title = primary_info.get("title", f"Video #{num}")

    log.info(
        "\n  🎬 Publishing #%s [%s/%s]",
        num, lang.upper(), mode.upper()
    )
    log.info("     %s", title[:55])

    yt_success = False
    fb_success = False

    # YouTube
    if "yt" in platforms and video_info_yt:
        yt_path = (
            video_info_yt.get("yt_path") or
            video_info_yt.get("output_path") or
            ""
        )
        if yt_path:
            record = {
                "number": num,
                "title":  title,
                "lang":   lang,
            }
            yt_success = _publish_to_youtube_safe(
                yt_path, record, lang, mode, dry_run,
            )
        else:
            log.warning("  ⚠️  YT path empty for #%s", num)
    elif "yt" in platforms:
        log.warning(
            "  ⚠️  YT video not available for #%s", num
        )

    # Facebook
    if "fb" in platforms and video_info_fb:
        fb_path = (
            video_info_fb.get("fb_path") or
            video_info_fb.get("output_path") or
            ""
        )
        if fb_path:
            record = {
                "number": num,
                "title":  title,
                "lang":   lang,
            }
            fb_success = _publish_to_facebook_safe(
                fb_path, record, lang, mode, dry_run,
            )
        else:
            log.warning("  ⚠️  FB path empty for #%s", num)
    elif "fb" in platforms:
        log.warning(
            "  ⚠️  FB video not available for #%s", num
        )

    any_success = yt_success or fb_success

    if not dry_run and any_success:
        # Mark only if ALL needed platforms succeeded
        yt_needed = "yt" in platforms
        fb_needed = "fb" in platforms
        all_ok    = (
            (yt_success if yt_needed else True) and
            (fb_success if fb_needed else True)
        )
        if all_ok:
            mark_pre_generated_published(num, lang, mode)
        else:
            log.warning(
                "  ⚠️  #%s: partial success — "
                "not marking as fully done",
                num
            )

    return any_success


# ═══════════════════════════════════════════════════════════════════
# PUBLISH FOR LANG + MODE
# ═══════════════════════════════════════════════════════════════════

def publish_for_lang_mode(
    lang:      str,
    mode:      str,
    platforms: list[str],
    force:     bool = False,
    dry_run:   bool = False,
) -> int:
    """
    Publish one video for (lang + mode + platforms).

    Each platform has independent quota + video fetch.
    """
    log.info("\n  %s", "─" * 55)
    log.info(
        "  📋 %s %s [%s]",
        lang.upper(),
        mode.upper(),
        '/'.join(p.upper() for p in platforms)
    )

    # Check time
    if not force and not _is_publish_time(lang, mode):
        next_min = _next_slot_in_minutes(lang, mode)
        log.info("  ⏳ Next slot in ~%s min", next_min)
        return 0

    # Check each platform independently
    active_platforms: list[str] = []

    for platform_code in platforms:
        db_platform = (
            "youtube" if platform_code == "yt"
            else "facebook"
        )

        # Quota check
        remaining = get_daily_remaining_publish(
            lang, mode, db_platform
        )
        if remaining <= 0:
            log.info(
                "  ✅ Quota reached: %s [%s/%s]",
                db_platform.upper(), lang, mode
            )
            continue

        # Anti-duplicate
        if not force and _was_recently_published(
            lang, mode, db_platform
        ):
            log.info(
                "  ⏭️  %s: published recently",
                db_platform.upper()
            )
            continue

        active_platforms.append(platform_code)

    if not active_platforms:
        log.info("  ℹ️  No active platforms")
        return 0

    # Fetch video independently for each platform
    video_info_yt: Optional[dict] = None
    video_info_fb: Optional[dict] = None

    if "yt" in active_platforms:
        ready = get_ready_to_publish(
            lang         = lang,
            content_mode = mode,
            platform     = "youtube",
            limit        = 1,
        )
        if ready:
            video_info_yt = ready[0]
        else:
            log.warning(
                "  ⚠️  No ready YT videos for %s/%s",
                lang.upper(), mode.upper()
            )

    if "fb" in active_platforms:
        ready = get_ready_to_publish(
            lang         = lang,
            content_mode = mode,
            platform     = "facebook",
            limit        = 1,
        )
        if ready:
            video_info_fb = ready[0]
        else:
            log.warning(
                "  ⚠️  No ready FB videos for %s/%s",
                lang.upper(), mode.upper()
            )

    # No videos at all
    if not video_info_yt and not video_info_fb:
        log.warning(
            "  ⚠️  No ready videos for %s/%s",
            lang.upper(), mode.upper()
        )
        return 0

    published = _publish_one(
        video_info_yt = video_info_yt,
        video_info_fb = video_info_fb,
        lang          = lang,
        mode          = mode,
        platforms     = active_platforms,
        dry_run       = dry_run,
    )

    return 1 if published else 0


# ═══════════════════════════════════════════════════════════════════
# CREDENTIALS CHECK — Per-language
# ═══════════════════════════════════════════════════════════════════

def _check_and_filter_platforms(
    platforms: list[str],
    lang:      str,
) -> list[str]:
    """Check credentials and remove invalid platforms."""
    valid: list[str] = []

    for p in platforms:
        if p == "yt":
            log.info(
                "  📺 Checking YouTube (%s)...",
                lang.upper()
            )
            if yt_check_credentials(lang):
                valid.append("yt")
            else:
                log.warning(
                    "  ⚠️  YouTube invalid (%s)",
                    lang.upper()
                )
        elif p == "fb":
            log.info(
                "  📘 Checking Facebook (%s)...",
                lang.upper()
            )
            # Per-language check
            if fb_check_credentials(lang):
                valid.append("fb")
            else:
                log.warning(
                    "  ⚠️  Facebook invalid (%s)",
                    lang.upper()
                )

    return valid


# ═══════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════

def main() -> None:
    args = parse_args()

    # Logging — entry point only
    logging.basicConfig(
        level   = logging.INFO,
        format  = "%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt = "%H:%M:%S",
    )

    init_db()

    # ── Check mode ────────────────────────────────────────────
    if args.check:
        _display_schedule()
        print_db_summary()
        return

    # ── Validate ──────────────────────────────────────────────
    if not args.lang:
        log.error("❌ --lang required")
        sys.exit(1)
    if not args.mode:
        log.error("❌ --mode required")
        sys.exit(1)

    lang     = args.lang
    mode     = args.mode
    platform = args.platform

    platforms_list = (
        ["yt", "fb"]
        if platform == "both"
        else [platform]
    )

    # ── Header ────────────────────────────────────────────────
    now_utc   = _now_utc()
    local_now = _local_time(lang)
    tz_name   = local_now.strftime("%Z %z")

    log.info("\n%s", "═" * 65)
    log.info("  📅 Video Scheduler")
    log.info("%s", "═" * 65)
    log.info("  Lang      : %s", lang.upper())
    log.info("  Mode      : %s", mode.upper())
    log.info(
        "  Platforms : %s",
        ', '.join(p.upper() for p in platforms_list)
    )
    log.info(
        "  UTC       : %s",
        now_utc.strftime('%Y-%m-%d %H:%M:%S')
    )
    log.info(
        "  Local     : %s (%s)",
        local_now.strftime('%H:%M:%S'),
        tz_name
    )
    log.info(
        "  Force     : %s",
        'YES' if args.force else 'NO'
    )
    log.info(
        "  Dry Run   : %s",
        'YES ⚠️' if args.dry_run else 'NO'
    )

    # ── Slots ─────────────────────────────────────────────────
    utc_slots   = _get_utc_slots(lang, mode)
    local_slots = PUBLISH_SCHEDULE[lang][mode]
    log.info("\n  📅 Today's slots:")
    for (h, m), utc_dt in zip(local_slots, utc_slots):
        is_now = _is_slot_active(utc_dt, now_utc)
        log.info(
            "     %02d:%02d local → %s UTC %s",
            h, m,
            utc_dt.strftime('%H:%M'),
            '🟢 NOW' if is_now else ''
        )

    # ── Quota ─────────────────────────────────────────────────
    quota = get_daily_quota(mode)
    log.info("\n  📊 Today's quota:")
    for p_code in platforms_list:
        db_p      = (
            "youtube" if p_code == "yt" else "facebook"
        )
        published = get_today_published_count(
            lang, mode, db_p
        )
        remaining = get_daily_remaining_publish(
            lang, mode, db_p
        )
        log.info(
            "     %s: %d/%d | %d remaining",
            db_p.upper(),
            published, quota,
            remaining
        )

    log.info("")

    # ── Credentials ───────────────────────────────────────────
    platforms_list = _check_and_filter_platforms(
        platforms_list, lang
    )
    if not platforms_list:
        log.error("  ❌ No valid platforms")
        sys.exit(1)

    # ── Publish ───────────────────────────────────────────────
    try:
        count = publish_for_lang_mode(
            lang      = lang,
            mode      = mode,
            platforms = platforms_list,
            force     = args.force,
            dry_run   = args.dry_run,
        )

        log.info("\n%s", "═" * 65)
        if count > 0:
            log.info("  ✅ Published: %d video(s)", count)
        else:
            log.info("  ℹ️  Nothing published")
        print_db_summary()
        log.info("%s\n", "═" * 65)

    except KeyboardInterrupt:
        log.warning("\n⛔ Interrupted")
        sys.exit(130)
    except Exception as e:
        log.error("\n❌ Error: %s", e)
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()

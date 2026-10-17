"""
🏥 System Health Check v2.0 — Final Production Edition

Features:
  ✅ Database integrity & size
  ✅ API Keys (Gemini, Groq, Pexels, Pixabay)
  ✅ Platform Tokens (YouTube, Facebook per-language)
  ✅ Storage directories
  ✅ Today's publishing status (timezone-aware)
  ✅ Disk usage
  ✅ WhatsApp/Telegram notifications
  ✅ Urgent alerts on critical errors
  ✅ JSON & Console output formats
  ✅ Granular check selection
  ✅ API keys hidden from URLs (security)
  ✅ Try/except per check (crash-safe)
  ✅ Try/except per DB query
  ✅ Hidden files ignored in storage check
  ✅ Per-language Facebook checks
  ✅ UTC timestamps everywhere
  ✅ Local hour per language timezone
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Optional

import requests

try:
    from zoneinfo import ZoneInfo
except ImportError:
    try:
        from backports.zoneinfo import ZoneInfo  # type: ignore
    except ImportError:
        ZoneInfo = None  # type: ignore

from db import init_db
from db import _conn  # temporary — TODO: move queries to db.py

from notifier import (
    notify_error,
    notify_info,
    notify_token_expired,
    notify_token_warning,
    notify_warning,
)
from token_manager import (
    check_facebook_token,
    check_youtube_token,
)

log = logging.getLogger(__name__)

# ═════════════════════════════════════════════════════════════════════════════
# CONSTANTS
# ═════════════════════════════════════════════════════════════════════════════

# Paths
BASE_DIR = Path(__file__).parent.resolve()
DB_PATH  = BASE_DIR / "vsg.db"

# Supported values
LANGS = ("ar", "fr", "en")
MODES = ("short", "long")

# Language timezones
LANG_TIMEZONES: dict[str, str] = {
    "ar": "Asia/Riyadh",
    "fr": "Europe/Paris",
    "en": "America/New_York",
}

# Thresholds
DB_MAX_SIZE_MB        = 500
DB_WARNING_SIZE_MB    = 200
DISK_WARNING_PERCENT  = 85
DISK_CRITICAL_PERCENT = 95
PUBLISH_WARNING_HOURS = 12
OUTPUT_WARNING_GB     = 5
HIGH_FAILED_RENDERS   = 10

# API Key counting
MAX_GEMINI_KEYS  = 50
MAX_OTHER_KEYS   = 10
MIN_GEMINI_KEYS  = 5

# Timeouts
API_TEST_TIMEOUT = 10

# Display
SUMMARY_WIDTH = 65
SECTION_WIDTH = 50

# Required structure
REQUIRED_DIRS: dict[str, str] = {
    "assets/music":            "Music files",
    "assets/music/motivation": "Motivation music",
    "assets/music/cinematic":  "Cinematic music",
    "assets/videos":           "Local videos",
    "sfx/swoosh":              "Swoosh SFX",
    "sfx/whoosh":              "Whoosh SFX",
    "sfx/smart":               "Smart SFX",
    "sfx/transitions":         "Transition SFX",
    "sfx/opening":             "Opening SFX",
    "sfx/big_transitions":     "Big transitions SFX",
    "sfx/small_transitions":   "Small transitions SFX",
    "sfx/particles":           "Particles SFX",
    "sfx/tv_static":           "TV Static SFX",
    "scripts":                 "Scripts",
}

REQUIRED_SCRIPTS: list[str] = [
    "scripts/ar_short.xlsx",
    "scripts/fr_short.xlsx",
    "scripts/en_short.xlsx",
    "scripts/ar_long.xlsx",
    "scripts/fr_long.xlsx",
    "scripts/en_long.xlsx",
]

# Output folders to monitor
MONITORED_OUTPUT_DIRS = ("output", "output_long")


# ═════════════════════════════════════════════════════════════════════════════
# ENUMS & DATA CLASSES
# ═════════════════════════════════════════════════════════════════════════════

class CheckStatus(str, Enum):
    """حالة الفحص."""
    HEALTHY = "healthy"
    WARNING = "warning"
    ERROR   = "error"
    UNKNOWN = "unknown"


@dataclass
class CheckResult:
    """نتيجة فحص واحد."""
    name:     str
    status:   str            = CheckStatus.UNKNOWN.value
    checks:   list[str]      = field(default_factory=list)
    warnings: list[str]      = field(default_factory=list)
    errors:   list[str]      = field(default_factory=list)
    stats:    dict[str, Any] = field(default_factory=dict)

    def finalize_status(self) -> None:
        """تحديد الحالة النهائية."""
        if self.errors:
            self.status = CheckStatus.ERROR.value
        elif self.warnings:
            self.status = CheckStatus.WARNING.value
        else:
            self.status = CheckStatus.HEALTHY.value

    def to_dict(self) -> dict:
        return {
            "name":     self.name,
            "status":   self.status,
            "checks":   self.checks,
            "warnings": self.warnings,
            "errors":   self.errors,
            "stats":    self.stats,
        }


# Status emojis
STATUS_EMOJIS: dict[str, str] = {
    CheckStatus.HEALTHY.value: "✅",
    CheckStatus.WARNING.value: "⚠️",
    CheckStatus.ERROR.value:   "❌",
    CheckStatus.UNKNOWN.value: "❓",
}


# ═════════════════════════════════════════════════════════════════════════════
# CLI
# ═════════════════════════════════════════════════════════════════════════════

CHECK_CHOICES = (
    "all",
    "database",
    "api_keys",
    "tokens",
    "storage",
    "publishing",
    "disk",
)


def parse_args() -> argparse.Namespace:
    """Parse CLI arguments."""
    p = argparse.ArgumentParser(
        description = "🏥 System Health Check",
    )
    p.add_argument(
        "--check",
        type    = str,
        default = "all",
        choices = CHECK_CHOICES,
    )
    p.add_argument(
        "--format",
        type    = str,
        default = "console",
        choices = ["console", "json"],
    )
    p.add_argument(
        "--notify",
        action = "store_true",
    )
    p.add_argument(
        "--no-fail",
        action = "store_true",
    )
    return p.parse_args()


# ═════════════════════════════════════════════════════════════════════════════
# TIME HELPERS (UTC-aware)
# ═════════════════════════════════════════════════════════════════════════════

def _now_utc() -> datetime:
    """UTC datetime aware."""
    return datetime.now(timezone.utc)


def _today_utc_str() -> str:
    """Today's date in UTC."""
    return _now_utc().strftime("%Y-%m-%d")


def _local_hour(lang: str) -> int:
    """Get current hour in language's timezone."""
    if ZoneInfo is None:
        return datetime.now().hour

    tz_name = LANG_TIMEZONES.get(lang, "UTC")
    try:
        return datetime.now(ZoneInfo(tz_name)).hour
    except Exception:
        return datetime.now().hour


# ═════════════════════════════════════════════════════════════════════════════
# CHECK 1: DATABASE
# ═════════════════════════════════════════════════════════════════════════════

def _check_db_size(result: CheckResult) -> bool:
    """فحص حجم DB."""
    if not DB_PATH.exists():
        result.errors.append("DB file not found")
        return False

    size_mb = DB_PATH.stat().st_size / 1_048_576
    result.checks.append(f"Size: {size_mb:.1f} MB")

    if size_mb > DB_MAX_SIZE_MB:
        result.errors.append(
            f"DB too large: {size_mb:.0f} MB "
            f"(max {DB_MAX_SIZE_MB} MB)"
        )
    elif size_mb > DB_WARNING_SIZE_MB:
        result.warnings.append(
            f"DB getting large: {size_mb:.0f} MB"
        )

    return True


def _check_db_stats(result: CheckResult) -> None:
    """فحص إحصائيات DB (try/except per query)."""
    init_db()
    c = _conn()

    # Table count
    try:
        tables = c.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table'"
        ).fetchall()
        result.checks.append(
            f"Tables: {len(tables)}"
        )
    except Exception as e:
        result.warnings.append(
            f"Cannot count tables: {e}"
        )

    # Per-query stats
    queries = [
        ("Used videos",
         "SELECT COUNT(*) FROM used_videos"),
        ("Renders done",
         "SELECT COUNT(*) FROM renders "
         "WHERE status='done'"),
        ("Renders failed",
         "SELECT COUNT(*) FROM renders "
         "WHERE status='failed'"),
        ("AI cached",
         "SELECT COUNT(*) FROM ai_cache"),
        ("Published",
         "SELECT COUNT(*) FROM publish_tracker"),
    ]

    for label, query in queries:
        try:
            count = c.execute(query).fetchone()[0]
            result.checks.append(f"{label}: {count}")

            if (
                label == "Renders failed" and
                count > HIGH_FAILED_RENDERS
            ):
                result.warnings.append(
                    f"High failed renders: {count}"
                )
        except Exception as e:
            result.warnings.append(
                f"Cannot query {label}: "
                f"{str(e)[:80]}"
            )


def check_database() -> CheckResult:
    """فحص صحة قاعدة البيانات."""
    result = CheckResult(name="Database")

    if not _check_db_size(result):
        result.finalize_status()
        return result

    try:
        _check_db_stats(result)
    except Exception as e:
        result.errors.append(
            f"DB read error: {str(e)[:100]}"
        )

    result.finalize_status()
    return result


# ═════════════════════════════════════════════════════════════════════════════
# CHECK 2: API KEYS (keys hidden from URLs)
# ═════════════════════════════════════════════════════════════════════════════

def _count_keys(
    prefix: str,
    max_n:  int = 50,
) -> int:
    """Count available API keys (both naming formats)."""
    count = 0
    seen  : set[str] = set()

    main = os.environ.get(prefix, "").strip()
    if main and main not in seen:
        count += 1
        seen.add(main)

    for i in range(1, max_n + 1):
        # With underscore
        k1 = os.environ.get(
            f"{prefix}_{i}", ""
        ).strip()
        if k1 and k1 not in seen:
            count += 1
            seen.add(k1)

        # Without underscore
        k2 = os.environ.get(
            f"{prefix}{i}", ""
        ).strip()
        if k2 and k2 not in seen:
            count += 1
            seen.add(k2)

    return count


def _test_api(
    url:     str,
    headers: Optional[dict] = None,
    params:  Optional[dict] = None,
) -> bool:
    """Test API endpoint (keys in headers/params, not URL)."""
    try:
        r = requests.get(
            url,
            headers = headers,
            params  = params,
            timeout = API_TEST_TIMEOUT,
        )
        return r.status_code == 200
    except Exception:
        return False


def _test_gemini() -> bool:
    """Test Gemini API (key in header, not URL)."""
    key = os.environ.get(
        "GEMINI_API_KEY", ""
    ).strip()
    if not key:
        return False

    return _test_api(
        url = (
            "https://generativelanguage.googleapis.com"
            "/v1beta/models"
        ),
        headers = {"x-goog-api-key": key},
    )


def _test_groq() -> bool:
    """Test Groq API."""
    key = os.environ.get(
        "GROQ_API_KEY", ""
    ).strip()
    if not key:
        return False

    return _test_api(
        url = (
            "https://api.groq.com/openai/v1/models"
        ),
        headers = {
            "Authorization": f"Bearer {key}"
        },
    )


def _test_pexels() -> bool:
    """Test Pexels API."""
    key = os.environ.get(
        "PEXELS_API_KEY", ""
    ).strip()
    if not key:
        return False

    return _test_api(
        url = (
            "https://api.pexels.com/videos/search"
        ),
        headers = {"Authorization": key},
        params  = {"query": "test", "per_page": 1},
    )


def _test_pixabay() -> bool:
    """Test Pixabay API (key in params)."""
    key = os.environ.get(
        "PIXABAY_API_KEY", ""
    ).strip()
    if not key:
        return False

    return _test_api(
        url = "https://pixabay.com/api/videos/",
        params = {
            "key": key,
            "q": "test",
            "per_page": 3,
        },
    )


def _check_service_keys(
    result:        CheckResult,
    name:          str,
    prefix:        str,
    max_n:         int,
    min_warning:   int,
    is_required:   bool,
    test_function: Callable[[], bool],
) -> int:
    """فحص مفاتيح خدمة معينة."""
    count = _count_keys(prefix, max_n)
    result.checks.append(
        f"{name:8}: {count} keys configured"
    )

    if is_required and count == 0:
        result.errors.append(f"No {name} keys!")
    elif count < min_warning:
        result.warnings.append(
            f"Few {name} keys: {count} "
            f"(recommended: {min_warning}+)"
        )

    if count > 0:
        if test_function():
            result.checks.append(
                f"{name:8}: ✅ working"
            )
        else:
            result.errors.append(
                f"{name} main key invalid!"
            )

    return count


def check_api_keys() -> CheckResult:
    """فحص جميع مفاتيح API."""
    result = CheckResult(name="API Keys")

    log.info("  🧪 Testing API connections...")

    _check_service_keys(
        result, "Gemini", "GEMINI_API_KEY",
        MAX_GEMINI_KEYS, MIN_GEMINI_KEYS,
        False, _test_gemini,
    )
    _check_service_keys(
        result, "Groq", "GROQ_API_KEY",
        MAX_OTHER_KEYS, 2,
        True, _test_groq,
    )
    _check_service_keys(
        result, "Pexels", "PEXELS_API_KEY",
        MAX_OTHER_KEYS, 1,
        True, _test_pexels,
    )
    _check_service_keys(
        result, "Pixabay", "PIXABAY_API_KEY",
        MAX_OTHER_KEYS, 1,
        True, _test_pixabay,
    )

    result.finalize_status()
    return result


# ═════════════════════════════════════════════════════════════════════════════
# CHECK 3: PLATFORM TOKENS (Per-language)
# ═════════════════════════════════════════════════════════════════════════════

def _check_youtube_tokens(
    result: CheckResult,
) -> None:
    """فحص جميع YouTube tokens."""
    log.info("  📺 Checking YouTube tokens...")

    for lang in LANGS:
        yt = check_youtube_token(lang)

        if yt["valid"]:
            result.checks.append(
                f"YouTube {lang.upper()}: ✅ valid"
            )
        else:
            error = yt.get("error", "invalid")
            result.errors.append(
                f"YouTube {lang.upper()}: ❌ {error}"
            )
            notify_token_expired(
                platform = "youtube",
                lang     = lang,
                error    = error,
            )


def _check_facebook_tokens(
    result: CheckResult,
) -> None:
    """فحص Facebook tokens لكل لغة."""
    log.info(
        "  📘 Checking Facebook tokens per-language..."
    )

    for lang in LANGS:
        page_id = (
            os.environ.get(
                f"FB_PAGE_ID_{lang.upper()}", ""
            ).strip()
            or os.environ.get(
                "FB_PAGE_ID", ""
            ).strip()
        )
        token = (
            os.environ.get(
                f"FB_PAGE_TOKEN_{lang.upper()}", ""
            ).strip()
            or os.environ.get(
                "FB_PAGE_TOKEN", ""
            ).strip()
        )

        if not (page_id and token):
            result.warnings.append(
                f"Facebook {lang.upper()}: "
                f"not configured"
            )
            continue

        fb = check_facebook_token(lang)

        if not fb["valid"]:
            error = fb.get("error", "invalid")
            result.errors.append(
                f"Facebook {lang.upper()}: "
                f"❌ {error}"
            )
            notify_token_expired(
                platform = "facebook",
                lang     = lang,
                error    = error,
            )
            continue

        days    = fb.get("days_left", 0)
        expires = fb.get("expires_at", "")

        if days == 999:
            result.checks.append(
                f"Facebook {lang.upper()}: "
                f"✅ permanent"
            )
        elif days <= 14:
            result.warnings.append(
                f"Facebook {lang.upper()} "
                f"expires in {days} days! "
                f"({expires})"
            )
            notify_token_warning(
                platform   = "facebook",
                lang       = lang,
                days_left  = days,
                expires_at = expires,
            )
        else:
            result.checks.append(
                f"Facebook {lang.upper()}: "
                f"✅ valid ({days} days left)"
            )


def check_tokens() -> CheckResult:
    """فحص YouTube و Facebook tokens."""
    result = CheckResult(name="Platform Tokens")

    _check_youtube_tokens(result)
    _check_facebook_tokens(result)

    result.finalize_status()
    return result


# ═════════════════════════════════════════════════════════════════════════════
# CHECK 4: STORAGE
# ═════════════════════════════════════════════════════════════════════════════

def _check_required_dirs(
    result: CheckResult,
) -> None:
    """فحص المجلدات المطلوبة (ignore hidden files)."""
    for dir_path, description in REQUIRED_DIRS.items():
        full_path = BASE_DIR / dir_path

        if not full_path.exists():
            result.errors.append(
                f"Missing: {dir_path} ({description})"
            )
            continue

        # Count non-hidden files
        files = [
            f for f in full_path.glob("*")
            if f.is_file()
            and not f.name.startswith(".")
        ]

        if not files:
            result.warnings.append(
                f"Empty: {dir_path}"
            )
        else:
            result.checks.append(
                f"{dir_path}: {len(files)} files"
            )


def _check_required_scripts(
    result: CheckResult,
) -> None:
    """فحص ملفات السكريبتات."""
    for script in REQUIRED_SCRIPTS:
        full_path = BASE_DIR / script
        if not full_path.exists():
            result.warnings.append(
                f"Script missing: {script}"
            )


def check_storage() -> CheckResult:
    """فحص المجلدات والملفات."""
    result = CheckResult(name="Storage")

    _check_required_dirs(result)
    _check_required_scripts(result)

    result.finalize_status()
    return result


# ═════════════════════════════════════════════════════════════════════════════
# CHECK 5: TODAY'S PUBLISHING (timezone-aware)
# ═════════════════════════════════════════════════════════════════════════════

def _get_today_count(
    lang:  str,
    mode:  str,
    today: str,
) -> int:
    """جلب عدد المنشور اليوم."""
    try:
        c = _conn()
        row = c.execute(
            """SELECT COUNT(*) FROM publish_tracker
               WHERE lang = ?
                 AND content_mode = ?
                 AND date(published_at) = ?""",
            (lang, mode, today),
        ).fetchone()
        return row[0] if row else 0
    except Exception as e:
        log.debug("Today count error: %s", e)
        return 0


def _check_lang_publishing(
    result: CheckResult,
    lang:   str,
    today:  str,
) -> None:
    """فحص نشر لغة واحدة (local timezone hour)."""
    lang_stats = {}

    for mode in MODES:
        lang_stats[mode] = _get_today_count(
            lang, mode, today
        )

    result.stats[lang] = lang_stats

    short_count = lang_stats.get("short", 0)
    long_count  = lang_stats.get("long",  0)

    result.checks.append(
        f"{lang.upper()}: "
        f"{short_count} short + {long_count} long"
    )

    # Warnings based on LOCAL time
    current_hour = _local_hour(lang)

    if current_hour >= 12 and short_count == 0:
        result.warnings.append(
            f"{lang.upper()}: No short videos "
            f"published today!"
        )

    if current_hour >= 23 and long_count == 0:
        result.warnings.append(
            f"{lang.upper()}: No long video "
            f"published today!"
        )


def _check_last_publish(
    result: CheckResult,
) -> None:
    """فحص آخر نشر (timezone-safe)."""
    try:
        c = _conn()
        row = c.execute(
            """SELECT lang, content_mode,
                      platform, published_at
               FROM publish_tracker
               ORDER BY published_at DESC
               LIMIT 1"""
        ).fetchone()
    except Exception as e:
        result.warnings.append(
            f"Cannot query last publish: {e}"
        )
        return

    if not row:
        return

    try:
        ts = row["published_at"]
        # Handle both formats
        ts = (
            ts.replace("Z", "+00:00")
              .replace(" ", "T")
        )
        last_time = datetime.fromisoformat(ts)

        # Make aware if naive
        if last_time.tzinfo is None:
            last_time = last_time.replace(
                tzinfo=timezone.utc
            )

        now_utc   = _now_utc()
        hours_ago = (
            (now_utc - last_time).total_seconds()
            / 3600
        )

        result.checks.append(
            f"Last publish: {hours_ago:.1f}h ago "
            f"({row['lang'].upper()} "
            f"{row['platform']})"
        )

        if hours_ago > PUBLISH_WARNING_HOURS:
            result.warnings.append(
                f"No publish in last "
                f"{hours_ago:.0f} hours!"
            )

    except Exception as e:
        result.warnings.append(
            f"Cannot parse last publish time: {e}"
        )


def check_publishing() -> CheckResult:
    """فحص حالة النشر اليوم."""
    result = CheckResult(name="Today's Publishing")

    try:
        init_db()
        today = _today_utc_str()

        for lang in LANGS:
            _check_lang_publishing(
                result, lang, today
            )

        _check_last_publish(result)

    except Exception as e:
        result.errors.append(
            f"Cannot check publishing: "
            f"{str(e)[:100]}"
        )

    result.finalize_status()
    return result


# ═════════════════════════════════════════════════════════════════════════════
# CHECK 6: DISK USAGE
# ═════════════════════════════════════════════════════════════════════════════

def _bytes_to_gb(size_bytes: int) -> float:
    """Convert bytes to GB."""
    return size_bytes / 1_073_741_824


def _check_disk_space(
    result: CheckResult,
) -> None:
    """فحص مساحة القرص."""
    usage = shutil.disk_usage(BASE_DIR)

    total_gb = _bytes_to_gb(usage.total)
    used_gb  = _bytes_to_gb(usage.used)
    free_gb  = _bytes_to_gb(usage.free)
    percent  = (usage.used / usage.total) * 100

    result.checks.append(
        f"Total: {total_gb:.1f} GB"
    )
    result.checks.append(
        f"Used:  {used_gb:.1f} GB ({percent:.1f}%)"
    )
    result.checks.append(
        f"Free:  {free_gb:.1f} GB"
    )

    if percent > DISK_CRITICAL_PERCENT:
        result.errors.append(
            f"Disk almost full: {percent:.0f}%"
        )
    elif percent > DISK_WARNING_PERCENT:
        result.warnings.append(
            f"Disk usage high: {percent:.0f}%"
        )


def _check_output_folders(
    result: CheckResult,
) -> None:
    """فحص حجم مجلدات الإخراج (log errors)."""
    for folder in MONITORED_OUTPUT_DIRS:
        folder_path = BASE_DIR / folder
        if not folder_path.exists():
            continue

        try:
            size_bytes = sum(
                f.stat().st_size
                for f in folder_path.rglob("*")
                if f.is_file()
            )
            size_gb = _bytes_to_gb(size_bytes)

            result.checks.append(
                f"{folder}/: {size_gb:.2f} GB"
            )

            if size_gb > OUTPUT_WARNING_GB:
                result.warnings.append(
                    f"{folder}/ is large: "
                    f"{size_gb:.1f} GB"
                )
        except Exception as e:
            result.warnings.append(
                f"Cannot check {folder}/: "
                f"{str(e)[:80]}"
            )


def check_disk() -> CheckResult:
    """فحص مساحة القرص."""
    result = CheckResult(name="Disk Usage")

    try:
        _check_disk_space(result)
        _check_output_folders(result)
    except Exception as e:
        result.errors.append(
            f"Cannot check disk: {str(e)[:100]}"
        )

    result.finalize_status()
    return result


# ═════════════════════════════════════════════════════════════════════════════
# REPORT BUILDERS
# ═════════════════════════════════════════════════════════════════════════════

def _get_status_emoji(status: str) -> str:
    """جلب emoji حسب الحالة."""
    return STATUS_EMOJIS.get(status, "❓")


def _count_by_status(
    results: list[CheckResult],
    status:  str,
) -> int:
    """عد النتائج حسب الحالة."""
    return sum(
        1 for r in results
        if r.status == status
    )


def build_console_report(
    results: list[CheckResult],
) -> str:
    """بناء تقرير console."""
    lines     = []
    separator = "═" * SUMMARY_WIDTH
    sub_sep   = "─" * SECTION_WIDTH
    now       = _now_utc().strftime(
        "%Y-%m-%d %H:%M:%S UTC"
    )

    lines.append(f"\n{separator}")
    lines.append(
        "  🏥 System Health Check Report"
    )
    lines.append(f"  📅 {now}")
    lines.append(separator)

    for r in results:
        emoji = _get_status_emoji(r.status)
        lines.append(f"\n  {emoji} {r.name}")
        lines.append(f"  {sub_sep}")

        for check in r.checks:
            lines.append(f"     {check}")

        if r.warnings:
            lines.append("")
            for w in r.warnings:
                lines.append(f"     ⚠️  {w}")

        if r.errors:
            lines.append("")
            for e in r.errors:
                lines.append(f"     ❌ {e}")

    # Summary
    total   = len(results)
    healthy = _count_by_status(
        results, CheckStatus.HEALTHY.value
    )
    warns   = _count_by_status(
        results, CheckStatus.WARNING.value
    )
    errs    = _count_by_status(
        results, CheckStatus.ERROR.value
    )

    lines.append(f"\n{separator}")
    lines.append("  📊 Summary")
    lines.append(
        f"     ✅ Healthy : {healthy}/{total}"
    )
    lines.append(f"     ⚠️  Warnings: {warns}")
    lines.append(f"     ❌ Errors  : {errs}")
    lines.append(f"{separator}\n")

    return "\n".join(lines)


def build_whatsapp_report(
    results: list[CheckResult],
) -> str:
    """بناء تقرير WhatsApp مختصر."""
    now   = _now_utc().strftime("%Y-%m-%d %H:%M UTC")
    lines = [f"🏥 Health Check — {now}\n"]

    for r in results:
        emoji = _get_status_emoji(r.status)
        lines.append(f"{emoji} {r.name}")

        if r.errors:
            for e in r.errors[:3]:
                lines.append(f"  ❌ {e}")
        elif r.warnings:
            for w in r.warnings[:2]:
                lines.append(f"  ⚠️ {w}")

        lines.append("")

    healthy = _count_by_status(
        results, CheckStatus.HEALTHY.value
    )
    warns   = _count_by_status(
        results, CheckStatus.WARNING.value
    )
    errs    = _count_by_status(
        results, CheckStatus.ERROR.value
    )

    lines.append("📊 Summary:")
    lines.append(f"✅ {healthy} healthy")
    if warns:
        lines.append(f"⚠️ {warns} warnings")
    if errs:
        lines.append(f"❌ {errs} errors")

    return "\n".join(lines)


# ═════════════════════════════════════════════════════════════════════════════
# CHECK ORCHESTRATOR (crash-safe per check)
# ═════════════════════════════════════════════════════════════════════════════

CHECK_FUNCTIONS: dict[
    str,
    tuple[str, Callable[[], CheckResult]],
] = {
    "database":   (
        "🗄️  Checking database...",
        check_database,
    ),
    "api_keys":   (
        "🔑 Checking API keys...",
        check_api_keys,
    ),
    "tokens":     (
        "🎫 Checking platform tokens...",
        check_tokens,
    ),
    "storage":    (
        "📂 Checking storage...",
        check_storage,
    ),
    "publishing": (
        "📤 Checking publishing status...",
        check_publishing,
    ),
    "disk":       (
        "💾 Checking disk usage...",
        check_disk,
    ),
}


def run_checks(
    check_filter: str = "all",
) -> list[CheckResult]:
    """تشغيل الفحوصات (crash-safe per check)."""
    results: list[CheckResult] = []

    for check_name, (label, func) in (
        CHECK_FUNCTIONS.items()
    ):
        if check_filter not in ("all", check_name):
            continue

        log.info("\n  %s", label)

        try:
            results.append(func())
        except Exception as e:
            # Catch crash per check
            error_result = CheckResult(
                name = (
                    check_name
                    .replace("_", " ")
                    .title()
                )
            )
            error_result.errors.append(
                f"Check crashed: {str(e)[:100]}"
            )
            error_result.finalize_status()
            results.append(error_result)
            log.error(
                "  ❌ Check '%s' crashed: %s",
                check_name, e
            )

    return results


# ═════════════════════════════════════════════════════════════════════════════
# NOTIFICATION
# ═════════════════════════════════════════════════════════════════════════════

def _send_notification(
    results: list[CheckResult],
) -> None:
    """إرسال التقرير عبر WhatsApp."""
    whatsapp_report = build_whatsapp_report(results)

    has_errors = any(
        r.status == CheckStatus.ERROR.value
        for r in results
    )
    has_warnings = any(
        r.status == CheckStatus.WARNING.value
        for r in results
    )

    if has_errors:
        notify_error(
            whatsapp_report, skip_rate=True
        )
    elif has_warnings:
        notify_warning(
            whatsapp_report, skip_rate=True
        )
    else:
        notify_info(
            whatsapp_report, skip_rate=True
        )


# ═════════════════════════════════════════════════════════════════════════════
# MAIN
# ═════════════════════════════════════════════════════════════════════════════

def main() -> None:
    """نقطة الدخول الرئيسية."""
    # Logging — entry point only
    logging.basicConfig(
        level   = logging.INFO,
        format  = (
            "%(asctime)s | %(levelname)-8s | "
            "%(message)s"
        ),
        datefmt = "%H:%M:%S",
    )

    args = parse_args()

    log.info(
        "\n  🏥 Running Health Check v2.0..."
    )

    # Run checks
    results = run_checks(args.check)

    # Display report
    if args.format == "json":
        results_dict = [
            r.to_dict() for r in results
        ]
        print(
            json.dumps(
                results_dict,
                indent  = 2,
                default = str,
            )
        )
    else:
        print(build_console_report(results))

    # Send notification
    if args.notify:
        _send_notification(results)

    # Exit code
    if not args.no_fail:
        has_errors = any(
            r.status == CheckStatus.ERROR.value
            for r in results
        )
        if has_errors:
            sys.exit(1)


if __name__ == "__main__":
    main()

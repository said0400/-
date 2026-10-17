"""
📱 Notification System

Channels (priority order):
  1. WhatsApp via Green-API (primary)
  2. Telegram Bot (backup)
  3. Console (always)
  4. Log file (always)

Features:
  ✅ Multi-channel delivery
  ✅ Rate limiting (5 min per identical message)
  ✅ Auto-cleanup of old rate limits
  ✅ Rich notification templates
  ✅ HTML support for Telegram
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Optional

import requests

# ═════════════════════════════════════════════════════════════════════════════
# CONSTANTS
# ═════════════════════════════════════════════════════════════════════════════

# Paths
BASE_DIR        = Path(__file__).parent.resolve()
NOTIFY_LOG      = BASE_DIR / "notifications.log"
RATE_LIMIT_FILE = BASE_DIR / ".notify_rate_limit.json"

# Rate limiting
RATE_LIMIT_SECONDS  = 300       # 5 دقائق
CLEANUP_MULTIPLIER  = 2         # cleanup حدوث = 10 دقائق
MESSAGE_HASH_LENGTH = 200       # طول النص للـ hash

# Timeouts
WHATSAPP_TIMEOUT = 15
TELEGRAM_TIMEOUT = 15

# API URLs
GREEN_API_BASE = "https://api.green-api.com"
TELEGRAM_API_BASE = "https://api.telegram.org"

# Logging
logging.basicConfig(
    level  = logging.INFO,
    format = "%(message)s",
)
log = logging.getLogger(__name__)


# ═════════════════════════════════════════════════════════════════════════════
# ENUMS & DATA CLASSES
# ═════════════════════════════════════════════════════════════════════════════

class NotificationLevel(str, Enum):
    """مستويات الإشعارات."""
    SUCCESS = "success"
    WARNING = "warning"
    ERROR   = "error"
    INFO    = "info"


# Emojis لكل مستوى
LEVEL_EMOJIS: dict[str, str] = {
    NotificationLevel.SUCCESS: "✅",
    NotificationLevel.WARNING: "⚠️",
    NotificationLevel.ERROR:   "❌",
    NotificationLevel.INFO:    "ℹ️",
}

# Emojis للمنصات واللغات
LANG_FLAGS: dict[str, str] = {
    "ar": "🇸🇦",
    "fr": "🇫🇷",
    "en": "🇺🇸",
}

PLATFORM_EMOJIS: dict[str, str] = {
    "facebook": "📘",
    "youtube":  "📺",
}

MODE_EMOJIS: dict[str, str] = {
    "short": "⚡",
    "long":  "🎬",
}


@dataclass
class WhatsAppCreds:
    """WhatsApp Green-API credentials."""
    instance_id: str
    api_token:   str
    phone:       str

    def is_valid(self) -> bool:
        return bool(self.instance_id and self.api_token)


@dataclass
class TelegramCreds:
    """Telegram Bot credentials."""
    bot_token: str
    chat_id:   str

    def is_valid(self) -> bool:
        return bool(self.bot_token and self.chat_id)


# ═════════════════════════════════════════════════════════════════════════════
# HELPERS
# ═════════════════════════════════════════════════════════════════════════════

def _get_lang_flag(lang: str) -> str:
    """جلب علم اللغة."""
    return LANG_FLAGS.get(lang, "🌐")


def _get_platform_emoji(platform: str) -> str:
    """جلب emoji المنصة."""
    return PLATFORM_EMOJIS.get(platform, "📤")


def _get_mode_emoji(mode: str) -> str:
    """جلب emoji نوع المحتوى."""
    return MODE_EMOJIS.get(mode, "📹")


# ═════════════════════════════════════════════════════════════════════════════
# RATE LIMITING
# ═════════════════════════════════════════════════════════════════════════════

def _hash_message(message: str) -> str:
    """
    بناء hash للرسالة لمنع التكرار.

    يستخدم SHA-256 لـ deterministic hash.
    """
    text = message[:MESSAGE_HASH_LENGTH]
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _load_rate_limits() -> dict:
    """تحميل rate limits من الملف."""
    if not RATE_LIMIT_FILE.exists():
        return {}

    try:
        return json.loads(
            RATE_LIMIT_FILE.read_text(encoding="utf-8")
        )
    except Exception:
        return {}


def _save_rate_limits(data: dict) -> None:
    """حفظ rate limits في الملف."""
    try:
        RATE_LIMIT_FILE.write_text(
            json.dumps(data, indent=2),
            encoding = "utf-8",
        )
    except Exception:
        pass


def _cleanup_old_limits(
    limits: dict,
    now:    float,
) -> dict:
    """تنظيف الإدخالات القديمة."""
    cutoff = RATE_LIMIT_SECONDS * CLEANUP_MULTIPLIER
    return {
        k: v
        for k, v in limits.items()
        if (now - v) < cutoff
    }


def _is_rate_limited(message_hash: str) -> bool:
    """
    التحقق إذا كانت الرسالة أُرسلت مؤخراً.

    Side effects:
        - يحدث الـ rate limits file
        - ينظف الإدخالات القديمة
    """
    limits    = _load_rate_limits()
    last_sent = limits.get(message_hash, 0)
    now       = time.time()

    # تحقق من الـ rate limit
    if (now - last_sent) < RATE_LIMIT_SECONDS:
        return True

    # تحديث + تنظيف
    cleaned = _cleanup_old_limits(limits, now)
    cleaned[message_hash] = now
    _save_rate_limits(cleaned)

    return False


# ═════════════════════════════════════════════════════════════════════════════
# WHATSAPP (Green-API)
# ═════════════════════════════════════════════════════════════════════════════

def _get_whatsapp_creds() -> WhatsAppCreds:
    """جلب WhatsApp credentials من البيئة."""
    return WhatsAppCreds(
        instance_id = os.environ.get(
            "GREEN_API_INSTANCE_ID", ""
        ).strip(),
        api_token = os.environ.get(
            "GREEN_API_TOKEN", ""
        ).strip(),
        phone = os.environ.get(
            "WHATSAPP_PHONE_NUMBER", ""
        ).strip(),
    )


def _build_whatsapp_chat_id(phone: str) -> str:
    """
    بناء chat ID لـ Green-API.

    Format: 212786850913@c.us
    """
    return f"{phone}@c.us"


def _build_whatsapp_url(
    instance_id: str,
    api_token:   str,
) -> str:
    """بناء URL لـ Green-API."""
    return (
        f"{GREEN_API_BASE}/waInstance{instance_id}"
        f"/sendMessage/{api_token}"
    )


def send_whatsapp(
    message: str,
    phone:   str = "",
) -> bool:
    """
    إرسال رسالة WhatsApp عبر Green-API.

    Args:
        message: نص الرسالة
        phone:   رقم الهاتف (بدون +، مع كود الدولة)
                 مثال: "212786850913"
                 إذا فارغ، يستخدم WHATSAPP_PHONE_NUMBER

    Returns:
        True إذا نجح الإرسال
    """
    creds = _get_whatsapp_creds()

    if not creds.is_valid():
        return False

    # الهاتف من parameter أو من env
    target_phone = phone or creds.phone
    if not target_phone:
        log.warning("  ⚠️  WhatsApp: No phone number set")
        return False

    chat_id = _build_whatsapp_chat_id(target_phone)
    url     = _build_whatsapp_url(
        creds.instance_id, creds.api_token,
    )

    try:
        r = requests.post(
            url,
            json = {
                "chatId":  chat_id,
                "message": message,
            },
            timeout = WHATSAPP_TIMEOUT,
        )

        if r.status_code == 200:
            return True

        log.warning(
            f"  ⚠️  WhatsApp failed: "
            f"{r.status_code} — {r.text[:100]}"
        )
        return False

    except requests.exceptions.Timeout:
        log.warning("  ⚠️  WhatsApp timeout")
        return False

    except Exception as e:
        log.warning(f"  ⚠️  WhatsApp error: {e}")
        return False


# ═════════════════════════════════════════════════════════════════════════════
# TELEGRAM
# ═════════════════════════════════════════════════════════════════════════════

def _get_telegram_creds() -> TelegramCreds:
    """جلب Telegram credentials من البيئة."""
    return TelegramCreds(
        bot_token = os.environ.get(
            "TELEGRAM_BOT_TOKEN", ""
        ).strip(),
        chat_id = os.environ.get(
            "TELEGRAM_CHAT_ID", ""
        ).strip(),
    )


def _build_telegram_url(bot_token: str) -> str:
    """بناء URL لـ Telegram Bot API."""
    return f"{TELEGRAM_API_BASE}/bot{bot_token}/sendMessage"


def send_telegram(message: str) -> bool:
    """
    إرسال رسالة Telegram.

    Returns:
        True إذا نجح الإرسال
    """
    creds = _get_telegram_creds()

    if not creds.is_valid():
        return False

    url = _build_telegram_url(creds.bot_token)

    try:
        r = requests.post(
            url,
            json = {
                "chat_id":    creds.chat_id,
                "text":       message,
                "parse_mode": "HTML",
            },
            timeout = TELEGRAM_TIMEOUT,
        )

        return r.status_code == 200

    except Exception:
        return False


# ═════════════════════════════════════════════════════════════════════════════
# LOG FILE
# ═════════════════════════════════════════════════════════════════════════════

def _log_to_file(message: str, level: str) -> None:
    """حفظ الإشعارات في ملف log."""
    try:
        timestamp = datetime.now().strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        log_line = (
            f"[{timestamp}] [{level.upper()}] {message}\n"
        )

        NOTIFY_LOG.parent.mkdir(
            parents = True, exist_ok = True,
        )

        with open(NOTIFY_LOG, "a", encoding="utf-8") as f:
            f.write(log_line)

    except Exception:
        # silent fail (لا نريد crash بسبب logging)
        pass


# ═════════════════════════════════════════════════════════════════════════════
# MAIN NOTIFY FUNCTION
# ═════════════════════════════════════════════════════════════════════════════

def _format_message(message: str, level: str) -> str:
    """تنسيق الرسالة مع emoji وtimestamp."""
    emoji     = LEVEL_EMOJIS.get(level, "ℹ️")
    timestamp = datetime.now().strftime("%H:%M:%S")
    return f"{emoji} [{timestamp}] {message}"


def notify(
    message:   str,
    level:     str  = "info",
    skip_rate: bool = False,
    silent:    bool = False,
) -> bool:
    """
    إرسال إشعار عبر كل القنوات المتاحة.

    Args:
        message:   نص الرسالة
        level:     success | warning | error | info
        skip_rate: تجاوز rate limiting
        silent:    لا تطبع في console

    Returns:
        True إذا أُرسلت على الأقل عبر قناة واحدة
    """
    if not message or not message.strip():
        return False

    formatted = _format_message(message, level)

    # Console (دائمًا)
    if not silent:
        print(f"\n  {formatted}")

    # Log to file (دائمًا)
    _log_to_file(message, level)

    # Rate limiting
    if not skip_rate:
        msg_hash = _hash_message(message)
        if _is_rate_limited(msg_hash):
            if not silent:
                print("  ⏭️  Skipped (rate limited)")
            return False

    # إرسال عبر القنوات
    sent_count = 0

    if send_whatsapp(formatted):
        sent_count += 1

    if send_telegram(formatted):
        sent_count += 1

    return sent_count > 0


# ═════════════════════════════════════════════════════════════════════════════
# CONVENIENCE FUNCTIONS
# ═════════════════════════════════════════════════════════════════════════════

def notify_success(message: str, **kwargs) -> bool:
    """إشعار نجاح."""
    return notify(message, level="success", **kwargs)


def notify_warning(message: str, **kwargs) -> bool:
    """إشعار تحذير."""
    return notify(message, level="warning", **kwargs)


def notify_error(message: str, **kwargs) -> bool:
    """إشعار خطأ."""
    return notify(message, level="error", **kwargs)


def notify_info(message: str, **kwargs) -> bool:
    """إشعار معلومات."""
    return notify(message, level="info", **kwargs)


# ═════════════════════════════════════════════════════════════════════════════
# RICH NOTIFICATIONS
# ═════════════════════════════════════════════════════════════════════════════

def notify_video_published(
    video_number: str,
    lang:         str,
    content_mode: str,
    platform:     str,
    title:        str = "",
) -> bool:
    """إشعار نشر فيديو ناجح."""
    lang_flag      = _get_lang_flag(lang)
    platform_emoji = _get_platform_emoji(platform)
    mode_emoji     = _get_mode_emoji(content_mode)

    message = (
        f"{mode_emoji} Video #{video_number} published!\n"
        f"{lang_flag} {lang.upper()} | "
        f"{platform_emoji} {platform.title()}\n"
    )

    if title:
        message += f"\n📌 {title[:60]}"

    return notify_success(message, skip_rate=True)


def notify_video_failed(
    video_number: str,
    lang:         str,
    content_mode: str,
    error:        str,
    platform:     str = "",
) -> bool:
    """إشعار فشل نشر فيديو."""
    lang_flag = _get_lang_flag(lang)

    message = (
        f"❌ Video #{video_number} FAILED!\n"
        f"{lang_flag} {lang.upper()} "
        f"[{content_mode.upper()}]\n"
    )

    if platform:
        message += f"📤 Platform: {platform}\n"

    message += f"\n💬 Error: {error[:200]}"

    return notify_error(message, skip_rate=True)


def notify_token_warning(
    platform:   str,
    lang:       str,
    days_left:  int,
    expires_at: str = "",
) -> bool:
    """تحذير اقتراب انتهاء التوكن."""
    lang_flag      = _get_lang_flag(lang)
    platform_emoji = _get_platform_emoji(platform)

    message = (
        f"⚠️  TOKEN WARNING!\n"
        f"{platform_emoji} {platform.title()} "
        f"({lang_flag} {lang.upper()})\n"
        f"⏰ Expires in {days_left} days"
    )

    if expires_at:
        message += f"\n📅 {expires_at}"

    message += "\n\n🔄 Please renew the token soon!"

    return notify_warning(message, skip_rate=True)


def notify_token_expired(
    platform: str,
    lang:     str,
    error:    str = "",
) -> bool:
    """التوكن انتهى نهائياً."""
    lang_flag      = _get_lang_flag(lang)
    platform_emoji = _get_platform_emoji(platform)

    message = (
        f"🚨 TOKEN EXPIRED!\n"
        f"{platform_emoji} {platform.title()} "
        f"({lang_flag} {lang.upper()})\n"
    )

    if error:
        message += f"\n💬 {error[:150]}"

    message += (
        "\n\n❗ Publishing is STOPPED for this account!"
        "\n🔧 Please renew immediately!"
    )

    return notify_error(message, skip_rate=True)


def notify_daily_summary(stats: dict) -> bool:
    """
    ملخص يومي للنشر.

    Args:
        stats: {
            "ar": {"short": 5, "long": 1},
            "fr": {"short": 5, "long": 1},
            "en": {"short": 5, "long": 1},
        }
    """
    today = datetime.now().strftime("%Y-%m-%d")
    message = f"📊 Daily Summary — {today}\n\n"

    for lang in ("ar", "fr", "en"):
        lang_flag   = _get_lang_flag(lang)
        lang_stats  = stats.get(lang, {})
        short_count = lang_stats.get("short", 0)
        long_count  = lang_stats.get("long",  0)

        message += (
            f"{lang_flag} {lang.upper()}: "
            f"{short_count} short + "
            f"{long_count} long\n"
        )

    total = sum(
        s.get("short", 0) + s.get("long", 0)
        for s in stats.values()
    )

    message += f"\n📈 Total: {total} videos"

    return notify_info(message, skip_rate=True)


def notify_workflow_start(
    lang:         str,
    content_mode: str,
) -> bool:
    """إشعار بدء workflow."""
    lang_flag  = _get_lang_flag(lang)
    mode_emoji = _get_mode_emoji(content_mode)

    message = (
        f"🚀 Workflow started\n"
        f"{lang_flag} {lang.upper()} "
        f"{mode_emoji} [{content_mode.upper()}]"
    )

    return notify_info(message, silent=True)


def notify_workflow_complete(
    lang:         str,
    content_mode: str,
    success:      int,
    failed:       int,
) -> bool:
    """إشعار انتهاء workflow."""
    lang_flag  = _get_lang_flag(lang)
    mode_emoji = _get_mode_emoji(content_mode)
    status     = "✅" if failed == 0 else "⚠️"

    message = (
        f"{status} Workflow complete\n"
        f"{lang_flag} {lang.upper()} "
        f"{mode_emoji} [{content_mode.upper()}]\n\n"
        f"✅ Success: {success}\n"
        f"❌ Failed:  {failed}"
    )

    return notify_info(message, skip_rate=True)


# ═════════════════════════════════════════════════════════════════════════════
# TEST FUNCTION
# ═════════════════════════════════════════════════════════════════════════════

def _print_creds_status(
    label:   str,
    creds:   list[tuple[str, bool]],
) -> None:
    """طباعة حالة credentials."""
    print(f"\n  {label}")
    for name, has_value in creds:
        status = "✅" if has_value else "❌"
        print(f"     {name:10}: {status}")


def test_notifications() -> None:
    """اختبار جميع قنوات الإشعار."""
    print("\n" + "═" * 55)
    print("  🧪 Testing Notifications")
    print("═" * 55)

    # WhatsApp
    wa_creds = _get_whatsapp_creds()
    _print_creds_status(
        "📱 WhatsApp:",
        [
            ("Instance", bool(wa_creds.instance_id)),
            ("Token",    bool(wa_creds.api_token)),
            ("Phone",    bool(wa_creds.phone)),
        ],
    )

    # Telegram
    tg_creds = _get_telegram_creds()
    _print_creds_status(
        "📨 Telegram:",
        [
            ("Bot Token", bool(tg_creds.bot_token)),
            ("Chat ID",   bool(tg_creds.chat_id)),
        ],
    )

    print("\n  📤 Sending test messages...")

    notify_success(
        "Test notification — System is working! 🎉",
        skip_rate = True,
    )
    notify_warning(
        "This is a test warning",
        skip_rate = True,
    )
    notify_error(
        "This is a test error",
        skip_rate = True,
    )

    print("\n  ✅ Test complete!")
    print("═" * 55 + "\n")


if __name__ == "__main__":
    test_notifications()

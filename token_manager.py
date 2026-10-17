"""
🔑 Token Manager v2.0 — Auto Token Validation & Refresh

Features:
  ✅ YouTube token validation + auto refresh
  ✅ Facebook token validation per-language
  ✅ Facebook debug_token uses APP_ID|APP_SECRET (correct)
  ✅ Facebook token extension (Short → Long-lived)
  ✅ 3 languages support (AR, FR, EN)
  ✅ Negative days_left → token expired (valid=False)
  ✅ Better JSON error handling
  ✅ Configurable Graph API version
  ✅ WARNING_DAYS_FB = 14 (safer than 7)
  ✅ Warnings logged clearly in check_all_tokens
  ✅ Per-language Facebook credentials
  ✅ Notifications via WhatsApp (Green-API)
"""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import requests

log = logging.getLogger(__name__)

# ═════════════════════════════════════════════════════════════════════════════
# CONSTANTS
# ═════════════════════════════════════════════════════════════════════════════

# API URLs
YOUTUBE_TOKEN_URL = "https://oauth2.googleapis.com/token"

# Configurable Graph API version
GRAPH_API_VERSION = os.environ.get("FB_API_VERSION", "v21.0")
GRAPH_API         = f"https://graph.facebook.com/{GRAPH_API_VERSION}"

# Languages
LANGS = ("ar", "fr", "en")

# Warnings
WARNING_DAYS_FB = 14   # تحذير قبل 14 يوماً من انتهاء FB token

# Timeouts
TOKEN_TIMEOUT = 30     # لعمليات التجديد الثقيلة
CHECK_TIMEOUT = 15     # للفحص السريع


# ═════════════════════════════════════════════════════════════════════════════
# DATA CLASSES
# ═════════════════════════════════════════════════════════════════════════════

@dataclass
class YouTubeTokenResult:
    """نتيجة فحص YouTube token."""
    valid:      bool = False
    lang:       str  = ""
    expires_in: int  = 0
    error:      str  = ""

    def to_dict(self) -> dict:
        return {
            "valid":      self.valid,
            "lang":       self.lang,
            "expires_in": self.expires_in,
            "error":      self.error,
        }


@dataclass
class FacebookTokenResult:
    """نتيجة فحص Facebook token."""
    valid:      bool = False
    lang:       str  = ""
    expires_at: str  = ""
    days_left:  int  = 0
    is_warning: bool = False
    error:      str  = ""

    def to_dict(self) -> dict:
        return {
            "valid":      self.valid,
            "lang":       self.lang,
            "expires_at": self.expires_at,
            "days_left":  self.days_left,
            "is_warning": self.is_warning,
            "error":      self.error,
        }


@dataclass
class CheckAllResult:
    """نتيجة فحص جميع التوكنات."""
    youtube:  dict[str, dict] = field(default_factory=dict)
    facebook: dict[str, dict] = field(default_factory=dict)
    warnings: list[str]       = field(default_factory=list)
    errors:   list[str]       = field(default_factory=list)

    def count_valid(self) -> int:
        count = 0
        for platform in (self.youtube, self.facebook):
            for lang_data in platform.values():
                if lang_data.get("valid"):
                    count += 1
        return count

    def has_errors(self) -> bool:
        return len(self.errors) > 0

    def has_warnings(self) -> bool:
        return len(self.warnings) > 0


# ═════════════════════════════════════════════════════════════════════════════
# HELPERS
# ═════════════════════════════════════════════════════════════════════════════

def _get_env(key_with_lang: str, key_generic: str) -> str:
    """يقرأ متغير من البيئة مع fallback."""
    value = os.environ.get(key_with_lang, "").strip()
    if value:
        return value
    return os.environ.get(key_generic, "").strip()


def _safe_str(value, max_len: int = 200) -> str:
    """تحويل آمن لـ string مع تقصير."""
    s = str(value)
    return s[:max_len] if len(s) > max_len else s


def _safe_json_parse(
    response: requests.Response,
) -> tuple[Optional[dict], Optional[str]]:
    """
    Parse JSON response آمن.
    
    Returns:
        (data, error_message)
    """
    try:
        data = response.json()
        return data, None
    except ValueError:
        return None, (
            f"HTTP {response.status_code}: Non-JSON response"
        )


# ═════════════════════════════════════════════════════════════════════════════
# YOUTUBE TOKEN MANAGER
# ═════════════════════════════════════════════════════════════════════════════

def _get_yt_creds(lang: str) -> tuple[str, str, str]:
    """
    يقرأ YouTube credentials من البيئة.

    Returns:
        (client_id, client_secret, refresh_token)
    """
    lang_upper = lang.upper()

    client_id = _get_env(
        f"YOUTUBE_CLIENT_ID_{lang_upper}",
        "YOUTUBE_CLIENT_ID",
    )
    client_secret = _get_env(
        f"YOUTUBE_CLIENT_SECRET_{lang_upper}",
        "YOUTUBE_CLIENT_SECRET",
    )
    refresh_token = _get_env(
        f"YOUTUBE_REFRESH_TOKEN_{lang_upper}",
        "YOUTUBE_REFRESH_TOKEN",
    )

    return client_id, client_secret, refresh_token


def _request_youtube_token(
    client_id:     str,
    client_secret: str,
    refresh_token: str,
) -> tuple[Optional[dict], Optional[str]]:
    """
    طلب access_token من YouTube.

    Returns:
        (data, error) — data أو error واحد فقط
    """
    try:
        r = requests.post(
            YOUTUBE_TOKEN_URL,
            data = {
                "client_id":     client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
                "grant_type":    "refresh_token",
            },
            timeout = TOKEN_TIMEOUT,
        )

        # Safe JSON parse
        data, parse_error = _safe_json_parse(r)
        if parse_error:
            return None, parse_error

        if r.status_code != 200:
            error = data.get("error", "unknown") if data else "unknown"
            desc  = data.get("error_description", "") if data else ""
            return None, (
                f"HTTP {r.status_code} — {error}: {desc}"
            )

        return data, None

    except requests.exceptions.Timeout:
        return None, "Request timeout"

    except requests.exceptions.RequestException as e:
        return None, _safe_str(e)

    except Exception as e:
        return None, _safe_str(e)


def check_youtube_token(lang: str) -> dict:
    """
    يتحقق من صلاحية YouTube token.

    Args:
        lang: ar | fr | en

    Returns:
        dict مع: valid, lang, expires_in, error
    """
    result = YouTubeTokenResult(lang=lang)

    client_id, client_secret, refresh_token = _get_yt_creds(lang)

    # التحقق من credentials
    if not all([client_id, client_secret, refresh_token]):
        result.error = "Missing credentials"
        return result.to_dict()

    # طلب token
    data, error = _request_youtube_token(
        client_id, client_secret, refresh_token
    )

    if error:
        result.error = error
        return result.to_dict()

    if not data:
        result.error = "Empty response"
        return result.to_dict()

    access_token = data.get("access_token", "")
    expires_in   = int(data.get("expires_in", 0))

    if not access_token:
        result.error = "No access_token in response"
        return result.to_dict()

    result.valid      = True
    result.expires_in = expires_in

    log.info(
        "  ✅ YouTube (%s): token valid (expires in %ds)",
        lang.upper(), expires_in
    )

    return result.to_dict()


def refresh_youtube_token(lang: str) -> dict:
    """
    يجدد YouTube access_token.

    Returns:
        dict مع: success, access_token, expires_in, error
    """
    result = {
        "success":      False,
        "access_token": "",
        "expires_in":   0,
        "error":        "",
    }

    client_id, client_secret, refresh_token = _get_yt_creds(lang)

    if not all([client_id, client_secret, refresh_token]):
        result["error"] = (
            f"Missing YouTube credentials for {lang.upper()}"
        )
        return result

    data, error = _request_youtube_token(
        client_id, client_secret, refresh_token
    )

    if error:
        result["error"] = error
        return result

    if not data:
        result["error"] = "Empty response"
        return result

    access_token = data.get("access_token", "")
    expires_in   = int(data.get("expires_in", 0))

    if not access_token:
        result["error"] = "Empty access_token"
        return result

    if expires_in == 0:
        log.warning(
            "  ⚠️  YouTube (%s): expires_in not returned",
            lang.upper()
        )

    result["success"]      = True
    result["access_token"] = access_token
    result["expires_in"]   = expires_in

    log.info(
        "  ✅ YouTube (%s): token refreshed (valid for %ds)",
        lang.upper(), expires_in
    )

    return result


# ═════════════════════════════════════════════════════════════════════════════
# FACEBOOK TOKEN MANAGER (Per-language)
# ═════════════════════════════════════════════════════════════════════════════

def _get_fb_creds(lang: str = "") -> tuple[str, str]:
    """
    يقرأ Facebook credentials per-language من البيئة.

    Supports:
        FB_PAGE_ID_AR, FB_PAGE_TOKEN_AR
        FB_PAGE_ID_FR, FB_PAGE_TOKEN_FR
        FB_PAGE_ID_EN, FB_PAGE_TOKEN_EN
    Fallback: FB_PAGE_ID / FB_PAGE_TOKEN

    Returns:
        (page_id, token)
    """
    lu = lang.upper().strip() if lang else ""

    if lu and lu in ("AR", "FR", "EN"):
        page_id = (
            os.environ.get(f"FB_PAGE_ID_{lu}", "").strip()
            or os.environ.get("FB_PAGE_ID", "").strip()
        )
        token = (
            os.environ.get(f"FB_PAGE_TOKEN_{lu}", "").strip()
            or os.environ.get("FB_PAGE_TOKEN", "").strip()
        )
    else:
        page_id = os.environ.get("FB_PAGE_ID", "").strip()
        token   = os.environ.get("FB_PAGE_TOKEN", "").strip()

    return page_id, token


def _get_fb_app_credentials() -> tuple[str, str]:
    """
    يقرأ Facebook App credentials.

    Returns:
        (app_id, app_secret)
    """
    app_id     = os.environ.get("FB_APP_ID", "").strip()
    app_secret = os.environ.get("FB_APP_SECRET", "").strip()
    return app_id, app_secret


def _debug_facebook_token(
    token: str,
) -> tuple[Optional[dict], Optional[str]]:
    """
    استدعاء Facebook debug_token API.

    Uses APP_ID|APP_SECRET as access_token (correct method).
    Falls back to same token if App credentials not available.

    Returns:
        (data, error)
    """
    # Build correct app_token
    app_id, app_secret = _get_fb_app_credentials()

    if app_id and app_secret:
        # Correct: APP_ID|APP_SECRET
        app_token = f"{app_id}|{app_secret}"
    else:
        # Fallback: use same token (less accurate)
        log.warning(
            "  ⚠️  FB_APP_ID/FB_APP_SECRET missing — "
            "debug_token may be inaccurate"
        )
        app_token = token

    try:
        r = requests.get(
            f"{GRAPH_API}/debug_token",
            params = {
                "input_token":  token,
                "access_token": app_token,
            },
            timeout = CHECK_TIMEOUT,
        )

        if r.status_code != 200:
            return None, f"HTTP {r.status_code}"

        # Safe JSON parse
        json_data, parse_error = _safe_json_parse(r)
        if parse_error:
            return None, parse_error

        data = json_data.get("data") if json_data else None

        # Validate data type
        if not data or not isinstance(data, dict):
            return None, "Invalid token response structure"

        return data, None

    except requests.exceptions.Timeout:
        return None, "Request timeout"

    except requests.exceptions.RequestException as e:
        return None, _safe_str(e)

    except Exception as e:
        return None, _safe_str(e)


def check_facebook_token(lang: str = "") -> dict:
    """
    يتحقق من صلاحية Facebook token per-language.

    Args:
        lang: ar | fr | en

    Returns:
        dict مع: valid, lang, expires_at, days_left, is_warning, error
    """
    result = FacebookTokenResult(lang=lang)

    # Per-language credentials
    page_id, token = _get_fb_creds(lang)

    if not page_id or not token:
        result.error = "Missing FB_PAGE_ID or FB_PAGE_TOKEN"
        return result.to_dict()

    # Debug token
    data, error = _debug_facebook_token(token)

    if error:
        result.error = error
        return result.to_dict()

    if not data:
        result.error = "Empty response"
        return result.to_dict()

    # التحقق من الصلاحية
    is_valid = data.get("is_valid", False)

    if not is_valid:
        error_info = data.get("error", {})
        if isinstance(error_info, dict):
            result.error = error_info.get(
                "message", "Token is invalid"
            )
        else:
            result.error = "Token is invalid"
        return result.to_dict()

    # حساب الانتهاء
    expires_at = data.get("expires_at", 0)

    if expires_at == 0:
        # Page token دائم
        result.valid      = True
        result.expires_at = "never"
        result.days_left  = 999
        result.is_warning = False

        log.info(
            "  ✅ Facebook (%s): token valid (permanent)",
            lang.upper() if lang else "generic"
        )
        return result.to_dict()

    # حساب الأيام المتبقية
    try:
        exp_date  = datetime.fromtimestamp(expires_at)
        now       = datetime.now()
        days_left = (exp_date - now).days

        # Token expired → valid=False
        if days_left < 0:
            result.valid      = False
            result.days_left  = days_left
            result.expires_at = exp_date.strftime("%Y-%m-%d")
            result.error      = (
                f"Token expired {abs(days_left)} days ago "
                f"({result.expires_at})"
            )
            log.error(
                "  ❌ Facebook (%s): token expired %d days ago",
                lang.upper() if lang else "generic",
                abs(days_left)
            )
            return result.to_dict()

        result.valid      = True
        result.expires_at = exp_date.strftime("%Y-%m-%d")
        result.days_left  = days_left
        result.is_warning = days_left <= WARNING_DAYS_FB

    except Exception as e:
        result.error = f"Date parsing error: {_safe_str(e)}"
        return result.to_dict()

    # الطباعة
    label = lang.upper() if lang else "generic"
    if result.is_warning:
        log.warning(
            "  ⚠️  Facebook (%s): expires in %d days (%s)",
            label, days_left, result.expires_at
        )
    else:
        log.info(
            "  ✅ Facebook (%s): valid (%d days left)",
            label, days_left
        )

    return result.to_dict()


def extend_facebook_token(lang: str = "") -> dict:
    """
    تمديد Facebook token (Short → Long-lived).
    يحتاج FB_APP_ID + FB_APP_SECRET.

    Args:
        lang: ar | fr | en (اختياري)

    Returns:
        dict مع: success, token, expires_in, error
    """
    result = {
        "success":    False,
        "token":      "",
        "expires_in": 0,
        "error":      "",
    }

    app_id, app_secret = _get_fb_app_credentials()
    _, token = _get_fb_creds(lang)  # Per-language

    if not all([app_id, app_secret, token]):
        result["error"] = (
            "Missing FB_APP_ID, FB_APP_SECRET, or FB_PAGE_TOKEN"
        )
        return result

    try:
        r = requests.get(
            f"{GRAPH_API}/oauth/access_token",
            params = {
                "grant_type":        "fb_exchange_token",
                "client_id":         app_id,
                "client_secret":     app_secret,
                "fb_exchange_token": token,
            },
            timeout = TOKEN_TIMEOUT,
        )

        # Safe JSON parse
        data, parse_error = _safe_json_parse(r)
        if parse_error:
            result["error"] = parse_error
            return result

        if not data:
            result["error"] = "Empty response"
            return result

        if "error" in data:
            error_info = data["error"]
            if isinstance(error_info, dict):
                result["error"] = error_info.get(
                    "message", "Unknown error"
                )
            else:
                result["error"] = str(error_info)
            return result

        new_token  = data.get("access_token", "")
        expires_in = int(data.get("expires_in", 0))

        if not new_token:
            result["error"] = "Empty token in response"
            return result

        result["success"]    = True
        result["token"]      = new_token
        result["expires_in"] = expires_in

        days  = expires_in // 86400
        label = lang.upper() if lang else "generic"
        log.info(
            "  ✅ Facebook (%s): token extended (%d days)",
            label, days
        )

        log.info(
            "  ⚠️  Remember to update FB_PAGE_TOKEN_%s "
            "in environment!",
            label
        )

        return result

    except requests.exceptions.Timeout:
        result["error"] = "Request timeout"
        return result

    except Exception as e:
        result["error"] = _safe_str(e)
        return result


# ═════════════════════════════════════════════════════════════════════════════
# CHECK ALL TOKENS
# ═════════════════════════════════════════════════════════════════════════════

def check_all_tokens() -> dict:
    """
    يتحقق من جميع tokens للـ 3 لغات.

    Per-language Facebook check.
    Warnings logged clearly.

    Returns:
        dict مع: youtube, facebook, warnings, errors
    """
    log.info("\n" + "═" * 55)
    log.info("  🔑 Token Health Check v2.0")
    log.info("═" * 55)

    result = CheckAllResult()

    # ── YouTube ──────────────────────────────────────────────
    log.info("\n  📺 YouTube Tokens:")

    for lang in LANGS:
        token_result = check_youtube_token(lang)
        result.youtube[lang] = token_result

        if not token_result["valid"]:
            msg = (
                f"YouTube ({lang.upper()}) INVALID: "
                f"{token_result['error']}"
            )
            result.errors.append(msg)
            log.error("  ❌ %s", msg)

    # ── Facebook (Per-language) ──────────────────────────────
    log.info("\n  📘 Facebook Tokens (per-language):")

    for lang in LANGS:
        # Check if credentials exist for this language
        page_id, token = _get_fb_creds(lang)

        if not page_id or not token:
            log.info(
                "  ℹ️  Facebook (%s): not configured — skipping",
                lang.upper()
            )
            continue

        token_result = check_facebook_token(lang)
        result.facebook[lang] = token_result

        if not token_result["valid"]:
            msg = (
                f"Facebook ({lang.upper()}) INVALID: "
                f"{token_result['error']}"
            )
            result.errors.append(msg)
            log.error("  ❌ %s", msg)

        elif token_result.get("is_warning"):
            msg = (
                f"Facebook ({lang.upper()}) expires in "
                f"{token_result['days_left']} days! "
                f"({token_result['expires_at']})"
            )
            result.warnings.append(msg)
            # Log warning explicitly
            log.warning("  ⚠️  %s", msg)

    # ── Summary ──────────────────────────────────────────────
    log.info("\n" + "─" * 55)
    log.info(
        "  ✅ Valid   : %d tokens",
        result.count_valid()
    )
    log.info(
        "  ⚠️  Warnings: %d",
        len(result.warnings)
    )
    log.info(
        "  ❌ Errors  : %d",
        len(result.errors)
    )
    log.info("═" * 55 + "\n")

    return {
        "youtube":  result.youtube,
        "facebook": result.facebook,
        "warnings": result.warnings,
        "errors":   result.errors,
    }


# ═════════════════════════════════════════════════════════════════════════════
# REFRESH ALL YOUTUBE TOKENS
# ═════════════════════════════════════════════════════════════════════════════

def refresh_all_youtube_tokens() -> dict:
    """
    يجدد جميع YouTube tokens للـ 3 لغات.

    Returns:
        dict مع نتائج كل لغة
    """
    log.info("\n  🔄 Refreshing YouTube tokens...")

    results = {}

    for lang in LANGS:
        r = refresh_youtube_token(lang)
        results[lang] = r

        if not r["success"]:
            log.error(
                "  ❌ YouTube (%s) refresh failed: %s",
                lang.upper(), r['error']
            )

    success_count = sum(
        1 for r in results.values() if r["success"]
    )

    log.info(
        "  ✅ Refreshed %d/%d YouTube tokens",
        success_count, len(LANGS)
    )

    return results


# ═════════════════════════════════════════════════════════════════════════════
# CLI MAIN
# ═════════════════════════════════════════════════════════════════════════════

def _print_warnings_and_errors(results: dict) -> None:
    """طباعة التحذيرات والأخطاء."""
    warnings = results.get("warnings", [])
    errors   = results.get("errors",   [])

    if warnings:
        log.warning("\n  ⚠️  WARNINGS:")
        for w in warnings:
            log.warning("     - %s", w)

    if errors:
        log.error("\n  ❌ ERRORS:")
        for e in errors:
            log.error("     - %s", e)
        log.error(
            "\n  ❌ Some tokens are invalid. Please renew them."
        )


def main() -> None:
    """
    يُستخدم في GitHub Actions للتحقق من التوكنات.

    Exit codes:
        0: كل التوكنات صالحة
        1: يوجد أخطاء
    """
    # Logging أول شيء (entry point)
    logging.basicConfig(
        level   = logging.INFO,
        format  = "%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt = "%H:%M:%S",
    )

    results = check_all_tokens()

    _print_warnings_and_errors(results)

    if results.get("errors"):
        sys.exit(1)

    log.info("\n  ✅ All tokens are valid!")


if __name__ == "__main__":
    main()

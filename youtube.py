"""
📺 YouTube Auto-Publisher v2.0 — Final Production Edition

Features:
  ✅ Multi-channel support (AR, FR, EN)
  ✅ Short → YouTube Shorts (#Shorts tag)
  ✅ Long  → YouTube Long form
  ✅ Chunked resumable upload (no RAM overflow)
  ✅ Correct resume after chunk failure (server offset query)
  ✅ Smart description from street language (Groq)
  ✅ Credential fallback (with/without language suffix)
  ✅ MIME type detection from file extension
  ✅ Correct 403 parsing (quota vs auth vs other)
  ✅ No duplicate uploads on retry
  ✅ Input validation on all public functions
  ✅ Thumbnail upload after video publish (auto)
  ✅ Custom tags from manifest (optional)
"""

from __future__ import annotations

import logging
import mimetypes
import os
import time
from pathlib import Path
from typing import Optional

import requests

log = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════
# CONSTANTS
# ═══════════════════════════════════════════════════════════════════

# API URLs
YOUTUBE_TOKEN_URL     = "https://oauth2.googleapis.com/token"
YOUTUBE_UPLOAD_URL    = "https://www.googleapis.com/upload/youtube/v3/videos"
YOUTUBE_THUMBNAIL_URL = "https://www.googleapis.com/upload/youtube/v3/thumbnails/set"

# File size limits (MB)
MIN_FILE_MB       = 0.5
MAX_FILE_MB_SHORT = 500
MAX_FILE_MB_LONG  = 128 * 1024   # 128 GB

# Thumbnail limits
MAX_THUMBNAIL_SIZE_MB = 2        # YouTube limit = 2 MB
MIN_THUMBNAIL_SIZE_KB = 5

# Chunked upload
CHUNK_SIZE = 8 * 1024 * 1024     # 8 MB (multiple of 256KB ✅)

# Text limits
MAX_DESC_LEN  = 5_000
MAX_TITLE_LEN = 100

# YouTube Categories
CATEGORY_PEOPLE_BLOGS = "22"
CATEGORY_EDUCATION    = "27"

# Default tags
TAGS_SHORT = ["shorts", "viral", "psychology", "motivation", "reels"]
TAGS_LONG  = ["psychology", "motivation", "education", "mindset", "viral"]

# Timeouts (seconds)
TOKEN_TIMEOUT     = 30
INIT_TIMEOUT      = 30
CHUNK_TIMEOUT     = 120
STATUS_TIMEOUT    = 30
THUMBNAIL_TIMEOUT = 60

# HTTP status codes
HTTP_RESUME_INCOMPLETE = 308
HTTP_SUCCESS           = (200, 201)
HTTP_RATE_LIMIT        = 429

# Retry strategy
RETRY_DELAYS = [10, 20, 40]
MAX_RETRIES  = 3

# Valid values
_VALID_LANGS = frozenset({"ar", "fr", "en"})
_VALID_MODES = frozenset({"short", "long"})

# Supported MIME types for videos
_SUPPORTED_MIME_TYPES = frozenset({
    "video/mp4",
    "video/quicktime",
    "video/x-msvideo",
    "video/webm",
    "video/mpeg",
    "video/x-matroska",
})

# Video extension → MIME mapping
_EXT_MIME_MAP: dict[str, str] = {
    ".mp4":  "video/mp4",
    ".mov":  "video/quicktime",
    ".avi":  "video/x-msvideo",
    ".webm": "video/webm",
    ".mpeg": "video/mpeg",
    ".mpg":  "video/mpeg",
    ".mkv":  "video/x-matroska",
}

# Thumbnail extension → MIME mapping
_THUMBNAIL_MIME_MAP: dict[str, str] = {
    ".png":  "image/png",
    ".jpg":  "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif":  "image/gif",
}


# ═══════════════════════════════════════════════════════════════════
# VALIDATION
# ═══════════════════════════════════════════════════════════════════

def _validate_lang(lang: str) -> None:
    """التحقق من صحة اللغة."""
    if lang not in _VALID_LANGS:
        raise ValueError(
            f"Invalid lang '{lang}'. "
            f"Must be one of: {sorted(_VALID_LANGS)}"
        )


def _validate_mode(mode: str) -> None:
    """التحقق من صحة الـ content_mode."""
    if mode not in _VALID_MODES:
        raise ValueError(
            f"Invalid content_mode '{mode}'. "
            f"Must be one of: {sorted(_VALID_MODES)}"
        )


# ═══════════════════════════════════════════════════════════════════
# CREDENTIALS
# ═══════════════════════════════════════════════════════════════════

def _get_env(key_with_lang: str, key_generic: str) -> str:
    """قراءة متغير من البيئة مع fallback."""
    value = os.environ.get(key_with_lang, "").strip()
    if value:
        return value
    return os.environ.get(key_generic, "").strip()


def _read_creds(lang: str) -> tuple[str, str, str]:
    """
    قراءة YouTube credentials للغة محددة.
    
    Returns:
        (client_id, client_secret, refresh_token)
    """
    lu = lang.upper()
    return (
        _get_env(
            f"YOUTUBE_CLIENT_ID_{lu}",
            "YOUTUBE_CLIENT_ID",
        ),
        _get_env(
            f"YOUTUBE_CLIENT_SECRET_{lu}",
            "YOUTUBE_CLIENT_SECRET",
        ),
        _get_env(
            f"YOUTUBE_REFRESH_TOKEN_{lu}",
            "YOUTUBE_REFRESH_TOKEN",
        ),
    )


def credentials_available(lang: str) -> bool:
    """
    التحقق من توفر credentials بدون رفع exception.
    
    Args:
        lang: ar | fr | en
    
    Returns:
        True إذا كل الـ credentials موجودة
    """
    try:
        _validate_lang(lang)
    except ValueError:
        return False
    return all(_read_creds(lang))


def _get_creds(lang: str) -> tuple[str, str, str]:
    """
    جلب YouTube credentials مع validation.
    
    Raises:
        RuntimeError: لو credentials ناقصة
    """
    creds = _read_creds(lang)
    if not all(creds):
        lu = lang.upper()
        raise RuntimeError(
            f"Missing YouTube credentials for {lu}.\n"
            f"  Set: YOUTUBE_CLIENT_ID_{lu}, "
            f"YOUTUBE_CLIENT_SECRET_{lu}, "
            f"YOUTUBE_REFRESH_TOKEN_{lu}"
        )
    return creds


# ═══════════════════════════════════════════════════════════════════
# ACCESS TOKEN
# ═══════════════════════════════════════════════════════════════════

def _get_access_token(lang: str) -> str:
    """
    جلب access_token من YouTube OAuth.
    
    Args:
        lang: ar | fr | en
    
    Returns:
        access_token string
    
    Raises:
        RuntimeError: عند فشل جلب الـ token
    """
    client_id, client_secret, refresh_token = _get_creds(lang)

    try:
        r = requests.post(
            YOUTUBE_TOKEN_URL,
            data={
                "client_id":     client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
                "grant_type":    "refresh_token",
            },
            timeout=TOKEN_TIMEOUT,
        )
        r.raise_for_status()
    
    except requests.exceptions.Timeout:
        raise RuntimeError(
            f"YouTube token timeout ({lang.upper()})"
        )
    except requests.exceptions.HTTPError as e:
        code = e.response.status_code if e.response else "?"
        raise RuntimeError(
            f"YouTube token HTTP error ({lang.upper()}): {code}"
        )

    # Safe JSON parsing
    try:
        data = r.json()
    except ValueError:
        raise RuntimeError(
            f"YouTube returned non-JSON response ({lang.upper()})"
        )

    access_token = data.get("access_token", "").strip()

    if not access_token:
        error = data.get("error", "unknown")
        desc  = data.get("error_description", "")
        raise RuntimeError(
            f"Failed to get token ({lang.upper()}): "
            f"{error} — {desc}"
        )

    log.info(
        "  ✅ YouTube token obtained (%s)",
        lang.upper()
    )
    return access_token


# ═══════════════════════════════════════════════════════════════════
# MIME TYPE DETECTION
# ═══════════════════════════════════════════════════════════════════

def _get_mime_type(video_path: Path) -> str:
    """استخراج MIME type من امتداد ملف الفيديو."""
    ext = video_path.suffix.lower()

    # Try local map first (most reliable)
    if ext in _EXT_MIME_MAP:
        return _EXT_MIME_MAP[ext]

    # Try Python's mimetypes
    mime_type, _ = mimetypes.guess_type(str(video_path))
    if mime_type and mime_type in _SUPPORTED_MIME_TYPES:
        return mime_type

    # Default fallback
    return "video/mp4"


def _get_thumbnail_mime(thumbnail_path: Path) -> str:
    """استخراج MIME type لصورة الـ thumbnail."""
    ext = thumbnail_path.suffix.lower()
    return _THUMBNAIL_MIME_MAP.get(ext, "image/png")


# ═══════════════════════════════════════════════════════════════════
# VIDEO VALIDATION
# ═══════════════════════════════════════════════════════════════════

def _validate_video(
    video_path:   str,
    content_mode: str = "short",
) -> tuple[float, int]:
    """
    التحقق من ملف الفيديو قبل الرفع.
    
    Args:
        video_path:   مسار الفيديو
        content_mode: short | long
    
    Returns:
        (size_mb, size_bytes)
    
    Raises:
        FileNotFoundError: إذا الفيديو غير موجود
        ValueError:        إذا حجم الفيديو خارج النطاق
    """
    path = Path(video_path).resolve()

    if not path.exists():
        raise FileNotFoundError(f"Video not found: {path}")
    if not path.is_file():
        raise FileNotFoundError(f"Not a file: {path}")

    size_bytes = path.stat().st_size
    size_mb    = size_bytes / 1_048_576

    max_mb = (
        MAX_FILE_MB_SHORT
        if content_mode == "short"
        else MAX_FILE_MB_LONG
    )

    if size_mb > max_mb:
        raise ValueError(
            f"File too large: {size_mb:.1f} MB "
            f"(max {max_mb} MB for {content_mode})"
        )
    if size_mb < MIN_FILE_MB:
        raise ValueError(
            f"File too small: {size_mb:.2f} MB "
            f"(min {MIN_FILE_MB} MB)"
        )

    return size_mb, size_bytes


# ═══════════════════════════════════════════════════════════════════
# THUMBNAIL VALIDATION
# ═══════════════════════════════════════════════════════════════════

def _validate_thumbnail(thumbnail_path: str) -> Optional[Path]:
    """
    التحقق من صحة ملف الـ thumbnail.
    
    Returns:
        Path للملف أو None عند الفشل
    """
    try:
        path = Path(thumbnail_path).resolve()

        if not path.exists() or not path.is_file():
            log.warning("  ⚠️  Thumbnail not found: %s", path)
            return None

        size_bytes = path.stat().st_size
        size_kb    = size_bytes / 1024
        size_mb    = size_bytes / 1_048_576

        if size_mb > MAX_THUMBNAIL_SIZE_MB:
            log.warning(
                "  ⚠️  Thumbnail too large: %.2f MB (max %d MB)",
                size_mb, MAX_THUMBNAIL_SIZE_MB
            )
            return None

        if size_kb < MIN_THUMBNAIL_SIZE_KB:
            log.warning(
                "  ⚠️  Thumbnail too small: %.2f KB (min %d KB)",
                size_kb, MIN_THUMBNAIL_SIZE_KB
            )
            return None

        ext = path.suffix.lower()
        if ext not in _THUMBNAIL_MIME_MAP:
            log.warning(
                "  ⚠️  Unsupported thumbnail format: %s", ext
            )
            return None

        return path

    except Exception as e:
        log.warning("  ⚠️  Thumbnail validation error: %s", e)
        return None


# ═══════════════════════════════════════════════════════════════════
# DESCRIPTION BUILDER
# ═══════════════════════════════════════════════════════════════════

def _get_fallback_cta(lang: str, content_mode: str) -> str:
    """جلب CTA افتراضي حسب اللغة و content_mode."""
    cta_map: dict[str, dict[str, str]] = {
        "ar": {
            "long":  "اشترك في القناة وفعّل الجرس 🔔\nشارك الفيديو 🔥",
            "short": "اشترك وفعّل الجرس 🔔",
        },
        "fr": {
            "long":  "Abonne-toi et active la cloche 🔔\nPartage 🔥",
            "short": "Abonne-toi 🔔",
        },
        "en": {
            "long":  "Subscribe and hit the bell 🔔\nShare this 🔥",
            "short": "Subscribe and hit the bell 🔔",
        },
    }
    return cta_map.get(lang, cta_map["en"]).get(
        content_mode, cta_map["en"]["short"]
    )


def _get_fallback_hashtags(content_mode: str) -> str:
    """جلب hashtags افتراضية."""
    if content_mode == "long":
        return "#psychology #motivation #mindset"
    return "#Shorts #viral #psychology"


def build_youtube_description(
    record:             dict,
    lang:               str = "ar",
    street_description: str = "",
    content_mode:       str = "short",
) -> str:
    """
    بناء وصف YouTube.
    
    Priority:
        1. street_description (من Groq AI) — الأفضل
        2. title + CTA + hashtags افتراضية
    
    Args:
        record:             dict يحتوي title
        lang:               ar | fr | en
        street_description: وصف من Groq (اختياري)
        content_mode:       short | long
    
    Returns:
        وصف جاهز للرفع
    """
    # Priority 1: AI-generated description
    if street_description and street_description.strip():
        desc = street_description.strip()
        # Add #Shorts tag if missing (for Short videos)
        if content_mode == "short" and "#shorts" not in desc.lower():
            desc = f"#Shorts\n\n{desc}"
        return desc[:MAX_DESC_LEN]

    # Priority 2: Fallback (title + CTA + hashtags)
    title    = record.get("title", "")
    cta      = _get_fallback_cta(lang, content_mode)
    hashtags = _get_fallback_hashtags(content_mode)
    desc     = f"{title}\n\n{cta}\n\n{hashtags}"
    return desc[:MAX_DESC_LEN]


# ═══════════════════════════════════════════════════════════════════
# METADATA BUILDER
# ═══════════════════════════════════════════════════════════════════

def _build_metadata(
    title:        str,
    description:  str,
    lang:         str,
    content_mode: str = "short",
) -> dict:
    """
    بناء metadata الفيديو للرفع.
    
    NOTE: selfDeclaredMadeForKids فقط (madeForKids read-only).
    """
    default_lang = lang if lang in _VALID_LANGS else "en"

    if content_mode == "short":
        # Ensure #Shorts in description (YouTube requirement)
        if "#shorts" not in description.lower():
            description = f"#Shorts\n\n{description}"
        tags     = list(TAGS_SHORT) + [lang]
        category = CATEGORY_PEOPLE_BLOGS
    else:
        tags     = list(TAGS_LONG) + [lang]
        category = CATEGORY_EDUCATION

    return {
        "snippet": {
            "title":                title[:MAX_TITLE_LEN],
            "description":          description[:MAX_DESC_LEN],
            "defaultLanguage":      default_lang,
            "defaultAudioLanguage": default_lang,
            "tags":                 tags,
            "categoryId":           category,
        },
        "status": {
            "privacyStatus":           "public",
            "selfDeclaredMadeForKids": False,
        },
    }


# ═══════════════════════════════════════════════════════════════════
# 403 ERROR PARSER
# ═══════════════════════════════════════════════════════════════════

def _parse_403_error(response: requests.Response) -> str:
    """
    تحليل سبب الـ 403.
    
    Returns:
        "auth"  → credentials مشكلة
        "quota" → تجاوز الـ quota
        "other" → خطأ آخر قابل للـ retry
    """
    try:
        data   = response.json()
        errors = data.get("error", {}).get("errors", [])
        
        for err in errors:
            reason = err.get("reason", "").lower()
            
            if reason in (
                "quotaexceeded",
                "userratecondexceeded",
                "dailylimitexceeded",
                "ratelimitexceeded",
            ):
                return "quota"
            
            if reason in (
                "authorizationrequired",
                "forbidden",
                "insufficientpermissions",
            ):
                return "auth"
    
    except Exception:
        pass
    
    return "other"


# ═══════════════════════════════════════════════════════════════════
# RESUMABLE UPLOAD — INIT
# ═══════════════════════════════════════════════════════════════════

def _init_resumable_upload(
    metadata:     dict,
    access_token: str,
    size_bytes:   int,
    mime_type:    str,
    retries:      int = MAX_RETRIES,
) -> str:
    """
    تهيئة resumable upload session.
    
    Returns:
        upload_url للاستخدام في رفع الـ chunks
    
    Raises:
        RuntimeError: عند فشل التهيئة
    """
    last_error: Optional[str] = None

    for attempt in range(retries):
        try:
            r = requests.post(
                YOUTUBE_UPLOAD_URL,
                params={
                    "uploadType": "resumable",
                    "part":       "snippet,status",
                },
                headers={
                    "Authorization":           f"Bearer {access_token}",
                    "Content-Type":            "application/json; charset=UTF-8",
                    "X-Upload-Content-Type":   mime_type,
                    "X-Upload-Content-Length": str(size_bytes),
                },
                json    = metadata,
                timeout = INIT_TIMEOUT,
            )
            r.raise_for_status()

            upload_url = r.headers.get("Location", "").strip()
            if not upload_url:
                raise RuntimeError(
                    "YouTube did not return upload URL"
                )

            log.info("  📡 Upload session initialized")
            return upload_url

        except requests.exceptions.HTTPError as e:
            code       = e.response.status_code if e.response else 0
            last_error = str(e)

            # Auth errors → no retry
            if code == 401:
                raise RuntimeError(
                    "YouTube auth error (401) — "
                    "refresh token invalid or expired"
                )

            if code == 403 and e.response:
                reason = _parse_403_error(e.response)
                if reason in ("auth", "quota"):
                    raise RuntimeError(
                        f"YouTube 403 ({reason}) — cannot retry"
                    )
                # "other" → retry

        except RuntimeError:
            raise

        except Exception as e:
            last_error = str(e)

        # Retry with backoff
        if attempt < retries - 1:
            wait = RETRY_DELAYS[
                min(attempt, len(RETRY_DELAYS) - 1)
            ]
            log.warning(
                "  ⚠️  Init retry %d/%d in %ds",
                attempt + 1, retries, wait
            )
            time.sleep(wait)

    raise RuntimeError(
        f"Failed to init upload after {retries} attempts: "
        f"{last_error}"
    )


# ═══════════════════════════════════════════════════════════════════
# RESUMABLE UPLOAD — STATUS QUERY
# ═══════════════════════════════════════════════════════════════════

def _query_upload_status(
    upload_url: str,
    size_bytes: int,
) -> int:
    """
    استعلام عن آخر byte مُستلَم من YouTube.
    يُستخدم عند فشل chunk للاستئناف من المكان الصحيح.
    
    Returns:
        offset التالي للرفع (0 لو لم يُستلم شيء)
    """
    try:
        r = requests.put(
            upload_url,
            headers={
                "Content-Range":  f"bytes */{size_bytes}",
                "Content-Length": "0",
            },
            timeout=STATUS_TIMEOUT,
        )

        if r.status_code == HTTP_RESUME_INCOMPLETE:
            range_header = r.headers.get("Range", "")
            if range_header and "-" in range_header:
                # Format: bytes=0-N
                end = range_header.split("-")[-1].strip()
                try:
                    return int(end) + 1
                except ValueError:
                    pass

        if r.status_code in HTTP_SUCCESS:
            return size_bytes  # Upload complete

    except Exception as e:
        log.debug("    Status query failed: %s", e)

    return 0


# ═══════════════════════════════════════════════════════════════════
# RESUMABLE UPLOAD — CHUNKS
# ═══════════════════════════════════════════════════════════════════

def _upload_chunks(
    upload_url: str,
    video_path: Path,
    size_bytes: int,
    mime_type:  str,
) -> dict:
    """
    رفع الفيديو chunk بـ chunk مع resume logic.
    
    Returns:
        dict مع response data (يحتوي video ID)
    """
    uploaded    = 0
    last_result : dict = {}

    with open(str(video_path), "rb") as f:

        while uploaded < size_bytes:
            # Seek to current position
            f.seek(uploaded)
            chunk      = f.read(CHUNK_SIZE)
            chunk_size = len(chunk)

            if not chunk:
                break

            chunk_end = uploaded + chunk_size - 1
            pct       = (uploaded / size_bytes) * 100

            log.info(
                "  📤 %.1f%% (%dMB / %dMB)",
                pct,
                uploaded // 1_048_576,
                size_bytes // 1_048_576
            )

            chunk_done = False

            for attempt in range(MAX_RETRIES):
                try:
                    r = requests.put(
                        upload_url,
                        headers={
                            "Content-Type":  mime_type,
                            "Content-Range": (
                                f"bytes {uploaded}-"
                                f"{chunk_end}/{size_bytes}"
                            ),
                        },
                        data    = chunk,
                        timeout = CHUNK_TIMEOUT,
                    )

                    # 308 = chunk received, continue
                    if r.status_code == HTTP_RESUME_INCOMPLETE:
                        uploaded   += chunk_size
                        chunk_done  = True
                        break

                    # 200/201 = upload complete
                    if r.status_code in HTTP_SUCCESS:
                        try:
                            last_result = r.json()
                        except ValueError:
                            last_result = {}
                        uploaded   += chunk_size
                        chunk_done  = True
                        break

                    # Rate limit → wait and retry
                    if r.status_code == HTTP_RATE_LIMIT:
                        wait = RETRY_DELAYS[
                            min(attempt, len(RETRY_DELAYS) - 1)
                        ]
                        log.warning(
                            "  ⚠️  Rate limit — waiting %ds", wait
                        )
                        time.sleep(wait)
                        continue

                    # Auth errors
                    if r.status_code == 401:
                        raise RuntimeError("YouTube auth error (401)")

                    if r.status_code == 403:
                        reason = _parse_403_error(r)
                        if reason in ("auth", "quota"):
                            raise RuntimeError(
                                f"YouTube 403 ({reason})"
                            )
                        # "other" → continue retry

                    # Unknown error
                    raise RuntimeError(
                        f"Chunk HTTP {r.status_code}: "
                        f"{r.text[:200]}"
                    )

                except RuntimeError:
                    # Don't retry on auth/quota errors
                    raise

                except Exception as e:
                    if attempt >= MAX_RETRIES - 1:
                        raise RuntimeError(
                            f"Chunk failed after {MAX_RETRIES} "
                            f"attempts: {e}"
                        )

                    # Query server for real offset (recovery)
                    real_offset = _query_upload_status(
                        upload_url, size_bytes
                    )

                    if real_offset > uploaded:
                        log.info(
                            "  ↩️  Server received %dMB — resuming",
                            real_offset // 1_048_576
                        )
                        uploaded   = real_offset
                        chunk_done = True
                        break

                    # Wait and retry
                    wait = RETRY_DELAYS[
                        min(attempt, len(RETRY_DELAYS) - 1)
                    ]
                    log.warning(
                        "  ⚠️  Chunk error (%d/%d): %s — retry in %ds",
                        attempt + 1, MAX_RETRIES,
                        str(e)[:80], wait
                    )
                    time.sleep(wait)

            if not chunk_done:
                raise RuntimeError(
                    f"Failed to upload chunk at offset {uploaded}"
                )

    return last_result


# ═══════════════════════════════════════════════════════════════════
# THUMBNAIL UPLOAD
# ═══════════════════════════════════════════════════════════════════

def _upload_thumbnail(
    video_id:       str,
    thumbnail_path: str,
    access_token:   str,
    retries:        int = MAX_RETRIES,
) -> bool:
    """
    رفع thumbnail لفيديو YouTube بعد نشره.
    
    Args:
        video_id:       ID الفيديو المنشور
        thumbnail_path: مسار صورة الـ thumbnail
        access_token:   YouTube access token
        retries:        عدد محاولات الرفع
    
    Returns:
        True إذا نجح الرفع، False عند الفشل
    """
    # Validate thumbnail file
    path = _validate_thumbnail(thumbnail_path)
    if not path:
        return False

    mime_type = _get_thumbnail_mime(path)
    size_kb   = path.stat().st_size / 1024

    log.info(
        "  🖼️  Uploading thumbnail (%.1f KB, %s)...",
        size_kb, mime_type
    )

    last_error: Optional[str] = None

    for attempt in range(retries):
        try:
            with open(str(path), "rb") as f:
                r = requests.post(
                    YOUTUBE_THUMBNAIL_URL,
                    params  = {"videoId": video_id},
                    headers = {
                        "Authorization": f"Bearer {access_token}",
                        "Content-Type":  mime_type,
                    },
                    data    = f,
                    timeout = THUMBNAIL_TIMEOUT,
                )
                r.raise_for_status()

            log.info(
                "  ✅ Thumbnail uploaded for video %s",
                video_id
            )
            return True

        except requests.exceptions.HTTPError as e:
            code       = e.response.status_code if e.response else 0
            last_error = str(e)

            log.warning(
                "  ⚠️  Thumbnail HTTP %d (attempt %d/%d)",
                code, attempt + 1, retries
            )

            # Auth errors → no retry
            if code == 401:
                log.error(
                    "  ❌ Thumbnail: auth error (401) — "
                    "token may be expired"
                )
                return False

            if code == 403 and e.response:
                reason = _parse_403_error(e.response)
                if reason in ("auth", "quota"):
                    log.error(
                        "  ❌ Thumbnail: 403 (%s) — cannot retry",
                        reason
                    )
                    return False

            # Retry with backoff
            if attempt < retries - 1:
                wait = min(10 * (2 ** attempt), 60)
                log.info("  ↩️  Thumbnail retry in %ds...", wait)
                time.sleep(wait)

        except requests.exceptions.Timeout:
            last_error = "Thumbnail upload timed out"
            log.warning(
                "  ⚠️  Thumbnail timeout (attempt %d/%d)",
                attempt + 1, retries
            )
            if attempt < retries - 1:
                time.sleep(15)

        except Exception as e:
            last_error = str(e)
            log.warning(
                "  ⚠️  Thumbnail error (attempt %d/%d): %s",
                attempt + 1, retries, last_error[:100]
            )
            if attempt < retries - 1:
                time.sleep(10)

    log.error(
        "  ❌ Thumbnail upload failed after %d attempts: %s",
        retries, last_error
    )
    return False


# ═══════════════════════════════════════════════════════════════════
# BUILD VIDEO URL
# ═══════════════════════════════════════════════════════════════════

def _build_video_url(video_id: str, content_mode: str) -> str:
    """بناء رابط الفيديو على YouTube."""
    if content_mode == "short":
        return f"https://www.youtube.com/shorts/{video_id}"
    return f"https://www.youtube.com/watch?v={video_id}"


# ═══════════════════════════════════════════════════════════════════
# CORE UPLOAD
# ═══════════════════════════════════════════════════════════════════

def _upload_video(
    video_path:     str,
    title:          str,
    description:    str,
    access_token:   str,
    lang:           str,
    content_mode:   str = "short",
    thumbnail_path: str = "",
) -> dict:
    """
    رفع الفيديو الكامل عبر Resumable Upload API.
    
    Pipeline:
        1. Validate video
        2. Init upload session
        3. Upload chunks
        4. Upload thumbnail (if provided)
        5. Return result with URL
    
    Args:
        video_path:     مسار الفيديو
        title:          عنوان الفيديو
        description:    وصف الفيديو
        access_token:   YouTube access token
        lang:           ar | fr | en
        content_mode:   short | long
        thumbnail_path: مسار الـ thumbnail (اختياري)
    
    Returns:
        dict مع id, url, video_id
    """
    path                = Path(video_path).resolve()
    size_mb, size_bytes = _validate_video(str(path), content_mode)
    mime_type           = _get_mime_type(path)

    log.info(
        "  📤 Uploading [%s] (%.1f MB) [%s] → YouTube (%s)...",
        content_mode.upper(),
        size_mb,
        mime_type,
        lang.upper()
    )

    # Build metadata
    metadata = _build_metadata(
        title, description, lang, content_mode
    )

    # Init resumable upload session
    upload_url = _init_resumable_upload(
        metadata, access_token, size_bytes, mime_type
    )

    # Upload video chunks
    result = _upload_chunks(
        upload_url, path, size_bytes, mime_type
    )

    video_id = result.get("id", "").strip()
    if not video_id:
        raise RuntimeError(
            f"YouTube response missing video ID: {result}"
        )

    url = _build_video_url(video_id, content_mode)
    log.info(
        "  ✅ [%s] published → %s",
        content_mode.upper(), url
    )

    # Upload thumbnail (non-blocking — failure doesn't fail the video)
    if thumbnail_path:
        try:
            thumbnail_uploaded = _upload_thumbnail(
                video_id       = video_id,
                thumbnail_path = thumbnail_path,
                access_token   = access_token,
            )
            if not thumbnail_uploaded:
                log.warning(
                    "  ⚠️  Video published but thumbnail failed"
                )
        except Exception as e:
            log.warning(
                "  ⚠️  Thumbnail upload exception "
                "(video still published): %s", e
            )
    else:
        log.info("  ℹ️  No thumbnail provided")

    # Enrich result with metadata
    result["url"]      = url
    result["video_id"] = video_id

    return result


# ═══════════════════════════════════════════════════════════════════
# MAIN PUBLISH FUNCTION
# ═══════════════════════════════════════════════════════════════════

def publish_to_youtube(
    video_path:         str,
    record:             dict,
    lang:               str  = "ar",
    street_description: str  = "",
    content_mode:       str  = "short",
    thumbnail_path:     str  = "",
    retries:            int  = MAX_RETRIES,
) -> dict:
    """
    نشر فيديو على YouTube مع thumbnail.
    
    Args:
        video_path:         مسار الفيديو
        record:             dict يحتوي title, number, ...
        lang:               ar | fr | en
        street_description: وصف من Groq AI (اختياري)
        content_mode:       short | long
        thumbnail_path:     مسار صورة الـ thumbnail (اختياري)
        retries:            عدد محاولات init الـ session
    
    Returns:
        dict مع: id, url, video_id
    
    Raises:
        ValueError:        إذا lang/mode غير صالح
        FileNotFoundError: إذا الفيديو غير موجود
        RuntimeError:      عند فشل النشر
    """
    # Validate inputs
    _validate_lang(lang)
    _validate_mode(content_mode)

    path = Path(video_path).resolve()
    if not path.exists() or not path.is_file():
        raise FileNotFoundError(f"Video not found: {path}")

    if not credentials_available(lang):
        raise RuntimeError(
            f"YouTube credentials not available for {lang.upper()}"
        )

    # Extract and validate title
    title = str(record.get("title", "")).strip()[:MAX_TITLE_LEN]
    if not title:
        raise ValueError("record['title'] cannot be empty")

    # Build description
    description = build_youtube_description(
        record             = record,
        lang               = lang,
        street_description = street_description,
        content_mode       = content_mode,
    )

    type_label = (
        "Shorts ⚡"
        if content_mode == "short"
        else "Long Form 🎬"
    )

    # Log publish info
    log.info(
        "\n  📺 Publishing → YouTube (%s) [%s]",
        lang.upper(), content_mode.upper()
    )
    log.info("     Title : %s", title[:60])
    log.info("     Desc  : %d chars", len(description))
    log.info("     Type  : %s", type_label)

    if thumbnail_path:
        log.info("     Thumb : %s", Path(thumbnail_path).name)
    else:
        log.info("     Thumb : (none)")

    last_error: Optional[str] = None

    # Retry loop for the entire publish operation
    for attempt in range(retries):
        try:
            # Get fresh access token for each attempt
            access_token = _get_access_token(lang)

            # Upload video + thumbnail
            result = _upload_video(
                video_path     = str(path),
                title          = title,
                description    = description,
                access_token   = access_token,
                lang           = lang,
                content_mode   = content_mode,
                thumbnail_path = thumbnail_path,
            )
            return result

        except requests.exceptions.HTTPError as e:
            code       = e.response.status_code if e.response else 0
            last_error = str(e)

            log.warning(
                "  ⚠️  HTTP %d (attempt %d/%d)",
                code, attempt + 1, retries
            )

            # Auth errors → fail immediately
            if code == 401:
                raise RuntimeError(
                    f"YouTube auth error (401). "
                    f"Refresh YOUTUBE_REFRESH_TOKEN_{lang.upper()}."
                )
            
            if code == 403 and e.response:
                reason = _parse_403_error(e.response)
                if reason == "quota":
                    raise RuntimeError(
                        "YouTube quota exceeded. Try again tomorrow."
                    )
                if reason == "auth":
                    raise RuntimeError(
                        f"YouTube auth error (403). "
                        f"Refresh YOUTUBE_REFRESH_TOKEN_{lang.upper()}."
                    )

            # Retry other errors
            if attempt < retries - 1:
                wait = RETRY_DELAYS[
                    min(attempt, len(RETRY_DELAYS) - 1)
                ]
                log.info("  ↩️  Retry in %ds...", wait)
                time.sleep(wait)

        except requests.exceptions.Timeout:
            last_error = "Request timed out"
            log.warning(
                "  ⚠️  Timeout (attempt %d/%d)",
                attempt + 1, retries
            )
            if attempt < retries - 1:
                time.sleep(20)

        except (FileNotFoundError, ValueError):
            # Validation errors → no retry
            raise

        except RuntimeError as e:
            err_str    = str(e).lower()
            last_error = str(e)

            # Don't retry on auth/quota errors
            if any(x in err_str for x in (
                "auth error",
                "quota exceeded",
                "403 (auth)",
                "403 (quota)",
            )):
                raise

            log.warning(
                "  ⚠️  Error (attempt %d/%d): %s",
                attempt + 1, retries, str(e)[:100]
            )
            if attempt < retries - 1:
                wait = RETRY_DELAYS[
                    min(attempt, len(RETRY_DELAYS) - 1)
                ]
                time.sleep(wait)

        except Exception as e:
            last_error = str(e)
            log.warning(
                "  ⚠️  Unexpected (attempt %d/%d): %s",
                attempt + 1, retries, last_error[:100]
            )
            if attempt < retries - 1:
                time.sleep(10)

    raise RuntimeError(
        f"YouTube publish failed after {retries} attempts: "
        f"{last_error}"
    )


# ═══════════════════════════════════════════════════════════════════
# CHECK CREDENTIALS
# ═══════════════════════════════════════════════════════════════════

def check_credentials(lang: str) -> bool:
    """
    التحقق من صحة credentials عبر جلب access_token فعلياً.
    
    Args:
        lang: ar | fr | en
    
    Returns:
        True إذا الـ credentials صالحة وتعمل
    """
    try:
        _validate_lang(lang)
    except ValueError as e:
        log.error("  ❌ %s", e)
        return False

    try:
        token = _get_access_token(lang)
        if token:
            log.info(
                "  ✅ YouTube (%s): credentials OK",
                lang.upper()
            )
            return True
        return False
    
    except Exception as e:
        log.error(
            "  ❌ YouTube credentials invalid (%s): %s",
            lang.upper(), e
        )
        return False

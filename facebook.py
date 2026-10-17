"""
📘 Facebook Auto-Publisher v2.0 — Final Production Edition

Features:
  ✅ Multi-page support (AR, FR, EN — per-language credentials)
  ✅ Short → Reel (9:16, 3-90s)
  ✅ Long  → Regular Video (any duration)
  ✅ Auto fallback: Reel fails → Regular Video (any attempt)
  ✅ Token expiry detection (no retry on auth errors)
  ✅ Retry with exponential backoff
  ✅ Resumable upload for Reels (3 phases)
  ✅ Streaming upload for Reels (no RAM overflow)
  ✅ Thumbnail upload for videos AND reels
  ✅ as_reel determined by content_mode (not parameter)
  ✅ Per-language Facebook Pages
  ✅ Separate file size limits (Reel vs Video)
  ✅ Configurable Graph API version
  ✅ Configurable video_state (PUBLISHED/DRAFT)
  ✅ Single validation (no duplicate ffprobe calls)
  ✅ Response body validation after Phase 2
  ✅ Safe file handle management (finally blocks)
"""

from __future__ import annotations

import logging
import os
import subprocess
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional

import requests

log = logging.getLogger(__name__)

# ═════════════════════════════════════════════════════════════════════════════
# CONSTANTS
# ═════════════════════════════════════════════════════════════════════════════

# API (configurable version)
GRAPH_API_VERSION = os.environ.get(
    "FB_API_VERSION", "v21.0"
)
GRAPH_API = (
    f"https://graph.facebook.com/{GRAPH_API_VERSION}"
)

# Reel constraints (seconds)
REEL_MAX_DURATION_S = 90.0
REEL_MIN_DURATION_S = 3.0

# Regular Video constraints (seconds)
VIDEO_MAX_DURATION_S = 14400.0  # 4 hours
VIDEO_MIN_DURATION_S = 1.0

# File constraints (MB) — separate for Reel vs Video
REEL_MAX_FILE_MB  = 1024     # 1 GB for Reels
VIDEO_MAX_FILE_MB = 10240    # 10 GB for Videos
MIN_FILE_MB       = 0.5

# Thumbnail constraints
MAX_THUMBNAIL_SIZE_MB = 10
MIN_THUMBNAIL_SIZE_KB = 5

# Text constraints
MAX_DESC_LEN  = 63206
MAX_TITLE_LEN = 255

# Timeouts (seconds)
CHECK_TIMEOUT     = 15
FFPROBE_TIMEOUT   = 15
API_TIMEOUT       = 30
PUBLISH_TIMEOUT   = 60
UPLOAD_TIMEOUT    = 600
THUMBNAIL_TIMEOUT = 60

# Retry strategy
DEFAULT_RETRIES    = 3
RETRY_BASE_WAIT    = 5
RETRY_MAX_WAIT     = 30
TIMEOUT_RETRY_WAIT = 15
GENERIC_ERROR_WAIT = 5

# Token expiry error codes
TOKEN_EXPIRED_CODES = (190, 102, 463, 467)

# HTTP success codes
HTTP_SUCCESS = (200, 201)

# Valid values
_VALID_LANGS = frozenset({"ar", "fr", "en", ""})

# Thumbnail MIME types
_THUMBNAIL_MIME_MAP: dict[str, str] = {
    ".png":  "image/png",
    ".jpg":  "image/jpeg",
    ".jpeg": "image/jpeg",
}


# ═════════════════════════════════════════════════════════════════════════════
# ENUMS & DATA CLASSES
# ═════════════════════════════════════════════════════════════════════════════

class UploadType(str, Enum):
    """نوع الـ upload."""
    REEL  = "reel"
    VIDEO = "video"


@dataclass
class FacebookCreds:
    """Facebook Page credentials."""
    page_id: str
    token:   str

    def is_valid(self) -> bool:
        return bool(self.page_id and self.token)


@dataclass
class VideoInfo:
    """معلومات الفيديو بعد validation."""
    size_mb:    float
    size_bytes: int
    duration:   float


# ═════════════════════════════════════════════════════════════════════════════
# CTA TEMPLATES
# ═════════════════════════════════════════════════════════════════════════════

CTA_TEMPLATES: dict[str, str] = {
    "ar": "اكتب رأيك في التعليقات 👇",
    "fr": "Dis-moi ton avis en commentaire 👇",
    "en": "Tell me in the comments 👇",
}


# ═════════════════════════════════════════════════════════════════════════════
# CREDENTIALS — Per-language support
# ═════════════════════════════════════════════════════════════════════════════

def _read_creds_from_env(
    lang: str = "",
) -> FacebookCreds:
    """
    قراءة Facebook credentials per-language.

    Supports:
        FB_PAGE_ID_AR, FB_PAGE_TOKEN_AR
        FB_PAGE_ID_FR, FB_PAGE_TOKEN_FR
        FB_PAGE_ID_EN, FB_PAGE_TOKEN_EN
    Fallback: FB_PAGE_ID / FB_PAGE_TOKEN
    """
    lu = lang.upper().strip() if lang else ""

    if lu and lu in ("AR", "FR", "EN"):
        page_id = (
            os.environ.get(
                f"FB_PAGE_ID_{lu}", ""
            ).strip()
            or os.environ.get(
                "FB_PAGE_ID", ""
            ).strip()
        )
        token = (
            os.environ.get(
                f"FB_PAGE_TOKEN_{lu}", ""
            ).strip()
            or os.environ.get(
                "FB_PAGE_TOKEN", ""
            ).strip()
        )
    else:
        page_id = os.environ.get(
            "FB_PAGE_ID", ""
        ).strip()
        token = os.environ.get(
            "FB_PAGE_TOKEN", ""
        ).strip()

    return FacebookCreds(
        page_id=page_id, token=token
    )


def _get_creds(lang: str = "") -> FacebookCreds:
    """جلب credentials مع validation."""
    creds = _read_creds_from_env(lang)

    if not creds.is_valid():
        lu = lang.upper() if lang else "generic"
        raise RuntimeError(
            f"Missing Facebook credentials for {lu}.\n"
            f"  Set FB_PAGE_ID_{lu} and "
            f"FB_PAGE_TOKEN_{lu}\n"
            f"  Or generic: FB_PAGE_ID and "
            f"FB_PAGE_TOKEN"
        )

    return creds


def credentials_available(
    lang: str = "",
) -> bool:
    """التحقق من وجود credentials."""
    creds = _read_creds_from_env(lang)
    return creds.is_valid()


def check_credentials(
    lang: str = "",
) -> bool:
    """التحقق من صحة credentials بـ API call."""
    try:
        creds = _get_creds(lang)

        r = requests.get(
            f"{GRAPH_API}/{creds.page_id}",
            params = {
                "access_token": creds.token,
                "fields":       "name,id",
            },
            timeout = CHECK_TIMEOUT,
        )
        r.raise_for_status()

        data = r.json()
        name = data.get("name", "Unknown")
        lu   = lang.upper() if lang else "generic"

        log.info(
            "  ✅ Facebook (%s): '%s' (ID: %s)",
            lu, name, data.get('id')
        )
        return True

    except Exception as e:
        lu = lang.upper() if lang else "generic"
        log.error(
            "  ❌ Facebook credentials invalid "
            "(%s): %s",
            lu, str(e)[:150]
        )
        return False


# ═════════════════════════════════════════════════════════════════════════════
# VIDEO VALIDATION
# ═════════════════════════════════════════════════════════════════════════════

def _probe_video_duration(path: str) -> float:
    """استخراج مدة الفيديو بـ ffprobe."""
    try:
        r = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                path,
            ],
            capture_output = True,
            text           = True,
            timeout        = FFPROBE_TIMEOUT,
        )
        output = r.stdout.strip()
        return float(output) if output else 0.0

    except FileNotFoundError:
        log.error(
            "  ❌ ffprobe not found — install FFmpeg"
        )
        return -1.0
    except subprocess.TimeoutExpired:
        log.warning("  ⚠️  ffprobe timeout")
        return 0.0
    except (ValueError, Exception) as e:
        log.debug("  ffprobe error: %s", e)
        return 0.0


def _validate_file_size(
    path:    Path,
    as_reel: bool = True,
) -> tuple[float, int]:
    """التحقق من حجم الملف."""
    size_bytes = path.stat().st_size
    size_mb    = size_bytes / 1_048_576

    max_mb = (
        REEL_MAX_FILE_MB
        if as_reel
        else VIDEO_MAX_FILE_MB
    )

    if size_mb > max_mb:
        label = 'Reel' if as_reel else 'Video'
        raise ValueError(
            f"File too large: {size_mb:.0f} MB "
            f"(max {max_mb} MB for {label})"
        )

    if size_mb < MIN_FILE_MB:
        raise ValueError(
            f"File too small: {size_mb:.2f} MB"
        )

    return size_mb, size_bytes


def _validate_reel_duration(
    duration: float,
) -> None:
    """التحقق من مدة Reel."""
    if duration < REEL_MIN_DURATION_S:
        raise ValueError(
            f"Reel too short: {duration:.1f}s "
            f"(min {REEL_MIN_DURATION_S}s)"
        )

    if duration > REEL_MAX_DURATION_S:
        raise ValueError(
            f"Reel too long: {duration:.1f}s "
            f"(max {REEL_MAX_DURATION_S}s)"
        )


def _validate_video_duration(
    duration: float,
) -> None:
    """التحقق من مدة Video عادي."""
    if duration < VIDEO_MIN_DURATION_S:
        raise ValueError(
            f"Video too short: {duration:.1f}s"
        )

    if duration > VIDEO_MAX_DURATION_S:
        raise ValueError(
            f"Video too long: {duration:.1f}s"
        )


def _validate_video(
    video_path: str,
    as_reel:    bool = True,
) -> VideoInfo:
    """التحقق الشامل من الفيديو."""
    path = Path(video_path).resolve()

    if not path.exists():
        raise FileNotFoundError(
            f"Video not found: {path}"
        )

    size_mb, size_bytes = _validate_file_size(
        path, as_reel
    )

    duration = _probe_video_duration(str(path))

    if duration == -1.0:
        log.warning(
            "  ⚠️  Cannot validate duration "
            "(ffprobe missing) — assuming 30s"
        )
        duration = 30.0
    elif duration <= 0:
        raise ValueError(
            "Could not determine video duration"
        )

    if as_reel:
        _validate_reel_duration(duration)
    else:
        _validate_video_duration(duration)

    return VideoInfo(
        size_mb    = size_mb,
        size_bytes = size_bytes,
        duration   = duration,
    )


# ═════════════════════════════════════════════════════════════════════════════
# THUMBNAIL HELPERS
# ═════════════════════════════════════════════════════════════════════════════

def _get_thumbnail_mime(
    thumbnail_path: Path,
) -> str:
    """استخراج MIME type."""
    ext = thumbnail_path.suffix.lower()
    return _THUMBNAIL_MIME_MAP.get(ext, "image/png")


def _validate_thumbnail(
    thumbnail_path: str,
) -> Optional[Path]:
    """التحقق من الـ thumbnail."""
    try:
        path = Path(thumbnail_path).resolve()

        if not path.exists() or not path.is_file():
            log.warning(
                "  ⚠️  Thumbnail not found: %s",
                path
            )
            return None

        size_bytes = path.stat().st_size
        size_kb    = size_bytes / 1024
        size_mb    = size_bytes / 1_048_576

        if size_mb > MAX_THUMBNAIL_SIZE_MB:
            log.warning(
                "  ⚠️  Thumbnail too large: "
                "%.2f MB (max %d MB)",
                size_mb, MAX_THUMBNAIL_SIZE_MB
            )
            return None

        if size_kb < MIN_THUMBNAIL_SIZE_KB:
            log.warning(
                "  ⚠️  Thumbnail too small: "
                "%.2f KB",
                size_kb
            )
            return None

        ext = path.suffix.lower()
        if ext not in _THUMBNAIL_MIME_MAP:
            log.warning(
                "  ⚠️  Unsupported thumbnail "
                "format: %s",
                ext
            )
            return None

        return path

    except Exception as e:
        log.warning(
            "  ⚠️  Thumbnail validation error: %s",
            e
        )
        return None


def _upload_reel_thumbnail(
    video_id:       str,
    thumbnail_path: str,
    page_id:        str,
    token:          str,
) -> bool:
    """رفع thumbnail لـ Reel بعد نشره."""
    thumb_path = _validate_thumbnail(thumbnail_path)
    if not thumb_path:
        return False

    try:
        log.info(
            "  🖼️  Uploading Reel thumbnail: %s",
            thumb_path.name
        )

        with open(str(thumb_path), "rb") as f:
            r = requests.post(
                f"{GRAPH_API}/{video_id}",
                data = {
                    "access_token": token,
                },
                files = {
                    "thumb": (
                        thumb_path.name,
                        f,
                        _get_thumbnail_mime(
                            thumb_path
                        ),
                    ),
                },
                timeout = THUMBNAIL_TIMEOUT,
            )
            r.raise_for_status()

        log.info("  ✅ Reel thumbnail uploaded")
        return True

    except Exception as e:
        log.warning(
            "  ⚠️  Reel thumbnail upload failed: %s",
            str(e)[:150]
        )
        return False


# ═════════════════════════════════════════════════════════════════════════════
# CAPTION BUILDER
# ═════════════════════════════════════════════════════════════════════════════

def _get_cta(lang: str) -> str:
    """جلب CTA حسب اللغة."""
    return CTA_TEMPLATES.get(
        lang, CTA_TEMPLATES["en"]
    )


def build_caption(
    record:     dict,
    lang:       str = "ar",
    ai_caption: str = "",
) -> str:
    """بناء caption."""
    if ai_caption and ai_caption.strip():
        return ai_caption[:MAX_DESC_LEN]

    title = record.get("title", "")
    cta   = _get_cta(lang)

    parts = []
    if title:
        parts.append(title)
    parts.append(f"\n{cta}")

    return "\n".join(parts)[:MAX_DESC_LEN]


# ═════════════════════════════════════════════════════════════════════════════
# UPLOAD AS REEL (Short) — Streaming + Thumbnail
# ═════════════════════════════════════════════════════════════════════════════

def _init_reel_upload(
    page_id: str,
    token:   str,
) -> tuple[str, str]:
    """Phase 1: Initialize Reel upload."""
    r = requests.post(
        f"{GRAPH_API}/{page_id}/video_reels",
        data = {
            "upload_phase": "start",
            "access_token": token,
        },
        timeout = API_TIMEOUT,
    )
    r.raise_for_status()

    data       = r.json()
    video_id   = data.get("video_id")
    upload_url = data.get("upload_url")

    if not video_id or not upload_url:
        raise RuntimeError(
            f"Reel init failed: {data}"
        )

    return video_id, upload_url


def _upload_reel_binary(
    upload_url: str,
    video_path: Path,
    file_size:  int,
    token:      str,
) -> None:
    """Phase 2: Upload binary (streaming)."""
    def file_generator(
        file_obj,
        chunk_size=8 * 1024 * 1024,
    ):
        """Generator for streaming upload."""
        while True:
            chunk = file_obj.read(chunk_size)
            if not chunk:
                break
            yield chunk

    with open(str(video_path), "rb") as f:
        r = requests.post(
            upload_url,
            headers = {
                "Authorization": f"OAuth {token}",
                "offset":        "0",
                "file_size":     str(file_size),
                "Content-Type":  "video/mp4",
            },
            data    = file_generator(f),
            timeout = UPLOAD_TIMEOUT,
        )
    r.raise_for_status()

    # Check response body for errors
    try:
        body = r.json()
        if (
            isinstance(body, dict) and
            "error" in body
        ):
            raise RuntimeError(
                f"Reel upload error: {body['error']}"
            )
    except ValueError:
        pass  # Not JSON — acceptable


def _finish_reel_upload(
    page_id:     str,
    token:       str,
    video_id:    str,
    title:       str,
    description: str,
) -> dict:
    """Phase 3: Finalize and publish Reel."""
    video_state = os.environ.get(
        "FB_VIDEO_STATE", "PUBLISHED"
    )

    r = requests.post(
        f"{GRAPH_API}/{page_id}/video_reels",
        data = {
            "upload_phase": "finish",
            "video_id":     video_id,
            "access_token": token,
            "title":        title[:MAX_TITLE_LEN],
            "description":  description,
            "video_state":  video_state,
        },
        timeout = PUBLISH_TIMEOUT,
    )
    r.raise_for_status()
    return r.json()


def _upload_as_reel(
    video_path:     str,
    title:          str,
    description:    str,
    page_id:        str,
    token:          str,
    info:           VideoInfo,
    thumbnail_path: str = "",
) -> dict:
    """نشر فيديو كـ Reel (3 phases + thumbnail)."""
    path = Path(video_path).resolve()

    log.info(
        "     [1/3] Initializing Reel upload "
        "(%.1f MB, %.1fs)...",
        info.size_mb, info.duration
    )

    # Phase 1: Initialize
    video_id, upload_url = _init_reel_upload(
        page_id, token
    )

    log.info(
        "     [2/3] Uploading binary "
        "(video_id=%s)...",
        video_id
    )

    # Phase 2: Upload (streaming)
    _upload_reel_binary(
        upload_url, path,
        info.size_bytes, token,
    )

    log.info("     [3/3] Publishing Reel...")

    # Phase 3: Finish
    result  = _finish_reel_upload(
        page_id, token, video_id,
        title, description,
    )
    post_id = result.get("id", video_id)

    log.info(
        "  ✅ Reel published → ID: %s", post_id
    )

    # Phase 4 (optional): Upload thumbnail
    if thumbnail_path and video_id:
        thumb_ok = _upload_reel_thumbnail(
            video_id       = video_id,
            thumbnail_path = thumbnail_path,
            page_id        = page_id,
            token          = token,
        )
        if not thumb_ok:
            log.warning(
                "  ⚠️  Reel published but "
                "thumbnail upload failed"
            )

    return result


# ═════════════════════════════════════════════════════════════════════════════
# UPLOAD AS REGULAR VIDEO (Long) — with Thumbnail
# ═════════════════════════════════════════════════════════════════════════════

def _upload_as_video(
    video_path:     str,
    title:          str,
    description:    str,
    page_id:        str,
    token:          str,
    info:           VideoInfo,
    thumbnail_path: str = "",
) -> dict:
    """نشر فيديو عادي مع thumbnail اختياري."""
    path = Path(video_path).resolve()

    log.info(
        "  📤 Uploading as Video "
        "(%.1f MB, %.1fs)...",
        info.size_mb, info.duration
    )

    # Validate + prepare thumbnail
    thumb_path = (
        _validate_thumbnail(thumbnail_path)
        if thumbnail_path
        else None
    )

    # Build form data
    data = {
        "title":        title[:MAX_TITLE_LEN],
        "description":  description,
        "access_token": token,
    }

    # Open files with proper cleanup
    video_file = open(str(path), "rb")
    thumb_file = None

    try:
        files = {
            "source": (
                path.name,
                video_file,
                "video/mp4",
            ),
        }

        if thumb_path:
            thumb_mime = _get_thumbnail_mime(
                thumb_path
            )
            thumb_file = open(
                str(thumb_path), "rb"
            )
            files["thumb"] = (
                thumb_path.name,
                thumb_file,
                thumb_mime,
            )
            log.info(
                "  🖼️  Including thumbnail: %s",
                thumb_path.name
            )

        r = requests.post(
            f"{GRAPH_API}/{page_id}/videos",
            data    = data,
            files   = files,
            timeout = UPLOAD_TIMEOUT,
        )
        r.raise_for_status()

        result  = r.json()
        post_id = result.get("id", "")

        log.info(
            "  ✅ Video published → ID: %s%s",
            post_id,
            " (with thumbnail)" if thumb_path else ""
        )
        return result

    finally:
        # Always close file handles
        video_file.close()
        if thumb_file:
            thumb_file.close()


# ═════════════════════════════════════════════════════════════════════════════
# ERROR HANDLING
# ═════════════════════════════════════════════════════════════════════════════

def _parse_facebook_error(
    e: requests.exceptions.HTTPError,
) -> tuple[int, str]:
    """تحليل خطأ Facebook."""
    try:
        err_json = e.response.json()
    except Exception:
        return 0, str(e)

    error = err_json.get("error", {})
    code  = error.get("code",    0)
    msg   = error.get("message", str(e))

    return code, msg


def _is_token_expired(error_code: int) -> bool:
    """التحقق من انتهاء التوكن."""
    return error_code in TOKEN_EXPIRED_CODES


def _calc_retry_wait(attempt: int) -> int:
    """حساب وقت الانتظار (exponential)."""
    return min(
        RETRY_BASE_WAIT * (2 ** attempt),
        RETRY_MAX_WAIT,
    )


# ═════════════════════════════════════════════════════════════════════════════
# UPLOAD DISPATCHER
# ═════════════════════════════════════════════════════════════════════════════

def _upload(
    upload_type:    str,
    video_path:     str,
    title:          str,
    description:    str,
    page_id:        str,
    token:          str,
    info:           VideoInfo,
    thumbnail_path: str = "",
) -> dict:
    """Dispatcher based on upload type."""
    if upload_type == UploadType.REEL:
        return _upload_as_reel(
            video_path, title, description,
            page_id, token, info,
            thumbnail_path,
        )
    return _upload_as_video(
        video_path, title, description,
        page_id, token, info,
        thumbnail_path,
    )


def _print_publish_header(
    title:          str,
    upload_type:    str,
    content_mode:   str,
    lang:           str,
    desc_length:    int,
    thumbnail_path: str = "",
) -> None:
    """طباعة header النشر."""
    type_label = (
        "Reel ⚡"
        if upload_type == UploadType.REEL
        else "Video 🎬"
    )

    log.info("\n  📘 Publishing to Facebook...")
    log.info("     Title  : %s", title[:60])
    log.info(
        "     Type   : %s [%s]",
        type_label, content_mode.upper()
    )
    log.info("     Lang   : %s", lang.upper())
    log.info("     Caption: %d chars", desc_length)

    if thumbnail_path:
        log.info(
            "     Thumb  : %s",
            Path(thumbnail_path).name
        )
    else:
        log.info("     Thumb  : (none)")


# ═════════════════════════════════════════════════════════════════════════════
# MAIN PUBLISH FUNCTION
# ═════════════════════════════════════════════════════════════════════════════

def publish_to_facebook(
    video_path:     str,
    record:         dict,
    lang:           str  = "ar",
    as_reel:        bool = True,    # ⚠️ DEPRECATED
    retries:        int  = DEFAULT_RETRIES,
    ai_caption:     str  = "",
    content_mode:   str  = "short",
    thumbnail_path: str  = "",
) -> dict:
    """
    نشر فيديو على Facebook Page مع thumbnail.

    Args:
        video_path:     مسار الفيديو
        record:         dict مع title, number, ...
        lang:           ar | fr | en
        as_reel:        ⚠️ DEPRECATED — content_mode يقرر
        retries:        عدد المحاولات
        ai_caption:     وصف AI (اختياري)
        content_mode:   short → Reel | long → Video
        thumbnail_path: مسار الـ thumbnail (اختياري)

    Returns:
        dict مع Facebook response

    Raises:
        FileNotFoundError: إذا الفيديو غير موجود
        RuntimeError:      عند فشل النشر
    """
    # 1) Validate video file
    path = Path(video_path).resolve()
    if not path.exists():
        raise FileNotFoundError(
            f"Video not found: {path}"
        )

    # 2) Per-language credentials
    creds = _get_creds(lang)

    # 3) Build metadata
    title = record.get("title", "")[:MAX_TITLE_LEN]
    description = build_caption(
        record, lang=lang, ai_caption=ai_caption
    )

    # 4) Upload type (content_mode decides)
    upload_type = (
        UploadType.REEL
        if content_mode == "short"
        else UploadType.VIDEO
    )

    # 5) Validate video once (not per retry)
    is_reel_type = (
        upload_type == UploadType.REEL
    )
    info = _validate_video(
        str(path), as_reel=is_reel_type
    )

    # 6) Display header
    _print_publish_header(
        title, upload_type, content_mode,
        lang, len(description), thumbnail_path,
    )

    # 7) Retry loop
    last_error          = None
    current_upload_type = upload_type
    current_info        = info

    for attempt in range(retries):
        try:
            return _upload(
                upload_type    = current_upload_type,
                video_path     = str(path),
                title          = title,
                description    = description,
                page_id        = creds.page_id,
                token          = creds.token,
                info           = current_info,
                thumbnail_path = thumbnail_path,
            )

        except requests.exceptions.HTTPError as e:
            err_code, err_msg = (
                _parse_facebook_error(e)
            )
            last_error = err_msg

            log.warning(
                "  ⚠️  Facebook error "
                "(code=%d): %s",
                err_code, err_msg[:100]
            )

            # Token expired → fail immediately
            if _is_token_expired(err_code):
                raise RuntimeError(
                    f"Facebook token expired "
                    f"(code={err_code}). "
                    f"Please refresh "
                    f"FB_PAGE_TOKEN_"
                    f"{lang.upper()}."
                )

            # Reel failed → switch to Video
            if (
                current_upload_type
                == UploadType.REEL
            ):
                log.info(
                    "  ↩️  Reel failed — "
                    "switching to regular video..."
                )
                current_upload_type = (
                    UploadType.VIDEO
                )
                # Re-validate for Video
                try:
                    current_info = (
                        _validate_video(
                            str(path),
                            as_reel=False,
                        )
                    )
                except ValueError as ve:
                    raise RuntimeError(
                        f"Cannot fall back to "
                        f"video: {ve}"
                    )
                continue

            # Retry other errors
            if attempt < retries - 1:
                wait = _calc_retry_wait(attempt)
                log.info(
                    "  ↩️  Retrying in %ds...",
                    wait
                )
                time.sleep(wait)

        except requests.exceptions.Timeout:
            last_error = "Upload timed out"
            log.warning(
                "  ⚠️  Timeout [%d/%d]",
                attempt + 1, retries
            )
            if attempt < retries - 1:
                time.sleep(TIMEOUT_RETRY_WAIT)

        except (FileNotFoundError, ValueError) as e:
            raise RuntimeError(
                f"Video validation failed: {e}"
            )

        except Exception as e:
            last_error = str(e)
            log.warning(
                "  ⚠️  Error [%d/%d]: %s",
                attempt + 1, retries,
                str(e)[:150]
            )
            if attempt < retries - 1:
                time.sleep(GENERIC_ERROR_WAIT)

    raise RuntimeError(
        f"Facebook publish failed after "
        f"{retries} attempts: {last_error}"
    )

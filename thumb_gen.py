"""
🖼️ Thumbnail HTML Generator

Features:
  ✅ Short: 1080×1920 (9:16 Reels/Shorts)
  ✅ Long:  1280×720  (16:9 YouTube)
  ✅ Background from Pexels Photos API (10 keys support)
  ✅ Fallback: frame extraction from raw video
  ✅ Auto title balancing (2 lines)
  ✅ Dynamic font sizing
  ✅ Multi-language support (AR, FR, EN)
"""

from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import requests

# ═════════════════════════════════════════════════════════════════════════════
# CONSTANTS
# ═════════════════════════════════════════════════════════════════════════════

# API
PEXELS_PHOTOS_URL  = "https://api.pexels.com/v1/search"
API_TIMEOUT        = 15
DOWNLOAD_TIMEOUT   = 30
PHOTOS_PER_PAGE    = 5

# Pexels keys support (matches video_sources.py)
MAX_PEXELS_KEYS = 10

# FFmpeg
FFMPEG_TIMEOUT    = 15
MIN_FRAME_BYTES   = 1000

# Title splitting
TITLE_SEARCH_RANGE = 2  # نطاق البحث حول المنتصف
TITLE_MAX_DIFF     = 10 ** 9

# Logging
logging.basicConfig(
    level  = logging.INFO,
    format = "%(message)s",
)
log = logging.getLogger(__name__)


# ═════════════════════════════════════════════════════════════════════════════
# DATA CLASSES
# ═════════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class Dimensions:
    """أبعاد thumbnail."""
    width:       int
    height:      int
    orientation: str


# مقاسات حسب content_mode
DIMENSIONS_MAP: dict[str, Dimensions] = {
    "short": Dimensions(1080, 1920, "portrait"),
    "long":  Dimensions(1280, 720,  "landscape"),
}


@dataclass(frozen=True)
class LangConfig:
    """إعدادات اللغة."""
    direction: str   # rtl | ltr
    code:      str   # ar | fr | en
    font:      str
    align:     str


_LANG_CONFIGS: dict[str, LangConfig] = {
    "ar": LangConfig(
        direction = "rtl",
        code      = "ar",
        font      = "'Noto Naskh Arabic', 'Amiri', serif",
        align     = "center",
    ),
    "fr": LangConfig(
        direction = "ltr",
        code      = "fr",
        font      = "'Noto Sans', 'DejaVu Sans', sans-serif",
        align     = "center",
    ),
    "en": LangConfig(
        direction = "ltr",
        code      = "en",
        font      = "'Noto Sans', 'DejaVu Sans', sans-serif",
        align     = "center",
    ),
}


# ═════════════════════════════════════════════════════════════════════════════
# HELPERS
# ═════════════════════════════════════════════════════════════════════════════

def _get_dimensions(content_mode: str) -> Dimensions:
    """جلب الأبعاد حسب content_mode."""
    return DIMENSIONS_MAP.get(content_mode, DIMENSIONS_MAP["short"])


def _get_lang_config(lang: str) -> LangConfig:
    """جلب إعدادات اللغة."""
    return _LANG_CONFIGS.get(lang, _LANG_CONFIGS["en"])


def _escape_html(s: str) -> str:
    """HTML escape آمن."""
    return (
        s.replace("&", "&amp;")
         .replace("<", "&lt;")
         .replace(">", "&gt;")
         .replace('"', "&quot;")
         .replace("'", "&#039;")
    )


# ═════════════════════════════════════════════════════════════════════════════
# TITLE SPLITTING
# ═════════════════════════════════════════════════════════════════════════════

def _split_title_two_lines(title: str) -> str:
    """
    تقسيم العنوان إلى سطرين متوازنين قدر الإمكان.

    Examples:
        "Hello World"           → "Hello\nWorld"
        "One Two Three Four"    → "One Two\nThree Four"
        "Short"                 → "Short"
    """
    words = title.strip().split()

    # كلمة واحدة أو أقل
    if len(words) <= 1:
        return title.strip()

    # كلمتان
    if len(words) == 2:
        return f"{words[0]}\n{words[1]}"

    # ثلاث كلمات أو أكثر: البحث عن أفضل تقسيم
    mid       = len(words) // 2
    best_idx  = mid
    best_diff = TITLE_MAX_DIFF

    # البحث حول المنتصف
    start = max(1, mid - TITLE_SEARCH_RANGE)
    end   = min(len(words), mid + TITLE_SEARCH_RANGE + 1)

    for i in range(start, end):
        left_text  = " ".join(words[:i])
        right_text = " ".join(words[i:])
        diff       = abs(len(left_text) - len(right_text))

        if diff < best_diff:
            best_diff = diff
            best_idx  = i

    line1 = " ".join(words[:best_idx]).strip()
    line2 = " ".join(words[best_idx:]).strip()

    if not line2:
        return line1

    return f"{line1}\n{line2}"


# ═════════════════════════════════════════════════════════════════════════════
# PEXELS PHOTO FETCH
# ═════════════════════════════════════════════════════════════════════════════

def _get_pexels_key() -> str:
    """
    جلب أول مفتاح Pexels متوفر.

    يدعم حتى MAX_PEXELS_KEYS مفاتيح:
        PEXELS_API_KEY
        PEXELS_API_KEY_1
        PEXELS_API_KEY_2
        ...
        PEXELS_API_KEY_{MAX_PEXELS_KEYS}
    """
    # المفتاح الرئيسي
    main_key = os.environ.get("PEXELS_API_KEY", "").strip()
    if main_key:
        return main_key

    # المفاتيح الإضافية
    for i in range(1, MAX_PEXELS_KEYS + 1):
        key = os.environ.get(
            f"PEXELS_API_KEY_{i}", ""
        ).strip()
        if key:
            return key

    return ""


def _select_photo_url(
    photo:       dict,
    orientation: str,
) -> str:
    """اختيار أفضل URL من صورة Pexels."""
    src = photo.get("src", {})

    if orientation == "portrait":
        # ترتيب الأولوية للـ portrait
        for size in ("portrait", "large2x", "large"):
            url = src.get(size, "")
            if url:
                return url

    else:  # landscape
        for size in ("landscape", "large2x", "large"):
            url = src.get(size, "")
            if url:
                return url

    return ""


def _download_image(
    url:         str,
    output_path: str,
) -> bool:
    """تحميل صورة من URL."""
    try:
        r = requests.get(url, timeout=DOWNLOAD_TIMEOUT)
        r.raise_for_status()

        Path(output_path).write_bytes(r.content)
        return True

    except Exception as e:
        log.warning(f"  ⚠️  Download failed: {e}")
        return False


def _fetch_pexels_photo(
    keyword:     str,
    output_path: str,
    orientation: str = "portrait",
) -> bool:
    """
    جلب صورة من Pexels Photos API.

    Returns:
        True إذا نجح
    """
    api_key = _get_pexels_key()
    if not api_key:
        return False

    try:
        r = requests.get(
            PEXELS_PHOTOS_URL,
            headers = {"Authorization": api_key},
            params  = {
                "query":       keyword,
                "per_page":    PHOTOS_PER_PAGE,
                "orientation": orientation,
            },
            timeout = API_TIMEOUT,
        )
        r.raise_for_status()

        photos = r.json().get("photos", [])
        if not photos:
            return False

        # اختيار أول صورة + أفضل URL
        photo = photos[0]
        img_url = _select_photo_url(photo, orientation)

        if not img_url:
            return False

        # تحميل
        if not _download_image(img_url, output_path):
            return False

        log.info(
            f"  🖼️  Pexels photo ({orientation}): "
            f"{keyword!r} → {Path(output_path).name}"
        )
        return True

    except Exception as e:
        log.warning(f"  ⚠️  Pexels photo failed: {e}")
        return False


# ═════════════════════════════════════════════════════════════════════════════
# FRAME EXTRACTION
# ═════════════════════════════════════════════════════════════════════════════

def _extract_frame_from_video(
    video_path:  str,
    output_path: str,
    width:       int,
    height:      int,
) -> bool:
    """
    استخراج frame من فيديو بـ ffmpeg.

    Returns:
        True إذا نجح
    """
    video = Path(video_path)
    if not video.exists():
        return False

    try:
        r = subprocess.run(
            [
                "ffmpeg", "-y",
                "-ss", "2",                    # بعد ثانيتين
                "-i", str(video),
                "-vframes", "1",
                "-vf",
                f"scale={width}:{height}:"
                f"force_original_aspect_ratio=increase,"
                f"crop={width}:{height}",
                "-q:v", "2",                   # جودة عالية
                output_path,
            ],
            capture_output = True,
            text           = True,
            timeout        = FFMPEG_TIMEOUT,
        )

        if r.returncode != 0:
            return False

        # التحقق من الحجم
        output = Path(output_path)
        if not output.exists():
            return False

        if output.stat().st_size < MIN_FRAME_BYTES:
            return False

        log.info(f"  🖼️  Frame extracted: {video.name}")
        return True

    except subprocess.TimeoutExpired:
        log.warning("  ⚠️  Frame extraction timeout")
        return False

    except Exception as e:
        log.warning(f"  ⚠️  Frame extraction failed: {e}")
        return False


# ═════════════════════════════════════════════════════════════════════════════
# BACKGROUND IMAGE
# ═════════════════════════════════════════════════════════════════════════════

def _get_background_image(
    keyword:     str,
    video_paths: Optional[list[str]],
    tmp_dir:     str,
    dims:        Dimensions,
) -> Optional[str]:
    """
    جلب صورة الخلفية بالأولوية:
        1. Pexels Photos API
        2. Frame من أول فيديو خام
        3. None (خلفية سوداء)
    """
    tmp_path = Path(tmp_dir)

    # 1) Pexels Photos
    pexels_img = str(tmp_path / "thumb_bg.jpg")
    if _fetch_pexels_photo(keyword, pexels_img, dims.orientation):
        return pexels_img

    # 2) Frame من فيديو
    if video_paths:
        for vp in video_paths:
            if not vp:
                continue

            if not Path(str(vp)).exists():
                continue

            frame_img = str(tmp_path / "thumb_frame.jpg")
            if _extract_frame_from_video(
                str(vp), frame_img,
                dims.width, dims.height,
            ):
                return frame_img

    log.warning("  ⚠️  No background image — using black")
    return None


# ═════════════════════════════════════════════════════════════════════════════
# FONT SIZE
# ═════════════════════════════════════════════════════════════════════════════

# جداول أحجام الخطوط
_FONT_SIZES_SHORT = [
    (10, 138, 126),  # (max_length, AR, EN)
    (15, 120, 110),
    (22, 104, 94),
    (30, 88,  80),
    (38, 76,  68),
    (99, 64,  58),
]

_FONT_SIZES_LONG = [
    (10, 96, 88),
    (15, 82, 74),
    (22, 70, 62),
    (30, 60, 54),
    (38, 52, 46),
    (99, 44, 40),
]


def _get_font_size(
    title:        str,
    lang:         str,
    content_mode: str = "short",
) -> str:
    """
    حساب حجم الخط حسب طول العنوان واللغة.

    Returns:
        font-size كـ string (مثل "120px")
    """
    lines     = title.split("\n")
    max_chars = max(len(line) for line in lines)
    is_ar     = (lang == "ar")

    # اختيار الجدول المناسب
    table = (
        _FONT_SIZES_LONG
        if content_mode == "long"
        else _FONT_SIZES_SHORT
    )

    # البحث عن الحجم المناسب
    for max_length, ar_size, en_size in table:
        if max_chars <= max_length:
            size = ar_size if is_ar else en_size
            return f"{size}px"

    # fallback (لن يحدث عادة)
    return "60px" if is_ar else "54px"


# ═════════════════════════════════════════════════════════════════════════════
# HTML TEMPLATES
# ═════════════════════════════════════════════════════════════════════════════

def _build_bg_style(bg_image_path: Optional[str]) -> str:
    """بناء CSS للخلفية."""
    if bg_image_path and Path(bg_image_path).exists():
        bg_abs = Path(bg_image_path).resolve()
        return (
            f"background-image: url('file://{bg_abs}'); "
            f"background-size: cover; "
            f"background-position: center;"
        )
    return "background: #000000;"


def _build_bg_style_long(bg_image_path: Optional[str]) -> str:
    """بناء CSS للخلفية (Long: خلفية رمادية داكنة)."""
    if bg_image_path and Path(bg_image_path).exists():
        bg_abs = Path(bg_image_path).resolve()
        return (
            f"background-image: url('file://{bg_abs}'); "
            f"background-size: cover; "
            f"background-position: center;"
        )
    return "background: #0a0a0a;"


def _generate_html_short(
    title:         str,
    lang:          str,
    bg_image_path: Optional[str],
    output_path:   str,
) -> Path:
    """
    توليد HTML للـ Short thumbnail (1080×1920).

    Layout:
        - خلفية + overlay 80% + vignette
        - العنوان في الوسط
        - خط أحمر تحت العنوان
    """
    title_lines = _split_title_two_lines(title)
    config      = _get_lang_config(lang)
    font_size   = _get_font_size(title_lines, lang, "short")
    title_html  = _escape_html(title_lines).replace("\n", "<br>")
    bg_style    = _build_bg_style(bg_image_path)

    html = f"""<!DOCTYPE html>
<html lang="{config.code}">
<head>
  <meta charset="UTF-8"/>
  <style>
    * {{ margin:0; padding:0; box-sizing:border-box; }}
    html, body {{
      width:1080px; height:1920px; overflow:hidden;
    }}
    .bg {{ position:absolute; inset:0; {bg_style} }}
    .overlay {{
      position:absolute; inset:0;
      background:rgba(0,0,0,0.80);
    }}
    .vignette {{
      position:absolute; inset:0;
      background:radial-gradient(
        ellipse at center,
        transparent 25%,
        rgba(0,0,0,0.65) 100%
      );
    }}
    .content {{
      position:absolute; inset:0;
      display:flex; flex-direction:column;
      align-items:center; justify-content:center;
      padding:80px 70px;
      direction:{config.direction};
      text-align:{config.align};
    }}
    .title {{
      font-family:{config.font};
      font-size:{font_size};
      font-weight:900;
      color:#FFFFFF;
      line-height:1.22;
      word-break:break-word;
      direction:{config.direction};
      text-align:{config.align};
      text-shadow:
        0 0 50px rgba(255,255,255,0.12),
        0 4px 30px rgba(0,0,0,0.95),
        2px 2px 0 rgba(0,0,0,0.85),
        -2px -2px 0 rgba(0,0,0,0.85);
      -webkit-text-stroke:1.5px rgba(0,0,0,0.6);
      paint-order:stroke fill;
      max-width:920px;
    }}
    .line {{
      width:180px; height:5px;
      border-radius:3px;
      margin-top:44px;
      background:linear-gradient(
        90deg,
        transparent,
        #FF1744 30%,
        #FF1744 70%,
        transparent
      );
      flex-shrink:0;
    }}
  </style>
</head>
<body>
  <div class="bg"></div>
  <div class="overlay"></div>
  <div class="vignette"></div>
  <div class="content">
    <div class="title">{title_html}</div>
    <div class="line"></div>
  </div>
</body>
</html>"""

    path = Path(output_path).resolve()
    path.write_text(html, encoding="utf-8")
    return path


def _generate_html_long(
    title:         str,
    lang:          str,
    bg_image_path: Optional[str],
    output_path:   str,
) -> Path:
    """
    توليد HTML للـ Long thumbnail (1280×720).

    Layout:
        - خلفية + side gradient + bottom gradient + vignette
        - العنوان في الزاوية السفلية
        - خط أحمر فوق وتحت العنوان
    """
    title_lines = _split_title_two_lines(title)
    config      = _get_lang_config(lang)
    font_size   = _get_font_size(title_lines, lang, "long")
    title_html  = _escape_html(title_lines).replace("\n", "<br>")
    bg_style    = _build_bg_style_long(bg_image_path)
    is_ar       = (lang == "ar")

    # متغيرات حسب الاتجاه
    text_side  = "right:0; left:0;" if is_ar else "left:0; right:0;"
    text_align = "right"            if is_ar else "left"
    text_dir   = "rtl"              if is_ar else "ltr"

    side_gradient = (
        "linear-gradient(to left, "
        "rgba(0,0,0,0.92) 0%, "
        "rgba(0,0,0,0.7) 40%, "
        "transparent 70%)"
        if is_ar else
        "linear-gradient(to right, "
        "rgba(0,0,0,0.92) 0%, "
        "rgba(0,0,0,0.7) 40%, "
        "transparent 70%)"
    )

    accent_margin = (
        "margin-right:auto;"
        if is_ar
        else "margin-left:0;"
    )

    bottom_line_direction = (
        "to left" if is_ar else "to right"
    )

    bottom_line_margin = (
        "margin-right:0; margin-left:auto;"
        if is_ar
        else "margin-left:0;"
    )

    html = f"""<!DOCTYPE html>
<html lang="{config.code}">
<head>
  <meta charset="UTF-8"/>
  <style>
    * {{ margin:0; padding:0; box-sizing:border-box; }}
    html, body {{
      width:1280px; height:720px; overflow:hidden;
    }}
    .bg {{ position:absolute; inset:0; {bg_style} }}
    .side-gradient {{
      position:absolute; inset:0;
      background:{side_gradient};
    }}
    .bottom-gradient {{
      position:absolute; bottom:0; left:0; right:0;
      height:50%;
      background:linear-gradient(
        to top,
        rgba(0,0,0,0.85) 0%,
        transparent 100%
      );
    }}
    .vignette {{
      position:absolute; inset:0;
      background:radial-gradient(
        ellipse at center,
        transparent 30%,
        rgba(0,0,0,0.5) 100%
      );
    }}
    .content {{
      position:absolute;
      bottom:48px;
      {text_side}
      padding:0 56px;
      direction:{text_dir};
      text-align:{text_align};
      max-width:680px;
    }}
    .accent-line {{
      width:60px; height:4px;
      border-radius:2px;
      background:#FF1744;
      margin-bottom:16px;
      {accent_margin}
    }}
    .title {{
      font-family:{config.font};
      font-size:{font_size};
      font-weight:900;
      color:#FFFFFF;
      line-height:1.25;
      word-break:break-word;
      direction:{text_dir};
      text-align:{text_align};
      text-shadow:
        0 0 40px rgba(255,255,255,0.1),
        0 3px 20px rgba(0,0,0,0.95),
        2px 2px 0 rgba(0,0,0,0.8);
      -webkit-text-stroke:1px rgba(0,0,0,0.6);
      paint-order:stroke fill;
    }}
    .bottom-line {{
      width:100px; height:3px;
      border-radius:2px;
      background:linear-gradient(
        {bottom_line_direction},
        #FF1744, transparent
      );
      margin-top:16px;
      {bottom_line_margin}
    }}
  </style>
</head>
<body>
  <div class="bg"></div>
  <div class="side-gradient"></div>
  <div class="bottom-gradient"></div>
  <div class="vignette"></div>
  <div class="content">
    <div class="accent-line"></div>
    <div class="title">{title_html}</div>
    <div class="bottom-line"></div>
  </div>
</body>
</html>"""

    path = Path(output_path).resolve()
    path.write_text(html, encoding="utf-8")
    return path


# ═════════════════════════════════════════════════════════════════════════════
# PUBLIC API
# ═════════════════════════════════════════════════════════════════════════════

def generate_thumbnail_html(
    title:        str,
    lang:         str                  = "ar",
    output_path:  str                  = "thumbnail.html",
    hook:         str                  = "",
    tone:         str                  = "energetic",
    keyword:      str                  = "",
    video_paths:  Optional[list]       = None,
    content_mode: str                  = "short",
) -> Path:
    """
    توليد ملف HTML للـ thumbnail.

    Args:
        title:        عنوان الفيديو
        lang:         اللغة (ar, fr, en)
        output_path:  مسار ملف HTML الناتج
        hook:         غير مستخدم (للتوافق الخلفي)
        tone:         غير مستخدم (للتوافق الخلفي)
        keyword:      كلمة البحث للصورة
        video_paths:  قائمة الفيديوهات للـ fallback
        content_mode: short | long

    Returns:
        Path للملف الناتج
    """
    out_path = Path(output_path).resolve()
    tmp_dir  = str(out_path.parent)

    # الأبعاد
    dims = _get_dimensions(content_mode)

    # كلمة البحث
    search_kw = keyword.strip() if keyword else title.strip()

    log.info(
        f"  🖼️  Generating thumbnail "
        f"[{content_mode.upper()}] "
        f"({dims.width}×{dims.height})..."
    )

    # جلب صورة الخلفية
    video_paths_str = (
        [str(p) for p in video_paths]
        if video_paths
        else None
    )

    bg_image = _get_background_image(
        keyword     = search_kw,
        video_paths = video_paths_str,
        tmp_dir     = tmp_dir,
        dims        = dims,
    )

    # توليد HTML حسب content_mode
    if content_mode == "long":
        result = _generate_html_long(
            title         = title,
            lang          = lang,
            bg_image_path = bg_image,
            output_path   = str(out_path),
        )
    else:
        result = _generate_html_short(
            title         = title,
            lang          = lang,
            bg_image_path = bg_image,
            output_path   = str(out_path),
        )

    log.info(
        f"  🖼️  Thumbnail HTML "
        f"[{content_mode.upper()}] → {out_path.name}"
    )

    return result

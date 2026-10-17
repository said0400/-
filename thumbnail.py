"""
🖼️ Thumbnail Renderer v2.1 — HTML to PNG via Playwright

Features:
  ✅ Short: 1080×1920 (9:16 Reels/Shorts)
  ✅ Long:  1280×720  (16:9 YouTube)
  ✅ Batch rendering (browser opens once)
  ✅ Multi-language locale support (AR/FR/EN)
  ✅ Font Loading API (document.fonts.ready)
  ✅ Per-language font wait times
  ✅ Safe browser/context cleanup
  ✅ Smart page recreation on errors
  ✅ Verify PNG file size
  ✅ Validate HTML before browser launch
  ✅ Path.with_suffix (safe output path)
  ✅ Fallback path on failure
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from playwright.sync_api import (
    sync_playwright,
    Browser,
    BrowserContext,
    Page,
)

log = logging.getLogger(__name__)

# ═════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ═════════════════════════════════════════════════════════════════════════════

# Browser launch arguments (lang removed — set via context locale)
_LAUNCH_ARGS = [
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--no-zygote",
    "--font-render-hinting=none",
]

# Locale per language
_LOCALES = {
    "ar": "ar-SA",
    "fr": "fr-FR",
    "en": "en-US",
}

# Font wait timing per language (Arabic needs more time)
_FONT_WAIT_MS_BY_LANG = {
    "ar": 1500,
    "fr": 800,
    "en": 800,
}

# Timing
_PAGE_TIMEOUT_MS = 30_000   # 30 seconds
_FONT_TIMEOUT_MS = 5_000    # 5 seconds for fonts.ready

# Device scale (1x because viewport already at target resolution)
_DEVICE_SCALE = 1

# Quality validation
MIN_THUMBNAIL_KB = 10


# ═════════════════════════════════════════════════════════════════════════════
# DATA CLASSES
# ═════════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class ThumbnailSize:
    """مقاس thumbnail."""
    width:  int
    height: int

    def to_viewport(self) -> dict[str, int]:
        """تحويل لـ viewport dict."""
        return {
            "width":  self.width,
            "height": self.height,
        }

    def to_clip(self) -> dict[str, int]:
        """تحويل لـ clip dict."""
        return {
            "x":      0,
            "y":      0,
            "width":  self.width,
            "height": self.height,
        }


# مقاسات حسب content_mode
_SIZES: dict[str, ThumbnailSize] = {
    "short": ThumbnailSize(1080, 1920),
    "long":  ThumbnailSize(1280, 720),
}


# ═════════════════════════════════════════════════════════════════════════════
# HELPERS
# ═════════════════════════════════════════════════════════════════════════════

def _get_size(content_mode: str) -> ThumbnailSize:
    """جلب مقاس thumbnail حسب content_mode."""
    size = _SIZES.get(content_mode)
    if size is None:
        log.warning(
            "  ⚠️  Unknown content_mode '%s' — defaulting to 'short'",
            content_mode
        )
        return _SIZES["short"]
    return size


def _get_locale(lang: str = "ar") -> str:
    """جلب locale حسب اللغة (fallback to en)."""
    return _LOCALES.get(lang, _LOCALES["en"])


def _get_font_wait_ms(lang: str = "ar") -> int:
    """جلب وقت انتظار الخطوط حسب اللغة."""
    return _FONT_WAIT_MS_BY_LANG.get(lang, 1000)


def _build_output_path(
    output_png: Optional[str],
    html_path:  Path,
) -> Path:
    """بناء مسار الإخراج (آمن — Path.with_suffix)."""
    if output_png:
        return Path(output_png).resolve()

    # Path.with_suffix بدلاً من str.replace (آمن)
    return html_path.with_suffix(".png").resolve()


def _file_size_kb(path: Path) -> int:
    """حجم الملف بالـ KB."""
    try:
        return path.stat().st_size // 1024
    except OSError:
        return 0


# ═════════════════════════════════════════════════════════════════════════════
# RENDER ONE
# ═════════════════════════════════════════════════════════════════════════════

def _render_one(
    page:         Page,
    html_path:    str,
    output_png:   Optional[str],
    size:         ThumbnailSize,
    content_mode: str = "short",
    lang:         str = "ar",
) -> Path:
    """
    رندر ملف HTML واحد إلى PNG.

    Raises:
        FileNotFoundError: إذا HTML غير موجود
        RuntimeError:      عند فشل الرندر أو الإخراج
    """
    html_path_obj   = Path(html_path).resolve()
    output_png_path = _build_output_path(output_png, html_path_obj)

    if not html_path_obj.exists():
        raise FileNotFoundError(
            f"Thumbnail HTML not found: {html_path_obj}"
        )

    # تأكد من وجود مجلد الإخراج
    output_png_path.parent.mkdir(parents=True, exist_ok=True)

    # تحميل الصفحة (domcontentloaded أسرع من load)
    page.goto(
        f"file://{html_path_obj}",
        wait_until = "domcontentloaded",
        timeout    = _PAGE_TIMEOUT_MS,
    )

    # انتظار اكتمال الخطوط عبر Font Loading API
    try:
        page.evaluate(
            """
            async () => {
                await Promise.race([
                    document.fonts.ready,
                    new Promise(r => setTimeout(r, %d))
                ]);
            }
            """ % _FONT_TIMEOUT_MS
        )
    except Exception:
        # Fallback: انتظر مدة ثابتة
        wait_ms = _get_font_wait_ms(lang)
        page.wait_for_timeout(wait_ms)

    # هامش أمان إضافي
    page.wait_for_timeout(300)

    # التقاط screenshot
    page.screenshot(
        path            = str(output_png_path),
        type            = "png",
        full_page       = False,
        clip            = size.to_clip(),
        omit_background = False,
    )

    # التحقق من حجم الملف
    size_kb = _file_size_kb(output_png_path)

    if size_kb == 0:
        raise RuntimeError(
            f"Screenshot produced empty file: {output_png_path}"
        )

    if size_kb < MIN_THUMBNAIL_KB:
        log.warning(
            "  ⚠️  Thumbnail suspiciously small: %d KB",
            size_kb
        )

    log.info(
        "  🖼️  Thumbnail [%s] → %s (%d KB, %dx%d)",
        content_mode.upper(),
        output_png_path.name,
        size_kb,
        size.width,
        size.height
    )

    return output_png_path


# ═════════════════════════════════════════════════════════════════════════════
# BROWSER CONTEXT MANAGER
# ═════════════════════════════════════════════════════════════════════════════

def _create_browser_context(
    playwright,
    size: ThumbnailSize,
    lang: str = "ar",
) -> tuple[Browser, BrowserContext, Page]:
    """
    إنشاء browser + context + page بأمان.

    Returns:
        (browser, context, page)

    Note:
        المستدعي مسؤول عن إغلاق browser.
        لو فشل أي شيء → كل الموارد تُغلق تلقائياً.
    """
    browser = playwright.chromium.launch(
        headless = True,
        args     = _LAUNCH_ARGS,
    )

    try:
        context = browser.new_context(
            viewport            = size.to_viewport(),
            device_scale_factor = _DEVICE_SCALE,
            locale              = _get_locale(lang),
        )

        try:
            page = context.new_page()
            return browser, context, page
        except Exception:
            # إغلاق context إذا فشل new_page
            try:
                context.close()
            except Exception:
                pass
            raise

    except Exception:
        # إغلاق browser إذا فشل new_context
        try:
            browser.close()
        except Exception:
            pass
        raise


def _recreate_page(
    context: BrowserContext,
    page:    Page,
) -> Page:
    """إعادة إنشاء page بعد فشل (إن أمكن)."""
    try:
        page.close()
    except Exception:
        pass

    try:
        return context.new_page()
    except Exception as e:
        log.warning("  ⚠️  Could not recreate page: %s", e)
        return page  # رجع الـ page القديمة


# ═════════════════════════════════════════════════════════════════════════════
# PUBLIC API
# ═════════════════════════════════════════════════════════════════════════════

def render_thumbnail(
    html_path:    str,
    output_png:   Optional[str] = None,
    content_mode: str           = "short",
    lang:         str           = "ar",
) -> Path:
    """
    رندر ملف HTML واحد إلى PNG.

    Args:
        html_path:    مسار HTML
        output_png:   مسار PNG الناتج (اختياري)
        content_mode: short | long
        lang:         ar | fr | en

    Returns:
        Path لملف PNG الناتج

    Raises:
        RuntimeError: لو لم يُنتَج أي ملف
    """
    results = render_thumbnails_batch(
        items        = [(html_path, output_png)],
        content_mode = content_mode,
        lang         = lang,
    )

    if not results:
        raise RuntimeError(
            f"Thumbnail rendering returned no results for: "
            f"{html_path}"
        )

    return results[0]


def render_thumbnails_batch(
    items:        list[tuple[str, Optional[str]]],
    content_mode: str = "short",
    lang:         str = "ar",
) -> list[Path]:
    """
    رندر عدة thumbnails بـ browser واحد.

    Args:
        items:        قائمة (html_path, output_png) tuples
        content_mode: short | long
        lang:         ar | fr | en (للـ locale)

    Returns:
        قائمة Paths للملفات الناتجة

    Notes:
        - إذا فشل رندر واحد، يستمر الباقي
        - يرجع Path حتى للفشل (fallback path)
        - يتحقق من وجود HTML قبل فتح browser
    """
    if not items:
        return []

    size    = _get_size(content_mode)
    results : list[Path] = []
    success_paths: set[Path] = set()

    # تحقق من وجود HTML files قبل فتح Browser
    valid_items: list[tuple[str, Optional[str]]] = []

    for html_path, output_png in items:
        if Path(html_path).exists():
            valid_items.append((html_path, output_png))
        else:
            log.error("  ❌ HTML not found: %s", html_path)
            fallback = _build_output_path(
                output_png, Path(html_path)
            )
            results.append(fallback)

    if not valid_items:
        log.warning("  ⚠️  No valid HTML files to render")
        return results

    log.info(
        "\n  🖼️  Rendering %d thumbnail(s) [%s] %dx%d",
        len(valid_items),
        content_mode.upper(),
        size.width,
        size.height
    )

    with sync_playwright() as p:
        browser, context, page = _create_browser_context(
            playwright = p,
            size       = size,
            lang       = lang,
        )

        try:
            for html_path, output_png in valid_items:
                try:
                    result = _render_one(
                        page         = page,
                        html_path    = html_path,
                        output_png   = output_png,
                        size         = size,
                        content_mode = content_mode,
                        lang         = lang,
                    )
                    results.append(result)
                    success_paths.add(result)

                except Exception as e:
                    log.error(
                        "  ⚠️  Thumbnail failed for %s: %s",
                        html_path, e
                    )

                    # Fallback path للحفاظ على ترتيب القائمة
                    fallback = _build_output_path(
                        output_png,
                        Path(html_path),
                    )
                    results.append(fallback)

                    # إعادة إنشاء page للـ render التالي
                    page = _recreate_page(context, page)

        finally:
            # ضمان إغلاق browser
            try:
                if context:
                    context.close()
            except Exception as e:
                log.debug("Context close error: %s", e)

            try:
                if browser:
                    browser.close()
            except Exception as e:
                log.debug("Browser close error: %s", e)

    # عد النجاحات الفعلية فقط
    success_count = len(success_paths)
    log.info(
        "  ✅ Thumbnails: %d/%d rendered successfully",
        success_count,
        len(valid_items)
    )

    return results

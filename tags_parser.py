"""
🏷️ Smart Emotional Tags Parser v2.0 — Street Edition

Features:
  ✅ 22 tags total (19 original + 3 new: hook/direct/cta)
  ✅ Auto-correct misspelled tags (fuzzy matching)
  ✅ Manual mapping for synonyms and variants
  ✅ Multi-language tag names (AR, FR, EN)
  ✅ Voice configuration per tag
  ✅ Summary display with stats
  ✅ Pre-text before first tag preserved
  ✅ strip_tags_from_text() preserves spaces
  ✅ line_counter consistent even with empty segments
  ✅ Abbreviation support (info, inspire, drama, etc.)
  ✅ Direct message → direct (French fix)

Supported Tags (22 total):
    Original (19):
      [intrigue], [desire], [information], [inspiration],
      [confident], [shock], [wisdom], [urgency], [calm],
      [emotional], [pause], [whisper], [curiosity],
      [storytelling], [dramatic], [revelation], [tension],
      [climax], [powerful]

    New (3):
      [hook], [direct], [cta]
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from difflib import get_close_matches
from typing import Optional

log = logging.getLogger(__name__)

# ═════════════════════════════════════════════════════════════════════════════
# CONSTANTS
# ═════════════════════════════════════════════════════════════════════════════

DEFAULT_TAG = "information"

FUZZY_CUTOFF        = 0.65   # More conservative (was 0.6)
FUZZY_MATCHES_LIMIT = 1

_VALID_LANGS = frozenset({"ar", "fr", "en"})

# Plural words per language (for summary)
_PLURAL_MAP: dict[str, tuple[str, str]] = {
    "ar": ("جملة", "جمل"),
    "fr": ("phrase", "phrases"),
    "en": ("sentence", "sentences"),
}


# ═════════════════════════════════════════════════════════════════════════════
# DATA CLASSES
# ═════════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class TagConfig:
    """
    Tag configuration (voice + linguistic).
    
    Attributes:
        voice_rate:   0.8 = slow, 1.0 = normal, 1.2 = fast
        voice_pitch:  -5 to +5 (semitones)
        voice_volume: 0.7 to 1.15
    """
    name_ar:      str
    name_en:      str
    name_fr:      str
    voice_style:  str
    voice_rate:   float
    voice_pitch:  int
    voice_volume: float
    description:  str

    def get_name(self, lang: str = "ar") -> str:
        """Get name based on language (dict-based)."""
        lang_map = {
            "ar": self.name_ar,
            "fr": self.name_fr,
            "en": self.name_en,
        }
        return lang_map.get(lang, self.name_en)

    def to_dict(self) -> dict:
        return {
            "name_ar":      self.name_ar,
            "name_en":      self.name_en,
            "name_fr":      self.name_fr,
            "voice_style":  self.voice_style,
            "voice_rate":   self.voice_rate,
            "voice_pitch":  self.voice_pitch,
            "voice_volume": self.voice_volume,
            "description":  self.description,
        }


# ═════════════════════════════════════════════════════════════════════════════
# TAGS DEFINITIONS — 22 tags total (19 original + 3 new)
# ═════════════════════════════════════════════════════════════════════════════

_TAG_CONFIGS: dict[str, TagConfig] = {

    # ═══════════════════════════════════════════════════════════════
    # ORIGINAL TAGS (19)
    # ═══════════════════════════════════════════════════════════════

    "intrigue": TagConfig(
        name_ar="إثارة الفضول", name_en="Intrigue",
        name_fr="Intrigue", voice_style="mysterious",
        voice_rate=0.92, voice_pitch=-1, voice_volume=0.95,
        description="صوت غامض همسي يثير الفضول",
    ),
    "desire": TagConfig(
        name_ar="رغبة وطموح", name_en="Desire",
        name_fr="Désir", voice_style="warm",
        voice_rate=0.98, voice_pitch=+1, voice_volume=1.0,
        description="صوت دافئ ملهب للطموح",
    ),
    "information": TagConfig(
        name_ar="معلومة محايدة", name_en="Information",
        name_fr="Information", voice_style="clear",
        voice_rate=1.0, voice_pitch=0, voice_volume=1.0,
        description="صوت واضح معلوماتي",
    ),
    "inspiration": TagConfig(
        name_ar="إلهام", name_en="Inspiration",
        name_fr="Inspiration", voice_style="uplifting",
        voice_rate=1.05, voice_pitch=+2, voice_volume=1.05,
        description="صوت متحمس مرتفع وملهم",
    ),
    "confident": TagConfig(
        name_ar="ثقة", name_en="Confident",
        name_fr="Confiant", voice_style="bold",
        voice_rate=0.97, voice_pitch=-1, voice_volume=1.05,
        description="صوت حاسم وقوي",
    ),
    "shock": TagConfig(
        name_ar="صدمة", name_en="Shock",
        name_fr="Choc", voice_style="intense",
        voice_rate=1.1, voice_pitch=+3, voice_volume=1.1,
        description="صوت مفاجئ وقوي",
    ),
    "wisdom": TagConfig(
        name_ar="حكمة", name_en="Wisdom",
        name_fr="Sagesse", voice_style="deep",
        voice_rate=0.88, voice_pitch=-2, voice_volume=0.95,
        description="صوت عميق متأمل",
    ),
    "urgency": TagConfig(
        name_ar="عاجل", name_en="Urgency",
        name_fr="Urgence", voice_style="fast",
        voice_rate=1.08, voice_pitch=+2, voice_volume=1.10,
        description="صوت سريع وحاد",
    ),
    "calm": TagConfig(
        name_ar="هدوء", name_en="Calm",
        name_fr="Calme", voice_style="peaceful",
        voice_rate=0.90, voice_pitch=-1, voice_volume=0.9,
        description="صوت هادئ ومطمئن",
    ),
    "emotional": TagConfig(
        name_ar="عاطفي", name_en="Emotional",
        name_fr="Émotionnel", voice_style="tender",
        voice_rate=0.93, voice_pitch=0, voice_volume=0.95,
        description="صوت رقيق ومؤثر",
    ),
    "pause": TagConfig(
        name_ar="وقفة درامية", name_en="Pause",
        name_fr="Pause", voice_style="peaceful",
        voice_rate=0.82, voice_pitch=-3, voice_volume=0.75,
        description="صوت هادئ جداً وبطيء للوقفات الدرامية",
    ),
    "whisper": TagConfig(
        name_ar="همس", name_en="Whisper",
        name_fr="Chuchotement", voice_style="mysterious",
        voice_rate=0.88, voice_pitch=-3, voice_volume=0.7,
        description="صوت همس غامض وسري",
    ),
    "curiosity": TagConfig(
        name_ar="فضول", name_en="Curiosity",
        name_fr="Curiosité", voice_style="mysterious",
        voice_rate=0.95, voice_pitch=+1, voice_volume=0.92,
        description="صوت يثير التساؤل والفضول العميق",
    ),
    "storytelling": TagConfig(
        name_ar="سرد قصة", name_en="Storytelling",
        name_fr="Récit", voice_style="clear",
        voice_rate=0.98, voice_pitch=0, voice_volume=1.0,
        description="صوت سردي مريح وواضح لرواية القصص",
    ),
    "dramatic": TagConfig(
        name_ar="درامي", name_en="Dramatic",
        name_fr="Dramatique", voice_style="deep",
        voice_rate=0.86, voice_pitch=-2, voice_volume=1.08,
        description="صوت عميق ومسرحي قوي",
    ),
    "revelation": TagConfig(
        name_ar="كشف حقيقة", name_en="Revelation",
        name_fr="Révélation", voice_style="intense",
        voice_rate=1.02, voice_pitch=+2, voice_volume=1.1,
        description="صوت صادم قوي لكشف الحقيقة",
    ),
    "tension": TagConfig(
        name_ar="توتر", name_en="Tension",
        name_fr="Tension", voice_style="fast",
        voice_rate=1.08, voice_pitch=+1, voice_volume=1.0,
        description="صوت متسارع يوحي بالتوتر المتصاعد",
    ),
    "climax": TagConfig(
        name_ar="ذروة", name_en="Climax",
        name_fr="Apogée", voice_style="bold",
        voice_rate=1.05, voice_pitch=+3, voice_volume=1.15,
        description="أقوى نقطة صوتية — ذروة القصة",
    ),
    "powerful": TagConfig(
        name_ar="قوي", name_en="Powerful",
        name_fr="Puissant", voice_style="bold",
        voice_rate=0.94, voice_pitch=-1, voice_volume=1.1,
        description="صوت حازم وواثق بقوة",
    ),

    # ═══════════════════════════════════════════════════════════════
    # NEW TAGS (3) — Street Content
    # ═══════════════════════════════════════════════════════════════

    "hook": TagConfig(
        name_ar="افتتاحية صادمة",
        name_en="Hook",
        name_fr="Accroche",
        voice_style="intense",
        voice_rate=1.10,
        voice_pitch=+2,
        voice_volume=1.10,
        description=(
            "افتتاحية صادمة تجذب الانتباه فوراً "
            "في الثواني الأولى من الفيديو"
        ),
    ),

    "direct": TagConfig(
        name_ar="مباشر وحاد",
        name_en="Direct",
        name_fr="Direct",
        voice_style="bold",
        voice_rate=0.97,
        voice_pitch=0,
        voice_volume=1.08,
        description=(
            "كلام مباشر بدون مقدمات — "
            "كأنك تمسك المشاهد من كتفه"
        ),
    ),

    "cta": TagConfig(
        name_ar="دعوة للتفاعل",
        name_en="Call to Action",
        name_fr="Appel à l'action",
        voice_style="uplifting",
        voice_rate=1.05,
        voice_pitch=+1,
        voice_volume=1.10,
        description=(
            "دعوة قوية للحفظ أو المتابعة أو "
            "المشاركة في نهاية الفيديو"
        ),
    ),
}


# ═════════════════════════════════════════════════════════════════════════════
# PUBLIC EXPORTS
# ═════════════════════════════════════════════════════════════════════════════

VALID_TAGS: dict[str, dict] = {
    name: cfg.to_dict()
    for name, cfg in _TAG_CONFIGS.items()
}

VALID_TAG_NAMES: list[str] = list(_TAG_CONFIGS.keys())


# ═════════════════════════════════════════════════════════════════════════════
# REGEX PATTERNS
# ═════════════════════════════════════════════════════════════════════════════

_TAG_RE = re.compile(r"\[([a-zA-Z_]+)\]")

TAG_PATTERN = re.compile(
    r"^\s*\[([a-zA-Z_]+)\]\s*",
    re.IGNORECASE | re.MULTILINE,
)
TAG_INLINE_PATTERN = _TAG_RE


# ═════════════════════════════════════════════════════════════════════════════
# MANUAL TAG MAPPING — Extended for variants
# ═════════════════════════════════════════════════════════════════════════════

_MANUAL_TAG_MAP: dict[str, str] = {

    # ═══════════════════════════════════════════════════════════════
    # Emotion synonyms
    # ═══════════════════════════════════════════════════════════════
    "excited":    "inspiration",
    "happy":      "inspiration",
    "sad":        "emotional",
    "angry":      "shock",
    "fear":       "tension",
    "reflective": "wisdom",
    "mysterious": "intrigue",
    "suspense":   "tension",
    "soft":       "calm",
    "strong":     "powerful",
    "epic":       "climax",
    "story":      "storytelling",
    "secret":     "whisper",
    "reveal":     "revelation",
    "truth":      "revelation",
    "silence":    "pause",
    "question":   "curiosity",

    # ═══════════════════════════════════════════════════════════════
    # Hook variants
    # ═══════════════════════════════════════════════════════════════
    "opening":   "hook",
    "intro":     "hook",
    "start":     "hook",
    "beginning": "hook",
    "open":      "hook",

    # ═══════════════════════════════════════════════════════════════
    # Direct variants (including French "direct message")
    # ═══════════════════════════════════════════════════════════════
    "direct message": "direct",
    "direct_message": "direct",
    "direct_msg":     "direct",
    "straight":       "direct",
    "blunt":          "direct",

    # ═══════════════════════════════════════════════════════════════
    # CTA variants
    # ═══════════════════════════════════════════════════════════════
    "call to action": "cta",
    "call_to_action": "cta",
    "callto":         "cta",
    "action":         "cta",
    "save":           "cta",
    "follow":         "cta",
    "subscribe":      "cta",
    "share":          "cta",

    # ═══════════════════════════════════════════════════════════════
    # Common abbreviations
    # ═══════════════════════════════════════════════════════════════
    "info":     "information",
    "inspire":  "inspiration",
    "conf":     "confident",
    "emotion":  "emotional",
    "drama":    "dramatic",
    "tense":    "tension",
}


# ═════════════════════════════════════════════════════════════════════════════
# CORE: SPLIT INTO TAGGED SENTENCES
# ═════════════════════════════════════════════════════════════════════════════

def split_into_tagged_sentences(content: str) -> list[dict]:
    """
    تقسيم المحتوى إلى جمل مع tags.

    Pre-text before first tag is preserved.
    line_counter is consistent even with empty segments.

    Returns:
        list of {"raw_tag": str|None, "text": str, "line": int}

    Examples:
        >>> split_into_tagged_sentences("مقدمة [shock] مفاجأة [calm] هدوء")
        [
            {"raw_tag": None,    "text": "مقدمة",  "line": 1},
            {"raw_tag": "shock", "text": "مفاجأة", "line": 2},
            {"raw_tag": "calm",  "text": "هدوء",   "line": 3},
        ]

        >>> split_into_tagged_sentences("[shock][calm] هدوء")
        [
            {"raw_tag": "calm", "text": "هدوء", "line": 2},
        ]
    """
    if not content or not content.strip():
        return []

    text    = content.strip()
    matches = list(_TAG_RE.finditer(text))

    if not matches:
        return [{
            "raw_tag": None,
            "text":    text,
            "line":    1,
        }]

    result:       list[dict] = []
    line_counter: int        = 1

    # Pre-text before first tag
    pre_text = text[:matches[0].start()].strip()
    if pre_text:
        result.append({
            "raw_tag": None,
            "text":    pre_text,
            "line":    line_counter,
        })
        line_counter += 1

    # Text between tags
    for i, match in enumerate(matches):
        raw_tag    = match.group(1).strip()
        text_start = match.end()
        text_end   = (
            matches[i + 1].start()
            if i + 1 < len(matches)
            else len(text)
        )

        segment      = text[text_start:text_end].strip()
        current_line = line_counter

        # line_counter increments always (even empty segments)
        line_counter += 1

        if not segment:
            # Empty segment (e.g., [hook][shock] sequence)
            continue

        result.append({
            "raw_tag": raw_tag,
            "text":    segment,
            "line":    current_line,
        })

    return result


# ═════════════════════════════════════════════════════════════════════════════
# TAG VALIDATION & CORRECTION
# ═════════════════════════════════════════════════════════════════════════════

def is_valid_tag(tag: Optional[str]) -> bool:
    """Check if tag exists in valid configs."""
    if not tag:
        return False
    return tag in _TAG_CONFIGS


def auto_correct_tag(
    raw_tag: Optional[str],
) -> tuple[Optional[str], str]:
    """
    Auto-correct misspelled tag.

    Strategy (4 levels):
        1. Exact match
        2. Case fix (lowercase)
        3. Manual mapping (synonyms + variants)
        4. Fuzzy match (typos only, cutoff=0.65)

    Returns:
        (corrected_tag, source) or (None, "no_match")
    """
    if not raw_tag:
        return (None, "empty_tag")

    cleaned = raw_tag.strip()

    # 1) Exact match
    if cleaned in _TAG_CONFIGS:
        return (cleaned, "exact_match")

    # 2) Case fix
    lower = cleaned.lower()
    if lower in _TAG_CONFIGS:
        return (lower, "case_fixed")

    # 3) Manual mapping (synonyms + variants)
    if lower in _MANUAL_TAG_MAP:
        return (_MANUAL_TAG_MAP[lower], "manual_map")

    # 4) Fuzzy match (for typos only)
    fuzzy = get_close_matches(
        lower,
        VALID_TAG_NAMES,
        n      = FUZZY_MATCHES_LIMIT,
        cutoff = FUZZY_CUTOFF,
    )
    if fuzzy:
        log.debug(
            "  🔧 Fuzzy: [%s] → [%s]",
            cleaned, fuzzy[0]
        )
        return (fuzzy[0], "spelling_fixed")

    return (None, "no_match")


def strip_tags_from_text(text: str) -> str:
    """
    Remove all tags from text.

    Replaces tag with space to preserve word boundaries.

    Examples:
        >>> strip_tags_from_text("[shock] Hello [calm] World")
        "Hello World"
        >>> strip_tags_from_text("Hello[shock]World")
        "Hello World"
    """
    if not text:
        return ""
    # Replace tag with space (preserves word boundaries)
    cleaned = _TAG_RE.sub(" ", text)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


# ═════════════════════════════════════════════════════════════════════════════
# TAG INFO ACCESSORS
# ═════════════════════════════════════════════════════════════════════════════

def get_tag_info(tag: str) -> Optional[dict]:
    """Get tag info as dict (for JSON/API)."""
    config = _TAG_CONFIGS.get(tag)
    return config.to_dict() if config else None


def get_tag_config(tag: str) -> Optional[TagConfig]:
    """Get tag config as TagConfig (for internal use)."""
    return _TAG_CONFIGS.get(tag)


def get_tag_name(tag: str, lang: str = "ar") -> str:
    """
    Get tag name in specified language.
    Falls back to English if lang invalid.
    """
    if lang not in _VALID_LANGS:
        log.warning(
            "  ⚠️  Unsupported lang %r in get_tag_name — using 'en'",
            lang
        )
        lang = "en"

    config = _TAG_CONFIGS.get(tag)
    if not config:
        return tag

    return config.get_name(lang)


# ═════════════════════════════════════════════════════════════════════════════
# SUMMARY
# ═════════════════════════════════════════════════════════════════════════════

def _categorize_sentence_source(
    sent: dict,
) -> tuple[str, Optional[str]]:
    """
    Categorize tag source for summary.
    
    NOTE: Expects these fields (added by process_tags/AI, NOT split_into_tagged_sentences):
        - tag_source: exact_match | case_fixed | manual_map | spelling_fixed | ai_suggested
        - raw_tag:    original tag from content
        - final_tag:  corrected tag
    """
    source    = sent.get("tag_source", "unknown")
    raw_tag   = sent.get("raw_tag")
    final_tag = sent.get("final_tag", DEFAULT_TAG)

    correction_sources = (
        "case_fixed",
        "spelling_fixed",
        "manual_map",
    )

    if source in correction_sources and raw_tag:
        return (
            "correction",
            f"     ⚠️  [{raw_tag}] → [{final_tag}]",
        )

    if source == "ai_suggested":
        line = sent.get("line", "?")
        return (
            "ai_suggested",
            f"     🤖 Line {line}: [{final_tag}] (AI suggested)",
        )

    return ("normal", None)


def format_tags_summary(
    tagged_sentences: list[dict],
    lang:             str = "ar",
) -> str:
    """
    Build tags summary with stats.
    
    Args:
        tagged_sentences: list with final_tag, tag_source, raw_tag, line
                         (these fields are added AFTER process_tags())
        lang:            ar | fr | en (for plural words)
    """
    if not tagged_sentences:
        return "  ⚠️  No tagged sentences found"

    # Plural words per language
    singular, plural_word = _PLURAL_MAP.get(
        lang, _PLURAL_MAP["en"]
    )

    tag_counts:   dict[str, int] = {}
    corrections:  list[str]      = []
    ai_suggested: list[str]      = []

    for sent in tagged_sentences:
        final_tag = sent.get("final_tag", DEFAULT_TAG)
        tag_counts[final_tag] = tag_counts.get(final_tag, 0) + 1

        category, message = _categorize_sentence_source(sent)
        if category == "correction" and message:
            corrections.append(message)
        elif category == "ai_suggested" and message:
            ai_suggested.append(message)

    lines = [
        "\n  📝 Tags Summary:",
        "  " + "─" * 45,
    ]

    sorted_tags = sorted(
        tag_counts.items(),
        key=lambda x: -x[1],
    )

    for tag, count in sorted_tags:
        config = _TAG_CONFIGS.get(tag)
        desc   = config.description if config else ""
        word   = singular if count == 1 else plural_word

        lines.append(
            f"     ├── [{tag:14}] : {count} {word}"
        )
        lines.append(f"     │   {desc[:50]}")

    if corrections:
        lines.append("\n  🔧 Auto-corrections:")
        lines.extend(corrections)

    if ai_suggested:
        lines.append("\n  🤖 AI-suggested tags:")
        lines.extend(ai_suggested)

    return "\n".join(lines)


def print_tags_summary(
    tagged_sentences: list[dict],
    lang:             str = "ar",
) -> None:
    """Print tags summary via log."""
    summary = format_tags_summary(tagged_sentences, lang)
    log.info(summary)

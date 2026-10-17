"""
🧠 Smart AI Assistant powered by Groq — Final Stable Version v7.6

Changes from v7.5:
  ✅ 1. _fix_trailing_comma_in_string() — فاصلة داخل string
  ✅ 2. _fix_multiline_strings() — multiline strings في JSON
  ✅ 3. _fix_unclosed_quotes() — quotes غير مغلقة
  ✅ 4. _apply_all_fixes() — دالة موحّدة لكل الإصلاحات
  ✅ 5. _parse_json_response() — 5 محاولات موحّدة
  ✅ 6. generate_hashtags() — prompt أصرم + temperature=0.5
  ✅ 7. generate_captions() — prompt أصرم مع ONE line rule

Changes in v7.6 (NEW):
  ✅ 8.  REASONING_MODELS + REASONING_TOKEN_MULTIPLIER —
         نضاعف max_tokens تلقائياً لموديلات التفكير (reasoning
         models مثل qwen) عشان ما ينقطع الرد أثناء <think>.
  ✅ 9.  اكتشاف القطع أثناء التفكير (finish_reason == "length"
         و <think> بدون </think>) في _call_groq → نبدّل الموديل
         فوراً بدل ما نرجع رد فاسد.
  ✅ 10. MODELS_PRIORITY أعيد ترتيبها: موديلات non-reasoning
         أولاً، وqwen (reasoning) في آخر القائمة كحل أخير.
  ✅ 11. generate_pattern_interrupts() — try/except + fallback
         بدل ما يوقف الفيديو كامل.
  ✅ 12. generate_engagement_questions() — try/except + fallback.
  ✅ 13. generate_hashtags() — try/except + fallback.
  ✅ 14. generate_captions() — try/except + fallback.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
from typing import Any, Optional

from groq import Groq

from tags_parser import (
    DEFAULT_TAG,
    VALID_TAG_NAMES,
    auto_correct_tag,
)

log = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════

# ✅ v7.6 — قائمة موديلات مع Auto Fallback
# ملاحظة مهمة: qwen/qwen3.6-27b هو "reasoning model" (يفكر قبل
# ما يجاوب بـ <think>...</think>) وهذا يستهلك توكنز كثيرة، فلو
# انقطع الرد أثناء التفكير ما توصل JSON صالحة أبداً. لذلك حطيناه
# آخر خيار (fallback أخير) بدل ثاني خيار، وموديلات non-reasoning
# الأسرع والأأمن قبله.
MODELS_PRIORITY = [
    "allam-2-7b",           # 7,000 طلب/يوم (الأعلى حصة والأفضل)
    "openai/gpt-oss-20b",   # 1,000 طلب/يوم (سريع، بدون thinking — احتياطي أول)
    "openai/gpt-oss-120b",  # 1,000 طلب/يوم (نموذج قوي جداً، بدون thinking)
    "qwen/qwen3.6-27b",     # 1,000 طلب/يوم (جودة عالية لكنه reasoning model — آخر خيار)
]

# ✅ v7.6 — موديلات التفكير (reasoning) التي تحتاج توكنز إضافية
REASONING_MODELS: set[str] = {
    "qwen/qwen3.6-27b",
}
# نضاعف max_tokens لهذي الموديلات عشان يكفي وقت "للتفكير"
# قبل ما يبدأ يكتب الـ JSON الفعلي.
REASONING_TOKEN_MULTIPLIER = 3.0
# حد أقصى لتفادي طلبات ضخمة جداً بدون داعي
REASONING_TOKEN_CAP = 8000


MAX_RETRIES_PER_KEY  = 3
RATE_LIMIT_WAIT      = 3.0
RATE_LIMIT_WAIT_MAX  = 60.0
MAX_KEYS_SCAN        = 20
GROQ_TIMEOUT         = 60
MAX_SEARCH_ATTEMPTS  = 20

TITLE_MAX_CHARS         = 80
TITLE_SHORT_CHARS       = 60
TITLE_DISPLAY_CHARS     = 50
CONTENT_ANALYSIS_CHARS  = 600
CONTENT_POWER_CHARS     = 800
CONTENT_CAPTION_CHARS   = 500
CONTENT_HOOK_CHARS      = 300
CONTENT_STREET_CHARS    = 800
SENTENCE_KEYWORDS_CHARS = 150
SENTENCE_TAGS_CHARS     = 120
CAPTION_MAX_LENGTH      = 60000
CAPTION_HASHTAG_LIMIT   = 12

BATCH_SIZE_TAGS     = 10
BATCH_SIZE_KEYWORDS = 20
BATCH_SLEEP         = 2.0
ANALYSIS_CACHE_SIZE = 50

INTER_OPERATION_SLEEP = 1.0

DEFAULT_EMOJI_LEFT   = "🔥"
DEFAULT_EMOJI_RIGHT  = "💥"
DEFAULT_INTENSITY    = 7
DEFAULT_VISUAL_STYLE = "person serious face talking camera"

RATE_LIMIT_KEYWORDS = (
    "429", "rate_limit", "rate limit",
    "quota", "ratequota", "tokens per minute",
)

_VALID_LANGS = frozenset({"ar", "fr", "en"})
_VALID_MODES = frozenset({"short", "long"})


# ═══════════════════════════════════════════════════
# LANGUAGE MAPS
# ═══════════════════════════════════════════════════

LANG_NAMES: dict[str, str] = {
    "ar": "Arabic",
    "fr": "French",
    "en": "English",
}


# ═══════════════════════════════════════════════════
# TAG VISUAL STYLES
# ═══════════════════════════════════════════════════

TAG_VISUAL_STYLE: dict[str, str] = {
    "intrigue":     "person whispering secret curious close up face",
    "desire":       "person reaching out longing wanting emotional",
    "information":  "person talking explaining serious face camera",
    "shock":        "person shocked surprised wide eyes jaw drop reaction",
    "urgency":      "person running fast stressed hurrying time pressure",
    "wisdom":       "older person thinking contemplating slow calm",
    "confident":    "confident person speaking assertive strong posture",
    "calm":         "person breathing calm peaceful relaxed serene",
    "emotional":    "person crying emotional tears face close up pain",
    "inspiration":  "person motivated determined success forward movement",
    "pause":        "person standing alone silent thinking moment",
    "whisper":      "person whispering close up lips secretive",
    "curiosity":    "person curious questioning looking wondering",
    "storytelling": "person speaking engaged crowd listening story",
    "dramatic":     "intense emotional dramatic person scene powerful",
    "revelation":   "person shocked truth realization wide eyes",
    "tension":      "person nervous anxious stressed hands face",
    "climax":       "intense emotional peak powerful breakthrough person",
    "powerful":     "strong determined person confident unstoppable",
}


# ═══════════════════════════════════════════════════
# ABSTRACT WORDS
# ═══════════════════════════════════════════════════

ABSTRACT_WORDS: frozenset[str] = frozenset({
    "mystery", "mysterious", "journey", "soul", "shadows",
    "silence", "whisper", "darkness", "longing", "ethereal",
    "abstract", "spiritual", "void", "abyss", "illusion",
    "dream", "fantasy", "essence", "energy", "vibes",
    "magic", "surreal", "haunting", "melancholy", "solitude",
    "echo", "horizon", "twilight", "dusk", "mist",
    "fog", "haze", "glow", "radiance", "aura",
    "pulse", "rhythm", "flow", "whispers", "echoes",
})


# ═══════════════════════════════════════════════════
# ✅ v7.7 — PLACEHOLDER KEYWORD DETECTION
# ═══════════════════════════════════════════════════
# المشكلة: أحياناً الموديل (خصوصاً qwen بعد قطع تفكيره) يرجّع
# نفس الـ placeholder المكتوب كمثال بالبرومبت حرفياً بدل ما
# يستبدله بكلمة بحث حقيقية، مثلاً: ["kw1","kw2","kw3"].
# الـ JSON يكون صحيح تقنياً فما يُكتشف كخطأ، لكن الفيديو يبحث
# فعلياً في Pexels عن الكلمة الحرفية "kw1" فيرجع نتيجة عشوائية.

_PLACEHOLDER_KEYWORD_RE = re.compile(
    r'^\s*kw\s*\d*\s*$', re.IGNORECASE
)
_PLACEHOLDER_KEYWORD_LITERALS: frozenset[str] = frozenset({
    "kw1", "kw2", "kw3", "kw4", "kw5",
    "keyword1", "keyword2", "keyword3",
    "item1", "item2", "item3",
    "example", "example1", "example2",
    "placeholder", "n/a", "na", "none", "null",
})


def _is_placeholder_keyword(kw: str) -> bool:
    """✅ v7.7 — يكشف كلمات placeholder وهمية زي 'kw1'."""
    normalized = kw.strip().lower()
    if not normalized:
        return True
    if normalized in _PLACEHOLDER_KEYWORD_LITERALS:
        return True
    if _PLACEHOLDER_KEYWORD_RE.match(normalized):
        return True
    return False


# ═══════════════════════════════════════════════════
# VISUAL KEYWORDS EXAMPLES
# ═══════════════════════════════════════════════════

VISUAL_KEYWORDS_EXAMPLES: dict[str, list[str]] = {
    "psychology": [
        "person thinking deeply alone indoor",
        "facial expression emotion change close up",
        "body language behavior human interaction",
    ],
    "relationships": [
        "two people serious conversation face",
        "person listening attentively nodding",
        "couple disagreement discussion indoor",
    ],
    "motivation": [
        "person determined walking forward purposeful",
        "focused person working hard success",
        "motivated person overcoming challenge obstacle",
    ],
    "social_skills": [
        "person speaking confidently group people",
        "confident handshake eye contact meeting",
        "person assertive body language speaking",
    ],
    "business": [
        "professional person office working serious",
        "business meeting people discussing table",
        "entrepreneur focused determined working",
    ],
    "lifestyle": [
        "person daily routine habit morning",
        "healthy person active lifestyle movement",
        "person organizing planning focused",
    ],
    "education": [
        "person studying learning focused reading",
        "student concentrating thinking problem",
        "person explaining teaching whiteboard",
    ],
    "health": [
        "person healthy active movement exercise",
        "person feeling good energetic positive",
        "wellness routine person calm breathing",
    ],
    "finance": [
        "person serious financial planning thinking",
        "professional calculating analyzing focused",
        "person stressed worried financial pressure",
    ],
    "spirituality": [
        "person calm meditation peaceful breathing",
        "person reflecting thinking quiet moment",
        "serene person nature peaceful outdoor",
    ],
    "default": [
        "person serious face close up talking",
        "emotional person expression dramatic",
        "person speaking camera direct confident",
    ],
}


def _get_topic_examples(content_type: str) -> list[str]:
    return VISUAL_KEYWORDS_EXAMPLES.get(
        content_type,
        VISUAL_KEYWORDS_EXAMPLES["default"],
    )


# ═══════════════════════════════════════════════════
# STREET STYLES
# ═══════════════════════════════════════════════════

STREET_STYLE: dict[str, dict] = {
    "ar": {
        "style_name": "لغة شارع الإمارات العربية المتحدة",
        "instructions": (
            "اكتب بأسلوب شباب الإمارات — "
            "كلمات مثل: والله، يبيلك، ما قصّر، خوش، زين، "
            "شدة، ولا يهمك، هالشي، عادي، طبعاً. "
            "مباشر وحماسي كأنك تتكلم مع صاحبك. "
            "استخدم إيموجي. لا تكتب بالفصحى الرسمية."
        ),
        "hashtag_lang": "اكتب hashtags بالعربية والإنجليزية",
    },
    "fr": {
        "style_name": "argot français",
        "instructions": (
            "Écris en argot français — "
            "wesh, c'est ouf, trop stylé, carrément, "
            "tranquille, grave, c'est chaud, t'as vu. "
            "Direct et énergique comme tu parles à un pote. "
            "Utilise des emojis. Pas de français formel."
        ),
        "hashtag_lang": "Hashtags en français et en anglais",
    },
    "en": {
        "style_name": "American street slang",
        "instructions": (
            "Write in authentic American street slang — "
            "no cap, fr fr, lowkey, bussin, it's giving, "
            "slay, periodt, facts, bet, that's wild, "
            "deadass, finna, hits different. "
            "Keep it real and hype. Use lots of emojis. "
            "No formal English."
        ),
        "hashtag_lang": "Write hashtags in English only",
    },
}


# ═══════════════════════════════════════════════════
# DEFAULTS
# ═══════════════════════════════════════════════════

DEFAULT_ACCENT_COLORS: list[str] = [
    "#FF003C", "#FFD700", "#00FFFF", "#39FF14",
]

HOOK_FALLBACK         = "shocking dramatic moment"
HOOK_FALLBACK_KEYWORD = "person emotional dramatic close up face"

_HEX_PATTERN = re.compile(r"^#[0-9A-Fa-f]{6}$")

_STREET_FALLBACK: dict[str, str] = {
    "ar": "شاهد الفيديو كاملاً! 🔥",
    "fr": "Regarde la vidéo complète ! 🔥",
    "en": "Watch the full video! 🔥",
}

# ✅ v7.6 — fallback ثابت لعمليات bilingual لما الـ AI يفشل
# نستخدمهم بدل ما نوقف الفيديو كامل.
_PATTERN_INTERRUPTS_FALLBACK: dict[str, list[str]] = {
    "ar": ["ثواني...", "لا تفوّت هذا!", "تخيل بس"],
    "fr": ["Attends...", "Incroyable !", "Regarde ça"],
    "en": ["Wait...", "No way!", "Listen up"],
}

_ENGAGEMENT_QUESTIONS_FALLBACK: dict[str, list[str]] = {
    "ar": [
        "شو رأيك بهذا؟ 🤔",
        "صار معك قبل؟ 😮",
        "تحس إنك تتفق؟ 💭",
    ],
    "fr": [
        "Qu'en penses-tu ? 🤔",
        "Ça t'est déjà arrivé ? 😮",
        "Tu es d'accord ? 💭",
    ],
    "en": [
        "What do you think? 🤔",
        "Has this happened to you? 😮",
        "Can you relate? 💭",
    ],
}

_HASHTAGS_FALLBACK_EN: list[str] = [
    "#viral", "#fyp", "#foryou", "#trending",
    "#motivation", "#psychology",
]
_HASHTAGS_FALLBACK_AR: list[str] = [
    "#فيروسي", "#ترند", "#تحفيز", "#سيكولوجيا",
]
_HASHTAGS_FALLBACK_FR: list[str] = [
    "#viral", "#tendance", "#motivation", "#psychologie",
]
_HASHTAGS_FALLBACK_BY_LANG: dict[str, list[str]] = {
    "ar": _HASHTAGS_FALLBACK_AR,
    "fr": _HASHTAGS_FALLBACK_FR,
    "en": _HASHTAGS_FALLBACK_EN,
}


# ═══════════════════════════════════════════════════
# EXCEPTIONS
# ═══════════════════════════════════════════════════

class AIEnrichmentError(Exception):
    pass


# ═══════════════════════════════════════════════════
# VALIDATION
# ═══════════════════════════════════════════════════

def _validate_lang(lang: str) -> None:
    if lang not in _VALID_LANGS:
        raise ValueError(f"Invalid lang '{lang}'")


def _validate_mode(mode: str) -> None:
    if mode not in _VALID_MODES:
        raise ValueError(f"Invalid content_mode '{mode}'")


# ═══════════════════════════════════════════════════
# THREAD-SAFE GROQ KEY ROTATION + CLIENT CACHING
# ═══════════════════════════════════════════════════

_groq_keys:   list[str]       = []
_groq_index:  int             = 0
_groq_lock:   threading.RLock = threading.RLock()
_keys_loaded: bool            = False
_clients:     dict[str, Groq] = {}


def _load_groq_keys() -> list[str]:
    keys: list[str] = []
    seen: set[str]  = set()

    main = os.environ.get("GROQ_API_KEY", "").strip()
    if main and main not in seen:
        keys.append(main)
        seen.add(main)

    for i in range(1, MAX_KEYS_SCAN + 1):
        k1 = os.environ.get(
            f"GROQ_API_KEY{i}", ""
        ).strip()
        if k1 and k1 not in seen:
            keys.append(k1)
            seen.add(k1)
        k2 = os.environ.get(
            f"GROQ_API_KEY_{i}", ""
        ).strip()
        if k2 and k2 not in seen:
            keys.append(k2)
            seen.add(k2)

    return keys


def _ensure_keys_loaded() -> None:
    global _groq_keys, _keys_loaded

    if _keys_loaded:
        return

    with _groq_lock:
        if _keys_loaded:
            return
        _groq_keys   = _load_groq_keys()
        _keys_loaded = True
        if _groq_keys:
            log.info(
                "  🔑 Loaded %d Groq API keys",
                len(_groq_keys)
            )
        else:
            log.warning(
                "  ⚠️  No Groq API keys found"
            )


def _get_client(key: str) -> Groq:
    with _groq_lock:
        if key not in _clients:
            _clients[key] = Groq(
                api_key = key,
                timeout = GROQ_TIMEOUT,
            )
        return _clients[key]


def _rotate_groq_key() -> None:
    global _groq_index
    with _groq_lock:
        n = len(_groq_keys)
        if n <= 1:
            log.warning(
                "  ⚠️  No additional Groq keys"
            )
            return
        _groq_index = (_groq_index + 1) % n
        new_idx     = _groq_index
    log.info(
        "  🔄 Groq key rotated → #%d/%d",
        new_idx + 1, n
    )


def _is_rate_limit_error(error: str) -> bool:
    err_lower = error.lower()
    return any(
        ind in err_lower
        for ind in RATE_LIMIT_KEYWORDS
    )


# ═══════════════════════════════════════════════════
# ✅ v7.5 — JSON FIXERS (كل دوال الإصلاح)
# ═══════════════════════════════════════════════════

def _fix_quotes(text: str) -> str:
    """إصلاح علامات الاقتباس الزائدة."""
    text = re.sub(r'""([^"\n]+)"', r'"\1"', text)
    text = re.sub(r'"([^"\n]+)""', r'"\1"', text)
    return text


def _fix_multiline_strings(text: str) -> str:
    """
    ✅ v7.5 — إصلاح multiline strings في JSON.

    المشكلة: LLM يكتب caption على عدة أسطر:
        "fr": "نص السطر الأول
        نص السطر الثاني"

    الإصلاح:
        "fr": "نص السطر الأول\\nنص السطر الثاني"
    """
    lines     = text.split('\n')
    result    = []
    in_string = False

    for line in lines:
        # عدّ quotes غير مُهرَّبة في السطر
        quote_count = 0
        j           = 0
        while j < len(line):
            if line[j] == '\\':
                j += 2
                continue
            if line[j] == '"':
                quote_count += 1
            j += 1

        if not in_string:
            result.append(line)
            # quotes فردية → دخلنا string
            if quote_count % 2 == 1:
                in_string = True
        else:
            # داخل string → استبدل newline بـ \n
            if result:
                result[-1] = (
                    result[-1] +
                    '\\n' +
                    line.strip()
                )
            else:
                result.append(line)
            # quotes فردية → خرجنا من string
            if quote_count % 2 == 1:
                in_string = False

    return '\n'.join(result)


def _fix_trailing_comma_in_string(text: str) -> str:
    """
    ✅ v7.5 — إصلاح فاصلة داخل string قبل الإغلاق.

    المشكلة:
        "Do you notice? 😊,"   ← فاصلة قبل "
        "text1""text2"         ← items ملتصقة

    الإصلاح:
        "Do you notice? 😊",
        "text1", "text2"
    """
    # Pattern 1: ,"  → ",
    # فاصلة داخل الـ string قبل إغلاقها مباشرة
    text = re.sub(
        r',"(\s*[,\]\n])',
        r'"\1',
        text,
    )

    # Pattern 2: "text," → "text",
    # أي فاصلة قبل آخر " في string
    text = re.sub(
        r',("\s*(?:[,\]\n]))',
        r'"\1',
        text,
    )

    # Pattern 3: "text1""text2" → "text1", "text2"
    # items ملتصقة بـ "" — لكن نتجنب كسر string فارغ شرعي
    # نطابق فقط عندما يوجد محتوى غير فراغ قبل وبعد الاقتباسين المتلاصقين
    text = re.sub(
        r'(?<=[^\s,:\[{])"(\s*)"(?=[^\s,\]}])',
        r'", "',
        text,
    )

    return text


def _fix_unclosed_quotes(text: str) -> str:
    """
    ✅ v7.5 — إصلاح quotes غير مغلقة داخل JSON arrays.

    المشكلة:
        "#_الموت_ليس_الخاسر_,   ← مفتوح بلا إغلاق
        "#_الأفضل_من_الخسارة"

    الإصلاح:
        "#_الموت_ليس_الخاسر_",  ← أضف " قبل الفاصلة
        "#_الأفضل_من_الخسارة"
    """
    # Pattern: "text, → "text",
    text = re.sub(
        r'"([^"\n,]+),\s*\n',
        r'"\1",\n',
        text,
    )
    return text


_UNQUOTED_ARRAY_RE = re.compile(
    r'\[\s*((?:[^"\[\]{}]|"[^"]*")'
    r'(?:\s*,\s*(?:[^"\[\]{}]|"[^"]*"))*)\s*\]',
    re.DOTALL,
)


def _fix_unquoted_array_values(text: str) -> str:
    """
    ✅ v7.3 — إصلاح array items بدون quotes.

    يعالج:
    1. ["item1", item2, item3]  → مختلطة
    2. [item1\nitem2]           → بدون فواصل
    """
    def _fix_array_content(match: re.Match) -> str:
        raw   = match.group(0)
        inner = raw[1:-1].strip()

        if not inner:
            return raw

        # إضافة فواصل مفقودة بين السطور
        inner = re.sub(
            r'("|\w|[^\s,\[\]{}])(\s*\n\s*)(?=[^\s,\[\]{}])',
            r'\1,\n    ',
            inner,
        )

        # تقسيم ذكي مع الحفاظ على quoted strings
        parts:    list[str] = []
        current             = ""
        in_quote            = False
        i                   = 0

        while i < len(inner):
            ch = inner[i]
            if ch == '"' and not in_quote:
                in_quote = True
                current += ch
            elif ch == '"' and in_quote:
                in_quote = False
                current += ch
            elif ch == ',' and not in_quote:
                parts.append(current.strip())
                current = ""
            else:
                current += ch
            i += 1

        if current.strip():
            parts.append(current.strip())

        fixed_parts: list[str] = []
        for part in parts:
            part = part.strip()
            if not part:
                continue

            if (
                part.startswith('"') and
                part.endswith('"') and
                len(part) >= 2
            ):
                fixed_parts.append(part)
                continue

            if part.startswith('"'):
                part = part[1:]
            if part.endswith('"'):
                part = part[:-1]

            part = part.replace('\\', '\\\\')
            part = part.replace('"', '\\"')
            part = part.strip()

            if part:
                fixed_parts.append(f'"{part}"')

        if not fixed_parts:
            return raw

        return "[" + ", ".join(fixed_parts) + "]"

    return _UNQUOTED_ARRAY_RE.sub(
        _fix_array_content, text
    )


def _apply_all_fixes(text: str) -> str:
    """
    ✅ v7.5 — تطبيق كل الإصلاحات بالترتيب الصحيح.

    الترتيب مهم:
    1. multiline أولاً (قبل أي شيء)
    2. trailing comma
    3. unclosed quotes
    4. unquoted array values
    5. double quotes زائدة أخيراً
    """
    text = _fix_multiline_strings(text)
    text = _fix_trailing_comma_in_string(text)
    text = _fix_unclosed_quotes(text)
    text = _fix_unquoted_array_values(text)
    text = _fix_quotes(text)
    return text


def _clean_json(raw: str) -> str:
    """تنظيف واستخراج أول JSON كامل."""
    if not raw:
        return ""

    text = raw.strip()

    text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL)
    text = re.sub(r'<reasoning>.*?</reasoning>', '', text, flags=re.DOTALL) 
  
    text = re.sub(r"^```(?:json|JSON)?\s*\n?", "", text)
    text = re.sub(r"\n?\s*```\s*$",            "", text)
    text = text.strip()

    # إصلاح قبل التحليل
    text = _fix_quotes(text)

    start_obj = text.find("{")
    start_arr = text.find("[")

    if start_obj == -1 and start_arr == -1:
        return text

    if start_obj == -1:
        start, open_char, close_char = (
            start_arr, "[", "]"
        )
    elif start_arr == -1:
        start, open_char, close_char = (
            start_obj, "{", "}"
        )
    else:
        if start_obj < start_arr:
            start, open_char, close_char = (
                start_obj, "{", "}"
            )
        else:
            start, open_char, close_char = (
                start_arr, "[", "]"
            )

    depth     = 0
    in_string = False
    i         = start

    while i < len(text):
        char = text[i]

        if (
            in_string and
            char == "\\" and
            i + 1 < len(text)
        ):
            i += 2
            continue

        if char == '"':
            in_string = not in_string
        elif not in_string:
            if char == open_char:
                depth += 1
            elif char == close_char:
                depth -= 1
                if depth == 0:
                    return text[start : i + 1].strip()
        i += 1

    return text[start:].strip()


def _parse_json_response(
    raw:           str,
    expected_type: type,
    operation:     str,
) -> Any:
    """
    ✅ v7.5 — Parse JSON مع 5 محاولات موحّدة.

    الترتيب:
    1. مباشر
    2. كل الـ fixes (_apply_all_fixes)
    3. backticks + كل الـ fixes
    4. _fix_quotes فقط (للتوافق القديم)
    5. بحث في positions + كل الـ fixes
    """
    if not raw or not raw.strip():
        raise AIEnrichmentError(
            f"❌ {operation}: empty response"
        )

    def _try_parse(text: str) -> Any:
        cleaned = _clean_json(text)
        data    = json.loads(cleaned)
        if isinstance(data, expected_type):
            return data
        raise ValueError(
            f"Expected {expected_type.__name__}, "
            f"got {type(data).__name__}"
        )

    # Attempt 1: مباشر
    try:
        return _try_parse(raw)
    except json.JSONDecodeError:
        pass
    except ValueError as e:
        raise AIEnrichmentError(
            f"❌ {operation}: {e}"
        )

    # Attempt 2: كل الـ fixes دفعة واحدة
    try:
        fixed  = _apply_all_fixes(raw)
        result = _try_parse(fixed)
        log.debug(
            "  ✓ %s: parsed after all fixes",
            operation,
        )
        return result
    except (json.JSONDecodeError, ValueError):
        pass

    # Attempt 3: إزالة backticks + كل الـ fixes
    try:
        no_ticks = (
            raw
            .replace("```json", "")
            .replace("```JSON", "")
            .replace("```",     "")
            .strip()
        )
        fixed  = _apply_all_fixes(no_ticks)
        result = _try_parse(fixed)
        log.debug(
            "  ✓ %s: parsed after backtick+fixes",
            operation,
        )
        return result
    except (json.JSONDecodeError, ValueError):
        pass

    # Attempt 4: _fix_quotes فقط (للتوافق القديم)
    try:
        result = _try_parse(_fix_quotes(raw))
        log.debug(
            "  ✓ %s: parsed after quote fix only",
            operation,
        )
        return result
    except (json.JSONDecodeError, ValueError):
        pass

    # Attempt 5: بحث في positions + كل الـ fixes
    try:
        open_chars = (
            ["[", "{"] if expected_type == list
            else ["{", "["]
        )
        for open_char in open_chars:
            idx      = 0
            attempts = 0
            while attempts < MAX_SEARCH_ATTEMPTS:
                idx = raw.find(open_char, idx)
                if idx == -1:
                    break
                try:
                    candidate = _apply_all_fixes(
                        raw[idx:]
                    )
                    candidate = _clean_json(candidate)
                    data      = json.loads(candidate)
                    if isinstance(data, expected_type):
                        log.debug(
                            "  ✓ %s: search at %d",
                            operation, idx,
                        )
                        return data
                except (
                    json.JSONDecodeError, ValueError
                ):
                    pass
                idx      += 1
                attempts += 1
    except Exception:
        pass

    raise AIEnrichmentError(
        f"❌ {operation} returned invalid JSON.\n"
        f"   Raw preview: {raw[:300]}..."
    )


# ═══════════════════════════════════════════════════
# SAFE HELPERS
# ═══════════════════════════════════════════════════

def _safe_truncate(text: str, max_chars: int) -> str:
    if not text:
        return ""
    if len(text) <= max_chars:
        return text
    truncated  = text[:max_chars]
    last_space = max(
        truncated.rfind(" "),
        truncated.rfind("\n"),
        truncated.rfind("."),
        truncated.rfind("،"),
    )
    if last_space > max_chars * 0.75:
        truncated = truncated[:last_space]
    return truncated.strip() + "..."


def _safe_title(
    title:     Any,
    max_chars: int = TITLE_MAX_CHARS,
) -> str:
    title = str(title or "").strip()
    if len(title) <= max_chars:
        return title
    return title[:max_chars].strip() + "..."


def _filter_abstract_keywords(
    keywords: list[str],
) -> list[str]:
    result: list[str] = []
    for kw in keywords:
        words          = kw.lower().split()
        abstract_count = sum(
            1 for w in words if w in ABSTRACT_WORDS
        )
        total = len(words)
        if abstract_count == 0:
            result.append(kw)
        elif abstract_count < total:
            clean = [
                w for w in words
                if w not in ABSTRACT_WORDS
            ]
            if len(clean) >= 2:
                result.append(" ".join(clean))
    return result


# ═══════════════════════════════════════════════════
# CONTENT CACHE
# ═══════════════════════════════════════════════════

_analysis_cache:      dict[str, dict] = {}
_analysis_cache_lock: threading.RLock = threading.RLock()


def _make_cache_key(text: str, lang: str) -> str:
    content = f"{lang}:{text}"
    return hashlib.sha256(
        content.encode("utf-8")
    ).hexdigest()[:32]


def _get_cached_analysis(
    title:   str,
    content: str,
    lang:    str,
) -> Optional[dict]:
    key = _make_cache_key(f"{title}:{content}", lang)
    with _analysis_cache_lock:
        return _analysis_cache.get(key)


def _set_cached_analysis(
    title:    str,
    content:  str,
    lang:     str,
    analysis: dict,
) -> None:
    key = _make_cache_key(f"{title}:{content}", lang)
    with _analysis_cache_lock:
        if len(_analysis_cache) >= ANALYSIS_CACHE_SIZE:
            first_key = next(iter(_analysis_cache))
            del _analysis_cache[first_key]
        _analysis_cache[key] = analysis


# ═══════════════════════════════════════════════════
# CORE GROQ CALLER
# ═══════════════════════════════════════════════════

def _looks_like_truncated_thinking(content: str) -> bool:
    """
    ✅ v7.6 — يكتشف إذا الرد انقطع أثناء "تفكير" الموديل
    (مثل qwen) قبل ما يوصل لإنتاج الـ JSON الفعلي.

    الحالة: فيه <think> بدون </think> مقابلة، وما فيه أي
    مؤشر JSON حقيقي ({ أو [) بعد آخر ظهور لـ <think>.
    """
    if "<think>" not in content:
        return False
    if "</think>" in content:
        return False
    # فيه <think> مفتوح بدون إغلاق — تحقق هل فيه JSON فعلي بعده
    idx = content.rfind("<think>")
    tail = content[idx:]
    if "{" in tail or "[" in tail:
        # ممكن يكون بدأ يكتب JSON فعلاً رغم عدم إغلاق الوسم —
        # نعطيه فرصة، الفشل الفعلي سيُكتشف لاحقاً في parsing
        return False
    return True


def _call_groq(
    prompt:         str,
    max_tokens:     int   = 800,
    temperature:    float = 0.7,
    operation_name: str   = "AI call",
) -> str:
    _ensure_keys_loaded()

    if not _groq_keys:
        raise AIEnrichmentError("GROQ_API_KEY not found in environment.")

    with _groq_lock:
        n_keys = len(_groq_keys)

    total_attempts     = n_keys * MAX_RETRIES_PER_KEY
    last_error         = None
    models_to_try      = list(MODELS_PRIORITY)
    current_model      = models_to_try[0]

    for attempt in range(total_attempts + len(MODELS_PRIORITY) * 3):
        with _groq_lock:
            n_keys  = len(_groq_keys)
            cur_idx = _groq_index % n_keys
            key     = _groq_keys[cur_idx]

        client = _get_client(key)

        # ✅ v7.6 — زيادة التوكنز تلقائياً لموديلات التفكير
        # (reasoning models) عشان "يكفيها" تفكر ثم تكتب الـ JSON
        # بدون ما ينقطع الرد في منتصف <think>.
        effective_max_tokens = max_tokens
        if current_model in REASONING_MODELS:
            effective_max_tokens = min(
                int(max_tokens * REASONING_TOKEN_MULTIPLIER),
                REASONING_TOKEN_CAP,
            )

        try:
            log.info(
                "  🤖 %s [key#%d/%d attempt %d model=%s]...",
                operation_name,
                cur_idx + 1, n_keys,
                attempt + 1,
                current_model,
            )

            resp = client.chat.completions.create(
                model       = current_model,
                messages    = [{"role": "user", "content": prompt}],
                temperature = temperature,
                max_tokens  = effective_max_tokens,
            )

            if not resp.choices:
                raise ValueError("Groq returned empty choices")

            choice       = resp.choices[0]
            content      = choice.message.content or ""
            finish_reason = getattr(choice, "finish_reason", None)

            if not content.strip():
                if len(models_to_try) > 1:
                    old = models_to_try.pop(0)
                    current_model = models_to_try[0]
                    log.warning("  🔄 Empty response from '%s' → trying '%s'", old, current_model)
                    continue
                raise ValueError("Empty response from Groq")

            # ✅ v7.6 — اكتشاف القطع أثناء التفكير (reasoning
            # truncation): الرد انتهى بسبب "length" وفيه <think>
            # بدون إغلاق ولا أثر JSON → لا فائدة من محاولة parsing،
            # بدّل الموديل فوراً بدل ما نرجّع رد فاسد للمتصل.
            if (
                finish_reason == "length"
                and _looks_like_truncated_thinking(content)
            ):
                if len(models_to_try) > 1:
                    old = models_to_try.pop(0)
                    current_model = models_to_try[0]
                    log.warning(
                        "  🔄 Truncated mid-thinking on '%s' "
                        "(finish_reason=length) → switching to '%s'",
                        old, current_model,
                    )
                    continue
                else:
                    log.warning(
                        "  ⚠️  Truncated mid-thinking and no more "
                        "models to try — returning content as-is "
                        "(caller will attempt JSON recovery)."
                    )

            return content.strip()

        except Exception as e:
            err_str    = str(e)
            last_error = err_str

            # 🟢 إذا كان الخطأ بسبب الموديل غير موجود (404) أو بسبب انتهاء الحصة (429/413/RPD):
            # نقوم بالانتقال للموديل التالي فوراً دون الانتظار وتكرار الفشل!
            if (
                "404" in err_str or 
                "model_not_found" in err_str or 
                "does not exist" in err_str or
                "429" in err_str or
                "rate limit" in err_str.lower() or
                "rate_limit" in err_str.lower() or
                "requests per day" in err_str.lower() or
                "rpd" in err_str.lower() or
                "413" in err_str or
                "payload too large" in err_str.lower()
            ):
                if len(models_to_try) > 1:
                    old = models_to_try.pop(0)
                    current_model = models_to_try[0]
                    log.warning(
                        "  🔄 Quota/Error on model '%s' → Switching immediately to '%s'!",
                        old, current_model
                    )
                    _rotate_groq_key()
                    continue
                else:
                    log.warning("  🛑 Rate limit on all models — rotating key...")
                    _rotate_groq_key()
                    time.sleep(2)

            elif "401" in err_str or "403" in err_str:
                log.warning("  🔑 Auth error [key#%d] — rotating...", cur_idx + 1)
                _rotate_groq_key()
                time.sleep(1)

            else:
                log.warning("  ⚠️  Error: %s — retrying...", err_str[:80])
                _rotate_groq_key()
                time.sleep(1)

    raise AIEnrichmentError(
        f"❌ {operation_name} FAILED after {attempt + 1} attempts.\n"
        f"   Last error: {last_error[:200] if last_error else '?'}"
    )

# ═══════════════════════════════════════════════════
# DATA EXTRACTION HELPERS
# ═══════════════════════════════════════════════════

def _extract_string_list(
    data:      Any,
    min_count: int,
) -> Optional[list[str]]:
    if not isinstance(data, list):
        return None
    values = [
        str(x).strip() for x in data
        if str(x).strip()
    ]
    if len(values) >= min_count:
        return values
    return None


def _extract_lang_value(
    data:      dict,
    lang:      str,
    operation: str,
    min_count: int = 3,
) -> list[str]:
    if lang in data:
        result = _extract_string_list(
            data[lang], min_count
        )
        if result:
            return result

    lang_aliases: dict[str, list[str]] = {
        "ar": ["arabic", "arab", "ar_content"],
        "fr": ["french", "français", "fr_content"],
        "en": ["english", "eng", "en_content"],
    }
    for alias in lang_aliases.get(lang, []):
        if alias in data:
            result = _extract_string_list(
                data[alias], min_count
            )
            if result:
                log.warning(
                    "  ⚠️  %s: using alias '%s'",
                    operation, alias,
                )
                return result

    if "en" in data and lang != "en":
        result = _extract_string_list(
            data["en"], min_count
        )
        if result:
            log.warning(
                "  ⚠️  %s: falling back to 'en'",
                operation,
            )
            return result

    for key, val in data.items():
        result = _extract_string_list(val, min_count)
        if result:
            log.warning(
                "  ⚠️  %s: last resort '%s'",
                operation, key,
            )
            return result

    raise AIEnrichmentError(
        f"❌ {operation}: cannot find valid "
        f"'{lang}' data.\n"
        f"   Keys found: {list(data.keys())}"
    )


def _extract_en_value(
    data:      dict,
    operation: str,
    min_count: int = 3,
) -> list[str]:
    if "en" in data:
        result = _extract_string_list(
            data["en"], min_count
        )
        if result:
            return result
    for key, val in reversed(list(data.items())):
        result = _extract_string_list(val, min_count)
        if result:
            log.warning(
                "  ⚠️  %s EN: using key '%s'",
                operation, key,
            )
            return result
    raise AIEnrichmentError(
        f"❌ {operation}: cannot find valid 'en' data."
    )


# ═══════════════════════════════════════════════════
# 1️⃣ CONTENT ANALYSIS
# ═══════════════════════════════════════════════════

REQUIRED_ANALYSIS_FIELDS = (
    "content_type", "primary_emotion",
    "intensity",    "tone",
)


def analyze_content(
    title:   str,
    content: str,
    lang:    str = "ar",
) -> dict:
    _validate_lang(lang)

    cached = _get_cached_analysis(title, content, lang)
    if cached:
        log.debug("  ♻️  Analysis from cache")
        return cached

    lang_name    = LANG_NAMES.get(lang, "Arabic")
    safe_title   = _safe_title(title, TITLE_MAX_CHARS)
    safe_content = _safe_truncate(
        content, CONTENT_ANALYSIS_CHARS
    )

    prompt = (
        f"Analyze this {lang_name} video content "
        f"and return JSON ONLY.\n\n"
        f"TITLE: {safe_title}\n\n"
        f"CONTENT:\n{safe_content}\n\n"
        f"Allowed values:\n"
        f"- content_type: psychology, relationships, business, lifestyle, motivation, education, health, spirituality, finance, social_skills\n"
        f"- primary_emotion: curiosity, fear, desire, anger, hope, sadness, joy, awe, surprise\n"
        f"- tone: energetic, calm, emotional, inspirational, mysterious, urgent\n"
        f"- intensity: integer between 1 and 10\n\n"
        f"Return ONLY a JSON object matching this structure "
        f"(no markdown, no explanation):\n"
        f'{{"content_type":"psychology",'
        f'"primary_emotion":"curiosity",'
        f'"secondary_emotions":["hope","awe"],'
        f'"intensity":7,'
        f'"audience":"short",'
        f'"tone":"energetic",'
        f'"topic_summary":"one sentence in {lang_name}"}}'
    )

    raw  = _call_groq(
        prompt,
        max_tokens     = 800,
        temperature    = 0.3,
        operation_name = "Content Analysis",
    )
    data = _parse_json_response(
        raw, dict, "Content Analysis"
    )

    for field in REQUIRED_ANALYSIS_FIELDS:
        if field not in data:
            raise AIEnrichmentError(
                f"❌ Content Analysis missing field: "
                f"{field}"
            )

    # معالجة استخراج الرقم للحقل intensity بشكل محصن ضد أي خطأ
    raw_intensity = data.get("intensity", DEFAULT_INTENSITY)
    try:
        if isinstance(raw_intensity, (int, float)):
            val = int(raw_intensity)
        else:
            digits = re.findall(r"\d+", str(raw_intensity))
            val = int(digits[0]) if digits else DEFAULT_INTENSITY
    except Exception:
        val = DEFAULT_INTENSITY

    data["intensity"] = max(1, min(10, val))

    _set_cached_analysis(title, content, lang, data)

    log.info(
        "  ✅ Analysis: %s | %s | intensity=%d/10",
        data['content_type'],
        data['primary_emotion'],
        data['intensity'],
    )
    return data


# ═══════════════════════════════════════════════════
# 2️⃣ TAG SUGGESTION
# ═══════════════════════════════════════════════════

def _normalize_suggested_tag(tag: str) -> str:
    tag = str(tag).strip().lower()
    if tag in VALID_TAG_NAMES:
        return tag
    corrected, _ = auto_correct_tag(tag)
    return corrected if corrected else DEFAULT_TAG


def _suggest_tags_batch(
    batch:     list[dict],
    context:   dict,
    lang:      str,
    start_idx: int = 0,
) -> list[str]:
    if not batch:
        return []

    lang_name      = LANG_NAMES.get(lang, "Arabic")
    available_tags = ", ".join(VALID_TAG_NAMES)
    sentences_text = "\n".join(
        f"{start_idx + i + 1}. "
        f"{_safe_truncate(s['text'], SENTENCE_TAGS_CHARS)}"
        for i, s in enumerate(batch)
    )

    prompt = (
        f"Choose emotional tags for video narration "
        f"sentences.\n\n"
        f"Context: "
        f"{context.get('content_type')} content, "
        f"{context.get('tone')} tone.\n"
        f"Language: {lang_name}\n\n"
        f"Available tags:\n{available_tags}\n\n"
        f"Sentences ({len(batch)} total):\n"
        f"{sentences_text}\n\n"
        f"Return ONLY a JSON array of exactly "
        f"{len(batch)} tags (no markdown):\n"
        f'Example: ["intrigue","desire","confident"]'
    )

    raw = _call_groq(
        prompt,
        max_tokens     = 300,
        temperature    = 0.5,
        operation_name = (
            f"Tag Suggestion "
            f"[{start_idx + 1}"
            f"-{start_idx + len(batch)}]"
        ),
    )
    tags = _parse_json_response(
        raw, list, "Tag Suggestion"
    )

    cleaned = [
        _normalize_suggested_tag(t)
        for t in tags[: len(batch)]
    ]
    while len(cleaned) < len(batch):
        cleaned.append(DEFAULT_TAG)

    return cleaned


def suggest_tags_for_sentences(
    sentences_needing_tags: list[dict],
    context:                dict,
    lang:                   str = "ar",
    content_mode:           str = "short",
) -> list[str]:
    if not sentences_needing_tags:
        return []

    n = len(sentences_needing_tags)

    if (
        content_mode == "short" or
        n <= BATCH_SIZE_TAGS
    ):
        result = _suggest_tags_batch(
            sentences_needing_tags, context, lang, 0
        )
        log.info(
            "  ✅ Suggested %d tags", len(result)
        )
        return result

    log.info(
        "  📦 Tag Suggestion: "
        "%d sentences → batches of %d",
        n, BATCH_SIZE_TAGS,
    )
    all_tags: list[str] = []

    for start in range(0, n, BATCH_SIZE_TAGS):
        end   = min(start + BATCH_SIZE_TAGS, n)
        batch = sentences_needing_tags[start:end]

        log.info(
            "  📦 Tag Batch [%d-%d/%d]...",
            start + 1, end, n,
        )
        batch_tags = _suggest_tags_batch(
            batch, context, lang, start
        )
        all_tags.extend(batch_tags)

        if end < n:
            time.sleep(BATCH_SLEEP)

    log.info(
        "  ✅ Suggested %d tags total", len(all_tags)
    )
    return all_tags


# ═══════════════════════════════════════════════════
# 3️⃣ POWER WORDS
# ═══════════════════════════════════════════════════

def _filter_power_words(
    words: list,
    count: int,
) -> list[str]:
    seen:   set[str]  = set()
    result: list[str] = []
    for w in words[:count]:
        if not isinstance(w, str):
            continue
        w = w.strip()
        if (
            w
            and w.lower() not in seen
            and len(w) >= 2
            and " " not in w
        ):
            result.append(w)
            seen.add(w.lower())
    return result


def generate_power_words(
    content: str,
    context: dict,
    lang:    str = "ar",
    count:   int = 10,
) -> list[str]:
    if not content or not content.strip():
        raise AIEnrichmentError(
            "Cannot generate power words from empty content"
        )

    lang_name    = LANG_NAMES.get(lang, "Arabic")
    safe_content = _safe_truncate(
        content, CONTENT_POWER_CHARS
    )

    prompt = (
        f"Extract {count} powerful single words "
        f"from this {lang_name} text.\n\n"
        f"Content type: "
        f"{context.get('content_type', 'general')}\n"
        f"Primary emotion: "
        f"{context.get('primary_emotion', 'curiosity')}\n\n"
        f"Rules:\n"
        f"- Single words only (no phrases)\n"
        f"- Must exist in the text\n"
        f"- Trigger strong emotions\n"
        f"- Must be in {lang_name}\n\n"
        f"Text:\n{safe_content}\n\n"
        f"Return ONLY a JSON array of {count} words "
        f"(no markdown):\n"
        f'Example: ["word1","word2","word3"]'
    )

    raw    = _call_groq(
        prompt,
        max_tokens     = 300,
        temperature    = 0.6,
        operation_name = (
            f"Power Words ({lang.upper()})"
        ),
    )
    words  = _parse_json_response(
        raw, list, "Power Words"
    )
    result = _filter_power_words(words, count)

    if len(result) < 3:
        raise AIEnrichmentError(
            f"❌ Power Words: only {len(result)} valid"
        )

    log.info(
        "  ✅ Power Words (%s): %d words",
        lang.upper(), len(result),
    )
    return result


# ═══════════════════════════════════════════════════
# 4️⃣ VISUAL KEYWORDS
# ═══════════════════════════════════════════════════

def _generate_visual_keywords_batch(
    sentences: list[str],
    title:     str,
    context:   dict,
    tags:      list[str],
    batch_num: int = 1,
) -> list[list[str]]:
    n            = len(sentences)
    content_type = context.get("content_type", "general")
    emotion      = context.get(
        "primary_emotion", "neutral"
    )
    safe_title   = _safe_title(title, TITLE_SHORT_CHARS)

    tokens_per_sentence = 40
    min_tokens          = 400
    max_allowed         = 4000
    calculated_tokens   = max(
        min_tokens,
        min(n * tokens_per_sentence, max_allowed),
    )

    topic_examples = _get_topic_examples(content_type)
    examples_str   = "\n".join(
        f'  GOOD: "{ex}"' for ex in topic_examples
    )

    sentences_text = ""
    for i, sentence in enumerate(sentences):
        tag         = (
            tags[i] if i < len(tags)
            else "information"
        )
        visual_hint = TAG_VISUAL_STYLE.get(
            tag, DEFAULT_VISUAL_STYLE
        )
        safe_sent   = _safe_truncate(
            sentence, SENTENCE_KEYWORDS_CHARS
        )
        sentences_text += (
            f"Sentence {i + 1}:\n"
            f"  Emotion tag: [{tag}]\n"
            f"  Text: \"{safe_sent}\"\n"
            f"  Visual: {visual_hint}\n\n"
        )

    prompt = (
        f"You are a professional video editor "
        f"searching stock footage.\n"
        f"Generate PRACTICAL search keywords "
        f"for Pexels and Pixabay.\n\n"
        f"Video topic: \"{safe_title}\"\n"
        f"Content type: {content_type} | "
        f"Emotion: {emotion}\n\n"
        f"=== CRITICAL RULES ===\n"
        f"1. Keywords must work as REAL search queries\n"
        f"2. ALWAYS include: person/people/face/hands\n"
        f"3. Include a clear ACTION or EMOTION word\n"
        f"4. 3-5 words per keyword\n"
        f"5. English only\n"
        f"6. Be SPECIFIC and CONCRETE\n\n"
        f"=== FORBIDDEN WORDS ===\n"
        f"mystery, shadows, silence, soul, journey, "
        f"ethereal, longing, abyss, whisper, darkness, "
        f"dream, essence\n\n"
        f"=== GOOD EXAMPLES for {content_type} ===\n"
        f"{examples_str}\n\n"
        f"=== BAD EXAMPLES ===\n"
        f'  BAD: "dark shadows mysterious atmosphere"\n'
        f'  GOOD: "person serious face talking camera"\n\n'
        f"=== SENTENCES ({n} total) ===\n"
        f"{sentences_text}"
        f"=== OUTPUT (CRITICAL) ===\n"
        f"Return ONLY a JSON array of {n} arrays "
        f"(no markdown):\n"
        f'[["kw1","kw2","kw3"],["kw1","kw2","kw3"]]'
    )

    fallbacks = [
        "person serious face talking camera",
        "emotional person close up expression",
        "confident person speaking direct",
    ]

    raw = ""
    try:
        raw  = _call_groq(
            prompt,
            max_tokens     = calculated_tokens,
            temperature    = 0.3,
            operation_name = (
                f"Visual Keywords "
                f"(batch {batch_num}, {n} sentences)"
            ),
        )
        data = _parse_json_response(
            raw, list, "Visual Keywords"
        )
        result: list[list[str]] = []

        for i in range(n):
            tag = (
                tags[i] if i < len(tags)
                else "information"
            )

            if (
                i < len(data) and
                isinstance(data[i], list)
            ):
                raw_kws = [
                    str(k).strip()
                    for k in data[i][:5]
                    if str(k).strip()
                ]
                # ✅ v7.7 — استبعد placeholders وهمية (kw1, kw2...)
                # قبل ما تدخل فلترة الكلمات المجردة، عشان ما
                # توصل حرفياً لبحث Pexels عن "kw1".
                raw_kws = [
                    k for k in raw_kws
                    if not _is_placeholder_keyword(k)
                ]
                kws = _filter_abstract_keywords(raw_kws)
                while len(kws) < 3:
                    fb = fallbacks[len(kws) % 3]
                    if fb not in kws:
                        kws.append(fb)
                    else:
                        kws.append(
                            f"person {tag} expression face"
                        )
            else:
                log.debug(
                    "  ⚠️  Sentence %d using fallback.",
                    i + 1,
                )
                kws = list(fallbacks)

            result.append(kws[:3])
            log.info(
                "     [%d/%d] [%s] → %s",
                i + 1, n, tag, kws[:3],
            )

        return result

    except Exception as e:
        log.warning(
            "  ⚠️  Keywords batch %d failed: %s",
            batch_num, e,
        )
        if raw:
            log.debug(
                "  Raw (first 500): %s", raw[:500]
            )
        return [list(fallbacks) for _ in range(n)]


def generate_visual_keywords(
    sentences:    list[str],
    title:        str,
    context:      dict,
    tags:         Optional[list[str]] = None,
    content_mode: str                 = "short",
) -> list[list[str]]:
    if not sentences:
        raise AIEnrichmentError(
            "Cannot generate keywords for empty sentences"
        )

    _validate_mode(content_mode)

    n = len(sentences)
    if tags is None:
        tags = ["information"] * n
    elif len(tags) != n:
        log.warning(
            "  ⚠️  tags length (%d) != sentences (%d)"
            " — fixing",
            len(tags), n,
        )
        if len(tags) < n:
            tags = (
                tags +
                ["information"] * (n - len(tags))
            )
        else:
            tags = tags[:n]

    log.info(
        "  🎬 Generating B-Roll keywords: %d sentences...",
        n,
    )

    if (
        content_mode == "short" or
        n <= BATCH_SIZE_KEYWORDS
    ):
        return _generate_visual_keywords_batch(
            sentences, title, context, tags,
            batch_num=1,
        )

    log.info(
        "  📦 Long mode — batching %d sentences "
        "(batches of %d)",
        n, BATCH_SIZE_KEYWORDS,
    )
    result:    list[list[str]] = []
    batch_num: int             = 0

    for start in range(0, n, BATCH_SIZE_KEYWORDS):
        end       = min(start + BATCH_SIZE_KEYWORDS, n)
        batch_num += 1

        log.info(
            "  📦 Keywords Batch [%d-%d/%d]...",
            start + 1, end, n,
        )
        batch_res = _generate_visual_keywords_batch(
            sentences[start:end],
            title,
            context,
            tags[start:end],
            batch_num=batch_num,
        )
        result.extend(batch_res)

        if end < n:
            time.sleep(BATCH_SLEEP)

    log.info(
        "  ✅ Visual Keywords: %d sentences × 3",
        len(result),
    )
    return result


# ═══════════════════════════════════════════════════
# BILINGUAL HELPERS
# ═══════════════════════════════════════════════════

def _get_bilingual_json_format(
    lang:    str,
    example: str = '["item1","item2",...]',
) -> str:
    if lang == "en":
        return f'{{"en": {example}}}'
    return (
        f'{{"{lang}": {example}, "en": {example}}}'
    )


def _get_lang_instruction(lang: str) -> str:
    if lang == "en":
        return "in English"
    lang_name = LANG_NAMES.get(lang, "Arabic")
    return f"in {lang_name} AND English"


def _generate_bilingual_content(
    operation_name: str,
    prompt:         str,
    max_tokens:     int,
    temperature:    float,
    lang:           str,
    count:          int,
    min_count:      int,
) -> dict[str, list[str]]:
    raw  = _call_groq(
        prompt,
        max_tokens     = max_tokens,
        temperature    = temperature,
        operation_name = operation_name,
    )
    data = _parse_json_response(
        raw, dict, operation_name
    )

    if lang == "en":
        en_values = _extract_en_value(
            data, operation_name, min_count,
        )
        result = {"en": en_values[:count]}
        if len(result["en"]) < min_count:
            raise AIEnrichmentError(
                f"❌ {operation_name}: not enough data"
            )
        log.info(
            "  ✅ %s: EN(%d)",
            operation_name, len(result['en']),
        )
        return result

    lang_values = _extract_lang_value(
        data, lang, operation_name, min_count,
    )
    en_values   = _extract_en_value(
        data, operation_name, min_count,
    )
    result = {
        lang: lang_values[:count],
        "en": en_values[:count],
    }
    if (
        len(result[lang]) < min_count or
        len(result["en"]) < min_count
    ):
        raise AIEnrichmentError(
            f"❌ {operation_name}: not enough data"
        )
    log.info(
        "  ✅ %s: %s(%d) | EN(%d)",
        operation_name,
        lang.upper(), len(result[lang]),
        len(result['en']),
    )
    return result


# ═══════════════════════════════════════════════════
# ✅ v7.5 — STRICT PROMPT BUILDER
# ═══════════════════════════════════════════════════

def _build_list_prompt(
    operation:  str,
    count:      int,
    lang:       str,
    safe_title: str,
    context:    dict,
    rules:      str,
    example:    str,
    correct_ex: str = "",
    wrong_ex:   str = "",
) -> str:
    """
    ✅ v7.5 — prompt أكثر صرامة مع CORRECT/WRONG examples.
    """
    lang_instruction = _get_lang_instruction(lang)
    json_format      = _get_bilingual_json_format(
        lang, example
    )
    content_type = context.get(
        "content_type", "general"
    )
    emotion      = context.get(
        "primary_emotion", "curiosity"
    )

    prompt = (
        f"Generate {count} {operation} "
        f"{lang_instruction}.\n\n"
        f'Title: "{safe_title}" | '
        f"Type: {content_type} | "
        f"Emotion: {emotion}\n\n"
        f"STRICT RULES:\n{rules}\n\n"
    )

    if correct_ex and wrong_ex:
        prompt += (
            f"CORRECT FORMAT:\n{correct_ex}\n\n"
            f"WRONG FORMAT (DO NOT DO THIS):\n"
            f"{wrong_ex}\n\n"
        )

    prompt += (
        f"CRITICAL: Every string item MUST have "
        f'double quotes. Example: "item"\n'
        f"Return ONLY valid JSON (no markdown):\n"
        f"{json_format}"
    )

    return prompt


# ═══════════════════════════════════════════════════
# 5️⃣ PATTERN INTERRUPTS
# ═══════════════════════════════════════════════════

def _pattern_interrupts_fallback(lang: str) -> dict[str, list[str]]:
    """✅ v7.6 — fallback جاهز لو فشلت كل محاولات الـ AI."""
    fb: dict[str, list[str]] = {
        "en": list(_PATTERN_INTERRUPTS_FALLBACK["en"])
    }
    if lang != "en":
        fb[lang] = list(
            _PATTERN_INTERRUPTS_FALLBACK.get(
                lang, _PATTERN_INTERRUPTS_FALLBACK["en"]
            )
        )
    return fb


def generate_pattern_interrupts(
    title:   str,
    content: str,
    context: dict,
    lang:    str = "ar",
    count:   int = 6,
) -> dict[str, list[str]]:
    safe_title = _safe_title(title, TITLE_SHORT_CHARS)

    prompt = _build_list_prompt(
        operation  = "SHORT pattern interrupt phrases",
        count      = count,
        lang       = lang,
        safe_title = safe_title,
        context    = context,
        rules      = (
            "1. 1-4 words MAX per phrase\n"
            "2. Shocking and attention-grabbing\n"
            "3. Each item MUST have double quotes\n"
            "4. Emojis allowed INSIDE the quotes\n"
            "5. NO missing commas between items"
        ),
        example    = (
            '["phrase1 😮","phrase2","phrase3"]'
        ),
        correct_ex = (
            '{"en": ["Wait...", "No way!", "Real talk"]}'
        ),
        wrong_ex   = (
            '{"en": [Wait..., No way!, Real talk]}'
        ),
    )

    # ✅ v7.6 — try/except + fallback بدل ما نوقف الفيديو كامل
    try:
        return _generate_bilingual_content(
            operation_name = "Pattern Interrupts",
            prompt         = prompt,
            max_tokens     = 800,
            temperature    = 0.7,
            lang           = lang,
            count          = count,
            min_count      = 3,
        )
    except Exception as e:
        log.warning(
            "  ⚠️  Pattern Interrupts failed: %s "
            "— using fallback",
            e,
        )
        return _pattern_interrupts_fallback(lang)


# ═══════════════════════════════════════════════════
# 6️⃣ ENGAGEMENT QUESTIONS
# ═══════════════════════════════════════════════════

def _engagement_questions_fallback(lang: str) -> dict[str, list[str]]:
    """✅ v7.6 — fallback جاهز لو فشلت كل محاولات الـ AI."""
    fb: dict[str, list[str]] = {
        "en": list(_ENGAGEMENT_QUESTIONS_FALLBACK["en"])
    }
    if lang != "en":
        fb[lang] = list(
            _ENGAGEMENT_QUESTIONS_FALLBACK.get(
                lang, _ENGAGEMENT_QUESTIONS_FALLBACK["en"]
            )
        )
    return fb


def generate_engagement_questions(
    title:   str,
    content: str,
    context: dict,
    lang:    str = "ar",
    count:   int = 6,
) -> dict[str, list[str]]:
    safe_title = _safe_title(title, TITLE_SHORT_CHARS)

    prompt = _build_list_prompt(
        operation  = "SHORT engagement questions",
        count      = count,
        lang       = lang,
        safe_title = safe_title,
        context    = context,
        rules      = (
            "1. 3-7 words per question\n"
            "2. Each item MUST have double quotes\n"
            "3. Each item MUST end with comma "
            "EXCEPT the last\n"
            "4. Emojis allowed INSIDE the quotes\n"
            "5. NO line breaks inside a string value\n"
            "6. Questions encourage comments"
        ),
        example    = (
            '["Question one? 🤔","Question two? 😮",'
            '"Question three? 💭"]'
        ),
        correct_ex = (
            '{"en": ["What do you think? 🤔", '
            '"Have you felt this? 😮", '
            '"Can you relate? 💭"]}'
        ),
        wrong_ex   = (
            '{"en": [What do you think, '
            'Have you felt this]}'
        ),
    )

    # ✅ v7.6 — try/except + fallback بدل ما نوقف الفيديو كامل
    try:
        return _generate_bilingual_content(
            operation_name = "Engagement Questions",
            prompt         = prompt,
            max_tokens     = 800,
            temperature    = 0.7,
            lang           = lang,
            count          = count,
            min_count      = 3,
        )
    except Exception as e:
        log.warning(
            "  ⚠️  Engagement Questions failed: %s "
            "— using fallback",
            e,
        )
        return _engagement_questions_fallback(lang)


# ═══════════════════════════════════════════════════
# 7️⃣ HASHTAGS — v7.5 strict prompt
# ═══════════════════════════════════════════════════

def _clean_hashtags(tags: list) -> list[str]:
    result: list[str] = []
    for tag in tags:
        tag = str(tag).strip()
        if not tag:
            continue
        if not tag.startswith("#"):
            tag = "#" + tag.replace(" ", "_")
        result.append(tag)
    return result


def _hashtags_fallback(
    lang:         str,
    content_type: str,
) -> dict[str, list[str]]:
    """✅ v7.6 — fallback جاهز لو فشلت كل محاولات الـ AI."""
    en_tags = list(_HASHTAGS_FALLBACK_EN)
    ct_tag  = "#" + re.sub(r"\s+", "_", content_type.strip())
    if ct_tag not in en_tags and len(ct_tag) > 1:
        en_tags.append(ct_tag)

    fb: dict[str, list[str]] = {"en": en_tags}
    if lang != "en":
        fb[lang] = list(
            _HASHTAGS_FALLBACK_BY_LANG.get(
                lang, _HASHTAGS_FALLBACK_EN
            )
        )
    return fb


def generate_hashtags(
    title:   str,
    content: str,
    context: dict,
    lang:    str = "ar",
    count:   int = 12,
) -> dict[str, list[str]]:
    """
    ✅ v7.5 — prompt أصرم + temperature=0.5
    ✅ v7.6 — try/except + fallback بدل ما نوقف الفيديو كامل
    """
    content_type     = context.get(
        "content_type", "general"
    )
    safe_title       = _safe_title(
        title, TITLE_SHORT_CHARS
    )
    lang_instruction = _get_lang_instruction(lang)
    json_format      = _get_bilingual_json_format(
        lang, '["#tag1","#tag2","#tag3"]'
    )

    prompt = (
        f"Generate {count} hashtags "
        f"{lang_instruction}.\n\n"
        f'Title: "{safe_title}" | '
        f"Type: {content_type}\n\n"
        f"STRICT RULES:\n"
        f"1. Each hashtag MUST start with #\n"
        f"2. Use underscores instead of spaces\n"
        f"3. Each item MUST have double quotes\n"
        f"4. NO commas or special chars inside the tag\n"
        f"5. NO unclosed quotes\n\n"
        f"CORRECT:\n"
        f'{{"ar": ["#لا_تستسلم", "#النجاح"]}}\n\n'
        f"WRONG:\n"
        f'{{"ar": ["#tag, extra", "#bad"]}}\n\n'
        f"Return ONLY valid JSON (no markdown):\n"
        f"{json_format}"
    )

    try:
        raw  = _call_groq(
            prompt,
            max_tokens     = 700,
            temperature    = 0.5,  # ✅ أقل من 0.6
            operation_name = "Hashtags",
        )
        data = _parse_json_response(raw, dict, "Hashtags")

        if lang == "en":
            en_values = _extract_en_value(
                data, "Hashtags", 5
            )
            result    = {
                "en": _clean_hashtags(en_values[:count])
            }
            if len(result["en"]) < 5:
                raise AIEnrichmentError(
                    "❌ Hashtags: not enough EN tags"
                )
            log.info(
                "  ✅ Hashtags: EN(%d)",
                len(result['en']),
            )
            return result

        lang_values = _extract_lang_value(
            data, lang, "Hashtags", 5
        )
        en_values   = _extract_en_value(
            data, "Hashtags", 5
        )
        result = {
            lang: _clean_hashtags(lang_values[:count]),
            "en": _clean_hashtags(en_values[:count]),
        }
        if (
            len(result[lang]) < 5 or
            len(result["en"]) < 5
        ):
            raise AIEnrichmentError(
                "❌ Hashtags: not enough tags"
            )
        log.info(
            "  ✅ Hashtags: %s(%d) | EN(%d)",
            lang.upper(), len(result[lang]),
            len(result['en']),
        )
        return result

    except Exception as e:
        log.warning(
            "  ⚠️  Hashtags failed: %s — using fallback",
            e,
        )
        return _hashtags_fallback(lang, content_type)


# ═══════════════════════════════════════════════════
# 8️⃣ CAPTIONS — v7.5 ONE line rule
# ═══════════════════════════════════════════════════

def _extract_caption(data: dict, lang: str) -> str:
    if (
        lang in data
        and isinstance(data[lang], str)
        and data[lang].strip()
    ):
        return data[lang].strip()

    if (
        lang != "en"
        and "en" in data
        and isinstance(data["en"], str)
        and data["en"].strip()
    ):
        log.warning(
            "  ⚠️  Caption: falling back to 'en' "
            "(requested '%s')",
            lang,
        )
        return data["en"].strip()

    return ""


def _append_hashtags_to_caption(
    caption: str,
    tags:    list[str],
    limit:   int = CAPTION_HASHTAG_LIMIT,
) -> str:
    if not tags:
        return caption
    tags_str = " ".join(tags[:limit])
    return f"{caption}\n.\n.\n.\n{tags_str}"


def _captions_fallback(
    title:    str,
    context:  dict,
    hashtags: dict[str, list[str]],
    lang:     str,
) -> dict[str, str]:
    """✅ v7.6 — fallback جاهز لو فشلت كل محاولات الـ AI."""
    cta = _STREET_FALLBACK.get(lang, _STREET_FALLBACK["en"])
    base_caption = f"{title}\n\n{cta}"

    if lang == "en":
        en_tags = hashtags.get("en", [])
        caption = _append_hashtags_to_caption(
            base_caption, en_tags
        )
        return {"en": caption[:CAPTION_MAX_LENGTH]}

    lang_tags = hashtags.get(lang, hashtags.get("en", []))
    en_tags   = hashtags.get("en", [])
    lang_cap  = _append_hashtags_to_caption(
        base_caption, lang_tags
    )
    en_cap    = _append_hashtags_to_caption(
        f"{title}\n\n{_STREET_FALLBACK['en']}", en_tags
    )
    return {
        lang: lang_cap[:CAPTION_MAX_LENGTH],
        "en": en_cap[:CAPTION_MAX_LENGTH],
    }


def generate_captions(
    title:    str,
    content:  str,
    context:  dict,
    hashtags: dict[str, list[str]],
    lang:     str = "ar",
) -> dict[str, str]:
    """
    ✅ v7.5 — prompt أصرم مع ONE line rule
    ✅ v7.6 — try/except + fallback بدل ما نوقف الفيديو كامل
    """
    lang_name    = LANG_NAMES.get(lang, "Arabic")
    safe_title   = _safe_title(
        title, TITLE_SHORT_CHARS
    )
    safe_content = _safe_truncate(
        content, CONTENT_CAPTION_CHARS
    )

    if lang == "en":
        json_format      = '{"en": "caption on ONE line"}'
        lang_instruction = "in English"
    else:
        json_format = (
            f'{{"{lang}": "caption on ONE line", '
            f'"en": "caption on ONE line"}}'
        )
        lang_instruction = (
            f"in {lang_name} AND English"
        )

    prompt = (
        f"Write a social media caption "
        f"{lang_instruction}.\n\n"
        f'Title: "{safe_title}"\n'
        f"Type: {context.get('content_type')} | "
        f"Emotion: {context.get('primary_emotion')}\n\n"
        f"Content:\n{safe_content}\n\n"
        f"Rules:\n"
        f"- Strong hook (1 line)\n"
        f"- 2-3 lines of value\n"
        f"- Call-to-action + emojis\n"
        f"- NO hashtags in body\n\n"
        f"CRITICAL JSON RULES:\n"
        f"1. The entire caption MUST be on ONE line\n"
        f"2. Use \\n for line breaks "
        f"(NOT actual newlines)\n"
        f"3. NO multiline strings in JSON\n\n"
        f"CORRECT:\n"
        f'{{"{lang}": "Hook! 🔥\\nLine 2\\nCTA 👇"}}\n\n'
        f"WRONG:\n"
        f'{{"{lang}": "Hook! 🔥\nLine 2\nCTA 👇"}}\n\n'
        f"Return ONLY valid JSON (no markdown):\n"
        f"{json_format}"
    )

    try:
        raw  = _call_groq(
            prompt,
            max_tokens     = 800,
            temperature    = 0.6,  # ✅ أقل من 0.7
            operation_name = "Captions",
        )
        data = _parse_json_response(raw, dict, "Captions")

        lang_caption = _extract_caption(data, lang)
        en_caption   = (
            data["en"].strip()
            if (
                "en" in data and
                isinstance(data["en"], str)
            )
            else ""
        )

        if not lang_caption:
            raise AIEnrichmentError(
                f"❌ Captions: missing {lang} caption"
            )

        if not en_caption:
            en_caption = lang_caption

        if lang == "en":
            en_tags = hashtags.get("en", [])
            caption = _append_hashtags_to_caption(
                lang_caption, en_tags
            )
            result  = {"en": caption[:CAPTION_MAX_LENGTH]}
            log.info(
                "  ✅ Captions: EN(%d)",
                len(result['en']),
            )
            return result

        lang_tags    = hashtags.get(
            lang, hashtags.get("en", [])
        )
        en_tags      = hashtags.get("en", [])
        lang_caption = _append_hashtags_to_caption(
            lang_caption, lang_tags
        )
        en_caption   = _append_hashtags_to_caption(
            en_caption, en_tags
        )
        result = {
            lang: lang_caption[:CAPTION_MAX_LENGTH],
            "en": en_caption[:CAPTION_MAX_LENGTH],
        }
        log.info(
            "  ✅ Captions: %s(%d) | EN(%d)",
            lang.upper(), len(result[lang]),
            len(result['en']),
        )
        return result

    except Exception as e:
        log.warning(
            "  ⚠️  Captions failed: %s — using fallback",
            e,
        )
        return _captions_fallback(
            title, context, hashtags, lang
        )


# ═══════════════════════════════════════════════════
# 9️⃣ STREET DESCRIPTION
# ═══════════════════════════════════════════════════

def generate_street_description(
    title:   str,
    content: str,
    context: dict,
    lang:    str = "ar",
) -> str:
    style        = STREET_STYLE.get(
        lang, STREET_STYLE["en"]
    )
    content_type = context.get(
        "content_type", "general"
    )
    emotion      = context.get(
        "primary_emotion", "curiosity"
    )
    safe_title   = _safe_title(title, TITLE_MAX_CHARS)
    safe_content = _safe_truncate(
        content, CONTENT_STREET_CHARS
    )

    prompt = (
        f"Write a long social media description for "
        f"Facebook and YouTube.\n\n"
        f'Title: "{safe_title}"\n'
        f"Type: {content_type} | Emotion: {emotion}\n\n"
        f"Content:\n{safe_content}\n\n"
        f"Style: {style['style_name']}\n"
        f"{style['instructions']}\n\n"
        f"Instructions:\n"
        f"1. Start with a shocking/interesting hook\n"
        f"2. Explain content in detail (8-12 lines)\n"
        f"3. Add a real example or story\n"
        f"4. Strong call-to-action\n"
        f"5. Use lots of emojis\n"
        f"6. {style['hashtag_lang']}: "
        f"add 20-25 hashtags at end separated by:\n"
        f".\n.\n.\n\n"
        f"Write ONLY the description directly "
        f"(no JSON, no markdown)."
    )

    try:
        raw = _call_groq(
            prompt,
            max_tokens     = 1200,
            temperature    = 0.85,
            operation_name = (
                f"Street Description ({lang.upper()})"
            ),
        )
        description = raw.strip()
        if not description or len(description) < 100:
            raise ValueError(
                f"Too short: {len(description)} chars"
            )
        log.info(
            "  ✅ Street Description (%s): %d chars",
            lang.upper(), len(description),
        )
        return description

    except Exception as e:
        log.warning(
            "  ⚠️  Street Description failed: %s", e
        )
        cta = _STREET_FALLBACK.get(
            lang, _STREET_FALLBACK["en"]
        )
        return (
            f"{safe_title}\n\n"
            f"{cta}\n\n"
            f"#shorts #viral #{content_type}"
        )


# ═══════════════════════════════════════════════════
# 🔟 ACCENT COLORS
# ═══════════════════════════════════════════════════

def _validate_hex_colors(
    colors: list,
    limit:  int = 4,
) -> list[str]:
    valid: list[str] = []
    for color in colors[:limit]:
        color = str(color).strip().upper()
        if _HEX_PATTERN.match(color):
            valid.append(color)
    return valid


def _fill_default_colors(
    colors: list[str],
    target: int = 4,
) -> list[str]:
    default_idx = 0
    max_tries   = target * 2
    for _ in range(max_tries):
        if len(colors) >= target:
            break
        colors.append(
            DEFAULT_ACCENT_COLORS[
                default_idx %
                len(DEFAULT_ACCENT_COLORS)
            ]
        )
        default_idx += 1
    return colors[:target]


def suggest_accent_colors(context: dict) -> list[str]:
    emotion      = context.get(
        "primary_emotion", "curiosity"
    )
    content_type = context.get(
        "content_type", "general"
    )
    intensity    = context.get("intensity", 7)

    prompt = (
        f"Suggest 4 vibrant HEX accent colors.\n"
        f"Type: {content_type} | "
        f"Emotion: {emotion} | "
        f"Intensity: {intensity}/10\n\n"
        f"Return ONLY a JSON array of 4 HEX codes "
        f"(no markdown):\n"
        f'["#FF003C","#FFD700","#00FFFF","#39FF14"]'
    )

    try:
        raw = _call_groq(
            prompt,
            max_tokens     = 150,
            temperature    = 0.6,
            operation_name = "Accent Colors",
        )
        colors       = _parse_json_response(
            raw, list, "Accent Colors"
        )
        valid_colors = _validate_hex_colors(colors)

        if len(valid_colors) < 2:
            log.warning(
                "  ⚠️  Accent Colors: "
                "only %d valid — using defaults",
                len(valid_colors),
            )
            return list(DEFAULT_ACCENT_COLORS)

        final = _fill_default_colors(valid_colors)
        log.info("  ✅ Accent Colors: %s", final)
        return final

    except Exception as e:
        log.warning(
            "  ⚠️  Accent Colors failed: %s "
            "— using defaults",
            e,
        )
        return list(DEFAULT_ACCENT_COLORS)


# ═══════════════════════════════════════════════════
# 1️⃣1️⃣ HOOK KEYWORD
# ═══════════════════════════════════════════════════

PROMPT_PREFIXES_TO_STRIP = (
    "keyword:", "answer:", "result:", "→", ":",
)


def _clean_keyword_response(keyword: str) -> str:
    keyword = keyword.strip().split("\n")[0].strip()
    keyword = keyword.strip('"').strip("'").strip()
    for prefix in PROMPT_PREFIXES_TO_STRIP:
        if keyword.lower().startswith(prefix):
            keyword = keyword[len(prefix):].strip()
    return keyword


def generate_hook_keyword(
    title:   str,
    content: str,
    context: dict,
) -> str:
    if not content or not content.strip():
        return HOOK_FALLBACK

    content_type = context.get(
        "content_type", "general"
    )
    emotion      = context.get(
        "primary_emotion", "curiosity"
    )
    safe_title   = _safe_title(
        title, TITLE_SHORT_CHARS
    )

    prompt = (
        f"ONE powerful visual keyword for first "
        f"3 seconds of video.\n\n"
        f'Topic: "{safe_title}" | '
        f"Type: {content_type} | "
        f"Emotion: {emotion}\n\n"
        f"Rules:\n"
        f"- 3-5 words ONLY\n"
        f"- English only\n"
        f"- Must work as Pexels/Pixabay search query\n"
        f"- Include: person + action/emotion\n"
        f"- Intense and concrete\n\n"
        f"Examples:\n"
        f"  'person shocked face close up'\n"
        f"  'crying person emotional scene'\n"
        f"  'determined person walking forward'\n\n"
        f"Return ONLY the keyword phrase "
        f"(no quotes, no JSON, no markdown):"
    )

    try:
        raw      = _call_groq(
            prompt,
            max_tokens     = 50,
            temperature    = 0.5,
            operation_name = "Hook Keyword",
        )
        keyword  = _clean_keyword_response(raw)
        filtered = _filter_abstract_keywords([keyword])
        keyword  = (
            filtered[0]
            if filtered
            else HOOK_FALLBACK_KEYWORD
        )
        if not keyword or len(keyword) > 80:
            keyword = HOOK_FALLBACK_KEYWORD
        log.info(
            "  ✅ Hook keyword: '%s'", keyword
        )
        return keyword

    except Exception as e:
        log.warning(
            "  ⚠️  Hook keyword failed: %s", e
        )
        return HOOK_FALLBACK_KEYWORD


# ═══════════════════════════════════════════════════
# 1️⃣2️⃣ CUSTOM HOOK
# ═══════════════════════════════════════════════════

def generate_custom_hook(
    title:   str,
    content: str,
    context: dict,
    lang:    str = "ar",
) -> str:
    lang_name    = LANG_NAMES.get(lang, "Arabic")
    content_type = context.get(
        "content_type", "general"
    )
    emotion      = context.get(
        "primary_emotion", "curiosity"
    )
    tone         = context.get("tone", "mysterious")
    safe_title   = _safe_title(
        title, TITLE_SHORT_CHARS
    )
    safe_content = _safe_truncate(
        content, CONTENT_HOOK_CHARS
    )

    prompt = (
        f"ONE powerful hook sentence in {lang_name}.\n\n"
        f'Title: "{safe_title}"\n'
        f"Type: {content_type} | "
        f"Emotion: {emotion} | "
        f"Tone: {tone}\n\n"
        f"Content preview:\n{safe_content}\n\n"
        f"Rules:\n"
        f"- {lang_name} ONLY\n"
        f"- Maximum 10 words\n"
        f"- Instant curiosity or shock\n"
        f"- Sound like a secret\n\n"
        f'GOOD Arabic: '
        f'"90٪ من الناس لا يعرفون هذا السر"\n'
        f'GOOD French: '
        f'"Ce que personne ne te dit..."\n'
        f'GOOD English: '
        f'"Nobody tells you this truth"\n\n'
        f"Return ONLY the hook sentence "
        f"(no quotes, no markdown):"
    )

    try:
        raw  = _call_groq(
            prompt,
            max_tokens     = 80,
            temperature    = 0.9,
            operation_name = (
                f"Custom Hook ({lang.upper()})"
            ),
        )
        hook = _clean_keyword_response(raw)
        if hook and 3 <= len(hook.split()) <= 15:
            log.info("  ✅ Custom hook: '%s'", hook)
            return hook
        log.warning(
            "  ⚠️  Hook too short/long — using title"
        )
        return safe_title

    except Exception as e:
        log.warning(
            "  ⚠️  Custom hook failed: %s", e
        )
        return safe_title


# ═══════════════════════════════════════════════════
# 🎯 MASTER ENRICHMENT HELPERS
# ═══════════════════════════════════════════════════

def _build_attractive_title(title: str) -> dict:
    return {
        "title":       title,
        "emoji_left":  DEFAULT_EMOJI_LEFT,
        "emoji_right": DEFAULT_EMOJI_RIGHT,
    }


def _apply_suggested_tags(
    tagged:    list[dict],
    suggested: list[str],
) -> None:
    for i, sent in enumerate(tagged):
        if i >= len(suggested):
            break
        sent["final_tag"]     = suggested[i]
        sent["tag_source"]    = "ai_suggested"
        sent["text_with_tag"] = (
            f"[{suggested[i]}] {sent['text']}"
        )


def _handle_tag_suggestions(
    tagged:       Optional[list[dict]],
    analysis:     dict,
    lang:         str,
    content_mode: str = "short",
) -> None:
    if not tagged:
        return

    tags_needed = [
        s for s in tagged
        if s.get("final_tag") is None
    ]
    if not tags_needed:
        return

    try:
        suggested = suggest_tags_for_sentences(
            tags_needed, analysis, lang, content_mode
        )
        _apply_suggested_tags(tags_needed, suggested)
        log.info(
            "  ✅ Tags applied: %d/%d",
            len(suggested), len(tags_needed),
        )
    except Exception as e:
        log.warning(
            "  ⚠️  Tag suggestions failed: %s "
            "— using DEFAULT_TAG",
            e,
        )
        for sent in tags_needed:
            sent["final_tag"]     = DEFAULT_TAG
            sent["tag_source"]    = "fallback"
            sent["text_with_tag"] = (
                f"[{DEFAULT_TAG}] {sent['text']}"
            )


def _prepare_keywords_input(
    tagged:  Optional[list[dict]],
    content: str,
) -> tuple[list[str], list[str]]:
    if tagged:
        sentences = [s["text"] for s in tagged]
        tags      = [
            s.get("final_tag", "information")
            for s in tagged
        ]
    else:
        sentences = [content[:200]]
        tags      = ["information"]
    return sentences, tags


# ═══════════════════════════════════════════════════
# 🎯 MASTER ENRICHMENT
# ═══════════════════════════════════════════════════

def enrich_record(
    record:       dict,
    lang:         str                  = "ar",
    tagged:       Optional[list[dict]] = None,
    verbose:      bool                 = True,
    content_mode: str                  = "short",
) -> dict:
    """
    الدالة الرئيسية للإثراء بالـ AI — v7.6

    ✅ الإصلاحات (v7.5):
      - _fix_multiline_strings()
      - _fix_trailing_comma_in_string()
      - _fix_unclosed_quotes()
      - _apply_all_fixes() — دالة موحّدة
      - _parse_json_response() — 5 محاولات موحّدة
      - generate_hashtags() — temperature=0.5
      - generate_captions() — ONE line rule

    ✅ الإصلاحات الجديدة (v7.6):
      - موديلات reasoning (qwen) تاخذ توكنز أكبر تلقائياً
      - اكتشاف القطع أثناء التفكير وتبديل الموديل فوراً
      - MODELS_PRIORITY معاد ترتيبها (non-reasoning أولاً)
      - Pattern Interrupts / Engagement Questions / Hashtags /
        Captions كلها صارت "graceful" — تفشل بهدوء مع fallback
        بدل ما توقف الفيديو كامل
    """
    _validate_lang(lang)
    _validate_mode(content_mode)

    title   = record.get("title", "")
    content = record.get("content", "").strip()

    if not title:
        raise AIEnrichmentError(
            "Cannot enrich: title is empty"
        )
    if not content:
        raise AIEnrichmentError(
            "Cannot enrich: content is empty"
        )

    lang_name = LANG_NAMES.get(lang, "Arabic")
    is_long   = content_mode == "long"

    if verbose:
        log.info(
            "\n  🧠 AI Enrichment for: '%s' (%s)",
            _safe_title(title, TITLE_DISPLAY_CHARS),
            lang_name,
        )
        log.info("  %s", "─" * 50)
        log.info(
            "  📌 Title: %s %s %s",
            DEFAULT_EMOJI_LEFT,
            _safe_title(title, TITLE_SHORT_CHARS),
            DEFAULT_EMOJI_RIGHT,
        )
        log.info(
            "  📐 Mode: %s", content_mode.upper()
        )

    # 1. Content Analysis
    analysis = analyze_content(title, content, lang)
    time.sleep(INTER_OPERATION_SLEEP)

    # 2. Tag Suggestions
    _handle_tag_suggestions(
        tagged, analysis, lang, content_mode
    )
    time.sleep(INTER_OPERATION_SLEEP)

    # 3. Power Words
    power_words = generate_power_words(
        content, analysis, lang
    )
    time.sleep(INTER_OPERATION_SLEEP)

    # 4. Visual Keywords
    sentences, tags = _prepare_keywords_input(
        tagged, content
    )
    visual_keywords = generate_visual_keywords(
        sentences    = sentences,
        title        = title,
        context      = analysis,
        tags         = tags,
        content_mode = content_mode,
    )
    time.sleep(INTER_OPERATION_SLEEP)

    # 5. Pattern Interrupts
    interrupts = generate_pattern_interrupts(
        title, content, analysis, lang,
    )
    time.sleep(INTER_OPERATION_SLEEP)

    # 6. Engagement Questions
    questions = generate_engagement_questions(
        title, content, analysis, lang,
    )
    time.sleep(INTER_OPERATION_SLEEP)

    # 7. Hashtags
    hashtags = generate_hashtags(
        title, content, analysis, lang,
    )
    time.sleep(INTER_OPERATION_SLEEP)

    # 8. Captions
    captions = generate_captions(
        title, content, analysis, hashtags, lang,
    )
    time.sleep(INTER_OPERATION_SLEEP)

    # 9. Street Description
    street_description = generate_street_description(
        title, content, analysis, lang,
    )
    time.sleep(INTER_OPERATION_SLEEP)

    # 10. Accent Colors
    accent_colors = suggest_accent_colors(analysis)

    # 11 & 12: Hooks — Short فقط
    if not is_long:
        time.sleep(INTER_OPERATION_SLEEP)
        hook_keyword = generate_hook_keyword(
            title, content, analysis,
        )
        time.sleep(INTER_OPERATION_SLEEP)
        custom_hook = generate_custom_hook(
            title, content, analysis, lang,
        )
    else:
        hook_keyword = (
            sentences[0][:60]
            if sentences
            else HOOK_FALLBACK_KEYWORD
        )
        custom_hook = ""

    attractive_title = _build_attractive_title(title)

    if verbose:
        total_ops = 12
        ops_done  = 10 if is_long else 12
        log.info("  %s", "─" * 50)
        log.info(
            "  ✅ AI enrichment complete "
            "(%d/%d operations)%s",
            ops_done, total_ops,
            " [long mode: hooks skipped]"
            if is_long else "",
        )
        log.info(
            "  🪝 Hook: '%s'", custom_hook
        )
        log.info(
            "  📌 Final: %s %s %s",
            attractive_title['emoji_left'],
            _safe_title(
                attractive_title['title'],
                TITLE_DISPLAY_CHARS,
            ),
            attractive_title['emoji_right'],
        )
        log.info(
            "  📝 Street Description: %d chars",
            len(street_description),
        )

    return {
        "analysis":             analysis,
        "power_words":          power_words,
        "visual_keywords":      visual_keywords,
        "pattern_interrupts":   interrupts,
        "engagement_questions": questions,
        "hashtags":             hashtags,
        "captions":             captions,
        "street_description":   street_description,
        "accent_colors":        accent_colors,
        "hook_keyword":         hook_keyword,
        "custom_hook":          custom_hook,
        "attractive_title":     attractive_title,
        "tagged":               tagged,
        "lang":                 lang,
        "content_mode":         content_mode,
    }

"""
🎙️ Text-to-Speech via Google Gemini TTS v2.0 — Street Style Edition

Features:
  ✅ Per-language voices (different voice for each lang)
  ✅ Saudi Arabic street style (Charon)
  ✅ Parisian French street style (Puck)
  ✅ American Gen Z street style (Fenrir)
  ✅ Authentic dialect prompting
  ✅ Tag-aware voice instructions (22 tags)
  ✅ Multi-key rotation (up to 50 keys)
  ✅ Supports both naming conventions:
       - GEMINI_API_KEY1   (no underscore)
       - GEMINI_API_KEY_1  (with underscore)
  ✅ Auto-retry on rate limits
  ✅ Truncation detection (per-language WPM)
  ✅ Thread-safe
  ✅ Merges ALL audio chunks (no truncated audio!)
  ✅ Compatible with google-genai 0.x and 1.x
  ✅ Reliable MIME → extension mapping
"""

from __future__ import annotations

import logging
import os
import struct
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Optional

from google import genai
from google.genai import types

# DEFAULT_TAG with fallback
try:
    from tags_parser import DEFAULT_TAG
except ImportError:
    DEFAULT_TAG = "information"

log = logging.getLogger(__name__)

# ═════════════════════════════════════════════════════════════════════════════
# CONSTANTS
# ═════════════════════════════════════════════════════════════════════════════

# Model
TTS_MODEL = "gemini-2.5-flash-preview-tts"

# Validation
MIN_DURATION_S = 2.0
MAX_KEYS_SCAN  = 50

# Timeouts
FFPROBE_TIMEOUT = 15

# Words per minute (per language — for truncation detection)
WPM_BY_LANG: dict[str, int] = {
    "ar": 140,   # Arabic: slower
    "fr": 180,   # French: medium
    "en": 160,   # English: standard
}

# Safe audio extensions
SAFE_AUDIO_EXTENSIONS: set[str] = {
    ".wav", ".mp3", ".ogg", ".aac", ".m4a", ".flac",
}

# Reliable MIME → extension mapping (no mimetypes.guess_extension)
_MIME_TO_EXT: dict[str, str] = {
    "audio/wav":  ".wav",
    "audio/wave": ".wav",
    "audio/l16":  ".wav",
    "audio/l24":  ".wav",
    "audio/mp3":  ".mp3",
    "audio/mpeg": ".mp3",
    "audio/ogg":  ".ogg",
    "audio/aac":  ".aac",
    "audio/flac": ".flac",
}

# Rate limit indicators
RATE_LIMIT_KEYWORDS = (
    "429", "resource_exhausted", "quota",
    "rate limit", "ratequota",
)


# ═════════════════════════════════════════════════════════════════════════════
# VOICES — Different voice per language
# ═════════════════════════════════════════════════════════════════════════════

VOICES: dict[str, str] = {
    "algenib":  "Algenib",
    "charon":   "Charon",    # ← قوي ذكوري (Saudi)
    "puck":     "Puck",      # ← شبابي حيوي (French)
    "fenrir":   "Fenrir",    # ← حاد قوي (American)
    "orus":     "Orus",
    "aoede":    "Aoede",
    "zephyr":   "Zephyr",
    "kore":     "Kore",
    "apophis":  "Apophis",
    "achird":   "Achird",
}


# ═════════════════════════════════════════════════════════════════════════════
# VOICE CONFIGURATIONS PER LANGUAGE — STREET STYLE
# ═════════════════════════════════════════════════════════════════════════════

VOICE_CONFIGS: dict[str, dict] = {

    # ═══════════════════════════════════════════════════════════════
    # 🇸🇦 ARABIC — Saudi Street Style (Khaliji)
    # ═══════════════════════════════════════════════════════════════
    "ar": {
        "voice_key":  "charon",
        "voice_name": "Charon",
        "director_note": (
            "# Audio Profile\n"
            "Young Saudi Arabian street narrator. "
            "Confident male voice in his late 20s.\n\n"

            "# Director's Note\n"
            "Style: Saudi Khaliji street dialect — "
            "casual, intense, direct, raw.\n"
            "Pace: Moderate-fast conversational pace "
            "with dramatic pauses for impact.\n"
            "Accent: AUTHENTIC Saudi Arabian "
            "(Najdi/Hijazi blend), street vernacular. "
            "NOT Egyptian, NOT Levantine, NOT Fusha.\n"
            "Tone: Confident street wisdom — like a "
            "trusted big brother giving real advice.\n\n"

            "## Scene\n"
            "A young Saudi influencer recording a viral "
            "video on his phone in a Riyadh cafe. "
            "Speaking directly to his audience as if "
            "talking to his close friends.\n\n"

            "## Pronunciation Rules\n"
            "- Pronounce ق as 'g' (Saudi style): "
            "قال = 'gaal', قلب = 'galb'\n"
            "- Use Khaliji intonation patterns\n"
            "- Drop case endings (no حركات الإعراب)\n"
            "- Natural elision: كيف الحال = 'keef-haalak'\n"
            "- Emphasize emotional words\n\n"

            "## Sample Context\n"
            "Motivational/psychology short-form video for "
            "Saudi/Khaliji youth. The narrator uses Saudi "
            "street expressions naturally: والله، يبيلك، "
            "خوش، زين، شدة، ولا يهمك، هالشي، عادي، تراني، "
            "صدق، يا حبيب القلب، تكفى، طيب، شف، اسمع، "
            "والنبي.\n\n"

            "## Critical Rules\n"
            "1. Speak EXACTLY as written - no formal "
            "corrections\n"
            "2. NEVER use Fusha (formal Arabic)\n"
            "3. NEVER use Egyptian or Levantine accent\n"
            "4. Use Saudi 'g' for ق consistently\n"
            "5. Sound like a real Saudi, not a "
            "TV presenter\n"
            "6. Emotional weight on dramatic words"
        ),
    },

    # ═══════════════════════════════════════════════════════════════
    # 🇫🇷 FRENCH — Parisian Street Slang
    # ═══════════════════════════════════════════════════════════════
    "fr": {
        "voice_key":  "puck",
        "voice_name": "Puck",
        "director_note": (
            "# Audio Profile\n"
            "Young Parisian street narrator. "
            "Confident male voice in his mid-20s "
            "with urban attitude.\n\n"

            "# Director's Note\n"
            "Style: French street slang (argot urbain) — "
            "raw, direct, modern, banlieue energy.\n"
            "Pace: Rapid fire delivery but perfectly "
            "articulated, with sharp emphatic pauses.\n"
            "Accent: AUTHENTIC modern Parisian street "
            "French (banlieue/cité influenced). "
            "NOT formal Parisian, NOT Marseille, "
            "NOT Quebec.\n"
            "Tone: Cool street confidence — like a "
            "young Parisian dropping hard truths to "
            "his frérots.\n\n"

            "## Scene\n"
            "A young French content creator filming in "
            "Paris. Speaking directly to camera with "
            "raw street confidence, the kind of guy "
            "who talks straight without sugar-coating.\n\n"

            "## Pronunciation Rules\n"
            "- Drop final consonants when natural: "
            "'pas' → 'pas' (silent s)\n"
            "- Use street contractions: 'tu es' → "
            "'t'es', 'je ne sais pas' → 'j'sais pas'\n"
            "- Modern Parisian liaison patterns\n"
            "- Emphasize verlan and slang words\n"
            "- Natural urban rhythm\n\n"

            "## Sample Context\n"
            "Viral French short-form video for young "
            "French audience. The narrator naturally "
            "uses Parisian street expressions: "
            "wesh, c'est ouf, trop stylé, carrément, "
            "tranquille, grave, c'est chaud, t'as vu, "
            "frérot, en mode, du coup, genre, vraiment, "
            "ça pue, mortel, ouf, chelou.\n\n"

            "## Critical Rules\n"
            "1. Speak EXACTLY as written - no formal "
            "corrections\n"
            "2. NEVER use formal French (français "
            "soutenu)\n"
            "3. NEVER use Quebec accent\n"
            "4. Use modern Parisian street patterns\n"
            "5. Sound like a real young Parisian, "
            "not a news anchor\n"
            "6. Drop syllables naturally as French "
            "youth do"
        ),
    },

    # ═══════════════════════════════════════════════════════════════
    # 🇺🇸 ENGLISH — American Urban Street Style (Gen Z)
    # ═══════════════════════════════════════════════════════════════
    "en": {
        "voice_key":  "fenrir",
        "voice_name": "Fenrir",
        "director_note": (
            "# Audio Profile\n"
            "Young American urban narrator. "
            "Confident male voice in his mid-20s with "
            "modern street energy.\n\n"

            "# Director's Note\n"
            "Style: Modern American street slang — "
            "Gen Z urban vernacular, authentic, raw.\n"
            "Pace: Conversational rhythm with sharp "
            "emphasis on key words, natural street "
            "cadence.\n"
            "Accent: AUTHENTIC modern American urban "
            "(NYC/LA Gen Z blend). NOT Southern, "
            "NOT British, NOT corporate American.\n"
            "Tone: Real street wisdom — like talking "
            "to your day-one homie about real life.\n\n"

            "## Scene\n"
            "A young American content creator filming "
            "a viral TikTok/Reel. Speaking directly to "
            "camera with authentic Gen Z street energy, "
            "dropping hard truths.\n\n"

            "## Pronunciation Rules\n"
            "- Use modern Gen Z intonation patterns\n"
            "- Natural contractions: 'gonna', 'wanna', "
            "'tryna', 'finna'\n"
            "- Drop unnecessary syllables: 'probably' "
            "→ 'prob'ly'\n"
            "- Urban rhythm with hip-hop influence\n"
            "- Sharp emphasis on slang and emotional "
            "words\n\n"

            "## Sample Context\n"
            "Motivational/psychology short-form video "
            "for American Gen Z. The narrator uses "
            "modern street expressions: "
            "no cap, fr fr, lowkey, bussin, it's "
            "giving, slay, periodt, facts, bet, "
            "that's wild, deadass, finna, hits "
            "different, on god, bro, real talk, "
            "straight up, no lie.\n\n"

            "## Critical Rules\n"
            "1. Speak EXACTLY as written - no formal "
            "corrections\n"
            "2. NEVER use formal English (no 'shall', "
            "'thus', 'whilst')\n"
            "3. NEVER use Southern drawl\n"
            "4. NEVER use British pronunciation\n"
            "5. Use modern urban Gen Z patterns\n"
            "6. Sound like a real young American on "
            "TikTok, not a news anchor"
        ),
    },
}


# ═════════════════════════════════════════════════════════════════════════════
# TAG VOICE INSTRUCTIONS (22 tags)
# ═════════════════════════════════════════════════════════════════════════════

TAG_VOICE_INSTRUCTIONS: dict[str, str] = {
    "intrigue": (
        "🔮 MYSTERIOUS & INTRIGUING\n"
        "- Speak slowly, like sharing a forbidden secret\n"
        "- Lower your volume slightly, almost whispering\n"
        "- Add tiny pauses before important words\n"
        "- Build curiosity with vocal tension\n"
        "- End sentences with subtle rising tone"
    ),
    "desire": (
        "💛 WARM & DESIRABLE\n"
        "- Speak with genuine warmth and passion\n"
        "- Soft, inviting tone, savoring each word\n"
        "- Make the listener WANT what you're describing"
    ),
    "information": (
        "📘 CLEAR & EDUCATIONAL\n"
        "- Crystal clarity, natural conversational pace\n"
        "- Slight emphasis on key terms\n"
        "- Like explaining to a curious friend"
    ),
    "inspiration": (
        "⚡ UPLIFTING & INSPIRING\n"
        "- Elevated energy, build momentum\n"
        "- Slightly faster, full of conviction\n"
        "- End with powerful emphasis"
    ),
    "confident": (
        "💪 BOLD & ASSERTIVE\n"
        "- Absolute certainty, strong grounded voice\n"
        "- No hesitation, each word lands with weight\n"
        "- Make declarations, not suggestions"
    ),
    "shock": (
        "💥 INTENSE & SHOCKING\n"
        "- Sudden sharp delivery\n"
        "- Higher pitch on key words\n"
        "- Brief pause AFTER the shocking word"
    ),
    "wisdom": (
        "🧠 DEEP & REFLECTIVE\n"
        "- Slowly and deliberately\n"
        "- Lower contemplative tone\n"
        "- Each word carries deep meaning"
    ),
    "urgency": (
        "🚨 URGENT & CRITICAL\n"
        "- Faster pace, slightly higher pitch\n"
        "- Sound like there's NO TIME to waste\n"
        "- End with imperative force"
    ),
    "calm": (
        "🌊 PEACEFUL & SOOTHING\n"
        "- Softly and gently, slow relaxed pace\n"
        "- Lower volume, reassuring and warm"
    ),
    "emotional": (
        "💔 TENDER & TOUCHING\n"
        "- Genuine emotion, slightly slower\n"
        "- Vulnerable, authentic delivery\n"
        "- Make the listener FEEL what you feel"
    ),
    "hook": (
        "🪝 ATTENTION GRABBER\n"
        "- Maximum energy from the first word\n"
        "- Shocking, abrupt, demanding attention\n"
        "- Make it impossible to scroll away"
    ),
    "direct": (
        "🎯 DIRECT & RAW\n"
        "- No fluff, straight to the point\n"
        "- Like grabbing someone by the shoulders\n"
        "- Crystal clear, no room for doubt"
    ),
    "cta": (
        "📣 CALL TO ACTION\n"
        "- Energetic and motivating\n"
        "- Urgent invitation to act NOW\n"
        "- End with confident momentum"
    ),
    "pause": (
        "⏸️ DRAMATIC PAUSE\n"
        "- Very slow, almost silent\n"
        "- Let the previous moment breathe\n"
        "- Build anticipation"
    ),
    "whisper": (
        "🤫 SECRETIVE WHISPER\n"
        "- Very quiet, close-to-mic\n"
        "- Like sharing a dangerous secret\n"
        "- Force the listener to lean in"
    ),
    "curiosity": (
        "🔍 PROVOCATIVE QUESTION\n"
        "- Tone that demands engagement\n"
        "- Slight upward inflection\n"
        "- Make them WANT the answer"
    ),
    "storytelling": (
        "📖 NARRATIVE FLOW\n"
        "- Engaging, like reading bedtime story\n"
        "- Natural rhythm, immersive\n"
        "- Bring characters to life"
    ),
    "dramatic": (
        "🎭 THEATRICAL INTENSITY\n"
        "- Maximum emotional weight\n"
        "- Cinematic delivery\n"
        "- Every word HITS"
    ),
    "revelation": (
        "💡 MOMENT OF TRUTH\n"
        "- Build-up then sudden reveal\n"
        "- Like dropping a bombshell\n"
        "- Slight pause before the reveal"
    ),
    "tension": (
        "⚡ BUILDING TENSION\n"
        "- Gradual escalation\n"
        "- Sharper consonants\n"
        "- Tension rising in voice"
    ),
    "climax": (
        "🔥 PEAK INTENSITY\n"
        "- MAXIMUM impact moment\n"
        "- Everything builds to this\n"
        "- Loud, sharp, undeniable"
    ),
    "powerful": (
        "💪 RAW POWER\n"
        "- Grounded, weighted delivery\n"
        "- Confident command of voice\n"
        "- Every syllable is concrete"
    ),
}

DEFAULT_VOICE_INSTRUCTION = (
    "🎙️ NATURAL & ENGAGING\n"
    "- Clear natural narration, confident but warm"
)


# ═════════════════════════════════════════════════════════════════════════════
# API KEY ROTATION (Thread-safe)
# ═════════════════════════════════════════════════════════════════════════════

_key_lock     = threading.Lock()
_key_index:   int                       = 0
_API_KEYS:    list[str]                 = []
_keys_loaded: bool                      = False
_clients:     dict[str, "genai.Client"] = {}


def _load_keys() -> list[str]:
    """Load all Gemini keys (supports multiple naming)."""
    keys: list[str] = []
    seen: set[str]  = set()

    # Main key
    main_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if main_key and main_key not in seen:
        keys.append(main_key)
        seen.add(main_key)

    # Numbered keys (both formats)
    for i in range(1, MAX_KEYS_SCAN + 1):
        # Without underscore: GEMINI_API_KEY1
        k1 = os.environ.get(f"GEMINI_API_KEY{i}", "").strip()
        if k1 and k1 not in seen:
            keys.append(k1)
            seen.add(k1)

        # With underscore: GEMINI_API_KEY_1
        k2 = os.environ.get(f"GEMINI_API_KEY_{i}", "").strip()
        if k2 and k2 not in seen:
            keys.append(k2)
            seen.add(k2)

    return keys


def _ensure_keys_loaded() -> None:
    """Thread-safe key loading with double-check."""
    global _API_KEYS, _keys_loaded

    if _keys_loaded:
        return

    with _key_lock:
        if _keys_loaded:
            return
        _API_KEYS    = _load_keys()
        _keys_loaded = True
        if _API_KEYS:
            log.info(
                "  🔑 Loaded %d Gemini API keys",
                len(_API_KEYS)
            )
        else:
            log.warning("  ⚠️  No Gemini API keys found")


def _get_client(key: str) -> "genai.Client":
    """Thread-safe client caching."""
    with _key_lock:
        if key not in _clients:
            _clients[key] = genai.Client(api_key=key)
        return _clients[key]


def _get_current_key() -> str:
    """Get current key."""
    _ensure_keys_loaded()
    with _key_lock:
        if not _API_KEYS:
            return ""
        return _API_KEYS[_key_index % len(_API_KEYS)]


def _rotate_key() -> None:
    """Rotate to next key (thread-safe)."""
    global _key_index
    with _key_lock:
        n = len(_API_KEYS)
        if n <= 1:
            if n == 1:
                log.warning(
                    "  ⚠️  No additional Gemini keys to rotate"
                )
            return
        _key_index = (_key_index + 1) % n
        new_idx    = _key_index
        total      = n
    log.info(
        "  🔄 Gemini key rotated → #%d/%d",
        new_idx + 1, total
    )


def _is_rate_limit(e: Exception) -> bool:
    """Check if error is rate limit."""
    msg = str(e).lower()
    return any(kw in msg for kw in RATE_LIMIT_KEYWORDS)


# ═════════════════════════════════════════════════════════════════════════════
# PROMPT BUILDER
# ═════════════════════════════════════════════════════════════════════════════

def _get_lang_note(lang: str) -> str:
    """Language-specific reading instruction."""
    notes = {
        "ar": (
            "Text is in ARABIC (Saudi street dialect). "
            "Read with AUTHENTIC Saudi Arabian pronunciation. "
            "Use 'g' for ق. Drop case endings. Sound natural."
        ),
        "fr": (
            "Text is in FRENCH (Parisian street slang). "
            "Read with AUTHENTIC modern Parisian street accent. "
            "Use street contractions. Sound urban and real."
        ),
        "en": (
            "Text is in ENGLISH (American Gen Z urban). "
            "Read with AUTHENTIC modern American urban accent. "
            "Use natural contractions. Sound like Gen Z TikTok."
        ),
    }
    return notes.get(lang, notes["en"])


def _build_tags_legend(tagged_sentences: list[dict]) -> str:
    """Build tags legend for used tags."""
    used_tags: set[str] = {
        s.get("final_tag", DEFAULT_TAG)
        for s in tagged_sentences
    }

    legend_parts = []
    for tag in sorted(used_tags):
        instruction = TAG_VOICE_INSTRUCTIONS.get(
            tag, DEFAULT_VOICE_INSTRUCTION
        )
        legend_parts.append(f"[{tag}]: {instruction}")

    return "\n\n".join(legend_parts)


def _build_script_text(tagged_sentences: list[dict]) -> str:
    """Build script text with tags."""
    lines = []
    for sent in tagged_sentences:
        tag  = sent.get("final_tag", DEFAULT_TAG)
        text = sent["text"]
        lines.append(f"[{tag}] {text}")

    return "\n\n".join(lines)


def _build_tagged_prompt(
    tagged_sentences: list[dict],
    lang:             str,
) -> str:
    """Build comprehensive prompt with director's note + tags."""
    if not tagged_sentences:
        raise ValueError("No tagged sentences provided")

    config        = VOICE_CONFIGS.get(lang, VOICE_CONFIGS["ar"])
    director_note = config["director_note"]
    lang_note     = _get_lang_note(lang)
    legend_text   = _build_tags_legend(tagged_sentences)
    script_text   = _build_script_text(tagged_sentences)
    n             = len(tagged_sentences)

    return f"""Read the following transcript with AUTHENTIC street accent and emotion.

{director_note}

# Language Instructions
{lang_note}

# Tag Instructions
Each sentence has a [tag] that tells you HOW to speak it.
DO NOT speak the tag — read ONLY the text after it.
CHANGE your voice style for each different tag.

{legend_text}

# Pacing Guide ({n} sentences)
- Sentence 1: MAXIMUM energy (hook)
- Middle sentences: Build intensity, vary the rhythm
- Last sentence: Deliver with complete conviction

# Transcript
{script_text}

# CRITICAL RULES
1. Read EVERY word from start to finish
2. Never trail off or stop early
3. Different tags = DIFFERENT voice styles
4. Make transitions feel natural and emotional
5. Use AUTHENTIC street accent as specified in director note
6. NEVER switch to formal/literary pronunciation
7. Sound like a REAL person, not an AI"""


# ═════════════════════════════════════════════════════════════════════════════
# AUDIO DURATION
# ═════════════════════════════════════════════════════════════════════════════

def _get_duration(path: str) -> float:
    """
    Get audio file duration in seconds.
    
    Returns:
        Duration (positive float) or 0.0 on error
        -1.0 if ffprobe is not found
    """
    try:
        r = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                path,
            ],
            capture_output = True,
            text           = True,
            timeout        = FFPROBE_TIMEOUT,
        )

        result = r.stdout.strip()
        try:
            return float(result) if result else 0.0
        except ValueError:
            return 0.0

    except FileNotFoundError:
        log.error("  ❌ ffprobe not found — install FFmpeg")
        return -1.0
    except subprocess.TimeoutExpired:
        log.warning("  ⚠️  ffprobe timeout")
        return 0.0
    except Exception:
        return 0.0


# ═════════════════════════════════════════════════════════════════════════════
# WAV HELPERS
# ═════════════════════════════════════════════════════════════════════════════

def _parse_mime(mime: str) -> dict:
    """Parse audio MIME type for parameters."""
    bps      = 16
    rate     = 24000
    channels = 1

    for part in mime.split(";"):
        p = part.strip()

        if p.lower().startswith("rate="):
            try:
                rate = int(p.split("=", 1)[1])
            except (ValueError, IndexError):
                pass
        elif p.lower().startswith("channels="):
            try:
                channels = int(p.split("=", 1)[1])
            except (ValueError, IndexError):
                pass
        elif p.startswith("audio/L"):
            try:
                bps = int(p.split("L", 1)[1])
            except (ValueError, IndexError):
                pass

    return {
        "bits_per_sample": bps,
        "rate":            rate,
        "channels":        channels,
    }


def _to_wav(audio_data: bytes, mime_type: str) -> bytes:
    """Convert raw PCM audio to WAV format."""
    params      = _parse_mime(mime_type)
    bps         = params["bits_per_sample"]
    rate        = params["rate"]
    channels    = params["channels"]
    data_size   = len(audio_data)
    block_align = channels * (bps // 8)
    byte_rate   = rate * block_align

    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF", 36 + data_size, b"WAVE",
        b"fmt ", 16, 1, channels,
        rate, byte_rate, block_align, bps,
        b"data", data_size,
    )

    return header + audio_data


# ═════════════════════════════════════════════════════════════════════════════
# AUDIO GENERATION
# ═════════════════════════════════════════════════════════════════════════════

def _build_tts_config(
    voice_name: str,
) -> "types.GenerateContentConfig":
    """Build Gemini TTS configuration."""
    return types.GenerateContentConfig(
        temperature         = 1.0,
        response_modalities = ["audio"],
        speech_config       = types.SpeechConfig(
            voice_config = types.VoiceConfig(
                prebuilt_voice_config = types.PrebuiltVoiceConfig(
                    voice_name = voice_name,
                )
            )
        ),
    )


def _extract_parts_from_chunk(chunk) -> list:
    """Extract parts from chunk (both SDK versions)."""
    # Pattern 1: Direct parts (older SDK)
    if hasattr(chunk, "parts") and chunk.parts:
        return list(chunk.parts)

    # Pattern 2: Via candidates (newer SDK 1.0.0+)
    if hasattr(chunk, "candidates") and chunk.candidates:
        try:
            candidates = chunk.candidates
            if len(candidates) > 0:
                candidate = candidates[0]
                if hasattr(candidate, "content") and candidate.content:
                    content = candidate.content
                    if hasattr(content, "parts") and content.parts:
                        return list(content.parts)
        except (IndexError, AttributeError):
            pass

    return []


def _extract_audio_from_part(part) -> Optional[tuple[bytes, str]]:
    """Extract audio data from a single part."""
    try:
        if not hasattr(part, "inline_data"):
            return None

        inline_data = part.inline_data
        if inline_data is None:
            return None

        if not hasattr(inline_data, "data") or not inline_data.data:
            return None

        data = inline_data.data
        mime = getattr(inline_data, "mime_type", None) or "audio/wav"

        return (data, mime)

    except (AttributeError, TypeError):
        return None


def _generate_audio_chunks(
    client: "genai.Client",
    prompt: str,
    config: "types.GenerateContentConfig",
) -> list[tuple[bytes, str]]:
    """Generate audio from Gemini."""
    contents = [
        types.Content(
            role  = "user",
            parts = [types.Part.from_text(text=prompt)],
        )
    ]

    audio_chunks: list[tuple[bytes, str]] = []

    for chunk in client.models.generate_content_stream(
        model    = TTS_MODEL,
        contents = contents,
        config   = config,
    ):
        parts = _extract_parts_from_chunk(chunk)
        if not parts:
            continue

        for part in parts:
            audio = _extract_audio_from_part(part)
            if audio:
                audio_chunks.append(audio)

    return audio_chunks


def _save_audio(
    data:        bytes,
    mime:        str,
    output_path: str,
) -> Path:
    """
    Save audio to file with correct extension.
    
    Uses reliable MIME → extension mapping (no mimetypes.guess_extension).
    """
    mime_clean = mime.split(";")[0].strip().lower()
    ext        = _MIME_TO_EXT.get(mime_clean)

    # Raw PCM needs WAV header
    needs_wav_wrap = (
        not ext or
        mime_clean.startswith("audio/l")
    )

    if needs_wav_wrap:
        ext  = ".wav"
        data = _to_wav(data, mime)

    base      = Path(output_path).with_suffix("")
    file_path = Path(f"{base}_0{ext}")
    file_path.write_bytes(data)

    return file_path


def _is_truncated(
    duration:    float,
    total_words: int,
    lang:        str = "ar",
) -> bool:
    """
    Detect if audio is truncated based on language WPM.
    
    Returns True if duration is less than 50% of expected.
    """
    if total_words <= 20:
        return False

    wpm          = WPM_BY_LANG.get(lang, 160)
    min_expected = (total_words / wpm) * 60
    return duration < min_expected * 0.5


def _retry_wait_time(attempt: int) -> float:
    """Calculate wait time before next attempt."""
    return min(2 ** attempt, 30)


# ═════════════════════════════════════════════════════════════════════════════
# SYNTHESIZE SPEECH (MAIN FUNCTION)
# ═════════════════════════════════════════════════════════════════════════════

def synthesize_speech(
    tagged_sentences: list[dict],
    output_path:      str = "output",
    voice_key:        str = "",
    lang:             str = "ar",
    retries:          int = 3,
) -> Path:
    """
    Convert tagged sentences to speech via Gemini TTS.
    
    ✅ Merges ALL audio chunks (no truncated audio!)
    ✅ Per-language voice selection
    ✅ Authentic street accent prompting
    ✅ Language-aware truncation detection
    
    Args:
        tagged_sentences: list of dicts with "text" and "final_tag"
        output_path:      base path (without extension)
        voice_key:        unused (auto-selected from VOICE_CONFIGS)
        lang:             ar | fr | en
        retries:          base retry count (may be increased with more keys)
    
    Returns:
        Path to saved audio file
    
    Raises:
        ValueError:    if no tagged sentences
        RuntimeError:  if TTS fails after all attempts
    """
    if not tagged_sentences:
        raise ValueError("No tagged sentences to synthesize")

    _ensure_keys_loaded()

    # Get voice config for language (per-language)
    config     = VOICE_CONFIGS.get(lang, VOICE_CONFIGS["ar"])
    voice_name = config["voice_name"]
    voice_key  = config["voice_key"]

    # Statistics
    total_words = sum(
        len(s.get("text", "").split())
        for s in tagged_sentences
    )
    unique_tags = {
        s.get("final_tag", DEFAULT_TAG)
        for s in tagged_sentences
    }

    # Build prompt
    prompt = _build_tagged_prompt(tagged_sentences, lang)

    # Calculate max attempts
    max_attempts = max(
        1,
        max(retries, len(_API_KEYS) * 2)
        if _API_KEYS
        else retries
    )

    # Log configuration
    log.info("\n  🎙️  TTS Configuration:")
    log.info("     Voice    : %s (%s)", voice_name, voice_key)
    log.info("     Lang     : %s", lang.upper())
    log.info("     Style    : Street/Casual (authentic)")
    log.info("     Words    : %d", total_words)
    log.info(
        "     Tags     : %s",
        ", ".join(sorted(unique_tags))
    )
    log.info("     Sentences: %d", len(tagged_sentences))
    log.info("     Keys     : %d available", len(_API_KEYS))
    log.info("     Max tries: %d", max_attempts)

    # Build TTS config
    tts_config = _build_tts_config(voice_name)

    # Generation attempts
    for attempt in range(max_attempts):
        cur_idx = _key_index

        log.info(
            "\n  🎙️  TTS attempt [%d/%d] | key #%d/%d",
            attempt + 1, max_attempts,
            cur_idx + 1, len(_API_KEYS) or 1,
        )

        try:
            # Get client
            key = _get_current_key()
            if not key:
                raise RuntimeError("No Gemini key available")

            client       = _get_client(key)
            audio_chunks = _generate_audio_chunks(
                client, prompt, tts_config
            )

            if not audio_chunks:
                raise RuntimeError("No audio data returned")

            # ✅ Merge ALL audio chunks
            log.info(
                "  📦 Merging %d audio chunks...",
                len(audio_chunks)
            )

            primary_mime = audio_chunks[0][1]
            all_data     = b"".join(d for d, _ in audio_chunks)

            log.info(
                "  📊 Total audio data: %d bytes",
                len(all_data)
            )

            # Save merged audio
            saved    = _save_audio(all_data, primary_mime, output_path)
            duration = _get_duration(str(saved))

            log.info(
                "  ✅ Audio saved: %s (%.1fs)",
                saved.name, duration
            )

            # Validate duration
            if duration == -1.0:
                # ffprobe not found → accept without validation
                log.warning(
                    "  ⚠️  Cannot validate (ffprobe missing) — accepting"
                )
                return saved

            if duration < MIN_DURATION_S:
                log.warning(
                    "  ⚠️  Too short (%.1fs) — retrying", duration
                )
                saved.unlink(missing_ok=True)
                _rotate_key()
                time.sleep(1)
                continue

            # Truncation check (language-aware)
            if _is_truncated(duration, total_words, lang):
                log.warning(
                    "  ⚠️  Likely truncated (lang=%s) — retrying",
                    lang
                )
                if attempt < max_attempts - 1:
                    saved.unlink(missing_ok=True)
                    _rotate_key()
                    time.sleep(1)
                    continue
                # On last attempt, accept what we have

            # Success!
            return saved

        except Exception as e:
            if _is_rate_limit(e):
                log.warning(
                    "  🛑 Rate limit on key #%d",
                    cur_idx + 1
                )
                _rotate_key()
                time.sleep(2)
            else:
                err_type = type(e).__name__
                log.warning(
                    "  ⚠️  TTS error [%s]: %s",
                    err_type, str(e)[:120]
                )
                _rotate_key()

                if attempt < max_attempts - 1:
                    wait = _retry_wait_time(attempt)
                    time.sleep(wait)

    raise RuntimeError(
        f"TTS failed after {max_attempts} attempts"
    )

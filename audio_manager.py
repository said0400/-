"""
🎚️ Professional Background Music & SFX Mixing System v2.0

Features:
  ✅ Auto EQ per language (AR/FR/EN)
  ✅ Auto ducking on voice (smart volume points)
  ✅ Professional compressor
  ✅ Smart SFX (keyword-based)
  ✅ Hook SFX (opening attention grabber)
  ✅ Big Transitions SFX (between sections)
  ✅ Small Transitions SFX (between sentences)
  ✅ Particles SFX (magical moments)
  ✅ TV Static SFX (modern effect with seed)
  ✅ Music Ducking on transitions
  ✅ Tag-aware SFX selection (22 tags)
  ✅ Section detection (Hook/Content/CTA)
  ✅ Audio limiter (prevents clipping)
  ✅ Smart ducking deduplication
  ✅ MAX_DUCKING_POINTS limit
  ✅ Cross-platform file cleanup
  ✅ FALLBACK_DURATION = 300s (Long support)
  ✅ Generic SFX track builder
"""

from __future__ import annotations

import logging
import os
import random
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Union

from sync import get_audio_duration

log = logging.getLogger(__name__)

# ═════════════════════════════════════════════════════════════════════════════
# 📁 PATHS & DIRECTORIES
# ═════════════════════════════════════════════════════════════════════════════

BASE_DIR  = Path(__file__).parent.resolve()
MUSIC_DIR = BASE_DIR / "assets" / "music"
SFX_DIR   = BASE_DIR / "sfx"

# Music pools
MUSIC_POOLS: dict[str, Path] = {
    "motivation": MUSIC_DIR / "motivation",
    "cinematic":  MUSIC_DIR / "cinematic",
}

# Old SFX pools
SFX_POOLS: dict[str, Path] = {
    "swoosh": SFX_DIR / "swoosh",
    "whoosh": SFX_DIR / "whoosh",
}

# Special SFX dirs
SMART_SFX_DIR       = SFX_DIR / "smart"
TRANSITION_SFX_DIR  = SFX_DIR / "transitions"
OPENING_SFX_DIR     = SFX_DIR / "opening"
BIG_TRANS_SFX_DIR   = SFX_DIR / "big_transitions"
SMALL_TRANS_SFX_DIR = SFX_DIR / "small_transitions"
PARTICLES_SFX_DIR   = SFX_DIR / "particles"
TV_STATIC_SFX_DIR   = SFX_DIR / "tv_static"


# ═════════════════════════════════════════════════════════════════════════════
# 🎚️ AUDIO CONSTANTS
# ═════════════════════════════════════════════════════════════════════════════

# File extensions
AUDIO_EXTENSIONS = ("*.wav", "*.mp3", "*.WAV", "*.MP3")

# Timeouts
FFMPEG_TIMEOUT = 300  # 5 minutes

# Default volumes
DEFAULT_MUSIC_VOLUME    = 0.12
DEFAULT_SFX_VOLUME      = 0.35
DEFAULT_SMART_SFX_VOL   = 0.40
DEFAULT_OPENING_VOL     = 0.65
DEFAULT_BIG_TRANS_VOL   = 0.70
DEFAULT_SMALL_TRANS_VOL = 0.30
DEFAULT_PARTICLE_VOL    = 0.25
DEFAULT_TV_STATIC_VOL   = 0.40

# Music ducking
MUSIC_DUCK_BIG_TRANS = 0.05
MUSIC_DUCK_VOICE     = 0.06

# Fade settings
DEFAULT_FADE_IN    = 1.0
DEFAULT_FADE_OUT   = 2.0
DEFAULT_DUCK_FADE  = 0.3
FALLBACK_DURATION  = 300.0  # 5 minutes (for Long videos)

# Limits
MAX_DUCKING_POINTS = 200


# ═════════════════════════════════════════════════════════════════════════════
# 🌍 LANGUAGE EQ FILTERS
# ═════════════════════════════════════════════════════════════════════════════

LANG_EQ: dict[str, str] = {
    "ar": (
        "equalizer=f=80:width_type=o:width=2:g=3,"
        "equalizer=f=200:width_type=o:width=2:g=2,"
        "equalizer=f=3000:width_type=o:width=2:g=-1,"
        "equalizer=f=8000:width_type=o:width=2:g=-2"
    ),
    "fr": (
        "equalizer=f=80:width_type=o:width=2:g=1,"
        "equalizer=f=1000:width_type=o:width=2:g=2,"
        "equalizer=f=2500:width_type=o:width=2:g=3,"
        "equalizer=f=8000:width_type=o:width=2:g=1"
    ),
    "en": (
        "equalizer=f=80:width_type=o:width=2:g=1,"
        "equalizer=f=500:width_type=o:width=2:g=-1,"
        "equalizer=f=4000:width_type=o:width=2:g=3,"
        "equalizer=f=10000:width_type=o:width=2:g=2"
    ),
}

# Compressor settings
COMPRESSOR_FILTER = (
    "acompressor="
    "threshold=-18dB:"
    "ratio=4:1:"
    "attack=5:"
    "release=60:"
    "makeup=3dB:"
    "knee=2dB"
)


# ═════════════════════════════════════════════════════════════════════════════
# 🎯 SECTION DETECTION (Hook / Content / CTA)
# ═════════════════════════════════════════════════════════════════════════════

CTA_TAGS  = {"confident", "inspiration", "powerful", "cta"}
HOOK_TAGS = {"intrigue", "shock", "urgency", "curiosity", "hook"}


@dataclass
class VideoSection:
    """قسم من الفيديو (Hook/Content/CTA)."""
    section_type: str
    start_time:   float
    end_time:     float
    sentence_idx: int
    tag:          str


def detect_video_sections(
    aligned: list[dict],
    tagged:  Optional[list[dict]] = None,
) -> list[VideoSection]:
    """كشف الفقرات الرئيسية في الفيديو."""
    if not aligned:
        return []

    sections: list[VideoSection] = []
    total = len(aligned)

    for i, seg in enumerate(aligned):
        start = float(seg.get("start", 0))
        end   = float(seg.get("end", start + 1))

        # استخراج tag
        tag = "information"
        if tagged and i < len(tagged):
            tag = tagged[i].get("final_tag") or "information"
        elif "tag" in seg:
            tag = seg.get("tag", "information")

        # تحديد نوع الفقرة
        if i == 0:
            section_type = "hook"
        elif i == total - 1:
            section_type = "cta"
        elif i == total - 2 and tag in CTA_TAGS and total > 4:
            section_type = "cta"
        else:
            section_type = "content"

        sections.append(VideoSection(
            section_type = section_type,
            start_time   = start,
            end_time     = end,
            sentence_idx = i,
            tag          = tag,
        ))

    return sections


def get_section_transitions(
    sections: list[VideoSection],
) -> list[dict]:
    """جلب نقاط الانتقال بين الفقرات."""
    if len(sections) < 2:
        return []

    transitions = []

    for i in range(len(sections) - 1):
        current  = sections[i]
        next_sec = sections[i + 1]

        transitions.append({
            "time":         current.end_time,
            "from_section": current.section_type,
            "to_section":   next_sec.section_type,
            "tag":          next_sec.tag,
            "is_big":       (
                current.section_type
                != next_sec.section_type
            ),
        })

    return transitions


# ═════════════════════════════════════════════════════════════════════════════
# 🎼 TAG-BASED SFX VOLUME (per tag — 22 tags)
# ═════════════════════════════════════════════════════════════════════════════

TAG_SFX_VOLUME: dict[str, float] = {
    "shock":        0.85,
    "climax":       0.85,
    "revelation":   0.75,
    "urgency":      0.70,
    "tension":      0.70,
    "dramatic":     0.70,
    "intrigue":     0.65,
    "powerful":     0.65,
    "confident":    0.60,
    "inspiration":  0.65,
    "information":  0.45,
    "emotional":    0.55,
    "desire":       0.55,
    "wisdom":       0.40,
    "calm":         0.35,
    "whisper":      0.40,
    "pause":        0.30,
    "curiosity":    0.55,
    "storytelling": 0.50,
    # New tags
    "hook":         0.85,
    "direct":       0.60,
    "cta":          0.65,
}

DEFAULT_TAG_SFX_VOLUME = 0.50


# ═════════════════════════════════════════════════════════════════════════════
# 🔍 SMART SFX KEYWORDS
# ═════════════════════════════════════════════════════════════════════════════

SFX_KEYWORDS: dict[str, list[str]] = {
    "impact_heavy": [
        "صدمة", "مفاجأة", "انفجر", "ضرب", "سقط", "انهار",
        "shock", "explode", "crash", "hit", "boom", "collapse",
        "choc", "explosion", "frappé",
    ],
    "impact_soft": [
        "خفيف", "لمس", "هدوء",
        "soft", "gentle", "touch",
        "léger", "doux",
    ],
    "heartbeat": [
        "قلب", "حب", "عشق", "خفقان", "عاطفة",
        "heart", "love", "emotion", "feel",
        "cœur", "amour", "émotion",
    ],
    "heartbeat_fast": [
        "توتر", "قلق", "خوف", "رعب", "هلع",
        "tension", "anxiety", "fear", "panic",
        "stress", "anxiété", "peur",
    ],
    "emotional_sting": [
        "حزن", "ألم", "دموع", "فقد",
        "sad", "pain", "tears", "loss",
        "triste", "douleur", "larmes",
    ],
    "coins": [
        "مال", "فلوس", "ثروة", "ربح", "غنى",
        "دولار", "ذهب",
        "money", "cash", "wealth", "profit",
        "rich", "gold",
        "argent", "richesse", "or",
    ],
    "success_bell": [
        "نجاح", "فوز", "إنجاز", "حقق", "تفوق",
        "success", "win", "achieve", "accomplish",
        "succès", "victoire", "réussite",
    ],
    "celebration": [
        "احتفال", "فرح", "بهجة", "مبروك",
        "celebrate", "joy", "happy",
        "congratulations",
        "célébration", "joie", "bonheur",
    ],
    "warning_beep": [
        "انتبه", "تحذير", "خطر", "احذر",
        "warning", "danger", "alert", "beware",
        "attention", "danger", "alerte",
    ],
    "tick_tock": [
        "وقت", "ساعة", "عاجل", "الآن", "موعد",
        "time", "clock", "urgent", "now", "deadline",
        "temps", "horloge", "urgent",
    ],
    "suspense_sting": [
        "سر", "غامض", "خفي", "مجهول", "لغز",
        "secret", "mystery", "hidden", "unknown",
        "secret", "mystère", "caché",
    ],
    "revelation": [
        "كشف", "اعترف", "الحقيقة", "اكتشاف",
        "reveal", "confess", "truth", "discovery",
        "révéler", "vérité", "découverte",
    ],
    "thunder": [
        "رعد", "عاصفة", "قوة", "هائل",
        "thunder", "storm", "powerful", "massive",
        "tonnerre", "tempête", "puissant",
    ],
    "applause": [
        "رائع", "ممتاز", "عظيم", "أحسنت",
        "amazing", "excellent", "great", "bravo",
        "incroyable", "excellent", "bravo",
    ],
    "crowd_gasp": [
        "لا يصدق", "مستحيل", "غير معقول",
        "unbelievable", "impossible", "incredible",
        "incroyable", "impossible",
    ],
}


# ═════════════════════════════════════════════════════════════════════════════
# 📦 DATA CLASSES
# ═════════════════════════════════════════════════════════════════════════════

@dataclass
class SmartSFXDetection:
    """نتيجة كشف Smart SFX لجملة."""
    sfx_path: Path
    sfx_name: str
    keyword:  str
    time:     float


# ═════════════════════════════════════════════════════════════════════════════
# 🛠️ HELPER FUNCTIONS
# ═════════════════════════════════════════════════════════════════════════════

def _make_temp_path(
    prefix: str,
    suffix: str = ".wav",
) -> str:
    """إنشاء مسار مؤقت آمن."""
    fd, path = tempfile.mkstemp(
        prefix=prefix, suffix=suffix
    )
    os.close(fd)
    return path


def _safe_unlink(
    path: Optional[Union[str, Path]],
) -> None:
    """حذف ملف بأمان (cross-platform)."""
    if path is None:
        return
    try:
        Path(path).unlink(missing_ok=True)
    except Exception:
        pass


def _get_audio_files(directory: Path) -> list[Path]:
    """جلب جميع ملفات الصوت (WAV + MP3)."""
    if not directory.exists():
        return []

    files = []
    for ext in AUDIO_EXTENSIONS:
        files.extend(directory.glob(ext))

    return files


def _normalize_text(text: str) -> str:
    """تطبيع النص للبحث."""
    return re.sub(r"[^\w\s]", " ", text.lower()).strip()


def _run_ffmpeg(
    args:    list[str],
    timeout: int = FFMPEG_TIMEOUT,
) -> tuple[bool, str]:
    """تشغيل ffmpeg بأمان."""
    try:
        r = subprocess.run(
            args,
            capture_output = True,
            text           = True,
            timeout        = timeout,
        )
        return r.returncode == 0, r.stderr
    except subprocess.TimeoutExpired:
        return False, "ffmpeg timeout"
    except Exception as e:
        return False, str(e)


def _safe_duration(
    path:     str,
    fallback: float = FALLBACK_DURATION,
) -> float:
    """جلب مدة آمنة مع fallback."""
    dur = get_audio_duration(path)
    return dur if dur > 0 else fallback


# ═════════════════════════════════════════════════════════════════════════════
# 🎵 MUSIC & SFX SELECTION
# ═════════════════════════════════════════════════════════════════════════════

def _collect_all_music() -> list[Path]:
    """جمع كل الموسيقى من الـ pools."""
    all_files = []

    for pool_dir in MUSIC_POOLS.values():
        if pool_dir.exists():
            all_files.extend(
                _get_audio_files(pool_dir)
            )

    if not all_files and MUSIC_DIR.exists():
        for ext in AUDIO_EXTENSIONS:
            all_files.extend(MUSIC_DIR.rglob(ext))

    return all_files


def get_music_file(
    content_type: str           = "motivational",
    seed:         Optional[int] = None,
) -> Optional[Path]:
    """اختيار ملف موسيقى عشوائي."""
    all_files = _collect_all_music()

    if not all_files:
        log.warning(
            "  ⚠️  No music files found in %s",
            MUSIC_DIR
        )
        return None

    rng  = random.Random(seed)
    pick = rng.choice(all_files)

    log.info("  🎵 Music: %s", pick.name)
    return pick


def get_sfx_file(
    sfx_type: str           = "swoosh",
    seed:     Optional[int] = None,
) -> Optional[Path]:
    """اختيار ملف SFX عشوائي من pool معين."""
    pool_dir = SFX_POOLS.get(sfx_type)
    if not pool_dir or not pool_dir.exists():
        return None

    files = _get_audio_files(pool_dir)
    if not files:
        return None

    return random.Random(seed).choice(files)


def _find_smart_sfx_file(
    sfx_name: str,
) -> Optional[Path]:
    """البحث عن ملف Smart SFX بالاسم."""
    for ext in (".wav", ".mp3", ".WAV", ".MP3"):
        candidate = SMART_SFX_DIR / f"{sfx_name}{ext}"
        if candidate.exists():
            return candidate

    for f in _get_audio_files(SMART_SFX_DIR):
        if sfx_name in f.stem.lower():
            return f

    return None


# ═════════════════════════════════════════════════════════════════════════════
# 🪝 OPENING SFX (Hook Sound)
# ═════════════════════════════════════════════════════════════════════════════

def get_opening_sfx(
    seed: Optional[int] = None,
) -> Optional[Path]:
    """🪝 جلب صوت بداية الفيديو."""
    if not OPENING_SFX_DIR.exists():
        log.warning(
            "  ⚠️  Opening SFX dir not found"
        )
        return None

    files = _get_audio_files(OPENING_SFX_DIR)
    if not files:
        log.warning(
            "  ⚠️  No opening SFX files found"
        )
        return None

    rng  = random.Random(seed)
    pick = rng.choice(files)

    log.info("  🪝 Opening SFX: %s", pick.name)
    return pick


# ═════════════════════════════════════════════════════════════════════════════
# 💥 BIG TRANSITIONS SFX
# ═════════════════════════════════════════════════════════════════════════════

def get_big_transition_sfx(
    transition_idx: int = 0,
) -> Optional[Path]:
    """💥 جلب صوت انتقال كبير."""
    if not BIG_TRANS_SFX_DIR.exists():
        return None

    files = _get_audio_files(BIG_TRANS_SFX_DIR)
    if not files:
        return None

    return files[transition_idx % len(files)]


# ═════════════════════════════════════════════════════════════════════════════
# 👆 SMALL TRANSITIONS SFX
# ═════════════════════════════════════════════════════════════════════════════

def get_small_transition_sfx(
    transition_idx: int = 0,
) -> Optional[Path]:
    """👆 جلب صوت انتقال صغير."""
    if not SMALL_TRANS_SFX_DIR.exists():
        return None

    files = _get_audio_files(SMALL_TRANS_SFX_DIR)
    if not files:
        return None

    return files[transition_idx % len(files)]


# ═════════════════════════════════════════════════════════════════════════════
# ✨ PARTICLES SFX
# ═════════════════════════════════════════════════════════════════════════════

def get_particle_sfx(
    idx: int = 0,
) -> Optional[Path]:
    """✨ جلب صوت جزيئة."""
    if not PARTICLES_SFX_DIR.exists():
        return None

    files = _get_audio_files(PARTICLES_SFX_DIR)
    if not files:
        return None

    return files[idx % len(files)]


# ═════════════════════════════════════════════════════════════════════════════
# 📺 TV STATIC SFX
# ═════════════════════════════════════════════════════════════════════════════

def get_tv_static_sfx(
    idx: int = 0,
) -> Optional[Path]:
    """📺 جلب صوت تشويش تلفاز."""
    if not TV_STATIC_SFX_DIR.exists():
        return None

    files = _get_audio_files(TV_STATIC_SFX_DIR)
    if not files:
        return None

    return files[idx % len(files)]


# ═════════════════════════════════════════════════════════════════════════════
# 🔍 SMART SFX DETECTION
# ═════════════════════════════════════════════════════════════════════════════

def _detect_sfx_for_sentence(
    sentence: str,
) -> Optional[dict]:
    """كشف SFX مناسب لجملة واحدة."""
    normalized = _normalize_text(sentence)

    for sfx_name, keywords in SFX_KEYWORDS.items():
        for kw in keywords:
            if _normalize_text(kw) not in normalized:
                continue

            sfx_path = _find_smart_sfx_file(sfx_name)
            if sfx_path:
                return {
                    "sfx_path": sfx_path,
                    "sfx_name": sfx_name,
                    "keyword":  kw,
                }

    return None


def detect_smart_sfx_for_sentences(
    sentences: list[str],
) -> list[Optional[dict]]:
    """كشف SFX لكل جملة."""
    return [
        _detect_sfx_for_sentence(s)
        for s in sentences
    ]


# ═════════════════════════════════════════════════════════════════════════════
# 🎚️ COMPRESSOR + EQ
# ═════════════════════════════════════════════════════════════════════════════

def _apply_filter(
    audio_path:  str,
    output_path: str,
    filter_str:  str,
    label:       str,
) -> str:
    """تطبيق ffmpeg filter."""
    if not Path(audio_path).exists():
        return audio_path

    log.info("  🎛️  Applying %s...", label)

    success, _ = _run_ffmpeg([
        "ffmpeg", "-y",
        "-i", audio_path,
        "-af", filter_str,
        "-c:a", "pcm_s16le",
        output_path,
    ])

    if not success:
        log.warning(
            "  ⚠️  %s failed — using original",
            label
        )
        return audio_path

    log.info("  ✅ %s applied", label)
    return output_path


def apply_compressor(
    audio_path:  str,
    output_path: str,
) -> str:
    """تطبيق compressor على الصوت."""
    return _apply_filter(
        audio_path, output_path,
        COMPRESSOR_FILTER, "compressor",
    )


def apply_eq(
    audio_path:  str,
    output_path: str,
    lang:        str = "ar",
) -> str:
    """تطبيق EQ حسب اللغة."""
    eq_filter = LANG_EQ.get(lang, LANG_EQ["ar"])
    return _apply_filter(
        audio_path, output_path,
        eq_filter, f"{lang.upper()} EQ",
    )


# ═════════════════════════════════════════════════════════════════════════════
# 🦆 SMART MUSIC DUCKING (with deduplication + limit)
# ═════════════════════════════════════════════════════════════════════════════

def _build_ducking_points(
    aligned:           list[dict],
    voice_dur:         float,
    music_volume:      float,
    duck_volume:       float,
    fade_time:         float,
    big_transitions:   Optional[list[dict]] = None,
    big_duck_volume:   float                = MUSIC_DUCK_BIG_TRANS,
) -> list[str]:
    """🦆 بناء نقاط حجم الموسيقى."""
    points: list[str] = [f"0/{music_volume}"]

    # Ducking على الصوت (عادي)
    for seg in aligned:
        start = float(seg.get("start", 0))
        end   = float(seg.get("end", start + 1))

        duck_start = max(0.0, start - fade_time)
        duck_end   = min(voice_dur, end + fade_time)

        points.append(
            f"{duck_start:.3f}/{music_volume}"
        )
        points.append(
            f"{start:.3f}/{duck_volume}"
        )
        points.append(
            f"{end:.3f}/{duck_volume}"
        )
        points.append(
            f"{duck_end:.3f}/{music_volume}"
        )

    # Ducking قوي عند الانتقالات الكبيرة
    if big_transitions:
        BIG_TRANS_DUCK_DURATION = 0.6

        for trans in big_transitions:
            t_time     = trans["time"]
            duck_start = max(0.0, t_time - 0.1)
            duck_end   = min(
                voice_dur,
                t_time + BIG_TRANS_DUCK_DURATION,
            )

            points.append(
                f"{duck_start:.3f}/{music_volume}"
            )
            points.append(
                f"{t_time:.3f}/{big_duck_volume}"
            )
            points.append(
                f"{duck_end:.3f}/{big_duck_volume}"
            )
            recovery_end = min(
                voice_dur, duck_end + 0.3
            )
            points.append(
                f"{recovery_end:.3f}/{music_volume}"
            )

    points.append(
        f"{voice_dur:.3f}/{music_volume}"
    )

    # Smart deduplication: keep lowest volume per timestamp
    time_to_vol: dict[str, float] = {}
    for point in points:
        try:
            t, v = point.split("/")
            t_key   = f"{float(t):.3f}"
            v_float = float(v)
            if t_key not in time_to_vol:
                time_to_vol[t_key] = v_float
            else:
                # Keep strongest ducking (lowest volume)
                time_to_vol[t_key] = min(
                    time_to_vol[t_key], v_float
                )
        except (ValueError, IndexError):
            continue

    clean = [
        f"{t}/{v}"
        for t, v in sorted(
            time_to_vol.items(),
            key=lambda x: float(x[0])
        )
    ]

    # Simplify if too many points
    if len(clean) > MAX_DUCKING_POINTS:
        log.warning(
            "  ⚠️  Ducking points too many (%d) "
            "— simplifying",
            len(clean)
        )
        step  = max(1, len(clean) // MAX_DUCKING_POINTS)
        clean = clean[::step]

    return clean


def _build_ducking_filter(
    aligned:         list[dict],
    voice_dur:       float,
    music_volume:    float                = DEFAULT_MUSIC_VOLUME,
    duck_volume:     float                = MUSIC_DUCK_VOICE,
    fade_time:       float                = DEFAULT_DUCK_FADE,
    big_transitions: Optional[list[dict]] = None,
) -> str:
    """بناء ducking filter للموسيقى."""
    if not aligned:
        return f"volume={music_volume}"

    try:
        points = _build_ducking_points(
            aligned, voice_dur,
            music_volume, duck_volume, fade_time,
            big_transitions=big_transitions,
        )
        points_str = "|".join(points)
        return (
            f"volume='{points_str}':eval=frame"
        )

    except Exception as e:
        log.warning(
            "  ⚠️  Ducking filter error: %s", e
        )
        return f"volume={music_volume}"


# ═════════════════════════════════════════════════════════════════════════════
# 🎚️ AUDIO MIXING
# ═════════════════════════════════════════════════════════════════════════════

def _build_music_filter(
    duck_filter: str,
    fade_in:     float,
    fade_out_st: float,
    fade_out:    float,
    voice_dur:   float,
) -> str:
    """بناء filter للموسيقى."""
    return (
        f"{duck_filter},"
        f"afade=t=in:st=0:d={fade_in:.3f},"
        f"afade=t=out:st={fade_out_st:.3f}"
        f":d={fade_out:.3f},"
        f"atrim=0:{voice_dur:.3f}"
    )


def _mix_with_filter(
    voice_path:   str,
    music_path:   str,
    output_path:  str,
    music_filter: str,
    voice_dur:    float,
) -> bool:
    """تنفيذ mix فعلي."""
    filter_complex = (
        f"[1:a]{music_filter}[music];"
        f"[0:a][music]amix=inputs=2:"
        f"duration=first:normalize=0[out]"
    )

    success, _ = _run_ffmpeg([
        "ffmpeg", "-y",
        "-i", voice_path,
        "-stream_loop", "-1",
        "-i", music_path,
        "-filter_complex", filter_complex,
        "-map", "[out]",
        "-c:a", "aac", "-b:a", "192k",
        "-t", f"{voice_dur:.3f}",
        output_path,
    ])

    return success


def _prerender_looped_track(
    audio_path:  str,
    target_dur:  float,
    output_path: str,
) -> Optional[Path]:
    """
    ✅ FIX (السبب الجذري لقِصَر الصوت الممزوج):

    نُجهّز (نُجسّد) مسار الموسيقى مُكرَّراً بمدة دقيقة = target_dur
    في تمريرة ffmpeg منفصلة ومستقلة، قبل أن يدخل filter_complex
    الذي يحتوي amix. هذا يتفادى الخلل المعروف في ffmpeg حين يُجمع
    -stream_loop مع amix/filter_complex، حيث تُسبّب نقاط اللف
    قفزات PTS قد تُنهي الترميز قبل الوصول لـ -t الكاملة.

    Returns:
        Path للملف الجاهز، أو None عند الفشل الكامل.
    """
    src_dur    = get_audio_duration(audio_path)
    needs_loop = src_dur <= 0 or src_dur < target_dur

    args: list[str] = ["ffmpeg", "-y"]
    if needs_loop:
        args += ["-stream_loop", "-1"]
    args += [
        "-i", audio_path,
        "-t", f"{target_dur:.3f}",
        "-c:a", "pcm_s16le",
        "-ar", "44100",
        output_path,
    ]

    success, err = _run_ffmpeg(args, timeout=FFMPEG_TIMEOUT)

    if not success or not Path(output_path).exists():
        log.warning(
            "  ⚠️  Pre-render music loop failed: %s",
            err[:200] if err else "unknown",
        )
        return None

    out_dur = get_audio_duration(output_path)

    if out_dur < target_dur - 0.3:
        log.warning(
            "  ⚠️  Pre-rendered music short: %.2fs vs "
            "target %.2fs — padding with apad",
            out_dur, target_dur,
        )
        padded = output_path + "_pad.wav"
        ok, _ = _run_ffmpeg([
            "ffmpeg", "-y", "-i", output_path,
            "-af", f"apad=whole_dur={target_dur:.3f}",
            "-t", f"{target_dur:.3f}",
            "-c:a", "pcm_s16le",
            padded,
        ])
        if ok and Path(padded).exists():
            _safe_unlink(output_path)
            Path(padded).rename(output_path)
        else:
            _safe_unlink(padded)

    final_dur = get_audio_duration(output_path)
    if final_dur < target_dur - 0.5:
        log.warning(
            "  ⚠️  Pre-render still short after pad: "
            "%.2fs vs %.2fs",
            final_dur, target_dur,
        )
        return None

    return Path(output_path)

def mix_audio(
    voice_path:      str,
    music_path:      str,
    output_path:     str,
    music_volume:    float                = DEFAULT_MUSIC_VOLUME,
    fade_in:         float                = DEFAULT_FADE_IN,
    fade_out:        float                = DEFAULT_FADE_OUT,
    lang:            str                  = "ar",
    aligned:         Optional[list[dict]] = None,
    big_transitions: Optional[list[dict]] = None,
) -> Path:
    """Mix صوت مع موسيقى مع ducking ذكي — ✅ FIX جذري لمشكلة القِصَر."""
    voice_dur   = _safe_duration(voice_path)
    fade_out_st = max(0.0, voice_dur - fade_out)

    log.info(
        "  🎚️  Mixing: voice=%.2fs "
        "music_vol=%.0f%% lang=%s",
        voice_dur,
        music_volume * 100,
        lang.upper()
    )

    # ── ✅ FIX: تجهيز الموسيقى بمدة دقيقة قبل amix ──────────
    prerender_target = voice_dur + fade_out + 0.5
    prerender_path   = (
        f"{output_path}_music_prerender.wav"
    )
    music_fixed = _prerender_looped_track(
        music_path, prerender_target, prerender_path
    )
    used_prerender = music_fixed is not None
    music_input    = (
        str(music_fixed) if used_prerender else music_path
    )

    # 🦆 Smart Ducking
    duck_filter = _build_ducking_filter(
        aligned          = aligned or [],
        voice_dur        = voice_dur,
        music_volume     = music_volume,
        duck_volume      = MUSIC_DUCK_VOICE,
        big_transitions  = big_transitions,
    )

    music_filter = _build_music_filter(
        duck_filter, fade_in, fade_out_st,
        fade_out, voice_dur,
    )

    # ✅ لا نستخدم -stream_loop هنا إذا نجح التجهيز المسبق —
    # هذا هو الإصلاح الجذري (تفادي تعارض stream_loop + amix)
    loop_args = [] if used_prerender else ["-stream_loop", "-1"]

    filter_complex = (
        f"[1:a]{music_filter}[music];"
        f"[0:a][music]amix=inputs=2:"
        f"duration=longest:normalize=0[out]"
    )

    success, _ = _run_ffmpeg([
        "ffmpeg", "-y",
        "-i", voice_path,
        *loop_args,
        "-i", music_input,
        "-filter_complex", filter_complex,
        "-map", "[out]",
        "-c:a", "aac", "-b:a", "192k",
        "-t", f"{voice_dur:.3f}",
        output_path,
    ])

    if used_prerender:
        _safe_unlink(prerender_path)

    if success and Path(output_path).exists():
        out_dur = get_audio_duration(output_path)

        if out_dur < voice_dur - 0.3:
            log.warning(
                "  ⚠️  Mix STILL short after root-cause fix: "
                "%.2fs vs %.2fs — check music file / disk space",
                out_dur, voice_dur,
            )
        else:
            log.info(
                "  ✅ Mix duration OK: %.2fs (target %.2fs)",
                out_dur, voice_dur,
            )

        if aligned:
            log.info(
                "  🦆 Ducking: %d sentences",
                len(aligned)
            )
        if big_transitions:
            log.info(
                "  💥 Big trans ducking: %d points",
                len(big_transitions)
            )
        log.info(
            "  ✅ Mixed → %s",
            Path(output_path).name
        )
        return Path(output_path)

    # ── Attempt 2: simple mix (بدون ducking) ────────────────
    log.warning(
        "  ⚠️  Audio mix failed — trying simple mix..."
    )

    simple_filter = (
        f"volume={music_volume},"
        f"afade=t=in:st=0:d={fade_in:.3f},"
        f"afade=t=out:st={fade_out_st:.3f}"
        f":d={fade_out:.3f},"
        f"atrim=0:{voice_dur:.3f}"
    )

    fallback_music = music_input if used_prerender else music_path
    fallback_loop  = [] if used_prerender else ["-stream_loop", "-1"]

    filter_complex_simple = (
        f"[1:a]{simple_filter}[music];"
        f"[0:a][music]amix=inputs=2:"
        f"duration=longest:normalize=0[out]"
    )

    success2, _ = _run_ffmpeg([
        "ffmpeg", "-y",
        "-i", voice_path,
        *fallback_loop,
        "-i", fallback_music,
        "-filter_complex", filter_complex_simple,
        "-map", "[out]",
        "-c:a", "aac", "-b:a", "192k",
        "-t", f"{voice_dur:.3f}",
        output_path,
    ])

    if success2 and Path(output_path).exists():
        log.info(
            "  ✅ Mixed (simple) → %s",
            Path(output_path).name
        )
        return Path(output_path)

    log.warning(
        "  ⚠️  Simple mix also failed — voice only"
    )
    return Path(voice_path)


# ═════════════════════════════════════════════════════════════════════════════
# 🎬 GENERIC SFX TRACK BUILDER
# ═════════════════════════════════════════════════════════════════════════════

def _build_sfx_track_generic(
    sfx_items:  list[dict],
    total_dur:  float,
    output_path: str,
    label:      str,
) -> Optional[Path]:
    """
    Generic builder for any SFX track.
    
    Each item in sfx_items:
        {"sfx_file": Path, "time": float, "volume": float}
    """
    if not sfx_items:
        return None

    inputs:  list[str] = []
    delays:  list[str] = []
    idx = 0

    for item in sfx_items:
        sfx_file = item.get("sfx_file")
        if not sfx_file:
            continue

        inputs += ["-i", str(sfx_file)]
        delay_ms = int(item["time"] * 1000)
        vol      = item.get("volume", 0.5)

        delays.append(
            f"[{idx}:a]volume={vol:.3f},"
            f"adelay={delay_ms}|{delay_ms}"
            f"[s{idx}]"
        )
        idx += 1

    if not delays:
        return None

    mix_inputs = "".join(
        f"[s{i}]" for i in range(len(delays))
    )
    filter_str = (
        ";".join(delays) +
        f";{mix_inputs}amix="
        f"inputs={len(delays)}:"
        f"normalize=0[out]"
    )

    success, _ = _run_ffmpeg([
        "ffmpeg", "-y",
        *inputs,
        "-filter_complex", filter_str,
        "-map", "[out]",
        "-t", str(total_dur),
        "-c:a", "pcm_s16le",
        output_path,
    ])

    if not success:
        log.warning("  ⚠️  %s track failed", label)
        return None

    log.info("  ✅ %s built", label)
    return Path(output_path)


# ═════════════════════════════════════════════════════════════════════════════
# 🪝 HOOK SFX BUILDER (Opening)
# ═════════════════════════════════════════════════════════════════════════════

def build_hook_sfx_track(
    total_duration: float,
    output_path:    str,
    seed:           Optional[int] = None,
) -> Optional[Path]:
    """🪝 بناء مسار Hook SFX."""
    hook_sfx = get_opening_sfx(seed=seed)
    if not hook_sfx:
        return None

    log.info("  🪝 Building Hook SFX track...")

    success, _ = _run_ffmpeg([
        "ffmpeg", "-y",
        "-i", str(hook_sfx),
        "-af",
        f"volume={DEFAULT_OPENING_VOL},"
        f"apad=pad_dur={total_duration}",
        "-t", str(total_duration),
        "-c:a", "pcm_s16le",
        output_path,
    ])

    if not success:
        log.warning("  ⚠️  Hook SFX track failed")
        return None

    log.info(
        "  ✅ Hook SFX built: %s", hook_sfx.name
    )
    return Path(output_path)


# ═════════════════════════════════════════════════════════════════════════════
# 💥 BIG TRANSITIONS SFX BUILDER
# ═════════════════════════════════════════════════════════════════════════════

def build_big_transitions_sfx_track(
    transitions:    list[dict],
    total_duration: float,
    output_path:    str,
) -> Optional[Path]:
    """💥 بناء مسار Big Transitions SFX."""
    big_trans = [
        t for t in transitions if t.get("is_big")
    ]

    if not big_trans:
        return None

    if not BIG_TRANS_SFX_DIR.exists():
        log.warning(
            "  ⚠️  Big transitions dir not found"
        )
        return None

    log.info(
        "  💥 Building Big Transitions SFX: "
        "%d transitions",
        len(big_trans)
    )

    items: list[dict] = []
    for i, trans in enumerate(big_trans):
        sfx_file = get_big_transition_sfx(
            transition_idx=i
        )
        if sfx_file:
            items.append({
                "sfx_file": sfx_file,
                "time":     trans["time"],
                "volume":   DEFAULT_BIG_TRANS_VOL,
            })
            log.info(
                "     [%.2fs] %s → %s (%s)",
                trans['time'],
                trans['from_section'],
                trans['to_section'],
                sfx_file.name
            )

    return _build_sfx_track_generic(
        items, total_duration, output_path,
        "Big Transitions SFX",
    )


# ═════════════════════════════════════════════════════════════════════════════
# 👆 SMALL TRANSITIONS SFX BUILDER
# ═════════════════════════════════════════════════════════════════════════════

def build_small_transitions_sfx_track(
    transitions:    list[dict],
    total_duration: float,
    output_path:    str,
) -> Optional[Path]:
    """👆 بناء مسار Small Transitions SFX."""
    small_trans = [
        t for t in transitions
        if not t.get("is_big")
    ]

    if not small_trans:
        return None

    if not SMALL_TRANS_SFX_DIR.exists():
        log.warning(
            "  ⚠️  Small transitions dir not found"
        )
        return None

    log.info(
        "  👆 Building Small Transitions SFX: "
        "%d snaps",
        len(small_trans)
    )

    items: list[dict] = []
    for i, trans in enumerate(small_trans):
        sfx_file = get_small_transition_sfx(
            transition_idx=i
        )
        if sfx_file:
            items.append({
                "sfx_file": sfx_file,
                "time":     trans["time"],
                "volume":   DEFAULT_SMALL_TRANS_VOL,
            })

    return _build_sfx_track_generic(
        items, total_duration, output_path,
        "Small Transitions SFX",
    )


# ═════════════════════════════════════════════════════════════════════════════
# ✨ PARTICLES SFX BUILDER
# ═════════════════════════════════════════════════════════════════════════════

def build_particles_sfx_track(
    aligned:        list[dict],
    tagged:         Optional[list[dict]],
    total_duration: float,
    output_path:    str,
) -> Optional[Path]:
    """✨ بناء مسار Particles SFX."""
    if not aligned or not PARTICLES_SFX_DIR.exists():
        return None

    magic_tags = {
        "shock", "revelation",
        "climax", "inspiration",
    }
    items: list[dict] = []

    for i, seg in enumerate(aligned):
        tag = "information"
        if tagged and i < len(tagged):
            tag = (
                tagged[i].get("final_tag")
                or "information"
            )
        elif "tag" in seg:
            tag = seg.get("tag", "information")

        if tag in magic_tags:
            sfx_file = get_particle_sfx(
                idx=len(items)
            )
            if sfx_file:
                items.append({
                    "sfx_file": sfx_file,
                    "time":     float(
                        seg.get("start", 0)
                    ),
                    "volume":   DEFAULT_PARTICLE_VOL,
                })

    if not items:
        return None

    log.info(
        "  ✨ Building Particles SFX: "
        "%d magic moments",
        len(items)
    )

    return _build_sfx_track_generic(
        items, total_duration, output_path,
        "Particles SFX",
    )


# ═════════════════════════════════════════════════════════════════════════════
# 📺 TV STATIC SFX BUILDER
# ═════════════════════════════════════════════════════════════════════════════

def build_tv_static_sfx_track(
    big_transitions: list[dict],
    total_duration:  float,
    output_path:     str,
    seed:            Optional[int] = None,
) -> Optional[Path]:
    """📺 بناء مسار TV Static SFX (مع seed)."""
    if not TV_STATIC_SFX_DIR.exists():
        return None

    big_trans = [
        t for t in big_transitions
        if t.get("is_big")
    ]
    if not big_trans:
        return None

    # Select 50% of big transitions (with seed)
    rng = random.Random(seed)
    selected = [
        t for t in big_trans
        if rng.random() > 0.5
    ]

    if not selected:
        return None

    log.info(
        "  📺 Building TV Static SFX: "
        "%d static effects",
        len(selected)
    )

    items: list[dict] = []
    for i, trans in enumerate(selected):
        sfx_file = get_tv_static_sfx(idx=i)
        if sfx_file:
            time_offset = max(
                0, trans["time"] - 0.1
            )
            items.append({
                "sfx_file": sfx_file,
                "time":     time_offset,
                "volume":   DEFAULT_TV_STATIC_VOL,
            })

    return _build_sfx_track_generic(
        items, total_duration, output_path,
        "TV Static SFX",
    )


# ═════════════════════════════════════════════════════════════════════════════
# 🔊 SMART SFX BUILDER (keyword-based)
# ═════════════════════════════════════════════════════════════════════════════

def build_smart_sfx_track(
    sentences:   list[str],
    aligned:     list[dict],
    output_path: str,
    sfx_volume:  float = DEFAULT_SMART_SFX_VOL,
) -> Optional[Path]:
    """بناء مسار Smart SFX من الجمل."""
    if not SMART_SFX_DIR.exists():
        return None

    if not sentences or not aligned:
        return None

    detections = detect_smart_sfx_for_sentences(
        sentences
    )
    sentence_times = [
        float(seg.get("start", 0))
        for seg in aligned[:len(sentences)]
    ]

    items: list[dict] = []
    for i, detection in enumerate(detections):
        if detection and i < len(sentence_times):
            items.append({
                "sfx_file": detection["sfx_path"],
                "time":     sentence_times[i],
                "volume":   sfx_volume,
            })

    if not items:
        return None

    log.info(
        "  🔊 Smart SFX: %d effects", len(items)
    )
    for item in items:
        log.info(
            "     [%.2fs] %s",
            item["time"],
            Path(str(item["sfx_file"])).name,
        )

    total_dur = (
        float(aligned[-1].get("end", 30))
        if aligned
        else 30.0
    )

    return _build_sfx_track_generic(
        items, total_dur, output_path,
        "Smart SFX",
    )


# ═════════════════════════════════════════════════════════════════════════════
# 🎬 CLIP TRANSITION SFX (backward compat)
# ═════════════════════════════════════════════════════════════════════════════

def _calculate_transition_times(
    clip_durations: list[float],
) -> list[float]:
    """حساب أوقات الانتقالات بين الكليبات."""
    times: list[float] = []
    cumulative = 0.0

    for dur in clip_durations[:-1]:
        cumulative += max(dur, 0.1)
        times.append(cumulative)

    return times


def build_sfx_track(
    n_clips:        int,
    clip_durations: list[float],
    sfx_type:       str           = "swoosh",
    output_path:    Optional[str] = None,
    seed:           Optional[int] = None,
) -> Optional[Path]:
    """بناء مسار SFX لانتقالات الكليبات."""
    if n_clips <= 1:
        return None

    pool_dir = SFX_POOLS.get(sfx_type)
    if not pool_dir or not pool_dir.exists():
        return None

    all_sfx = _get_audio_files(pool_dir)
    if not all_sfx:
        return None

    if output_path is None:
        output_path = _make_temp_path(
            "sfx_track_", ".wav"
        )

    transition_times = _calculate_transition_times(
        clip_durations
    )
    if not transition_times:
        return None

    total_dur = sum(clip_durations)
    rng       = random.Random(seed)

    items: list[dict] = []
    for trans_t in transition_times:
        sfx_file = rng.choice(all_sfx)
        items.append({
            "sfx_file": sfx_file,
            "time":     trans_t,
            "volume":   DEFAULT_SFX_VOLUME,
        })

    result = _build_sfx_track_generic(
        items, total_dur, output_path,
        "SFX Track",
    )

    if result:
        log.info(
            "  ✅ SFX track: %d transitions",
            len(transition_times)
        )

    return result


# ═════════════════════════════════════════════════════════════════════════════
# 🔀 MERGE TRACKS (with limiter to prevent clipping)
# ═════════════════════════════════════════════════════════════════════════════

def _merge_two_tracks(
    base_path:    str,
    overlay_path: str,
    output_path:  str,
    duration:     float,
    volume:       Optional[float] = None,
) -> bool:
    """دمج track مع آخر."""
    if volume is not None:
        filter_complex = (
            f"[1:a]volume={volume}[sfx];"
            f"[0:a][sfx]amix=inputs=2:"
            f"duration=first:normalize=0[out]"
        )
    else:
        filter_complex = (
            "[0:a][1:a]amix=inputs=2:"
            "duration=first:normalize=0[out]"
        )

    success, _ = _run_ffmpeg([
        "ffmpeg", "-y",
        "-i", base_path,
        "-i", overlay_path,
        "-filter_complex", filter_complex,
        "-map", "[out]",
        "-c:a", "aac", "-b:a", "192k",
        "-t", f"{duration:.3f}",
        output_path,
    ])

    return success


def _merge_multiple_tracks(
    base_path:     str,
    overlay_paths: list[str],
    output_path:   str,
    duration:      float,
) -> bool:
    """
    دمج عدة tracks مع base track + Limiter.
    
    alimiter prevents clipping when many SFX 
    tracks are mixed together.
    """
    if not overlay_paths:
        return False

    inputs = ["-i", base_path]
    for path in overlay_paths:
        inputs.extend(["-i", path])

    total_inputs = len(overlay_paths) + 1
    mix_inputs   = "".join(
        f"[{i}:a]" for i in range(total_inputs)
    )

    # amix + alimiter (prevents clipping)
    filter_complex = (
        f"{mix_inputs}amix="
        f"inputs={total_inputs}:"
        f"duration=first:"
        f"normalize=0[mixed];"
        f"[mixed]alimiter="
        f"limit=0.9:"
        f"attack=5:"
        f"release=50[out]"
    )

    success, _ = _run_ffmpeg([
        "ffmpeg", "-y",
        *inputs,
        "-filter_complex", filter_complex,
        "-map", "[out]",
        "-c:a", "aac", "-b:a", "192k",
        "-t", f"{duration:.3f}",
        output_path,
    ])

    return success


# ═════════════════════════════════════════════════════════════════════════════
# 🎬 PIPELINE STEPS (Internal)
# ═════════════════════════════════════════════════════════════════════════════

def _step_compressor_and_eq(
    voice_path: str,
    lang:       str,
) -> tuple[str, list[str]]:
    """تطبيق Compressor + EQ."""
    temp_files = []

    # Compressor
    comp_path = _make_temp_path(
        "voice_comp_", ".wav"
    )
    temp_files.append(comp_path)
    voice_processed = apply_compressor(
        voice_path, comp_path
    )

    # EQ
    eq_path = _make_temp_path(
        "voice_eq_", ".wav"
    )
    temp_files.append(eq_path)
    voice_eq = apply_eq(
        voice_processed, eq_path, lang=lang
    )

    return voice_eq, temp_files


# ═════════════════════════════════════════════════════════════════════════════
# 🎯 MAIN PIPELINE: mix_voice_music_sfx
# ═════════════════════════════════════════════════════════════════════════════

def mix_voice_music_sfx(
    voice_path:     str,
    content_type:   str,
    output_path:    str,
    clip_durations: Optional[list[float]] = None,
    sfx_type:       str                   = "swoosh",
    music_volume:   float                 = DEFAULT_MUSIC_VOLUME,
    sfx_volume:     float                 = DEFAULT_SFX_VOLUME,
    seed:           Optional[int]         = None,
    lang:           str                   = "ar",
    aligned:        Optional[list[dict]]  = None,
    sentences:      Optional[list[str]]   = None,
    tagged:         Optional[list[dict]]  = None,
) -> Path:
    """🎬 FULL PROFESSIONAL AUDIO PIPELINE."""
    temp_files: list[str] = []
    voice_dur = _safe_duration(voice_path)
    sfx_tracks: list[str] = []

    try:
        log.info("\n  " + "═" * 60)
        log.info(
            "  🎬 PROFESSIONAL AUDIO PIPELINE STARTED"
        )
        log.info("  " + "═" * 60)

        # ─────────────────────────────────────────
        # STEP 1+2: Compressor + EQ
        # ─────────────────────────────────────────
        voice_eq, eq_temps = (
            _step_compressor_and_eq(voice_path, lang)
        )
        temp_files.extend(eq_temps)

        # ─────────────────────────────────────────
        # STEP 3: Section detection
        # ─────────────────────────────────────────
        sections        = []
        all_transitions = []
        big_transitions = []

        if aligned:
            sections = detect_video_sections(
                aligned, tagged
            )
            all_transitions = get_section_transitions(
                sections
            )
            big_transitions = [
                t for t in all_transitions
                if t.get("is_big")
            ]

            log.info("\n  📊 Video Analysis:")
            log.info(
                "     Sections: %d", len(sections)
            )
            log.info(
                "     Big transitions: %d",
                len(big_transitions)
            )
            log.info(
                "     Small transitions: %d",
                len(all_transitions)
                - len(big_transitions)
            )

            for sec in sections:
                log.info(
                    "     [%5.2fs] %-8s [%s]",
                    sec.start_time,
                    sec.section_type,
                    sec.tag
                )

        # ─────────────────────────────────────────
        # STEP 4: Music + Smart Ducking
        # ─────────────────────────────────────────
        music_file = get_music_file(
            content_type, seed=seed
        )

        if music_file is None:
            log.warning(
                "  ⚠️  No music — voice only"
            )
            return Path(voice_path)

        p          = Path(output_path)
        mixed_path = _make_temp_path(
            f"{p.stem}_vm_", ".aac"
        )
        temp_files.append(mixed_path)

        mixed = mix_audio(
            voice_path      = voice_eq,
            music_path      = str(music_file),
            output_path     = mixed_path,
            music_volume    = music_volume,
            lang            = lang,
            aligned         = aligned or [],
            big_transitions = big_transitions,
        )

        if str(mixed) in (voice_eq, voice_path):
            log.warning("  ⚠️  Music mix failed")
            return Path(voice_path)

        # ─────────────────────────────────────────
        # STEP 4.5: Validate mixed duration
        # ─────────────────────────────────────────
        current = str(mixed)
        # ✅ FIX: نستخدم voice_dur (المدة المضمونة والمقصودة) بدل
        # إعادة قياس مدة الملف الوسيط، الذي قد يكون أقصر بسبب مشاكل
        # ترميز سابقة (خصوصاً في الفيديوهات الطويلة). كل مسارات الـ SFX
        # التالية ستُبنى الآن بالمدة الصحيحة فعلياً بدل توريث الخطأ.
        target_dur = voice_dur
        total_dur  = target_dur

        mixed_actual_dur = _safe_duration(current)
        if mixed_actual_dur < target_dur - 0.3:
            log.warning(
                "  ⚠️  Mixed track (%.2fs) shorter than voice "
                "(%.2fs) even after fix — padding before SFX build",
                mixed_actual_dur, target_dur,
            )
            padded_mixed = f"{current}_padfix.aac"
            ok, _ = _run_ffmpeg([
                "ffmpeg", "-y", "-i", current,
                "-af", f"apad=whole_dur={target_dur:.3f}",
                "-t", f"{target_dur:.3f}",
                "-c:a", "aac", "-b:a", "192k",
                padded_mixed,
            ])
            if ok and Path(padded_mixed).exists():
                _safe_unlink(current)
                current = padded_mixed
                temp_files.append(current)

        # ─────────────────────────────────────────
        # STEP 5: Build SFX tracks
        # ─────────────────────────────────────────
        log.info("\n  🎯 Building SFX tracks...")

        # 🪝 Hook SFX
        hook_sfx_path = _make_temp_path(
            "hook_sfx_", ".wav"
        )
        hook_track = build_hook_sfx_track(
            total_duration = total_dur,
            output_path    = hook_sfx_path,
            seed           = seed,
        )
        if hook_track:
            sfx_tracks.append(hook_sfx_path)
            temp_files.append(hook_sfx_path)
        else:
            _safe_unlink(hook_sfx_path)

        # 💥 Big Transitions SFX
        if all_transitions:
            big_sfx_path = _make_temp_path(
                "big_sfx_", ".wav"
            )
            big_track = (
                build_big_transitions_sfx_track(
                    transitions    = all_transitions,
                    total_duration = total_dur,
                    output_path    = big_sfx_path,
                )
            )
            if big_track:
                sfx_tracks.append(big_sfx_path)
                temp_files.append(big_sfx_path)
            else:
                _safe_unlink(big_sfx_path)

        # 👆 Small Transitions SFX
        if all_transitions:
            small_sfx_path = _make_temp_path(
                "small_sfx_", ".wav"
            )
            small_track = (
                build_small_transitions_sfx_track(
                    transitions    = all_transitions,
                    total_duration = total_dur,
                    output_path    = small_sfx_path,
                )
            )
            if small_track:
                sfx_tracks.append(small_sfx_path)
                temp_files.append(small_sfx_path)
            else:
                _safe_unlink(small_sfx_path)

        # ✨ Particles SFX
        if aligned:
            particles_sfx_path = _make_temp_path(
                "particles_", ".wav"
            )
            particles_track = (
                build_particles_sfx_track(
                    aligned        = aligned,
                    tagged         = tagged,
                    total_duration = total_dur,
                    output_path    = particles_sfx_path,
                )
            )
            if particles_track:
                sfx_tracks.append(
                    particles_sfx_path
                )
                temp_files.append(
                    particles_sfx_path
                )
            else:
                _safe_unlink(particles_sfx_path)

        # 📺 TV Static SFX
        if big_transitions:
            tv_sfx_path = _make_temp_path(
                "tv_static_", ".wav"
            )
            tv_track = (
                build_tv_static_sfx_track(
                    big_transitions = all_transitions,
                    total_duration  = total_dur,
                    output_path     = tv_sfx_path,
                    seed            = seed,
                )
            )
            if tv_track:
                sfx_tracks.append(tv_sfx_path)
                temp_files.append(tv_sfx_path)
            else:
                _safe_unlink(tv_sfx_path)

        # 🔊 Smart SFX (keyword-based)
        if sentences and aligned:
            smart_sfx_path = _make_temp_path(
                "smart_sfx_", ".wav"
            )
            smart_track = build_smart_sfx_track(
                sentences   = sentences,
                aligned     = aligned,
                output_path = smart_sfx_path,
                sfx_volume  = DEFAULT_SMART_SFX_VOL,
            )
            if smart_track:
                sfx_tracks.append(smart_sfx_path)
                temp_files.append(smart_sfx_path)
            else:
                _safe_unlink(smart_sfx_path)

        # ─────────────────────────────────────────
        # STEP 6: Merge all SFX tracks
        # ─────────────────────────────────────────
        if sfx_tracks:
            log.info(
                "\n  🔀 Merging %d SFX tracks "
                "with audio...",
                len(sfx_tracks)
            )

            final_temp = _make_temp_path(
                "final_", ".aac"
            )

            success = _merge_multiple_tracks(
                base_path     = current,
                overlay_paths = sfx_tracks,
                output_path   = final_temp,
                duration      = total_dur,
            )

            if success:
                # Move to final output
                shutil.move(
                    final_temp, output_path
                )
                _safe_unlink(current)

                log.info(
                    "\n  ✅ FINAL AUDIO READY: %s",
                    Path(output_path).name
                )
                log.info(
                    "  " + "═" * 60 + "\n"
                )
                return Path(output_path)
            else:
                log.warning(
                    "  ⚠️  SFX merge failed "
                    "— using base mix"
                )
                _safe_unlink(final_temp)

        # ─────────────────────────────────────────
        # Fallback: no SFX
        # ─────────────────────────────────────────
        if current != output_path:
            if Path(current).exists():
                shutil.move(current, output_path)
            else:
                shutil.copy(
                    voice_path, output_path
                )

        log.info(
            "\n  ✅ Audio ready (basic): %s",
            Path(output_path).name
        )
        return Path(output_path)

    finally:
        # Cleanup ALL temp files
        for f in temp_files:
            _safe_unlink(f)

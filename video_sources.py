"""
📹 Stock Video Fetcher v5.1 — Final Production Edition

Changes from v5.0:
  ✅ long_portrait orientation fix (Pexels + Pixabay)
  ✅ _select_pexels_files() — long_portrait → portrait
  ✅ _filter_pixabay_by_orientation() — long_portrait
  ✅ _pexels_search() — long_portrait → portrait
  ✅ fetch_videos_for_script() — orientation log fix
"""

from __future__ import annotations

import logging
import os
import random
import re
import subprocess
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
import time
from pathlib import Path
from typing import Optional

import requests

# ── DB import with fallback ───────────────────────
try:
    from db import is_video_used, mark_video_used
except ImportError:
    def is_video_used(vid_id: str, source: str) -> bool:
        return False
    def mark_video_used(
        vid_id: str, keyword: str, source: str
    ) -> None:
        pass

log = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════
# CONSTANTS
# ═══════════════════════════════════════════════════

BASE_DIR        = Path(__file__).parent.resolve()
LOCAL_VIDEO_DIR = BASE_DIR / "assets" / "videos"
TEMP_DIR        = Path(tempfile.gettempdir())

# HD requirements
MIN_HD_WIDTH_LANDSCAPE  = 1280
MIN_HD_WIDTH_PORTRAIT   = 720
MIN_HD_HEIGHT_PORTRAIT  = 1280

PORTRAIT_RATIO_MIN  = 1.3
LANDSCAPE_RATIO_MIN = 1.3

MIN_DURATION   = 5
MIN_FILE_BYTES = 500_000
MIN_FRAMES     = 30
MIN_FPS        = 10

DOWNLOAD_TIMEOUT = 90
API_TIMEOUT      = 15
FFPROBE_TIMEOUT  = 15
FFMPEG_TIMEOUT   = 10

PEXELS_API_URL  = "https://api.pexels.com/videos/search"
PIXABAY_API_URL = "https://pixabay.com/api/videos/"

RETRY_DELAYS  = [1.0, 2.0, 4.0]
MAX_KEYS_SCAN = 20

PEXELS_MAX_PAGE  = 8
PIXABAY_MAX_PAGE = 8

PEXELS_PER_PAGE   = 15
PIXABAY_PER_PAGE  = 20
MAX_VIDEOS_TO_TRY = 12

HTTP_RATE_LIMIT  = 429
HTTP_AUTH_ERRORS = (401, 403)
HTTP_BAD_REQUEST = 400

MOTION_DIFF_THRESHOLD = 0.05

VALID_VIDEO_CONTENT_TYPES = frozenset({
    "video/mp4",
    "video/webm",
    "video/quicktime",
    "video/x-msvideo",
    "application/octet-stream",
})

# ✅ v5.1 — Portrait content modes
_PORTRAIT_MODES = frozenset({
    "short",
    "long_portrait",
})


# ═══════════════════════════════════════════════════
# KEYWORD TRANSLATIONS
# ═══════════════════════════════════════════════════

KEYWORD_TRANSLATIONS: dict[str, str] = {
    "longing eyes":          "person looking away thoughtful close up",
    "strong eye":            "intense eye contact person close up",
    "mysterious closeup":    "serious person face dark dramatic",
    "clear faces":           "person face serious close up cinematic",
    "wide eyes":             "shocked surprised person eyes reaction",
    "determined eyes":       "focused determined person face intense",
    "dark shadows":          "person shadow dark room dramatic light",
    "deep shadows":          "silhouette dark background cinematic",
    "old silhouettes":       "person silhouette dramatic lighting",
    "dramatic shadows":      "dramatic lighting person shadow moody",
    "running figures":       "person walking fast purposeful determined",
    "powerful stance":       "confident person standing strong posture",
    "reaching hands":        "person hand reaching gesture emotional",
    "fist pounding":         "person fist strong determined action",
    "fist pounding table":   "person hitting table frustrated angry",
    "neutral expressions":   "person serious calm face thinking",
    "ticking clocks":        "clock time pressure deadline stress",
    "sudden shock":          "person shocked reaction surprise face",
    "confidence":            "confident person speaking assertive strong",
    "leadership":            "leader person commanding presence authority",
    "respect":               "two people respectful serious conversation",
    "argument":              "people disagreeing tense discussion",
    "persuasion":            "person speaking convincing gesture serious",
    "social pressure":       "group people crowd social pressure",
    "decision making":       "person thinking deciding serious face",
    "confrontation":         "two people face to face tense serious",
    "authority":             "person authority commanding posture strong",
    "intrigue":              "person curious interested leaning forward",
    "desire":                "person longing wanting emotional expression",
    "wisdom":                "thoughtful person contemplating serious calm",
    "calm":                  "person calm peaceful serene breathing",
    "anger":                 "person angry frustrated intense expression",
    "fear":                  "person anxious worried fearful face",
    "inspiration":           "person motivated inspired determined forward",
    "revelation":            "person shocked truth realization wide eyes",
}

CINEMATIC_SUFFIXES_SHORT: list[str] = [
    "cinematic dark moody",
    "dramatic close up portrait",
    "moody atmospheric dark",
    "cinematic slow motion person",
    "vertical portrait dramatic",
    "close up face emotional",
    "portrait cinematic lighting",
    "dramatic face close up",
]

CINEMATIC_SUFFIXES_LONG: list[str] = [
    "cinematic widescreen dramatic",
    "cinematic footage professional dark",
    "dramatic wide shot moody",
    "atmospheric cinematic scene",
    "wide angle dramatic lighting",
    "professional cinematic footage",
    "landscape dramatic moody",
    "widescreen moody atmosphere",
]

TAG_FALLBACK_KEYWORDS: dict[str, list[str]] = {
    "shock": [
        "person shocked reaction close up",
        "dramatic surprise face expression",
        "intense reaction person cinematic",
    ],
    "urgency": [
        "person stressed hurrying time pressure",
        "urgent serious person rushing",
        "tense person deadline pressure",
    ],
    "intrigue": [
        "person curious mysterious expression",
        "intriguing person thinking close up",
        "mysterious serious person dark",
    ],
    "emotional": [
        "emotional person crying close up",
        "sad person tearful expression face",
        "emotional person pain suffering",
    ],
    "confident": [
        "confident person assertive strong",
        "powerful person determined standing",
        "strong confident person speaking",
    ],
    "inspiration": [
        "motivated person determined forward",
        "inspired person success mindset",
        "determined person achievement goal",
    ],
    "wisdom": [
        "wise person thinking contemplating",
        "thoughtful person calm serious",
        "experienced person reflective calm",
    ],
    "calm": [
        "calm person peaceful breathing",
        "serene person quiet moment",
        "peaceful person relaxed indoor",
    ],
    "information": [
        "person explaining serious talking",
        "person speaking direct camera",
        "serious person discussing facts",
    ],
    "storytelling": [
        "person engaged storytelling speaking",
        "storyteller person animated talking",
        "person narrative speaking crowd",
    ],
    "dramatic": [
        "dramatic person intense scene",
        "powerful emotional person dramatic",
        "intense dramatic person close up",
    ],
    "revelation": [
        "person shocked truth realization",
        "dramatic reveal person wide eyes",
        "person discovering truth shocked",
    ],
    "tension": [
        "tense person nervous anxious",
        "stressed person tension building",
        "anxious person worried serious",
    ],
    "climax": [
        "intense emotional peak person",
        "powerful breakthrough person determined",
        "climactic moment person intense",
    ],
    "desire": [
        "person longing wanting emotional",
        "desire person reaching forward",
        "passionate person emotional close up",
    ],
    "pause": [
        "person standing still thoughtful",
        "quiet moment person alone",
        "person pausing reflecting serious",
    ],
    "whisper": [
        "person whispering close up lips",
        "secretive person quiet speaking",
        "close up mouth speaking softly",
    ],
    "curiosity": [
        "person curious questioning face",
        "wondering person looking around",
        "inquisitive person thinking deeply",
    ],
    "powerful": [
        "strong determined person confident",
        "powerful person standing strong",
        "unstoppable person moving forward",
    ],
    "hook": [
        "person dramatic intense face close up",
        "shocking surprised person reaction",
        "magnetic confident person looking camera",
    ],
    "direct": [
        "person pointing direct camera serious",
        "confident person speaking directly",
        "no nonsense person stern face",
    ],
    "cta": [
        "person inviting hand gesture warm",
        "encouraging person smile direct",
        "motivating person call action",
    ],
    "default": [
        "person serious face talking camera",
        "emotional person close up expression",
        "confident person speaking direct",
    ],
}

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
# ✅ v5.1 — ORIENTATION HELPER
# ═══════════════════════════════════════════════════

def _is_portrait_mode(content_mode: str) -> bool:
    """
    ✅ v5.1 — Portrait modes: short + long_portrait.
    Landscape modes: long.
    """
    return content_mode in _PORTRAIT_MODES


# ═══════════════════════════════════════════════════
# KEYWORD HELPERS
# ═══════════════════════════════════════════════════

def _translate_keyword(keyword: str) -> str:
    kw_lower = keyword.lower().strip()

    if kw_lower in KEYWORD_TRANSLATIONS:
        return KEYWORD_TRANSLATIONS[kw_lower]

    for poetic, practical in KEYWORD_TRANSLATIONS.items():
        if poetic in kw_lower:
            return practical
        if kw_lower in poetic and len(kw_lower) > 4:
            return practical

    return keyword


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
                if w not in ABSTRACT_WORDS and len(w) > 2
            ]
            if len(clean) >= 2:
                result.append(" ".join(clean))

    return result


def _build_keyword_variants(
    keyword:      str,
    content_mode: str,
    topic:        str = "",
    n_variants:   int = 6,
    chunk_index:  int = 0,
) -> list[str]:
    kw = " ".join(keyword.strip().split())
    if not kw:
        return ["person serious face talking camera"]

    practical = _translate_keyword(kw)

    # ✅ Portrait suffixes للـ short و long_portrait
    suffixes = (
        CINEMATIC_SUFFIXES_SHORT
        if _is_portrait_mode(content_mode)
        else CINEMATIC_SUFFIXES_LONG
    )

    all_variants: list[str] = []
    seen:         set[str]  = set()

    def add(q: str) -> None:
        clean = " ".join(q.strip().split())
        key   = clean.lower()
        if clean and key not in seen and len(clean) >= 5:
            seen.add(key)
            all_variants.append(clean)

    for suffix in suffixes:
        add(f"{practical} {suffix}")

    add(practical)

    if topic and topic.strip():
        topic_word = topic.strip().split()[0]
        add(f"{practical} {topic_word}")
        for suffix in suffixes[:2]:
            add(f"{practical} {topic_word} {suffix}")

    if kw.lower() != practical.lower():
        for suffix in suffixes[:3]:
            add(f"{kw} {suffix}")
        add(kw)

    if all_variants:
        offset       = chunk_index % len(all_variants)
        all_variants = (
            all_variants[offset:] +
            all_variants[:offset]
        )

    return all_variants[:n_variants] if all_variants else [
        "person serious face talking camera"
    ]


def _get_tag_fallback(
    tag:          str,
    content_mode: str,
) -> list[str]:
    pool = TAG_FALLBACK_KEYWORDS.get(
        tag, TAG_FALLBACK_KEYWORDS["default"]
    )

    # ✅ Portrait suffixes للـ portrait modes
    suffixes = (
        CINEMATIC_SUFFIXES_SHORT
        if _is_portrait_mode(content_mode)
        else CINEMATIC_SUFFIXES_LONG
    )

    result: list[str] = []
    seen:   set[str]  = set()

    for kw in pool:
        if kw not in seen:
            seen.add(kw)
            result.append(kw)
        variant = f"{kw} {suffixes[0]}"
        if variant not in seen:
            seen.add(variant)
            result.append(variant)

    return result


# ═══════════════════════════════════════════════════
# API KEY ROTATION (Thread-safe)
# ═══════════════════════════════════════════════════

_key_indices: dict[str, int]       = {}
_keys_cache:  dict[str, list[str]] = {}
_key_lock:    threading.Lock       = threading.Lock()


def _load_keys_for(prefix: str) -> list[str]:
    with _key_lock:
        if prefix in _keys_cache:
            return _keys_cache[prefix]

        keys: list[str] = []
        seen: set[str]  = set()

        main = os.environ.get(prefix, "").strip()
        if main and main not in seen:
            keys.append(main)
            seen.add(main)

        for i in range(1, MAX_KEYS_SCAN + 1):
            k = os.environ.get(
                f"{prefix}_{i}", ""
            ).strip()
            if not k:
                break
            if k not in seen:
                keys.append(k)
                seen.add(k)

        _keys_cache[prefix]  = keys
        _key_indices[prefix] = 0

        if keys:
            log.info(
                "  🔑 Loaded %d keys for %s",
                len(keys), prefix,
            )
        else:
            log.warning(
                "  ⚠️  No keys found for %s", prefix
            )

        return keys


def _get_current_key(prefix: str) -> str:
    keys = _load_keys_for(prefix)
    if not keys:
        return ""

    with _key_lock:
        idx = _key_indices.get(prefix, 0)

    return keys[idx % len(keys)]


def _rotate_key(prefix: str) -> str:
    with _key_lock:
        keys = _keys_cache.get(prefix, [])
        n    = len(keys)

        if n == 0:
            return ""

        if n == 1:
            log.warning(
                "  ⚠️  %s: only 1 key, cannot rotate",
                prefix,
            )
            return keys[0]

        old = _key_indices.get(prefix, 0)
        new = (old + 1) % n
        _key_indices[prefix] = new

    log.info(
        "  🔄 %s key rotated → #%d/%d",
        prefix, new + 1, n,
    )
    return keys[new]


def _get_pexels_key()     -> str:
    return _get_current_key("PEXELS_API_KEY")
def _get_pixabay_key()    -> str:
    return _get_current_key("PIXABAY_API_KEY")
def _rotate_pexels_key()  -> str:
    return _rotate_key("PEXELS_API_KEY")
def _rotate_pixabay_key() -> str:
    return _rotate_key("PIXABAY_API_KEY")


# ═══════════════════════════════════════════════════
# VIDEO VALIDATION (ffprobe)
# ═══════════════════════════════════════════════════

def _parse_fps(value: str) -> float:
    if not value or "/" not in value:
        return 0.0

    try:
        num, den = value.split("/")
        den_int  = int(den)
        fps      = int(num) / den_int if den_int > 0 else 0.0
        return max(0.0, fps)
    except (ValueError, ZeroDivisionError):
        return 0.0


def _probe_video_info(path: Path) -> dict:
    default = {
        "valid":    False,
        "reason":   "probe error",
        "duration": 0.0,
        "frames":   0,
        "fps":      0.0,
    }

    try:
        r = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-select_streams", "v:0",
                "-show_entries",
                "stream=nb_frames,"
                "r_frame_rate,"
                "avg_frame_rate,"
                "duration",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1",
                str(path),
            ],
            capture_output = True,
            text           = True,
            timeout        = FFPROBE_TIMEOUT,
        )
        if r.returncode != 0:
            return {**default, "reason": "ffprobe failed"}

    except subprocess.TimeoutExpired:
        return {**default, "reason": "ffprobe timeout"}
    except FileNotFoundError:
        return {**default, "reason": "ffprobe not found"}
    except Exception as e:
        return {**default, "reason": f"probe error: {e}"}

    info: dict                   = {
        "duration": 0.0, "frames": 0, "fps": 0.0,
    }
    durations_found: list[float] = []
    fps_values:      dict        = {}

    for line in r.stdout.strip().split("\n"):
        line = line.strip()
        if not line or "=" not in line:
            continue

        key, val = line.split("=", 1)
        key, val = key.strip(), val.strip()

        if not val or val in ("N/A", ""):
            continue

        if key == "duration":
            try:
                durations_found.append(float(val))
            except ValueError:
                pass
        elif key == "nb_frames":
            try:
                info["frames"] = int(val)
            except ValueError:
                pass
        elif key == "avg_frame_rate":
            fps_values["avg"] = _parse_fps(val)
        elif key == "r_frame_rate":
            fps_values["r"] = _parse_fps(val)

    info["fps"] = (
        fps_values.get("avg", 0.0) or
        fps_values.get("r",   0.0) or
        0.0
    )

    if durations_found:
        info["duration"] = max(durations_found)

    if (
        info["frames"] == 0 and
        info["duration"] > 0 and
        info["fps"] > 0
    ):
        info["frames"] = int(
            info["duration"] * info["fps"]
        )

    if info["duration"] < MIN_DURATION:
        return {
            **info, "valid": False,
            "reason": (
                f"too short ({info['duration']:.1f}s)"
            ),
        }

    if 0 < info["fps"] < MIN_FPS:
        return {
            **info, "valid": False,
            "reason": f"low fps ({info['fps']:.1f})",
        }

    if 0 < info["frames"] < MIN_FRAMES:
        return {
            **info, "valid": False,
            "reason": (
                f"too few frames ({info['frames']})"
            ),
        }

    return {**info, "valid": True, "reason": "ok"}


def _detect_motion(path: Path, duration: float) -> bool:
    if duration < 2:
        return True

    try:
        r = subprocess.run(
            [
                "ffmpeg", "-v", "error",
                "-i", str(path),
                "-vf",
                "select='gt(scene,0.02)',"
                "metadata=print:file=-",
                "-frames:v", "30",
                "-f", "null", "-",
            ],
            capture_output = True,
            text           = True,
            timeout        = FFMPEG_TIMEOUT,
        )
        combined = r.stdout + r.stderr
        if (
            "lavfi.scene_score" in combined or
            "pts_time" in combined
        ):
            return True
    except Exception:
        pass

    try:
        pid         = os.getpid()
        frame_sizes : list[int] = []
        temp_files  : list[str] = []

        sample_times = sorted({
            0.5,
            duration / 3,
            duration * 2 / 3,
            max(0.1, duration - 0.5),
        })
        sample_times = [
            t for t in sample_times
            if 0 <= t < duration
        ]

        try:
            for i, t in enumerate(sample_times):
                tmp = str(
                    TEMP_DIR / f"_motion_{pid}_{i}.jpg"
                )
                temp_files.append(tmp)

                r2 = subprocess.run(
                    [
                        "ffmpeg", "-v", "error", "-y",
                        "-ss", str(t),
                        "-i", str(path),
                        "-vframes", "1",
                        "-q:v", "5",
                        tmp,
                    ],
                    capture_output = True,
                    text           = True,
                    timeout        = FFMPEG_TIMEOUT,
                )
                if (
                    r2.returncode == 0 and
                    Path(tmp).exists()
                ):
                    frame_sizes.append(
                        Path(tmp).stat().st_size
                    )
        finally:
            for tmp in temp_files:
                Path(tmp).unlink(missing_ok=True)

        if len(frame_sizes) < 2:
            return True

        if len(set(frame_sizes)) == 1:
            return False

        min_s = min(frame_sizes)
        max_s = max(frame_sizes)

        if min_s == 0:
            return True

        return (
            (max_s - min_s) / min_s >
            MOTION_DIFF_THRESHOLD
        )

    except Exception:
        return True


def _is_video_valid(
    path: Path,
) -> tuple[bool, str]:
    if not path.exists() or not path.is_file():
        return False, "file not found"

    info = _probe_video_info(path)
    if not info["valid"]:
        return False, info["reason"]

    if not _detect_motion(path, info["duration"]):
        return False, "static (no motion detected)"

    return (
        True,
        f"ok ({info['frames']}f, "
        f"{info['fps']:.0f}fps, "
        f"{info['duration']:.1f}s)",
    )


# ═══════════════════════════════════════════════════
# DOWNLOAD
# ═══════════════════════════════════════════════════

def _is_valid_content_type(content_type: str) -> bool:
    if not content_type:
        return False
    ct = content_type.lower().split(";")[0].strip()
    return ct in VALID_VIDEO_CONTENT_TYPES or "video" in ct


def _safe_name(keyword: str, length: int = 20) -> str:
    clean = re.sub(r"[^a-z0-9_]", "_", keyword.lower())
    clean = re.sub(r"_+", "_", clean).strip("_")
    return clean[:length] if clean else "video"


def _safe_size(p: Path) -> int:
    try:
        return p.stat().st_size
    except (FileNotFoundError, OSError):
        return 0


def _download(
    url:     str,
    dest:    Path,
    retries: int = 3,
) -> bool:
    for attempt in range(retries):
        try:
            with requests.get(
                url,
                stream          = True,
                timeout         = DOWNLOAD_TIMEOUT,
                allow_redirects = True,
            ) as r:
                r.raise_for_status()

                content_type = r.headers.get(
                    "Content-Type", ""
                )
                if not _is_valid_content_type(
                    content_type
                ):
                    log.debug(
                        "    ⏭️  Invalid content-type: %s",
                        content_type,
                    )
                    return False

                content_len = int(
                    r.headers.get("Content-Length", 0)
                    or 0
                )
                if 0 < content_len < MIN_FILE_BYTES:
                    log.debug(
                        "    ⏭️  Too small: %d bytes",
                        content_len,
                    )
                    return False

                with open(dest, "wb") as f:
                    for chunk in r.iter_content(65_536):
                        if chunk:
                            f.write(chunk)

            if (
                not dest.exists() or
                _safe_size(dest) < MIN_FILE_BYTES
            ):
                dest.unlink(missing_ok=True)
                raise ValueError(
                    f"File too small: {dest}"
                )

            is_valid, reason = _is_video_valid(dest)
            if not is_valid:
                log.info(
                    "    ⏭️  Invalid video: %s", reason
                )
                dest.unlink(missing_ok=True)
                return False

            log.info("    ✅ Downloaded: %s", reason)
            return True

        except Exception as e:
            dest.unlink(missing_ok=True)

            if attempt < retries - 1:
                wait = RETRY_DELAYS[
                    min(attempt, len(RETRY_DELAYS) - 1)
                ]
                log.warning(
                    "    ⚠️  Download %d/%d failed: "
                    "%s — retry %.1fs",
                    attempt + 1, retries,
                    str(e)[:80], wait,
                )
                time.sleep(wait)

    return False


# ═══════════════════════════════════════════════════
# LOCAL VIDEOS
# ═══════════════════════════════════════════════════

def _list_local_videos() -> list[Path]:
    if not LOCAL_VIDEO_DIR.exists():
        return []

    return (
        list(LOCAL_VIDEO_DIR.glob("*.mp4")) +
        list(LOCAL_VIDEO_DIR.glob("*.mov"))
    )


def _search_local(
    keyword:      str,
    index:        int,
    output_dir:   str,
    session_used: set[str],
    session_lock: threading.Lock,
    content_mode: str = "short",
) -> Optional[Path]:
    all_videos = _list_local_videos()
    if not all_videos:
        return None

    kw_clean = keyword.lower().replace(" ", "_")
    matches  = [
        v for v in all_videos
        if kw_clean in v.stem.lower()
    ]
    pool = matches if matches else all_videos

    valid_pool: list[Path] = []
    for v in pool:
        if str(v) in session_used:
            continue
        is_valid, _ = _is_video_valid(v)
        if is_valid:
            valid_pool.append(v)

    if not valid_pool:
        valid_pool = [
            v for v in pool
            if str(v) not in session_used
        ]

    if not valid_pool:
        valid_pool = pool

    if not valid_pool:
        return None

    pick = random.choice(valid_pool)
    with session_lock:
        session_used.add(str(pick))

    log.info("    📁 Local: %s", pick.name)
    return pick


# ═══════════════════════════════════════════════════
# ✅ v5.1 — PEXELS HD ENFORCED (portrait fix)
# ═══════════════════════════════════════════════════

def _select_pexels_files(
    files:        list[dict],
    content_mode: str,
) -> list[dict]:
    """
    ✅ v5.1 — اختيار أفضل ملف Pexels.
    Portrait modes: short + long_portrait
    Landscape modes: long
    """
    mp4_files = [
        f for f in files
        if f.get("file_type") == "video/mp4"
    ]
    if not mp4_files:
        return []

    is_portrait = _is_portrait_mode(content_mode)

    if is_portrait:
        # Portrait HD (720×1280+)
        filtered = [
            f for f in mp4_files
            if (
                f.get("height", 0) > f.get("width", 0) and
                f.get("width", 0)  >= MIN_HD_WIDTH_PORTRAIT and
                f.get("height", 0) >= MIN_HD_HEIGHT_PORTRAIT
            )
        ]

        if not filtered:
            log.debug(
                "  ⚠️  No HD portrait — any quality"
            )
            filtered = [
                f for f in mp4_files
                if f.get("height", 0) > f.get("width", 0)
            ]
    else:
        # Landscape HD (1280×720+)
        filtered = [
            f for f in mp4_files
            if (
                f.get("width", 0) > f.get("height", 0) and
                f.get("width", 0) >= MIN_HD_WIDTH_LANDSCAPE
            )
        ]

        if not filtered:
            log.debug(
                "  ⚠️  No HD landscape — any quality"
            )
            filtered = [
                f for f in mp4_files
                if f.get("width", 0) > f.get("height", 0)
            ]

    if not filtered:
        filtered = mp4_files

    return sorted(
        filtered,
        key     = lambda f: (
            f.get("width", 0) * f.get("height", 0)
        ),
        reverse = True,
    )


def _pexels_search(
    query:        str,
    content_mode: str,
    page:         int = 1,
    retries:      int = 3,
) -> list[dict]:
    """
    ✅ v5.1 — استدعاء Pexels API.
    Portrait: short + long_portrait → orientation=portrait
    Landscape: long → orientation=landscape
    """
    # ✅ الإصلاح الرئيسي
    orientation = (
        "portrait"
        if _is_portrait_mode(content_mode)
        else "landscape"
    )
    size = "large"

    for attempt in range(retries):
        api_key = _get_pexels_key()
        if not api_key:
            return []

        try:
            r = requests.get(
                PEXELS_API_URL,
                headers = {"Authorization": api_key},
                params  = {
                    "query":       query,
                    "per_page":    PEXELS_PER_PAGE,
                    "orientation": orientation,
                    "size":        size,
                    "page":        page,
                },
                timeout = API_TIMEOUT,
            )
            r.raise_for_status()
            return r.json().get("videos", [])

        except requests.exceptions.HTTPError as e:
            status = (
                e.response.status_code
                if e.response else 0
            )

            if status == HTTP_RATE_LIMIT:
                log.warning(
                    "    ⚠️  Pexels rate limit — rotating"
                )
                _rotate_pexels_key()
                time.sleep(2)
            elif status in HTTP_AUTH_ERRORS:
                log.warning(
                    "    ⚠️  Pexels auth — rotating"
                )
                _rotate_pexels_key()
                continue
            else:
                if attempt < retries - 1:
                    time.sleep(
                        RETRY_DELAYS[
                            min(attempt,
                                len(RETRY_DELAYS) - 1)
                        ]
                    )
        except Exception:
            if attempt < retries - 1:
                time.sleep(
                    RETRY_DELAYS[
                        min(attempt, len(RETRY_DELAYS) - 1)
                    ]
                )

    return []


def _search_pexels(
    keyword:      str,
    index:        int,
    sub:          int,
    output_dir:   str,
    session_used: set[str],
    session_lock: threading.Lock,
    content_mode: str           = "short",
    topic:        str           = "",
    last_used_id: Optional[str] = None,
) -> Optional[Path]:
    if not _get_pexels_key():
        return None

    variants = _build_keyword_variants(
        keyword, content_mode, topic,
        n_variants=6, chunk_index=index,
    )
    tried_ids: set[str] = set()

    for v_idx, query in enumerate(variants):
        page = (
            (index * 3 + v_idx) % PEXELS_MAX_PAGE
        ) + 1

        log.info(
            "    🔎 Pexels [%s] p%d v%d/%d: %r",
            content_mode, page,
            v_idx + 1, len(variants), query,
        )

        videos = _pexels_search(
            query, content_mode, page=page
        )

        if not videos and page > 1:
            videos = _pexels_search(
                query, content_mode, page=1
            )

        videos = [
            v for v in videos
            if v.get("duration", 0) >= MIN_DURATION
        ]

        if not videos:
            continue

        if len(videos) > 3:
            top    = videos[:5]
            rest   = videos[5:]
            random.shuffle(top)
            videos = top + rest

        for video in videos[:MAX_VIDEOS_TO_TRY]:
            vid_id = str(video["id"])
            sk     = f"px_{vid_id}"

            if vid_id in tried_ids:
                continue
            tried_ids.add(vid_id)

            if last_used_id and vid_id == last_used_id:
                log.debug(
                    "    ⏭️  Same as last: %s", vid_id
                )
                continue

            with session_lock:
                if sk in session_used:
                    continue
                session_used.add(sk)

            if is_video_used(vid_id, "pexels"):
                with session_lock:
                    session_used.discard(sk)
                continue

            files = _select_pexels_files(
                video.get("video_files", []),
                content_mode,
            )
            if not files:
                with session_lock:
                    session_used.discard(sk)
                continue

            url = files[0].get("link", "")
            if not url:
                with session_lock:
                    session_used.discard(sk)
                continue

            dest = Path(output_dir) / (
                f"{index:03d}_{sub}_px_{vid_id}_"
                f"{_safe_name(keyword)}.mp4"
            )

            if _download(url, dest):
                mark_video_used(vid_id, keyword, "pexels")
                return dest
            else:
                with session_lock:
                    session_used.discard(sk)

    return None


# ═══════════════════════════════════════════════════
# ✅ v5.1 — PIXABAY HD ENFORCED (portrait fix)
# ═══════════════════════════════════════════════════

def _select_pixabay_url(
    videos:       dict,
    content_mode: str,
) -> str:
    priority = ["large", "medium", "small", "tiny"]

    for quality in priority:
        url = videos.get(quality, {}).get("url", "")
        if url and ".mp4" in url.lower():
            return url

    return ""


def _filter_pixabay_by_orientation(
    hits:         list[dict],
    content_mode: str,
) -> list[dict]:
    """
    ✅ v5.1 — فلترة Pixabay حسب orientation.
    Portrait modes: short + long_portrait
    Landscape modes: long
    """
    if not hits:
        return hits

    filtered:    list[dict] = []
    is_portrait = _is_portrait_mode(content_mode)

    for hit in hits:
        videos  = hit.get("videos", {})
        matched = False

        for quality in ("large", "medium"):
            vid = videos.get(quality, {})
            w   = vid.get("width",  0)
            h   = vid.get("height", 0)

            if w > 0 and h > 0:
                if is_portrait:
                    # Portrait HD: 720×1280+
                    if (
                        h > w and
                        w >= MIN_HD_WIDTH_PORTRAIT and
                        h >= MIN_HD_HEIGHT_PORTRAIT
                    ):
                        filtered.append(hit)
                        matched = True
                        break
                else:
                    # Landscape HD: 1280×720+
                    if (
                        w > h and
                        w >= MIN_HD_WIDTH_LANDSCAPE
                    ):
                        filtered.append(hit)
                        matched = True
                        break

    return (
        filtered if len(filtered) >= 3 else hits
    )


def _pixabay_search(
    query:   str,
    page:    int = 1,
    retries: int = 3,
) -> list[dict]:
    for attempt in range(retries):
        api_key = _get_pixabay_key()
        if not api_key:
            return []

        try:
            r = requests.get(
                PIXABAY_API_URL,
                params={
                    "key":        api_key,
                    "q":          query,
                    "video_type": "film",
                    "per_page":   PIXABAY_PER_PAGE,
                    "page":       page,
                    "safesearch": "true",
                    "order":      "popular",
                },
                timeout=API_TIMEOUT,
            )
            r.raise_for_status()
            return r.json().get("hits", [])

        except requests.exceptions.HTTPError as e:
            status = (
                e.response.status_code
                if e.response else 0
            )

            if status == HTTP_RATE_LIMIT:
                log.warning(
                    "    ⚠️  Pixabay rate limit — rotating"
                )
                _rotate_pixabay_key()
                time.sleep(2)
            elif status in (
                HTTP_BAD_REQUEST, *HTTP_AUTH_ERRORS
            ):
                log.warning(
                    "    ⚠️  Pixabay auth — rotating"
                )
                _rotate_pixabay_key()
                continue
            else:
                if attempt < retries - 1:
                    time.sleep(
                        RETRY_DELAYS[
                            min(attempt,
                                len(RETRY_DELAYS) - 1)
                        ]
                    )
        except Exception:
            if attempt < retries - 1:
                time.sleep(
                    RETRY_DELAYS[
                        min(attempt, len(RETRY_DELAYS) - 1)
                    ]
                )

    return []


def _search_pixabay(
    keyword:      str,
    index:        int,
    sub:          int,
    output_dir:   str,
    session_used: set[str],
    session_lock: threading.Lock,
    content_mode: str           = "short",
    topic:        str           = "",
    last_used_id: Optional[str] = None,
) -> Optional[Path]:
    if not _get_pixabay_key():
        return None

    variants = _build_keyword_variants(
        keyword, content_mode, topic,
        n_variants=6, chunk_index=index + 2,
    )
    tried_ids: set[str] = set()

    for v_idx, query in enumerate(variants):
        page = (
            (index * 3 + v_idx + 4) % PIXABAY_MAX_PAGE
        ) + 1

        log.info(
            "    🔎 Pixabay [%s] p%d v%d/%d: %r",
            content_mode, page,
            v_idx + 1, len(variants), query,
        )

        hits = _pixabay_search(query, page=page)
        if not hits and page > 1:
            hits = _pixabay_search(query, page=1)

        # ✅ orientation filter (يعرف long_portrait)
        hits = _filter_pixabay_by_orientation(
            hits, content_mode
        )
        hits = [
            h for h in hits
            if h.get("duration", 0) >= MIN_DURATION
        ]

        if not hits:
            continue

        if len(hits) > 3:
            top  = hits[:5]
            rest = hits[5:]
            random.shuffle(top)
            hits = top + rest

        for hit in hits[:MAX_VIDEOS_TO_TRY]:
            vid_id = str(hit["id"])
            sk     = f"pb_{vid_id}"

            if vid_id in tried_ids:
                continue
            tried_ids.add(vid_id)

            if last_used_id and vid_id == last_used_id:
                log.debug(
                    "    ⏭️  Same as last: %s", vid_id
                )
                continue

            with session_lock:
                if sk in session_used:
                    continue
                session_used.add(sk)

            if is_video_used(vid_id, "pixabay"):
                with session_lock:
                    session_used.discard(sk)
                continue

            url = _select_pixabay_url(
                hit.get("videos", {}), content_mode
            )
            if not url:
                with session_lock:
                    session_used.discard(sk)
                continue

            dest = Path(output_dir) / (
                f"{index:03d}_{sub}_pb_{vid_id}_"
                f"{_safe_name(keyword)}.mp4"
            )

            if _download(url, dest):
                mark_video_used(
                    vid_id, keyword, "pixabay"
                )
                return dest
            else:
                with session_lock:
                    session_used.discard(sk)

    return None


# ═══════════════════════════════════════════════════
# EXTRACT VIDEO ID FROM FILENAME
# ═══════════════════════════════════════════════════

_FILENAME_ID_RE = re.compile(r"_(px|pb)_(\d+)_")


def _extract_video_id(
    path: Optional[Path],
) -> Optional[str]:
    if not path:
        return None

    match = _FILENAME_ID_RE.search(path.stem)
    if match:
        return match.group(2)

    return path.stem


# ═══════════════════════════════════════════════════
# FALLBACK (Anti-duplication, 4 levels)
# ═══════════════════════════════════════════════════

def _get_fallback_video(
    output_dir:   str,
    index:        int,
    last_used:    Optional[Path]     = None,
    session_used: Optional[set[str]] = None,
) -> Optional[Path]:
    out_path   = Path(output_dir)
    existing   = sorted(out_path.glob("*.mp4"))
    used_paths = session_used or set()
    used_names = {Path(p).name for p in used_paths}

    # Level 1
    pool = [
        f for f in existing
        if (
            f != last_used and
            str(f) not in used_paths and
            f.name not in used_names and
            _safe_size(f) > MIN_FILE_BYTES
        )
    ]

    if pool:
        pick = random.choice(pool)
        if session_used is not None:
            session_used.add(str(pick))
        log.info("    🆕 New fallback: %s", pick.name)
        return pick

    # Level 2
    local = _list_local_videos()
    if local:
        unused_local = [
            v for v in local
            if (
                str(v) not in used_paths and
                v.name not in used_names
            )
        ]

        if unused_local:
            pick = random.choice(unused_local)
            if session_used is not None:
                session_used.add(str(pick))
            log.info(
                "    📁 Local (new): %s", pick.name
            )
            return pick

    # Level 3
    pool = [
        f for f in existing
        if (
            f != last_used and
            _safe_size(f) > MIN_FILE_BYTES
        )
    ]

    if pool:
        pick = random.choice(pool)
        log.warning(
            "    ⚠️  Reusing (no new): %s", pick.name
        )
        return pick

    # Level 4
    if existing:
        pick = random.choice(existing)
        log.warning(
            "    ⚠️  Last resort: %s", pick.name
        )
        return pick

    log.error("    ❌ No fallback videos!")
    return None


# ═══════════════════════════════════════════════════
# GAP FILLING
# ═══════════════════════════════════════════════════

def _fill_gaps(
    results:      list[Optional[Path]],
    output_dir:   str,
    session_used: Optional[set[str]] = None,
) -> list[Path]:
    available = [r for r in results if r is not None]
    local     = _list_local_videos()
    all_pool  = list(set(available + local))

    if not all_pool:
        raise RuntimeError(
            "Could not fetch any videos. "
            "Check PEXELS_API_KEY and PIXABAY_API_KEY."
        )

    used_in_gaps: set[Path]      = set()
    last_used:    Optional[Path] = None

    for i in range(len(results)):
        if results[i] is not None:
            last_used = results[i]
            continue

        candidates = [
            v for v in all_pool
            if v != last_used and v not in used_in_gaps
        ]

        if not candidates:
            candidates = [
                v for v in all_pool if v != last_used
            ]

        if not candidates:
            candidates = all_pool

        picked     = random.choice(candidates)
        results[i] = picked
        last_used  = picked
        used_in_gaps.add(picked)

        log.info(
            "  [%d] ♻️  Gap filled: %s",
            i + 1, picked.name,
        )

    final = []
    for i, r in enumerate(results):
        if r is None:
            raise RuntimeError(
                f"Could not fill gap at index {i}"
            )
        final.append(r)

    unique_count = len(set(final))
    total_count  = len(final)
    log.info(
        "  📊 Used %d unique videos out of %d clips",
        unique_count, total_count,
    )

    return final


# ═══════════════════════════════════════════════════
# TRY FETCH ONE
# ═══════════════════════════════════════════════════

def _try_fetch_one(
    keywords:     list[str],
    index:        int,
    output_dir:   str,
    session_used: set[str],
    session_lock: threading.Lock,
    content_mode: str,
    topic:        str           = "",
    tag:          str           = "information",
    last_used_id: Optional[str] = None,
) -> Optional[Path]:
    clean_kws = _filter_abstract_keywords(keywords)
    if not clean_kws:
        clean_kws = keywords

    tag_fallbacks = _get_tag_fallback(tag, content_mode)
    all_keywords: list[str] = list(clean_kws)
    for kw in tag_fallbacks[:3]:
        if kw not in all_keywords:
            all_keywords.append(kw)

    for sub, kw in enumerate(all_keywords):
        kw = kw.strip()
        if not kw:
            continue

        log.info(
            "  [%d] [%s] Trying (%d/%d): %r",
            index + 1, tag,
            sub + 1, len(all_keywords), kw,
        )

        path = _search_local(
            kw, index, output_dir,
            session_used, session_lock, content_mode,
        )
        if path:
            return path

        path = _search_pexels(
            kw, index, sub, output_dir,
            session_used, session_lock,
            content_mode, topic,
            last_used_id=last_used_id,
        )
        if path:
            return path

        path = _search_pixabay(
            kw, index, sub, output_dir,
            session_used, session_lock,
            content_mode, topic,
            last_used_id=last_used_id,
        )
        if path:
            return path

    return None


# ═══════════════════════════════════════════════════
# ✅ v5.1 — MAIN FETCH FUNCTION
# ═══════════════════════════════════════════════════

def fetch_videos_for_script(
    keywords_per_sentence: list[list[str]],
    clip_durations:        list[float],
    output_dir:            str,
    content_mode:          str                  = "short",
    aligned:               Optional[list[dict]] = None,
    topic:                 str                  = "",
    max_workers:           int                  = 3,
) -> list[Path]:
    """
    ✅ v5.2 — جلب الفيديوهات بالتوازي الفعلي (ThreadPoolExecutor).

    ملاحظات التصميم:
      - IO-bound بالكامل (requests + subprocess ffprobe/ffmpeg)
        لذلك ThreadPoolExecutor فعّال جداً هنا (الـ GIL يُحرَّر
        أثناء الشبكة و subprocess).
      - session_used محمي بالفعل بـ session_lock، وتدوير مفاتيح
        الـ API محمي بـ _key_lock — آمن للاستخدام من عدة threads.
      - قيد "تجنب نفس فيديو المقطع السابق مباشرة" (last_used_id)
        أصبح "أفضل محاولة" (best-effort) بدل ضمان صارم، لأن
        المقاطع تُنفَّذ بالتوازي فعلياً؛ لا يزال يمنع التكرار عبر
        session_used + قاعدة البيانات (mark_video_used) بشكل مضمون.
    """
    Path(output_dir).mkdir(parents=True, exist_ok=True)

    n            = len(keywords_per_sentence)
    session_used : set[str]             = set()
    session_lock : threading.Lock       = threading.Lock()
    results      : list[Optional[Path]] = [None] * n

    if n == 0:
        return []

    pexels_keys  = _load_keys_for("PEXELS_API_KEY")
    pixabay_keys = _load_keys_for("PIXABAY_API_KEY")

    orientation = (
        "portrait"
        if _is_portrait_mode(content_mode)
        else "landscape"
    )

    effective_workers = max(1, min(max_workers, n))

    log.info(
        "\n  📹 Fetching %d videos [%s] (workers=%d)",
        n, content_mode.upper(), effective_workers,
    )
    log.info("     Orientation : %s", orientation)
    log.info(
        "     Pexels keys : %d | Pixabay keys: %d",
        len(pexels_keys), len(pixabay_keys),
    )
    log.info(
        "     Quality     : HD enforced (Large priority)"
    )
    if topic:
        log.info("     Topic       : %s", topic[:50])

    def _get_tag(i: int) -> str:
        if aligned and i < len(aligned):
            return str(
                aligned[i].get("tag", "information")
            )
        return "information"

    # ✅ last_used_id مشترك بين الخيوط — best-effort فقط
    last_used_holder: dict = {"id": None}
    last_used_lock = threading.Lock()

    def _fetch_one_chunk(i: int) -> tuple[int, Optional[Path]]:
        kws = keywords_per_sentence[i]
        tag = _get_tag(i)
        dur = (
            clip_durations[i]
            if i < len(clip_durations)
            else 3.0
        )

        with last_used_lock:
            avoid_id = last_used_holder["id"]

        log.info(
            "\n  🎞️  Chunk [%d/%d] (%.2fs) [%s]",
            i + 1, n, dur, tag,
        )

        path = _try_fetch_one(
            keywords     = kws,
            index        = i,
            output_dir   = output_dir,
            session_used = session_used,
            session_lock = session_lock,
            content_mode = content_mode,
            topic        = topic,
            tag          = tag,
            last_used_id = avoid_id,
        )

        if not path:
            path = _get_fallback_video(
                output_dir, i,
                last_used    = None,
                session_used = session_used,
            )

        vid_id = _extract_video_id(path) if path else None
        if vid_id:
            with last_used_lock:
                last_used_holder["id"] = vid_id

        return i, path

    with ThreadPoolExecutor(
        max_workers=effective_workers
    ) as executor:
        futures = {
            executor.submit(_fetch_one_chunk, i): i
            for i in range(n)
        }
        for future in as_completed(futures):
            i = futures[future]
            try:
                idx, path = future.result()
            except Exception as e:
                log.error(
                    "  ❌ Chunk [%d/%d] crashed: %s",
                    i + 1, n, e,
                )
                idx, path = i, None

            results[idx] = path

            if path:
                vid_id = _extract_video_id(path)
                log.info(
                    "  [%d/%d] ✅ %s (id=%s)",
                    idx + 1, n, path.name, vid_id,
                )
            else:
                log.warning(
                    "  [%d/%d] ❌ not found", idx + 1, n
                )

    final = _fill_gaps(results, output_dir, session_used)

    success = sum(1 for r in final if r is not None)
    log.info(
        "\n  ✅ Fetched: %d/%d videos [%s]",
        success, n, content_mode.upper(),
    )

    return final

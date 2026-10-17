// remotion/render.mjs
// ═══════════════════════════════════════════════════════════════════════════
// 🎬 Video Renderer — Cinematic Edition v6.1
//
// Fixes from v6:
//   ✅ 1. _finalizeLongVideo() — stream_loop + re-encode للـ video
//   ✅ 2. safeWordTop — يأخذ translateY و scaleMult في الحسبان
//   ✅ 3. renderPNGsShort() — حذف HTML files بعد الاستخدام
//   ✅ 4. bg_style — يُستخدم في buildDramaticLightingFilter()
// ═══════════════════════════════════════════════════════════════════════════

import {
  readFileSync, writeFileSync, mkdirSync,
  copyFileSync, existsSync, symlinkSync,
  rmSync,
} from "fs";
import { spawnSync } from "child_process";
import { tmpdir } from "os";
import { join } from "path";
import { chromium } from "playwright";

// ═══════════════════════════════════════════════════════════════════════════
// ARGS & MANIFEST
// ═══════════════════════════════════════════════════════════════════════════

const manifestPath = process.argv[2];
const outputPath   = process.argv[3];

if (!manifestPath || !outputPath) {
  console.error(
    "Usage: node render.mjs <manifest.json> <output.mp4>"
  );
  process.exit(1);
}

let props;
try {
  props = JSON.parse(
    readFileSync(manifestPath, "utf-8")
  );
} catch (e) {
  console.error(
    `❌ Cannot read manifest: ${e.message}`
  );
  process.exit(1);
}

const {
  title,
  display_title  = title,
  emoji_left     = "🔥",
  emoji_right    = "💥",
  sentences      = [],
  audio,
  videos         = [],
  duration_s     = 0,
  power_words    = [],
  aligned        = [],
  lang           = "ar",
  clip_duration  = 3.0,
  clip_durations = [],
  has_hook       = false,
  custom_hook    = "",
  analysis       = {},
  mode           = "words_only",
  content_mode   = "short",
  platform       = "yt",
  width          = null,
  height         = null,
  bg_style       = "video",
} = props;

// ═══════════════════════════════════════════════════════════════════════════
// DIMENSIONS
// ═══════════════════════════════════════════════════════════════════════════

function getDimensions() {
  if (width && height && width > 0 && height > 0) {
    return { width, height };
  }
  if (content_mode === "long" && platform === "yt") {
    return { width: 1920, height: 1080 };
  }
  return { width: 1080, height: 1920 };
}

const { width: WIDTH, height: HEIGHT } = getDimensions();

const isLong  = content_mode === "long";
const isShort = !isLong;
const FPS     = 30;

const INTRO_FRAMES       = Math.floor(1.0 * FPS);
const OUTRO_FRAMES       = Math.floor(1.0 * FPS);
const HOOK_FRAMES        = Math.floor(3.0 * FPS);
const TITLE_SLIDE_FRAMES = Math.floor(0.6 * FPS);
const XFADE_DUR          = isLong ? 0.50 : 0.28;
const NORMALIZE_BUFFER   = 1.15;

// ═══════════════════════════════════════════════════════════════════════════
// RNG
// ═══════════════════════════════════════════════════════════════════════════

const _VIDEO_SEED = (
  (title || "").charCodeAt(0) * 31 +
  (lang  || "ar").charCodeAt(0) * 7
) || 42;

function createRng(seed) {
  let s = (seed >>> 0) || 1;
  return function () {
    s = Math.imul(s ^ (s >>> 15), s | 1);
    s ^= s + Math.imul(s ^ (s >>> 7), s | 61);
    return ((s ^ (s >>> 14)) >>> 0) / 0x100000000;
  };
}

const _nextRand = createRng(_VIDEO_SEED);

// ═══════════════════════════════════════════════════════════════════════════
// ✅ DURATION CACHE — LRU — مُعرَّف قبل _cleanup
// ═══════════════════════════════════════════════════════════════════════════

const DURATION_CACHE_MAX = 100;
const _durationCache     = new Map();

function _durationCacheGet(fp) {
  if (!_durationCache.has(fp)) return undefined;
  const val = _durationCache.get(fp);
  _durationCache.delete(fp);
  _durationCache.set(fp, val);
  return val;
}

function _durationCacheSet(fp, val) {
  if (_durationCache.size >= DURATION_CACHE_MAX) {
    const firstKey =
      _durationCache.keys().next().value;
    _durationCache.delete(firstKey);
  }
  _durationCache.set(fp, val);
}

// ═══════════════════════════════════════════════════════════════════════════
// TMP DIR + CLEANUP
// ═══════════════════════════════════════════════════════════════════════════

const safeOut = outputPath
  .replace(/[^a-zA-Z0-9]/g, "_")
  .replace(/_+/g, "_")
  .slice(-22);
const TMP = join(
  tmpdir(), `vsg_${safeOut}_${process.pid}`
);
mkdirSync(TMP, { recursive: true });

let _renderCompleted = false;

function _cleanup() {
  try {
    rmSync(TMP, { recursive: true, force: true });
    console.log(`  🗑️  TMP cleaned: ${TMP}`);
  } catch {}
  try { _durationCache.clear(); } catch {}
}

process.on("exit",              ()  => _cleanup());
process.on("SIGINT",            ()  => {
  _cleanup(); process.exit(130);
});
process.on("SIGTERM",           ()  => {
  _cleanup(); process.exit(143);
});
process.on("uncaughtException", (e) => {
  console.error("❌ Uncaught:", e.message);
  _cleanup(); process.exit(1);
});
process.on("unhandledRejection", (r) => {
  console.error("❌ Unhandled rejection:", r);
  _cleanup(); process.exit(1);
});

console.log(
  `📌 ${emoji_left} ${display_title} ${emoji_right}`
);
console.log(
  `🌐 Lang:${lang.toUpperCase()} | Mode:${mode} | ` +
  `${content_mode.toUpperCase()}/${platform.toUpperCase()}` +
  ` | ${WIDTH}×${HEIGHT}`
);
isLong
  ? console.log("  🚀 Long: FFmpeg cinematic")
  : console.log("  🎨 Short: Playwright quality");

// ═══════════════════════════════════════════════════════════════════════════
// TRANSITIONS
// ═══════════════════════════════════════════════════════════════════════════

const CINEMATIC_TRANSITIONS_LONG = [
  "fade","fadeblack","fadegrays",
  "smoothleft","smoothright","smoothup","smoothdown",
  "wipeleft","wiperight","circleopen",
  "fadefast","distance",
];

const CINEMATIC_TRANSITIONS_SHORT = [
  "fade","fadeblack",
  "smoothleft","smoothright","smoothup","smoothdown",
  "wipeleft","wiperight",
  "circleopen","circleclose","circlecrop","rectcrop",
  "slideright","slideleft","slideup","slidedown",
  "pixelize",
];

const TAG_TRANSITIONS = {
  shock:        ["circleopen","pixelize","rectcrop","fadeblack"],
  urgency:      ["wipeleft","wiperight","slideright","slideleft"],
  revelation:   ["circleopen","circleclose","distance","fadeblack"],
  climax:       ["circleopen","pixelize","rectcrop","circlecrop"],
  dramatic:     ["fadeblack","fadegrays","smoothleft","distance"],
  tension:      ["wipeleft","smoothleft","slideup","fadeblack"],
  emotional:    ["fade","fadegrays","smoothup","smoothdown"],
  calm:         ["fade","fadegrays","smoothleft","smoothright"],
  wisdom:       ["fade","smoothleft","fadegrays","distance"],
  information:  ["smoothleft","smoothright","fade","wipeleft"],
  inspiration:  ["circleopen","smoothup","slideup","wiperight"],
  storytelling: ["smoothleft","fade","smoothright","wipeleft"],
  confident:    ["wipeleft","slideright","circleopen","smoothright"],
  default:      ["fade","smoothleft","wipeleft","fadeblack"],
};

function pickTransition(prevType, tag) {
  if (!tag) tag = "default";
  const tagPool  =
    TAG_TRANSITIONS[tag] || TAG_TRANSITIONS.default;
  const modePool = isLong
    ? CINEMATIC_TRANSITIONS_LONG
    : CINEMATIC_TRANSITIONS_SHORT;
  const combined = [...tagPool, ...tagPool, ...modePool];
  const filtered = combined.filter(t => t !== prevType);
  const pool     = filtered.length > 0
    ? filtered : combined;
  return pool[Math.floor(_nextRand() * pool.length)];
}

// ═══════════════════════════════════════════════════════════════════════════
// GPS LOCATIONS
// ═══════════════════════════════════════════════════════════════════════════

const GPS_LOCATIONS = {
  ar: {
    city:"Riyadh", country:"Saudi Arabia",
    lat:"24.7136", lon:"46.6753",
    latRef:"N", lonRef:"E",
    iso6709:"+24.7136+046.6753/",
  },
  fr: {
    city:"Paris", country:"France",
    lat:"48.8566", lon:"2.3522",
    latRef:"N", lonRef:"E",
    iso6709:"+48.8566+002.3522/",
  },
  en: {
    city:"New York", country:"United States",
    lat:"40.7128", lon:"-74.0060",
    latRef:"N", lonRef:"W",
    iso6709:"+40.7128-074.0060/",
  },
};
const location =
  GPS_LOCATIONS[lang] || GPS_LOCATIONS.ar;

function buildiPhoneMetadata() {
  const now     = new Date();
  const dateISO = now.toISOString();
  const dateStr = dateISO
    .replace(/[-:]/g, "").split(".")[0];

  const randHex = (len) =>
    Array.from({ length: len }, () =>
      Math.floor(_nextRand() * 16).toString(16)
    ).join("").toUpperCase();

  const serial = "F" + randHex(8);
  const uuid   = [
    randHex(8), randHex(4), randHex(4),
    randHex(4), randHex(12),
  ].join("-");

  const g = () =>
    ((_nextRand() * 0.02) - 0.01).toFixed(6);
  const a = () =>
    ((_nextRand() * 0.1)  - 0.05).toFixed(6);

  return [
    "-map_metadata", "-1",
    "-metadata", "make=Apple",
    "-metadata", "model=iPhone 16 Pro Max",
    "-metadata", "software=iOS 18.3",
    "-metadata", "encoder=Apple iPhone 16 Pro Max",
    "-metadata",
    "handler_name=Core Media Data Handler",
    "-metadata", "com.apple.quicktime.make=Apple",
    "-metadata",
    "com.apple.quicktime.model=iPhone 16 Pro Max",
    "-metadata",
    "com.apple.quicktime.software=iOS 18.3",
    "-metadata",
    `com.apple.quicktime.creationdate=${dateISO}`,
    "-metadata",
    `com.apple.quicktime.location.ISO6709=${location.iso6709}`,
    "-metadata",
    `com.apple.quicktime.location.name=${location.city}, ${location.country}`,
    "-metadata",
    `com.apple.quicktime.content.identifier=${uuid}`,
    "-metadata",
    "com.apple.quicktime.fullframerate=1",
    "-metadata", `creation_time=${dateISO}`,
    "-metadata", `date=${dateStr}`,
    "-metadata", "focal_length=9",
    "-metadata", "aperture=f/2.8",
    "-metadata", "iso=64",
    "-metadata", "exposure_time=1/120",
    "-metadata", "white_balance=Auto",
    "-metadata", "flash=No Flash",
    "-metadata",
    "lens=Apple iPhone 16 Pro Max back camera 9mm f/2.8",
    "-metadata", "lens_make=Apple",
    "-metadata", "lens_serial_number=" + serial,
    "-metadata", `location=${location.iso6709}`,
    "-metadata", `GPS_latitude=${location.lat}`,
    "-metadata",
    `GPS_latitude_ref=${location.latRef}`,
    "-metadata", `GPS_longitude=${location.lon}`,
    "-metadata",
    `GPS_longitude_ref=${location.lonRef}`,
    "-metadata", "GPS_altitude=50",
    "-metadata", "GPS_map_datum=WGS-84",
    "-metadata",
    `GPS_date_stamp=${dateStr.substring(0, 8)}`,
    "-metadata", "media_type=Video",
    "-metadata", `gyroscope_x=${g()}`,
    "-metadata", `gyroscope_y=${g()}`,
    "-metadata", `gyroscope_z=${g()}`,
    "-metadata", `accelerometer_x=${a()}`,
    "-metadata", `accelerometer_y=${a()}`,
    "-metadata",
    `accelerometer_z=${(9.8 + parseFloat(a())).toFixed(6)}`,
    "-metadata", "comment=",
    "-metadata", "artist=",
    "-metadata", "copyright=",
    "-metadata", "description=",
  ];
}

// ═══════════════════════════════════════════════════════════════════════════
// UTILITIES
// ═══════════════════════════════════════════════════════════════════════════

function safeKey(str, maxLen) {
  if (maxLen === undefined) maxLen = 25;
  if (!str) return "empty";
  let hash = 0;
  for (let i = 0; i < str.length; i++) {
    hash = ((hash << 5) - hash) + str.charCodeAt(i);
    hash |= 0;
  }
  const ascii = str.slice(0, 8)
    .replace(/[^a-zA-Z0-9]/g, "_");
  return `${ascii}_${(hash >>> 0).toString(16)}`
    .slice(0, maxLen);
}

const esc = s => (s || "").toString()
  .replace(/&/g,  "&amp;")
  .replace(/</g,  "&lt;")
  .replace(/>/g,  "&gt;")
  .replace(/"/g,  "&quot;")
  .replace(/'/g,  "&#039;");

function normalizeWord(w) {
  return (w || "").toString()
    .replace(/[.,!?؟،;:"'(){}[\]<>«»…]/g, "")
    .trim().toLowerCase();
}

const isArabic = t => /[\u0600-\u06FF]/.test(t);
const isFrench = t =>
  /[àâçéèêëîïôùûüÿœæ]/i.test(t);

function getFontFamily(text) {
  return isArabic(text)
    ? `"Noto Naskh Arabic","Amiri",serif`
    : `"Noto Sans","DejaVu Sans",sans-serif`;
}
function getDir(text) {
  return isArabic(text) ? "rtl" : "ltr";
}
function getLang(text) {
  if (isArabic(text)) return "ar";
  if (isFrench(text)) return "fr";
  return "en";
}

function probeDuration(fp) {
  if (!fp || !existsSync(fp)) return 0;
  const cached = _durationCacheGet(fp);
  if (cached !== undefined) return cached;

  const r = spawnSync("ffprobe", [
    "-v", "error",
    "-show_entries", "format=duration",
    "-of", "default=noprint_wrappers=1:nokey=1",
    fp,
  ], { stdio: ["ignore", "pipe", "pipe"] });

  const d = parseFloat(
    r.stdout.toString().trim()
  ) || 0;
  _durationCacheSet(fp, d);
  return d;
}

function hasAudioStream(fp) {
  if (!fp || !existsSync(fp)) return false;
  const r = spawnSync("ffprobe", [
    "-v", "error",
    "-select_streams", "a",
    "-show_entries", "stream=codec_type",
    "-of", "csv=p=0", fp,
  ], { stdio: ["ignore", "pipe", "pipe"] });
  return r.stdout.toString().trim()
    .includes("audio");
}

function runFFmpeg(args, label) {
  const r = spawnSync("ffmpeg", args, {
    stdio: ["ignore", "pipe", "pipe"],
  });
  if (r.status !== 0) {
    const stderr = r.stderr
      ? r.stderr.toString().slice(-2000)
      : "no stderr";
    console.log(
      `  ⚠️ FFmpeg${label ? ` [${label}]` : ""}` +
      ` failed: ${stderr}`
    );
  }
  return r;
}

function isPowerWord(w) {
  if (!power_words || !power_words.length)
    return false;
  const n = normalizeWord(w);
  if (n.length < 2) return false;
  return power_words.some(pw => {
    const p = normalizeWord(pw);
    return p && (
      n === p ||
      (p.length >= 3 && n.includes(p)) ||
      (n.length >= 3 && p.includes(n))
    );
  });
}

function linkFrame(src, dst) {
  if (!src || !existsSync(src)) return;
  if (existsSync(dst)) return;
  try { symlinkSync(src, dst); }
  catch {
    try { copyFileSync(src, dst); } catch {}
  }
}

function escapePathForConcat(filePath) {
  return filePath.replace(/'/g, "'\\''");
}

function writeConcatFile(listPath, filePaths) {
  const content = filePaths
    .map(f => `file '${escapePathForConcat(f)}'`)
    .join("\n");
  writeFileSync(listPath, content);
}

function applyMetadata(inp, out) {
  if (!inp || !existsSync(inp)) {
    console.log(
      `  ⚠️ applyMetadata: not found: ${inp}`
    );
    return;
  }
  const m = buildiPhoneMetadata();

  const r = runFFmpeg(
    ["-y", "-i", inp, "-c", "copy", ...m, out],
    "metadata-copy"
  );
  if (r.status === 0 && existsSync(out)) {
    console.log(
      `  ✅ Metadata: 📱 iPhone | 📍 ${location.city}`
    );
    return;
  }

  console.log("  ⚠️ Metadata copy failed — re-encode");
  const r2 = runFFmpeg([
    "-y", "-i", inp,
    "-c:v", "libx264", "-preset", "fast", "-crf", "18",
    "-c:a", "aac", "-b:a", "192k",
    "-pix_fmt", "yuv420p",
    ...m, out,
  ], "metadata-reencode");

  if (r2.status === 0 && existsSync(out)) {
    console.log(
      `  ✅ Metadata (re-encoded): 📍 ${location.city}`
    );
    return;
  }

  console.log("  ⚠️ Metadata failed — copy as-is");
  copyFileSync(inp, out);
}

// ═══════════════════════════════════════════════════════════════════════════
// AUDIO & TIMING
// ═══════════════════════════════════════════════════════════════════════════

const realAudioDuration = probeDuration(audio);
const effectiveDuration = realAudioDuration > 1
  ? realAudioDuration : duration_s;
const totalFrames =
  Math.ceil(effectiveDuration * FPS);

console.log(
  `🎵 Audio:${realAudioDuration.toFixed(3)}s | ` +
  `Frames:${totalFrames}`
);

// ═══════════════════════════════════════════════════════════════════════════
// ✅ CINEMATIC FILTERS — bg_style يُستخدم
// ═══════════════════════════════════════════════════════════════════════════

const SLOW_FACTOR = 1.25;

function buildZoomOutFilter(dur, idx) {
  const sz = (1.02 + (idx % 4) * 0.005).toFixed(3);
  return (
    `scale=w=iw*${sz}:h=ih*${sz},` +
    `crop=${WIDTH}:${HEIGHT}`
  );
}

function buildCameraShakeFilter(idx) {
  const f1 = (0.3 + (idx % 3) * 0.1).toFixed(2);
  const f2 = (0.2 + (idx % 2) * 0.15).toFixed(2);
  const ax  = 1 + (idx % 2);
  const ay  = 1 + (idx % 2);
  return (
    `crop=${WIDTH}:${HEIGHT}:` +
    `'max(0,(iw-${WIDTH})/2+${ax}*sin(2*PI*${f1}*t))':` +
    `'max(0,(ih-${HEIGHT})/2+${ay}*sin(2*PI*${f2}*t+1))'`
  );
}

function buildFilmLookFilter() {
  return (
    `curves=` +
    `r='0/0 0.25/0.20 0.5/0.50 0.75/0.80 1/0.94':` +
    `g='0/0 0.25/0.20 0.5/0.48 0.75/0.78 1/0.91':` +
    `b='0/0.04 0.25/0.26 0.5/0.50 0.75/0.80 1/0.97'`
  );
}

function buildColorGrading(isHookClip) {
  return isHookClip
    ? `eq=contrast=1.22:brightness=-0.04:saturation=0.75`
    : `eq=contrast=1.18:brightness=-0.03:saturation=0.80`;
}

function buildVignetteFilter() {
  return `vignette=PI/4:eval=frame`;
}

function buildFilmGrainFilter(idx) {
  return `noise=alls=${4 + (idx % 3)}:allf=t+u`;
}

function buildOriginalityFilter(idx) {
  const h  = idx % 2 === 0 ? 2 : -2;
  const s  = (1.02 + (idx % 3) * 0.01).toFixed(2);
  const sh = (0.22 + (idx % 2) * 0.06).toFixed(2);
  return (
    `hue=h=${h}:s=${s},` +
    `unsharp=3:3:${sh}:3:3:0.0`
  );
}

/**
 * ✅ إصلاح v6.1: bg_style يُستخدم للتأثير المناسب.
 * @param {string} style - "video" | "cinematic" | "blur"
 */
function buildDramaticLightingFilter(style) {
  if (!style) style = bg_style || "video";

  if (style === "cinematic") {
    return (
      `curves=` +
      `r='0/0 0.5/0.48 1/0.92':` +
      `g='0/0 0.5/0.44 1/0.86':` +
      `b='0/0.05 0.5/0.42 1/0.80',` +
      `eq=contrast=1.25:brightness=-0.06:saturation=0.75`
    );
  }
  if (style === "blur") {
    return (
      `curves=` +
      `r='0/0 0.5/0.52 1/0.96':` +
      `g='0/0 0.5/0.48 1/0.90':` +
      `b='0/0.02 0.5/0.46 1/0.85',` +
      `eq=contrast=1.08:brightness=-0.01:saturation=0.90,` +
      `gblur=sigma=0.5`
    );
  }
  // default: "video"
  return (
    `curves=` +
    `r='0/0 0.5/0.52 1/0.96':` +
    `g='0/0 0.5/0.48 1/0.90':` +
    `b='0/0.02 0.5/0.46 1/0.85',` +
    `eq=contrast=1.12:brightness=-0.03:saturation=0.85`
  );
}

// ═══════════════════════════════════════════════════════════════════════════
// normalizeClip
// ═══════════════════════════════════════════════════════════════════════════

/**
 * تحويل أي فيديو إلى WIDTH×HEIGHT, FPS, yuv420p.
 * @param {string} videoPath   - مسار الفيديو المصدر
 * @param {string} outputPath  - مسار الـ output
 * @param {number} durationSec - المدة المطلوبة
 * @param {number} idx         - رقم الـ clip
 * @returns {boolean}
 */
function normalizeClip(
  videoPath, outputPath, durationSec, idx
) {
  const srcDur   = probeDuration(videoPath);
  const buffered = durationSec * NORMALIZE_BUFFER;
  const loopArgs = (
    srcDur > 0 && srcDur < buffered
  ) ? ["-stream_loop", "-1"] : [];

  const r = runFFmpeg([
    "-y", ...loopArgs, "-i", videoPath,
    "-t", buffered.toFixed(3),
    "-vf", [
      `scale=${WIDTH}:${HEIGHT}` +
      `:force_original_aspect_ratio=increase`,
      `crop=${WIDTH}:${HEIGHT}`,
      "setsar=1",
      `fps=${FPS}`,
    ].join(","),
    "-c:v", "libx264", "-preset", "fast",
    "-crf", "22",
    "-pix_fmt", "yuv420p", "-an",
    outputPath,
  ], `normalize[${idx}]`);

  if (r.status !== 0 || !existsSync(outputPath)) {
    runFFmpeg([
      "-y", "-stream_loop", "-1", "-i", videoPath,
      "-t", durationSec.toFixed(3),
      "-vf", [
        `scale=${WIDTH}:${HEIGHT}`,
        "setsar=1", `fps=${FPS}`,
      ].join(","),
      "-c:v", "libx264", "-preset", "fast",
      "-crf", "23",
      "-pix_fmt", "yuv420p", "-an",
      outputPath,
    ], `normalize-fallback[${idx}]`);
  }

  return existsSync(outputPath);
}

// ═══════════════════════════════════════════════════════════════════════════
// processBackground — SHORT
// ═══════════════════════════════════════════════════════════════════════════

function processBackground(
  videoPath, dur, outputFile, idx, isHookClip
) {
  if (isHookClip === undefined) isHookClip = false;

  const d         = Math.max(dur, 0.5);
  const fadeIn    = Math.min(0.15, d * 0.05);
  const fadeOut   = Math.min(0.15, d * 0.05);
  const srcNeeded = d / SLOW_FACTOR;

  const normOut = join(
    TMP, `norm_${String(idx).padStart(3, "0")}.mp4`
  );
  const stage1  = join(
    TMP, `s1_${String(idx).padStart(3, "0")}.mp4`
  );

  try {
    const normalizeOk = normalizeClip(
      videoPath, normOut, srcNeeded, idx
    );

    if (!normalizeOk) {
      console.log(
        `  ⚠️ Normalize fail [${idx}] — using source`
      );
    }

    const effectsInput = (
      normalizeOk && existsSync(normOut)
    ) ? normOut : videoPath;

    const vfParts = [];
    if (!normalizeOk) {
      vfParts.push(
        `scale=${WIDTH}:${HEIGHT}` +
        `:force_original_aspect_ratio=increase`
      );
      vfParts.push(`crop=${WIDTH}:${HEIGHT}`);
      vfParts.push("setsar=1");
    }
    vfParts.push(`setpts=${SLOW_FACTOR}*PTS`);
    vfParts.push(buildZoomOutFilter(d, idx));
    vfParts.push(buildCameraShakeFilter(idx));
    vfParts.push(buildColorGrading(isHookClip));
    vfParts.push(buildFilmLookFilter());
    vfParts.push(buildVignetteFilter());
    vfParts.push(buildFilmGrainFilter(idx));
    vfParts.push(buildOriginalityFilter(idx));
    vfParts.push(
      `fade=t=in:st=0:d=${fadeIn.toFixed(3)}`
    );
    vfParts.push(
      `fade=t=out:st=${(d - fadeOut).toFixed(3)}` +
      `:d=${fadeOut.toFixed(3)}`
    );

    const r = runFFmpeg([
     "-y", "-i", effectsInput,
     "-t", d.toFixed(3),        // ✅ بدل srcNeeded
     "-vf", vfParts.join(","),
     "-r", String(FPS),
     "-c:v", "libx264", "-preset", "fast",
     "-crf", isHookClip ? "16" : "18",
     "-pix_fmt", "yuv420p", "-an",
     stage1,
    ], `effects[${idx}]`);

    if (r.status !== 0 || !existsSync(stage1)) {
      const fallback = existsSync(normOut)
        ? normOut : effectsInput;
      if (existsSync(fallback)) {
        copyFileSync(fallback, stage1);
        console.log(
          `  ⚠️ Effects fail [${idx}] — normalized`
        );
      } else {
        runFFmpeg([
          "-y", "-stream_loop", "-1",
          "-i", videoPath,
          "-t", d.toFixed(3),
          "-vf", [
            `scale=${WIDTH}:${HEIGHT}`,
            "setsar=1", `fps=${FPS}`,
          ].join(","),
          "-c:v", "libx264", "-preset", "fast",
          "-crf", "23",
          "-pix_fmt", "yuv420p", "-an",
          stage1,
        ], `effects-fallback[${idx}]`);
      }
    } else {
      console.log(
        `  ✅ Clip [${idx}] cinematic ready`
      );
    }

    const trimOut = join(
      TMP, `trim_${String(idx).padStart(3, "0")}.mp4`
    );
    runFFmpeg([
      "-y",
      "-i", existsSync(stage1) ? stage1 : normOut,
      "-t", d.toFixed(3),
      "-c", "copy", trimOut,
    ], `trim[${idx}]`);

    // ✅ تمرير bg_style للـ lighting filter
    const srcTrim = existsSync(trimOut)
      ? trimOut : stage1;
    const r2 = runFFmpeg([
      "-y", "-i", srcTrim,
      "-vf", buildDramaticLightingFilter(bg_style),
      "-c:v", "libx264", "-preset", "fast",
      "-crf", isHookClip ? "16" : "18",
      "-pix_fmt", "yuv420p", "-an",
      outputFile,
    ], `lighting[${idx}]`);

    if (r2.status !== 0 || !existsSync(outputFile)) {
      const fallback = existsSync(srcTrim)
        ? srcTrim
        : existsSync(normOut) ? normOut : null;
      if (fallback) copyFileSync(fallback, outputFile);
    }

  } finally {
    const trimOut = join(
      TMP, `trim_${String(idx).padStart(3, "0")}.mp4`
    );
    for (const f of [normOut, stage1, trimOut]) {
      try {
        spawnSync("rm", ["-f", f], { stdio: "ignore" });
      } catch {}
    }
  }

  return outputFile;
}

// ═══════════════════════════════════════════════════════════════════════════
// LONG BG VIDEO
// ═══════════════════════════════════════════════════════════════════════════

function buildLongBgVideo(
  clipPlan, audioPath, outputFile
) {
  console.log(
    `\n🚀 Long BG: ${clipPlan.length} clips`
  );

  if (!audioPath || !existsSync(audioPath)) {
    console.error(`❌ Audio not found: ${audioPath}`);
    process.exit(1);
  }

  const normalizedClips = [];

  for (const clip of clipPlan) {
    const i = clip.index;
    const d = clip.duration;
    const v = clip.videoPath;

    const normOut   = join(
      TMP, `ln_${String(i).padStart(3, "0")}.mp4`
    );
    const srcNeeded = d / SLOW_FACTOR;
    const fadeIn    = Math.min(0.15, d * 0.05);
    const fadeOut   = Math.min(0.15, d * 0.05);

    process.stdout.write(
      `  [${i + 1}/${clipPlan.length}] ` +
      `${d.toFixed(1)}s... `
    );

    const normalizeOk = normalizeClip(
      v, normOut, srcNeeded, i
    );

    if (normalizeOk) {
      const effectsOut = join(
        TMP, `le_${String(i).padStart(3, "0")}.mp4`
      );

      const vf = [
        `setpts=${SLOW_FACTOR}*PTS`,
        buildZoomOutFilter(d, i),
        buildCameraShakeFilter(i),
        buildColorGrading(false),
        buildFilmLookFilter(),
        buildVignetteFilter(),
        buildFilmGrainFilter(i),
        buildOriginalityFilter(i),
        `fade=t=in:st=0:d=${fadeIn.toFixed(3)}`,
        `fade=t=out:st=${(d - fadeOut).toFixed(3)}` +
        `:d=${fadeOut.toFixed(3)}`,
      ].join(",");

      const rEffects = runFFmpeg([
       "-y", "-i", normOut,
       "-t", d.toFixed(3),        // ✅ بدل srcNeeded
       "-vf", vf, "-r", String(FPS),
       "-c:v", "libx264", "-preset", "fast",
       "-crf", "20",
       "-pix_fmt", "yuv420p", "-an",
       effectsOut,
      ], `long-effects[${i}]`);

      const useFile = (
        rEffects.status === 0 &&
        existsSync(effectsOut)
      ) ? effectsOut : normOut;

      const trimOut = join(
        TMP, `lt_${String(i).padStart(3, "0")}.mp4`
      );
      const rTrim = runFFmpeg([
        "-y", "-i", useFile,
        "-t", d.toFixed(3),
        "-c", "copy", trimOut,
      ], `long-trim[${i}]`);

      if (rTrim.status === 0 && existsSync(trimOut)) {
        normalizedClips.push(trimOut);
        try {
          spawnSync(
            "rm", ["-f", normOut, effectsOut],
            { stdio: "ignore" }
          );
        } catch {}
      } else {
        normalizedClips.push(useFile);
      }
      process.stdout.write("✓\n");

    } else {
      console.log(`⚠️ → fallback`);
      const fallOut = join(
        TMP, `lf_${String(i).padStart(3, "0")}.mp4`
      );
      runFFmpeg([
        "-y", "-stream_loop", "-1", "-i", v,
        "-t", d.toFixed(3),
        "-vf", [
          `scale=${WIDTH}:${HEIGHT}` +
          `:force_original_aspect_ratio=increase`,
          `crop=${WIDTH}:${HEIGHT}`,
          "setsar=1", `fps=${FPS}`,
        ].join(","),
        "-r", String(FPS),
        "-c:v", "libx264", "-preset", "fast",
        "-crf", "23",
        "-pix_fmt", "yuv420p", "-an",
        fallOut,
      ], `long-fallback[${i}]`);

      if (existsSync(fallOut)) {
        normalizedClips.push(fallOut);
      } else if (normalizedClips.length > 0) {
        // ✅ نسخ بدل إعادة استخدام المسار
        const lastClip =
          normalizedClips[normalizedClips.length - 1];
        const copyOut = join(
          TMP,
          `lc_${String(i).padStart(3, "0")}.mp4`
        );
        try {
          copyFileSync(lastClip, copyOut);
          normalizedClips.push(copyOut);
          console.log(
            `  ⚠️ Complete fail [${i}] — copied last`
          );
        } catch (e) {
          console.log(
            `  ⚠️ Copy failed [${i}]: ${e.message}`
          );
          normalizedClips.push(lastClip);
        }
      }
    }
  }

  if (normalizedClips.length === 0) {
    console.error("❌ No clips to concat");
    process.exit(1);
  }

  console.log(
    `\n✨ Concat: ${normalizedClips.length} clips...`
  );

  if (normalizedClips.length > 30) {
    const listFile  = join(TMP, "long_list.txt");
    const concatRaw = join(TMP, "long_raw.mp4");
    writeConcatFile(listFile, normalizedClips);

    const rConcat = runFFmpeg([
      "-y", "-f", "concat", "-safe", "0",
      "-i", listFile,
      "-c:v", "libx264", "-preset", "fast",
      "-crf", "19",
      "-pix_fmt", "yuv420p", "-an",
      concatRaw,
    ], "long-concat");

    if (
      rConcat.status !== 0 || !existsSync(concatRaw)
    ) {
      console.log("  ⚠️ Concat failed — first clip");
      copyFileSync(normalizedClips[0], concatRaw);
    }

    return _finalizeLongVideo(
      concatRaw, audioPath, outputFile
    );
  }

  // xfade
  try {
    const inputs  = normalizedClips
      .flatMap(f => ["-i", f]);
    let current   = "[0:v]";
    const filters = [];
    let cumOff    = 0;
    let lastTrans = null;

    for (
      let i = 0;
      i < normalizedClips.length - 1;
      i++
    ) {
      const tag = (aligned && i < aligned.length)
        ? (aligned[i].tag || "default")
        : "default";
      const xft = pickTransition(lastTrans, tag);
      lastTrans = xft;

      const dur = clipPlan[i]
        ? clipPlan[i].duration : 3.0;

      // ✅ تراكم صحيح
      cumOff += Math.max(0.001, dur - XFADE_DUR);

      const outLbl =
        i === normalizedClips.length - 2
          ? "[vfinal]"
          : `[v${i + 1}]`;

      filters.push(
        `${current}[${i + 1}:v]xfade=` +
        `transition=${xft}:` +
        `duration=${XFADE_DUR.toFixed(3)}:` +
        `offset=${cumOff.toFixed(3)}${outLbl}`
      );
      current = outLbl;
    }

    const concatXfade = join(TMP, "long_xfade.mp4");
    const rXfade      = runFFmpeg([
      "-y", ...inputs,
      "-filter_complex", filters.join(";"),
      "-map", "[vfinal]",
      "-c:v", "libx264", "-preset", "fast",
      "-crf", "19",
      "-pix_fmt", "yuv420p", "-an",
      concatXfade,
    ], "long-xfade");

    if (
      rXfade.status === 0 && existsSync(concatXfade)
    ) {
      console.log("  ✅ xfade done");
      return _finalizeLongVideo(
        concatXfade, audioPath, outputFile
      );
    }
    throw new Error("xfade failed");

  } catch (err) {
    console.log(
      `  ⚠️ xfade error: ${err.message} — fallback`
    );
    const listFile  = join(TMP, "long_list_fb.txt");
    const concatRaw = join(TMP, "long_raw_fb.mp4");
    writeConcatFile(listFile, normalizedClips);
    runFFmpeg([
      "-y", "-f", "concat", "-safe", "0",
      "-i", listFile,
      "-c:v", "libx264", "-preset", "fast",
      "-crf", "19",
      "-pix_fmt", "yuv420p", "-an",
      concatRaw,
    ], "long-concat-fallback");
    return _finalizeLongVideo(
      concatRaw, audioPath, outputFile
    );
  }
}

/**
 * ✅ إصلاح v6.1:
 * إذا video أقصر من audio → stream_loop + re-encode
 * بدون -shortest
 */
function _finalizeLongVideo(
  videoSource, audioPath, outputFile
) {
  // تطبيق lighting
  const litOut = join(TMP, "long_lit.mp4");
  const rLit   = runFFmpeg([
    "-y", "-i", videoSource,
    "-vf", buildDramaticLightingFilter(bg_style),
    "-c:v", "libx264", "-preset", "fast",
    "-crf", "19",
    "-pix_fmt", "yuv420p", "-an",
    litOut,
  ], "long-lighting");

  const videoFinal = (
    rLit.status === 0 && existsSync(litOut)
  ) ? litOut : videoSource;

  console.log(
    rLit.status === 0
      ? "  ✅ Lighting applied"
      : "  ⚠️ Lighting skipped"
  );

  const ad = probeDuration(audioPath);
  const vd = probeDuration(videoFinal);
  console.log(
    `🎵 Audio:${ad.toFixed(1)}s | ` +
    `Video:${vd.toFixed(1)}s`
  );

  const tmpMerge = join(TMP, "long_merged.mp4");
  const needsLoop = vd < ad - 0.3;

  if (needsLoop) {
    // ✅ إصلاح v6.1: loop + re-encode
    const rMerge = runFFmpeg([
      "-y",
      "-stream_loop", "-1", "-i", videoFinal,
      "-i", audioPath,
      "-map", "0:v:0", "-map", "1:a:0",
      "-c:v", "libx264", "-preset", "fast",
      "-crf", "21",
      "-c:a", "aac", "-b:a", "192k",
      "-pix_fmt", "yuv420p",
      "-t", Math.max(ad, 1).toFixed(3),
      tmpMerge,
    ], "long-merge-loop");

    if (rMerge.status !== 0) {
      console.log("  ⚠️ Merge loop failed — copy");
      copyFileSync(videoFinal, tmpMerge);
    }
  } else {
    // ✅ video كافٍ — copy مباشر
    const rMerge = runFFmpeg([
      "-y",
      "-i", videoFinal,
      "-i", audioPath,
      "-map", "0:v:0", "-map", "1:a:0",
      "-c:v", "copy",
      "-c:a", "aac", "-b:a", "192k",
      "-t", Math.max(ad, 1).toFixed(3),
      tmpMerge,
    ], "long-merge");

    if (rMerge.status !== 0) {
      console.log("  ⚠️ Merge failed — video only");
      copyFileSync(videoFinal, tmpMerge);
    }
  }

  applyMetadata(tmpMerge, outputFile);

  try {
    spawnSync(
      "rm", ["-f", litOut, tmpMerge],
      { stdio: "ignore" }
    );
  } catch {}

  console.log(`\n✅ Long BG done → ${outputFile}`);
  return outputFile;
}

// ═══════════════════════════════════════════════════════════════════════════
// ASS SUBTITLES
// ═══════════════════════════════════════════════════════════════════════════

const TAG_ASS_COLORS = {
  shock:"&H00FFFFFF",        urgency:"&H000022FF",
  intrigue:"&H0000D7FF",     emotional:"&H00AB8FFF",
  confident:"&H00FFFFFF",    inspiration:"&H0000D7FF",
  wisdom:"&H00FFB182",       desire:"&H0047B3FF",
  calm:"&H00EADE80",         information:"&H00FFFFFF",
  pause:"&H00C5BEB0",        whisper:"&H00D893CE",
  curiosity:"&H0076F1FF",    storytelling:"&H0080CCFF",
  dramatic:"&H009A9AEF",     revelation:"&H00C8C8FF",
  tension:"&H004370FF",      climax:"&H00FFFFFF",
  powerful:"&H00F1ECEC",     default:"&H00FFFFFF",
};

function secondsToAssTime(s) {
  const h   = Math.floor(s / 3600);
  const m   = Math.floor((s % 3600) / 60);
  const sec = Math.floor(s % 60);
  const cs  = Math.floor((s % 1) * 100);
  return (
    `${h}:${String(m).padStart(2, "0")}:` +
    `${String(sec).padStart(2, "0")}.` +
    `${String(cs).padStart(2, "0")}`
  );
}

function escapeAssText(text) {
  return (text || "").toString()
    .replace(/\\/g,    "\\\\")
    .replace(/\{/g,    "\\{")
    .replace(/\}/g,    "\\}")
    .replace(/\r?\n/g, "\\N")
    .replace(/;/g,     "\\;");
}

function buildAssFile() {
  const isAr     = isArabic(display_title);
  const fontName = isAr
    ? "Noto Naskh Arabic" : "Noto Sans";

  const wordSize  = (
    content_mode === "long" && platform === "yt"
  ) ? 70 : 85;
  const titleSize = (
    content_mode === "long" && platform === "yt"
  ) ? 36 : 46;

  const wordMarginV  = Math.floor(HEIGHT * 0.40);
  const titleMarginV = (
    content_mode === "long" && platform === "yt"
  ) ? 30 : 400;

  const header = [
    "[Script Info]",
    "ScriptType: v4.00+",
    `PlayResX: ${WIDTH}`,
    `PlayResY: ${HEIGHT}`,
    "WrapStyle: 1",
    "ScaledBorderAndShadow: yes",
    "Collisions: Normal",
    "",
    "[V4+ Styles]",
    "Format: Name, Fontname, Fontsize, " +
    "PrimaryColour, SecondaryColour, " +
    "OutlineColour, BackColour, Bold, Italic, " +
    "Underline, StrikeOut, ScaleX, ScaleY, " +
    "Spacing, Angle, BorderStyle, Outline, " +
    "Shadow, Alignment, MarginL, MarginR, " +
    "MarginV, Encoding",

    `Style: Word,${fontName},${wordSize},` +
    `&H00FFFFFF,&H000000FF,&H00000000,` +
    `&HB4000000,-1,0,0,0,100,100,0,0,1,4,2,` +
    `2,80,80,${wordMarginV},1`,

    `Style: Title,${fontName},${titleSize},` +
    `&H00FFFFFF,&H000000FF,&H00000000,` +
    `&H80000000,-1,0,0,0,100,100,0,0,1,2,1,` +
    `8,40,40,${titleMarginV},1`,

    "",
    "[Events]",
    "Format: Layer, Start, End, Style, Name, " +
    "MarginL, MarginR, MarginV, Effect, Text",
  ].join("\r\n");

  const events = [];

  events.push(
    `Dialogue: 0,${secondsToAssTime(0)},` +
    `${secondsToAssTime(effectiveDuration)},` +
    `Title,,0,0,0,,` +
    escapeAssText(
      `${emoji_left} ${display_title} ${emoji_right}`
    )
  );

  if (aligned && aligned.length > 0) {
    let wordCount = 0;
    for (const seg of aligned) {
      if (!seg.words || !seg.words.length) continue;
      const tag      = seg.tag || "information";
      const assColor =
        TAG_ASS_COLORS[tag] || TAG_ASS_COLORS.default;

      for (const w of seg.words) {
        if (!w.word || !w.word.trim()) continue;
        const s = parseFloat(w.start);
        const e = parseFloat(w.end);
        if (
          isNaN(s) || isNaN(e) ||
          s < 0 || e <= s
        ) continue;

        const isPower = isPowerWord(w.word);
        const color   = isPower
          ? "&H0000D7FF" : assColor;
        const styleOverride = isPower
          ? `{\\c${color}\\b1\\fscx115\\fscy115}`
          : `{\\c${color}}`;

        events.push(
          `Dialogue: 1,${secondsToAssTime(s)},` +
          `${secondsToAssTime(e)},` +
          `Word,,0,0,0,,` +
          `${styleOverride}` +
          `${escapeAssText(w.word.trim())}`
        );
        wordCount++;
      }
    }
    console.log(`  ✅ ASS: ${wordCount} words`);

  } else if (sentences.length > 0) {
    console.log("  ℹ️ ASS: fallback sentences");
    const perSent = effectiveDuration /
      Math.max(sentences.length, 1);
    sentences.forEach((sent, si) => {
      const sentWords = sent.split(/\s+/)
        .filter(Boolean);
      const sentStart = si * perSent;
      const perWord   = perSent /
        Math.max(sentWords.length, 1);
      sentWords.forEach((w, wi) => {
        const s = sentStart + wi * perWord;
        events.push(
          `Dialogue: 1,${secondsToAssTime(s)},` +
          `${secondsToAssTime(s + perWord)},` +
          `Word,,0,0,0,,${escapeAssText(w)}`
        );
      });
    });
  }

  return header + "\r\n" +
    events.join("\r\n") + "\r\n";
}

// ═══════════════════════════════════════════════════════════════════════════
// LONG WORDS OVERLAY
// ═══════════════════════════════════════════════════════════════════════════

function buildLongWordsOverlay(
  bgVideoPath, audioPath, outputFile
) {
  if (!audioPath || !existsSync(audioPath)) {
    console.error(`❌ Audio not found: ${audioPath}`);
    process.exit(1);
  }

  console.log("\n📝 Long Words: ASS overlay...");

  const assContent = buildAssFile();
  const assFile    = join(TMP, "long_subs.ass");
  writeFileSync(assFile, assContent, "utf-8");
  console.log(
    `  ✅ ASS: ${assContent.length} bytes`
  );

  const fontsDirs = [
    "/usr/share/fonts/truetype",
    "/usr/share/fonts",
    "/usr/local/share/fonts",
  ];
  const fontsDir =
    fontsDirs.find(d => existsSync(d)) ||
    "/usr/share/fonts";

  const assEscaped = assFile
    .replace(/\\/g, "/")
    .replace(/:/g,  "\\:")
    .replace(/ /g,  "\\ ");

  const assFilter =
    `ass='${assEscaped}':fontsdir='${fontsDir}'`;
  const bgHasAudio = hasAudioStream(bgVideoPath);
  const tmpOut     = join(TMP, "long_words_tmp.mp4");

  const ffArgs = bgHasAudio
    ? [
        "-y", "-i", bgVideoPath,
        "-vf", assFilter,
        "-map", "0:v:0", "-map", "0:a:0",
        "-c:v", "libx264", "-preset", "fast",
        "-crf", "18",
        "-c:a", "aac", "-b:a", "192k",
        "-pix_fmt", "yuv420p",
        tmpOut,
      ]
    : [
        "-y",
        "-i", bgVideoPath, "-i", audioPath,
        "-vf", assFilter,
        "-map", "0:v:0", "-map", "1:a:0",
        "-c:v", "libx264", "-preset", "fast",
        "-crf", "18",
        "-c:a", "aac", "-b:a", "192k",
        "-pix_fmt", "yuv420p",
        tmpOut,
      ];

  const r = runFFmpeg(ffArgs, "ass-overlay");

  if (r.status !== 0) {
    console.log("  ⚠️ ASS failed — bg as-is");
    if (bgHasAudio) {
      copyFileSync(bgVideoPath, tmpOut);
    } else {
      runFFmpeg([
        "-y",
        "-i", bgVideoPath, "-i", audioPath,
        "-map", "0:v:0", "-map", "1:a:0",
        "-c:v", "copy",
        "-c:a", "aac", "-b:a", "192k",
        tmpOut,
      ], "ass-fallback");
    }
  } else {
    console.log("  ✅ Words overlay done");
  }

  applyMetadata(tmpOut, outputFile);

  try {
    spawnSync(
      "rm", ["-f", tmpOut, assFile],
      { stdio: "ignore" }
    );
  } catch {}

  console.log(`✅ Long words → ${outputFile}`);
  return outputFile;
}

// ═══════════════════════════════════════════════════════════════════════════
// SHORT: SECTION DETECTION
// ═══════════════════════════════════════════════════════════════════════════

const CTA_TAGS = [
  "confident", "inspiration", "powerful"
];

function detectVideoSections() {
  if (!aligned || aligned.length === 0) {
    const totalClips = (
      clip_durations && clip_durations.length > 0
    )
      ? clip_durations.length
      : Math.max(
          1,
          Math.ceil(effectiveDuration / clip_duration)
        );
    return Array.from(
      { length: totalClips },
      (_, i) => ({
        type: i === 0 ? "hook"
          : i === totalClips - 1 ? "cta"
          : "body",
        start: 0, end: 0, idx: i,
        tag: i === 0 ? "intrigue"
          : i === totalClips - 1 ? "confident"
          : "information",
      })
    );
  }
  const total = aligned.length;
  return aligned.map((seg, i) => {
    const tag = seg.tag || "information";
    let sectionType;
    if (i === 0) {
      sectionType = "hook";
    } else if (i === total - 1) {
      sectionType = "cta";
    } else if (
      i === total - 2 && CTA_TAGS.includes(tag)
    ) {
      sectionType = "cta";
    } else {
      sectionType = "body";
    }
    return {
      type:  sectionType,
      start: parseFloat(seg.start || 0),
      end:   parseFloat(seg.end   || 0),
      idx:   i, tag,
    };
  });
}

// ═══════════════════════════════════════════════════════════════════════════
// SHORT: CLIP PLAN
// ═══════════════════════════════════════════════════════════════════════════

function buildClipPlan() {
  if (!videos || videos.length === 0) {
    console.error("❌ No videos in manifest");
    process.exit(1);
  }

  const validVideos = videos.filter(
    v => v && existsSync(v)
  );
  if (validVideos.length === 0) {
    console.error("❌ All video files missing");
    process.exit(1);
  }

  const sections = detectVideoSections();

  let durations;
  if (clip_durations && clip_durations.length > 0) {
    durations = clip_durations.map(
      d => Math.max(d, 0.5)
    );
  } else {
    const n = Math.max(
      1,
      Math.floor(effectiveDuration / clip_duration)
    );
    durations = Array.from(
      { length: n },
      () => effectiveDuration / n
    );
  }

  const count = durations.length;
  const syncedSections = Array.from(
    { length: count },
    (_, i) => sections[i] || {
      type: i === 0 ? "hook"
        : i === count - 1 ? "cta" : "body",
      tag: i === 0 ? "intrigue"
        : i === count - 1 ? "confident"
        : "information",
      start: 0, end: 0, idx: i,
    }
  );

  let offset    = 0;
  let lastTrans = null;

  const plan = durations.map((d, i) => {
    const sec     = syncedSections[i];
    const nextSec = syncedSections[i + 1] || null;
    let trans     = null;

    if (nextSec) {
      const transType = pickTransition(
        lastTrans, sec.tag
      );
      lastTrans     = transType;
      const isMajor = sec.type !== nextSec.type;
      trans = {
        level:    isMajor ? "major" : "minor",
        type:     transType,
        duration: isMajor
          ? XFADE_DUR : XFADE_DUR * 0.56,
      };
    }

    const entry = {
      index:      i,
      start:      parseFloat(offset.toFixed(3)),
      duration:   parseFloat(d.toFixed(3)),
      videoPath:  validVideos[i % validVideos.length],
      isHook:     i === 0 && has_hook && isShort,
      section:    sec,
      transition: trans,
    };
    offset += d;
    return entry;
  });

  console.log(
    `\n📋 Clip plan: ${plan.length} clips ` +
    `[${content_mode.toUpperCase()}]`
  );
  plan.slice(0, 5).forEach(c => {
    console.log(
      `   [${c.index + 1}] ${c.start.toFixed(2)}s ` +
      `(${c.duration.toFixed(2)}s) ` +
      `[${c.section.type}]` +
      `${c.transition ? ` → [${c.transition.type}]` : ""}`
    );
  });
  if (plan.length > 5) {
    console.log(
      `   ... and ${plan.length - 5} more`
    );
  }

  return plan;
}

// ═══════════════════════════════════════════════════════════════════════════
// SHORT: MERGE AUDIO
// ═══════════════════════════════════════════════════════════════════════════

function mergeAudio(
  videoPath, audioPath, outputFile
) {
  if (!audioPath || !existsSync(audioPath)) {
    console.log("  ⚠️ Audio not found — no audio");
    copyFileSync(videoPath, outputFile);
    return;
  }

  const ad = probeDuration(audioPath);
  const vd = probeDuration(videoPath);
  console.log(
    `🎵 Audio:${ad.toFixed(3)}s | ` +
    `Video:${vd.toFixed(3)}s`
  );

  let v = videoPath;
  if (vd < ad - 0.3) {
    const lp = join(TMP, "looped.mp4");
    const r  = runFFmpeg([
      "-y", "-stream_loop", "-1", "-i", videoPath,
      "-t", ad.toFixed(3),
      "-c:v", "libx264", "-preset", "fast",
      "-crf", "21", "-pix_fmt", "yuv420p", "-an",
      lp,
    ], "loop-video");
    if (r.status === 0 && existsSync(lp)) v = lp;
  }

  const tmp = join(TMP, "merged_temp.mp4");
  // ✅ بدون -shortest
  const r   = runFFmpeg([
    "-y", "-i", v, "-i", audioPath,
    "-map", "0:v:0", "-map", "1:a:0",
    "-c:v", "copy",
    "-c:a", "aac", "-b:a", "192k",
    "-t", Math.max(ad, 1).toFixed(3),
    tmp,
  ], "merge-audio");

  if (r.status !== 0) {
    console.log("  ⚠️ Merge failed — video only");
    copyFileSync(v, tmp);
  }

  applyMetadata(tmp, outputFile);
  try {
    spawnSync("rm", ["-f", tmp], { stdio: "ignore" });
  } catch {}
  console.log(`✅ Done → ${outputFile}`);
}

// ═══════════════════════════════════════════════════════════════════════════
// SHORT: CONCAT WITH TRANSITIONS
// ═══════════════════════════════════════════════════════════════════════════

function concatClipsWithTransitions(
  processedClips, clipPlan
) {
  if (processedClips.length === 0) return null;
  if (processedClips.length === 1) {
    return processedClips[0];
  }

  console.log(
    `\n✨ Merge: ${processedClips.length} clips...`
  );

  const groups = [];
  let grpBuf   = [{
    clip: processedClips[0],
    dur:  clipPlan[0].duration,
    tag:  clipPlan[0].section?.tag || "default",
  }];

  for (
    let i = 0;
    i < processedClips.length - 1;
    i++
  ) {
    const trans = clipPlan[i].transition;
    if (trans && trans.level === "major") {
      groups.push({ clips: grpBuf, nextTrans: trans });
      grpBuf = [{
        clip: processedClips[i + 1],
        dur:  clipPlan[i + 1].duration,
        tag:  clipPlan[i + 1].section?.tag ||
              "default",
      }];
    } else {
      grpBuf.push({
        clip: processedClips[i + 1],
        dur:  clipPlan[i + 1].duration,
        tag:  clipPlan[i + 1].section?.tag ||
              "default",
      });
    }
  }
  groups.push({ clips: grpBuf, nextTrans: null });

  const groupOutputs = [];
  let gIdx           = 0;

  for (const group of groups) {
    const clips = group.clips;

    if (clips.length === 1) {
      groupOutputs.push({
        file:  clips[0].clip,
        dur:   clips[0].dur,
        trans: group.nextTrans,
      });
      continue;
    }

    const X       = XFADE_DUR * 0.56;
    const fl      = [];
    // ✅ cumOff يبدأ من 0 في كل group
    let cumOff    = 0;
    let lbl       = "[0:v]";
    let lastTrans = null;

    for (let i = 1; i < clips.length; i++) {
      const prevDur = Math.max(
        clips[i - 1].dur, X + 0.05
      );
      // ✅ تراكم صحيح
      cumOff += Math.max(0.001, prevDur - X);

      const xft = pickTransition(
        lastTrans, clips[i - 1].tag
      );
      lastTrans = xft;

      const out = i === clips.length - 1
        ? "[vout]" : `[v${i}]`;
      fl.push(
        `${lbl}[${i}:v]xfade=transition=${xft}:` +
        `duration=${X.toFixed(3)}:` +
        `offset=${cumOff.toFixed(3)}${out}`
      );
      lbl = out;
    }

    const groupOut = join(TMP, `grp_${gIdx}.mp4`);
    const totDur   =
      clips.reduce((s, c) => s + c.dur, 0) -
      X * (clips.length - 1);

    const r = runFFmpeg([
      "-y",
      ...clips.flatMap(c => ["-i", c.clip]),
      "-filter_complex", fl.join(";"),
      "-map", "[vout]",
      "-c:v", "libx264", "-preset", "fast",
      "-crf", "18",
      "-pix_fmt", "yuv420p", "-an",
      groupOut,
    ], `group-merge[${gIdx}]`);

    if (r.status !== 0 || !existsSync(groupOut)) {
      const ls = join(TMP, `gls_${gIdx}.txt`);
      writeConcatFile(ls, clips.map(c => c.clip));
      spawnSync("ffmpeg", [
        "-y", "-f", "concat", "-safe", "0",
        "-i", ls, "-c", "copy", groupOut,
      ], { stdio: "ignore" });
    } else {
      console.log(
        `  ✨ Group[${groupOutputs.length + 1}]: ` +
        `${clips.length} clips`
      );
    }

    groupOutputs.push({
      file:  groupOut,
      dur:   Math.max(totDur, 0.5),
      trans: group.nextTrans,
    });
    gIdx++;
  }

  if (groupOutputs.length === 1) {
    return groupOutputs[0].file;
  }

  let mergedFile = groupOutputs[0].file;
  let mergedDur  = groupOutputs[0].dur;

  for (let i = 1; i < groupOutputs.length; i++) {
    const trans    = groupOutputs[i - 1].trans;
    const nextFile = groupOutputs[i].file;
    const majorOut = join(TMP, `maj_${i}.mp4`);

    const transType = (trans && trans.type)
      || pickTransition(null, "default");
    const transDur  = (trans && trans.duration)
      || XFADE_DUR;
    const offset    = Math.max(
      0.1, mergedDur - transDur
    );

    const fc = (
      `[0:v]format=yuv420p[v0];` +
      `[1:v]format=yuv420p[v1];` +
      `[v0][v1]xfade=transition=${transType}:` +
      `duration=${transDur.toFixed(3)}:` +
      `offset=${offset.toFixed(3)}[out]`
    );

    const r = runFFmpeg([
      "-y", "-i", mergedFile, "-i", nextFile,
      "-filter_complex", fc,
      "-map", "[out]",
      "-c:v", "libx264", "-preset", "fast",
      "-crf", "18",
      "-pix_fmt", "yuv420p", "-an",
      majorOut,
    ], `major-trans[${i}]`);

    if (r.status !== 0 || !existsSync(majorOut)) {
      const ls = join(TMP, `maj_ls_${i}.txt`);
      writeConcatFile(ls, [mergedFile, nextFile]);
      spawnSync("ffmpeg", [
        "-y", "-f", "concat", "-safe", "0",
        "-i", ls, "-c", "copy", majorOut,
      ], { stdio: "ignore" });
    } else {
      console.log(`  💥 Major [${transType}]`);
    }

    if (existsSync(majorOut)) {
      mergedFile = majorOut;
      mergedDur  = probeDuration(majorOut);
    } else {
      mergedFile = nextFile;
      mergedDur  = groupOutputs[i].dur;
    }
  }

  return mergedFile;
}

// ═══════════════════════════════════════════════════════════════════════════
// SHORT: OVERLAY ON BG
// ═══════════════════════════════════════════════════════════════════════════

function overlayOnBg(
  bgVideo, captionMov, audioPath, outputFile
) {
  const fc = (
    "[1:v]format=rgba[cap];" +
    "[0:v][cap]overlay=0:0:format=auto," +
    "format=yuv420p[out]"
  );
  const bgHasAudio = hasAudioStream(bgVideo);

  const r = runFFmpeg(
    bgHasAudio
      ? [
          "-y",
          "-i", bgVideo, "-i", captionMov,
          "-filter_complex", fc,
          "-map", "[out]", "-map", "0:a:0",
          "-c:v", "libx264", "-preset", "fast",
          "-crf", "19",
          "-c:a", "aac", "-b:a", "192k",
          "-pix_fmt", "yuv420p",
          outputFile,
        ]
      : [
          "-y",
          "-i", bgVideo,
          "-i", captionMov,
          "-i", audioPath,
          "-filter_complex", fc,
          "-map", "[out]", "-map", "2:a:0",
          "-c:v", "libx264", "-preset", "fast",
          "-crf", "19",
          "-c:a", "aac", "-b:a", "192k",
          "-pix_fmt", "yuv420p",
          outputFile,
        ],
    "overlay"
  );

  if (r.status !== 0) {
    console.log("  ⚠️ Overlay failed — bg video");
    copyFileSync(bgVideo, outputFile);
  }
  return outputFile;
}

function framesToMov(frameDir, outputMov) {
  runFFmpeg([
    "-y",
    "-framerate", String(FPS),
    "-i", `${frameDir}/frame_%06d.png`,
    "-vf", `scale=${WIDTH}:${HEIGHT},format=rgba`,
    "-c:v", "png", "-an",
    outputMov,
  ], "frames-to-mov");
  return outputMov;
}

// ═══════════════════════════════════════════════════════════════════════════
// SHORT: EMOTION & WORD STYLES
// ═══════════════════════════════════════════════════════════════════════════

const EMOTION_COLORS = {
  curiosity: {
    word:"#FFD700", glow:"rgba(255,215,0,0.5)",
    power:"#FF1744",
  },
  fear: {
    word:"#FF4444", glow:"rgba(255,68,68,0.5)",
    power:"#FFD700",
  },
  hope: {
    word:"#00E676", glow:"rgba(0,230,118,0.5)",
    power:"#FFFFFF",
  },
  joy: {
    word:"#FF9100", glow:"rgba(255,145,0,0.5)",
    power:"#FFFFFF",
  },
  awe: {
    word:"#E040FB", glow:"rgba(224,64,251,0.5)",
    power:"#FFD700",
  },
  surprise: {
    word:"#40C4FF", glow:"rgba(64,196,255,0.5)",
    power:"#FFD700",
  },
  desire: {
    word:"#FF1744", glow:"rgba(255,23,68,0.5)",
    power:"#FFD700",
  },
  anger: {
    word:"#FF1744", glow:"rgba(255,23,68,0.5)",
    power:"#FFD700",
  },
  sadness: {
    word:"#82B1FF", glow:"rgba(130,177,255,0.5)",
    power:"#FFFFFF",
  },
  default: {
    word:"#FFFFFF", glow:"rgba(255,255,255,0.4)",
    power:"#FF1744",
  },
};

const emotion = (
  analysis.primary_emotion || ""
).toLowerCase();
const COLORS  =
  EMOTION_COLORS[emotion] || EMOTION_COLORS.default;

const TAG_WORD_STYLES = {
  shock: {
    colorWord:"#FFFFFF",
    colorGlow:"rgba(255,50,50,0.9)",
    scaleMult:1.30, glowSpread:80,
    strokeColor:"rgba(255,0,0,0.8)",
    strokeWidth:5, brightness:1.4,
  },
  urgency: {
    colorWord:"#FF2200",
    colorGlow:"rgba(255,34,0,0.8)",
    scaleMult:1.20, glowSpread:60,
    strokeColor:"rgba(0,0,0,0.9)",
    strokeWidth:4, brightness:1.3,
  },
  intrigue: {
    colorWord:"#FFD700",
    colorGlow:"rgba(255,215,0,0.7)",
    scaleMult:1.0, glowSpread:50,
    strokeColor:"rgba(0,0,0,0.95)",
    strokeWidth:4, brightness:1.0,
  },
  emotional: {
    colorWord:"#FF8FAB",
    colorGlow:"rgba(255,143,171,0.7)",
    scaleMult:0.95, glowSpread:45,
    strokeColor:"rgba(0,0,0,0.9)",
    strokeWidth:4, brightness:1.0,
  },
  confident: {
    colorWord:"#FFFFFF",
    colorGlow:"rgba(255,255,255,0.6)",
    scaleMult:1.10, glowSpread:40,
    strokeColor:"rgba(0,0,0,0.95)",
    strokeWidth:5, brightness:1.2,
  },
  inspiration: {
    colorWord:"#FFD700",
    colorGlow:"rgba(255,215,0,0.8)",
    scaleMult:1.15, glowSpread:70,
    strokeColor:"rgba(0,0,0,0.9)",
    strokeWidth:4, brightness:1.3,
  },
  wisdom: {
    colorWord:"#82B1FF",
    colorGlow:"rgba(130,177,255,0.6)",
    scaleMult:0.90, glowSpread:35,
    strokeColor:"rgba(0,0,0,0.9)",
    strokeWidth:3, brightness:0.95,
  },
  desire: {
    colorWord:"#FFB347",
    colorGlow:"rgba(255,179,71,0.7)",
    scaleMult:1.0, glowSpread:45,
    strokeColor:"rgba(0,0,0,0.9)",
    strokeWidth:4, brightness:1.1,
  },
  calm: {
    colorWord:"#80DEEA",
    colorGlow:"rgba(128,222,234,0.5)",
    scaleMult:0.85, glowSpread:30,
    strokeColor:"rgba(0,0,0,0.85)",
    strokeWidth:3, brightness:0.9,
  },
  information: {
    colorWord:"#FFFFFF",
    colorGlow:"rgba(255,255,255,0.35)",
    scaleMult:1.0, glowSpread:30,
    strokeColor:"rgba(0,0,0,0.95)",
    strokeWidth:4, brightness:1.0,
  },
  pause: {
    colorWord:"#B0BEC5",
    colorGlow:"rgba(176,190,197,0.4)",
    scaleMult:0.80, glowSpread:25,
    strokeColor:"rgba(0,0,0,0.8)",
    strokeWidth:2, brightness:0.85,
  },
  whisper: {
    colorWord:"#CE93D8",
    colorGlow:"rgba(206,147,216,0.6)",
    scaleMult:0.88, glowSpread:35,
    strokeColor:"rgba(0,0,0,0.9)",
    strokeWidth:3, brightness:0.9,
  },
  curiosity: {
    colorWord:"#FFF176",
    colorGlow:"rgba(255,241,118,0.6)",
    scaleMult:1.02, glowSpread:45,
    strokeColor:"rgba(0,0,0,0.9)",
    strokeWidth:4, brightness:1.05,
  },
  storytelling: {
    colorWord:"#FFCC80",
    colorGlow:"rgba(255,204,128,0.5)",
    scaleMult:0.95, glowSpread:35,
    strokeColor:"rgba(0,0,0,0.9)",
    strokeWidth:3, brightness:1.0,
  },
  dramatic: {
    colorWord:"#EF9A9A",
    colorGlow:"rgba(239,154,154,0.7)",
    scaleMult:1.12, glowSpread:55,
    strokeColor:"rgba(100,0,0,0.8)",
    strokeWidth:4, brightness:1.15,
  },
  revelation: {
    colorWord:"#FFFFFF",
    colorGlow:"rgba(255,255,200,0.9)",
    scaleMult:1.25, glowSpread:75,
    strokeColor:"rgba(200,150,0,0.8)",
    strokeWidth:5, brightness:1.45,
  },
  tension: {
    colorWord:"#FF7043",
    colorGlow:"rgba(255,112,67,0.75)",
    scaleMult:1.15, glowSpread:55,
    strokeColor:"rgba(0,0,0,0.9)",
    strokeWidth:4, brightness:1.25,
  },
  climax: {
    colorWord:"#FFFFFF",
    colorGlow:"rgba(255,100,50,0.95)",
    scaleMult:1.35, glowSpread:90,
    strokeColor:"rgba(255,50,0,0.9)",
    strokeWidth:6, brightness:1.5,
  },
  powerful: {
    colorWord:"#ECEFF1",
    colorGlow:"rgba(236,239,241,0.65)",
    scaleMult:1.12, glowSpread:45,
    strokeColor:"rgba(0,0,0,0.95)",
    strokeWidth:5, brightness:1.2,
  },
};

const DEFAULT_WORD_STYLE =
  TAG_WORD_STYLES.information;
const POWER_STYLE = {
  colorWord:   COLORS.power,
  colorGlow:   "rgba(255,23,68,0.9)",
  scaleMult:   1.15, glowSpread: 90,
  strokeColor: "rgba(0,0,0,0.5)",
  strokeWidth: 2,   brightness:  1.5,
};

function getWordStyle(tag) {
  return TAG_WORD_STYLES[tag] || DEFAULT_WORD_STYLE;
}

const TAG_TRANSITION_CFG = {
  shock: {
    flashColor:"rgba(255,255,255,1.0)",
    flashFrames:9, shakeAmount:18, scaleBoost:1.12,
  },
  urgency: {
    flashColor:"rgba(220,0,0,0.85)",
    flashFrames:7, shakeAmount:12, scaleBoost:1.08,
  },
  intrigue: {
    flashColor:"rgba(0,0,0,0.6)",
    flashFrames:10, shakeAmount:5, scaleBoost:1.04,
  },
  emotional: {
    flashColor:"rgba(255,100,150,0.35)",
    flashFrames:12, shakeAmount:3, scaleBoost:1.02,
  },
  confident: {
    flashColor:"rgba(255,255,255,0.55)",
    flashFrames:6, shakeAmount:6, scaleBoost:1.06,
  },
  inspiration: {
    flashColor:"rgba(255,215,0,0.6)",
    flashFrames:8, shakeAmount:4, scaleBoost:1.07,
  },
  wisdom: {
    flashColor:"rgba(130,177,255,0.3)",
    flashFrames:14, shakeAmount:2, scaleBoost:1.01,
  },
  desire: {
    flashColor:"rgba(255,100,180,0.4)",
    flashFrames:10, shakeAmount:4, scaleBoost:1.03,
  },
  calm: {
    flashColor:"rgba(100,200,255,0.2)",
    flashFrames:16, shakeAmount:1, scaleBoost:1.0,
  },
  information: {
    flashColor:"rgba(255,255,255,0.15)",
    flashFrames:6, shakeAmount:0, scaleBoost:1.0,
  },
  pause: {
    flashColor:"rgba(0,0,0,0.7)",
    flashFrames:18, shakeAmount:0, scaleBoost:1.0,
  },
  whisper: {
    flashColor:"rgba(100,0,150,0.4)",
    flashFrames:12, shakeAmount:2, scaleBoost:1.02,
  },
  curiosity: {
    flashColor:"rgba(255,241,118,0.4)",
    flashFrames:10, shakeAmount:3, scaleBoost:1.03,
  },
  storytelling: {
    flashColor:"rgba(255,200,100,0.25)",
    flashFrames:8, shakeAmount:1, scaleBoost:1.01,
  },
  dramatic: {
    flashColor:"rgba(180,0,0,0.6)",
    flashFrames:12, shakeAmount:10, scaleBoost:1.10,
  },
  revelation: {
    flashColor:"rgba(255,255,200,0.9)",
    flashFrames:10, shakeAmount:14, scaleBoost:1.15,
  },
  tension: {
    flashColor:"rgba(255,100,0,0.5)",
    flashFrames:8, shakeAmount:10, scaleBoost:1.08,
  },
  climax: {
    flashColor:"rgba(255,255,255,0.95)",
    flashFrames:11, shakeAmount:20, scaleBoost:1.18,
  },
  powerful: {
    flashColor:"rgba(255,255,255,0.6)",
    flashFrames:7, shakeAmount:7, scaleBoost:1.07,
  },
};

const DEFAULT_TRANSITION_CFG = {
  flashColor:"rgba(255,255,255,0.3)",
  flashFrames:7, shakeAmount:4, scaleBoost:1.02,
};

// ═══════════════════════════════════════════════════════════════════════════
// SHORT: WORD LIST & FRAME STATE
// ═══════════════════════════════════════════════════════════════════════════

function buildWordList() {
  const words = [];
  for (const seg of aligned) {
    if (!seg.words || !seg.words.length) continue;
    const segTag = seg.tag || "information";
    for (const x of seg.words) {
      if (!x.word || !x.word.trim()) continue;
      const s = parseFloat(x.start);
      const e = parseFloat(x.end);
      if (
        isNaN(s) || isNaN(e) ||
        s < 0 || e <= s
      ) continue;
      words.push({
        word:    x.word.trim(),
        start:   s, end: e,
        tag:     segTag,
        isPower: isPowerWord(x.word),
      });
    }
  }

  if (!words.length && sentences.length) {
    const all = sentences.join(" ")
      .split(/\s+/).filter(Boolean);
    const pw  = effectiveDuration /
      Math.max(all.length, 1);
    all.forEach((w, i) => words.push({
      word:    w,
      start:   i * pw, end: (i + 1) * pw,
      tag:     "information",
      isPower: isPowerWord(w),
    }));
  }

  words.sort((a, b) => a.start - b.start);
  console.log(`📊 Words: ${words.length}`);
  return words;
}

/**
 * ✅ Pointer متحرك بدل Binary search — O(n)
 */
function buildFrameStateMap(words) {
  const map = new Array(totalFrames).fill(null);
  if (!words.length) return map;

  let wordIdx = 0;

  for (let f = 0; f < totalFrames; f++) {
    const t = f / FPS;

    // تقدم pointer للأمام
    while (
      wordIdx < words.length - 1 &&
      words[wordIdx].end <= t
    ) {
      wordIdx++;
    }

    const w = words[wordIdx];
    if (w && t >= w.start && t < w.end) {
      map[f] = {
        word:     w.word,
        tag:      w.tag,
        isPower:  w.isPower,
        progress: (t - w.start) /
          Math.max(w.end - w.start, 0.001),
      };
    }
  }

  const cov = map.filter(Boolean).length;
  console.log(
    `Coverage: ${cov}/${totalFrames} ` +
    `(${((cov / totalFrames) * 100).toFixed(1)}%)`
  );
  return map;
}

function buildSentenceBoundaryMap() {
  if (!aligned || !aligned.length) return new Map();
  const map = new Map();
  for (let i = 0; i < aligned.length - 1; i++) {
    const seg = aligned[i];
    const et  = parseFloat(seg.end || 0);
    if (et <= 0) continue;
    const tag = seg.tag || "information";
    const cfg =
      TAG_TRANSITION_CFG[tag] ||
      DEFAULT_TRANSITION_CFG;
    const ef  = Math.floor(et * FPS);
    for (let f = 0; f < cfg.flashFrames; f++) {
      const fr = ef + f;
      if (
        fr >= 0 && fr < totalFrames &&
        !map.has(fr)
      ) {
        map.set(fr, {
          tag, config: cfg,
          progress: f /
            Math.max(cfg.flashFrames - 1, 1),
        });
      }
    }
  }
  return map;
}

// ═══════════════════════════════════════════════════════════════════════════
// SHORT: ANIMATION
// ═══════════════════════════════════════════════════════════════════════════

function computeTitleAnimation(gf) {
  if (gf < INTRO_FRAMES) {
    const t = gf / INTRO_FRAMES;
    const e = 1 - Math.pow(1 - t, 3);
    return { opacity: e, translateY: (1 - e) * -80 };
  }
  if (gf >= totalFrames - OUTRO_FRAMES) {
    const t = (
      gf - (totalFrames - OUTRO_FRAMES)
    ) / OUTRO_FRAMES;
    return {
      opacity:    1 - Math.pow(t, 2),
      translateY: Math.pow(t, 2) * -60,
    };
  }
  return { opacity: 1.0, translateY: 0 };
}

function computeWordAnimation(progress, scaleMult) {
  if (progress < 0.15) {
    const t = progress / 0.15;
    const e = 1 - Math.pow(1 - t, 2);
    return {
      scale:      0.6 + e * 0.48,
      opacity:    Math.min(1, t * 3),
      translateY: (1 - e) * 30,
    };
  }
  if (progress > 0.85) {
    const t = (progress - 0.85) / 0.15;
    return {
      scale:      1 - t * 0.05,
      opacity:    1 - t * 0.3,
      translateY: 0,
    };
  }
  return {
    scale: scaleMult, opacity: 1.0, translateY: 0,
  };
}

function computeTransitionEffect(transState, gf) {
  if (!transState) {
    return {
      flashOpacity: 0,
      flashColor:   "rgba(0,0,0,0)",
      shakeX: 0, shakeY: 0, transScale: 1.0,
    };
  }
  const c  = transState.config;
  const tp = transState.progress;
  let fo   = tp < 0.3
    ? tp / 0.3
    : 1 - (tp - 0.3) / 0.7;
  fo = Math.max(0, Math.min(1, fo));
  let sx = 0, sy = 0;
  if (c.shakeAmount > 0) {
    const s = c.shakeAmount * (1 - tp);
    sx = Math.sin(gf * 2.3) * s;
    sy = Math.cos(gf * 1.7) * s;
  }
  let ts = 1.0;
  if (c.scaleBoost > 1.0 && tp < 0.5) {
    ts = 1 + (c.scaleBoost - 1) * (1 - tp * 2);
  }
  return {
    flashOpacity: fo, flashColor: c.flashColor,
    shakeX: sx, shakeY: sy, transScale: ts,
  };
}

// ═══════════════════════════════════════════════════════════════════════════
// SHORT: FONT SIZE
// ═══════════════════════════════════════════════════════════════════════════

const SHORT_FONT_SIZES = [
  { maxLen:  2, ar: 170, en: 160 },
  { maxLen:  4, ar: 150, en: 140 },
  { maxLen:  6, ar: 130, en: 120 },
  { maxLen:  9, ar: 110, en: 102 },
  { maxLen: 12, ar:  92, en:  86 },
  { maxLen: 99, ar:  76, en:  72 },
];

function computeFontSize(word, isAr, scaleMult) {
  if (!word) return 100;
  let base = 100;
  for (const fs of SHORT_FONT_SIZES) {
    if (word.length <= fs.maxLen) {
      base = isAr ? fs.ar : fs.en;
      break;
    }
  }
  return Math.max(
    60, Math.min(220, Math.round(base * scaleMult))
  );
}

// ═══════════════════════════════════════════════════════════════════════════
// SHORT: HTML BUILDER
// ═══════════════════════════════════════════════════════════════════════════

const HOOK_DEFAULTS = {
  ar: "🔴 لا تتجاوز هذا",
  fr: "🔴 Ne ratez pas ça",
  en: "🔴 Don't skip this",
};
const getHookText = () =>
  (custom_hook && custom_hook.trim()) ||
  HOOK_DEFAULTS[lang] ||
  HOOK_DEFAULTS.en;

/**
 * ✅ stateKey مع progress bucket أدق
 */
function stateKey(state, gf, ts) {
  if (gf < INTRO_FRAMES) return `intro_f${gf}`;
  if (gf >= totalFrames - OUTRO_FRAMES) {
    return `outro_f${gf}`;
  }

  if (ts) {
    const pb = ts.progress < 0.5 ? "in" : "out";
    const wk = state
      ? safeKey(state.word, 10) : "empty";
    return (
      `tr_${ts.tag}_${pb}_${wk}_` +
      `${state && state.isPower ? 1 : 0}`
    );
  }

  const h = gf < HOOK_FRAMES ? "h" : "n";
  if (!state) return `empty_${h}`;

  const p = state.progress;
  // ✅ bucket أدق
  const b = p < 0.15 ? "pop"
    : p > 0.85 ? "fade"
    : `hold${Math.floor(p * 10)}`;

  return (
    `w_${safeKey(state.word, 12)}_` +
    `${state.tag}_${state.isPower ? 1 : 0}_${h}_${b}`
  );
}

function buildHTMLShort(params) {
  const word            = params.word;
  const tag             = params.tag || "information";
  const isPower         = params.isPower || false;
  const isHook          = params.isHook || false;
  const globalFrame     = params.globalFrame || 0;
  const progress        =
    params.progress !== undefined
      ? params.progress : 0.5;
  const transitionState =
    params.transitionState || null;

  const ar   = word ? isArabic(word) : false;
  const dir  = word ? getDir(word)   : "ltr";
  const font = word
    ? getFontFamily(word)
    : `"Noto Sans",sans-serif`;
  const la   = word ? getLang(word)  : "en";
  const td   = getDir(display_title);
  const tf   = getFontFamily(display_title);
  const ts   = isPower
    ? POWER_STYLE : getWordStyle(tag);
  const ta   = computeTitleAnimation(globalFrame);
  const wa   = word
    ? computeWordAnimation(progress, ts.scaleMult)
    : { scale: 1, opacity: 0, translateY: 0 };
  const tr   = computeTransitionEffect(
    transitionState, globalFrame
  );
  const fs   = computeFontSize(
    word || "", ar, ts.scaleMult
  );
  const fsc  = wa.scale * tr.transScale;
  const fo   = word ? wa.opacity : 0;
  const wt   = (
    `translate(-50%,calc(-50% + ` +
    `${wa.translateY.toFixed(1)}px)) ` +
    `translate(${tr.shakeX.toFixed(2)}px,` +
    `${tr.shakeY.toFixed(2)}px) ` +
    `scale(${fsc.toFixed(4)})`
  );
  const ht   = getHookText();
  const ha   = isArabic(ht);
  const hd   = ha ? "rtl" : "ltr";
  const hf   = getFontFamily(ht);
  const tia  = isArabic(display_title);
  const tfs  = tia ? 52 : 46;
  const es   = tia ? 56 : 50;

  // ✅ إصلاح v6.1: top ديناميكي يأخذ
  // translateY و scaleMult في الحسبان
  const MAX_TRANSLATE_Y = 30;
  const effectiveFs     = Math.ceil(
    fs * ts.scaleMult
  );
  const wordTopPx = Math.floor(HEIGHT * 0.54);
  const safeWordTop = Math.max(
    HEIGHT * 0.3,  // حد أدنى
    Math.min(
      wordTopPx,
      HEIGHT - effectiveFs * 1.2 - MAX_TRANSLATE_Y
    )
  );

  const pcs = isPower
    ? (
        `background:linear-gradient(135deg,` +
        `#FF1744,#D50000);` +
        `padding:24px 60px;border-radius:9999px;` +
        `border:3px solid rgba(255,255,255,0.3);` +
        `box-shadow:0 0 60px rgba(255,23,68,0.8),` +
        `0 0 120px rgba(255,23,68,0.4);`
      )
    : `background:transparent;padding:0;`;

  const wts = isPower
    ? (
        `font-family:${font};` +
        `font-size:${fs}px;font-weight:900;` +
        `color:#FFF;line-height:1.15;` +
        `letter-spacing:${ar ? "1px" : "3px"};` +
        `display:block;word-break:break-word;` +
        `-webkit-text-stroke:${ts.strokeWidth}px ` +
        `${ts.strokeColor};` +
        `paint-order:stroke fill;`
      )
    : (
        `font-family:${font};` +
        `font-size:${fs}px;font-weight:900;` +
        `color:${ts.colorWord};line-height:1.15;` +
        `letter-spacing:${ar ? "1px" : "3px"};` +
        `display:block;word-break:break-word;` +
        `-webkit-text-stroke:${ts.strokeWidth}px ` +
        `${ts.strokeColor};` +
        `paint-order:stroke fill;` +
        `text-shadow:` +
        `0 0 ${ts.glowSpread}px ${ts.colorGlow},` +
        `0 0 ${ts.glowSpread * 1.5}px ` +
        `${ts.colorGlow};` +
        `filter:brightness(${ts.brightness});`
      );

  return (
    `<!DOCTYPE html>` +
    `<html lang="${la}"><head>` +
    `<meta charset="UTF-8"/><style>` +
    `*{margin:0;padding:0;box-sizing:border-box;}` +
    `html,body{` +
    `width:${WIDTH}px;height:${HEIGHT}px;` +
    `overflow:hidden;background:transparent;}` +
    `.ot{position:absolute;top:0;left:0;` +
    `right:0;height:40%;` +
    `background:linear-gradient(to bottom,` +
    `rgba(0,0,0,0.90) 0%,rgba(0,0,0,0.55) 50%,` +
    `transparent 100%);` +
    `pointer-events:none;z-index:1;}` +
    `.ob{position:absolute;bottom:0;left:0;` +
    `right:0;height:45%;` +
    `background:linear-gradient(to top,` +
    `rgba(0,0,0,0.92) 0%,rgba(0,0,0,0.50) 65%,` +
    `transparent 100%);` +
    `pointer-events:none;z-index:1;}` +
    `.flash{position:absolute;inset:0;` +
    `background:${tr.flashColor};` +
    `opacity:${tr.flashOpacity.toFixed(4)};` +
    `pointer-events:none;z-index:50;}` +
    `.tc{position:absolute;top:410px;left:50%;` +
    `width:92%;max-width:980px;` +
    `direction:${td};text-align:center;` +
    `z-index:30;` +
    `transform:translateX(-50%) ` +
    `translateY(${ta.translateY.toFixed(2)}px);` +
    `opacity:${ta.opacity.toFixed(4)};}` +
    `.tc::after{content:'';display:block;` +
    `margin:16px auto 0;` +
    `width:120px;height:4px;border-radius:2px;` +
    `background:linear-gradient(90deg,` +
    `transparent,#FF1744,transparent);` +
    `opacity:${ta.opacity.toFixed(4)};}` +
    `.tt{font-family:${tf};font-size:${tfs}px;` +
    `font-weight:900;color:#FFF;` +
    `display:inline-flex;align-items:center;` +
    `justify-content:center;gap:14px;` +
    `line-height:1.3;direction:${td};` +
    `-webkit-text-stroke:2px rgba(0,0,0,0.8);` +
    `paint-order:stroke fill;` +
    `text-shadow:` +
    `0 0 30px rgba(255,23,68,0.6),` +
    `0 4px 20px rgba(0,0,0,0.9),` +
    `2px 2px 0 rgba(0,0,0,0.8);}` +
    `.te{font-size:${es}px;` +
    `-webkit-text-stroke:0;}` +
    `.hb{position:absolute;` +
    `top:${tia ? "290px" : "270px"};` +
    `left:50%;transform:translateX(-50%);` +
    `background:linear-gradient(135deg,` +
    `rgba(220,0,0,0.95),rgba(160,0,0,0.95));` +
    `color:#fff;font-family:${hf};` +
    `font-size:${ha ? "32px" : "28px"};` +
    `font-weight:900;` +
    `padding:12px 38px;border-radius:9999px;` +
    `z-index:25;white-space:nowrap;` +
    `direction:${hd};` +
    `border:2px solid rgba(255,120,120,0.4);` +
    `box-shadow:` +
    `0 0 50px rgba(220,0,0,0.7),` +
    `0 8px 24px rgba(0,0,0,0.5);}` +
    // ✅ top ديناميكي
    `.wc{position:absolute;left:50%;` +
    `top:${safeWordTop}px;` +
    `transform:${wt};` +
    `opacity:${fo.toFixed(4)};` +
    `direction:${dir};text-align:center;` +
    `z-index:10;width:95%;max-width:1020px;}` +
    `.wp{display:inline-block;${pcs}}` +
    `.wt{${wts}}` +
    `</style></head><body>` +
    `<div class="ot"></div>` +
    `<div class="ob"></div>` +
    `<div class="flash"></div>` +
    `<div class="tc"><div class="tt">` +
    `<span class="te">${emoji_left}</span>` +
    `<span>${esc(display_title)}</span>` +
    `<span class="te">${emoji_right}</span>` +
    `</div></div>` +
    (isHook
      ? `<div class="hb">${esc(ht)}</div>`
      : "") +
    (word
      ? `<div class="wc"><div class="wp">` +
        `<span class="wt">${esc(word)}</span>` +
        `</div></div>`
      : "") +
    `</body></html>`
  );
}

// ═══════════════════════════════════════════════════════════════════════════
// SHORT: BROWSER & RENDER PNGs
// ═══════════════════════════════════════════════════════════════════════════

const BROWSER_ARGS = [
  "--no-sandbox",
  "--disable-setuid-sandbox",
  "--disable-dev-shm-usage",
  "--disable-gpu",
  "--no-zygote",
  "--font-render-hinting=none",
  "--lang=ar,fr,en",
];

async function launchBrowser() {
  const browser = await chromium.launch({
    headless: true, args: BROWSER_ARGS,
  });
  const context = await browser.newContext({
    viewport:          { width: WIDTH, height: HEIGHT },
    deviceScaleFactor: 1,
    locale:            "ar-SA",
  });
  const page = await context.newPage();
  return { browser, page };
}

/**
 * ✅ warmup مع timeout=15s + screenshot للتحقق
 */
async function warmupFonts(page) {
  const cases = [
    {
      word:"مرحبا", lang:"ar",
      tag:"shock", isPower:true,
    },
    {
      word:"Hello", lang:"en",
      tag:"information", isPower:false,
    },
  ];

  for (const tc of cases) {
    const html = buildHTMLShort({
      word:        tc.word,
      tag:         tc.tag,
      isPower:     tc.isPower,
      isHook:      false,
      globalFrame: TITLE_SLIDE_FRAMES,
      progress:    0.5,
    });
    const p = join(TMP, `init_${tc.lang}.html`);
    writeFileSync(p, html, "utf-8");

    await page.goto(
      `file://${p}`,
      { waitUntil: "load", timeout: 15000 }
    );

    // ✅ انتظار fonts مع timeout منفصل
    await page.evaluate(async () => {
      await Promise.race([
        document.fonts.ready,
        new Promise(r => setTimeout(r, 5000)),
      ]);
    });

    // ✅ screenshot للتحقق من التحميل الفعلي
    await page.screenshot({
      path: join(TMP, `warmup_${tc.lang}.png`),
      type: "png",
    });
    await page.waitForTimeout(200);
  }
  console.log("✅ Fonts loaded");
}

async function renderPNGsShort(page, fsm, bm) {
  const unique = new Map();
  for (let f = 0; f < fsm.length; f++) {
    const ts = bm.get(f) || null;
    const k  = stateKey(fsm[f], f, ts);
    if (!unique.has(k)) {
      unique.set(k, {
        word:        fsm[f] ? fsm[f].word : null,
        tag:         fsm[f]
          ? fsm[f].tag : "information",
        isPower:     fsm[f]
          ? fsm[f].isPower : false,
        isHook:      f < HOOK_FRAMES,
        globalFrame: f,
        progress:    fsm[f]
          ? fsm[f].progress : 0.5,
        transitionState: ts,
      });
    }
  }

  console.log(
    `\n📸 ${unique.size} unique states [SHORT]`
  );
  await warmupFonts(page);

  const cache = new Map();
  let done    = 0;

  for (const [k, s] of unique) {
    const html = buildHTMLShort(s);
    const hp   = join(TMP, `${k}.html`);
    writeFileSync(hp, html, "utf-8");

    await page.goto(
      `file://${hp}`,
      { waitUntil: "load", timeout: 5000 }
    );
    await page.evaluate(
      () => document.fonts.ready
    );
    await page.waitForTimeout(30);

    const pp = join(TMP, `${k}.png`);
    await page.screenshot({
      path: pp, type: "png", omitBackground: true,
    });
    cache.set(k, pp);

    // ✅ حذف HTML بعد الاستخدام
    try {
      spawnSync("rm", ["-f", hp], { stdio: "ignore" });
    } catch {}

    done++;
    if (done % 50 === 0 || done === unique.size) {
      process.stdout.write(
        `  ${done}/${unique.size} PNGs\n`
      );
    }
  }

  return cache;
}

// ═══════════════════════════════════════════════════════════════════════════
// MODE HANDLERS
// ═══════════════════════════════════════════════════════════════════════════

async function handleBgOnlyMode() {
  const plan = buildClipPlan();

  if (isLong) {
    console.log(`\n🚀 Long BG: FFmpeg pipeline`);
    buildLongBgVideo(plan, audio, outputPath);
    console.log(
      `\n🎉 BG [${content_mode.toUpperCase()}/` +
      `${platform.toUpperCase()}] → ${outputPath}\n`
    );
    _renderCompleted = true;
    return;
  }

  console.log(
    `\n📊 Processing ${plan.length} clips [SHORT]`
  );
  const processedClips = [];

  for (const clip of plan) {
    const i = clip.index;
    const d = clip.duration;
    const v = clip.videoPath;
    const h = clip.isHook;
    process.stdout.write(
      `  [${i + 1}/${plan.length}] ` +
      `${d.toFixed(2)}s [${clip.section.type}]` +
      `${h ? " 🔥" : ""}... `
    );
    const bg = join(
      TMP,
      `bg_${String(i).padStart(3, "0")}.mp4`
    );
    processBackground(v, d, bg, i, h);
    processedClips.push(bg);
    process.stdout.write("✓\n");
  }

  const merged = concatClipsWithTransitions(
    processedClips, plan
  );
  if (!merged || !existsSync(merged)) {
    console.error("❌ No merged video");
    process.exit(1);
  }

  console.log("\n🎵 Merging audio...");
  mergeAudio(merged, audio, outputPath);
  console.log(`\n🎉 BG [SHORT] → ${outputPath}\n`);
  _renderCompleted = true;
}

async function handleWordsOnlyMode() {
  const bgVideo = videos[0];
  if (!bgVideo || !existsSync(bgVideo)) {
    console.error(
      "❌ words_only requires videos[0]"
    );
    process.exit(1);
  }

  if (isLong) {
    console.log(`\n🚀 Long Words: ASS overlay`);
    buildLongWordsOverlay(
      bgVideo, audio, outputPath
    );
    console.log(
      `\n🎉 Final [${content_mode.toUpperCase()}/` +
      `${platform.toUpperCase()}] → ${outputPath}\n`
    );
    _renderCompleted = true;
    return;
  }

  console.log(
    `\n🎨 Short Words: Playwright quality`
  );
  const words = buildWordList();
  const fsm   = buildFrameStateMap(words);
  const bm    = buildSentenceBoundaryMap();

  const launched = await launchBrowser();
  const browser  = launched.browser;
  const page     = launched.page;

  try {
    console.log(`\n🖼️ Rendering PNGs...`);
    const cache = await renderPNGsShort(
      page, fsm, bm
    );
    console.log(`✅ ${cache.size} PNGs\n`);

    const fd = join(TMP, "frames_words");
    mkdirSync(fd, { recursive: true });
    const ep =
      cache.get("empty_n") ||
      cache.get("intro_f0");

    for (let f = 0; f < totalFrames; f++) {
      const ts = bm.get(f) || null;
      const k  = stateKey(fsm[f], f, ts);
      linkFrame(
        cache.get(k) || ep || "",
        join(
          fd,
          `frame_${String(f).padStart(6, "0")}.png`
        ),
      );
    }

    const cm = join(TMP, "cap_words.mov");
    framesToMov(fd, cm);

    console.log("🔧 Overlaying words...");
    const wv = join(TMP, "with_words.mp4");
    overlayOnBg(bgVideo, cm, audio, wv);

    console.log("📱 Applying metadata...");
    applyMetadata(wv, outputPath);

    _renderCompleted = true;
    console.log(
      `\n🎉 Final [SHORT] → ${outputPath}\n`
    );

  } finally {
    try {
      if (browser && browser.isConnected()) {
        await browser.close();
      }
    } catch (e) {
      console.warn(
        "Browser close error:", e.message
      );
    }
  }
}

// ═══════════════════════════════════════════════════════════════════════════
// MAIN
// ═══════════════════════════════════════════════════════════════════════════

const MODE_HANDLERS = {
  bg_only:         handleBgOnlyMode,
  long_bg_only:    handleBgOnlyMode,
  words_only:      handleWordsOnlyMode,
  long_words_only: handleWordsOnlyMode,
};

async function main() {
  console.log(
    `\n🚀 Mode:${mode} | ` +
    `${content_mode.toUpperCase()}/` +
    `${platform.toUpperCase()}\n`
  );

  const handler = MODE_HANDLERS[mode];
  if (!handler) {
    console.error(`❌ Unknown mode: ${mode}`);
    process.exit(1);
  }
  await handler();
}

main().catch(e => {
  console.error("❌", e.message || e);
  process.exit(1);
});

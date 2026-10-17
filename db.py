"""
🗄️ SQLite Database for VSG (Video Script Generator) v2.0

Features:
  ✅ Used videos tracking (Pexels/Pixabay)
  ✅ Resume system (renders)
  ✅ AI cache (per video_number + lang + content_mode)
  ✅ Publish tracking (per language + platform + mode)
  ✅ Daily quota tracking (5 short + 1 long per lang per day)
  ✅ Pre-generation tracking (ready to publish)
  ✅ Auto-next system (tracks platforms independently)
  ✅ Loop support (reset when done)
  ✅ Thread-safe (WAL mode + RLock + unique Savepoints)
  ✅ Auto-migrations (smart error classification)
  ✅ Input validation on all public functions
  ✅ UTC timestamps everywhere
  ✅ mark_render_done — atomic read+write
  ✅ init_db — thread-safe with _write_lock
  ✅ get_ready_to_publish — distinguishes "no videos" vs "missing files"
  ✅ save_pre_generated — Long FB path fix
  ✅ print_db_summary — crash-safe per query
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

log = logging.getLogger(__name__)

# ═════════════════════════════════════════════════════════════════════════════
# CONSTANTS
# ═════════════════════════════════════════════════════════════════════════════

BASE_DIR = Path(__file__).parent.resolve()
DB_PATH  = BASE_DIR / "vsg.db"

CONNECTION_TIMEOUT = 30.0
BUSY_TIMEOUT_MS    = 30_000
CACHE_SIZE_KB      = -8_000  # Negative = KB in SQLite

DAILY_QUOTA: dict[str, int] = {
    "short": 5,
    "long":  1,
}

LANGS     = frozenset({"ar", "fr", "en"})
MODES     = frozenset({"short", "long"})
PLATFORMS = frozenset({"facebook", "youtube"})

MAX_ERROR_LENGTH = 500


# ═════════════════════════════════════════════════════════════════════════════
# THREAD-LOCAL STATE
# ═════════════════════════════════════════════════════════════════════════════

_local      = threading.local()
_write_lock = threading.RLock()


# ═════════════════════════════════════════════════════════════════════════════
# VALIDATION
# ═════════════════════════════════════════════════════════════════════════════

def _validate_lang(lang: str) -> None:
    if lang not in LANGS:
        raise ValueError(
            f"Invalid lang '{lang}'. "
            f"Must be one of: {sorted(LANGS)}"
        )


def _validate_mode(mode: str) -> None:
    if mode not in MODES:
        raise ValueError(
            f"Invalid content_mode '{mode}'. "
            f"Must be one of: {sorted(MODES)}"
        )


def _validate_platform(platform: str) -> None:
    if platform not in PLATFORMS:
        raise ValueError(
            f"Invalid platform '{platform}'. "
            f"Must be one of: {sorted(PLATFORMS)}"
        )


def _validate_video_number(video_number: str) -> str:
    vn = str(video_number).strip()
    if not vn:
        raise ValueError("video_number cannot be empty")
    return vn


# ═════════════════════════════════════════════════════════════════════════════
# TIME HELPERS — UTC always
# ═════════════════════════════════════════════════════════════════════════════

def _today_utc_iso() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ═════════════════════════════════════════════════════════════════════════════
# CONNECTION MANAGEMENT
# ═════════════════════════════════════════════════════════════════════════════

def _apply_pragmas(conn: sqlite3.Connection) -> None:
    """Apply PRAGMAs — WAL outside any transaction."""
    result = conn.execute("PRAGMA journal_mode=WAL").fetchone()
    if result and result[0] != "wal":
        log.warning("  ⚠️  WAL not activated: %s", result[0])
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute(f"PRAGMA cache_size={CACHE_SIZE_KB}")
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA temp_store=MEMORY")


def _create_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(
        str(DB_PATH),
        check_same_thread = True,
        timeout           = CONNECTION_TIMEOUT,
    )
    conn.row_factory = sqlite3.Row
    _apply_pragmas(conn)
    return conn


def _conn() -> sqlite3.Connection:
    conn = getattr(_local, "conn", None)
    if conn is None:
        _local.conn = _create_connection()
    return _local.conn


def close_thread_conn() -> None:
    conn = getattr(_local, "conn", None)
    if conn is None:
        return
    try:
        conn.close()
    except Exception:
        pass
    _local.conn = None


@contextmanager
def write_transaction() -> Iterator[sqlite3.Connection]:
    """
    Context manager for safe writes.
    
    RLock allows same thread to enter multiple times.
    Unique Savepoint per nested call.
    Auto COMMIT / ROLLBACK.
    """
    with _write_lock:
        conn = _conn()

        if conn.in_transaction:
            # Nested: use Savepoint
            counter = getattr(_local, "_sp_counter", 0) + 1
            _local._sp_counter = counter
            sp = f"sp_{threading.get_ident()}_{counter}"

            try:
                conn.execute(f"SAVEPOINT {sp}")
                yield conn
                conn.execute(f"RELEASE SAVEPOINT {sp}")
            except Exception as exc:
                try:
                    conn.execute(f"ROLLBACK TO SAVEPOINT {sp}")
                    conn.execute(f"RELEASE SAVEPOINT {sp}")
                except Exception:
                    pass
                log.error("  ❌ DB nested write failed: %s", exc)
                raise
            finally:
                _local._sp_counter = counter - 1
        else:
            # Top-level: BEGIN IMMEDIATE
            try:
                conn.execute("BEGIN IMMEDIATE")
                yield conn
                conn.execute("COMMIT")
            except Exception as exc:
                try:
                    conn.execute("ROLLBACK")
                except Exception:
                    pass
                log.error("  ❌ DB write failed: %s", exc)
                raise


# ═════════════════════════════════════════════════════════════════════════════
# SCHEMA
# ═════════════════════════════════════════════════════════════════════════════

_UTC_DEFAULT = "strftime('%Y-%m-%dT%H:%M:%SZ','now')"

_SCHEMA_STATEMENTS: list[str] = [

    f"""
    CREATE TABLE IF NOT EXISTS used_videos (
        id        INTEGER PRIMARY KEY AUTOINCREMENT,
        source_id TEXT    NOT NULL,
        source    TEXT    NOT NULL DEFAULT 'pixabay',
        keyword   TEXT,
        used_at   TEXT    NOT NULL DEFAULT ({_UTC_DEFAULT}),
        UNIQUE(source_id, source)
    )
    """,

    f"""
    CREATE TABLE IF NOT EXISTS renders (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        video_number TEXT    NOT NULL,
        lang         TEXT    NOT NULL,
        content_mode TEXT    NOT NULL DEFAULT 'short',
        status       TEXT    NOT NULL DEFAULT 'pending',
        output_path  TEXT,
        fb_path      TEXT,
        yt_path      TEXT,
        duration_s   REAL,
        error        TEXT,
        created_at   TEXT    NOT NULL DEFAULT ({_UTC_DEFAULT}),
        updated_at   TEXT    NOT NULL DEFAULT ({_UTC_DEFAULT}),
        UNIQUE(video_number, lang, content_mode),
        CHECK(lang         IN ('ar','fr','en')),
        CHECK(content_mode IN ('short','long')),
        CHECK(status IN ('pending','running','done','failed'))
    )
    """,

    f"""
    CREATE TABLE IF NOT EXISTS ai_cache (
        cache_key            TEXT PRIMARY KEY,
        lang                 TEXT NOT NULL DEFAULT 'ar',
        content_mode         TEXT NOT NULL DEFAULT 'short',
        title                TEXT,
        analysis             TEXT,
        power_words          TEXT,
        visual_keywords      TEXT,
        pattern_interrupts   TEXT,
        engagement_questions TEXT,
        hashtags             TEXT,
        captions             TEXT,
        street_description   TEXT,
        accent_colors        TEXT,
        hook_keyword         TEXT,
        custom_hook          TEXT,
        attractive_title     TEXT,
        tagged               TEXT,
        created_at           TEXT NOT NULL DEFAULT ({_UTC_DEFAULT}),
        updated_at           TEXT NOT NULL DEFAULT ({_UTC_DEFAULT}),
        CHECK(lang         IN ('ar','fr','en')),
        CHECK(content_mode IN ('short','long'))
    )
    """,

    f"""
    CREATE TABLE IF NOT EXISTS publish_tracker (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        video_number TEXT    NOT NULL,
        lang         TEXT    NOT NULL,
        content_mode TEXT    NOT NULL DEFAULT 'short',
        platform     TEXT    NOT NULL DEFAULT 'youtube',
        published_at TEXT    NOT NULL DEFAULT ({_UTC_DEFAULT}),
        UNIQUE(video_number, lang, content_mode, platform),
        CHECK(lang         IN ('ar','fr','en')),
        CHECK(content_mode IN ('short','long')),
        CHECK(platform     IN ('facebook','youtube'))
    )
    """,

    f"""
    CREATE TABLE IF NOT EXISTS scripts (
        video_number TEXT    NOT NULL,
        lang         TEXT    NOT NULL DEFAULT 'ar',
        content_mode TEXT    NOT NULL DEFAULT 'short',
        title        TEXT,
        sentences    INTEGER NOT NULL DEFAULT 0,
        words        INTEGER NOT NULL DEFAULT 0,
        saved_at     TEXT    NOT NULL DEFAULT ({_UTC_DEFAULT}),
        PRIMARY KEY (video_number, lang, content_mode),
        CHECK(lang         IN ('ar','fr','en')),
        CHECK(content_mode IN ('short','long'))
    )
    """,

    f"""
    CREATE TABLE IF NOT EXISTS pre_generated (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        video_number TEXT    NOT NULL,
        lang         TEXT    NOT NULL,
        content_mode TEXT    NOT NULL DEFAULT 'short',
        title        TEXT,
        output_path  TEXT    NOT NULL,
        fb_path      TEXT,
        yt_path      TEXT,
        duration_s   REAL,
        generated_at TEXT    NOT NULL DEFAULT ({_UTC_DEFAULT}),
        scheduled_at TEXT,
        published    INTEGER NOT NULL DEFAULT 0,
        published_at TEXT,
        UNIQUE(video_number, lang, content_mode),
        CHECK(lang         IN ('ar','fr','en')),
        CHECK(content_mode IN ('short','long'))
    )
    """,

    # Indexes
    "CREATE INDEX IF NOT EXISTS idx_used_videos ON used_videos(source_id, source)",
    "CREATE INDEX IF NOT EXISTS idx_renders ON renders(video_number, lang, content_mode)",
    "CREATE INDEX IF NOT EXISTS idx_renders_status ON renders(status)",
    "CREATE INDEX IF NOT EXISTS idx_renders_lang ON renders(lang, content_mode, status)",
    "CREATE INDEX IF NOT EXISTS idx_ai_cache ON ai_cache(cache_key)",
    "CREATE INDEX IF NOT EXISTS idx_ai_cache_lang ON ai_cache(lang, content_mode)",
    "CREATE INDEX IF NOT EXISTS idx_publish ON publish_tracker(video_number, lang, content_mode, platform)",
    "CREATE INDEX IF NOT EXISTS idx_publish_lang ON publish_tracker(lang, content_mode, platform)",
    "CREATE INDEX IF NOT EXISTS idx_publish_date ON publish_tracker(lang, content_mode, platform, published_at)",
    "CREATE INDEX IF NOT EXISTS idx_pre_generated ON pre_generated(lang, content_mode, published)",
    "CREATE INDEX IF NOT EXISTS idx_pre_gen_schedule ON pre_generated(scheduled_at, published)",
]


def init_db() -> None:
    """
    تهيئة قاعدة البيانات.
    
    Thread-safe with _write_lock.
    DDL in single transaction.
    Auto rollback on failure.
    """
    with _write_lock:
        conn = _conn()

        # Close any dangling transaction
        if conn.in_transaction:
            log.warning(
                "  ⚠️  init_db: open transaction found — rolling back"
            )
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass

        # Schema creation
        conn.execute("BEGIN IMMEDIATE")
        try:
            for stmt in _SCHEMA_STATEMENTS:
                stmt = stmt.strip()
                if stmt:
                    conn.execute(stmt)
            conn.execute("COMMIT")
        except Exception as e:
            log.error("  ❌ Schema creation failed: %s", e)
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise

    # Migrations in separate transaction
    with write_transaction() as c:
        _run_migrations(c)

    log.info("  ✅ DB initialized")


# ═════════════════════════════════════════════════════════════════════════════
# MIGRATIONS (smart error classification)
# ═════════════════════════════════════════════════════════════════════════════

_SIMPLE_MIGRATIONS: list[str] = [
    "ALTER TABLE renders ADD COLUMN content_mode TEXT DEFAULT 'short'",
    "ALTER TABLE renders ADD COLUMN fb_path TEXT",
    "ALTER TABLE renders ADD COLUMN yt_path TEXT",
    "ALTER TABLE ai_cache ADD COLUMN lang TEXT DEFAULT 'ar'",
    "ALTER TABLE ai_cache ADD COLUMN tagged TEXT",
    "ALTER TABLE ai_cache ADD COLUMN street_description TEXT",
    "ALTER TABLE ai_cache ADD COLUMN content_mode TEXT DEFAULT 'short'",
    "ALTER TABLE ai_cache ADD COLUMN custom_hook TEXT",
    "ALTER TABLE publish_tracker ADD COLUMN platform TEXT DEFAULT 'youtube'",
    "ALTER TABLE publish_tracker ADD COLUMN content_mode TEXT DEFAULT 'short'",
    "ALTER TABLE scripts ADD COLUMN content_mode TEXT DEFAULT 'short'",
    "ALTER TABLE pre_generated ADD COLUMN title TEXT",
]


def _run_migrations(c: sqlite3.Connection) -> None:
    """Run migrations with smart error classification."""
    applied = 0
    skipped = 0
    failed  = 0

    for sql in _SIMPLE_MIGRATIONS:
        try:
            c.execute(sql)
            applied += 1
        except sqlite3.OperationalError as e:
            msg = str(e).lower()

            if "duplicate column" in msg or "already exists" in msg:
                # Already applied — normal
                skipped += 1
            elif "no such table" in msg:
                # Table not yet created (will be in schema)
                log.warning(
                    "  ⚠️  Migration skipped (table not yet created): %s",
                    sql[:60]
                )
                skipped += 1
            else:
                # Real error
                failed += 1
                log.error(
                    "  ❌ Migration failed: %s | SQL: %s",
                    e, sql[:60]
                )

    if applied > 0:
        log.info(
            "  ✅ Migrations: %d applied, %d skipped, %d failed",
            applied, skipped, failed
        )
    elif failed > 0:
        log.warning(
            "  ⚠️  Migrations: 0 applied, %d failed",
            failed
        )
# ═════════════════════════════════════════════════════════════════════════════
# CACHE KEY
# ═════════════════════════════════════════════════════════════════════════════

def make_cache_key(
    video_number: str,
    lang:         str,
    content_mode: str = "short",
) -> str:
    _validate_lang(lang)
    _validate_mode(content_mode)
    return f"{video_number}_{lang}_{content_mode}"


# ═════════════════════════════════════════════════════════════════════════════
# USED VIDEOS
# ═════════════════════════════════════════════════════════════════════════════

def is_video_used(source_id: str, source: str = "pixabay") -> bool:
    row = _conn().execute(
        "SELECT 1 FROM used_videos WHERE source_id=? AND source=?",
        (str(source_id), source),
    ).fetchone()
    return row is not None


def mark_video_used(
    source_id: str,
    keyword:   str,
    source:    str = "pixabay",
) -> None:
    with write_transaction() as c:
        c.execute(
            """
            INSERT OR IGNORE INTO used_videos
                (source_id, source, keyword)
            VALUES (?, ?, ?)
            """,
            (str(source_id), source, str(keyword)),
        )


def get_used_count() -> int:
    row = _conn().execute(
        "SELECT COUNT(*) FROM used_videos"
    ).fetchone()
    return int(row[0]) if row else 0


def reset_used_videos() -> int:
    with write_transaction() as c:
        cursor = c.execute("DELETE FROM used_videos")
        return cursor.rowcount


# ═════════════════════════════════════════════════════════════════════════════
# RENDERS
# ═════════════════════════════════════════════════════════════════════════════

def is_render_done(
    video_number: str,
    lang:         str,
    content_mode: str           = "short",
    platform:     Optional[str] = None,
    check_file:   bool          = True,
) -> bool:
    _validate_lang(lang)
    _validate_mode(content_mode)
    vn = _validate_video_number(video_number)

    row = _conn().execute(
        """
        SELECT status, output_path, fb_path, yt_path
        FROM renders
        WHERE video_number=? AND lang=? AND content_mode=?
        """,
        (vn, lang, content_mode),
    ).fetchone()

    if not row or row["status"] != "done":
        return False

    if platform in ("facebook", "fb"):
        path = row["fb_path"] or row["output_path"]
    elif platform in ("youtube", "yt"):
        path = row["yt_path"] or row["output_path"]
    else:
        path = (
            row["output_path"] or
            row["fb_path"]     or
            row["yt_path"]
        )

    if not path:
        return False

    if not check_file:
        return True

    return Path(path).exists()


def get_render_output(
    video_number: str,
    lang:         str,
    content_mode: str = "short",
) -> Optional[str]:
    _validate_lang(lang)
    _validate_mode(content_mode)
    vn = _validate_video_number(video_number)

    row = _conn().execute(
        """
        SELECT output_path FROM renders
        WHERE video_number=? AND lang=? AND content_mode=?
          AND status='done'
        """,
        (vn, lang, content_mode),
    ).fetchone()
    return row["output_path"] if row else None


def mark_render_start(
    video_number: str,
    lang:         str,
    content_mode: str = "short",
) -> None:
    _validate_lang(lang)
    _validate_mode(content_mode)
    vn = _validate_video_number(video_number)

    with write_transaction() as c:
        c.execute(
            f"""
            INSERT INTO renders
                (video_number, lang, content_mode, status, updated_at)
            VALUES (?, ?, ?, 'running', {_UTC_DEFAULT})
            ON CONFLICT(video_number, lang, content_mode) DO UPDATE SET
                status     = 'running',
                error      = NULL,
                updated_at = {_UTC_DEFAULT}
            """,
            (vn, lang, content_mode),
        )


def mark_render_done(
    video_number: str,
    lang:         str,
    output_path:  str,
    duration:     float,
    content_mode: str = "short",
    fb_path:      str = "",
    yt_path:      str = "",
) -> None:
    """
    Mark render as done.
    Atomic read+write in same transaction.
    Does not overwrite existing paths with empty strings.
    """
    _validate_lang(lang)
    _validate_mode(content_mode)
    vn = _validate_video_number(video_number)

    with write_transaction() as c:
        # Read existing paths
        existing = c.execute(
            """
            SELECT fb_path, yt_path, output_path
            FROM renders
            WHERE video_number=? AND lang=? AND content_mode=?
            """,
            (vn, lang, content_mode),
        ).fetchone()

        if existing:
            final_fb = fb_path     or existing["fb_path"]     or output_path
            final_yt = yt_path     or existing["yt_path"]     or output_path
            final_op = output_path or existing["output_path"] or output_path
        else:
            final_fb = fb_path or output_path
            final_yt = yt_path or output_path
            final_op = output_path

        c.execute(
            f"""
            INSERT INTO renders
                (video_number, lang, content_mode, status,
                 output_path, fb_path, yt_path,
                 duration_s, updated_at)
            VALUES (?, ?, ?, 'done', ?, ?, ?, ?, {_UTC_DEFAULT})
            ON CONFLICT(video_number, lang, content_mode) DO UPDATE SET
                status      = 'done',
                output_path = excluded.output_path,
                fb_path     = excluded.fb_path,
                yt_path     = excluded.yt_path,
                duration_s  = excluded.duration_s,
                error       = NULL,
                updated_at  = {_UTC_DEFAULT}
            """,
            (vn, lang, content_mode, final_op, final_fb, final_yt,
             float(duration)),
        )


def mark_render_failed(
    video_number: str,
    lang:         str,
    error:        str,
    content_mode: str = "short",
) -> None:
    _validate_lang(lang)
    _validate_mode(content_mode)
    vn = _validate_video_number(video_number)

    with write_transaction() as c:
        c.execute(
            f"""
            INSERT INTO renders
                (video_number, lang, content_mode, status,
                 error, updated_at)
            VALUES (?, ?, ?, 'failed', ?, {_UTC_DEFAULT})
            ON CONFLICT(video_number, lang, content_mode) DO UPDATE SET
                status     = 'failed',
                error      = excluded.error,
                updated_at = {_UTC_DEFAULT}
            """,
            (vn, lang, content_mode, str(error)[:MAX_ERROR_LENGTH]),
        )


# ═════════════════════════════════════════════════════════════════════════════
# PUBLISH TRACKING
# ═════════════════════════════════════════════════════════════════════════════

def is_published(
    video_number: str,
    lang:         str,
    platform:     str = "youtube",
    content_mode: str = "short",
) -> bool:
    _validate_lang(lang)
    _validate_platform(platform)
    _validate_mode(content_mode)
    vn = _validate_video_number(video_number)

    row = _conn().execute(
        """
        SELECT 1 FROM publish_tracker
        WHERE video_number=? AND lang=?
          AND content_mode=? AND platform=?
        """,
        (vn, lang, content_mode, platform),
    ).fetchone()
    return row is not None


def is_published_facebook(
    video_number: str,
    lang:         str,
    content_mode: str = "short",
) -> bool:
    return is_published(video_number, lang, "facebook", content_mode)


def is_published_youtube(
    video_number: str,
    lang:         str,
    content_mode: str = "short",
) -> bool:
    return is_published(video_number, lang, "youtube", content_mode)


def is_fully_published(
    video_number: str,
    lang:         str,
    content_mode: str = "short",
) -> bool:
    return (
        is_published_facebook(video_number, lang, content_mode) and
        is_published_youtube(video_number, lang, content_mode)
    )


def mark_published(
    video_number: str,
    lang:         str,
    platform:     str = "youtube",
    content_mode: str = "short",
) -> None:
    _validate_lang(lang)
    _validate_platform(platform)
    _validate_mode(content_mode)
    vn = _validate_video_number(video_number)

    with write_transaction() as c:
        c.execute(
            f"""
            INSERT OR IGNORE INTO publish_tracker
                (video_number, lang, content_mode,
                 platform, published_at)
            VALUES (?, ?, ?, ?, {_UTC_DEFAULT})
            """,
            (vn, lang, content_mode, platform),
        )


def mark_video_published_for_lang(
    video_number: str,
    lang:         str,
    platform:     str = "youtube",
    content_mode: str = "short",
) -> None:
    mark_published(video_number, lang, platform, content_mode)


def get_published_count(
    lang:         str,
    platform:     str = "youtube",
    content_mode: str = "short",
) -> int:
    _validate_lang(lang)
    _validate_platform(platform)
    _validate_mode(content_mode)

    row = _conn().execute(
        """
        SELECT COUNT(*) FROM publish_tracker
        WHERE lang=? AND platform=? AND content_mode=?
        """,
        (lang, platform, content_mode),
    ).fetchone()
    return int(row[0]) if row else 0


# ═════════════════════════════════════════════════════════════════════════════
# AUTO-NEXT
# ═════════════════════════════════════════════════════════════════════════════

def _get_published_numbers(
    lang:         str,
    content_mode: str,
    platforms:    tuple[str, ...],
) -> set[str]:
    """
    Get video numbers published on ALL specified platforms (AND logic).
    """
    c     = _conn()
    sets : list[set[str]] = []

    for platform in platforms:
        rows = c.execute(
            """
            SELECT video_number FROM publish_tracker
            WHERE lang=? AND content_mode=? AND platform=?
            """,
            (lang, content_mode, platform),
        ).fetchall()
        sets.append({str(r["video_number"]) for r in rows})

    if not sets:
        return set()

    # Intersection: published on ALL platforms
    result = sets[0]
    for s in sets[1:]:
        result = result & s
    return result


def get_next_video_number(
    lang:              str,
    available_numbers: list[str],
    content_mode:      str             = "short",
    platforms:         tuple[str, ...] = ("youtube",),
) -> Optional[str]:
    _validate_lang(lang)
    _validate_mode(content_mode)

    if not available_numbers:
        return None

    published = _get_published_numbers(lang, content_mode, platforms)

    for num in available_numbers:
        if str(num) not in published:
            return str(num)

    return None


# ═════════════════════════════════════════════════════════════════════════════
# LOOP (Reset)
# ═════════════════════════════════════════════════════════════════════════════

def reset_published_for_lang(
    lang:         str,
    content_mode: str = "short",
) -> int:
    _validate_lang(lang)
    _validate_mode(content_mode)

    with write_transaction() as c:
        cursor = c.execute(
            """
            DELETE FROM publish_tracker
            WHERE lang=? AND content_mode=?
            """,
            (lang, content_mode),
        )
        count = cursor.rowcount

    log.info(
        "  🔄 Reset %s (%s) — %d cleared",
        lang.upper(), content_mode, count
    )
    return count


# ═════════════════════════════════════════════════════════════════════════════
# DAILY QUOTA
# ═════════════════════════════════════════════════════════════════════════════

def get_today_published_count(
    lang:         str,
    content_mode: str = "short",
    platform:     str = "youtube",
) -> int:
    _validate_lang(lang)
    _validate_mode(content_mode)
    _validate_platform(platform)

    today = _today_utc_iso()

    row = _conn().execute(
        """
        SELECT COUNT(*) FROM publish_tracker
        WHERE lang=?
          AND content_mode=?
          AND platform=?
          AND substr(published_at, 1, 10) = ?
        """,
        (lang, content_mode, platform, today),
    ).fetchone()
    return int(row[0]) if row else 0


def get_today_generated_count(
    lang:         str,
    content_mode: str = "short",
) -> int:
    _validate_lang(lang)
    _validate_mode(content_mode)

    today = _today_utc_iso()

    row = _conn().execute(
        """
        SELECT COUNT(*) FROM pre_generated
        WHERE lang=?
          AND content_mode=?
          AND substr(generated_at, 1, 10) = ?
        """,
        (lang, content_mode, today),
    ).fetchone()
    return int(row[0]) if row else 0


def get_daily_quota(content_mode: str = "short") -> int:
    _validate_mode(content_mode)
    return DAILY_QUOTA.get(content_mode, 5)


def get_daily_remaining_publish(
    lang:         str,
    content_mode: str = "short",
    platform:     str = "youtube",
) -> int:
    published = get_today_published_count(lang, content_mode, platform)
    return max(0, get_daily_quota(content_mode) - published)


def get_daily_remaining_generate(
    lang:         str,
    content_mode: str = "short",
) -> int:
    generated = get_today_generated_count(lang, content_mode)
    return max(0, get_daily_quota(content_mode) - generated)


def is_daily_quota_reached(
    lang:         str,
    content_mode: str = "short",
    platform:     str = "youtube",
) -> bool:
    return get_daily_remaining_publish(lang, content_mode, platform) <= 0


def is_daily_generate_quota_reached(
    lang:         str,
    content_mode: str = "short",
) -> bool:
    return get_daily_remaining_generate(lang, content_mode) <= 0


def get_last_publish_time(
    lang:         str,
    content_mode: str = "short",
    platform:     str = "youtube",
) -> Optional[datetime]:
    """
    Last publish time.
    Handles both UTC ISO formats.
    Returns aware datetime (UTC).
    """
    _validate_lang(lang)
    _validate_mode(content_mode)
    _validate_platform(platform)

    row = _conn().execute(
        """
        SELECT published_at FROM publish_tracker
        WHERE lang=? AND content_mode=? AND platform=?
        ORDER BY published_at DESC
        LIMIT 1
        """,
        (lang, content_mode, platform),
    ).fetchone()

    if not row or not row["published_at"]:
        return None

    try:
        ts = row["published_at"]
        # Handle: "2024-01-15T10:30:00Z" and "2024-01-15 10:30:00"
        ts = ts.replace("Z", "+00:00").replace(" ", "T")
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


# ═════════════════════════════════════════════════════════════════════════════
# PRE-GENERATED VIDEOS
# ═════════════════════════════════════════════════════════════════════════════

def save_pre_generated(
    video_number: str,
    lang:         str,
    content_mode: str,
    output_path:  str,
    duration_s:   float,
    fb_path:      str           = "",
    yt_path:      str           = "",
    scheduled_at: Optional[str] = None,
    title:        str           = "",
) -> None:
    """
    Save pre-generated video.
    
    Long FB path fix:
    - Short: fb_path fallback to output_path (both portrait)
    - Long: fb_path must be separate (no fallback to landscape)
    """
    _validate_lang(lang)
    _validate_mode(content_mode)
    vn = _validate_video_number(video_number)

    # Path logic
    if content_mode == "long":
        # Long: separate paths (YT=landscape, FB=portrait)
        final_yt_path = yt_path or output_path
        final_fb_path = fb_path or output_path

        if not final_fb_path:
            log.debug(
                "  ℹ️  Long #%s: no fb_path provided",
                vn
            )
    else:
        # Short: same portrait for both
        final_yt_path = yt_path or output_path
        final_fb_path = fb_path or output_path

    with write_transaction() as c:
        c.execute(
            f"""
            INSERT INTO pre_generated
                (video_number, lang, content_mode, title,
                 output_path, fb_path, yt_path,
                 duration_s, scheduled_at, published, generated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, {_UTC_DEFAULT})
            ON CONFLICT(video_number, lang, content_mode) DO UPDATE SET
                title        = excluded.title,
                output_path  = excluded.output_path,
                fb_path      = excluded.fb_path,
                yt_path      = excluded.yt_path,
                duration_s   = excluded.duration_s,
                scheduled_at = excluded.scheduled_at,
                published    = 0,
                published_at = NULL,
                generated_at = {_UTC_DEFAULT}
            """,
            (
                vn, lang, content_mode, str(title),
                output_path,
                final_fb_path,
                final_yt_path,
                float(duration_s),
                scheduled_at,
            ),
        )


def get_ready_to_publish(
    lang:         str,
    content_mode: str = "short",
    platform:     str = "youtube",
    limit:        int = 1,
) -> list[dict]:
    """
    Videos ready to publish.
    
    Distinguishes "no videos in DB" from "files missing".
    """
    _validate_lang(lang)
    _validate_mode(content_mode)
    _validate_platform(platform)

    rows = _conn().execute(
        """
        SELECT
            pg.video_number,
            pg.lang,
            pg.content_mode,
            pg.title,
            pg.output_path,
            pg.fb_path,
            pg.yt_path,
            pg.duration_s,
            pg.scheduled_at
        FROM pre_generated pg
        WHERE pg.lang         = ?
          AND pg.content_mode = ?
          AND pg.published    = 0
          AND NOT EXISTS (
              SELECT 1 FROM publish_tracker pt
              WHERE pt.video_number = pg.video_number
                AND pt.lang         = pg.lang
                AND pt.content_mode = pg.content_mode
                AND pt.platform     = ?
          )
        ORDER BY pg.scheduled_at ASC,
                 pg.generated_at ASC
        LIMIT ?
        """,
        (lang, content_mode, platform, max(1, limit)),
    ).fetchall()

    # No rows in DB
    if not rows:
        log.debug(
            "  📭 No pre-generated videos for %s/%s/%s",
            lang, content_mode, platform
        )
        return []

    result:        list[dict] = []
    missing_files: list[str]  = []

    for r in rows:
        path = (
            r["fb_path"] if platform == "facebook"
            else r["yt_path"]
        ) or r["output_path"]

        if not path or not Path(path).exists():
            missing_files.append(str(r["video_number"]))
            continue

        result.append({
            "video_number": r["video_number"],
            "lang":         r["lang"],
            "content_mode": r["content_mode"],
            "title":        r["title"] or f"Video #{r['video_number']}",
            "output_path":  r["output_path"],
            "fb_path":      r["fb_path"],
            "yt_path":      r["yt_path"],
            "duration_s":   r["duration_s"],
            "scheduled_at": r["scheduled_at"],
            "path":         path,
        })

    # Warn about missing files
    if missing_files:
        log.error(
            "  ❌ %d pre-generated videos have MISSING FILES "
            "for %s/%s/%s: %s",
            len(missing_files),
            lang, content_mode, platform,
            ", ".join(f"#{n}" for n in missing_files[:5])
        )

        if not result:
            log.error(
                "  ❌ ALL ready videos are missing files! "
                "Check output directory."
            )

    return result


def mark_pre_generated_published(
    video_number: str,
    lang:         str,
    content_mode: str = "short",
) -> None:
    _validate_lang(lang)
    _validate_mode(content_mode)
    vn = _validate_video_number(video_number)

    with write_transaction() as c:
        c.execute(
            f"""
            UPDATE pre_generated
            SET published    = 1,
                published_at = {_UTC_DEFAULT}
            WHERE video_number=? AND lang=? AND content_mode=?
            """,
            (vn, lang, content_mode),
        )


def get_pre_generated_count(
    lang:         str,
    content_mode: str            = "short",
    published:    Optional[bool] = None,
) -> int:
    _validate_lang(lang)
    _validate_mode(content_mode)

    if published is None:
        row = _conn().execute(
            """
            SELECT COUNT(*) FROM pre_generated
            WHERE lang=? AND content_mode=?
            """,
            (lang, content_mode),
        ).fetchone()
    else:
        row = _conn().execute(
            """
            SELECT COUNT(*) FROM pre_generated
            WHERE lang=? AND content_mode=? AND published=?
            """,
            (lang, content_mode, 1 if published else 0),
        ).fetchone()

    return int(row[0]) if row else 0


# ═════════════════════════════════════════════════════════════════════════════
# PENDING PUBLISH (backward compat)
# ═════════════════════════════════════════════════════════════════════════════

def get_pending_publish(
    lang:         Optional[str] = None,
    platform:     str           = "youtube",
    content_mode: str           = "short",
) -> list[dict]:
    _validate_platform(platform)
    _validate_mode(content_mode)
    if lang:
        _validate_lang(lang)

    if lang:
        rows = _conn().execute(
            """
            SELECT r.video_number, r.lang, r.content_mode,
                   r.output_path, r.fb_path, r.yt_path
            FROM renders r
            WHERE r.status       = 'done'
              AND r.output_path  IS NOT NULL
              AND r.content_mode = ?
              AND r.lang         = ?
              AND NOT EXISTS (
                  SELECT 1 FROM publish_tracker p
                  WHERE p.video_number = r.video_number
                    AND p.lang         = r.lang
                    AND p.content_mode = r.content_mode
                    AND p.platform     = ?
              )
            ORDER BY r.video_number ASC
            """,
            (content_mode, lang, platform),
        ).fetchall()
    else:
        rows = _conn().execute(
            """
            SELECT r.video_number, r.lang, r.content_mode,
                   r.output_path, r.fb_path, r.yt_path
            FROM renders r
            WHERE r.status       = 'done'
              AND r.output_path  IS NOT NULL
              AND r.content_mode = ?
              AND NOT EXISTS (
                  SELECT 1 FROM publish_tracker p
                  WHERE p.video_number = r.video_number
                    AND p.lang         = r.lang
                    AND p.content_mode = r.content_mode
                    AND p.platform     = ?
              )
            ORDER BY r.lang, r.video_number ASC
            """,
            (content_mode, platform),
        ).fetchall()

    result: list[dict] = []
    for r in rows:
        path = (
            r["fb_path"] if platform == "facebook"
            else r["yt_path"]
        ) or r["output_path"]

        if not path or not Path(path).exists():
            continue

        result.append({
            "video_number": r["video_number"],
            "lang":         r["lang"],
            "content_mode": r["content_mode"],
            "output_path":  r["output_path"],
            "fb_path":      r["fb_path"],
            "yt_path":      r["yt_path"],
            "path":         path,
        })

    return result


# ═════════════════════════════════════════════════════════════════════════════
# SCRIPTS METADATA
# ═════════════════════════════════════════════════════════════════════════════

def save_script_meta(
    video_number: str,
    title:        str,
    lang:         str,
    sentences:    int,
    words:        int,
    content_mode: str = "short",
) -> None:
    _validate_lang(lang)
    _validate_mode(content_mode)
    vn = _validate_video_number(video_number)

    with write_transaction() as c:
        c.execute(
            f"""
            INSERT INTO scripts
                (video_number, lang, content_mode,
                 title, sentences, words)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(video_number, lang, content_mode) DO UPDATE SET
                title     = excluded.title,
                sentences = excluded.sentences,
                words     = excluded.words,
                saved_at  = {_UTC_DEFAULT}
            """,
            (vn, lang, content_mode,
             str(title), int(sentences), int(words)),
        )


# ═════════════════════════════════════════════════════════════════════════════
# AI CACHE
# ═════════════════════════════════════════════════════════════════════════════

_JSON_FIELDS: tuple[str, ...] = (
    "analysis", "power_words", "visual_keywords",
    "pattern_interrupts", "engagement_questions",
    "hashtags", "captions", "accent_colors",
    "attractive_title", "tagged",
)

_TEXT_FIELDS: tuple[str, ...] = (
    "street_description", "hook_keyword", "custom_hook",
)


def _safe_json_loads(text: Optional[str]) -> Any:
    if not text:
        return None
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None


def _safe_json_dumps(obj: Any) -> Optional[str]:
    if obj is None:
        return None
    try:
        return json.dumps(obj, ensure_ascii=False)
    except (TypeError, ValueError):
        return None


def _safe_row_get(
    row: sqlite3.Row, name: str, default: Any = ""
) -> Any:
    try:
        val = row[name]
        return val if val is not None else default
    except (IndexError, KeyError):
        return default


def has_ai_cache(cache_key: str) -> bool:
    row = _conn().execute(
        "SELECT 1 FROM ai_cache WHERE cache_key=?",
        (str(cache_key),),
    ).fetchone()
    return row is not None


def get_ai_cache(cache_key: str) -> Optional[dict]:
    row = _conn().execute(
        "SELECT * FROM ai_cache WHERE cache_key=?",
        (str(cache_key),),
    ).fetchone()

    if not row:
        return None

    result: dict = {
        "cache_key":    _safe_row_get(row, "cache_key"),
        "lang":         _safe_row_get(row, "lang",         "ar"),
        "content_mode": _safe_row_get(row, "content_mode", "short"),
        "title":        _safe_row_get(row, "title",        ""),
        "created_at":   _safe_row_get(row, "created_at",   ""),
        "updated_at":   _safe_row_get(row, "updated_at",   ""),
    }
    for field in _JSON_FIELDS:
        result[field] = _safe_json_loads(
            _safe_row_get(row, field)
        )
    for field in _TEXT_FIELDS:
        result[field] = _safe_row_get(row, field, "")

    return result


def save_ai_cache(
    cache_key:    str,
    title:        str,
    lang:         str,
    enriched:     dict,
    content_mode: str = "short",
) -> None:
    _validate_lang(lang)
    _validate_mode(content_mode)

    if not isinstance(enriched, dict):
        raise ValueError(
            f"enriched must be dict, "
            f"got {type(enriched).__name__}"
        )

    cache_key = str(cache_key).strip()
    if not cache_key:
        raise ValueError("cache_key cannot be empty")

    with write_transaction() as c:
        c.execute(
            f"""
            INSERT INTO ai_cache (
                cache_key, lang, content_mode, title,
                analysis, power_words, visual_keywords,
                pattern_interrupts, engagement_questions,
                hashtags, captions, street_description,
                accent_colors, hook_keyword, custom_hook,
                attractive_title, tagged
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(cache_key) DO UPDATE SET
                lang                 = excluded.lang,
                content_mode         = excluded.content_mode,
                title                = excluded.title,
                analysis             = excluded.analysis,
                power_words          = excluded.power_words,
                visual_keywords      = excluded.visual_keywords,
                pattern_interrupts   = excluded.pattern_interrupts,
                engagement_questions = excluded.engagement_questions,
                hashtags             = excluded.hashtags,
                captions             = excluded.captions,
                street_description   = excluded.street_description,
                accent_colors        = excluded.accent_colors,
                hook_keyword         = excluded.hook_keyword,
                custom_hook          = excluded.custom_hook,
                attractive_title     = excluded.attractive_title,
                tagged               = excluded.tagged,
                updated_at           = {_UTC_DEFAULT}
            """,
            (
                cache_key, lang, content_mode, str(title),
                _safe_json_dumps(enriched.get("analysis")),
                _safe_json_dumps(enriched.get("power_words")),
                _safe_json_dumps(enriched.get("visual_keywords")),
                _safe_json_dumps(enriched.get("pattern_interrupts")),
                _safe_json_dumps(enriched.get("engagement_questions")),
                _safe_json_dumps(enriched.get("hashtags")),
                _safe_json_dumps(enriched.get("captions")),
                enriched.get("street_description", "") or "",
                _safe_json_dumps(enriched.get("accent_colors")),
                enriched.get("hook_keyword", "") or "",
                enriched.get("custom_hook",  "") or "",
                _safe_json_dumps(enriched.get("attractive_title")),
                _safe_json_dumps(enriched.get("tagged")),
            ),
        )


def clear_ai_cache(cache_key: Optional[str] = None) -> int:
    with write_transaction() as c:
        if cache_key:
            cursor = c.execute(
                "DELETE FROM ai_cache WHERE cache_key=?",
                (str(cache_key),),
            )
        else:
            cursor = c.execute("DELETE FROM ai_cache")
        return cursor.rowcount


def show_ai_cache(cache_key: Optional[str] = None) -> None:
    if cache_key:
        cache = get_ai_cache(cache_key)
        if not cache:
            log.info("  ❌ No cache: %s", cache_key)
            return
        sep = "═" * 60
        log.info("\n  %s", sep)
        log.info("  📦 %s", cache_key)
        log.info("  %s", sep)
        analysis = cache.get("analysis") or {}
        if analysis:
            log.info(
                "  📊 %s | %s",
                analysis.get("content_type"),
                analysis.get("primary_emotion")
            )
        hook = cache.get("hook_keyword", "")
        if hook:
            log.info("  🔥 Hook: '%s'", hook)
        desc = cache.get("street_description", "")
        if desc:
            log.info("  📝 %d chars", len(desc))
        log.info(
            "  🌐 %s | 📺 %s",
            cache.get("lang", "ar").upper(),
            cache.get("content_mode", "short").upper()
        )
        log.info("  %s\n", sep)
        return

    rows = _conn().execute(
        """
        SELECT cache_key, lang, content_mode, title, created_at
        FROM ai_cache
        ORDER BY content_mode, lang, cache_key
        """
    ).fetchall()

    if not rows:
        log.info("  📭 AI Cache is empty")
        return

    sep = "═" * 80
    log.info("\n  %s", sep)
    log.info("  📦 AI Cache (%d entries)", len(rows))
    log.info("  %s", sep)
    for r in rows:
        log.info(
            "  %-18s %-4s %-6s %-28s %s",
            str(r["cache_key"])[:18],
            str(r["lang"] or "ar").upper(),
            str(r["content_mode"] or "short")[:5],
            (r["title"] or "")[:28],
            (r["created_at"] or "")[:19]
        )
    log.info("  %s\n", sep)


# ═════════════════════════════════════════════════════════════════════════════
# SUMMARY (crash-safe per query)
# ═════════════════════════════════════════════════════════════════════════════

def print_db_summary() -> None:
    """Print DB summary (crash-safe per query)."""
    try:
        c = _conn()
    except Exception as e:
        log.warning("  ⚠️  Cannot connect to DB: %s", e)
        return

    def _safe_count(query: str, default: int = 0) -> int:
        try:
            row = c.execute(query).fetchone()
            return int(row[0]) if row else default
        except Exception as e:
            log.debug("  Query error: %s", e)
            return default

    try:
        used = get_used_count()
    except Exception:
        used = 0

    done_short = _safe_count(
        "SELECT COUNT(*) FROM renders "
        "WHERE status='done' AND content_mode='short'"
    )

    done_long = _safe_count(
        "SELECT COUNT(*) FROM renders "
        "WHERE status='done' AND content_mode='long'"
    )

    failed = _safe_count(
        "SELECT COUNT(*) FROM renders WHERE status='failed'"
    )

    cached = _safe_count(
        "SELECT COUNT(*) FROM ai_cache"
    )

    pre_ready = _safe_count(
        "SELECT COUNT(*) FROM pre_generated WHERE published=0"
    )

    log.info(
        "  📊 DB: %d videos used | Short: %d ✅ | Long: %d ✅ | "
        "%d failed ❌ | AI: %d cached | Ready: %d 🎬",
        used, done_short, done_long, failed, cached, pre_ready
    )

    # Today's stats per language/mode
    try:
        today = _today_utc_iso()
        for lang in sorted(LANGS):
            for mode in sorted(MODES):
                try:
                    pub_yt = get_today_published_count(lang, mode, "youtube")
                    pub_fb = get_today_published_count(lang, mode, "facebook")
                    gen    = get_today_generated_count(lang, mode)
                    quota  = get_daily_quota(mode)

                    if pub_yt > 0 or pub_fb > 0 or gen > 0:
                        log.info(
                            "  📅 %s | %s %-5s | Gen: %d/%d | "
                            "YT: %d/%d | FB: %d/%d",
                            today,
                            lang.upper(),
                            mode,
                            gen, quota,
                            pub_yt, quota,
                            pub_fb, quota
                        )
                except Exception as e:
                    log.debug(
                        "  Stats error for %s/%s: %s",
                        lang, mode, e
                    )
    except Exception as e:
        log.debug("  Today summary error: %s", e)

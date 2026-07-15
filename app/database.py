"""SQLite 数据库管理"""

import aiosqlite
import json
import re
import unicodedata
from datetime import datetime
from pathlib import Path, PurePosixPath
from app.config import settings

DB_PATH = Path(settings.db_path)


def _normalise_media_identity_text(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return re.sub(r"[^\w]+", "", text)


def build_subscribe_media_key(
    current_quality: dict | None,
    item_name: str = "",
    item_id: str = "",
) -> str:
    """为审核项生成不依赖 Emby ID 的稳定媒体标识。"""
    current = current_quality if isinstance(current_quality, dict) else {}
    provider_ids = current.get("provider_ids") or current.get("ProviderIds") or {}
    provider_ids = provider_ids if isinstance(provider_ids, dict) else {}
    provider_lookup = {str(key).casefold(): value for key, value in provider_ids.items()}

    raw_type = str(current.get("type") or current.get("Type") or "media").strip().casefold()
    media_type = {
        "movie": "movie",
        "series": "series",
        "tv": "series",
        "episode": "series",
    }.get(raw_type, raw_type or "media")
    path = str(current.get("path") or current.get("Path") or "").strip()

    tmdb_id = str(current.get("tmdb_id") or provider_lookup.get("tmdb") or "").strip()
    if not tmdb_id and path:
        match = re.search(r"tmdbid\s*=\s*(\d+)", path, re.I)
        tmdb_id = match.group(1) if match else ""
    if tmdb_id:
        return f"{media_type}:tmdb:{tmdb_id.casefold()}"

    imdb_id = str(current.get("imdb_id") or provider_lookup.get("imdb") or "").strip().casefold()
    if not imdb_id and path:
        match = re.search(r"imdbid\s*=\s*(tt\d+)", path, re.I)
        imdb_id = match.group(1).casefold() if match else ""
    if imdb_id:
        return f"{media_type}:imdb:{imdb_id}"

    name = _normalise_media_identity_text(current.get("name") or current.get("Name") or item_name)
    year = _normalise_media_identity_text(current.get("year") or current.get("ProductionYear") or "")
    folder = ""
    if path:
        posix_path = PurePosixPath(path.replace("\\", "/"))
        leaf = posix_path.name
        folder = posix_path.parent.name if "." in leaf else leaf
        folder = _normalise_media_identity_text(folder)
    if name or year or folder:
        return f"{media_type}:meta:{name}:{year}:{folder}"

    return f"{media_type}:item:{str(item_id or '').strip()}" if item_id else ""


async def get_db() -> aiosqlite.Connection:
    """获取数据库连接"""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = await aiosqlite.connect(str(DB_PATH))
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA foreign_keys=ON")
    return db


async def init_db():
    """初始化数据库表"""
    db = await get_db()
    try:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS quality_cache (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                result_json TEXT NOT NULL,
                summary_json TEXT NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS upgrade_plans (
                id TEXT PRIMARY KEY,
                emby_item_id TEXT NOT NULL,
                current_quality_json TEXT NOT NULL,
                target_quality TEXT NOT NULL DEFAULT '2160p',
                search_results_json TEXT DEFAULT '[]',
                selected_index INTEGER DEFAULT -1,
                status TEXT DEFAULT 'pending',
                progress TEXT DEFAULT '',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS download_tasks (
                id TEXT PRIMARY KEY,
                plan_id TEXT NOT NULL,
                source TEXT NOT NULL,
                torrent_url TEXT DEFAULT '',
                status TEXT DEFAULT 'pending',
                progress REAL DEFAULT 0.0,
                message TEXT DEFAULT '',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS scan_config (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS ignore_items (
                emby_id TEXT PRIMARY KEY,
                item_name TEXT NOT NULL,
                ignored_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS subscribe_ignore (
                rule_id TEXT NOT NULL,
                item_id TEXT NOT NULL,
                item_name TEXT NOT NULL,
                ignored_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (rule_id, item_id)
            );

            CREATE TABLE IF NOT EXISTS subscribe_reviews (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                rule_id TEXT DEFAULT '',
                rule_name TEXT DEFAULT '',
                item_id TEXT DEFAULT '',
                item_name TEXT DEFAULT '',
                current_quality TEXT DEFAULT '{}',
                search_result TEXT DEFAULT '{}',
                source TEXT DEFAULT '',
                action_type TEXT DEFAULT '',
                media_key TEXT DEFAULT '',
                candidate_rank TEXT DEFAULT '[]',
                status TEXT DEFAULT 'pending',
                message TEXT DEFAULT '',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS subscribe_rules (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                data_json TEXT NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS subscribe_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                rule_id TEXT,
                rule_name TEXT,
                action TEXT NOT NULL,
                item_name TEXT DEFAULT '',
                item_id TEXT DEFAULT '',
                message TEXT DEFAULT '',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS rejected_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                item_id TEXT NOT NULL,
                result_key TEXT NOT NULL,
                result_label TEXT DEFAULT '',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_rejected_item ON rejected_results(item_id);
            CREATE INDEX IF NOT EXISTS idx_logs_item_action ON subscribe_logs(item_id, action);

            CREATE TABLE IF NOT EXISTS gap_scan_cache (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                result_json TEXT NOT NULL DEFAULT '[]',
                summary_json TEXT NOT NULL DEFAULT '{}',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS gap_ignores (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                target_type TEXT NOT NULL,
                target_id TEXT NOT NULL UNIQUE,
                series_id TEXT NOT NULL,
                series_name TEXT NOT NULL,
                season_number INTEGER DEFAULT 0,
                episode_number INTEGER DEFAULT 0,
                ignored_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_gap_ignores_series ON gap_ignores(series_id);

            CREATE TABLE IF NOT EXISTS gap_transfer_marks (
                resource_key TEXT PRIMARY KEY,
                resource_title TEXT NOT NULL DEFAULT '',
                series_name TEXT NOT NULL DEFAULT '',
                season_number INTEGER DEFAULT 0,
                episode_numbers_json TEXT NOT NULL DEFAULT '[]',
                status TEXT NOT NULL DEFAULT '',
                response_json TEXT NOT NULL DEFAULT '{}',
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_gap_transfer_marks_updated ON gap_transfer_marks(updated_at);

            CREATE TABLE IF NOT EXISTS gap_auto_fill_jobs (
                series_id TEXT NOT NULL,
                season_number INTEGER NOT NULL,
                series_name TEXT NOT NULL DEFAULT '',
                year TEXT NOT NULL DEFAULT '',
                tmdb_id INTEGER NOT NULL DEFAULT 0,
                imdb_id TEXT NOT NULL DEFAULT '',
                library_name TEXT NOT NULL DEFAULT '',
                enabled INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL DEFAULT 'waiting',
                attempts INTEGER NOT NULL DEFAULT 0,
                last_message TEXT NOT NULL DEFAULT '',
                last_resource_title TEXT NOT NULL DEFAULT '',
                last_results_json TEXT NOT NULL DEFAULT '[]',
                last_run_at DATETIME,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (series_id, season_number)
            );
            CREATE INDEX IF NOT EXISTS idx_gap_auto_fill_enabled ON gap_auto_fill_jobs(enabled, updated_at);
        """)
        cursor = await db.execute("PRAGMA table_info(subscribe_reviews)")
        review_columns = {row["name"] for row in await cursor.fetchall()}
        if "media_key" not in review_columns:
            await db.execute("ALTER TABLE subscribe_reviews ADD COLUMN media_key TEXT DEFAULT ''")
        if "candidate_rank" not in review_columns:
            await db.execute("ALTER TABLE subscribe_reviews ADD COLUMN candidate_rank TEXT DEFAULT '[]'")

        cursor = await db.execute(
            "SELECT id, item_id, item_name, current_quality FROM subscribe_reviews WHERE COALESCE(media_key, '') = ''"
        )
        for row in await cursor.fetchall():
            try:
                current_quality = json.loads(row["current_quality"] or "{}")
            except (TypeError, json.JSONDecodeError):
                current_quality = {}
            media_key = build_subscribe_media_key(current_quality, row["item_name"], row["item_id"])
            if media_key:
                await db.execute(
                    "UPDATE subscribe_reviews SET media_key = ? WHERE id = ?",
                    (media_key, row["id"]),
                )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_subscribe_reviews_media "
            "ON subscribe_reviews(rule_id, media_key, status)"
        )

        cursor = await db.execute("PRAGMA table_info(gap_auto_fill_jobs)")
        auto_fill_columns = {row["name"] for row in await cursor.fetchall()}
        if "last_results_json" not in auto_fill_columns:
            await db.execute(
                "ALTER TABLE gap_auto_fill_jobs ADD COLUMN last_results_json TEXT NOT NULL DEFAULT '[]'"
            )
        await db.execute(
            """
            UPDATE gap_auto_fill_jobs
            SET status = 'waiting', updated_at = datetime('now','localtime')
            WHERE enabled = 1 AND status IN ('searching', 'transferring')
            """
        )
        await db.commit()
    finally:
        await db.close()


# ─── 质量缓存 ───

async def save_quality_cache(items: list[dict], summary: dict):
    """保存质量扫描缓存"""
    db = await get_db()
    try:
        await db.execute(
            "INSERT OR REPLACE INTO quality_cache (id, result_json, summary_json, created_at) VALUES (1, ?, ?, datetime('now','localtime'))",
            (json.dumps(items, ensure_ascii=False), json.dumps(summary, ensure_ascii=False))
        )
        await db.commit()
    finally:
        await db.close()


async def load_quality_cache() -> tuple[list[dict], dict]:
    """加载质量扫描缓存"""
    db = await get_db()
    try:
        cursor = await db.execute("SELECT result_json, summary_json, created_at FROM quality_cache WHERE id = 1")
        row = await cursor.fetchone()
        if row:
            return json.loads(row["result_json"]), json.loads(row["summary_json"]), row["created_at"]
        return [], {}, None
    finally:
        await db.close()


async def get_quality_cache_age() -> dict | None:
    """获取缓存年龄信息（分钟），无缓存返回 None"""
    db = await get_db()
    try:
        cursor = await db.execute("SELECT created_at FROM quality_cache WHERE id = 1")
        row = await cursor.fetchone()
        if not row:
            return None
        created_at = row["created_at"]
        # 计算分钟差
        from datetime import datetime
        now = datetime.now()
        try:
            cached_time = datetime.strptime(created_at, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            cached_time = datetime.strptime(created_at, "%Y-%m-%d %H:%M:%S.%f")
        age_minutes = int((now - cached_time).total_seconds() / 60)
        return {
            "created_at": created_at,
            "age_minutes": age_minutes,
            "is_fresh": age_minutes < 60,
        }
    finally:
        await db.close()


def parse_cache_time(created_at: str) -> datetime | None:
    """将缓存时间字符串解析为 datetime 对象"""
    from datetime import datetime
    try:
        return datetime.strptime(created_at, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        try:
            return datetime.strptime(created_at, "%Y-%m-%d %H:%M:%S.%f")
        except ValueError:
            return None


# ─── 洗版计划 ───

async def save_upgrade_plan(plan: dict):
    """保存洗版计划"""
    db = await get_db()
    try:
        await db.execute("""
            INSERT OR REPLACE INTO upgrade_plans
            (id, emby_item_id, current_quality_json, target_quality, search_results_json, selected_index, status, progress, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now','localtime'))
        """, (
            plan["id"], plan["emby_item_id"],
            json.dumps(plan.get("current_quality", {}), ensure_ascii=False),
            plan.get("target_quality", "2160p"),
            json.dumps(plan.get("search_results", []), ensure_ascii=False),
            plan.get("selected_index", -1),
            plan.get("status", "pending"),
            plan.get("progress", "")
        ))
        await db.commit()
    finally:
        await db.close()


async def list_upgrade_plans(status: str = "") -> list[dict]:
    """列出洗版计划"""
    db = await get_db()
    try:
        if status:
            cursor = await db.execute(
                "SELECT * FROM upgrade_plans WHERE status = ? ORDER BY created_at DESC", (status,)
            )
        else:
            cursor = await db.execute(
                "SELECT * FROM upgrade_plans ORDER BY created_at DESC"
            )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]
    finally:
        await db.close()


async def get_upgrade_plan(plan_id: str) -> dict | None:
    """获取单个洗版计划"""
    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM upgrade_plans WHERE id = ?", (plan_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None
    finally:
        await db.close()


async def update_upgrade_plan_status(plan_id: str, status: str, progress: str = "") -> bool:
    """只更新洗版计划状态，保留现有质量/搜索 JSON 字段。"""
    db = await get_db()
    try:
        cursor = await db.execute(
            """
            UPDATE upgrade_plans
            SET status = ?, progress = ?, updated_at = datetime('now','localtime')
            WHERE id = ?
            """,
            (status, progress, plan_id),
        )
        await db.commit()
        return cursor.rowcount > 0
    finally:
        await db.close()


# ─── 订阅规则 ───

async def save_subscribe_rule(rule: dict):
    """保存订阅规则"""
    db = await get_db()
    try:
        data = {k: v for k, v in rule.items() if k != "id"}
        await db.execute("""
            INSERT OR REPLACE INTO subscribe_rules
            (id, name, data_json, updated_at)
            VALUES (?, ?, ?, datetime('now','localtime'))
        """, (rule["id"], rule.get("name", ""), json.dumps(data, ensure_ascii=False)))
        await db.commit()
    finally:
        await db.close()


async def list_subscribe_rules() -> list[dict]:
    """列出所有订阅规则"""
    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM subscribe_rules ORDER BY created_at ASC")
        rows = await cursor.fetchall()
        rules = []
        for r in rows:
            rd = dict(r)
            data = json.loads(rd.pop("data_json"))
            rd.update(data)
            rules.append(rd)
        return rules
    finally:
        await db.close()


async def get_subscribe_rule(rule_id: str) -> dict | None:
    """获取单个订阅规则"""
    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM subscribe_rules WHERE id = ?", (rule_id,))
        row = await cursor.fetchone()
        if row:
            rd = dict(row)
            data = json.loads(rd.pop("data_json"))
            rd.update(data)
            return rd
        return None
    finally:
        await db.close()


async def delete_subscribe_rule(rule_id: str) -> bool:
    """删除订阅规则"""
    db = await get_db()
    try:
        cursor = await db.execute("DELETE FROM subscribe_rules WHERE id = ?", (rule_id,))
        await db.commit()
        return cursor.rowcount > 0
    finally:
        await db.close()


async def add_subscribe_log(rule_id: str, rule_name: str, action: str, item_name: str = "", item_id: str = "", message: str = ""):
    """添加订阅执行日志，同 item_id+action 已存在时合并（只保留最新）"""
    db = await get_db()
    try:
        # 检查是否已有同 item_id + action 的记录
        cursor = await db.execute(
            "SELECT id FROM subscribe_logs WHERE item_id = ? AND action = ? AND item_id != ''",
            (item_id, action)
        )
        existing = await cursor.fetchone()
        if existing:
            await db.execute(
                "UPDATE subscribe_logs SET rule_id = ?, rule_name = ?, item_name = ?, message = ?, created_at = datetime('now','localtime') WHERE id = ?",
                (rule_id, rule_name, item_name, message, existing["id"])
            )
        else:
            await db.execute(
                "INSERT INTO subscribe_logs (rule_id, rule_name, action, item_name, item_id, message, created_at) VALUES (?, ?, ?, ?, ?, ?, datetime('now','localtime'))",
                (rule_id, rule_name, action, item_name, item_id, message)
            )
        await db.commit()
    finally:
        await db.close()


async def list_subscribe_logs(limit: int = 50) -> list[dict]:
    """列出订阅执行日志"""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT * FROM subscribe_logs ORDER BY created_at DESC LIMIT ?", (limit,)
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]
    finally:
        await db.close()


async def get_subscribe_logs_for_item(item_id: str, action: str = "") -> list[dict]:
    """检查某条目是否已被某操作处理过"""
    db = await get_db()
    try:
        if action:
            cursor = await db.execute(
                "SELECT * FROM subscribe_logs WHERE item_id = ? AND action = ? ORDER BY created_at DESC LIMIT 10",
                (item_id, action)
            )
        else:
            cursor = await db.execute(
                "SELECT * FROM subscribe_logs WHERE item_id = ? ORDER BY created_at DESC LIMIT 10",
                (item_id,)
            )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]
    finally:
        await db.close()


# ─── 忽略列表 ───

async def remove_item_from_cache(emby_id: str) -> bool:
    """从质量缓存中移除某个条目（当它在 Emby 中已删除时）"""
    items, summary, _ = await load_quality_cache()
    if not items:
        return False
    before = len(items)
    items = [item for item in items if item.get("emby_id") != emby_id]
    if len(items) < before:
        # 更新 summary 的 total_count
        summary["total_count"] = len(items)
        db = await get_db()
        try:
            await db.execute(
                "INSERT OR REPLACE INTO quality_cache (id, result_json, summary_json, created_at) VALUES (1, ?, ?, datetime('now','localtime'))",
                (json.dumps(items, ensure_ascii=False), json.dumps(summary, ensure_ascii=False))
            )
            await db.commit()
        finally:
            await db.close()
        return True
    return False


async def add_ignore_item(emby_id: str, name: str):
    """添加忽略项"""
    db = await get_db()
    try:
        await db.execute(
            "INSERT OR IGNORE INTO ignore_items (emby_id, item_name) VALUES (?, ?)",
            (emby_id, name)
        )
        await db.commit()
    finally:
        await db.close()


async def get_ignore_ids() -> set[str]:
    """获取已忽略的 ID 集合"""
    db = await get_db()
    try:
        cursor = await db.execute("SELECT emby_id FROM ignore_items")
        rows = await cursor.fetchall()
        return {r["emby_id"] for r in rows}
    finally:
        await db.close()


async def get_subscribe_logs(limit: int = 20) -> list[dict]:
    """获取最近的活动日志（供 TG /logs 使用）"""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT id, rule_name, action, item_name, message, created_at "
            "FROM subscribe_logs ORDER BY id DESC LIMIT ?",
            (limit,)
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]
    finally:
        await db.close()


async def has_processed_item(rule_id: str, item_id: str) -> str | None:
    """检查某条目是否已被处理过（被任何规则处理过都算）
    返回: None=未处理, 'pending_review'=待审核, 'submitted_download'=下载已提交但未验证,
          'rejected'=已拒绝（仍可搜索但需排除旧结果）, 'done'=已完成(transfer/已确认转存/approved/ignored)
    failed 不算完成，允许下次规则运行时重新生成审核项并重试。
    """
    db = await get_db()
    try:
        # 1. 检查 subscribe_logs 是否已有成功记录。
        # MoviePilot 的 download/MP auto_approved 只代表已推送下载，不等于媒体库已升级。
        cursor = await db.execute(
            """
            SELECT action, message FROM subscribe_logs
            WHERE item_id = ? AND action IN ('download','transfer','auto_approved')
            ORDER BY created_at DESC LIMIT 10
            """,
            (item_id,)
        )
        rows = await cursor.fetchall()
        for row in rows:
            action = row["action"]
            message = row["message"] or ""
            if action == "transfer":
                return 'done'
            if action == "auto_approved":
                if "115" in message or "转存" in message or "已在 115" in message:
                    return 'done'
                return 'submitted_download'
            if action == "download":
                return 'submitted_download'

        # 2. 检查 subscribe_reviews 状态
        cursor = await db.execute(
            "SELECT status FROM subscribe_reviews WHERE item_id = ? ORDER BY created_at DESC LIMIT 1",
            (item_id,)
        )
        row = await cursor.fetchone()
        if row:
            st = row[0]
            if st == 'pending':
                return 'pending_review'
            if st == 'rejected':
                return 'rejected'
            if st == 'failed':
                return None
            if st in ('approved', 'ignored'):
                return 'done'

        return None
    finally:
        await db.close()


async def add_rejected_result(item_id: str, result_key: str, result_label: str = ""):
    """记录被拒绝的搜索结果，下次搜索时排除"""
    db = await get_db()
    try:
        await db.execute(
            "INSERT OR IGNORE INTO rejected_results (item_id, result_key, result_label) VALUES (?, ?, ?)",
            (item_id, result_key, result_label)
        )
        await db.commit()
    finally:
        await db.close()


async def get_rejected_result_keys(item_id: str) -> list[str]:
    """获取某条目所有被拒绝的结果标识"""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT result_key FROM rejected_results WHERE item_id = ?",
            (item_id,)
        )
        rows = await cursor.fetchall()
        return [r[0] for r in rows]
    finally:
        await db.close()


# ─── 订阅审核 ───


def _parse_candidate_rank(value: object) -> list[float]:
    if isinstance(value, str):
        try:
            value = json.loads(value or "[]")
        except (TypeError, json.JSONDecodeError):
            value = []
    if not isinstance(value, (list, tuple)):
        return []

    rank: list[float] = []
    for part in value:
        try:
            rank.append(float(part))
        except (TypeError, ValueError):
            return []
    return rank


def _candidate_rank_order(value: object, row_id: int = 0) -> tuple:
    rank = _parse_candidate_rank(value)
    return (0, tuple(rank), -int(row_id or 0)) if rank else (1, (), -int(row_id or 0))


def _subscribe_result_key(result: dict | None) -> str:
    result = result if isinstance(result, dict) else {}
    for key in ("hdhive_slug", "page_url", "resource_url", "slug", "enclosure"):
        value = str(result.get(key) or "").strip()
        if value:
            return f"{key}:{value}"
    return ""


async def _insert_subscribe_review_row(
    db: aiosqlite.Connection,
    review: dict,
    media_key: str,
    candidate_rank: list[float],
) -> int:
    cursor = await db.execute("""
        INSERT INTO subscribe_reviews (
            rule_id, rule_name, item_id, item_name, current_quality, search_result,
            source, action_type, media_key, candidate_rank, status, message
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
    """, (
        review.get("rule_id", ""),
        review.get("rule_name", ""),
        review.get("item_id", ""),
        review.get("item_name", ""),
        json.dumps(review.get("current_quality", {}), ensure_ascii=False),
        json.dumps(review.get("search_result", {}), ensure_ascii=False),
        review.get("source", ""),
        review.get("action_type", ""),
        media_key,
        json.dumps(candidate_rank),
        review.get("message", ""),
    ))
    return int(cursor.lastrowid)


async def _delete_pending_review_ids(db: aiosqlite.Connection, review_ids: list[int]):
    ids = [int(review_id) for review_id in review_ids if review_id]
    if not ids:
        return
    placeholders = ",".join("?" for _ in ids)
    await db.execute(
        f"DELETE FROM subscribe_reviews WHERE status = 'pending' AND id IN ({placeholders})",
        ids,
    )


async def delete_pending_subscribe_reviews(review_ids: list[int]) -> int:
    """删除指定的待审核记录，已处理记录不受影响。"""
    ids: list[int] = []
    for review_id in review_ids:
        try:
            value = int(review_id)
        except (TypeError, ValueError):
            continue
        if value > 0 and value not in ids:
            ids.append(value)
    if not ids:
        return 0

    db = await get_db()
    try:
        await db.execute("BEGIN IMMEDIATE")
        placeholders = ",".join("?" for _ in ids)
        cursor = await db.execute(
            f"DELETE FROM subscribe_reviews WHERE status = 'pending' AND id IN ({placeholders})",
            ids,
        )
        await db.commit()
        return max(0, int(cursor.rowcount or 0))
    except Exception:
        await db.rollback()
        raise
    finally:
        await db.close()


async def add_subscribe_review(review: dict):
    """添加审核条目。

    同一规则、同一逻辑媒体只保留候选排名最优的一条；相同资源只刷新执行信息。
    approved/ignored 视为完成，rejected/failed 允许再次生成审核项。
    """
    db = await get_db()
    try:
        await db.execute("BEGIN IMMEDIATE")
        rule_id = str(review.get("rule_id") or "")
        item_id = str(review.get("item_id") or "")
        current_quality = review.get("current_quality") or {}
        media_key = str(review.get("media_key") or "") or build_subscribe_media_key(
            current_quality,
            str(review.get("item_name") or ""),
            item_id,
        )
        candidate_rank = _parse_candidate_rank(review.get("candidate_rank"))

        # 已完成的同一 Emby 条目沿用原有行为，不重新生成审核。
        cursor = await db.execute(
            "SELECT id, status FROM subscribe_reviews WHERE rule_id = ? AND item_id = ? ORDER BY created_at DESC LIMIT 1",
            (rule_id, item_id)
        )
        existing = await cursor.fetchone()
        if existing:
            existing_id, existing_status = existing
            if existing_status in ("approved", "ignored"):
                print(f"[DB] add_subscribe_review: 跳过已处理的条目 rule_id={rule_id} item_id={item_id} (status={existing_status})")
                await db.rollback()
                return None
            if existing_status in ("rejected", "failed"):
                print(f"[DB] add_subscribe_review: 允许重试 rule_id={rule_id} item_id={item_id} (latest status={existing_status})")

        if media_key:
            cursor = await db.execute(
                """
                SELECT * FROM subscribe_reviews
                WHERE rule_id = ? AND status = 'pending'
                  AND (media_key = ? OR (COALESCE(media_key, '') = '' AND item_id = ?))
                ORDER BY id DESC
                """,
                (rule_id, media_key, item_id),
            )
        else:
            cursor = await db.execute(
                """
                SELECT * FROM subscribe_reviews
                WHERE rule_id = ? AND item_id = ? AND status = 'pending'
                ORDER BY id DESC
                """,
                (rule_id, item_id),
            )
        pending_rows = await cursor.fetchall()

        if not pending_rows:
            review_id = await _insert_subscribe_review_row(db, review, media_key, candidate_rank)
            await db.commit()
            return review_id

        best_existing = min(
            pending_rows,
            key=lambda row: _candidate_rank_order(row["candidate_rank"], row["id"]),
        )
        best_rank = _parse_candidate_rank(best_existing["candidate_rank"])
        try:
            best_result = json.loads(best_existing["search_result"] or "{}")
        except (TypeError, json.JSONDecodeError):
            best_result = {}
        candidate_result = review.get("search_result") or {}
        same_resource = bool(
            _subscribe_result_key(candidate_result)
            and _subscribe_result_key(candidate_result) == _subscribe_result_key(best_result)
        )
        duplicate_ids = [row["id"] for row in pending_rows if row["id"] != best_existing["id"]]

        if same_resource:
            await db.execute(
                """
                UPDATE subscribe_reviews
                SET rule_name = ?, item_id = ?, item_name = ?, current_quality = ?,
                    search_result = ?, source = ?, action_type = ?, media_key = ?,
                    candidate_rank = ?, message = ?, updated_at = datetime('now','localtime')
                WHERE id = ?
                """,
                (
                    review.get("rule_name", ""),
                    item_id,
                    review.get("item_name", ""),
                    json.dumps(current_quality, ensure_ascii=False),
                    json.dumps(candidate_result, ensure_ascii=False),
                    review.get("source", ""),
                    review.get("action_type", ""),
                    media_key,
                    json.dumps(candidate_rank),
                    review.get("message", ""),
                    best_existing["id"],
                ),
            )
            await _delete_pending_review_ids(db, duplicate_ids)
            await db.commit()
            return None

        candidate_is_better = bool(candidate_rank) and (
            not best_rank or tuple(candidate_rank) < tuple(best_rank)
        )
        if candidate_is_better:
            review_id = await _insert_subscribe_review_row(db, review, media_key, candidate_rank)
            await _delete_pending_review_ids(db, [row["id"] for row in pending_rows])
            await db.commit()
            return review_id

        # 旧候选更好时保留其资源，但同步最新 Emby ID 和当前质量，避免审核执行引用旧条目。
        await db.execute(
            """
            UPDATE subscribe_reviews
            SET item_id = ?, item_name = ?, current_quality = ?, media_key = ?,
                updated_at = datetime('now','localtime')
            WHERE id = ?
            """,
            (
                item_id,
                review.get("item_name", ""),
                json.dumps(current_quality, ensure_ascii=False),
                media_key,
                best_existing["id"],
            ),
        )
        await _delete_pending_review_ids(db, duplicate_ids)
        await db.commit()
        return None
    except Exception:
        await db.rollback()
        raise
    finally:
        await db.close()


async def reconcile_pending_subscribe_reviews(metadata: list[dict]) -> int:
    """按已计算的媒体键和候选排名整理历史 pending 记录，返回删除数量。"""
    metadata_by_id = {
        int(item["id"]): item
        for item in metadata
        if isinstance(item, dict) and item.get("id") is not None
    }
    db = await get_db()
    try:
        await db.execute("BEGIN IMMEDIATE")
        for review_id, item in metadata_by_id.items():
            media_key = str(item.get("media_key") or "")
            candidate_rank = _parse_candidate_rank(item.get("candidate_rank"))
            await db.execute(
                "UPDATE subscribe_reviews SET media_key = ?, candidate_rank = ? WHERE id = ? AND status = 'pending'",
                (media_key, json.dumps(candidate_rank), review_id),
            )

        cursor = await db.execute(
            """
            SELECT id, rule_id, item_id, item_name, current_quality, media_key, candidate_rank
            FROM subscribe_reviews WHERE status = 'pending' ORDER BY id DESC
            """
        )
        groups: dict[tuple[str, str], list[aiosqlite.Row]] = {}
        for row in await cursor.fetchall():
            media_key = str(row["media_key"] or "")
            if not media_key:
                continue
            groups.setdefault((str(row["rule_id"] or ""), media_key), []).append(row)

        deleted = 0
        for rows in groups.values():
            if len(rows) < 2:
                continue
            best = min(
                rows,
                key=lambda row: _candidate_rank_order(row["candidate_rank"], row["id"]),
            )
            latest = max(rows, key=lambda row: int(row["id"]))
            await db.execute(
                """
                UPDATE subscribe_reviews
                SET item_id = ?, item_name = ?, current_quality = ?, media_key = ?,
                    updated_at = datetime('now','localtime')
                WHERE id = ?
                """,
                (
                    latest["item_id"],
                    latest["item_name"],
                    latest["current_quality"],
                    latest["media_key"],
                    best["id"],
                ),
            )
            duplicate_ids = [row["id"] for row in rows if row["id"] != best["id"]]
            await _delete_pending_review_ids(db, duplicate_ids)
            deleted += len(duplicate_ids)

        await db.commit()
        return deleted
    except Exception:
        await db.rollback()
        raise
    finally:
        await db.close()


async def list_subscribe_reviews(status: str = "pending") -> list[dict]:
    """列出审核条目，按质量评分降序排列
    status: 'pending' | 'approved' | 'rejected' | 'ignored' | 'failed' | 'history'(=所有非pending)
    """
    db = await get_db()
    try:
        if status == "history":
            cursor = await db.execute(
                "SELECT * FROM subscribe_reviews WHERE status != 'pending' ORDER BY created_at DESC"
            )
        elif status:
            cursor = await db.execute(
                "SELECT * FROM subscribe_reviews WHERE status = ? ORDER BY created_at DESC", (status,)
            )
        else:
            cursor = await db.execute(
                "SELECT * FROM subscribe_reviews ORDER BY created_at DESC"
            )
        rows = await cursor.fetchall()
        results = []
        for r in rows:
            rd = dict(r)
            # 解析 JSON 字段
            for key in ("current_quality", "search_result"):
                try:
                    rd[key] = json.loads(rd.get(key, "{}"))
                except:
                    rd[key] = {}
            results.append(rd)
        # 按当前质量评分降序排列（评分越低越差，优先洗版）
        results.sort(key=lambda x: (x.get("current_quality", {}) or {}).get("quality_score", 0))
        return results
    finally:
        await db.close()


async def get_subscribe_review(review_id: int) -> dict | None:
    """按自增 ID 获取单条审核记录，包含已处理记录。"""
    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM subscribe_reviews WHERE id = ?", (review_id,))
        row = await cursor.fetchone()
        if not row:
            return None
        review = dict(row)
        for key in ("current_quality", "search_result"):
            try:
                review[key] = json.loads(review.get(key, "{}"))
            except Exception:
                review[key] = {}
        return review
    finally:
        await db.close()


async def update_subscribe_review(review_id: int, status: str, message: str = ""):
    """更新审核状态"""
    db = await get_db()
    try:
        await db.execute(
            "UPDATE subscribe_reviews SET status = ?, message = ?, updated_at = datetime('now','localtime') WHERE id = ?",
            (status, message, review_id)
        )
        await db.commit()
    finally:
        await db.close()


async def count_pending_reviews() -> int:
    """统计待审核数量"""
    db = await get_db()
    try:
        cursor = await db.execute("SELECT COUNT(*) as cnt FROM subscribe_reviews WHERE status = 'pending'")
        row = await cursor.fetchone()
        return row["cnt"] if row else 0
    finally:
        await db.close()


async def clear_subscribe_reviews():
    """清空所有订阅审核记录"""
    db = await get_db()
    try:
        cursor = await db.execute("SELECT COUNT(*) as cnt FROM subscribe_reviews")
        row = await cursor.fetchone()
        total = row["cnt"] if row else 0
        await db.execute("DELETE FROM subscribe_reviews")
        await db.commit()
        return total
    finally:
        await db.close()


async def delete_subscribe_history(item_id: str) -> dict:
    """删除指定条目的所有历史记录（日志+审核+忽略+拒绝结果），恢复可搜索状态
    返回: {"logs": n, "reviews": n, "ignores": n, "plans": n}
    """
    db = await get_db()
    try:
        # 删除日志
        c1 = await db.execute("DELETE FROM subscribe_logs WHERE item_id = ?", (item_id,))
        deleted_logs = c1.rowcount

        # 删除审核记录
        c2 = await db.execute("DELETE FROM subscribe_reviews WHERE item_id = ?", (item_id,))
        deleted_reviews = c2.rowcount

        # 删除忽略记录
        c3 = await db.execute("DELETE FROM subscribe_ignore WHERE item_id = ?", (item_id,))
        deleted_ignores = c3.rowcount

        # 删除拒绝结果记录
        await db.execute("DELETE FROM rejected_results WHERE item_id = ?", (item_id,))

        # 删除手动洗版计划
        c4 = await db.execute("DELETE FROM upgrade_plans WHERE emby_item_id = ?", (item_id,))
        deleted_plans = c4.rowcount

        await db.commit()
        return {"logs": deleted_logs, "reviews": deleted_reviews, "ignores": deleted_ignores, "plans": deleted_plans}
    finally:
        await db.close()


# ─── 订阅忽略 ───

async def add_subscribe_ignore(rule_id: str, item_id: str, item_name: str):
    """添加订阅忽略项"""
    db = await get_db()
    try:
        await db.execute(
            "INSERT OR IGNORE INTO subscribe_ignore (rule_id, item_id, item_name) VALUES (?, ?, ?)",
            (rule_id, item_id, item_name)
        )
        await db.commit()
    finally:
        await db.close()


async def remove_subscribe_ignore(rule_id: str, item_id: str):
    """移除订阅忽略项"""
    db = await get_db()
    try:
        await db.execute(
            "DELETE FROM subscribe_ignore WHERE rule_id = ? AND item_id = ?",
            (rule_id, item_id)
        )
        await db.commit()
    finally:
        await db.close()


async def list_subscribe_ignores(rule_id: str = "") -> list[dict]:
    """列出订阅忽略项，可筛选规则"""
    db = await get_db()
    try:
        if rule_id:
            cursor = await db.execute(
                "SELECT * FROM subscribe_ignore WHERE rule_id = ? ORDER BY ignored_at DESC", (rule_id,)
            )
        else:
            cursor = await db.execute(
                "SELECT * FROM subscribe_ignore ORDER BY ignored_at DESC"
            )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]
    finally:
        await db.close()


async def get_subscribe_ignore_ids(rule_id: str) -> set[str]:
    """获取指定规则下已忽略的 item_id 集合"""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT item_id FROM subscribe_ignore WHERE rule_id = ?", (rule_id,)
        )
        rows = await cursor.fetchall()
        return {r["item_id"] for r in rows}
    finally:
        await db.close()


# ─── 缺集管理 ───

def _default_gap_config() -> dict:
    excluded = [x.strip() for x in settings.exclude_library_ids.split(",") if x.strip()]
    return {
        "excluded_libraries": excluded,
        "cache_interval_hours": 6,
        "auto_fill_enabled": False,
    }


async def get_gap_config() -> dict:
    """读取缺集管理配置。"""
    defaults = _default_gap_config()
    db = await get_db()
    try:
        cursor = await db.execute(
            """
            SELECT key, value FROM scan_config
            WHERE key IN ('gaps_excluded_libraries', 'gaps_cache_interval_hours', 'gaps_auto_fill_enabled')
            """
        )
        rows = await cursor.fetchall()
        data = {row["key"]: row["value"] for row in rows}

        excluded_raw = data.get("gaps_excluded_libraries")
        if excluded_raw:
            try:
                excluded = json.loads(excluded_raw)
                if not isinstance(excluded, list):
                    excluded = defaults["excluded_libraries"]
            except json.JSONDecodeError:
                excluded = [x.strip() for x in excluded_raw.split(",") if x.strip()]
        else:
            excluded = defaults["excluded_libraries"]

        try:
            cache_hours = int(data.get("gaps_cache_interval_hours") or defaults["cache_interval_hours"])
        except (TypeError, ValueError):
            cache_hours = defaults["cache_interval_hours"]

        auto_fill_enabled = str(data.get("gaps_auto_fill_enabled") or "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }

        return {
            "excluded_libraries": [str(x) for x in excluded if str(x).strip()],
            "cache_interval_hours": max(1, cache_hours),
            "auto_fill_enabled": auto_fill_enabled,
        }
    finally:
        await db.close()


async def save_gap_config(excluded_libraries: list[str], cache_interval_hours: int = 6):
    """保存缺集管理配置。"""
    db = await get_db()
    try:
        await db.execute(
            "INSERT OR REPLACE INTO scan_config (key, value) VALUES ('gaps_excluded_libraries', ?)",
            (json.dumps(excluded_libraries, ensure_ascii=False),),
        )
        await db.execute(
            "INSERT OR REPLACE INTO scan_config (key, value) VALUES ('gaps_cache_interval_hours', ?)",
            (str(max(1, int(cache_interval_hours or 6))),),
        )
        await db.commit()
    finally:
        await db.close()


async def get_gap_auto_fill_enabled() -> bool:
    """读取全局影巢自动补季开关。"""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT value FROM scan_config WHERE key = 'gaps_auto_fill_enabled'"
        )
        row = await cursor.fetchone()
        return str(row["value"] if row else "").strip().lower() in {"1", "true", "yes", "on"}
    finally:
        await db.close()


async def set_gap_auto_fill_enabled(enabled: bool):
    """保存全局影巢自动补季开关。"""
    db = await get_db()
    try:
        await db.execute(
            "INSERT OR REPLACE INTO scan_config (key, value) VALUES ('gaps_auto_fill_enabled', ?)",
            ("1" if enabled else "0",),
        )
        await db.commit()
    finally:
        await db.close()


async def sync_gap_auto_fill_jobs(
    items: list[dict],
    reset_completed: bool = False,
    prune_missing: bool = False,
) -> int:
    """批量同步当前整季缺口为自动补季任务。"""
    db = await get_db()
    synced = 0
    seen: set[tuple[str, int]] = set()
    try:
        for item in items:
            series_id = str(item.get("series_id") or "").strip()
            try:
                season_number = int(item.get("season_number") or 0)
            except (TypeError, ValueError):
                continue
            key = (series_id, season_number)
            if not series_id or key in seen:
                continue
            seen.add(key)
            try:
                tmdb_id = int(item.get("tmdb_id") or 0)
            except (TypeError, ValueError):
                tmdb_id = 0

            if reset_completed:
                conflict_updates = """
                    enabled = 1,
                    status = 'waiting'
                """
            else:
                conflict_updates = """
                    enabled = CASE
                        WHEN gap_auto_fill_jobs.status = 'completed' THEN gap_auto_fill_jobs.enabled
                        ELSE 1
                    END,
                    status = CASE
                        WHEN gap_auto_fill_jobs.status = 'completed' THEN gap_auto_fill_jobs.status
                        ELSE 'waiting'
                    END
                """

            await db.execute(
                f"""
                INSERT INTO gap_auto_fill_jobs
                (
                    series_id, season_number, series_name, year, tmdb_id, imdb_id, library_name,
                    enabled, status, last_message, last_resource_title, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, 1, 'waiting', '', '', datetime('now','localtime'))
                ON CONFLICT(series_id, season_number) DO UPDATE SET
                    series_name = excluded.series_name,
                    year = excluded.year,
                    tmdb_id = excluded.tmdb_id,
                    imdb_id = excluded.imdb_id,
                    library_name = excluded.library_name,
                    {conflict_updates},
                    updated_at = datetime('now','localtime')
                """,
                (
                    series_id,
                    season_number,
                    str(item.get("series_name") or ""),
                    str(item.get("year") or ""),
                    tmdb_id,
                    str(item.get("imdb_id") or ""),
                    str(item.get("library_name") or ""),
                ),
            )
            synced += 1

        if prune_missing:
            cursor = await db.execute(
                """
                SELECT
                    series_id, season_number, last_resource_title, last_results_json
                FROM gap_auto_fill_jobs
                WHERE enabled = 1
                """
            )
            for row in await cursor.fetchall():
                key = (str(row["series_id"] or ""), int(row["season_number"] or 0))
                if key in seen:
                    continue
                try:
                    saved_results = json.loads(row["last_results_json"] or "[]")
                except (TypeError, json.JSONDecodeError):
                    saved_results = []
                if row["last_resource_title"] or (isinstance(saved_results, list) and saved_results):
                    await db.execute(
                        """
                        UPDATE gap_auto_fill_jobs
                        SET
                            enabled = 0,
                            status = 'disabled',
                            last_message = '当前完整扫描已不再纳入自动补季，任务自动停止',
                            updated_at = datetime('now','localtime')
                        WHERE series_id = ? AND season_number = ?
                        """,
                        key,
                    )
                else:
                    await db.execute(
                        "DELETE FROM gap_auto_fill_jobs WHERE series_id = ? AND season_number = ?",
                        key,
                    )
        await db.commit()
        return synced
    finally:
        await db.close()


async def disable_gap_auto_fill_jobs(message: str = "已关闭全局自动补季") -> int:
    """关闭所有尚未完成的自动补季任务。"""
    db = await get_db()
    try:
        cursor = await db.execute(
            """
            UPDATE gap_auto_fill_jobs
            SET enabled = 0,
                status = 'disabled',
                last_message = ?,
                updated_at = datetime('now','localtime')
            WHERE enabled = 1
            """,
            (str(message or ""),),
        )
        await db.commit()
        return max(0, int(cursor.rowcount or 0))
    finally:
        await db.close()


async def save_gap_cache(results: list[dict], summary: dict):
    """保存缺集扫描缓存。"""
    db = await get_db()
    try:
        await db.execute(
            """
            INSERT OR REPLACE INTO gap_scan_cache (id, result_json, summary_json, created_at)
            VALUES (1, ?, ?, datetime('now','localtime'))
            """,
            (json.dumps(results, ensure_ascii=False), json.dumps(summary, ensure_ascii=False)),
        )
        await db.commit()
    finally:
        await db.close()


async def load_gap_cache() -> tuple[list[dict], dict, str | None]:
    """读取缺集扫描缓存。"""
    db = await get_db()
    try:
        cursor = await db.execute("SELECT result_json, summary_json, created_at FROM gap_scan_cache WHERE id = 1")
        row = await cursor.fetchone()
        if not row:
            return [], {}, None
        try:
            results = json.loads(row["result_json"] or "[]")
        except json.JSONDecodeError:
            results = []
        try:
            summary = json.loads(row["summary_json"] or "{}")
        except json.JSONDecodeError:
            summary = {}
        return results if isinstance(results, list) else [], summary if isinstance(summary, dict) else {}, row["created_at"]
    finally:
        await db.close()


def _gap_auto_fill_job_row(row: aiosqlite.Row | None) -> dict | None:
    if not row:
        return None
    item = dict(row)
    item["enabled"] = bool(item.get("enabled"))
    raw_results = item.pop("last_results_json", "[]")
    try:
        results = json.loads(raw_results or "[]")
    except (TypeError, json.JSONDecodeError):
        results = []
    item["last_results"] = results if isinstance(results, list) else []
    return item


async def get_gap_auto_fill_job(series_id: str, season_number: int) -> dict | None:
    """读取单个影巢自动补季任务。"""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT * FROM gap_auto_fill_jobs WHERE series_id = ? AND season_number = ?",
            (str(series_id or ""), int(season_number)),
        )
        return _gap_auto_fill_job_row(await cursor.fetchone())
    finally:
        await db.close()


async def list_gap_auto_fill_jobs(enabled_only: bool = False) -> list[dict]:
    """列出影巢自动补季任务。"""
    db = await get_db()
    try:
        if enabled_only:
            cursor = await db.execute(
                "SELECT * FROM gap_auto_fill_jobs WHERE enabled = 1 ORDER BY updated_at DESC"
            )
        else:
            cursor = await db.execute(
                "SELECT * FROM gap_auto_fill_jobs ORDER BY updated_at DESC"
            )
        rows = await cursor.fetchall()
        return [_gap_auto_fill_job_row(row) for row in rows]
    finally:
        await db.close()


async def enable_gap_auto_fill_job(
    series_id: str,
    season_number: int,
    series_name: str = "",
    year: int | str = "",
    tmdb_id: int | str = 0,
    imdb_id: str = "",
    library_name: str = "",
) -> dict | None:
    """启用或重新启用一个影巢自动补季任务。"""
    try:
        tmdb_number = int(tmdb_id or 0)
    except (TypeError, ValueError):
        tmdb_number = 0
    db = await get_db()
    try:
        await db.execute(
            """
            INSERT INTO gap_auto_fill_jobs
            (
                series_id, season_number, series_name, year, tmdb_id, imdb_id, library_name,
                enabled, status, last_message, last_resource_title, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, 1, 'waiting', '', '', datetime('now','localtime'))
            ON CONFLICT(series_id, season_number) DO UPDATE SET
                series_name = excluded.series_name,
                year = excluded.year,
                tmdb_id = excluded.tmdb_id,
                imdb_id = excluded.imdb_id,
                library_name = excluded.library_name,
                enabled = 1,
                status = 'waiting',
                last_message = '',
                last_resource_title = '',
                updated_at = datetime('now','localtime')
            """,
            (
                str(series_id or ""),
                int(season_number),
                str(series_name or ""),
                str(year or ""),
                tmdb_number,
                str(imdb_id or ""),
                str(library_name or ""),
            ),
        )
        await db.commit()
    finally:
        await db.close()
    return await get_gap_auto_fill_job(series_id, season_number)


async def update_gap_auto_fill_job(
    series_id: str,
    season_number: int,
    *,
    enabled: bool | None = None,
    status: str | None = None,
    message: str | None = None,
    resource_title: str | None = None,
    results: list[dict] | None = None,
    increment_attempts: bool = False,
    touch_run_at: bool = False,
    require_enabled: bool = False,
) -> dict | None:
    """更新自动补季任务状态；require_enabled 可避免覆盖用户刚取消的任务。"""
    updates: list[str] = []
    values: list = []
    if enabled is not None:
        updates.append("enabled = ?")
        values.append(1 if enabled else 0)
    if status is not None:
        updates.append("status = ?")
        values.append(str(status or ""))
    if message is not None:
        updates.append("last_message = ?")
        values.append(str(message or ""))
    if resource_title is not None:
        updates.append("last_resource_title = ?")
        values.append(str(resource_title or ""))
    if results is not None:
        updates.append("last_results_json = ?")
        values.append(json.dumps(results[:10], ensure_ascii=False))
    if increment_attempts:
        updates.append("attempts = attempts + 1")
    if touch_run_at:
        updates.append("last_run_at = datetime('now','localtime')")
    updates.append("updated_at = datetime('now','localtime')")

    db = await get_db()
    try:
        where = "series_id = ? AND season_number = ?"
        values.extend([str(series_id or ""), int(season_number)])
        if require_enabled:
            where += " AND enabled = 1"
        await db.execute(
            f"UPDATE gap_auto_fill_jobs SET {', '.join(updates)} WHERE {where}",
            values,
        )
        await db.commit()
    finally:
        await db.close()
    return await get_gap_auto_fill_job(series_id, season_number)


async def save_gap_transfer_mark(
    resource_key: str,
    resource_title: str = "",
    series_name: str = "",
    season_number: int = 0,
    episodes: list[int] | None = None,
    status: str = "",
    response: dict | None = None,
):
    """记录缺集资源已解锁/转存，供下次搜索时回显。"""
    key = str(resource_key or "").strip()
    if not key:
        return
    episode_numbers: list[int] = []
    for episode in episodes or []:
        try:
            num = int(episode)
        except (TypeError, ValueError):
            continue
        if num > 0:
            episode_numbers.append(num)
    db = await get_db()
    try:
        await db.execute(
            """
            INSERT OR REPLACE INTO gap_transfer_marks
            (resource_key, resource_title, series_name, season_number, episode_numbers_json, status, response_json, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now','localtime'))
            """,
            (
                key,
                str(resource_title or ""),
                str(series_name or ""),
                int(season_number or 0),
                json.dumps(sorted(set(episode_numbers)), ensure_ascii=False),
                str(status or ""),
                json.dumps(response or {}, ensure_ascii=False),
            ),
        )
        await db.commit()
    finally:
        await db.close()


async def list_gap_transfer_marks(resource_keys: list[str]) -> dict[str, dict]:
    """按资源 key 批量读取缺集转存标记。"""
    keys = [str(key).strip() for key in resource_keys if str(key or "").strip()]
    keys = list(dict.fromkeys(keys))
    if not keys:
        return {}

    db = await get_db()
    try:
        placeholders = ",".join("?" for _ in keys)
        cursor = await db.execute(
            f"SELECT * FROM gap_transfer_marks WHERE resource_key IN ({placeholders})",
            keys,
        )
        rows = await cursor.fetchall()
        marks: dict[str, dict] = {}
        for row in rows:
            item = dict(row)
            try:
                item["episodes"] = json.loads(item.get("episode_numbers_json") or "[]")
            except json.JSONDecodeError:
                item["episodes"] = []
            try:
                item["response"] = json.loads(item.get("response_json") or "{}")
            except json.JSONDecodeError:
                item["response"] = {}
            marks[item["resource_key"]] = item
        return marks
    finally:
        await db.close()


async def list_recent_gap_transfer_marks(limit: int = 50) -> list[dict]:
    """读取最近的缺集解锁/转存记录，供历史页补充展示。"""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT * FROM gap_transfer_marks ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        )
        rows = await cursor.fetchall()
        items: list[dict] = []
        for row in rows:
            item = dict(row)
            try:
                item["episodes"] = json.loads(item.get("episode_numbers_json") or "[]")
            except json.JSONDecodeError:
                item["episodes"] = []
            try:
                item["response"] = json.loads(item.get("response_json") or "{}")
            except json.JSONDecodeError:
                item["response"] = {}
            items.append(item)
        return items
    finally:
        await db.close()


def make_gap_episode_target_id(series_id: str, season_number: int, episode_number: int) -> str:
    """生成单集忽略 ID。"""
    return f"{series_id}:S{int(season_number):02d}E{int(episode_number):02d}"


def make_gap_season_target_id(series_id: str, season_number: int) -> str:
    """生成整季缺失忽略 ID。"""
    return f"{series_id}:S{int(season_number):02d}:season"


async def add_gap_ignore_series(series_id: str, series_name: str):
    """忽略整部剧集。"""
    db = await get_db()
    try:
        await db.execute(
            """
            INSERT OR REPLACE INTO gap_ignores
            (target_type, target_id, series_id, series_name, season_number, episode_number, ignored_at)
            VALUES ('series', ?, ?, ?, 0, 0, datetime('now','localtime'))
            """,
            (series_id, series_id, series_name),
        )
        await db.commit()
    finally:
        await db.close()


async def add_gap_ignore_episode(series_id: str, series_name: str, season_number: int, episode_number: int):
    """忽略单集缺口。"""
    target_id = make_gap_episode_target_id(series_id, season_number, episode_number)
    db = await get_db()
    try:
        await db.execute(
            """
            INSERT OR REPLACE INTO gap_ignores
            (target_type, target_id, series_id, series_name, season_number, episode_number, ignored_at)
            VALUES ('episode', ?, ?, ?, ?, ?, datetime('now','localtime'))
            """,
            (target_id, series_id, series_name, int(season_number), int(episode_number)),
        )
        await db.commit()
    finally:
        await db.close()


async def add_gap_ignore_season(series_id: str, series_name: str, season_number: int):
    """忽略整季缺口。"""
    target_id = make_gap_season_target_id(series_id, season_number)
    db = await get_db()
    try:
        await db.execute(
            """
            INSERT OR REPLACE INTO gap_ignores
            (target_type, target_id, series_id, series_name, season_number, episode_number, ignored_at)
            VALUES ('season', ?, ?, ?, ?, 0, datetime('now','localtime'))
            """,
            (target_id, series_id, series_name, int(season_number)),
        )
        await db.commit()
    finally:
        await db.close()


async def list_gap_ignores(target_type: str = "") -> list[dict]:
    """列出缺集忽略项。"""
    db = await get_db()
    try:
        if target_type:
            cursor = await db.execute(
                "SELECT * FROM gap_ignores WHERE target_type = ? ORDER BY ignored_at DESC",
                (target_type,),
            )
        else:
            cursor = await db.execute("SELECT * FROM gap_ignores ORDER BY ignored_at DESC")
        rows = await cursor.fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["type"] = item.pop("target_type", "")
            item["target"] = item["type"]
            item["row_id"] = item.get("id")
            item["id"] = item.get("target_id", "")
            if item["type"] == "series":
                item["label"] = item.get("series_name", "")
            else:
                item["label"] = f"{item.get('series_name', '')} S{int(item.get('season_number') or 0):02d}E{int(item.get('episode_number') or 0):02d}"
            items.append(item)
        return items
    finally:
        await db.close()


async def get_gap_ignore_targets() -> set[str]:
    """获取缺集忽略目标 ID 集合。"""
    db = await get_db()
    try:
        cursor = await db.execute("SELECT target_id FROM gap_ignores")
        rows = await cursor.fetchall()
        return {row["target_id"] for row in rows}
    finally:
        await db.close()


async def remove_gap_ignore(target_id: str, target_type: str = "") -> bool:
    """移除缺集忽略项，兼容目标 ID 和行 ID。"""
    db = await get_db()
    try:
        params: list = [target_id]
        where = "target_id = ?"
        try:
            row_id = int(target_id)
            where = f"({where} OR id = ?)"
            params.append(row_id)
        except (TypeError, ValueError):
            pass
        if target_type:
            where = f"{where} AND target_type = ?"
            params.append(target_type)
        cursor = await db.execute(f"DELETE FROM gap_ignores WHERE {where}", params)
        await db.commit()
        return cursor.rowcount > 0
    finally:
        await db.close()


async def remove_gap_ignores(target_ids: list[str]) -> int:
    """批量移除缺集忽略项。"""
    deleted = 0
    for target_id in target_ids:
        if await remove_gap_ignore(str(target_id)):
            deleted += 1
    return deleted

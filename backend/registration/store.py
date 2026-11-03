# -*- coding: utf-8 -*-
"""XRegistration 注册结果仓储。

新数据库只保存注册管理需要的通用字段，不创建第三方鉴权产物或投递表。
读取旧数据库时也只暴露通用字段，旧表遗留的鉴权列不会进入运行时。
"""
from __future__ import annotations

import datetime as _datetime
import ipaddress
import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Tuple


RESULT_COLUMNS = (
    "source_key", "batch_id", "source", "started_at", "finished_at",
    "duration_seconds", "email", "password", "status", "success", "provider", "platform",
    "worker_id", "email_account_id", "email_disable_status", "email_disabled_at",
    "email_disable_error", "failure_type", "failure_reason", "screenshot_path",
    "account_file", "extra_json",
)
RESULT_SELECT = ", ".join(("id", *RESULT_COLUMNS))
SQLITE_IN_BATCH_SIZE = 900


class RegistrationRepository:
    def __init__(self, database_path: os.PathLike[str] | str):
        self.database_path = os.path.abspath(os.fspath(database_path))
        os.makedirs(os.path.dirname(self.database_path), exist_ok=True)
        self._initialize()

    @staticmethod
    def now_text() -> str:
        return _datetime.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.database_path, timeout=15.0)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=15000")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _initialize(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS registration_results (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_key TEXT UNIQUE,
                    batch_id TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL DEFAULT 'web',
                    started_at TEXT NOT NULL,
                    finished_at TEXT NOT NULL,
                    duration_seconds REAL NOT NULL DEFAULT 0,
                    email TEXT NOT NULL DEFAULT '',
                    password TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'failure',
                    success INTEGER NOT NULL DEFAULT 0,
                    provider TEXT NOT NULL DEFAULT '',
                    platform TEXT NOT NULL DEFAULT 'xai',
                    worker_id INTEGER NOT NULL DEFAULT 0,
                    email_account_id TEXT NOT NULL DEFAULT '',
                    email_disable_status TEXT NOT NULL DEFAULT 'not_attempted',
                    email_disabled_at TEXT NOT NULL DEFAULT '',
                    email_disable_error TEXT NOT NULL DEFAULT '',
                    failure_type TEXT NOT NULL DEFAULT '',
                    failure_reason TEXT NOT NULL DEFAULT '',
                    screenshot_path TEXT NOT NULL DEFAULT '',
                    account_file TEXT NOT NULL DEFAULT '',
                    extra_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS idx_registration_results_finished
                    ON registration_results(finished_at DESC);
                CREATE INDEX IF NOT EXISTS idx_registration_results_email_success
                    ON registration_results(email COLLATE NOCASE, success);
                CREATE INDEX IF NOT EXISTS idx_registration_results_status
                    ON registration_results(status);
                CREATE INDEX IF NOT EXISTS idx_registration_results_batch
                    ON registration_results(batch_id);

                CREATE TABLE IF NOT EXISTS registration_job_snapshot (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    batch_id TEXT NOT NULL DEFAULT '',
                    running INTEGER NOT NULL DEFAULT 0,
                    started_at REAL,
                    finished_at REAL,
                    target_count INTEGER NOT NULL DEFAULT 0,
                    workers INTEGER NOT NULL DEFAULT 1,
                    source TEXT NOT NULL DEFAULT 'web',
                    last_error TEXT NOT NULL DEFAULT '',
                    completed_count INTEGER NOT NULL DEFAULT 0,
                    success_count INTEGER NOT NULL DEFAULT 0,
                    failure_count INTEGER NOT NULL DEFAULT 0,
                    current_stage TEXT NOT NULL DEFAULT '',
                    current_email TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS flagged_exit_ips (
                    ip TEXT PRIMARY KEY,
                    first_seen_at TEXT NOT NULL DEFAULT '',
                    last_seen_at TEXT NOT NULL DEFAULT '',
                    hit_count INTEGER NOT NULL DEFAULT 1,
                    last_email TEXT NOT NULL DEFAULT '',
                    last_risk_source TEXT NOT NULL DEFAULT '',
                    last_failure_reason TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_flagged_exit_ips_last_seen
                    ON flagged_exit_ips(last_seen_at);
                """
            )

            # 旧数据库可能缺少后来加入的通用字段；不迁移任何鉴权列。
            existing_columns = {
                str(row["name"])
                for row in conn.execute("PRAGMA table_info(registration_results)").fetchall()
            }
            migrations = {
                "email_account_id": "TEXT NOT NULL DEFAULT ''",
                "email_disable_status": "TEXT NOT NULL DEFAULT 'not_attempted'",
                "email_disabled_at": "TEXT NOT NULL DEFAULT ''",
                "email_disable_error": "TEXT NOT NULL DEFAULT ''",
                "screenshot_path": "TEXT NOT NULL DEFAULT ''",
                "platform": "TEXT NOT NULL DEFAULT 'xai'",
            }
            for column, definition in migrations.items():
                if column not in existing_columns:
                    conn.execute(
                        f"ALTER TABLE registration_results ADD COLUMN {column} {definition}"
                    )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_registration_results_platform "
                "ON registration_results(platform)"
            )

            exit_ip_columns = {
                str(row["name"])
                for row in conn.execute("PRAGMA table_info(flagged_exit_ips)").fetchall()
            }
            if "last_risk_source" not in exit_ip_columns:
                conn.execute(
                    "ALTER TABLE flagged_exit_ips "
                    "ADD COLUMN last_risk_source TEXT NOT NULL DEFAULT ''"
                )
            # 兼容旧库同义字段，新库不会创建该历史字段。
            legacy_risk_column = "last_" + "bot_flag_source"
            if legacy_risk_column in exit_ip_columns:
                conn.execute(
                    f"UPDATE flagged_exit_ips SET last_risk_source = {legacy_risk_column} "
                    "WHERE last_risk_source = ''"
                )

            conn.execute(
                """
                UPDATE registration_results
                SET email_disable_status = 'not_applicable'
                WHERE lower(provider) != 'outlookemail'
                  AND email_disable_status = 'not_attempted'
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_registration_results_email_disable_status
                ON registration_results(email_disable_status)
                """
            )
            conn.execute(
                "INSERT OR IGNORE INTO registration_job_snapshot (id, updated_at) VALUES (1, ?)",
                (self.now_text(),),
            )
            conn.execute("PRAGMA user_version = 10")

    def add_result(self, record: Dict[str, Any]) -> int:
        now = self.now_text()
        status = str(record.get("status") or "failure").strip().lower()
        success = 1 if status == "success" or bool(record.get("success")) else 0
        extra = record.get("extra_json", record.get("extra", {}))
        extra_json = extra if isinstance(extra, str) else json.dumps(
            extra or {}, ensure_ascii=False, sort_keys=True
        )
        provider = str(record.get("provider") or "")
        disable_status = str(
            record.get("email_disable_status") or "not_attempted"
        ).strip().lower()
        if provider.lower() != "outlookemail" and disable_status == "not_attempted":
            disable_status = "not_applicable"

        normalized = {
            "source_key": record.get("source_key") or None,
            "batch_id": str(record.get("batch_id") or ""),
            "source": str(record.get("source") or "web"),
            "started_at": str(record.get("started_at") or now),
            "finished_at": str(record.get("finished_at") or now),
            "duration_seconds": max(float(record.get("duration_seconds") or 0), 0.0),
            "email": str(record.get("email") or "").strip(),
            "password": str(record.get("password") or ""),
            "status": status,
            "success": success,
            "provider": provider,
            "platform": str(record.get("platform") or "xai").strip().lower(),
            "worker_id": int(record.get("worker_id") or 0),
            "email_account_id": str(record.get("email_account_id") or ""),
            "email_disable_status": disable_status,
            "email_disabled_at": str(record.get("email_disabled_at") or ""),
            "email_disable_error": str(record.get("email_disable_error") or ""),
            "failure_type": str(record.get("failure_type") or ""),
            "failure_reason": str(record.get("failure_reason") or ""),
            "screenshot_path": str(record.get("screenshot_path") or ""),
            "account_file": str(record.get("account_file") or ""),
            "extra_json": extra_json,
        }
        columns = ", ".join(RESULT_COLUMNS)
        placeholders = ", ".join(f":{name}" for name in RESULT_COLUMNS)
        with self._connect() as conn:
            cursor = conn.execute(
                f"INSERT INTO registration_results ({columns}) VALUES ({placeholders})",
                normalized,
            )
            return int(cursor.lastrowid or 0)

    def has_success(self, email: str) -> bool:
        normalized = str(email or "").strip()
        if not normalized:
            return False
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM registration_results "
                "WHERE success = 1 AND email = ? COLLATE NOCASE LIMIT 1",
                (normalized,),
            ).fetchone()
        return row is not None

    def has_registered_or_consumed(self, email: str) -> bool:
        """已成功、明确判定已注册或已处理邮箱的记录不再重复取用。"""
        normalized = str(email or "").strip()
        if not normalized:
            return False
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT 1 FROM registration_results
                WHERE email = ? COLLATE NOCASE
                  AND (
                    success = 1
                    OR lower(coalesce(failure_type, '')) = 'already_registered'
                    OR lower(coalesce(email_disable_status, '')) IN ('success', 'failed')
                  )
                LIMIT 1
                """,
                (normalized,),
            ).fetchone()
        return row is not None

    @staticmethod
    def _result_filters(
        *, status: str = "", email_disable_status: str = "", keyword: str = "",
        batch_id: str = "", platform: str = "",
    ) -> Tuple[str, List[Any]]:
        clauses: List[str] = []
        params: List[Any] = []
        normalized_status = str(status or "").strip().lower()
        if normalized_status:
            clauses.append("status = ?")
            params.append(normalized_status)
        normalized_disable_status = str(email_disable_status or "").strip().lower()
        if normalized_disable_status:
            clauses.append("email_disable_status = ?")
            params.append(normalized_disable_status)
        normalized_batch_id = str(batch_id or "").strip()
        if normalized_batch_id:
            clauses.append("batch_id = ?")
            params.append(normalized_batch_id)
        normalized_platform = str(platform or "").strip().lower()
        if normalized_platform:
            clauses.append("platform = ?")
            params.append(normalized_platform)
        normalized_keyword = str(keyword or "").strip()
        if normalized_keyword:
            like = f"%{normalized_keyword}%"
            clauses.append(
                "(email LIKE ? OR provider LIKE ? OR failure_reason LIKE ? "
                "OR batch_id LIKE ? OR email_account_id LIKE ? OR email_disable_error LIKE ?)"
            )
            params.extend([like, like, like, like, like, like])
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        return where, params

    def list_results(
        self, *, status: str = "", email_disable_status: str = "", keyword: str = "",
        batch_id: str = "", platform: str = "", limit: int = 2000, offset: int = 0,
    ) -> List[Dict[str, Any]]:
        where, params = self._result_filters(
            status=status, email_disable_status=email_disable_status,
            keyword=keyword, batch_id=batch_id, platform=platform,
        )
        safe_limit = max(1, min(int(limit or 2000), 10000))
        safe_offset = max(0, int(offset or 0))
        params.extend([safe_limit, safe_offset])
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT {RESULT_SELECT} FROM registration_results {where} "
                "ORDER BY finished_at DESC, id DESC LIMIT ? OFFSET ?",
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def count_results(
        self, *, status: str = "", email_disable_status: str = "", keyword: str = "",
        batch_id: str = "", platform: str = "",
    ) -> int:
        where, params = self._result_filters(
            status=status, email_disable_status=email_disable_status,
            keyword=keyword, batch_id=batch_id, platform=platform,
        )
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT COUNT(*) AS total FROM registration_results {where}", params
            ).fetchone()
        return int(row["total"] or 0)

    def list_result_ids(
        self, *, status: str = "", email_disable_status: str = "", keyword: str = "",
        batch_id: str = "", platform: str = "",
    ) -> List[int]:
        where, params = self._result_filters(
            status=status, email_disable_status=email_disable_status,
            keyword=keyword, batch_id=batch_id, platform=platform,
        )
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT id FROM registration_results {where} "
                "ORDER BY finished_at DESC, id DESC",
                params,
            ).fetchall()
        return [int(row["id"]) for row in rows]

    @staticmethod
    def _normalize_ids(ids: Iterable[int | str]) -> List[int]:
        normalized: List[int] = []
        seen = set()
        for raw in ids or []:
            try:
                value = int(raw)
            except (TypeError, ValueError):
                continue
            if value <= 0 or value in seen:
                continue
            seen.add(value)
            normalized.append(value)
        return normalized

    def get_results_by_ids(self, ids: Iterable[int | str]) -> List[Dict[str, Any]]:
        """按主键批量读取记录，保持传入顺序。"""
        normalized = self._normalize_ids(ids)
        if not normalized:
            return []
        by_id: Dict[int, Dict[str, Any]] = {}
        with self._connect() as conn:
            for start in range(0, len(normalized), SQLITE_IN_BATCH_SIZE):
                batch = normalized[start : start + SQLITE_IN_BATCH_SIZE]
                placeholders = ", ".join("?" for _ in batch)
                rows = conn.execute(
                    f"SELECT {RESULT_SELECT} FROM registration_results "
                    f"WHERE id IN ({placeholders})", batch,
                ).fetchall()
                by_id.update({int(row["id"]): dict(row) for row in rows})
        return [by_id[item_id] for item_id in normalized if item_id in by_id]

    @staticmethod
    def _normalize_exit_ip(value: Any) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        try:
            return ipaddress.ip_address(text).compressed
        except ValueError:
            return ""

    def remember_flagged_exit_ip(
        self, ip: str, *, email: str = "", risk_source: Any = None,
        failure_reason: str = "",
    ) -> bool:
        normalized = self._normalize_exit_ip(ip)
        if not normalized:
            return False
        now = self.now_text()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO flagged_exit_ips (
                    ip, first_seen_at, last_seen_at, hit_count,
                    last_email, last_risk_source, last_failure_reason
                ) VALUES (?, ?, ?, 1, ?, ?, ?)
                ON CONFLICT(ip) DO UPDATE SET
                    last_seen_at = excluded.last_seen_at,
                    hit_count = hit_count + 1,
                    last_email = CASE WHEN excluded.last_email = ''
                        THEN last_email ELSE excluded.last_email END,
                    last_risk_source = CASE WHEN excluded.last_risk_source = ''
                        THEN last_risk_source ELSE excluded.last_risk_source END,
                    last_failure_reason = CASE WHEN excluded.last_failure_reason = ''
                        THEN last_failure_reason ELSE excluded.last_failure_reason END
                """,
                (
                    normalized, now, now, str(email or "").strip(),
                    "" if risk_source is None else str(risk_source),
                    str(failure_reason or "").strip(),
                ),
            )
        return True

    def is_flagged_exit_ip(self, ip: str) -> bool:
        normalized = self._normalize_exit_ip(ip)
        if not normalized:
            return False
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM flagged_exit_ips WHERE ip = ?", (normalized,)
            ).fetchone()
        return row is not None

    def list_flagged_exit_ips(self, limit: int = 200) -> List[Dict[str, Any]]:
        try:
            capped = max(1, min(int(limit or 200), 1000))
        except (TypeError, ValueError):
            capped = 200
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT ip, first_seen_at, last_seen_at, hit_count,
                       last_email, last_risk_source, last_failure_reason
                FROM flagged_exit_ips
                ORDER BY last_seen_at DESC, ip LIMIT ?
                """, (capped,),
            ).fetchall()
        return [dict(row) for row in rows]

    def delete_flagged_exit_ip(self, ip: str) -> bool:
        normalized = self._normalize_exit_ip(ip)
        if not normalized:
            return False
        with self._connect() as conn:
            cursor = conn.execute("DELETE FROM flagged_exit_ips WHERE ip = ?", (normalized,))
        return bool(cursor.rowcount)

    def delete_results(self, ids: Iterable[int | str]) -> List[Dict[str, Any]]:
        records = self.get_results_by_ids(ids)
        if not records:
            return []
        delete_ids = [int(row["id"]) for row in records]
        with self._connect() as conn:
            for start in range(0, len(delete_ids), SQLITE_IN_BATCH_SIZE):
                batch = delete_ids[start : start + SQLITE_IN_BATCH_SIZE]
                placeholders = ", ".join("?" for _ in batch)
                conn.execute(
                    f"DELETE FROM registration_results WHERE id IN ({placeholders})", batch
                )
        return records

    def stats(self) -> Dict[str, Any]:
        today = _datetime.datetime.now().astimezone().strftime("%Y-%m-%d")
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END) AS success,
                    SUM(CASE WHEN status = 'failure' THEN 1 ELSE 0 END) AS failure,
                    SUM(CASE WHEN status = 'skipped' THEN 1 ELSE 0 END) AS skipped,
                    SUM(CASE WHEN status = 'cancelled' THEN 1 ELSE 0 END) AS cancelled,
                    SUM(CASE WHEN email_disable_status = 'success' THEN 1 ELSE 0 END) AS email_disabled,
                    SUM(CASE WHEN email_disable_status = 'failed' THEN 1 ELSE 0 END) AS email_disable_failed,
                    SUM(CASE WHEN substr(finished_at, 1, 10) = ? THEN 1 ELSE 0 END) AS today_total,
                    SUM(CASE WHEN substr(finished_at, 1, 10) = ? AND status = 'success'
                        THEN 1 ELSE 0 END) AS today_success,
                    COUNT(DISTINCT CASE WHEN success = 1 THEN lower(email) END)
                        AS unique_success_emails,
                    AVG(CASE WHEN status = 'success' AND duration_seconds > 0
                        THEN duration_seconds END) AS avg_success_seconds
                FROM registration_results
                """, (today, today),
            ).fetchone()
            providers = conn.execute(
                """
                SELECT provider, COUNT(*) AS total,
                       SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END) AS success
                FROM registration_results GROUP BY provider
                ORDER BY total DESC, provider ASC
                """
            ).fetchall()
        result = {key: (row[key] or 0) for key in row.keys()}
        result["providers"] = [dict(item) for item in providers]
        return result

    def import_existing_accounts(self, accounts_dir: os.PathLike[str] | str) -> int:
        """导入旧账号文本；兼容二段和三段格式，但只保存邮箱与密码。"""
        root = Path(accounts_dir)
        if not root.is_dir():
            return 0
        imported = 0
        paths = list(root.glob("*.txt"))
        x_root = root / "x"
        if x_root.is_dir():
            paths.extend(x_root.glob("*.txt"))
        for path in sorted(paths):
            if path.name.startswith(("mail_", "accounts_summary_")):
                continue
            try:
                lines = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
                stamp = _datetime.datetime.fromtimestamp(
                    path.stat().st_mtime,
                    tz=_datetime.datetime.now().astimezone().tzinfo,
                ).strftime("%Y-%m-%d %H:%M:%S")
            except OSError:
                continue
            for raw_line in lines:
                parts = raw_line.strip().split("----")
                if len(parts) < 2:
                    continue
                email, password = parts[0].strip(), parts[1].strip()
                if "@" not in email or not password or self.has_success(email):
                    continue
                is_individual_file = path.stem.lower() == email.lower()
                platform = "x" if path.parent == x_root else "xai"
                try:
                    self.add_result(
                        {
                            "source_key": f"legacy-success:{platform}:{email.lower()}",
                            "batch_id": "legacy-import",
                            "source": "legacy_import",
                            "started_at": stamp,
                            "finished_at": stamp,
                            "email": email,
                            "password": password,
                            "status": "success",
                            "provider": "历史文件",
                            "platform": platform,
                            "account_file": str(path.resolve()) if is_individual_file else "",
                            "extra": {"imported_from": str(path.resolve())},
                        }
                    )
                    imported += 1
                except (OSError, sqlite3.IntegrityError, ValueError):
                    continue
        return imported

    def get_job_snapshot(self) -> Dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM registration_job_snapshot WHERE id = 1"
            ).fetchone()
        if not row:
            return {}
        data = dict(row)
        data["running"] = bool(data.get("running"))
        return data

    def save_job_snapshot(self, snapshot: Dict[str, Any]) -> None:
        payload = {
            "batch_id": str(snapshot.get("batch_id") or ""),
            "running": 1 if snapshot.get("running") else 0,
            "started_at": snapshot.get("started_at"),
            "finished_at": snapshot.get("finished_at"),
            "target_count": int(snapshot.get("target_count") or 0),
            "workers": int(snapshot.get("workers") or 1),
            "source": str(snapshot.get("source") or "web"),
            "last_error": str(snapshot.get("last_error") or ""),
            "completed_count": int(snapshot.get("completed_count") or 0),
            "success_count": int(snapshot.get("success_count") or 0),
            "failure_count": int(snapshot.get("failure_count") or 0),
            "current_stage": str(snapshot.get("current_stage") or ""),
            "current_email": str(snapshot.get("current_email") or ""),
            "updated_at": self.now_text(),
        }
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO registration_job_snapshot (
                    id, batch_id, running, started_at, finished_at, target_count, workers,
                    source, last_error, completed_count, success_count, failure_count,
                    current_stage, current_email, updated_at
                ) VALUES (
                    1, :batch_id, :running, :started_at, :finished_at, :target_count, :workers,
                    :source, :last_error, :completed_count, :success_count, :failure_count,
                    :current_stage, :current_email, :updated_at
                )
                ON CONFLICT(id) DO UPDATE SET
                    batch_id = excluded.batch_id,
                    running = excluded.running,
                    started_at = excluded.started_at,
                    finished_at = excluded.finished_at,
                    target_count = excluded.target_count,
                    workers = excluded.workers,
                    source = excluded.source,
                    last_error = excluded.last_error,
                    completed_count = excluded.completed_count,
                    success_count = excluded.success_count,
                    failure_count = excluded.failure_count,
                    current_stage = excluded.current_stage,
                    current_email = excluded.current_email,
                    updated_at = excluded.updated_at
                """, payload,
            )

    def latest_web_batch_id(self) -> str:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT batch_id FROM registration_results
                WHERE batch_id IS NOT NULL AND trim(batch_id) != ''
                  AND batch_id != 'legacy-import'
                ORDER BY finished_at DESC, id DESC LIMIT 1
                """
            ).fetchone()
        return str(row["batch_id"] if row else "") or ""

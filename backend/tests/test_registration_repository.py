import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from backend.registration.store import RegistrationRepository


class RegistrationRepositoryTests(unittest.TestCase):
    def test_new_database_contains_only_registration_tables_and_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "results.sqlite3"
            store = RegistrationRepository(path)
            with closing(sqlite3.connect(path)) as conn:
                tables = {
                    row[0]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
                columns = {
                    row[1]
                    for row in conn.execute("PRAGMA table_info(registration_results)")
                }
                version = conn.execute("PRAGMA user_version").fetchone()[0]

            self.assertEqual(version, 10)
            self.assertEqual(
                tables - {"sqlite_sequence"},
                {"registration_results", "registration_job_snapshot", "flagged_exit_ips"},
            )
            self.assertIn("email", columns)
            self.assertIn("password", columns)
            self.assertIn("failure_reason", columns)
            self.assertIn("platform", columns)
            self.assertNotIn("auth_info", columns)

            result_id = store.add_result(
                {
                    "email": "person@example.com",
                    "password": "secret",
                    "status": "success",
                    "provider": "fixture",
                }
            )
            row = store.get_results_by_ids([result_id])[0]
            self.assertEqual(row["email"], "person@example.com")
            self.assertEqual(row["password"], "secret")
            self.assertTrue(row["success"])
            self.assertEqual(row["platform"], "xai")

            x_result_id = store.add_result(
                {
                    "email": "x-user@example.com",
                    "password": "secret",
                    "status": "success",
                    "provider": "fixture",
                    "platform": "x",
                }
            )
            x_row = store.get_results_by_ids([x_result_id])[0]
            self.assertEqual(x_row["platform"], "x")
            self.assertEqual(store.count_results(platform="x"), 1)

    def test_pagination_filters_stats_and_large_batches(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RegistrationRepository(Path(tmp) / "results.sqlite3")
            for index in range(5):
                store.add_result(
                    {
                        "email": f"user-{index}@example.com",
                        "password": f"pass-{index}",
                        "status": "success" if index < 4 else "failure",
                        "provider": "fixture",
                        "finished_at": f"2026-08-04 00:00:0{index}",
                    }
                )

            self.assertEqual(store.count_results(status="success", keyword="user-"), 4)
            page = store.list_results(
                status="success", keyword="user-", limit=2, offset=2
            )
            self.assertEqual(
                [row["email"] for row in page],
                ["user-1@example.com", "user-0@example.com"],
            )
            self.assertEqual(store.stats()["success"], 4)
            records = store.get_results_by_ids(range(1, 1006))
            self.assertEqual([row["id"] for row in records], [1, 2, 3, 4, 5])
            self.assertEqual(len(store.delete_results(range(1, 1006))), 5)
            self.assertEqual(store.count_results(), 0)

    def test_imports_two_and_three_part_legacy_files_as_email_password(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "one@example.com.txt").write_text(
                "one@example.com----one-pass\n", encoding="utf-8"
            )
            (root / "accounts_old.txt").write_text(
                "two@example.com----two-pass----legacy-third-field\n", encoding="utf-8"
            )
            (root / "x").mkdir()
            (root / "x" / "x-user@example.com.txt").write_text(
                "x-user@example.com----x-pass\n", encoding="utf-8"
            )
            store = RegistrationRepository(root / "results.sqlite3")
            self.assertEqual(store.import_existing_accounts(root), 3)
            rows = {row["email"]: row for row in store.list_results()}
            self.assertEqual(rows["one@example.com"]["password"], "one-pass")
            self.assertEqual(rows["two@example.com"]["password"], "two-pass")
            self.assertEqual(rows["two@example.com"]["account_file"], "")
            self.assertEqual(rows["x-user@example.com"]["platform"], "x")
            self.assertTrue(rows["x-user@example.com"]["account_file"].endswith("x-user@example.com.txt"))
            self.assertEqual(store.import_existing_accounts(root), 0)

    def test_job_snapshot_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RegistrationRepository(Path(tmp) / "results.sqlite3")
            store.save_job_snapshot(
                {
                    "batch_id": "web-1",
                    "running": True,
                    "target_count": 3,
                    "workers": 2,
                    "completed_count": 1,
                    "success_count": 1,
                }
            )
            snapshot = store.get_job_snapshot()
            self.assertTrue(snapshot["running"])
            self.assertEqual(snapshot["batch_id"], "web-1")
            self.assertEqual(store.latest_web_batch_id(), "")


if __name__ == "__main__":
    unittest.main()

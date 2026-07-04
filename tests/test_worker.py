"""Tests for atomic job claiming and the generic worker loop."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.services import brand_service
from src.storage.sqlite_store import SQLiteStore
from src.workers import job_runner


class ClaimPendingJobTests(unittest.TestCase):
    def _store(self, tmpdir: str) -> SQLiteStore:
        return SQLiteStore(str(Path(tmpdir) / "brand3.sqlite3"))

    def test_claims_oldest_queued_job(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self._store(tmpdir)
            first = store.create_analysis_job(url="https://a.com", brand_name="A", use_llm=False, use_social=False)
            second = store.create_analysis_job(url="https://b.com", brand_name="B", use_llm=False, use_social=False)

            claimed = store.claim_pending_job()
            self.assertIsNotNone(claimed)
            assert claimed is not None
            self.assertEqual(claimed["id"], first)
            self.assertEqual(claimed["status"], "running")
            self.assertEqual(claimed["attempt_count"], 1)

            next_claim = store.claim_pending_job()
            self.assertIsNotNone(next_claim)
            assert next_claim is not None
            self.assertEqual(next_claim["id"], second)
            store.close()

    def test_returns_none_when_queue_empty(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self._store(tmpdir)
            self.assertIsNone(store.claim_pending_job())
            store.close()

    def test_skips_cancel_requested_jobs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self._store(tmpdir)
            cancelled_id = store.create_analysis_job(url="https://a.com", brand_name="A", use_llm=False, use_social=False)
            good_id = store.create_analysis_job(url="https://b.com", brand_name="B", use_llm=False, use_social=False)
            store.request_analysis_job_cancel(cancelled_id)

            claimed = store.claim_pending_job()
            self.assertIsNotNone(claimed)
            assert claimed is not None
            self.assertEqual(claimed["id"], good_id)
            store.close()
            self.assertIsInstance(cancelled_id, int)

    def test_second_worker_on_same_job_gets_none(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store_a = self._store(tmpdir)
            store_b = self._store(tmpdir)
            job_id = store_a.create_analysis_job(url="https://a.com", brand_name="A", use_llm=False, use_social=False)

            first = store_a.claim_pending_job(job_id=job_id)
            second = store_b.claim_pending_job(job_id=job_id)

            self.assertIsNotNone(first)
            self.assertIsNone(second)
            store_a.close()
            store_b.close()

    def test_claim_by_id_only_claims_queued(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self._store(tmpdir)
            job_id = store.create_analysis_job(url="https://a.com", brand_name="A", use_llm=False, use_social=False)
            store.fail_analysis_job(job_id, "boom")

            claimed = store.claim_pending_job(job_id=job_id)
            self.assertIsNone(claimed)
            store.close()


class ExecuteAnalysisJobTests(unittest.TestCase):
    def test_execute_runs_pipeline_after_atomic_claim(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "brand3.sqlite3"
            store = SQLiteStore(str(db_path))
            job_id = store.create_analysis_job(
                url="https://example.com",
                brand_name="Example",
                use_llm=False,
                use_social=False,
            )
            store.close()

            with patch.object(brand_service, "BRAND3_DB_PATH", str(db_path)):
                with patch.object(
                    brand_service,
                    "run",
                    return_value={"brand": "Example", "url": "https://example.com", "run_id": 1, "composite_score": 50.0},
                ):
                    payload = brand_service.execute_analysis_job(job_id)

            self.assertEqual(payload["status"], "done")
            self.assertEqual(payload["attempt_count"], 1)


class WorkerLoopTests(unittest.TestCase):
    def test_runs_claimed_job_and_stops_on_shutdown(self):
        claimed_jobs = [{"id": 7, "url": "https://a.com"}]
        ran = []

        flag = job_runner._ShutdownFlag()

        def fake_claim(_worker_id):
            if claimed_jobs:
                return claimed_jobs.pop(0)
            flag.request()
            return None

        def fake_runner(job):
            ran.append(job["id"])
            return job

        job_runner.run(
            poll_interval=0,
            shutdown=flag,
            claim=fake_claim,
            runner=fake_runner,
            sleep=lambda _s: None,
        )
        self.assertEqual(ran, [7])

    def test_sleeps_and_continues_when_claim_raises(self):
        flag = job_runner._ShutdownFlag()
        calls = {"n": 0}

        def fake_claim(_worker_id):
            calls["n"] += 1
            if calls["n"] == 1:
                raise sqlite3.OperationalError("database is locked")
            flag.request()
            return None

        job_runner.run(
            poll_interval=0,
            shutdown=flag,
            claim=fake_claim,
            runner=lambda _j: None,
            sleep=lambda _s: None,
        )
        self.assertGreaterEqual(calls["n"], 2)


if __name__ == "__main__":
    unittest.main()

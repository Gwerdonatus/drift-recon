"""
Unit tests for app/workers/scheduler.py.

Patching note: reconciliation_job() and drift_check_job() use lazy imports
(imports inside the function body). This means names like get_db_context,
ReconciliationOrchestrator, and DriftAnalyzer are NEVER attributes of the
scheduler module itself. They must be patched at their source modules:

    ✗  patch("app.workers.scheduler.get_db_context")   # AttributeError
    ✓  patch("app.database.get_db_context")             # correct
"""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from sqlalchemy.exc import IntegrityError, OperationalError


# ── helpers ────────────────────────────────────────────────────────────────────


def _make_settings(**overrides):
    s = MagicMock()
    s.DATABASE_URL = "postgresql+asyncpg://user:pass@localhost/testdb"
    s.RECONCILIATION_CRON = "0 */6 * * *"
    s.DRIFT_CHECK_CRON = "0 8 * * *"
    s.ALERT_ON_LOW_MATCH_RATE = False
    s.ALERT_MATCH_RATE_THRESHOLD = 0.80
    s.ALERT_ON_DRIFT = False
    s.ALERT_WEBHOOK_URL = None
    for k, v in overrides.items():
        setattr(s, k, v)
    return s


def _mock_db_context(mock_db=None):
    """Return a context manager mock that yields mock_db."""
    if mock_db is None:
        mock_db = MagicMock()
        query_result = MagicMock()
        query_result.scalars.return_value = ["default"]
        mock_db.execute = AsyncMock(return_value=query_result)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=mock_db)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx, mock_db


# ── _ensure_apscheduler_table ──────────────────────────────────────────────────


class TestEnsureApschedulerTable:

    @patch("app.workers.scheduler.create_engine")
    def test_creates_table_and_index_on_fresh_db(self, mock_create_engine):
        from app.workers.scheduler import _ensure_apscheduler_table

        mock_engine = MagicMock()
        mock_conn = MagicMock()
        mock_create_engine.return_value = mock_engine
        mock_engine.begin.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_engine.begin.return_value.__exit__ = MagicMock(return_value=False)

        _ensure_apscheduler_table("postgresql://localhost/db", "apscheduler_jobs")

        mock_create_engine.assert_called_once_with("postgresql://localhost/db")
        assert mock_conn.execute.call_count == 2  # CREATE TABLE + CREATE INDEX
        mock_engine.dispose.assert_called_once()

    @patch("app.workers.scheduler.create_engine")
    def test_integrity_error_is_swallowed(self, mock_create_engine):
        from app.workers.scheduler import _ensure_apscheduler_table

        mock_engine = MagicMock()
        mock_create_engine.return_value = mock_engine
        mock_engine.begin.side_effect = IntegrityError(
            "stmt", {}, Exception("duplicate key")
        )

        _ensure_apscheduler_table("postgresql://localhost/db", "apscheduler_jobs")
        mock_engine.dispose.assert_called_once()

    @patch("app.workers.scheduler.create_engine")
    def test_operational_error_is_swallowed(self, mock_create_engine):
        from app.workers.scheduler import _ensure_apscheduler_table

        mock_engine = MagicMock()
        mock_create_engine.return_value = mock_engine
        mock_engine.begin.side_effect = OperationalError(
            "stmt", {}, Exception("connection refused")
        )

        _ensure_apscheduler_table("postgresql://localhost/db", "apscheduler_jobs")
        mock_engine.dispose.assert_called_once()

    @patch("app.workers.scheduler.create_engine")
    def test_engine_always_disposed(self, mock_create_engine):
        """dispose() is in finally — must run even when an error occurs."""
        from app.workers.scheduler import _ensure_apscheduler_table

        mock_engine = MagicMock()
        mock_create_engine.return_value = mock_engine
        mock_engine.begin.side_effect = IntegrityError("s", {}, Exception())

        _ensure_apscheduler_table("postgresql://localhost/db", "jobs")
        mock_engine.dispose.assert_called_once()


# ── start_scheduler ────────────────────────────────────────────────────────────


class TestStartScheduler:

    @patch("app.workers.scheduler._ensure_apscheduler_table")
    @patch("app.workers.scheduler.AsyncIOScheduler")
    @patch("app.workers.scheduler.SQLAlchemyJobStore")
    @patch("app.workers.scheduler.get_settings")
    def test_returns_started_scheduler(
        self, mock_get_settings, _store, mock_sched_cls, mock_ensure
    ):
        from app.workers.scheduler import start_scheduler

        mock_get_settings.return_value = _make_settings()
        mock_sched = MagicMock()
        mock_sched_cls.return_value = mock_sched

        result = start_scheduler()

        assert result is mock_sched
        mock_sched.start.assert_called_once()

    @patch("app.workers.scheduler._ensure_apscheduler_table")
    @patch("app.workers.scheduler.AsyncIOScheduler")
    @patch("app.workers.scheduler.SQLAlchemyJobStore")
    @patch("app.workers.scheduler.get_settings")
    def test_asyncpg_stripped_from_db_url(
        self, mock_get_settings, _store, mock_sched_cls, mock_ensure
    ):
        from app.workers.scheduler import start_scheduler

        mock_get_settings.return_value = _make_settings(
            DATABASE_URL="postgresql+asyncpg://u:p@host/mydb"
        )
        mock_sched_cls.return_value = MagicMock()

        start_scheduler()

        url_passed = mock_ensure.call_args[0][0]
        assert "+asyncpg" not in url_passed
        assert url_passed == "postgresql://u:p@host/mydb"

    @patch("app.workers.scheduler._ensure_apscheduler_table")
    @patch("app.workers.scheduler.AsyncIOScheduler")
    @patch("app.workers.scheduler.SQLAlchemyJobStore")
    @patch("app.workers.scheduler.get_settings")
    def test_registers_exactly_two_jobs(
        self, mock_get_settings, _store, mock_sched_cls, mock_ensure
    ):
        from app.workers.scheduler import start_scheduler

        mock_get_settings.return_value = _make_settings()
        mock_sched = MagicMock()
        mock_sched_cls.return_value = mock_sched

        start_scheduler()

        assert mock_sched.add_job.call_count == 2
        job_ids = {c.kwargs["id"] for c in mock_sched.add_job.call_args_list}
        assert job_ids == {"reconciliation_run", "drift_check"}

    @patch("app.workers.scheduler._ensure_apscheduler_table")
    @patch("app.workers.scheduler.get_settings")
    def test_invalid_reconciliation_cron_raises(self, mock_get_settings, _ensure):
        from app.workers.scheduler import start_scheduler

        mock_get_settings.return_value = _make_settings(RECONCILIATION_CRON="bad cron")
        with pytest.raises(ValueError, match="Invalid cron string"):
            start_scheduler()

    @patch("app.workers.scheduler._ensure_apscheduler_table")
    @patch("app.workers.scheduler.get_settings")
    def test_invalid_drift_cron_raises(self, mock_get_settings, _ensure):
        from app.workers.scheduler import start_scheduler

        mock_get_settings.return_value = _make_settings(DRIFT_CHECK_CRON="* * *")
        with pytest.raises(ValueError, match="Invalid cron string"):
            start_scheduler()

    @patch("app.workers.scheduler._ensure_apscheduler_table")
    @patch("app.workers.scheduler.AsyncIOScheduler")
    @patch("app.workers.scheduler.SQLAlchemyJobStore")
    @patch("app.workers.scheduler.get_settings")
    def test_ensure_called_before_start(
        self, mock_get_settings, _store, mock_sched_cls, mock_ensure
    ):
        """_ensure_apscheduler_table must precede scheduler.start()."""
        from app.workers.scheduler import start_scheduler

        call_order = []
        mock_ensure.side_effect = lambda *a, **k: call_order.append("ensure")
        mock_sched = MagicMock()
        mock_sched.start.side_effect = lambda: call_order.append("start")
        mock_sched_cls.return_value = mock_sched
        mock_get_settings.return_value = _make_settings()

        start_scheduler()

        assert call_order == ["ensure", "start"]


# ── stop_scheduler ─────────────────────────────────────────────────────────────


class TestStopScheduler:

    def test_calls_shutdown_no_wait(self):
        from app.workers.scheduler import stop_scheduler

        mock_sched = MagicMock()
        stop_scheduler(mock_sched)
        mock_sched.shutdown.assert_called_once_with(wait=False)


# ── reconciliation_job ─────────────────────────────────────────────────────────


class TestReconciliationJob:

    @pytest.mark.asyncio
    @patch("app.workers.scheduler.get_settings")
    async def test_runs_for_default_source(self, mock_get_settings):
        from app.workers.scheduler import reconciliation_job

        mock_get_settings.return_value = _make_settings()

        mock_result = MagicMock()
        mock_result.match_rate = 0.95
        mock_result.matched = [MagicMock()] * 9
        mock_result.unmatched_transactions = [MagicMock()]
        mock_result.total_transactions = 10
        mock_result.run_id = "run-abc"

        mock_orchestrator = AsyncMock()
        mock_orchestrator.run.return_value = mock_result
        ctx, _ = _mock_db_context()

        with patch("app.database.get_db_context", return_value=ctx), patch(
            "app.services.matcher.ReconciliationOrchestrator",
            return_value=mock_orchestrator,
        ):
            await reconciliation_job()

        mock_orchestrator.run.assert_called_once_with(source_name="default")

    @pytest.mark.asyncio
    @patch("app.workers.scheduler.send_alert")
    @patch("app.workers.scheduler.get_settings")
    async def test_alert_fires_when_match_rate_below_threshold(
        self, mock_get_settings, mock_send_alert
    ):
        from app.workers.scheduler import reconciliation_job

        mock_get_settings.return_value = _make_settings(
            ALERT_ON_LOW_MATCH_RATE=True,
            ALERT_MATCH_RATE_THRESHOLD=0.80,
        )
        mock_send_alert.return_value = None

        mock_result = MagicMock()
        mock_result.match_rate = 0.60
        mock_result.matched = []
        mock_result.unmatched_transactions = [MagicMock()] * 4
        mock_result.total_transactions = 10
        mock_result.run_id = "run-low"

        mock_orchestrator = AsyncMock()
        mock_orchestrator.run.return_value = mock_result
        ctx, _ = _mock_db_context()

        with patch("app.database.get_db_context", return_value=ctx), patch(
            "app.services.matcher.ReconciliationOrchestrator",
            return_value=mock_orchestrator,
        ):
            await reconciliation_job()

        mock_send_alert.assert_called_once()
        assert "Low Match Rate" in mock_send_alert.call_args.kwargs["title"]

    @pytest.mark.asyncio
    @patch("app.workers.scheduler.send_alert")
    @patch("app.workers.scheduler.get_settings")
    async def test_no_alert_when_match_rate_above_threshold(
        self, mock_get_settings, mock_send_alert
    ):
        from app.workers.scheduler import reconciliation_job

        mock_get_settings.return_value = _make_settings(
            ALERT_ON_LOW_MATCH_RATE=True,
            ALERT_MATCH_RATE_THRESHOLD=0.80,
        )

        mock_result = MagicMock()
        mock_result.match_rate = 0.95
        mock_result.matched = [MagicMock()] * 9
        mock_result.unmatched_transactions = []
        mock_result.total_transactions = 9
        mock_result.run_id = "run-high"

        mock_orchestrator = AsyncMock()
        mock_orchestrator.run.return_value = mock_result
        ctx, _ = _mock_db_context()

        with patch("app.database.get_db_context", return_value=ctx), patch(
            "app.services.matcher.ReconciliationOrchestrator",
            return_value=mock_orchestrator,
        ):
            await reconciliation_job()

        mock_send_alert.assert_not_called()

    @pytest.mark.asyncio
    @patch("app.workers.scheduler.send_alert")
    @patch("app.workers.scheduler.get_settings")
    async def test_no_alert_when_zero_transactions(
        self, mock_get_settings, mock_send_alert
    ):
        from app.workers.scheduler import reconciliation_job

        mock_get_settings.return_value = _make_settings(
            ALERT_ON_LOW_MATCH_RATE=True,
            ALERT_MATCH_RATE_THRESHOLD=0.80,
        )

        mock_result = MagicMock()
        mock_result.match_rate = 0.0
        mock_result.matched = []
        mock_result.unmatched_transactions = []
        mock_result.total_transactions = 0
        mock_result.run_id = "run-empty"

        mock_orchestrator = AsyncMock()
        mock_orchestrator.run.return_value = mock_result
        ctx, _ = _mock_db_context()

        with patch("app.database.get_db_context", return_value=ctx), patch(
            "app.services.matcher.ReconciliationOrchestrator",
            return_value=mock_orchestrator,
        ):
            await reconciliation_job()

        mock_send_alert.assert_not_called()

    @pytest.mark.asyncio
    async def test_exception_is_caught_not_raised(self):
        from app.workers.scheduler import reconciliation_job

        ctx, _ = _mock_db_context()
        ctx.__aenter__ = AsyncMock(side_effect=Exception("DB unavailable"))

        with patch("app.database.get_db_context", return_value=ctx):
            await reconciliation_job()  # Must not raise


# ── drift_check_job ────────────────────────────────────────────────────────────


class TestDriftCheckJob:

    @pytest.mark.asyncio
    async def test_clean_run_no_events(self):
        from app.workers.scheduler import drift_check_job

        mock_analyzer = AsyncMock()
        mock_analyzer.analyze_latest.return_value = []
        ctx, _ = _mock_db_context()

        with patch("app.database.get_db_context", return_value=ctx), patch(
            "app.services.drift_analyzer.DriftAnalyzer", return_value=mock_analyzer
        ):
            await drift_check_job()

        mock_analyzer.analyze_latest.assert_called_once_with(source_name="default")

    @pytest.mark.asyncio
    @patch("app.workers.scheduler.send_alert")
    @patch("app.workers.scheduler.get_settings")
    async def test_high_severity_triggers_alert(
        self, mock_get_settings, mock_send_alert
    ):
        from app.workers.scheduler import drift_check_job

        mock_get_settings.return_value = _make_settings(ALERT_ON_DRIFT=True)
        mock_send_alert.return_value = None

        high_event = MagicMock()
        high_event.severity = "high"
        high_event.metric_name = "amount_mean"
        high_event.hypothesis = "Mean shifted 20%"

        mock_analyzer = AsyncMock()
        mock_analyzer.analyze_latest.return_value = [high_event]
        ctx, _ = _mock_db_context()

        with patch("app.database.get_db_context", return_value=ctx), patch(
            "app.services.drift_analyzer.DriftAnalyzer", return_value=mock_analyzer
        ):
            await drift_check_job()

        mock_send_alert.assert_called_once()
        assert "Drift" in mock_send_alert.call_args.kwargs["title"]

    @pytest.mark.asyncio
    @patch("app.workers.scheduler.send_alert")
    @patch("app.workers.scheduler.get_settings")
    async def test_low_severity_no_alert(self, mock_get_settings, mock_send_alert):
        from app.workers.scheduler import drift_check_job

        mock_get_settings.return_value = _make_settings(ALERT_ON_DRIFT=True)

        low_event = MagicMock()
        low_event.severity = "low"
        low_event.metric_name = "amount_stddev"
        low_event.hypothesis = "Minor variance increase"

        mock_analyzer = AsyncMock()
        mock_analyzer.analyze_latest.return_value = [low_event]
        ctx, _ = _mock_db_context()

        with patch("app.database.get_db_context", return_value=ctx), patch(
            "app.services.drift_analyzer.DriftAnalyzer", return_value=mock_analyzer
        ):
            await drift_check_job()

        mock_send_alert.assert_not_called()

    @pytest.mark.asyncio
    async def test_insufficient_data_skipped_not_raised(self):
        from app.workers.scheduler import drift_check_job
        from app.core.exceptions import InsufficientDataError

        mock_analyzer = AsyncMock()
        # InsufficientDataError takes two positional args: (message, available)
        mock_analyzer.analyze_latest.side_effect = InsufficientDataError(
            "not enough rows", 5
        )
        ctx, _ = _mock_db_context()

        with patch("app.database.get_db_context", return_value=ctx), patch(
            "app.services.drift_analyzer.DriftAnalyzer", return_value=mock_analyzer
        ):
            await drift_check_job()  # Must not raise

    @pytest.mark.asyncio
    async def test_generic_exception_is_caught(self):
        from app.workers.scheduler import drift_check_job

        mock_analyzer = AsyncMock()
        mock_analyzer.analyze_latest.side_effect = RuntimeError("unexpected")
        ctx, _ = _mock_db_context()

        with patch("app.database.get_db_context", return_value=ctx), patch(
            "app.services.drift_analyzer.DriftAnalyzer", return_value=mock_analyzer
        ):
            await drift_check_job()  # Must not raise

    @pytest.mark.asyncio
    @patch("app.workers.scheduler.get_settings")
    async def test_events_persisted_to_db_session(self, mock_get_settings):
        from app.workers.scheduler import drift_check_job

        mock_get_settings.return_value = _make_settings(ALERT_ON_DRIFT=False)

        events = [MagicMock(severity="low"), MagicMock(severity="low")]
        mock_analyzer = AsyncMock()
        mock_analyzer.analyze_latest.return_value = events
        ctx, mock_db = _mock_db_context()

        with patch("app.database.get_db_context", return_value=ctx), patch(
            "app.services.drift_analyzer.DriftAnalyzer", return_value=mock_analyzer
        ):
            await drift_check_job()

        assert mock_db.add.call_count == 2


# ── send_alert ─────────────────────────────────────────────────────────────────


class TestSendAlert:

    @pytest.mark.asyncio
    @patch("app.workers.scheduler.get_settings")
    async def test_no_op_when_webhook_url_is_none(self, mock_get_settings):
        from app.workers.scheduler import send_alert

        mock_get_settings.return_value = _make_settings(ALERT_WEBHOOK_URL=None)

        with patch("httpx.AsyncClient") as mock_httpx:
            await send_alert(title="Test", message="msg")
            mock_httpx.assert_not_called()

    @pytest.mark.asyncio
    @patch("app.workers.scheduler.get_settings")
    async def test_posts_correct_slack_payload(self, mock_get_settings):
        from app.workers.scheduler import send_alert

        mock_get_settings.return_value = _make_settings(
            ALERT_WEBHOOK_URL="https://hooks.slack.com/services/test"
        )

        mock_response = MagicMock()
        mock_client = AsyncMock()
        mock_client.post.return_value = mock_response

        with patch("httpx.AsyncClient") as mock_httpx:
            mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

            await send_alert(title="⚠️ Low Match Rate", message="Source: default")

        _, kwargs = mock_client.post.call_args
        payload = kwargs["json"]
        assert "⚠️ Low Match Rate" in payload["text"]
        assert "Source: default" in payload["text"]
        assert payload["username"] == "Drift Recon Bot"
        mock_response.raise_for_status.assert_called_once()

    @pytest.mark.asyncio
    @patch("app.workers.scheduler.get_settings")
    async def test_http_error_is_swallowed(self, mock_get_settings):
        from app.workers.scheduler import send_alert

        mock_get_settings.return_value = _make_settings(
            ALERT_WEBHOOK_URL="https://hooks.slack.com/services/test"
        )

        mock_client = AsyncMock()
        mock_client.post.side_effect = Exception("Connection timeout")

        with patch("httpx.AsyncClient") as mock_httpx:
            mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

            await send_alert(title="Test", message="msg")  # Must not raise

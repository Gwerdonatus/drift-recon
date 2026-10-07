"""
Background job scheduler using APScheduler.

Why APScheduler over Celery:
- No broker required (no Redis queue, no worker process)
- Single-process deployment — fits a solo-engineer VPS setup
- Persistent job store in PostgreSQL (survives restarts)
- Sufficient for reconciliation runs that happen every few hours

When to switch to Celery:
- You need parallel workers across multiple machines
- Jobs need to be triggered by external events (not just schedule)
- Queue depth monitoring becomes a requirement
"""

from __future__ import annotations


from apscheduler.executors.asyncio import AsyncIOExecutor
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError, OperationalError

from app.config import get_settings
from app.core.logging import get_logger

log = get_logger(__name__)


def _ensure_apscheduler_table(sync_db_url: str, tablename: str) -> None:
    """
    Pre-create the APScheduler jobs table before the scheduler starts.

    APScheduler's SQLAlchemyJobStore calls jobs_t.create(engine, checkfirst=True)
    on scheduler.start(). On Postgres, this hits a race condition on the pg_type
    catalog when the table already exists from a prior run on a persistent volume:

        UniqueViolation: duplicate key value violates unique constraint
        "pg_type_typname_nsp_index"

    Creating the table here — before handing control to APScheduler — means
    APScheduler's own checkfirst=True finds it already present and skips DDL
    entirely, avoiding the race.
    """
    engine = create_engine(sync_db_url)
    try:
        # Use a savepoint so a failure here doesn't poison the outer connection
        with engine.begin() as conn:
            conn.execute(
                text(
                    f"""
                    CREATE TABLE IF NOT EXISTS {tablename} (
                        id          VARCHAR(191) NOT NULL,
                        next_run_time FLOAT(25),
                        job_state   BYTEA        NOT NULL,
                        PRIMARY KEY (id)
                    )
                """
                )
            )
            conn.execute(
                text(
                    f"""
                    CREATE INDEX IF NOT EXISTS ix_{tablename}_next_run_time
                    ON {tablename} (next_run_time)
                """
                )
            )
        log.info("apscheduler_table_ensured", tablename=tablename)
    except (IntegrityError, OperationalError) as exc:
        # Already exists — this is fine; APScheduler will reuse it
        log.info(
            "apscheduler_table_already_exists",
            tablename=tablename,
            detail=str(exc),
        )
    finally:
        engine.dispose()


def start_scheduler() -> AsyncIOScheduler:
    settings = get_settings()

    # Use sync DB URL for APScheduler (it uses its own SQLAlchemy session)
    sync_db_url = settings.DATABASE_URL.replace("+asyncpg", "")

    tablename = "apscheduler_jobs"

    # Guard against the pg_type race condition on persistent volumes.
    # Must happen before AsyncIOScheduler.start() is called.
    _ensure_apscheduler_table(sync_db_url, tablename)

    scheduler = AsyncIOScheduler(
        jobstores={"default": SQLAlchemyJobStore(url=sync_db_url, tablename=tablename)},
        executors={"default": AsyncIOExecutor()},
        job_defaults={
            "coalesce": True,  # If job missed multiple times, run once
            "max_instances": 1,  # Never run same job concurrently
            "misfire_grace_time": 300,  # 5 minute grace window
        },
    )

    # Parse cron strings from config
    def parse_cron(cron_str: str) -> dict:
        parts = cron_str.strip().split()
        if len(parts) != 5:
            raise ValueError(f"Invalid cron string: {cron_str}")
        minute, hour, day, month, day_of_week = parts
        return dict(
            minute=minute,
            hour=hour,
            day=day,
            month=month,
            day_of_week=day_of_week,
        )

    recon_cron = parse_cron(settings.RECONCILIATION_CRON)
    drift_cron = parse_cron(settings.DRIFT_CHECK_CRON)

    scheduler.add_job(
        reconciliation_job,
        trigger="cron",
        id="reconciliation_run",
        replace_existing=True,
        **recon_cron,
    )

    scheduler.add_job(
        drift_check_job,
        trigger="cron",
        id="drift_check",
        replace_existing=True,
        **drift_cron,
    )

    scheduler.start()
    log.info(
        "scheduler_started",
        reconciliation_cron=settings.RECONCILIATION_CRON,
        drift_cron=settings.DRIFT_CHECK_CRON,
    )
    return scheduler


def stop_scheduler(scheduler: AsyncIOScheduler) -> None:
    scheduler.shutdown(wait=False)
    log.info("scheduler_stopped")


async def reconciliation_job() -> None:
    """
    Scheduled reconciliation run.
    Runs for all configured sources — extend this list or make it
    database-driven as you add more sources.
    """
    from app.database import get_db_context
    from app.services.matcher import ReconciliationOrchestrator

    log.info("scheduled_reconciliation_start")

    # TODO: Load active sources from DB instead of hardcoding
    sources = ["default"]  # Replace with your actual source names

    for source in sources:
        try:
            async with get_db_context() as db:
                orchestrator = ReconciliationOrchestrator(db=db)
                result = await orchestrator.run(source_name=source)
                log.info(
                    "scheduled_reconciliation_complete",
                    source=source,
                    match_rate=f"{result.match_rate:.2%}",
                    matched=len(result.matched),
                    unmatched=len(result.unmatched_transactions),
                )

                # Send alert if match rate is below threshold
                settings = get_settings()
                if (
                    settings.ALERT_ON_LOW_MATCH_RATE
                    and result.match_rate < settings.ALERT_MATCH_RATE_THRESHOLD
                    and result.total_transactions > 0
                ):
                    await send_alert(
                        title="⚠️ Low Match Rate Detected",
                        message=(
                            f"Source: {source}\n"
                            f"Match rate: {result.match_rate:.1%} "
                            f"(threshold: {settings.ALERT_MATCH_RATE_THRESHOLD:.1%})\n"
                            f"Run: {result.run_id}"
                        ),
                    )

        except Exception as e:
            log.error("scheduled_reconciliation_failed", source=source, error=str(e))


async def drift_check_job() -> None:
    """Scheduled drift analysis."""
    from app.core.exceptions import InsufficientDataError
    from app.database import get_db_context
    from app.services.drift_analyzer import DriftAnalyzer

    log.info("scheduled_drift_check_start")
    sources = ["default"]

    for source in sources:
        try:
            async with get_db_context() as db:
                analyzer = DriftAnalyzer(db=db)
                events = await analyzer.analyze_latest(source_name=source)

                if events:
                    for event in events:
                        db.add(event)

                    settings = get_settings()
                    if settings.ALERT_ON_DRIFT:
                        high_events = [e for e in events if e.severity == "high"]
                        if high_events:
                            await send_alert(
                                title="🚨 High Severity Drift Detected",
                                message="\n".join(
                                    f"[{e.severity.upper()}] {e.metric_name}: {e.hypothesis}"
                                    for e in high_events
                                ),
                            )

                    log.warning(
                        "drift_events_persisted",
                        source=source,
                        count=len(events),
                    )
                else:
                    log.info("drift_check_clean", source=source)

        except InsufficientDataError as e:
            log.info(
                "drift_check_skipped_insufficient_data", source=source, detail=str(e)
            )
        except Exception as e:
            log.error("drift_check_failed", source=source, error=str(e))


async def send_alert(title: str, message: str) -> None:
    """Send webhook alert (Slack-compatible payload)."""
    import httpx

    settings = get_settings()
    if not settings.ALERT_WEBHOOK_URL:
        return

    payload = {
        "text": f"*{title}*\n{message}",
        "username": "Drift Recon Bot",
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(settings.ALERT_WEBHOOK_URL, json=payload)
            resp.raise_for_status()
    except Exception as e:
        log.error("alert_webhook_failed", error=str(e))

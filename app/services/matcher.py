"""
Reconciliation matching engine.

Algorithm: Multi-factor confidence scoring with configurable weights.

Confidence = w_amount * score_amount
           + w_date   * score_date
           + w_ref    * score_reference
           + w_desc   * score_description

Score factors:

  amount_score:
    - 1.0  if |delta| / |bank_amount| == 0 (exact)
    - 0.9  if |delta| / |bank_amount| <= tolerance
    - 0.5  if |delta| / |bank_amount| <= tolerance * 3
    - 0.0  otherwise
    Rationale: Amount is the strongest signal. Penalties are steep.

  date_score:
    - 1.0  same day
    - 0.9  1 day apart
    - 0.7  2 days apart
    - 0.5  3 days apart (cutoff at DATE_TOLERANCE_DAYS)
    - 0.0  beyond tolerance
    Rationale: Bank value dates often lag internal posting dates by 1-3 days.

  reference_score:
    - 1.0  exact match (case-insensitive)
    - 0.85 fuzzy token sort ratio >= 90
    - 0.60 fuzzy ratio >= 70
    - 0.0  one/both sides missing or below threshold
    Rationale: References are often reformatted by banks (truncation, prefixes).

  description_score:
    - Uses rapidfuzz token_set_ratio (handles word reordering)
    - 1.0  >= 95%
    - linear scale 0.0–1.0 between 0% and 95%

Matching strategy:
  1. Filter candidates by amount sign (debit vs credit must match)
  2. Filter by date window (±DATE_TOLERANCE_DAYS)
  3. Score all candidate pairs
  4. Greedy assignment: assign highest-confidence pairs first,
     mark both sides as consumed to prevent double-matching

Complexity: O(T * B) where T=transactions, B=bank entries in window.
For typical batches (<10k records), this is fine. Beyond that,
consider blocking on amount range to reduce candidate set.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import numpy as np
from rapidfuzz import fuzz
from sqlalchemy import and_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.core.logging import get_logger
from app.models import (
    BankStatement,
    MatchStatus,
    ReconciliationResult,
    ReconciliationSnapshot,
    Transaction,
    TransactionStatus,
)

log = get_logger(__name__)


@dataclass
class MatchCandidate:
    transaction_id: uuid.UUID
    bank_statement_id: uuid.UUID
    score_amount: float
    score_date: float
    score_reference: float
    score_description: float
    confidence: float
    amount_delta: Decimal
    date_delta_days: int
    match_reason: str


@dataclass
class MatcherResult:
    run_id: str
    source_name: str
    started_at: datetime
    completed_at: datetime
    total_transactions: int
    total_bank_entries: int
    matched: list[MatchCandidate] = field(default_factory=list)
    review: list[MatchCandidate] = field(default_factory=list)
    unmatched_transactions: list[uuid.UUID] = field(default_factory=list)
    unmatched_bank: list[uuid.UUID] = field(default_factory=list)

    @property
    def match_rate(self) -> float:
        if self.total_transactions == 0:
            return 0.0
        return len(self.matched) / self.total_transactions

    @property
    def duration_seconds(self) -> float:
        return (self.completed_at - self.started_at).total_seconds()


# ─── Scoring functions ─────────────────────────────────────────────────────────


def score_amount(
    txn_amount: Decimal,
    bank_amount: Decimal,
    tolerance_pct: float,
) -> tuple[float, Decimal]:
    """
    Returns (score, delta).
    Delta is positive if bank > internal (bank received more than we recorded).
    """
    delta = bank_amount - txn_amount
    if bank_amount == 0:
        return 0.0, delta

    relative_diff = abs(float(delta)) / abs(float(bank_amount))

    if relative_diff == 0:
        return 1.0, delta
    elif relative_diff <= tolerance_pct:
        return 0.9, delta
    elif relative_diff <= tolerance_pct * 3:
        return 0.5, delta
    else:
        return 0.0, delta


def score_date(
    txn_date: date,
    bank_date: date,
    tolerance_days: int,
) -> tuple[float, int]:
    """Returns (score, delta_days). delta_days = bank_date - txn_date."""
    delta = (bank_date - txn_date).days
    abs_delta = abs(delta)

    score_map = {0: 1.0, 1: 0.9, 2: 0.7, 3: 0.5}
    if abs_delta > tolerance_days:
        return 0.0, delta
    return score_map.get(abs_delta, 0.3), delta


def score_reference(ref_a: str | None, ref_b: str | None) -> float:
    """
    Fuzzy reference matching.
    Banks often truncate, prefix, or reformat payment references.
    Token sort ratio handles: "REF-001 PAYMENT" vs "PAYMENT REF-001"
    """
    if not ref_a or not ref_b:
        return 0.0

    a = ref_a.strip().upper()
    b = ref_b.strip().upper()

    if a == b:
        return 1.0

    token_sort = fuzz.token_sort_ratio(a, b) / 100.0
    partial = fuzz.partial_ratio(a, b) / 100.0

    best = max(token_sort, partial)

    if best >= 0.90:
        return 0.85
    elif best >= 0.70:
        return 0.60
    else:
        return 0.0


def score_description(desc_a: str | None, desc_b: str | None) -> float:
    """
    Token set ratio: handles word reordering and subset matches.
    E.g., "AMAZON PRIME MONTHLY" matches "PRIME MONTHLY AMAZON"
    """
    if not desc_a or not desc_b:
        return 0.0

    ratio = (
        fuzz.token_set_ratio(
            desc_a.upper()[:200],  # Truncate to avoid O(n²) on long strings
            desc_b.upper()[:200],
        )
        / 100.0
    )

    return min(ratio / 0.95, 1.0)  # Linear scale, cap at 1.0


def compute_confidence(
    s_amount: float,
    s_date: float,
    s_reference: float,
    s_description: float,
    weights: dict,
) -> float:
    return (
        weights["amount"] * s_amount
        + weights["date"] * s_date
        + weights["reference"] * s_reference
        + weights["description"] * s_description
    )


def build_match_reason(
    confidence: float,
    s_amount: float,
    s_date: float,
    s_reference: float,
    s_description: float,
    amount_delta: Decimal,
    date_delta: int,
) -> str:
    """Generate a human-readable explanation for the match result."""
    parts = []

    if s_amount >= 0.9:
        parts.append(f"exact amount match (Δ={amount_delta:+.2f})")
    elif s_amount >= 0.5:
        parts.append(f"near-amount match (Δ={amount_delta:+.2f})")
    else:
        parts.append(f"amount mismatch (Δ={amount_delta:+.2f}, score={s_amount:.2f})")

    if s_date >= 0.9:
        parts.append(f"same/next-day date (Δ={date_delta:+d}d)")
    elif s_date > 0:
        parts.append(f"date within tolerance (Δ={date_delta:+d}d)")
    else:
        parts.append(f"date outside tolerance (Δ={date_delta:+d}d)")

    if s_reference >= 0.85:
        parts.append("strong reference match")
    elif s_reference > 0:
        parts.append(f"partial reference match ({s_reference:.0%})")

    if s_description >= 0.7:
        parts.append("description match")

    return f"[{confidence:.2%}] " + "; ".join(parts)


# ─── Matcher ───────────────────────────────────────────────────────────────────


class ReconciliationMatcher:
    """
    Stateless matching engine. Load transactions and bank entries,
    run match(), get back a MatcherResult.
    """

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()

    def match(
        self,
        transactions: list[dict],
        bank_entries: list[dict],
        run_id: str,
        source_name: str,
    ) -> MatcherResult:
        """
        Core matching algorithm. Pure Python/numpy — no DB access here.
        This keeps the matching logic testable without a database.
        """
        started_at = datetime.now(timezone.utc)
        weights = self.settings.match_weights
        tolerance_pct = self.settings.AMOUNT_TOLERANCE_PERCENT
        tolerance_days = self.settings.DATE_TOLERANCE_DAYS
        match_threshold = self.settings.MATCH_CONFIDENCE_THRESHOLD
        review_threshold = self.settings.MATCH_REVIEW_THRESHOLD

        log.info(
            "matcher_start",
            run_id=run_id,
            transactions=len(transactions),
            bank_entries=len(bank_entries),
        )

        # Candidate generation: score all pairs where amount signs match
        # and dates are within the tolerance window
        all_candidates: list[MatchCandidate] = []

        consumed_transactions: set[uuid.UUID] = set()
        consumed_bank: set[uuid.UUID] = set()

        for txn in transactions:
            txn_date = txn["transaction_date"]
            if isinstance(txn_date, str):
                txn_date = date.fromisoformat(txn_date)
            date_min = txn_date - timedelta(days=tolerance_days)
            date_max = txn_date + timedelta(days=tolerance_days)

            for bank in bank_entries:
                # Provider payment identity must agree before fuzzy scoring.
                if source_name.startswith("stripe-sandbox:") and (
                    not txn.get("reference")
                    or txn["reference"] != bank.get("reference")
                ):
                    continue
                # Currency is a hard eligibility rule, never a fuzzy score.
                if not txn.get("currency") or txn.get("currency") != bank.get(
                    "currency"
                ):
                    continue
                bank_date = bank["value_date"]
                if isinstance(bank_date, str):
                    bank_date = date.fromisoformat(bank_date)

                # Pre-filter: sign must match (both positive or both negative)
                txn_amt = Decimal(str(txn["amount"]))
                bank_amt = Decimal(str(bank["amount"]))
                if (txn_amt > 0) != (bank_amt > 0):
                    continue

                # Pre-filter: date window
                if not (date_min <= bank_date <= date_max):
                    continue

                # Score
                s_amount, delta_amount = score_amount(txn_amt, bank_amt, tolerance_pct)
                if s_amount == 0.0:
                    continue  # Skip zero-amount-score pairs (optimisation)

                s_date, delta_days = score_date(txn_date, bank_date, tolerance_days)
                s_ref = score_reference(txn.get("reference"), bank.get("reference"))
                s_desc = score_description(
                    txn.get("description"), bank.get("description")
                )

                confidence = compute_confidence(
                    s_amount, s_date, s_ref, s_desc, weights
                )

                if confidence < review_threshold:
                    continue

                reason = build_match_reason(
                    confidence,
                    s_amount,
                    s_date,
                    s_ref,
                    s_desc,
                    delta_amount,
                    delta_days,
                )

                all_candidates.append(
                    MatchCandidate(
                        transaction_id=txn["id"],
                        bank_statement_id=bank["id"],
                        score_amount=s_amount,
                        score_date=s_date,
                        score_reference=s_ref,
                        score_description=s_desc,
                        confidence=confidence,
                        amount_delta=delta_amount,
                        date_delta_days=delta_days,
                        match_reason=reason,
                    )
                )

        # Sort by confidence descending, then assign greedily
        # (highest confidence pairs are locked in first)
        all_candidates.sort(key=lambda c: c.confidence, reverse=True)

        matched: list[MatchCandidate] = []
        review: list[MatchCandidate] = []

        for candidate in all_candidates:
            if (
                candidate.transaction_id in consumed_transactions
                or candidate.bank_statement_id in consumed_bank
            ):
                continue

            if candidate.confidence >= match_threshold:
                matched.append(candidate)
                consumed_transactions.add(candidate.transaction_id)
                consumed_bank.add(candidate.bank_statement_id)
            else:
                # REVIEW band: best candidate below match threshold
                review.append(candidate)
                consumed_transactions.add(candidate.transaction_id)
                consumed_bank.add(candidate.bank_statement_id)

        # Unmatched = anything not consumed
        all_txn_ids = {t["id"] for t in transactions}
        all_bank_ids = {b["id"] for b in bank_entries}

        result = MatcherResult(
            run_id=run_id,
            source_name=source_name,
            started_at=started_at,
            completed_at=datetime.now(timezone.utc),
            total_transactions=len(transactions),
            total_bank_entries=len(bank_entries),
            matched=matched,
            review=review,
            unmatched_transactions=list(all_txn_ids - consumed_transactions),
            unmatched_bank=list(all_bank_ids - consumed_bank),
        )

        log.info(
            "matcher_complete",
            run_id=run_id,
            matched=len(matched),
            review=len(review),
            unmatched_txn=len(result.unmatched_transactions),
            unmatched_bank=len(result.unmatched_bank),
            match_rate=f"{result.match_rate:.2%}",
            duration_s=f"{result.duration_seconds:.3f}",
        )

        return result


# ─── Orchestration (DB layer) ──────────────────────────────────────────────────


class ReconciliationOrchestrator:
    """
    Coordinates: DB load → match → persist results → create snapshot.
    Separated from the Matcher so pure matching logic stays testable.
    """

    def __init__(self, db: AsyncSession, settings: Settings | None = None):
        self.db = db
        self.settings = (settings or get_settings()).model_copy()
        self.matcher = ReconciliationMatcher(self.settings)

    async def run(
        self,
        source_name: str,
        from_date: date | None = None,
        to_date: date | None = None,
        confidence_threshold: float | None = None,
        review_threshold: float | None = None,
    ) -> MatcherResult:
        """Full reconciliation run for a source."""
        run_id = f"run_{uuid.uuid4().hex[:12]}_{int(time.time())}"

        if confidence_threshold is not None:
            self.settings.MATCH_CONFIDENCE_THRESHOLD = confidence_threshold
        if review_threshold is not None:
            self.settings.MATCH_REVIEW_THRESHOLD = review_threshold

        # Serialize runs per source so concurrent callers cannot consume the same records.
        from sqlalchemy import text

        await self.db.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:source))"),
            {"source": source_name},
        )

        # Load pending transactions
        txn_query = select(Transaction).where(
            and_(
                Transaction.source == source_name,
                Transaction.status == TransactionStatus.PENDING,
                Transaction.deleted_at.is_(None),
            )
        )
        if from_date:
            txn_query = txn_query.where(Transaction.transaction_date >= from_date)
        if to_date:
            txn_query = txn_query.where(Transaction.transaction_date <= to_date)

        txn_result = await self.db.execute(txn_query)
        transactions = txn_result.scalars().all()

        # Load pending bank entries
        bank_query = select(BankStatement).where(
            and_(
                BankStatement.bank_name == source_name,
                BankStatement.status == TransactionStatus.PENDING,
                BankStatement.deleted_at.is_(None),
            )
        )
        if from_date:
            bank_query = bank_query.where(BankStatement.value_date >= from_date)
        if to_date:
            bank_query = bank_query.where(BankStatement.value_date <= to_date)

        bank_result = await self.db.execute(bank_query)
        bank_entries = bank_result.scalars().all()

        # Serialize to dicts for matcher (decoupled from ORM)
        txn_dicts = [
            {
                "id": t.id,
                "transaction_date": t.transaction_date,
                "amount": t.amount,
                "currency": t.currency,
                "reference": t.reference,
                "description": t.description,
            }
            for t in transactions
        ]
        bank_dicts = [
            {
                "id": b.id,
                "value_date": b.value_date,
                "amount": b.amount,
                "currency": b.currency,
                "reference": b.reference,
                "description": b.description,
            }
            for b in bank_entries
        ]

        # Run matching (pure, no DB)
        result = self.matcher.match(txn_dicts, bank_dicts, run_id, source_name)

        # Persist results
        await self._persist_results(result)

        # Create snapshot
        await self._create_snapshot(result)
        await self.db.flush()  # Drift analysis must see this run, not the previous one.

        return result

    async def _persist_results(self, result: MatcherResult) -> None:
        """Write reconciliation results and update transaction/bank statuses."""
        now = datetime.now(timezone.utc)
        records: list[ReconciliationResult] = []

        for c in result.matched:
            records.append(
                ReconciliationResult(
                    run_id=result.run_id,
                    transaction_id=c.transaction_id,
                    bank_statement_id=c.bank_statement_id,
                    status=MatchStatus.MATCHED,
                    confidence_score=Decimal(str(round(c.confidence, 4))),
                    score_amount=Decimal(str(round(c.score_amount, 4))),
                    score_date=Decimal(str(round(c.score_date, 4))),
                    score_reference=Decimal(str(round(c.score_reference, 4))),
                    score_description=Decimal(str(round(c.score_description, 4))),
                    match_reason=c.match_reason,
                    amount_delta=c.amount_delta,
                    date_delta_days=c.date_delta_days,
                )
            )

        for c in result.review:
            records.append(
                ReconciliationResult(
                    run_id=result.run_id,
                    transaction_id=c.transaction_id,
                    bank_statement_id=c.bank_statement_id,
                    status=MatchStatus.REVIEW,
                    confidence_score=Decimal(str(round(c.confidence, 4))),
                    score_amount=Decimal(str(round(c.score_amount, 4))),
                    score_date=Decimal(str(round(c.score_date, 4))),
                    score_reference=Decimal(str(round(c.score_reference, 4))),
                    score_description=Decimal(str(round(c.score_description, 4))),
                    match_reason=c.match_reason,
                    amount_delta=c.amount_delta,
                    date_delta_days=c.date_delta_days,
                )
            )

        # Unmatched transactions
        for txn_id in result.unmatched_transactions:
            records.append(
                ReconciliationResult(
                    run_id=result.run_id,
                    transaction_id=txn_id,
                    bank_statement_id=None,
                    status=MatchStatus.UNMATCHED,
                    confidence_score=Decimal("0"),
                    score_amount=Decimal("0"),
                    score_date=Decimal("0"),
                    score_reference=Decimal("0"),
                    score_description=Decimal("0"),
                    match_reason="No matching bank entry found",
                )
            )

        self.db.add_all(records)

        # Update transaction statuses
        matched_ids = [c.transaction_id for c in result.matched]
        review_ids = [c.transaction_id for c in result.review]
        unmatched_ids = result.unmatched_transactions

        if matched_ids:
            await self.db.execute(
                update(Transaction)
                .where(Transaction.id.in_(matched_ids))
                .values(status=TransactionStatus.MATCHED, updated_at=now)
            )
        if review_ids:
            await self.db.execute(
                update(Transaction)
                .where(Transaction.id.in_(review_ids))
                .values(status=TransactionStatus.REVIEW, updated_at=now)
            )
        if unmatched_ids:
            await self.db.execute(
                update(Transaction)
                .where(Transaction.id.in_(unmatched_ids))
                .values(status=TransactionStatus.UNMATCHED, updated_at=now)
            )

        # Update bank statement statuses
        bank_matched_ids = [c.bank_statement_id for c in result.matched]
        bank_review_ids = [c.bank_statement_id for c in result.review]
        bank_unmatched_ids = result.unmatched_bank

        if bank_matched_ids:
            await self.db.execute(
                update(BankStatement)
                .where(BankStatement.id.in_(bank_matched_ids))
                .values(status=TransactionStatus.MATCHED, updated_at=now)
            )
        if bank_review_ids:
            await self.db.execute(
                update(BankStatement)
                .where(BankStatement.id.in_(bank_review_ids))
                .values(status=TransactionStatus.REVIEW, updated_at=now)
            )
        if bank_unmatched_ids:
            await self.db.execute(
                update(BankStatement)
                .where(BankStatement.id.in_(bank_unmatched_ids))
                .values(status=TransactionStatus.UNMATCHED, updated_at=now)
            )

    async def _create_snapshot(self, result: MatcherResult) -> None:
        """Persist aggregate metrics for this run (used by drift detection)."""
        all_results = result.matched + result.review
        confidences = [c.confidence for c in all_results] if all_results else []
        amount_deltas = (
            [float(c.amount_delta) for c in all_results] if all_results else []
        )
        date_deltas = [c.date_delta_days for c in all_results] if all_results else []

        matched_ids = [candidate.transaction_id for candidate in result.matched]
        amounts = await self.db.execute(
            select(Transaction.amount).where(Transaction.id.in_(matched_ids))
        )
        matched_amounts = sum(
            (abs(amount) for amount in amounts.scalars()), Decimal("0")
        )

        total = result.total_transactions
        match_rate = (
            Decimal(str(round(result.match_rate, 4))) if total > 0 else Decimal("0")
        )
        review_rate = (
            Decimal(str(round(len(result.review) / total, 4)))
            if total > 0
            else Decimal("0")
        )

        snapshot = ReconciliationSnapshot(
            run_id=result.run_id,
            run_date=result.started_at,
            source_name=result.source_name,
            total_transactions=result.total_transactions,
            total_bank_entries=result.total_bank_entries,
            matched_count=len(result.matched),
            review_count=len(result.review),
            unmatched_count=len(result.unmatched_transactions),
            match_rate=match_rate,
            review_rate=review_rate,
            avg_confidence=(
                Decimal(str(round(float(np.mean(confidences)), 4)))
                if confidences
                else None
            ),
            p50_confidence=(
                Decimal(str(round(float(np.percentile(confidences, 50)), 4)))
                if confidences
                else None
            ),
            p10_confidence=(
                Decimal(str(round(float(np.percentile(confidences, 10)), 4)))
                if confidences
                else None
            ),
            total_amount_matched=matched_amounts,
            avg_amount_delta=(
                Decimal(str(round(float(np.mean(amount_deltas)), 4)))
                if amount_deltas
                else None
            ),
            avg_date_delta_days=(
                Decimal(str(round(float(np.mean(date_deltas)), 2)))
                if date_deltas
                else None
            ),
            max_date_delta_days=(
                max(abs(d) for d in date_deltas) if date_deltas else None
            ),
            run_duration_seconds=Decimal(str(round(result.duration_seconds, 3))),
        )
        self.db.add(snapshot)

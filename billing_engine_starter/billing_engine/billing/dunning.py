from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import Enum
from typing import Optional

from billing_engine.db import (
    InvoiceRepository,
    LedgerRepository,
    SubscriptionRepository,
    PaymentAttemptRepository,
)
from billing_engine.models import (
    Invoice,
    LedgerEntry,
    LedgerDirection,
    SubscriptionStatus,
)
from billing_engine.payments.gateway import PaymentGateway


class DunningState(str, Enum):
    PENDING = "PENDING"
    RETRYING = "RETRYING"
    SUCCEEDED = "SUCCEEDED"
    FAILED_FINAL = "FAILED_FINAL"


@dataclass(frozen=True)
class DunningOutcome:
    state: DunningState
    attempt_no: int
    next_retry_at: Optional[datetime]


RETRY_DELAYS_DAYS = {1: 1, 2: 3}
MAX_ATTEMPTS = 3


class DunningProcess:
    def __init__(
        self,
        gateway: PaymentGateway,
        invoice_repo: InvoiceRepository,
        ledger_repo: LedgerRepository,
        subscription_repo: SubscriptionRepository,
        attempt_repo: PaymentAttemptRepository,
    ) -> None:
        self.gateway = gateway
        self.invoice_repo = invoice_repo
        self.ledger_repo = ledger_repo
        self.subscription_repo = subscription_repo
        self.attempt_repo = attempt_repo

    def attempt(
        self,
        invoice: Invoice,
        customer_id: int,
        now: datetime,
    ) -> DunningOutcome:
        """Try once. Record the attempt. Return the resulting outcome."""

        attempt_no = self.attempt_repo.count_for_invoice(invoice.id) + 1
        result = self.gateway.charge(invoice)

        if result.success:
            self.invoice_repo.mark_paid(invoice.id)

            self.ledger_repo.add(
                LedgerEntry(
                    id=None,
                    invoice_id=invoice.id,
                    customer_id=customer_id,
                    amount=invoice.total,
                    direction=LedgerDirection.CREDIT,
                    reason=f"Payment for invoice {invoice.id}",
                )
            )

            self.attempt_repo.add(
                invoice.id,
                attempt_no,
                "SUCCESS",
                None,
                None,
            )

            return DunningOutcome(
                DunningState.SUCCEEDED,
                attempt_no,
                None,
            )

        if attempt_no >= MAX_ATTEMPTS:
            self.invoice_repo.mark_failed(invoice.id)

            self.subscription_repo.update_status(
                invoice.subscription_id,
                SubscriptionStatus.PAST_DUE,
                past_due_since=now.date(),
            )

            self.attempt_repo.add(
                invoice.id,
                attempt_no,
                "FAILED",
                result.failure_reason,
                None,
            )

            return DunningOutcome(
                DunningState.FAILED_FINAL,
                attempt_no,
                None,
            )

        delay = RETRY_DELAYS_DAYS[attempt_no]
        next_retry = now + timedelta(days=delay)

        self.attempt_repo.add(
            invoice.id,
            attempt_no,
            "FAILED",
            result.failure_reason,
            next_retry,
        )

        return DunningOutcome(
            DunningState.RETRYING,
            attempt_no,
            next_retry,
        )

    @staticmethod
    def should_cancel(
        past_due_since: date,
        today: date,
        grace_days: int = 7,
    ) -> bool:
        """Helper used by BillingCycle to decide PAST_DUE → CANCELLED."""
        return (today - past_due_since).days >= grace_days
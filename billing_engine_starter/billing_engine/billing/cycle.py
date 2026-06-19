"""
BillingCycle — finds due subscriptions, generates invoices, posts ledger DEBITs,
advances the subscription period. Must be IDEMPOTENT (safe to run twice).
"""

from __future__ import annotations

import calendar
import sqlite3
from dataclasses import dataclass
from datetime import date
from typing import Callable

from billing_engine.billing.pipeline import build_invoice
from billing_engine.billing.proration import compute_proration
from billing_engine.db import (
    Database,
    CustomerRepository,
    PlanRepository,
    SubscriptionRepository,
    UsageRecordRepository,
    InvoiceRepository,
    InvoiceLineItemRepository,
    LedgerRepository,
)
from billing_engine.models import (
    BillingPeriod,
    InvoiceLineItem,
    InvoiceStatus,
    LedgerDirection,
    LedgerEntry,
    Subscription,
    SubscriptionStatus,
    Invoice,
    LineItemKind,
)
from billing_engine.money import Money


@dataclass
class BillingResult:
    invoices_created: int
    invoices_skipped_duplicate: int
    trials_activated: int


class BillingCycle:
    """Day-3 deliverable. Day-4 stretch: add `upgrade_subscription(...)`."""

    def __init__(
        self,
        db: Database,
        customer_repo: CustomerRepository,
        plan_repo: PlanRepository,
        subscription_repo: SubscriptionRepository,
        usage_repo: UsageRecordRepository,
        invoice_repo: InvoiceRepository,
        line_item_repo: InvoiceLineItemRepository,
        ledger_repo: LedgerRepository,
        strategy_factory: Callable,
        discount_factory: Callable,
        tax_factory: Callable,
    ) -> None:
        self.db = db
        self.customer_repo = customer_repo
        self.plan_repo = plan_repo
        self.subscription_repo = subscription_repo
        self.usage_repo = usage_repo
        self.invoice_repo = invoice_repo
        self.line_item_repo = line_item_repo
        self.ledger_repo = ledger_repo
        self.strategy_factory = strategy_factory
        self.discount_factory = discount_factory
        self.tax_factory = tax_factory

    @staticmethod
    def _add_month(d: date) -> date:
        if d.month == 12:
            year, month = d.year + 1, 1
        else:
            year, month = d.year, d.month + 1

        day = min(d.day, calendar.monthrange(year, month)[1])
        return date(year, month, day)

    @staticmethod
    def _add_year(d: date) -> date:
        year = d.year + 1
        day = min(d.day, calendar.monthrange(year, d.month)[1])
        return date(year, d.month, day)

    def _next_period_end(self, period_start: date, billing_period: BillingPeriod) -> date:
        if billing_period == BillingPeriod.MONTHLY:
            return self._add_month(period_start)
        return self._add_year(period_start)

    def _activate_ended_trials(self, as_of: date) -> int:
        activated = 0
        for sub in self.subscription_repo.list_all():
            if (
                sub.status == SubscriptionStatus.TRIAL
                and sub.trial_end is not None
                and sub.trial_end <= as_of
            ):
                self.subscription_repo.update_status(sub.id, SubscriptionStatus.ACTIVE)
                activated += 1
        return activated

    def _build_issued_invoice(self, sub: Subscription):
        plan = self.plan_repo.get(sub.plan_id)
        customer = self.customer_repo.get(sub.customer_id)

        if plan is None or customer is None:
            return None, None

        strategy = self.strategy_factory(plan)
        discount = self.discount_factory(sub.discount_id)
        tax_calc, tax_context = self.tax_factory(customer)

        usage_quantity = self.usage_repo.sum_for_period(
            sub.id,
            "units",
            sub.current_period_start,
            sub.current_period_end,
        )

        invoice_count_so_far = self.invoice_repo.count_for_subscription(sub.id)

        draft_invoice = build_invoice(
            subscription=sub,
            plan=plan,
            strategy=strategy,
            discount=discount,
            tax_calc=tax_calc,
            tax_context=tax_context,
            usage_quantity=usage_quantity,
            period_start=sub.current_period_start,
            period_end=sub.current_period_end,
            invoice_count_so_far=invoice_count_so_far,
        )

        draft_invoice.status = InvoiceStatus.ISSUED
        return draft_invoice, plan

    def _persist_invoice_for_subscription(
        self,
        sub: Subscription,
        plan: BillingPeriod,
        draft_invoice,
    ) -> None:
        saved_invoice = self.invoice_repo.add(draft_invoice)

        for line_item in draft_invoice.line_items:
            self.line_item_repo.add(
                InvoiceLineItem(
                    id=None,
                    invoice_id=saved_invoice.id,
                    description=line_item.description,
                    amount=line_item.amount,
                    kind=line_item.kind,
                )
            )

        self.ledger_repo.add(
            LedgerEntry(
                id=None,
                invoice_id=saved_invoice.id,
                customer_id=sub.customer_id,
                amount=saved_invoice.total,
                direction=LedgerDirection.DEBIT,
                reason=f"Invoice {saved_invoice.id} issued",
            )
        )

        new_start = sub.current_period_end
        new_end = self._next_period_end(new_start, plan)

        self.subscription_repo.update_period(sub.id, new_start, new_end)

    def run(self, as_of: date) -> BillingResult:
        invoices_created = 0
        invoices_skipped_duplicate = 0

        trials_activated = self._activate_ended_trials(as_of)

        due_subscriptions = self.subscription_repo.get_due_for_billing(as_of)

        for sub in due_subscriptions:
            draft_invoice, plan = self._build_issued_invoice(sub)
            if draft_invoice is None or plan is None:
                continue

            try:
                self._persist_invoice_for_subscription(sub, plan.billing_period, draft_invoice)
                invoices_created += 1
            except sqlite3.IntegrityError:
                invoices_skipped_duplicate += 1

        return BillingResult(
            invoices_created=invoices_created,
            invoices_skipped_duplicate=invoices_skipped_duplicate,
            trials_activated=trials_activated,
        )

    def upgrade_subscription(
        self,
        subscription_id: int,
        new_plan_id: int,
        switch_date: date,
    ) -> None:
        sub = self.subscription_repo.get(subscription_id)
        if sub is None:
            raise ValueError("Subscription not found")

        old_plan = self.plan_repo.get(sub.plan_id)
        new_plan = self.plan_repo.get(new_plan_id)
        customer = self.customer_repo.get(sub.customer_id)

        if old_plan is None or new_plan is None or customer is None:
            raise ValueError("Missing required data")

        strategy_old = self.strategy_factory(old_plan)
        strategy_new = self.strategy_factory(new_plan)

        old_price = strategy_old.price(0, sub.current_period_start, sub.current_period_end)
        new_price = strategy_new.price(0, sub.current_period_start, sub.current_period_end)

        tax_calc, tax_context = self.tax_factory(customer)

        proration = compute_proration(
            old_plan_price=old_price,
            new_plan_price=new_price,
            period_start=sub.current_period_start,
            period_end=sub.current_period_end,
            switch_date=switch_date,
            tax_calc=tax_calc,
            tax_context=tax_context,
        )

        net_total = (
            proration.charge_amount
            + proration.charge_tax
            - proration.credit_amount
            - proration.credit_tax
        )

        invoice = self.invoice_repo.add(
            Invoice(
                id=None,
                subscription_id=sub.id,
                period_start=switch_date,
                period_end=sub.current_period_end,
                subtotal=net_total,
                discount_total=Money("0", net_total.currency),
                tax_total=Money("0", net_total.currency),
                total=net_total,
                status=InvoiceStatus.ISSUED,
                issued_at=None,
                pdf_path=None,
            )
        )

        self.line_item_repo.add(
            InvoiceLineItem(
                id=None,
                invoice_id=invoice.id,
                description="Proration credit",
                amount=proration.credit_amount,
                kind=LineItemKind.PRORATION_CREDIT,
            )
        )

        self.line_item_repo.add(
            InvoiceLineItem(
                id=None,
                invoice_id=invoice.id,
                description="Proration charge",
                amount=proration.charge_amount,
                kind=LineItemKind.PRORATION_CHARGE,
            )
        )

        self.ledger_repo.add(
            LedgerEntry(
                id=None,
                invoice_id=invoice.id,
                customer_id=sub.customer_id,
                amount=invoice.total,
                direction=LedgerDirection.DEBIT,
                reason=f"Proration invoice {invoice.id}",
            )
        )

        self.subscription_repo.update_plan(subscription_id, new_plan_id)
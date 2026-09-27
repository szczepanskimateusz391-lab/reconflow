from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .case_service import build_operational_cases, case_id_for_alert, financial_contributions
from .config import get_settings
from .models import (
    Alert,
    AlertHistory,
    Document,
    MatchCandidate,
    Order,
    OrderResult,
    Payment,
    PaymentAssignment,
    ReconciliationRun,
    RejectedRow,
    ReturnRecord,
    UserDecision,
)


ZERO = Decimal("0.00")
CANCELLED_ORDER_STATUSES = {"cancelled"}
CANCELLED_RETURN_STATUSES = {"cancelled"}


def money(value: Decimal | int | str) -> Decimal:
    return Decimal(value).quantize(Decimal("0.01"))


def _fingerprint(alert_type: str, entity_type: str, entity_key: str) -> str:
    return hashlib.sha256(f"{alert_type}|{entity_type}|{entity_key}".encode()).hexdigest()


def _source_record(record: Any, record_type: str) -> dict[str, Any]:
    external = getattr(record, "external_id", None) or getattr(record, "transaction_id", None)
    return {
        "record_type": record_type,
        "id": record.id,
        "source_system": record.source_system,
        "external_id": external,
        "source_file": record.source_file,
        "source_row": record.source_row,
    }


class AlertCollector:
    def __init__(self, db: Session, run: ReconciliationRun):
        self.db = db
        self.run = run
        self.seen: set[str] = set()

    def add(
        self,
        *,
        alert_type: str,
        title: str,
        description: str,
        evidence: dict[str, Any],
        source_records: list[dict[str, Any]],
        entity_type: str,
        entity_id: int,
        entity_key: str | None = None,
        order_id: int | None = None,
        currency: str | None = None,
        discrepancy_amount: Decimal | None = None,
    ) -> Alert:
        fingerprint = _fingerprint(alert_type, entity_type, entity_key or str(entity_id))
        self.seen.add(fingerprint)
        alert = self.db.scalar(select(Alert).where(Alert.fingerprint == fingerprint))
        now = datetime.now(timezone.utc)
        serialised_evidence = _json_safe(evidence)
        if alert is None:
            alert = Alert(
                fingerprint=fingerprint,
                alert_type=alert_type,
                title=title,
                description=description,
                evidence=serialised_evidence,
                source_records=source_records,
                entity_type=entity_type,
                entity_id=entity_id,
                order_id=order_id,
                currency=currency,
                discrepancy_amount=discrepancy_amount,
                handling_status="new",
                active=True,
                first_seen_run_id=self.run.id,
                last_seen_run_id=self.run.id,
                created_at=now,
                updated_at=now,
            )
            self.db.add(alert)
            self.db.flush()
            self.db.add(
                AlertHistory(
                    alert_id=alert.id,
                    action="detected",
                    new_status="new",
                    actor="reconciliation-engine",
                    details={"run_id": self.run.id},
                )
            )
        else:
            was_active = alert.active
            previous_status = alert.handling_status
            alert.title = title
            alert.description = description
            alert.evidence = serialised_evidence
            alert.source_records = source_records
            alert.order_id = order_id
            alert.currency = currency
            alert.discrepancy_amount = discrepancy_amount
            alert.last_seen_run_id = self.run.id
            alert.active = True
            alert.updated_at = now
            if not was_active:
                if previous_status in {"resolved", "ignored"}:
                    # A previously closed finding is a new operational task when
                    # it becomes true again for another analysis date. The old
                    # decision remains in AlertHistory.
                    alert.handling_status = "in_progress"
                self.db.add(
                    AlertHistory(
                        alert_id=alert.id,
                        action="reopened_by_analysis",
                        previous_status=previous_status,
                        new_status=alert.handling_status,
                        comment=(
                            "Wynik ponownie spełnia regułę. Poprzednia decyzja pozostała w historii, "
                            "a sprawa wymaga ponownej oceny."
                            if previous_status in {"resolved", "ignored"}
                            else None
                        ),
                        actor="reconciliation-engine",
                        details={"run_id": self.run.id},
                    )
                )
        return alert

    def deactivate_not_seen(self) -> None:
        active_alerts = self.db.scalars(select(Alert).where(Alert.active.is_(True))).all()
        alerts_by_id = {alert.id: alert for alert in self.db.scalars(select(Alert)).all()}

        # A review alert caused by a changed closed result remains current until
        # it is handled or the original financial finding becomes current again.
        for alert in active_alerts:
            if alert.alert_type != "manual_decision_challenged":
                continue
            original_id = (alert.evidence or {}).get("changed_alert_id")
            original = alerts_by_id.get(original_id) if isinstance(original_id, int) else None
            if original is not None and not original.active:
                self.seen.add(alert.fingerprint)

        for alert in active_alerts:
            if alert.fingerprint not in self.seen:
                alert.active = False
                alert.updated_at = datetime.now(timezone.utc)
                self.db.add(
                    AlertHistory(
                        alert_id=alert.id,
                        action="cleared_by_analysis",
                        previous_status=alert.handling_status,
                        new_status=alert.handling_status,
                        actor="reconciliation-engine",
                        details={"run_id": self.run.id},
                    )
                )
                if (
                    alert.handling_status in {"resolved", "ignored"}
                    and alert.alert_type != "manual_decision_challenged"
                ):
                    self.add(
                        alert_type="manual_decision_challenged",
                        title="Wynik zamkniętej sprawy zmienił się",
                        description=(
                            "Nowe dane albo inna data oceny sprawiły, że wcześniejsze ustalenie nie jest "
                            "już bieżące. Poprzednia decyzja pozostaje w historii i wymaga ponownej oceny."
                        ),
                        evidence={
                            "reason": "resolved_finding_changed",
                            "changed_alert_id": alert.id,
                            "previous_alert_type": alert.alert_type,
                            "previous_discrepancy_amount": (
                                format(alert.discrepancy_amount, "f")
                                if alert.discrepancy_amount is not None
                                else None
                            ),
                            "run_id": self.run.id,
                        },
                        source_records=alert.source_records,
                        entity_type=alert.entity_type,
                        entity_id=alert.entity_id,
                        entity_key=f"resolved-result-changed:{alert.id}",
                        order_id=alert.order_id,
                        currency=alert.currency,
                    )


def _json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (datetime,)):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def _order_key(source: str, external_id: str) -> tuple[str, str]:
    return source, external_id


def _resolve_order_ref(
    orders_by_key: dict[tuple[str, str], Order], source_system: str, ref: str, explicit_source: str | None
) -> Order | None:
    return orders_by_key.get(_order_key(explicit_source or source_system, ref))


def _candidate_score(payment: Payment, order: Order, tolerance: Decimal) -> tuple[int, dict[str, Any]]:
    score = 0
    breakdown: dict[str, Any] = {}
    if payment.currency != order.currency:
        return 0, {"currency": "different currency; candidate excluded"}
    score += 20
    breakdown["currency"] = {"points": 20, "reason": f"same currency {payment.currency}"}

    difference = abs(payment.amount - order.gross_amount)
    if difference <= tolerance:
        score += 40
        breakdown["amount"] = {"points": 40, "reason": f"amount differs by {difference}"}
    elif payment.amount < order.gross_amount:
        score += 20
        breakdown["amount"] = {"points": 20, "reason": "amount can be a partial payment"}
    else:
        breakdown["amount"] = {"points": 0, "reason": "amount exceeds order total"}

    days = abs((payment.transaction_date - order.order_date).days)
    date_points = 20 if days <= 2 else 15 if days <= 7 else 5 if days <= 30 else 0
    score += date_points
    breakdown["date"] = {"points": date_points, "reason": f"dates are {days} day(s) apart"}

    searchable = payment.title.casefold()
    customer_points = 0
    reason = "no customer clue"
    if order.customer_email and order.customer_email.casefold() in searchable:
        customer_points = 20
        reason = "customer email appears in payment title"
    elif order.customer_name:
        tokens = [part for part in order.customer_name.casefold().split() if len(part) >= 3]
        matches = [token for token in tokens if token in searchable]
        if tokens and len(matches) == len(tokens):
            customer_points = 20
            reason = "all customer name tokens appear in payment title"
        elif matches:
            customer_points = 10
            reason = "part of customer name appears in payment title"
    score += customer_points
    breakdown["customer"] = {"points": customer_points, "reason": reason}
    return score, breakdown


def _add_source_conflict_alerts(db: Session, collector: AlertCollector) -> None:
    conflicts = db.scalars(select(RejectedRow).where(RejectedRow.is_conflict.is_(True))).all()
    for conflict in conflicts:
        raw = conflict.raw_data
        business_id = (
            raw.get("order_id")
            or raw.get("transaction_id")
            or raw.get("document_id")
            or raw.get("return_id")
            or str(conflict.id)
        )
        source = raw.get("source_system", "unknown")
        collector.add(
            alert_type="source_identifier_conflict",
            title="Konflikt identyfikatora źródłowego",
            description="Ten sam klucz źródłowy wystąpił z inną treścią. Import nie nadpisał rekordu.",
            evidence={"reason": conflict.reason, "raw_data": raw},
            source_records=[
                {
                    "record_type": "rejected_row",
                    "id": conflict.id,
                    "source_system": source,
                    "external_id": business_id,
                    "source_file": conflict.file_name,
                    "source_row": conflict.row_number,
                }
            ],
            entity_type="source_key",
            entity_id=conflict.id,
            entity_key=f"{source}:{business_id}",
        )


def run_reconciliation(db: Session, analysis_at: datetime | None = None) -> ReconciliationRun:
    settings = get_settings()
    analysis_at = analysis_at or datetime.now(timezone.utc)
    if analysis_at.tzinfo is None:
        analysis_at = analysis_at.replace(tzinfo=timezone.utc)
    tolerance = settings.amount_tolerance
    run = ReconciliationRun(
        analysis_at=analysis_at,
        tolerance=tolerance,
        document_grace_days=settings.document_grace_days,
        status="running",
    )
    db.add(run)
    db.flush()
    collector = AlertCollector(db, run)

    orders = db.scalars(select(Order)).all()
    payments = db.scalars(select(Payment)).all()
    documents = db.scalars(select(Document)).all()
    returns = db.scalars(select(ReturnRecord)).all()
    orders_by_key = {_order_key(order.source_system, order.external_id): order for order in orders}
    assignments_by_payment = {
        item.payment_id: item for item in db.scalars(select(PaymentAssignment)).all()
    }
    rejected_candidate_pairs = {
        (decision.payment_id, decision.order_id)
        for decision in db.scalars(
            select(UserDecision).where(UserDecision.action == "reject_candidate")
        ).all()
        if decision.payment_id is not None and decision.order_id is not None
    }

    # Exact source references establish relationships independently of financial evaluation.
    for payment in payments:
        if payment.processing_scope != "direct" or not payment.order_ref:
            continue
        target = _resolve_order_ref(
            orders_by_key, payment.source_system, payment.order_ref, payment.order_source_system
        )
        if target is None:
            continue
        existing = assignments_by_payment.get(payment.id)
        if existing is None:
            assignment = PaymentAssignment(
                payment_id=payment.id,
                order_id=target.id,
                origin="automatic_reference",
                state="confirmed",
                explanation=(
                    "Powiązano po jednoznacznym order_ref w źródle "
                    f"{payment.order_source_system or payment.source_system}."
                ),
            )
            db.add(assignment)
            db.flush()
            assignments_by_payment[payment.id] = assignment
        elif existing.origin == "manual" and existing.order_id != target.id:
            collector.add(
                alert_type="manual_decision_challenged",
                title="Nowe dane podważają ręczne powiązanie",
                description="Jednoznaczne odwołanie źródłowe wskazuje inne zamówienie niż zachowana decyzja ręczna.",
                evidence={
                    "manual_order_id": existing.order_id,
                    "referenced_order_id": target.id,
                    "order_ref": payment.order_ref,
                },
                source_records=[_source_record(payment, "payment"), _source_record(target, "order")],
                entity_type="payment",
                entity_id=payment.id,
                order_id=existing.order_id,
                currency=payment.currency,
            )

    db.flush()

    assignments_by_payment = {
        item.payment_id: item for item in db.scalars(select(PaymentAssignment)).all()
    }
    assignments_by_order: dict[int, list[tuple[Payment, PaymentAssignment]]] = defaultdict(list)
    payment_by_id = {payment.id: payment for payment in payments}
    for payment_id, assignment in assignments_by_payment.items():
        payment = payment_by_id.get(payment_id)
        if payment:
            assignments_by_order[assignment.order_id].append((payment, assignment))

    # Unreferenced direct payments only receive explainable candidates; they are never auto-approved.
    for payment in payments:
        if (
            payment.id in assignments_by_payment
            or payment.processing_scope != "direct"
            or payment.kind != "payment"
            or payment.status != "completed"
            or payment.transaction_date > analysis_at.date()
        ):
            continue
        scored: list[tuple[int, Order, dict[str, Any]]] = []
        for order in orders:
            if order.status in CANCELLED_ORDER_STATUSES:
                continue
            score, breakdown = _candidate_score(payment, order, tolerance)
            if score >= 50:
                scored.append((score, order, breakdown))
        scored.sort(key=lambda item: (-item[0], item[1].id))
        for score, order, breakdown in scored[:5]:
            db.add(
                MatchCandidate(
                    run_id=run.id,
                    payment_id=payment.id,
                    order_id=order.id,
                    rule_score=score,
                    rule_breakdown=breakdown,
                    status=(
                        "rejected"
                        if (payment.id, order.id) in rejected_candidate_pairs
                        else "pending"
                    ),
                )
            )
        eligible_scored = [
            item for item in scored if (payment.id, item[1].id) not in rejected_candidate_pairs
        ]
        if eligible_scored:
            best_score = eligible_scored[0][0]
            best = [item for item in eligible_scored if item[0] == best_score]
            if len(best) > 1:
                collector.add(
                    alert_type="ambiguous_match",
                    title="Niejednoznaczne dopasowanie",
                    description="Kilka zamówień uzyskało ten sam najwyższy wynik reguł; wymagane jest zatwierdzenie użytkownika.",
                    evidence={
                        "rule_score": best_score,
                        "candidate_order_ids": [item[1].id for item in best],
                        "rule_breakdowns": [item[2] for item in best],
                    },
                    source_records=[_source_record(payment, "payment")]
                    + [_source_record(item[1], "order") for item in best],
                    entity_type="payment",
                    entity_id=payment.id,
                    currency=payment.currency,
                    discrepancy_amount=payment.amount,
                )
                continue
        collector.add(
            alert_type="unmatched_transaction",
            title="Transakcja bez rozpoznanego zamówienia",
            description=(
                "Brak jednoznacznego odwołania. Kandydaci, jeśli istnieją, wymagają ręcznego zatwierdzenia."
            ),
            evidence={
                "candidate_count": min(len(eligible_scored), 5),
                "candidate_order_ids": [item[1].id for item in eligible_scored[:5]],
                "rejected_candidate_order_ids": [
                    item[1].id
                    for item in scored[:5]
                    if (payment.id, item[1].id) in rejected_candidate_pairs
                ],
            },
            source_records=[_source_record(payment, "payment")],
            entity_type="payment",
            entity_id=payment.id,
            currency=payment.currency,
            discrepancy_amount=payment.amount,
        )

    # Aggregate settlements are explicitly routed out of MVP matching.
    for payment in payments:
        if (
            payment.processing_scope in {"aggregate", "marketplace_payout"}
            and payment.transaction_date <= analysis_at.date()
        ):
            collector.add(
                alert_type="separate_process_required",
                title="Wymagany osobny proces rozliczenia",
                description="Płatność zbiorcza lub wypłata marketplace nie jest automatycznie dopasowywana w MVP.",
                evidence={"processing_scope": payment.processing_scope},
                source_records=[_source_record(payment, "payment")],
                entity_type="payment",
                entity_id=payment.id,
                currency=payment.currency,
                discrepancy_amount=payment.amount if payment.status == "completed" else None,
            )

    documents_by_order: dict[int, list[Document]] = defaultdict(list)
    for document in documents:
        if document.document_date > analysis_at.date():
            continue
        order = _resolve_order_ref(
            orders_by_key, document.source_system, document.order_ref, document.order_source_system
        )
        if order is None:
            collector.add(
                alert_type="document_without_order",
                title="Dokument bez zamówienia",
                description="Odwołanie dokumentu nie wskazuje istniejącego zamówienia w zadanym źródle.",
                evidence={
                    "order_ref": document.order_ref,
                    "order_source_system": document.order_source_system or document.source_system,
                },
                source_records=[_source_record(document, "document")],
                entity_type="document",
                entity_id=document.id,
                currency=document.currency,
                discrepancy_amount=abs(document.amount),
            )
        else:
            documents_by_order[order.id].append(document)

    returns_by_order: dict[int, list[ReturnRecord]] = defaultdict(list)
    for return_record in returns:
        if return_record.return_date > analysis_at.date():
            continue
        order = _resolve_order_ref(
            orders_by_key,
            return_record.source_system,
            return_record.order_ref,
            return_record.order_source_system,
        )
        if order:
            returns_by_order[order.id].append(return_record)

    _add_source_conflict_alerts(db, collector)

    for order in orders:
        order_sources = [_source_record(order, "order")]
        linked = assignments_by_order.get(order.id, [])
        same_currency_completed_payments = [
            payment
            for payment, _assignment in linked
            if payment.kind == "payment"
            and payment.status == "completed"
            and payment.currency == order.currency
            and payment.transaction_date <= analysis_at.date()
        ]
        same_currency_completed_refunds = [
            payment
            for payment, _assignment in linked
            if payment.kind == "refund"
            and payment.status == "completed"
            and payment.currency == order.currency
            and payment.transaction_date <= analysis_at.date()
        ]
        completed_payment_total = money(sum((p.amount for p in same_currency_completed_payments), ZERO))
        completed_refund_total = money(sum((p.amount for p in same_currency_completed_refunds), ZERO))

        manual_completed_payments = [
            payment
            for payment, assignment in linked
            if assignment.origin == "manual"
            and payment.kind == "payment"
            and payment.status == "completed"
            and payment.currency == order.currency
            and payment.transaction_date <= analysis_at.date()
        ]
        if manual_completed_payments and completed_payment_total > order.gross_amount + tolerance:
            for manual_payment in manual_completed_payments:
                collector.add(
                    alert_type="manual_decision_challenged",
                    title="Nowe dane podważają ręczne powiązanie",
                    description="Po zachowanej decyzji ręcznej pojawiły się dane, przez które suma płatności przekracza wartość zamówienia.",
                    evidence={
                        "manual_payment_id": manual_payment.id,
                        "gross_amount": order.gross_amount,
                        "completed_payments": completed_payment_total,
                        "reason": "overpayment_after_manual_link",
                    },
                    source_records=order_sources
                    + [_source_record(payment, "payment") for payment in same_currency_completed_payments],
                    entity_type="payment",
                    entity_id=manual_payment.id,
                    order_id=order.id,
                    currency=order.currency,
                    discrepancy_amount=money(completed_payment_total - order.gross_amount),
                )

        foreign = [
            payment
            for payment, _assignment in linked
            if payment.currency != order.currency
            and payment.transaction_date <= analysis_at.date()
        ]
        for payment in foreign:
            collector.add(
                alert_type="currency_conflict",
                title="Konflikt walut",
                description="Znaleziono powiązaną płatność, ale ma inną walutę niż zamówienie. Kwot nie przeliczono ani nie odjęto.",
                evidence={"order_currency": order.currency, "transaction_currency": payment.currency},
                source_records=order_sources + [_source_record(payment, "payment")],
                entity_type="payment",
                entity_id=payment.id,
                order_id=order.id,
            )

        order_alert_types: list[str] = []
        if order.status not in CANCELLED_ORDER_STATUSES:
            if analysis_at.date() > order.due_date:
                if completed_payment_total <= tolerance:
                    collector.add(
                        alert_type="missing_payment_overdue",
                        title="Brak płatności po terminie",
                        description="Nie znaleziono zakończonej płatności w walucie zamówienia do dnia oceny.",
                        evidence={
                            "gross_amount": order.gross_amount,
                            "completed_payments": completed_payment_total,
                            "due_date": order.due_date.isoformat(),
                            "analysis_at": analysis_at.isoformat(),
                        },
                        source_records=order_sources,
                        entity_type="order",
                        entity_id=order.id,
                        order_id=order.id,
                        currency=order.currency,
                        discrepancy_amount=order.gross_amount,
                    )
                    order_alert_types.append("missing_payment_overdue")
                elif completed_payment_total < order.gross_amount - tolerance:
                    difference = money(order.gross_amount - completed_payment_total)
                    collector.add(
                        alert_type="underpayment_overdue",
                        title="Niedopłata po terminie",
                        description="Jednoznaczne powiązanie zachowano, ale suma zakończonych płatności jest za niska.",
                        evidence={
                            "gross_amount": order.gross_amount,
                            "completed_payments": completed_payment_total,
                            "tolerance": tolerance,
                            "due_date": order.due_date.isoformat(),
                            "analysis_at": analysis_at.isoformat(),
                        },
                        source_records=order_sources
                        + [_source_record(payment, "payment") for payment in same_currency_completed_payments],
                        entity_type="order",
                        entity_id=order.id,
                        order_id=order.id,
                        currency=order.currency,
                        discrepancy_amount=difference,
                    )
                    order_alert_types.append("underpayment_overdue")
            if completed_payment_total > order.gross_amount + tolerance:
                difference = money(completed_payment_total - order.gross_amount)
                collector.add(
                    alert_type="overpayment",
                    title="Nadpłata",
                    description="Suma zakończonych płatności przekracza wartość zamówienia ponad tolerancję.",
                    evidence={
                        "gross_amount": order.gross_amount,
                        "completed_payments": completed_payment_total,
                        "tolerance": tolerance,
                    },
                    source_records=order_sources
                    + [_source_record(payment, "payment") for payment in same_currency_completed_payments],
                    entity_type="order",
                    entity_id=order.id,
                    order_id=order.id,
                    currency=order.currency,
                    discrepancy_amount=difference,
                )
                order_alert_types.append("overpayment")

        duplicate_groups: dict[tuple[Decimal, str, Any], list[Payment]] = defaultdict(list)
        for payment in same_currency_completed_payments:
            duplicate_groups[(payment.amount, payment.currency, payment.transaction_date)].append(payment)
        for duplicate_key, group in duplicate_groups.items():
            if len(group) > 1:
                collector.add(
                    alert_type="suspected_duplicate_payment",
                    title="Podejrzenie powtórnej płatności",
                    description="Dwie zakończone transakcje mają tę samą kwotę, walutę i datę. Wymagają sprawdzenia.",
                    evidence={
                        "amount": duplicate_key[0],
                        "currency": duplicate_key[1],
                        "date": duplicate_key[2].isoformat(),
                        "transaction_ids": [payment.transaction_id for payment in group],
                    },
                    source_records=order_sources + [_source_record(payment, "payment") for payment in group],
                    entity_type="order",
                    entity_id=order.id,
                    entity_key=f"{order.id}:{duplicate_key[0]}:{duplicate_key[1]}:{duplicate_key[2]}",
                    order_id=order.id,
                    currency=order.currency,
                    discrepancy_amount=duplicate_key[0],
                )
                order_alert_types.append("suspected_duplicate_payment")

        active_documents = [doc for doc in documents_by_order.get(order.id, []) if doc.status == "issued"]
        required_documents = [
            doc for doc in active_documents if doc.document_type in settings.required_document_types
        ]
        if (
            order.status not in CANCELLED_ORDER_STATUSES
            and not required_documents
            and (analysis_at.date() - order.order_date).days > settings.document_grace_days
        ):
            collector.add(
                alert_type="missing_expected_document",
                title="Brak oczekiwanego dokumentu",
                description="Nie znaleziono oczekiwanego dokumentu sprzedaży w wymaganym czasie.",
                evidence={
                    "required_document_types": settings.required_document_types,
                    "grace_days": settings.document_grace_days,
                    "order_date": order.order_date.isoformat(),
                },
                source_records=order_sources,
                entity_type="order",
                entity_id=order.id,
                order_id=order.id,
                currency=order.currency,
                discrepancy_amount=order.gross_amount,
            )
            order_alert_types.append("missing_expected_document")

        same_currency_documents = [doc for doc in active_documents if doc.currency == order.currency]
        document_total = money(sum((doc.amount for doc in same_currency_documents), ZERO))
        for doc in active_documents:
            if doc.currency != order.currency:
                collector.add(
                    alert_type="currency_conflict",
                    title="Konflikt walut dokumentu",
                    description="Dokument jest powiązany, ale ma inną walutę; jego kwoty nie włączono do porównania.",
                    evidence={"order_currency": order.currency, "document_currency": doc.currency},
                    source_records=order_sources + [_source_record(doc, "document")],
                    entity_type="document",
                    entity_id=doc.id,
                    order_id=order.id,
                )
        accepted_returns = [
            ret
            for ret in returns_by_order.get(order.id, [])
            if ret.status in {"approved", "received"} and ret.currency == order.currency
        ]
        expected_document_total = money(
            order.gross_amount - sum((ret.expected_refund_amount for ret in accepted_returns), ZERO)
        )
        if required_documents and abs(document_total - expected_document_total) > tolerance:
            difference = money(abs(document_total - expected_document_total))
            collector.add(
                alert_type="document_value_mismatch",
                title="Rozbieżność wartości dokumentów",
                description="Suma wystawionych dokumentów i podpisanych korekt różni się od oczekiwanej wartości netto zamówienia.",
                evidence={
                    "document_total": document_total,
                    "expected_document_total": expected_document_total,
                    "corrections_are_signed": True,
                    "tolerance": tolerance,
                },
                source_records=order_sources
                + [_source_record(doc, "document") for doc in same_currency_documents],
                entity_type="order",
                entity_id=order.id,
                order_id=order.id,
                currency=order.currency,
                discrepancy_amount=difference,
            )
            order_alert_types.append("document_value_mismatch")

        for return_record in returns_by_order.get(order.id, []):
            if return_record.status in CANCELLED_RETURN_STATUSES:
                continue
            matching_refunds = [
                payment
                for payment, _assignment in linked
                if payment.kind == "refund"
                and payment.status == "completed"
                and payment.return_ref == return_record.external_id
                and payment.source_system == return_record.source_system
                and payment.transaction_date <= analysis_at.date()
            ]
            currency_mismatch = [
                payment for payment in matching_refunds if payment.currency != return_record.currency
            ]
            for payment in currency_mismatch:
                collector.add(
                    alert_type="currency_conflict",
                    title="Konflikt walut refundacji",
                    description="Refundacja ma inną walutę niż oczekiwany zwrot i nie została odjęta.",
                    evidence={
                        "expected_currency": return_record.currency,
                        "refund_currency": payment.currency,
                    },
                    source_records=[
                        _source_record(order, "order"),
                        _source_record(return_record, "return"),
                        _source_record(payment, "payment"),
                    ],
                    entity_type="payment",
                    entity_id=payment.id,
                    order_id=order.id,
                )
            refunded = money(
                sum(
                    (
                        payment.amount
                        for payment in matching_refunds
                        if payment.currency == return_record.currency
                    ),
                    ZERO,
                )
            )
            expected = money(return_record.expected_refund_amount)
            return_sources = order_sources + [_source_record(return_record, "return")]
            if refunded > expected + tolerance:
                collector.add(
                    alert_type="refund_exceeds_expected",
                    title="Refundacja przekracza oczekiwaną kwotę",
                    description="Suma zakończonych refundacji przekracza kwotę oczekiwaną dla zwrotu.",
                    evidence={"expected": expected, "refunded": refunded, "tolerance": tolerance},
                    source_records=return_sources
                    + [_source_record(payment, "payment") for payment in matching_refunds],
                    entity_type="return",
                    entity_id=return_record.id,
                    order_id=order.id,
                    currency=return_record.currency,
                    discrepancy_amount=money(refunded - expected),
                )
                order_alert_types.append("refund_exceeds_expected")
            elif ZERO < refunded < expected - tolerance:
                collector.add(
                    alert_type="partial_refund",
                    title="Refundacja częściowa",
                    description="Zakończone refundacje pokrywają tylko część oczekiwanej kwoty zwrotu.",
                    evidence={"expected": expected, "refunded": refunded, "tolerance": tolerance},
                    source_records=return_sources
                    + [_source_record(payment, "payment") for payment in matching_refunds],
                    entity_type="return",
                    entity_id=return_record.id,
                    order_id=order.id,
                    currency=return_record.currency,
                    discrepancy_amount=money(expected - refunded),
                )
                order_alert_types.append("partial_refund")
            elif refunded <= tolerance and analysis_at.date() > return_record.refund_due_date:
                collector.add(
                    alert_type="refund_overdue",
                    title="Refundacja niewykonana w terminie",
                    description="Nie znaleziono zakończonego zwrotu pieniędzy do terminu refundacji.",
                    evidence={
                        "expected": expected,
                        "refunded": refunded,
                        "refund_due_date": return_record.refund_due_date.isoformat(),
                    },
                    source_records=return_sources,
                    entity_type="return",
                    entity_id=return_record.id,
                    order_id=order.id,
                    currency=return_record.currency,
                    discrepancy_amount=expected,
                )
                order_alert_types.append("refund_overdue")

        relationship_state = "linked" if linked else "unlinked"
        financial_status = (
            "cancelled"
            if order.status in CANCELLED_ORDER_STATUSES
            else "exception"
            if order_alert_types or foreign
            else "balanced"
        )
        db.add(
            OrderResult(
                run_id=run.id,
                order_id=order.id,
                relationship_state=relationship_state,
                financial_status=financial_status,
                completed_payments=completed_payment_total,
                completed_refunds=completed_refund_total,
                document_total=document_total,
                currency=order.currency,
                explanation={
                    "linked_payment_ids": [payment.id for payment, _assignment in linked],
                    "completed_payment_ids": [payment.id for payment in same_currency_completed_payments],
                    "completed_refund_ids": [payment.id for payment in same_currency_completed_refunds],
                    "ignored_foreign_currency_payment_ids": [payment.id for payment in foreign],
                    "alert_types": order_alert_types,
                    "tolerance": format(tolerance, "f"),
                },
            )
        )

    collector.deactivate_not_seen()
    run.status = "completed"
    run.completed_at = datetime.now(timezone.utc)
    run.metrics = {
        "orders": len(orders),
        "payments": len(payments),
        "documents": len(documents),
        "returns": len(returns),
        "automatic_reference_links": sum(
            1 for assignment in assignments_by_payment.values() if assignment.origin == "automatic_reference"
        ),
    }
    db.commit()
    db.refresh(run)
    return run


def latest_summary(db: Session) -> dict[str, Any]:
    settings = get_settings()
    context_kind = "test" if settings.test_instance else "demo" if settings.demo_mode else "local"
    context_label = "Dane testowe" if settings.test_instance else "Dane demo" if settings.demo_mode else "Dane lokalne"
    run = db.scalar(select(ReconciliationRun).order_by(ReconciliationRun.id.desc()).limit(1))
    if run is None:
        return {
            "run": None,
            "by_currency": [],
            "alerts_by_type": {},
            "active_alerts": 0,
            "detected_cases": 0,
            "priority_cases": [],
            "data_context": {
                "kind": context_kind,
                "label": context_label,
                "timeliness_note": "Terminowość zostanie oceniona na wybrany dzień analizy.",
            },
        }
    results = db.scalars(select(OrderResult).where(OrderResult.run_id == run.id)).all()
    orders = {order.id: order for order in db.scalars(select(Order)).all()}
    detected_alerts = db.scalars(select(Alert).where(Alert.active.is_(True))).all()
    cases = build_operational_cases(db, detected_alerts, run)
    open_cases = [case for case in cases if case["handling_status"] in {"new", "in_progress"}]
    open_alert_ids = {
        finding["id"]
        for case in open_cases
        for finding in case["findings"]
    }
    confirmed_alerts = db.scalars(
        select(Alert).where(
            Alert.active.is_(True),
            Alert.handling_status == "resolved",
            Alert.confirmed_effect_amount.is_not(None),
        )
    ).all()

    covered: dict[str, Decimal] = defaultdict(lambda: ZERO)
    for result in results:
        order = orders[result.order_id]
        covered[result.currency] += order.gross_amount

    contributions = financial_contributions(detected_alerts)
    category_totals: dict[str, dict[str, Decimal]] = defaultdict(
        lambda: defaultdict(lambda: ZERO)
    )
    category_items: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(
        lambda: defaultdict(list)
    )
    case_titles = {case["id"]: case["title"] for case in cases}
    for contribution in contributions:
        currency = contribution["currency"]
        category = contribution["category"]
        category_totals[currency][category] += contribution["amount"]
        category_items[currency][category].append(
            {
                "case_id": contribution["case_id"],
                "title": case_titles.get(contribution["case_id"], contribution["title"]),
                "rule": contribution["rule"],
                "amount": format(money(contribution["amount"]), "f"),
                "currency": currency,
                "handling_status": contribution["handling_status"],
                "calculation": contribution["calculation"],
                "source_records": contribution["source_records"],
                "deduplicated_alert_ids": contribution["alert_ids"],
                "basis": contribution["basis_key"],
            }
        )

    confirmed_by_case: dict[tuple[str, str], Decimal] = {}
    for alert in confirmed_alerts:
        if alert.currency and alert.confirmed_effect_amount is not None:
            key = (case_id_for_alert(alert), alert.currency)
            confirmed_by_case[key] = max(
                confirmed_by_case.get(key, ZERO), money(alert.confirmed_effect_amount)
            )
    confirmed: dict[str, Decimal] = defaultdict(lambda: ZERO)
    confirmed_items: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for (case_id, currency), amount in confirmed_by_case.items():
        confirmed[currency] += amount
        alert = next(
            item
            for item in confirmed_alerts
            if case_id_for_alert(item) == case_id and item.currency == currency
        )
        confirmed_items[currency].append(
            {
                "case_id": case_id,
                "title": case_titles.get(case_id, alert.title),
                "amount": format(amount, "f"),
                "currency": currency,
                "note": "Kwota potwierdzona przez użytkownika; nie zmienia danych finansowych ani wykrytej różnicy.",
            }
        )

    alert_counts: dict[str, int] = defaultdict(int)
    for alert in detected_alerts:
        if alert.id in open_alert_ids:
            alert_counts[alert.alert_type] += 1

    currencies = sorted(
        set(covered)
        | set(category_totals)
        | set(confirmed)
    )
    by_currency = []
    for currency in currencies:
        differences = money(category_totals[currency]["amount_differences"])
        explanation = money(category_totals[currency]["requires_explanation"])
        separate = money(category_totals[currency]["separate_process"])
        by_currency.append(
            {
                "currency": currency,
                "controlled_order_value": format(money(covered[currency]), "f"),
                "amount_differences": {
                    "total": format(differences, "f"),
                    "items": category_items[currency]["amount_differences"],
                    "rule": "Suma odrębnych sald płatności, dokumentów i refundacji po deduplikacji wspólnej podstawy obliczenia.",
                },
                "requires_explanation": {
                    "total": format(explanation, "f"),
                    "items": category_items[currency]["requires_explanation"],
                    "rule": "Kwoty rekordów wymagających weryfikacji; nie są automatycznie uznawane za błąd ani stratę.",
                },
                "separate_process": {
                    "total": format(separate, "f"),
                    "items": category_items[currency]["separate_process"],
                    "rule": "Kwoty wypłat zbiorczych i marketplace wyłączonych z automatycznego dopasowania MVP.",
                },
                # Kept for API compatibility; the UI uses the explicit categories above.
                "detected_discrepancy_exposure": format(differences, "f"),
                "confirmed_user_effect": format(money(confirmed[currency]), "f"),
                "confirmed_user_effect_items": confirmed_items[currency],
            }
        )
    return {
        "run": {
            "id": run.id,
            "analysis_at": run.analysis_at,
            "started_at": run.started_at,
            "completed_at": run.completed_at,
            "status": run.status,
            "tolerance": format(run.tolerance, "f"),
            "document_grace_days": run.document_grace_days,
            "metrics": run.metrics,
        },
        "by_currency": by_currency,
        "alerts_by_type": dict(sorted(alert_counts.items())),
        "active_alerts": len(open_cases),
        "detected_cases": len(cases),
        "priority_cases": [
            {
                key: value
                for key, value in case.items()
                if key not in {"contributions", "priority_rank"}
            }
            for case in open_cases[:5]
        ],
        "data_context": {
            "kind": context_kind,
            "label": context_label,
            "timeliness_note": "Terminowość jest oceniana na wybrany dzień analizy, niezależnie od czasu importu i uruchomienia.",
        },
    }

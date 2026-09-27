from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import (
    Alert,
    AlertHistory,
    Document,
    MatchCandidate,
    Order,
    Payment,
    PaymentAssignment,
    ReconciliationRun,
    ReturnRecord,
    UserDecision,
)


ZERO = Decimal("0.00")
OPEN_STATUSES = {"new", "in_progress"}
PRIORITY_RANK = {"high": 3, "medium": 2, "low": 1}

AMOUNT_DIFFERENCE_TYPES = {
    "missing_payment_overdue",
    "underpayment_overdue",
    "overpayment",
    "document_value_mismatch",
    "refund_overdue",
    "partial_refund",
    "refund_exceeds_expected",
}
EXPLANATION_TYPES = {
    "ambiguous_match",
    "currency_conflict",
    "document_without_order",
    "missing_expected_document",
    "suspected_duplicate_payment",
    "unmatched_transaction",
    "source_identifier_conflict",
    "manual_decision_challenged",
}

TYPE_URGENCY = {
    "manual_decision_challenged": (50, "wcześniejsza decyzja wymaga ponownej oceny"),
    "source_identifier_conflict": (50, "konflikt klucza źródłowego"),
    "missing_payment_overdue": (40, "rozliczenie płatności jest po terminie"),
    "underpayment_overdue": (40, "niedopłata jest po terminie"),
    "refund_overdue": (40, "zwrot pieniędzy jest po terminie"),
    "currency_conflict": (40, "waluty powiązanych rekordów są różne"),
    "overpayment": (30, "wykryto nadpłatę"),
    "refund_exceeds_expected": (30, "refundacja przekracza oczekiwaną kwotę"),
    "partial_refund": (30, "refundacja jest niepełna"),
    "document_value_mismatch": (30, "wartość dokumentów wymaga uzgodnienia"),
    "unmatched_transaction": (25, "transakcja nie ma potwierdzonego zamówienia"),
    "ambiguous_match": (25, "dopasowanie wymaga decyzji użytkownika"),
    "suspected_duplicate_payment": (25, "płatność może być powtórzona"),
    "separate_process_required": (20, "rekord wymaga osobnego procesu"),
    "missing_expected_document": (20, "brakuje oczekiwanego dokumentu"),
    "document_without_order": (20, "dokument nie ma rozpoznanego zamówienia"),
}


def money(value: Decimal | int | str) -> Decimal:
    return Decimal(value).quantize(Decimal("0.01"))


def json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_value(item) for item in value]
    return value


def alert_dict(alert: Alert) -> dict[str, Any]:
    return {
        "id": alert.id,
        "alert_type": alert.alert_type,
        "title": alert.title,
        "description": alert.description,
        "evidence": alert.evidence,
        "source_records": alert.source_records,
        "entity_type": alert.entity_type,
        "entity_id": alert.entity_id,
        "order_id": alert.order_id,
        "currency": alert.currency,
        "discrepancy_amount": json_value(alert.discrepancy_amount),
        "handling_status": alert.handling_status,
        "confirmed_effect_amount": json_value(alert.confirmed_effect_amount),
        "active": alert.active,
        "first_seen_run_id": alert.first_seen_run_id,
        "last_seen_run_id": alert.last_seen_run_id,
        "created_at": alert.created_at,
        "updated_at": alert.updated_at,
    }


def case_id_for_alert(alert: Alert) -> str:
    if alert.order_id is not None:
        return f"order-{alert.order_id}"
    return f"{alert.entity_type}-{alert.entity_id}"


def group_alerts(alerts: Iterable[Alert]) -> dict[str, list[Alert]]:
    grouped: dict[str, list[Alert]] = defaultdict(list)
    for alert in alerts:
        grouped[case_id_for_alert(alert)].append(alert)
    return dict(grouped)


def _case_status(alerts: list[Alert]) -> str:
    statuses = {alert.handling_status for alert in alerts}
    if statuses == {"new"}:
        return "new"
    if statuses & OPEN_STATUSES:
        return "in_progress"
    if "resolved" in statuses:
        return "resolved"
    return "ignored"


def _basis_key(alert: Alert) -> str:
    if alert.alert_type in {"missing_payment_overdue", "underpayment_overdue", "overpayment"}:
        return f"payment-balance:order:{alert.order_id}"
    if alert.alert_type in {"document_value_mismatch", "missing_expected_document"}:
        return f"document-balance:order:{alert.order_id}"
    if alert.alert_type in {"refund_overdue", "partial_refund", "refund_exceeds_expected"}:
        return f"refund-balance:return:{alert.entity_id}"
    if alert.alert_type in {"unmatched_transaction", "ambiguous_match"}:
        return f"transaction:payment:{alert.entity_id}"
    if alert.alert_type == "separate_process_required":
        return f"settlement:payment:{alert.entity_id}"
    return alert.fingerprint


def _category_for_alert(alert: Alert, related: list[Alert]) -> str | None:
    if alert.alert_type == "separate_process_required":
        return "separate_process"
    if alert.alert_type == "missing_payment_overdue" and any(
        item.alert_type == "currency_conflict" for item in related
    ):
        return "requires_explanation"
    if alert.alert_type in AMOUNT_DIFFERENCE_TYPES:
        return "amount_differences"
    if alert.alert_type in EXPLANATION_TYPES:
        return "requires_explanation"
    return "requires_explanation" if alert.discrepancy_amount is not None else None


def _calculation_text(alert: Alert, related: list[Alert]) -> str:
    evidence = alert.evidence or {}
    currency = alert.currency or ""
    if alert.alert_type == "underpayment_overdue":
        return (
            f"{evidence.get('gross_amount', '?')} {currency} − "
            f"{evidence.get('completed_payments', '?')} {currency} = "
            f"{json_value(alert.discrepancy_amount)} {currency}"
        )
    if alert.alert_type == "overpayment":
        return (
            f"{evidence.get('completed_payments', '?')} {currency} − "
            f"{evidence.get('gross_amount', '?')} {currency} = "
            f"{json_value(alert.discrepancy_amount)} {currency}"
        )
    if alert.alert_type in {"partial_refund", "refund_overdue"}:
        return (
            f"Oczekiwano {evidence.get('expected', '?')} {currency}, zakończono "
            f"{evidence.get('refunded', '0.00')} {currency}; brak "
            f"{json_value(alert.discrepancy_amount)} {currency}."
        )
    if alert.alert_type == "refund_exceeds_expected":
        return (
            f"Zakończono {evidence.get('refunded', '?')} {currency}, oczekiwano "
            f"{evidence.get('expected', '?')} {currency}; różnica "
            f"{json_value(alert.discrepancy_amount)} {currency}."
        )
    if alert.alert_type == "missing_payment_overdue" and any(
        item.alert_type == "currency_conflict" for item in related
    ):
        conflict = next(item for item in related if item.alert_type == "currency_conflict")
        return (
            f"Rozliczenie {json_value(alert.discrepancy_amount)} {currency} nie jest potwierdzone; "
            f"powiązana transakcja ma walutę {conflict.evidence.get('transaction_currency', '?')}. "
            "Kwot nie przeliczono ani nie odjęto."
        )
    if alert.alert_type == "missing_payment_overdue":
        return (
            f"Wartość zamówienia {evidence.get('gross_amount', '?')} {currency}; "
            f"zakończone płatności {evidence.get('completed_payments', '0.00')} {currency}."
        )
    if alert.alert_type == "separate_process_required":
        return f"Całe {json_value(alert.discrepancy_amount)} {currency} skierowano do osobnego procesu."
    if alert.discrepancy_amount is not None:
        return f"Kwota sprawy: {json_value(alert.discrepancy_amount)} {currency}. Wymaga wyjaśnienia."
    return "Ustalenie nie tworzy samodzielnej różnicy kwotowej."


def financial_contributions(alerts: Iterable[Alert]) -> list[dict[str, Any]]:
    grouped = group_alerts(alerts)
    deduplicated: dict[tuple[str, str, str], dict[str, Any]] = {}
    for case_id, related in grouped.items():
        for alert in related:
            category = _category_for_alert(alert, related)
            if category is None or not alert.currency or alert.discrepancy_amount is None:
                continue
            amount = abs(money(alert.discrepancy_amount))
            key = (category, alert.currency, _basis_key(alert))
            item = {
                "case_id": case_id,
                "category": category,
                "alert_ids": [alert.id],
                "title": alert.title,
                "rule": alert.alert_type,
                "currency": alert.currency,
                "amount": amount,
                "handling_status": _case_status(related),
                "calculation": _calculation_text(alert, related),
                "source_records": alert.source_records,
                "basis_key": key[2],
            }
            existing = deduplicated.get(key)
            if existing is None or amount > existing["amount"]:
                if existing:
                    item["alert_ids"] = existing["alert_ids"] + [alert.id]
                deduplicated[key] = item
            elif alert.id not in existing["alert_ids"]:
                existing["alert_ids"].append(alert.id)
    return list(deduplicated.values())


def _deadline_for_case(
    alerts: list[Alert],
    orders: dict[int, Order],
    returns: dict[int, ReturnRecord],
    document_grace_days: int,
) -> date | None:
    deadlines: list[date] = []
    for alert in alerts:
        if alert.alert_type in {"refund_overdue", "partial_refund", "refund_exceeds_expected"}:
            return_record = returns.get(alert.entity_id)
            if return_record:
                deadlines.append(return_record.refund_due_date)
        elif alert.alert_type in {"missing_expected_document", "document_value_mismatch"}:
            order = orders.get(alert.order_id or -1)
            if order:
                deadlines.append(order.order_date + timedelta(days=document_grace_days))
        elif alert.order_id is not None and alert.alert_type in {
            "missing_payment_overdue",
            "underpayment_overdue",
            "currency_conflict",
        }:
            order = orders.get(alert.order_id)
            if order:
                deadlines.append(order.due_date)
    return min(deadlines) if deadlines else None


def _case_copy(alerts: list[Alert]) -> tuple[str, str]:
    types = {alert.alert_type for alert in alerts}
    if {"currency_conflict", "missing_payment_overdue"}.issubset(types):
        return (
            "Płatność w innej walucie — rozliczenie niepotwierdzone",
            "Znaleziono powiązaną płatność, ale ma inną walutę niż zamówienie. Kwot nie przeliczono, więc rozliczenie pozostaje niepotwierdzone na dzień oceny.",
        )
    first = sorted(alerts, key=lambda item: (-TYPE_URGENCY.get(item.alert_type, (10, ""))[0], item.id))[0]
    if len(alerts) == 1:
        return first.title, first.description
    return first.title, f"Ta sprawa łączy {len(alerts)} powiązane ustalenia dla jednego zamówienia."


def _priority_for_case(
    alerts: list[Alert],
    deadline: date | None,
    analysis_date: date,
    amount_bands: list[tuple[str, str]],
) -> tuple[str, str, int]:
    top_type = max(alerts, key=lambda item: TYPE_URGENCY.get(item.alert_type, (10, "inne ustalenie"))[0])
    score, type_reason = TYPE_URGENCY.get(top_type.alert_type, (10, "ustalenie kontrolne"))
    reasons = [type_reason]
    if deadline:
        overdue_days = (analysis_date - deadline).days
        if overdue_days > 30:
            score += 20
            reasons.append(f"termin przekroczony o {overdue_days} dni")
        elif overdue_days > 7:
            score += 10
            reasons.append(f"termin przekroczony o {overdue_days} dni")
        elif overdue_days > 0:
            score += 5
            reasons.append(f"termin przekroczony o {overdue_days} dni")
        else:
            reasons.append(f"termin za {abs(overdue_days)} dni")
    for currency, band in amount_bands:
        if band == "top":
            score += 10
            reasons.append(f"kwota w najwyższej grupie spraw {currency}")
        elif band == "middle":
            score += 5
            reasons.append(f"kwota w środkowej grupie spraw {currency}")
    priority = "high" if score >= 40 else "medium" if score >= 25 else "low"
    return priority, "; ".join(reasons) + ".", score


def operational_case_sort_key(item: dict[str, Any]) -> tuple[int, str, str]:
    """Canonical queue order shared by the API and summary preview."""
    return (
        -PRIORITY_RANK.get(item["priority"], 0),
        item["deadline"] or "9999-12-31",
        item["id"],
    )


def build_operational_cases(
    db: Session,
    alerts: list[Alert] | None = None,
    run: ReconciliationRun | None = None,
) -> list[dict[str, Any]]:
    alerts = alerts if alerts is not None else db.scalars(select(Alert).where(Alert.active.is_(True))).all()
    run = run or db.scalar(select(ReconciliationRun).order_by(ReconciliationRun.id.desc()).limit(1))
    if run is None:
        return []
    groups = group_alerts(alerts)
    orders = {item.id: item for item in db.scalars(select(Order)).all()}
    returns = {item.id: item for item in db.scalars(select(ReturnRecord)).all()}
    contributions = financial_contributions(alerts)
    contributions_by_case: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in contributions:
        contributions_by_case[item["case_id"]].append(item)

    raw_cases: list[dict[str, Any]] = []
    values_by_currency: dict[str, list[Decimal]] = defaultdict(list)
    for case_id, related in groups.items():
        amounts: dict[str, Decimal] = defaultdict(lambda: ZERO)
        for item in contributions_by_case.get(case_id, []):
            amounts[item["currency"]] += item["amount"]
        for currency, amount in amounts.items():
            if amount > ZERO:
                values_by_currency[currency].append(amount)
        deadline = _deadline_for_case(related, orders, returns, run.document_grace_days)
        title, description = _case_copy(related)
        refs: dict[tuple[str, int], dict[str, Any]] = {}
        for alert in related:
            for ref in alert.source_records:
                refs[(ref["record_type"], ref["id"])] = ref
        raw_cases.append(
            {
                "id": case_id,
                "title": title,
                "description": description,
                "handling_status": _case_status(related),
                "active": any(alert.active for alert in related),
                "finding_count": len(related),
                "alert_types": sorted({alert.alert_type for alert in related}),
                "findings": [alert_dict(alert) for alert in sorted(related, key=lambda item: item.id)],
                "source_records": list(refs.values()),
                "amounts": [
                    {"currency": currency, "amount": format(money(amount), "f")}
                    for currency, amount in sorted(amounts.items())
                ],
                "deadline": deadline.isoformat() if deadline else None,
                "days_overdue": (run.analysis_at.date() - deadline).days if deadline else None,
                "updated_at": max(alert.updated_at for alert in related),
                "order_id": next((alert.order_id for alert in related if alert.order_id is not None), None),
                "contributions": [
                    {**item, "amount": format(money(item["amount"]), "f")}
                    for item in contributions_by_case.get(case_id, [])
                ],
            }
        )

    for case in raw_cases:
        bands: list[tuple[str, str]] = []
        for amount_item in case["amounts"]:
            currency = amount_item["currency"]
            amount = Decimal(amount_item["amount"])
            ordered = sorted(values_by_currency[currency], reverse=True)
            position = ordered.index(amount)
            third = max(1, (len(ordered) + 2) // 3)
            band = "top" if position < third else "middle" if position < third * 2 else "lower"
            bands.append((currency, band))
        related = groups[case["id"]]
        priority, reason, score = _priority_for_case(
            related,
            date.fromisoformat(case["deadline"]) if case["deadline"] else None,
            run.analysis_at.date(),
            bands,
        )
        case["priority"] = priority
        case["priority_reason"] = reason
        case["priority_rank"] = score

    return sorted(raw_cases, key=operational_case_sort_key)


def _record_dict(record: Any, record_type: str, fields: list[str]) -> dict[str, Any]:
    return {
        "record_type": record_type,
        **{field: json_value(getattr(record, field)) for field in fields},
    }


def case_detail(db: Session, case_id: str) -> dict[str, Any] | None:
    run = db.scalar(select(ReconciliationRun).order_by(ReconciliationRun.id.desc()).limit(1))
    all_alerts = db.scalars(select(Alert)).all()
    related = group_alerts(all_alerts).get(case_id, [])
    if not related or run is None:
        return None

    current_related = [alert for alert in related if alert.active]
    display_alerts = current_related or related
    cases = build_operational_cases(db, display_alerts, run)
    case = next((item for item in cases if item["id"] == case_id), None)
    if case is None:
        return None

    # Queue and summary contain current findings only. Details additionally
    # retain historical findings and their decisions, while current amounts
    # are always calculated from the latest run.
    case["findings"] = [alert_dict(alert) for alert in sorted(related, key=lambda item: item.id)]
    case["finding_count"] = len(related)
    case["historical_finding_count"] = sum(not alert.active for alert in related)
    case["active"] = bool(current_related)
    if not current_related:
        case["amounts"] = []
        case["contributions"] = []
    records: dict[tuple[str, int], dict[str, Any]] = {}

    order = db.get(Order, case["order_id"]) if case["order_id"] is not None else None
    if order:
        records[("order", order.id)] = _record_dict(
            order,
            "order",
            ["id", "source_system", "external_id", "sales_channel", "order_date", "due_date", "status", "gross_amount", "currency", "customer_name", "customer_email", "source_file", "source_row"],
        )
        assignments = db.scalars(select(PaymentAssignment).where(PaymentAssignment.order_id == order.id)).all()
        payments = [db.get(Payment, assignment.payment_id) for assignment in assignments]
        for payment in [item for item in payments if item is not None]:
            records[("payment", payment.id)] = _record_dict(
                payment,
                "payment",
                ["id", "source_system", "transaction_id", "kind", "status", "transaction_date", "amount", "currency", "title", "order_ref", "order_source_system", "return_ref", "original_payment_ref", "processing_scope", "source_file", "source_row"],
            )
        documents = db.scalars(select(Document)).all()
        for document in documents:
            if (document.order_source_system or document.source_system, document.order_ref) == (order.source_system, order.external_id):
                records[("document", document.id)] = _record_dict(
                    document,
                    "document",
                    ["id", "source_system", "external_id", "number", "document_type", "status", "document_date", "amount", "currency", "order_ref", "order_source_system", "original_document_ref", "source_file", "source_row"],
                )
        returns = db.scalars(select(ReturnRecord)).all()
        for return_record in returns:
            if (return_record.order_source_system or return_record.source_system, return_record.order_ref) == (order.source_system, order.external_id):
                records[("return", return_record.id)] = _record_dict(
                    return_record,
                    "return",
                    ["id", "source_system", "external_id", "order_ref", "order_source_system", "return_date", "status", "expected_refund_amount", "currency", "refund_due_date", "correction_ref", "source_file", "source_row"],
                )
    else:
        model_by_type = {"payment": Payment, "document": Document, "return": ReturnRecord, "order": Order}
        fields_by_type = {
            "payment": ["id", "source_system", "transaction_id", "kind", "status", "transaction_date", "amount", "currency", "title", "order_ref", "order_source_system", "return_ref", "original_payment_ref", "processing_scope", "source_file", "source_row"],
            "document": ["id", "source_system", "external_id", "number", "document_type", "status", "document_date", "amount", "currency", "order_ref", "order_source_system", "original_document_ref", "source_file", "source_row"],
            "return": ["id", "source_system", "external_id", "order_ref", "order_source_system", "return_date", "status", "expected_refund_amount", "currency", "refund_due_date", "correction_ref", "source_file", "source_row"],
            "order": ["id", "source_system", "external_id", "sales_channel", "order_date", "due_date", "status", "gross_amount", "currency", "customer_name", "customer_email", "source_file", "source_row"],
        }
        for alert in related:
            for ref in alert.source_records:
                model = model_by_type.get(ref["record_type"])
                if model:
                    record = db.get(model, ref["id"])
                    if record:
                        records[(ref["record_type"], ref["id"])] = _record_dict(
                            record, ref["record_type"], fields_by_type[ref["record_type"]]
                        )

    histories = db.scalars(
        select(AlertHistory).where(AlertHistory.alert_id.in_([alert.id for alert in related])).order_by(AlertHistory.created_at, AlertHistory.id)
    ).all()
    history = [
        {
            "id": item.id,
            "alert_id": item.alert_id,
            "alert_title": next(alert.title for alert in related if alert.id == item.alert_id),
            "action": item.action,
            "previous_status": item.previous_status,
            "new_status": item.new_status,
            "comment": item.comment,
            "actor": item.actor,
            "details": item.details,
            "created_at": item.created_at,
        }
        for item in histories
    ]
    decisions = db.scalars(select(UserDecision).order_by(UserDecision.created_at, UserDecision.id)).all()
    for decision in decisions:
        if (order and decision.order_id == order.id) or any(
            alert.entity_type == "payment" and decision.payment_id == alert.entity_id for alert in related
        ):
            history.append(
                {
                    "id": -decision.id,
                    "alert_id": None,
                    "alert_title": "Decyzja o powiązaniu",
                    "action": decision.action,
                    "previous_status": None,
                    "new_status": None,
                    "comment": decision.comment,
                    "actor": decision.actor,
                    "details": {"payment_id": decision.payment_id, "order_id": decision.order_id, "candidate_id": decision.candidate_id},
                    "created_at": decision.created_at,
                }
            )
    history.sort(key=lambda item: item["created_at"])

    payment_entity_ids = {
        alert.entity_id for alert in related if alert.entity_type == "payment"
    }
    candidates: list[dict[str, Any]] = []
    if payment_entity_ids:
        candidate_rows = db.scalars(
            select(MatchCandidate)
            .where(MatchCandidate.payment_id.in_(payment_entity_ids))
            .order_by(MatchCandidate.run_id.desc(), MatchCandidate.rule_score.desc())
        ).all()
        latest_run_id = candidate_rows[0].run_id if candidate_rows else None
        for candidate in [item for item in candidate_rows if item.run_id == latest_run_id]:
            candidate_order = db.get(Order, candidate.order_id)
            candidates.append(
                {
                    "id": candidate.id,
                    "order_id": candidate.order_id,
                    "rule_score": candidate.rule_score,
                    "rule_breakdown": candidate.rule_breakdown,
                    "status": candidate.status,
                    "order": (
                        _record_dict(
                            candidate_order,
                            "order",
                            ["id", "source_system", "external_id", "order_date", "due_date", "status", "gross_amount", "currency", "customer_name", "customer_email", "source_file", "source_row"],
                        )
                        if candidate_order
                        else None
                    ),
                }
            )

    calculation = None
    if order:
        assignments = db.scalars(select(PaymentAssignment).where(PaymentAssignment.order_id == order.id)).all()
        payment_pairs = [(db.get(Payment, item.payment_id), item) for item in assignments]
        completed = [
            (payment, assignment)
            for payment, assignment in payment_pairs
            if payment is not None
            and payment.kind == "payment"
            and payment.status == "completed"
            and payment.currency == order.currency
            and payment.transaction_date <= run.analysis_at.date()
        ]
        total = money(sum((payment.amount for payment, _ in completed), ZERO))
        difference = money(max(order.gross_amount - total, ZERO))
        calculation = {
            "order_value": format(money(order.gross_amount), "f"),
            "completed_payment_total": format(total, "f"),
            "missing_amount": format(difference, "f"),
            "currency": order.currency,
            "equation": f"{money(order.gross_amount)} − {total} = {difference} {order.currency}",
            "due_date": order.due_date.isoformat(),
            "analysis_at": run.analysis_at,
            "tolerance": format(run.tolerance, "f"),
            "completed_payments": [
                {
                    "transaction_id": payment.transaction_id,
                    "status": payment.status,
                    "amount": format(money(payment.amount), "f"),
                    "currency": payment.currency,
                    "transaction_date": payment.transaction_date.isoformat(),
                    "source_file": payment.source_file,
                    "source_row": payment.source_row,
                    "matching_explanation": assignment.explanation,
                }
                for payment, assignment in completed
            ],
            "other_currency_payments": [
                {
                    "transaction_id": payment.transaction_id,
                    "amount": format(money(payment.amount), "f"),
                    "currency": payment.currency,
                    "matching_explanation": assignment.explanation,
                }
                for payment, assignment in payment_pairs
                if payment is not None
                and payment.kind == "payment"
                and payment.status == "completed"
                and payment.currency != order.currency
                and payment.transaction_date <= run.analysis_at.date()
            ],
        }

    return {
        **case,
        "analysis_at": run.analysis_at,
        "run_started_at": run.started_at,
        "run_completed_at": run.completed_at,
        "records": list(records.values()),
        "history": history,
        "calculation": calculation,
        "candidates": candidates,
    }


def find_case_alerts(db: Session, case_id: str) -> list[Alert]:
    alerts = db.scalars(select(Alert).where(Alert.active.is_(True))).all()
    return group_alerts(alerts).get(case_id, [])

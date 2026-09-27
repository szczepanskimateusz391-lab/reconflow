from __future__ import annotations

import csv
from datetime import datetime, timezone
from decimal import Decimal
import io
import json
import logging
from typing import Any

from fastapi import Depends, FastAPI, File, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from .api_models import (
    AlertStatusRequest,
    CandidateDecisionRequest,
    ManualLinkRequest,
    ReconciliationRequest,
)
from .case_service import (
    build_operational_cases,
    case_detail,
    find_case_alerts,
    operational_case_sort_key,
)
from .config import get_settings
from .db import get_db
from .importer import batch_dict, import_csv
from .models import (
    Alert,
    AlertHistory,
    Document,
    ImportBatch,
    MatchCandidate,
    Order,
    Payment,
    PaymentAssignment,
    ReconciliationRun,
    RejectedRow,
    ReturnRecord,
    UserDecision,
)
from .reconciliation import latest_summary, run_reconciliation


logger = logging.getLogger(__name__)


app = FastAPI(
    title="ReconFlow API",
    version="0.1.0",
    description="Deterministyczne uzgadnianie syntetycznych danych e-commerce.",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (datetime,)):
        return value.isoformat()
    return value


def _model_dict(record: Any, fields: list[str]) -> dict[str, Any]:
    return {field: _json_value(getattr(record, field)) for field in fields}


def _alert_dict(alert: Alert) -> dict[str, Any]:
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
        "discrepancy_amount": _json_value(alert.discrepancy_amount),
        "handling_status": alert.handling_status,
        "confirmed_effect_amount": _json_value(alert.confirmed_effect_amount),
        "active": alert.active,
        "first_seen_run_id": alert.first_seen_run_id,
        "last_seen_run_id": alert.last_seen_run_id,
        "created_at": alert.created_at,
        "updated_at": alert.updated_at,
    }


@app.get("/api/health", response_model=None)
def health(db: Session = Depends(get_db)) -> Any:
    try:
        db.execute(text("SELECT 1"))
    except SQLAlchemyError:
        logger.exception("ReconFlow database health check failed")
        return JSONResponse(
            status_code=503,
            content={
                "status": "degraded",
                "api": "available",
                "database": "unavailable",
                "test_instance": get_settings().test_instance,
            },
        )
    return {
        "status": "ok",
        "api": "available",
        "database": "available",
        "test_instance": get_settings().test_instance,
    }


@app.post("/api/imports/{dataset}")
async def upload_import(
    dataset: str,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    if dataset not in {"orders", "payments", "documents", "returns"}:
        raise HTTPException(status_code=404, detail="Unknown dataset")
    content = await file.read()
    if len(content) > 20 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="CSV file exceeds the 20 MB demo limit")
    return batch_dict(import_csv(db, dataset, file.filename or f"{dataset}.csv", content))


@app.get("/api/imports")
def list_imports(db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    batches = db.scalars(select(ImportBatch).order_by(ImportBatch.id.desc())).all()
    return [batch_dict(batch) for batch in batches]


@app.get("/api/imports/{batch_id}")
def import_detail(batch_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    batch = db.get(ImportBatch, batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="Import not found")
    rejected = db.scalars(
        select(RejectedRow).where(RejectedRow.batch_id == batch_id).order_by(RejectedRow.row_number)
    ).all()
    result = batch_dict(batch)
    result["rejected_rows"] = [
        {
            "id": row.id,
            "file_name": row.file_name,
            "row_number": row.row_number,
            "reason": row.reason,
            "raw_data": row.raw_data,
            "is_conflict": row.is_conflict,
        }
        for row in rejected
    ]
    return result


@app.get("/api/imports/{batch_id}/rejected.csv")
def rejected_rows_csv(batch_id: int, db: Session = Depends(get_db)) -> StreamingResponse:
    batch = db.get(ImportBatch, batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="Import not found")
    rows = db.scalars(
        select(RejectedRow).where(RejectedRow.batch_id == batch_id).order_by(RejectedRow.row_number)
    ).all()
    output = io.StringIO()
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["file_name", "row_number", "is_conflict", "reason", "raw_data"])
    for row in rows:
        writer.writerow([row.file_name, row.row_number, row.is_conflict, row.reason, row.raw_data])
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="import-{batch_id}-rejected.csv"'},
    )


@app.post("/api/reconciliations")
def reconcile(
    request: ReconciliationRequest,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    run = run_reconciliation(db, request.analysis_at)
    return {
        "id": run.id,
        "status": run.status,
        "analysis_at": run.analysis_at,
        "completed_at": run.completed_at,
        "metrics": run.metrics,
    }


@app.get("/api/reconciliations")
def list_reconciliations(db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    runs = db.scalars(select(ReconciliationRun).order_by(ReconciliationRun.id.desc())).all()
    return [
        {
            "id": run.id,
            "status": run.status,
            "analysis_at": run.analysis_at,
            "started_at": run.started_at,
            "completed_at": run.completed_at,
            "tolerance": _json_value(run.tolerance),
            "document_grace_days": run.document_grace_days,
            "metrics": run.metrics,
        }
        for run in runs
    ]


@app.get("/api/summary")
def reconciliation_summary(db: Session = Depends(get_db)) -> dict[str, Any]:
    return latest_summary(db)


@app.get("/api/alerts")
def list_alerts(
    status: str | None = Query(default=None),
    alert_type: str | None = Query(default=None),
    currency: str | None = Query(default=None),
    active: bool = Query(default=True),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    statement = select(Alert).where(Alert.active.is_(active))
    if status:
        statement = statement.where(Alert.handling_status == status)
    if alert_type:
        statement = statement.where(Alert.alert_type == alert_type)
    if currency:
        statement = statement.where(Alert.currency == currency.upper())
    alerts = db.scalars(statement.order_by(Alert.updated_at.desc(), Alert.id.desc())).all()
    return [_alert_dict(alert) for alert in alerts]


@app.get("/api/cases")
def list_cases(
    status: str | None = Query(default=None),
    alert_type: str | None = Query(default=None),
    currency: str | None = Query(default=None),
    priority: str | None = Query(default=None),
    sort: str = Query(default="priority"),
    direction: str = Query(default="asc"),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    cases = build_operational_cases(db)
    if status == "open":
        cases = [item for item in cases if item["handling_status"] in {"new", "in_progress"}]
    elif status:
        if status not in {"new", "in_progress", "resolved", "ignored"}:
            raise HTTPException(status_code=422, detail="Unknown case status")
        cases = [item for item in cases if item["handling_status"] == status]
    if alert_type:
        cases = [item for item in cases if alert_type in item["alert_types"]]
    if currency:
        selected_currency = currency.upper()
        cases = [
            item
            for item in cases
            if any(amount["currency"] == selected_currency for amount in item["amounts"])
        ]
    if priority:
        if priority not in {"high", "medium", "low"}:
            raise HTTPException(status_code=422, detail="Unknown priority")
        cases = [item for item in cases if item["priority"] == priority]
    if sort not in {"priority", "deadline", "amount"}:
        raise HTTPException(status_code=422, detail="Unknown sort field")
    reverse = direction == "desc"
    if direction not in {"asc", "desc"}:
        raise HTTPException(status_code=422, detail="Unknown sort direction")
    if sort == "priority":
        # Priority has one business meaning: high to low, then earliest due date.
        # The direction parameter is accepted for API compatibility but does not
        # invert this canonical operational order.
        cases.sort(key=operational_case_sort_key)
    elif sort == "deadline":
        cases.sort(key=lambda item: item["deadline"] or "9999-12-31", reverse=reverse)
    else:
        if not currency:
            raise HTTPException(
                status_code=422,
                detail="Amount sorting requires one selected currency",
            )
        selected_currency = currency.upper()
        cases.sort(
            key=lambda item: next(
                (
                    Decimal(amount["amount"])
                    for amount in item["amounts"]
                    if amount["currency"] == selected_currency
                ),
                Decimal("0.00"),
            ),
            reverse=reverse,
        )
    return [
        {key: value for key, value in item.items() if key not in {"priority_rank", "contributions"}}
        for item in cases
    ]


@app.get("/api/cases/{case_id}")
def get_case(case_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    result = case_detail(db, case_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Case not found")
    return {key: value for key, value in result.items() if key != "priority_rank"}


@app.post("/api/cases/{case_id}/status")
def update_case_status(
    case_id: str,
    request: AlertStatusRequest,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    alerts = sorted(find_case_alerts(db, case_id), key=lambda item: item.id)
    if not alerts:
        raise HTTPException(status_code=404, detail="Case not found")
    if request.confirmed_effect_amount is not None:
        currencies = {alert.currency for alert in alerts if alert.currency}
        if len(currencies) != 1:
            raise HTTPException(
                status_code=422,
                detail="Confirmed effect requires a case with exactly one currency",
            )
    effect_target = next(
        (alert for alert in alerts if alert.currency and alert.discrepancy_amount is not None),
        alerts[0],
    )
    now = datetime.now(timezone.utc)
    for alert in alerts:
        previous = alert.handling_status
        requested_effect = request.confirmed_effect_amount if alert.id == effect_target.id else None
        latest_history = db.scalar(
            select(AlertHistory)
            .where(AlertHistory.alert_id == alert.id)
            .order_by(AlertHistory.id.desc())
            .limit(1)
        )
        if (
            previous == request.status
            and alert.confirmed_effect_amount == requested_effect
            and latest_history is not None
            and latest_history.action == "status_changed"
            and latest_history.new_status == request.status
            and latest_history.comment == request.comment
        ):
            # A repeated identical request is a transport/UI retry, not a new decision.
            continue
        alert.handling_status = request.status
        alert.confirmed_effect_amount = requested_effect
        alert.updated_at = now
        db.add(
            AlertHistory(
                alert_id=alert.id,
                action="status_changed",
                previous_status=previous,
                new_status=request.status,
                comment=request.comment,
                actor="demo-user",
                details={
                    "case_id": case_id,
                    "confirmed_effect_amount": (
                        format(request.confirmed_effect_amount, "f")
                        if alert.id == effect_target.id and request.confirmed_effect_amount is not None
                        else None
                    ),
                },
            )
        )
    db.commit()
    result = case_detail(db, case_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Case no longer available")
    return {key: value for key, value in result.items() if key != "priority_rank"}


def _source_entity(db: Session, record_type: str, record_id: int) -> dict[str, Any] | None:
    mapping: dict[str, tuple[Any, list[str]]] = {
        "order": (
            Order,
            [
                "id", "source_system", "external_id", "sales_channel", "order_date", "due_date",
                "status", "gross_amount", "currency", "customer_name", "customer_email", "source_file", "source_row",
            ],
        ),
        "payment": (
            Payment,
            [
                "id", "source_system", "transaction_id", "kind", "status", "transaction_date", "amount",
                "currency", "title", "order_ref", "order_source_system", "return_ref", "original_payment_ref",
                "processing_scope", "source_file", "source_row",
            ],
        ),
        "document": (
            Document,
            [
                "id", "source_system", "external_id", "number", "document_type", "status", "document_date",
                "amount", "currency", "order_ref", "order_source_system", "original_document_ref", "source_file", "source_row",
            ],
        ),
        "return": (
            ReturnRecord,
            [
                "id", "source_system", "external_id", "order_ref", "order_source_system", "return_date", "status",
                "expected_refund_amount", "currency", "refund_due_date", "correction_ref", "source_file", "source_row",
            ],
        ),
    }
    if record_type not in mapping:
        return None
    model, fields = mapping[record_type]
    record = db.get(model, record_id)
    return _model_dict(record, fields) if record else None


@app.get("/api/alerts/{alert_id}")
def alert_detail(alert_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    alert = db.get(Alert, alert_id)
    if alert is None:
        raise HTTPException(status_code=404, detail="Alert not found")
    histories = db.scalars(
        select(AlertHistory).where(AlertHistory.alert_id == alert_id).order_by(AlertHistory.id)
    ).all()
    candidates: list[MatchCandidate] = []
    if alert.entity_type == "payment":
        candidates = db.scalars(
            select(MatchCandidate)
            .where(MatchCandidate.payment_id == alert.entity_id)
            .order_by(MatchCandidate.run_id.desc(), MatchCandidate.rule_score.desc())
        ).all()
        latest_run = candidates[0].run_id if candidates else None
        candidates = [candidate for candidate in candidates if candidate.run_id == latest_run]
    result = _alert_dict(alert)
    result["records"] = [
        entity
        for ref in alert.source_records
        if (entity := _source_entity(db, ref["record_type"], ref["id"])) is not None
    ]
    result["history"] = [
        {
            "id": history.id,
            "action": history.action,
            "previous_status": history.previous_status,
            "new_status": history.new_status,
            "comment": history.comment,
            "actor": history.actor,
            "details": history.details,
            "created_at": history.created_at,
        }
        for history in histories
    ]
    decisions = db.scalars(select(UserDecision).order_by(UserDecision.id)).all()
    relevant_decisions = [
        decision
        for decision in decisions
        if (
            (alert.entity_type == "payment" and decision.payment_id == alert.entity_id)
            or (alert.order_id is not None and decision.order_id == alert.order_id)
        )
    ]
    result["history"].extend(
        {
            "id": -decision.id,
            "action": decision.action,
            "previous_status": None,
            "new_status": None,
            "comment": decision.comment,
            "actor": decision.actor,
            "details": {
                "payment_id": decision.payment_id,
                "order_id": decision.order_id,
                "candidate_id": decision.candidate_id,
            },
            "created_at": decision.created_at,
        }
        for decision in relevant_decisions
    )
    result["history"].sort(key=lambda item: item["created_at"])
    result["candidates"] = [
        {
            "id": candidate.id,
            "order_id": candidate.order_id,
            "rule_score": candidate.rule_score,
            "rule_breakdown": candidate.rule_breakdown,
            "status": candidate.status,
            "order": _source_entity(db, "order", candidate.order_id),
        }
        for candidate in candidates
    ]
    return result


@app.post("/api/alerts/{alert_id}/status")
def update_alert_status(
    alert_id: int,
    request: AlertStatusRequest,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    alert = db.get(Alert, alert_id)
    if alert is None:
        raise HTTPException(status_code=404, detail="Alert not found")
    previous = alert.handling_status
    alert.handling_status = request.status
    alert.confirmed_effect_amount = request.confirmed_effect_amount
    alert.updated_at = datetime.now(timezone.utc)
    db.add(
        AlertHistory(
            alert_id=alert.id,
            action="status_changed",
            previous_status=previous,
            new_status=request.status,
            comment=request.comment,
            actor="demo-user",
            details={
                "confirmed_effect_amount": (
                    format(request.confirmed_effect_amount, "f")
                    if request.confirmed_effect_amount is not None
                    else None
                )
            },
        )
    )
    db.commit()
    db.refresh(alert)
    return _alert_dict(alert)


def _manual_link(db: Session, payment: Payment, order: Order, comment: str, candidate_id: int | None) -> None:
    # PostgreSQL serializes competing decisions on the payment row. The second
    # transaction sees the assignment committed by the first and returns a
    # business conflict instead of leaking a unique-constraint exception.
    locked_payment = db.scalar(
        select(Payment).where(Payment.id == payment.id).with_for_update()
    )
    if locked_payment is None:
        raise HTTPException(status_code=404, detail="Payment not found")
    existing = db.scalar(select(PaymentAssignment).where(PaymentAssignment.payment_id == payment.id))
    if existing and existing.order_id != order.id:
        raise HTTPException(
            status_code=409,
            detail="This payment is already assigned to another order and cannot be used twice",
        )
    now = datetime.now(timezone.utc)
    if existing:
        existing.origin = "manual"
        existing.explanation = f"Ręczne powiązanie: {comment}"
        existing.updated_at = now
    else:
        db.add(
            PaymentAssignment(
                payment_id=payment.id,
                order_id=order.id,
                origin="manual",
                state="confirmed",
                explanation=f"Ręczne powiązanie: {comment}",
                created_at=now,
                updated_at=now,
            )
        )
    db.add(
        UserDecision(
            action="manual_link",
            payment_id=payment.id,
            order_id=order.id,
            candidate_id=candidate_id,
            comment=comment,
            actor="demo-user",
        )
    )


@app.post("/api/payments/{payment_id}/link")
def manual_link(
    payment_id: int,
    request: ManualLinkRequest,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    payment = db.get(Payment, payment_id)
    order = db.get(Order, request.order_id)
    if payment is None or order is None:
        raise HTTPException(status_code=404, detail="Payment or order not found")
    _manual_link(db, payment, order, request.comment, None)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="This payment is already assigned to another order and cannot be used twice",
        ) from exc
    return {"status": "saved", "payment_id": payment.id, "order_id": order.id}


@app.post("/api/candidates/{candidate_id}/decision")
def candidate_decision(
    candidate_id: int,
    request: CandidateDecisionRequest,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    candidate = db.get(MatchCandidate, candidate_id)
    if candidate is None:
        raise HTTPException(status_code=404, detail="Candidate not found")
    payment = db.get(Payment, candidate.payment_id)
    order = db.get(Order, candidate.order_id)
    if payment is None or order is None:
        raise HTTPException(status_code=404, detail="Candidate source record no longer exists")
    if request.decision == "approve":
        _manual_link(db, payment, order, request.comment, candidate.id)
        candidate.status = "approved"
        for other in db.scalars(
            select(MatchCandidate).where(
                MatchCandidate.run_id == candidate.run_id,
                MatchCandidate.payment_id == candidate.payment_id,
                MatchCandidate.id != candidate.id,
            )
        ).all():
            other.status = "rejected"
    else:
        candidate.status = "rejected"
        db.add(
            UserDecision(
                action="reject_candidate",
                payment_id=payment.id,
                order_id=order.id,
                candidate_id=candidate.id,
                comment=request.comment,
                actor="demo-user",
            )
        )
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="This payment is already assigned to another order and cannot be used twice",
        ) from exc
    return {"status": "saved", "candidate_id": candidate.id, "decision": request.decision}


@app.get("/api/exports/alerts.csv", include_in_schema=False)
@app.get("/api/exports/cases.csv")
def export_alerts(
    status: str | None = Query(default=None),
    alert_type: str | None = Query(default=None),
    currency: str | None = Query(default=None),
    priority: str | None = Query(default=None),
    sort: str = Query(default="priority"),
    direction: str = Query(default="asc"),
    db: Session = Depends(get_db),
) -> StreamingResponse:
    cases = list_cases(
        status=status,
        alert_type=alert_type,
        currency=currency,
        priority=priority,
        sort=sort,
        direction=direction,
        db=db,
    )
    output = io.StringIO()
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(
        [
            "row_type", "case_id", "handling_status", "priority", "title", "deadline",
            "currencies", "amounts", "finding_count", "finding_ids", "finding_types",
            "description", "source_records",
        ]
    )
    for case in cases:
        amounts = case["amounts"]
        findings = case["findings"]
        writer.writerow(
            [
                "operational_case",
                case["id"],
                case["handling_status"],
                case["priority"],
                case["title"],
                case["deadline"] or "",
                "|".join(item["currency"] for item in amounts),
                "|".join(f'{item["currency"]}:{item["amount"]}' for item in amounts),
                case["finding_count"],
                "|".join(str(item["id"]) for item in findings),
                "|".join(item["alert_type"] for item in findings),
                case["description"],
                json.dumps(case["source_records"], ensure_ascii=False, separators=(",", ":")),
            ]
        )
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="reconflow-cases.csv"'},
    )

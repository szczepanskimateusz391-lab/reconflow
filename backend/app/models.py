from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ImportBatch(Base):
    __tablename__ = "import_batches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    dataset: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    file_name: Mapped[str] = mapped_column(String(255), nullable=False)
    file_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="processing")
    imported_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    skipped_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    conflict_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rejected_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    rejected_rows: Mapped[list["RejectedRow"]] = relationship(
        back_populates="batch", cascade="all, delete-orphan"
    )


class RejectedRow(Base):
    __tablename__ = "rejected_rows"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("import_batches.id"), index=True)
    file_name: Mapped[str] = mapped_column(String(255), nullable=False)
    row_number: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    raw_data: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    is_conflict: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    batch: Mapped[ImportBatch] = relationship(back_populates="rejected_rows")


class SourceFields:
    source_system: Mapped[str] = mapped_column(String(80), nullable=False)
    import_batch_id: Mapped[int] = mapped_column(ForeignKey("import_batches.id"), nullable=False)
    source_file: Mapped[str] = mapped_column(String(255), nullable=False)
    source_row: Mapped[int] = mapped_column(Integer, nullable=False)
    record_hash: Mapped[str] = mapped_column(String(64), nullable=False)


class Order(SourceFields, Base):
    __tablename__ = "orders"
    __table_args__ = (
        UniqueConstraint("source_system", "external_id", name="uq_order_source_external"),
        CheckConstraint("gross_amount >= 0", name="ck_order_amount_nonnegative"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    external_id: Mapped[str] = mapped_column(String(120), nullable=False)
    sales_channel: Mapped[str] = mapped_column(String(80), nullable=False)
    order_date: Mapped[date] = mapped_column(Date, nullable=False)
    due_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    gross_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    customer_name: Mapped[str | None] = mapped_column(String(200))
    customer_email: Mapped[str | None] = mapped_column(String(320))

    assignments: Mapped[list["PaymentAssignment"]] = relationship(back_populates="order")


class Payment(SourceFields, Base):
    __tablename__ = "payments"
    __table_args__ = (
        UniqueConstraint("source_system", "transaction_id", name="uq_payment_source_transaction"),
        CheckConstraint("amount > 0", name="ck_payment_amount_positive"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    transaction_id: Mapped[str] = mapped_column(String(120), nullable=False)
    kind: Mapped[str] = mapped_column(String(10), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    transaction_date: Mapped[date] = mapped_column(Date, nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    order_ref: Mapped[str | None] = mapped_column(String(120))
    order_source_system: Mapped[str | None] = mapped_column(String(80))
    return_ref: Mapped[str | None] = mapped_column(String(120))
    original_payment_ref: Mapped[str | None] = mapped_column(String(120))
    processing_scope: Mapped[str] = mapped_column(String(30), nullable=False, default="direct")

    assignment: Mapped["PaymentAssignment | None"] = relationship(back_populates="payment")


class Document(SourceFields, Base):
    __tablename__ = "documents"
    __table_args__ = (
        UniqueConstraint("source_system", "external_id", name="uq_document_source_external"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    external_id: Mapped[str] = mapped_column(String(120), nullable=False)
    number: Mapped[str] = mapped_column(String(120), nullable=False)
    document_type: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    document_date: Mapped[date] = mapped_column(Date, nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    order_ref: Mapped[str] = mapped_column(String(120), nullable=False)
    order_source_system: Mapped[str | None] = mapped_column(String(80))
    original_document_ref: Mapped[str | None] = mapped_column(String(120))


class ReturnRecord(SourceFields, Base):
    __tablename__ = "returns"
    __table_args__ = (
        UniqueConstraint("source_system", "external_id", name="uq_return_source_external"),
        CheckConstraint("expected_refund_amount >= 0", name="ck_return_amount_nonnegative"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    external_id: Mapped[str] = mapped_column(String(120), nullable=False)
    order_ref: Mapped[str] = mapped_column(String(120), nullable=False)
    order_source_system: Mapped[str | None] = mapped_column(String(80))
    return_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    expected_refund_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    refund_due_date: Mapped[date] = mapped_column(Date, nullable=False)
    correction_ref: Mapped[str | None] = mapped_column(String(120))


class PaymentAssignment(Base):
    __tablename__ = "payment_assignments"
    __table_args__ = (UniqueConstraint("payment_id", name="uq_assignment_payment"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    payment_id: Mapped[int] = mapped_column(ForeignKey("payments.id"), nullable=False)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), nullable=False, index=True)
    origin: Mapped[str] = mapped_column(String(20), nullable=False)
    state: Mapped[str] = mapped_column(String(20), nullable=False, default="confirmed")
    explanation: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    payment: Mapped[Payment] = relationship(back_populates="assignment")
    order: Mapped[Order] = relationship(back_populates="assignments")


class ReconciliationRun(Base):
    __tablename__ = "reconciliation_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    analysis_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    tolerance: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    document_grace_days: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="running")
    metrics: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OrderResult(Base):
    __tablename__ = "order_results"
    __table_args__ = (UniqueConstraint("run_id", "order_id", name="uq_result_run_order"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("reconciliation_runs.id"), index=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), index=True)
    relationship_state: Mapped[str] = mapped_column(String(30), nullable=False)
    financial_status: Mapped[str] = mapped_column(String(30), nullable=False)
    completed_payments: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    completed_refunds: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    document_total: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    explanation: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)


class MatchCandidate(Base):
    __tablename__ = "match_candidates"
    __table_args__ = (
        UniqueConstraint("run_id", "payment_id", "order_id", name="uq_candidate_run_pair"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("reconciliation_runs.id"), index=True)
    payment_id: Mapped[int] = mapped_column(ForeignKey("payments.id"), index=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), index=True)
    rule_score: Mapped[int] = mapped_column(Integer, nullable=False)
    rule_breakdown: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Alert(Base):
    __tablename__ = "alerts"
    __table_args__ = (
        UniqueConstraint("fingerprint", name="uq_alert_fingerprint"),
        Index("ix_alert_queue", "active", "handling_status", "alert_type"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    alert_type: Mapped[str] = mapped_column(String(80), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    source_records: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    entity_type: Mapped[str] = mapped_column(String(30), nullable=False)
    entity_id: Mapped[int] = mapped_column(Integer, nullable=False)
    order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id"), index=True)
    currency: Mapped[str | None] = mapped_column(String(3))
    discrepancy_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    handling_status: Mapped[str] = mapped_column(String(20), nullable=False, default="new")
    confirmed_effect_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    first_seen_run_id: Mapped[int] = mapped_column(ForeignKey("reconciliation_runs.id"))
    last_seen_run_id: Mapped[int] = mapped_column(ForeignKey("reconciliation_runs.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AlertHistory(Base):
    __tablename__ = "alert_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    alert_id: Mapped[int] = mapped_column(ForeignKey("alerts.id"), index=True)
    action: Mapped[str] = mapped_column(String(50), nullable=False)
    previous_status: Mapped[str | None] = mapped_column(String(20))
    new_status: Mapped[str | None] = mapped_column(String(20))
    comment: Mapped[str | None] = mapped_column(Text)
    actor: Mapped[str] = mapped_column(String(100), nullable=False, default="demo-user")
    details: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class UserDecision(Base):
    __tablename__ = "user_decisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    action: Mapped[str] = mapped_column(String(50), nullable=False)
    payment_id: Mapped[int | None] = mapped_column(ForeignKey("payments.id"), index=True)
    order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id"), index=True)
    candidate_id: Mapped[int | None] = mapped_column(ForeignKey("match_candidates.id"))
    comment: Mapped[str | None] = mapped_column(Text)
    actor: Mapped[str] = mapped_column(String(100), nullable=False, default="demo-user")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

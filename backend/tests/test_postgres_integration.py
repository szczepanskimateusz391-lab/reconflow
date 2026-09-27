from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal
import os
from threading import Barrier

from fastapi import HTTPException
import pytest
from sqlalchemy import func, select, text

from app.api_models import ManualLinkRequest
from app.db import Base, SessionLocal, engine
from app.importer import import_csv
from app.main import manual_link
from app.models import Alert, Order, OrderResult, Payment, PaymentAssignment, RejectedRow, UserDecision
from app.reconciliation import run_reconciliation


POSTGRES_TEST_URL = os.getenv("RECONFLOW_POSTGRES_TEST_URL")
pytestmark = pytest.mark.skipif(
    not POSTGRES_TEST_URL,
    reason="Set RECONFLOW_POSTGRES_TEST_URL and migrate the dedicated PostgreSQL database.",
)


@pytest.fixture(autouse=True)
def clean_postgres_database():
    if not POSTGRES_TEST_URL:
        yield
        return
    assert engine.url.get_backend_name() == "postgresql"
    assert str(engine.url) == POSTGRES_TEST_URL
    preparer = engine.dialect.identifier_preparer
    table_names = ", ".join(preparer.quote(table.name) for table in Base.metadata.sorted_tables)
    with engine.begin() as connection:
        connection.execute(text(f"TRUNCATE TABLE {table_names} RESTART IDENTITY CASCADE"))
    yield


def _csv(value: str) -> bytes:
    return value.encode("utf-8")


def test_postgres_reimport_and_source_identifier_conflict_do_not_overwrite():
    datasets = {
        "orders": _csv(
            "source_system,order_id,sales_channel,order_date,due_date,status,gross_amount,currency,customer_name,customer_email\n"
            "pg-audit,PG-ORDER,web,2025-01-01,2025-01-05,completed,150.10,PLN,Klient PG,pg@example.test\n"
        ),
        "payments": _csv(
            "source_system,transaction_id,kind,status,transaction_date,amount,currency,title,order_ref,order_source_system,return_ref,original_payment_ref,processing_scope\n"
            "pg-audit,PG-PAY,payment,completed,2025-01-02,100.10,PLN,Płatność,PG-ORDER,pg-audit,,,direct\n"
        ),
        "documents": _csv(
            "source_system,document_id,number,document_type,status,document_date,amount,currency,order_ref,order_source_system,original_document_ref\n"
            "pg-audit,PG-DOC,FV/PG,invoice,issued,2025-01-02,150.10,PLN,PG-ORDER,pg-audit,\n"
        ),
        "returns": _csv(
            "source_system,return_id,order_ref,order_source_system,return_date,status,expected_refund_amount,currency,refund_due_date,correction_ref\n"
            "pg-audit,PG-RET,PG-ORDER,pg-audit,2025-01-03,approved,10.10,PLN,2025-01-10,\n"
        ),
    }
    with SessionLocal() as db:
        for dataset, content in datasets.items():
            first = import_csv(db, dataset, f"{dataset}.csv", content)
            second = import_csv(db, dataset, f"{dataset}.csv", content)
            assert first.imported_count == 1
            assert second.imported_count == 0
            assert second.skipped_count == 1
            assert second.rejected_count == 0

        conflict = datasets["orders"].replace(b"150.10", b"999.99")
        batch = import_csv(db, "orders", "orders-conflict.csv", conflict)
        assert batch.conflict_count == 1
        assert batch.rejected_count == 1
        stored = db.scalar(
            select(Order).where(Order.source_system == "pg-audit", Order.external_id == "PG-ORDER")
        )
        assert stored.gross_amount == Decimal("150.10")
        rejection = db.scalar(select(RejectedRow).where(RejectedRow.batch_id == batch.id))
        assert rejection.is_conflict is True
        assert "source_identifier_conflict" in rejection.reason


def test_postgres_numeric_precision_and_analysis_date_boundary():
    orders = _csv(
        "source_system,order_id,sales_channel,order_date,due_date,status,gross_amount,currency,customer_name,customer_email\n"
        "pg-audit,DATE-BOUNDARY,web,2025-01-01,2025-01-05,completed,0.30,PLN,Granica Dat,date@example.test\n"
    )
    payments = _csv(
        "source_system,transaction_id,kind,status,transaction_date,amount,currency,title,order_ref,order_source_system,return_ref,original_payment_ref,processing_scope\n"
        "pg-audit,DEC-010,payment,completed,2025-01-10,0.10,PLN,Część 1,DATE-BOUNDARY,pg-audit,,,direct\n"
        "pg-audit,DEC-020,payment,completed,2025-01-20,0.20,PLN,Część 2,DATE-BOUNDARY,pg-audit,,,direct\n"
    )
    documents = _csv(
        "source_system,document_id,number,document_type,status,document_date,amount,currency,order_ref,order_source_system,original_document_ref\n"
        "pg-audit,D-DATE,FV/DATE,invoice,issued,2025-01-02,0.30,PLN,DATE-BOUNDARY,pg-audit,\n"
    )
    with SessionLocal() as db:
        import_csv(db, "orders", "orders.csv", orders)
        import_csv(db, "payments", "payments.csv", payments)
        import_csv(db, "documents", "documents.csv", documents)
        order = db.scalar(select(Order).where(Order.external_id == "DATE-BOUNDARY"))

        before = run_reconciliation(db, datetime(2025, 1, 19, 23, 59, tzinfo=timezone.utc))
        before_result = db.scalar(
            select(OrderResult).where(OrderResult.run_id == before.id, OrderResult.order_id == order.id)
        )
        assert before_result.completed_payments == Decimal("0.10")
        before_alert = db.scalar(
            select(Alert).where(Alert.order_id == order.id, Alert.alert_type == "underpayment_overdue", Alert.active.is_(True))
        )
        assert before_alert.discrepancy_amount == Decimal("0.20")

        boundary = run_reconciliation(db, datetime(2025, 1, 20, 0, 0, tzinfo=timezone.utc))
        boundary_result = db.scalar(
            select(OrderResult).where(OrderResult.run_id == boundary.id, OrderResult.order_id == order.id)
        )
        assert boundary_result.completed_payments == Decimal("0.30")
        assert db.scalar(
            select(func.count()).select_from(Alert).where(
                Alert.order_id == order.id,
                Alert.alert_type == "underpayment_overdue",
                Alert.active.is_(True),
            )
        ) == 0


def _unassigned_payment_fixture() -> tuple[int, tuple[int, int]]:
    orders = _csv(
        "source_system,order_id,sales_channel,order_date,due_date,status,gross_amount,currency,customer_name,customer_email\n"
        "pg-audit,LINK-1,web,2025-01-01,2025-01-10,completed,20.00,PLN,Pierwszy,one@example.test\n"
        "pg-audit,LINK-2,web,2025-01-01,2025-01-10,completed,20.00,PLN,Drugi,two@example.test\n"
    )
    payment = _csv(
        "source_system,transaction_id,kind,status,transaction_date,amount,currency,title,order_ref,order_source_system,return_ref,original_payment_ref,processing_scope\n"
        "pg-audit,LINK-PAY,payment,completed,2025-01-02,20.00,PLN,Nieprzypisana,,,,,direct\n"
    )
    with SessionLocal() as db:
        import_csv(db, "orders", "orders.csv", orders)
        import_csv(db, "payments", "payments.csv", payment)
        payment_id = db.scalar(select(Payment.id).where(Payment.transaction_id == "LINK-PAY"))
        order_ids = tuple(db.scalars(select(Order.id).order_by(Order.external_id)).all())
        return payment_id, (order_ids[0], order_ids[1])


def test_postgres_payment_cannot_be_assigned_to_two_orders():
    payment_id, order_ids = _unassigned_payment_fixture()
    with SessionLocal() as db:
        first = manual_link(
            payment_id,
            ManualLinkRequest(order_id=order_ids[0], comment="Pierwsza decyzja PostgreSQL"),
            db,
        )
        assert first["status"] == "saved"
        with pytest.raises(HTTPException) as conflict:
            manual_link(
                payment_id,
                ManualLinkRequest(order_id=order_ids[1], comment="Druga decyzja PostgreSQL"),
                db,
            )
        assert conflict.value.status_code == 409
        assignment = db.scalar(select(PaymentAssignment).where(PaymentAssignment.payment_id == payment_id))
        assert assignment.order_id == order_ids[0]
        assert db.scalar(select(func.count()).select_from(UserDecision)) == 1


def test_postgres_concurrent_assignment_has_one_winner_and_controlled_conflict():
    payment_id, order_ids = _unassigned_payment_fixture()
    barrier = Barrier(2)

    def attempt(order_id: int) -> tuple[str, int]:
        with SessionLocal() as db:
            barrier.wait(timeout=10)
            try:
                manual_link(
                    payment_id,
                    ManualLinkRequest(order_id=order_id, comment=f"Wyścig dla {order_id}"),
                    db,
                )
                return "saved", order_id
            except HTTPException as exc:
                return f"conflict-{exc.status_code}", order_id

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(attempt, order_ids))

    assert sorted(result[0] for result in results) == ["conflict-409", "saved"]
    winner = next(order_id for status, order_id in results if status == "saved")
    with SessionLocal() as db:
        assignments = db.scalars(
            select(PaymentAssignment).where(PaymentAssignment.payment_id == payment_id)
        ).all()
        assert len(assignments) == 1
        assert assignments[0].order_id == winner
        assert db.scalar(select(func.count()).select_from(UserDecision)) == 1

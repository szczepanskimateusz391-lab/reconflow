import csv
from decimal import Decimal
import io
from pathlib import Path

from sqlalchemy import func, select

from app.models import Order, RejectedRow
from conftest import ROOT, import_demo


def test_import_is_idempotent_and_preserves_decimal(client, session):
    orders_path = ROOT / "demo-data" / "orders.csv"
    first = client.post(
        "/api/imports/orders",
        files={"file": (orders_path.name, orders_path.read_bytes(), "text/csv")},
    )
    second = client.post(
        "/api/imports/orders",
        files={"file": (orders_path.name, orders_path.read_bytes(), "text/csv")},
    )

    assert first.json()["imported_count"] == 15
    assert second.json()["imported_count"] == 0
    assert second.json()["skipped_count"] == 15
    assert session.scalar(select(func.count()).select_from(Order)) == 15
    order = session.scalar(
        select(Order).where(Order.source_system == "shop-a", Order.external_id == "A-101")
    )
    assert order.gross_amount == Decimal("150.00")


def test_same_identifier_in_two_sources_is_not_merged(client, session):
    path = ROOT / "demo-data" / "orders.csv"
    client.post("/api/imports/orders", files={"file": (path.name, path.read_bytes(), "text/csv")})
    records = session.scalars(select(Order).where(Order.external_id == "A-100")).all()
    assert {(record.source_system, record.gross_amount) for record in records} == {
        ("shop-a", Decimal("100.00")),
        ("shop-b", Decimal("55.00")),
    }


def test_changed_record_is_conflict_and_not_overwritten(client, session):
    path = ROOT / "demo-data" / "orders.csv"
    client.post("/api/imports/orders", files={"file": (path.name, path.read_bytes(), "text/csv")})
    conflict_path = ROOT / "demo-data" / "conflicting-order.csv"
    response = client.post(
        "/api/imports/orders",
        files={"file": (conflict_path.name, conflict_path.read_bytes(), "text/csv")},
    )
    assert response.status_code == 200
    assert response.json()["conflict_count"] == 1
    assert response.json()["rejected_count"] == 1
    order = session.scalar(
        select(Order).where(Order.source_system == "shop-a", Order.external_id == "A-100")
    )
    assert order.gross_amount == Decimal("100.00")
    rejected = session.scalar(select(RejectedRow).where(RejectedRow.is_conflict.is_(True)))
    assert rejected.row_number == 2
    assert rejected.file_name == "conflicting-order.csv"


def test_header_and_row_validation_are_reported(client):
    malformed = b"source_system,order_id\nshop-a,X\n"
    response = client.post(
        "/api/imports/orders", files={"file": ("bad.csv", malformed, "text/csv")}
    )
    assert response.status_code == 200
    assert response.json()["status"] == "failed"
    detail = client.get(f"/api/imports/{response.json()['id']}").json()
    assert detail["rejected_rows"][0]["row_number"] == 1
    assert "Invalid CSV headers" in detail["rejected_rows"][0]["reason"]


def test_all_demo_files_import_without_rejections(client):
    import_demo(client)
    history = client.get("/api/imports").json()
    assert len(history) == 4
    assert all(batch["rejected_count"] == 0 for batch in history)
    assert {batch["dataset"]: batch["imported_count"] for batch in history} == {
        "orders": 15,
        "payments": 20,
        "documents": 18,
        "returns": 3,
    }


def test_reimport_of_all_four_demo_files_only_skips_existing_records(client):
    import_demo(client)
    expected = {"orders": 15, "payments": 20, "documents": 18, "returns": 3}
    for dataset, count in expected.items():
        path = ROOT / "demo-data" / f"{dataset}.csv"
        response = client.post(
            f"/api/imports/{dataset}",
            files={"file": (path.name, path.read_bytes(), "text/csv")},
        )
        payload = response.json()
        assert payload["imported_count"] == 0
        assert payload["skipped_count"] == count
        assert payload["conflict_count"] == 0
        assert payload["rejected_count"] == 0


def test_invalid_date_and_amount_have_exact_rejection_rows_and_csv_report(client):
    content = (
        "source_system,order_id,sales_channel,order_date,due_date,status,gross_amount,currency,customer_name,customer_email\n"
        "audit,A-VALID,web,2025-01-01,2025-01-10,completed,10.00,PLN,Klient Testowy,test@example.test\n"
        "audit,A-DATE,web,nie-data,2025-01-10,completed,20.00,PLN,Klient Data,data@example.test\n"
        "audit,A-AMOUNT,web,2025-01-01,2025-01-10,completed,abc,PLN,Klient Kwota,kwota@example.test\n"
    ).encode("utf-8")

    response = client.post(
        "/api/imports/orders",
        files={"file": ("invalid-rows.csv", content, "text/csv")},
    )
    payload = response.json()
    assert response.status_code == 200
    assert payload["status"] == "completed_with_errors"
    assert payload["imported_count"] == 1
    assert payload["skipped_count"] == 0
    assert payload["conflict_count"] == 0
    assert payload["rejected_count"] == 2

    detail = client.get(f"/api/imports/{payload['id']}").json()
    assert [row["row_number"] for row in detail["rejected_rows"]] == [3, 4]
    assert "order_date" in detail["rejected_rows"][0]["reason"]
    assert "gross_amount" in detail["rejected_rows"][1]["reason"]

    report = client.get(f"/api/imports/{payload['id']}/rejected.csv")
    rows = list(csv.DictReader(io.StringIO(report.text)))
    assert report.status_code == 200
    assert len(rows) == 2
    assert [row["row_number"] for row in rows] == ["3", "4"]
    assert all(row["file_name"] == "invalid-rows.csv" for row in rows)

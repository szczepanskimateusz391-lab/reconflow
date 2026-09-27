"""Import generated data, reconcile it, and compare results with independent truth files."""
from __future__ import annotations

import argparse
from collections import defaultdict
import csv
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import sys
import time


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", type=Path, default=Path("demo-data/generated"))
    parser.add_argument("--database", type=Path, default=Path("work/benchmark.db"))
    parser.add_argument("--output", type=Path, default=Path("docs/benchmark-results.json"))
    args = parser.parse_args()

    database = args.database.resolve()
    database.parent.mkdir(parents=True, exist_ok=True)
    if database.exists():
        database.unlink()
    os.environ["DATABASE_URL"] = f"sqlite+pysqlite:///{database.as_posix()}"
    os.environ["AMOUNT_TOLERANCE"] = "0.01"
    os.environ["DOCUMENT_GRACE_DAYS"] = "3"

    project_root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(project_root / "backend"))
    from sqlalchemy import select
    from app.db import Base, SessionLocal, engine
    from app.importer import import_csv
    from app.models import Alert, Order, Payment, PaymentAssignment
    from app.reconciliation import run_reconciliation

    Base.metadata.create_all(engine)
    db = SessionLocal()
    try:
        import_start = time.perf_counter()
        import_stats = {}
        for dataset in ("orders", "payments", "documents", "returns"):
            path = args.data / f"{dataset}.csv"
            batch = import_csv(db, dataset, path.name, path.read_bytes())
            import_stats[dataset] = {
                "imported": batch.imported_count,
                "rejected": batch.rejected_count,
                "conflicts": batch.conflict_count,
            }
        import_seconds = time.perf_counter() - import_start

        reconciliation_start = time.perf_counter()
        run = run_reconciliation(db, datetime(2025, 4, 30, 12, 0, tzinfo=timezone.utc))
        reconciliation_seconds = time.perf_counter() - reconciliation_start

        with (args.data / "truth_links.csv").open(encoding="utf-8", newline="") as handle:
            truth_links = {
                (row["payment_source_system"], row["transaction_id"]): row
                for row in csv.DictReader(handle)
            }
        orders = {order.id: order for order in db.scalars(select(Order)).all()}
        payments = {payment.id: payment for payment in db.scalars(select(Payment)).all()}
        assignments = db.scalars(select(PaymentAssignment)).all()
        automatic_assignments = [item for item in assignments if item.origin == "automatic_reference"]
        correct_automatic = 0
        false_automatic = 0
        for assignment in automatic_assignments:
            payment = payments[assignment.payment_id]
            order = orders[assignment.order_id]
            truth = truth_links.get((payment.source_system, payment.transaction_id))
            if truth and (order.source_system, order.external_id) == (
                truth["order_source_system"], truth["order_id"]
            ):
                correct_automatic += 1
            else:
                false_automatic += 1

        with (args.data / "expected_alerts.csv").open(encoding="utf-8", newline="") as handle:
            expected_rows = list(csv.DictReader(handle))
        expected = {
            (row["alert_type"], row["entity_type"], row["source_system"], row["external_id"])
            for row in expected_rows
        }
        actual = set()
        for alert in db.scalars(select(Alert).where(Alert.active.is_(True))).all():
            reference = next(
                (
                    ref
                    for ref in alert.source_records
                    if ref["record_type"] == alert.entity_type and ref["id"] == alert.entity_id
                ),
                None,
            )
            if reference:
                actual.add(
                    (
                        alert.alert_type,
                        alert.entity_type,
                        reference["source_system"],
                        reference["external_id"],
                    )
                )

        types = sorted({item[0] for item in expected | actual})
        alert_metrics = {}
        for alert_type in types:
            expected_type = {item for item in expected if item[0] == alert_type}
            actual_type = {item for item in actual if item[0] == alert_type}
            true_positive = len(expected_type & actual_type)
            precision = true_positive / len(actual_type) if actual_type else (1.0 if not expected_type else 0.0)
            recall = true_positive / len(expected_type) if expected_type else (1.0 if not actual_type else 0.0)
            alert_metrics[alert_type] = {
                "expected": len(expected_type),
                "actual": len(actual_type),
                "true_positive": true_positive,
                "precision": round(precision, 6),
                "recall": round(recall, 6),
            }

        results = {
            "measured_at": datetime.now(timezone.utc).isoformat(),
            "synthetic": True,
            "seed": 20250914,
            "analysis_at": "2025-04-30T12:00:00+00:00",
            "environment": {
                "platform": platform.platform(),
                "python": platform.python_version(),
                "database": "SQLite benchmark harness (production target: PostgreSQL 17.2)",
                "processor": platform.processor() or "not reported",
            },
            "records": import_stats,
            "linking": {
                "truth_links": len(truth_links),
                "automatic_assignments": len(automatic_assignments),
                "correct_automatic_assignments": correct_automatic,
                "false_automatic_assignments": false_automatic,
                "automatic_link_precision": round(
                    correct_automatic / len(automatic_assignments), 6
                ) if automatic_assignments else 1.0,
                "automatic_handling_rate": round(
                    correct_automatic / len(truth_links), 6
                ) if truth_links else 1.0,
            },
            "alerts": {
                "expected_total": len(expected),
                "actual_total": len(actual),
                "by_type": alert_metrics,
            },
            "timing_seconds": {
                "csv_import": round(import_seconds, 4),
                "reconciliation": round(reconciliation_seconds, 4),
                "total": round(import_seconds + reconciliation_seconds, 4),
            },
            "run_metrics": run.metrics,
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
        print(json.dumps(results, indent=2, ensure_ascii=False))
    finally:
        db.close()


if __name__ == "__main__":
    main()

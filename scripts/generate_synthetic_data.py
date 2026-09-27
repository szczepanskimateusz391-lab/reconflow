"""Generate deterministic synthetic ReconFlow CSVs and independent ground truth.

The generator deliberately does not import or call the reconciliation engine.
"""
from __future__ import annotations

import argparse
import csv
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
import random


ORDER_HEADERS = [
    "source_system", "order_id", "sales_channel", "order_date", "due_date", "status",
    "gross_amount", "currency", "customer_name", "customer_email",
]
PAYMENT_HEADERS = [
    "source_system", "transaction_id", "kind", "status", "transaction_date", "amount",
    "currency", "title", "order_ref", "order_source_system", "return_ref",
    "original_payment_ref", "processing_scope",
]
DOCUMENT_HEADERS = [
    "source_system", "document_id", "number", "document_type", "status", "document_date",
    "amount", "currency", "order_ref", "order_source_system", "original_document_ref",
]
RETURN_HEADERS = [
    "source_system", "return_id", "order_ref", "order_source_system", "return_date",
    "status", "expected_refund_amount", "currency", "refund_due_date", "correction_ref",
]
TRUTH_LINK_HEADERS = [
    "payment_source_system", "transaction_id", "order_source_system", "order_id", "expected_mode",
]
TRUTH_ALERT_HEADERS = [
    "alert_type", "entity_type", "source_system", "external_id", "currency", "discrepancy_amount",
]


def amount(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.01")), "f")


def add_alert(rows, alert_type, entity_type, source, external_id, currency="", discrepancy=""):
    rows.append(
        {
            "alert_type": alert_type,
            "entity_type": entity_type,
            "source_system": source,
            "external_id": external_id,
            "currency": currency,
            "discrepancy_amount": amount(Decimal(discrepancy)) if discrepancy != "" else "",
        }
    )


def generate(order_count: int, seed: int, output: Path) -> None:
    rng = random.Random(seed)
    output.mkdir(parents=True, exist_ok=True)
    orders, payments, documents, returns, truth_links, truth_alerts = [], [], [], [], [], []
    scenario_population = (
        ["good"] * 60
        + ["manual_candidate"] * 3
        + ["partial"] * 8
        + ["under"] * 5
        + ["missing"] * 3
        + ["over"] * 2
        + ["pending"] * 2
        + ["failed"] * 2
        + ["currency"] * 2
        + ["duplicate"] * 1
        + ["missing_document"] * 2
        + ["refund_full"] * 3
        + ["refund_partial"] * 2
        + ["refund_over"] * 1
        + ["refund_missing"] * 2
        + ["cancelled"] * 2
    )
    start = date(2025, 1, 1)

    for index in range(1, order_count + 1):
        source = "shop-a" if index % 2 else "shop-b"
        order_id = f"ORD-{index:06d}"
        scenario = rng.choice(scenario_population)
        order_date = start + timedelta(days=rng.randrange(60))
        due_date = order_date + timedelta(days=7)
        cents = rng.randrange(5_000, 50_001)
        cents -= cents % 2
        gross = Decimal(cents) / 100
        currency = "EUR" if index % 11 == 0 else "PLN"
        customer_name = f"Klient {index:06d}"
        orders.append(
            {
                "source_system": source,
                "order_id": order_id,
                "sales_channel": "marketplace" if source == "shop-b" else "web",
                "order_date": order_date.isoformat(),
                "due_date": due_date.isoformat(),
                "status": "cancelled" if scenario == "cancelled" else "completed",
                "gross_amount": amount(gross),
                "currency": currency,
                "customer_name": customer_name,
                "customer_email": f"customer{index:06d}@example.test",
            }
        )

        def add_payment(
            suffix: str,
            kind: str,
            status: str,
            value: Decimal,
            payment_currency: str = currency,
            order_reference: str = order_id,
            return_ref: str = "",
            original_ref: str = "",
            title: str | None = None,
            expected_mode: str = "automatic_reference",
            payment_date: date | None = None,
        ) -> str:
            transaction_id = f"TX-{index:06d}{suffix}"
            payments.append(
                {
                    "source_system": source,
                    "transaction_id": transaction_id,
                    "kind": kind,
                    "status": status,
                    "transaction_date": (payment_date or order_date + timedelta(days=1)).isoformat(),
                    "amount": amount(value),
                    "currency": payment_currency,
                    "title": title or f"{kind.title()} {order_id}",
                    "order_ref": order_reference,
                    "order_source_system": source if order_reference else "",
                    "return_ref": return_ref,
                    "original_payment_ref": original_ref,
                    "processing_scope": "direct",
                }
            )
            truth_links.append(
                {
                    "payment_source_system": source,
                    "transaction_id": transaction_id,
                    "order_source_system": source,
                    "order_id": order_id,
                    "expected_mode": expected_mode,
                }
            )
            return transaction_id

        if scenario == "cancelled":
            continue
        if scenario == "partial":
            first = (gross * Decimal("0.40")).quantize(Decimal("0.01"))
            add_payment("-A", "payment", "completed", first)
            add_payment("-B", "payment", "completed", gross - first, payment_date=order_date + timedelta(days=2))
        elif scenario == "under":
            paid = (gross * Decimal("0.75")).quantize(Decimal("0.01"))
            add_payment("", "payment", "completed", paid)
            add_alert(truth_alerts, "underpayment_overdue", "order", source, order_id, currency, gross - paid)
        elif scenario == "missing":
            add_alert(truth_alerts, "missing_payment_overdue", "order", source, order_id, currency, gross)
        elif scenario == "over":
            add_payment("", "payment", "completed", gross + Decimal("5.00"))
            add_alert(truth_alerts, "overpayment", "order", source, order_id, currency, "5.00")
        elif scenario == "pending":
            add_payment("", "payment", "pending", gross)
            add_alert(truth_alerts, "missing_payment_overdue", "order", source, order_id, currency, gross)
        elif scenario == "failed":
            add_payment("", "payment", "failed", gross)
            add_alert(truth_alerts, "missing_payment_overdue", "order", source, order_id, currency, gross)
        elif scenario == "currency":
            other_currency = "PLN" if currency == "EUR" else "EUR"
            transaction_id = add_payment("", "payment", "completed", gross, other_currency)
            add_alert(truth_alerts, "currency_conflict", "payment", source, transaction_id)
            add_alert(truth_alerts, "missing_payment_overdue", "order", source, order_id, currency, gross)
        elif scenario == "duplicate":
            half = gross / 2
            add_payment("-A", "payment", "completed", half)
            add_payment("-B", "payment", "completed", half)
            add_alert(truth_alerts, "suspected_duplicate_payment", "order", source, order_id, currency, half)
        elif scenario == "manual_candidate":
            transaction_id = add_payment(
                "",
                "payment",
                "completed",
                gross,
                order_reference="",
                title=customer_name,
                expected_mode="manual_expected",
            )
            add_alert(truth_alerts, "unmatched_transaction", "payment", source, transaction_id, currency, gross)
            # Until a user approves the candidate, the order itself still has no confirmed payment.
            add_alert(truth_alerts, "missing_payment_overdue", "order", source, order_id, currency, gross)
        else:
            original_payment = add_payment("", "payment", "completed", gross)

        if scenario != "missing_document":
            document_id = f"DOC-{index:06d}"
            documents.append(
                {
                    "source_system": source,
                    "document_id": document_id,
                    "number": f"FV/{index:06d}",
                    "document_type": "invoice",
                    "status": "issued",
                    "document_date": order_date.isoformat(),
                    "amount": amount(gross),
                    "currency": currency,
                    "order_ref": order_id,
                    "order_source_system": source,
                    "original_document_ref": "",
                }
            )
        else:
            add_alert(truth_alerts, "missing_expected_document", "order", source, order_id, currency, gross)

        if scenario.startswith("refund_"):
            expected = (gross * Decimal("0.20")).quantize(Decimal("0.01"))
            return_id = f"RET-{index:06d}"
            correction_id = f"COR-{index:06d}"
            returns.append(
                {
                    "source_system": source,
                    "return_id": return_id,
                    "order_ref": order_id,
                    "order_source_system": source,
                    "return_date": (order_date + timedelta(days=10)).isoformat(),
                    "status": "approved",
                    "expected_refund_amount": amount(expected),
                    "currency": currency,
                    "refund_due_date": (order_date + timedelta(days=17)).isoformat(),
                    "correction_ref": correction_id,
                }
            )
            documents.append(
                {
                    "source_system": source,
                    "document_id": correction_id,
                    "number": f"KOR/{index:06d}",
                    "document_type": "correction",
                    "status": "issued",
                    "document_date": (order_date + timedelta(days=10)).isoformat(),
                    "amount": amount(-expected),
                    "currency": currency,
                    "order_ref": order_id,
                    "order_source_system": source,
                    "original_document_ref": f"DOC-{index:06d}",
                }
            )
            if scenario == "refund_full":
                add_payment("-R", "refund", "completed", expected, return_ref=return_id, original_ref=original_payment)
            elif scenario == "refund_partial":
                refunded = (expected / 2).quantize(Decimal("0.01"))
                add_payment("-R", "refund", "completed", refunded, return_ref=return_id, original_ref=original_payment)
                add_alert(truth_alerts, "partial_refund", "return", source, return_id, currency, expected - refunded)
            elif scenario == "refund_over":
                add_payment("-R", "refund", "completed", expected + Decimal("3.00"), return_ref=return_id, original_ref=original_payment)
                add_alert(truth_alerts, "refund_exceeds_expected", "return", source, return_id, currency, "3.00")
            elif scenario == "refund_missing":
                add_alert(truth_alerts, "refund_overdue", "return", source, return_id, currency, expected)

    # Standalone edge cases provide workload outside the order loop.
    for index in range(1, 51):
        transaction_id = f"PAYOUT-{index:04d}"
        payments.append(
            {
                "source_system": "marketplace-x", "transaction_id": transaction_id, "kind": "payment",
                "status": "completed", "transaction_date": "2025-03-31", "amount": "1000.00",
                "currency": "PLN", "title": "Marketplace settlement net of fees", "order_ref": "",
                "order_source_system": "", "return_ref": "", "original_payment_ref": "",
                "processing_scope": "marketplace_payout",
            }
        )
        add_alert(truth_alerts, "separate_process_required", "payment", "marketplace-x", transaction_id, "PLN", "1000.00")
    for index in range(1, 51):
        transaction_id = f"UNKNOWN-{index:04d}"
        payments.append(
            {
                "source_system": "bank", "transaction_id": transaction_id, "kind": "payment",
                "status": "completed", "transaction_date": "2025-03-31", "amount": "99999.00",
                "currency": "PLN", "title": "Unidentified transfer", "order_ref": "",
                "order_source_system": "", "return_ref": "", "original_payment_ref": "",
                "processing_scope": "direct",
            }
        )
        add_alert(truth_alerts, "unmatched_transaction", "payment", "bank", transaction_id, "PLN", "99999.00")
    for index in range(1, 21):
        document_id = f"ORPHAN-{index:04d}"
        documents.append(
            {
                "source_system": "shop-a", "document_id": document_id, "number": f"FV/X/{index:04d}",
                "document_type": "invoice", "status": "issued", "document_date": "2025-03-01",
                "amount": "50.00", "currency": "PLN", "order_ref": f"ABSENT-{index:04d}",
                "order_source_system": "shop-a", "original_document_ref": "",
            }
        )
        add_alert(truth_alerts, "document_without_order", "document", "shop-a", document_id, "PLN", "50.00")

    datasets = [
        ("orders.csv", ORDER_HEADERS, orders),
        ("payments.csv", PAYMENT_HEADERS, payments),
        ("documents.csv", DOCUMENT_HEADERS, documents),
        ("returns.csv", RETURN_HEADERS, returns),
        ("truth_links.csv", TRUTH_LINK_HEADERS, truth_links),
        ("expected_alerts.csv", TRUTH_ALERT_HEADERS, truth_alerts),
    ]
    for file_name, headers, rows in datasets:
        with (output / file_name).open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=headers, lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)

    (output / "MANIFEST.txt").write_text(
        f"SYNTHETIC DATA ONLY\nseed={seed}\norders={len(orders)}\npayments={len(payments)}\n"
        f"documents={len(documents)}\nreturns={len(returns)}\ntruth_links={len(truth_links)}\n"
        f"expected_alerts={len(truth_alerts)}\n",
        encoding="utf-8",
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--orders", type=int, default=10_000)
    parser.add_argument("--seed", type=int, default=20250914)
    parser.add_argument("--output", type=Path, default=Path("demo-data/generated"))
    args = parser.parse_args()
    generate(args.orders, args.seed, args.output)


if __name__ == "__main__":
    main()

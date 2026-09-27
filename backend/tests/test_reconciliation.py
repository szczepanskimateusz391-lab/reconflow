from collections import Counter
import csv
from datetime import datetime, timezone
from decimal import Decimal
import io

from sqlalchemy import func, select

from app.models import Alert, AlertHistory, MatchCandidate, Order, OrderResult, Payment, PaymentAssignment, UserDecision
from conftest import import_demo


ANALYSIS_AT = "2025-02-15T12:00:00Z"


def run_demo(client):
    response = client.post("/api/reconciliations", json={"analysis_at": ANALYSIS_AT})
    assert response.status_code == 200, response.text
    return response.json()


def test_expected_alerts_and_exact_reference_matching(client, session):
    import_demo(client)
    run_demo(client)
    alerts = client.get("/api/alerts").json()
    counts = Counter(alert["alert_type"] for alert in alerts)
    assert counts == Counter(
        {
            "underpayment_overdue": 1,
            "partial_refund": 1,
            "missing_payment_overdue": 2,
            "ambiguous_match": 1,
            "currency_conflict": 1,
            "suspected_duplicate_payment": 1,
            "refund_exceeds_expected": 1,
            "refund_overdue": 1,
            "missing_expected_document": 1,
            "overpayment": 1,
            "separate_process_required": 1,
            "unmatched_transaction": 1,
            "document_without_order": 1,
        }
    )

    underpaid = session.scalar(
        select(Order).where(Order.source_system == "shop-a", Order.external_id == "A-101")
    )
    assignment = session.scalar(
        select(PaymentAssignment)
        .join(Payment)
        .where(Payment.source_system == "shop-a", Payment.transaction_id == "P-101")
    )
    assert assignment.order_id == underpaid.id
    assert assignment.origin == "automatic_reference"
    underpayment_alert = next(item for item in alerts if item["alert_type"] == "underpayment_overdue")
    assert underpayment_alert["discrepancy_amount"] == "50.00"


def test_pending_before_due_is_not_overdue_and_failed_is_not_paid(client):
    import_demo(client)
    run_demo(client)
    alerts = client.get("/api/alerts").json()
    order_a104 = client.get("/api/summary").json()
    assert not any(
        alert["order_id"]
        and any(ref.get("external_id") == "A-104" for ref in alert["source_records"])
        and alert["alert_type"] in {"missing_payment_overdue", "underpayment_overdue"}
        for alert in alerts
    )
    assert any(
        alert["alert_type"] == "missing_payment_overdue"
        and any(ref.get("external_id") == "A-105" for ref in alert["source_records"])
        for alert in alerts
    )


def test_demo_payment_and_refund_totals_use_only_completed_same_currency_records(client, session):
    import_demo(client)
    run = run_demo(client)
    orders = {order.external_id: order for order in session.scalars(select(Order)).all()}

    def result(order_id):
        return session.scalar(
            select(OrderResult).where(
                OrderResult.run_id == run["id"],
                OrderResult.order_id == orders[order_id].id,
            )
        )

    assert result("A-102").completed_payments == Decimal("200.00")
    assert result("A-104").completed_payments == Decimal("0.00")
    assert result("A-105").completed_payments == Decimal("0.00")
    assert result("A-108").completed_payments == Decimal("0.00")
    assert result("A-109").completed_payments == Decimal("100.00")
    assert result("A-103").completed_refunds == Decimal("20.00")
    assert result("A-110").completed_refunds == Decimal("25.00")
    assert result("A-111").completed_refunds == Decimal("0.00")


def test_future_completed_payment_is_not_counted_before_analysis_date(client, session):
    orders = (
        "source_system,order_id,sales_channel,order_date,due_date,status,gross_amount,currency,customer_name,customer_email\n"
        "audit,FUTURE-PAY,web,2025-01-01,2025-01-10,completed,100.00,PLN,Przyszła Płatność,future@example.test\n"
    ).encode()
    payments = (
        "source_system,transaction_id,kind,status,transaction_date,amount,currency,title,order_ref,order_source_system,return_ref,original_payment_ref,processing_scope\n"
        "audit,P-FUTURE,payment,completed,2025-01-20,100.00,PLN,Future payment,FUTURE-PAY,audit,,,direct\n"
    ).encode()
    client.post("/api/imports/orders", files={"file": ("orders.csv", orders, "text/csv")})
    client.post("/api/imports/payments", files={"file": ("payments.csv", payments, "text/csv")})
    order = session.scalar(select(Order).where(Order.external_id == "FUTURE-PAY"))

    before = client.post(
        "/api/reconciliations", json={"analysis_at": "2025-01-15T12:00:00Z"}
    ).json()
    before_result = session.scalar(
        select(OrderResult).where(
            OrderResult.run_id == before["id"], OrderResult.order_id == order.id
        )
    )
    before_alerts = client.get("/api/alerts").json()
    assert before_result.completed_payments == Decimal("0.00")
    assert any(
        alert["alert_type"] == "missing_payment_overdue" and alert["order_id"] == order.id
        for alert in before_alerts
    )

    after = client.post(
        "/api/reconciliations", json={"analysis_at": "2025-01-25T12:00:00Z"}
    ).json()
    after_result = session.scalar(
        select(OrderResult).where(
            OrderResult.run_id == after["id"], OrderResult.order_id == order.id
        )
    )
    after_alerts = client.get("/api/alerts").json()
    assert after_result.completed_payments == Decimal("100.00")
    assert not any(
        alert["order_id"] == order.id
        and alert["alert_type"] in {"missing_payment_overdue", "underpayment_overdue"}
        for alert in after_alerts
    )


def test_future_completed_refund_is_not_counted_before_analysis_date(client, session):
    orders = (
        "source_system,order_id,sales_channel,order_date,due_date,status,gross_amount,currency,customer_name,customer_email\n"
        "audit,FUTURE-REFUND,web,2025-01-01,2025-01-05,completed,100.00,PLN,Przyszły Zwrot,refund@example.test\n"
    ).encode()
    payments = (
        "source_system,transaction_id,kind,status,transaction_date,amount,currency,title,order_ref,order_source_system,return_ref,original_payment_ref,processing_scope\n"
        "audit,P-BASE,payment,completed,2025-01-02,100.00,PLN,Base payment,FUTURE-REFUND,audit,,,direct\n"
        "audit,R-FUTURE,refund,completed,2025-01-20,40.00,PLN,Future refund,FUTURE-REFUND,audit,RET-FUTURE,P-BASE,direct\n"
    ).encode()
    returns = (
        "source_system,return_id,order_ref,order_source_system,return_date,status,expected_refund_amount,currency,refund_due_date,correction_ref\n"
        "audit,RET-FUTURE,FUTURE-REFUND,audit,2025-01-03,approved,40.00,PLN,2025-01-10,\n"
    ).encode()
    client.post("/api/imports/orders", files={"file": ("orders.csv", orders, "text/csv")})
    client.post("/api/imports/payments", files={"file": ("payments.csv", payments, "text/csv")})
    client.post("/api/imports/returns", files={"file": ("returns.csv", returns, "text/csv")})
    order = session.scalar(select(Order).where(Order.external_id == "FUTURE-REFUND"))

    before = client.post(
        "/api/reconciliations", json={"analysis_at": "2025-01-15T12:00:00Z"}
    ).json()
    before_result = session.scalar(
        select(OrderResult).where(
            OrderResult.run_id == before["id"], OrderResult.order_id == order.id
        )
    )
    assert before_result.completed_refunds == Decimal("0.00")
    assert any(
        alert["alert_type"] == "refund_overdue" and alert["order_id"] == order.id
        for alert in client.get("/api/alerts").json()
    )

    after = client.post(
        "/api/reconciliations", json={"analysis_at": "2025-01-25T12:00:00Z"}
    ).json()
    after_result = session.scalar(
        select(OrderResult).where(
            OrderResult.run_id == after["id"], OrderResult.order_id == order.id
        )
    )
    assert after_result.completed_refunds == Decimal("40.00")
    assert not any(
        alert["order_id"] == order.id
        and alert["alert_type"] in {"refund_overdue", "partial_refund"}
        for alert in client.get("/api/alerts").json()
    )


def test_a109_duplicate_is_balanced_and_has_no_overdue_deadline(client, session):
    import_demo(client)
    run = run_demo(client)
    order = session.scalar(select(Order).where(Order.external_id == "A-109"))
    result = session.scalar(
        select(OrderResult).where(
            OrderResult.run_id == run["id"], OrderResult.order_id == order.id
        )
    )
    alerts = client.get("/api/alerts").json()
    assert result.completed_payments == Decimal("100.00")
    assert any(
        alert["alert_type"] == "suspected_duplicate_payment" and alert["order_id"] == order.id
        for alert in alerts
    )
    assert not any(
        alert["order_id"] == order.id
        and alert["alert_type"] in {"overpayment", "underpayment_overdue", "missing_payment_overdue"}
        for alert in alerts
    )
    case = next(item for item in client.get("/api/cases").json() if item["order_id"] == order.id)
    assert case["deadline"] is None
    assert case["days_overdue"] is None
    assert "termin" not in case["priority_reason"]
    overpaid_order = session.scalar(select(Order).where(Order.external_id == "A-113"))
    overpayment_case = next(
        item for item in client.get("/api/cases").json()
        if item["order_id"] == overpaid_order.id and "overpayment" in item["alert_types"]
    )
    assert overpayment_case["deadline"] is None
    assert overpayment_case["days_overdue"] is None
    assert "termin" not in overpayment_case["priority_reason"]


def test_ambiguous_candidate_is_not_auto_linked(client, session):
    import_demo(client)
    run_demo(client)
    payment = session.scalar(select(Payment).where(Payment.transaction_id == "P-AMB"))
    assert session.scalar(
        select(PaymentAssignment).where(PaymentAssignment.payment_id == payment.id)
    ) is None
    candidates = session.scalars(
        select(MatchCandidate).where(MatchCandidate.payment_id == payment.id)
    ).all()
    assert len(candidates) == 2
    assert candidates[0].rule_score == candidates[1].rule_score


def test_rerun_reuses_alerts_and_preserves_manual_decision(client, session):
    import_demo(client)
    run_demo(client)
    ambiguous = next(
        alert for alert in client.get("/api/alerts").json() if alert["alert_type"] == "ambiguous_match"
    )
    detail = client.get(f"/api/alerts/{ambiguous['id']}").json()
    selected = detail["candidates"][0]
    response = client.post(
        f"/api/candidates/{selected['id']}/decision",
        json={"decision": "approve", "comment": "Zweryfikowano z syntetycznym wyciągiem."},
    )
    assert response.status_code == 200
    alert_count_before = session.scalar(select(func.count()).select_from(Alert))

    run_demo(client)
    alert_count_after = session.scalar(select(func.count()).select_from(Alert))
    payment = session.scalar(select(Payment).where(Payment.transaction_id == "P-AMB"))
    assignment = session.scalar(
        select(PaymentAssignment).where(PaymentAssignment.payment_id == payment.id)
    )
    assert assignment.origin == "manual"
    assert assignment.order_id == selected["order_id"]
    assert alert_count_after == alert_count_before
    assert session.scalar(select(func.count()).select_from(UserDecision)) == 1
    assert not any(
        alert["alert_type"] == "ambiguous_match" for alert in client.get("/api/alerts").json()
    )


def test_rejected_candidate_stays_rejected_after_rerun(client, session):
    import_demo(client)
    run_demo(client)
    ambiguous = next(
        alert for alert in client.get("/api/alerts").json() if alert["alert_type"] == "ambiguous_match"
    )
    detail = client.get(f"/api/alerts/{ambiguous['id']}").json()
    rejected = detail["candidates"][0]
    response = client.post(
        f"/api/candidates/{rejected['id']}/decision",
        json={"decision": "reject", "comment": "Wykluczono po kontroli identyfikatora."},
    )
    assert response.status_code == 200
    run_demo(client)
    active = client.get("/api/alerts").json()
    payment_alert = next(
        alert
        for alert in active
        if alert["entity_type"] == "payment"
        and any(ref.get("external_id") == "P-AMB" for ref in alert["source_records"])
    )
    rerun_detail = client.get(f"/api/alerts/{payment_alert['id']}").json()
    persisted = next(item for item in rerun_detail["candidates"] if item["order_id"] == rejected["order_id"])
    assert persisted["status"] == "rejected"
    assert any(item["action"] == "reject_candidate" for item in rerun_detail["history"])


def test_payment_cannot_be_assigned_twice(client, session):
    import_demo(client)
    run_demo(client)
    payment = session.scalar(select(Payment).where(Payment.transaction_id == "P-AMB"))
    orders = session.scalars(
        select(Order).where(Order.external_id.in_(["A-106", "A-107"])).order_by(Order.id)
    ).all()
    first = client.post(
        f"/api/payments/{payment.id}/link",
        json={"order_id": orders[0].id, "comment": "Pierwsza decyzja"},
    )
    second = client.post(
        f"/api/payments/{payment.id}/link",
        json={"order_id": orders[1].id, "comment": "Próba zmiany celu"},
    )
    assert first.status_code == 200
    assert second.status_code == 409
    assignment = session.scalar(
        select(PaymentAssignment).where(PaymentAssignment.payment_id == payment.id)
    )
    assert assignment.order_id == orders[0].id


def test_changed_data_preserves_history_and_recalculates_for_each_analysis_date(client, session):
    orders = (
        "source_system,order_id,sales_channel,order_date,due_date,status,gross_amount,currency,customer_name,customer_email\n"
        "audit,A-101,web,2025-01-01,2025-01-05,completed,150.00,PLN,Klient Testowy,a101@example.test\n"
    ).encode()
    first_payment = (
        "source_system,transaction_id,kind,status,transaction_date,amount,currency,title,"
        "order_ref,order_source_system,return_ref,original_payment_ref,processing_scope\n"
        "audit,P-A101-100,payment,completed,2025-01-03,100.00,PLN,Pierwsza wpłata,"
        "A-101,audit,,,direct\n"
    ).encode()
    documents = (
        "source_system,document_id,number,document_type,status,document_date,amount,currency,"
        "order_ref,order_source_system,original_document_ref\n"
        "audit,D-A101,FV/A-101,invoice,issued,2025-01-02,150.00,PLN,A-101,audit,\n"
    ).encode()
    assert client.post("/api/imports/orders", files={"file": ("orders.csv", orders, "text/csv")}).status_code == 200
    assert client.post("/api/imports/payments", files={"file": ("payments-before.csv", first_payment, "text/csv")}).status_code == 200
    assert client.post("/api/imports/documents", files={"file": ("documents.csv", documents, "text/csv")}).status_code == 200

    order = session.scalar(select(Order).where(Order.source_system == "audit", Order.external_id == "A-101"))
    case_id = f"order-{order.id}"

    before = client.post(
        "/api/reconciliations", json={"analysis_at": "2025-01-15T12:00:00Z"}
    )
    assert before.status_code == 200
    before_case = client.get(f"/api/cases/{case_id}").json()
    assert before_case["calculation"]["equation"] == "150.00 − 100.00 = 50.00 PLN"
    assert [item["transaction_id"] for item in before_case["calculation"]["completed_payments"]] == [
        "P-A101-100"
    ]
    underpayment_id = next(
        finding["id"] for finding in before_case["findings"]
        if finding["alert_type"] == "underpayment_overdue"
    )

    decision_comment = "Wyjaśniono niedopłatę na podstawie danych testowych."
    resolved = client.post(
        f"/api/cases/{case_id}/status",
        json={"status": "resolved", "comment": decision_comment},
    )
    assert resolved.status_code == 200

    extra_payment = (
        "source_system,transaction_id,kind,status,transaction_date,amount,currency,title,"
        "order_ref,order_source_system,return_ref,original_payment_ref,processing_scope\n"
        "audit,P-A101-50,payment,completed,2025-01-20,50.00,PLN,Dopłata,"
        "A-101,audit,,,direct\n"
    ).encode()
    imported = client.post(
        "/api/imports/payments",
        files={"file": ("payments-after.csv", extra_payment, "text/csv")},
    )
    assert imported.status_code == 200
    assert imported.json()["imported_count"] == 1

    after = client.post(
        "/api/reconciliations", json={"analysis_at": "2025-01-25T12:00:00Z"}
    )
    assert after.status_code == 200
    after_cases = client.get("/api/cases").json()
    after_case = next(item for item in after_cases if item["id"] == case_id)
    assert after_case["alert_types"] == ["manual_decision_challenged"]
    assert after_case["amounts"] == []
    assert after_case["handling_status"] == "new"
    after_summary = client.get("/api/summary").json()
    after_pln = next(item for item in after_summary["by_currency"] if item["currency"] == "PLN")
    assert after_pln["amount_differences"]["total"] == "0.00"
    assert after_summary["active_alerts"] == 1

    after_detail = client.get(f"/api/cases/{case_id}").json()
    assert after_detail["calculation"]["equation"] == "150.00 − 150.00 = 0.00 PLN"
    assert {
        item["transaction_id"] for item in after_detail["calculation"]["completed_payments"]
    } == {"P-A101-100", "P-A101-50"}
    historical = next(
        finding for finding in after_detail["findings"]
        if finding["alert_type"] == "underpayment_overdue"
    )
    assert historical["id"] == underpayment_id
    assert historical["active"] is False
    assert historical["handling_status"] == "resolved"
    assert any(item["action"] == "status_changed" and item["comment"] == decision_comment for item in after_detail["history"])
    assert any(item["action"] == "cleared_by_analysis" for item in after_detail["history"])

    again_before = client.post(
        "/api/reconciliations", json={"analysis_at": "2025-01-15T12:00:00Z"}
    )
    assert again_before.status_code == 200
    final_queue_case = next(item for item in client.get("/api/cases").json() if item["id"] == case_id)
    assert final_queue_case["alert_types"] == ["underpayment_overdue"]
    assert final_queue_case["handling_status"] == "in_progress"
    assert final_queue_case["amounts"] == [{"currency": "PLN", "amount": "50.00"}]

    final_summary = client.get("/api/summary").json()
    final_pln = next(item for item in final_summary["by_currency"] if item["currency"] == "PLN")
    assert final_pln["amount_differences"]["total"] == "50.00"
    assert final_summary["active_alerts"] == 1
    final_detail = client.get(f"/api/cases/{case_id}").json()
    assert final_detail["calculation"]["equation"] == "150.00 − 100.00 = 50.00 PLN"
    assert [item["transaction_id"] for item in final_detail["calculation"]["completed_payments"]] == [
        "P-A101-100"
    ]
    assert any(item["action"] == "reopened_by_analysis" for item in final_detail["history"])
    assert any(item["action"] == "status_changed" and item["comment"] == decision_comment for item in final_detail["history"])
    assert session.scalar(select(func.count()).select_from(Alert)) == 2


def test_new_data_can_challenge_a_preserved_manual_decision(client, session):
    import_demo(client)
    run_demo(client)
    payment = session.scalar(select(Payment).where(Payment.transaction_id == "P-AMB"))
    order = session.scalar(select(Order).where(Order.external_id == "A-106"))
    linked = client.post(
        f"/api/payments/{payment.id}/link",
        json={"order_id": order.id, "comment": "Ręczne wskazanie na podstawie danych klienta"},
    )
    assert linked.status_code == 200
    new_payment = (
        "source_system,transaction_id,kind,status,transaction_date,amount,currency,title,"
        "order_ref,order_source_system,return_ref,original_payment_ref,processing_scope\n"
        "shop-a,P-NEW,payment,completed,2025-02-12,120.00,PLN,New source data,"
        "A-106,shop-a,,,direct\n"
    ).encode()
    imported = client.post(
        "/api/imports/payments",
        files={"file": ("new-payment.csv", new_payment, "text/csv")},
    )
    assert imported.status_code == 200
    run_demo(client)
    alerts = client.get("/api/alerts").json()
    challenged = next(item for item in alerts if item["alert_type"] == "manual_decision_challenged")
    assert challenged["entity_id"] == payment.id
    assignment = session.scalar(
        select(PaymentAssignment).where(PaymentAssignment.payment_id == payment.id)
    )
    assert assignment.origin == "manual"
    assert assignment.order_id == order.id


def test_resolving_alert_does_not_change_financial_data(client, session):
    import_demo(client)
    run_demo(client)
    alert = next(
        item for item in client.get("/api/alerts").json() if item["alert_type"] == "underpayment_overdue"
    )
    order = session.get(Order, alert["order_id"])
    before = order.gross_amount
    response = client.post(
        f"/api/alerts/{alert['id']}/status",
        json={
            "status": "resolved",
            "comment": "Klient dopłacił poza zakresem danych demonstracyjnych.",
            "confirmed_effect_amount": "50.00",
        },
    )
    assert response.status_code == 200
    session.refresh(order)
    assert order.gross_amount == before == Decimal("150.00")
    detail = client.get(f"/api/alerts/{alert['id']}").json()
    assert detail["history"][-1]["action"] == "status_changed"


def test_summary_separates_amount_meanings_and_currencies(client):
    import_demo(client)
    run_demo(client)
    summary = client.get("/api/summary").json()
    currencies = {row["currency"]: row for row in summary["by_currency"]}
    assert set(currencies) == {"EUR", "PLN"}
    assert currencies["EUR"]["controlled_order_value"] == "100.00"
    assert currencies["PLN"]["controlled_order_value"] == "1445.00"

    # The former 2,419 PLN total mixed three meanings. They now reconcile explicitly.
    assert currencies["PLN"]["amount_differences"]["total"] == "165.00"
    assert currencies["PLN"]["requires_explanation"]["total"] == "1304.00"
    assert currencies["PLN"]["separate_process"]["total"] == "950.00"
    assert sum(
        Decimal(currencies["PLN"][key]["total"])
        for key in ("amount_differences", "requires_explanation", "separate_process")
    ) == Decimal("2419.00")

    # A-108 has two findings, but one 100 EUR amount requiring explanation.
    assert currencies["EUR"]["amount_differences"]["total"] == "0.00"
    assert currencies["EUR"]["requires_explanation"]["total"] == "100.00"
    assert len(currencies["EUR"]["requires_explanation"]["items"]) == 1
    assert currencies["EUR"]["requires_explanation"]["items"][0]["case_id"].startswith("order-")

    expected_pln_differences = {
        "underpayment_overdue": Decimal("50.00"),
        "partial_refund": Decimal("20.00"),
        "missing_payment_overdue": Decimal("70.00"),
        "refund_exceeds_expected": Decimal("5.00"),
        "refund_overdue": Decimal("15.00"),
        "overpayment": Decimal("5.00"),
    }
    difference_items = currencies["PLN"]["amount_differences"]["items"]
    assert {item["rule"]: Decimal(item["amount"]) for item in difference_items} == expected_pln_differences
    assert sum(Decimal(item["amount"]) for item in difference_items) == Decimal("165.00")

    for row in currencies.values():
        for category in ("amount_differences", "requires_explanation", "separate_process"):
            items = row[category]["items"]
            assert sum(Decimal(item["amount"]) for item in items) == Decimal(row[category]["total"])
            assert len({item["basis"] for item in items}) == len(items)


def test_operational_case_groups_currency_findings(client):
    import_demo(client)
    run_demo(client)
    cases = client.get("/api/cases").json()
    a108 = next(
        case
        for case in cases
        if any(ref["external_id"] == "A-108" for ref in case["source_records"])
    )
    assert a108["finding_count"] == 2
    assert set(a108["alert_types"]) == {"currency_conflict", "missing_payment_overdue"}
    assert a108["title"] == "Płatność w innej walucie — rozliczenie niepotwierdzone"
    assert "Znaleziono powiązaną płatność" in a108["description"]
    detail = client.get(f"/api/cases/{a108['id']}").json()
    assert detail["calculation"]["completed_payment_total"] == "0.00"
    assert detail["calculation"]["other_currency_payments"][0]["currency"] == "PLN"


def test_case_counters_refresh_without_changing_detected_amount(client):
    import_demo(client)
    run_demo(client)
    before = client.get("/api/summary").json()
    cases = client.get("/api/cases").json()
    assert before["detected_cases"] == 13
    assert before["active_alerts"] == 13
    underpayment = next(case for case in cases if "underpayment_overdue" in case["alert_types"])

    response = client.post(
        f"/api/cases/{underpayment['id']}/status",
        json={
            "status": "resolved",
            "comment": "Zamknięto obsługę bez potwierdzonego efektu finansowego.",
        },
    )
    assert response.status_code == 200
    assert response.json()["handling_status"] == "resolved"

    after = client.get("/api/summary").json()
    assert after["active_alerts"] == 12
    assert after["detected_cases"] == 13
    assert after["by_currency"][1]["amount_differences"]["total"] == "165.00"
    assert after["by_currency"][1]["confirmed_user_effect"] == "0.00"
    assert len(client.get("/api/cases?status=open").json()) == 12
    assert len(client.get("/api/cases?status=resolved").json()) == 1

    run_demo(client)
    rerun = client.get("/api/summary").json()
    assert rerun["active_alerts"] == 12
    assert rerun["by_currency"][1]["amount_differences"]["total"] == "165.00"
    persisted = client.get(f"/api/cases/{underpayment['id']}").json()
    assert persisted["handling_status"] == "resolved"
    assert any(item["action"] == "status_changed" for item in persisted["history"])


def test_repeated_identical_status_request_does_not_duplicate_history(client, session):
    import_demo(client)
    run_demo(client)
    case = next(
        item for item in client.get("/api/cases").json()
        if "underpayment_overdue" in item["alert_types"]
    )
    payload = {
        "status": "resolved",
        "comment": "Identyczne ponowienie transportowe.",
    }
    first = client.post(f"/api/cases/{case['id']}/status", json=payload)
    second = client.post(f"/api/cases/{case['id']}/status", json=payload)
    assert first.status_code == 200
    assert second.status_code == 200
    alert_ids = [finding["id"] for finding in case["findings"]]
    matching_history = session.scalars(
        select(AlertHistory).where(
            AlertHistory.alert_id.in_(alert_ids),
            AlertHistory.comment == payload["comment"],
        )
    ).all()
    assert len(matching_history) == len(alert_ids)


def test_underpayment_case_has_auditable_calculation_and_sources(client):
    import_demo(client)
    run_demo(client)
    underpayment = next(
        case for case in client.get("/api/cases").json()
        if "underpayment_overdue" in case["alert_types"]
    )
    detail = client.get(f"/api/cases/{underpayment['id']}").json()
    calculation = detail["calculation"]
    assert calculation["order_value"] == "150.00"
    assert calculation["completed_payment_total"] == "100.00"
    assert calculation["missing_amount"] == "50.00"
    assert calculation["currency"] == "PLN"
    assert calculation["due_date"] == "2025-01-10"
    assert calculation["analysis_at"].startswith("2025-02-15T12:00:00")
    assert len(calculation["completed_payments"]) == 1
    assert calculation["completed_payments"][0]["transaction_id"] == "P-101"
    assert calculation["completed_payments"][0]["status"] == "completed"
    assert calculation["completed_payments"][0]["amount"] == "100.00"
    assert calculation["completed_payments"][0]["source_file"] == "payments.csv"
    assert calculation["completed_payments"][0]["source_row"] == 3
    assert "order_ref" in calculation["completed_payments"][0]["matching_explanation"]
    records = {(record["record_type"], record.get("external_id") or record.get("transaction_id")): record for record in detail["records"]}
    assert records[("order", "A-101")]["gross_amount"] == "150.00"
    assert records[("order", "A-101")]["due_date"] == "2025-01-10"
    assert records[("payment", "P-101")]["amount"] == "100.00"
    assert records[("document", "D-101")]["amount"] == "150.00"


def test_summary_and_queue_share_canonical_priority_order(client):
    import_demo(client)
    run_demo(client)
    cases = client.get("/api/cases?status=open&sort=priority").json()
    rank = {"high": 3, "medium": 2, "low": 1}
    expected = sorted(
        cases,
        key=lambda item: (
            -rank[item["priority"]],
            item["deadline"] or "9999-12-31",
            item["id"],
        ),
    )
    assert [item["id"] for item in cases] == [item["id"] for item in expected]
    reversed_request = client.get("/api/cases?status=open&sort=priority&direction=desc").json()
    assert [item["id"] for item in reversed_request] == [item["id"] for item in expected]
    summary = client.get("/api/summary").json()
    assert [item["id"] for item in summary["priority_cases"]] == [
        item["id"] for item in expected[:5]
    ]


def test_amount_sort_requires_single_currency(client):
    import_demo(client)
    run_demo(client)
    assert client.get("/api/cases?sort=amount").status_code == 422
    response = client.get("/api/cases?sort=amount&currency=PLN&direction=desc")
    assert response.status_code == 200
    amounts = [
        Decimal(next(item["amount"] for item in case["amounts"] if item["currency"] == "PLN"))
        for case in response.json()
    ]
    assert amounts == sorted(amounts, reverse=True)


def test_case_export_matches_filters_and_has_one_row_per_operational_case(client):
    import_demo(client)
    run_demo(client)
    underpayment = next(
        case for case in client.get("/api/cases").json()
        if "underpayment_overdue" in case["alert_types"]
    )
    client.post(
        f"/api/cases/{underpayment['id']}/status",
        json={"status": "resolved", "comment": "Kontrola eksportu przypadków."},
    )

    response = client.get("/api/exports/cases.csv?status=resolved")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert 'filename="reconflow-cases.csv"' in response.headers["content-disposition"]
    rows = list(csv.DictReader(io.StringIO(response.text)))
    assert len(rows) == 1
    row = rows[0]
    assert row["row_type"] == "operational_case"
    assert row["case_id"] == underpayment["id"]
    assert row["handling_status"] == "resolved"
    assert row["amounts"] == "PLN:50.00"
    assert row["currencies"] == "PLN"
    assert row["finding_count"] == "1"
    assert row["finding_types"] == "underpayment_overdue"
    assert "Niedopłata" in row["title"]

    open_response = client.get("/api/exports/cases.csv?status=open")
    open_rows = list(csv.DictReader(io.StringIO(open_response.text)))
    assert len(open_rows) == 12
    assert underpayment["id"] not in {item["case_id"] for item in open_rows}
    assert len({item["case_id"] for item in open_rows}) == len(open_rows)

    duplicate_response = client.get(
        "/api/exports/cases.csv?status=open&alert_type=suspected_duplicate_payment"
    )
    duplicate_rows = list(csv.DictReader(io.StringIO(duplicate_response.text)))
    assert len(duplicate_rows) == 1
    assert duplicate_rows[0]["finding_types"] == "suspected_duplicate_payment"
    assert duplicate_rows[0]["deadline"] == ""

import csv
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import io
import json
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from .csv_schemas import CSV_HEADERS, DATASET_SCHEMAS
from .models import Document, ImportBatch, Order, Payment, RejectedRow, ReturnRecord


MODEL_BY_DATASET = {
    "orders": Order,
    "payments": Payment,
    "documents": Document,
    "returns": ReturnRecord,
}

KEY_FIELD = {
    "orders": "external_id",
    "payments": "transaction_id",
    "documents": "external_id",
    "returns": "external_id",
}

INPUT_KEY_FIELD = {
    "orders": "order_id",
    "payments": "transaction_id",
    "documents": "document_id",
    "returns": "return_id",
}


def _canonical_hash(data: dict[str, Any]) -> str:
    canonical = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _normalise_row(raw: dict[str, str | None]) -> dict[str, Any]:
    return {key: (value.strip() if isinstance(value, str) else value) for key, value in raw.items()}


def _model_values(dataset: str, validated: dict[str, Any]) -> dict[str, Any]:
    values = dict(validated)
    if dataset == "orders":
        values["external_id"] = values.pop("order_id")
    elif dataset == "documents":
        values["external_id"] = values.pop("document_id")
    elif dataset == "returns":
        values["external_id"] = values.pop("return_id")
    return values


def _validation_reason(exc: ValidationError) -> str:
    pieces = []
    for error in exc.errors(include_url=False):
        location = ".".join(str(part) for part in error["loc"])
        pieces.append(f"{location}: {error['msg']}")
    return "; ".join(pieces)


def import_csv(db: Session, dataset: str, file_name: str, content: bytes) -> ImportBatch:
    if dataset not in DATASET_SCHEMAS:
        raise ValueError(f"Unsupported dataset: {dataset}")

    batch = ImportBatch(
        dataset=dataset,
        file_name=file_name,
        file_sha256=hashlib.sha256(content).hexdigest(),
        status="processing",
    )
    db.add(batch)
    db.flush()

    try:
        decoded = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        batch.status = "failed"
        batch.rejected_count = 1
        batch.completed_at = datetime.now(timezone.utc)
        db.add(
            RejectedRow(
                batch_id=batch.id,
                file_name=file_name,
                row_number=1,
                reason=f"File must be UTF-8: {exc}",
                raw_data={},
            )
        )
        db.commit()
        return batch

    reader = csv.DictReader(io.StringIO(decoded, newline=""))
    actual_headers = reader.fieldnames or []
    expected_headers = CSV_HEADERS[dataset]
    if actual_headers != expected_headers:
        missing = [header for header in expected_headers if header not in actual_headers]
        extra = [header for header in actual_headers if header not in expected_headers]
        reason = (
            "Invalid CSV headers or order. "
            f"Expected: {','.join(expected_headers)}. "
            f"Missing: {','.join(missing) or '-'}. Extra: {','.join(extra) or '-'}"
        )
        batch.status = "failed"
        batch.rejected_count = 1
        batch.completed_at = datetime.now(timezone.utc)
        db.add(
            RejectedRow(
                batch_id=batch.id,
                file_name=file_name,
                row_number=1,
                reason=reason,
                raw_data={"headers": actual_headers},
            )
        )
        db.commit()
        return batch

    schema = DATASET_SCHEMAS[dataset]
    model_class = MODEL_BY_DATASET[dataset]
    input_key = INPUT_KEY_FIELD[dataset]
    database_key = KEY_FIELD[dataset]

    for row_number, source_row in enumerate(reader, start=2):
        raw = _normalise_row(source_row)
        try:
            validated_model = schema.model_validate(raw)
        except ValidationError as exc:
            batch.rejected_count += 1
            db.add(
                RejectedRow(
                    batch_id=batch.id,
                    file_name=file_name,
                    row_number=row_number,
                    reason=_validation_reason(exc),
                    raw_data=raw,
                )
            )
            continue

        canonical = validated_model.model_dump(mode="json")
        record_hash = _canonical_hash(canonical)
        source_system = canonical["source_system"]
        external_key = canonical[input_key]
        existing = db.scalar(
            select(model_class).where(
                model_class.source_system == source_system,
                getattr(model_class, database_key) == external_key,
            )
        )
        if existing:
            if existing.record_hash == record_hash:
                batch.skipped_count += 1
            else:
                batch.conflict_count += 1
                batch.rejected_count += 1
                db.add(
                    RejectedRow(
                        batch_id=batch.id,
                        file_name=file_name,
                        row_number=row_number,
                        reason=(
                            "source_identifier_conflict: an existing record has the same "
                            f"key ({source_system}, {external_key}) but different content"
                        ),
                        raw_data=raw,
                        is_conflict=True,
                    )
                )
            continue

        values = _model_values(dataset, validated_model.model_dump())
        values.update(
            import_batch_id=batch.id,
            source_file=file_name,
            source_row=row_number,
            record_hash=record_hash,
        )
        db.add(model_class(**values))
        batch.imported_count += 1

    batch.status = "completed_with_errors" if batch.rejected_count else "completed"
    batch.completed_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(batch)
    return batch


def batch_dict(batch: ImportBatch) -> dict[str, Any]:
    return {
        "id": batch.id,
        "dataset": batch.dataset,
        "file_name": batch.file_name,
        "file_sha256": batch.file_sha256,
        "status": batch.status,
        "imported_count": batch.imported_count,
        "skipped_count": batch.skipped_count,
        "conflict_count": batch.conflict_count,
        "rejected_count": batch.rejected_count,
        "created_at": batch.created_at,
        "completed_at": batch.completed_at,
    }

from datetime import date
from decimal import Decimal
import re

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,119}$")
CURRENCY_RE = re.compile(r"^[A-Z]{3}$")


class CsvBase(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    source_system: str = Field(min_length=1, max_length=80)

    @field_validator("source_system")
    @classmethod
    def valid_source(cls, value: str) -> str:
        if not IDENTIFIER_RE.fullmatch(value):
            raise ValueError("must be a stable identifier using letters, numbers, . _ : / or -")
        return value

    @staticmethod
    def validate_identifier(value: str | None) -> str | None:
        if value in (None, ""):
            return None
        if not IDENTIFIER_RE.fullmatch(value):
            raise ValueError("invalid identifier")
        return value

    @staticmethod
    def validate_currency(value: str) -> str:
        normalized = value.upper()
        if not CURRENCY_RE.fullmatch(normalized):
            raise ValueError("currency must be a three-letter ISO-style code")
        return normalized


class OrderCsv(CsvBase):
    order_id: str
    sales_channel: str = Field(min_length=1, max_length=80)
    order_date: date
    due_date: date
    status: str
    gross_amount: Decimal = Field(ge=0, decimal_places=2, max_digits=18)
    currency: str
    customer_name: str | None = None
    customer_email: str | None = Field(default=None, max_length=320)

    _order_id = field_validator("order_id")(CsvBase.validate_identifier)
    _currency = field_validator("currency")(CsvBase.validate_currency)

    @field_validator("customer_email")
    @classmethod
    def valid_email(cls, value: str | None) -> str | None:
        if value and ("@" not in value or value.startswith("@") or value.endswith("@")):
            raise ValueError("invalid customer_email")
        return value

    @field_validator("status")
    @classmethod
    def valid_status(cls, value: str) -> str:
        if value not in {"new", "confirmed", "completed", "cancelled"}:
            raise ValueError("status must be new, confirmed, completed or cancelled")
        return value

    @model_validator(mode="after")
    def dates_in_order(self):
        if self.due_date < self.order_date:
            raise ValueError("due_date cannot be before order_date")
        return self


class PaymentCsv(CsvBase):
    transaction_id: str
    kind: str
    status: str
    transaction_date: date
    amount: Decimal = Field(gt=0, decimal_places=2, max_digits=18)
    currency: str
    title: str = Field(min_length=1, max_length=500)
    order_ref: str | None = None
    order_source_system: str | None = None
    return_ref: str | None = None
    original_payment_ref: str | None = None
    processing_scope: str = "direct"

    _transaction_id = field_validator("transaction_id")(CsvBase.validate_identifier)
    _order_ref = field_validator("order_ref")(CsvBase.validate_identifier)
    _return_ref = field_validator("return_ref")(CsvBase.validate_identifier)
    _original_payment_ref = field_validator("original_payment_ref")(CsvBase.validate_identifier)
    _currency = field_validator("currency")(CsvBase.validate_currency)

    @field_validator("kind")
    @classmethod
    def valid_kind(cls, value: str) -> str:
        if value not in {"payment", "refund"}:
            raise ValueError("kind must be payment or refund")
        return value

    @field_validator("status")
    @classmethod
    def valid_status(cls, value: str) -> str:
        if value not in {"pending", "completed", "failed"}:
            raise ValueError("status must be pending, completed or failed")
        return value

    @field_validator("processing_scope")
    @classmethod
    def valid_scope(cls, value: str) -> str:
        if value not in {"direct", "aggregate", "marketplace_payout"}:
            raise ValueError("processing_scope must be direct, aggregate or marketplace_payout")
        return value


class DocumentCsv(CsvBase):
    document_id: str
    number: str = Field(min_length=1, max_length=120)
    document_type: str
    status: str
    document_date: date
    amount: Decimal = Field(decimal_places=2, max_digits=18)
    currency: str
    order_ref: str
    order_source_system: str | None = None
    original_document_ref: str | None = None

    _document_id = field_validator("document_id")(CsvBase.validate_identifier)
    _order_ref = field_validator("order_ref")(CsvBase.validate_identifier)
    _original_document_ref = field_validator("original_document_ref")(CsvBase.validate_identifier)
    _currency = field_validator("currency")(CsvBase.validate_currency)

    @field_validator("document_type")
    @classmethod
    def valid_type(cls, value: str) -> str:
        if value not in {"invoice", "receipt", "correction"}:
            raise ValueError("document_type must be invoice, receipt or correction")
        return value

    @field_validator("status")
    @classmethod
    def valid_status(cls, value: str) -> str:
        if value not in {"draft", "issued", "cancelled"}:
            raise ValueError("status must be draft, issued or cancelled")
        return value

    @model_validator(mode="after")
    def correction_rules(self):
        if self.document_type == "correction" and not self.original_document_ref:
            raise ValueError("correction requires original_document_ref")
        if self.document_type != "correction" and self.amount < 0:
            raise ValueError("only correction documents may use a negative signed amount")
        return self


class ReturnCsv(CsvBase):
    return_id: str
    order_ref: str
    order_source_system: str | None = None
    return_date: date
    status: str
    expected_refund_amount: Decimal = Field(ge=0, decimal_places=2, max_digits=18)
    currency: str
    refund_due_date: date
    correction_ref: str | None = None

    _return_id = field_validator("return_id")(CsvBase.validate_identifier)
    _order_ref = field_validator("order_ref")(CsvBase.validate_identifier)
    _correction_ref = field_validator("correction_ref")(CsvBase.validate_identifier)
    _currency = field_validator("currency")(CsvBase.validate_currency)

    @field_validator("status")
    @classmethod
    def valid_status(cls, value: str) -> str:
        if value not in {"requested", "approved", "received", "cancelled"}:
            raise ValueError("status must be requested, approved, received or cancelled")
        return value

    @model_validator(mode="after")
    def dates_in_order(self):
        if self.refund_due_date < self.return_date:
            raise ValueError("refund_due_date cannot be before return_date")
        return self


DATASET_SCHEMAS = {
    "orders": OrderCsv,
    "payments": PaymentCsv,
    "documents": DocumentCsv,
    "returns": ReturnCsv,
}

CSV_HEADERS = {
    name: list(schema.model_fields.keys()) for name, schema in DATASET_SCHEMAS.items()
}

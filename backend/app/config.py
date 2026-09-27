from dataclasses import dataclass
from decimal import Decimal
import os


@dataclass(frozen=True)
class Settings:
    database_url: str
    amount_tolerance: Decimal
    document_grace_days: int
    required_document_types: tuple[str, ...]
    demo_mode: bool
    test_instance: bool


def get_settings() -> Settings:
    return Settings(
        database_url=os.getenv(
            "DATABASE_URL",
            "postgresql+psycopg://reconflow:reconflow@localhost:5432/reconflow",
        ),
        amount_tolerance=Decimal(os.getenv("AMOUNT_TOLERANCE", "0.01")),
        document_grace_days=int(os.getenv("DOCUMENT_GRACE_DAYS", "3")),
        required_document_types=tuple(
            part.strip()
            for part in os.getenv("REQUIRED_DOCUMENT_TYPES", "invoice,receipt").split(",")
            if part.strip()
        ),
        demo_mode=os.getenv("DEMO_MODE", "true").strip().lower() in {"1", "true", "yes", "on"},
        test_instance=os.getenv("RECONFLOW_TEST_INSTANCE", "false").strip().lower()
        in {"1", "true", "yes", "on"},
    )

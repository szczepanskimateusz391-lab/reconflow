from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, model_validator


class ReconciliationRequest(BaseModel):
    analysis_at: datetime | None = None


class AlertStatusRequest(BaseModel):
    status: Literal["new", "in_progress", "resolved", "ignored"]
    comment: str = Field(min_length=1, max_length=2000)
    confirmed_effect_amount: Decimal | None = Field(default=None, ge=0, decimal_places=2)

    @model_validator(mode="after")
    def effect_only_for_resolution(self):
        if self.confirmed_effect_amount is not None and self.status != "resolved":
            raise ValueError("confirmed_effect_amount is allowed only for resolved alerts")
        return self


class ManualLinkRequest(BaseModel):
    order_id: int = Field(gt=0)
    comment: str = Field(min_length=1, max_length=2000)

class CandidateDecisionRequest(BaseModel):
    decision: Literal["approve", "reject"]
    comment: str = Field(min_length=1, max_length=2000)

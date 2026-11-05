import math
from typing import Literal, Optional, get_args

from pydantic import BaseModel, Field, field_validator, model_validator


AttackType = Literal["Normal", "SQLi", "XSS"]
CANONICAL_ATTACK_TYPES = get_args(AttackType)


class PayloadRequest(BaseModel):
    payload: str


class AnalysisResponse(BaseModel):
    source: Literal["Local_ML"]
    classification_status: Literal["CLASSIFIED", "NEEDS_REVIEW"]
    attack_type: Optional[AttackType]
    confidence: Optional[float] = Field(
        ge=0,
        le=1,
        allow_inf_nan=False,
    )
    risk_score: Optional[int] = Field(
        ge=1,
        le=10,
        strict=True,
    )
    reason: Optional[
        Literal["LOW_CONFIDENCE", "MODEL_UNAVAILABLE", "INFERENCE_ERROR"]
    ]
    recommendation: Optional[str]

    @field_validator("confidence", mode="before")
    @classmethod
    def validate_confidence(cls, value):
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("confidence must be a finite number or null")
        try:
            confidence = float(value)
        except (OverflowError, ValueError) as exc:
            raise ValueError("confidence must be a finite number or null") from exc
        if not math.isfinite(confidence) or not 0 <= confidence <= 1:
            raise ValueError("confidence must be between 0 and 1")
        return confidence

    @field_validator("risk_score", mode="before")
    @classmethod
    def validate_risk_score(cls, value):
        if value is None:
            return None
        if type(value) is not int or not 1 <= value <= 10:
            raise ValueError("risk_score must be an integer between 1 and 10")
        return value

    @model_validator(mode="after")
    def validate_outcome(self):
        if self.classification_status == "CLASSIFIED":
            if (
                self.attack_type is None
                or self.confidence is None
                or self.confidence < 0.85
                or self.risk_score is None
                or self.reason is not None
            ):
                raise ValueError("CLASSIFIED outcome fields are inconsistent")
            return self

        if (
            self.attack_type is not None
            or self.risk_score is not None
            or self.reason is None
            or self.recommendation is not None
        ):
            raise ValueError("NEEDS_REVIEW outcome fields are inconsistent")
        if self.reason == "LOW_CONFIDENCE":
            if self.confidence is None or self.confidence >= 0.85:
                raise ValueError("LOW_CONFIDENCE requires confidence below 0.85")
        elif self.confidence is not None:
            raise ValueError(
                "MODEL_UNAVAILABLE and INFERENCE_ERROR require null confidence"
            )
        return self

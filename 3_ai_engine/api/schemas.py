from pydantic import BaseModel
from typing import Optional

class PayloadRequest(BaseModel):
    payload: str

class AnalysisResponse(BaseModel):
    source: str
    attack_type: str
    confidence: float
    risk_score: int
    recommendation: Optional[str] = None

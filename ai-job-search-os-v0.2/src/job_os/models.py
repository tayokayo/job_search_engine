from datetime import datetime
from typing import Literal, Optional
from pydantic import BaseModel, Field

class Job(BaseModel):
    source: str
    source_id: str
    source_email_message_id: Optional[str] = None
    source_url: Optional[str] = None
    canonical_job_url: Optional[str] = None
    official_company_job_url: Optional[str] = None
    title: str
    company: str
    location: str
    description: str = ""
    discovered_at: datetime = Field(default_factory=datetime.utcnow)
    last_verified_at: Optional[datetime] = None
    processing_state: Literal["new","parsed","enriched","scored","digested","archived","failed"] = "new"

class OpportunityEvaluation(BaseModel):
    job_source_id: str
    overall_score: int = Field(ge=0,le=100)
    bucket: Literal["A","B","C"]
    hard_constraints_triggered: list[str] = []
    strongest_verified_matches: list[str]
    inferred_matches: list[str] = []
    concerns: list[str]
    missing_requirements: list[str]
    strategic_exception: Optional[str] = None
    recommended_resume: Literal["executive_base","product_commerce","product_operations"]
    recommended_action: str
    rationale: str

class ApplicationStrategyPack(BaseModel):
    job_source_id: str
    recommended_resume: str
    tailoring_recommendations: list[str]
    strongest_verified_evidence: list[str]
    gaps_and_risks: list[str]
    company_intelligence: list[dict]
    positioning_angle: str
    hiring_manager_name: Optional[str] = None
    hiring_manager_confidence: Optional[str] = None
    draft_message: str

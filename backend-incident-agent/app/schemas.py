from typing import Optional
from pydantic import BaseModel, Field

class IncidentAlert(BaseModel):
    service: str = Field(description="The name of the backend service failing")
    severity: str = Field(description="Initial severity level (e.g., LOW, HIGH, CRITICAL)")
    alert_name: str = Field(description="Name or title of the alert")
    error_log: str = Field(description="Raw log content attached to the alert")

class IncidentAnalysisSchema(BaseModel):
    root_cause: str = Field(description="Detailed SRE analysis of what caused the failure")
    mitigation_plan: str = Field(description="Proposed mitigation action or plan")
    risk_category: str = Field(
        description="Categorization of risk: 'LOW_RISK' for safe/minor actions (e.g., clearing safe caches), 'HIGH_RISK' for service restarts, migrations, state changes, etc."
    )
    runbook_url: Optional[str] = Field(
        default=None,
        description="URL of the runbook or post-mortem found in search matching this incident type"
    )

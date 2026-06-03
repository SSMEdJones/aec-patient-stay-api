"""Data models for clinical notes and patient information."""
from pydantic import BaseModel, Field
from typing import Optional, List
from datetime import datetime
from enum import Enum


class NoteType(str, Enum):
    """Types of clinical notes from DocumentReference."""
    PROGRESS_NOTE = "Progress Note"
    DISCHARGE_SUMMARY = "Discharge Summary"
    H_AND_P = "History and Physical"
    CONSULTATION = "Consultation"
    OPERATIVE_NOTE = "Operative Note"
    PROCEDURE_NOTE = "Procedure Note"
    NURSING_NOTE = "Nursing Note"
    RADIOLOGY_REPORT = "Radiology Report"
    LAB_REPORT = "Lab Report"
    OTHER = "Other"


class DocumentStatus(str, Enum):
    """FHIR DocumentReference status codes."""
    CURRENT = "current"
    SUPERSEDED = "superseded"
    ENTERED_IN_ERROR = "entered-in-error"


class Author(BaseModel):
    """Author of a clinical document."""
    id: Optional[str] = None
    name: Optional[str] = None
    role: Optional[str] = None  # e.g., "Physician", "Nurse"


class ClinicalNote(BaseModel):
    """Represents a clinical note from Epic's DocumentReference."""
    
    id: str
    status: DocumentStatus = DocumentStatus.CURRENT
    type: NoteType = NoteType.OTHER
    type_code: Optional[str] = None  # LOINC code
    
    # Content
    title: Optional[str] = None
    content: Optional[str] = None  # The actual note text
    content_type: str = "text/plain"
    
    # Metadata
    date: Optional[datetime] = None
    author: Optional[Author] = None
    
    # Patient context
    patient_id: Optional[str] = None
    encounter_id: Optional[str] = None
    
    # Epic-specific
    document_reference_id: Optional[str] = None


class PatientInfo(BaseModel):
    """Basic patient information from FHIR Patient resource."""
    
    id: str
    mrn: Optional[str] = None  # Medical Record Number
    
    # Name
    given_name: Optional[str] = None
    family_name: Optional[str] = None
    
    @property
    def full_name(self) -> str:
        parts = [self.given_name, self.family_name]
        return " ".join(p for p in parts if p)
    
    # Demographics
    birth_date: Optional[datetime] = None
    gender: Optional[str] = None
    
    # Contact
    phone: Optional[str] = None
    address: Optional[str] = None


class Encounter(BaseModel):
    """Patient encounter/visit information."""
    
    id: str
    status: str  # planned, in-progress, finished, etc.
    encounter_class: Optional[str] = None  # inpatient, outpatient, emergency
    
    # Timing
    start_date: Optional[datetime] = None
    end_date: Optional[datetime] = None
    
    # Location
    location: Optional[str] = None
    department: Optional[str] = None
    
    # Reason
    reason_for_visit: Optional[str] = None
    chief_complaint: Optional[str] = None
    
    # Diagnosis
    diagnoses: List[str] = Field(default_factory=list)


class PatientStaySummary(BaseModel):
    """Generated summary of a patient's hospital stay."""
    
    patient: PatientInfo
    encounter: Encounter
    
    # Aggregated from clinical notes
    clinical_notes: List[ClinicalNote] = Field(default_factory=list)
    
    # LLM-generated summary
    summary: Optional[str] = None
    key_findings: List[str] = Field(default_factory=list)
    diagnoses: List[str] = Field(default_factory=list)
    treatments: List[str] = Field(default_factory=list)
    follow_up_recommendations: List[str] = Field(default_factory=list)
    
    # Metadata
    generated_at: datetime = Field(default_factory=datetime.now)
    model_used: Optional[str] = None

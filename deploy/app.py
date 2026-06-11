"""
FastAPI web service for Patient Stay Summary API.

Run with: uvicorn app:app --reload --port 8001
"""
from fastapi import FastAPI, HTTPException, Query, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from typing import Optional, List, Dict
from datetime import datetime
from enum import Enum
from pathlib import Path
import httpx
import secrets
import urllib.parse
import hashlib
import base64
import tempfile
import shutil
import uuid
import os
import json

from config import (
    DEBUG_MODE, EPIC_CLIENT_ID, EPIC_AUTHORIZE_URL, EPIC_TOKEN_URL,
    EPIC_REDIRECT_URI, EPIC_SCOPE, EPIC_BASE_URL
)
from models import PatientStaySummary, ClinicalNote, NoteType

# Use mock client for now (switch to EpicFHIRClient when credentials ready)
from api.mock_client import MockEpicFHIRClient
from llm import DocumentGenerator

app = FastAPI(
    title="AEC Patient Stay Summary API",
    description="Generate clinical summaries from Epic FHIR data using LLMs",
    version="1.0.0",
)

# CORS for web clients
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount static files for frontend UI
static_path = Path(__file__).parent / "static"
static_path.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_path)), name="static")

# Temp storage for uploaded PDFs and generated documents (in production, use proper storage)
temp_storage: Dict[str, Dict] = {}

# Initialize clients
epic_client = MockEpicFHIRClient()
llm_generator = DocumentGenerator()

# Simple in-memory OAuth state (use Redis/DB in production)
oauth_state_store: dict = {}
current_token: dict = {}


# ============================================================================
# Request/Response Models
# ============================================================================

class SummaryRequest(BaseModel):
    """Request to generate a patient stay summary."""
    patient_id: str = Field(..., description="FHIR Patient ID")
    encounter_id: Optional[str] = Field(None, description="FHIR Encounter ID (optional)")
    include_notes: bool = Field(True, description="Include clinical notes in response")


class DischargeInstructionsRequest(BaseModel):
    """Request to generate discharge instructions."""
    patient_id: str = Field(..., description="FHIR Patient ID")
    encounter_id: str = Field(..., description="FHIR Encounter ID")
    reading_level: str = Field(
        "patient-friendly",
        description="Reading level: 'patient-friendly', 'detailed', or 'simple'"
    )


class ClinicalNoteResponse(BaseModel):
    """Clinical note in response."""
    id: str
    type: str
    title: Optional[str]
    date: Optional[datetime]
    author: Optional[str]
    content_preview: Optional[str] = Field(None, description="First 500 chars of content")


class PatientSummaryResponse(BaseModel):
    """Response containing generated summary."""
    patient_id: str
    patient_name: str
    mrn: Optional[str]
    encounter_id: Optional[str]
    
    # Generated content
    summary: str
    diagnoses: List[str]
    treatments: List[str]
    follow_up_recommendations: List[str]
    key_findings: List[str]
    
    # Metadata
    model_used: str
    generated_at: datetime
    note_count: int
    
    # Optional: include notes
    clinical_notes: Optional[List[ClinicalNoteResponse]] = None


class DischargeInstructionsResponse(BaseModel):
    """Response containing discharge instructions."""
    patient_id: str
    patient_name: str
    encounter_id: str
    instructions: str
    reading_level: str
    generated_at: datetime


class HealthResponse(BaseModel):
    """Health check response."""
    status: str
    version: str
    mode: str
    llm_provider: str


# ============================================================================
# Appeal Letter Models
# ============================================================================

class AppealDataResponse(BaseModel):
    """Response containing extracted and generated appeal data for editing."""
    session_id: str
    
    # Patient info (editable)
    member_name: str = ""
    dob: str = ""
    age: str = ""
    gender: str = ""
    member_id: str = ""
    medical_history: str = ""
    complaint: str = ""
    
    # Case info (editable)
    observation_date: str = ""
    inpatient_date: str = ""
    place_of_service: str = ""
    reference_number: str = ""
    
    # Payer info (editable)
    payer_name: str = ""
    street_address: str = ""
    city: str = ""
    state: str = ""
    zip_code: str = ""
    
    # Generated content (editable)
    midnight_reason_1: str = ""
    midnight_reason_2: str = ""


class GenerateAppealRequest(BaseModel):
    """Request to generate final appeal document with edited data."""
    session_id: str
    
    # All editable fields
    member_name: str
    dob: str
    age: str
    gender: str
    member_id: str
    medical_history: str
    complaint: str
    
    observation_date: str = ""
    inpatient_date: str = ""
    place_of_service: str
    reference_number: str
    
    payer_name: str = ""
    street_address: str = ""
    city: str = ""
    state: str = ""
    zip_code: str = ""
    
    midnight_reason_1: str
    midnight_reason_2: str


class ChatMessage(BaseModel):
    """Chat message for appeal assistant."""
    session_id: str
    message: str
    conversation_history: Optional[List[Dict]] = None
    context: Optional[Dict] = None


class FeedbackExample(BaseModel):
    """RAG feedback for improving future generations."""
    condition_category: str  # e.g., "chest_pain", "gi_bleed", "syncope"
    original_text: str
    improved_text: str
    improvement_reason: str
    created_by: str = "unknown"


# ============================================================================
# Endpoints
# ============================================================================

@app.get("/health", response_model=HealthResponse, tags=["System"])
async def health_check():
    """Check API health and configuration."""
    from config import LLM_PROVIDER, BEDROCK_MODEL_ID
    return HealthResponse(
        status="healthy",
        version="1.0.0",
        mode="mock" if isinstance(epic_client, MockEpicFHIRClient) else "live",
        llm_provider=f"{LLM_PROVIDER} ({BEDROCK_MODEL_ID})"
    )


# ============================================================================
# OAuth2 Patient Flow
# ============================================================================

@app.get("/", response_class=HTMLResponse, tags=["OAuth"])
async def home():
    """Home page with login link."""
    logged_in = bool(current_token.get("access_token"))
    patient_id = current_token.get("patient", "N/A")
    
    if logged_in:
        return f"""
        <html>
        <head><title>Patient Stay API</title></head>
        <body>
            <h1>Patient Stay Summary API</h1>
            <p style="color: green;">✓ Logged in as Patient: {patient_id}</p>
            <p>Access Token: {current_token.get('access_token', '')[:50]}...</p>
            <ul>
                <li><a href="/docs">API Documentation</a></li>
                <li><a href="/health">Health Check</a></li>
                <li><a href="/test/fhir-patient">Test FHIR Patient Fetch</a></li>
            </ul>
            <p><a href="/logout">Logout</a></p>
        </body>
        </html>
        """
    else:
        return """
        <html>
        <head><title>Patient Stay API</title></head>
        <body>
            <h1>Patient Stay Summary API</h1>
            <p>Not logged in. <a href="/login">Login with Epic</a></p>
            <p><small>Use sandbox credentials: fhircamila / epicepic1</small></p>
        </body>
        </html>
        """


@app.get("/login", tags=["OAuth"])
async def login():
    """Redirect to Epic OAuth2 authorization with PKCE."""
    state = secrets.token_urlsafe(32)
    
    # Generate PKCE code verifier and challenge
    code_verifier = secrets.token_urlsafe(64)[:128]  # 43-128 chars
    code_challenge = base64.urlsafe_b64encode(
        hashlib.sha256(code_verifier.encode()).digest()
    ).decode().rstrip("=")
    
    # Store state and verifier for callback
    oauth_state_store[state] = {"code_verifier": code_verifier}
    
    params = {
        "response_type": "code",
        "client_id": EPIC_CLIENT_ID,
        "redirect_uri": EPIC_REDIRECT_URI,
        "scope": EPIC_SCOPE,
        "state": state,
        "aud": EPIC_BASE_URL,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    
    auth_url = f"{EPIC_AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"
    return RedirectResponse(url=auth_url)


@app.get("/callback", tags=["OAuth"])
async def oauth_callback(code: str = None, state: str = None, error: str = None):
    """Handle OAuth2 callback from Epic."""
    if error:
        return HTMLResponse(f"<h1>OAuth Error</h1><p>{error}</p><a href='/'>Back</a>")
    
    if not state or state not in oauth_state_store:
        return HTMLResponse("<h1>Invalid state</h1><p>CSRF check failed.</p><a href='/'>Back</a>")
    
    state_data = oauth_state_store.pop(state)
    code_verifier = state_data.get("code_verifier")
    
    # Exchange code for token with PKCE verifier
    async with httpx.AsyncClient(verify=False) as client:
        response = await client.post(
            EPIC_TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": EPIC_REDIRECT_URI,
                "client_id": EPIC_CLIENT_ID,
                "code_verifier": code_verifier,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"}
        )
    
    if response.status_code != 200:
        return HTMLResponse(f"<h1>Token Error</h1><pre>{response.text}</pre><a href='/'>Back</a>")
    
    token_data = response.json()
    current_token.update(token_data)
    
    return RedirectResponse(url="/")


@app.get("/logout", tags=["OAuth"])
async def logout():
    """Clear current token."""
    current_token.clear()
    return RedirectResponse(url="/")


@app.get("/test/fhir-patient", tags=["OAuth"])
async def test_fhir_patient():
    """Test fetching the current patient from Epic FHIR using OAuth token."""
    if not current_token.get("access_token"):
        raise HTTPException(status_code=401, detail="Not authenticated. Go to /login first.")
    
    patient_id = current_token.get("patient")
    if not patient_id:
        return {"error": "No patient ID in token", "token_keys": list(current_token.keys())}
    
    # Fetch patient from FHIR
    async with httpx.AsyncClient(verify=False) as client:
        response = await client.get(
            f"{EPIC_BASE_URL}/Patient/{patient_id}",
            headers={"Authorization": f"Bearer {current_token['access_token']}"}
        )
    
    if response.status_code != 200:
        return {"error": response.status_code, "detail": response.text}
    
    return response.json()


@app.post("/summary", response_model=PatientSummaryResponse, tags=["Summaries"])
async def generate_summary(request: SummaryRequest):
    """
    Generate a clinical summary for a patient's hospital stay.
    
    Uses Epic FHIR API to fetch clinical notes and LLM to generate summary.
    """
    try:
        # Fetch patient data
        patient = await epic_client.get_patient(request.patient_id)
        
        # Fetch encounter if provided
        if request.encounter_id:
            encounter = await epic_client.get_encounter(request.encounter_id)
        else:
            from models import Encounter
            encounter = Encounter(id="current", status="in-progress")
        
        # Fetch clinical notes
        notes = await epic_client.search_clinical_notes(
            patient_id=request.patient_id,
            encounter_id=request.encounter_id
        )
        
        if not notes:
            raise HTTPException(
                status_code=404,
                detail=f"No clinical notes found for patient {request.patient_id}"
            )
        
        # Generate summary
        summary = llm_generator.generate_stay_summary(
            patient=patient,
            encounter=encounter,
            notes=notes
        )
        
        # Build response
        response = PatientSummaryResponse(
            patient_id=patient.id,
            patient_name=patient.full_name,
            mrn=patient.mrn,
            encounter_id=request.encounter_id,
            summary=summary.summary,
            diagnoses=summary.diagnoses,
            treatments=summary.treatments,
            follow_up_recommendations=summary.follow_up_recommendations,
            key_findings=summary.key_findings,
            model_used=summary.model_used,
            generated_at=summary.generated_at,
            note_count=len(notes),
        )
        
        # Include notes if requested
        if request.include_notes:
            response.clinical_notes = [
                ClinicalNoteResponse(
                    id=note.id,
                    type=note.type.value,
                    title=note.title,
                    date=note.date,
                    author=note.author.name if note.author else None,
                    content_preview=note.content[:500] if note.content else None
                )
                for note in notes
            ]
        
        return response
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/discharge-instructions", response_model=DischargeInstructionsResponse, tags=["Summaries"])
async def generate_discharge_instructions(request: DischargeInstructionsRequest):
    """
    Generate patient-friendly discharge instructions.
    
    First generates a clinical summary, then creates discharge instructions
    at the specified reading level.
    """
    try:
        # Fetch patient data
        patient = await epic_client.get_patient(request.patient_id)
        encounter = await epic_client.get_encounter(request.encounter_id)
        notes = await epic_client.search_clinical_notes(
            patient_id=request.patient_id,
            encounter_id=request.encounter_id
        )
        
        # Generate summary first
        summary = llm_generator.generate_stay_summary(
            patient=patient,
            encounter=encounter,
            notes=notes
        )
        
        # Generate discharge instructions
        instructions = llm_generator.generate_discharge_instructions(
            patient=patient,
            summary=summary,
            reading_level=request.reading_level
        )
        
        return DischargeInstructionsResponse(
            patient_id=patient.id,
            patient_name=patient.full_name,
            encounter_id=request.encounter_id,
            instructions=instructions,
            reading_level=request.reading_level,
            generated_at=datetime.now()
        )
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/patients/{patient_id}", tags=["Patients"])
async def get_patient(patient_id: str):
    """Get patient demographics."""
    try:
        patient = await epic_client.get_patient(patient_id)
        return {
            "id": patient.id,
            "name": patient.full_name,
            "mrn": patient.mrn,
            "birth_date": patient.birth_date,
            "gender": patient.gender,
        }
    except Exception as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.get("/patients/{patient_id}/notes", tags=["Clinical Notes"])
async def get_clinical_notes(
    patient_id: str,
    encounter_id: Optional[str] = Query(None),
    limit: int = Query(20, le=100)
):
    """Get clinical notes for a patient."""
    try:
        notes = await epic_client.search_clinical_notes(
            patient_id=patient_id,
            encounter_id=encounter_id,
            max_results=limit
        )
        
        return {
            "patient_id": patient_id,
            "count": len(notes),
            "notes": [
                {
                    "id": note.id,
                    "type": note.type.value,
                    "title": note.title,
                    "date": note.date,
                    "author": note.author.name if note.author else None,
                }
                for note in notes
            ]
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================================
# Test Endpoints (for development)
# ============================================================================

@app.get("/test/patients", tags=["Testing"])
async def list_test_patients():
    """List available test patients (mock mode only)."""
    return {
        "mode": "mock",
        "patients": [
            {"name": "Elijah Davis", "fhir_id": "egqBHVfQlt4Bw3XGXoxVxHg3", "mrn": "203709"},
            {"name": "Olivia Roberts", "fhir_id": "eh2xYHuzl9nkSFVvV3osUHg3", "mrn": "203715"},
            {"name": "Warren McGinnis", "fhir_id": "e0w0LEDCYtfckT6N.CkJKCw3", "mrn": "203710"},
        ]
    }


# ============================================================================
# Appeal Letter Endpoints
# ============================================================================

@app.get("/appeal", response_class=HTMLResponse, tags=["Appeal"])
async def appeal_ui():
    """Serve the Appeal Letter Generator UI."""
    ui_path = Path(__file__).parent / "static" / "appeal.html"
    if ui_path.exists():
        return HTMLResponse(content=ui_path.read_text(encoding="utf-8"))
    else:
        return HTMLResponse(content="""
        <html>
        <head><title>Appeal Generator</title></head>
        <body>
            <h1>Appeal Letter Generator UI not found</h1>
            <p>Please ensure static/appeal.html exists.</p>
        </body>
        </html>
        """)


@app.get("/appeal/ministries", tags=["Appeal"])
async def get_ministries():
    """Get list of available ministries for template selection."""
    return [
        {"code": code, "name": info["name"]}
        for code, info in MINISTRY_CONFIG.items()
    ]


# Ministry Configuration (maps ministry codes to templates and display names)
MINISTRY_CONFIG = {
    "ssm_stmarys": {
        "name": "SSM Health St. Mary's Hospital - Madison",
        "template": "examples/Template.docx",  # Default template for now
    },
    "ssm_stclare": {
        "name": "SSM Health St. Clare Hospital - Baraboo",
        "template": "examples/Template.docx",
    },
    "ssm_stagnes": {
        "name": "SSM Health St. Agnes Hospital - Fond du Lac",
        "template": "examples/Template.docx",
    },
    "ssm_dean": {
        "name": "SSM Health Dean Medical Group",
        "template": "examples/Template.docx",
    },
    "ssm_stlouis": {
        "name": "SSM Health St. Louis University Hospital",
        "template": "examples/Template.docx",
    },
    "ssm_cardinal": {
        "name": "SSM Health Cardinal Glennon Children's Hospital",
        "template": "examples/Template.docx",
    },
    "ssm_depaul": {
        "name": "SSM Health DePaul Hospital - St. Louis",
        "template": "examples/Template.docx",
    },
    "ssm_joseph_stcharles": {
        "name": "SSM Health St. Joseph Hospital - St. Charles",
        "template": "examples/Template.docx",
    },
    "ssm_joseph_wentzville": {
        "name": "SSM Health St. Joseph Hospital - Wentzville",
        "template": "examples/Template.docx",
    },
    "ssm_oklahoma": {
        "name": "SSM Health Oklahoma",
        "template": "examples/Template.docx",
    },
}


@app.post("/appeal/upload", response_model=AppealDataResponse, tags=["Appeal"])
async def upload_pdf_for_appeal(
    file: UploadFile = File(...),
    ministry: str = Form("")
):
    """
    Upload a PDF clinical chart and extract data for appeal letter.
    
    Returns editable data that can be modified before generating the final letter.
    """
    from services.pdf_chart_parser import PDFChartParser
    from services.midnight_reason_generator import MidnightReasonGenerator
    import random
    
    # Validate file type
    if not file.filename.lower().endswith('.pdf'):
        raise HTTPException(status_code=400, detail="Only PDF files are accepted")
    
    # Save uploaded file temporarily
    session_id = str(uuid.uuid4())
    temp_dir = Path(tempfile.gettempdir()) / "appeal_sessions" / session_id
    temp_dir.mkdir(parents=True, exist_ok=True)
    
    pdf_path = temp_dir / file.filename
    with open(pdf_path, "wb") as f:
        shutil.copyfileobj(file.file, f)
    
    try:
        # Parse PDF - skip de-identification for demo mode (show original patient name)
        parser = PDFChartParser(use_llm=True)
        patient_data = parser.parse_and_deidentify(str(pdf_path), skip_deidentify=True)
        
        # Generate MidnightReason justifications
        reason_gen = MidnightReasonGenerator()
        reason_output = reason_gen.generate_from_data(patient_data)
        
        # Format dates for display
        def fmt_date(d):
            if not d:
                return ""
            try:
                from datetime import datetime
                dt = datetime.strptime(d[:10], "%Y-%m-%d")
                return dt.strftime("%m/%d/%Y")
            except:
                return d
        
        # Generate random IDs
        random_member_id = f"{random.randint(100000000, 999999999)}"
        random_ref_num = f"A{random.randint(100000000, 999999999)}"
        
        # Format medical history from conditions
        from services.appeal_letter_generator import AppealLetterGenerator
        letter_gen = AppealLetterGenerator()
        medical_history = letter_gen._format_medical_history(patient_data.conditions)
        
        # Format gender display
        gender = patient_data.gender.lower() if patient_data.gender else ""
        gender_display = "male" if gender in ("male", "m") else "female" if gender in ("female", "f") else gender
        
        # Get ministry info
        ministry_info = MINISTRY_CONFIG.get(ministry, {"name": "", "template": "examples/Template.docx"})
        ministry_name = ministry_info["name"]
        
        # Store session data for later use
        temp_storage[session_id] = {
            "pdf_path": str(pdf_path),
            "patient_data": patient_data,
            "reason_output": reason_output,
            "ministry": ministry,
            "ministry_template": ministry_info["template"],
            "account_number": patient_data.account_number,
        }
        
        # Use account number if available, otherwise random
        member_id = patient_data.account_number if patient_data.account_number else random_member_id
        
        return AppealDataResponse(
            session_id=session_id,
            member_name=patient_data.patient_name,
            dob=fmt_date(patient_data.dob),
            age=str(patient_data.age) if patient_data.age else "",
            gender=gender_display,
            member_id=member_id,
            medical_history=medical_history,
            complaint=patient_data.chief_complaint or "evaluation and management",
            observation_date=fmt_date(getattr(patient_data, 'observation_date', '')),
            inpatient_date=fmt_date(getattr(patient_data, 'inpatient_date', '')),
            place_of_service=ministry_name,
            reference_number=random_ref_num,
            payer_name="Medicare Advantage Plan",
            street_address="PO Box 0000",
            city="City",
            state="ST",
            zip_code="00000",
            midnight_reason_1=reason_output.midnight_reason_1,
            midnight_reason_2=reason_output.midnight_reason_2,
        )
        
    except Exception as e:
        # Clean up on error
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise HTTPException(status_code=500, detail=f"Error processing PDF: {str(e)}")


@app.post("/appeal/generate", tags=["Appeal"])
async def generate_appeal_document(request: GenerateAppealRequest):
    """
    Generate the final appeal letter document with user-edited data.
    
    Returns a download URL for the generated DOCX file.
    """
    from services.appeal_letter_generator import AppealLetterGenerator, AppealLetterData
    from services.midnight_reason_generator import MidnightReasonOutput
    
    session_id = request.session_id
    if session_id not in temp_storage:
        raise HTTPException(status_code=404, detail="Session not found. Please upload PDF again.")
    
    session_data = temp_storage[session_id]
    
    try:
        # Format DOS (Date of Service)
        dos_formatted = ""
        if request.observation_date and request.inpatient_date:
            dos_formatted = f"{request.observation_date} - Observation,\n  {request.inpatient_date} – Current, Inpatient"
        elif request.observation_date:
            dos_formatted = f"{request.observation_date} - Observation"
        elif request.inpatient_date:
            dos_formatted = f"{request.inpatient_date} - Inpatient"
        
        # Build letter data from request
        letter_data = AppealLetterData(
            member_name=request.member_name,
            dob=request.dob,
            age=request.age,
            gender=request.gender,
            member_id=request.member_id,
            medical_history=request.medical_history,
            complaint=request.complaint,
            place_of_service=request.place_of_service,
            street_address=request.street_address,
            city=request.city,
            state=request.state,
            zip_code=request.zip_code,
            reference_number=request.reference_number,
            dos=dos_formatted,
            patient_background="",  # Not used in current template
            midnight_reason_1=request.midnight_reason_1,
            midnight_reason_2=request.midnight_reason_2,
        )
        
        # Generate output filename: {First Initial} {Last Name} {Account Number}.docx
        import re
        name_parts = request.member_name.strip().split()
        if len(name_parts) >= 2:
            first_initial = name_parts[0][0].upper()
            last_name = name_parts[-1]
        else:
            first_initial = name_parts[0][0].upper() if name_parts else "X"
            last_name = name_parts[0] if name_parts else "Unknown"
        
        # Get account number from session or request member_id
        account_number = session_data.get("account_number", "") or request.member_id
        account_number = re.sub(r'[^\w]', '', account_number)  # Remove special chars
        
        if account_number:
            filename = f"{first_initial} {last_name} {account_number}.docx"
        else:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"{first_initial} {last_name}_{timestamp}.docx"
        
        # Create output directory
        output_dir = Path(__file__).parent / "output" / "final"
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / filename
        
        # Get ministry template from session (or use default)
        ministry_template = session_data.get("ministry_template", "examples/Template.docx")
        
        # Fill template
        letter_gen = AppealLetterGenerator(template_path=ministry_template)
        letter_gen._fill_template(letter_data, output_path)
        
        # Store output path for download
        temp_storage[session_id]["output_path"] = str(output_path)
        temp_storage[session_id]["output_filename"] = filename
        
        return {
            "success": True,
            "filename": filename,
            "download_url": f"download/{session_id}"
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error generating document: {str(e)}")


@app.get("/appeal/download/{session_id}", tags=["Appeal"])
async def download_appeal_document(session_id: str):
    """Download the generated appeal letter document."""
    if session_id not in temp_storage:
        raise HTTPException(status_code=404, detail="Session not found")
    
    session_data = temp_storage[session_id]
    output_path = session_data.get("output_path")
    output_filename = session_data.get("output_filename")
    
    if not output_path or not Path(output_path).exists():
        raise HTTPException(status_code=404, detail="Document not found. Please generate first.")
    
    return FileResponse(
        path=output_path,
        filename=output_filename,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )


@app.delete("/appeal/session/{session_id}", tags=["Appeal"])
async def cleanup_session(session_id: str):
    """Clean up session data and temporary files."""
    if session_id in temp_storage:
        session_data = temp_storage.pop(session_id)
        # Clean up temp PDF directory
        pdf_path = session_data.get("pdf_path")
        if pdf_path:
            temp_dir = Path(pdf_path).parent
            shutil.rmtree(temp_dir, ignore_errors=True)
    
    return {"success": True, "message": "Session cleaned up"}


# ============================================================================
# Chat Assistant Endpoints
# ============================================================================

# RAG feedback storage (JSON file - replace with database in production)
RAG_FEEDBACK_FILE = Path(__file__).parent / "data" / "appeal_feedback.json"

def load_rag_feedback() -> List[Dict]:
    """Load RAG feedback examples from file."""
    if RAG_FEEDBACK_FILE.exists():
        try:
            with open(RAG_FEEDBACK_FILE, "r") as f:
                return json.load(f)
        except:
            return []
    return []

def save_rag_feedback(feedback: Dict):
    """Save a new RAG feedback example."""
    RAG_FEEDBACK_FILE.parent.mkdir(parents=True, exist_ok=True)
    examples = load_rag_feedback()
    feedback["created_at"] = datetime.now().isoformat()
    examples.append(feedback)
    with open(RAG_FEEDBACK_FILE, "w") as f:
        json.dump(examples, f, indent=2)

def get_relevant_feedback(condition_keywords: List[str], limit: int = 3) -> List[Dict]:
    """Get relevant RAG feedback examples for given conditions."""
    all_feedback = load_rag_feedback()
    relevant = []
    
    for fb in all_feedback:
        category = fb.get("condition_category", "").lower()
        for keyword in condition_keywords:
            if keyword.lower() in category:
                relevant.append(fb)
                break
    
    # Return most recent examples
    return sorted(relevant, key=lambda x: x.get("created_at", ""), reverse=True)[:limit]


@app.post("/appeal/chat", tags=["Chat"])
async def chat_with_assistant(chat: ChatMessage):
    """
    Chat endpoint for appeal letter assistant.
    Uses AWS Bedrock Claude to explain and improve generated content.
    """
    import json
    import boto3
    
    session_id = chat.session_id
    if session_id not in temp_storage:
        raise HTTPException(status_code=404, detail="Session not found. Please upload a PDF first.")
    
    session_data = temp_storage[session_id]
    patient_data = session_data.get("patient_data")
    reason_output = session_data.get("reason_output")
    
    # Get context from request or build from session
    context = chat.context or {}
    if not context and patient_data:
        context = {
            "patient_name": patient_data.patient_name,
            "chief_complaint": patient_data.chief_complaint,
            "conditions": patient_data.conditions,
            "medications": [m.get("name", "") for m in patient_data.medications] if patient_data.medications else [],
            "midnight_reason_1": reason_output.midnight_reason_1 if reason_output else "",
            "midnight_reason_2": reason_output.midnight_reason_2 if reason_output else "",
        }
    
    # Get relevant RAG feedback for similar conditions
    condition_keywords = []
    chief_complaint = context.get("chief_complaint", "").lower()
    for keyword in ["chest pain", "gi bleed", "syncope", "fall", "sepsis", "pneumonia", "copd", "chf", "uti", "aki"]:
        if keyword in chief_complaint:
            condition_keywords.append(keyword.replace(" ", "_"))
    
    rag_examples = get_relevant_feedback(condition_keywords) if condition_keywords else []
    
    # Build system prompt
    system_prompt = f"""You are an Appeal Letter Assistant helping clinical staff improve Medicare appeal justifications.

CURRENT CASE CONTEXT:
- Chief Complaint: {context.get('chief_complaint', 'N/A')}
- Conditions: {', '.join(context.get('conditions', [])[:10])}
- Medications: {', '.join(context.get('medications', [])[:10])}

GENERATED MIDNIGHT REASONS:
1. {context.get('midnight_reason_1', 'Not generated yet')}

2. {context.get('midnight_reason_2', 'Not generated yet')}

{f'''PREVIOUS FEEDBACK EXAMPLES (use these to improve suggestions):
''' + chr(10).join([f"- Category: {ex.get('condition_category')}: {ex.get('improvement_reason')}" for ex in rag_examples]) if rag_examples else ''}

YOUR ROLE:
1. Explain why the Midnight Reasons were generated the way they were
2. Suggest improvements based on clinical best practices and Medicare guidelines
3. Learn from user feedback to improve future generations
4. Focus on medical necessity, severity of illness, and intensity of services

DETECTING IMPROVEMENTS:
When the user provides feedback or a better version, acknowledge it and output:

[FEEDBACK_DETECTED]
condition_category: (e.g., chest_pain, gi_bleed, syncope, general)
original_text: (the original text they're improving)
improved_text: (their suggested improvement)
improvement_reason: (one sentence summary of why this is better)
[/FEEDBACK_DETECTED]

IMPORTANT: Only include this block when the user provides a specific improvement, not just questions.

Keep responses concise and clinically focused."""

    # Build messages
    messages = []
    if chat.conversation_history:
        for msg in chat.conversation_history:
            messages.append({
                "role": msg.get("role", "user"),
                "content": [{"text": msg.get("content", "")}]
            })
    
    messages.append({
        "role": "user",
        "content": [{"text": chat.message}]
    })
    
    try:
        bedrock = boto3.client("bedrock-runtime", region_name="us-east-1")
        response = bedrock.converse(
            modelId="us.anthropic.claude-sonnet-4-20250514-v1:0",
            system=[{"text": system_prompt}],
            messages=messages,
            inferenceConfig={"maxTokens": 1000, "temperature": 0.3}
        )
        
        assistant_response = response["output"]["message"]["content"][0]["text"]
        
        # Check for feedback in response
        import re
        feedback_match = re.search(r'\[FEEDBACK_DETECTED\]([\s\S]*?)\[\/FEEDBACK_DETECTED\]', assistant_response)
        pending_feedback = None
        
        if feedback_match:
            block = feedback_match.group(1)
            pending_feedback = {}
            
            for field in ["condition_category", "original_text", "improved_text", "improvement_reason"]:
                match = re.search(rf'{field}:\s*(.+?)(?=\n\w+:|$)', block, re.DOTALL)
                if match:
                    pending_feedback[field] = match.group(1).strip()
            
            # Clean the response for display
            assistant_response = re.sub(r'\[FEEDBACK_DETECTED\][\s\S]*?\[\/FEEDBACK_DETECTED\]', '', assistant_response).strip()
        
        return {
            "response": assistant_response,
            "session_id": session_id,
            "pending_feedback": pending_feedback
        }
        
    except Exception as e:
        return {
            "response": f"I encountered an error: {str(e)}. Please check your AWS credentials.",
            "session_id": session_id,
            "pending_feedback": None
        }


@app.post("/appeal/feedback", tags=["Chat"])
async def save_feedback(feedback: FeedbackExample):
    """Save user feedback for RAG training."""
    try:
        save_rag_feedback(feedback.dict())
        return {"success": True, "message": "Feedback saved for future improvements"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Could not save feedback: {str(e)}")


@app.get("/appeal/feedback", tags=["Chat"])
async def get_feedback(category: Optional[str] = None, limit: int = 10):
    """Get saved feedback examples."""
    all_feedback = load_rag_feedback()
    
    if category:
        all_feedback = [f for f in all_feedback if category.lower() in f.get("condition_category", "").lower()]
    
    return sorted(all_feedback, key=lambda x: x.get("created_at", ""), reverse=True)[:limit]


if __name__ == "__main__":
    import uvicorn
    from config import API_PORT
    uvicorn.run(app, host="0.0.0.0", port=API_PORT)

"""
FastAPI web service for Patient Stay Summary API.

Run with: uvicorn app:app --reload --port 8001
"""
from fastapi import FastAPI, HTTPException, Query, UploadFile, File, Form, Response
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
import logging
import argparse
from logging.handlers import RotatingFileHandler

# Parse command line args (before logging setup)
parser = argparse.ArgumentParser(description='Patient Stay Appeal API')
parser.add_argument('--nodebug', action='store_true', help='Disable debug UI for demos')
args, _ = parser.parse_known_args()
DEBUG_UI_ENABLED = not args.nodebug

# Setup logging
log_dir = Path(__file__).parent / "logs"
log_dir.mkdir(exist_ok=True)

# Configure root logger
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        RotatingFileHandler(
            log_dir / "app.log",
            maxBytes=10*1024*1024,  # 10MB
            backupCount=5
        ),
        logging.StreamHandler()  # Also log to console
    ]
)
logger = logging.getLogger(__name__)

from config import (
    DEBUG_MODE, EPIC_CLIENT_ID, EPIC_AUTHORIZE_URL, EPIC_TOKEN_URL,
    EPIC_REDIRECT_URI, EPIC_SCOPE, EPIC_BASE_URL, LANGFUSE_HOST, LANGFUSE_PROJECT_ID
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

# Favicon handler to suppress browser 404 errors
@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return Response(status_code=204)

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
    account_number: str = ""  # Account/encounter number from PDF
    medical_history: str = ""
    complaint: str = ""
    
    # Case info (editable)
    place_of_service: str = ""  # emergency department, hospital, etc.
    observation_date: str = ""
    inpatient_date: str = ""
    reference_number: str = ""
    
    # Payer info (editable)
    payer_name: str = ""
    insurance_name: str = ""  # Original extracted insurance name
    street_address: str = ""
    city: str = ""
    state: str = ""
    zip_code: str = ""
    
    # Generated content (editable)
    midnight_reason_1: str = ""
    midnight_reason_2: str = ""
    
    # Lab results from PDF
    lab_results: List[Dict] = []


class GenerateAppealRequest(BaseModel):
    """Request to generate final appeal document with edited data."""
    session_id: str
    
    # Ministry selection (allows changing after upload)
    ministry: str = ""
    
    # All editable fields
    member_name: str
    dob: str
    age: str
    gender: str
    member_id: str
    medical_history: str
    complaint: str
    
    place_of_service: str = ""
    observation_date: str = ""
    inpatient_date: str = ""
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
            <p style="color: green;">âœ“ Logged in as Patient: {patient_id}</p>
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
    """Get list of available ministries for template selection, including auto-detection patterns."""
    return [
        {
            "code": code,
            "name": info["name"],
            "station_codes": info.get("station_codes", []),
            "name_patterns": info.get("name_patterns", [])
        }
        for code, info in MINISTRY_CONFIG.items()
    ]


# Load ministry configuration from JSON file (single source of truth)
def load_ministry_config():
    """Load ministry configuration from ministries.json."""
    config_path = Path(__file__).parent / "ministries.json"
    try:
        with open(config_path, "r") as f:
            data = json.load(f)
            return data.get("ministries", {})
    except Exception as e:
        logging.error(f"Failed to load ministries.json: {e}")
        return {}

MINISTRY_CONFIG = load_ministry_config()


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
    
    logger.info(f"Processing PDF upload: {file.filename} (session: {session_id}, ministry: {ministry})")
    
    try:
        # Parse PDF - skip de-identification for demo mode (show original patient name)
        parser = PDFChartParser(use_llm=True)
        patient_data = parser.parse_and_deidentify(str(pdf_path), skip_deidentify=True)
        
        logger.info(f"Parsed patient: {patient_data.patient_name} (session: {session_id})")
        
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
        
        # Generate random member ID as fallback
        random_member_id = f"{random.randint(100000000, 999999999)}"
        
        # Format medical history from conditions
        from services.appeal_letter_generator import AppealLetterGenerator
        letter_gen = AppealLetterGenerator()
        medical_history = letter_gen._format_medical_history(patient_data.conditions)
        
        # Format gender display
        gender = patient_data.gender.lower() if patient_data.gender else ""
        gender_display = "male" if gender in ("male", "m") else "female" if gender in ("female", "f") else gender
        
        # Get ministry info (for dynamic letterhead)
        ministry_info = MINISTRY_CONFIG.get(ministry, {
            "name": "",
            "address": "",
            "city": "",
            "state": "",
            "zip": "",
            "phone": ""
        })
        ministry_name = ministry_info["name"]
        
        # Place of service from PDF extraction (emergency department, hospital, etc.)
        place_of_service = getattr(patient_data, 'place_of_service', '') or 'Emergency Department'
        
        # Get debug data from parser for verification UI
        debug_data = parser.get_debug_data()
        
        # Get debug data from midnight reason generator
        midnight_debug_data = reason_gen.get_debug_data()
        
        # Store session data for later use
        temp_storage[session_id] = {
            "pdf_path": str(pdf_path),
            "patient_data": patient_data,
            "reason_output": reason_output,
            "ministry": ministry,
            "ministry_name": ministry_name,  # Hospital name for letterhead/signing
            "ministry_info": ministry_info,  # Full ministry info for dynamic header
            "account_number": patient_data.account_number,
            "insurance_name": getattr(patient_data, 'insurance_name', ''),
            "insurance_id": getattr(patient_data, 'insurance_id', ''),
            # Debug data for extraction verification
            "debug_data": debug_data,
            # Debug data for midnight reason generation
            "midnight_debug_data": midnight_debug_data,
        }
        
        # Use insurance member ID if available, otherwise account number, otherwise random
        if getattr(patient_data, 'insurance_id', ''):
            member_id = patient_data.insurance_id
        elif patient_data.account_number:
            member_id = patient_data.account_number
        else:
            member_id = random_member_id
        
        # Use extracted insurance name or default
        payer_name = getattr(patient_data, 'insurance_name', '') or "Medicare Advantage Plan"
        
        # Use extracted payer address or placeholders
        street_address = getattr(patient_data, 'insurance_address', '') or "PO Box 0000"
        city = getattr(patient_data, 'insurance_city', '') or "City"
        state = getattr(patient_data, 'insurance_state', '') or "ST"
        zip_code = getattr(patient_data, 'insurance_zip', '') or "00000"
        
        return AppealDataResponse(
            session_id=session_id,
            member_name=patient_data.patient_name,
            dob=fmt_date(patient_data.dob),
            age=str(patient_data.age) if patient_data.age else "",
            gender=gender_display,
            member_id=member_id,
            account_number=patient_data.account_number or "",
            medical_history=medical_history,
            complaint=patient_data.chief_complaint or "evaluation and management",
            place_of_service=getattr(patient_data, 'place_of_service', '') or place_of_service,
            observation_date=fmt_date(getattr(patient_data, 'observation_date', '')),
            inpatient_date=fmt_date(getattr(patient_data, 'inpatient_date', '')),
            reference_number="",  # User enters from denial letter
            payer_name=payer_name,
            insurance_name=getattr(patient_data, 'insurance_name', '') or "",
            street_address=street_address,
            city=city,
            state=state,
            zip_code=zip_code,
            midnight_reason_1=reason_output.midnight_reason_1,
            midnight_reason_2=reason_output.midnight_reason_2,
            lab_results=patient_data.lab_results or [],
        )
        
    except Exception as e:
        # Clean up on error
        shutil.rmtree(temp_dir, ignore_errors=True)
        logger.error(f"Error processing PDF upload: {str(e)}", exc_info=True)
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
    logger.info(f"Generating appeal document (session: {session_id}, patient: {request.member_name})")
    
    if session_id not in temp_storage:
        logger.warning(f"Session not found: {session_id}")
        raise HTTPException(status_code=404, detail="Session not found. Please upload PDF again.")
    
    session_data = temp_storage[session_id]
    
    try:
        # Format DOS (Date of Service)
        dos_formatted = ""
        if request.observation_date and request.inpatient_date:
            dos_formatted = f"{request.observation_date} - Observation,\n  {request.inpatient_date} â€“ Current, Inpatient"
        elif request.observation_date:
            dos_formatted = f"{request.observation_date} - Observation"
        elif request.inpatient_date:
            dos_formatted = f"{request.inpatient_date} - Inpatient"
        
        # Get ministry info - prefer request value, fall back to session
        if request.ministry and request.ministry in MINISTRY_CONFIG:
            ministry_info = MINISTRY_CONFIG[request.ministry]
        else:
            ministry_info = session_data.get("ministry_info", {})
        
        # Build letter data from request
        letter_data = AppealLetterData(
            member_name=request.member_name,
            dob=request.dob,
            age=request.age,
            gender=request.gender,
            member_id=request.member_id,
            medical_history=request.medical_history,
            complaint=request.complaint,
            place_of_service=request.place_of_service or "Emergency Department",
            street_address=request.street_address,
            city=request.city,
            state=request.state,
            zip_code=request.zip_code,
            reference_number=request.reference_number,
            dos=dos_formatted,
            patient_background="",  # Not used in current template
            midnight_reason_1=request.midnight_reason_1,
            midnight_reason_2=request.midnight_reason_2,
            # Ministry info for dynamic header
            ministry_name=ministry_info.get("name", ""),
            ministry_address=ministry_info.get("address", ""),
            ministry_city=ministry_info.get("city", ""),
            ministry_state=ministry_info.get("state", ""),
            ministry_zip=ministry_info.get("zip", ""),
            ministry_phone=ministry_info.get("phone", ""),
        )
        
        # Generate output filename: {First Initial} {Last Name} {Account Number}.docx
        import re
        member_name = request.member_name.strip()
        
        # Handle "Last, First" format (e.g., "Eisenman, Shirley J")
        if ',' in member_name:
            parts = member_name.split(',', 1)
            last_name = parts[0].strip()
            first_parts = parts[1].strip().split() if len(parts) > 1 else []
            first_initial = first_parts[0][0].upper() if first_parts else "X"
        else:
            # "First Last" format
            name_parts = member_name.split()
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
        
        # Use ministry-specific template (with correct letterhead baked in)
        template_file = ministry_info.get("template_file", "Template.docx")
        template_path = f"appeal_templates/{template_file}"
        
        # Fall back to generic template if ministry template doesn't exist
        if not Path(template_path).exists():
            logger.warning(f"Ministry template not found: {template_path}, using Template.docx")
            template_path = "appeal_templates/Template.docx"
        
        logger.info(f"Using template: {template_path}")
        letter_gen = AppealLetterGenerator(template_path=template_path)
        letter_gen._fill_template(letter_data, output_path)
        
        # Store output path for download
        temp_storage[session_id]["output_path"] = str(output_path)
        temp_storage[session_id]["output_filename"] = filename
        
        logger.info(f"Generated document: {filename} (session: {session_id})")
        
        return {
            "success": True,
            "filename": filename,
            "download_url": f"appeal/download/{session_id}"
        }
        
    except Exception as e:
        logger.error(f"Error generating document: {str(e)}", exc_info=True)
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


@app.get("/appeal/debug-enabled", tags=["Appeal"])
async def debug_enabled():
    """Check if debug UI is enabled (hidden when started with --nodebug)."""
    return {"enabled": DEBUG_UI_ENABLED}


@app.get("/appeal/debug/{session_id}", tags=["Appeal"])
async def get_extraction_debug(session_id: str):
    """
    Get extraction debug data for verification UI.
    
    Returns source text and faithfulness validation results so users can
    verify Langfuse claims against the actual PDF content.
    """
    if not DEBUG_UI_ENABLED:
        raise HTTPException(status_code=404, detail="Debug UI is disabled")
    if session_id not in temp_storage:
        raise HTTPException(status_code=404, detail="Session not found. Please upload a PDF first.")
    
    session_data = temp_storage[session_id]
    debug_data = session_data.get("debug_data", {})
    midnight_debug_data = session_data.get("midnight_debug_data", {})
    patient_data = session_data.get("patient_data")
    
    # Get patient name for display
    patient_name = ""
    if patient_data:
        patient_name = getattr(patient_data, 'patient_name', '') or ''
    
    # Build Langfuse URL - link to LLM-as-a-Judge evals page
    extraction_timestamp = debug_data.get("extraction_timestamp", "")
    # Use configured project ID, fall back to placeholder if not set
    project_id = LANGFUSE_PROJECT_ID or "YOUR_PROJECT_ID"
    langfuse_base = LANGFUSE_HOST.rstrip('/')
    # Link to evals page for LLM-as-a-Judge scores
    langfuse_trace_url = f"{langfuse_base}/project/{project_id}/evals"
    
    return {
        "session_id": session_id,
        "patient_name": patient_name,
        "extraction_timestamp": extraction_timestamp,
        # PDF Extraction debug (pdf_chart_parser)
        "faithfulness_score": debug_data.get("faithfulness_score", 0),
        "conditions_validated": debug_data.get("conditions_validated", []),
        "conditions_flagged": debug_data.get("conditions_flagged", []),
        "medications_validated": debug_data.get("medications_validated", []),
        "medications_flagged": debug_data.get("medications_flagged", []),
        "lab_results_validated": debug_data.get("lab_results_validated", []),
        "lab_results_flagged": debug_data.get("lab_results_flagged", []),
        "source_text": debug_data.get("source_text", ""),
        "details": debug_data.get("details", []),
        "langfuse_trace_url": langfuse_trace_url,
        # Midnight Reason Generation debug (midnight_reason_generator)
        "midnight_debug": {
            "input_conditions": midnight_debug_data.get("input_conditions", []),
            "input_medications": midnight_debug_data.get("input_medications", []),
            "generated_text": midnight_debug_data.get("generated_text", ""),
            "patient_background": midnight_debug_data.get("patient_background", ""),
            "midnight_reason_1": midnight_debug_data.get("midnight_reason_1", ""),
            "midnight_reason_2": midnight_debug_data.get("midnight_reason_2", ""),
            "conditions_used": midnight_debug_data.get("conditions_used", []),
            "conditions_hallucinated": midnight_debug_data.get("conditions_hallucinated", []),
            "generation_timestamp": midnight_debug_data.get("generation_timestamp", ""),
        }
    }


@app.get("/appeal/debug", response_class=HTMLResponse, tags=["Appeal"])
async def debug_ui():
    """Serve the Extraction Debug & Verification UI."""
    if not DEBUG_UI_ENABLED:
        raise HTTPException(status_code=404, detail="Debug UI is disabled")
    ui_path = Path(__file__).parent / "static" / "debug.html"
    if ui_path.exists():
        return HTMLResponse(content=ui_path.read_text(encoding="utf-8"))
    else:
        return HTMLResponse(content="""
        <html>
        <head><title>Debug UI</title></head>
        <body>
            <h1>Debug UI not found</h1>
            <p>Please ensure static/debug.html exists.</p>
        </body>
        </html>
        """)


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
    print(f"[CHAT] Received chat request for session: {chat.session_id}", flush=True)
    import json
    import boto3
    
    session_id = chat.session_id
    if session_id not in temp_storage:
        raise HTTPException(status_code=404, detail="Session not found. Please upload a PDF first.")
    
    session_data = temp_storage[session_id]
    patient_data = session_data.get("patient_data")
    reason_output = session_data.get("reason_output")
    
    # Start with frontend context, then fill in missing values from patient_data
    context = dict(chat.context) if chat.context else {}
    
    if patient_data:
        # Format labs for display
        labs_summary = ""
        if patient_data.lab_results:
            labs_list = []
            for lab in patient_data.lab_results:  # Include all extracted labs
                name = lab.get("name", lab.get("test", ""))
                value = lab.get("value", "")
                unit = lab.get("unit", "")
                flag = lab.get("flag", "")
                if name and value:
                    entry = f"{name}: {value}"
                    if unit:
                        entry += f" {unit}"
                    if flag:
                        entry += f" ({flag})"
                    labs_list.append(entry)
            labs_summary = "; ".join(labs_list) if labs_list else "None extracted"
        
        # Format clinical notes if available (HPI and other notes used by midnight reason generator)
        clinical_notes_summary = ""
        if hasattr(patient_data, 'clinical_notes') and patient_data.clinical_notes:
            clinical_notes_summary = "\n\n".join(patient_data.clinical_notes[:3])  # First 3 notes
        
        # Format vitals for display
        vitals_summary = ""
        vitals = getattr(patient_data, 'vital_signs', None) or getattr(patient_data, 'vitals', None)
        if vitals:
            vitals_list = []
            for v in vitals:
                if isinstance(v, dict):
                    for key, val in v.items():
                        if val:
                            vitals_list.append(f"{key}: {val}")
            vitals_summary = "; ".join(vitals_list) if vitals_list else "None extracted"
        
        # Format medications
        meds_list = []
        if patient_data.medications:
            for m in patient_data.medications[:15]:
                name = m.get("name", "") if isinstance(m, dict) else str(m)
                if name:
                    meds_list.append(name)
        
        # Debug: log what place_of_service is in patient_data
        logger.info(f"[CHAT DEBUG] place_of_service in patient_data: '{getattr(patient_data, 'place_of_service', 'NOT_FOUND')}'")
        logger.info(f"[CHAT DEBUG] place_of_service from frontend context: '{context.get('place_of_service', 'NOT_SENT')}'")
        
        # Fill in any missing context fields from patient_data
        defaults = {
            "patient_name": patient_data.patient_name,
            "age": getattr(patient_data, 'age', 'N/A') or 'N/A',
            "gender": getattr(patient_data, 'gender', 'N/A') or 'N/A',
            "admission_date": getattr(patient_data, 'admission_date', 'N/A') or 'N/A',
            "place_of_service": getattr(patient_data, 'place_of_service', 'N/A') or 'N/A',
            "place_of_service_raw_code": getattr(patient_data, 'place_of_service_raw_code', '') or '',
            "facility_name": getattr(patient_data, 'facility_name', 'N/A') or 'N/A',
            "attending_physician": getattr(patient_data, 'attending_physician', 'N/A') or 'N/A',
            "chief_complaint": patient_data.chief_complaint,
            "hpi": getattr(patient_data, 'hpi', 'Not available') or 'Not available',
            "conditions": patient_data.conditions,
            "medications": meds_list,
            "labs": labs_summary,
            "vitals": vitals_summary,
            "patient_background": reason_output.patient_background if reason_output else "",
            "midnight_reason_1": reason_output.midnight_reason_1 if reason_output else "",
            "midnight_reason_2": reason_output.midnight_reason_2 if reason_output else "",
        }
        
        # Only add defaults for keys not already in context (or empty values)
        for key, value in defaults.items():
            if key not in context or not context[key]:
                context[key] = value
    
    # Get relevant RAG feedback for similar conditions
    condition_keywords = []
    chief_complaint = context.get("chief_complaint", "").lower()
    for keyword in ["chest pain", "gi bleed", "syncope", "fall", "sepsis", "pneumonia", "copd", "chf", "uti", "aki"]:
        if keyword in chief_complaint:
            condition_keywords.append(keyword.replace(" ", "_"))
    
    rag_examples = get_relevant_feedback(condition_keywords) if condition_keywords else []
    
    # Build system prompt with full context
    system_prompt = f"""You are an Appeal Letter Assistant helping clinical staff understand and improve Medicare appeal justifications.

PATIENT DEMOGRAPHICS:
- Age: {context.get('age', 'N/A')}
- Gender: {context.get('gender', 'N/A')}
- Admission Date: {context.get('admission_date', 'N/A')}

FACILITY & SERVICE INFO:
- Place of Service: {context.get('place_of_service', 'N/A')} (extracted from SERVICE code: {context.get('place_of_service_raw_code', 'not available')})
- Facility Name: {context.get('facility_name', 'N/A')}
- Attending Physician: {context.get('attending_physician', 'N/A')}

CLINICAL DATA FROM PDF:
- Chief Complaint: {context.get('chief_complaint', 'N/A')}
- History of Present Illness (HPI): {context.get('hpi', 'Not extracted')[:500]}
- Conditions: {', '.join(context.get('conditions', [])[:15])}
- Medications: {', '.join(context.get('medications', [])[:15])}
- Labs: {context.get('labs', 'None extracted')}
- Vitals: {context.get('vitals', 'None extracted')}

GENERATED APPEAL CONTENT:

Patient Background (Opening Paragraph):
{context.get('patient_background', 'Not generated yet')}

Midnight Reason 1:
{context.get('midnight_reason_1', 'Not generated yet')}

Midnight Reason 2:
{context.get('midnight_reason_2', 'Not generated yet')}

{f'''PREVIOUS FEEDBACK EXAMPLES (use these to improve suggestions):
''' + chr(10).join([f"- Category: {ex.get('condition_category')}: {ex.get('improvement_reason')}" for ex in rag_examples]) if rag_examples else ''}

DATA EXTRACTION SOURCES (use these when explaining where data came from):
- Place of Service: Extracted from the SERVICE field in the PDF's ADMISSION RECORD section. Code mappings: ERS=Emergency Department, OBS=Observation, HOSP/HOSPI=Inpatient Hospital, MED=Medical Unit, SURG=Surgical Unit, ICU/CCU/MICU/SICU=Intensive Care, PEDS=Pediatrics, PSYCH=Psychiatric Unit
- Age/Gender/Admission Date: Extracted from patient demographics section of the PDF
- Chief Complaint & HPI: Extracted from clinical notes sections
- Labs/Vitals: Extracted from diagnostic/results sections of the PDF
- Conditions/Medications: Extracted via LLM analysis of the full clinical text

YOUR ROLE:
1. Answer the user's question directly without pivoting to other topics
2. If asked about a field (like chief complaint), just answer about that field - don't explain how it was used in midnight reasons unless specifically asked
3. When asked where a value came from, refer to the DATA EXTRACTION SOURCES above
4. Only discuss midnight reasons if the user explicitly asks about them
5. Suggest improvements only when asked for suggestions

CRITICAL CONSTRAINTS:
- The midnight reason generator was instructed to ONLY use lab values from the same Labs list shown above
- If a lab value appears in the midnight reasons but is NOT in the Labs list above, that value may have been hallucinated and should be corrected
- ONLY cite sources from the DATA EXTRACTION SOURCES section above - never invent or guess where data came from
- If data isn't in the context above, simply say what IS there (e.g., "The medications listed are all oral: [list]") - don't explain that something wasn't extracted
- Never fabricate clinical details, lab values, or patient information not provided in the context above

DETECTING IMPROVEMENTS:
When the user provides feedback or a better version, acknowledge it and output:

[FEEDBACK_DETECTED]
condition_category: (e.g., chest_pain, gi_bleed, syncope, general)
original_text: (the original text they're improving)
improved_text: (their suggested improvement)
improvement_reason: (one sentence summary of why this is better)
[/FEEDBACK_DETECTED]

IMPORTANT: Only include this block when the user provides a specific improvement, not just questions.

RESPONSE STYLE:
- Be concise: 1-2 sentences for simple questions
- Answer the exact question asked - don't add context about midnight reasons unless asked
- If asked "what is the chief complaint?" just state it, don't explain how it was used
- State what IS in the data, not what ISN'T
- Never reference the system prompt, "DATA EXTRACTION SOURCES", "extracted data", or internal instructions
- Only elaborate if the user explicitly asks for more detail"""

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
        logger.error(f"Error saving feedback: {str(e)}", exc_info=True)
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
    logger.info(f"Starting Patient Stay Appeal API on port {API_PORT}")
    uvicorn.run(app, host="0.0.0.0", port=API_PORT)

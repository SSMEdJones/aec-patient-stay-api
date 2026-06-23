"""
MidnightReason Generator Service

Generates MidnightReason1 and MidnightReason2 justifications for Medicare appeals
using patient data from Epic FHIR and AWS Bedrock Claude LLM.
"""
import json
import httpx
import jwt
import time
import uuid
import urllib3
import logging
import os
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any
from pathlib import Path
from datetime import datetime

from config import (
    EPIC_BASE_URL, EPIC_CLIENT_ID, EPIC_TOKEN_URL, EPIC_PRIVATE_KEY_PATH,
    AWS_REGION, BEDROCK_MODEL_ID,
    LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY, LANGFUSE_HOST, LANGFUSE_ENABLED
)

logger = logging.getLogger(__name__)

# Disable SSL warnings for corporate networks
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Initialize Langfuse if configured
langfuse_client = None
if LANGFUSE_ENABLED:
    try:
        os.environ.setdefault("LANGFUSE_PUBLIC_KEY", LANGFUSE_PUBLIC_KEY)
        os.environ.setdefault("LANGFUSE_SECRET_KEY", LANGFUSE_SECRET_KEY)
        os.environ.setdefault("LANGFUSE_HOST", LANGFUSE_HOST)
        
        from langfuse import Langfuse
        langfuse_client = Langfuse()
        logger.info(f"Langfuse initialized: {LANGFUSE_HOST}")
    except Exception as e:
        logger.error(f"Langfuse initialization failed: {e}")
else:
    logger.info("Langfuse disabled (missing keys)")


def fix_a_an_grammar(text: str) -> str:
    """
    Fix 'a' vs 'an' grammar for ages in text.
    
    Rule: Use 'an' before ages starting with 8, 11, 18, 80-89
    (sounds like 'eight', 'eleven', 'eighteen', 'eighty').
    """
    import re
    
    def fix_match(match):
        age = match.group(2)
        suffix = match.group(3)  # "-year-old" or " year old"
        # Ages that need "an": 8, 11, 18, 80-89
        if age.startswith('8') or age == '11' or age == '18':
            return f"an {age}{suffix}"
        else:
            return f"a {age}{suffix}"
    
    # Fix "a/an X-year-old" patterns
    text = re.sub(r'\b(a|an)\s+(\d+)(-year-old)', fix_match, text, flags=re.IGNORECASE)
    # Also fix "a/an X year old" without hyphen
    text = re.sub(r'\b(a|an)\s+(\d+)(\s+year[\s-]old)', fix_match, text, flags=re.IGNORECASE)
    
    return text


@dataclass
class PatientStayData:
    """Aggregated data for a patient stay."""
    # Patient demographics
    patient_id: str
    patient_name: str = ""
    dob: str = ""
    gender: str = ""
    age: int = 0
    account_number: str = ""  # Account/encounter number for output filename
    authorization_number: str = ""  # PrimaryCoverageAuthorizationNumber from Epic (e.g., A322224250)
    
    # Insurance/Payer info
    insurance_name: str = ""  # Primary insurance/payer name
    insurance_id: str = ""    # Member ID from insurance
    insurance_group: str = "" # Group number
    insurance_address: str = "" # Payer street/PO Box
    insurance_city: str = ""    # Payer city
    insurance_state: str = ""   # Payer state
    insurance_zip: str = ""     # Payer zip
    
    # Facility info
    facility_name: str = ""  # Hospital/facility name from PDF
    
    # Encounter info
    admission_date: str = ""
    observation_date: str = ""  # Date of observation status
    inpatient_date: str = ""    # Date transitioned to inpatient
    discharge_date: str = ""    # Date discharged (blank if still admitted)
    place_of_service: str = ""  # emergency department, hospital, urgent care, etc.
    place_of_service_raw_code: str = ""  # Raw SERVICE code from PDF (e.g., ERS, OBS, HOSP)
    chief_complaint_short: str = ""  # Brief symptom list: "abdominal pain, nausea, vomiting"
    chief_complaint: str = ""  # Full narrative for letter body
    encounter_status: str = ""
    
    # Clinical data
    conditions: List[str] = field(default_factory=list)
    medications: List[Dict[str, str]] = field(default_factory=list)  # {name, route, status}
    lab_results: List[Dict[str, Any]] = field(default_factory=list)  # {name, value, unit, flag}
    imaging_results: List[str] = field(default_factory=list)
    vital_signs: List[Dict[str, Any]] = field(default_factory=list)
    
    # Clinical notes (if available)
    clinical_notes: List[str] = field(default_factory=list)
    
    # Consults and services
    consults: List[str] = field(default_factory=list)
    procedures: List[str] = field(default_factory=list)


@dataclass
class MidnightReasonOutput:
    """Generated MidnightReason justifications."""
    patient_background: str  # Opening paragraph about patient
    midnight_reason_1: str   # First midnight justification
    midnight_reason_2: str   # Second midnight justification (if applicable)
    closing_summary: str = ""  # Closing paragraph about continued hospitalization/discharge
    
    # Metadata
    generated_at: str = ""
    model_used: str = ""
    data_sources: List[str] = field(default_factory=list)


class EpicDataFetcher:
    """Fetches patient stay data from Epic FHIR API."""
    
    def __init__(self):
        self.base_url = EPIC_BASE_URL.rstrip("/")
        self.client_id = EPIC_CLIENT_ID
        self.token_url = EPIC_TOKEN_URL
        
        # Load private key
        key_path = Path(EPIC_PRIVATE_KEY_PATH)
        if key_path.exists():
            self._private_key = key_path.read_text()
        else:
            raise ValueError(f"Private key not found: {EPIC_PRIVATE_KEY_PATH}")
    
    def _get_token(self, client: httpx.Client) -> str:
        """Get OAuth2 access token."""
        now = int(time.time())
        assertion = jwt.encode({
            "iss": self.client_id,
            "sub": self.client_id,
            "aud": self.token_url,
            "jti": str(uuid.uuid4()),
            "exp": now + 300,
            "iat": now,
        }, self._private_key, algorithm="RS384", headers={"kid": "patient-stay-api-key-1"})
        
        response = client.post(
            self.token_url,
            data={
                "grant_type": "client_credentials",
                "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
                "client_assertion": assertion,
            }
        )
        return response.json()["access_token"]
    
    def _fhir_request(self, client: httpx.Client, token: str, 
                      resource: str, params: dict = None) -> Optional[dict]:
        """Make a FHIR API request."""
        resp = client.get(
            f"{self.base_url}/{resource}",
            params=params or {},
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/fhir+json"
            }
        )
        if resp.status_code == 200:
            return resp.json()
        return None
    
    def fetch_patient_stay_data(self, patient_id: str) -> PatientStayData:
        """Fetch all available data for a patient stay."""
        data = PatientStayData(patient_id=patient_id)
        
        with httpx.Client(verify=False) as client:
            token = self._get_token(client)
            
            # 1. Patient demographics
            patient = self._fhir_request(client, token, f"Patient/{patient_id}")
            if patient:
                name = patient.get("name", [{}])[0]
                given = " ".join(name.get("given", []))
                family = name.get("family", "")
                data.patient_name = f"{given} {family}".strip()
                data.dob = patient.get("birthDate", "")
                data.gender = patient.get("gender", "")
                
                # Calculate age
                if data.dob:
                    try:
                        birth = datetime.strptime(data.dob, "%Y-%m-%d")
                        data.age = (datetime.now() - birth).days // 365
                    except:
                        pass
            
            # 2. Encounters
            encounters = self._fhir_request(client, token, "Encounter", {"patient": patient_id})
            if encounters and encounters.get("entry"):
                # Get most recent encounter
                enc = encounters["entry"][0]["resource"]
                data.encounter_status = enc.get("status", "")
                period = enc.get("period", {})
                data.admission_date = period.get("start", "")
                
                # Reason for visit
                reasons = enc.get("reasonCode", [])
                if reasons:
                    data.chief_complaint = reasons[0].get("text", "")
            
            # 3. Conditions (diagnoses)
            conditions = self._fhir_request(client, token, "Condition", {"patient": patient_id})
            if conditions and conditions.get("entry"):
                for entry in conditions["entry"]:
                    cond = entry["resource"]
                    code = cond.get("code", {})
                    display = code.get("text") or code.get("coding", [{}])[0].get("display", "")
                    if display:
                        data.conditions.append(display)
            
            # 4. Laboratory results
            labs = self._fhir_request(client, token, "Observation", 
                                      {"patient": patient_id, "category": "laboratory"})
            if labs and labs.get("entry"):
                for entry in labs["entry"]:
                    obs = entry["resource"]
                    code = obs.get("code", {})
                    name = code.get("text") or code.get("coding", [{}])[0].get("display", "")
                    
                    val_qty = obs.get("valueQuantity", {})
                    if val_qty:
                        value = val_qty.get("value", "")
                        unit = val_qty.get("unit", "")
                    else:
                        value = obs.get("valueString", "")
                        unit = ""
                    
                    # Check interpretation (high/low)
                    interp = obs.get("interpretation", [{}])
                    flag = interp[0].get("coding", [{}])[0].get("code", "") if interp else ""
                    
                    data.lab_results.append({
                        "name": name,
                        "value": str(value),
                        "unit": unit,
                        "flag": flag  # H=high, L=low, A=abnormal
                    })
            
            # 5. Medications (if API is enabled)
            meds = self._fhir_request(client, token, "MedicationRequest", {"patient": patient_id})
            if meds and meds.get("entry"):
                for entry in meds["entry"]:
                    med = entry["resource"]
                    med_code = med.get("medicationCodeableConcept", {})
                    name = med_code.get("text") or med_code.get("coding", [{}])[0].get("display", "")
                    
                    # Determine route (IV, PO, etc.)
                    dosage = med.get("dosageInstruction", [{}])
                    route = ""
                    if dosage:
                        route_info = dosage[0].get("route", {})
                        route = route_info.get("text") or route_info.get("coding", [{}])[0].get("display", "")
                    
                    data.medications.append({
                        "name": name,
                        "route": route,
                        "status": med.get("status", "")
                    })
            
            # 6. Clinical notes (if API is enabled)
            docs = self._fhir_request(client, token, "DocumentReference", {"patient": patient_id})
            if docs and docs.get("entry"):
                for entry in docs["entry"][:5]:  # Limit to 5 most recent
                    doc = entry["resource"]
                    content_list = doc.get("content", [])
                    if content_list:
                        attach = content_list[0].get("attachment", {})
                        # Try to get text content
                        if attach.get("data"):
                            import base64
                            try:
                                text = base64.b64decode(attach["data"]).decode("utf-8", errors="ignore")
                                data.clinical_notes.append(text[:2000])  # Limit length
                            except:
                                pass
        
        return data


class MidnightReasonGenerator:
    """
    Generates MidnightReason justifications using LLM.
    
    Uses patient data from Epic FHIR to generate:
    - Patient background paragraph
    - MidnightReason1 (first midnight justification)
    - MidnightReason2 (second midnight justification)
    """
    
    def __init__(self, model_id: str = None, skip_epic: bool = True):
        import boto3
        self.model_id = model_id or BEDROCK_MODEL_ID
        self._boto3 = boto3  # Store module reference for creating fresh clients
        # Note: bedrock client is created fresh on each call to handle credential refresh
        
        # Only initialize Epic fetcher if needed (not for PDF-based flow)
        self._epic_fetcher = None
        self._skip_epic = skip_epic
        
        # Debug data capture
        self._last_input_conditions = []
        self._last_input_medications = []
        self._last_generated_text = ""
        self._last_patient_background = ""
        self._last_midnight_reason_1 = ""
        self._last_midnight_reason_2 = ""
        self._last_closing_summary = ""
        self._last_generation_timestamp = None
        self._last_validation_result = {}
    
    def _get_bedrock_client(self):
        """Create a fresh Bedrock client to pick up refreshed credentials."""
        return self._boto3.client(
            "bedrock-runtime",
            region_name=AWS_REGION,
            verify=False
        )
    
    def get_debug_data(self) -> dict:
        """
        Get debug data from the last generation for verification UI.
        
        Returns dict with:
            - input_conditions: Conditions provided to the LLM
            - input_medications: Medications provided to the LLM
            - generated_text: Full generated midnight reason text
            - conditions_used: Conditions mentioned in output that were in input
            - conditions_hallucinated: Conditions mentioned in output NOT in input
            - generation_timestamp: ISO timestamp
        """
        return {
            "input_conditions": self._last_input_conditions,
            "input_medications": self._last_input_medications,
            "generated_text": self._last_generated_text,
            "patient_background": self._last_patient_background,
            "midnight_reason_1": self._last_midnight_reason_1,
            "midnight_reason_2": self._last_midnight_reason_2,
            "closing_summary": self._last_closing_summary,
            "conditions_used": self._last_validation_result.get("conditions_used", []),
            "conditions_hallucinated": self._last_validation_result.get("conditions_hallucinated", []),
            "generation_timestamp": self._last_generation_timestamp,
            "source": "midnight-reason-generator"
        }
    
    def _validate_generation(self, generated_text: str, input_conditions: List[str], input_medications: List[str]) -> dict:
        """
        Validate that generated text only uses conditions/medications from input.
        
        Returns dict with conditions_used and conditions_hallucinated lists.
        """
        import re
        
        generated_lower = generated_text.lower()
        
        # Common condition terms to check for in generated text
        # These are medical terms that if mentioned should be in the input
        common_conditions = [
            "diabetes", "diabetic", "hypertension", "htn", "heart failure", "chf", "hfpef", "hfref",
            "copd", "asthma", "pneumonia", "uti", "urinary tract infection", "sepsis", "aki",
            "acute kidney injury", "ckd", "chronic kidney disease", "anemia", "afib", "atrial fibrillation",
            "cad", "coronary artery disease", "stroke", "cva", "tia", "dvt", "pe", "pulmonary embolism",
            "gerd", "cirrhosis", "hepatitis", "pancreatitis", "cholecystitis", "diverticulitis",
            "cellulitis", "osteomyelitis", "endocarditis", "meningitis", "encephalopathy",
            "hyperlipidemia", "hld", "obesity", "hypothyroidism", "hyperthyroidism",
            "fibromyalgia", "lupus", "rheumatoid arthritis", "osteoarthritis", "gout",
            "depression", "anxiety", "dementia", "alzheimer", "parkinson", "multiple sclerosis",
            "epilepsy", "seizure", "cancer", "malignancy", "leukemia", "lymphoma"
        ]
        
        # Build set of normalized input conditions
        input_conditions_lower = set()
        for cond in input_conditions:
            cond_lower = cond.lower()
            input_conditions_lower.add(cond_lower)
            # Add abbreviations/variations
            if "hypertension" in cond_lower:
                input_conditions_lower.add("htn")
            if "heart failure" in cond_lower or "chf" in cond_lower:
                input_conditions_lower.add("chf")
                input_conditions_lower.add("heart failure")
                input_conditions_lower.add("hfpef")
                input_conditions_lower.add("hfref")
            if "diabetes" in cond_lower:
                input_conditions_lower.add("dm")
                input_conditions_lower.add("diabetic")
            if "atrial fibrillation" in cond_lower:
                input_conditions_lower.add("afib")
            if "chronic kidney" in cond_lower:
                input_conditions_lower.add("ckd")
            if "coronary artery" in cond_lower:
                input_conditions_lower.add("cad")
            if "copd" in cond_lower or "chronic obstructive" in cond_lower:
                input_conditions_lower.add("copd")
        
        conditions_used = []
        conditions_hallucinated = []
        
        for term in common_conditions:
            if term in generated_lower:
                # Check if this term or its variants are in input
                found_in_input = False
                for input_cond in input_conditions_lower:
                    if term in input_cond or input_cond in term:
                        found_in_input = True
                        break
                
                if found_in_input:
                    conditions_used.append(term)
                else:
                    conditions_hallucinated.append(term)
        
        return {
            "conditions_used": list(set(conditions_used)),
            "conditions_hallucinated": list(set(conditions_hallucinated))
        }
    
    @property
    def epic_fetcher(self):
        """Lazy initialization of EpicDataFetcher."""
        if self._epic_fetcher is None and not self._skip_epic:
            self._epic_fetcher = EpicDataFetcher()
        return self._epic_fetcher
    
    def _call_llm(self, system_prompt: str, user_prompt: str, 
                  temperature: float = 0.3, max_tokens: int = 2000,
                  trace_name: str = "midnight-reason",
                  patient_context: dict = None) -> tuple:
        """Call AWS Bedrock Claude model.
        
        Args:
            patient_context: Optional dict with patient data for Langfuse tracing.
                             Should include conditions, medications, labs for faithfulness checks.
        
        Returns:
            Tuple of (output_text, langfuse_trace or None)
        """
        request_body = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": max_tokens,
            "temperature": temperature,
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_prompt}]
        }
        
        # Create fresh client to pick up refreshed AWS credentials
        bedrock = self._get_bedrock_client()
        response = bedrock.invoke_model(
            modelId=self.model_id,
            body=json.dumps(request_body),
            contentType="application/json",
            accept="application/json"
        )
        
        response_body = json.loads(response["body"].read())
        output_text = response_body["content"][0]["text"]
        
        # Log to Langfuse if enabled (v2 API)
        trace = None
        if langfuse_client:
            try:
                usage = response_body.get("usage", {})
                # Use patient_context if provided for better faithfulness evaluation
                if patient_context:
                    input_data = {
                        "patient_data": patient_context,
                        "prompt_preview": user_prompt[:500]
                    }
                else:
                    input_data = {"system": system_prompt[:1000], "user": user_prompt[:1000]}
                
                # Create main trace
                trace = langfuse_client.trace(
                    name=trace_name,
                    input=input_data,
                    output={"response": output_text},
                    metadata={"model": self.model_id, "temperature": temperature}
                )
                
                # Main LLM generation span
                trace.generation(
                    name="llm-call",
                    model=self.model_id,
                    input=input_data,
                    output=output_text,
                    usage={
                        "input": usage.get("input_tokens", 0),
                        "output": usage.get("output_tokens", 0)
                    }
                )
                
                logger.info("Langfuse trace created")
            except Exception as e:
                logger.error(f"Langfuse error: {e}")
        
        return output_text, trace
    
    def _log_components_to_langfuse(self, trace, result: dict, patient_context: dict = None):
        """Log each generated component as a separate TRACE in Langfuse.
        
        This creates separate traces for LLM evaluators to pick up:
        - midnight_reason_1 -> appears in Traces tab
        - midnight_reason_2 -> appears in Traces tab
        - closing_summary -> appears in Traces tab
        
        Note: Hook is evaluated separately via pdf_chart_parser
        """
        if not langfuse_client:
            return
        
        try:
            # Format patient context as input for evaluators
            # Limit lists to prevent truncation in Langfuse
            input_data = {}
            if patient_context:
                input_data = {
                    "patient_name": patient_context.get('patient_name', ''),
                    "discharge_date": patient_context.get('discharge_date', ''),
                    "conditions": patient_context.get('conditions', [])[:10],  # Limit to 10
                    "medications": patient_context.get('medications', [])[:15],  # Limit to 15
                    "lab_results": patient_context.get('lab_results', [])[:15],  # Limit to 15
                    "chief_complaint": patient_context.get('chief_complaint', '')[:500],  # Limit chars
                    "consults": patient_context.get('consults', [])[:5],
                    "procedures": patient_context.get('procedures', [])[:5]
                }
                logger.info(f"Langfuse input_data consults: {input_data.get('consults', [])}")
                logger.info(f"Langfuse input_data procedures: {input_data.get('procedures', [])}")
            
            # Component definitions: (id, display_name)
            components = [
                ("midnight_reason_1", "Midnight Reason 1"),
                ("midnight_reason_2", "Midnight Reason 2"),
                ("closing_summary", "Closing Summary"),
            ]
            
            for comp_id, comp_name in components:
                comp_text = result.get(comp_id, "")
                if comp_text:
                    # Create a separate TRACE for each component
                    # This makes them show up in Traces tab for evaluator matching
                    comp_trace = langfuse_client.trace(
                        name=comp_id,  # e.g., "midnight_reason_1"
                        input=input_data,  # Direct dict, not wrapped
                        output=comp_text,  # Direct string, not wrapped
                        metadata={
                            "component": comp_id,
                            "component_name": comp_name
                        }
                    )
                    
                    # Also add a generation under this trace
                    generation = comp_trace.generation(
                        name="generation",
                        model="claude-sonnet-4-20250514",
                        input=input_data,
                        output=comp_text
                    )
                    generation.end()
                    
                    # Add heuristic scores to the component trace
                    text_len = len(comp_text)
                    
                    if comp_id == "midnight_reason_1":
                        expected_len = 300
                        keywords = ["management", "monitoring", "medically", "necessary", "requiring"]
                    elif comp_id == "midnight_reason_2":
                        expected_len = 300
                        keywords = ["continued", "IV", "monitoring", "treatment", "2MN", "inpatient"]
                    else:  # closing_summary
                        expected_len = 150
                        keywords = ["hospitalization", "required", "continued", "treatment"]
                    
                    length_score = min(1.0, text_len / expected_len)
                    comp_trace.score(
                        name="length",
                        value=length_score,
                        comment=f"{comp_name}: {text_len} chars (expected {expected_len}+)"
                    )
                    
                    keywords_found = sum(1 for kw in keywords if kw.lower() in comp_text.lower())
                    comp_trace.score(
                        name="relevance",
                        value=keywords_found / len(keywords),
                        comment=f"{comp_name}: {keywords_found}/{len(keywords)} keywords"
                    )
            
            langfuse_client.flush()
            logger.info("Langfuse component traces logged")
        except Exception as e:
            logger.error(f"Langfuse component logging error: {e}")
    
    def _format_patient_data(self, data: PatientStayData) -> str:
        """Format patient data for the LLM prompt."""
        lines = []
        
        lines.append("## PATIENT DEMOGRAPHICS")
        lines.append(f"Name: {data.patient_name}")
        lines.append(f"Age: {data.age} years old")
        lines.append(f"Gender: {data.gender}")
        lines.append(f"DOB: {data.dob}")
        lines.append("")
        
        lines.append("## ADMISSION INFORMATION")
        lines.append(f"Admission Date: {data.admission_date}")
        lines.append(f"Discharge Date: {data.discharge_date if data.discharge_date else '(still admitted)'}")
        lines.append(f"Chief Complaint: {data.chief_complaint or 'Not specified'}")
        lines.append(f"Encounter Status: {data.encounter_status}")
        lines.append("")
        
        lines.append("## DIAGNOSES/CONDITIONS")
        if data.conditions:
            for cond in data.conditions:
                lines.append(f"- {cond}")
        else:
            lines.append("No conditions documented")
        lines.append("")
        
        lines.append("## LABORATORY RESULTS")
        if data.lab_results:
            for lab in data.lab_results:
                flag = f" ({lab['flag']})" if lab.get("flag") else ""
                lines.append(f"- {lab['name']}: {lab['value']} {lab['unit']}{flag}")
        else:
            lines.append("No laboratory results available")
        lines.append("")
        
        lines.append("## MEDICATIONS")
        if data.medications:
            for med in data.medications:
                route = f" ({med['route']})" if med.get("route") else ""
                lines.append(f"- {med['name']}{route}")
        else:
            lines.append("No medications documented (API may not be enabled)")
        lines.append("")
        
        lines.append("## CONSULTS")
        if data.consults:
            for consult in data.consults:
                lines.append(f"- {consult}")
        else:
            lines.append("No consults documented")
        lines.append("")
        
        lines.append("## PROCEDURES")
        if data.procedures:
            for procedure in data.procedures:
                lines.append(f"- {procedure}")
        else:
            lines.append("No procedures documented")
        lines.append("")
        
        if data.clinical_notes:
            lines.append("## CLINICAL NOTES")
            for i, note in enumerate(data.clinical_notes, 1):
                lines.append(f"Note {i}:")
                lines.append(note[:1500])
                lines.append("")
        
        return "\n".join(lines)
    
    def generate(self, patient_id: str) -> MidnightReasonOutput:
        """
        Generate MidnightReason justifications for a patient.
        
        Args:
            patient_id: Epic FHIR Patient ID
            
        Returns:
            MidnightReasonOutput with generated justifications
        """
        # Fetch patient data from Epic
        patient_data = self.epic_fetcher.fetch_patient_stay_data(patient_id)
        formatted_data = self._format_patient_data(patient_data)
        
        # Determine data sources
        data_sources = ["Patient", "Encounter", "Condition"]
        if patient_data.lab_results:
            data_sources.append("Observation (Labs)")
        if patient_data.medications:
            data_sources.append("MedicationRequest")
        if patient_data.clinical_notes:
            data_sources.append("DocumentReference")
        
        # Generate MidnightReason content
        system_prompt = """You are a clinical documentation specialist generating Medicare appeal justifications.

Your task is to generate three components for a Medicare inpatient appeal letter:

1. **Patient Background Paragraph**: A single paragraph summarizing the patient's demographics, past medical history (using standard abbreviations like HTN, DM, COPD, CHF, etc.), and reason for presenting to the emergency department.

2. **MidnightReason1** (First Midnight): Generate content that completes the sentence "During the first midnight, the member continued to require inpatient care for..."
   - DO NOT start with "During the first midnight" - the template adds this prefix
   - Primary diagnosis/reason requiring management
   - Specific medications with route prefix for EVERY medication (IV furosemide, PO metoprolol, SC enoxaparin, etc.) - NEVER list a medication without its route
   - Abnormal laboratory findings with specific values (elevated troponin 0.08 ng/mL, eGFR 48 mL/min, etc.)
   - Imaging findings or pending cultures if available
   - Monitoring requirements (continuous telemetry, pulse oximetry, etc.)
   - MUST end with '...and remained unsafe for discharge due to [specific clinical reason].'

3. **MidnightReason2** (Second Midnight): Generate content that completes the sentence "During the second midnight, the member still required hospital-level care due to..."
   - DO NOT start with "During the second midnight" - the template adds this prefix
   - Show PROGRESSION from first midnight (continued, persistent, worsening, pending results)
   - Continued medications with routes (IV antibiotics, PO diuretics, etc.)
   - Serial labs showing trends if available
   - Therapy evaluations (PT evaluation, OT evaluation)
   - Consultations (cardiology, nephrology, etc.)
   - Discharge planning complexity (Case Management, Social Work engagement)
   - MUST end with '...and inability to safely transition to a lower level of care because [specific clinical reason].'

4. **Closing Summary**: A brief closing paragraph about the patient's current status:
   - If discharge_date is blank (patient still admitted): Start with "[Patient name] continues to require hospitalization for [primary condition]. Ongoing treatment includes [key treatments]."
   - If discharge_date is provided (patient discharged): Start with "[Patient name] required continued hospitalization through [discharge date] for [primary condition]. Treatment included [key treatments]."
   - This paragraph should summarize the clinical justification for the entire hospital stay.

CRITICAL FORMATTING - MANDATORY SENTENCE ENDINGS:
- MidnightReason1 must NOT start with 'During the first midnight' - the template adds this prefix
- MidnightReason1 FINAL SENTENCE MUST literally end with the words '...and remained unsafe for discharge due to [specific reason].'
  CORRECT: "...and remained unsafe for discharge due to ongoing hemodynamic instability."
  WRONG: "...need for continuous respiratory monitoring and inpatient-level bronchodilator therapy."
  WRONG: Any ending that does not contain the exact phrase "remained unsafe for discharge due to"
- MidnightReason2 must NOT start with 'During the second midnight' - the template adds this prefix
- MidnightReason2 FINAL SENTENCE MUST literally end with '...and inability to safely transition to a lower level of care because [specific reason].'
  CORRECT: "...and inability to safely transition to a lower level of care because of persistent oxygen requirements."
  WRONG: Any ending that does not contain the exact phrase "inability to safely transition to a lower level of care because"

TONE - AVOID SENSATIONALIZED LANGUAGE:
- Do NOT use dramatic phrases like: "life-threatening", "extreme", "critical", "urgent emergent", "highly unstable", "severe, life-threatening", "extreme hemodynamic stress"
- Do NOT stack multiple dramatic adjectives ("complex, highly unstable interplay")
- Use factual, measured clinical language - let the lab values and clinical findings speak for themselves
- Say "severe anemia (Hgb 6.1 g/dL)" NOT "severe, life-threatening anemia"
- Say "required blood transfusion" NOT "urgent blood product transfusion"
- Say "neurological monitoring" NOT "monitoring for rapid progression"
- Medicare reviewers are physicians - overly dramatic language undermines credibility

Use formal medical language appropriate for Medicare appeals. Reference specific lab values, medication names, and clinical findings.

Output Format (JSON):
{
    "patient_background": "...",
    "midnight_reason_1": "...",
    "midnight_reason_2": "..."
}"""

        user_prompt = f"""Generate MidnightReason justifications based on the following patient data:

{formatted_data}

Generate the patient background paragraph, MidnightReason1, and MidnightReason2 as a JSON object.

CRITICAL FORMATTING RULES:
- midnight_reason_1 must NOT start with "During the first midnight" - the template adds this prefix
- midnight_reason_1 FINAL SENTENCE MUST end with the exact phrase "...and remained unsafe for discharge due to [specific clinical reason]."
- midnight_reason_2 must NOT start with "During the second midnight" - the template adds this prefix
- midnight_reason_2 FINAL SENTENCE MUST end with the exact phrase "...and inability to safely transition to a lower level of care because [specific clinical reason]."
- midnight_reason_2 should show PROGRESSION from Day 1 (continued, persistent, pending results, worsening/improving)
- DO NOT end with generic phrases about "need for monitoring" or "inpatient-level therapy" - use the EXACT required endings above

FAITHFULNESS - YOU MAY ONLY USE:
- Conditions from the CONDITIONS list above - do NOT invent or infer additional diagnoses
- Medications from the MEDICATIONS list above - do NOT mention medications not in the list (e.g., if no steroids in list, do NOT say "steroid therapy")
- Lab values from the LAB RESULTS list above
- Consults from the CONSULTS list above - include specialty recommendations and findings
- Procedures from the PROCEDURES list above - include completed and planned procedures (e.g., wound debridement, planned amputation)
- If a medication class is implied by chief complaint but not in MEDICATIONS list, use hedging: "pain management as clinically indicated" NOT "IV opioids"
- If a condition is not in the list, do NOT mention it even if it seems clinically related

CLINICAL INFERENCES - LABEL THEM CLEARLY:
- If lab values suggest a condition not explicitly diagnosed (e.g., elevated creatinine in CKD patient suggesting acute worsening), use hedging language:
  - "findings consistent with [condition]"
  - "laboratory values suggestive of [condition]"
  - "creatinine elevation concerning for acute-on-chronic kidney injury"
- Do NOT state inferred diagnoses as documented facts
- Let the physician reviewer make the final clinical determination
- Example: Say "CKD stage 3 with creatinine 2.8 mg/dL, findings consistent with acute exacerbation" NOT "acute on chronic kidney injury"

REQUIRED LANGUAGE:
- Include "medically necessary" somewhere in midnight_reason_1
- Include "inpatient level of care" or similar somewhere in the output

Generate COMPLETE paragraphs - these will be used directly in the appeal letter.

If specific data is not available (like medications or imaging), use clinically reasonable language to indicate what would typically be required for the documented conditions.
For conditions like pneumonia, assume appropriate IV antibiotics. For pain, assume appropriate analgesia.
Follow the format and style of formal Medicare appeal letters."""

        # Create patient context for Langfuse faithfulness evaluation
        patient_context = {
            "patient_name": patient_data.patient_name,
            "discharge_date": patient_data.discharge_date,
            "conditions": patient_data.conditions,
            "medications": [m.get("name", "") for m in patient_data.medications],
            "lab_results": [f"{l.get('name', '')}: {l.get('value', '')} {l.get('unit', '')}" for l in patient_data.lab_results],
            "chief_complaint": patient_data.chief_complaint,
            "consults": patient_data.consults,
            "procedures": patient_data.procedures
        }

        response, trace = self._call_llm(system_prompt, user_prompt, temperature=0.3, max_tokens=2000, patient_context=patient_context)
        
        # Parse JSON response
        try:
            # Try to extract JSON from the response
            json_start = response.find("{")
            json_end = response.rfind("}") + 1
            if json_start >= 0 and json_end > json_start:
                result = json.loads(response[json_start:json_end])
            else:
                result = json.loads(response)
        except json.JSONDecodeError:
            # If parsing fails, use the raw response
            result = {
                "patient_background": response,
                "midnight_reason_1": "",
                "midnight_reason_2": ""
            }
        
        # Log each component separately to Langfuse
        self._log_components_to_langfuse(trace, result, patient_context)
        
        # Fix a/an grammar in patient background
        patient_bg = fix_a_an_grammar(result.get("patient_background", ""))
        
        return MidnightReasonOutput(
            patient_background=patient_bg,
            midnight_reason_1=result.get("midnight_reason_1", ""),
            midnight_reason_2=result.get("midnight_reason_2", ""),
            closing_summary=result.get("closing_summary", ""),
            generated_at=datetime.now().isoformat(),
            model_used=self.model_id,
            data_sources=data_sources
        )
    
    def generate_from_data(self, patient_data: PatientStayData) -> MidnightReasonOutput:
        """
        Generate MidnightReason from pre-fetched patient data.
        
        Useful when you've already fetched the data or want to test with mock data.
        """
        formatted_data = self._format_patient_data(patient_data)
        
        # Same generation logic as generate()
        system_prompt = """You are a clinical documentation specialist generating Medicare appeal justifications.

Your task is to generate three components for a Medicare inpatient appeal letter:

1. **Patient Background Paragraph**: A single paragraph with:
   - Patient demographics: "[Name] is an 81-year-old female..." or "[Name] is a 65-year-old male..."
   - GRAMMAR RULE: Use "an" before ages starting with 8, 11, 18, 80-89 (sounds like "eight", "eleven", "eighteen", "eighty"). Use "a" for all other ages.
   - Past medical history using abbreviations (HTN, DM, COPD, CHF, CKD, HLD, CAD, AFib, etc.)
   - Reason for presenting to the ED

2. **MidnightReason1** (First Midnight): Generate a COMPLETE paragraph that starts with 'During the first midnight, the patient continued to require inpatient care for [condition]'. Include:
   - Primary diagnoses with INLINE lab values: "acute kidney injury (Creatinine 1.47 mg/dL, eGFR 36 mL/min/1.73m²)"
   - Specific pathogen if culture available: "urinary tract infection (Klebsiella pneumoniae)"
   - Lab findings inline: "anemia (Hgb 9.7 g/dL)", "cardiac strain (Troponin I 46 ng/L)"
   - Specific medications with route prefix for EVERY medication (IV furosemide, PO metoprolol, SC enoxaparin, etc.) - NEVER list a medication without its route
   - Clinical balancing acts: "required balancing Furosemide for HFpEF while protecting kidneys from AKI"
   - Monitoring: telemetry, serial labs
   - End with '...and remained unsafe for discharge due to [specific clinical reason].'

3. **MidnightReason2** (Second Midnight): Generate a COMPLETE paragraph that starts with 'During the second midnight, the patient still required hospital-level care due to [condition]'. Include:
   - Show PROGRESSION from Day 1 (continued, persistent, pending results, worsening/improving)
   - Continued medications with routes: "continuation of IV Ceftriaxone for [organism]", "PO diuretics"
   - Serial labs showing trends if available
   - Required consultations: "Cardiology consultation for elevated troponin and CHF, Nephrology consideration for AKI"
   - PT/OT evaluations with clinical context: "PT/OT evaluations for 3-week progressive weakness and high fall risk"
   - Discharge complexity: "Case Management and Social Work actively engaged in coordinating complex discharge plan due to [specific barriers]"
   - End with '...and inability to safely transition to a lower level of care because [specific clinical reason].'

4. **Closing Summary**: Generate a COMPLETE closing sentence:
   - MUST start with "In summary, the patient required continued inpatient hospitalization through the denied period for..."
   - Include the primary conditions being treated
   - Include key ongoing treatments (IV antibiotics, pain control, PT/OT, etc.)
   - MUST end with "...and the inpatient stay should be approved as medically necessary."
   - Should be 1-2 sentences maximum

CRITICAL FORMATTING - MANDATORY SENTENCE ENDINGS:
- MidnightReason1 must NOT start with 'During the first midnight' - the template adds this prefix
- MidnightReason1 FINAL SENTENCE MUST literally end with '...and remained unsafe for discharge due to [specific reason].'
  CORRECT: "...and remained unsafe for discharge due to ongoing hemodynamic instability."
  WRONG: "...need for continuous respiratory monitoring and inpatient-level bronchodilator therapy."
- MidnightReason2 must NOT start with 'During the second midnight' - the template adds this prefix
- MidnightReason2 FINAL SENTENCE MUST literally end with '...and inability to safely transition to a lower level of care because [specific reason].'
  CORRECT: "...and inability to safely transition to a lower level of care because of persistent oxygen requirements."
- Closing Summary MUST start with "In summary, the patient required continued inpatient hospitalization through the denied period for..."
- Closing Summary MUST end with "...and the inpatient stay should be approved as medically necessary."
- Include specific lab values inline with diagnoses, not just listed separately
- Reference specific pathogens from cultures when available
- Explain clinical reasoning for treatment decisions

FAITHFULNESS - CRITICAL:
- ONLY use conditions that appear in the provided CONDITIONS list - do NOT add or infer conditions
- If "fibromyalgia" is not in the conditions list, do NOT mention fibromyalgia
- If "diabetes" is not in the conditions list, do NOT mention diabetes
- Copy condition names EXACTLY as they appear in the input
- You may use standard abbreviations (HTN for hypertension, CHF for heart failure, etc.)
- Lab values, medications, and other clinical data must come from the provided data

MEDICAL NECESSITY LANGUAGE - REQUIRED:
- MUST include the phrase "medically necessary" or "medical necessity" at least once in midnight_reason_1
- Include language like: "This level of care was medically necessary because..." or "inpatient admission was medically necessary for..."
- Include: "could not be safely performed in a lower level of care" or "required inpatient level of care"

TONE - AVOID SENSATIONALIZED LANGUAGE:
- Do NOT use dramatic phrases like: "life-threatening", "extreme", "critical", "urgent emergent", "highly unstable", "severe, life-threatening", "extreme hemodynamic stress"
- Do NOT stack multiple dramatic adjectives ("complex, highly unstable interplay")
- Use factual, measured clinical language - let the lab values and clinical findings speak for themselves
- Say "severe anemia (Hgb 6.1 g/dL)" NOT "severe, life-threatening anemia"
- Say "required blood transfusion" NOT "urgent blood product transfusion"
- Say "neurological monitoring" NOT "monitoring for rapid progression"
- Medicare reviewers are physicians - overly dramatic language undermines credibility

Output Format (JSON):
{
    "patient_background": "...",
    "midnight_reason_1": "...",
    "midnight_reason_2": "...",
    "closing_summary": "..."
}"""

        user_prompt = f"""Generate MidnightReason justifications based on the following patient data:

{formatted_data}

Generate the patient background paragraph, MidnightReason1, MidnightReason2, and closing_summary as a JSON object.

CRITICAL FORMATTING RULES:
- midnight_reason_1 must NOT start with "During the first midnight" - the template adds this prefix
- midnight_reason_1 FINAL SENTENCE MUST end with the exact phrase "...and remained unsafe for discharge due to [specific clinical reason]."
- midnight_reason_2 must NOT start with "During the second midnight" - the template adds this prefix
- midnight_reason_2 FINAL SENTENCE MUST end with the exact phrase "...and inability to safely transition to a lower level of care because [specific clinical reason]."
- midnight_reason_2 should show PROGRESSION from Day 1 (continued, persistent, pending results, worsening/improving)
- closing_summary MUST start with "In summary, the patient required continued inpatient hospitalization through the denied period for..."
- closing_summary MUST end with "...and the inpatient stay should be approved as medically necessary."
- closing_summary SHOULD include: key procedures performed (e.g., wound debridement), abnormal lab values supporting severity (e.g., elevated CRP/ESR for infection), and significant comorbidities that complicate care
- DO NOT end midnight paragraphs with generic phrases about "need for monitoring" or "inpatient-level therapy" - use the EXACT required endings above

FAITHFULNESS - YOU MAY ONLY USE:
- Conditions from the CONDITIONS list above - do NOT invent or infer additional diagnoses
- Medications from the MEDICATIONS list above - do NOT mention medications not in the list (e.g., if no steroids in list, do NOT say "steroid therapy")
- Lab values from the LAB RESULTS list above
- Consults from the CONSULTS list above - include specialty recommendations and findings
- Procedures from the PROCEDURES list above - include completed and planned procedures (e.g., wound debridement, planned amputation)
- If a medication class is implied by chief complaint but not in MEDICATIONS list, use hedging: "pain management as clinically indicated" NOT "IV opioids"
- If a condition is not in the list, do NOT mention it even if it seems clinically related

CLINICAL INFERENCES - LABEL THEM CLEARLY:
- If lab values suggest a condition not explicitly diagnosed (e.g., elevated creatinine in CKD patient suggesting acute worsening), use hedging language:
  - "findings consistent with [condition]"
  - "laboratory values suggestive of [condition]"
  - "creatinine elevation concerning for acute-on-chronic kidney injury"
- Do NOT state inferred diagnoses as documented facts
- Let the physician reviewer make the final clinical determination
- Example: Say "CKD stage 3 with creatinine 2.8 mg/dL, findings consistent with acute exacerbation" NOT "acute on chronic kidney injury"

REQUIRED LANGUAGE:
- Include "medically necessary" somewhere in midnight_reason_1
- Include "inpatient level of care" or similar somewhere in the output

Generate COMPLETE paragraphs - these will be used directly in the appeal letter.

If specific data is not available (like medications or imaging), use clinically reasonable language to indicate what would typically be required for the documented conditions.
Follow the format and style of formal Medicare appeal letters."""

        # Create patient context for Langfuse faithfulness evaluation
        patient_context = {
            "patient_name": patient_data.patient_name,
            "discharge_date": patient_data.discharge_date,
            "conditions": patient_data.conditions,
            "medications": [m.get("name", "") for m in patient_data.medications],
            "lab_results": [f"{l.get('name', '')}: {l.get('value', '')} {l.get('unit', '')}" for l in patient_data.lab_results],
            "chief_complaint": patient_data.chief_complaint,
            "consults": patient_data.consults,
            "procedures": patient_data.procedures
        }

        response, trace = self._call_llm(system_prompt, user_prompt, temperature=0.3, max_tokens=2000, patient_context=patient_context)
        
        # Parse JSON response
        try:
            json_start = response.find("{")
            json_end = response.rfind("}") + 1
            if json_start >= 0 and json_end > json_start:
                result = json.loads(response[json_start:json_end])
            else:
                result = json.loads(response)
        except json.JSONDecodeError:
            result = {
                "patient_background": response,
                "midnight_reason_1": "",
                "midnight_reason_2": ""
            }
        
        # Log each component separately to Langfuse
        self._log_components_to_langfuse(trace, result, patient_context)
        
        # Determine data sources
        data_sources = ["Patient", "Encounter", "Condition"]
        if patient_data.lab_results:
            data_sources.append("Observation (Labs)")
        if patient_data.medications:
            data_sources.append("MedicationRequest")
        if patient_data.clinical_notes:
            data_sources.append("DocumentReference")
        
        # Fix a/an grammar in patient background
        patient_bg = fix_a_an_grammar(result.get("patient_background", ""))
        
        # Capture debug data for verification UI
        generated_text = f"{patient_bg}\n\nMidnightReason1: {result.get('midnight_reason_1', '')}\n\nMidnightReason2: {result.get('midnight_reason_2', '')}"
        self._last_input_conditions = patient_data.conditions
        self._last_input_medications = [m.get("name", "") for m in patient_data.medications]
        self._last_generated_text = generated_text
        self._last_patient_background = patient_bg
        self._last_midnight_reason_1 = result.get('midnight_reason_1', '')
        self._last_midnight_reason_2 = result.get('midnight_reason_2', '')
        self._last_closing_summary = result.get('closing_summary', '')
        self._last_generation_timestamp = datetime.now().isoformat()
        self._last_validation_result = self._validate_generation(
            generated_text, 
            patient_data.conditions,
            self._last_input_medications
        )
        
        return MidnightReasonOutput(
            patient_background=patient_bg,
            midnight_reason_1=result.get("midnight_reason_1", ""),
            midnight_reason_2=result.get("midnight_reason_2", ""),
            closing_summary=result.get("closing_summary", ""),
            generated_at=datetime.now().isoformat(),
            model_used=self.model_id,
            data_sources=data_sources
        )

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
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any
from pathlib import Path
from datetime import datetime

from config import (
    EPIC_BASE_URL, EPIC_CLIENT_ID, EPIC_TOKEN_URL, EPIC_PRIVATE_KEY_PATH,
    AWS_REGION, BEDROCK_MODEL_ID,
    LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY, LANGFUSE_HOST, LANGFUSE_ENABLED
)

# Disable SSL warnings for corporate networks
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Initialize Langfuse if configured
langfuse_client = None
if LANGFUSE_ENABLED:
    try:
        from langfuse import Langfuse
        langfuse_client = Langfuse(
            public_key=LANGFUSE_PUBLIC_KEY,
            secret_key=LANGFUSE_SECRET_KEY,
            host=LANGFUSE_HOST
        )
    except Exception:
        pass


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
    
    # Encounter info
    admission_date: str = ""
    observation_date: str = ""  # Date of observation status
    inpatient_date: str = ""    # Date transitioned to inpatient
    chief_complaint: str = ""
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
    
    def __init__(self, model_id: str = None):
        import boto3
        self.model_id = model_id or BEDROCK_MODEL_ID
        self.bedrock = boto3.client(
            "bedrock-runtime",
            region_name=AWS_REGION,
            verify=False
        )
        self.epic_fetcher = EpicDataFetcher()
    
    def _call_llm(self, system_prompt: str, user_prompt: str, 
                  temperature: float = 0.3, max_tokens: int = 2000,
                  trace_name: str = "midnight-reason") -> str:
        """Call AWS Bedrock Claude model."""
        request_body = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": max_tokens,
            "temperature": temperature,
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_prompt}]
        }
        
        response = self.bedrock.invoke_model(
            modelId=self.model_id,
            body=json.dumps(request_body),
            contentType="application/json",
            accept="application/json"
        )
        
        response_body = json.loads(response["body"].read())
        output_text = response_body["content"][0]["text"]
        
        # Log to Langfuse if enabled
        if langfuse_client:
            try:
                trace = langfuse_client.trace(
                    name=trace_name,
                    metadata={"model": self.model_id, "temperature": temperature}
                )
                trace.generation(
                    name="llm-call",
                    model=self.model_id,
                    input={"system": system_prompt[:500], "user": user_prompt[:500]},
                    output=output_text,
                    usage={
                        "input": response_body.get("usage", {}).get("input_tokens", 0),
                        "output": response_body.get("usage", {}).get("output_tokens", 0)
                    }
                )
            except Exception:
                pass
        
        return output_text
    
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

2. **MidnightReason1** (First Midnight): This will be inserted after "The first midnight was medically necessary for" - so start with "management of..." or similar phrasing. Include:
   - Primary diagnosis/reason requiring management
   - Specific medications administered with routes (IV hydromorphone, IV ondansetron, PO methocarbamol, etc.)
   - Abnormal laboratory findings with specific values (elevated troponin 0.08 ng/mL, etc.)
   - Imaging findings if available
   - Monitoring requirements (continuous telemetry, pulse oximetry, etc.)

3. **MidnightReason2** (Second Midnight): This will be inserted after "The second midnight was medically necessary for" - so start with a list of services/treatments. Include:
   - Continued IV medications and adjustments
   - Therapy evaluations (PT evaluation, OT evaluation)
   - Consultations (cardiology, wound care, etc.)
   - Discharge planning complexity (Case Management, Social Work engagement)
   - DO NOT end with "which meets 2MN" or similar - the template already includes that text

CRITICAL FORMATTING:
- MidnightReason1 should start with "management of [condition] requiring..." NOT "The first midnight..." or "due to..."
- MidnightReason2 should start with specific treatments/services like "IV [medication], continued [treatment], PT evaluation..." NOT "The second midnight..."

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
- midnight_reason_1 must start with "management of [condition] requiring..." (NOT "The first midnight..." or "due to...")
- midnight_reason_2 must start with treatment/service list like "IV [medication], continued..." (NOT "The second midnight..." or "due to...")

These will be inserted after "The first/second midnight was medically necessary for" so they must flow grammatically.

If specific data is not available (like medications or imaging), use clinically reasonable language to indicate what would typically be required for the documented conditions.
For conditions like pneumonia, assume appropriate IV antibiotics. For pain, assume appropriate analgesia.
Follow the format and style of formal Medicare appeal letters."""

        response = self._call_llm(system_prompt, user_prompt, temperature=0.3, max_tokens=2000)
        
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
        
        # Fix a/an grammar in patient background
        patient_bg = fix_a_an_grammar(result.get("patient_background", ""))
        
        return MidnightReasonOutput(
            patient_background=patient_bg,
            midnight_reason_1=result.get("midnight_reason_1", ""),
            midnight_reason_2=result.get("midnight_reason_2", ""),
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

2. **MidnightReason1** (First Midnight): This will be inserted after "The first midnight was medically necessary for" - start with "management of...". Include:
   - Primary diagnoses with INLINE lab values: "acute kidney injury (Creatinine 1.47 mg/dL, eGFR 36 mL/min/1.73m²)"
   - Specific pathogen if culture available: "urinary tract infection (Klebsiella pneumoniae)"
   - Lab findings inline: "anemia (Hgb 9.7 g/dL)", "cardiac strain (Troponin I 46 ng/L)"
   - Specific IV medications with clinical reasoning
   - Clinical balancing acts: "required balancing Furosemide for HFpEF while protecting kidneys from AKI"
   - Monitoring: telemetry, serial labs
   - End with why inpatient level required: "continuous physician oversight that could not be safely performed in a lower level of care"

3. **MidnightReason2** (Second Midnight): Start with specific treatments. Include:
   - Continued IV medications: "continuation of IV Ceftriaxone for [organism]"
   - Pain/symptom management with medication names
   - Required consultations: "Cardiology consultation for elevated troponin and CHF, Nephrology consideration for AKI"
   - PT/OT evaluations with clinical context: "PT/OT evaluations for 3-week progressive weakness and high fall risk"
   - Discharge complexity: "Case Management and Social Work actively engaged in coordinating complex discharge plan due to [specific barriers]"
   - DO NOT end with "which meets 2MN" or similar - the template already includes that text

CRITICAL FORMATTING:
- MidnightReason1 starts with "management of [condition(s)] with inline lab values..."
- MidnightReason2 starts with "continuation of [treatment]..."
- Include specific lab values inline with diagnoses, not just listed separately
- Reference specific pathogens from cultures when available
- Explain clinical reasoning for treatment decisions

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
    "midnight_reason_2": "..."
}"""

        user_prompt = f"""Generate MidnightReason justifications based on the following patient data:

{formatted_data}

Generate the patient background paragraph, MidnightReason1, and MidnightReason2 as a JSON object.

CRITICAL FORMATTING RULES:
- midnight_reason_1 must start with "management of [condition] requiring..." (NOT "The first midnight..." or "due to...")
- midnight_reason_2 must start with treatment/service list like "IV [medication], continued..." (NOT "The second midnight..." or "due to...")

These will be inserted after "The first/second midnight was medically necessary for" so they must flow grammatically.

If specific data is not available (like medications or imaging), use clinically reasonable language to indicate what would typically be required for the documented conditions.
Follow the format and style of formal Medicare appeal letters."""

        response = self._call_llm(system_prompt, user_prompt, temperature=0.3, max_tokens=2000)
        
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
        
        return MidnightReasonOutput(
            patient_background=patient_bg,
            midnight_reason_1=result.get("midnight_reason_1", ""),
            midnight_reason_2=result.get("midnight_reason_2", ""),
            generated_at=datetime.now().isoformat(),
            model_used=self.model_id,
            data_sources=data_sources
        )

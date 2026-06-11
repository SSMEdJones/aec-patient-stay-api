"""
PDF Chart Parser with De-identification

Extracts clinical data from PDF patient charts, de-identifies PHI,
and creates structured PatientStayData for testing.
"""
import re
import json
import random
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Dict, List
from dataclasses import dataclass, field, asdict

import pdfplumber
import boto3

from services.midnight_reason_generator import PatientStayData
from config import LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY, LANGFUSE_HOST, LANGFUSE_ENABLED

logger = logging.getLogger(__name__)

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


# Synthetic name pools for de-identification
FIRST_NAMES_F = ["Maria", "Jennifer", "Linda", "Patricia", "Elizabeth", "Susan", "Dorothy", "Helen", "Nancy", "Betty"]
FIRST_NAMES_M = ["James", "Robert", "Michael", "William", "David", "Richard", "Joseph", "Thomas", "Charles", "Daniel"]
LAST_NAMES = ["Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller", "Davis", "Rodriguez", "Martinez"]

# Medical abbreviation mappings (bidirectional)
MEDICAL_ABBREVIATIONS = {
    # Cardiovascular
    "chf": "congestive heart failure",
    "cad": "coronary artery disease",
    "afib": "atrial fibrillation",
    "a-fib": "atrial fibrillation",
    "af": "atrial fibrillation",
    "htn": "hypertension",
    "mi": "myocardial infarction",
    "cabg": "coronary artery bypass graft",
    "dvt": "deep vein thrombosis",
    "pe": "pulmonary embolism",
    "pvd": "peripheral vascular disease",
    "pad": "peripheral artery disease",
    "tia": "transient ischemic attack",
    "cva": "cerebrovascular accident",
    "stroke": "cerebrovascular accident",
    
    # Respiratory
    "copd": "chronic obstructive pulmonary disease",
    "sob": "shortness of breath",
    "osa": "obstructive sleep apnea",
    
    # Endocrine/Metabolic
    "dm": "diabetes mellitus",
    "dm2": "diabetes mellitus type 2",
    "t2dm": "type 2 diabetes mellitus",
    "iddm": "insulin dependent diabetes mellitus",
    "niddm": "non-insulin dependent diabetes mellitus",
    "hld": "hyperlipidemia",
    
    # Renal
    "ckd": "chronic kidney disease",
    "esrd": "end stage renal disease",
    "aki": "acute kidney injury",
    "arf": "acute renal failure",
    "bph": "benign prostatic hyperplasia",
    "uti": "urinary tract infection",
    
    # GI
    "gerd": "gastroesophageal reflux disease",
    "gi": "gastrointestinal",
    "gib": "gastrointestinal bleeding",
    "ugib": "upper gastrointestinal bleeding",
    "lgib": "lower gastrointestinal bleeding",
    "ibs": "irritable bowel syndrome",
    "ibd": "inflammatory bowel disease",
    
    # Neurological
    "ms": "multiple sclerosis",
    "als": "amyotrophic lateral sclerosis",
    "sz": "seizure",
    "loc": "loss of consciousness",
    
    # Psychiatric
    "mdd": "major depressive disorder",
    "gad": "generalized anxiety disorder",
    "ptsd": "post traumatic stress disorder",
    "adhd": "attention deficit hyperactivity disorder",
    
    # Other common
    "hx": "history",
    "pmh": "past medical history",
    "fx": "fracture",
    "ra": "rheumatoid arthritis",
    "oa": "osteoarthritis",
    "sle": "systemic lupus erythematosus",
    "hiv": "human immunodeficiency virus",
    "aids": "acquired immunodeficiency syndrome",
    "ca": "cancer",
    "chemo": "chemotherapy",
}

# Compound word equivalences (space vs no space, hyphenated variants)
COMPOUND_EQUIVALENCES = {
    "cerebrovascular": ["cerebral vascular", "cerebro-vascular", "cerebro vascular"],
    "cardiovascular": ["cardio vascular", "cardio-vascular"],
    "gastrointestinal": ["gastro intestinal", "gastro-intestinal", "gi"],
    "musculoskeletal": ["musculo skeletal", "musculo-skeletal"],
    "genitourinary": ["genito urinary", "genito-urinary", "gu"],
    "hepatobiliary": ["hepato biliary", "hepato-biliary"],
    "bronchopulmonary": ["broncho pulmonary", "broncho-pulmonary"],
    "nephrotoxic": ["nephro toxic", "nephro-toxic"],
    "cardiotoxic": ["cardio toxic", "cardio-toxic"],
    "neurovascular": ["neuro vascular", "neuro-vascular"],
    "atherosclerotic": ["athero sclerotic", "athero-sclerotic"],
    "hyperglycemia": ["hyper glycemia", "elevated glucose", "high blood sugar"],
    "hypoglycemia": ["hypo glycemia", "low blood sugar"],
    "hypertension": ["high blood pressure", "elevated blood pressure", "elevated bp"],
    "hypotension": ["low blood pressure"],
    "tachycardia": ["rapid heart rate", "elevated heart rate"],
    "bradycardia": ["slow heart rate", "low heart rate"],
    "dyspnea": ["shortness of breath", "difficulty breathing", "sob"],
}


@dataclass
class ExtractedChartData:
    """Raw extracted data before de-identification."""
    # Original PHI (will be replaced)
    original_name: str = ""
    original_dob: str = ""
    original_mrn: str = ""
    account_number: str = ""  # Account/encounter number from PDF
    original_address: str = ""
    original_ssn: str = ""
    original_phone: str = ""
    original_relatives: List[str] = field(default_factory=list)
    
    # Clinical data (kept)
    gender: str = ""
    age: int = 0
    admission_date: str = ""
    observation_date: str = ""  # Date of observation status
    inpatient_date: str = ""    # Date transitioned to inpatient
    place_of_service: str = ""  # emergency department, hospital, urgent care, etc.
    chief_complaint_short: str = ""  # Short symptom list: "abdominal pain, nausea, vomiting"
    chief_complaint: str = ""  # Full narrative for letter body
    hpi: str = ""
    conditions: List[str] = field(default_factory=list)
    medications: List[Dict] = field(default_factory=list)
    lab_results: List[Dict] = field(default_factory=list)
    vitals: Dict = field(default_factory=dict)
    assessment_plan: str = ""
    clinical_notes: List[str] = field(default_factory=list)
    
    # Insurance/Payer info
    insurance_name: str = ""  # Primary insurance/payer name
    insurance_id: str = ""    # Member ID from insurance
    insurance_group: str = "" # Group number
    insurance_address: str = "" # Payer street/PO Box
    insurance_city: str = ""    # Payer city
    insurance_state: str = ""   # Payer state
    insurance_zip: str = ""     # Payer zip
    
    # Facility info
    facility_name: str = ""
    attending_physician: str = ""


class PDFChartParser:
    """
    Parses PDF clinical charts, extracts data, and de-identifies PHI.
    
    Usage:
        parser = PDFChartParser()
        patient_data = parser.parse_and_deidentify("chart.pdf")
        debug_data = parser.get_debug_data()  # Access validation results
    """
    
    def __init__(self, use_llm: bool = True):
        """
        Args:
            use_llm: If True, use Claude for intelligent extraction. 
                     If False, use regex-based extraction only.
        """
        self.use_llm = use_llm
        if use_llm:
            self.bedrock = boto3.client("bedrock-runtime", region_name="us-east-1")
            self.model_id = "us.anthropic.claude-sonnet-4-20250514-v1:0"
        
        # Debug data storage - populated during extraction for verification
        self._last_source_text = ""
        self._last_faithfulness_result = {}
        self._last_extraction_timestamp = None
    
    def get_debug_data(self) -> dict:
        """
        Get debug data from the last extraction for verification UI.
        
        Returns dict with:
            - source_text: Full PDF text for searching
            - faithfulness_score: 0-1 score
            - conditions_validated: List of conditions found in source
            - conditions_flagged: List of conditions NOT found in source
            - medications_validated: List of medications found
            - medications_flagged: List of medications NOT found
            - lab_results_validated: List of lab results found in source
            - lab_results_flagged: List of lab results NOT found (possible hallucination)
            - extraction_timestamp: ISO timestamp
        """
        return {
            "source_text": self._last_source_text,
            "faithfulness_score": self._last_faithfulness_result.get("faithfulness_score", 0),
            "conditions_validated": self._last_faithfulness_result.get("conditions_validated", []),
            "conditions_flagged": self._last_faithfulness_result.get("conditions_flagged", []),
            "medications_validated": self._last_faithfulness_result.get("medications_validated", []),
            "medications_flagged": self._last_faithfulness_result.get("medications_flagged", []),
            "lab_results_validated": self._last_faithfulness_result.get("lab_results_validated", []),
            "lab_results_flagged": self._last_faithfulness_result.get("lab_results_flagged", []),
            "extraction_timestamp": self._last_extraction_timestamp,
            "details": self._last_faithfulness_result.get("details", []),
        }
    
    def extract_text(self, pdf_path: str) -> str:
        """Extract all text from PDF."""
        full_text = ""
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                text = page.extract_text() or ""
                full_text += text + "\n\n"
        return full_text
    
    def _validate_extraction_faithfulness(self, extracted_data: dict, source_text: str) -> dict:
        """
        Validate that extracted clinical terms appear in the source text.
        Flags potential hallucinations where LLM added information not in source.
        Allows bidirectional abbreviation matching (CHF ↔ congestive heart failure).
        
        Returns:
            dict with validation results and faithfulness score
        """
        source_lower = source_text.lower()
        
        # Build reverse lookup (expansion -> abbreviation)
        abbrev_to_expansion = MEDICAL_ABBREVIATIONS
        expansion_to_abbrev = {v: k for k, v in MEDICAL_ABBREVIATIONS.items()}
        
        # Build reverse lookup for compound equivalences
        compound_reverse = {}
        for main_term, variants in COMPOUND_EQUIVALENCES.items():
            for variant in variants:
                compound_reverse[variant] = main_term
        
        results = {
            "conditions_validated": [],
            "conditions_flagged": [],  # Not found in source - potential hallucination
            "medications_validated": [],
            "medications_flagged": [],
            "lab_results_validated": [],
            "lab_results_flagged": [],
            "faithfulness_score": 0.0,
            "details": []
        }
        
        def check_term_in_source(term: str) -> bool:
            """Check if term or its abbreviation/expansion appears in source."""
            term_lower = term.lower()
            
            # Direct match
            if term_lower in source_lower:
                return True
            
            # Check compound word equivalences (cerebrovascular ↔ cerebral vascular)
            if term_lower in COMPOUND_EQUIVALENCES:
                for variant in COMPOUND_EQUIVALENCES[term_lower]:
                    if variant in source_lower:
                        return True
            # Also check reverse (if extracted term is a variant)
            if term_lower in compound_reverse:
                main_term = compound_reverse[term_lower]
                if main_term in source_lower:
                    return True
            
            # Check if term is an abbreviation with expansion in source
            if term_lower in abbrev_to_expansion:
                expansion = abbrev_to_expansion[term_lower]
                if expansion in source_lower:
                    return True
            
            # Check if term is an expansion with abbreviation in source
            if term_lower in expansion_to_abbrev:
                abbrev = expansion_to_abbrev[term_lower]
                if abbrev in source_lower:
                    return True
            
            # Partial match - check if key words from term appear
            # (handles "diabetes mellitus type 2" matching "type 2 diabetes")
            words = [w for w in term_lower.split() if len(w) > 3]
            if words and all(word in source_lower for word in words):
                return True
            
            # Check abbreviation variants in the term
            for abbrev, expansion in abbrev_to_expansion.items():
                if abbrev in term_lower:
                    # Term contains abbreviation - check if expansion in source
                    if expansion in source_lower:
                        return True
                if expansion in term_lower:
                    # Term contains expansion - check if abbreviation in source  
                    if abbrev in source_lower:
                        return True
            
            return False
        
        # Check each condition against source
        for condition in extracted_data.get("conditions", []):
            # Remove common suffixes like "(HCC)" for matching
            clean_condition = re.sub(r'\s*\(hcc\).*', '', condition.lower()).strip()
            
            if check_term_in_source(clean_condition):
                results["conditions_validated"].append(condition)
            else:
                results["conditions_flagged"].append(condition)
                results["details"].append(f"Condition not in source: {condition}")
        
        # Check medications
        for med in extracted_data.get("medications", []):
            med_name = med.get("name", "").lower() if isinstance(med, dict) else str(med).lower()
            # Check first word of medication name (generic name)
            first_word = med_name.split()[0] if med_name.split() else ""
            if first_word and len(first_word) > 3 and first_word in source_lower:
                results["medications_validated"].append(med)
            elif med_name in source_lower:
                results["medications_validated"].append(med)
            else:
                results["medications_flagged"].append(med)
                results["details"].append(f"Medication not in source: {med_name}")
        
        # Check lab results - flag if numeric values exist but aren't in source
        # Must check that lab name AND value appear TOGETHER (within ~50 chars) to avoid
        # false positives where "4.5" appears in medication dosage but not as a lab value
        logger.info(f"Validating {len(extracted_data.get('lab_results', []))} lab results")
        for lab in extracted_data.get("lab_results", []):
            lab_name = lab.get("name", "").lower() if isinstance(lab, dict) else ""
            lab_value = str(lab.get("value", "")) if isinstance(lab, dict) else ""
            
            logger.debug(f"Checking lab: name='{lab_name}', value='{lab_value}'")
            
            # Skip empty values
            if not lab_value or lab_value.lower() in ["", "none", "n/a", "pending"]:
                logger.debug(f"Skipping lab '{lab_name}' - empty or pending value")
                continue
            
            # For numeric values, be STRICT - require name and value to appear together
            # This prevents "4.5" in "take 4.5mg metformin" from validating a fabricated lab
            is_numeric = lab_value.replace(".", "").replace("-", "").replace("<", "").replace(">", "").isdigit()
            
            if is_numeric and lab_name:
                # Build pattern: lab name within ~100 chars of value (either order)
                # Examples: "RBC: 4.5" or "4.5 (RBC)" or "RBC...4.5"
                escaped_name = re.escape(lab_name)
                escaped_value = re.escape(lab_value)
                
                # Check both orderings: name...value and value...name
                pattern1 = rf'{escaped_name}.{{0,100}}{escaped_value}'
                pattern2 = rf'{escaped_value}.{{0,100}}{escaped_name}'
                
                name_value_together = (
                    re.search(pattern1, source_lower, re.IGNORECASE) or 
                    re.search(pattern2, source_lower, re.IGNORECASE)
                )
                
                if name_value_together:
                    logger.info(f"Lab VALIDATED: {lab_name}={lab_value} (found together in source)")
                    results["lab_results_validated"].append(lab)
                else:
                    logger.warning(f"Lab FLAGGED: {lab_name}={lab_value} (NOT found together in source)")
                    results["lab_results_flagged"].append(lab)
                    results["details"].append(f"Lab value not found with name in source: {lab_name}={lab_value} (possible hallucination)")
            elif is_numeric:
                # Numeric but no name - flag it (can't verify without name)
                results["lab_results_flagged"].append(lab)
                results["details"].append(f"Lab value without verifiable name: {lab_value}")
            else:
                # Non-numeric values (like "positive", "negative") - check both name and value in source
                value_in_source = lab_value.lower() in source_lower
                name_in_source = lab_name in source_lower if lab_name else False
                
                if name_in_source and value_in_source:
                    results["lab_results_validated"].append(lab)
                else:
                    results["lab_results_flagged"].append(lab)
                    results["details"].append(f"Lab result not verifiable in source: {lab_name}={lab_value}")
        
        # Calculate overall faithfulness score
        total_conditions = len(extracted_data.get("conditions", []))
        total_meds = len(extracted_data.get("medications", []))
        total_labs = len(results["lab_results_validated"]) + len(results["lab_results_flagged"])
        total_items = total_conditions + total_meds + total_labs
        
        validated_items = len(results["conditions_validated"]) + len(results["medications_validated"]) + len(results["lab_results_validated"])
        
        if total_items > 0:
            results["faithfulness_score"] = validated_items / total_items
        else:
            results["faithfulness_score"] = 1.0  # No items to validate
        
        return results
    
    def _build_langfuse_excerpts(self, extracted_data: dict, source_text: str, max_chars: int = 25000) -> dict:
        """
        Build smart excerpts for Langfuse LLM-as-a-Judge.
        
        Instead of sending the first N chars (which misses labs at end of doc),
        we extract context around each extracted value so the judge can verify.
        
        Returns:
            dict with prioritized excerpts that fit within max_chars
        """
        source_lower = source_text.lower()
        excerpts = {
            "patient_demographics": "",
            "conditions_evidence": [],
            "medications_evidence": [],
            "lab_values_evidence": [],
            "summary": ""
        }
        
        used_chars = 0
        context_window = 150  # chars before/after each found term
        
        # 1. Patient demographics (first 1000 chars typically has name, DOB, MRN)
        excerpts["patient_demographics"] = source_text[:1000]
        used_chars += 1000
        
        # 2. Find evidence for each extracted condition
        for condition in extracted_data.get("conditions", [])[:15]:  # Top 15 conditions
            if used_chars >= max_chars:
                break
            cond_lower = condition.lower() if isinstance(condition, str) else ""
            if not cond_lower:
                continue
            idx = source_lower.find(cond_lower)
            if idx >= 0:
                start = max(0, idx - context_window)
                end = min(len(source_text), idx + len(cond_lower) + context_window)
                excerpt = source_text[start:end].strip()
                if excerpt not in excerpts["conditions_evidence"]:
                    excerpts["conditions_evidence"].append(f"...{excerpt}...")
                    used_chars += len(excerpt) + 10
        
        # 3. Find evidence for each medication
        for med in extracted_data.get("medications", [])[:12]:  # Top 12 meds
            if used_chars >= max_chars:
                break
            med_name = med.get("name", "").lower() if isinstance(med, dict) else ""
            if not med_name or len(med_name) < 3:
                continue
            idx = source_lower.find(med_name)
            if idx >= 0:
                start = max(0, idx - context_window)
                end = min(len(source_text), idx + len(med_name) + context_window)
                excerpt = source_text[start:end].strip()
                if excerpt not in excerpts["medications_evidence"]:
                    excerpts["medications_evidence"].append(f"...{excerpt}...")
                    used_chars += len(excerpt) + 10
        
        # 4. Find evidence for each lab value (CRITICAL - this is what Langfuse was missing)
        for lab in extracted_data.get("lab_results", [])[:20]:  # Top 20 labs
            if used_chars >= max_chars:
                break
            lab_name = lab.get("name", "").lower() if isinstance(lab, dict) else ""
            lab_value = str(lab.get("value", "")) if isinstance(lab, dict) else ""
            
            if not lab_name or len(lab_name) < 2:
                continue
            
            # Search for lab name + value together
            idx = source_lower.find(lab_name)
            if idx >= 0:
                start = max(0, idx - 50)
                end = min(len(source_text), idx + len(lab_name) + 200)  # Wider window for lab values
                excerpt = source_text[start:end].strip()
                
                # Only include if we find the value nearby
                if lab_value and lab_value in excerpt:
                    if excerpt not in excerpts["lab_values_evidence"]:
                        excerpts["lab_values_evidence"].append(f"...{excerpt}...")
                        used_chars += len(excerpt) + 10
        
        # 5. Build summary
        excerpts["summary"] = (
            f"Document: {len(source_text)} chars. "
            f"Found evidence for: {len(excerpts['conditions_evidence'])} conditions, "
            f"{len(excerpts['medications_evidence'])} medications, "
            f"{len(excerpts['lab_values_evidence'])} lab values."
        )
        
        return excerpts
    
    def _extract_with_llm(self, text: str) -> ExtractedChartData:
        """Use Claude to extract structured data from clinical text."""
        prompt = f"""Extract clinical data from this patient chart. Return JSON with these fields:

{{
    "original_name": "patient's full name",
    "original_dob": "date of birth (MM/DD/YYYY)",
    "original_mrn": "medical record number",
    "account_number": "account number or encounter number (look for ACCOUNT NO., Account #, Encounter, FIN, Visit Number)",
    "gender": "M or F",
    "age": 0,
    "admission_date": "first admission/observation date (MM/DD/YYYY)",
    "observation_date": "date patient was on observation status - usually first day (MM/DD/YYYY or null if not mentioned)",
    "inpatient_date": "date patient transitioned to inpatient status - usually day after observation (MM/DD/YYYY or null if not mentioned)",
    "place_of_service": "Determine initial point of care: If 'presents via EMS', 'arrived by ambulance', 'brought to ED', or 'emergency department' → use 'Emergency Department'. If direct admission to floor/unit without ED → use 'Hospital'. If outpatient → 'Urgent Care' or 'Clinic'.",
    "chief_complaint": "Full narrative: Extract from CHIEF COMPLAINT and HPI sections. Include presenting symptoms, timeline, severity, and associated symptoms. Use PAST TENSE. Do NOT add diagnoses or clinical findings not explicitly stated. Example: 'altered mental status and generalized weakness with progressive decline over 3 days'",
    "chief_complaint_short": "Brief symptom list only: 'chest pain, shortness of breath'",
    "hpi": "history of present illness summary - VERBATIM from chart (2-4 sentences with timeline and key details)",
    "conditions": ["CHRONIC DISEASES ONLY from PAST MEDICAL HISTORY section. Extract EXACTLY as written. Do NOT include acute symptoms."],
    "medications": [
        {{"name": "drug name", "dose": "dose", "route": "PO/IV/etc", "frequency": "frequency"}}
    ],
    "lab_results": [
        {{"name": "test name", "value": "EXACT value from LAB TABLE ONLY - do NOT use values from narrative text", "unit": "unit", "flag": "ONLY 'H' or 'L' if EXPLICITLY marked in chart, otherwise leave BLANK"}}
    ],
    "vitals": {{
        "bp": "ONLY if documented (e.g. '120/80') - leave empty string if not in chart",
        "hr": "ONLY if documented - leave empty string if not in chart",
        "temp": "ONLY if documented - leave empty string if not in chart",
        "rr": "ONLY if documented - leave empty string if not in chart",
        "spo2": "ONLY if documented - leave empty string if not in chart"
    }},
    "insurance_name": "primary insurance/payer name (look for INSURANCE 1, UHC, Humana, Aetna, etc.)",
    "insurance_id": "INSURED ID number (look for INSURED ID:, Member ID, Subscriber ID - e.g. 931969345)",
    "insurance_group": "group number (look for GRP #, Group Number)",
    "insurance_address": "payer mailing address (look for PO BOX or street address near insurance info)",
    "insurance_city": "payer city",
    "insurance_state": "payer state abbreviation (2 letters)",
    "insurance_zip": "payer zip code",
    "facility_name": "hospital name",
    "attending_physician": "doctor name"
}}

CRITICAL INSTRUCTIONS:
- VITALS: Only include vitals that are EXPLICITLY documented with values. If chart only shows HR and SpO2, leave BP, temp, RR as empty strings. DO NOT fabricate "normal" values.
- LAB VALUES: Extract ONLY from structured LAB TABLES with columns. Narrative text like "CBC showing anemia 7 down from 9.8" means hemoglobin is 7, NOT a different number. Use the EXACT numeric values shown.
- LAB FLAGS: Only mark 'H' or 'L' if explicitly shown next to value. Do NOT infer flags - leave blank if not marked.
- CONDITIONS: Extract ONLY from PAST MEDICAL HISTORY section, not from narrative or assessment.
- PLACE OF SERVICE: If patient was ADMITTED to a hospital unit (ICU, neuro-tele, med-surg, etc.), use "hospital" NOT "emergency department". Only use "emergency department" if the entire encounter was ED-only without admission.
- CHIEF COMPLAINT: Keep brief (1-2 sentences). Do NOT include full HPI narrative. Example: "generalized weakness and altered mental status"
- ONLY include information explicitly stated in the chart - do NOT infer or make up findings
- Do NOT add clinical findings like tachycardia, fever, hypotension unless explicitly documented with values
- Extract conditions EXACTLY as written in PAST MEDICAL HISTORY
- Extract all medications with their doses exactly as listed

CHART TEXT:
{text[:30000]}

Return ONLY valid JSON, no other text."""

        response = self.bedrock.converse(
            modelId=self.model_id,
            messages=[{"role": "user", "content": [{"text": prompt}]}],
            inferenceConfig={"maxTokens": 4000, "temperature": 0.1}
        )
        
        result_text = response["output"]["message"]["content"][0]["text"]
        usage = response.get("usage", {})
        
        # Parse JSON from response first (need for validation)
        json_valid = False
        try:
            json_match = re.search(r'\{[\s\S]*\}', result_text)
            if json_match:
                data = json.loads(json_match.group())
                json_valid = True
            else:
                raise ValueError("No JSON found in response")
        except json.JSONDecodeError as e:
            print(f"JSON parse error: {e}")
            print(f"Response: {result_text[:500]}")
            data = {}
        
        # Validate faithfulness - check if extracted terms appear in source
        faithfulness_result = self._validate_extraction_faithfulness(data, text)
        
        # Store debug data for verification UI
        self._last_source_text = text
        self._last_faithfulness_result = faithfulness_result
        self._last_extraction_timestamp = datetime.now().isoformat()
        
        # Log to Langfuse with full audit trail
        langfuse_trace = None
        if langfuse_client:
            try:
                # Build smart excerpts that include evidence for extracted values
                # This ensures Langfuse LLM-as-a-Judge can verify labs that appear late in doc
                smart_excerpts = self._build_langfuse_excerpts(data, text, max_chars=25000)
                
                langfuse_trace = langfuse_client.trace(
                    name="pdf-chart-extraction",
                    input={
                        "pdf_length_chars": len(text),
                        "source_evidence": smart_excerpts,  # Focused excerpts around extracted values
                        "extracted_data_preview": {
                            "conditions": data.get("conditions", [])[:10],
                            "medications": [m.get("name") for m in data.get("medications", [])[:8]],
                            "lab_results": [f"{l.get('name')}={l.get('value')}" for l in data.get("lab_results", [])[:15]],
                        }
                    },
                    output={
                        "extracted_data": data,
                        "faithfulness_validation": {
                            "score": faithfulness_result["faithfulness_score"],
                            "conditions_validated": faithfulness_result["conditions_validated"],
                            "conditions_flagged": faithfulness_result["conditions_flagged"],
                            "medications_validated": len(faithfulness_result["medications_validated"]),
                            "medications_flagged": [m.get("name", str(m)) for m in faithfulness_result["medications_flagged"]],
                            "lab_results_validated": len(faithfulness_result.get("lab_results_validated", [])),
                            "lab_results_flagged": [f"{l.get('name')}={l.get('value')}" for l in faithfulness_result.get("lab_results_flagged", [])],
                        }
                    },
                    metadata={"model": self.model_id}
                )
                langfuse_trace.generation(
                    name="extract-clinical-data",
                    model=self.model_id,
                    input=prompt[:8000],  # Truncate prompt for Langfuse
                    output=result_text,
                    usage={
                        "input": usage.get("inputTokens", 0),
                        "output": usage.get("outputTokens", 0)
                    }
                )
                logger.info("Langfuse trace created")
            except Exception as e:
                logger.error(f"Langfuse error: {e}")
        
        # Log and REMOVE flagged items (potential hallucinations)
        if faithfulness_result["conditions_flagged"]:
            logger.warning(f"REMOVING conditions not in source: {faithfulness_result['conditions_flagged']}")
            # Keep only validated conditions
            data["conditions"] = faithfulness_result["conditions_validated"]
        
        if faithfulness_result["medications_flagged"]:
            logger.warning(f"REMOVING medications not in source: {faithfulness_result['medications_flagged']}")
            # Keep only validated medications
            data["medications"] = faithfulness_result["medications_validated"]
        
        # Score the extraction quality
        if langfuse_trace:
            try:
                # JSON validity score
                langfuse_trace.score(
                    name="json_valid",
                    value=1 if json_valid else 0,
                    comment="Valid JSON extracted from LLM response"
                )
                
                # Data completeness score (check key fields)
                key_fields = ["original_name", "admission_date", "chief_complaint", "conditions"]
                filled = sum(1 for f in key_fields if data.get(f))
                langfuse_trace.score(
                    name="data_completeness",
                    value=filled / len(key_fields),
                    comment=f"{filled}/{len(key_fields)} key fields populated"
                )
                
                # Faithfulness score - did extracted terms appear in source?
                langfuse_trace.score(
                    name="faithfulness",
                    value=faithfulness_result["faithfulness_score"],
                    comment=f"Validated: {len(faithfulness_result['conditions_validated'])} conditions, {len(faithfulness_result['medications_validated'])} meds. Flagged: {len(faithfulness_result['conditions_flagged'])} conditions, {len(faithfulness_result['medications_flagged'])} meds."
                )
                
                # If there are flagged items, add details
                if faithfulness_result["details"]:
                    langfuse_trace.score(
                        name="hallucination_flags",
                        value=len(faithfulness_result["conditions_flagged"]) + len(faithfulness_result["medications_flagged"]),
                        comment="; ".join(faithfulness_result["details"][:5])  # First 5 flags
                    )
                
                langfuse_client.flush()
                logger.info("Langfuse scores sent")
            except Exception as e:
                logger.error(f"Langfuse scoring error: {e}")
        
        # Convert to dataclass
        extracted = ExtractedChartData(
            original_name=data.get("original_name", ""),
            original_dob=data.get("original_dob", ""),
            original_mrn=data.get("original_mrn", ""),
            account_number=data.get("account_number", ""),
            gender=data.get("gender", ""),
            age=data.get("age", 0),
            admission_date=data.get("admission_date", ""),
            observation_date=data.get("observation_date", ""),
            inpatient_date=data.get("inpatient_date", ""),
            place_of_service=data.get("place_of_service", "Emergency Department"),
            chief_complaint_short=data.get("chief_complaint_short", ""),
            chief_complaint=data.get("chief_complaint", ""),
            hpi=data.get("hpi", ""),
            conditions=data.get("conditions", []),
            medications=data.get("medications", []),
            lab_results=data.get("lab_results", []),
            vitals=data.get("vitals", {}),
            insurance_name=data.get("insurance_name", ""),
            insurance_id=data.get("insurance_id", ""),
            insurance_group=data.get("insurance_group", ""),
            insurance_address=data.get("insurance_address", ""),
            insurance_city=data.get("insurance_city", ""),
            insurance_state=data.get("insurance_state", ""),
            insurance_zip=data.get("insurance_zip", ""),
            facility_name=data.get("facility_name", ""),
            attending_physician=data.get("attending_physician", "")
        )
        
        return extracted
    
    def _extract_with_regex(self, text: str) -> ExtractedChartData:
        """Fallback regex-based extraction."""
        extracted = ExtractedChartData()
        
        # Name pattern
        name_match = re.search(r'(\w+),\s*(\w+)\s*(\w?)', text)
        if name_match:
            extracted.original_name = f"{name_match.group(2)} {name_match.group(3)} {name_match.group(1)}".strip()
        
        # DOB pattern
        dob_match = re.search(r'DOB[:\s]*(\d{1,2}/\d{1,2}/\d{4})', text)
        if dob_match:
            extracted.original_dob = dob_match.group(1)
        
        # MRN pattern
        mrn_match = re.search(r'MRN[:\s]*(\d+)', text)
        if mrn_match:
            extracted.original_mrn = mrn_match.group(1)
        
        # Gender
        if 'LegalSex:F' in text or 'SEX.*F' in text:
            extracted.gender = "F"
        elif 'LegalSex:M' in text or 'SEX.*M' in text:
            extracted.gender = "M"
        
        # Age
        age_match = re.search(r'AGE\s+(\d+)', text)
        if age_match:
            extracted.age = int(age_match.group(1))
        
        # Admission date
        adm_match = re.search(r'Adm[:\s]*(\d{1,2}/\d{1,2}/\d{4})', text)
        if adm_match:
            extracted.admission_date = adm_match.group(1)
        
        # Chief complaint
        cc_match = re.search(r'CC[:\s]*([^\n]+)', text)
        if cc_match:
            extracted.chief_complaint = cc_match.group(1).strip()
        
        # Conditions - look for common patterns
        condition_patterns = [
            r'past medical history of ([^.]+)',
            r'history of ([^,\.]+(?:,\s*[^,\.]+)*)',
            r'diagnosed with ([^.]+)',
        ]
        for pattern in condition_patterns:
            matches = re.findall(pattern, text, re.IGNORECASE)
            for match in matches:
                conditions = [c.strip() for c in match.split(',')]
                extracted.conditions.extend(conditions)
        
        return extracted
    
    def _mask_mrn(self, mrn: str) -> str:
        """Mask MRN showing only last 3 digits."""
        if not mrn:
            return "***000"
        mrn = str(mrn).strip()
        if len(mrn) <= 3:
            return mrn
        return "*" * (len(mrn) - 3) + mrn[-3:]
    
    def _randomize_dob(self, dob: str) -> str:
        """Randomize DOB month/day but keep same year (preserves age)."""
        if not dob:
            return ""
        # Parse the date
        for fmt in ["%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y"]:
            try:
                dt = datetime.strptime(dob.strip(), fmt)
                # Keep year, randomize month/day
                new_month = random.randint(1, 12)
                new_day = random.randint(1, 28)  # Safe for all months
                new_dt = datetime(dt.year, new_month, new_day)
                return new_dt.strftime("%Y-%m-%d")
            except ValueError:
                continue
        return dob
    
    def _generate_synthetic_name(self, gender: str) -> str:
        """Generate only a synthetic patient name (minimal de-identification)."""
        first_names = FIRST_NAMES_F if gender.upper() == "F" else FIRST_NAMES_M
        first = random.choice(first_names)
        last = random.choice(LAST_NAMES)
        return f"{first} {last}"
    
    def _normalize_date(self, date_str: str) -> str:
        """Convert date to YYYY-MM-DD format."""
        if not date_str:
            return ""
        # Try common formats
        for fmt in ["%m/%d/%Y", "%Y-%m-%d", "%m-%d-%Y", "%d/%m/%Y"]:
            try:
                dt = datetime.strptime(date_str.strip(), fmt)
                return dt.strftime("%Y-%m-%d")
            except ValueError:
                continue
        return date_str  # Return as-is if no format matches
    
    def deidentify(self, extracted: ExtractedChartData, skip_deidentify: bool = False) -> PatientStayData:
        """
        Convert extracted data to PatientStayData, optionally de-identified.
        
        Args:
            extracted: Raw extracted data from PDF
            skip_deidentify: If True, keep original patient name (for demos)
        
        Only replaces patient name - preserves all dates and clinical content.
        """
        # Use original name or generate synthetic
        if skip_deidentify:
            patient_name = extracted.original_name
            patient_id = extracted.original_mrn or extracted.account_number
        else:
            patient_name = self._generate_synthetic_name(extracted.gender)
            patient_id = self._mask_mrn(extracted.original_mrn)
        
        # Normalize and randomize DOB (keep year for same age) - only if de-identifying
        dob_normalized = self._normalize_date(extracted.original_dob)
        if skip_deidentify:
            dob = dob_normalized
        else:
            dob = self._randomize_dob(dob_normalized)
        admission_date = self._normalize_date(extracted.admission_date)
        observation_date = self._normalize_date(extracted.observation_date) if extracted.observation_date else ""
        inpatient_date = self._normalize_date(extracted.inpatient_date) if extracted.inpatient_date else ""
        
        # Use extracted age, or calculate from DOB
        age = extracted.age
        if not age and dob:
            try:
                birth = datetime.strptime(dob, "%Y-%m-%d")
                age = (datetime.now() - birth).days // 365
            except:
                pass
        
        # Build PatientStayData
        patient_data = PatientStayData(
            patient_id=patient_id,
            patient_name=patient_name,
            dob=dob,
            gender=extracted.gender,
            age=age,
            account_number=extracted.account_number,
            insurance_name=extracted.insurance_name,
            insurance_id=extracted.insurance_id,
            insurance_group=extracted.insurance_group,
            insurance_address=extracted.insurance_address,
            insurance_city=extracted.insurance_city,
            insurance_state=extracted.insurance_state,
            insurance_zip=extracted.insurance_zip,
            admission_date=admission_date,
            observation_date=observation_date,
            inpatient_date=inpatient_date,
            encounter_status="in-progress",
            place_of_service=extracted.place_of_service or "Emergency Department",
            chief_complaint_short=extracted.chief_complaint_short,
            chief_complaint=extracted.chief_complaint,
            conditions=extracted.conditions,
            medications=[
                {
                    "name": med.get("name", ""),
                    "route": med.get("route", "PO"),
                    "status": "active"
                }
                for med in extracted.medications
            ],
            lab_results=[
                {
                    "name": lab.get("name", ""),
                    "value": str(lab.get("value", "")),
                    "unit": lab.get("unit", ""),
                    "flag": lab.get("flag", "")
                }
                for lab in extracted.lab_results
            ],
            clinical_notes=[extracted.hpi] if extracted.hpi else []
        )
        
        return patient_data
    
    def parse_and_deidentify(self, pdf_path: str, skip_deidentify: bool = False) -> PatientStayData:
        """
        Main method: Parse PDF, extract data, and optionally de-identify.
        
        Args:
            pdf_path: Path to PDF clinical chart
            skip_deidentify: If True, keep original patient name (for demos)
            
        Returns:
            PatientStayData ready for appeal letter generation
        """
        print(f"Parsing PDF: {pdf_path}")
        text = self.extract_text(pdf_path)
        print(f"Extracted {len(text)} characters of text")
        
        print("Extracting clinical data...")
        if self.use_llm:
            extracted = self._extract_with_llm(text)
        else:
            extracted = self._extract_with_regex(text)
        
        print(f"Found {len(extracted.conditions)} conditions, {len(extracted.medications)} medications, {len(extracted.lab_results)} labs")
        
        if skip_deidentify:
            print("Keeping original patient name (demo mode)...")
        else:
            print("De-identifying patient data...")
        patient_data = self.deidentify(extracted, skip_deidentify=skip_deidentify)
        
        print(f"Created patient: {patient_data.patient_name}")
        return patient_data
    
    def parse_to_json(self, pdf_path: str, output_path: Optional[str] = None) -> str:
        """
        Parse PDF and save de-identified data as JSON.
        
        Useful for creating reusable test fixtures.
        """
        patient_data = self.parse_and_deidentify(pdf_path)
        
        # Convert to dict
        data_dict = {
            "patient_id": patient_data.patient_id,
            "patient_name": patient_data.patient_name,
            "dob": patient_data.dob,
            "gender": patient_data.gender,
            "age": patient_data.age,
            "admission_date": patient_data.admission_date,
            "encounter_status": patient_data.encounter_status,
            "chief_complaint": patient_data.chief_complaint,
            "conditions": patient_data.conditions,
            "medications": patient_data.medications,
            "lab_results": patient_data.lab_results,
            "clinical_notes": patient_data.clinical_notes
        }
        
        json_str = json.dumps(data_dict, indent=2)
        
        if output_path:
            Path(output_path).write_text(json_str)
            print(f"Saved to: {output_path}")
        
        return json_str


if __name__ == "__main__":
    import sys
    
    if len(sys.argv) < 2:
        print("Usage: python pdf_chart_parser.py <pdf_path> [--generate-appeal] [--hospital NAME] [--no-llm]")
        print("\nOptions:")
        print("  --generate-appeal    Generate appeal letter after parsing")
        print("  --hospital NAME      Hospital/facility name for letter")
        print("  --no-llm             Use regex extraction only (no LLM)")
        sys.exit(1)
    
    pdf_path = sys.argv[1]
    use_llm = "--no-llm" not in sys.argv
    generate_appeal = "--generate-appeal" in sys.argv
    
    # Parse hospital name if provided
    hospital = "SSM Health St. Marys Hospital"
    for i, arg in enumerate(sys.argv):
        if arg == "--hospital" and i + 1 < len(sys.argv):
            hospital = sys.argv[i + 1]
    
    parser = PDFChartParser(use_llm=use_llm)
    patient_data = parser.parse_and_deidentify(pdf_path)
    
    print("\n" + "="*60)
    print("DE-IDENTIFIED PATIENT DATA")
    print("="*60)
    print(f"Name: {patient_data.patient_name}")
    print(f"DOB: {patient_data.dob}")
    print(f"Gender: {patient_data.gender}")
    print(f"Age: {patient_data.age}")
    print(f"Admission: {patient_data.admission_date}")
    obs_date = getattr(patient_data, 'observation_date', '')
    inp_date = getattr(patient_data, 'inpatient_date', '')
    if obs_date:
        print(f"Observation Date: {obs_date}")
    if inp_date:
        print(f"Inpatient Date: {inp_date}")
    print(f"Chief Complaint: {patient_data.chief_complaint}")
    print(f"\nConditions ({len(patient_data.conditions)}):")
    for c in patient_data.conditions[:10]:
        print(f"  - {c}")
    print(f"\nMedications ({len(patient_data.medications)}):")
    for m in patient_data.medications[:10]:
        print(f"  - {m['name']} ({m.get('route', 'PO')})")
    print(f"\nLab Results ({len(patient_data.lab_results)}):")
    for lab in patient_data.lab_results[:10]:
        flag = f" [{lab['flag']}]" if lab.get('flag') else ""
        print(f"  - {lab['name']}: {lab['value']} {lab.get('unit', '')}{flag}")
    
    if generate_appeal:
        from services.midnight_reason_generator import MidnightReasonGenerator
        from services.appeal_letter_generator import AppealLetterGenerator
        
        print("\n" + "="*60)
        print("GENERATING APPEAL LETTER")
        print("="*60)
        
        reason_gen = MidnightReasonGenerator()
        reason_output = reason_gen.generate_from_data(patient_data)
        
        letter_gen = AppealLetterGenerator()
        output_path = letter_gen.generate_from_data(
            patient_data=patient_data,
            reason_output=reason_output,
            place_of_service=hospital
        )
        
        print(f"\nAppeal letter saved: {output_path}")

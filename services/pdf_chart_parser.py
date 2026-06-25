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
    authorization_number: str = ""  # PrimaryCoverageAuthorizationNumber (e.g., A322224250)
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
    discharge_date: str = ""    # Date discharged (blank if still admitted)
    place_of_service: str = ""  # emergency department, hospital, urgent care, etc.
    place_of_service_raw_code: str = ""  # Raw SERVICE code from PDF (e.g., ERS, OBS, HOSP)
    chief_complaint_short: str = ""  # Short symptom list: "abdominal pain, nausea, vomiting"
    chief_complaint: str = ""  # Full narrative for letter body (assembled from 2 parts below)
    presenting_symptom: str = ""  # Exact symptom from Chief Complaint section
    pmh_relevant: str = ""  # Pertinent PMH conditions related to this admission
    hpi: str = ""
    conditions: List[str] = field(default_factory=list)
    medications: List[Dict] = field(default_factory=list)
    lab_results: List[Dict] = field(default_factory=list)
    vitals: Dict = field(default_factory=dict)
    assessment_plan: str = ""
    clinical_notes: List[str] = field(default_factory=list)
    consults: List[str] = field(default_factory=list)  # Consult services and findings
    procedures: List[str] = field(default_factory=list)  # Planned or completed procedures
    
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
        self.model_id = "us.anthropic.claude-sonnet-4-20250514-v1:0"
        # Note: bedrock client is created fresh on each call to handle credential refresh
        
        # Debug data storage - populated during extraction for verification
        self._last_source_text = ""
        self._last_faithfulness_result = {}
        self._last_extraction_timestamp = None
        self._last_trace_ids = {}  # Langfuse trace IDs for score fetching
    
    def _get_bedrock_client(self):
        """Create a fresh Bedrock client to pick up refreshed credentials."""
        return boto3.client("bedrock-runtime", region_name="us-east-1")
    
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
            "trace_ids": self._last_trace_ids,  # Langfuse trace IDs for score fetching
        }
    
    def extract_text(self, pdf_path: str) -> str:
        """Extract all text from PDF."""
        full_text = ""
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                text = page.extract_text() or ""
                full_text += text + "\n\n"
        
        # Filter out any embedded appeal letter content to prevent LLM from copying it
        original_len = len(full_text)
        full_text = self._filter_appeal_letter_content(full_text)
        filtered_len = len(full_text)
        if original_len != filtered_len:
            logger.info(f"Appeal letter filter removed {original_len - filtered_len} chars")
        return full_text
    
    def _filter_appeal_letter_content(self, text: str) -> str:
        """Remove any embedded appeal letter content from PDF text.
        
        This prevents the LLM from reading and copying previously generated
        appeal letters that may contain hallucinations.
        
        Handles both normal text and concatenated/no-space text from PDF extraction.
        """
        import re
        
        # Pattern 0: Remove "Dear Medical Director" appeal letters (common format)
        text = re.sub(
            r'Dear Medical Director.*?(?:Sincerely|Respectfully)',
            '[APPEAL LETTER REMOVED]',
            text,
            flags=re.DOTALL | re.IGNORECASE
        )
        
        # Pattern 0b: Remove letters containing 42 C.F.R. § 422.584 (expedited reconsideration)
        text = re.sub(
            r'42\s*C\.?F\.?R\.?\s*§?\s*422\.584.*?(?:Sincerely|Respectfully)',
            '[LEGAL APPEAL REMOVED]',
            text,
            flags=re.DOTALL | re.IGNORECASE
        )
        
        # Pattern 0c: Remove unfilled templates (*** placeholders indicate template)
        if '***' in text:
            text = re.sub(
                r'(?:Your member was admitted|During the first midnight|During the second midnight).*?\*\*\*.*?(?:Sincerely|Respectfully|\Z)',
                '[TEMPLATE REMOVED]',
                text,
                flags=re.DOTALL | re.IGNORECASE
            )
            logger.warning("Removed unfilled template (*** placeholders detected)")
        
        # Pattern 1: Remove entire appeal letter section (MEDICARE APPEAL to Physician Advisor)
        text = re.sub(
            r'MEDICARE APPEAL LETTER.*?Physician Advisor',
            '[APPEAL LETTER REMOVED]',
            text,
            flags=re.DOTALL | re.IGNORECASE
        )
        
        # Pattern 2: Remove "To Whom It May Concern" through "Respectfully submitted"
        text = re.sub(
            r'To Whom It May Concern.*?Respectfully submitted',
            '[APPEAL CONTENT REMOVED]',
            text,
            flags=re.DOTALL | re.IGNORECASE
        )
        
        # Pattern 3: Remove Fast Appeal / Formal Request sections (with or without spaces)
        # These appear when a prior appeal letter is embedded in the PDF
        text = re.sub(
            r'Formal\s*Request\s*for\s*Fast\s*Appeal.*?(?:Physician\s*Advisor|Respectfully\s*submitted|SSM\s*Health)',
            '[FAST APPEAL REMOVED]',
            text,
            flags=re.DOTALL | re.IGNORECASE
        )
        
        # Pattern 4: Remove concatenated appeal headers (no spaces from PDF extraction)
        text = re.sub(
            r'FormalRequestforFastAppeal.*?(?:PhysicianAdvisor|Respectfullysubmitted|SSMHealth)',
            '[FAST APPEAL REMOVED]',
            text,
            flags=re.DOTALL | re.IGNORECASE
        )
        
        # Pattern 5: Remove "Request for Reconsideration" sections
        text = re.sub(
            r'Request\s*for\s*Reconsideration.*?(?:Physician\s*Advisor|Respectfully|Sincerely)',
            '[RECONSIDERATION REMOVED]',
            text,
            flags=re.DOTALL | re.IGNORECASE
        )
        
        # Pattern 6: FALLBACK - If "Fast Appeal" still exists (end markers not found),
        # remove next 10000 chars (typical appeal letter length)
        if re.search(r'Fast\s*Appeal|FastAppeal', text, re.IGNORECASE):
            text = re.sub(
                r'(Fast\s*Appeal|FastAppeal).{0,10000}',
                '[APPEAL CONTENT REMOVED]',
                text,
                flags=re.DOTALL | re.IGNORECASE
            )
            logger.warning("Used fallback appeal filter (no end marker found)")
        
        # Pattern 6b: FALLBACK - If legal citation still exists, remove surrounding content
        if re.search(r'42\s*C\.?F\.?R\.?|Medicare Act guarantees|MAO coverage denial', text, re.IGNORECASE):
            text = re.sub(
                r'(?:In my medical opinion|The Medicare Act guarantees|MAO coverage denial).*?(?:Sincerely|Respectfully|\n\n)',
                '[LEGAL CONTENT REMOVED]',
                text,
                flags=re.DOTALL | re.IGNORECASE
            )
            logger.warning("Removed legal boilerplate from appeal letter")
        
        # Pattern 7: Remove isolated appeal letter phrases that might remain
        appeal_phrases = [
            r'During the first midnight[^.]*\.',
            r'During the second midnight[^.]*\.',
            r'Your member was admitted inpatient due to[^.]*\.',
            r'In summary, the patient required continued inpatient hospitalization[^.]*\.',
            r'In summary, the member required continued inpatient hospitalization[^.]*\.',
            r'Duringthefirstmidnight[^.]*\.',  # No-space version
            r'Duringthesecondmidnight[^.]*\.',  # No-space version
            r'Yourmemberwasadmittedinpatientdueto[^.]*\.',  # No-space version
            # Legal citations that are appeal-specific
            r'I write pursuant to 42 C\.F\.R\.[^.]*\.',
            r'Section 40\.8 of the Medicare Managed Care[^.]*\.',
            r'Section 50\.8 reiterates[^.]*\.',
            r'MA Plans may not impose conditions[^.]*\.',
            r'MAO must, therefore, provide coverage[^.]*\.',
            r'MAO coverage denial violates[^.]*\.',
            r'I look forward to an expedited and favorable decision[^.]*\.',
        ]
        for phrase in appeal_phrases:
            text = re.sub(phrase, '', text, flags=re.IGNORECASE)
        
        return text
    
    def _extract_service_code(self, text: str) -> str:
        """
        Extract place of service from SERVICE field in ADMISSION RECORD section.
        
        Service codes:
            ERS = Emergency Room Services → Emergency Department
            HOSPI = Hospitalist → Hospital
            OBS = Observation → Observation Unit
            SURG = Surgery → Hospital (Surgical)
            
        Returns tuple: (raw_code, human-readable place of service string)
        """
        import re
        
        # pdfplumber extracts columnar text with headers on one line and values below
        # Look for SERVICE STATION header row, then find service code in values row
        # Pattern: SERVICE STATION ... (newline) ... (service code)
        service_match = re.search(
            r'SERVICE\s+STATION.*?\n.*?\b(ERS|OBS|HOSPI?|HOSP|MED|MEDSURG|SURG|ICU|CCU|TELE|NEURO|ER|ED|EMER)\b', 
            text, 
            re.IGNORECASE | re.DOTALL
        )
        
        # Log for debugging
        logger.info(f"SERVICE code extraction: match={service_match.group(1) if service_match else 'None'}")
        print(f"[DEBUG] SERVICE code extraction: match={service_match.group(1) if service_match else 'None'}", flush=True)
        if service_match:
            code = service_match.group(1).upper()
            logger.info(f"SERVICE code uppercase: '{code}'")
            
            # Map service codes to place of service
            service_map = {
                'ERS': 'Emergency Department',
                'ER': 'Emergency Department',
                'ED': 'Emergency Department',
                'EMER': 'Emergency Department',
                'HOSPI': 'Hospital',
                'HOSP': 'Hospital',
                'MED': 'Hospital',
                'MEDSURG': 'Hospital',
                'OBS': 'Observation Unit',
                'SURG': 'Hospital',
                'SURGERY': 'Hospital',
                'ICU': 'Hospital (ICU)',
                'CCU': 'Hospital (CCU)',
                'TELE': 'Hospital (Telemetry)',
                'NEURO': 'Hospital (Neurology)',
                'CARD': 'Hospital (Cardiology)',
                'ORTHO': 'Hospital (Orthopedics)',
            }
            
            if code in service_map:
                result = service_map[code]
                logger.info(f"SERVICE mapped '{code}' -> '{result}'")
                return (code, result)
            
            # If code starts with known prefix
            for prefix, pos in service_map.items():
                if code.startswith(prefix):
                    logger.info(f"SERVICE prefix matched '{code}' starts with '{prefix}' -> '{pos}'")
                    return (code, pos)
        
        logger.info("SERVICE code extraction: no mapping found, returning empty")
        return ("", "")  # Return empty tuple if not found - LLM will determine
    
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
            med_freq = med.get("frequency", "").lower() if isinstance(med, dict) else ""
            med_dose = med.get("dose", "").lower() if isinstance(med, dict) else ""
            
            # Check first word of medication name (generic name)
            first_word = med_name.split()[0] if med_name.split() else ""
            name_found = False
            if first_word and len(first_word) > 3 and first_word in source_lower:
                name_found = True
            elif med_name in source_lower:
                name_found = True
            
            if name_found:
                # Get context around medication name for validating dose and frequency
                escaped_name = re.escape(first_word if first_word else med_name)
                name_match = re.search(escaped_name, source_lower, re.IGNORECASE)
                
                if name_match:
                    # Use wider context (300 chars) to catch frequency/dose that may not be immediately adjacent
                    start = max(0, name_match.start() - 100)
                    end = min(len(source_lower), name_match.end() + 300)
                    context = source_lower[start:end]
                    
                    # DOSE VALIDATION - check if extracted dose appears in context
                    if med_dose and med_dose not in ["", "n/a", "unknown"]:
                        dose_in_context = med_dose.lower() in context
                        
                        # Check for dose contradictions (e.g., "1 tablet" vs "1.5 tablet")
                        if not dose_in_context:
                            # Try to find the actual dose in source
                            tablet_match = re.search(r'(\d+\.?\d*)\s*(?:tablet|tab)', context)
                            mg_match = re.search(r'(\d+\.?\d*)\s*(?:mg|mcg)', context)
                            
                            if tablet_match:
                                source_dose = tablet_match.group(0)
                                if med_dose.replace(" ", "") != source_dose.replace(" ", ""):
                                    logger.warning(f"Medication dose mismatch: source says '{source_dose}' but extracted '{med_dose}': {med_name} - correcting")
                                    med["dose"] = source_dose
                                    med["dose_flagged"] = True
                            elif mg_match and "mg" in med_dose:
                                source_dose = mg_match.group(0)
                                # Only flag if significantly different
                                try:
                                    extracted_num = float(re.search(r'(\d+\.?\d*)', med_dose).group(1))
                                    source_num = float(mg_match.group(1))
                                    if abs(extracted_num - source_num) > 0.5:  # More than 0.5 difference
                                        logger.warning(f"Medication dose mismatch: source has '{source_dose}' but extracted '{med_dose}': {med_name}")
                                        med["dose_flagged"] = True
                                except:
                                    pass
                    
                    # FREQUENCY VALIDATION - only correct clear mismatches, don't clear frequencies
                    if med_freq and med_freq.lower() not in ["", "prn", "as needed", "once", "daily"]:
                        # Check if extracted frequency appears in context
                        freq_in_context = med_freq.lower() in context
                        
                        # Check for frequency contradictions - only correct when source clearly says different
                        tid_in_context = "tid" in context or "three times" in context or "3 times" in context
                        bid_in_context = "bid" in context or "twice" in context or "2 times" in context
                        qid_in_context = "qid" in context or "four times" in context or "4 times" in context
                        
                        if tid_in_context and not bid_in_context and med_freq.lower() in ["bid", "twice", "twice daily"]:
                            # Source says TID but we extracted BID - correct it
                            logger.warning(f"Medication frequency mismatch: source says TID but extracted {med_freq}: {med_name} - correcting to TID")
                            med["frequency"] = "TID"
                            med["frequency_corrected"] = True
                        elif bid_in_context and not tid_in_context and med_freq.lower() in ["tid", "three times"]:
                            # Source says BID but we extracted TID - correct it
                            logger.warning(f"Medication frequency mismatch: source says BID but extracted {med_freq}: {med_name} - correcting to BID")
                            med["frequency"] = "BID"
                            med["frequency_corrected"] = True
                        # DON'T clear frequencies - just leave them even if not found in context
                        # The LLM may have extracted correctly from a different part of the document
                
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
            lab_date = str(lab.get("date", "")) if isinstance(lab, dict) else ""
            
            logger.debug(f"Checking lab: name='{lab_name}', value='{lab_value}', date='{lab_date}'")
            
            # Skip empty values
            if not lab_value or lab_value.lower() in ["", "none", "n/a", "pending"]:
                logger.debug(f"Skipping lab '{lab_name}' - empty or pending value")
                continue
            
            # FIRST: Validate the date if provided - fabricated dates are a red flag
            date_valid = True
            if lab_date:
                # Check if this exact date appears in source (check multiple formats)
                date_found = False
                lab_date_lower = lab_date.lower()
                date_variants = [lab_date_lower]
                
                # Try exact match first
                if lab_date_lower in source_lower:
                    date_found = True
                else:
                    # Build date format variants to check
                    # Without leading zeros (06/15/26 -> 6/15/26)
                    alt_date = lab_date.replace("/0", "/").lstrip("0").lower()
                    date_variants.append(alt_date)
                    
                    # 2-digit year to 4-digit (06/15/26 -> 06/15/2026)
                    if re.match(r'\d{1,2}/\d{1,2}/\d{2}$', lab_date):
                        parts = lab_date.split('/')
                        year_4digit = f"{parts[0]}/{parts[1]}/20{parts[2]}"
                        date_variants.append(year_4digit.lower())
                    
                    # 4-digit year to 2-digit (06/15/2026 -> 06/15/26)
                    if re.match(r'\d{1,2}/\d{1,2}/\d{4}$', lab_date):
                        parts = lab_date.split('/')
                        year_2digit = f"{parts[0]}/{parts[1]}/{parts[2][2:]}"
                        date_variants.append(year_2digit.lower())
                    
                    # Dash format (06-15-26)
                    date_variants.append(lab_date.replace("/", "-").lower())
                    
                    for variant in date_variants:
                        if variant in source_lower:
                            date_found = True
                            break
                
                if not date_found:
                    # Try to find an actual date near the lab name in source
                    name_idx = source_lower.find(lab_name)
                    if name_idx < 0 and '/' in lab_name:
                        name_idx = source_lower.find(lab_name.split('/')[0].strip())
                    if name_idx < 0 and ' ' in lab_name:
                        name_idx = source_lower.find(lab_name.split()[0].strip())
                    
                    if name_idx >= 0:
                        # Search for a date pattern near the lab name
                        context_start = max(0, name_idx - 150)
                        context_end = min(len(source_text), name_idx + 300)
                        context = source_text[context_start:context_end]
                        
                        # Look for date patterns like 06/14/26 or 06/14/2026
                        date_pattern = r'(\d{1,2}[-/]\d{1,2}[-/]\d{2,4})'
                        date_matches = re.findall(date_pattern, context)
                        if date_matches:
                            # Use the first date found near the lab
                            actual_date = date_matches[0].replace('-', '/')
                            logger.warning(f"Lab date CORRECTED: {lab_name} {lab_date} -> {actual_date} (source date)")
                            lab["date"] = actual_date
                            lab["date_corrected"] = True
                            date_valid = True
                        else:
                            logger.warning(f"Lab date NOT in source: {lab_date} for {lab_name}={lab_value} - likely fabricated. Checked variants: {date_variants[:3]}")
                            date_valid = False
                            results["lab_results_flagged"].append(lab)
                            results["details"].append(f"Lab date fabricated: {lab_date} not in source")
                            continue  # Skip this lab entirely
                    else:
                        logger.warning(f"Lab date NOT in source: {lab_date} for {lab_name}={lab_value} - likely fabricated. Checked variants: {date_variants[:3]}")
                        date_valid = False
                        results["lab_results_flagged"].append(lab)
                        results["details"].append(f"Lab date fabricated: {lab_date} not in source")
                        continue  # Skip this lab entirely
            
            # For numeric values, be STRICT - require name and value to appear together
            # This prevents "4.5" in "take 4.5mg metformin" from validating a fabricated lab
            is_numeric = lab_value.replace(".", "").replace("-", "").replace("<", "").replace(">", "").isdigit()
            
            if is_numeric and lab_name:
                # Build pattern: lab name within ~200 chars of value (either order)
                # Examples: "RBC: 4.5" or "4.5 (RBC)" or "RBC...4.5"
                escaped_name = re.escape(lab_name)
                escaped_value = re.escape(lab_value)
                
                # Check both orderings: name...value and value...name (increased to 200 chars)
                pattern1 = rf'{escaped_name}.{{0,200}}{escaped_value}'
                pattern2 = rf'{escaped_value}.{{0,200}}{escaped_name}'
                
                name_value_together = (
                    re.search(pattern1, source_lower, re.IGNORECASE) or 
                    re.search(pattern2, source_lower, re.IGNORECASE)
                )
                
                # If full name not found, try first significant word (for "BUN/Creatinine Ratio" -> "bun")
                if not name_value_together and '/' in lab_name:
                    first_part = lab_name.split('/')[0].strip()
                    if len(first_part) >= 2:
                        escaped_first = re.escape(first_part)
                        pattern1 = rf'{escaped_first}.{{0,200}}{escaped_value}'
                        pattern2 = rf'{escaped_value}.{{0,200}}{escaped_first}'
                        name_value_together = (
                            re.search(pattern1, source_lower, re.IGNORECASE) or 
                            re.search(pattern2, source_lower, re.IGNORECASE)
                        )
                
                # Also try first word for multi-word names like "Immature Reticulocyte Fraction"
                if not name_value_together and ' ' in lab_name:
                    first_word = lab_name.split()[0].strip()
                    if len(first_word) >= 3:
                        escaped_first = re.escape(first_word)
                        pattern1 = rf'{escaped_first}.{{0,200}}{escaped_value}'
                        pattern2 = rf'{escaped_value}.{{0,200}}{escaped_first}'
                        name_value_together = (
                            re.search(pattern1, source_lower, re.IGNORECASE) or 
                            re.search(pattern2, source_lower, re.IGNORECASE)
                        )
                
                if name_value_together:
                    logger.info(f"Lab VALIDATED: {lab_name}={lab_value} (found together in source)")
                    results["lab_results_validated"].append(lab)
                else:
                    # Value not found - try to find the ACTUAL value near the lab name
                    # Search for lab name and extract nearby numeric value
                    name_idx = source_lower.find(lab_name)
                    if name_idx < 0 and '/' in lab_name:
                        name_idx = source_lower.find(lab_name.split('/')[0].strip())
                    if name_idx < 0 and ' ' in lab_name:
                        name_idx = source_lower.find(lab_name.split()[0].strip())
                    
                    if name_idx >= 0:
                        # Get context around the lab name
                        context_start = max(0, name_idx - 20)
                        context_end = min(len(source_text), name_idx + 100)
                        context = source_text[context_start:context_end]
                        
                        # Look for numeric value after the lab name
                        value_match = re.search(r'[\s:]\s*(\d+\.?\d*)\s*(?:[KkMmGg/]|$|\s)', context)
                        if value_match:
                            actual_value = value_match.group(1)
                            if actual_value != lab_value:
                                logger.warning(f"Lab value CORRECTED: {lab_name} {lab_value} -> {actual_value} (source value)")
                                lab["value"] = actual_value
                                lab["value_corrected"] = True
                                results["lab_results_validated"].append(lab)
                            else:
                                results["lab_results_validated"].append(lab)
                        else:
                            logger.warning(f"Lab FLAGGED: {lab_name}={lab_value} (NOT found together in source)")
                            results["lab_results_flagged"].append(lab)
                            results["details"].append(f"Lab value not found with name in source: {lab_name}={lab_value} (possible hallucination)")
                    else:
                        logger.warning(f"Lab FLAGGED: {lab_name}={lab_value} (lab name not in source)")
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
        
        # Validate hook components - check for fabricated clinical phrases
        # Now we check the 2 structured parts instead of assembled chief_complaint
        presenting_symptom = extracted_data.get("presenting_symptom", "")
        pmh_relevant = extracted_data.get("pmh_relevant", "")
        hook_components = f"{presenting_symptom} {pmh_relevant}"
        hallucinated_phrases = []
        
        # List of specific clinical phrases that must appear in source if mentioned
        # These are high-risk fabrications that would be embarrassing if wrong
        clinical_phrases_to_check = [
            # Respiratory issues - often fabricated
            "respiratory failure", "hypoxic respiratory", "acute hypoxic",
            "respiratory distress", "hypoxia", "hypoxemia", "desaturation",
            # Sedation/overdose fabrications
            "over-sedation", "oversedation", "medication-induced", "drug-induced",
            "became sedated", "over sedated", "excessive sedation",
            # IV medications - frequently invented
            "iv ativan", "iv morphine", "iv lorazepam", "iv dilaudid", "iv fentanyl",
            "iv hydromorphone", "iv versed", "iv midazolam",
            # Oxygen specifics
            "2l oxygen", "3l oxygen", "4l oxygen", "5l oxygen", "6l oxygen",
            "oxygen support", "nasal cannula", "high flow",
            # Severe events
            "septic shock", "cardiogenic shock", "hemorrhagic shock",
            "cardiac arrest", "respiratory arrest", "code blue",
            "intubated", "ventilator", "bipap", "cpap",
            # Common dramatic additions
            "unresponsive", "altered mental status", "ams", "encephalopathy",
            "icu admission", "transferred to icu", "critical care",
            # Substance abuse - frequently fabricated
            "amphetamine abuse", "cocaine abuse", "opioid abuse", "heroin abuse",
            "methamphetamine abuse", "alcohol abuse", "substance abuse",
            "drug abuse", "iv drug use", "ivdu", "injection drug use",
            "opioid use disorder", "alcohol use disorder", "substance use disorder",
            # Psychiatric - often invented
            "suicidal ideation", "homicidal ideation", "psychosis", "psychotic",
            "schizophrenia", "bipolar disorder", "schizoaffective",
            # Kidney disease - often invented or embellished
            "ckd stage 3b", "ckd stage 3a", "ckd stage 4", "ckd stage 5",
            "acute kidney injury on ckd", "aki on ckd", "aki on chronic kidney disease",
            "chronic kidney disease stage",
        ]
        
        hook_lower = hook_components.lower()
        for phrase in clinical_phrases_to_check:
            if phrase in hook_lower:
                if phrase not in source_lower:
                    hallucinated_phrases.append(phrase)
        
        if hallucinated_phrases:
            results["chief_complaint_hallucinations"] = hallucinated_phrases
            results["details"].append(f"Hook components contain fabricated phrases: {hallucinated_phrases}")
            logger.warning(f"Hook components may contain hallucinated phrases: {hallucinated_phrases}")
        
        # Also check HPI for hallucinated phrases
        hpi = extracted_data.get("hpi", "")
        hpi_hallucinations = []
        hpi_lower = hpi.lower()
        for phrase in clinical_phrases_to_check:
            if phrase in hpi_lower:
                if phrase not in source_lower:
                    hpi_hallucinations.append(phrase)
        
        if hpi_hallucinations:
            results["hpi_hallucinations"] = hpi_hallucinations
            results["details"].append(f"HPI contains fabricated phrases: {hpi_hallucinations}")
            logger.warning(f"HPI may contain hallucinated phrases: {hpi_hallucinations}")
        
        return results
    
    def _build_langfuse_excerpts(self, extracted_data: dict, source_text: str, max_chars: int = 50000) -> dict:
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
            "hpi_evidence": "",
            "conditions_evidence": [],
            "medications_evidence": [],
            "lab_values_evidence": [],
            "summary": ""
        }
        
        used_chars = 0
        context_window = 250  # chars before/after each found term (increased from 150)
        
        # 1. Patient demographics (first 1000 chars typically has name, DOB, MRN)
        excerpts["patient_demographics"] = source_text[:1000]
        used_chars += 1000
        
        # 2. Extract HPI / Chief Complaint section for evaluator to verify synthesized hook
        hpi_markers = ["history of present illness", "hpi:", "chief complaint:", "reason for visit:", 
                       "presenting complaint", "cc:", "history of presenting illness"]
        hpi_end_markers = ["past medical history", "pmh:", "review of systems", "ros:", 
                           "physical exam", "medications", "allergies", "social history"]
        
        hpi_start = -1
        for marker in hpi_markers:
            idx = source_lower.find(marker)
            if idx >= 0:
                hpi_start = idx
                break
        
        if hpi_start >= 0:
            # Find where HPI ends (next section)
            hpi_end = len(source_text)
            for end_marker in hpi_end_markers:
                end_idx = source_lower.find(end_marker, hpi_start + 100)
                if end_idx > hpi_start and end_idx < hpi_end:
                    hpi_end = end_idx
            
            # Cap HPI excerpt at 5000 chars to leave room for other evidence
            hpi_excerpt = source_text[hpi_start:min(hpi_end, hpi_start + 5000)].strip()
            excerpts["hpi_evidence"] = hpi_excerpt
            used_chars += len(hpi_excerpt)
            logger.info(f"HPI excerpt: {len(hpi_excerpt)} chars extracted")
        else:
            logger.info("No HPI section found in PDF")
        
        # 3. Find evidence for each extracted condition
        for condition in extracted_data.get("conditions", [])[:30]:  # Top 30 conditions (increased from 15)
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
        
        # 4. Find evidence for each medication (with flexible matching)
        meds_found = 0
        meds_not_found = []
        for med in extracted_data.get("medications", [])[:25]:  # Top 25 meds (increased from 12)
            if used_chars >= max_chars:
                break
            med_name = med.get("name", "").lower() if isinstance(med, dict) else ""
            if not med_name or len(med_name) < 3:
                continue
            
            # Try exact match first
            idx = source_lower.find(med_name)
            
            # If not found, try first word only (e.g., "metoprolol" from "metoprolol tartrate")
            if idx < 0:
                first_word = med_name.split()[0] if ' ' in med_name else med_name
                if len(first_word) >= 4:
                    idx = source_lower.find(first_word)
            
            if idx >= 0:
                meds_found += 1
                start = max(0, idx - context_window)
                end = min(len(source_text), idx + len(med_name) + context_window)
                excerpt = source_text[start:end].strip()
                if excerpt not in excerpts["medications_evidence"]:
                    excerpts["medications_evidence"].append(f"...{excerpt}...")
                    used_chars += len(excerpt) + 10
            else:
                meds_not_found.append(med_name)
        
        # Log medications not found in source (potential hallucinations)
        if meds_not_found:
            logger.warning(f"Medications NOT in source text (possible hallucination): {meds_not_found[:10]}")
        logger.info(f"Medications evidence: {meds_found} found, {len(meds_not_found)} not found")
        
        # 5. Find evidence for each lab value (CRITICAL - this is what Langfuse was missing)
        for lab in extracted_data.get("lab_results", []):  # ALL labs (removed limit)
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
        
        # 6. Build summary
        hpi_status = f"{len(excerpts['hpi_evidence'])} chars" if excerpts['hpi_evidence'] else "not found"
        excerpts["summary"] = (
            f"Document: {len(source_text)} chars. "
            f"HPI: {hpi_status}. "
            f"Found evidence for: {len(excerpts['conditions_evidence'])} conditions, "
            f"{len(excerpts['medications_evidence'])} medications, "
            f"{len(excerpts['lab_values_evidence'])} lab values."
        )
        
        return excerpts
    
    def _extract_with_llm(self, text: str) -> ExtractedChartData:
        """Use Claude to extract structured data from clinical text."""
        prompt = f"""CRITICAL MEDICAL DATA EXTRACTION - READ THIS FIRST:

You are extracting data from a REAL PATIENT'S medical chart. This data will be used in Medicare appeals that affect real people's healthcare coverage. FABRICATED DATA IS UNACCEPTABLE.

RULES YOU MUST FOLLOW:
1. You are a DATA COPIER, not a writer. Copy EXACTLY what you see. Do NOT paraphrase, translate, or "improve" anything.
2. If you cannot find a value in the source text, return EMPTY STRING. Do NOT guess or infer.
3. If a date shows "06/14/26" in the source, you write "06/14/26" - NOT "06/15/26", NOT "06/18/26", NOT any other date.
4. If a dose shows "50 mg", you write "50 mg" - NOT "25 mg", NOT "100 mg".
5. If a frequency shows "every 6 hours as needed", you write "every 6 hours as needed" - NOT "BID", NOT "Q6H PRN".
6. If age shows "70-year-old", you write "70-year-old" - NOT "78-year-old".
7. Do NOT invent lab values. If you don't see "ALBUMIN 2.9" in the source, do NOT output it.
8. LAB VALUES ARE CRITICAL: If source shows WBC 17.0, you write 17.0 - NOT 16.7, NOT 17.1, NOT any rounded/modified number.
9. FOR PMH_RELEVANT: EXCLUDE substance abuse, psychiatric history, social factors - these don't strengthen Medicare appeals. INCLUDE only conditions that explain medical necessity: CKD, diabetes, heart failure, COPD, etc.

YOUR OUTPUT WILL BE VERIFIED. Every value you return will be checked against the source document. Fabrications will be flagged and removed.

When in doubt: OMIT IT. An empty field is infinitely better than a fabricated one.

Now extract clinical data from this patient chart. Return JSON with these fields:

{{
    "original_name": "patient's full name in FIRST LAST format (e.g., 'John Smith' not 'Smith, John')",
    "original_dob": "date of birth (MM/DD/YYYY)",
    "original_mrn": "medical record number",
    "account_number": "account number or encounter number (look for ACCOUNT NO., Account #, Encounter, FIN, Visit Number)",
    "authorization_number": "ONLY if found: look for 'PrimaryCoverageAuthorizationNumber:' followed by alphanumeric code. If NOT in document, use empty string ''",
    "gender": "M or F",
    "admission_date": "first admission/observation date (MM/DD/YYYY)",
    "observation_date": "date patient was on observation status - usually first day (MM/DD/YYYY or null if not mentioned)",
    "inpatient_date": "date patient transitioned to inpatient status - usually day after observation (MM/DD/YYYY or null if not mentioned)",
    "discharge_date": "date patient was discharged (MM/DD/YYYY) - look for DISCHARGE DATE/TIME field. If no discharge date found or patient is still admitted, use null",
    "presenting_symptom": "ACUTE findings from Chief Complaint, HPI, and labs. Include: (1) Symptoms (nausea, vomiting, pain), (2) ACUTE diagnoses (AKI, acute kidney injury, sepsis). For this patient with GI symptoms: 'nausea/vomiting, epigastric pain, acute kidney injury'. Do NOT include chronic PMH conditions here - those go in pmh_relevant. 10-20 words.",
    "pmh_relevant": "CHRONIC conditions from PAST MEDICAL HISTORY only - NOT acute diagnoses. Example: 'CKD stage 3b' (chronic) goes here. 'AKI' or 'acute kidney injury' (acute) does NOT go here - it goes in presenting_symptom. 1-2 chronic conditions that complicate the acute presentation. If patient has AKI on CKD: put 'acute kidney injury' in presenting_symptom, put 'CKD stage 3b' in pmh_relevant. NEVER duplicate - if CKD is here, don't mention CKD in presenting_symptom.",
    "chief_complaint_short": "Brief symptom list from Chief Complaint section (3-8 words): 'worsening leg pain and tremors'",
    "hpi": "Copy VERBATIM from HPI section - do NOT paraphrase, do NOT change ages, do NOT add details. If HPI says '70-year-old', write '70-year-old' NOT '78-year-old'. Copy exactly 2-4 sentences.",
    "conditions": ["CHRONIC DISEASES ONLY from PAST MEDICAL HISTORY section. Extract EXACTLY as written. Do NOT include acute symptoms."],
    "medications": [
        {{"name": "ONLY drug names explicitly listed in MEDICATION or MAR sections", "dose": "Extract the NUMERIC dose (e.g., '20mg', '125mcg', '100mg') - NOT '1tablet' or '1cap'. If source shows 'escitalopram 20mg', write '20mg'. Look for mg, mcg, mL values.", "route": "PO/IV/etc or empty string if not listed", "frequency": "Copy VERBATIM - TID, BID, QD, Q6H, PRN, AT BEDTIME, etc."}}
    ],
    "lab_results": [
        {{"name": "test name", "value": "COPY THE EXACT NUMBER - if source shows 17.0, write 17.0 NOT 16.7. Character-for-character copy.", "unit": "unit", "date": "COPY THE EXACT DATE - if source shows 06/14/26, write 06/14/26 NOT 06/15/26. Character-for-character copy.", "flag": "'H' if value is HIGH/elevated (above normal range or flagged), 'L' if LOW (below normal range or flagged), otherwise BLANK"}}
    ],
    "vitals": {{
        "bp": "ONLY if documented (e.g. '120/80') - leave empty string if not in chart",
        "hr": "ONLY if documented - leave empty string if not in chart",
        "temp": "ONLY if documented - leave empty string if not in chart",
        "rr": "ONLY if documented - leave empty string if not in chart",
        "spo2": "ONLY if documented - leave empty string if not in chart"
    }},
    "insurance_name": "primary insurance/payer name ONLY if explicitly in document. Use empty string if not found.",
    "insurance_id": "INSURED ID ONLY if explicitly in document (look for INSURED ID:, Member ID). Use empty string if not found.",
    "insurance_group": "group number ONLY if explicitly stated. Use empty string if not found.",
    "insurance_address": "payer address ONLY if explicitly in document. Use empty string if not found.",
    "insurance_city": "payer city ONLY if explicitly stated. Use empty string if not found.",
    "insurance_state": "payer state ONLY if explicitly stated. Use empty string if not found.",
    "insurance_zip": "payer zip ONLY if explicitly stated. Use empty string if not found.",
    "facility_name": "hospital name",
    "attending_physician": "doctor name",
    "consults": ["List of specialty consults with key findings. Format: 'Vascular Surgery - recommended BKA due to nonhealing ulcer'. Include specialty name AND their recommendation/finding. ONLY include recommendations EXPLICITLY stated in the consult note - do NOT fabricate medication recommendations like 'adding Tramadol' unless that EXACT phrase appears."],
    "procedures": ["ONLY procedures that were actually COMPLETED DURING THIS ADMISSION with explicit documented results. Format: 'CT chest - bilateral pulmonary emboli identified', 'EGD - gastric ulcer visualized'. Do NOT include pending/ordered/planned procedures. Do NOT include HISTORICAL procedures from prior years. Do NOT fabricate results for tests that were only ordered."]
}}

CRITICAL INSTRUCTIONS:
- VITALS: Only include vitals that are EXPLICITLY documented with values. If chart only shows HR and SpO2, leave BP, temp, RR as empty strings. DO NOT fabricate "normal" values.
- PROCEDURES: ONLY include procedures COMPLETED DURING THIS ADMISSION with DOCUMENTED RESULTS. If a test is "ordered" or "pending" without results, do NOT include it. NEVER fabricate results like "normal" or "negative" for tests that were only ordered. Do NOT confuse HISTORICAL procedures (from prior years/admissions) with CURRENT procedures. A "2010 stress test: no ischemia" is history, not a current result.
- LAB VALUES: This is CRITICAL - lab hallucination is unacceptable. 
  STRICT RULES:
  1. Extract ONLY from structured LAB TABLES with visible columns (DATE | TEST | VALUE | UNIT | FLAG)
  2. For EACH lab, copy the DATE exactly as shown in the table (e.g., "06/15/26" not "06/18/26")
  3. Copy the VALUE character-for-character (if table shows "7.6", write "7.6" NOT "8.2" or "8")
  4. Do NOT fabricate dates - if you can't find a clear date, leave date field empty
  5. Do NOT average or synthesize values across multiple dates
  6. If you see labs from multiple dates (06/15, 06/14, 06/13), extract from the MOST RECENT date only
  7. VERIFY: Before outputting each lab, ask yourself "Did I see this EXACT number in the source?" If unsure, omit it.
- LAB FLAGS: Mark 'H' for HIGH/elevated values and 'L' for LOW values when ANY flag indicator is present - including 'H', 'L', 'HIGH', 'LOW', '*', '!', or abnormal indicators. Use clinical judgment: if a value is marked abnormal and is above normal range → 'H'; if below normal range → 'L'. ALWAYS include flags for clearly abnormal values like: hemoglobin <12, glucose >140, sodium <135 or >145, creatinine >1.2, eGFR <60.
- CONDITIONS: Extract ONLY from PAST MEDICAL HISTORY section, not from narrative or assessment.
- PLACE OF SERVICE: Determine based on HOW patient arrived, not current unit. If transported by EMS/ambulance → "Emergency Department". If walked in or direct admit → "Hospital". The SERVICE code (MED, SURG, etc.) shows current unit, not arrival point.
- PRESENTING SYMPTOM / PMH - NO DUPLICATION ALLOWED:
  - presenting_symptom: ACUTE findings - symptoms + ACUTE diagnoses
    * Symptoms: nausea, vomiting, pain, fall
    * ACUTE diagnoses: AKI (acute kidney injury), sepsis, pneumonia
    * Example: "nausea/vomiting with epigastric pain and acute kidney injury"
  - pmh_relevant: CHRONIC conditions from PMH ONLY
    * CKD stage 3b (chronic) → goes HERE
    * AKI (acute) → does NOT go here
    * NEVER DUPLICATE: if pmh_relevant has "CKD", do NOT put kidney-related terms in presenting_symptom
    Example for AKI on CKD patient:
      presenting_symptom: "nausea/vomiting with epigastric pain and acute kidney injury"
      pmh_relevant: "CKD stage 3b"
  - The hook will be assembled as: "{{presenting_symptom}} with {{pmh_relevant}}, requiring hospital-level evaluation..."
  - Do NOT include patient demographics, age, gender in these fields
- MEDICATIONS: Extract NUMERIC doses (20mg, 125mcg, 100mg), NOT "1tablet" or "1cap".
  
  FAITHFULNESS RULES:
  - ONLY include symptoms, findings, and treatments EXPLICITLY documented in the chart text
  - Do NOT say "failed antibiotics" or "failed outpatient treatment" unless explicitly stated - if patient denies recent antibiotics, do NOT claim they failed
  - Do NOT invent wound descriptions (drainage, exposed tissue, etc.) unless EXACTLY quoted in the chart
  - Do NOT mention treatments (wound vac, PICC line, etc.) unless documented
  - Lab values can be cited but do NOT claim they are "elevated" unless flagged as High (H) or explicitly stated as abnormal
  - If clinical details are sparse, keep the hook brief rather than inventing severity indicators
- ONLY include information explicitly stated in the chart - do NOT infer or make up findings
- Do NOT add clinical findings like tachycardia, fever, hypotension unless explicitly documented with values
- Extract conditions EXACTLY as written in PAST MEDICAL HISTORY
- MEDICATIONS: Extract NUMERIC doses like "20mg", "125mcg", "100mg" - NOT "1tablet" or "1cap". Look at the full medication line for the mg/mcg value. Example: "escitalopram 20mg 1tablet daily" → dose is "20mg", not "1tablet". ONLY extract medications whose drug names are EXPLICITLY listed in the chart. Do NOT infer medications from treatment descriptions. If no medication list is visible, return an empty array.
- VITALS: Only extract vitals if you see actual numeric values (e.g., "BP 120/80"). Do NOT fabricate normal values.
- HPI: Copy text VERBATIM from the chart. Do NOT rephrase or add details not present.

FINAL VERIFICATION - For each field, ask: "Can I point to the EXACT text in the source where I found this?"

If the answer is NO, DELETE THAT FIELD VALUE and leave it empty.

Check each field:
1. LAB VALUES: Can I point to where I see this exact number in the source? (If no → delete)
2. LAB DATES: Can I point to where I see this exact date in the source? (If no → delete)
3. MEDICATION DOSES: Is this the NUMERIC dose (20mg, 125mcg) not "1tablet"? (If "1tablet" → find the mg dose)
4. MEDICATION FREQUENCIES: Can I point to where I see this exact frequency in the source? (If no → delete)
5. HPI: Did I copy this VERBATIM or did I change words/numbers? (If changed → copy verbatim instead)
6. PMH_RELEVANT: Is this condition ACTUALLY in the PMH section? (If not found → leave EMPTY)

REMEMBER: This is a patient's medical record. Fabrication is unacceptable.
Empty fields are safe. Fabricated fields are dangerous.

CHART TEXT:
{text[:100000]}

Return ONLY valid JSON, no other text."""

        # Create fresh client to pick up refreshed AWS credentials
        bedrock = self._get_bedrock_client()
        response = bedrock.converse(
            modelId=self.model_id,
            messages=[{"role": "user", "content": [{"text": prompt}]}],
            inferenceConfig={"maxTokens": 4000, "temperature": 0.0}  # Zero temp for exact extraction
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
                smart_excerpts = self._build_langfuse_excerpts(data, text, max_chars=50000)
                
                # Format source evidence as plain text for Langfuse evaluator
                # This allows evaluator to use {{input}} directly without nested JSON
                source_text_for_eval = f"""=== PATIENT DEMOGRAPHICS ===
{smart_excerpts.get('patient_demographics', '')}

=== HPI / CHIEF COMPLAINT ===
{smart_excerpts.get('hpi_evidence', '(Not found in document)')}

=== CONDITIONS EVIDENCE ===
{chr(10).join(smart_excerpts.get('conditions_evidence', []))}

=== MEDICATIONS EVIDENCE ===
{chr(10).join(smart_excerpts.get('medications_evidence', []))}

=== LAB VALUES EVIDENCE ===
{chr(10).join(smart_excerpts.get('lab_values_evidence', []))}

{smart_excerpts.get('summary', '')}"""
                
                # Log what we're sending for debugging
                logger.info(f"Langfuse input length: {len(source_text_for_eval)} chars")
 
                logger.debug(f"Langfuse input preview: {source_text_for_eval[:500]}...")
                
                langfuse_trace = langfuse_client.trace(
                    name="pdf-chart-extraction",
                    input=source_text_for_eval,  # Plain text - evaluator uses {{input}}
                    output=data,  # Extracted JSON - evaluator uses {{output}}
                    metadata={
                        "model": self.model_id,
                        "pdf_length_chars": len(text),
                        "faithfulness_score": faithfulness_result["faithfulness_score"],
                        "conditions_validated": len(faithfulness_result["conditions_validated"]),
                        "conditions_flagged": len(faithfulness_result["conditions_flagged"]),
                        "medications_validated": len(faithfulness_result["medications_validated"]),
                        "medications_flagged": len(faithfulness_result["medications_flagged"]),
                    }
                )
                
                # Store trace ID for later score fetching
                try:
                    self._last_trace_ids["pdf_extractor"] = langfuse_trace.id
                except:
                    pass  # Langfuse SDK version may vary
                
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
        
        if faithfulness_result["lab_results_flagged"]:
            flagged_labs = [f"{l.get('name')}={l.get('value')}" for l in faithfulness_result['lab_results_flagged'][:5]]
            logger.warning(f"REMOVING {len(faithfulness_result['lab_results_flagged'])} hallucinated lab values: {flagged_labs}")
            # Keep only validated labs
            data["lab_results"] = faithfulness_result["lab_results_validated"]
        
        # ADDITIONAL VALIDATION: Check HPI for inconsistent ages
        hpi = data.get("hpi", "")
        source_lower = text.lower()
        if hpi:
            # Find all age mentions in HPI (e.g., "70-year-old", "78 year old", "70 y/o")
            hpi_ages = re.findall(r'(\d{1,3})[\s-]*(?:year|yr|y)[\s/-]*old', hpi.lower())
            for age in hpi_ages:
                # Check if this exact age pattern appears in source
                age_pattern = rf'{age}[\s-]*(?:year|yr|y)[\s/-]*old'
                if not re.search(age_pattern, source_lower):
                    # Age in HPI doesn't appear in source - likely fabricated
                    logger.warning(f"HPI contains fabricated age '{age}-year-old' - clearing HPI")
                    data["hpi"] = ""  # Clear fabricated HPI
                    break
        
        # ADDITIONAL VALIDATION: Check medication frequencies against source
        for med in data.get("medications", []):
            freq = med.get("frequency", "")
            med_name = med.get("name", "").lower()
            if freq and med_name:
                freq_lower = freq.lower()
                # Check if this frequency appears near the medication name in source
                med_idx = source_lower.find(med_name[:5]) if len(med_name) >= 5 else -1
                if med_idx >= 0:
                    # Get context around medication (300 chars)
                    start = max(0, med_idx - 50)
                    end = min(len(source_lower), med_idx + 300)
                    context = source_lower[start:end]
                    
                    # Check if extracted frequency appears in context
                    if freq_lower not in context:
                        # Also check for common translations
                        freq_found = False
                        if freq_lower in ["bid", "twice daily", "2x daily"]:
                            freq_found = any(f in context for f in ["bid", "twice", "2x", "two times"])
                        elif freq_lower in ["tid", "three times daily", "3x daily"]:
                            freq_found = any(f in context for f in ["tid", "three times", "3x"])
                        elif freq_lower in ["qid", "four times daily", "4x daily"]:
                            freq_found = any(f in context for f in ["qid", "four times", "4x"])
                        elif freq_lower in ["daily", "once daily", "qd"]:
                            freq_found = any(f in context for f in ["daily", "once", "qd", "qday"])
                        
                        if not freq_found:
                            logger.warning(f"Medication frequency '{freq}' for '{med_name}' not found in source - clearing")
                            med["frequency"] = ""
        
        # Sanitize hook component fields if hallucinated phrases detected
        if faithfulness_result.get("chief_complaint_hallucinations"):
            phrases = faithfulness_result["chief_complaint_hallucinations"]
            logger.warning(f"Hook components contain hallucinated phrases: {phrases}")
            
            # Clean each component field separately
            for field_name in ["presenting_symptom", "pmh_relevant"]:
                field_value = data.get(field_name, "")
                if not field_value:
                    continue
                    
                for phrase in phrases:
                    if phrase.lower() in field_value.lower():
                        # Remove the hallucinated phrase
                        field_value = re.sub(re.escape(phrase), "", field_value, flags=re.IGNORECASE)
                
                # Clean up artifacts
                field_value = re.sub(r'\s+', ' ', field_value).strip()
                field_value = re.sub(r'\s*,\s*,\s*', ', ', field_value)
                field_value = re.sub(r'^[,.\s]+', '', field_value)
                field_value = re.sub(r'[,.\s]+$', '', field_value)
                
                data[field_name] = field_value
                
            logger.info(f"Cleaned hook component fields by removing fabricated phrases")
        
        # Sanitize HPI if hallucinated phrases detected
        if faithfulness_result.get("hpi_hallucinations"):
            phrases = faithfulness_result["hpi_hallucinations"]
            logger.warning(f"HPI contains hallucinated phrases: {phrases}")
            # Strip the fabricated phrases from HPI instead of replacing entirely
            # HPI is longer, so try to salvage what we can
            hpi = data.get("hpi", "")
            for phrase in phrases:
                # Case-insensitive removal of hallucinated phrases
                hpi = re.sub(re.escape(phrase), "", hpi, flags=re.IGNORECASE)
            # Clean up multiple spaces and awkward punctuation
            hpi = re.sub(r'\s+', ' ', hpi).strip()
            hpi = re.sub(r'\s*,\s*,\s*', ', ', hpi)  # Fix double commas
            hpi = re.sub(r'\s*\.\s*\.', '.', hpi)  # Fix double periods
            data["hpi"] = hpi
            logger.info(f"Cleaned HPI by removing fabricated phrases: {phrases}")
        
        # Sanitize lab flags - some flags are logically impossible
        # e.g., eGFR flagged "H" makes no sense (low eGFR is concerning, not high)
        for lab in data.get("lab_results", []):
            name_lower = lab.get("name", "").lower()
            flag = lab.get("flag", "")
            value_str = str(lab.get("value", ""))
            
            try:
                value = float(value_str) if value_str else None
            except ValueError:
                value = None
            
            # CORRECT lab flags based on clinical knowledge of normal ranges
            # The LLM sees * in the source and guesses H/L, but often gets direction wrong
            
            # eGFR: < 60 is LOW (kidney dysfunction), never "high"
            if "egfr" in name_lower:
                if flag == "H":
                    logger.warning(f"Correcting flag: eGFR={value_str} cannot be 'H' - low is the concern")
                    lab["flag"] = "L" if value and value < 60 else ""
                elif value and value < 60 and not flag:
                    lab["flag"] = "L"  # Add missing low flag
            
            # HGB (Hemoglobin): < 12 is LOW (anemia), > 17 is HIGH
            if ("hgb" in name_lower or "hemoglobin" in name_lower):
                if flag == "H" and value and value < 14:
                    logger.warning(f"Correcting flag: HGB={value_str} flagged 'H' but value is low")
                    lab["flag"] = "L" if value < 12 else ""
                elif value and value < 12 and not flag:
                    lab["flag"] = "L"
            
            # HCT (Hematocrit): < 36 is LOW, > 50 is HIGH  
            if ("hct" in name_lower or "hematocrit" in name_lower):
                if flag == "H" and value and value < 45:
                    logger.warning(f"Correcting flag: HCT={value_str} flagged 'H' but value is low")
                    lab["flag"] = "L" if value < 36 else ""
                elif value and value < 36 and not flag:
                    lab["flag"] = "L"
            
            # Glucose: < 70 is LOW (hypoglycemia), > 100 fasting / > 140 random is HIGH
            if "glucose" in name_lower or "gluc" in name_lower:
                if flag == "L" and value and value > 100:
                    logger.warning(f"Correcting flag: Glucose={value_str} flagged 'L' but value is high")
                    lab["flag"] = "H"
                elif value and value > 140 and not flag:
                    lab["flag"] = "H"
        
        # Clean up suspicious medication frequencies
        # Common fabrications: BID when source says PRN, 4X/day when source says TID
        for med in data.get("medications", []):
            if med.get("frequency_flagged"):
                logger.warning(f"Clearing suspicious frequency for {med.get('name')}: was '{med.get('frequency')}'")
                med["frequency"] = ""  # Clear to avoid misinformation
                del med["frequency_flagged"]
        
        # Validate medication doses and frequencies against source text
        source_lower = text.lower()
        for med in data.get("medications", []):
            med_name = med.get("name", "").lower()
            dose = med.get("dose", "")
            freq = med.get("frequency", "")
            
            # Skip if no med name
            if not med_name:
                continue
            
            # Find medication context in source (100 chars around mention)
            med_idx = source_lower.find(med_name[:5])  # First 5 chars of drug name
            if med_idx == -1:
                continue
            
            med_context = source_lower[max(0, med_idx-50):med_idx+150]
            
            # Check for fractional tablet doses - these are often fabricated
            # Real prescriptions say "1 tablet" or "2 tablets", rarely "1.5 tablet"
            if "1.5 tablet" in dose.lower() or "0.5 tablet" in dose.lower():
                # Check if fractional dose is actually in source
                if "1.5" not in med_context and "½" not in med_context and "half" not in med_context:
                    logger.warning(f"Suspicious fractional dose for {med_name}: '{dose}' - not found in source context")
                    # Try to extract real dose from context
                    dose_match = re.search(r'(\d+)\s*(mg|tablet|tab|ml)', med_context)
                    if dose_match:
                        med["dose"] = dose_match.group(0)
                        logger.info(f"Corrected dose to: {med['dose']}")
            
            # Check for frequency mismatches
            # "4X/day" should be "QID" in medical terms, but if source says "TID", that's 3x
            freq_lower = freq.lower()
            if "4x" in freq_lower or "4 times" in freq_lower or "qid" in freq_lower:
                # Check if source actually says QID/4x or something different
                if "tid" in med_context or "three times" in med_context or "3 times" in med_context:
                    logger.warning(f"Frequency mismatch for {med_name}: extracted '{freq}' but source shows TID")
                    med["frequency"] = "TID"
            
            # Validate common drugs with known dosing
            if "sinemet" in med_name or "carbidopa" in med_name or "levodopa" in med_name:
                # Sinemet is usually 1 tablet TID, not 1.5 tablet 4X/day
                if "tid" in med_context and ("4x" in freq_lower or "qid" in freq_lower):
                    logger.warning(f"Sinemet frequency correction: '{freq}' -> 'TID' (source shows TID)")
                    med["frequency"] = "TID"
        
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
        
        # Extract place of service - check for EMS/ambulance transport first
        # If patient arrived by EMS, they started in the ER regardless of current unit
        chief_complaint = data.get("chief_complaint", "").lower()
        hpi = data.get("hpi", "").lower()
        arrival_text = chief_complaint + " " + hpi
        
        ems_keywords = ["ems", "ambulance", "911", "transported by", "brought by ambulance", "arrived via ems"]
        arrived_by_ems = any(kw in arrival_text for kw in ems_keywords)
        
        if arrived_by_ems:
            # Patient arrived by EMS - initial arrival point was Emergency Department
            data["place_of_service"] = "Emergency Department"
            data["place_of_service_raw_code"] = "EMS"
            logger.info(f"Set place_of_service = 'Emergency Department' (EMS transport detected in chief complaint)")
        else:
            # Fall back to SERVICE code extraction for current unit
            raw_code, service_code_pos = self._extract_service_code(text)
            logger.info(f"SERVICE place_of_service result: '{service_code_pos}' (raw code: '{raw_code}')")
            if service_code_pos:
                data["place_of_service"] = service_code_pos
                data["place_of_service_raw_code"] = raw_code
                logger.info(f"Set data['place_of_service'] = '{service_code_pos}' (raw: '{raw_code}')")
        
        # Fallback regex extraction for authorization number if LLM didn't capture it
        if not data.get("authorization_number"):
            auth_match = re.search(r'PrimaryCoverageAuthorizationNumber[:\s]*([A-Z]?\d+)', text, re.IGNORECASE)
            if auth_match:
                data["authorization_number"] = auth_match.group(1)
                logger.info(f"Extracted authorization_number via regex: {data['authorization_number']}")
        else:
            logger.info(f"LLM extracted authorization_number: {data.get('authorization_number')}")
        
        # Log consults/procedures extraction
        consults = data.get("consults", [])
        procedures = data.get("procedures", [])
        logger.info(f"LLM extracted consults ({len(consults)}): {consults}")
        logger.info(f"LLM extracted procedures ({len(procedures)}): {procedures}")
        
        # ASSEMBLE HOOK from structured parts (no labs - those go in midnight reasons)
        # Pattern: "{presenting_symptom} with {pmh_relevant}, requiring hospital-level evaluation, treatment, and monitoring."
        presenting_symptom = data.get("presenting_symptom", "").strip()
        pmh_relevant = data.get("pmh_relevant", "").strip()
        
        if presenting_symptom:
            if pmh_relevant:
                # Full format: symptom with PMH context
                assembled_hook = f"{presenting_symptom} with {pmh_relevant}, requiring hospital-level evaluation, treatment, and monitoring."
            else:
                # No relevant PMH - just symptom
                assembled_hook = f"{presenting_symptom}, requiring hospital-level evaluation, treatment, and monitoring."
            
            data["chief_complaint"] = assembled_hook
            logger.info(f"Assembled hook: {assembled_hook[:120]}...")
        else:
            # Fallback to chief_complaint_short if no presenting_symptom
            short = data.get("chief_complaint_short", "")
            if short:
                data["chief_complaint"] = f"{short}, requiring hospital-level evaluation, treatment, and monitoring."
                logger.warning(f"No presenting_symptom, using chief_complaint_short fallback")
        
        # Convert to dataclass
        extracted = ExtractedChartData(
            original_name=data.get("original_name", ""),
            original_dob=data.get("original_dob", ""),
            original_mrn=data.get("original_mrn", ""),
            account_number=data.get("account_number", ""),
            authorization_number=data.get("authorization_number", ""),
            gender=data.get("gender", ""),
            age=data.get("age", 0),
            admission_date=data.get("admission_date", ""),
            observation_date=data.get("observation_date", ""),
            inpatient_date=data.get("inpatient_date", ""),
            discharge_date=data.get("discharge_date", ""),
            place_of_service=data.get("place_of_service", "Hospital"),
            place_of_service_raw_code=data.get("place_of_service_raw_code", ""),
            chief_complaint_short=data.get("chief_complaint_short", ""),
            chief_complaint=data.get("chief_complaint", ""),
            presenting_symptom=data.get("presenting_symptom", ""),
            pmh_relevant=data.get("pmh_relevant", ""),
            hpi=data.get("hpi", ""),
            conditions=data.get("conditions", []),
            medications=data.get("medications", []),
            lab_results=data.get("lab_results", []),
            vitals=data.get("vitals", {}),
            consults=data.get("consults", []),
            procedures=data.get("procedures", []),
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
    
    def _normalize_name(self, name: str) -> str:
        """Convert 'Last, First' format to 'First Last' format."""
        if not name:
            return ""
        name = name.strip()
        # Check for "Last, First" or "Last, First Middle" pattern
        if "," in name:
            parts = [p.strip() for p in name.split(",", 1)]
            if len(parts) == 2:
                last = parts[0]
                first_middle = parts[1]
                return f"{first_middle} {last}"
        return name
    
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
            patient_name = self._normalize_name(extracted.original_name)
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
        discharge_date = self._normalize_date(extracted.discharge_date) if extracted.discharge_date else ""
        
        # ALWAYS calculate age from DOB - don't trust LLM extraction
        # This prevents LLM from copying wrong age from narrative text like "70-year-old female" in consults
        age = 0
        if dob:
            try:
                birth = datetime.strptime(dob, "%Y-%m-%d")
                age = (datetime.now() - birth).days // 365
                logger.info(f"Calculated age from DOB: {age}")
            except:
                pass
        
        # Fallback to extracted age only if calculation failed
        if not age and extracted.age:
            age = extracted.age
            logger.warning(f"Using LLM-extracted age (DOB calculation failed): {age}")
        
        # Build PatientStayData
        patient_data = PatientStayData(
            patient_id=patient_id,
            patient_name=patient_name,
            dob=dob,
            gender=extracted.gender,
            age=age,
            account_number=extracted.account_number,
            authorization_number=extracted.authorization_number,
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
            discharge_date=discharge_date,
            encounter_status="in-progress" if not discharge_date else "finished",
            place_of_service=extracted.place_of_service or "Hospital",
            place_of_service_raw_code=extracted.place_of_service_raw_code or "",
            chief_complaint_short=extracted.chief_complaint_short,
            chief_complaint=extracted.chief_complaint,
            presenting_symptom=extracted.presenting_symptom,
            pmh_relevant=extracted.pmh_relevant,
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
            clinical_notes=[extracted.hpi] if extracted.hpi else [],
            consults=extracted.consults,
            procedures=extracted.procedures
        )
        
        return patient_data
    
    def _extract_real_chief_complaint_from_source(self, source_text: str) -> str:
        """Extract the REAL chief complaint directly from PDF source text.
        
        Used when LLM extraction contains hallucinations. This does simple
        pattern matching to find the actual chief complaint from the source.
        """
        if not source_text:
            return ""
        
        source_lower = source_text.lower()
        
        # Look for common chief complaint markers in order of specificity
        patterns = [
            ("chief complaint:", 300),
            ("chief complaint", 300),
            ("cc:", 200),
            ("reason for visit:", 300),
            ("reason for visit", 300),
            ("presenting complaint:", 300),
            ("presenting complaint", 300),
        ]
        
        for pattern, max_len in patterns:
            idx = source_lower.find(pattern)
            if idx >= 0:
                # Start after the header
                start = idx + len(pattern)
                # Find the end - next section header or double newline
                excerpt = source_text[start:start + max_len].strip()
                
                # Clean up - remove leading colons/whitespace
                excerpt = excerpt.lstrip(": \t\n")
                
                # Find natural endpoint
                for end_marker in ["\n\n", "\nHISTORY", "\nHPI", "\nPAST", "\nALLERGIES", 
                                   "\nMEDICATIONS", "\nVITALS", "\nPHYSICAL", "\nDIAGNOSIS"]:
                    marker_idx = excerpt.upper().find(end_marker)
                    if marker_idx > 10:  # Don't cut too short
                        excerpt = excerpt[:marker_idx].strip()
                        break
                
                # Also check for lowercase section starts
                for end_marker in ["\nhistory", "\npast", "\nallergies"]:
                    marker_idx = excerpt.lower().find(end_marker)
                    if marker_idx > 10:
                        excerpt = excerpt[:marker_idx].strip()
                        break
                
                if excerpt and len(excerpt) > 5:
                    # Clean up trailing punctuation
                    excerpt = excerpt.rstrip(".,;:")
                    logger.info(f"Extracted real chief complaint from source: {excerpt[:80]}...")
                    return excerpt
        
        return ""

    def _extract_raw_chief_complaint(self, source_text: str) -> str:
        """Extract raw chief complaint / HPI / surgical history from PDF text for evaluator.
        
        This finds the source material that the LLM should synthesize into a narrative hook.
        Returns up to 15000 chars of relevant source text to allow proper verification.
        """
        if not source_text:
            return ""
        
        source_lower = source_text.lower()
        excerpts = []
        
        # Common section headers - expanded to include imaging, medications, surgical history
        section_patterns = [
            ("chief complaint", 500),
            ("reason for visit", 500),
            ("cc:", 300),
            ("history of present illness", 800),
            ("hpi:", 600),
            ("presenting complaint", 500),
            ("past medical history", 800),
            ("pmh:", 600),
            ("surgical history", 1000),
            ("past surgical", 1000),
            ("procedure", 800),
            ("bypass", 400),
            ("amputation", 400),
            # Imaging results - specific markers found in PDFs
            ("ct lumbar spine", 1200),
            ("mri lumbar spine", 1200),
            ("ct abdomen", 1200),
            ("ct pelvis", 1200),
            ("x-ray lumbar", 800),
            ("xray lumbar", 800),
            ("imaging studies", 600),
            # Direct condition terms - capture context around these
            ("spondylolysis", 500),
            ("anterolisthesis", 500),
            ("stenosis", 400),
            ("degenerative", 400),
            ("fat necrosis", 400),
            ("necrosis", 400),
            # Infections and diagnoses
            ("uti", 300),
            ("urinary tract infection", 400),
            ("present on admission", 600),
            # Medications & clinical course
            ("decadron", 400),
            ("dexamethasone", 400),
            ("started on", 300),
            ("clinical course", 600),
            ("hospital day", 400),
        ]
        
        for pattern, max_len in section_patterns:
            idx = source_lower.find(pattern)
            if idx >= 0:
                # Extract text after the header
                start = max(0, idx - 50)  # Include some context before
                end = min(len(source_text), idx + max_len)
                excerpt = source_text[start:end].strip()
                
                # Find natural end (next section header or double newline)
                for end_marker in ["\n\n", "ALLERGIES", "SOCIAL HISTORY", "REVIEW OF SYSTEMS", "FAMILY HISTORY"]:
                    marker_idx = excerpt.upper().find(end_marker)
                    if marker_idx > 50:  # Don't cut too short
                        excerpt = excerpt[:marker_idx].strip()
                        break
                
                if excerpt and excerpt not in excerpts:
                    excerpts.append(excerpt)
        
        # Combine excerpts up to 15000 chars
        result = " | ".join(excerpts)
        return result[:15000] if len(result) > 15000 else result
    
    def _log_hook_to_langfuse(self, patient_data: PatientStayData, source_text: str = ""):
        """Log the hook (chief complaint) as a separate generation for Langfuse evaluation.
        
        This triggers the hook-evaluator to assess the extracted chief complaint.
        Uses the full chief_complaint (same as what goes in the appeal letter).
        
        Args:
            patient_data: The extracted patient data with synthesized hook
            source_text: Raw PDF text - used to extract source chief complaint/HPI for evaluator
        """
        if not langfuse_client:
            return
        
        try:
            # Use full chief_complaint - this matches what goes in the document (app.py line 776)
            hook = patient_data.chief_complaint or patient_data.chief_complaint_short
            if not hook:
                return
            
            # Extract raw chief complaint / HPI from source PDF text for evaluator comparison
            raw_chief_complaint = self._extract_raw_chief_complaint(source_text) if source_text else ""
            
            # Input context for evaluator - use RAW source text, not synthesized hook
            # Include labs so evaluator can verify lab values cited in hook
            input_data = {
                "raw_chief_complaint": raw_chief_complaint,  # Source text from PDF
                "conditions": patient_data.conditions[:5],
                "lab_results": patient_data.lab_results[:20]  # Include labs for faithfulness check
            }
            
            # Debug: log what we're sending to Langfuse
            logger.info(f"Langfuse hook input_data labs count: {len(patient_data.lab_results)}")
            logger.info(f"Langfuse hook input_data labs preview: {patient_data.lab_results[:3]}")
            
            # Create a trace for hook evaluation
            # Send hook as plain string - evaluators typically use {{output}} directly
            trace = langfuse_client.trace(
                name="hook",
                input=input_data,
                output=hook,  # Plain string for {{output}} in evaluator
                metadata={
                    "patient_name": patient_data.patient_name,
                    "component": "hook"
                }
            )
            
            # Store trace ID for later score fetching
            try:
                self._last_trace_ids["hook"] = trace.id
            except:
                pass  # Langfuse SDK version may vary
            
            # Create generation for hook evaluator - also plain string
            generation = trace.generation(
                name="generation",
                model="claude-sonnet-4-20250514",
                input=input_data,
                output=hook  # Plain string - evaluator uses {{output}}
            )
            generation.end()
            
            # Log context size for debugging
            logger.info(f"Langfuse hook input: {len(raw_chief_complaint)} chars source context")
            
            # Add heuristic scores
            # Target: 70-90 words for new 2-paragraph format. Penalty for >120 words
            hook_len = len(hook)
            hook_words = len(hook.split())
            if hook_words < 50:
                length_score = hook_words / 50 * 0.5  # Too short
            elif hook_words <= 100:
                length_score = 1.0  # Ideal range (70-100 words)
            elif hook_words <= 120:
                length_score = 0.8  # Acceptable but long
            else:
                length_score = max(0.4, 0.8 - (hook_words - 120) / 100)  # Penalty for >120 words
            trace.score(
                name="hook_length",
                value=length_score,
                comment=f"Hook: {hook_words} words (ideal: 70-100, penalty >120)"
            )
            
            # Check for past tense (simple heuristic)
            past_tense_indicators = ["presented", "complained", "reported", "experienced", "had", "was", "were", "became", "arrived", "transported"]
            past_tense_found = any(ind in hook.lower() for ind in past_tense_indicators)
            trace.score(
                name="hook_past_tense",
                value=1.0 if past_tense_found else 0.5,
                comment=f"Past tense: {'yes' if past_tense_found else 'no'}"
            )
            
            # Check for rich narrative context (location, arrival mode, etc.)
            context_indicators = ["ems", "ambulance", "emergency", "hospital", "restaurant", "home", "arrived", "transported", "alert", "oriented"]
            context_found = sum(1 for ind in context_indicators if ind in hook.lower())
            context_score = min(1.0, context_found / 4)  # Want at least 4 context elements
            trace.score(
                name="hook_context",
                value=context_score,
                comment=f"Narrative context: {context_found} elements found"
            )
            
            langfuse_client.flush()
            logger.info("Langfuse hook generation logged")
        except Exception as e:
            logger.error(f"Langfuse hook logging error: {e}")
    
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
        
        # Log hook to Langfuse for evaluation - pass source text for raw chief complaint extraction
        self._log_hook_to_langfuse(patient_data, source_text=text)
        
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

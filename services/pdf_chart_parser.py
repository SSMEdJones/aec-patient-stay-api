"""
PDF Chart Parser with De-identification

Extracts clinical data from PDF patient charts, de-identifies PHI,
and creates structured PatientStayData for testing.
"""
import re
import json
import random
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Dict, List
from dataclasses import dataclass, field, asdict

import pdfplumber
import boto3

from services.midnight_reason_generator import PatientStayData


# Synthetic name pools for de-identification
FIRST_NAMES_F = ["Maria", "Jennifer", "Linda", "Patricia", "Elizabeth", "Susan", "Dorothy", "Helen", "Nancy", "Betty"]
FIRST_NAMES_M = ["James", "Robert", "Michael", "William", "David", "Richard", "Joseph", "Thomas", "Charles", "Daniel"]
LAST_NAMES = ["Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller", "Davis", "Rodriguez", "Martinez"]


@dataclass
class ExtractedChartData:
    """Raw extracted data before de-identification."""
    # Original PHI (will be replaced)
    original_name: str = ""
    original_dob: str = ""
    original_mrn: str = ""
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
    chief_complaint: str = ""
    hpi: str = ""
    conditions: List[str] = field(default_factory=list)
    medications: List[Dict] = field(default_factory=list)
    lab_results: List[Dict] = field(default_factory=list)
    vitals: Dict = field(default_factory=dict)
    assessment_plan: str = ""
    clinical_notes: List[str] = field(default_factory=list)
    
    # Facility info
    facility_name: str = ""
    attending_physician: str = ""


class PDFChartParser:
    """
    Parses PDF clinical charts, extracts data, and de-identifies PHI.
    
    Usage:
        parser = PDFChartParser()
        patient_data = parser.parse_and_deidentify("chart.pdf")
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
    
    def extract_text(self, pdf_path: str) -> str:
        """Extract all text from PDF."""
        full_text = ""
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                text = page.extract_text() or ""
                full_text += text + "\n\n"
        return full_text
    
    def _extract_with_llm(self, text: str) -> ExtractedChartData:
        """Use Claude to extract structured data from clinical text."""
        prompt = f"""Extract clinical data from this patient chart. Return JSON with these fields:

{{
    "original_name": "patient's full name",
    "original_dob": "date of birth (MM/DD/YYYY)",
    "original_mrn": "medical record number",
    "gender": "M or F",
    "age": 0,
    "admission_date": "first admission/observation date (MM/DD/YYYY)",
    "observation_date": "date patient was on observation status - usually first day (MM/DD/YYYY or null if not mentioned)",
    "inpatient_date": "date patient transitioned to inpatient status - usually day after observation (MM/DD/YYYY or null if not mentioned)",
    "chief_complaint": "synthesize ALL HPI sections into one clinical presentation in PAST TENSE: start with 'complaints of [symptoms]', then 'The patient reported [timeline and details]', include failed home treatments, relevant history (e.g. prior procedures, never had colonoscopy), and end with 'Given [reasons], hospital admission was medically necessary for further evaluation and management.' (3-5 sentences)",
    "hpi": "history of present illness summary (1-2 sentences)",
    "conditions": ["CHRONIC DISEASES ONLY for past medical history: diabetes, hypertension, CKD, CHF, COPD, hypothyroidism, hyperthyroidism, hyperlipidemia, celiac disease, CAD, AFib, TIA/stroke, cancer history, etc. Do NOT include acute symptoms like nausea, vomiting, abdominal pain, constipation, diarrhea - those belong in chief_complaint. Mark HCC conditions."],
    "medications": [
        {{"name": "drug name", "dose": "dose", "route": "PO/IV/etc", "frequency": "frequency"}}
    ],
    "lab_results": [
        {{"name": "test name", "value": "result", "unit": "unit", "flag": "H/L/normal"}}
    ],
    "vitals": {{
        "bp": "120/80",
        "hr": "80",
        "temp": "98.6",
        "rr": "16",
        "spo2": "98"
    }},
    "facility_name": "hospital name",
    "attending_physician": "doctor name"
}}

CRITICAL INSTRUCTIONS:
- ONLY include information explicitly stated in the chart - do NOT infer or make up findings
- Do NOT add clinical findings like tachycardia, fever, hypotension unless explicitly documented with values
- Include ALL conditions mentioned, especially those marked (HCC)
- Extract all medications with their doses
- Extract abnormal lab values with H/L flags
- For chief_complaint: ONLY describe what is explicitly documented, no assumptions

CHART TEXT:
{text[:30000]}

Return ONLY valid JSON, no other text."""

        response = self.bedrock.converse(
            modelId=self.model_id,
            messages=[{"role": "user", "content": [{"text": prompt}]}],
            inferenceConfig={"maxTokens": 4000, "temperature": 0.1}
        )
        
        result_text = response["output"]["message"]["content"][0]["text"]
        
        # Parse JSON from response
        try:
            # Find JSON in response
            json_match = re.search(r'\{[\s\S]*\}', result_text)
            if json_match:
                data = json.loads(json_match.group())
            else:
                raise ValueError("No JSON found in response")
        except json.JSONDecodeError as e:
            print(f"JSON parse error: {e}")
            print(f"Response: {result_text[:500]}")
            data = {}
        
        # Convert to dataclass
        extracted = ExtractedChartData(
            original_name=data.get("original_name", ""),
            original_dob=data.get("original_dob", ""),
            original_mrn=data.get("original_mrn", ""),
            gender=data.get("gender", ""),
            age=data.get("age", 0),
            admission_date=data.get("admission_date", ""),
            observation_date=data.get("observation_date", ""),
            inpatient_date=data.get("inpatient_date", ""),
            chief_complaint=data.get("chief_complaint", ""),
            hpi=data.get("hpi", ""),
            conditions=data.get("conditions", []),
            medications=data.get("medications", []),
            lab_results=data.get("lab_results", []),
            vitals=data.get("vitals", {}),
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
    
    def deidentify(self, extracted: ExtractedChartData) -> PatientStayData:
        """
        Convert extracted data to de-identified PatientStayData.
        
        Only replaces patient name - preserves all dates and clinical content.
        """
        # Generate only a synthetic name
        synthetic_name = self._generate_synthetic_name(extracted.gender)
        
        # Normalize and randomize DOB (keep year for same age)
        dob_normalized = self._normalize_date(extracted.original_dob)
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
        
        # Build PatientStayData with real dates, synthetic name only
        patient_data = PatientStayData(
            patient_id=self._mask_mrn(extracted.original_mrn),
            patient_name=synthetic_name,
            dob=dob,
            gender=extracted.gender,
            age=age,
            admission_date=admission_date,
            observation_date=observation_date,
            inpatient_date=inpatient_date,
            encounter_status="in-progress",
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
    
    def parse_and_deidentify(self, pdf_path: str) -> PatientStayData:
        """
        Main method: Parse PDF, extract data, and return de-identified PatientStayData.
        
        Args:
            pdf_path: Path to PDF clinical chart
            
        Returns:
            De-identified PatientStayData ready for appeal letter generation
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
        
        print("De-identifying patient data...")
        patient_data = self.deidentify(extracted)
        
        print(f"Created de-identified patient: {patient_data.patient_name}")
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

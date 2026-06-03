"""
Mock Epic FHIR client for development without sandbox credentials.
Returns realistic test data matching Epic's FHIR R4 format.
"""
from datetime import datetime, timedelta
from typing import List, Optional
import random

from models import (
    ClinicalNote, PatientInfo, Encounter, NoteType, 
    DocumentStatus, Author
)


class MockEpicFHIRClient:
    """
    Mock client that returns realistic test data.
    Use this for development until Epic sandbox credentials are available.
    """
    
    # Test patients matching Epic's sandbox
    TEST_PATIENTS = {
        "egqBHVfQlt4Bw3XGXoxVxHg3": {
            "name": "Elijah Davis",
            "mrn": "203709",
            "dob": "1965-03-15",
            "gender": "male"
        },
        "eh2xYHuzl9nkSFVvV3osUHg3": {
            "name": "Olivia Roberts",
            "mrn": "203715",
            "dob": "1978-07-22",
            "gender": "female"
        },
        "e0w0LEDCYtfckT6N.CkJKCw3": {
            "name": "Warren McGinnis",
            "mrn": "203710",
            "dob": "1952-11-08",
            "gender": "male"
        },
    }
    
    def __init__(self):
        print("[MOCK MODE] Using MockEpicFHIRClient - no real API calls")
    
    async def get_patient(self, patient_id: str) -> PatientInfo:
        """Return mock patient data."""
        if patient_id in self.TEST_PATIENTS:
            p = self.TEST_PATIENTS[patient_id]
            name_parts = p["name"].split()
            return PatientInfo(
                id=patient_id,
                mrn=p["mrn"],
                given_name=name_parts[0],
                family_name=name_parts[-1],
                birth_date=datetime.fromisoformat(p["dob"]),
                gender=p["gender"],
            )
        
        # Default test patient
        return PatientInfo(
            id=patient_id,
            mrn="TEST-001",
            given_name="Test",
            family_name="Patient",
            birth_date=datetime(1970, 1, 1),
            gender="unknown",
        )
    
    async def get_encounter(self, encounter_id: str) -> Encounter:
        """Return mock encounter data."""
        return Encounter(
            id=encounter_id,
            status="finished",
            encounter_class="inpatient",
            start_date=datetime.now() - timedelta(days=5),
            end_date=datetime.now() - timedelta(days=1),
            location="Medical Unit 3B",
            department="Internal Medicine",
            reason_for_visit="Chest pain, shortness of breath",
            chief_complaint="Patient presents with 3-day history of chest pain",
            diagnoses=[
                "Acute coronary syndrome",
                "Hypertension",
                "Type 2 diabetes mellitus"
            ],
        )
    
    async def search_clinical_notes(
        self,
        patient_id: str,
        encounter_id: Optional[str] = None,
        note_types: Optional[List[str]] = None,
        date_from: Optional[datetime] = None,
        date_to: Optional[datetime] = None,
        max_results: int = 100
    ) -> List[ClinicalNote]:
        """Return mock clinical notes."""
        
        notes = [
            ClinicalNote(
                id="doc-001",
                status=DocumentStatus.CURRENT,
                type=NoteType.H_AND_P,
                type_code="34117-2",
                title="History and Physical",
                content=self._generate_hp_note(),
                content_type="text/plain",
                date=datetime.now() - timedelta(days=5),
                author=Author(id="prov-001", name="Dr. Sarah Chen", role="Attending Physician"),
                patient_id=patient_id,
                encounter_id=encounter_id,
            ),
            ClinicalNote(
                id="doc-002",
                status=DocumentStatus.CURRENT,
                type=NoteType.PROGRESS_NOTE,
                type_code="11506-3",
                title="Progress Note - Day 2",
                content=self._generate_progress_note(2),
                content_type="text/plain",
                date=datetime.now() - timedelta(days=3),
                author=Author(id="prov-001", name="Dr. Sarah Chen", role="Attending Physician"),
                patient_id=patient_id,
                encounter_id=encounter_id,
            ),
            ClinicalNote(
                id="doc-003",
                status=DocumentStatus.CURRENT,
                type=NoteType.PROGRESS_NOTE,
                type_code="11506-3",
                title="Progress Note - Day 4",
                content=self._generate_progress_note(4),
                content_type="text/plain",
                date=datetime.now() - timedelta(days=1),
                author=Author(id="prov-002", name="Dr. Michael Park", role="Hospitalist"),
                patient_id=patient_id,
                encounter_id=encounter_id,
            ),
            ClinicalNote(
                id="doc-004",
                status=DocumentStatus.CURRENT,
                type=NoteType.CONSULTATION,
                type_code="11488-4",
                title="Cardiology Consultation",
                content=self._generate_consult_note(),
                content_type="text/plain",
                date=datetime.now() - timedelta(days=4),
                author=Author(id="prov-003", name="Dr. James Wilson", role="Cardiologist"),
                patient_id=patient_id,
                encounter_id=encounter_id,
            ),
            ClinicalNote(
                id="doc-005",
                status=DocumentStatus.CURRENT,
                type=NoteType.DISCHARGE_SUMMARY,
                type_code="18842-5",
                title="Discharge Summary",
                content=self._generate_discharge_summary(),
                content_type="text/plain",
                date=datetime.now() - timedelta(hours=12),
                author=Author(id="prov-001", name="Dr. Sarah Chen", role="Attending Physician"),
                patient_id=patient_id,
                encounter_id=encounter_id,
            ),
        ]
        
        return notes[:max_results]
    
    def _generate_hp_note(self) -> str:
        return """HISTORY AND PHYSICAL

CHIEF COMPLAINT: Chest pain and shortness of breath x 3 days

HISTORY OF PRESENT ILLNESS:
This is a 58-year-old male with history of hypertension and type 2 diabetes who presents with 3-day history of intermittent chest pain described as pressure-like, radiating to left arm, associated with shortness of breath and diaphoresis. Pain is worse with exertion, partially relieved with rest. Denies syncope, palpitations, or lower extremity edema.

PAST MEDICAL HISTORY:
- Hypertension (10 years)
- Type 2 Diabetes Mellitus (5 years)
- Hyperlipidemia
- Former smoker (quit 2 years ago, 30 pack-year history)

MEDICATIONS:
- Lisinopril 20mg daily
- Metformin 1000mg twice daily
- Atorvastatin 40mg daily
- Aspirin 81mg daily

ALLERGIES: Penicillin (rash)

PHYSICAL EXAMINATION:
- Vitals: BP 158/92, HR 88, RR 18, Temp 98.6F, SpO2 96% on RA
- General: Alert, oriented, mild distress
- Cardiovascular: Regular rate and rhythm, S1/S2 normal, no murmurs
- Respiratory: Clear to auscultation bilaterally
- Extremities: No edema, pulses 2+ bilaterally

ASSESSMENT AND PLAN:
1. Acute coronary syndrome - rule out MI
   - Serial troponins q6h
   - Continuous telemetry
   - Cardiology consult
   - Start heparin drip per ACS protocol
2. Hypertension - poorly controlled
   - Continue home medications
   - Monitor closely
3. Type 2 DM - continue metformin, sliding scale insulin

Disposition: Admit to telemetry unit for cardiac monitoring and workup.
"""

    def _generate_progress_note(self, day: int) -> str:
        return f"""PROGRESS NOTE - HOSPITAL DAY {day}

SUBJECTIVE:
Patient reports improved chest pain since admission. Mild dyspnea on exertion persists. Sleeping well, appetite improved. No new complaints.

OBJECTIVE:
- Vitals: BP 142/84, HR 76, RR 16, SpO2 98% on RA
- General: Comfortable, in no acute distress
- Cardiovascular: Regular rate and rhythm, no murmurs
- Respiratory: Clear bilaterally
- Labs: Troponin trending down (0.08 -> 0.04), BMP stable

ASSESSMENT AND PLAN:
1. NSTEMI - improving
   - Continue anticoagulation
   - Cardiology following, cath scheduled for tomorrow
   - Continue telemetry monitoring
2. Hypertension - better controlled on current regimen
3. Type 2 DM - glucose well controlled on current regimen

Plan to continue current management. Will reassess after cardiac catheterization.
"""

    def _generate_consult_note(self) -> str:
        return """CARDIOLOGY CONSULTATION

REASON FOR CONSULTATION: Evaluation of acute coronary syndrome

HISTORY: As per primary team note. 58-year-old male with HTN, DM2, presenting with chest pain concerning for ACS.

CARDIAC HISTORY REVIEW:
- No prior MI or revascularization
- No known CAD
- Risk factors: HTN, DM, former smoker, hyperlipidemia, family history (father MI age 62)

ECG REVIEW: Sinus rhythm, rate 78. ST depression V4-V6. No ST elevation.

ECHO FINDINGS: EF 50-55%, mild hypokinesis of inferolateral wall, no significant valvular disease.

IMPRESSION:
NSTEMI with moderate risk features. Recommend cardiac catheterization within 24-48 hours.

RECOMMENDATIONS:
1. Continue dual antiplatelet therapy
2. Continue heparin anticoagulation
3. Schedule cardiac catheterization
4. Optimize medical therapy post-cath
5. Cardiac rehab referral upon discharge
6. Aggressive risk factor modification

Thank you for this consultation. Will follow along during hospitalization.

James Wilson, MD
Cardiology
"""

    def _generate_discharge_summary(self) -> str:
        return """DISCHARGE SUMMARY

ADMISSION DATE: [5 days ago]
DISCHARGE DATE: [Today]
LENGTH OF STAY: 5 days

PRINCIPAL DIAGNOSIS: Non-ST elevation myocardial infarction (NSTEMI)

SECONDARY DIAGNOSES:
1. Coronary artery disease - two-vessel disease
2. Hypertension
3. Type 2 diabetes mellitus
4. Hyperlipidemia

HOSPITAL COURSE:
Patient admitted with chest pain and elevated troponins consistent with NSTEMI. Initial management included anticoagulation, dual antiplatelet therapy, and medical optimization. Cardiac catheterization on hospital day 3 revealed two-vessel CAD with 80% LAD stenosis and 70% RCA stenosis. Successful PCI with drug-eluting stent to LAD performed. Post-procedure course uncomplicated with resolution of symptoms.

PROCEDURES:
- Cardiac catheterization with PCI and DES to LAD

DISCHARGE MEDICATIONS:
1. Aspirin 81mg daily (continue indefinitely)
2. Clopidogrel 75mg daily (continue for 12 months minimum)
3. Atorvastatin 80mg daily (increased from 40mg)
4. Metoprolol succinate 50mg daily (new)
5. Lisinopril 20mg daily (continue)
6. Metformin 1000mg twice daily (continue)

DISCHARGE INSTRUCTIONS:
- Follow up with cardiologist in 2 weeks
- Follow up with PCP in 1 week
- Cardiac rehabilitation referral placed
- Resume normal activities gradually
- No heavy lifting >10 lbs for 1 week
- Call or return if chest pain, shortness of breath, or bleeding

DISCHARGE CONDITION: Stable, improved

DISCHARGE DISPOSITION: Home

Sarah Chen, MD
Attending Physician
"""

"""
Generate synthetic clinical chart PDFs for testing the appeal letter generator.
Creates realistic-looking but completely fake patient data.

Usage:
    python scripts/generate_sample_pdfs.py --count 5 --output ./examples/synthetic
"""
import argparse
import random
from datetime import datetime, timedelta
from pathlib import Path
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.lib import colors

# Synthetic data pools
FIRST_NAMES_F = ["Maria", "Jennifer", "Linda", "Patricia", "Elizabeth", "Susan", "Dorothy", "Helen", "Nancy", "Betty",
                 "Margaret", "Sandra", "Ashley", "Kimberly", "Emily", "Donna", "Michelle", "Carol", "Amanda", "Melissa"]
FIRST_NAMES_M = ["James", "Robert", "Michael", "William", "David", "Richard", "Joseph", "Thomas", "Charles", "Daniel",
                 "Matthew", "Anthony", "Mark", "Donald", "Steven", "Paul", "Andrew", "Joshua", "Kenneth", "Kevin"]
LAST_NAMES = ["Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller", "Davis", "Rodriguez", "Martinez",
              "Hernandez", "Lopez", "Gonzalez", "Wilson", "Anderson", "Thomas", "Taylor", "Moore", "Jackson", "Martin"]

# Pediatric names (for Cardinal Glennon)
FIRST_NAMES_PEDS_M = ["Liam", "Noah", "Oliver", "Ethan", "Lucas", "Mason", "Logan", "Aiden", "Jackson", "Caden"]
FIRST_NAMES_PEDS_F = ["Emma", "Olivia", "Ava", "Sophia", "Isabella", "Mia", "Charlotte", "Amelia", "Harper", "Evelyn"]

CHRONIC_CONDITIONS = [
    "Hypertension", "Type 2 Diabetes Mellitus", "Hyperlipidemia", "Coronary Artery Disease",
    "Atrial Fibrillation", "Chronic Kidney Disease Stage 3", "COPD", "Hypothyroidism",
    "Osteoarthritis", "GERD", "Obesity", "Heart Failure with Preserved EF",
    "Peripheral Vascular Disease", "History of TIA", "Chronic Back Pain"
]

# Pediatric conditions
PEDIATRIC_CONDITIONS = [
    "Asthma", "Reactive Airway Disease", "ADHD", "Type 1 Diabetes Mellitus",
    "Seizure Disorder", "Congenital Heart Disease", "Sickle Cell Disease",
    "Autism Spectrum Disorder", "Food Allergies", "Eczema"
]

CHIEF_COMPLAINTS = [
    {
        "complaint": "abdominal pain and nausea",
        "hpi": "Patient presents with 2-day history of diffuse abdominal pain, worse in the right lower quadrant, associated with nausea and one episode of non-bloody emesis. Denies fever, diarrhea, or urinary symptoms. Pain is constant, rated 7/10, not relieved by OTC antacids.",
        "diagnosis": "Acute appendicitis",
        "workup": "CT abdomen/pelvis showed acute uncomplicated appendicitis. WBC elevated at 14.2. Patient taken to OR for laparoscopic appendectomy."
    },
    {
        "complaint": "chest pain and shortness of breath",
        "hpi": "Patient presents with substernal chest pressure radiating to left arm, onset 3 hours ago while at rest. Associated with diaphoresis and mild dyspnea. Has history of CAD with prior stent placement 2019. Took aspirin at home without relief.",
        "diagnosis": "NSTEMI",
        "workup": "Initial troponin 0.45, repeat 1.2. EKG showed ST depressions in V4-V6. Started on heparin drip, dual antiplatelet therapy. Cardiology consulted for cardiac catheterization."
    },
    {
        "complaint": "altered mental status and fever",
        "hpi": "Patient brought in by family for confusion and fever for past 24 hours. Normally alert and oriented at baseline. Family notes decreased oral intake and urinary incontinence. Temperature at home was 101.8F.",
        "diagnosis": "Urinary tract infection with sepsis",
        "workup": "UA positive for nitrites, leukocyte esterase, >100 WBC. Blood cultures pending. Lactate 2.8. Started on IV antibiotics and fluids per sepsis protocol."
    },
    {
        "complaint": "lower extremity swelling and pain",
        "hpi": "Patient presents with 4-day history of progressive left leg swelling and pain. Recently returned from cross-country flight 1 week ago. Denies chest pain or shortness of breath. No history of prior DVT/PE.",
        "diagnosis": "Deep vein thrombosis, left lower extremity",
        "workup": "Venous duplex showed occlusive thrombus in left femoral and popliteal veins. D-dimer elevated at 4500. Started on therapeutic anticoagulation."
    },
    {
        "complaint": "weakness and dizziness",
        "hpi": "Patient presents with generalized weakness and lightheadedness for 2 days. Reports dark, tarry stools for past 3 days. Denies hematemesis. Has history of NSAID use for chronic back pain. Appears pale.",
        "diagnosis": "Upper GI bleed",
        "workup": "Hemoglobin 7.2, down from baseline of 12. BUN/Cr ratio elevated. Transfused 2 units PRBCs. GI consulted for EGD which showed bleeding gastric ulcer, successfully treated with epinephrine injection and clips."
    },
    {
        "complaint": "severe headache",
        "hpi": "Patient presents with sudden onset severe headache described as 'worst headache of my life', onset 2 hours ago. Associated with neck stiffness and photophobia. Denies trauma, fever, or recent illness. Blood pressure elevated at 185/110.",
        "diagnosis": "Subarachnoid hemorrhage",
        "workup": "CT head showed subarachnoid hemorrhage in the basal cisterns. CTA head revealed 5mm anterior communicating artery aneurysm. Neurosurgery consulted urgently."
    },
    {
        "complaint": "difficulty breathing and cough",
        "hpi": "Patient presents with 5-day history of progressive dyspnea on exertion, now dyspneic at rest. Productive cough with yellow sputum. History of COPD, uses home oxygen 2L. Increased inhaler use without relief.",
        "diagnosis": "COPD exacerbation with pneumonia",
        "workup": "Chest X-ray showed right lower lobe infiltrate. ABG on room air: pH 7.32, pCO2 58, pO2 55. Started on IV steroids, antibiotics, nebulizers. Placed on BiPAP."
    },
    {
        "complaint": "flank pain and hematuria",
        "hpi": "Patient presents with sudden onset severe left flank pain radiating to groin, associated with gross hematuria. Pain is colicky, rated 10/10. History of prior kidney stones. Nausea and vomiting present.",
        "diagnosis": "Nephrolithiasis with ureteral obstruction",
        "workup": "CT KUB showed 8mm stone at left UVJ with moderate hydronephrosis. Creatinine mildly elevated at 1.4. Urology consulted for ureteral stent placement."
    }
]

# Pediatric-specific chief complaints (for Cardinal Glennon)
PEDIATRIC_COMPLAINTS = [
    {
        "complaint": "fever and difficulty breathing",
        "hpi": "Child brought in by parents with 3-day history of fever up to 103F and worsening cough. Started with runny nose, now with increased work of breathing. Decreased oral intake. No improvement with home nebulizer treatments.",
        "diagnosis": "RSV bronchiolitis with respiratory distress",
        "workup": "Respiratory panel positive for RSV. Chest X-ray showed bilateral peribronchial thickening. O2 sat 88% on room air, improved to 95% on 2L NC. Started on supportive care with supplemental oxygen and IV fluids."
    },
    {
        "complaint": "abdominal pain and vomiting",
        "hpi": "Child presents with 2-day history of periumbilical pain that has migrated to right lower quadrant. Multiple episodes of non-bilious vomiting. Low-grade fever. Decreased appetite. No diarrhea.",
        "diagnosis": "Acute appendicitis",
        "workup": "WBC elevated at 15.8. Ultrasound showed non-compressible appendix 9mm in diameter with surrounding fat stranding. Pediatric surgery consulted for appendectomy."
    },
    {
        "complaint": "wheezing and shortness of breath",
        "hpi": "Child with history of asthma presents with acute exacerbation. Symptoms started 2 days ago with upper respiratory infection. Using rescue inhaler every 2 hours without relief. Audible wheezing at rest.",
        "diagnosis": "Acute asthma exacerbation, moderate-severe",
        "workup": "Peak flow 50% of predicted. O2 sat 91% on room air. Started on continuous albuterol nebulizers, IV steroids, and supplemental oxygen. Chest X-ray negative for pneumonia."
    },
    {
        "complaint": "seizure activity",
        "hpi": "Child brought in by EMS after witnessed generalized tonic-clonic seizure at home lasting approximately 3 minutes. First-time seizure. Had fever of 102F earlier today. Post-ictal on arrival, now more alert.",
        "diagnosis": "Complex febrile seizure",
        "workup": "Temperature 102.8F on arrival. Basic labs within normal limits. Lumbar puncture performed, CSF negative for meningitis. Neurology consulted. Admitted for observation and fever management."
    },
    {
        "complaint": "dehydration and lethargy",
        "hpi": "Infant brought in with 3 days of diarrhea and vomiting. Decreased wet diapers over past 24 hours. Parents note child is more sleepy than usual and not interested in feeding. Sunken fontanelle noted.",
        "diagnosis": "Acute gastroenteritis with moderate dehydration",
        "workup": "BMP shows BUN 28, Cr 0.8. Stool studies pending. IV fluid bolus given with improvement in mental status. Admitted for IV rehydration and monitoring."
    }
]

# Pediatric medications
PEDIATRIC_MEDICATIONS = [
    ("Albuterol", "2.5mg", "Neb", "Q4H PRN"),
    ("Amoxicillin", "400mg", "PO", "BID"),
    ("Ibuprofen", "200mg", "PO", "Q6H PRN"),
    ("Montelukast", "5mg", "PO", "daily"),
    ("Cetirizine", "5mg", "PO", "daily"),
    ("Fluticasone", "50mcg", "Inhaled", "BID"),
    ("Ondansetron", "4mg", "PO/IV", "Q8H PRN"),
    ("Prednisolone", "15mg", "PO", "daily"),
]

# Pediatric lab templates
PEDIATRIC_LABS = {
    "respiratory": [
        ("WBC", "12.5", "K/uL", ""),
        ("Hemoglobin", "11.8", "g/dL", ""),
        ("Platelets", "320", "K/uL", ""),
        ("RSV", "Positive", "", ""),
        ("Influenza A/B", "Negative", "", ""),
    ],
    "infection": [
        ("WBC", "15.8", "K/uL", "H"),
        ("Hemoglobin", "12.2", "g/dL", ""),
        ("Platelets", "285", "K/uL", ""),
        ("CRP", "8.5", "mg/dL", "H"),
    ],
    "dehydration": [
        ("BUN", "28", "mg/dL", "H"),
        ("Creatinine", "0.8", "mg/dL", ""),
        ("Sodium", "148", "mEq/L", "H"),
        ("Potassium", "3.2", "mEq/L", "L"),
        ("Glucose", "85", "mg/dL", ""),
    ],
}

# Pediatric insurance plans (Medicaid, CHIP)
PEDIATRIC_INSURANCE_PLANS = [
    {
        "name": "MISSOURI HEALTHNET MANAGED CARE",
        "code": "MO01",
        "address": "PO BOX 6500",
        "city": "JEFFERSON CITY",
        "state": "MO",
        "zip": "65102-6500"
    },
    {
        "name": "UNITED HEALTHCARE COMMUNITY PLAN",
        "code": "UH01",
        "address": "PO BOX 31364",
        "city": "SALT LAKE CITY",
        "state": "UT",
        "zip": "84131-0364"
    },
    {
        "name": "ANTHEM BLUE CROSS MEDICAID",
        "code": "AN01",
        "address": "PO BOX 105187",
        "city": "ATLANTA",
        "state": "GA",
        "zip": "30348-5187"
    },
]

MEDICATIONS = [
    ("Metoprolol", "25mg", "PO", "BID"),
    ("Lisinopril", "10mg", "PO", "daily"),
    ("Atorvastatin", "40mg", "PO", "daily at bedtime"),
    ("Metformin", "500mg", "PO", "BID"),
    ("Aspirin", "81mg", "PO", "daily"),
    ("Omeprazole", "20mg", "PO", "daily"),
    ("Amlodipine", "5mg", "PO", "daily"),
    ("Furosemide", "40mg", "PO", "daily"),
    ("Gabapentin", "300mg", "PO", "TID"),
    ("Levothyroxine", "50mcg", "PO", "daily"),
    ("Eliquis", "5mg", "PO", "BID"),
    ("Jardiance", "10mg", "PO", "daily"),
    ("Entresto", "49/51mg", "PO", "BID"),
    ("Plavix", "75mg", "PO", "daily"),
]

PHYSICIANS = [
    "Dr. Sarah Thompson, MD", "Dr. Michael Chen, MD", "Dr. Jennifer Martinez, DO",
    "Dr. Robert Williams, MD", "Dr. Amanda Johnson, MD", "Dr. David Lee, MD",
    "Dr. Lisa Anderson, MD", "Dr. James Wilson, DO", "Dr. Emily Davis, MD"
]

HOSPITALS = [
    "SSM Health St. Mary's Hospital - Madison",
    "SSM Health St. Clare Hospital - Baraboo", 
    "SSM Health St. Agnes Hospital - Fond du Lac",
    "SSM Health St. Joseph Hospital - Lake St. Louis",
    "SSM Health St. Louis University Hospital",
    "SSM Health Cardinal Glennon Children's Hospital",
    "SSM Health DePaul Hospital - St. Louis"
]

INSURANCE_PLANS = [
    {
        "name": "UHC MANAGED MEDICARE ADV",
        "code": "4854",
        "address": "PO BOX 31362",
        "city": "SALT LAKE CITY",
        "state": "UT",
        "zip": "84131-0362"
    },
    {
        "name": "HUMANA MEDICARE ADVANTAGE",
        "code": "5521",
        "address": "PO BOX 14601",
        "city": "LEXINGTON",
        "state": "KY",
        "zip": "40512-4601"
    },
    {
        "name": "AETNA MEDICARE ADVANTAGE",
        "code": "3387",
        "address": "PO BOX 981106",
        "city": "EL PASO",
        "state": "TX",
        "zip": "79998-1106"
    }
]

LAB_RESULTS_TEMPLATES = {
    "infection": [
        ("WBC", "14.2", "K/uL", "H"),
        ("Hemoglobin", "11.8", "g/dL", "L"),
        ("Platelets", "245", "K/uL", ""),
        ("BUN", "28", "mg/dL", "H"),
        ("Creatinine", "1.4", "mg/dL", "H"),
        ("Glucose", "156", "mg/dL", "H"),
        ("Sodium", "138", "mEq/L", ""),
        ("Potassium", "4.2", "mEq/L", ""),
        ("Lactate", "2.8", "mmol/L", "H"),
    ],
    "cardiac": [
        ("Troponin I", "0.45", "ng/mL", "H"),
        ("BNP", "850", "pg/mL", "H"),
        ("Hemoglobin", "12.5", "g/dL", ""),
        ("Creatinine", "1.2", "mg/dL", ""),
        ("Glucose", "145", "mg/dL", "H"),
        ("Sodium", "136", "mEq/L", ""),
        ("Potassium", "4.5", "mEq/L", ""),
    ],
    "gi_bleed": [
        ("Hemoglobin", "7.2", "g/dL", "L"),
        ("Hematocrit", "22", "%", "L"),
        ("BUN", "45", "mg/dL", "H"),
        ("Creatinine", "1.1", "mg/dL", ""),
        ("INR", "1.2", "", ""),
        ("Platelets", "198", "K/uL", ""),
    ],
    "renal": [
        ("WBC", "10.2", "K/uL", ""),
        ("Hemoglobin", "12.8", "g/dL", ""),
        ("BUN", "32", "mg/dL", "H"),
        ("Creatinine", "1.4", "mg/dL", "H"),  # Matches workup text "Creatinine mildly elevated at 1.4"
        ("Glucose", "108", "mg/dL", ""),
        ("Sodium", "139", "mEq/L", ""),
        ("Potassium", "4.3", "mEq/L", ""),
        ("Urinalysis", "3+ blood, 1+ protein", "", ""),
    ],
    "general": [
        ("WBC", "8.5", "K/uL", ""),
        ("Hemoglobin", "13.2", "g/dL", ""),
        ("BUN", "18", "mg/dL", ""),
        ("Creatinine", "1.0", "mg/dL", ""),
        ("Glucose", "112", "mg/dL", ""),
        ("Sodium", "140", "mEq/L", ""),
        ("Potassium", "4.0", "mEq/L", ""),
    ],
}


def generate_mrn():
    """Generate a fake MRN."""
    return f"{random.randint(10000000, 99999999)}"


def generate_dob(min_age=45, max_age=85):
    """Generate a random date of birth."""
    age = random.randint(min_age, max_age)
    year = datetime.now().year - age
    month = random.randint(1, 12)
    day = random.randint(1, 28)
    return datetime(year, month, day)


def generate_admission_date():
    """Generate a recent admission date."""
    days_ago = random.randint(2, 14)
    return datetime.now() - timedelta(days=days_ago)


def generate_vitals(is_pediatric=False):
    """Generate realistic vital signs."""
    if is_pediatric:
        return {
            "temp": f"{random.uniform(98.0, 103.0):.1f}°F",
            "hr": f"{random.randint(90, 160)} bpm",
            "bp": f"{random.randint(85, 115)}/{random.randint(50, 75)} mmHg",
            "rr": f"{random.randint(20, 40)}/min",
            "o2": f"{random.randint(88, 100)}% on {random.choice(['RA', '2L NC', 'NRB', 'High-flow'])}"
        }
    return {
        "temp": f"{random.uniform(97.5, 101.5):.1f}°F",
        "hr": f"{random.randint(65, 110)} bpm",
        "bp": f"{random.randint(110, 180)}/{random.randint(60, 100)} mmHg",
        "rr": f"{random.randint(14, 24)}/min",
        "o2": f"{random.randint(88, 100)}% on {random.choice(['RA', '2L NC', '4L NC', 'NRB'])}"
    }


def generate_patient(hospital=None):
    """Generate a complete fake patient record."""
    # Select hospital first to determine if pediatric
    if hospital is None:
        hospital = random.choice(HOSPITALS)
    
    is_pediatric = "Cardinal Glennon" in hospital
    
    if is_pediatric:
        # Pediatric patient (ages 1-17)
        gender = random.choice(["M", "F"])
        first_name = random.choice(FIRST_NAMES_PEDS_M if gender == "M" else FIRST_NAMES_PEDS_F)
        last_name = random.choice(LAST_NAMES)
        dob = generate_dob(min_age=1, max_age=17)
        age = (datetime.now() - dob).days // 365
        
        complaint = random.choice(PEDIATRIC_COMPLAINTS)
        conditions = random.sample(PEDIATRIC_CONDITIONS, k=random.randint(1, 3))
        meds = random.sample(PEDIATRIC_MEDICATIONS, k=random.randint(2, 5))
        
        # Pick pediatric labs
        if "respiratory" in complaint["diagnosis"].lower() or "rsv" in complaint["diagnosis"].lower() or "asthma" in complaint["diagnosis"].lower():
            labs = PEDIATRIC_LABS["respiratory"]
        elif "dehydration" in complaint["diagnosis"].lower() or "gastro" in complaint["diagnosis"].lower():
            labs = PEDIATRIC_LABS["dehydration"]
        else:
            labs = PEDIATRIC_LABS["infection"]
    else:
        # Adult patient
        gender = random.choice(["M", "F"])
        first_name = random.choice(FIRST_NAMES_M if gender == "M" else FIRST_NAMES_F)
        last_name = random.choice(LAST_NAMES)
        dob = generate_dob()
        age = (datetime.now() - dob).days // 365
        
        complaint = random.choice(CHIEF_COMPLAINTS)
        conditions = random.sample(CHRONIC_CONDITIONS, k=random.randint(2, 6))
        meds = random.sample(MEDICATIONS, k=random.randint(4, 10))
        
        # Pick adult lab results based on complaint type
        if "infection" in complaint["complaint"] or "sepsis" in complaint["diagnosis"].lower():
            labs = LAB_RESULTS_TEMPLATES["infection"]
        elif "chest" in complaint["complaint"] or "cardiac" in complaint["diagnosis"].lower():
            labs = LAB_RESULTS_TEMPLATES["cardiac"]
        elif "bleed" in complaint["diagnosis"].lower() or "gi" in complaint["diagnosis"].lower():
            labs = LAB_RESULTS_TEMPLATES["gi_bleed"]
        elif "kidney" in complaint["diagnosis"].lower() or "stone" in complaint["diagnosis"].lower() or "nephro" in complaint["diagnosis"].lower() or "flank" in complaint["complaint"].lower():
            labs = LAB_RESULTS_TEMPLATES["renal"]
        else:
            labs = LAB_RESULTS_TEMPLATES["general"]
    
    admission_date = generate_admission_date()
    # 70% chance of observation before inpatient
    has_observation = random.random() < 0.7
    if has_observation:
        observation_date = admission_date
        inpatient_date = admission_date + timedelta(days=random.randint(1, 2))
    else:
        observation_date = None
        inpatient_date = admission_date
    
    # Use pediatric insurance for children, Medicare for adults
    insurance = random.choice(PEDIATRIC_INSURANCE_PLANS if is_pediatric else INSURANCE_PLANS)
    
    return {
        "name": f"{first_name} {last_name}",
        "dob": dob.strftime("%m/%d/%Y"),
        "age": age,
        "gender": gender,
        "mrn": generate_mrn(),
        "account_number": f"{random.randint(10000000000, 99999999999)}",
        "insurance": insurance,
        "insured_id": f"{random.randint(900000000, 999999999)}",
        "group_number": f"{random.randint(50000, 99999)}",
        "admission_date": admission_date.strftime("%m/%d/%Y"),
        "observation_date": observation_date.strftime("%m/%d/%Y") if observation_date else None,
        "inpatient_date": inpatient_date.strftime("%m/%d/%Y"),
        "chief_complaint": complaint["complaint"],
        "hpi": complaint["hpi"],
        "diagnosis": complaint["diagnosis"],
        "workup": complaint["workup"],
        "conditions": conditions,
        "medications": meds,
        "lab_results": labs,
        "vitals": generate_vitals(is_pediatric=is_pediatric),
        "attending": random.choice(PHYSICIANS),
        "hospital": hospital,
        "is_pediatric": is_pediatric
    }


def create_pdf(patient, output_path):
    """Create a clinical chart PDF for the patient."""
    doc = SimpleDocTemplate(str(output_path), pagesize=letter,
                           topMargin=0.5*inch, bottomMargin=0.5*inch,
                           leftMargin=0.75*inch, rightMargin=0.75*inch)
    
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle('Title', parent=styles['Heading1'], fontSize=14, spaceAfter=6)
    heading_style = ParagraphStyle('Heading', parent=styles['Heading2'], fontSize=11, spaceBefore=12, spaceAfter=6)
    normal_style = ParagraphStyle('Normal', parent=styles['Normal'], fontSize=10, spaceAfter=4)
    small_style = ParagraphStyle('Small', parent=styles['Normal'], fontSize=9)
    
    story = []
    
    # Header - mimics SSM Health chart format
    story.append(Paragraph(f"<b>{patient['hospital']}</b>", title_style))
    story.append(Paragraph("ADMISSION RECORD", small_style))
    story.append(Spacer(1, 8))
    
    # Account/Visit Info - key for extraction
    story.append(Paragraph(f"<b>ACCOUNT NO.</b> {patient['account_number']}", small_style))
    story.append(Spacer(1, 12))
    
    # Patient Demographics
    story.append(Paragraph("<b>PATIENT INFORMATION</b>", heading_style))
    demo_data = [
        ["Patient Name:", patient['name'], "MRN:", patient['mrn']],
        ["Date of Birth:", patient['dob'], "Age:", f"{patient['age']} years"],
        ["Gender:", "Male" if patient['gender'] == "M" else "Female", "Attending:", patient['attending']],
        ["Admission Date:", patient['admission_date'], "", ""]
    ]
    demo_table = Table(demo_data, colWidths=[1.5*inch, 2*inch, 1.2*inch, 2*inch])
    demo_table.setStyle(TableStyle([
        ('FONTSIZE', (0, 0), (-1, -1), 9),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
    ]))
    story.append(demo_table)
    story.append(Spacer(1, 8))
    
    # Insurance Information - key for extraction
    story.append(Paragraph("<b>INSURANCE 1</b>", heading_style))
    ins = patient['insurance']
    story.append(Paragraph(f"{ins['name']}", normal_style))
    story.append(Paragraph(f"{ins['address']}", normal_style))
    story.append(Paragraph(f"{ins['city']}, {ins['state']} {ins['zip']}", normal_style))
    story.append(Paragraph(f"GRP # {patient['group_number']}", normal_style))
    story.append(Paragraph(f"INSURED ID: {patient['insured_id']}", normal_style))
    story.append(Spacer(1, 8))
    
    # Service Dates
    story.append(Paragraph("<b>SERVICE DATES</b>", heading_style))
    if patient['observation_date']:
        story.append(Paragraph(f"Observation Start: {patient['observation_date']}", normal_style))
    story.append(Paragraph(f"Inpatient Admission: {patient['inpatient_date']}", normal_style))
    story.append(Spacer(1, 8))
    
    # ED Provider Notes header - key for place_of_service extraction
    story.append(Paragraph("<b>ED Provider Notes</b>", heading_style))
    story.append(Paragraph(f"Author: {patient['attending']}  Service: Emergency Medicine", small_style))
    story.append(Paragraph("EMERGENCY DEPARTMENT ENCOUNTER", small_style))
    story.append(Spacer(1, 8))
    
    # Chief Complaint
    story.append(Paragraph("<b>CHIEF COMPLAINT</b>", heading_style))
    story.append(Paragraph(patient['chief_complaint'].capitalize(), normal_style))
    story.append(Spacer(1, 8))
    
    # HPI
    story.append(Paragraph("<b>HISTORY OF PRESENT ILLNESS</b>", heading_style))
    story.append(Paragraph(patient['hpi'], normal_style))
    story.append(Spacer(1, 8))
    
    # Past Medical History
    story.append(Paragraph("<b>PAST MEDICAL HISTORY</b>", heading_style))
    for condition in patient['conditions']:
        story.append(Paragraph(f"• {condition}", normal_style))
    story.append(Spacer(1, 8))
    
    # Medications
    story.append(Paragraph("<b>HOME MEDICATIONS</b>", heading_style))
    for med in patient['medications']:
        story.append(Paragraph(f"• {med[0]} {med[1]} {med[2]} {med[3]}", normal_style))
    story.append(Spacer(1, 8))
    
    # Vital Signs
    story.append(Paragraph("<b>INITIAL VITAL SIGNS</b>", heading_style))
    vitals = patient['vitals']
    story.append(Paragraph(f"Temperature: {vitals['temp']} | Heart Rate: {vitals['hr']} | "
                          f"Blood Pressure: {vitals['bp']} | Respiratory Rate: {vitals['rr']} | "
                          f"O2 Saturation: {vitals['o2']}", normal_style))
    story.append(Spacer(1, 8))
    
    # Laboratory Results - key for extraction
    story.append(Paragraph("<b>LABORATORY RESULTS</b>", heading_style))
    lab_data = [["Test", "Result", "Units", "Flag"]]
    for lab in patient['lab_results']:
        lab_data.append(list(lab))
    lab_table = Table(lab_data, colWidths=[2*inch, 1.2*inch, 1*inch, 0.8*inch])
    lab_table.setStyle(TableStyle([
        ('FONTSIZE', (0, 0), (-1, -1), 9),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
        ('BACKGROUND', (0, 0), (-1, 0), colors.lightgrey),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
    ]))
    story.append(lab_table)
    story.append(Spacer(1, 8))
    
    # Assessment/Plan
    story.append(Paragraph("<b>ASSESSMENT AND PLAN</b>", heading_style))
    story.append(Paragraph(f"<b>Primary Diagnosis:</b> {patient['diagnosis']}", normal_style))
    story.append(Spacer(1, 4))
    story.append(Paragraph(patient['workup'], normal_style))
    story.append(Spacer(1, 12))
    
    # Medical Necessity Statement
    story.append(Paragraph("<b>MEDICAL NECESSITY FOR INPATIENT ADMISSION</b>", heading_style))
    necessity = f"""The patient required inpatient level of care due to the severity of their presentation 
    with {patient['chief_complaint']}. Given the patient's age ({patient['age']} years old), multiple comorbidities 
    including {', '.join(patient['conditions'][:3])}, and the need for {random.choice([
        'continuous cardiac monitoring',
        'IV antibiotics and close monitoring',
        'serial laboratory evaluation',
        'frequent neurological assessments',
        'intensive respiratory support'
    ])}, outpatient management was not appropriate. The patient's condition required a level of care 
    that could only be safely provided in an inpatient setting with 24-hour nursing care and 
    immediate physician availability."""
    story.append(Paragraph(necessity, normal_style))
    story.append(Spacer(1, 12))
    
    # Signature
    story.append(Paragraph(f"Electronically signed by: {patient['attending']}", small_style))
    story.append(Paragraph(f"Date/Time: {datetime.now().strftime('%m/%d/%Y %H:%M')}", small_style))
    
    doc.build(story)
    return output_path


def main():
    parser = argparse.ArgumentParser(description="Generate synthetic clinical chart PDFs")
    parser.add_argument("--count", type=int, default=3, help="Number of PDFs to generate")
    parser.add_argument("--output", type=str, default="./examples/synthetic", help="Output directory")
    args = parser.parse_args()
    
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)
    
    print(f"Generating {args.count} synthetic clinical charts...")
    
    for i in range(args.count):
        patient = generate_patient()
        safe_name = patient['name'].replace(' ', '_')
        filename = f"{safe_name}_{patient['mrn']}.pdf"
        output_path = output_dir / filename
        
        create_pdf(patient, output_path)
        print(f"  Created: {output_path}")
        print(f"    Patient: {patient['name']}, {patient['age']}yo {patient['gender']}")
        print(f"    Complaint: {patient['chief_complaint']}")
        print(f"    Diagnosis: {patient['diagnosis']}")
        print()
    
    print(f"Done! Generated {args.count} PDFs in {output_dir}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python
"""
Generate Appeal Letter from PDF Clinical Chart

Usage:
    python generate_appeal.py <pdf_path> [--hospital NAME] [--no-llm]

Examples:
    python generate_appeal.py "examples/S Taylor Clinical 89261252403.pdf"
    python generate_appeal.py chart.pdf --hospital "SSM Health St. Marys Hospital"
"""
import sys
import os

# Ensure project root is in path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from services.pdf_chart_parser import PDFChartParser
from services.midnight_reason_generator import MidnightReasonGenerator
from services.appeal_letter_generator import AppealLetterGenerator


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    
    pdf_path = sys.argv[1]
    use_llm = "--no-llm" not in sys.argv
    
    # Parse hospital name if provided
    hospital = "SSM Health St. Marys Hospital"
    for i, arg in enumerate(sys.argv):
        if arg == "--hospital" and i + 1 < len(sys.argv):
            hospital = sys.argv[i + 1]
    
    # Step 1: Parse and de-identify
    print(f"Parsing: {pdf_path}")
    parser = PDFChartParser(use_llm=use_llm)
    patient_data = parser.parse_and_deidentify(pdf_path)
    
    print("\n" + "="*60)
    print("DE-IDENTIFIED PATIENT DATA")
    print("="*60)
    print(f"Name: {patient_data.patient_name}")
    print(f"DOB: {patient_data.dob}")
    print(f"Age/Gender: {patient_data.age} {patient_data.gender}")
    obs_date = getattr(patient_data, 'observation_date', '')
    inp_date = getattr(patient_data, 'inpatient_date', '')
    if obs_date:
        print(f"Observation Date: {obs_date}")
    if inp_date:
        print(f"Inpatient Date: {inp_date}")
    print(f"\nPMH: {', '.join(patient_data.conditions[:10])}")
    print(f"\nChief Complaint: {patient_data.chief_complaint[:200]}..." if len(patient_data.chief_complaint) > 200 else f"\nChief Complaint: {patient_data.chief_complaint}")
    
    # Step 2: Generate MidnightReason
    print("\n" + "="*60)
    print("GENERATING MIDNIGHT REASONS")
    print("="*60)
    reason_gen = MidnightReasonGenerator()
    reason_output = reason_gen.generate_from_data(patient_data)
    print("MidnightReason 1:", reason_output.midnight_reason_1[:150] + "...")
    print("MidnightReason 2:", reason_output.midnight_reason_2[:150] + "...")
    
    # Step 3: Generate appeal letter
    print("\n" + "="*60)
    print("GENERATING APPEAL LETTER")
    print("="*60)
    letter_gen = AppealLetterGenerator()
    output_path = letter_gen.generate_from_data(
        patient_data=patient_data,
        reason_output=reason_output,
        place_of_service=hospital
    )
    
    print(f"\nAppeal letter saved: {output_path}")
    return output_path


if __name__ == "__main__":
    main()

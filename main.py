"""
AEC Patient Stay API

Fetches clinical notes from Epic's FHIR API and generates 
patient stay summaries using LLMs.
"""
import asyncio
import argparse
import structlog
from datetime import datetime
from typing import Optional

from api import EpicFHIRClient
from llm import DocumentGenerator
from models import PatientStaySummary

# Configure structured logging
structlog.configure(
    processors=[
        structlog.stdlib.filter_by_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.JSONRenderer()
    ],
    wrapper_class=structlog.stdlib.BoundLogger,
    context_class=dict,
    logger_factory=structlog.stdlib.LoggerFactory(),
    cache_logger_on_first_use=True,
)

logger = structlog.get_logger(__name__)


async def generate_patient_summary(
    patient_id: str,
    encounter_id: Optional[str] = None,
    output_file: Optional[str] = None
) -> PatientStaySummary:
    """
    Generate a patient stay summary from Epic clinical notes.
    
    Args:
        patient_id: FHIR Patient ID
        encounter_id: Optional FHIR Encounter ID to filter notes
        output_file: Optional file path to save the summary
    
    Returns:
        PatientStaySummary object with generated content
    """
    epic_client = EpicFHIRClient()
    generator = DocumentGenerator()
    
    logger.info("Starting patient summary generation", patient_id=patient_id)
    
    # Fetch patient information
    patient = await epic_client.get_patient(patient_id)
    logger.info("Retrieved patient", name=patient.full_name)
    
    # Fetch encounter if specified
    encounter = None
    if encounter_id:
        encounter = await epic_client.get_encounter(encounter_id)
        logger.info("Retrieved encounter", encounter_id=encounter_id)
    else:
        # Create a placeholder encounter
        from models import Encounter
        encounter = Encounter(
            id="current",
            status="in-progress",
        )
    
    # Fetch clinical notes
    notes = await epic_client.search_clinical_notes(
        patient_id=patient_id,
        encounter_id=encounter_id,
    )
    logger.info("Retrieved clinical notes", count=len(notes))
    
    if not notes:
        logger.warning("No clinical notes found for patient")
    
    # Generate summary
    summary = generator.generate_stay_summary(
        patient=patient,
        encounter=encounter,
        notes=notes,
    )
    
    logger.info("Generated patient stay summary")
    
    # Save to file if requested
    if output_file:
        with open(output_file, "w") as f:
            f.write(f"# Patient Stay Summary\n\n")
            f.write(f"**Patient:** {patient.full_name}\n")
            f.write(f"**Generated:** {summary.generated_at}\n")
            f.write(f"**Model:** {summary.model_used}\n\n")
            f.write("---\n\n")
            f.write(summary.summary or "No summary generated")
            
            if summary.diagnoses:
                f.write("\n\n## Diagnoses\n")
                for dx in summary.diagnoses:
                    f.write(f"- {dx}\n")
            
            if summary.treatments:
                f.write("\n\n## Treatments\n")
                for tx in summary.treatments:
                    f.write(f"- {tx}\n")
            
            if summary.follow_up_recommendations:
                f.write("\n\n## Follow-up\n")
                for fu in summary.follow_up_recommendations:
                    f.write(f"- {fu}\n")
        
        logger.info("Saved summary to file", path=output_file)
    
    return summary


async def generate_discharge_instructions(
    patient_id: str,
    encounter_id: str,
    reading_level: str = "patient-friendly",
    output_file: Optional[str] = None
) -> str:
    """
    Generate discharge instructions for a patient.
    
    Args:
        patient_id: FHIR Patient ID
        encounter_id: FHIR Encounter ID
        reading_level: "patient-friendly", "detailed", or "simple"
        output_file: Optional file path to save instructions
    
    Returns:
        Discharge instructions text
    """
    # First generate the summary
    summary = await generate_patient_summary(patient_id, encounter_id)
    
    # Generate discharge instructions
    generator = DocumentGenerator()
    instructions = generator.generate_discharge_instructions(
        patient=summary.patient,
        summary=summary,
        reading_level=reading_level,
    )
    
    if output_file:
        with open(output_file, "w") as f:
            f.write(instructions)
        logger.info("Saved discharge instructions", path=output_file)
    
    return instructions


def main():
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Generate patient stay summaries from Epic clinical notes"
    )
    
    subparsers = parser.add_subparsers(dest="command", help="Commands")
    
    # Summary command
    summary_parser = subparsers.add_parser("summary", help="Generate patient stay summary")
    summary_parser.add_argument("patient_id", help="FHIR Patient ID")
    summary_parser.add_argument("--encounter", "-e", help="FHIR Encounter ID")
    summary_parser.add_argument("--output", "-o", help="Output file path")
    
    # Discharge command
    discharge_parser = subparsers.add_parser("discharge", help="Generate discharge instructions")
    discharge_parser.add_argument("patient_id", help="FHIR Patient ID")
    discharge_parser.add_argument("encounter_id", help="FHIR Encounter ID")
    discharge_parser.add_argument(
        "--level", "-l",
        choices=["patient-friendly", "detailed", "simple"],
        default="patient-friendly",
        help="Reading level for instructions"
    )
    discharge_parser.add_argument("--output", "-o", help="Output file path")
    
    args = parser.parse_args()
    
    if args.command == "summary":
        result = asyncio.run(generate_patient_summary(
            patient_id=args.patient_id,
            encounter_id=args.encounter,
            output_file=args.output,
        ))
        print("\n" + "="*60)
        print("PATIENT STAY SUMMARY")
        print("="*60)
        print(result.summary)
        
    elif args.command == "discharge":
        result = asyncio.run(generate_discharge_instructions(
            patient_id=args.patient_id,
            encounter_id=args.encounter_id,
            reading_level=args.level,
            output_file=args.output,
        ))
        print("\n" + "="*60)
        print("DISCHARGE INSTRUCTIONS")
        print("="*60)
        print(result)
        
    else:
        parser.print_help()


if __name__ == "__main__":
    main()

# AEC Patient Stay API

Generates patient stay summaries and discharge instructions from Epic clinical notes using LLMs.

## Features

- **Epic FHIR R4 Integration**: Fetches clinical notes via DocumentReference.Search (spec 5454)
- **LLM-Powered Summaries**: Uses AWS Bedrock (Claude), Azure OpenAI, or OpenAI to generate comprehensive clinical summaries
- **Discharge Instructions**: Generates patient-friendly discharge instructions at configurable reading levels
- **Structured Output**: Extracts diagnoses, treatments, and follow-up recommendations

## Project Structure

```
aec-patient-stay-api/
├── api/
│   ├── __init__.py
│   └── epic_client.py      # Epic FHIR API client
├── llm/
│   ├── __init__.py
│   └── document_generator.py
├── templates/
│   ├── clinical_summary.txt
│   └── discharge_instructions.txt
├── config.py               # Pydantic settings
├── models.py               # Data models
├── main.py                 # CLI entry point
├── requirements.txt
└── .env.example
```

## Setup

### 1. Install Dependencies

```bash
python -m venv .venv
.venv\Scripts\activate  # Windows
pip install -r requirements.txt
```

### 2. Configure Environment

Copy `.env.example` to `.env` and fill in your credentials:

```bash
cp .env.example .env
```

**Required settings:**

- `EPIC_CLIENT_ID`: Your Epic app client ID
- `EPIC_PRIVATE_KEY_PATH`: Path to your RSA private key for JWT auth
- `LLM_PROVIDER`: Set to `bedrock`, `azure`, or `openai`

**For AWS Bedrock (recommended):**
- `AWS_REGION`: AWS region (default: us-east-1)
- `BEDROCK_MODEL_ID`: Model ID (default: anthropic.claude-3-sonnet-20240229-v1:0)
- AWS credentials via environment vars, `~/.aws/credentials`, or IAM role

**For Azure OpenAI:**
- `AZURE_OPENAI_ENDPOINT`: Your Azure OpenAI endpoint
- `AZURE_OPENAI_API_KEY`: Your Azure OpenAI API key
- `AZURE_OPENAI_DEPLOYMENT`: Your GPT-4 deployment name

### 3. Epic API Setup

1. Register your app at [open.epic.com](https://open.epic.com)
2. Generate an RSA key pair for backend service authentication
3. Upload your public key to Epic
4. Save your private key to `./keys/epic_private_key.pem`

## Usage

### Generate Patient Stay Summary

```bash
python main.py summary <patient_id> [--encounter <encounter_id>] [--output summary.md]
```

### Generate Discharge Instructions

```bash
python main.py discharge <patient_id> <encounter_id> [--level patient-friendly|detailed|simple] [--output discharge.md]
```

### Programmatic Usage

```python
import asyncio
from api import EpicFHIRClient
from llm import DocumentGenerator

async def main():
    client = EpicFHIRClient()
    generator = DocumentGenerator()
    
    # Fetch data from Epic
    patient = await client.get_patient("patient-fhir-id")
    encounter = await client.get_encounter("encounter-fhir-id")
    notes = await client.search_clinical_notes(
        patient_id="patient-fhir-id",
        encounter_id="encounter-fhir-id"
    )
    
    # Generate summary
    summary = generator.generate_stay_summary(patient, encounter, notes)
    print(summary.summary)

asyncio.run(main())
```

## Epic FHIR Resources Used

| Resource | Purpose |
|----------|---------|
| `Patient` | Patient demographics |
| `Encounter` | Visit/admission details |
| `DocumentReference` | Clinical notes (spec 5454) |

## LOINC Codes for Note Types

| Code | Type |
|------|------|
| 11506-3 | Progress Note |
| 18842-5 | Discharge Summary |
| 34117-2 | History and Physical |
| 11488-4 | Consultation Note |
| 11504-8 | Operative Note |

## License

Internal use only. Contains PHI handling - ensure HIPAA compliance.

"""
Create test patients and clinical notes in Epic FHIR sandbox.

Usage:
    python scripts/create_test_patient.py --name "John Smith" --dob "1965-03-15" --note examples/note1.docx
    python scripts/create_test_patient.py --list  # List existing test patients
"""

import argparse
import asyncio
import base64
import json
import uuid
from datetime import datetime
from pathlib import Path

import httpx
import jwt
from docx import Document


# Epic sandbox configuration
TOKEN_URL = "https://fhir.epic.com/interconnect-fhir-oauth/oauth2/token"
FHIR_BASE = "https://fhir.epic.com/interconnect-fhir-oauth/api/FHIR/R4"
CLIENT_ID = "e196658f-e79a-474c-97ab-267beab191a5"
PRIVATE_KEY_PATH = Path("./keys/epic_private_key.pem")


def get_access_token() -> str:
    """Get OAuth access token using JWT authentication."""
    private_key = PRIVATE_KEY_PATH.read_text()
    now = int(datetime.now().timestamp())
    
    jwt_payload = {
        "iss": CLIENT_ID,
        "sub": CLIENT_ID,
        "aud": TOKEN_URL,
        "jti": str(uuid.uuid4()),
        "exp": now + 300,
        "iat": now,
    }
    
    assertion = jwt.encode(
        jwt_payload, 
        private_key, 
        algorithm="RS384", 
        headers={"kid": "patient-stay-api-key-1"}
    )
    
    with httpx.Client(verify=False) as client:
        response = client.post(
            TOKEN_URL,
            data={
                "grant_type": "client_credentials",
                "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
                "client_assertion": assertion,
            }
        )
        response.raise_for_status()
        return response.json()["access_token"]


def read_docx(path: Path) -> str:
    """Extract text from a .docx file."""
    doc = Document(path)
    return "\n".join(para.text for para in doc.paragraphs)


def create_patient_resource(given_name: str, family_name: str, dob: str, gender: str = "unknown") -> dict:
    """Create a FHIR Patient resource."""
    return {
        "resourceType": "Patient",
        "name": [{
            "use": "official",
            "family": family_name,
            "given": [given_name]
        }],
        "gender": gender,
        "birthDate": dob,
        "identifier": [{
            "system": "urn:oid:1.2.3.4.5.6.7.8.9",
            "value": f"TEST-{uuid.uuid4().hex[:8].upper()}"
        }]
    }


def create_document_reference(patient_id: str, note_text: str, title: str = "Clinical Note") -> dict:
    """Create a FHIR DocumentReference for a clinical note."""
    # Encode the note as base64
    note_bytes = note_text.encode("utf-8")
    note_b64 = base64.b64encode(note_bytes).decode("utf-8")
    
    return {
        "resourceType": "DocumentReference",
        "status": "current",
        "type": {
            "coding": [{
                "system": "http://loinc.org",
                "code": "11506-3",
                "display": "Progress note"
            }]
        },
        "subject": {
            "reference": f"Patient/{patient_id}"
        },
        "date": datetime.now().isoformat(),
        "description": title,
        "content": [{
            "attachment": {
                "contentType": "text/plain",
                "data": note_b64,
                "title": title
            }
        }]
    }


def create_patient(access_token: str, patient: dict) -> dict:
    """POST a Patient resource to Epic."""
    with httpx.Client(verify=False) as client:
        response = client.post(
            f"{FHIR_BASE}/Patient",
            json=patient,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/fhir+json",
                "Accept": "application/fhir+json"
            }
        )
        print(f"Create Patient status: {response.status_code}")
        if response.status_code in (200, 201):
            return response.json()
        else:
            print(f"Error: {response.text[:500]}")
            return None


def create_document(access_token: str, doc_ref: dict) -> dict:
    """POST a DocumentReference to Epic."""
    with httpx.Client(verify=False) as client:
        response = client.post(
            f"{FHIR_BASE}/DocumentReference",
            json=doc_ref,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/fhir+json",
                "Accept": "application/fhir+json"
            }
        )
        print(f"Create DocumentReference status: {response.status_code}")
        if response.status_code in (200, 201):
            return response.json()
        else:
            print(f"Error: {response.text[:500]}")
            return None


def search_patients(access_token: str, name: str = None) -> list:
    """Search for patients."""
    params = {}
    if name:
        params["name"] = name
    
    with httpx.Client(verify=False) as client:
        response = client.get(
            f"{FHIR_BASE}/Patient",
            params=params,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/fhir+json"
            }
        )
        if response.status_code == 200:
            bundle = response.json()
            return bundle.get("entry", [])
        return []


def main():
    parser = argparse.ArgumentParser(description="Create test patients and notes in Epic sandbox")
    parser.add_argument("--name", help="Patient name (e.g., 'John Smith')")
    parser.add_argument("--dob", help="Date of birth (YYYY-MM-DD)")
    parser.add_argument("--gender", choices=["male", "female", "other", "unknown"], default="unknown")
    parser.add_argument("--note", type=Path, help="Path to .docx file with clinical note")
    parser.add_argument("--note-title", default="Progress Note", help="Title for the clinical note")
    parser.add_argument("--list", action="store_true", help="List test patients")
    parser.add_argument("--search", help="Search patients by name")
    
    args = parser.parse_args()
    
    print("Getting access token...")
    token = get_access_token()
    print("✓ Token acquired")
    
    if args.list or args.search:
        print("\nSearching patients...")
        patients = search_patients(token, args.search)
        print(f"Found {len(patients)} patients")
        for entry in patients[:10]:
            p = entry.get("resource", {})
            name = p.get("name", [{}])[0]
            given = " ".join(name.get("given", []))
            family = name.get("family", "")
            print(f"  - {given} {family} (ID: {p.get('id', 'N/A')[:30]}...)")
        return
    
    if args.name and args.dob:
        # Parse name
        parts = args.name.split()
        given = parts[0]
        family = parts[-1] if len(parts) > 1 else parts[0]
        
        print(f"\nCreating patient: {given} {family}, DOB: {args.dob}")
        patient = create_patient_resource(given, family, args.dob, args.gender)
        result = create_patient(token, patient)
        
        if result:
            patient_id = result.get("id")
            print(f"✓ Patient created with ID: {patient_id}")
            
            # If a note file was provided, attach it
            if args.note and args.note.exists():
                print(f"\nReading note from: {args.note}")
                note_text = read_docx(args.note)
                print(f"  Note length: {len(note_text)} characters")
                
                print(f"\nCreating DocumentReference...")
                doc_ref = create_document_reference(patient_id, note_text, args.note_title)
                doc_result = create_document(token, doc_ref)
                
                if doc_result:
                    print(f"✓ Document created with ID: {doc_result.get('id')}")
    else:
        parser.print_help()


if __name__ == "__main__":
    main()

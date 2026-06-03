"""
Fetch patient stay data that would be used to generate MidnightReason justifications.
This demonstrates the data available from Epic FHIR APIs.
"""
import httpx
import jwt
import time
import uuid
from pathlib import Path
from datetime import datetime

private_key = Path('./keys/epic_private_key.pem').read_text()
client_id = 'e196658f-e79a-474c-97ab-267beab191a5'
token_url = 'https://fhir.epic.com/interconnect-fhir-oauth/oauth2/token'
fhir_base = 'https://fhir.epic.com/interconnect-fhir-oauth/api/FHIR/R4'

def get_token():
    now = int(time.time())
    assertion = jwt.encode({
        'iss': client_id, 'sub': client_id, 'aud': token_url,
        'jti': str(uuid.uuid4()), 'exp': now + 300, 'iat': now,
    }, private_key, algorithm='RS384', headers={'kid': 'patient-stay-api-key-1'})
    
    with httpx.Client(verify=False) as client:
        return client.post(token_url, data={
            'grant_type': 'client_credentials',
            'client_assertion_type': 'urn:ietf:params:oauth:client-assertion-type:jwt-bearer',
            'client_assertion': assertion,
        }).json()['access_token']

def fetch_resource(client, token, resource, params):
    resp = client.get(
        f"{fhir_base}/{resource}",
        params=params,
        headers={'Authorization': f'Bearer {token}', 'Accept': 'application/fhir+json'}
    )
    if resp.status_code == 200:
        return resp.json().get('entry', [])
    return None

# Test patient: Elijah Davis (has Condition, MedicationRequest data)
patient_id = 'egqBHVfQlt4Bw3XGXoxVxHg3'
patient_name = 'Elijah Davis'

print("=" * 70)
print(f"PATIENT STAY DATA FOR: {patient_name}")
print("=" * 70)

token = get_token()

with httpx.Client(verify=False) as client:
    # 1. Patient Demographics
    print("\n1. PATIENT DEMOGRAPHICS")
    print("-" * 40)
    resp = client.get(
        f"{fhir_base}/Patient/{patient_id}",
        headers={'Authorization': f'Bearer {token}', 'Accept': 'application/fhir+json'}
    )
    if resp.status_code == 200:
        pat = resp.json()
        name = pat.get('name', [{}])[0]
        print(f"   Name: {' '.join(name.get('given', []))} {name.get('family', '')}")
        print(f"   DOB: {pat.get('birthDate', 'N/A')}")
        print(f"   Gender: {pat.get('gender', 'N/A')}")
    
    # 2. Encounters (Hospital Stays)
    print("\n2. ENCOUNTERS (Hospital Stays)")
    print("-" * 40)
    encounters = fetch_resource(client, token, 'Encounter', {'patient': patient_id})
    if encounters:
        for enc in encounters[:5]:
            e = enc['resource']
            status = e.get('status', 'N/A')
            enc_class = e.get('class', {}).get('display', e.get('class', {}).get('code', 'N/A'))
            period = e.get('period', {})
            start = period.get('start', 'N/A')
            end = period.get('end', 'ongoing')
            print(f"   - Status: {status}, Class: {enc_class}")
            print(f"     Period: {start} to {end}")
    else:
        print("   No encounters found or API not enabled")
    
    # 3. Conditions (Diagnoses)
    print("\n3. CONDITIONS (Diagnoses for MidnightReason)")
    print("-" * 40)
    conditions = fetch_resource(client, token, 'Condition', {'patient': patient_id})
    if conditions:
        for cond in conditions[:10]:
            c = cond['resource']
            code = c.get('code', {})
            display = code.get('text') or code.get('coding', [{}])[0].get('display', 'Unknown')
            clinical_status = c.get('clinicalStatus', {}).get('coding', [{}])[0].get('code', 'N/A')
            print(f"   - {display} (status: {clinical_status})")
    else:
        print("   No conditions found or API not enabled")
    
    # 4. Observations (Labs & Vitals)
    print("\n4. OBSERVATIONS (Labs & Vitals)")
    print("-" * 40)
    observations = fetch_resource(client, token, 'Observation', {'patient': patient_id, '_count': '20'})
    if observations:
        for obs in observations[:10]:
            o = obs['resource']
            code = o.get('code', {})
            display = code.get('text') or code.get('coding', [{}])[0].get('display', 'Unknown')
            value = o.get('valueQuantity', {})
            val_str = f"{value.get('value', '')} {value.get('unit', '')}" if value else o.get('valueString', 'N/A')
            print(f"   - {display}: {val_str}")
    else:
        print("   No observations found or API not enabled")
    
    # 5. MedicationRequest (Ordered Medications)
    print("\n5. MEDICATIONS (IV/PO for MidnightReason)")
    print("-" * 40)
    meds = fetch_resource(client, token, 'MedicationRequest', {'patient': patient_id})
    if meds:
        for med in meds[:10]:
            m = med['resource']
            med_code = m.get('medicationCodeableConcept', {})
            display = med_code.get('text') or med_code.get('coding', [{}])[0].get('display', 'Unknown')
            status = m.get('status', 'N/A')
            print(f"   - {display} (status: {status})")
    elif meds is None:
        print("   MedicationRequest API not enabled (403)")
    else:
        print("   No medications found")

    # 6. DocumentReference (Clinical Notes)
    print("\n6. CLINICAL NOTES")
    print("-" * 40)
    docs = fetch_resource(client, token, 'DocumentReference', {'patient': patient_id})
    if docs:
        for doc in docs[:5]:
            d = doc['resource']
            doc_type = d.get('type', {}).get('text', 'Unknown')
            print(f"   - {doc_type}")
    elif docs is None:
        print("   DocumentReference API not enabled (403)")
    else:
        print("   No documents found")

print("\n" + "=" * 70)
print("DATA AVAILABLE FOR MIDNIGHT REASON GENERATION:")
print("=" * 70)
print("""
To generate MidnightReason1 and MidnightReason2, we need:
  - Conditions: diagnoses justifying admission
  - Medications: IV vs PO medications administered  
  - Observations: abnormal lab values, vitals
  - Encounters: admission dates/status
  - DocumentReference: clinical notes (NEEDS API ADDED)
  - MedicationRequest: medication orders (NEEDS API ADDED if not enabled)
""")

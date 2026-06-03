"""Check multiple test patients for available data."""
import httpx
import jwt
import time
import uuid
from pathlib import Path

private_key = Path('./keys/epic_private_key.pem').read_text()
client_id = 'e196658f-e79a-474c-97ab-267beab191a5'
token_url = 'https://fhir.epic.com/interconnect-fhir-oauth/oauth2/token'
fhir_base = 'https://fhir.epic.com/interconnect-fhir-oauth/api/FHIR/R4'

now = int(time.time())
assertion = jwt.encode({
    'iss': client_id, 'sub': client_id, 'aud': token_url,
    'jti': str(uuid.uuid4()), 'exp': now + 300, 'iat': now,
}, private_key, algorithm='RS384', headers={'kid': 'patient-stay-api-key-1'})

with httpx.Client(verify=False) as client:
    token = client.post(token_url, data={
        'grant_type': 'client_credentials',
        'client_assertion_type': 'urn:ietf:params:oauth:client-assertion-type:jwt-bearer',
        'client_assertion': assertion,
    }).json()['access_token']
    
    # Test patients with various data types
    patients = [
        ('Theodore Baxter', 'eIXesllypH1M1wBVAYSaWaw3'),
        ('Derrick Lin', 'eq081-VQEgP8drUUqCWzHUg3'),
        ('Camila Lopez', 'erXuFYUfucBZaryVksYEcMg3'),
    ]
    
    for name, pid in patients:
        print()
        print("=" * 50)
        print(name)
        print("=" * 50)
        
        # Observations
        resp = client.get(f'{fhir_base}/Observation', params={'patient': pid, '_count': '10'},
            headers={'Authorization': f'Bearer {token}', 'Accept': 'application/fhir+json'})
        if resp.status_code == 200:
            entries = resp.json().get('entry', [])
            print(f"Observations: {len(entries)}")
            for e in entries[:5]:
                o = e['resource']
                code = o.get('code', {})
                disp = code.get('text') or code.get('coding', [{}])[0].get('display', 'N/A')
                val = o.get('valueQuantity', {})
                val_str = f"{val.get('value', '')} {val.get('unit', '')}" if val else o.get('valueString', 'N/A')
                print(f"  - {disp}: {val_str}")
        else:
            print(f"Observations: {resp.status_code}")
        
        # Conditions
        resp = client.get(f'{fhir_base}/Condition', params={'patient': pid},
            headers={'Authorization': f'Bearer {token}', 'Accept': 'application/fhir+json'})
        if resp.status_code == 200:
            entries = resp.json().get('entry', [])
            print(f"Conditions: {len(entries)}")
            for e in entries[:5]:
                c = e['resource']
                code = c.get('code', {})
                disp = code.get('text') or code.get('coding', [{}])[0].get('display', 'N/A')
                print(f"  - {disp}")
        else:
            print(f"Conditions: {resp.status_code}")

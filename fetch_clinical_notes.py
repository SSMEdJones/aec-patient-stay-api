"""Fetch clinical notes from Epic test patients."""
import httpx
import jwt
import time
import uuid
import base64
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
    
    # Test patients with DocumentReference
    test_patients = [
        ('Elijah Davis', 'egqBHVfQlt4Bw3XGXoxVxHg3'),
        ('Warren McGinnis', 'e0w0LEDCYtfckT6N.CkJKCw3'),
        ('Olivia Roberts', 'eh2xYHuzl9nkSFVvV3osUHg3'),
    ]
    
    for name, pid in test_patients:
        print()
        print("=" * 60)
        print(f"{name} ({pid[:20]}...)")
        print("=" * 60)
        
        # Get DocumentReference
        resp = client.get(
            fhir_base + '/DocumentReference',
            params={'patient': pid},
            headers={'Authorization': f'Bearer {token}', 'Accept': 'application/fhir+json'}
        )
        
        print(f"DocumentReference Status: {resp.status_code}")
        if resp.status_code == 200:
            data = resp.json()
            entries = data.get('entry', [])
            print(f"Found {len(entries)} documents")
            
            for i, entry in enumerate(entries[:3]):
                doc = entry.get('resource', {})
                doc_type = doc.get('type', {}).get('text', 'Unknown type')
                desc = doc.get('description', 'No description')
                print(f"\n  Document {i+1}: {doc_type}")
                print(f"  Description: {desc[:100]}")
                
                # Try to get content
                content = doc.get('content', [])
                if content:
                    attach = content[0].get('attachment', {})
                    content_type = attach.get('contentType', 'unknown')
                    url = attach.get('url', '')
                    data_b64 = attach.get('data', '')
                    print(f"  Content-Type: {content_type}")
                    if data_b64:
                        try:
                            text = base64.b64decode(data_b64).decode('utf-8', errors='ignore')
                            print(f"  Content preview: {text[:500]}...")
                        except:
                            print(f"  Content: [binary data]")
                    elif url:
                        print(f"  Content URL: {url[:80]}...")
        else:
            print(f"Error: {resp.text[:200]}")

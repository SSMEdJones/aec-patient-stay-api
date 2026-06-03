"""Epic FHIR API client for clinical notes (DocumentReference)."""
import httpx
import jwt
import time
import uuid
import urllib3
import structlog
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any
from pathlib import Path

from models import ClinicalNote, PatientInfo, Encounter, NoteType, DocumentStatus, Author
from config import EPIC_BASE_URL, EPIC_CLIENT_ID, EPIC_TOKEN_URL, EPIC_PRIVATE_KEY_PATH

# Disable SSL warnings for corporate networks
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logger = structlog.get_logger(__name__)


class EpicAuthError(Exception):
    """Authentication error with Epic API."""
    pass


class EpicAPIError(Exception):
    """General Epic API error."""
    pass


class EpicFHIRClient:
    """
    Client for Epic's FHIR R4 API.
    
    Supports:
    - Backend service authentication (JWT-based)
    - DocumentReference.Search for clinical notes
    - Patient and Encounter resources
    """
    
    def __init__(self):
        self.base_url = EPIC_BASE_URL.rstrip("/")
        self.client_id = EPIC_CLIENT_ID
        self.token_url = EPIC_TOKEN_URL
        
        self._access_token: Optional[str] = None
        self._token_expires_at: Optional[datetime] = None
        
        # Load private key for JWT auth if provided
        self._private_key: Optional[str] = None
        if EPIC_PRIVATE_KEY_PATH:
            key_path = Path(EPIC_PRIVATE_KEY_PATH)
            if key_path.exists():
                self._private_key = key_path.read_text()
    
    async def _get_access_token(self) -> str:
        """
        Get OAuth2 access token using backend service authentication.
        
        Uses JWT assertion for client credentials flow as per Epic's
        Backend Services specification.
        """
        # Return cached token if still valid
        if self._access_token and self._token_expires_at:
            if datetime.now() < self._token_expires_at - timedelta(minutes=1):
                return self._access_token
        
        if not self._private_key:
            raise EpicAuthError(
                "Private key required for backend service authentication. "
                "Set EPIC_PRIVATE_KEY_PATH in .env"
            )
        
        # Create JWT assertion
        now = int(time.time())
        jwt_payload = {
            "iss": self.client_id,
            "sub": self.client_id,
            "aud": self.token_url,
            "jti": str(uuid.uuid4()),
            "exp": now + 300,  # 5 minute expiry
            "iat": now,
        }
        
        assertion = jwt.encode(
            jwt_payload,
            self._private_key,
            algorithm="RS384"
        )
        
        # Request access token
        async with httpx.AsyncClient() as client:
            response = await client.post(
                self.token_url,
                data={
                    "grant_type": "client_credentials",
                    "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
                    "client_assertion": assertion,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"}
            )
        
        if response.status_code != 200:
            logger.error("Token request failed", status=response.status_code, body=response.text)
            raise EpicAuthError(f"Failed to get access token: {response.status_code}")
        
        token_data = response.json()
        self._access_token = token_data["access_token"]
        expires_in = token_data.get("expires_in", 3600)
        self._token_expires_at = datetime.now() + timedelta(seconds=expires_in)
        
        logger.info("Obtained new access token", expires_in=expires_in)
        return self._access_token
    
    async def _request(
        self,
        method: str,
        endpoint: str,
        params: Optional[Dict[str, Any]] = None,
        json_data: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Make an authenticated request to the Epic FHIR API."""
        token = await self._get_access_token()
        
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/fhir+json",
        }
        
        async with httpx.AsyncClient() as client:
            response = await client.request(
                method=method,
                url=url,
                params=params,
                json=json_data,
                headers=headers,
                timeout=30.0
            )
        
        if response.status_code == 401:
            # Token might be expired, clear and retry once
            self._access_token = None
            self._token_expires_at = None
            token = await self._get_access_token()
            
            async with httpx.AsyncClient() as client:
                response = await client.request(
                    method=method,
                    url=url,
                    params=params,
                    json=json_data,
                    headers={**headers, "Authorization": f"Bearer {token}"},
                    timeout=30.0
                )
        
        if response.status_code >= 400:
            logger.error(
                "FHIR API error",
                status=response.status_code,
                endpoint=endpoint,
                body=response.text[:500]
            )
            raise EpicAPIError(f"FHIR API error: {response.status_code} - {response.text[:200]}")
        
        return response.json()
    
    async def get_patient(self, patient_id: str) -> PatientInfo:
        """Fetch patient demographics from FHIR Patient resource."""
        data = await self._request("GET", f"Patient/{patient_id}")
        
        # Parse FHIR Patient resource
        name = data.get("name", [{}])[0]
        given = name.get("given", [])
        
        # Extract MRN from identifiers
        mrn = None
        for identifier in data.get("identifier", []):
            if identifier.get("type", {}).get("text") == "MRN":
                mrn = identifier.get("value")
                break
        
        return PatientInfo(
            id=data["id"],
            mrn=mrn,
            given_name=given[0] if given else None,
            family_name=name.get("family"),
            birth_date=data.get("birthDate"),
            gender=data.get("gender"),
        )
    
    async def get_encounter(self, encounter_id: str) -> Encounter:
        """Fetch encounter details from FHIR Encounter resource."""
        data = await self._request("GET", f"Encounter/{encounter_id}")
        
        # Parse period
        period = data.get("period", {})
        
        # Parse diagnoses
        diagnoses = []
        for dx in data.get("diagnosis", []):
            condition_ref = dx.get("condition", {}).get("reference")
            if condition_ref:
                diagnoses.append(condition_ref)
        
        # Parse reason
        reasons = data.get("reasonCode", [])
        reason_text = reasons[0].get("text") if reasons else None
        
        return Encounter(
            id=data["id"],
            status=data.get("status", "unknown"),
            encounter_class=data.get("class", {}).get("code"),
            start_date=period.get("start"),
            end_date=period.get("end"),
            reason_for_visit=reason_text,
            diagnoses=diagnoses,
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
        """
        Search for clinical notes using DocumentReference.Search (R4).
        
        This corresponds to Epic spec 5454.
        
        Args:
            patient_id: FHIR Patient ID
            encounter_id: Optional FHIR Encounter ID to filter by
            note_types: Optional list of LOINC codes for note types
            date_from: Optional start date filter
            date_to: Optional end date filter
            max_results: Maximum number of results to return
        
        Returns:
            List of ClinicalNote objects
        """
        params: Dict[str, Any] = {
            "patient": patient_id,
            "status": "current",
            "_count": min(max_results, 100),
        }
        
        if encounter_id:
            params["encounter"] = encounter_id
        
        if note_types:
            # LOINC codes for document types
            params["type"] = ",".join(note_types)
        
        if date_from:
            params["date"] = f"ge{date_from.strftime('%Y-%m-%d')}"
        
        if date_to:
            params["date"] = f"le{date_to.strftime('%Y-%m-%d')}"
        
        data = await self._request("GET", "DocumentReference", params=params)
        
        notes = []
        for entry in data.get("entry", []):
            resource = entry.get("resource", {})
            note = self._parse_document_reference(resource, patient_id)
            if note:
                notes.append(note)
        
        logger.info(
            "Retrieved clinical notes",
            patient_id=patient_id,
            count=len(notes)
        )
        
        return notes
    
    def _parse_document_reference(
        self,
        resource: Dict[str, Any],
        patient_id: str
    ) -> Optional[ClinicalNote]:
        """Parse a FHIR DocumentReference resource into a ClinicalNote."""
        try:
            # Get document type
            type_coding = resource.get("type", {}).get("coding", [{}])[0]
            type_code = type_coding.get("code")
            type_display = type_coding.get("display", "Other")
            
            # Map to NoteType enum
            note_type = self._map_loinc_to_note_type(type_code, type_display)
            
            # Get content
            content_list = resource.get("content", [])
            content_text = None
            content_type = "text/plain"
            
            for content in content_list:
                attachment = content.get("attachment", {})
                content_type = attachment.get("contentType", "text/plain")
                
                # Content might be inline (data) or a URL
                if "data" in attachment:
                    import base64
                    content_text = base64.b64decode(attachment["data"]).decode("utf-8")
                elif "url" in attachment:
                    # Would need to fetch the document separately
                    content_text = f"[Document available at: {attachment['url']}]"
            
            # Get author
            author = None
            authors = resource.get("author", [])
            if authors:
                author_ref = authors[0]
                author = Author(
                    id=author_ref.get("reference"),
                    name=author_ref.get("display")
                )
            
            # Get encounter reference
            context = resource.get("context", {})
            encounter_refs = context.get("encounter", [])
            encounter_id = None
            if encounter_refs:
                encounter_id = encounter_refs[0].get("reference", "").replace("Encounter/", "")
            
            return ClinicalNote(
                id=resource["id"],
                status=DocumentStatus(resource.get("status", "current")),
                type=note_type,
                type_code=type_code,
                title=resource.get("description"),
                content=content_text,
                content_type=content_type,
                date=resource.get("date"),
                author=author,
                patient_id=patient_id,
                encounter_id=encounter_id,
                document_reference_id=resource["id"],
            )
        except Exception as e:
            logger.warning("Failed to parse DocumentReference", error=str(e))
            return None
    
    def _map_loinc_to_note_type(self, code: Optional[str], display: str) -> NoteType:
        """Map LOINC code to NoteType enum."""
        # Common LOINC codes for clinical notes
        loinc_mapping = {
            "11506-3": NoteType.PROGRESS_NOTE,
            "18842-5": NoteType.DISCHARGE_SUMMARY,
            "34117-2": NoteType.H_AND_P,
            "11488-4": NoteType.CONSULTATION,
            "11504-8": NoteType.OPERATIVE_NOTE,
            "28570-0": NoteType.PROCEDURE_NOTE,
            "34746-8": NoteType.NURSING_NOTE,
            "18748-4": NoteType.RADIOLOGY_REPORT,
            "11502-2": NoteType.LAB_REPORT,
        }
        
        if code and code in loinc_mapping:
            return loinc_mapping[code]
        
        # Try matching by display text
        display_lower = display.lower()
        if "progress" in display_lower:
            return NoteType.PROGRESS_NOTE
        elif "discharge" in display_lower:
            return NoteType.DISCHARGE_SUMMARY
        elif "history" in display_lower or "h&p" in display_lower:
            return NoteType.H_AND_P
        elif "consult" in display_lower:
            return NoteType.CONSULTATION
        elif "operative" in display_lower:
            return NoteType.OPERATIVE_NOTE
        
        return NoteType.OTHER
    
    async def get_document_content(self, document_url: str) -> str:
        """
        Fetch the actual content of a document from its URL.
        
        Some DocumentReferences only contain a URL to the actual content,
        which needs to be fetched separately.
        """
        token = await self._get_access_token()
        
        async with httpx.AsyncClient() as client:
            response = await client.get(
                document_url,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "*/*"
                },
                timeout=30.0
            )
        
        if response.status_code != 200:
            raise EpicAPIError(f"Failed to fetch document: {response.status_code}")
        
        return response.text

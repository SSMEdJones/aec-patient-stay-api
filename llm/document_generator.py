"""LLM-powered clinical document generator."""
import json
import urllib3
import structlog
from typing import List, Optional, Union
from pathlib import Path
from datetime import datetime

from models import ClinicalNote, PatientInfo, Encounter, PatientStaySummary
from config import (
    LLM_PROVIDER, AWS_REGION, BEDROCK_MODEL_ID,
    AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_API_KEY, AZURE_OPENAI_DEPLOYMENT, AZURE_OPENAI_API_VERSION,
    OPENAI_API_KEY, OPENAI_MODEL,
    LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY, LANGFUSE_HOST, LANGFUSE_ENABLED
)

# Disable SSL warnings for corporate networks (same as aec-mychart-qa-api)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logger = structlog.get_logger(__name__)

# Initialize Langfuse if configured
langfuse_client = None
if LANGFUSE_ENABLED:
    try:
        from langfuse import Langfuse
        langfuse_client = Langfuse(
            public_key=LANGFUSE_PUBLIC_KEY,
            secret_key=LANGFUSE_SECRET_KEY,
            host=LANGFUSE_HOST
        )
        logger.info("Langfuse initialized for LLM observability", host=LANGFUSE_HOST)
    except Exception as e:
        logger.warning("Failed to initialize Langfuse", error=str(e))


class BedrockClient:
    """Wrapper for AWS Bedrock API calls (same pattern as aec-mychart-qa-api)."""
    
    def __init__(self, model_id: str = None):
        import boto3
        self.model_id = model_id or BEDROCK_MODEL_ID
        # verify=False for corporate network SSL
        self.client = boto3.client(
            "bedrock-runtime",
            region_name=AWS_REGION,
            verify=False
        )
    
    def get_model_info(self) -> str:
        """Return model information for logging."""
        return f"AWS Bedrock {self.model_id}"
    
    def chat_completion(
        self,
        messages: List[dict],
        temperature: float = 0.3,
        max_tokens: int = 2000,
        response_format: Optional[dict] = None
    ) -> str:
        """Make a chat completion request to Bedrock."""
        # Format for Claude models
        system_msg = None
        user_messages = []
        
        for msg in messages:
            if msg["role"] == "system":
                system_msg = msg["content"]
            else:
                user_messages.append({
                    "role": msg["role"],
                    "content": msg["content"]
                })
        
        request_body = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": user_messages,
        }
        
        if system_msg:
            request_body["system"] = system_msg
        
        response = self.client.invoke_model(
            modelId=self.model_id,
            body=json.dumps(request_body),
            contentType="application/json",
            accept="application/json"
        )
        
        response_body = json.loads(response["body"].read())
        output_text = response_body["content"][0]["text"]
        
        # Log to Langfuse if enabled
        if langfuse_client:
            try:
                trace = langfuse_client.trace(
                    name="bedrock-chat-completion",
                    metadata={"model": self.model_id, "temperature": temperature}
                )
                trace.generation(
                    name="chat-completion",
                    model=self.model_id,
                    input=messages,
                    output=output_text,
                    usage={
                        "input": response_body.get("usage", {}).get("input_tokens", 0),
                        "output": response_body.get("usage", {}).get("output_tokens", 0)
                    }
                )
            except Exception as e:
                logger.warning("Langfuse logging failed", error=str(e))
        
        return output_text


class DocumentGenerator:
    """
    Generates clinical summaries from patient notes using LLMs.
    
    Supports AWS Bedrock (Claude), Azure OpenAI, and direct OpenAI API.
    """
    
    def __init__(self, template_dir: str = "templates"):
        self.template_dir = Path(template_dir)
        self._client = None
        self._model: str = ""
        self._provider: str = ""
        self._initialize_client()
    
    def _initialize_client(self):
        """Initialize the appropriate LLM client based on config."""
        self._provider = LLM_PROVIDER
        
        if self._provider == "bedrock":
            self._client = BedrockClient(model_id=BEDROCK_MODEL_ID)
            self._model = BEDROCK_MODEL_ID
            logger.info("Initialized AWS Bedrock client", model=self._model)
        elif self._provider == "azure":
            from openai import AzureOpenAI
            self._client = AzureOpenAI(
                azure_endpoint=AZURE_OPENAI_ENDPOINT,
                api_key=AZURE_OPENAI_API_KEY,
                api_version=AZURE_OPENAI_API_VERSION,
            )
            self._model = AZURE_OPENAI_DEPLOYMENT
            logger.info("Initialized Azure OpenAI client", deployment=self._model)
        else:
            from openai import OpenAI
            self._client = OpenAI(api_key=OPENAI_API_KEY)
            self._model = OPENAI_MODEL
            logger.info("Initialized OpenAI client", model=self._model)
    
    def _call_llm(
        self,
        messages: List[dict],
        temperature: float = 0.3,
        max_tokens: int = 2000,
        response_format: Optional[dict] = None
    ) -> str:
        """Call the LLM with the given messages."""
        if self._provider == "bedrock":
            return self._client.chat_completion(
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                response_format=response_format
            )
        else:
            # OpenAI / Azure OpenAI
            kwargs = {
                "model": self._model,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
            }
            if response_format:
                kwargs["response_format"] = response_format
            
            response = self._client.chat.completions.create(**kwargs)
            return response.choices[0].message.content
    
    def _load_template(self, template_name: str) -> str:
        """Load a prompt template from the templates directory."""
        template_path = self.template_dir / template_name
        if template_path.exists():
            return template_path.read_text()
        
        logger.warning(f"Template not found: {template_name}, using default")
        return self._get_default_template(template_name)
    
    def _get_default_template(self, template_name: str) -> str:
        """Return default template if file not found."""
        if "summary" in template_name.lower():
            return """You are a clinical documentation specialist. Generate a comprehensive patient stay summary.

PATIENT INFORMATION:
{patient_info}

ENCOUNTER DETAILS:
{encounter_info}

CLINICAL NOTES:
{clinical_notes}

Generate a structured summary including:
1. Brief patient overview
2. Reason for admission/visit
3. Key clinical findings
4. Diagnoses (primary and secondary)
5. Treatments provided
6. Follow-up recommendations

Use clear, professional medical language. Be concise but thorough."""
        
        return "{content}"
    
    def _format_patient_info(self, patient: PatientInfo) -> str:
        """Format patient information for the prompt."""
        lines = [
            f"Name: {patient.full_name}",
            f"MRN: {patient.mrn or 'N/A'}",
            f"Date of Birth: {patient.birth_date or 'N/A'}",
            f"Gender: {patient.gender or 'N/A'}",
        ]
        return "\n".join(lines)
    
    def _format_encounter_info(self, encounter: Encounter) -> str:
        """Format encounter information for the prompt."""
        lines = [
            f"Encounter ID: {encounter.id}",
            f"Status: {encounter.status}",
            f"Class: {encounter.encounter_class or 'N/A'}",
            f"Start Date: {encounter.start_date or 'N/A'}",
            f"End Date: {encounter.end_date or 'N/A'}",
            f"Location: {encounter.location or 'N/A'}",
            f"Reason for Visit: {encounter.reason_for_visit or 'N/A'}",
        ]
        if encounter.diagnoses:
            lines.append(f"Diagnoses: {', '.join(encounter.diagnoses)}")
        return "\n".join(lines)
    
    def _format_clinical_notes(self, notes: List[ClinicalNote]) -> str:
        """Format clinical notes for the prompt."""
        formatted = []
        for i, note in enumerate(notes, 1):
            note_text = f"""
--- Note {i}: {note.type.value} ---
Date: {note.date or 'N/A'}
Author: {note.author.name if note.author else 'N/A'}
Title: {note.title or 'Untitled'}

{note.content or '[No content available]'}
"""
            formatted.append(note_text)
        
        return "\n".join(formatted)
    
    def generate_stay_summary(
        self,
        patient: PatientInfo,
        encounter: Encounter,
        notes: List[ClinicalNote],
        template_name: str = "clinical_summary.txt"
    ) -> PatientStaySummary:
        """
        Generate a comprehensive patient stay summary using the LLM.
        
        Args:
            patient: Patient demographics
            encounter: Encounter/visit details
            notes: List of clinical notes from the stay
            template_name: Name of the prompt template to use
        
        Returns:
            PatientStaySummary with LLM-generated content
        """
        # Load and format template
        template = self._load_template(template_name)
        prompt = template.format(
            patient_info=self._format_patient_info(patient),
            encounter_info=self._format_encounter_info(encounter),
            clinical_notes=self._format_clinical_notes(notes),
        )
        
        logger.info(
            "Generating stay summary",
            patient_id=patient.id,
            encounter_id=encounter.id,
            note_count=len(notes)
        )
        
        # Call LLM
        summary_text = self._call_llm(
            messages=[
                {
                    "role": "system",
                    "content": "You are a clinical documentation specialist with expertise in medical summarization. Provide accurate, clear, and professionally structured clinical summaries."
                },
                {
                    "role": "user",
                    "content": prompt
                }
            ],
            temperature=0.3,  # Lower temperature for more consistent clinical output
            max_tokens=2000,
        )
        
        # Parse structured elements from the summary
        key_findings, diagnoses, treatments, follow_up = self._extract_structured_elements(
            summary_text
        )
        
        return PatientStaySummary(
            patient=patient,
            encounter=encounter,
            clinical_notes=notes,
            summary=summary_text,
            key_findings=key_findings,
            diagnoses=diagnoses,
            treatments=treatments,
            follow_up_recommendations=follow_up,
            model_used=self._model,
        )
    
    def _extract_structured_elements(
        self,
        summary: str
    ) -> tuple[List[str], List[str], List[str], List[str]]:
        """
        Extract structured elements from the generated summary.
        
        Uses another LLM call to parse the summary into structured lists.
        """
        extraction_prompt = f"""Extract the following from this clinical summary as JSON:
- key_findings: List of key clinical findings
- diagnoses: List of diagnoses
- treatments: List of treatments provided
- follow_up: List of follow-up recommendations

Summary:
{summary}

Respond ONLY with valid JSON in this format:
{{"key_findings": [...], "diagnoses": [...], "treatments": [...], "follow_up": [...]}}"""

        try:
            response_text = self._call_llm(
                messages=[{"role": "user", "content": extraction_prompt}],
                temperature=0,
                max_tokens=1000,
                response_format={"type": "json_object"} if self._provider != "bedrock" else None,
            )
            
            # For Bedrock, extract JSON from response
            if self._provider == "bedrock":
                # Find JSON in response
                import re
                json_match = re.search(r'\{.*\}', response_text, re.DOTALL)
                if json_match:
                    response_text = json_match.group()
            
            data = json.loads(response_text)
            
            return (
                data.get("key_findings", []),
                data.get("diagnoses", []),
                data.get("treatments", []),
                data.get("follow_up", []),
            )
        except Exception as e:
            logger.warning("Failed to extract structured elements", error=str(e))
            return [], [], [], []
    
    def generate_discharge_instructions(
        self,
        patient: PatientInfo,
        summary: PatientStaySummary,
        reading_level: str = "patient-friendly"
    ) -> str:
        """
        Generate patient-friendly discharge instructions.
        
        Args:
            patient: Patient information
            summary: Generated patient stay summary
            reading_level: "patient-friendly", "detailed", or "simple"
        
        Returns:
            Formatted discharge instructions
        """
        style_instructions = {
            "patient-friendly": "Use clear, simple language that a patient with no medical background can understand. Avoid medical jargon.",
            "detailed": "Provide comprehensive instructions with medical terminology explained.",
            "simple": "Use very simple language, short sentences, and bullet points. Suitable for low health literacy."
        }
        
        prompt = f"""Create discharge instructions for this patient:

Patient: {patient.full_name}

Summary of Stay:
{summary.summary}

Diagnoses: {', '.join(summary.diagnoses) if summary.diagnoses else 'See summary'}
Treatments: {', '.join(summary.treatments) if summary.treatments else 'See summary'}
Follow-up: {', '.join(summary.follow_up_recommendations) if summary.follow_up_recommendations else 'See summary'}

Style: {style_instructions.get(reading_level, style_instructions['patient-friendly'])}

Generate clear discharge instructions including:
1. What happened during their stay
2. Medications to take at home
3. Activity restrictions
4. Warning signs to watch for
5. Follow-up appointments needed
6. Who to contact with questions"""

        instructions = self._call_llm(
            messages=[
                {
                    "role": "system",
                    "content": "You are a patient education specialist. Create clear, actionable discharge instructions."
                },
                {"role": "user", "content": prompt}
            ],
            temperature=0.4,
            max_tokens=1500,
        )
        
        return instructions

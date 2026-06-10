"""Configuration settings for the Epic Clinical Notes API."""
import os
from dotenv import load_dotenv

# Load .env file
load_dotenv()

# =============================================================================
# AWS Configuration (same pattern as aec-mychart-qa-api)
# =============================================================================
AWS_REGION = os.getenv("AWS_REGION", "us-east-1")

# LLM Configuration
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "bedrock")  # bedrock, azure, or openai
BEDROCK_MODEL_ID = os.getenv("BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-20250514-v1:0")  # Same as mychart-qa

# Azure OpenAI (alternative)
AZURE_OPENAI_ENDPOINT = os.getenv("AZURE_OPENAI_ENDPOINT", "")
AZURE_OPENAI_API_KEY = os.getenv("AZURE_OPENAI_API_KEY", "")
AZURE_OPENAI_DEPLOYMENT = os.getenv("AZURE_OPENAI_DEPLOYMENT", "gpt-4")
AZURE_OPENAI_API_VERSION = os.getenv("AZURE_OPENAI_API_VERSION", "2024-02-15-preview")

# OpenAI direct (alternative)
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4-turbo-preview")

# =============================================================================
# Epic FHIR API Configuration
# =============================================================================
EPIC_BASE_URL = os.getenv("EPIC_BASE_URL", "https://fhir.epic.com/interconnect-fhir-oauth/api/FHIR/R4")
EPIC_CLIENT_ID = os.getenv("EPIC_CLIENT_ID", "")
EPIC_CLIENT_SECRET = os.getenv("EPIC_CLIENT_SECRET", "")
EPIC_PRIVATE_KEY_PATH = os.getenv("EPIC_PRIVATE_KEY_PATH", "./keys/epic_private_key.pem")
EPIC_TOKEN_URL = os.getenv("EPIC_TOKEN_URL", "https://fhir.epic.com/interconnect-fhir-oauth/oauth2/token")

# OAuth2 Patient Flow
EPIC_AUTHORIZE_URL = os.getenv("EPIC_AUTHORIZE_URL", "https://fhir.epic.com/interconnect-fhir-oauth/oauth2/authorize")
_default_port = os.getenv("API_PORT", "8001")
EPIC_REDIRECT_URI = os.getenv("EPIC_REDIRECT_URI", f"http://localhost:{_default_port}/callback")
EPIC_SCOPE = os.getenv("EPIC_SCOPE", "openid fhirUser patient/*.read")

# =============================================================================
# Application Settings
# =============================================================================
API_PORT = int(os.getenv("API_PORT", "8001"))  # Use 8001 to avoid collision with mychart-qa (8000)
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
DEBUG_MODE = os.getenv("DEBUG_MODE", "False").lower() == "true"

# =============================================================================
# Langfuse LLM Observability
# =============================================================================
LANGFUSE_PUBLIC_KEY = os.getenv("LANGFUSE_PUBLIC_KEY", "")
LANGFUSE_SECRET_KEY = os.getenv("LANGFUSE_SECRET_KEY", "")
# Accept both LANGFUSE_HOST and LANGFUSE_BASE_URL (used by AI Portal)
LANGFUSE_HOST = os.getenv("LANGFUSE_HOST") or os.getenv("LANGFUSE_BASE_URL") or "https://cloud.langfuse.com"
LANGFUSE_PROJECT_ID = os.getenv("LANGFUSE_PROJECT_ID", "cmpyima37000zpd07vdrkhp9f")  # Project ID for trace URLs
LANGFUSE_ENABLED = bool(LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY)

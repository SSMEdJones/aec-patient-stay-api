"""
Fast Appeal Lambda Function Handler

Wraps the FastAPI application with Mangum for AWS Lambda + API Gateway.
"""
import sys
from pathlib import Path

# Add parent directory to path so we can import the main app
parent_dir = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(parent_dir))

from mangum import Mangum
from app import app

# Create the Lambda handler
handler = Mangum(app, lifespan="off")

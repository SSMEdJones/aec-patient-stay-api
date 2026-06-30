#!/bin/bash
#
# Build and deploy Fast Appeal API to AWS Lambda
# Usage: ./scripts/deploy.sh <environment>
#        ./scripts/deploy.sh dev
#        ./scripts/deploy.sh prod --skip-build
#

set -e

ENVIRONMENT="${1:-dev}"
SKIP_BUILD="${2:-}"

# Configuration
AWS_REGION="us-east-1"
ECR_REPO="fast-appeal-api"
IMAGE_TAG="latest"

echo ""
echo "=== Fast Appeal API Deployment ($ENVIRONMENT) ==="

# Validate environment
if [[ ! "$ENVIRONMENT" =~ ^(dev|stg|prod)$ ]]; then
    echo "Error: Environment must be dev, stg, or prod"
    exit 1
fi

# Get AWS account ID
echo ""
echo "[1/5] Getting AWS account info..."
AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
if [ -z "$AWS_ACCOUNT_ID" ]; then
    echo "Error: Failed to get AWS account ID. Make sure you're logged in."
    exit 1
fi
echo "  Account: $AWS_ACCOUNT_ID"

ECR_URI="$AWS_ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com"
IMAGE_URI="$ECR_URI/$ECR_REPO:$IMAGE_TAG"

# Create ECR repository if needed
echo ""
echo "[2/5] Ensuring ECR repository exists..."
if ! aws ecr describe-repositories --repository-names "$ECR_REPO" --region "$AWS_REGION" 2>/dev/null; then
    echo "  Creating ECR repository..."
    aws ecr create-repository --repository-name "$ECR_REPO" --region "$AWS_REGION"
fi
echo "  ECR repo: $ECR_REPO"

# Login to ECR
echo ""
echo "[3/5] Logging into ECR..."
aws ecr get-login-password --region "$AWS_REGION" | docker login --username AWS --password-stdin "$ECR_URI"
echo "  Logged in"

# Build and push Docker image
if [ "$SKIP_BUILD" != "--skip-build" ]; then
    echo ""
    echo "[4/5] Building and pushing Docker image..."
    
    # Build
    docker build -t "$ECR_REPO:$IMAGE_TAG" .
    
    # Tag
    docker tag "$ECR_REPO:$IMAGE_TAG" "$IMAGE_URI"
    
    # Push
    docker push "$IMAGE_URI"
    echo "  Image pushed: $IMAGE_URI"
else
    echo ""
    echo "[4/5] Skipping build (using existing image)"
fi

# Update samconfig.toml with account ID
echo ""
echo "[5/5] Deploying with SAM CLI..."
sed -i.bak "s/<ACCOUNT_ID>/$AWS_ACCOUNT_ID/g" samconfig.toml

# Deploy
sam deploy --config-env "$ENVIRONMENT" --no-fail-on-empty-changeset

# Get outputs
echo ""
echo "=== Deployment Complete ==="
echo ""
echo "Stack Outputs:"
aws cloudformation describe-stacks \
    --stack-name "fast-appeal-api-$ENVIRONMENT" \
    --query "Stacks[0].Outputs" \
    --output table

echo ""
echo "Use the CloudFrontUrl output value for VITE_FAST_APPEAL_URL"

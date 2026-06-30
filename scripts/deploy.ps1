<#
.SYNOPSIS
    Build and deploy Fast Appeal API to AWS Lambda

.DESCRIPTION
    Builds Docker image, pushes to ECR, and deploys via SAM CLI

.PARAMETER Environment
    Target environment: dev, stg, or prod

.PARAMETER SkipBuild
    Skip Docker build step (use existing image)

.EXAMPLE
    .\scripts\deploy.ps1 -Environment dev
    .\scripts\deploy.ps1 -Environment prod -SkipBuild
#>

param(
    [Parameter(Mandatory=$true)]
    [ValidateSet("dev", "stg", "prod")]
    [string]$Environment,
    
    [switch]$SkipBuild
)

$ErrorActionPreference = "Stop"

# Configuration
$AWS_REGION = "us-east-1"
$ECR_REPO = "fast-appeal-api"
$IMAGE_TAG = "latest"

Write-Host "`n=== Fast Appeal API Deployment ($Environment) ===" -ForegroundColor Cyan

# Get AWS account ID
Write-Host "`n[1/5] Getting AWS account info..." -ForegroundColor Yellow
$AWS_ACCOUNT_ID = aws sts get-caller-identity --query Account --output text
if ($LASTEXITCODE -ne 0) {
    Write-Error "Failed to get AWS account ID. Make sure you're logged in."
    exit 1
}
Write-Host "  Account: $AWS_ACCOUNT_ID" -ForegroundColor Green

$ECR_URI = "$AWS_ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com"
$IMAGE_URI = "$ECR_URI/${ECR_REPO}:$IMAGE_TAG"

# Create ECR repository if needed
Write-Host "`n[2/5] Ensuring ECR repository exists..." -ForegroundColor Yellow
$repoExists = aws ecr describe-repositories --repository-names $ECR_REPO --region $AWS_REGION 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "  Creating ECR repository..." -ForegroundColor Yellow
    aws ecr create-repository --repository-name $ECR_REPO --region $AWS_REGION
}
Write-Host "  ECR repo: $ECR_REPO" -ForegroundColor Green

# Login to ECR
Write-Host "`n[3/5] Logging into ECR..." -ForegroundColor Yellow
aws ecr get-login-password --region $AWS_REGION | docker login --username AWS --password-stdin $ECR_URI
if ($LASTEXITCODE -ne 0) {
    Write-Error "ECR login failed"
    exit 1
}
Write-Host "  Logged in" -ForegroundColor Green

# Build and push Docker image
if (-not $SkipBuild) {
    Write-Host "`n[4/5] Building and pushing Docker image..." -ForegroundColor Yellow
    
    # Build
    docker build -t "${ECR_REPO}:$IMAGE_TAG" .
    if ($LASTEXITCODE -ne 0) {
        Write-Error "Docker build failed"
        exit 1
    }
    
    # Tag
    docker tag "${ECR_REPO}:$IMAGE_TAG" $IMAGE_URI
    
    # Push
    docker push $IMAGE_URI
    if ($LASTEXITCODE -ne 0) {
        Write-Error "Docker push failed"
        exit 1
    }
    Write-Host "  Image pushed: $IMAGE_URI" -ForegroundColor Green
} else {
    Write-Host "`n[4/5] Skipping build (using existing image)" -ForegroundColor DarkGray
}

# Update samconfig.toml with account ID
Write-Host "`n[5/5] Deploying with SAM CLI..." -ForegroundColor Yellow

# Replace placeholder account ID in samconfig.toml
$samconfig = Get-Content samconfig.toml -Raw
$samconfig = $samconfig -replace '<ACCOUNT_ID>', $AWS_ACCOUNT_ID
$samconfig | Set-Content samconfig.toml

# Deploy
sam deploy --config-env $Environment --no-fail-on-empty-changeset

if ($LASTEXITCODE -ne 0) {
    Write-Error "SAM deployment failed"
    exit 1
}

# Get outputs
Write-Host "`n=== Deployment Complete ===" -ForegroundColor Cyan
Write-Host "`nStack Outputs:" -ForegroundColor Yellow

aws cloudformation describe-stacks `
    --stack-name "fast-appeal-api-$Environment" `
    --query "Stacks[0].Outputs" `
    --output table

Write-Host "`nUse the CloudFrontUrl output value for VITE_FAST_APPEAL_URL" -ForegroundColor Green

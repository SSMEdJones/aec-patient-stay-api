# Local AWS Credential Refresh Script
# Usage: Paste your credentials from AWS Console, then run this script
#
# Before running, set these values from AWS Console -> "Command line or programmatic access":
param(
    [Parameter(Mandatory=$true)]
    [string]$AccessKeyId,
    
    [Parameter(Mandatory=$true)]
    [string]$SecretAccessKey,
    
    [Parameter(Mandatory=$true)]
    [string]$SessionToken,
    
    [string]$Region = "us-east-1"
)

Write-Host "`n=== AWS Credential Refresh for Local Dev ===" -ForegroundColor Cyan

# 1. Kill any running Python processes (releases cached credentials)
Write-Host "`n1. Stopping Python processes..." -ForegroundColor Yellow
$pythonProcs = Get-Process python* -ErrorAction SilentlyContinue
if ($pythonProcs) {
    $pythonProcs | Stop-Process -Force
    Write-Host "   Stopped $($pythonProcs.Count) Python process(es)" -ForegroundColor Gray
} else {
    Write-Host "   No Python processes running" -ForegroundColor Gray
}

# 2. Set AWS credentials
Write-Host "`n2. Setting AWS credentials..." -ForegroundColor Yellow
aws configure set aws_access_key_id $AccessKeyId
aws configure set aws_secret_access_key $SecretAccessKey
aws configure set aws_session_token $SessionToken
aws configure set region $Region
Write-Host "   Credentials saved to ~/.aws/credentials" -ForegroundColor Gray

# 3. Verify with STS
Write-Host "`n3. Verifying credentials with AWS STS..." -ForegroundColor Yellow
try {
    $identity = aws sts get-caller-identity 2>&1
    if ($LASTEXITCODE -eq 0) {
        $parsed = $identity | ConvertFrom-Json
        Write-Host "   Account: $($parsed.Account)" -ForegroundColor Green
        Write-Host "   User: $($parsed.Arn.Split('/')[-1])" -ForegroundColor Green
    } else {
        Write-Host "   STS call failed: $identity" -ForegroundColor Red
        exit 1
    }
} catch {
    Write-Host "   Error: $_" -ForegroundColor Red
    exit 1
}

# 4. Test Bedrock (the actual service the app uses)
Write-Host "`n4. Testing AWS Bedrock connection..." -ForegroundColor Yellow
$pythonPath = "C:\Users\ejones08\AppData\Local\Microsoft\WindowsApps\python3.12.exe"
$testScript = @"
import boto3
try:
    client = boto3.client('bedrock-runtime', region_name='us-east-1')
    response = client.converse(
        modelId='us.anthropic.claude-sonnet-4-20250514-v1:0',
        messages=[{'role': 'user', 'content': [{'text': 'Say OK'}]}],
        inferenceConfig={'maxTokens': 5}
    )
    print('SUCCESS')
except Exception as e:
    print(f'FAILED: {e}')
"@

$result = & $pythonPath -c $testScript 2>&1
if ($result -match "SUCCESS") {
    Write-Host "   Bedrock connection: OK" -ForegroundColor Green
} else {
    Write-Host "   Bedrock connection: FAILED" -ForegroundColor Red
    Write-Host "   $result" -ForegroundColor Gray
    exit 1
}

# 5. Summary
Write-Host "`n=== Credentials Ready ===" -ForegroundColor Green
Write-Host "Start the server with:" -ForegroundColor Cyan
Write-Host "  cd $PWD" -ForegroundColor White
Write-Host "  $pythonPath -m uvicorn app:app --reload --port 8001" -ForegroundColor White
Write-Host ""

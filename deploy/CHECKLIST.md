# Patient Stay Appeal Letter API - Deployment Checklist

## Deployment Configuration

| Environment | Server | Site | Physical Path | Port |
|-------------|--------|------|---------------|------|
| DEV | S927-WBAPPDEV1 | CAPS | F:\inetpub-S928-STGSP3WIL2\PatientStayAPI | 8001 |
| STG | (TBD) | CAPS | (TBD) | 8001 |
| PROD | (TBD) | CAPS | (TBD) | 8001 |

---

## On Your Development Machine (This PC)

### Step 1: Create Deployment Package
```powershell
cd C:\Users\ejones08\source\repos\aec-patient-stay-api

# Create a zip file with everything needed
Compress-Archive -Path @(
    ".\app.py",
    ".\config.py",
    ".\main.py",
    ".\models.py",
    ".\requirements.txt",
    ".\api",
    ".\llm",
    ".\services",
    ".\static",
    ".\templates",
    ".\deploy\web.config",
    ".\.env"
) -DestinationPath ".\patient-stay-deploy.zip" -Force
```

### Step 2: Copy to Server
```powershell
# Copy to DEV server
Copy-Item ".\patient-stay-deploy.zip" -Destination "\\S927-WBAPPDEV1\f$\temp\patient-stay-deploy.zip"
```

---

## On the Web Server (Run as Administrator)

### Step 3: Prerequisites (One-time - likely already done for MyChartQA)
- [ ] **Python 3.10+** - Should already be installed
- [ ] **IIS URL Rewrite** - Should already be installed
- [ ] **IIS ARR** - Should already be installed with proxy enabled

### Step 4: Extract and Setup Files
```powershell
# Set the deployment path
$deployPath = "F:\inetpub-S928-STGSP3WIL2\PatientStayAPI"

# Create folder
New-Item -ItemType Directory -Path $deployPath -Force

# Extract zip
Expand-Archive -Path "F:\temp\patient-stay-deploy.zip" -DestinationPath "F:\temp\patient-stay-extract" -Force

# Copy all files
Copy-Item -Path "F:\temp\patient-stay-extract\*" -Destination $deployPath -Recurse -Force

# Copy web.config to root
Copy-Item -Path "$deployPath\deploy\web.config" -Destination "$deployPath\web.config" -Force

# Move .env to root (if it ended up in a subfolder)
if (Test-Path "$deployPath\.env") {
    Write-Host ".env already in place"
} else {
    Write-Host "Please copy .env file to $deployPath"
}
```

### Step 5: Setup Python Environment
```powershell
$deployPath = "F:\inetpub-S928-STGSP3WIL2\PatientStayAPI"
cd $deployPath

# Create virtual environment
python -m venv .venv

# Install dependencies
.\.venv\Scripts\pip.exe install -r requirements.txt
```

### Step 6: Configure Environment
Edit the `.env` file:
```powershell
notepad "$deployPath\.env"
```

Required settings:
```ini
# AWS Bedrock (for LLM)
AWS_REGION=us-east-1
AWS_PROFILE=dev1

# LLM
LLM_PROVIDER=bedrock
BEDROCK_MODEL_ID=us.anthropic.claude-sonnet-4-20250514-v1:0

# App
API_PORT=8001
DEBUG_MODE=False

# Langfuse (optional - for LLM observability)
# LANGFUSE_PUBLIC_KEY=pk-lf-...
# LANGFUSE_SECRET_KEY=sk-lf-...
# LANGFUSE_HOST=https://your-langfuse-instance.com
```

### Step 7: Create IIS Application (One-time)
```powershell
Import-Module WebAdministration

# Create the application under CAPS
New-WebApplication -Name "patient-stay" -Site "CAPS" -PhysicalPath "F:\inetpub-S928-STGSP3WIL2\PatientStayAPI" -ApplicationPool "CapitalRequest"
```

### Step 8: Install API as Scheduled Task
```powershell
$deployPath = "F:\inetpub-S928-STGSP3WIL2\PatientStayAPI"

# Create logs folder
New-Item -ItemType Directory -Path "$deployPath\logs" -Force

# Create scheduled task to run Python API as SYSTEM
$action = New-ScheduledTaskAction -Execute "$deployPath\.venv\Scripts\python.exe" -Argument "app.py" -WorkingDirectory $deployPath
$trigger = New-ScheduledTaskTrigger -AtStartup
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit (New-TimeSpan -Days 365)

Register-ScheduledTask -TaskName "PatientStay" -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force

# Start the task now
Start-ScheduledTask -TaskName "PatientStay"
```

### Step 9: Configure AWS Credentials for SYSTEM
```powershell
# First, make sure AWS SSO is configured for your user
aws sso login --profile dev1

# For SYSTEM to use AWS, you need to either:
# Option A: Copy SSO cache (temporary - expires)
Copy-Item -Path "$env:USERPROFILE\.aws" -Destination "C:\Windows\System32\config\systemprofile\.aws" -Recurse -Force

# Option B: Use IAM credentials (recommended for servers)
# Create IAM credentials in AWS Console, then:
# 1. Create C:\Windows\System32\config\systemprofile\.aws\credentials with:
#    [default]
#    aws_access_key_id = AKIA...
#    aws_secret_access_key = ...
#    region = us-east-1
```

### Step 10: Test
```powershell
# Test API directly
Invoke-RestMethod -Uri "http://localhost:8001/health"

# Test appeal page directly
Start-Process "http://localhost:8001/appeal"

# Test via IIS
Start-Process "http://caps-dev.ssmhc.com/patient-stay/appeal"
```

- [ ] Open browser: `http://caps-dev.ssmhc.com/patient-stay/appeal` → Should show upload form
- [ ] Test health: `http://caps-dev.ssmhc.com/patient-stay/health` → Should return JSON
- [ ] Test PDF upload and appeal generation

---

## Managing the API

### View Status
```powershell
Get-ScheduledTask -TaskName "PatientStay"
Get-ScheduledTask -TaskName "PatientStay" | Get-ScheduledTaskInfo
```

### Restart API
```powershell
Stop-ScheduledTask -TaskName "PatientStay"
Start-ScheduledTask -TaskName "PatientStay"
```

### View Logs
```powershell
Get-Content "F:\inetpub-S928-STGSP3WIL2\PatientStayAPI\logs\*.log" -Tail 50
```

---

## Troubleshooting

### API not responding (502 Bad Gateway)
1. Check if scheduled task is running:
   ```powershell
   Get-ScheduledTask -TaskName "PatientStay"
   ```
2. Check if port 8001 is listening:
   ```powershell
   Get-NetTCPConnection -LocalPort 8001
   ```
3. Check for Python errors:
   ```powershell
   # Run manually to see errors
   cd F:\inetpub-S928-STGSP3WIL2\PatientStayAPI
   .\.venv\Scripts\python.exe app.py
   ```

### AWS Bedrock errors (ExpiredTokenException)
- SSO tokens expire after ~8-12 hours
- For production, use IAM credentials instead of SSO
- Re-run `aws sso login --profile dev1` and recopy .aws folder

### PDF Upload fails
- Check that temp folder is writable: `$env:TEMP`
- Check that pdfplumber is installed: `.\.venv\Scripts\pip.exe show pdfplumber`

### IIS 502/503 errors
- Verify ARR proxy is enabled (IIS Manager → Server → Application Request Routing)
- Check web.config is in the application root
- Verify port 8001 matches in web.config and app.py

---

## Quick Update Procedure

After making changes to the code:

### On Dev Machine:
```powershell
cd C:\Users\ejones08\source\repos\aec-patient-stay-api

# Rebuild zip (excludes examples, output, test files per .gitignore)
Compress-Archive -Path @(
    ".\app.py",
    ".\config.py",
    ".\main.py",
    ".\models.py",
    ".\requirements.txt",
    ".\api",
    ".\llm",
    ".\services",
    ".\static",
    ".\templates"
) -DestinationPath ".\patient-stay-deploy.zip" -Force

# Copy to server
Copy-Item ".\patient-stay-deploy.zip" -Destination "\\S927-WBAPPDEV1\f$\temp\patient-stay-deploy.zip"
```

### On Server:
```powershell
$deployPath = "F:\inetpub-S928-STGSP3WIL2\PatientStayAPI"

# Stop the API
Stop-ScheduledTask -TaskName "PatientStay"

# Extract update (preserve .env and .venv)
Expand-Archive -Path "F:\temp\patient-stay-deploy.zip" -DestinationPath "F:\temp\patient-stay-extract" -Force
Copy-Item -Path "F:\temp\patient-stay-extract\*" -Destination $deployPath -Recurse -Force

# Start the API
Start-ScheduledTask -TaskName "PatientStay"
```

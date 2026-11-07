<#
Deploy (or update) Ava on an Ubuntu server, from Windows.

Works with Oracle Cloud (user "ubuntu", the default) and other Ubuntu servers (-User root).
Usage, from the brief-agent folder:
    powershell -ExecutionPolicy Bypass -File deploy\deploy.ps1 -Server 203.0.113.10
    powershell -ExecutionPolicy Bypass -File deploy\deploy.ps1 -Server 203.0.113.10 -Domain ava.example.com

Without -Domain it uses <ip-with-dashes>.sslip.io, a free name that points to your server's
IP, so HTTPS works without buying a domain. Safe to run again for every update.
#>
param(
    [Parameter(Mandatory = $true)][string]$Server,
    [string]$Domain = "",
    # Oracle Cloud and most Ubuntu images log in as "ubuntu"; use -User root for others
    [string]$User = "ubuntu",
    [string]$Key = "$env:USERPROFILE\.ssh\ava_server"
)
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)
if (-not $Domain) { $Domain = ($Server -replace '\.', '-') + ".sslip.io" }
$work = Join-Path $env:TEMP "ava-deploy"
New-Item -ItemType Directory -Force $work | Out-Null
$sshOpts = @("-i", $Key, "-o", "StrictHostKeyChecking=accept-new")

Write-Host "==> Building server settings from .env.local"
& .venv\Scripts\python.exe deploy\make_server_env.py .env.local "$work\ava.env"
if ($LASTEXITCODE) { throw "settings failed" }

Write-Host "==> Packing app (no secrets, no local files)"
tar -czf "$work\app.tar.gz" --exclude=.venv --exclude=.git --exclude=".env*" --exclude=__pycache__ `
    --exclude=.ruff_cache --exclude=.pytest_cache --exclude="*.egg-info" --exclude=docs .
if ($LASTEXITCODE) { throw "tar failed" }

# setup.sh must have Linux line endings
$sh = (Get-Content deploy\setup.sh -Raw) -replace "`r`n", "`n"
[IO.File]::WriteAllText("$work\setup.sh", $sh)

Write-Host "==> Uploading to $User@$Server"
ssh @sshOpts "${User}@${Server}" "mkdir -p ~/ava-upload && chmod 700 ~/ava-upload"
scp @sshOpts "$work\app.tar.gz" "$work\ava.env" "$work\setup.sh" "${User}@${Server}:ava-upload/"
if ($LASTEXITCODE) { throw "upload failed" }

Write-Host "==> Running setup on the server (first time takes several minutes)"
ssh @sshOpts "${User}@${Server}" "cd ~/ava-upload && sudo bash setup.sh $Domain app.tar.gz ava.env && rm -rf ~/ava-upload"
if ($LASTEXITCODE) { throw "setup failed" }

Remove-Item "$work\ava.env" -Force
Write-Host "`nAva is live at https://$Domain"

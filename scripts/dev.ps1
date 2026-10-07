# One-command development start (Windows PowerShell).
#
#   .\scripts\dev.ps1
#
# Runs database migrations, seeds demo workflows when the database is empty,
# and starts the API with the embedded worker and scheduler. The frontend dev
# server stays separate: `cd frontend; npm run dev`.
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent (Split-Path -Parent $PSScriptRoot))
if (-not $env:ENVIRONMENT) { $env:ENVIRONMENT = "development" }
python -m app.cli dev @args

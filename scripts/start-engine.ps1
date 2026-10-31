[CmdletBinding()]
param([int]$Port = 8000, [string]$PythonExe = '', [string]$DataDir = '')
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$env:LORA1_ENGINE_URL = "http://127.0.0.1:$Port"
if ($PythonExe) { $env:LORA1_PYTHON = $PythonExe }
if ($DataDir) { $env:LORA1_DATA_DIR = [System.IO.Path]::GetFullPath($DataDir) }
Push-Location -LiteralPath $projectRoot
try { node 'scripts/start-engine.mjs' }
finally { Pop-Location }

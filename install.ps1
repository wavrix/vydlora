# One-command bootstrap for Windows 10/11. Safe to rerun; never resets a checkout.
[CmdletBinding()]
param(
    [string]$Directory = (Join-Path $env:LOCALAPPDATA 'Programs\Vydlora'),
    [switch]$NoStart,
    [switch]$NoBrowser,
    [switch]$NoUpdate,
    [switch]$SkipBrowser
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
$InstallDirectory = [IO.Path]::GetFullPath($Directory)
$Repository = 'https://github.com/wavrix/vydlora.git'

function Update-InstallerPath {
    $pathParts = @([Environment]::GetEnvironmentVariable('Path', 'Machine'), [Environment]::GetEnvironmentVariable('Path', 'User'), $env:Path)
    $pathParts += @((Join-Path $env:ProgramFiles 'nodejs'), (Join-Path $env:ProgramFiles 'Git\cmd'), (Join-Path $env:LOCALAPPDATA 'Programs\Git\cmd'), (Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Links'))
    $pathEntries = @($pathParts | Where-Object { $_ } | ForEach-Object { $_ -split ';' } | Where-Object { $_ } | Select-Object -Unique)
    $env:Path = $pathEntries -join ';'
}

function Find-InstallerCommand([string]$Name) {
    $found = Get-Command $Name -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($found) { return $found.Source }
    return $null
}

function Invoke-InstallerCommand([string]$Executable, [string[]]$CommandArguments) {
    & $Executable @CommandArguments
    if ($LASTEXITCODE -ne 0) { throw "Command failed (exit $LASTEXITCODE): $Executable. Fix the error above, then rerun the installer." }
}

function Find-InstallerPython {
    $candidates = @((Join-Path $InstallDirectory 'engine\.venv\Scripts\python.exe'))
    if ($env:LORA1_PYTHON) { $candidates += $env:LORA1_PYTHON }
    foreach ($name in @('python.exe', 'python3.exe')) {
        $candidates += @(Get-Command $name -All -CommandType Application -ErrorAction SilentlyContinue | Where-Object { $_.Source -notmatch '\\WindowsApps\\' } | ForEach-Object { $_.Source })
    }
    foreach ($base in @((Join-Path $env:LOCALAPPDATA 'Programs\Python'), $env:ProgramFiles)) {
        if (Test-Path -LiteralPath $base) { $candidates += @(Get-ChildItem -LiteralPath $base -Directory -Filter 'Python3*' -ErrorAction SilentlyContinue | ForEach-Object { Join-Path $_.FullName 'python.exe' }) }
    }
    $launcher = Find-InstallerCommand 'py.exe'
    if ($launcher) {
        try { $resolved = & $launcher -3 -c 'import sys; print(sys.executable)' 2>$null; if ($LASTEXITCODE -eq 0) { $candidates += $resolved } } catch { }
    }
    foreach ($candidate in @($candidates | Where-Object { $_ } | Select-Object -Unique)) {
        if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) { continue }
        try {
            $resolved = & $candidate -c 'import sys, venv; assert sys.version_info >= (3, 11); print(sys.executable)' 2>$null
            if ($LASTEXITCODE -eq 0) { return ($resolved | Select-Object -Last 1).Trim() }
        } catch { }
    }
    return $null
}

function Install-RequiredPackage([string]$Package, [switch]$ForCurrentUser) {
    $manager = Find-InstallerCommand 'winget.exe'
    if (-not $manager) {
        Write-Host 'Preparing Windows Package Manager...'
        try { Add-AppxPackage -RegisterByFamilyName -MainPackage Microsoft.DesktopAppInstaller_8wekyb3d8bbwe -ErrorAction Stop } catch { }
        Update-InstallerPath
        $manager = Find-InstallerCommand 'winget.exe'
        if (-not $manager) {
            Install-PackageProvider -Name NuGet -Scope CurrentUser -Force | Out-Null
            Install-Module -Name Microsoft.WinGet.Client -Scope CurrentUser -Repository PSGallery -Force | Out-Null
            Import-Module Microsoft.WinGet.Client
            Repair-WinGetPackageManager
            Update-InstallerPath
            $manager = Find-InstallerCommand 'winget.exe'
        }
    }
    if (-not $manager) { throw 'Windows Package Manager could not be installed. Install Microsoft App Installer, then rerun this command.' }
    Write-Host "Installing $Package... Windows may request permission."
    $packageArguments = @('install', '--exact', '--id', $Package, '--source', 'winget', '--silent', '--accept-package-agreements', '--accept-source-agreements')
    if ($ForCurrentUser) { $packageArguments += @('--scope', 'user') }
    Invoke-InstallerCommand $manager $packageArguments
    Update-InstallerPath
}

Write-Host "Vydlora installer`nInstall folder: $InstallDirectory"
Update-InstallerPath
$gitCommand = Find-InstallerCommand 'git.exe'
if (-not $gitCommand) { Install-RequiredPackage 'Git.Git'; $gitCommand = Find-InstallerCommand 'git.exe' }
if (-not $gitCommand) { throw 'Git is installed but unavailable. Open a new terminal and rerun this command.' }

if (Test-Path -LiteralPath (Join-Path $InstallDirectory '.git')) {
    $remote = (& $gitCommand -C $InstallDirectory remote get-url origin).Trim()
    if ($LASTEXITCODE -ne 0 -or $remote -notmatch '^(https://github\.com/wavrix/vydlora(?:\.git)?|git@github\.com:wavrix/vydlora(?:\.git)?)$') { throw 'This folder belongs to another repository. Choose a different -Directory.' }
    if (-not $NoUpdate) {
        $changes = & $gitCommand -C $InstallDirectory status --porcelain
        if ($changes) { throw 'This checkout has source changes. Save/commit them or use -NoUpdate; the installer will not overwrite them.' }
        $branch = (& $gitCommand -C $InstallDirectory branch --show-current).Trim()
        if ($branch -ne 'main') { throw 'This checkout is not on main. Use -NoUpdate to keep your branch.' }
        Invoke-InstallerCommand $gitCommand @('-C', $InstallDirectory, 'pull', '--ff-only', 'origin', 'main')
    }
} else {
    if ((Test-Path -LiteralPath $InstallDirectory) -and @(Get-ChildItem -LiteralPath $InstallDirectory -Force).Count -gt 0) { throw 'The destination is not an empty Vydlora checkout. Choose another -Directory.' }
    $parentDirectory = Split-Path -Parent $InstallDirectory
    New-Item -ItemType Directory -Path $parentDirectory -Force | Out-Null
    Invoke-InstallerCommand $gitCommand @('clone', '--branch', 'main', $Repository, $InstallDirectory)
}

$nodeCommand = Find-InstallerCommand 'node.exe'
$nodeIsSupported = $false
if ($nodeCommand) { $nodeVersion = & $nodeCommand --version; $nodeIsSupported = $nodeVersion -match '^v(\d+)\.' -and [int]$Matches[1] -ge 22 }
if (-not $nodeIsSupported) { Install-RequiredPackage 'OpenJS.NodeJS.LTS'; $nodeCommand = Find-InstallerCommand 'node.exe' }
if (-not $nodeCommand -or -not ((& $nodeCommand --version) -match '^v(\d+)\.') -or [int]$Matches[1] -lt 22) { throw 'Node.js 22+ is unavailable. Open a new terminal and rerun the installer.' }
$pythonCommand = Find-InstallerPython
if (-not $pythonCommand) { Install-RequiredPackage 'Python.Python.3.12' -ForCurrentUser; $pythonCommand = Find-InstallerPython }
if (-not $pythonCommand) { throw 'Python 3.11+ could not be found after installation. Open a new terminal and rerun this command.' }
if (-not (Find-InstallerCommand 'ffmpeg.exe')) { Install-RequiredPackage 'Gyan.FFmpeg' -ForCurrentUser }
if (-not (Find-InstallerCommand 'ffmpeg.exe')) { throw 'FFmpeg is unavailable after installation. Open a new terminal and rerun this command.' }

Push-Location $InstallDirectory
try {
    $setupArguments = @('scripts/setup-engine.mjs', '--python', $pythonCommand)
    if ($SkipBrowser) { $setupArguments += '--skip-browser' }
    Invoke-InstallerCommand $nodeCommand $setupArguments
    $restartCommand = "@echo off`r`ncd /d `"$($InstallDirectory.Replace('%','%%'))`"`r`n`"$($nodeCommand.Replace('%','%%'))`" scripts\launch.mjs %*`r`n"
    [IO.File]::WriteAllText((Join-Path $InstallDirectory '.runtime\start-vydlora.cmd'), $restartCommand)
    Write-Host "Vydlora is installed in $InstallDirectory."
    Write-Host "Start again with: $InstallDirectory\.runtime\start-vydlora.cmd"
    if (-not $NoStart) {
        $launchArguments = @('scripts/launch.mjs')
        if ($NoBrowser) { $launchArguments += '--no-browser' }
        Invoke-InstallerCommand $nodeCommand $launchArguments
    }
} finally { Pop-Location }

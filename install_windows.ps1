# Mediatovideo Converter Windows prerequisite installer and launcher.
# Called by run_windows.bat so every action remains visible.
#
# This script never decides which Python, Tk or FFmpeg versions are acceptable:
# it asks scripts/check_runtime.py (policy from mediatovideo_converter/runtime.py)
# and "run_app.py --check-video-tools" (policy from converter.py) to probe each
# candidate under a watchdog. Its own job is to enumerate candidates and to
# install or update the tooling when no candidate passes.

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$script:StepNumber = 0
$script:PythonExecutable = $null
$script:PythonPrefixArguments = @()
$script:CheckRuntimeScript = Join-Path $PSScriptRoot "scripts\check_runtime.py"
$script:LastProbeOutput = ""

function Write-InstallerHeader {
    Clear-Host
    Write-Host "============================================================" -ForegroundColor Cyan
    Write-Host " Mediatovideo Converter - Windows startup" -ForegroundColor Cyan
    Write-Host "============================================================" -ForegroundColor Cyan
    Write-Host "This window checks and installs the required components."
    Write-Host "It remains open so progress and errors are always visible."
    Write-Host ""
}

function Write-InstallerStep {
    param([Parameter(Mandatory = $true)][string]$Message)
    $script:StepNumber += 1
    Write-Host "[$($script:StepNumber)] $Message" -ForegroundColor Cyan
}

function Write-InstallerSuccess {
    param([Parameter(Mandatory = $true)][string]$Message)
    Write-Host "    OK: $Message" -ForegroundColor Green
}

function Stop-InstallerError {
    param(
        [Parameter(Mandatory = $true)][string]$Stage,
        [Parameter(Mandatory = $true)][string]$Problem,
        [Parameter(Mandatory = $true)][string]$Action,
        [string]$Details = ""
    )
    Write-Host ""
    Write-Host "============================================================" -ForegroundColor Red
    Write-Host " STARTUP ERROR" -ForegroundColor Red
    Write-Host "============================================================" -ForegroundColor Red
    Write-Host "Stage:   $Stage" -ForegroundColor Yellow
    Write-Host "Problem: $Problem" -ForegroundColor White
    if ($Details) {
        Write-Host "Details: $Details" -ForegroundColor DarkGray
    }
    Write-Host "What to do: $Action" -ForegroundColor Yellow
    Write-Host ""
    exit 1
}

function Refresh-ProcessPath {
    $machinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    $extraPaths = @(
        "$env:LOCALAPPDATA\Microsoft\WindowsApps",
        "$env:LOCALAPPDATA\Microsoft\WinGet\Links",
        "$env:ProgramFiles\WinGet\Links"
    )
    $env:Path = (@($machinePath, $userPath) + $extraPaths | Where-Object { $_ }) -join ";"
}

# --- Runtime probing (policy lives in scripts/check_runtime.py) ------------

function Get-CheckerHost {
    # Any working Python can drive the checker; the checker itself probes the
    # real candidates as child processes.
    $candidates = @(
        @{ Executable = "py"; Arguments = @("-3") },
        @{ Executable = "python"; Arguments = @() },
        @{ Executable = "python3"; Arguments = @() },
        @{ Executable = "py"; Arguments = @() }
    )
    foreach ($candidate in $candidates) {
        $command = Get-Command $candidate.Executable -ErrorAction SilentlyContinue
        if (-not $command) {
            continue
        }
        $null = & $command.Source @($candidate.Arguments) -c "import sys" 2>$null
        if ($LASTEXITCODE -eq 0) {
            return @{ Executable = $command.Source; Arguments = $candidate.Arguments }
        }
    }
    return $null
}

function Invoke-RuntimeChecker {
    param([Parameter(Mandatory = $true)][string[]]$CheckerArguments)
    $checkerHost = Get-CheckerHost
    if (-not $checkerHost) {
        return $null
    }
    $output = & $checkerHost.Executable @($checkerHost.Arguments) $script:CheckRuntimeScript @CheckerArguments 2>&1
    return @{
        ExitCode = $LASTEXITCODE
        Output   = (($output | Out-String).Trim())
    }
}

function Get-RuntimeRequirementsText {
    $result = Invoke-RuntimeChecker -CheckerArguments @("--print-requirements")
    if ($result -and $result.Output) {
        return $result.Output
    }
    return "the required Python and Tk versions"
}

function Test-PythonCandidate {
    param(
        [Parameter(Mandatory = $true)][string]$Executable,
        [string[]]$PrefixArguments = @()
    )
    $command = Get-Command $Executable -ErrorAction SilentlyContinue
    if (-not $command) {
        return $false
    }
    $checkerArguments = @("--check-runtime", "--python", $command.Source)
    foreach ($value in $PrefixArguments) {
        $checkerArguments += "--python-arg=$value"
    }
    $result = Invoke-RuntimeChecker -CheckerArguments $checkerArguments
    if (-not $result) {
        return $false
    }
    if ($result.ExitCode -eq 0) {
        $script:PythonExecutable = $command.Source
        $script:PythonPrefixArguments = $PrefixArguments
        return $true
    }
    $script:LastProbeOutput = $result.Output
    return $false
}

function Find-CompatiblePython {
    $candidates = @(
        @{ Executable = "py"; Arguments = @("-3.14") },
        @{ Executable = "python3.14"; Arguments = @() },
        @{ Executable = "python"; Arguments = @() },
        @{ Executable = "python3"; Arguments = @() },
        @{ Executable = "py"; Arguments = @("-3") },
        @{ Executable = "$env:LOCALAPPDATA\Programs\Python\Python314\python.exe"; Arguments = @() },
        @{ Executable = "$env:ProgramFiles\Python314\python.exe"; Arguments = @() }
    )
    foreach ($candidate in $candidates) {
        if (Test-PythonCandidate -Executable $candidate.Executable -PrefixArguments $candidate.Arguments) {
            return $true
        }
    }
    return $false
}

# --- Python installation ---------------------------------------------------

function Require-WinGet {
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        Write-InstallerSuccess "Windows Package Manager (WinGet) is available."
        return
    }
    Stop-InstallerError `
        -Stage "Preparing automatic installation" `
        -Problem "Windows Package Manager (winget) is not available." `
        -Action "Install or update 'App Installer' from Microsoft Store, then run run_windows.bat again." `
        -Details "WinGet is included with supported Windows 10 and Windows 11 installations."
}

function Find-PythonManager {
    foreach ($name in @("pymanager", "py")) {
        $command = Get-Command $name -ErrorAction SilentlyContinue
        if ($command) {
            $null = & $command.Source help install 2>$null
            if ($LASTEXITCODE -eq 0) {
                return $command.Source
            }
        }
    }
    $managerPaths = @(
        "$env:LOCALAPPDATA\Microsoft\WindowsApps\PythonSoftwareFoundation.PythonManager_3847v3x7pw1km\pymanager.exe",
        "$env:LOCALAPPDATA\Microsoft\WindowsApps\PythonSoftwareFoundation.PythonManager_qbz5n2kfra8p0\pymanager.exe"
    )
    foreach ($path in $managerPaths) {
        if (Test-Path $path) {
            $null = & $path help install 2>$null
            if ($LASTEXITCODE -eq 0) {
                return $path
            }
        }
    }
    return $null
}

function Install-WindowsPython {
    $requirements = Get-RuntimeRequirementsText
    Write-InstallerStep "Python and Tk were missing or outdated; installing or updating them ($requirements)."
    Require-WinGet
    & winget install 9NQ7512CXL7T -e --accept-package-agreements --accept-source-agreements --disable-interactivity
    if ($LASTEXITCODE -ne 0) {
        Stop-InstallerError `
            -Stage "Installing Python" `
            -Problem "WinGet could not install the official Python install manager." `
            -Action "Check the internet connection and Microsoft Store access, then run this launcher again." `
            -Details "WinGet exit code: $LASTEXITCODE"
    }
    Refresh-ProcessPath
    $manager = Find-PythonManager
    if (-not $manager) {
        Stop-InstallerError `
            -Stage "Installing Python" `
            -Problem "The Python install manager finished installing but could not be started." `
            -Action "Restart Windows once, then run run_windows.bat again."
    }
    # "install 3.14" resolves to the newest stable 3.14.x and is also the
    # supported way to replace an older or broken 3.14 installation.
    & $manager install 3.14
    if ($LASTEXITCODE -ne 0) {
        Stop-InstallerError `
            -Stage "Installing Python runtime" `
            -Problem "Python 3.14 could not be installed or updated." `
            -Action "Check the internet connection, then run this launcher again." `
            -Details "Python install manager exit code: $LASTEXITCODE"
    }
    Refresh-ProcessPath
    if (Find-CompatiblePython) {
        Write-InstallerSuccess "Python and Tkinter are installed and working."
        return
    }
    if ($script:LastProbeOutput) {
        Write-Host $script:LastProbeOutput -ForegroundColor DarkGray
    }
    Stop-InstallerError `
        -Stage "Verifying Python" `
        -Problem "Python was installed or updated, but it still does not meet the required version and features." `
        -Action "Restart Windows, then run run_windows.bat again. If it persists, repair Python from Windows Installed Apps."
}

# --- FFmpeg (version and capability policy lives in converter.py) ---------

function Find-VideoTools {
    if (-not $script:PythonExecutable) {
        return $false
    }
    $prefix = $script:PythonPrefixArguments
    & $script:PythonExecutable @prefix (Join-Path $PSScriptRoot "run_app.py") --check-video-tools *> $null
    return ($LASTEXITCODE -eq 0)
}

function Add-WinGetFFmpegToPath {
    $searchRoots = @(
        "$env:LOCALAPPDATA\Microsoft\WinGet\Packages",
        "$env:ProgramFiles\WinGet\Packages"
    ) | Where-Object { Test-Path $_ }
    foreach ($root in $searchRoots) {
        $packages = Get-ChildItem -Path $root -Directory -Filter "Gyan.FFmpeg*" -ErrorAction SilentlyContinue
        $ffmpeg = $packages | Get-ChildItem -Filter ffmpeg.exe -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($ffmpeg -and (Test-Path (Join-Path $ffmpeg.DirectoryName "ffprobe.exe"))) {
            $env:Path = "$($ffmpeg.DirectoryName);$env:Path"
            return
        }
    }
}

function Install-WindowsFFmpeg {
    Write-InstallerStep "A compatible FFmpeg/FFprobe pair was not found; installing or updating FFmpeg."
    Require-WinGet
    # An installed-but-old build must be upgraded; a plain install would be a
    # no-op and leave the capability check failing.
    # An unrelated FFmpeg on PATH says nothing about whether WinGet's package
    # is installed. Query the exact package before choosing install or upgrade.
    & winget list --id Gyan.FFmpeg -e --source winget --accept-source-agreements *> $null
    if ($LASTEXITCODE -eq 0) {
        & winget upgrade --id Gyan.FFmpeg -e --source winget --accept-package-agreements --accept-source-agreements --disable-interactivity | Out-Host
    }
    else {
        & winget install --id Gyan.FFmpeg -e --source winget --accept-package-agreements --accept-source-agreements --disable-interactivity | Out-Host
    }
    if ($LASTEXITCODE -ne 0) {
        Stop-InstallerError `
            -Stage "Installing FFmpeg" `
            -Problem "WinGet could not install FFmpeg." `
            -Action "Check the internet connection and available disk space, then run this launcher again." `
            -Details "WinGet exit code: $LASTEXITCODE"
    }
    Refresh-ProcessPath
    Add-WinGetFFmpegToPath
    if (Find-VideoTools) {
        Write-InstallerSuccess "FFmpeg and FFprobe are installed and working."
        return
    }
    $prefix = $script:PythonPrefixArguments
    & $script:PythonExecutable @prefix (Join-Path $PSScriptRoot "run_app.py") --check-video-tools
    Stop-InstallerError `
        -Stage "Verifying FFmpeg" `
        -Problem "FFmpeg was installed or updated, but it still does not meet the required version and features." `
        -Action "Run 'winget upgrade Gyan.FFmpeg' in a terminal, then run run_windows.bat again."
}

# --- Top level ------------------------------------------------------------

function Find-PackagedApp {
    # A frozen build carries its own Python, Tk and video tools, so it is always
    # preferred over an install that would touch the user's machine.
    foreach ($candidate in @(
            (Join-Path $PSScriptRoot "dist\Mediatovideo Converter.exe"),
            (Join-Path $PSScriptRoot "dist\Mediatovideo Converter\Mediatovideo Converter.exe"),
            (Join-Path $PSScriptRoot "Mediatovideo Converter.exe")
        )) {
        if (Test-Path $candidate -PathType Leaf) {
            return $candidate
        }
    }
    return $null
}

function Install-WindowsPrerequisites {
    $requirements = Get-RuntimeRequirementsText
    Write-InstallerStep "Checking Python and Tkinter ($requirements)."
    Refresh-ProcessPath
    if (Find-CompatiblePython) {
        Write-InstallerSuccess "Compatible Python and Tkinter found."
    }
    else {
        Install-WindowsPython
    }
    Write-InstallerStep "Checking FFmpeg and FFprobe."
    Add-WinGetFFmpegToPath
    if (Find-VideoTools) {
        Write-InstallerSuccess "Compatible FFmpeg and FFprobe found."
    }
    else {
        Install-WindowsFFmpeg
    }
    Write-InstallerSuccess "No additional Python packages are required."
}

function Start-MediatovideoConverter {
    $packagedApp = Find-PackagedApp
    if ($packagedApp) {
        Write-InstallerStep "Starting the packaged Mediatovideo Converter build."
        Set-Location $PSScriptRoot
        & $packagedApp
        if ($LASTEXITCODE -ne 0) {
            Stop-InstallerError `
                -Stage "Running Mediatovideo Converter" `
                -Problem "The packaged application stopped unexpectedly." `
                -Action "Read the error shown above. Run this launcher again after correcting it." `
                -Details "Application exit code: $LASTEXITCODE"
        }
        Write-Host ""
        Write-InstallerSuccess "Mediatovideo Converter closed normally."
        return
    }

    Write-InstallerStep "Starting Mediatovideo Converter."
    Set-Location $PSScriptRoot
    $prefixArguments = $script:PythonPrefixArguments
    & $script:PythonExecutable @prefixArguments (Join-Path $PSScriptRoot "run_app.py")
    if ($LASTEXITCODE -ne 0) {
        Stop-InstallerError `
            -Stage "Running Mediatovideo Converter" `
            -Problem "The application stopped unexpectedly." `
            -Action "Read the error shown above. Run this launcher again after correcting it." `
            -Details "Application exit code: $LASTEXITCODE"
    }
    Write-Host ""
    Write-InstallerSuccess "Mediatovideo Converter closed normally."
}

Write-InstallerHeader
if (-not (Find-PackagedApp)) {
    Install-WindowsPrerequisites
}
Start-MediatovideoConverter

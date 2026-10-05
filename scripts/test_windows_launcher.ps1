<# Execute Windows launcher orchestration with mocked installers, without installs. #>
$ErrorActionPreference = 'Stop'
$source = (Get-Content (Join-Path $PSScriptRoot '../install_windows.ps1') -Raw).Replace("`r`n", "`n")
$boundary = $source.LastIndexOf("`nWrite-InstallerHeader`n")
if ($boundary -lt 0) { throw 'Launcher entry point not found.' }
# Load the real functions while retaining the real top-level orchestration for
# the test. Parsing/evaluating the definitions also catches PowerShell errors.
# Dynamic scriptblocks have no file-backed PSScriptRoot. Bind the original
# launcher's directory explicitly while loading its unchanged function logic.
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path.Replace("'", "''")
$definitions = $source.Substring(0, $boundary).Replace('$PSScriptRoot', "'$repoRoot'")
. ([scriptblock]::Create($definitions))
$startup = [scriptblock]::Create($source.Substring($boundary))
function Write-InstallerHeader {}
function Write-InstallerStep {}
function Write-InstallerSuccess {}
function Stop-InstallerError { throw 'Unexpected installer error.' }
function Require-WinGet {}
function Refresh-ProcessPath {}
function Add-WinGetFFmpegToPath {}
function Find-VideoTools { return $true }
function ffmpeg { 'ffmpeg version 7.0.0' } # An unrelated old tool is present.

$script:Prerequisites = 0
$script:Starts = 0
function Find-PackagedApp { return 'packaged.exe' }
function Install-WindowsPrerequisites { $script:Prerequisites++ }
function Start-MediatovideoConverter { $script:Starts++ }
& $startup
if ($script:Prerequisites -ne 0 -or $script:Starts -ne 1) {
    throw 'An adjacent packaged app must start without installing prerequisites.'
}

function winget {
    $script:Operations += $args[0]
    if ($args[0] -eq 'list' -and -not $script:GyanInstalled) {
        $global:LASTEXITCODE = 1
    } else { $global:LASTEXITCODE = 0 }
}
foreach ($installed in @($false, $true)) {
    $script:GyanInstalled = $installed
    $script:Operations = @()
    Install-WindowsFFmpeg
    $expected = if ($installed) { 'upgrade' } else { 'install' }
    if (($script:Operations -join ',') -ne "list,$expected") {
        throw "Expected exact-package list then $expected, got $($script:Operations -join ',')."
    }
}
Write-Host 'Windows launcher: packaged-app bypass and FFmpeg install/upgrade scenarios passed.'

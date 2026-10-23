# open-agent-harness installer for Windows. Run it from a clone of the repository:
#
#   powershell -ExecutionPolicy Bypass -File .\install\install.ps1 [-Yes] [-DryRun] [-SkipTools]
#
# It finds Python 3.11+ (offering uv when it is missing), runs the guided tool setup
# (python -m oah.setup, which asks before each install), installs oah, checks PATH and
# finishes with `oah doctor`. After every install it reloads PATH from the registry, so
# new tools work in this same window. It never changes your PATH settings without asking.
#
# Works in Windows PowerShell 5.1 and PowerShell 7. This file is ASCII only on purpose:
# Windows PowerShell 5.1 misreads UTF-8 files that have no byte order mark.

param(
    [switch]$Yes,
    [switch]$DryRun,
    [switch]$SkipTools,
    [switch]$Help
)

$ErrorActionPreference = 'Continue'
$UvInstallText = 'irm https://astral.sh/uv/install.ps1 | iex'
$UvUrl = 'https://docs.astral.sh/uv/getting-started/installation/'
$PythonToInstall = '3.13'

function Say([string]$Text) {
    [Console]::Out.WriteLine($Text)
}

function Show-Usage {
    Say 'Usage: powershell -ExecutionPolicy Bypass -File .\install\install.ps1 [-Yes] [-DryRun] [-SkipTools]'
    Say ''
    Say 'Installs oah from this clone, after checking the tools it uses.'
    Say '  -Yes        install missing tools without asking about each one'
    Say '  -DryRun     print every step and change nothing'
    Say '  -SkipTools  skip the tool check and only install oah'
    Say '  -Help       show this help'
}

function Step([int]$Number, [string]$Title) {
    Say ''
    Say "== Step $Number/5: $Title"
}

function Fail([string]$Text) {
    Say ''
    Say $Text
    Say 'OAH_INSTALL_FAILED'
    exit 1
}

# Print a command the way a user would type it: arguments with spaces in single quotes.
function Format-Command([string[]]$Argv) {
    $parts = foreach ($part in $Argv) {
        if ($part -match '\s') { "'" + $part + "'" } else { $part }
    }
    return ($parts -join ' ')
}

# Reload PATH from the registry (machine, then user), keeping entries only this window has.
function Update-SessionPath {
    $machine = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $user = [Environment]::GetEnvironmentVariable('Path', 'User')
    $merged = New-Object 'System.Collections.Generic.List[string]'
    foreach ($entry in (@($machine, $user, $env:Path) -join ';').Split(';')) {
        $clean = $entry.Trim()
        if ($clean -and -not ($merged -contains $clean)) { $merged.Add($clean) }
    }
    $env:Path = $merged -join ';'
}

# Run a command (or only print it in a dry run). The exit code goes to $script:StepCode,
# so the command's own output goes straight to the window.
function Invoke-Step([string[]]$Argv) {
    if ($DryRun) {
        Say ('  [dry-run] would run: ' + (Format-Command $Argv))
        $script:StepCode = 0
        return
    }
    Say ('  Running: ' + (Format-Command $Argv))
    $exe = $Argv[0]
    $rest = @($Argv | Select-Object -Skip 1)
    try {
        & $exe @rest
        $script:StepCode = $LASTEXITCODE
    } catch {
        Say ('  Could not start ' + $Argv[0] + ': ' + $_.Exception.Message)
        $script:StepCode = 1
    }
    if ($null -eq $script:StepCode) { $script:StepCode = 0 }
    Update-SessionPath
}

# Run an official PowerShell one-liner in a separate Windows PowerShell, showing its exact text.
function Invoke-OneLiner([string]$Text) {
    if ($DryRun) {
        Say ('  [dry-run] would run: ' + $Text)
        $script:StepCode = 0
        return
    }
    Say ('  Running: ' + $Text)
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -Command $Text
    $script:StepCode = $LASTEXITCODE
    Update-SessionPath
}

# Ask a yes/no question. A dry run only prints it and goes on as if the answer were yes,
# so that every later step is printed too; nothing runs in a dry run anyway.
function Ask([string]$Question) {
    if ($DryRun) {
        Say ('  [dry-run] would ask: ' + $Question + ' [y/N]')
        return $true
    }
    try { $answer = Read-Host ('  ' + $Question + ' [y/N]') } catch { $answer = '' }
    return ($answer -match '^\s*(y|yes)\s*$')
}

# Like Ask, but -Yes answers it.
function Get-Consent([string]$Question) {
    if ($Yes) { return $true }
    return (Ask $Question)
}

function Find-Program([string]$Name) {
    $found = Get-Command $Name -CommandType Application -ErrorAction SilentlyContinue |
        Where-Object { $_.Path -like '*.exe' } | Select-Object -First 1
    if ($found) { return $found.Path }
    return $null
}

function Find-Uv {
    $found = Find-Program 'uv'
    if ($found) { return $found }
    foreach ($candidate in @((Join-Path $HOME '.local\bin\uv.exe'), (Join-Path $HOME '.cargo\bin\uv.exe'))) {
        if (Test-Path -LiteralPath $candidate) { return $candidate }
    }
    return $null
}

# Python code passed to python.exe never contains double quotes (5.1 would mangle them).
$PythonProbe = 'import sys; print(sys.executable if sys.version_info >= (3, 11) else str())'

function Find-Python {
    $env:PYTHONDONTWRITEBYTECODE = '1'
    foreach ($candidate in @(@('py', '-3'), @('python'), @('python3'))) {
        $exe = Find-Program $candidate[0]
        if (-not $exe) { continue }
        $rest = @($candidate | Select-Object -Skip 1)
        try { $out = & $exe @rest -c $PythonProbe 2>$null } catch { continue }
        if ($LASTEXITCODE -ne 0 -or -not $out) { continue }
        $path = ([string]($out | Select-Object -Last 1)).Trim()
        if ($path -and (Test-Path -LiteralPath $path)) { return $path }
    }
    if ($script:Uv) {
        try { $out = & $script:Uv python find '>=3.11' 2>$null } catch { $out = $null }
        if ($LASTEXITCODE -eq 0 -and $out) {
            $path = ([string]($out | Select-Object -Last 1)).Trim()
            if ($path -and (Test-Path -LiteralPath $path)) { return $path }
        }
    }
    return $null
}

function Test-OnPath([string]$Folder) {
    $wanted = $Folder.TrimEnd('\')
    foreach ($entry in $env:Path.Split(';')) {
        if ($entry.Trim().TrimEnd('\') -eq $wanted) { return $true }
    }
    return $false
}

if ($Help) { Show-Usage; exit 0 }
if ($args.Count -gt 0) {
    [Console]::Error.WriteLine("OAH_INSTALL_USAGE: unknown option '" + $args[0] + "'. See the options below.")
    Show-Usage
    exit 2
}

$Repo = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).ProviderPath.TrimEnd('\')
if (-not (Test-Path -LiteralPath (Join-Path $Repo 'pyproject.toml')) -or
        -not (Test-Path -LiteralPath (Join-Path $Repo 'src\oah'))) {
    Fail ("OAH_INSTALL_NOT_A_CLONE: $Repo is not a clone of open-agent-harness. " +
          'Run this script from inside the clone: powershell -ExecutionPolicy Bypass -File .\install\install.ps1')
}

Say 'open-agent-harness installer (Windows)'
if ($DryRun) { Say 'Dry run: every step is printed, and nothing is installed or changed.' }
if ((Test-Path variable:IsWindows) -and -not $IsWindows) {
    Say 'Note: this script is written for Windows. Use install/install.sh on macOS.'
}

$script:Uv = Find-Uv
$Py = $null

Step 1 'Find Python 3.11 or newer'
$Py = Find-Python
if ($Py) {
    Say "  Found: $Py"
} else {
    Say '  Python 3.11 or newer was not found.'
    if (-not $script:Uv) {
        Say '  uv can download Python for you. Its official install command is:'
        Say "    $UvInstallText"
        Say '  It downloads the official script from astral.sh and runs it, and it adds its folder'
        Say "  to your user PATH. More: $UvUrl"
        if (Get-Consent 'Install uv now?') {
            Invoke-OneLiner $UvInstallText
            if ($script:StepCode -ne 0) { Fail 'The uv installer did not finish. Read its messages above, then run this script again.' }
            $script:Uv = Find-Uv
        }
    }
    if ($script:Uv -or $DryRun) {
        if (Get-Consent "Download Python $PythonToInstall with uv?") {
            $uvExe = if ($script:Uv) { $script:Uv } else { 'uv' }
            Invoke-Step @($uvExe, 'python', 'install', $PythonToInstall)
            if ($script:StepCode -ne 0) { Fail 'uv could not download Python. Read its messages above, then run this script again.' }
            $Py = Find-Python
        }
    }
    if (-not $Py -and -not $DryRun) {
        Fail ("oah needs Python 3.11 or newer. Next step: install uv with`r`n    $UvInstallText`r`n" +
              'or install Python from https://www.python.org/downloads/, then run this script again.')
    }
}

Step 2 'Check the tools oah uses'
if ($SkipTools) {
    Say "  Skipped (-SkipTools). Run 'oah doctor' later to see what is missing."
} elseif (-not $Py) {
    Say '  [dry-run] would run: python -m oah.setup --os windows --dry-run (once Python is installed)'
} else {
    $setupArgs = @('-m', 'oah.setup', '--os', 'windows')
    if ($Yes) { $setupArgs += '--yes' }
    if ($DryRun) { $setupArgs += '--dry-run' }
    Say '  The guided setup checks each tool and asks before installing anything.'
    $savedPythonPath = $env:PYTHONPATH
    $env:PYTHONPATH = Join-Path $Repo 'src'
    $env:PYTHONDONTWRITEBYTECODE = '1'
    & $Py @setupArgs
    if ($LASTEXITCODE -ne 0) { Say '  Some tools are not ready yet. The check at the end lists them again.' }
    $env:PYTHONPATH = $savedPythonPath
    Update-SessionPath
    $script:Uv = Find-Uv
}

Step 3 'Install oah'
$Method = ''
$Pipx = Find-Program 'pipx'
if ($script:Uv) {
    $Method = 'uv'
    Invoke-Step @($script:Uv, 'tool', 'install', '--force', $Repo)
    if ($script:StepCode -ne 0) { Fail 'uv could not install oah. Read its messages above, then run this script again.' }
} elseif ($Pipx) {
    $Method = 'pipx'
    Invoke-Step @($Pipx, 'install', '--force', $Repo)
    if ($script:StepCode -ne 0) { Fail 'pipx could not install oah. Read its messages above, then run this script again.' }
} elseif ($Py) {
    $Method = 'pip'
    Say '  uv and pipx were not found, so oah is installed for your user with pip.'
    Invoke-Step @($Py, '-m', 'pip', 'install', '--user', $Repo)
    if ($script:StepCode -ne 0) { Fail 'pip could not install oah. Read its messages above, then run this script again.' }
} else {
    $Method = 'uv'
    Invoke-Step @('uv', 'tool', 'install', '--force', $Repo)
}

Step 4 'Check that oah is on your PATH'
$BinDir = $null
if ($Method -eq 'uv' -and $script:Uv) {
    $BinDir = (& $script:Uv tool dir --bin 2>$null | Select-Object -Last 1)
} elseif ($Method -eq 'pipx' -and -not $DryRun) {
    # Skipped in a dry run: pipx writes a log file on every command.
    $BinDir = (& $Pipx environment --value PIPX_BIN_DIR 2>$null | Select-Object -Last 1)
    if (-not $BinDir) { $BinDir = Join-Path $HOME '.local\bin' }
} elseif ($Method -eq 'pip') {
    $code = 'import sysconfig; print(sysconfig.get_path(''scripts'', sysconfig.get_preferred_scheme(''user'')))'
    $BinDir = (& $Py -c $code 2>$null | Select-Object -Last 1)
}
if ($BinDir) { $BinDir = ([string]$BinDir).Trim() }
if (-not $BinDir) {
    Say '  [dry-run] would check that the folder holding oah is on your PATH.'
} elseif (Test-OnPath $BinDir) {
    Say "  oah is on your PATH ($BinDir)."
} else {
    Say "  oah is installed in $BinDir, which is not on your PATH yet."
    if ($Method -eq 'uv') {
        Say "  'uv tool update-shell' adds that folder to your user PATH setting."
        if ($Yes) {
            Say '  -Yes does not cover that, because it changes your PATH setting. Run it yourself if you want it:'
            Say ('    ' + (Format-Command @($script:Uv, 'tool', 'update-shell')))
        } elseif (Ask "Run 'uv tool update-shell' now?") {
            Invoke-Step @($script:Uv, 'tool', 'update-shell')
        } else {
            Say ('  Skipped. You can run it later: ' + (Format-Command @($script:Uv, 'tool', 'update-shell')))
        }
    } else {
        Say '  To fix that, add the folder to your user PATH, for example with this PowerShell command:'
        Say ("    [Environment]::SetEnvironmentVariable('Path', [Environment]::GetEnvironmentVariable('Path', 'User') + ';" +
             $BinDir + "', 'User')")
    }
    Say '  Then open a new PowerShell window so it takes effect.'
}

Step 5 'Check everything with oah doctor'
$Oah = if ($BinDir) { Join-Path $BinDir 'oah.exe' } else { 'oah' }
if ($DryRun) {
    Invoke-Step @($Oah, 'doctor')
    Say ''
    Say 'Dry run finished: nothing was installed or changed.'
    Say 'OAH_INSTALL_DRY_RUN_OK'
    exit 0
}
if (-not (Test-Path -LiteralPath $Oah)) {
    $Oah = Find-Program 'oah'
    if (-not $Oah) { Fail 'oah was installed, but this script cannot find it. Open a new PowerShell window and run: oah doctor' }
}
Invoke-Step @($Oah, 'doctor')
if ($script:StepCode -eq 0) {
    Say ''
    Say 'oah is installed and every required tool is ready.'
    Say 'Next: open a new PowerShell window, go to your project folder and run: oah init'
    Say 'OAH_INSTALL_OK'
    exit 0
}
Say ''
Say 'oah is installed, but some required tools are not ready (see the list above).'
Say 'Install or sign in to them as shown, then run: oah doctor'
Say 'OAH_INSTALL_INCOMPLETE'
exit 1

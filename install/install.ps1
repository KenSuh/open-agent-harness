# open-agent-harness installer for Windows. Two ways to run it:
#
#   powershell -ExecutionPolicy Bypass -File .\install\install.ps1 [-Yes] [-DryRun] [-SkipTools]   (from a clone)
#   irm https://github.com/KenSuh/open-agent-harness/releases/download/<version>/install.ps1 | iex                (no clone, no git needed)
#   & ([scriptblock]::Create((irm <same URL>))) -DryRun                                            (piped, with options)
#
# Outside a clone it downloads the pinned release archive below, checks its SHA-256 before
# unpacking it, and continues from there. It then finds Python 3.11+ (offering uv when it is
# missing), runs the guided tool setup (python -m oah.setup, which asks before each install),
# installs oah, checks that a new PowerShell window finds it, and finishes with `oah doctor`.
# After every install it reloads PATH from the registry, so new tools work in this same window.
# It never changes your PATH settings without asking.
#
# Works in Windows PowerShell 5.1 and PowerShell 7. This file is ASCII only on purpose:
# Windows PowerShell 5.1 misreads UTF-8 files that have no byte order mark. When piped into
# iex it never calls `exit`, which would close your window.

param(
    [switch]$Yes,
    [switch]$DryRun,
    [switch]$SkipTools,
    [switch]$Help
)

# ---- Release pin: the owner fills in the version and digest when a release is published. -----
$ReleaseVersion = '__OAH_RELEASE_VERSION__'  # the release tag
$ReleaseSha256 = '__OAH_RELEASE_SHA256__'    # SHA-256 of the .zip below, as published with the release
$Repository = 'https://github.com/KenSuh/open-agent-harness'
$ReleaseUrl = "$Repository/releases/download/$ReleaseVersion/open-agent-harness-$ReleaseVersion.zip"
# ------------------------------------------------------------------------------------------------

$OahScriptRoot = $PSScriptRoot  # empty when the script arrives through irm | iex
$OahExtraArgs = @($args | Where-Object { $null -ne $_ })
$script:OahWork = $null  # the temporary download folder; always used as $script:OahWork

$ErrorActionPreference = 'Continue'
$UvInstallText = 'irm https://astral.sh/uv/install.ps1 | iex'
$UvUrl = 'https://docs.astral.sh/uv/getting-started/installation/'
$PythonToInstall = '3.13'
$WindowsPowerShell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'

function Say([string]$Text) {
    [Console]::Out.WriteLine($Text)
}

function Show-Usage {
    Say 'Usage: powershell -ExecutionPolicy Bypass -File .\install\install.ps1 [-Yes] [-DryRun] [-SkipTools]'
    Say '   or: & ([scriptblock]::Create((irm <repository>/releases/download/<version>/install.ps1))) [-Yes] [-DryRun] [-SkipTools]'
    Say ''
    Say 'Installs oah, after checking the tools it uses.'
    Say '  -Yes        install missing tools without asking about each one'
    Say '  -DryRun     print every step and change nothing'
    Say '  -SkipTools  skip the tool check and only install oah'
    Say '  -Help       show this help'
}

function Step([int]$Number, [string]$Title) {
    Say ''
    Say "== Step $Number/5: $Title"
}

# Stop with a message. It throws instead of calling exit, so a piped run keeps the window open.
function Fail([string]$Text) {
    Say ''
    Say $Text
    Say 'OAH_INSTALL_FAILED'
    throw 'OAH_INSTALL_FAILED'
}

# Print a command the way a user would type it: arguments with spaces in single quotes.
function Format-Command([string[]]$Argv) {
    $parts = foreach ($part in $Argv) {
        if ($part -match '\s') { "'" + $part + "'" } else { $part }
    }
    return ($parts -join ' ')
}

# One Windows command line from an argv list, quoted by the rules programs use to split it again.
function Join-Arguments([string[]]$Argv) {
    $parts = foreach ($arg in $Argv) {
        if ($arg.Length -gt 0 -and $arg -notmatch '[\s"]') {
            $arg
        } else {
            $escaped = [regex]::Replace($arg, '(\\*)"', { param($m) ($m.Groups[1].Value * 2) + '\"' })
            $escaped = [regex]::Replace($escaped, '(\\+)$', { param($m) $m.Groups[1].Value * 2 })
            '"' + $escaped + '"'
        }
    }
    return ($parts -join ' ')
}

# Start a program attached to this window (its prompts reach you directly) and return its exit code.
function Start-Native([string]$Exe, [string[]]$Arguments) {
    $info = New-Object System.Diagnostics.ProcessStartInfo
    $info.FileName = $Exe
    $info.Arguments = Join-Arguments $Arguments
    $info.UseShellExecute = $false
    $info.EnvironmentVariables['UV_NO_MODIFY_PATH'] = '1'
    $process = [System.Diagnostics.Process]::Start($info)
    $process.WaitForExit()
    return $process.ExitCode
}

# Reload registry PATH, keeping this window's entries and the discovered uv directory.
function Update-SessionPath {
    $machine = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $user = [Environment]::GetEnvironmentVariable('Path', 'User')
    $merged = New-Object 'System.Collections.Generic.List[string]'
    $uv = Find-Uv
    $uvDir = if ($uv) { [IO.Path]::GetDirectoryName($uv) } else { '' }
    foreach ($entry in (@($machine, $user, $env:Path, $uvDir) -join ';').Split(';')) {
        $clean = $entry.Trim()
        if ($clean -and -not ($merged -contains $clean)) { $merged.Add($clean) }
    }
    $env:Path = $merged -join ';'
}

# Run a command (or only print it in a dry run). The exit code goes to $script:StepCode.
function Invoke-Step([string[]]$Argv) {
    if ($DryRun) {
        Say ('  [dry-run] would run: ' + (Format-Command $Argv))
        $script:StepCode = 0
        return
    }
    Say ('  Running: ' + (Format-Command $Argv))
    try {
        $script:StepCode = Start-Native $Argv[0] @($Argv | Select-Object -Skip 1)
    } catch {
        Say ('  Could not start ' + $Argv[0] + ': ' + $_.Exception.Message)
        $script:StepCode = 1
    }
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
    $script:StepCode = Start-Native $WindowsPowerShell @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', $Text)
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

function Test-Clone([string]$Folder) {
    return ((Test-Path -LiteralPath (Join-Path $Folder 'pyproject.toml')) -and
            (Test-Path -LiteralPath (Join-Path $Folder 'src\oah')) -and
            (Test-Path -LiteralPath (Join-Path $Folder 'install\install.ps1')))
}

# Does a new PowerShell window find oah? A new window takes PATH from the registry.
function Find-OahInNewWindow {
    $machine = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $user = [Environment]::GetEnvironmentVariable('Path', 'User')
    foreach ($entry in (@($machine, $user) -join ';').Split(';')) {
        $clean = $entry.Trim()
        if (-not $clean) { continue }
        $candidate = Join-Path $clean 'oah.exe'
        if (Test-Path -LiteralPath $candidate) { return $candidate }
    }
    return $null
}

# Follow redirects ourselves so an HTTPS release can never redirect to HTTP or a local file.
function Save-Release([Uri]$Uri, [string]$Destination) {
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    for ($redirects = 0; $redirects -le 5; $redirects++) {
        if ($Uri.Scheme -ne 'https') { throw 'Use an HTTPS release URL with HTTPS-only redirects, or clone the repository instead.' }
        $request = [Net.WebRequest]::Create($Uri)
        $request.AllowAutoRedirect = $false
        $response = $request.GetResponse()
        try {
            if ([int]$response.StatusCode -in @(301, 302, 303, 307, 308)) {
                if ($redirects -eq 5 -or -not $response.Headers['Location']) {
                    throw 'The release has too many redirects or no redirect target. Clone the repository instead.'
                }
                $Uri = [Uri]::new($Uri, [string]$response.Headers['Location'])
                continue
            }
            $downloadStream = $response.GetResponseStream()
            try {
                $outputStream = [IO.File]::Create($Destination)
                try { $downloadStream.CopyTo($outputStream) } finally { $outputStream.Dispose() }
            } finally {
                $downloadStream.Dispose()
            }
            return
        } finally {
            $response.Dispose()
        }
    }
}

# Download the pinned release, check its SHA-256, unpack it and return the folder that holds it.
function Get-Release {
    Say ''
    Say '== Get the release (not running from a clone)'
    $url = if ($env:OAH_RELEASE_URL) { $env:OAH_RELEASE_URL } else { $ReleaseUrl }
    $digest = if ($env:OAH_RELEASE_SHA256) { $env:OAH_RELEASE_SHA256 } else { $ReleaseSha256 }
    if (($url + $digest) -like '*__OAH_RELEASE_*') {
        Fail ("OAH_INSTALL_NOT_PUBLISHED: no release has been published yet, so there is nothing to download.`r`n" +
              "Clone the repository instead. It needs git (winget install --id Git.Git -e installs it):`r`n" +
              "    git clone $Repository.git`r`n    cd open-agent-harness`r`n" +
              '    powershell -ExecutionPolicy Bypass -File .\install\install.ps1')
    }
    $releaseUri = $null
    if (-not [Uri]::TryCreate($url, [UriKind]::Absolute, [ref]$releaseUri) -or
        $releaseUri.Scheme -ne 'https' -or -not $releaseUri.Host) {
        Fail 'OAH_INSTALL_HTTPS_REQUIRED: use an HTTPS release URL, or run this installer from a clone.'
    }
    if ($DryRun) {
        Say '  [dry-run] would create a temporary folder for the release.'
        Say "  [dry-run] would download: $url (HTTPS only, including redirects)"
        Say "  [dry-run] would verify SHA-256: $digest (before extraction)"
        Say '  [dry-run] would extract: release.zip into <release-folder>'
        Say '  [dry-run] would remove the temporary folder when finished.'
        return '<release-folder>'
    }
    $script:OahWork = Join-Path ([IO.Path]::GetTempPath()) ('oah-install-' + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $script:OahWork | Out-Null
    Say "  Downloading $url"
    $archive = Join-Path $script:OahWork 'release.zip'
    try {
        Save-Release $releaseUri $archive
    } catch {
        Fail ('The download failed (' + $_.Exception.Message + '). Check your internet connection and run this again.')
    }
    $sha256 = [System.Security.Cryptography.SHA256]::Create()
    try {
        $stream = [IO.File]::OpenRead($archive)
        try {
            $actual = [BitConverter]::ToString($sha256.ComputeHash($stream)).Replace('-', '').ToLowerInvariant()
        } finally {
            $stream.Dispose()
        }
    } finally {
        $sha256.Dispose()
    }
    $wanted = $digest.Trim().ToLowerInvariant()
    if ($actual -ne $wanted) {
        Fail ("OAH_INSTALL_DIGEST_MISMATCH: the download's SHA-256 is $actual, but the release says $wanted.`r`n" +
              'Nothing was unpacked or installed. Download again later, or clone the repository instead.')
    }
    Say "  SHA-256 verified: $actual"
    $unpacked = Join-Path $script:OahWork 'release'
    try {
        Expand-Archive -LiteralPath $archive -DestinationPath $unpacked
    } catch {
        Fail 'The release archive could not be unpacked. Clone the repository instead.'
    }
    $candidates = @($unpacked) + @(Get-ChildItem -LiteralPath $unpacked -Directory | ForEach-Object { $_.FullName })
    foreach ($candidate in $candidates) {
        if (Test-Clone $candidate) {
            Say '  Unpacked to a temporary folder; it is removed when this script ends.'
            return $candidate
        }
    }
    Fail 'The release archive does not contain open-agent-harness. Report this, or clone the repository instead.'
}

function Invoke-OahInstall {
    if ($Help) { Show-Usage; return 0 }
    if ($OahExtraArgs.Count -gt 0) {
        [Console]::Error.WriteLine("OAH_INSTALL_USAGE: unknown option '" + $OahExtraArgs[0] + "'. See the options below.")
        Show-Usage
        return 2
    }

    $Repo = $null
    if ($OahScriptRoot) {
        $candidate = (Resolve-Path -LiteralPath (Join-Path $OahScriptRoot '..')).ProviderPath.TrimEnd('\')
        if (Test-Clone $candidate) { $Repo = $candidate }
    }

    Say 'open-agent-harness installer (Windows)'
    if ($DryRun) { Say 'Dry run: every step is printed, and nothing is installed or changed.' }
    if ((Test-Path variable:IsWindows) -and -not $IsWindows) {
        Say 'Note: this script is written for Windows. Use install/install.sh on macOS.'
    }
    $ReleaseDryRun = $false
    if (-not $Repo) {
        $Repo = Get-Release
        $ReleaseDryRun = [bool]$DryRun
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
            Say '  It downloads the official script from astral.sh and runs it with UV_NO_MODIFY_PATH=1.'
            Say "  To change your user PATH, accept the separate PATH prompt in step 4. More: $UvUrl"
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

    Update-SessionPath
    Step 2 'Check the tools oah uses'
    if ($SkipTools) {
        Say "  Skipped (-SkipTools). Run 'oah doctor' later to see what is missing."
    } elseif ($ReleaseDryRun) {
        Say '  [dry-run] would run: python -m oah.setup --os windows --dry-run (after downloading and extracting the release)'
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
        $setupCode = Start-Native $Py $setupArgs
        if ($setupCode -ne 0) { Say '  Some tools are not ready yet. The check at the end lists them again.' }
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

    Step 4 'Check that a new PowerShell window finds oah'
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
    $found = if ($DryRun) { $null } else { Find-OahInNewWindow }
    if ($DryRun) {
        Say '  [dry-run] would check that a new PowerShell window (PATH from the registry) finds oah.exe.'
    } elseif ($found) {
        Say "  A new PowerShell window finds oah: $found"
    } else {
        $where = if ($BinDir) { $BinDir } else { 'a folder that is not on PATH' }
        Say "  A new PowerShell window does not find oah yet. It is installed in $where."
        if ($Method -eq 'uv') {
            Say "  'uv tool update-shell' adds that folder to your user PATH setting."
            if ($Yes) {
                Say '  -Yes does not cover that, because it changes your PATH setting. Run it yourself if you want it:'
                Say ('    ' + (Format-Command @($script:Uv, 'tool', 'update-shell')))
            } elseif (Ask "Run 'uv tool update-shell' now?") {
                Invoke-Step @($script:Uv, 'tool', 'update-shell')
                $found = Find-OahInNewWindow
                if ($found) { Say "  A new PowerShell window now finds oah: $found" }
            } else {
                Say ('  Skipped. You can run it later: ' + (Format-Command @($script:Uv, 'tool', 'update-shell')))
            }
        } elseif ($BinDir) {
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
        return 0
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
        return 0
    }
    Say ''
    Say 'oah is installed, but some required tools are not ready (see the list above).'
    Say 'Install or sign in to them as shown, run: oah doctor, then in your project folder: oah init'
    Say 'OAH_INSTALL_INCOMPLETE'
    return 1
}

$OahExitCode = 1
try {
    $OahExitCode = [int](@(Invoke-OahInstall) | Select-Object -Last 1)
} catch {
    if ($_.Exception.Message -ne 'OAH_INSTALL_FAILED') {
        Say ''
        Say ('Unexpected error: ' + $_.Exception.Message)
        Say 'OAH_INSTALL_FAILED'
    }
    $OahExitCode = 1
} finally {
    if ($script:OahWork -and (Test-Path -LiteralPath $script:OahWork)) {
        Remove-Item -LiteralPath $script:OahWork -Recurse -Force -ErrorAction SilentlyContinue
    }
}
if ($OahScriptRoot) { exit $OahExitCode }
$global:LASTEXITCODE = $OahExitCode

param(
    [int]$Port = 8897,
    [switch]$NoBrowser,
    [switch]$CheckOnly
)

$ErrorActionPreference = "Stop"
$LocalVenvPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
$WorkspaceRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$WorkspaceVenvPython = Join-Path $WorkspaceRoot ".venv\Scripts\python.exe"
$LauncherScript = Join-Path $PSScriptRoot "launcher.py"

if (Test-Path $LocalVenvPython) {
    $PythonExe = $LocalVenvPython
} elseif (Test-Path $WorkspaceVenvPython) {
    $PythonExe = $WorkspaceVenvPython
} else {
    $SysPy = Get-Command python -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source
    if (-not $SysPy) {
        Write-Error "No Python virtual environment (.venv) or system python found. Run Install.cmd first."
    }
    $PythonExe = $SysPy
}

$ArgsList = @($LauncherScript, "--host", "127.0.0.1", "--port", "$Port")
if ($NoBrowser) {
    $ArgsList += "--no-browser"
}
if ($CheckOnly) {
    $ArgsList += "--check-only"
}

& $PythonExe @ArgsList

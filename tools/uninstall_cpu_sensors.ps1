# Removes what install_cpu_sensors.ps1 installed: the autostart task, LibreHardwareMonitor and the
# PawnIO driver. Needs administrator rights (one UAC prompt). Works offline.
param([string]$Log = (Join-Path $PSScriptRoot 'uninstall.log'))
$ErrorActionPreference = 'Stop'

function Say([string]$message) {
    Add-Content -LiteralPath $Log -Value ('[{0}] {1}' -f (Get-Date -Format 'HH:mm:ss'), $message) -Encoding UTF8
}

try {
    Unregister-ScheduledTask -TaskName 'LibreHardwareMonitor' -Confirm:$false -ErrorAction SilentlyContinue
    Get-Process -Name LibreHardwareMonitor -ErrorAction SilentlyContinue | Stop-Process -Force
    Start-Sleep -Milliseconds 500
    $dest = Join-Path $env:ProgramFiles 'LibreHardwareMonitor'
    if (Test-Path -LiteralPath $dest) { Remove-Item -LiteralPath $dest -Recurse -Force }
    Say 'LibreHardwareMonitor removed'
    $p = Start-Process -FilePath (Join-Path $PSScriptRoot 'PawnIO_setup.exe') -ArgumentList '-uninstall', '-silent' -Wait -PassThru
    Say "PawnIO uninstaller exit code: $($p.ExitCode)"
    Say 'OK'
}
catch {
    Say "ERROR: $($_.Exception.Message)"
    exit 1
}

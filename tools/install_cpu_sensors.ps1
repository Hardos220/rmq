# Installs the CPU temperature source for the resource manager, offline, from the files next to it:
#   1. PawnIO 2.2.0 - a signed driver that lets monitoring tools read CPU sensors;
#   2. LibreHardwareMonitor 0.9.6 into Program Files - CPU sensors only, a password-protected
#      web server on port 8085 (inbound connections from the network are blocked by Windows
#      Firewall), started minimized to the tray at logon with administrator rights.
# Needs administrator rights (one UAC prompt). Remove with uninstall_cpu_sensors.ps1.
param([string]$Log = (Join-Path $PSScriptRoot 'install.log'))
$ErrorActionPreference = 'Stop'

function Say([string]$message) {
    Add-Content -LiteralPath $Log -Value ('[{0}] {1}' -f (Get-Date -Format 'HH:mm:ss'), $message) -Encoding UTF8
}

try {
    Say 'install started'

    # 1. the driver
    $setup = Join-Path $PSScriptRoot 'PawnIO_setup.exe'
    $p = Start-Process -FilePath $setup -ArgumentList '-install', '-silent' -Wait -PassThru
    Say "PawnIO installer exit code: $($p.ExitCode)"
    if ($p.ExitCode -ne 0 -and $p.ExitCode -ne 3010) { throw "PawnIO installer failed with code $($p.ExitCode)" }
    if ($p.ExitCode -eq 3010) { Say 'PawnIO asks for a reboot to finish' }

    # 2. the program (Program Files: only administrators can change what runs elevated at logon)
    $dest = Join-Path $env:ProgramFiles 'LibreHardwareMonitor'
    Get-Process -Name LibreHardwareMonitor -ErrorAction SilentlyContinue | Stop-Process -Force
    Start-Sleep -Milliseconds 500
    New-Item -ItemType Directory -Force -Path $dest | Out-Null
    Expand-Archive -LiteralPath (Join-Path $PSScriptRoot 'LibreHardwareMonitor.zip') -DestinationPath $dest -Force
    Get-ChildItem -LiteralPath $dest -Recurse -File | Unblock-File
    Say "LibreHardwareMonitor extracted to $dest"

    # 3. its settings: CPU sensors only (no GPU polling that would wake the laptop's GPU, no disk or
    #    embedded-controller probing), local web server with a random password
    $bytes = New-Object byte[] 24
    [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    $password = [Convert]::ToBase64String($bytes) -replace '[+/=]', ''
    $settings = [ordered]@{
        startMinMenuItem       = 'true'
        minTrayMenuItem        = 'true'
        minCloseMenuItem       = 'true'
        runWebServerMenuItem   = 'true'
        listenerPort           = '8085'
        authenticationEnabled  = 'true'
        authenticationUserName = 'rmq'
        authenticationPassword = $password
        cpuMenuItem            = 'true'
        mainboardMenuItem      = 'false'
        ramMenuItem            = 'false'
        gpuMenuItem            = 'false'
        hddMenuItem            = 'false'
        nicMenuItem            = 'false'
        fanControllerMenuItem  = 'false'
        psuMenuItem            = 'false'
        batteryMenuItem        = 'false'
        powerMonitorMenuItem   = 'false'
    }
    $xml = "<?xml version=`"1.0`" encoding=`"utf-8`"?>`r`n<configuration>`r`n  <appSettings>`r`n"
    foreach ($key in $settings.Keys) { $xml += "    <add key=`"$key`" value=`"$($settings[$key])`" />`r`n" }
    $xml += "  </appSettings>`r`n</configuration>`r`n"
    [IO.File]::WriteAllText((Join-Path $dest 'LibreHardwareMonitor.config'), $xml, (New-Object Text.UTF8Encoding $false))
    Say 'settings written'

    # 4. start at logon with administrator rights, also on battery, without a time limit
    $user = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    $action = New-ScheduledTaskAction -Execute (Join-Path $dest 'LibreHardwareMonitor.exe') -WorkingDirectory $dest
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
    $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Highest
    $taskSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
        -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
    Register-ScheduledTask -TaskName 'LibreHardwareMonitor' -Action $action -Trigger $trigger -Principal $principal `
        -Settings $taskSettings -Force | Out-Null
    Start-ScheduledTask -TaskName 'LibreHardwareMonitor'
    Say 'autostart task registered and started'
    Say 'OK'
}
catch {
    Say "ERROR: $($_.Exception.Message)"
    exit 1
}

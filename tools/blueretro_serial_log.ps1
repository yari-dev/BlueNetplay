param(
    [string]$Port = "",
    [int]$Baud = 921600
)

Write-Host "Serial devices on this PC:"
$devices = Get-CimInstance Win32_PnPEntity | Where-Object { $_.Name -match '\(COM\d+\)' }
foreach ($d in $devices) { Write-Host ("  {0}   [{1}]  status: {2}" -f $d.Name, $d.PNPDeviceID, $d.Status) }

$noDriver = Get-CimInstance Win32_PnPEntity | Where-Object {
    $_.ConfigManagerErrorCode -ne 0 -and $_.PNPDeviceID -match 'VID_(10C4|1A86|0403|303A)' }
foreach ($d in $noDriver) {
    Write-Host ("  NO DRIVER: {0} [{1}] error code {2}" -f $d.Name, $d.PNPDeviceID, $d.ConfigManagerErrorCode) -ForegroundColor Yellow
}

if (-not $Port) {
    $usb = @($devices | Where-Object { $_.PNPDeviceID -match '^(USB|FTDIBUS)' })
    if ($usb.Count -eq 1) {
        $Port = [regex]::Match($usb[0].Name, 'COM\d+').Value
    } elseif ($usb.Count -eq 0) {
        Write-Host "No USB serial port found. Plug in the ESP32's USB cable and run this again." -ForegroundColor Red
        exit 1
    } else {
        Write-Host "Several USB serial ports found. Run again with -Port COMx." -ForegroundColor Red
        exit 1
    }
}

$logFile = Join-Path $PSScriptRoot ("blueretro_{0}.log" -f (Get-Date -Format "yyyyMMdd_HHmmss"))
$sp = New-Object System.IO.Ports.SerialPort $Port, $Baud, ([System.IO.Ports.Parity]::None), 8, ([System.IO.Ports.StopBits]::One)
$sp.DtrEnable = $false
$sp.RtsEnable = $false
$sp.ReadBufferSize = 1MB
$sp.Encoding = [System.Text.Encoding]::UTF8

try {
    $sp.Open()
} catch {
    Write-Host "Could not open $Port : $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "Close any other program using the port (Arduino IDE, PuTTY, ...) and try again."
    exit 1
}

Write-Host ""
Write-Host "Logging $Port @ $Baud to $logFile" -ForegroundColor Green
Write-Host "Press Ctrl+C to stop." -ForegroundColor Green
Write-Host ""

$writer = New-Object System.IO.StreamWriter($logFile, $true, [System.Text.Encoding]::UTF8)
$pending = ""
try {
    while ($true) {
        $chunk = $sp.ReadExisting()
        if ($chunk) {
            $pending += $chunk
            $parts = $pending -split "`n"
            $pending = $parts[-1]
            foreach ($line in $parts[0..($parts.Count - 2)]) {
                $text = "{0} {1}" -f (Get-Date -Format "HH:mm:ss.fff"), $line.TrimEnd("`r")
                Write-Host $text
                $writer.WriteLine($text)
            }
            $writer.Flush()
        } else {
            Start-Sleep -Milliseconds 10
        }
    }
} finally {
    if ($pending) { $writer.WriteLine("{0} {1}" -f (Get-Date -Format "HH:mm:ss.fff"), $pending) }
    $writer.Close()
    $sp.Close()
    Write-Host ""
    Write-Host "Saved to $logFile" -ForegroundColor Green
}

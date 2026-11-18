# E-Bike Doctor PRO - save the LIVE Stripe keys into C:\EBD_SIGNING\stripe-live\stripe-live.env WITHOUT showing them.
# Run it yourself in a normal PowerShell window (it asks for the values with hidden input; nothing is printed or logged):
#   powershell -ExecutionPolicy Bypass -File tools\stripe_live_keyfile.ps1
# The file is never overwritten (an existing one is renamed to .bak-<time> only with -Replace) and only your user can read it.
param([switch]$Replace)
$ErrorActionPreference = "Stop"
$dir = "C:\EBD_SIGNING\stripe-live"
$file = Join-Path $dir "stripe-live.env"

function Read-Hidden([string]$label) {
  $s = Read-Host -AsSecureString $label
  $b = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($s)
  try { return ([Runtime.InteropServices.Marshal]::PtrToStringBSTR($b)).Trim() } finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($b) }
}

if (Test-Path $file) {
  if (-not $Replace) { Write-Host "A kulcsfajl mar letezik: $file  (csere: -Replace)" -ForegroundColor Yellow; exit 1 }
  Rename-Item $file ($file + ".bak-" + (Get-Date -Format "yyyyMMdd-HHmmss"))
}
New-Item -ItemType Directory -Force $dir | Out-Null

$sk = Read-Hidden "Titkos / korlatozott kulcs (rk_live_... vagy sk_live_...)"
if ($sk -notmatch '^(rk|sk)_live_[A-Za-z0-9]{20,}$') { Write-Host "Nem eles titkos kulcs (rk_live_ / sk_live_). Semmi nem lett mentve." -ForegroundColor Red; exit 1 }
$pk = Read-Hidden "Publikus kulcs (pk_live_...)"
if ($pk -notmatch '^pk_live_[A-Za-z0-9]{20,}$') { Write-Host "Nem eles publikus kulcs (pk_live_). Semmi nem lett mentve." -ForegroundColor Red; exit 1 }
$wh = Read-Hidden "Webhook alairo titok (whsec_...)  [ha meg nincs: Enter, kesobb potolhato -Replace-szel]"
if ($wh -and $wh -notmatch '^whsec_[A-Za-z0-9]{20,}$') { Write-Host "Nem webhook-titok (whsec_). Semmi nem lett mentve." -ForegroundColor Red; exit 1 }

$lines = @("STRIPE_SECRET_KEY=$sk", "STRIPE_PUBLISHABLE_KEY=$pk")
if ($wh) { $lines += "STRIPE_WEBHOOK_SECRET=$wh" }
[IO.File]::WriteAllText($file, (($lines -join "`n") + "`n"), (New-Object Text.UTF8Encoding($false)))
& icacls $file /inheritance:r /grant:r "$($env:USERNAME):(R,W)" | Out-Null      # only the current Windows user
$sk = $null; $pk = $null; $wh = $null
Write-Host "Mentve: $file  (kulcs: $(if ($lines[0] -match 'rk_live_') {'korlatozott'} else {'teljes'}), webhook-titok: $(if ($lines.Count -gt 2) {'van'} else {'MEG NINCS'}))" -ForegroundColor Green
Write-Host "Ertek nem jelent meg es nem kerult naploba. Szolj Claude-nak: 'kulcsfajl kesz'." -ForegroundColor Green

# E-Bike Doctor backend 2.2 – post-deployment verification (Windows PowerShell 5.1, no Python needed).
# Usage:  powershell -ExecutionPolicy Bypass -File backend\tools\verify_deploy.ps1 [-Base https://e-bike-doctor.onrender.com]
# Optional: set $env:EBD_ADMIN_TOKEN beforehand to also read /v1/metrics. Secrets/tokens are NEVER printed.
# Cost: uses a throw-away test install ID and about 3 AI requests (1 stream chat, 1 chat, 1 photo analysis).
param([string]$Base = "https://e-bike-doctor.onrender.com")
$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Drawing
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$results = New-Object System.Collections.Generic.List[object]
function Add-Result($name, $ok, $detail) { $results.Add([pscustomobject]@{ Check = $name; Result = $(if ($ok -eq $true) { "OK" } elseif ($ok -eq $false) { "HIBA" } else { "FIGYELEM" }); Detail = $detail }) }

function Invoke-Json($method, $path, $body, $headers) {
    $req = [System.Net.HttpWebRequest]::Create($Base + $path)
    $req.Method = $method; $req.Timeout = 120000; $req.ReadWriteTimeout = 120000; $req.Accept = "application/json"
    foreach ($k in $headers.Keys) { $req.Headers.Add($k, $headers[$k]) }
    if ($body) { $bytes = [Text.Encoding]::UTF8.GetBytes($body); $req.ContentType = "application/json; charset=utf-8"; $req.ContentLength = $bytes.Length; $s = $req.GetRequestStream(); $s.Write($bytes, 0, $bytes.Length); $s.Close() }
    $sw = [Diagnostics.Stopwatch]::StartNew()
    try { $resp = $req.GetResponse() } catch [System.Net.WebException] { $resp = $_.Exception.Response; if (-not $resp) { throw } }
    $text = (New-Object IO.StreamReader($resp.GetResponseStream(), [Text.Encoding]::UTF8)).ReadToEnd()
    $sw.Stop()
    $out = [pscustomobject]@{ Status = [int]$resp.StatusCode; Body = $text; Ms = $sw.ElapsedMilliseconds; Headers = $resp.Headers }
    $resp.Close(); return $out
}

Write-Host "E-Bike Doctor backend ellenőrzés: $Base" -ForegroundColor Cyan

# 1. health (also wakes a sleeping instance)
$h = Invoke-Json "GET" "/health" $null @{}
$hj = $h.Body | ConvertFrom-Json
Add-Result "health elérhető" ($h.Status -eq 200) "HTTP $($h.Status), $($h.Ms) ms"
Add-Result "verzió 2.2.0" ($hj.version -eq "2.2.0") "version=$($hj.version)"
Add-Result "AI konfigurálva" ($hj.ai_configured -eq $true) "provider=$($hj.provider)"
Add-Result "streaming" ($hj.streaming -eq $true) ""
Add-Result "napi keret = 50" ($hj.daily_limit -eq 50) "daily_limit=$($hj.daily_limit)"
$storeOk = if ("$($hj.store)" -match "upstash") { $true } else { $null }
Add-Result "tartós számláló (Upstash)" $storeOk "store=$($hj.store) – memória esetén a keret újraindításkor nullázódik"
Add-Result "képelemző végpont" (@($hj.endpoints) -contains "/v1/vision-diagnose") ""
$models = if ($hj.models) { ($hj.models | ConvertTo-Json -Compress) } else { "?" }
Add-Result "modellek" $null $models

# 2. register + quota (token never printed)
$iid = "verify-" + [guid]::NewGuid().ToString("N").Substring(0, 20)
$r = Invoke-Json "POST" "/v1/register" ('{"install_id":"' + $iid + '"}') @{}
$token = ($r.Body | ConvertFrom-Json).token
Add-Result "regisztráció (token)" ([bool]$token) "HTTP $($r.Status)"
$auth = @{ "X-EBD-Token" = $token }
$q0 = ((Invoke-Json "GET" "/v1/quota" $null $auth).Body | ConvertFrom-Json).remaining
Add-Result "kvóta lekérdezés" ($q0 -eq 50) "remaining=$q0"

# 3. streaming chat – time to first text
$question = "Ellenőrző kérdés $(Get-Random -Maximum 99999): hogyan ellenőrizzem biztonságosan a fékkapcsolót egy e-biciklin?"
$req = [System.Net.HttpWebRequest]::Create($Base + "/v1/chat/stream")
$req.Method = "POST"; $req.ContentType = "application/json; charset=utf-8"; $req.Accept = "text/event-stream"; $req.Timeout = 120000; $req.ReadWriteTimeout = 120000
$req.Headers.Add("X-EBD-Token", $token)
$payload = [Text.Encoding]::UTF8.GetBytes((@{ language = "hu"; messages = @(@{ role = "user"; content = $question }) } | ConvertTo-Json -Depth 4 -Compress))
$req.ContentLength = $payload.Length; $st = $req.GetRequestStream(); $st.Write($payload, 0, $payload.Length); $st.Close()
$sw = [Diagnostics.Stopwatch]::StartNew(); $first = $null; $deltas = 0; $text = ""; $ev = ""
try {
    $resp = $req.GetResponse(); $rd = New-Object IO.StreamReader($resp.GetResponseStream(), [Text.Encoding]::UTF8)
    while (-not $rd.EndOfStream) {
        $line = $rd.ReadLine()
        if ($line.StartsWith("event:")) { $ev = $line.Substring(6).Trim() }
        elseif ($line.StartsWith("data:") -and $ev -eq "delta") { if ($null -eq $first) { $first = $sw.ElapsedMilliseconds }; $deltas++; $text += (($line.Substring(5).Trim() | ConvertFrom-Json).t) }
    }
    $resp.Close(); $sw.Stop()
    Add-Result "streaming chat" ($deltas -gt 0) "első szöveg: $first ms, teljes: $($sw.ElapsedMilliseconds) ms, $deltas darab, $($text.Length) karakter"
} catch { Add-Result "streaming chat" $false $_.Exception.Message }

# 4. classic chat (2.0-compatible endpoint used by older app versions)
$c = Invoke-Json "POST" "/v1/chat" (@{ language = "de"; messages = @(@{ role = "user"; content = "Prüfkontrolle $(Get-Random): Warum ruckelt ein Nabenmotor beim Anfahren?" }) } | ConvertTo-Json -Depth 4 -Compress) $auth
$cj = $c.Body | ConvertFrom-Json
Add-Result "chat (kompatibilis)" ($c.Status -eq 200 -and $cj.answer.Length -gt 20) "HTTP $($c.Status), $($c.Ms) ms, modell=$($cj.model)"

# 5. photo analysis – synthetic 7-segment-like display image showing "E21"
$bmp = New-Object System.Drawing.Bitmap 900, 500
$g = [System.Drawing.Graphics]::FromImage($bmp); $g.Clear([System.Drawing.Color]::FromArgb(25, 35, 30))
$g.FillRectangle((New-Object System.Drawing.SolidBrush([System.Drawing.Color]::FromArgb(150, 190, 150))), 80, 80, 740, 340)
$font = New-Object System.Drawing.Font("Consolas", 150, [System.Drawing.FontStyle]::Bold)
$g.DrawString("E21", $font, [System.Drawing.Brushes]::Black, 190, 120)
$g.DrawString("ERROR", (New-Object System.Drawing.Font("Arial", 28)), [System.Drawing.Brushes]::Black, 100, 90)
$ms = New-Object IO.MemoryStream; $bmp.Save($ms, [System.Drawing.Imaging.ImageFormat]::Jpeg); $g.Dispose(); $bmp.Dispose()
$b64 = [Convert]::ToBase64String($ms.ToArray())
$v = Invoke-Json "POST" "/v1/vision-diagnose" (@{ language = "en"; mode = "code"; image_base64 = $b64; mime_type = "image/jpeg"; brand = "SW900" } | ConvertTo-Json -Compress) $auth
$vj = $v.Body | ConvertFrom-Json
$codesLine = ($vj.answer -split "`n" | Where-Object { $_ -match "CODES\s*:" } | Select-Object -Last 1)
Add-Result "képelemzés (E21 tesztkép)" ($v.Status -eq 200 -and $codesLine -match "E21") "HTTP $($v.Status), $($v.Ms) ms, $([math]::Round($ms.Length/1KB)) KB, sor: $codesLine"

# 6. quota decreased (cache hits / failures are not counted)
$q1 = ((Invoke-Json "GET" "/v1/quota" $null $auth).Body | ConvertFrom-Json).remaining
Add-Result "kvóta csökkent" ($q1 -lt $q0) "előtte $q0, utána $q1"

# 7. optional metrics
if ($env:EBD_ADMIN_TOKEN) {
    $m = Invoke-Json "GET" "/v1/metrics" $null @{ "X-Admin-Token" = $env:EBD_ADMIN_TOKEN }
    Add-Result "metrikák (admin)" ($m.Status -eq 200) "HTTP $($m.Status)"
} else { Add-Result "metrikák (admin)" $null "kihagyva – EBD_ADMIN_TOKEN nincs beállítva" }

$results | Format-Table -AutoSize -Wrap
$fail = @($results | Where-Object { $_.Result -eq "HIBA" }).Count
if ($fail -eq 0) { Write-Host "Minden kötelező ellenőrzés sikeres." -ForegroundColor Green; exit 0 } else { Write-Host "$fail ellenőrzés sikertelen." -ForegroundColor Red; exit 1 }

# Strata Q2_0+MTP production server (port 18100, OpenAI-compatible /v1/chat/completions)
#   Usage: pwsh -NoProfile -File start-strata-server.ps1 [-Quant q2_0|iq3_s] [-Ctx 8192] [-Cache 7500] [-Vision none|cpu] [-Force]
#   S08-measured launch shape (Q2_0 pack + MTP rt + profile + --expert-cache 7500) plus resident --serve.
#   Stop is automatic: the engine halts at <|endoftext|>/<|im_end|> with DONE ... stop,
#   and serve/server.py maps that to finish_reason=stop (safety net on top).
#   Does nothing if 127.0.0.1:18100 already answers.
param(
    [ValidateSet('q2_0', 'iq3_s')][string]$Quant = 'q2_0',
    [int]$Ctx = 8192,
    [int]$Cache = 7500,
    [ValidateSet('none', 'cpu')][string]$Vision = 'none',
    [ValidateSet('cache', '0121', 'legacy')][string]$Engine = 'cache',   # cache = 0121 + saved-state cache f621c84 (pinned), 0121 = upstream 0.1.21 port (pinned), legacy = 726f8b5
    [switch]$Force
)
$ErrorActionPreference = 'Continue'
$Port  = 18100
$Host_ = '127.0.0.1'
$Base  = "http://${Host_}:$Port"
$Root  = if ($env:HERMESTRATA_ROOT) { $env:HERMESTRATA_ROOT } else { (Resolve-Path "$PSScriptRoot\..").Path }   # project root (set HERMESTRATA_ROOT)
$CfgBase = "$Root\deploy-strata\strata-qwen-$Quant.json"
$CfgRun  = "$Root\deploy-strata\strata-qwen-$Quant.run.json"
$Log   = "$Root\deploy-strata\strata-server.log"
if ($Engine -in 'cache', '0121') {
    if ($Engine -eq 'cache') {
        $SrvRoot = "$Root\strata-prod-cache"            # detached worktree @ cache-fix-1 f621c84 (roots/tips, F1/F2b)
        $Exe     = "$Root\deploy-strata\bin-cache\strata.exe"
    } else {
        $SrvRoot = "$Root\strata-prod"                  # detached worktree @ v100-upstream c6d44e6
        $Exe     = "$Root\deploy-strata\bin-0121\strata.exe"
    }
    # 0.1.21 packs cache slots per blob size: the same N takes ~25% more VRAM; 2800 OOMs the verify graphs at ctx 128K.
    # Max per ctx leaving ~1.9 GB free (ctx sweep 2026-09-29, strata-up/dev-18101/ctx-sweep.jsonl)
    # KV streaming (upstream --kv-resident): 20480 cells per QSA layer in VRAM, the rest in pinned host memory,
    # paged in on demand; frees ~1.4 GB at ctx 128K for experts (2026-09-29 A/B: dev-18101/kvres-*.jsonl)
    $KvResident = if ($Ctx -gt 20480) { 20480 } else { 0 }
    $max = if ($KvResident) { 3127 } elseif ($Ctx -le 16384) { 3235 } else { 3136 }
    if ($Cache -gt $max) { Write-Host "Engine 0121: expert cache $Cache -> $max (VRAM headroom at ctx $Ctx)"; $Cache = $max }
} else {
    $SrvRoot = "$Root\strata"
    $Exe     = "$Root\deploy-strata\bin\strata.exe"
}
$ServerPy = "$SrvRoot\serve\server.py"

function Test-Health {
    try { (Invoke-WebRequest "$Base/health" -TimeoutSec 3 -UseBasicParsing).StatusCode -eq 200 } catch { $false }
}

if (Test-Health) { Write-Host "Server already running: $Base"; exit 0 }

# Warn and quit if an experiment session (Muse/Sol) holds the GPU
$lock = "$Root\locks\gpu.lock\owner"
$others = Get-Process strata,llama-server -ErrorAction SilentlyContinue
if (((Test-Path $lock) -or $others) -and -not $Force) {
    Write-Host "GPU is in use by another session:" -ForegroundColor Yellow
    if (Test-Path $lock) { Write-Host ("  experiment lock: " + (Get-Content $lock -Raw).Trim()) }
    foreach ($p in $others) { Write-Host "  $($p.ProcessName) PID $($p.Id) (started $($p.StartTime))" }
    Write-Host "Wait for it to free up, or re-run with -Force to proceed anyway."
    exit 3
}

$Py = (Get-Command python.exe -ErrorAction SilentlyContinue).Source
if (-not $Py) { Write-Host "python.exe not found." -ForegroundColor Red; exit 2 }

# Clone the base config with this run's --max-context / --expert-cache (--kv int8 above 8K, upstream convention)
$cfg = Get-Content $CfgBase -Raw -Encoding UTF8 | ConvertFrom-Json
$kept = @()
for ($i = 0; $i -lt $cfg.args.Count; $i++) {
    if ($cfg.args[$i] -in '--max-context', '--expert-cache', '--kv') { $i++; continue }
    $kept += $cfg.args[$i]
}
$kept += @('--max-context', "$Ctx", '--expert-cache', "$Cache")
$Kv = 'fp16'
if ($Ctx -gt 8192) { $kept += @('--kv', 'int8'); $Kv = 'int8' }
if (($Engine -in 'cache', '0121') -and $KvResident) { $kept += @('--kv-resident', "$KvResident"); $Kv += "+stream$KvResident" }
if ($Engine -eq 'cache') { $kept += @('--root-dir', "$Root\strata-cache\roots") }   # pinned roots persist across restarts (E3)
if ($Vision -eq 'cpu') {
    # images: strata-vision (llama.cpp mtmd + mmproj) on the CPU, ~300 image tokens per picture; engine takes GENI
    $kept += '--vision'
    $gguf = $kept[[array]::IndexOf($kept, '--native') + 1]
    $cfg | Add-Member -Force vision ([pscustomobject]@{
        exe = "$Root\build-vision\bin\strata-vision.exe"; mmproj = (Join-Path (Split-Path $gguf) 'mmproj-Qwen3.8-Flash-Next-BF16.gguf')
        model = $gguf; gpu = $false; max_tokens = 300; threads = 6 })
}
$cfg.args = $kept
$cfg.exe = $Exe
$cfg.cwd = $SrvRoot
[IO.File]::WriteAllText($CfgRun, ($cfg | ConvertTo-Json -Depth 6), [Text.UTF8Encoding]::new($false))   # no BOM (server.py rejects it; PS 5.1 adds one)

Write-Host "Starting (arena load takes 1-3 min on first start): engine=$Engine quant=$Quant ctx=$Ctx cache=$Cache kv=$Kv vision=$Vision"
$pr = Start-Process -FilePath $Py -ArgumentList @($ServerPy, '--engine', 'strata', '--config', $CfgRun, '--host', $Host_, '--port', "$Port") -WorkingDirectory $SrvRoot -RedirectStandardError $Log -RedirectStandardOutput "$Log.out" -WindowStyle Hidden -PassThru

for ($i = 0; $i -lt 90; $i++) {
    Start-Sleep -Seconds 4
    if (Test-Health) { Write-Host "Ready: $Base/v1  (model: qwen3.8-flash-next)" -ForegroundColor Green; exit 0 }
    if ($pr.HasExited) {
        Write-Host "Server died during startup. Log: $Log" -ForegroundColor Red
        Get-Content $Log -Tail 15
        exit 2
    }
}
Write-Host "Not ready after 6 min. Log: $Log" -ForegroundColor Red
exit 2

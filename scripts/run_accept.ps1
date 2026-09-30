# Unattended GPU acceptance run (DESIGN-HERMES-CACHE §5, cases 1/3/4/5). Stops production 18100 first, restores it at the end.
$ErrorActionPreference = 'Continue'
$R = if ($env:HERMESTRATA_ROOT) { $env:HERMESTRATA_ROOT } else { (Resolve-Path "$PSScriptRoot\..").Path }; $A = "$R\scripts\accept_cache.py"; $O = "$R\logs\accept"; $D = "$R\strata-up\dev-18101"
New-Item -ItemType Directory -Force $O | Out-Null
function Log($m) { $l = "[$(Get-Date -Format HH:mm:ss)] $m"; $l; Add-Content "$O\run.log" $l }

$productionStopped = $false
try {
    Log 'stop production 18100'
    pwsh -NoProfile -File "$R\deploy-strata\stop-strata-server.ps1" | Out-Null
    $productionStopped = $true
    taskkill /IM strata-vision.exe /F 2>$null | Out-Null
    
    Log 'case 1 (root + aux title), cached';            python -X utf8 $A c1 --variant cached *>> "$O\c1.out"
    Log 'case 3 (parent/child/fork), cached';           python -X utf8 $A c3 --variant cached *>> "$O\c3.out"
    
    foreach ($v in 'cold', 'cached') {
      Log "case 4 step3_validate capture $v"
      python -X utf8 $A serve --variant $v *>> "$O\c4.out"
      python -X utf8 "$D\step3_validate.py" capture --base-url http://127.0.0.1:18101 --label $v --out "$O\step3-$v.jsonl" *>> "$O\c4.out"
    }
    python -X utf8 "$D\step3_validate.py" compare --cold "$O\step3-cold.jsonl" --cached "$O\step3-cached.jsonl" --md "$O\step3-compare.md" *>> "$O\c4.out"
    Log "case 4 compare exit=$LASTEXITCODE"
    
    # case 5: decode speed, 3 repeats each: production binary vs new build, spec-adapt off/on
    foreach ($cfg in @(@('prod', ''), @('cached', ''), @('cached', '--spec-adapt'))) {
      $v = $cfg[0]; $x = $cfg[1]; $tag = if ($x) { "$v-adapt" } else { $v }
      Log "case 5 bench $tag"
      $ex = if ($x) { @("--extra=$x") } else { @() }   # "--extra --spec-adapt" would be parsed as its own option
      python -X utf8 $A serve --variant $v @ex *>> "$O\c5.out"
      foreach ($i in 1..3) { python -X utf8 "$D\step3_bench.py" run --base-url http://127.0.0.1:18101 --label "$tag-$i" --out "$O\bench-$tag.jsonl" *>> "$O\c5.out" }
    }
    
}
finally {
    # Always stop the dev acceptance server and restore production after a terminating error / Ctrl+C.
    python -X utf8 $A stop
    if ($productionStopped) {
        Log 'restore production 18100'
        pwsh -NoProfile -File "$R\deploy-strata\start-strata-server.ps1" -Quant iq3_s -Ctx 262144 -Cache 9999 -Vision cpu -Force *>> "$O\restore.out"
    }
}
Log 'done'

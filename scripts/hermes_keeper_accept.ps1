# GPU acceptance for the prefix keeper (briefs/BRIEF-PREFIX-KEEPER.md), run by Claude on production 18100 with real Hermes.
# 1) two new sessions back to back, with the git status changed in between -> 2nd session's first request resumes the
#    ROOT (<= 2 s prefill) and the model still sees the NEW status (asks it to report the untracked count).
# 2) parent -> forked child -> parent: parent's post-fork request never cold (tip/chain), child recalls a planted fact.
# Pass = engine log resume lines + answers; token parity is not a criterion.
$ErrorActionPreference = 'Continue'
$R = if ($env:HERMESTRATA_ROOT) { $env:HERMESTRATA_ROOT } else { (Resolve-Path "$PSScriptRoot\..").Path }; if (-not $env:HERMES_HOME) { throw "set HERMES_HOME (Hermes home dir)" }
$env:HERMES_WORKSPACE_LATE = '1'; $env:HERMES_TITLE_AFTER_TURN = '1'
$EL = "$R\deploy-strata\strata-engine.log"; $O = "$R\logs\accept"; $W = "$R\hermes-work\roguelike"
$out = "$O\keeper-accept.txt"
function Run($tag, $q, $extra) {
  $pos = (Get-Item $EL).Length; $t0 = Get-Date
  & "$env:HERMES_HOME\bin\hermes.exe" -p flashnext chat -q $q --oneshot --yolo --max-turns 15 --in $W @extra *> "$O\keeper-$tag.out"
  $fs = [IO.File]::Open($EL, 'Open', 'Read', 'ReadWrite'); $fs.Seek($pos, 0) | Out-Null; $txt = (New-Object IO.StreamReader($fs)).ReadToEnd(); $fs.Close()
  "=== $tag  $([int]((Get-Date) - $t0).TotalSeconds) s" | Tee-Object -Append $out
  $txt -split "`n" | ? { $_ -match 'resume from|keeper' } | % { $_.Trim().Substring(0, [Math]::Min(170, $_.Trim().Length)) } | Tee-Object -Append $out
  "--- answer:" | Tee-Object -Append $out
  (Get-Content "$O\keeper-$tag.out" -Raw) -replace '(?s)Resume this session.*', '' | % { $_.Trim().Substring([Math]::Max(0, $_.Trim().Length - 400)) } | Tee-Object -Append $out
}
"# keeper acceptance $(Get-Date -Format 'yyyy-MM-dd HH:mm')" | Tee-Object -Append $out
$probe = "$W\_keeper_probe_$(Get-Date -Format HHmmss).txt"
Run 's1' '1+1は？数字だけ答えて。' @()
New-Item -ItemType File $probe | Out-Null                      # changes the workspace git status line
Run 's2' 'システムプロンプトの workspace 情報にある git の untracked の数を、数字だけ答えて。' @()
Remove-Item $probe
Run 'fork' "（合言葉: 赤いカワウソ）delegate_task をちょうど1回だけ、tasks の要素に `"fork`": true を付けて使い、子エージェントに「最初に教わった合言葉を1行で答える」作業をさせて。結果を受け取ったら、子の答えをそのまま1行で報告して終わって。" @()

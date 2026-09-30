$ErrorActionPreference='Continue'
$R = if ($env:HERMESTRATA_ROOT) { $env:HERMESTRATA_ROOT } else { (Resolve-Path "$PSScriptRoot\..").Path }; if (-not $env:HERMES_HOME) { throw "set HERMES_HOME (Hermes home dir)" }
$env:HERMES_WORKSPACE_LATE='1'; $env:HERMES_TITLE_AFTER_TURN='1'
$EL="$R\deploy-strata\strata-engine.log"; $O="$R\logs\accept"
$task="$R\hermes-work\roguelike\engine.py の関数名を列挙し、各関数の役割を1行ずつ書く"
$runs=@(
 @('isolated', "delegate_task をちょうど1回だけ使い、子エージェントに次の作業をさせて: 「$task」。fork は指定しない。結果を受け取ったら3行で要約して終わって。自分ではファイルを読まないこと。"),
 @('fork',     "delegate_task をちょうど1回だけ、tasks の要素に `"fork`": true を付けて使い、子エージェントに次の作業をさせて: 「$task」。結果を受け取ったら3行で要約して終わって。自分ではファイルを読まないこと。")
)
foreach($r in $runs){
  $pos=(Get-Item $EL).Length; $t0=Get-Date
  & "$env:HERMES_HOME\bin\hermes.exe" -p flashnext chat -q $r[1] --oneshot --yolo --max-turns 15 --in $R\hermes-work\roguelike *> "$O\hermes-$($r[0]).out"
  $dt=((Get-Date)-$t0).TotalSeconds
  $fs=[IO.File]::Open($EL,'Open','Read','ReadWrite'); $fs.Seek($pos,0)|Out-Null; $txt=(New-Object IO.StreamReader($fs)).ReadToEnd(); $fs.Close()
  $lines=$txt -split "`n" | ? { $_ -match 'resume from|prompt \d+ tokens' }
  "=== $($r[0]) total $([int]$dt) s" | Tee-Object -Append "$O\hermes-fork-test.txt"
  $lines | % { $_.Trim().Substring(0,[Math]::Min(170,$_.Trim().Length)) } | Tee-Object -Append "$O\hermes-fork-test.txt"
}

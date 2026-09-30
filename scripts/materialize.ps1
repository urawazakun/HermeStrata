# Build the final source trees from the pinned upstream commits + this repo's patch series, for reading and review.
#   pwsh scripts/materialize.ps1 [-Out <dir>] [-StrataRepo <git url or local clone>] [-HermesRepo <...>]
# Result: <Out>\strata (upstream base + patches/strata/*.patch, one commit per patch)
#         <Out>\hermes-agent (base + patches/hermes-agent/*.patch)
# Read the final state with `git -C <Out>\strata diff <base>..HEAD` or just open the files.
param(
    [string]$Out = (Join-Path (Resolve-Path "$PSScriptRoot\..").Path 'materialized'),
    [string]$StrataRepo = 'https://github.com/Niko1221/Strata.git',
    [string]$HermesRepo = 'https://github.com/NousResearch/hermes-agent.git'
)
$ErrorActionPreference = 'Stop'
$Repo = (Resolve-Path "$PSScriptRoot\..").Path
# Pinned bases. Strata: 0.1.30 (30ec18e); the older 0.1.21 series is kept in patches/strata-0.1.21. Hermes: the parent of patches/hermes-agent/0001.
$bases = @{
    strata         = @{ url = $StrataRepo; base = '30ec18ec7094550fcc594fd948220d511d80464e'; patches = "$Repo\patches\strata" }
    'hermes-agent' = @{ url = $HermesRepo; base = 'd5aaaa4a'; patches = "$Repo\patches\hermes-agent" }
}
New-Item -ItemType Directory -Force $Out | Out-Null
foreach ($name in $bases.Keys) {
    $b = $bases[$name]; $dir = Join-Path $Out $name
    if (-not (Test-Path "$dir\.git")) {
        $filter = if ($b.url -match '^(https?|git|ssh)://') { @('--filter=blob:none') } else { @() }   # partial clone only from a remote
        git clone --quiet @filter $b.url $dir
    }
    git -C $dir fetch --quiet origin $b.base 2>$null
    git -C $dir checkout --quiet --detach $b.base
    git -C $dir -c user.name=materialize -c user.email=materialize@localhost am --quiet --committer-date-is-author-date (Get-ChildItem $b.patches -Filter *.patch | Sort-Object Name | ForEach-Object FullName)
    if ($LASTEXITCODE) { git -C $dir am --abort; throw "${name}: a patch did not apply (see above)" }
    "{0}: {1} patches on {2} -> {3}" -f $name, (Get-ChildItem $b.patches -Filter *.patch).Count, $b.base.Substring(0, 8), (git -C $dir rev-parse --short HEAD)
}

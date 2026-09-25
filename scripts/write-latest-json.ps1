param(
  [Parameter(Mandatory = $true)][string]$AssetName,
  [Parameter(Mandatory = $true)][string]$Notes,
  # -Version 缺省读 package.json：曾经必须手传，传错会把旧 .sig 配上新版本号。
  [string]$Version = "",
  [string]$Tag = "",
  [string]$ReleaseAssetName = "",
  [string]$RepoSlug = "dzc-bit/ai-invest-workbench",
  [string]$OutputPath = "release-assets\latest.json"
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot

if (-not $Version) {
  $packageJson = Get-Content -Raw (Join-Path $repoRoot "package.json") | ConvertFrom-Json
  $Version = $packageJson.version
}

if (-not $Tag) {
  $Tag = "v$Version"
}

if (-not $ReleaseAssetName) {
  $ReleaseAssetName = $AssetName
}

$bundleDir = Join-Path $repoRoot "src-tauri\target\release\bundle\nsis"
$signaturePath = Join-Path $bundleDir "$AssetName.sig"
$assetPath = Join-Path $bundleDir $AssetName
if (-not (Test-Path $signaturePath)) {
  throw "signature file not found: $signaturePath"
}
if (-not (Test-Path $assetPath)) {
  throw "installer not found: $assetPath"
}

# 红线（AGENTS.md §12）：签名缺失时不得复用旧 .sig。bundle/nsis 里永久堆着
# 多个历史版本的 exe+sig——校验 .sig 内嵌的 file 名与 -AssetName 一致、
# .sig mtime 不早于 .exe mtime，防止"旧签名配新版本号"静默过检。
# tauri v2 的 .sig 是 base64(minisign 明文) 的单行文件：解码后从
# "file:<name>" 注释行取签名对象名，不能按 JSON 解析（首次 v1.6.0 发布实测
# 踩到——校验逻辑曾按 JSON 读，真实签名上必然 throw）。
$signatureContent = (Get-Content -Raw $signaturePath).Trim()
$signedFile = ""
try {
  $minisignText = [System.Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($signatureContent))
  foreach ($line in ($minisignText -split "`n")) {
    # minisign 明文第三行是 "trusted comment: timestamp:...\tfile:<名字>"，
    # file: 字段在行中间而不是行首。
    $marker = $line.IndexOf("file:")
    if ($marker -ge 0) {
      $signedFile = $line.Substring($marker + 5).Trim()
      break
    }
  }
} catch {
  throw "signature file is not base64-encoded minisign content: $signaturePath"
}
if (-not $signedFile) {
  throw "signature file has no 'file:' comment line: $signaturePath"
}
if ($signedFile -ne $AssetName) {
  throw "signature was generated for '$signedFile' but AssetName is '$AssetName' (stale signature reuse)"
}
$assetTime = (Get-Item $assetPath).LastWriteTimeUtc
$signatureTime = (Get-Item $signaturePath).LastWriteTimeUtc
if ($signatureTime -lt $assetTime) {
  throw "signature file is older than the installer; regenerate the signature for this build"
}

$latest = @{
  version = $Version
  notes = $Notes
  pub_date = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
  platforms = @{
    "windows-x86_64" = @{
      signature = $signatureContent
      url = "https://github.com/$RepoSlug/releases/download/$Tag/$ReleaseAssetName"
    }
  }
}

$latestJson = $latest | ConvertTo-Json -Depth 5
$resolvedOutput = Join-Path $repoRoot $OutputPath
New-Item -ItemType Directory -Force (Split-Path -Parent $resolvedOutput) | Out-Null
[System.IO.File]::WriteAllText($resolvedOutput, $latestJson, [System.Text.UTF8Encoding]::new($false))
Write-Host "latest.json written for $Version (asset $AssetName, signature verified)"

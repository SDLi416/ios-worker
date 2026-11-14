# Shared streaming hash helper; works without the Get-FileHash module.
function Get-WorkerFileSha256([string]$LiteralPath) {
 $stream=[IO.File]::OpenRead($LiteralPath)
 try {
  $sha=[Security.Cryptography.SHA256]::Create()
  try {return ([BitConverter]::ToString($sha.ComputeHash($stream))).Replace('-','').ToLowerInvariant()}
  finally {$sha.Dispose()}
 } finally {$stream.Dispose()}
}

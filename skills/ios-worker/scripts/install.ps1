param([ValidateSet('codex','claude')][string]$Agent='codex',[string]$Destination='',[switch]$Upgrade)
$ErrorActionPreference='Stop'
$stage=$null;$backup=$null
try {
 $source=Split-Path $PSScriptRoot -Parent
 if(-not $Destination){
  $profileDir=[Environment]::GetFolderPath('UserProfile')
  $agentDir=if($Agent -eq 'codex'){'.agents'}else{'.claude'}
  $Destination=Join-Path (Join-Path $profileDir $agentDir) 'skills/ios-worker'
 }
 $Destination=[IO.Path]::GetFullPath($Destination)
 if($Destination -eq $source -or $Destination.StartsWith($source+[IO.Path]::DirectorySeparatorChar,[StringComparison]::OrdinalIgnoreCase)){throw 'Install from a separate extracted package'}
 function CheckTree([string]$path){
  $items=@(Get-Item -LiteralPath $path)+@(Get-ChildItem -LiteralPath $path -Force -Recurse)
  foreach($item in $items){if($item.Attributes -band [IO.FileAttributes]::ReparsePoint){throw 'Skill package and private profiles must not contain symbolic links'}}
 }
 CheckTree $source
 $exists=Test-Path -LiteralPath $Destination
 if($exists){
  if(-not $Upgrade){throw 'Destination exists; use -Upgrade to preserve credentials and back up the previous skill'}
  if((Get-Item -LiteralPath $Destination).Attributes -band [IO.FileAttributes]::ReparsePoint){throw 'Upgrade destination cannot be a symbolic link'}
  $skill=Join-Path $Destination 'SKILL.md'
  if(-not(Test-Path -LiteralPath $skill) -or (Get-Content -LiteralPath $skill -Raw) -notmatch 'name: ios-worker'){throw 'Upgrade destination is not an ios-worker skill'}
  $oldPrivate=Join-Path $Destination 'private'
  if(Test-Path -LiteralPath $oldPrivate){CheckTree $oldPrivate}
 }
 $parent=Split-Path $Destination -Parent
 $stage=Join-Path $parent ('.ios-worker-stage-'+[guid]::NewGuid().ToString('N'))
 $private=Join-Path $stage 'private';$null=[IO.Directory]::CreateDirectory($private)
 if($env:OS -eq 'Windows_NT'){
  $sid=[Security.Principal.WindowsIdentity]::GetCurrent().User.Value
  & icacls $private /inheritance:r /grant:r "*${sid}:(OI)(CI)F" '*S-1-5-18:(OI)(CI)F' /Q |Out-Null
  if($LASTEXITCODE -ne 0){throw 'Could not restrict private profile permissions'}
 }else{& chmod '700' $stage $private}
 foreach($item in Get-ChildItem -LiteralPath $source -Force){
  if($item.Name -eq '__pycache__'){continue}
  if($item.Name -eq 'private'){
   foreach($profile in Get-ChildItem -LiteralPath $item.FullName -Force){Copy-Item -LiteralPath $profile.FullName -Destination $private -Recurse -Force}
  }else{Copy-Item -LiteralPath $item.FullName -Destination $stage -Recurse}
 }
 if($exists -and (Test-Path -LiteralPath $oldPrivate)){
  foreach($profile in Get-ChildItem -LiteralPath $oldPrivate -Force){Copy-Item -LiteralPath $profile.FullName -Destination $private -Recurse -Force}
 }
 if($env:OS -ne 'Windows_NT'){& chmod -R 'go-rwx' $stage;if($LASTEXITCODE -ne 0){throw 'Could not restrict installed permissions'}}
 if($exists){
  $backupRoot=Join-Path (Split-Path $parent -Parent) 'skill-backups';$null=[IO.Directory]::CreateDirectory($backupRoot)
  $backup=Join-Path $backupRoot ('ios-worker-'+(Get-Date -Format 'yyyyMMdd-HHmmss')+'-'+[guid]::NewGuid().ToString('N').Substring(0,6))
  Move-Item -LiteralPath $Destination -Destination $backup
 }
 try{Move-Item -LiteralPath $stage -Destination $Destination;$stage=$null}
 catch{if($backup){Move-Item -LiteralPath $backup -Destination $Destination};throw}
 @{installed=$Destination;version='2.0.0';backup=$backup;private_profiles_preserved=$exists;next='Start a new agent session and ask ios-worker for status. No MCP setup is required.'}|ConvertTo-Json
}catch{@{error=$_.Exception.Message}|ConvertTo-Json -Compress;exit 1}
finally{if($stage -and (Test-Path -LiteralPath $stage)){Remove-Item -LiteralPath $stage -Recurse -Force}}

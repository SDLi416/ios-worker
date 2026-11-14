param([string]$RequestFile='')
$ErrorActionPreference='Stop'
$skillRoot=Split-Path $PSScriptRoot -Parent
try {
 $request=if($RequestFile){Get-Content -LiteralPath $RequestFile -Raw -Encoding UTF8|ConvertFrom-Json}else{[pscustomobject]@{operation='status'}}
 $base=if($RequestFile){Split-Path ([IO.Path]::GetFullPath($RequestFile)) -Parent}else{(Get-Location).Path}
 $config=if($request.operation -in @('cleanup_execute','retention_execute','register_device')){
  if($env:IOS_WORKER_ADMIN_CONFIG){$env:IOS_WORKER_ADMIN_CONFIG}else{Join-Path $skillRoot 'private/admin-connection.json'}
 }else{
  if($env:IOS_WORKER_CONFIG){$env:IOS_WORKER_CONFIG}else{Join-Path $skillRoot 'private/connection.json'}
 }
 if(-not(Test-Path -LiteralPath $config -PathType Leaf)){throw 'Connection profile missing'}
 function Api([string]$Verb,[string]$ApiPath,$Body=$null,[string]$UploadPath='',[string]$OutputPath='') {
  $params=@{Action='Api';Config=$config;Method=$Verb;Path=$ApiPath}
  if($null -ne $Body){$params.Json=ConvertTo-Json -InputObject $Body -Depth 50 -Compress}
  if($UploadPath){$params.Upload=$UploadPath};if($OutputPath){$params.OutputFile=$OutputPath}
  $output=& (Join-Path $PSScriptRoot 'transport.ps1') @params
  return (ConvertFrom-Json -InputObject ($output -join "`n"))
 }
 function Id([string]$value){if($value -notmatch '^[a-z0-9][a-z0-9_-]{0,63}$'){throw 'Invalid identifier'};return $value}
 function LocalPath([string]$value){if([IO.Path]::IsPathRooted($value)){return $value};return (Join-Path $base $value)}
 switch($request.operation){
  'status' {$result=Api 'GET' '/v1/status'}
  'sessions' {$result=Api 'GET' '/v1/sessions'}
  'toolchain' {$result=Api 'GET' '/v1/toolchain'}
  'sync' {
   $source=LocalPath $request.source
   $manifestRaw=& (Join-Path $PSScriptRoot 'snapshot.ps1') -Source $source -ManifestOnly
   $manifest=ConvertFrom-Json -InputObject ($manifestRaw -join "`n")
   if($manifest.error){throw $manifest.error}
   $project=Id $request.project_id
   $plan=Api 'POST' ('/v1/projects/'+$project+'/sync/plan') @{files=@($manifest.files)}
   if($plan.reused){$result=@{project_id=$project;revision=$plan.revision.id;reused=$true;uploaded_files=0;uploaded_bytes=0}}
   else{
    $paths=@{};foreach($file in $manifest.files){$paths[$file.sha256]=Join-Path $source $file.path}
    foreach($file in $plan.missing){$null=Api 'POST' ('/v1/sources/blobs/'+$file.sha256) $null $paths[$file.sha256]}
    $revision=Api 'POST' ('/v1/projects/'+$project+'/sync/commit') @{plan_id=$plan.plan_id}
    $result=@{project_id=$project;revision=$revision.id;reused=$false;uploaded_files=@($plan.missing).Count;uploaded_bytes=$plan.upload_bytes;source_bytes=$plan.total_bytes;manifest_digest=$revision.manifest_digest}
   }
  }
  {$_ -in @('session_open','session_close','session_install','session_launch','session_configure','ui','diagnostics','capture','capture_matrix','attachments','prepare','register_device')} {
   $job=@{};foreach($property in $request.PSObject.Properties){if($property.Name -notin @('operation','wait','wait_seconds')){$job[$property.Name]=$property.Value}}
   if($request.operation.StartsWith('session_')){$job.action='session';$job.operation=$request.operation.Substring(8)}
   else{$job.action=$request.operation;if($job.ContainsKey('command')){$job.operation=$job.command;$job.Remove('command')}}
   $result=Api 'POST' '/v1/jobs' $job
  }
  'projects' {$result=Api 'GET' '/v1/projects'}
  'jobs' {$result=Api 'GET' '/v1/jobs';if(-not $request.full){$result=@($result|Select-Object -Last 20 -Property id,status,request,started_at,finished_at,error)}}
  'simulators' {$result=Api 'GET' '/v1/simulators/inventory'}
  'storage' {$result=Api 'GET' '/v1/storage'}
  'upload' {
   $source=LocalPath $request.zip_path
   if((Get-Item -LiteralPath $source).Length -gt 2000000000){throw 'Source ZIP exceeds 2 GB'}
   $result=Api 'POST' ('/v1/projects/'+(Id $request.project_id)+'/sources') $null $source
  }
  'submit' {
   $job=$request.job
   if($job.action -eq 'test' -and -not $job.session_id){
    $health=Api 'GET' '/v1/health'
    if('test_requires_idle_simulator' -notin $health.capabilities){throw 'Worker needs simulator ownership update before testing'}
    $inventory=Api 'GET' '/v1/simulators/inventory'
    $udid=if($job.simulator_id){$job.simulator_id}else{$health.default_simulator}
    $device=@($inventory.devices|Where-Object {$_.udid -eq $udid})
    if($device.Count -ne 1 -or -not $device[0].worker_managed){throw 'Choose an existing Worker-managed simulator; other devices are read-only'}
    if($device[0].state -ne 'Shutdown' -or $udid -in $inventory.booted_ids){throw 'Simulator is occupied. Choose another idle existing device or wait; do not create a replacement.'}
    $job|Add-Member -NotePropertyName simulator_id -NotePropertyValue $udid -Force
   }
   if($job.action -eq 'simulator' -and $job.operation -in @('create','erase','delete')){throw 'Skill does not create/erase/delete simulators; reuse existing devices'}
   $result=Api 'POST' '/v1/jobs' $job
  }
  {$_ -in @('job','wait')} {$suffix=if($request.full){''}else{'/summary'};$result=Api 'GET' ('/v1/jobs/'+(Id $request.job_id)+$suffix)}
  'log' {$suffix=if($request.full){'/log'}else{'/log-summary'};$result=Api 'GET' ('/v1/jobs/'+(Id $request.job_id)+$suffix)}
  'cancel' {$result=Api 'POST' ('/v1/jobs/'+(Id $request.job_id)+'/cancel') @{}}
  'download' {
   $path='/v1/jobs/'+(Id $request.job_id)
   $record=Api 'GET' $path
   $item=@($record.artifacts|Where-Object {$_.name -ceq $request.name})
   if($item.Count -ne 1){throw 'Artifact not listed by this job'}
   $out=LocalPath $request.output_path
   $parent=Split-Path ([IO.Path]::GetFullPath($out)) -Parent
   if(-not(Test-Path -LiteralPath $parent)){[IO.Directory]::CreateDirectory($parent)|Out-Null}
   $null=Api 'GET' ($path+'/artifacts/'+[uri]::EscapeDataString($request.name)) $null '' $out
   $digest=(Get-FileHash -LiteralPath $out -Algorithm SHA256).Hash.ToLowerInvariant()
   if($digest -ne $item[0].sha256){throw 'Downloaded artifact checksum mismatch; retain it for inspection'}
   $result=@{saved=$out;sha256=$digest;verified=$true}
  }
  'retention_preview' {
   $keep=if($null -ne $request.keep_last){$request.keep_last}else{10}
   $days=if($null -ne $request.older_than_days){$request.older_than_days}else{14}
   $result=Api 'POST' '/v1/sources/retention/plan' @{project_id=(Id $request.project_id);keep_last=$keep;older_than_days=$days}
  }
  'retention_execute' {
   if(-not $request.plan_id -or $request.plan_id -cne $request.confirmation){throw 'confirmation must equal the reviewed plan_id'}
   $result=Api 'POST' '/v1/sources/retention/execute' @{plan_id=$request.plan_id;confirmation=$request.confirmation}
  }
  'cleanup_preview' {
   $days=if($null -ne $request.older_than_days){$request.older_than_days}else{14}
   $result=Api 'POST' '/v1/storage/plan' @{categories=$request.categories;older_than_days=$days}
  }
  'cleanup_execute' {
   if(-not $request.plan_id -or $request.plan_id -cne $request.confirmation){throw 'confirmation must equal the reviewed plan_id'}
   $result=Api 'POST' '/v1/storage/execute' @{plan_id=$request.plan_id;confirmation=$request.confirmation}
  }
  default {throw 'Unknown operation'}
 }
 $shouldWait=if($null -ne $request.wait){[bool]$request.wait}else{$request.operation -ne 'job'}
 if($result.status -in @('queued','running') -and $shouldWait){
  $seconds=if($request.wait_seconds){[Math]::Min(7200,[Math]::Max(1,[int]$request.wait_seconds))}else{900}
  $deadline=[DateTime]::UtcNow.AddSeconds($seconds)
  [Console]::Error.WriteLine('Waiting for job '+$result.id)
  $delay=500
  while($result.status -in @('queued','running') -and [DateTime]::UtcNow -lt $deadline){
   Start-Sleep -Milliseconds $delay;$delay=[Math]::Min(5000,[int]($delay*1.4))
   $result=Api 'GET' ('/v1/jobs/'+(Id $result.id)+'/summary')
  }
  if($result.status -in @('queued','running')){$result|Add-Member -NotePropertyName wait_timed_out -NotePropertyValue $true -Force}
 }
 ConvertTo-Json -InputObject $result -Depth 60
 if($result.status -in @('failed','cancelled','interrupted')){exit 2}
}catch{
 ConvertTo-Json -InputObject @{error=$_.Exception.Message;hint='Check Mac service/LAN route/profile/IP allowlist. Never disable TLS validation.'} -Compress
 exit 1
}

param(
    [ValidateSet('Api','Verify','Smoke')][string]$Action='Verify',
    [string]$Config=(Join-Path $PSScriptRoot 'connection.json'),
    [ValidateSet('GET','POST')][string]$Method='GET',
    [string]$Path='/v1/health',
    [string]$Json='', [string]$JsonFile='', [string]$Upload='', [string]$OutputFile='',
    [switch]$Wait
)
$ErrorActionPreference='Stop'
Add-Type -AssemblyName System.Net.Http
if (-not ('IosWorkerTransport' -as [type])) {
$source=@'
using System;
using System.IO;
using System.Net;
using System.Net.Http;
using System.Net.Security;
using System.Security.Cryptography;
using System.Security.Cryptography.X509Certificates;
public static class IosWorkerTransport {
 public static string Send(string url,string method,string token,string pin,string json,string upload,string output) {
  var handler=new HttpClientHandler(); handler.UseProxy=false; handler.AllowAutoRedirect=false;
  handler.ServerCertificateCustomValidationCallback=(request,cert,chain,errors)=> {
   if(cert==null || (errors & SslPolicyErrors.RemoteCertificateNameMismatch)!=0 ||
      (errors & SslPolicyErrors.RemoteCertificateNotAvailable)!=0) return false;
   var now=DateTime.UtcNow;
   if(now<cert.NotBefore.ToUniversalTime() || now>cert.NotAfter.ToUniversalTime())return false;
   using(var sha=SHA256.Create()) {
    string actual=BitConverter.ToString(sha.ComputeHash(cert.RawData)).Replace("-","");
    return String.Equals(actual,pin,StringComparison.OrdinalIgnoreCase);
   }
  };
  using(var client=new HttpClient(handler)) {
   client.Timeout=TimeSpan.FromMinutes(15);
   using(var request=new HttpRequestMessage(new HttpMethod(method),url)) {
    request.Headers.Authorization=new System.Net.Http.Headers.AuthenticationHeaderValue("Bearer",token);
    request.Headers.UserAgent.ParseAdd("ios-worker-powershell/1.0");
    if(!String.IsNullOrEmpty(upload)) {
     request.Content=new StreamContent(File.OpenRead(upload));
     request.Content.Headers.ContentType=new System.Net.Http.Headers.MediaTypeHeaderValue("application/zip");
    } else if(method=="POST") {
     request.Content=new StringContent(String.IsNullOrEmpty(json)?"{}":json,System.Text.Encoding.UTF8,"application/json");
    }
    using(var response=client.SendAsync(request,HttpCompletionOption.ResponseHeadersRead).GetAwaiter().GetResult()) {
     if(!response.IsSuccessStatusCode)throw new Exception("HTTP "+(int)response.StatusCode+": "+response.Content.ReadAsStringAsync().GetAwaiter().GetResult());
     if(!String.IsNullOrEmpty(output)) {
      using(var file=new FileStream(output,FileMode.CreateNew,FileAccess.Write))
       response.Content.CopyToAsync(file).GetAwaiter().GetResult();
      return "{\"saved\":true}";
     }
     return response.Content.ReadAsStringAsync().GetAwaiter().GetResult();
    }
   }
  }
 }
}
'@
if ($PSVersionTable.PSEdition -eq 'Desktop') {
 Add-Type -TypeDefinition $source -ReferencedAssemblies @('System.Net.Http','System','System.Core','System.Security')
} else { Add-Type -TypeDefinition $source }
}
$connection=Get-Content -LiteralPath $Config -Raw -Encoding UTF8 | ConvertFrom-Json
if (-not $connection.url.StartsWith('https://')) {throw 'HTTPS required'}
function Invoke-Worker([string]$Verb,[string]$ApiPath,[string]$Body='', [string]$UploadPath='', [string]$SavePath='') {
 if(-not $ApiPath.StartsWith('/v1/')) {throw 'Use a /v1/ API path'}
 $raw=[IosWorkerTransport]::Send($connection.url.TrimEnd('/')+$ApiPath,$Verb,$connection.token,$connection.certificate_sha256,$Body,$UploadPath,$SavePath)
 try {return ($raw | ConvertFrom-Json)} catch {return $raw}
}
function Wait-Worker($job) {
 while($job.status -in @('queued','running')) {
  Start-Sleep -Seconds 3
  $job=Invoke-Worker 'GET' ('/v1/jobs/'+$job.id)
 }
 return $job
}
if($Action -eq 'Verify') {
 $result=[ordered]@{
  health=(Invoke-Worker 'GET' '/v1/health')
  connection=(Invoke-Worker 'POST' '/v1/hello' (@{client_id=$env:COMPUTERNAME}|ConvertTo-Json -Compress))
  simulators=(Invoke-Worker 'GET' '/v1/simulators')
 }
} elseif($Action -eq 'Smoke') {
 $revision=Invoke-Worker 'POST' '/v1/projects/worker-smoke/sources' '' (Join-Path $PSScriptRoot 'WorkerSmoke.zip')
 $runs=@()
 foreach($step in @('build','test')) {
  $body=@{action=$step;project_id='worker-smoke';scheme='WorkerSmoke';revision=$revision.id}|ConvertTo-Json -Compress
  $job=Wait-Worker (Invoke-Worker 'POST' '/v1/jobs' $body)
  $runs+=$job
  if($job.status -ne 'succeeded') { $job | ConvertTo-Json -Depth 30;throw ('Job failed: '+$job.id+'; retrieve /v1/jobs/'+$job.id+'/log') }
 }
 $result=$runs
} else {
 if($JsonFile) {$Json=Get-Content -LiteralPath $JsonFile -Raw -Encoding UTF8}
 if($Upload) {$Upload=(Resolve-Path -LiteralPath $Upload).Path}
 if($OutputFile) {$OutputFile=[System.IO.Path]::GetFullPath($OutputFile)}
 $result=Invoke-Worker $Method $Path $Json $Upload $OutputFile
 if($Wait) {$result=Wait-Worker $result}
}
$result | ConvertTo-Json -Depth 50
if($result.status -in @('failed','cancelled','interrupted')) {exit 1}

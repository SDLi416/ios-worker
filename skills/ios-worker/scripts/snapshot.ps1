param([Parameter(Mandatory=$true)][string]$Source,[string]$OutputFile='', [switch]$ManifestOnly)
$ErrorActionPreference='Stop'
try {
 . (Join-Path $PSScriptRoot 'files.ps1')
 $root=(Resolve-Path -LiteralPath $Source).Path.TrimEnd([IO.Path]::DirectorySeparatorChar)
 $out=if($OutputFile){[IO.Path]::GetFullPath($OutputFile)}else{''}
 if(-not $ManifestOnly -and -not $out){throw 'OutputFile required'}
 if($out -and $out.StartsWith($root+[IO.Path]::DirectorySeparatorChar,[StringComparison]::OrdinalIgnoreCase)){throw 'Put the ZIP outside the source repository'}
 function GitOutput([string]$Arguments){
  $info=New-Object Diagnostics.ProcessStartInfo
  $info.FileName=(Get-Command git -ErrorAction Stop).Source;$info.Arguments=$Arguments;$info.WorkingDirectory=$root
  $info.UseShellExecute=$false;$info.RedirectStandardOutput=$true;$info.RedirectStandardError=$true
  $info.StandardOutputEncoding=New-Object Text.UTF8Encoding($false)
  $proc=New-Object Diagnostics.Process;$proc.StartInfo=$info;$null=$proc.Start()
  $stdout=$proc.StandardOutput.ReadToEnd();$stderr=$proc.StandardError.ReadToEnd();$proc.WaitForExit()
  if($proc.ExitCode -ne 0){throw ('Git failed: '+$stderr)}
  $proc.Dispose();return $stdout
 }
 $gitRoot=(GitOutput 'rev-parse --show-toplevel').Trim()
 # Let Git identify the root: path strings can differ through macOS /var
 # aliases or Windows short/long directory names for the same directory.
 if((GitOutput 'rev-parse --show-prefix').Trim()){throw 'Source must be the repository root'}
 $root=[IO.Path]::GetFullPath($gitRoot).TrimEnd([IO.Path]::DirectorySeparatorChar)
 if($out -and $out.StartsWith($root+[IO.Path]::DirectorySeparatorChar,[StringComparison]::OrdinalIgnoreCase)){throw 'Put the ZIP outside the source repository'}
 $modes=@{}
 foreach($entry in (GitOutput 'ls-files --stage -z').Split([char]0)){
  if(-not $entry){continue};$parts=$entry.Split([char]9,2);$meta=$parts[0].Split(' ')
  if($meta[2] -ne '0'){throw 'Resolve merge conflicts before building'}
  if($meta[0] -eq '160000'){throw 'Submodule detected: prepare a complete ZIP explicitly'}
  if($meta[0] -eq '120000'){throw 'Git symlink detected: prepare a reviewed source snapshot explicitly'}
  $modes[$parts[1]]=[Convert]::ToInt32($meta[0],8)
 }
 $names=@((GitOutput 'ls-files --cached --others --exclude-standard -z').Split([char]0)|Where-Object {$_}|Sort-Object -Unique)
 $files=@();[long]$expanded=0
 foreach($name in $names){
  $path=Join-Path $root $name
  if(-not(Test-Path -LiteralPath $path)){continue}
  $item=Get-Item -LiteralPath $path -Force
  if($item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)){throw ('Links/special files unsupported: '+$name)}
  if($name -match '(^|/)[^/]*\.xcarchive(/|$)'){throw ('Archive found among source files: '+$name)}
  if($item.Extension.ToLowerInvariant() -in @('.p12','.pfx','.p8','.key','.pem') -or ($item.Name.StartsWith('.env') -and $item.Name -notin @('.env.example','.env.sample','.env.template'))){throw ('Potential credential file found; prepare a reviewed ZIP explicitly: '+$name)}
  $stream=[IO.File]::OpenRead($path);try{$buffer=New-Object byte[] 100;$count=$stream.Read($buffer,0,100)}finally{$stream.Dispose()}
  if([Text.Encoding]::UTF8.GetString($buffer,0,$count).StartsWith('version https://git-lfs.github.com/spec/v1')){throw ('Unresolved Git LFS pointer: '+$name)}
  $mode=if($modes.ContainsKey($name)){$modes[$name]}else{33188}
  if($env:OS -ne 'Windows_NT'){$mode=32768 -bor [int][IO.File]::GetUnixFileMode($path)}
  $files+=@{name=$name;path=$path;mode=[int]$mode};$expanded+=$item.Length
 }
 if($files.Count -eq 0 -or $files.Count -gt 100000 -or $expanded -gt 8000000000){throw 'Source snapshot is empty or too large'}
 if($ManifestOnly){
  $manifest=@(foreach($file in $files){@{path=$file.name;sha256=(Get-WorkerFileSha256 $file.path);size=(Get-Item -LiteralPath $file.path -Force).Length;executable=[bool]($file.mode -band 73)}})
  ConvertTo-Json -InputObject @{files=$manifest} -Depth 10 -Compress
  return
 }
 Add-Type -AssemblyName System.IO.Compression
 $null=[IO.Directory]::CreateDirectory((Split-Path $out -Parent))
 $output=[IO.File]::Open($out,[IO.FileMode]::CreateNew)
 try{
  $zip=New-Object IO.Compression.ZipArchive($output,[IO.Compression.ZipArchiveMode]::Create,$true)
  try{
   foreach($file in $files){
    $entry=$zip.CreateEntry($file.name,[IO.Compression.CompressionLevel]::Optimal)
    $entry.ExternalAttributes=($file.mode -shl 16)
    $fileInput=[IO.File]::OpenRead($file.path);$dest=$entry.Open()
    try{$fileInput.CopyTo($dest)}finally{$fileInput.Dispose();$dest.Dispose()}
   }
  }finally{$zip.Dispose()}
 }finally{$output.Dispose()}
 @{zip_path=$out;file_count=$files.Count;sha256=(Get-WorkerFileSha256 $out);includes_working_tree_changes=$true}|ConvertTo-Json
}catch{@{error=$_.Exception.Message}|ConvertTo-Json -Compress;exit 1}

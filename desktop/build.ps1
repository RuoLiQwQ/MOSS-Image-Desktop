param([string]$OutputDirectory)
$ErrorActionPreference = 'Stop'
$bundleRoot = Split-Path -Parent $PSScriptRoot
if (-not $OutputDirectory) { $OutputDirectory = Join-Path $bundleRoot 'dist' }
$OutputDirectory = [IO.Path]::GetFullPath($OutputDirectory)
New-Item -ItemType Directory -Force -Path $OutputDirectory | Out-Null
$compiler = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
if (-not (Test-Path -LiteralPath $compiler)) { throw 'Windows .NET Framework C# compiler was not found.' }
Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem
$payload = Join-Path $OutputDirectory 'MossPayload.zip'
$manifest = [ordered]@{}
$files = @()
foreach ($folder in @('app', 'vendor')) {
    $files += Get-ChildItem -LiteralPath (Join-Path $bundleRoot $folder) -Recurse -File |
        Where-Object { $_.FullName -notmatch '[\\/]__pycache__[\\/]' -and $_.Extension -ne '.pyc' }
}
foreach ($name in @('requirements.lock', 'requirements.in', 'model-manifest.json', 'LICENSE', 'THIRD_PARTY_NOTICES.md', 'README.md', 'TEST_REPORT.md', 'UI_REFERENCE.md', 'RELEASE_NOTES.md', '使用说明.txt')) {
    $files += Get-Item -LiteralPath (Join-Path $bundleRoot $name)
}
$fileStream = [IO.File]::Open($payload, [IO.FileMode]::Create)
$archive = New-Object IO.Compression.ZipArchive($fileStream, [IO.Compression.ZipArchiveMode]::Create, $false)
try {
    foreach ($file in ($files | Sort-Object FullName)) {
        $relative = $file.FullName.Substring($bundleRoot.Length + 1).Replace('\', '/')
        $manifest[$relative] = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
        [IO.Compression.ZipFileExtensions]::CreateEntryFromFile($archive, $file.FullName, $relative, [IO.Compression.CompressionLevel]::Optimal) | Out-Null
    }
    $entry = $archive.CreateEntry('BUNDLE_MANIFEST.json')
    $writer = New-Object IO.StreamWriter($entry.Open(), (New-Object Text.UTF8Encoding($false)))
    try { $writer.Write(($manifest | ConvertTo-Json -Depth 4)) } finally { $writer.Dispose() }
} finally { $archive.Dispose(); $fileStream.Dispose() }
$exe = Join-Path $OutputDirectory 'MOSS-Image-Desktop-v0.1.exe'
$compilerArgs = @('/nologo', '/target:winexe', '/platform:x64', '/optimize+', '/codepage:65001',
    '/reference:System.dll', '/reference:System.Core.dll', '/reference:System.Drawing.dll',
    '/reference:System.Windows.Forms.dll', '/reference:System.Web.Extensions.dll',
    '/reference:System.IO.Compression.dll', '/reference:System.IO.Compression.FileSystem.dll',
    ('/win32manifest:' + (Join-Path $PSScriptRoot 'app.manifest')),
    ('/win32icon:' + (Join-Path $PSScriptRoot 'moss.ico')),
    ('/resource:' + $payload + ',MossPayload.zip'), ('/out:' + $exe), (Join-Path $PSScriptRoot 'Desktop.cs'))
& $compiler @compilerArgs
if ($LASTEXITCODE -ne 0) { throw 'Desktop compilation failed.' }
$sha = (Get-FileHash -LiteralPath $exe -Algorithm SHA256).Hash.ToLowerInvariant()
[IO.File]::WriteAllText((Join-Path $OutputDirectory 'DESKTOP-SHA256SUMS.txt'), ($sha + '  ' + [IO.Path]::GetFileName($exe) + [Environment]::NewLine), [Text.Encoding]::ASCII)
Write-Output ('Built: ' + $exe)
Write-Output ('SHA256: ' + $sha)

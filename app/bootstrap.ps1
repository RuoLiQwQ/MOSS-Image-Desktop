param(
    [switch]$PrepareOnly,
    [string]$ConfigPath,
    [switch]$Desktop
)
$ErrorActionPreference = 'Stop'
# A native launcher can inherit PowerShell 7's module path. Prefer the modules
# shipped with this Windows PowerShell process, without changing user settings.
$nativeModulePath = [IO.Path]::Combine($PSHOME, 'Modules')
$env:PSModulePath = $nativeModulePath + [IO.Path]::PathSeparator + $env:PSModulePath
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$OutputEncoding = [Console]::OutputEncoding
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$bundleRoot = Split-Path -Parent $PSScriptRoot
$transcriptStarted = $false

function Assert-NoReparsePath([string]$Path) {
    $cursor = $Path
    while (-not [string]::IsNullOrEmpty($cursor)) {
        if (Test-Path -LiteralPath $cursor) {
            $entry = Get-Item -LiteralPath $cursor -Force
            if (($entry.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw ('保存位置不能经过符号链接或目录联接，请选择普通文件夹：' + $cursor)
            }
        }
        $parent = [IO.Path]::GetDirectoryName($cursor.TrimEnd('\'))
        if ($parent -eq $cursor) { break }
        $cursor = $parent
    }
}

function Get-SafeStoragePath([object]$Value, [string]$Label) {
    if ($Value -isnot [string] -or [string]::IsNullOrWhiteSpace($Value)) {
        throw ($Label + '没有填写，请在软件中重新选择文件夹。')
    }
    $candidate = [string]$Value
    if ($candidate -notmatch '^[A-Za-z]:[\\/]' -or $candidate -match '[\x00-\x1f<>"|?*]' -or $candidate.Substring(2).Contains(':')) {
        throw ($Label + '必须是本机磁盘的完整路径，例如 D:\MOSS-Image\data。')
    }
    $candidate = $candidate.Replace('/', '\').TrimEnd('\')
    if ($candidate.Length -le 3) { throw ($Label + '不能直接选择整个磁盘，请新建一个专用文件夹。') }
    foreach ($segment in $candidate.Substring(3).Split('\')) {
        if ([string]::IsNullOrEmpty($segment) -or $segment -eq '.' -or $segment -eq '..' -or $segment -match '[ .]$' -or $segment -match '^(?i:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?$') {
            throw ($Label + '包含 Windows 不支持的文件夹名称。')
        }
    }
    $full = [IO.Path]::GetFullPath($candidate).TrimEnd('\')
    $driveRoot = [IO.Path]::GetPathRoot($full)
    if ($full.Length -le 3) { throw ($Label + '不能直接选择整个磁盘，请新建一个专用文件夹。') }
    $drive = [IO.DriveInfo]::new($driveRoot)
    if (-not $drive.IsReady -or ($drive.DriveType -ne [IO.DriveType]::Fixed -and $drive.DriveType -ne [IO.DriveType]::Removable)) {
        throw ($Label + '需要可用的本机磁盘；不支持网络盘。')
    }
    $protectedTrees = @($env:windir, $env:ProgramFiles, ${env:ProgramFiles(x86)}, $env:ProgramData)
    foreach ($protected in $protectedTrees) {
        if ([string]::IsNullOrEmpty($protected)) { continue }
        $normalized = [IO.Path]::GetFullPath($protected).TrimEnd('\')
        if ($full.Equals($normalized, [StringComparison]::OrdinalIgnoreCase) -or $full.StartsWith($normalized + '\', [StringComparison]::OrdinalIgnoreCase)) {
            throw ($Label + '不能选择 Windows 或软件的系统目录，请选择专用数据文件夹。')
        }
    }
    if ($env:LOCALAPPDATA) {
        $desktopHome = [IO.Path]::GetFullPath((Join-Path $env:LOCALAPPDATA 'MOSS-Image-Desktop')).TrimEnd('\')
        if ($full.Equals($desktopHome, [StringComparison]::OrdinalIgnoreCase) -or $full.StartsWith($desktopHome + '\', [StringComparison]::OrdinalIgnoreCase) -or $desktopHome.StartsWith($full + '\', [StringComparison]::OrdinalIgnoreCase)) {
            throw ($Label + '不能与软件自身目录相同或互相包含，请选择专用数据文件夹。')
        }
    }
    $protectedRoots = @($env:USERPROFILE, $env:PUBLIC, $env:LOCALAPPDATA, $env:APPDATA)
    if ($env:USERPROFILE) { $protectedRoots += [IO.Path]::GetDirectoryName($env:USERPROFILE) }
    foreach ($protected in $protectedRoots) {
        if ($protected -and $full.Equals([IO.Path]::GetFullPath($protected).TrimEnd('\'), [StringComparison]::OrdinalIgnoreCase)) {
            throw ($Label + '不能直接选择用户或应用数据的总文件夹，请新建专用文件夹。')
        }
    }
    Assert-NoReparsePath $full
    if ((Test-Path -LiteralPath $full) -and -not (Test-Path -LiteralPath $full -PathType Container)) {
        throw ($Label + '已被同名文件占用，请选择另一个文件夹。')
    }
    return $full
}

try {
    if (-not [Environment]::Is64BitOperatingSystem -or $env:PROCESSOR_ARCHITECTURE -eq 'ARM64') {
        throw '本安装包支持 Intel / AMD 64 位 Windows 10/11；不支持 ARM Windows。'
    }
    if ([Environment]::OSVersion.Version.Build -lt 19045) { throw '需要 Windows 10 22H2（19045）或更新的 Windows 11。' }
    Set-Location -LiteralPath $bundleRoot
    $runtime = Join-Path $bundleRoot 'runtime'
    $cache = $runtime
    $logs = Join-Path $bundleRoot 'logs'
    if (-not [string]::IsNullOrWhiteSpace($ConfigPath)) {
        $ConfigPath = [IO.Path]::GetFullPath($ConfigPath)
        if (-not (Test-Path -LiteralPath $ConfigPath -PathType Leaf)) { throw '找不到保存位置设置，请在软件中重新保存后启动。' }
        $settingsText = Get-Content -LiteralPath $ConfigPath -Raw -Encoding UTF8
        if ($settingsText -notmatch '^\s*\{') { throw '保存位置设置格式不正确，请在软件中重新保存设置。' }
        $settings = $settingsText | ConvertFrom-Json
        if ($settings -isnot [System.Management.Automation.PSCustomObject] -or ($settings.schema_version -isnot [int] -and $settings.schema_version -isnot [long]) -or $settings.schema_version -ne 2) { throw '保存位置设置版本不兼容，请在软件中重新保存设置。' }
        if ($settings.source -notin @('modelscope', 'huggingface')) { throw '模型下载来源无效，请在软件中重新选择。' }
        $dataRoot = Get-SafeStoragePath $settings.data_dir '运行环境和缓存位置'
        $modelRoot = Get-SafeStoragePath $settings.model_dir '模型位置'
        $outputRoot = Get-SafeStoragePath $settings.output_dir '作品位置'
        $selectedRoots = @($dataRoot, $modelRoot, $outputRoot)
        for ($i = 0; $i -lt $selectedRoots.Count; $i++) {
            for ($j = $i + 1; $j -lt $selectedRoots.Count; $j++) {
                $left = $selectedRoots[$i]
                $right = $selectedRoots[$j]
                if ($left.Equals($right, [StringComparison]::OrdinalIgnoreCase) -or $left.StartsWith($right + '\', [StringComparison]::OrdinalIgnoreCase) -or $right.StartsWith($left + '\', [StringComparison]::OrdinalIgnoreCase)) {
                    throw '运行缓存、模型和作品目录需要分别选择独立文件夹，不能相同或互相包含。'
                }
            }
        }
        $runtime = Join-Path $dataRoot 'runtime'
        $cache = Join-Path $dataRoot 'cache'
        $logs = Join-Path $dataRoot 'logs'
    }
    # Validate every path we will write before starting any download or transcript.
    $ownedPaths = @($runtime, $cache, $logs,
        (Join-Path $runtime 'uv'), (Join-Path $runtime 'uv.zip'),
        (Join-Path $runtime 'uv\uv-0.13.1.data\scripts\uv.exe'),
        (Join-Path $runtime 'python'), (Join-Path $runtime 'bin'),
        (Join-Path $runtime 'control\Scripts\python.exe'),
        (Join-Path $cache 'uv-cache'), (Join-Path $cache 'temp'),
        (Join-Path $logs 'bootstrap.log'))
    foreach ($ownedPath in $ownedPaths) { Assert-NoReparsePath $ownedPath }
    $bootstrapDrive = [IO.DriveInfo]::new([IO.Path]::GetPathRoot($runtime))
    if ($bootstrapDrive.AvailableFreeSpace -lt 1GB) {
        throw ('运行缓存所在磁盘 ' + $bootstrapDrive.Name + ' 的可用空间不足 1 GiB，请在软件中选择空间更充足的位置。')
    }
    New-Item -ItemType Directory -Force -Path $runtime,$logs,(Join-Path $cache 'temp') | Out-Null
    Start-Transcript -Path (Join-Path $logs 'bootstrap.log') -Append | Out-Null
    $transcriptStarted = $true
    Write-Host 'MOSS-Image 中文桌面版 · v0.1 正式版' -ForegroundColor Cyan
    Write-Host '正在准备启动器。首次需要联网下载，请稍候。'
    $env:TEMP = Join-Path $cache 'temp'
    $env:TMP = $env:TEMP
    $env:UV_CACHE_DIR = Join-Path $cache 'uv-cache'
    $env:UV_PYTHON_INSTALL_DIR = Join-Path $runtime 'python'
    $env:UV_PYTHON_BIN_DIR = Join-Path $runtime 'bin'
    $env:UV_NO_CONFIG = '1'
    $env:UV_PYTHON_PREFERENCE = 'only-managed'
    $env:UV_LINK_MODE = 'copy'
    $env:UV_HTTP_TIMEOUT = '60'
    $env:UV_HTTP_RETRIES = '3'
    $env:PYTHONUTF8 = '1'
    $env:PYTHONIOENCODING = 'utf-8'
    $env:PYTHONUNBUFFERED = '1'
    $uvExe = Join-Path $runtime 'uv\uv-0.13.1.data\scripts\uv.exe'
    $uvExeHash = '4075a09490327d2bedfacc807fa48252fb93647c5bd7ef28f00e579160ea3fb5'
    $uvValid = (Test-Path -LiteralPath $uvExe) -and ((Get-FileHash -LiteralPath $uvExe -Algorithm SHA256).Hash.ToLowerInvariant() -eq $uvExeHash)
    if (-not $uvValid) {
        $uvArchive = Join-Path $runtime 'uv.zip'
        $uvUrl = 'https://files.pythonhosted.org/packages/2a/8e/d23278bb327d2a75dc1d033c81b7fe23bbb28e363e02e7f306abd047924a/uv-0.13.1-py3-none-win_amd64.whl'
        $expected = '4f693cd4feb52f08f30474c4612f24be2050472609be65b9960b635f286da284'
        Write-Host '正在下载环境管理工具（uv 0.13.1）…'
        Invoke-WebRequest -UseBasicParsing -Uri $uvUrl -OutFile $uvArchive -TimeoutSec 180
        if ((Get-FileHash -LiteralPath $uvArchive -Algorithm SHA256).Hash.ToLowerInvariant() -ne $expected) {
            throw '启动工具校验失败，请重新运行启动文件。'
        }
        Expand-Archive -LiteralPath $uvArchive -DestinationPath (Join-Path $runtime 'uv') -Force
    }
    if ((Get-FileHash -LiteralPath $uvExe -Algorithm SHA256).Hash.ToLowerInvariant() -ne $uvExeHash) {
        throw '环境管理工具校验失败，请重新解压安装包后再试。'
    }
    $controlPython = Join-Path $runtime 'control\Scripts\python.exe'
    Write-Host '正在准备专用 Python；不会替换电脑已有的 Python…'
    # Do not overwrite an interpreter already used by an open launcher.
    # A move invalidates the old base path and triggers a non-destructive refresh.
    $controlHealthy = $false
    if (Test-Path -LiteralPath $controlPython) {
        try {
            & $controlPython -I -c "import sys; from pathlib import Path; ok = sys.version_info[:3] == (3,12,12) and Path(sys.base_prefix).resolve().is_relative_to(Path(sys.argv[1]).resolve()); sys.exit(0 if ok else 1)" (Join-Path $runtime 'python') 2>$null
            $controlHealthy = $LASTEXITCODE -eq 0
        } catch { $controlHealthy = $false }
    }
    if (-not $controlHealthy) {
        & $uvExe venv --allow-existing --relocatable --python 3.12.12 (Join-Path $runtime 'control')
        if ($LASTEXITCODE -ne 0) { throw 'Python 准备失败。请检查到 GitHub 的网络连接，再双击重试。' }
    }
    if ($PrepareOnly) { Write-Host '启动环境已准备完成。'; exit 0 }
    $launcherArguments = @((Join-Path $PSScriptRoot 'launcher.py'))
    if (-not [string]::IsNullOrWhiteSpace($ConfigPath)) { $launcherArguments += @('--config', $ConfigPath) }
    if ($Desktop) { $launcherArguments += '--desktop' }
    [Console]::Out.Flush()
    & $controlPython -u @launcherArguments
    if ($LASTEXITCODE -ne 0) { throw ('启动器异常退出。详细记录位于：' + $logs) }
} catch {
    Write-Host ('无法继续：' + $_.Exception.Message) -ForegroundColor Red
    Write-Host '请检查选择的位置和可用空间，再重新启动。'
    Write-Host '无需管理员运行。不要在压缩包预览窗口内启动。'
    exit 1
} finally {
    if ($transcriptStarted) { try { Stop-Transcript | Out-Null } catch {} }
}

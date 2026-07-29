[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$InstallPrefix,

    [Parameter(Mandatory = $true)]
    [string]$EvidenceDir
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$InstallPrefix = [IO.Path]::GetFullPath($InstallPrefix)
$EvidenceDir = [IO.Path]::GetFullPath($EvidenceDir)
if ((Test-Path -LiteralPath $InstallPrefix) -or (Test-Path -LiteralPath $EvidenceDir)) {
    throw "Python build output already exists"
}

$Repository = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\.."))
$HashFile = Join-Path $Repository "validation\cross_platform\cpython-3.12.13.sha256"
$HashFields = (Get-Content -LiteralPath $HashFile -Raw).Trim() -split "\s+"
$ExpectedHash = $HashFields[0]
if ($ExpectedHash -notmatch "^[0-9a-f]{64}$") {
    throw "invalid pinned CPython source SHA-256"
}

$SourceUrl = "https://www.python.org/ftp/python/3.12.13/Python-3.12.13.tar.xz"
$WorkRoot = Join-Path ([IO.Path]::GetTempPath()) ("continuum-cpython-" + [guid]::NewGuid().ToString("N"))
$SourceArchive = Join-Path $WorkRoot "Python-3.12.13.tar.xz"
$SourceRoot = Join-Path $WorkRoot "Python-3.12.13"
$BuildLog = Join-Path $EvidenceDir "python-build.log"
$LayoutLog = Join-Path $EvidenceDir "python-layout.log"
$VsWhere = Join-Path ${env:ProgramFiles(x86)} "Microsoft Visual Studio\Installer\vswhere.exe"

New-Item -ItemType Directory -Path $WorkRoot | Out-Null
New-Item -ItemType Directory -Path $EvidenceDir | Out-Null

try {
    if (-not (Test-Path -LiteralPath $VsWhere -PathType Leaf)) {
        throw "vswhere.exe is required to select a supported native toolset"
    }
    $VisualStudioVersion = (
        & $VsWhere `
            -latest `
            -products * `
            -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 `
            -property installationVersion
    ).Trim()
    $VisualStudioMajor = [int]($VisualStudioVersion.Split(".")[0])
    $PlatformToolset = switch ($VisualStudioMajor) {
        17 { "v143" }
        18 { "v145" }
        default {
            throw "unsupported Visual Studio major version: $VisualStudioMajor"
        }
    }

    Invoke-WebRequest -Uri $SourceUrl -OutFile $SourceArchive
    $ActualHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $SourceArchive).Hash.ToLowerInvariant()
    if ($ActualHash -ne $ExpectedHash) {
        throw "CPython source SHA-256 mismatch: $ActualHash"
    }

    & tar.exe -xf $SourceArchive -C $WorkRoot
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $SourceRoot -PathType Container)) {
        throw "failed to extract the CPython source archive"
    }

    $BuildScript = Join-Path $SourceRoot "PCbuild\build.bat"
    & $BuildScript -p x64 "/p:PlatformToolset=$PlatformToolset" 2>&1 |
        Tee-Object -FilePath $BuildLog
    if ($LASTEXITCODE -ne 0) {
        throw "CPython PCbuild failed with exit code $LASTEXITCODE"
    }

    $SourceLauncher = Join-Path $SourceRoot "python.bat"
    $LayoutScript = Join-Path $SourceRoot "PC\layout"
    $BuildDirectory = Join-Path $SourceRoot "PCbuild\amd64"
    & $SourceLauncher $LayoutScript `
        -b $BuildDirectory `
        -s $SourceRoot `
        --copy $InstallPrefix `
        --preset-default 2>&1 | Tee-Object -FilePath $LayoutLog
    if ($LASTEXITCODE -ne 0) {
        throw "CPython layout creation failed with exit code $LASTEXITCODE"
    }

    $Python = Join-Path $InstallPrefix "python.exe"
    if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
        throw "CPython layout did not produce python.exe"
    }

    $Version = (& $Python --version 2>&1).Trim()
    $Identity = & $Python -c @"
import json, platform, struct, sys
with open(sys.executable, "rb") as handle:
    if handle.read(2) != b"MZ":
        raise SystemExit("Python executable is not a PE image")
    handle.seek(0x3C)
    pe_offset = struct.unpack("<I", handle.read(4))[0]
    handle.seek(pe_offset)
    if handle.read(4) != b"PE\x00\x00":
        raise SystemExit("Python executable has no PE signature")
    machine = struct.unpack("<H", handle.read(2))[0]
print(json.dumps({
    "implementation": platform.python_implementation(),
    "machine": platform.machine(),
    "pe_machine": f"0x{machine:04x}",
    "system": platform.system(),
    "version": platform.python_version(),
    "executable": sys.executable,
}, sort_keys=True))
"@
    $IdentityObject = $Identity | ConvertFrom-Json
    if (
        $Version -ne "Python 3.12.13" -or
        $IdentityObject.implementation -ne "CPython" -or
        $IdentityObject.system -ne "Windows" -or
        $IdentityObject.machine -notin @("AMD64", "x86_64") -or
        $IdentityObject.pe_machine -ne "0x8664"
    ) {
        throw "built interpreter is not native Windows x86_64 CPython 3.12.13"
    }

    & $VsWhere -latest -products * -format json |
        Set-Content -LiteralPath (Join-Path $EvidenceDir "visual-studio.json") -Encoding utf8
    $VisualStudioRoot = (
        & $VsWhere `
            -latest `
            -products * `
            -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 `
            -property installationPath
    ).Trim()
    $DeveloperCommand = Join-Path $VisualStudioRoot "Common7\Tools\VsDevCmd.bat"
    $CompilerIdentity = & cmd.exe /d /s /c (
        "`"$DeveloperCommand`" -no_logo -arch=x64 -host_arch=x64 " +
        ">nul && cl.exe 2>&1"
    )
    if (($CompilerIdentity -join "`n") -notmatch "Compiler Version") {
        throw "could not record the Microsoft C/C++ compiler identity"
    }
    $CompilerIdentity |
        Set-Content -LiteralPath (Join-Path $EvidenceDir "compiler-identity.txt") -Encoding utf8

    $SourceUrl | Set-Content -LiteralPath (Join-Path $EvidenceDir "python-source-url.txt") -Encoding ascii
    "$ExpectedHash  Python-3.12.13.tar.xz" |
        Set-Content -LiteralPath (Join-Path $EvidenceDir "python-source.sha256") -Encoding ascii
    $Version | Set-Content -LiteralPath (Join-Path $EvidenceDir "python-version.txt") -Encoding ascii
    $Python | Set-Content -LiteralPath (Join-Path $EvidenceDir "python-executable.txt") -Encoding utf8
    $Identity | Set-Content -LiteralPath (Join-Path $EvidenceDir "python-identity.json") -Encoding utf8
    "PCbuild\build.bat -p x64 /p:PlatformToolset=$PlatformToolset" |
        Set-Content -LiteralPath (Join-Path $EvidenceDir "python-build-command.txt") -Encoding ascii
    "python.bat PC\layout --copy <prefix> --preset-default" |
        Set-Content -LiteralPath (Join-Path $EvidenceDir "python-layout-command.txt") -Encoding ascii

    $Metadata = [ordered]@{
        builder_target = "windows"
        build_command = "PCbuild\build.bat -p x64 /p:PlatformToolset=$PlatformToolset"
        install_prefix = $InstallPrefix
        layout_command = "python.bat PC\layout --copy <prefix> --preset-default"
        python_executable = $Python
        python_implementation = $IdentityObject.implementation
        python_machine = "x86_64"
        python_pe_machine = $IdentityObject.pe_machine
        python_system = $IdentityObject.system
        python_version = $IdentityObject.version
        source_sha256 = $ExpectedHash
        source_tarball = "Python-3.12.13.tar.xz"
        source_url = $SourceUrl
        visual_studio_version = $VisualStudioVersion
        platform_toolset = $PlatformToolset
    }
    $Metadata |
        ConvertTo-Json -Depth 5 |
        Set-Content -LiteralPath (Join-Path $EvidenceDir "python-build-metadata.json") -Encoding utf8

    Write-Host "Built interpreter: $Python"
    Write-Host "CPython 3.12.13 native Windows x86_64 build evidence complete."
} finally {
    if (Test-Path -LiteralPath $WorkRoot) {
        Remove-Item -LiteralPath $WorkRoot -Recurse -Force
    }
}

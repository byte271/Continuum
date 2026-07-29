[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$OutputParent
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$OutputParent = [IO.Path]::GetFullPath($OutputParent)
$Repository = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$BundleName = "continuum-windows-x86_64"
$Destination = Join-Path $OutputParent $BundleName
$Archive = Join-Path $OutputParent "$BundleName.zip"
$Sidecar = "$Archive.sha256"
$BuildEvidence = Join-Path $OutputParent "$BundleName-build-evidence"
foreach ($Path in @($Destination, $Archive, $Sidecar, $BuildEvidence)) {
    if (Test-Path -LiteralPath $Path) {
        throw "bundle output already exists: $Path"
    }
}

$WorkRoot = Join-Path ([IO.Path]::GetTempPath()) ("continuum-bundle-" + [guid]::NewGuid().ToString("N"))
$PythonPrefix = Join-Path $WorkRoot "python-prefix"
$Bundle = Join-Path $WorkRoot $BundleName
New-Item -ItemType Directory -Path $OutputParent | Out-Null
New-Item -ItemType Directory -Path $WorkRoot | Out-Null

try {
    & (Join-Path $Repository "validation\windows\build_cpython.ps1") `
        -InstallPrefix $PythonPrefix `
        -EvidenceDir $BuildEvidence

    New-Item -ItemType Directory -Path (Join-Path $Bundle "bin") -Force | Out-Null
    New-Item -ItemType Directory -Path (Join-Path $Bundle "app") -Force | Out-Null
    New-Item -ItemType Directory -Path (Join-Path $Bundle "examples") -Force | Out-Null
    Move-Item -LiteralPath $PythonPrefix -Destination (Join-Path $Bundle "runtime")
    Copy-Item -LiteralPath (Join-Path $Repository "continuum") `
        -Destination (Join-Path $Bundle "app\continuum") -Recurse
    Get-ChildItem -LiteralPath (Join-Path $Bundle "app") -Recurse -Directory -Filter "__pycache__" |
        Remove-Item -Recurse -Force
    Get-ChildItem -LiteralPath (Join-Path $Bundle "app") -Recurse -File -Filter "*.pyc" |
        Remove-Item -Force
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot "continuum.cmd") `
        -Destination (Join-Path $Bundle "bin\continuum.cmd")
    Copy-Item -LiteralPath (Join-Path $Repository "examples\demo.py") `
        -Destination (Join-Path $Bundle "examples\demo.py")
    Copy-Item -LiteralPath (Join-Path $Repository "examples\demo_input.txt") `
        -Destination (Join-Path $Bundle "examples\demo_input.txt")
    Copy-Item -LiteralPath (Join-Path $Repository "LICENSE") -Destination $Bundle
    Copy-Item -LiteralPath (Join-Path $Repository "README.md") -Destination $Bundle
    Move-Item -LiteralPath $BuildEvidence -Destination (Join-Path $Bundle "python-build-evidence")

    $GitCommit = (& git -C $Repository rev-parse HEAD).Trim()
    if ($LASTEXITCODE -ne 0) {
        throw "cannot resolve Git commit"
    }
    $SourceHash = ((Get-Content -LiteralPath (Join-Path $Repository "validation\cross_platform\cpython-3.12.13.sha256") -Raw).Trim() -split "\s+")[0]
    $RuntimePython = Join-Path $Bundle "runtime\python.exe"
    $ManifestPath = Join-Path $Bundle "runtime-manifest.json"
    $env:PYTHONHOME = Join-Path $Bundle "runtime"
    $env:PYTHONPATH = Join-Path $Bundle "app"
    & $RuntimePython -c @"
import json, platform
from pathlib import Path
from continuum import IR_VERSION, __version__
manifest = {
    "architecture": "x86_64",
    "bundle_target": "windows-x86_64",
    "continuum_version": __version__,
    "cpython_source_sha256": "$SourceHash",
    "git_commit": "$GitCommit",
    "ir_version": IR_VERSION,
    "python_implementation": platform.python_implementation(),
    "python_version": platform.python_version(),
    "self_contained": True,
    "system": platform.system(),
}
if manifest["system"] != "Windows" or platform.machine() not in {"AMD64", "x86_64"}:
    raise SystemExit("bundle host identity is not Windows x86_64")
if manifest["python_version"] != "3.12.13":
    raise SystemExit("bundle does not contain exact CPython 3.12.13")
Path(r"$ManifestPath").write_text(
    json.dumps(manifest, indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
)
"@
    if ($LASTEXITCODE -ne 0) {
        throw "cannot create the runtime bundle manifest"
    }
    $env:CONTINUUM_BUNDLE_MANIFEST = $ManifestPath
    & (Join-Path $Bundle "bin\continuum.cmd") --version
    if ($LASTEXITCODE -ne 0) {
        throw "bundled launcher version check failed"
    }
    & (Join-Path $Bundle "bin\continuum.cmd") doctor --json |
        Set-Content -LiteralPath (Join-Path $Bundle "doctor-build-check.json") -Encoding utf8
    if ($LASTEXITCODE -ne 0) {
        throw "bundled launcher doctor check failed"
    }

    $SelfTest = Join-Path $WorkRoot "bundle-self-test.py"
    'print("CONTINUUM_BUNDLE_OK")' |
        Set-Content -LiteralPath $SelfTest -Encoding utf8
    $SelfTestOutput = (& (Join-Path $Bundle "bin\continuum.cmd") run $SelfTest).Trim()
    if ($LASTEXITCODE -ne 0 -or $SelfTestOutput -ne "CONTINUUM_BUNDLE_OK") {
        throw "bundle self-test failed"
    }

    Move-Item -LiteralPath $Bundle -Destination $Destination
    $env:PYTHONHOME = Join-Path $Destination "runtime"
    $env:PYTHONPATH = Join-Path $Destination "app"
    $ArchivePython = Join-Path $Destination "runtime\python.exe"
    & $ArchivePython (Join-Path $PSScriptRoot "archive_bundle_zip.py") $Destination $Archive
    if ($LASTEXITCODE -ne 0) {
        throw "bundle archive creation failed"
    }
    Write-Host "Bundle: $Destination"
    Write-Host "Archive: $Archive"
    Write-Host "SHA-256: $Sidecar"
} finally {
    Remove-Item Env:PYTHONHOME -ErrorAction SilentlyContinue
    Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
    Remove-Item Env:CONTINUUM_BUNDLE_MANIFEST -ErrorAction SilentlyContinue
    if (Test-Path -LiteralPath $WorkRoot) {
        Remove-Item -LiteralPath $WorkRoot -Recurse -Force
    }
}

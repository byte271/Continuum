[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Archive,

    [Parameter(Mandatory = $true)]
    [string]$Sha256,

    [string]$Prefix = (Join-Path $env:LOCALAPPDATA "Continuum")
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

if ($Sha256 -notmatch "^[0-9a-f]{64}$") {
    throw "SHA-256 must be exactly 64 lowercase hexadecimal characters"
}
$Prefix = [IO.Path]::GetFullPath($Prefix)
$Temporary = Join-Path ([IO.Path]::GetTempPath()) ("continuum-install-" + [guid]::NewGuid().ToString("N"))
$DownloadedArchive = Join-Path $Temporary "continuum.zip"
$BundleName = "continuum-windows-x86_64"
$LibraryDir = Join-Path $Prefix "lib\$BundleName"
$CommandPath = Join-Path $Prefix "bin\continuum.cmd"

New-Item -ItemType Directory -Path $Temporary | Out-Null
try {
    if ($Archive -match "^https://") {
        Invoke-WebRequest -Uri $Archive -OutFile $DownloadedArchive
    } elseif ($Archive -match "^http://") {
        throw "installer refuses non-HTTPS downloads"
    } else {
        Copy-Item -LiteralPath ([IO.Path]::GetFullPath($Archive)) -Destination $DownloadedArchive
    }

    $ActualHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $DownloadedArchive).Hash.ToLowerInvariant()
    if ($ActualHash -ne $Sha256) {
        throw "archive SHA-256 mismatch"
    }

    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $Zip = [IO.Compression.ZipFile]::OpenRead($DownloadedArchive)
    try {
        $Seen = [Collections.Generic.HashSet[string]]::new(
            [StringComparer]::OrdinalIgnoreCase
        )
        foreach ($Entry in $Zip.Entries) {
            $Name = $Entry.FullName
            if (
                $Name.Contains("\") -or
                $Name.Contains(":") -or
                $Name.StartsWith("/") -or
                -not ($Name -eq "$BundleName/" -or $Name.StartsWith("$BundleName/"))
            ) {
                throw "unsafe or unexpected archive member: $Name"
            }
            $Parts = $Name.Split("/", [StringSplitOptions]::RemoveEmptyEntries)
            if ($Parts -contains ".." -or $Parts -contains ".") {
                throw "path traversal in archive member: $Name"
            }
            foreach ($Part in $Parts) {
                if ($Part.TrimEnd([char[]]@(" ", ".")) -ne $Part) {
                    throw "Windows-normalized archive member is unsafe: $Name"
                }
                $Stem = [IO.Path]::GetFileNameWithoutExtension($Part)
                if ($Stem -match "^(?i:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])$") {
                    throw "reserved Windows path in archive member: $Name"
                }
            }
            $CollisionKey = $Name.TrimEnd([char]'/')
            if (-not $Seen.Add($CollisionKey)) {
                throw "duplicate Windows archive member: $Name"
            }
            $UnixMode = ($Entry.ExternalAttributes -shr 16) -band 0xF000
            if ($UnixMode -eq 0xA000) {
                throw "symbolic links are not allowed in the Windows bundle: $Name"
            }
        }
    } finally {
        $Zip.Dispose()
    }

    $Extracted = Join-Path $Temporary "extracted"
    [IO.Compression.ZipFile]::ExtractToDirectory($DownloadedArchive, $Extracted)
    $Staged = Join-Path $Extracted $BundleName
    & (Join-Path $Staged "bin\continuum.cmd") doctor | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "staged launcher failed its compatibility check"
    }
    if ((Test-Path -LiteralPath $LibraryDir) -or (Test-Path -LiteralPath $CommandPath)) {
        throw "Continuum installation already exists under $Prefix"
    }

    New-Item -ItemType Directory -Path (Split-Path -Parent $LibraryDir) -Force | Out-Null
    New-Item -ItemType Directory -Path (Split-Path -Parent $CommandPath) -Force | Out-Null
    Move-Item -LiteralPath $Staged -Destination $LibraryDir
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot "continuum-installed.cmd") -Destination $CommandPath
    try {
        & $CommandPath doctor | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw "installed launcher failed its compatibility check"
        }
    } catch {
        Remove-Item -LiteralPath $CommandPath -Force -ErrorAction SilentlyContinue
        if (Test-Path -LiteralPath $LibraryDir) {
            Move-Item -LiteralPath $LibraryDir -Destination $Staged
        }
        throw
    }

    Write-Host "Installed Continuum: $CommandPath"
    Write-Host "Verify: $CommandPath doctor"
    Write-Host "Uninstall: Remove-Item '$CommandPath'; Remove-Item '$LibraryDir' -Recurse"
} finally {
    if (Test-Path -LiteralPath $Temporary) {
        Remove-Item -LiteralPath $Temporary -Recurse -Force
    }
}

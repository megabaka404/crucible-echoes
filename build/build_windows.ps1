param(
    [switch]$Clean,
    [string]$PythonPath
)

$ErrorActionPreference = "Stop"
$repoRoot = [System.IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
Set-Location -LiteralPath $repoRoot

$python = Join-Path $repoRoot ".venv\Scripts\python.exe"
if ($PythonPath) {
    if (-not [System.IO.Path]::IsPathRooted($PythonPath)) { throw "-PythonPath must be an absolute path." }
    $python = [System.IO.Path]::GetFullPath($PythonPath)
} elseif (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if (-not $pythonCommand) { throw "Python not found. Pass -PythonPath with a working interpreter's absolute path." }
    $python = $pythonCommand.Source
}
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) { throw "Python executable does not exist: $python" }
& $python -c "import sys; print(sys.executable)"
if ($LASTEXITCODE -ne 0) { throw "Python failed to start. Pass -PythonPath with a working interpreter." }
& $python -m pip install -r (Join-Path $repoRoot "requirements.txt")
if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed (exit $LASTEXITCODE)." }

$buildBase = [System.IO.Path]::GetFullPath((Join-Path $repoRoot "build"))
$distBase = [System.IO.Path]::GetFullPath((Join-Path $repoRoot "dist"))
$buildTarget = [System.IO.Path]::GetFullPath((Join-Path $buildBase "CrucibleEchoes"))
$distTarget = [System.IO.Path]::GetFullPath((Join-Path $distBase "CrucibleEchoes"))
if ($Clean) {
    foreach ($target in @(@{ Path = $buildTarget; Parent = $buildBase }, @{ Path = $distTarget; Parent = $distBase })) {
        $resolvedParent = [System.IO.Path]::GetFullPath($target.Parent).TrimEnd('\') + '\'
        $resolvedTarget = [System.IO.Path]::GetFullPath($target.Path)
        if (-not $resolvedTarget.StartsWith($resolvedParent, [System.StringComparison]::OrdinalIgnoreCase) -or
            (Split-Path -Leaf $resolvedTarget) -ne "CrucibleEchoes") {
            throw "Refusing to clean unexpected build path: $resolvedTarget"
        }
        if (Test-Path -LiteralPath $resolvedTarget) {
            $targetInfo = Get-Item -LiteralPath $resolvedTarget
            $parentInfo = Get-Item -LiteralPath $target.Parent
            if (($targetInfo.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -or
                ($parentInfo.Attributes -band [System.IO.FileAttributes]::ReparsePoint)) {
                throw "Refusing to clean a redirected build path: $resolvedTarget"
            }
            $actualTarget = $targetInfo.FullName
            if (-not $actualTarget.StartsWith($resolvedParent, [System.StringComparison]::OrdinalIgnoreCase)) {
                throw "Refusing to clean path outside its build directory: $actualTarget"
            }
            Remove-Item -LiteralPath $actualTarget -Recurse -Force
        }
    }
}
& $python -m PyInstaller --noconfirm --clean (Join-Path $repoRoot "build\CrucibleEchoes.spec")
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed (exit $LASTEXITCODE). Archive was not generated." }
$builtExe = Join-Path $distTarget "CrucibleEchoes.exe"
if (-not (Test-Path -LiteralPath $builtExe -PathType Leaf)) { throw "Build did not produce the expected EXE: $builtExe" }
$zip = Join-Path $distBase "CrucibleEchoes-windows.zip"
if (Test-Path -LiteralPath $zip) { Remove-Item -LiteralPath $zip -Force }
Compress-Archive -LiteralPath $distTarget -DestinationPath $zip
Write-Host "Build complete: $zip"

param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$ToolArgs
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$BundledPython = Join-Path $HOME '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
$PythonExe = $null

$systemPython = Get-Command python -ErrorAction SilentlyContinue
if ($systemPython) {
    & $systemPython.Source -c "import sys, lxml, pypdf; assert sys.version_info >= (3, 10)" 2>$null
    if ($LASTEXITCODE -eq 0) {
        $PythonExe = $systemPython.Source
    }
}

if (-not $PythonExe -and (Test-Path -LiteralPath $BundledPython)) {
    $PythonExe = $BundledPython
}

if (-not $PythonExe) {
    throw 'Python 3.10+ with required packages was not found.'
}

Push-Location $ProjectRoot
try {
    & $PythonExe -m market_maker_tool @ToolArgs
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}

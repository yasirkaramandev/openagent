$ErrorActionPreference = "Stop"
$ExamplePython = if ($env:OPENAGENT_EXAMPLE_PYTHON) {
    $env:OPENAGENT_EXAMPLE_PYTHON
} else {
    "python"
}

& $ExamplePython (Join-Path $PSScriptRoot "verify_example.py") @args
exit $LASTEXITCODE


param([string]$RepositoryRoot = 'C:\dev\ifood-agent-challenge')
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $RepositoryRoot
if (-not (Test-Path -LiteralPath 'pyproject.toml')) { throw 'pyproject.toml não encontrado no repositório.' }
Write-Host "=== REPOSITORIO ===" -ForegroundColor Cyan
git status --short --branch
if ($LASTEXITCODE -ne 0) { throw 'git status falhou' }
Write-Host "=== DATASET ===" -ForegroundColor Cyan
$book = 'data/raw/despensa_dona_maria.xlsx'
if (Test-Path -LiteralPath $book) {
    Write-Host 'Workbook local: presente' -ForegroundColor Green
} else {
    Write-Warning "Workbook local ausente: $book. Verifique SABOR_DA_MARIA_WORKBOOK."
}
$tracked = @(git ls-files -- $book)
if ($tracked.Count -eq 0) { Write-Warning 'Workbook nao esta versionado nesse caminho. O avaliador precisara receber o arquivo ou configurar a variavel de ambiente.' }
else { Write-Host 'Workbook: versionado no git' -ForegroundColor Green }
Write-Host '=== QUALIDADE ===' -ForegroundColor Cyan
$checks = @(
    @('ruff check','ruff','check','src','tests','evals'),
    @('ruff format','ruff','format','--check','src','tests','evals'),
    @('mypy','mypy','src'),
    @('pyright','pyright'),
    @('pytest','pytest','tests/unit','tests/integration','tests/evaluation','tests/contract','-q')
)
foreach ($entry in $checks) {
    $label = $entry[0]
    $uvArgs = @('run','--locked') + @($entry[1..($entry.Count-1)])
    Write-Host "Executando: $label" -ForegroundColor Yellow
    & uv @uvArgs
    if ($LASTEXITCODE -ne 0) { throw "$label falhou (exit code $LASTEXITCODE)" }
}
Write-Host '=== HERMES MCP ===' -ForegroundColor Cyan
& hermes mcp test sabor-da-maria
if ($LASTEXITCODE -ne 0) { throw "MCP Hermes falhou (exit code $LASTEXITCODE)" }
Write-Host '=== PR CHECKS ===' -ForegroundColor Cyan
& gh pr checks 1 --repo FilipiWanderley/ifood-agent-challenge
if ($LASTEXITCODE -ne 0) { throw "PR checks falhou (exit code $LASTEXITCODE)" }
Write-Host "TODOS OS CHECKS EXECUTADOS PASSARAM" -ForegroundColor Green

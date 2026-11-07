$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
# Keep test databases and the one-file EXE extraction on the build workspace.
# The hosted runner's default user TEMP previously failed with WinError 483.
$buildTemp = Join-Path $PSScriptRoot 'build\temp'
New-Item -ItemType Directory -Force -Path $buildTemp | Out-Null
$env:TEMP = $buildTemp
$env:TMP = $buildTemp
python -c "import pathlib,tempfile,sqlite3; p=pathlib.Path(tempfile.mkdtemp()); db=p/'probe.sqlite3'; c=sqlite3.connect(db); c.execute('PRAGMA journal_mode=WAL'); c.execute('CREATE TABLE probe(value INTEGER)'); c.execute('INSERT INTO probe VALUES (1)'); c.commit(); c.close(); db.unlink(); p.rmdir(); print('Temporary storage probe: OK')"
if ($LASTEXITCODE -ne 0) { throw 'Build workspace storage is unavailable. Retry on a fresh runner.' }
python -m unittest discover -s tests -v
if ($LASTEXITCODE -ne 0) { throw 'Tests failed.' }
python -m PyInstaller --noconfirm NewsRadar.spec
if ($LASTEXITCODE -ne 0) { throw 'EXE build failed.' }
$exePath = Join-Path $PSScriptRoot 'dist\NewsRadar.exe'
$reportPath = Join-Path $PSScriptRoot 'build\exe-self-test.json'
if (Test-Path -LiteralPath $reportPath) { Remove-Item -LiteralPath $reportPath }
$process = Start-Process -FilePath $exePath -ArgumentList @('--self-test', ('"' + $reportPath + '"')) -WindowStyle Hidden -PassThru -Wait
if ($process.ExitCode -ne 0) {
    if (Test-Path -LiteralPath $reportPath) { Get-Content -LiteralPath $reportPath -Raw }
    throw "EXE self-test failed. See $reportPath"
}
$report = Get-Content -LiteralPath $reportPath -Raw | ConvertFrom-Json
if (-not $report.ok) { throw 'EXE self-test did not pass.' }
Write-Output "Ready: $exePath"

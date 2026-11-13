$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$tool = Join-Path $root 'build/native/gsr-windows/layer-gsr-config.exe'
$folder = New-Item -ItemType Directory -Path (Join-Path ([IO.Path]::GetTempPath()) ("layer-gsr-" + [Guid]::NewGuid()))
$unicode = 'GSR ' + [char]0x00FC + ' config_ui'
$fixture = Join-Path $folder.FullName $unicode
try {
    $json = '{"main":{"config_file_version":3},"replay":{"time":240,"short_save_duration":90,"long_save_duration":300}}'
    [IO.File]::WriteAllText($fixture, $json)
    $result = & $tool validate $fixture
    if ($LASTEXITCODE -ne 0 -or $result -ne '240 90 240') {
        throw "Expected the patched GSR replay durations 240 90 240. Got: $result"
    }
    [IO.File]::WriteAllText($fixture, '{"replay":')
    & $tool validate $fixture 2>$null
    if ($LASTEXITCODE -ne 1) { throw 'The configuration reader accepted malformed JSON.' }
    Write-Output 'Patched GSR replay validation and Unicode path passed.'
} finally {
    Remove-Item -LiteralPath $fixture -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $folder.FullName
}

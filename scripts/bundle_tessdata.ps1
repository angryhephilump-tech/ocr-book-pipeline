# Download top-20 Tesseract language packs into vendor/tesseract/tessdata
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$VendorTess = Join-Path $Root "vendor\tesseract"
$TessData = Join-Path $VendorTess "tessdata"
if (-not (Test-Path $TessData)) {
    New-Item -ItemType Directory -Path $TessData -Force | Out-Null
}

$Langs = @(
    "spa", "eng", "fra", "deu", "por", "ita", "nld", "rus", "ara",
    "chi_sim", "chi_tra", "jpn", "kor", "hin", "pol", "tur", "vie", "ind", "ron", "swe"
)

$BaseUrl = "https://github.com/tesseract-ocr/tessdata/raw/main"
Write-Host "=== Bundling tessdata (top 20) ===" -ForegroundColor Cyan

foreach ($lang in $Langs) {
    $dest = Join-Path $TessData "$lang.traineddata"
    if (Test-Path $dest) {
        Write-Host "  skip $lang (exists)" -ForegroundColor DarkGray
        continue
    }
    $url = "$BaseUrl/$lang.traineddata"
    Write-Host "  download $lang …" -ForegroundColor Yellow
    Invoke-WebRequest -Uri $url -OutFile $dest -UseBasicParsing
}

Write-Host "Done: $TessData" -ForegroundColor Green

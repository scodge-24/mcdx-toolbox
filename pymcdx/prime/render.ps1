# Render a .mcdx through the local Mathcad Prime install to worksheet.xps + page-NN.png.
# Shared COM does not provide reliable process ownership. Never hide/quit/kill Prime.
param([string]$Mcdx, [string]$OutDir, [double]$Dpi = 110, [switch]$Resave)
$ErrorActionPreference = 'Stop'

$before = @(Get-Process MathcadPrime -ErrorAction SilentlyContinue | ForEach-Object Id)
$app = New-Object -ComObject MathcadPrime.Application
$after = @(Get-Process MathcadPrime -ErrorAction SilentlyContinue | ForEach-Object Id)
@{ before = $before; after = $after; ownership = 'unproven-shared-COM' } |
    ConvertTo-Json | Set-Content -Path (Join-Path $OutDir 'session.json')

$xps = Join-Path $OutDir 'worksheet.xps'
$ws = $null
try {
    $ws = $app.Open($Mcdx)
    if ($null -eq $ws) { throw 'Mathcad Prime could not open the worksheet (corrupt or unreadable .mcdx)' }
    $ws.Synchronize()
    if ($Resave) { $ws.SaveAs((Join-Path $OutDir 'resaved.mcdx')) }
    $ws.SaveAs($xps)
}
finally {
    if ($null -ne $ws) { $ws.Close(2) }  # only the unique staged worksheet
}

Add-Type -AssemblyName ReachFramework, PresentationCore, PresentationFramework, WindowsBase
$doc = New-Object System.Windows.Xps.Packaging.XpsDocument($xps, [IO.FileAccess]::Read)
try {
    $paginator = $doc.GetFixedDocumentSequence().DocumentPaginator
    $scale = $Dpi / 96
    for ($i = 0; $i -lt $paginator.PageCount; $i++) {
        $page = $paginator.GetPage($i)
        $size = $page.Size
        $bmp = New-Object System.Windows.Media.Imaging.RenderTargetBitmap(
            [int]($size.Width * $scale), [int]($size.Height * $scale), $Dpi, $Dpi,
            [System.Windows.Media.PixelFormats]::Pbgra32)
        $bg = New-Object System.Windows.Media.DrawingVisual
        $dc = $bg.RenderOpen()
        $dc.DrawRectangle([System.Windows.Media.Brushes]::White, $null,
            (New-Object System.Windows.Rect(0, 0, $size.Width, $size.Height)))
        $dc.Close()
        $bmp.Render($bg)
        $bmp.Render($page.Visual)
        $enc = New-Object System.Windows.Media.Imaging.PngBitmapEncoder
        $enc.Frames.Add([System.Windows.Media.Imaging.BitmapFrame]::Create($bmp))
        $stream = [IO.File]::Create((Join-Path $OutDir ('page-{0:D2}.png' -f ($i + 1))))
        try { $enc.Save($stream) } finally { $stream.Close() }
    }
}
finally { $doc.Close() }

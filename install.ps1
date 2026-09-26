# One-line installer (Windows PowerShell, run as user, not admin):
#   irm https://raw.githubusercontent.com/Mishaadevv/blender-mcp/main/install.ps1 | iex
# With args:
#   & ([scriptblock]::Create((irm https://raw.githubusercontent.com/Mishaadevv/blender-mcp/main/install.ps1))) --all
param([string]$Repo = "Mishaadevv/blender-mcp", [string]$Args = "--all")
$ErrorActionPreference = "Stop"
$url = "https://raw.githubusercontent.com/$Repo/main/install.py"
$tmp = Join-Path $env:TEMP ("blender-mcp-install-" + [guid]::NewGuid().ToString("N") + ".py")
Write-Host "[blender-mcp] downloading $url"
Invoke-WebRequest -Uri $url -OutFile $tmp
$py = $null
foreach ($c in @("python","py","python3")) { if (Get-Command $c -ErrorAction SilentlyContinue) { $py = $c; break } }
if (-not $py) { Write-Error "Python 3.11+ not found. Install from https://www.python.org/downloads/"; exit 1 }
$extra = @()
if ($Args) { $extra = $Args -split '\s+' | Where-Object { $_ -ne '' } }
Write-Host "[blender-mcp] running: $py $tmp $($extra -join ' ')"
& $py $tmp @extra
exit $LASTEXITCODE

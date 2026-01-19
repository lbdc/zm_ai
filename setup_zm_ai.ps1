# Set window title
$host.UI.RawUI.WindowTitle = "ZoneMinder AI Setup"

function Show-Recommendations {
    Write-Host "`nRecommended versions for compatibility:" -ForegroundColor Cyan
    Write-Host "  Python:        3.10.x"
    Write-Host "  PyTorch:       2.3.0"
    Write-Host "  CUDA runtime:  12.1 (via PyTorch wheel)"
    Write-Host "  NVIDIA Driver: >= 528.02"
    Write-Host "`nFix suggestions:"
    Write-Host "  - Download Python: https://www.python.org/downloads/release/python-31012/"
    Write-Host "  - Download NVIDIA Drivers: https://www.nvidia.com/Download/index.aspx"
    Write-Host "  - Install CUDA Toolkit: https://developer.nvidia.com/cuda-downloads"
    Write-Host "  - Install PyTorch with CUDA 12.1: pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu121"
    Write-Host "  - Install YOLOv8: pip install ultralytics"
}

function Require-VCRedistX64 {
    # Detect "Microsoft Visual C++ 2015-2022 Redistributable (x64)" installed
    $vcKeyPaths = @(
        "HKLM:\SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64",
        "HKLM:\SOFTWARE\WOW6432Node\Microsoft\VisualStudio\14.0\VC\Runtimes\x64"
    )

    foreach ($p in $vcKeyPaths) {
        try {
            $k = Get-ItemProperty -Path $p -ErrorAction Stop
            # "Installed"=1 and Version exists when present
            if ($k.Installed -eq 1 -and $k.Version) {
                Write-Host "VC++ x64 Redistributable found (Version: $($k.Version))." -ForegroundColor Green
                return
            }
        } catch { }
    }

    Write-Host "`nMissing requirement: Microsoft Visual C++ 2015-2022 Redistributable (x64)" -ForegroundColor Red
    Write-Host "Install it from:" -ForegroundColor Yellow
    Write-Host "  https://aka.ms/vc14/vc_redist.x64.exe"
    Read-Host -Prompt "`nPress Enter to exit"
    exit 1
}

function Require-FFmpeg {
    # Prefer PATH check first
    $ff = Get-Command ffmpeg -ErrorAction SilentlyContinue
    if ($ff) {
        Write-Host "FFmpeg found in PATH: $($ff.Source)" -ForegroundColor Green
        return
    }

    # Optional: check common locations (helpful on Windows installs)
    $common = @(
        "$env:ProgramFiles\ffmpeg\bin\ffmpeg.exe",
        "$env:ProgramFiles\FFmpeg\bin\ffmpeg.exe",
        "$env:LocalAppData\Programs\ffmpeg\bin\ffmpeg.exe",
        "C:\ffmpeg\bin\ffmpeg.exe"
    )

    foreach ($c in $common) {
        if (Test-Path $c) {
            Write-Host "FFmpeg found at: $c" -ForegroundColor Green
            Write-Host "Tip: add this folder to PATH for easier use:" -ForegroundColor Cyan
            Write-Host "  $(Split-Path -Parent $c)"
            return
        }
    }

    Write-Host "`nMissing requirement: FFmpeg" -ForegroundColor Red
    Write-Host "Download builds from:" -ForegroundColor Yellow
    Write-Host "  https://github.com/BtbN/FFmpeg-Builds/releases"
    Write-Host "`nAfter extracting, ensure ffmpeg.exe is available either:" -ForegroundColor Cyan
    Write-Host "  - In PATH (recommended), or"
    Write-Host "  - In one of these common locations:"
    $common | ForEach-Object { Write-Host "    $_" }
    Read-Host -Prompt "`nPress Enter to exit"
    exit 1
}

# -------------------------------
# REQUIRED SYSTEM DEPENDENCIES
# -------------------------------
Write-Host "`nChecking required system dependencies..." -ForegroundColor Cyan
Require-VCRedistX64
Require-FFmpeg

# -------------------------------
# EXISTING SCRIPT CONTINUES HERE
# -------------------------------

# Check for Python
try {
    $pythonVersion = & python --version 2>&1
    $cleanVersion = $pythonVersion -replace "^Python\s+", ""
} catch {
    $pythonVersion = $null
}

if (-not $cleanVersion -or $cleanVersion -notmatch "^3\.(10|11|12)") {
    Write-Host "`nPython 3.10, 3.11, or 3.12 is not installed or not in PATH." -ForegroundColor Red
    Write-Host "Please install Python 3.12 from the official website:" -ForegroundColor Yellow
    Write-Host "  https://www.python.org/downloads/release/"
    Write-Host "`nIMPORTANT: During installation, check the box for 'Add Python to PATH'." -ForegroundColor Cyan
    Read-Host -Prompt "`nPress Enter to exit"
    exit 1
}

Write-Host "Python version found: $pythonVersion"

# Confirm Python is callable
Write-Host "`nChecking Python installation..."
if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    Write-Host "Python is not installed or not in PATH." -ForegroundColor Red
    Show-Recommendations
    Read-Host -Prompt "Press Enter to exit"
    exit 1
}

# Create virtual environment if it doesn't exist
if (-not (Test-Path "venv")) {
    Write-Host "Creating virtual environment..."
    python -m venv venv
}

# Activate virtual environment
Write-Host "Activating virtual environment..."
& .\venv\Scripts\Activate.ps1

# Upgrade pip
Write-Host "Upgrading pip..."
python -m pip install --upgrade pip

# Install dependencies
Write-Host "Installing required packages from requirements.txt..."
pip install -r requirements.txt

# Install pytorch
Write-Host "Installing PyTorch (2.5.1 + cu121)..."
pip install torch==2.5.1+cu121 torchvision==0.20.1+cu121 torchaudio==2.5.1 --index-url https://download.pytorch.org/whl/cu121

# Verify PyTorch installation and compatibility
Write-Host "`nVerifying PyTorch installation and CUDA support..."
try {
    $torchInfo = python -c "import torch; print(torch.__version__); print(torch.version.cuda); print(torch.cuda.is_available()); print(torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'No GPU')" 2>&1
    $torchLines = $torchInfo -split "`n"
    $torchVersion = $torchLines[0].Trim()
    $torchCuda = $torchLines[1].Trim()
    $cudaAvailable = $torchLines[2].Trim()
    $gpuName = $torchLines[3].Trim()

    Write-Host "`nPyTorch version: $torchVersion"
    Write-Host "CUDA runtime version in PyTorch: $torchCuda"
    Write-Host "CUDA available: $cudaAvailable"
    Write-Host "GPU Detected: $gpuName"

    if ($torchVersion -notmatch "^2\.[3-5]") {
        Write-Host "PyTorch version is newer or older than tested range (2.3 to 2.5). If it works for you, you're good." -ForegroundColor Yellow
        Show-Recommendations
    }
    if ($torchCuda -ne "12.1") {
        Write-Host "CUDA runtime is not 12.1 (required by recommended setup)." -ForegroundColor Yellow
        Show-Recommendations
    }
    if ($cudaAvailable -ne "True") {
        Write-Host "CUDA is not available. Your driver may be outdated or unsupported." -ForegroundColor Yellow
        Show-Recommendations
    }

} catch {
    Write-Host "Failed to verify PyTorch and CUDA: $_" -ForegroundColor Red
    Show-Recommendations
}

# Install Ultralytics (YOLOv8)
Write-Host "`nInstalling Ultralytics (YOLOv8)..."
pip install ultralytics

# Verify Ultralytics (YOLOv8) installation
Write-Host "`nVerifying Ultralytics (YOLOv8) installation..."
try {
    $ultraVer = python -c "import ultralytics; print(ultralytics.__version__)" 2>&1
    Write-Host "Ultralytics YOLOv8 version: $ultraVer" -ForegroundColor Green
} catch {
    Write-Host "Ultralytics (YOLOv8) is not installed or failed to import!" -ForegroundColor Red
    Show-Recommendations
}

# Reminder to launch the app
Write-Host "`nedit settings.ini and email_settings.ini" -ForegroundColor Green
Write-Host "`nTo launch the Flask app, run:" -ForegroundColor Green
Write-Host "    start_zm_ai.ps1"
Write-Host "Then open your browser to: http://localhost:8001/zm_ai"

exit 0


#!/bin/bash

show_recommendations() {
    echo ""
    echo "Recommended versions for compatibility:"
    echo "  Python:        3.10.x"
    echo "  PyTorch:       2.3.0 - 2.5.1"
    echo "  CUDA runtime:  12.1 (via PyTorch wheel)"
    echo "  NVIDIA Driver: >= 528.02"
    echo ""
    echo "Fix suggestions:"
    echo "  - Download Python: https://www.python.org/downloads/release/python-31012/"
    echo "  - Install NVIDIA Drivers: https://www.nvidia.com/Download/index.aspx"
    echo "  - Install CUDA Toolkit: https://developer.nvidia.com/cuda-downloads"
    echo "  - Install PyTorch with CUDA 12.1:"
    echo "      pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu121"
    echo "  - Install YOLOv8:"
    echo "      pip install ultralytics"
}

# Install required system Python packages
echo ""
echo "Installing required Python system packages (via apt)..."
if command -v apt >/dev/null 2>&1; then
    sudo apt update
    sudo apt install -y python3-fastapi python3-psutil python3-opencv python3-watchdog python3-venv tmux
else
    echo "apt not found. This step is only supported on Debian/Ubuntu systems."
    echo "Please install the following packages manually if needed:"
    echo "  python3-fastapi python3-psutil python3-opencv python3-watchdog python3-venv tmux"
fi


# Check for NVIDIA drivers
echo "Checking for NVIDIA drivers..."
if command -v nvidia-smi >/dev/null 2>&1; then
    DRIVER_VERSION=$(nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>/dev/null | head -n 1)
    GPU_NAME=$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -n 1)
    echo "NVIDIA driver detected: $DRIVER_VERSION"
    echo "GPU: $GPU_NAME"
else
    echo "NVIDIA drivers not found or nvidia-smi not available."
    echo "Please ensure NVIDIA drivers are installed and the GPU is recognized."
    show_recommendations
    read -p "Press Enter to exit..."
    exit 1
fi

# Check for Python
PYTHON_VERSION=$(python3 --version 2>/dev/null)
if [[ $? -ne 0 || ! "$PYTHON_VERSION" =~ 3\.(10|11|12) ]]; then
    echo ""
    echo "Python 3.10 is not installed or not in PATH."
    echo "Please install Python 3.10 from the official website:"
    echo "  https://www.python.org/downloads/release/python-31012/"
    echo ""
    echo "IMPORTANT: Add Python 3.10 to PATH."
    read -p "Press Enter to exit..."
    exit 1
fi

echo "Python version found: $PYTHON_VERSION"

# Confirm Python is callable
if ! command -v python3 &>/dev/null; then
    echo "Python is not installed or not in PATH."
    show_recommendations
    read -p "Press Enter to exit..."
    exit 1
fi

# Create virtual environment if not exists
if [ ! -d "venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv venv
fi

# Activate virtual environment
# Wait until venv/bin/activate exists (max 5 seconds)
for i in {1..10}; do
    if [ -f "venv/bin/activate" ]; then
        break
    fi
    echo "Waiting for virtual environment to be ready..."
    sleep 0.5
done

if [ ! -f "venv/bin/activate" ]; then
    echo "Error: venv/bin/activate not found. Virtual environment setup may have failed."
    exit 1
fi

# Now activate (outside the loop)
echo "Activating virtual environment..."
source venv/bin/activate

# Upgrade pip
echo "Upgrading pip..."
python -m pip install --upgrade pip

# Install requirements.txt
echo "Installing requirements.txt..."
pip install -r requirements.txt

# Install PyTorch with CUDA 12.1
echo "Installing PyTorch (2.5.1 + cu121)..."
pip install torch==2.5.1+cu121 torchvision==0.20.1+cu121 torchaudio==2.5.1 --index-url https://download.pytorch.org/whl/cu121

# Verify PyTorch installation and CUDA support
echo ""
echo "Verifying PyTorch installation and CUDA support..."
torch_output=$(python3 -c "import torch; print(torch.__version__); print(torch.version.cuda); print(torch.cuda.is_available()); print(torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'No GPU')" 2>&1)

if [[ $? -eq 0 ]]; then
    torch_version=$(echo "$torch_output" | sed -n 1p)
    torch_cuda=$(echo "$torch_output" | sed -n 2p)
    cuda_available=$(echo "$torch_output" | sed -n 3p)
    gpu_name=$(echo "$torch_output" | sed -n 4p)

    echo ""
    echo "PyTorch version: $torch_version"
    echo "CUDA runtime version in PyTorch: $torch_cuda"
    echo "CUDA available: $cuda_available"
    echo "GPU Detected: $gpu_name"

    if [[ ! "$torch_version" =~ ^2\.[3-5] ]]; then
        echo "Warning: PyTorch version is outside tested range (2.3 - 2.5)"
        show_recommendations
    fi

    if [[ "$torch_cuda" != "12.1" ]]; then
        echo "Warning: CUDA runtime is not 12.1 (recommended setup)"
        show_recommendations
    fi

    if [[ "$cuda_available" != "True" ]]; then
        echo "Warning: CUDA is not available. Driver may be outdated or unsupported"
        show_recommendations
    fi
else
    echo "Failed to verify PyTorch and CUDA:"
    echo "$torch_output"
    show_recommendations
fi

# Install Ultralytics (YOLOv8)
echo "Installing Ultralytics (YOLOv8)..."
pip install ultralytics

# Verify Ultralytics (YOLOv8) installation
echo ""
echo "Verifying Ultralytics (YOLOv8) installation..."
ultra_ver=$(python -c "import ultralytics; print(ultralytics.__version__)" 2>/dev/null)
if [[ $? -eq 0 ]]; then
    echo "Ultralytics YOLOv8 version: $ultra_ver"
else
    echo "Ultralytics (YOLOv8) is not installed or failed to import"
    show_recommendations
fi

# Reminder to launch the app
echo ""
echo "To launch the FastAPI app, run:"
echo "    bash ./start_zm_ai.sh"
echo "Then open your browser to: http://localhost:8001/zm_ai"

exit 0


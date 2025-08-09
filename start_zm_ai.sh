#!/bin/bash

# === USER CONFIGURATION ===
SCRIPT1="zm_ai.py"
VENV_DIR="venv"  # change this if your venv has a different name

# Get the directory where this script is located
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Activate virtual environment and run script
cd "$SCRIPT_DIR"
source "$VENV_DIR/bin/activate"
python "$SCRIPT1"


#!/bin/bash

# === USER CONFIGURATION ===
SCRIPT1="zm_ai.py" 

# Get the directory where this script is located
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Start a new tmux session named 'zmai'
tmux new-session -d -s zmai

# Enable mouse support
tmux set-option -g mouse on

# Run the first script in the first pane
tmux send-keys -t zmai "cd \"$SCRIPT_DIR\" && python3 \"$SCRIPT1\"" C-m


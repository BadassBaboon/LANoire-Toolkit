#!/usr/bin/env python3
"""
L.A. Noire PC Toolkit Launcher
Usage:
  python run_toolkit.py          (Launches the GUI)
  python run_toolkit.py <cmd>    (Passes through to CLI)
"""

import sys
from pathlib import Path

# Ensure toolkit directory is in sys.path
toolkit_root = Path(__file__).resolve().parent
if str(toolkit_root) not in sys.path:
    sys.path.insert(0, str(toolkit_root))

if __name__ == "__main__":
    if len(sys.argv) > 1:
        import cli
        cli.main()
    else:
        from gui.app import launch_gui
        launch_gui()

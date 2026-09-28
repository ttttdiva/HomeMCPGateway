"""Launch a dedicated native WinForms GUI for opt-in real MCP desktop tests."""
from pathlib import Path
import os
import subprocess
import sys

if __name__ == "__main__":
    shell = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    sys.exit(subprocess.call([str(shell), "-NoProfile", "-NonInteractive", "-STA",
        "-ExecutionPolicy", "Bypass", "-File", str(Path(__file__).with_suffix(".ps1")), sys.argv[1]],
        creationflags=subprocess.CREATE_NO_WINDOW))

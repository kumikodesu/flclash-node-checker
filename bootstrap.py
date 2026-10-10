"""Load the portable dependencies without relying on user Python packages."""
from pathlib import Path
import runpy
import sys

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "vendor"))

if __name__ == "__main__":
    if sys.argv[1:] == ["--check-dependencies"]:
        import requests
        import tkinter
        import yaml
        print(f"PyYAML: {yaml.__file__}")
        print(f"requests: {requests.__file__}")
    else:
        runpy.run_path(str(ROOT / "checker.py"), run_name="__main__")

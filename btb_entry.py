"""PyInstaller entry point (the console-script `btb` does not exist inside a frozen exe)."""
import sys

from btborders.cli import main

if __name__ == "__main__":
    sys.exit(main())

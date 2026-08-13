#!/usr/bin/env python3
"""Single-file entry point kept for checkout-based git hooks and `python3 hologram.py`
invocations; the actual tool lives in the `hologram/` package next to this shim."""

from hologram.cli import main

if __name__ == "__main__":
    main()

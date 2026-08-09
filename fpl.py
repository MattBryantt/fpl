#!/usr/bin/env python3
"""Entry point: `python fpl.py <command>`. See `python fpl.py --help`."""

import sys

from fplkit.cli import main

if __name__ == "__main__":
    sys.exit(main())

"""Command line for printing a file."""

import argparse
import sys


def main(argv=None):
    """Prints the lines of the file named in `argv`; returns the exit code."""
    parser = argparse.ArgumentParser(prog="listcli")
    parser.add_argument("path")
    args = parser.parse_args(argv)
    with open(args.path, encoding="utf-8") as handle:
        for line in handle:
            sys.stdout.write(line)
    return 0

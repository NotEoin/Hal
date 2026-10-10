"""python3 -m halcore <command>: the Python half of the `hal` launcher."""
import sys


def main() -> int:
    cmd, rest = (sys.argv[1], sys.argv[2:]) if len(sys.argv) > 1 else ("chat", [])
    if cmd == "chat":
        from .chat import main as run
    elif cmd == "api":
        from .api import main as run
    elif cmd == "ext":
        from .ext import main as run
    elif cmd == "eval":
        from .evaluate import main as run
    elif cmd == "console":
        from .console import main as run
    elif cmd == "units":
        from .units import main as run
    elif cmd == "power":
        from .power import main as run
    else:
        print(f"halcore: unknown command {cmd}", file=sys.stderr)
        return 2
    return run(rest) or 0


if __name__ == "__main__":
    sys.exit(main())

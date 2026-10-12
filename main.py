"""Entry point: python main.py <command> ..."""
import sys

try:
    from cli.commands import main
except ModuleNotFoundError as exc:
    missing = getattr(exc, "name", None) or exc

    print(
        f"error: missing dependency ({missing}).\n"
        "Run with the project virtualenv instead of system python:\n"
        r"  .\.venv\Scripts\python.exe main.py ...   (or run.bat for the web UI)",
        file=sys.stderr,
    )
    raise SystemExit(2)

if __name__ == "__main__":
    raise SystemExit(main())

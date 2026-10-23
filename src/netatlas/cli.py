"""Small CLI entry point for launching the visual application."""
import argparse

from . import __version__


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="netatlas", description="Visualize live Linux network connections.")
    parser.add_argument("--version", action="version", version=f"NetAtlas {__version__}")
    parser.parse_args(argv)
    from .app import run
    return run()


if __name__ == "__main__":
    raise SystemExit(main())

"""Support ``python -m qed`` as an alias for the installed CLI."""

from qed.cli import app


def main() -> None:
    app()


if __name__ == "__main__":
    main()

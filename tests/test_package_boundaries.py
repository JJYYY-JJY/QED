import tomllib
from pathlib import Path


def test_source_distribution_allowlists_python_package_inputs() -> None:
    project_root = Path(__file__).resolve().parents[1]
    config = tomllib.loads((project_root / "pyproject.toml").read_text())

    assert config["tool"]["hatch"]["build"]["targets"]["sdist"]["include"] == [
        "/LICENSE",
        "/README.md",
        "/pyproject.toml",
        "/src/qed",
    ]

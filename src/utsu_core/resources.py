"""
Files that ship inside a package (SQL schemas), found through the package rather than the project folder, so a
program works from an installed copy as well as from the source tree.
"""
from importlib import resources
from pathlib import Path


def package_text(package: str, name: str) -> str:
    """The text of a data file in a package, e.g. `package_text("utsu_programs.agm", "schema.sql")`."""
    return resources.files(package).joinpath(name).read_text(encoding="utf-8-sig")


def schema_dirs(*packages: str) -> list[Path]:
    """The folders that hold `.sql` schemas: each package's own, its `schema` folder and those of its sub-packages."""
    found = []
    for name in packages:
        root = Path(str(resources.files(name)))
        found.extend(folder for folder in (root, root / "schema", *sorted(p for p in root.iterdir() if p.is_dir()))
                     if any(folder.glob("*.sql")))
    return list(dict.fromkeys(found))

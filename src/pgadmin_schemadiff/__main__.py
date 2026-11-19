"""Module entrypoint for ``python -m pgadmin_schemadiff``."""

from pgadmin_schemadiff.cli import main

if __name__ == "__main__":
    raise SystemExit(main())

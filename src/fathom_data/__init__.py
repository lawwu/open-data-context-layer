"""The old name of the `odcl` package. Import `odcl` instead; this alias will be removed.

`Fathom` took a SQLite path. `Catalog` takes a PostgreSQL URL (or reads `ODCL_DATABASE_URL`); bring an old file
across with `odcl import-sqlite PATH`.
"""
import importlib
import sys

from odcl import Catalog as Fathom
from odcl import CatalogError as FathomError
from odcl import __version__

for _name in ("store", "cli", "dbt", "mcp", "legacy"):
    sys.modules[f"{__name__}.{_name}"] = importlib.import_module(f"odcl.{_name}")

__all__ = ["Fathom", "FathomError", "__version__"]

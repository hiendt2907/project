"""Catalogue lệnh chẩn đoán khai báo — xem `command_catalog.py`."""

from pkg.diagnostics.command_catalog import (
    Catalog,
    CatalogError,
    CommandSpec,
    is_path_readable,
    load_catalog,
)

__all__ = ["Catalog", "CatalogError", "CommandSpec", "is_path_readable", "load_catalog"]

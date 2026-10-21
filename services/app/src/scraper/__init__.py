"""Scraper multi-sites (HTTP, sans navigateur).

Paquet principal. L'import du paquet enregistre les adaptateurs de site
disponibles dans `scraper.core.registry`.

    from scraper.core.registry import get_site, available_sites
    get_site("avito")
"""

# l'import enregistre les adaptateurs (avito, tomobila, ...)
from . import sites as _sites  # noqa: F401,E402

__all__ = ["_sites"]

"""Registre des adaptateurs de site.

Usage dans un script tiers :
    from scraper.core.registry import register_site, get_site
    register_site(MonAdaptateur())
    site = get_site("mon-site")
"""

from __future__ import annotations

from typing import Optional

from .site import SiteAdapter

_SITES: dict[str, SiteAdapter] = {}


def register_site(adapter: SiteAdapter) -> SiteAdapter:
    """Enregistre un adaptateur (par son `name`)."""
    if not adapter.name:
        raise ValueError("l'adaptateur doit avoir un `name`")
    _SITES[adapter.name] = adapter
    return adapter


def get_site(name: str) -> SiteAdapter:
    """Retourne l'adaptateur enregistre pour ce nom."""
    if name not in _SITES:
        raise KeyError(
            f"site inconnu: {name!r} (disponibles: {', '.join(sorted(_SITES)) or 'aucun'})")
    return _SITES[name]


def available_sites() -> list[str]:
    return sorted(_SITES)


def site_info() -> list[dict]:
    return [
        {
            "name": s.name,
            "label": s.label or s.name,
            "default_category": s.default_category,
            "categories": s.categories,
        }
        for s in sorted(_SITES.values(), key=lambda x: x.name)
    ]

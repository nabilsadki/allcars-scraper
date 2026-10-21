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
            "modes": getattr(s, "modes", ["list", "rescan", "details"]),
        }
        for s in sorted(_SITES.values(), key=lambda x: x.name)
    ]


def publish(path: str = "/data/sites.json") -> None:
    """Ecrit les metadonnées des sites (pour l'interface web).

    L'interface web tourne dans un autre conteneur : elle ne peut pas
    importer le registre. On publie donc un fichier JSON partage.
    """
    import json
    import os
    import tempfile
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".sites-",
                                   suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"sites": site_info()}, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception:
        pass

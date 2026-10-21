"""Enregistrement des adaptateurs de site.

Pour ajouter un site :
  1. ecrire `scraper/sites/mon_site.py` avec une classe `MonAdapter(SiteAdapter)`
  2. l'importer et l'enregistrer ici
  3. il est dispo immediatement : `app list --site mon_site ...`
"""

from ..core.registry import register_site
from .avito import AvitoAdapter
from .tomobila import TomobilaAdapter

register_site(AvitoAdapter())
register_site(TomobilaAdapter())

__all__ = ["AvitoAdapter", "TomobilaAdapter"]

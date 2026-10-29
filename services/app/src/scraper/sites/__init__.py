"""Enregistrement des adaptateurs de site.

Pour ajouter un site :
  1. ecrire `scraper/sites/mon_site.py` avec une classe `MonAdapter(SiteAdapter)`
  2. l'importer et l'enregistrer ici
  3. il est dispo immediatement : `app list --site mon_site ...`
"""

from ..core.registry import register_site
from .avito import AvitoAdapter
from .globaloccaz import GlobalOccazAdapter
from .kifal import KifalAdapter
from .leguideauto import LeguideautoAdapter
from .moteur import MoteurAdapter
from .otoclic import OtoclicAdapter
from .renew import RenewAdapter
from .siaracash import SiaracashAdapter
from .tomobila import TomobilaAdapter
from .wandaloo import WandalooAdapter

for _adapter in (AvitoAdapter(), TomobilaAdapter(), GlobalOccazAdapter(),
                 KifalAdapter(), RenewAdapter(), OtoclicAdapter(),
                 LeguideautoAdapter(), MoteurAdapter(), SiaracashAdapter(),
                 WandalooAdapter()):
    register_site(_adapter)

__all__ = ["AvitoAdapter", "TomobilaAdapter", "GlobalOccazAdapter",
           "KifalAdapter", "RenewAdapter", "OtoclicAdapter",
           "LeguideautoAdapter", "MoteurAdapter", "SiaracashAdapter", "WandalooAdapter"]

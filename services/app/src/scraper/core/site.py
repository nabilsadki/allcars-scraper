"""Contrat d'un site scrapable.

Pour ajouter un site, on implemente `SiteAdapter` et on l'enregistre dans
`scraper/sites/__init__.py` (ou via `core.registry.register_site`).

Le reste de la chaine (pipeline, base, versionnement, scoring, images, CLI)
est independant du site.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Optional

# Contrat de l'annonce normalisee (un dict, comme partout dans le projet).
# Les cles sont communes a tous les sites ; un site peut en ajouter d'autres
# (elles sont alors stockees dans raw_json / options_json).
AD_FIELDS = [
    "ad_id",          # identifiant interne du site
    "list_id",        # identifiant secondaire (utilise pour le mapping URL)
    "title",
    "description",
    "price",
    "price_str",
    "old_price",
    "discount",
    "category",
    "type",
    "city",
    "area",
    "seller",
    "seller_type",
    "seller_id",
    "phone",
    "list_time",
    "year",
    "mileage_km",
    "gearbox",
    "fuel",
    "brand",
    "model",
    "doors",
    "origin",
    "first_hand",
    "cond",
    "fiscal_power",
    "options",        # dict d'equipements
    "option_labels",  # libelles lisibles des equipements
    "specs",          # specs affichees [{key,label,value}]
    "images",         # [{url,url_full_hd,thumb}]
    "nb_images",
    "is_premium",
    "is_urgent",
    "is_hot_deal",
    "url",
    "details_fetched",
]


class SiteAdapter(ABC):
    """Un site a scraper.

    Attributs
    ---------
    name        : identifiant court du site ("avito", "tomobila"...)
    label       : libelle lisible
    default_category : categorie utilisee par defaut
    """

    name: str = ""
    label: str = ""
    default_category: str = ""
    #: categories communes a tous les sites (utilisees par le CLI)
    categories: list[str] = []

    # ------------------------------------------------------------- a fournir
    @abstractmethod
    def listing_url(self, category: str, offset: int) -> str:
        """URL de la page de resultats pour un decalage donne."""

    @abstractmethod
    def parse_listing(self, html: str, category: str = "") -> list[dict]:
        """Extrait la liste des annonces d'une page de resultats.

        `category` est le chemin demande : il permet a l'adaptateur de
        ecarter les annonces qui n'ont rien a voir (publicites
        sponsorisees d'autres categories).
        """

    @abstractmethod
    def detail_url(self, ad: dict) -> Optional[str]:
        """URL de la fiche d'une annonce (None si non necessaire)."""

    @abstractmethod
    def parse_detail(self, html: str, ad: dict) -> dict:
        """Complete l'annonce avec les donnees de sa fiche."""

    # ------------------------------------------------------------- optionnel
    def total_ads(self, html: str) -> Optional[int]:
        """Nombre total d'annonces du catalogue (si la page l'expose)."""
        return None

    def slices(self, category: str, client=None) -> list[tuple[str, str]]:
        """Decoupe la categorie en tranches : [(libelle, chemin_url), ...].

        Utile quand le site plafonne la pagination : chaque tranche a son
        propre plafond, donc on couvre plus de annonces.
        """
        return [(category, category)]

    def enrich(self, ad: dict, client) -> dict:
        """Enrichissement optionnel (marque/modele via catalogue, etc.)."""
        return ad

    def image_url(self, img: dict, variant: str = "images") -> str:
        """URL d'une image pour une variante de qualite donnee."""
        return img.get("url") or ""

    def image_id(self, url: str) -> str:
        """Identifiant stable d'une image."""
        import os
        import re
        name = os.path.basename(url.split("?")[0])
        return re.sub(r"[^0-9A-Za-z_.-]", "_", name) or "img"


def make_ad(**kwargs) -> dict:
    """Cree une annonce normalisee avec toutes les cles du contrat."""
    ad = {f: None for f in AD_FIELDS}
    ad.update(kwargs)
    ad.setdefault("options", {})
    ad.setdefault("option_labels", [])
    ad.setdefault("specs", [])
    ad.setdefault("images", [])
    ad.setdefault("nb_images", 0)
    ad.setdefault("is_premium", False)
    ad.setdefault("is_urgent", False)
    ad.setdefault("is_hot_deal", False)
    ad.setdefault("details_fetched", False)
    return ad

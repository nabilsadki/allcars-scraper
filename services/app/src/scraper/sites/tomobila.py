"""Adaptateur Tomobila.ma (WordPress).

Second site pour prouver que le pipeline est bien generique : rien ici
n'est specifique a Avito, seule l'extraction change.

Le site est un WordPress classique : les annonces sont dans des cartes
`.stm-single-car-listing-data` (12 par page), la pagination passe par
`/page/N/`.

Les selecteurs viennent de l'ancien projet (voitures-maroc-sy / Crawler.php).
"""

from __future__ import annotations

import re
from typing import Any, Optional

from ..core.site import SiteAdapter, make_ad

BASE = "https://www.tomobila.ma"
LIST_PATH = "/achat-voiture-occasion/"
DETAIL_RE = re.compile(r"/voiture-occasion-ou-neuve/[^/]+-(\d+)/?")


class TomobilaAdapter(SiteAdapter):
    name = "tomobila"
    label = "Tomobila.ma"
    default_category = "achat-voiture-occasion"
    categories = ["achat-voiture-occasion"]
    page_size_fixed = False

    # ------------------------------------------------------------- listing
    def listing_url(self, category: str, offset: int) -> str:
        page = offset // 12 if offset else 0
        cat = category.strip("/")
        if page == 0:
            return f"{BASE}/{cat}/"
        return f"{BASE}/{cat}/page/{page + 1}/"

    def parse_listing(self, html: str, category: str = "") -> list[dict]:
        out = []
        seen = set()
        # chaque carte contient un lien vers /voiture-occasion-ou-neuve/...-<id>/
        for m in re.finditer(
            r'<a href="((?:https://www\.tomobila\.ma)?/voiture-occasion-ou-neuve/[^"]+)"[^>]*>(.*?)</a>',
            html, re.S):
            url, inner = m.group(1), m.group(2)
            if not url.startswith("http"):
                url = "https://www.tomobila.ma" + url
            idm = DETAIL_RE.search(url)
            if not idm:
                continue
            ad_id = idm.group(1)
            if ad_id in seen:
                continue
            seen.add(ad_id)
            title = _clean(_first_text(inner, "labels")) or _slug_title(url)
            ad = make_ad(
                ad_id=ad_id,
                title=title,
                url=url,
                category="Voiture d'occasion",
            )
            ad["_raw_html"] = html
            out.append(ad)
        # prix / ville / kilométrage sont lus dans le texte de la page
        self._enrich_from_html(out, html)
        return out

    def _enrich_from_html(self, ads: list[dict], html: str) -> None:
        """Associe les prix (et quelques champs) aux annonces trouvees."""
        prices = re.findall(r'normal-price"[^>]*>\s*<span[^>]*>([\d\s]+)\s*Dhs', html)
        prices = [int(re.sub(r"\s", "", p)) for p in prices]
        for ad, price in zip(ads, prices):
            ad["price"] = price
            ad["price_str"] = f"{price} DH"

    def total_ads(self, html: str) -> Optional[int]:
        m = re.search(r'([\d\s]+)\s*(?:v|result|annonce)', html, re.I)
        return None

    def enrich(self, ad: dict, client) -> dict:
        """Deduit marque / modele du titre via le referentiel partage.

        Le site ne remplit pas toujours la marque dans sa fiche : on
        complete avec le dictionnaire marques/modeles (conserve en cache).
        """
        if ad.get("brand") and ad.get("model"):
            return ad
        try:
            from ..core.catalog import shared_catalog
            cat = shared_catalog(client)
            if cat is None:
                return ad
            brand, model = cat.parse_brand_model(ad.get("title") or "")
            if brand and not ad.get("brand"):
                ad["brand"] = brand
            if model and not ad.get("model"):
                ad["model"] = model
        except Exception:
            pass
        return ad

    # -------------------------------------------------------------- detail
    def detail_url(self, ad: dict) -> Optional[str]:
        return ad.get("url")

    def parse_detail(self, html: str, ad: dict) -> dict:
        out = dict(ad)
        # titre
        m = re.search(r'<h1[^>]*>(.*?)</h1>', html, re.S)
        if m:
            t = _clean(m.group(1))
            if t and not re.search(r"vente de voitures|occasions ou neuves", t, re.I):
                out["title"] = t
        # prix
        m = re.search(r'normal-price"[^>]*>\s*<span[^>]*>([\d\s]+)\s*Dhs', html)
        if m:
            out["price"] = int(re.sub(r"\s", "", m.group(1)))
            out["price_str"] = f"{out['price']} DH"

        # tableau d'attributs : <td class="label-td"> ... Kilometrage </td>
        #                       <td class="heading-font"> 90000 km </td>
        for label, value in _label_values(html).items():
            key = _ATTRIBUTES.get(label)
            if not key:
                continue
            if key == "mileage_km":
                digits = re.sub(r"[^\d]", "", value)
                if digits:
                    out["mileage_km"] = int(digits)
            elif key == "year":
                m2 = re.search(r"(19[5-9]\d|20[0-2]\d)", value)
                if m2:
                    out["year"] = int(m2.group(1))
            elif key == "fiscal_power":
                m2 = re.search(r"(\d+)\s*CV", value, re.I)
                if m2:
                    out["fiscal_power"] = f"{m2.group(1)} CV"
            else:
                out[key] = value

        # description
        m = re.search(r'<div[^>]*class="[^"]*(?:stm-single-car-content|car-description|'
                      r'stm-car-details-content)[^"]*"[^>]*>(.*?)</div>', html, re.S)
        if m:
            desc = _clean(m.group(1))
            if len(desc) > 30:
                out["description"] = desc
        # images
        imgs = []
        for u in dict.fromkeys(re.findall(
                r'https://www\.tomobila\.ma/wp-content/uploads/[^"\']+?'
                r'\.(?:jpg|jpeg|png|webp)', html)):
            imgs.append({"url": u, "url_full_hd": u, "thumb": u})
        if imgs:
            out["images"] = imgs
            out["nb_images"] = len(imgs)
        return out

    # -------------------------------------------------------------- images
    def image_url(self, img: dict, variant: str = "images") -> str:
        return img.get("url") or ""


# Libelles du site -> cles normalisees
_ATTRIBUTES = {
    "marque": "brand",
    "modèle": "model",     # e accent grave (libelle du site)
    "modéle": "model",     # e accent aigu (libelle du site)
    "modele": "model",
    "mise en circulation": "year",
    "ville": "city",
    "carburant": "fuel",
    "kilometrage": "mileage_km",
    "kilométrage": "mileage_km",
    "annee": "year",
    "année": "year",
    "boite": "gearbox",
    "boîte": "gearbox",
    "boite de vitesses": "gearbox",
    "puissance": "fiscal_power",
    "puissance fiscale": "fiscal_power",
    "cylindree": "engine",
    "cylindrée": "engine",
    "origine": "origin",
    "etat": "cond",
    "état": "cond",
    "nombre de portes": "doors",
    "transmission": "gearbox",
}


def _label_values(html: str) -> dict:
    """Extrait les paires libelle -> valeur du tableau d'attributs."""
    out = {}
    for m in re.finditer(
            r'<td class="label-td"[^>]*>(.*?)</td>\s*<td class="heading-font"[^>]*>(.*?)</td>',
            html, re.S):
        label = _clean(m.group(1)).lower()
        value = _clean(m.group(2))
        if label and value:
            out[label] = value
    return out


# ------------------------------------------------------------------ utils
def _clean(html: str) -> str:
    import html as _html
    text = re.sub(r"<[^>]+>", " ", html or "")
    text = _html.unescape(text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _first_text(html: str, cls: str) -> str:
    m = re.search(rf'class="[^"]*{cls}[^"]*"[^>]*>(.*?)</div>', html, re.S)
    return _clean(m.group(1)) if m else ""


def _slug_title(url: str) -> str:
    slug = url.rstrip("/").split("/")[-1]
    slug = re.sub(r"-\d+$", "", slug)
    return slug.replace("-", " ").strip()

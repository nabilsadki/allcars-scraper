"""Adaptateur Global Occaz (globaloccaz.com).

Le site est un WordPress equipe du plugin "Vehica" : le catalogue est
expose par une API REST qui renvoie TOUTES les voitures en une requete,
avec des attributs types (marque, modele, annee, prix, km, carburant,
boite, equipements, VIN, fiche expertise...).

    https://globaloccaz.com/wp-json/vehica/v1/cars

Les photos sont dans la page de detail (classe vehica-gallery-v3__image).
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

from ..core.site import SiteAdapter, make_ad

BASE = "https://globaloccaz.com"
API = f"{BASE}/wp-json/vehica/v1/cars"

#: libelles des attributs vehica -> cles normalisees
_ATTR_MAP = {
    "marque": "brand",
    "modèle": "model",
    "modele": "model",
    "année": "year",
    "annee": "year",
    "prix": "price",
    "kilométrage": "mileage_km",
    "kilometrage": "mileage_km",
    "carburant": "fuel",
    "transmission": "gearbox",
    "boîte de vitesses": "gearbox",
    "condition": "cond",
    "nombre de portes": "doors",
    "puissance fiscale": "fiscal_power",
    "couleur": "color",
    "catégorie": "category",
    "offre type": "body_type",
    "source type": "source_type",
    "telephone de commercial": "phone",
    "vin": "vin",
    "fiche expertise": "report_url",
    "drive type": "drive_type",
    "engine size": "engine",
}


def _display(attr: dict) -> str:
    """Valeur lisible d'un attribut vehica."""
    val = attr.get("displayValue")
    if val is None:
        val = attr.get("value")
    if isinstance(val, list):
        parts = []
        for v in val:
            if isinstance(v, dict):
                parts.append(str(v.get("name") or ""))
            else:
                parts.append(str(v))
        return ", ".join(p for p in parts if p)
    if isinstance(val, dict):
        return ", ".join(str(v) for v in val.values())
    return "" if val is None else str(val)


def _num(text: str) -> Optional[int]:
    digits = re.sub(r"[^\d]", "", text or "")
    return int(digits) if digits else None


class GlobalOccazAdapter(SiteAdapter):
    name = "globaloccaz"
    label = "Global Occaz"
    default_category = "occasion"
    categories = ["occasion"]
    page_size_fixed = True

    # ------------------------------------------------------------- listing
    def listing_url(self, category: str, offset: int) -> str:
        # l'API renvoie tout le catalogue en une requete
        return API

    def parse_listing(self, html: str, category: str = "") -> list[dict]:
        try:
            data = json.loads(html)
        except Exception:
            return []
        out = []
        for car in (data.get("results") or []):
            out.append(self._normalize(car))
        return out

    def _normalize(self, car: dict) -> dict:
        ad = make_ad(
            ad_id=car.get("id"),
            title=car.get("name"),
            url=car.get("url"),
            description=car.get("description") or None,
        )
        specs = []
        options = {}
        option_labels = []
        for attr in (car.get("attributes") or []):
            label = (attr.get("name") or "").strip()
            value = _display(attr)
            if not label:
                continue
            specs.append({"key": label, "label": label, "value": value})
            key = _ATTR_MAP.get(label.lower())
            if key is None:
                continue
            if key == "price":
                ad["price"] = _num(value)
                ad["price_str"] = value.strip()
            elif key == "mileage_km":
                ad["mileage_km"] = _num(value)
            elif key == "year":
                ad["year"] = _num(value)
            elif key == "fuel":
                ad["fuel"] = value
            elif key == "gearbox":
                ad["gearbox"] = value
            elif key == "cond":
                ad["cond"] = value
            elif key == "doors":
                ad["doors"] = _num(value)
            elif key == "phone":
                ad["phone"] = value.strip() or None
            elif key == "brand":
                ad["brand"] = value or None
            elif key == "model":
                ad["model"] = value or None
            elif key == "fiscal_power":
                ad["fiscal_power"] = value
            elif key == "vin":
                options["vin"] = value
                option_labels.append(f"VIN {value}")
            elif key == "report_url":
                options["fiche_expertise"] = value
                option_labels.append("Fiche expertise")
            elif key == "color":
                options["couleur"] = value
                option_labels.append(f"Couleur {value}")
            elif key == "body_type":
                options["carrosserie"] = value
                out["body_type"] = value
            elif key == "source_type":
                options["type_offre"] = value
        # le titre contient souvent "MARQUE MODELE ..." : on complete
        if not ad.get("brand") and ad.get("title"):
            parts = ad["title"].split()
            if parts:
                ad["brand"] = parts[0].title()
        ad["specs"] = specs
        ad["options"] = options
        ad["option_labels"] = option_labels
        ad["category"] = "Voiture d'occasion"
        return ad

    def total_ads(self, html: str) -> Optional[int]:
        try:
            return int(json.loads(html).get("resultsCount") or 0)
        except Exception:
            return None

    # -------------------------------------------------------------- detail
    def detail_url(self, ad: dict) -> Optional[str]:
        return ad.get("url")

    def parse_detail(self, html: str, ad: dict) -> dict:
        out = dict(ad)
        images = []
        for url in re.findall(
                r'<img[^>]+class="[^"]*vehica-gallery[^"]*"[^>]+src="([^"]+)"', html):
            images.append({"url": url, "url_full_hd": url, "thumb": url})
        if not images:
            for url in re.findall(r'https://globaloccaz\.com/wp-content/uploads/[^"\']+?'
                                  r'\.(?:jpg|jpeg|png|webp)', html):
                if url not in [i["url"] for i in images]:
                    images.append({"url": url, "url_full_hd": url, "thumb": url})
        if images:
            out["images"] = images
            out["nb_images"] = len(images)
        # date de publication (JSON-LD)
        m = re.search(r'"datePublished"\s*:\s*"(\d{4}-\d{2}-\d{2})', html)
        if m:
            out["publish_date"] = m.group(1)
        # description
        m = re.search(r'<div[^>]+class="[^"]*vehica-vehicle-description[^"]*"[^>]*>(.*?)</div>',
                      html, re.S)
        if m:
            desc = re.sub(r"<[^>]+>", " ", m.group(1))
            desc = re.sub(r"\s+", " ", desc).strip()
            if len(desc) > 20:
                out["description"] = desc
        return out

    # -------------------------------------------------------------- images
    def image_url(self, img: dict, variant: str = "images") -> str:
        return img.get("url") or ""

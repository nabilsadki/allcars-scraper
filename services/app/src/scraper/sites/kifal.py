"""Adaptateur Occasion Kifal (occasion.kifal.ma).

Le site encode les donnees dans le nom du fichier de l'URL :

    /annonce/MARQUE_MODELE_ANNEE_CARBURANT_BOITE_VILLE_ID_REF.htm
    /annonce/MERCEDES-BENZ_Classe%20C_2019_Diesel_Automatique_RABAT_7767_VEH00013PM.htm

Les cartes utilisent aussi les microdata schema.org (price,
mileageFromOdometer, productionYear, fuelType, image).
"""

from __future__ import annotations

import re
from typing import Optional
from urllib.parse import unquote

from ..core.site import SiteAdapter, make_ad

BASE = "https://occasion.kifal.ma"
LISTING = f"{BASE}/annonces"

FUELS = ("diesel", "essence", "electrique", "hybride", "gaz", "petrole", "lpg")
GEARBOX = ("automatique", "manuelle", "bva", "bvm", "mecanique", "automatique/electrique")

def _num(text: str) -> Optional[int]:
    digits = re.sub(r"[^\d]", "", text or "")
    return int(digits) if digits else None


_CARD_RE = re.compile(r'<div class="card[^"]*card-annonce"[^>]*data-url="([^"]+)"', re.S)


class KifalAdapter(SiteAdapter):
    name = "kifal"
    label = "Occasion Kifal"
    default_category = "voitures-occasion"
    categories = ["voitures-occasion"]
    page_size_fixed = True

    # ------------------------------------------------------------- listing
    def listing_url(self, category: str, offset: int) -> str:
        page = offset // 35 + 1 if offset else 1
        return f"{LISTING}?page={page}"

    def parse_listing(self, html: str, category: str = "") -> list[dict]:
        out = []
        seen = set()
        for m in _CARD_RE.finditer(html):
            data_url = m.group(1)
            if data_url in seen:
                continue
            seen.add(data_url)
            # le fragment de la carte va jusqu'a la suivante
            start = m.start()
            nxt = html.find('card-annonce"', m.end())
            frag = html[start:nxt if nxt > 0 else len(html)]
            ad = self._normalize(data_url, frag)
            if ad:
                out.append(ad)
        return out

    def _normalize(self, data_url: str, frag: str) -> Optional[dict]:
        url = data_url if data_url.startswith("http") else f"{BASE}/annonce/{data_url}"
        parts = unquote(data_url).replace(".htm", "").split("_")
        if len(parts) < 4:
            return None
        brand = parts[0].strip()
        # l'annee : le premier element numerique sur 4 chiffres
        yi = next((i for i, p in enumerate(parts) if re.fullmatch(r"(?:19|20)\d{2}", p)), None)
        if yi is None:
            year = None
            model = " ".join(parts[1:2])
            tail = parts[2:]
        else:
            year = int(parts[yi])
            model = " ".join(parts[1:yi]).strip()
            tail = parts[yi + 1:]
        # carburant / boite / ville dans l'ordre
        fuel = gearbox = city = None
        if tail:
            fuel = tail[0].strip() if tail else None
        if len(tail) > 1:
            gearbox = tail[1].strip()
        if len(tail) > 2:
            city = " ".join(tail[2:]).strip()
        if fuel and fuel.lower() not in FUELS:
            if gearbox and gearbox.lower() in FUELS:
                fuel, gearbox = gearbox, None
            else:
                gearbox, city = fuel, None
        price = self._price(frag)
        km = self._km(frag)
        title = self._title(frag) or f"{brand} {model}".strip()
        ad = make_ad(
            ad_id=self._ad_id(data_url),
            title=title,
            url=url,
            price=price,
            price_str=(f"{price:,}".replace(",", " ") + " DH") if price else None,
            mileage_km=km,
            year=year,
            brand=brand.title() if brand else None,
            model=model.title() if model else None,
            fuel=fuel.title() if fuel else None,
            gearbox=gearbox.title() if gearbox else None,
            city=city.title() if city else None,
            category="Voiture d'occasion",
        )
        ad["list_id"] = self._ad_id(data_url)
        # description courte (la version du vehicule)
        m = re.search(r'<div class="mb-0 leading-tight mb-3">(.*?)</div>', frag, re.S)
        if m:
            desc = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", m.group(1))).strip()
            if len(desc) > 3:
                ad["description"] = desc
        # image
        m = re.search(r'<img[^>]+src="([^"]+)"[^>]*class="cover-image"', frag)
        if not m:
            m = re.search(r'<span class="firstImage"><img src="([^"]+)"', frag)
        if m:
            img = m.group(1)
            ad["images"] = [{"url": img, "url_full_hd": img, "thumb": img}]
            ad["nb_images"] = 1
        return ad

    @staticmethod
    def _ad_id(data_url: str) -> str:
        m = re.search(r"_(\d+)_[A-Z0-9]+\.htm$", data_url)
        return m.group(1) if m else data_url.split("_")[-1].replace(".htm", "")

    @staticmethod
    def _price(frag: str) -> Optional[int]:
        """Prix : le nombre suivi de DH/MAD dans la carte."""
        for pat in (r'<b[^>]*>([\d\s ]+)</b>\s*(?:Dh|DH|MAD|dhs)',
                    r"([\d][\d\s .]*)\s*(?:DH|MAD|Dhs|dhs)",
                    r'"price"\s*:\s*"?([\d\s ]+)',
                    r'data-price="([\d\s ]+)"'):
            m = re.search(pat, frag)
            if m:
                digits = re.sub(r"[^\d]", "", m.group(1))
                if digits:
                    return int(digits)
        return None

    @staticmethod
    def _km(frag: str) -> Optional[int]:
        m = re.search(r"([\d\s .]+)\s*(?:KM|km|Km)\b", frag)
        if not m:
            return None
        digits = re.sub(r"[^\d]", "", m.group(1))
        return int(digits) if digits else None

    @staticmethod
    def _title(frag: str) -> str:
        m = re.search(r'title-card-full-annonce">(.*?)</div>', frag, re.S)
        if not m:
            m = re.search(r'aria-label="([^"]+)"', frag)
        return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", m.group(1))).strip() if m else ""

    def total_ads(self, html: str) -> Optional[int]:
        m = re.search(r'total-card-annonces[^>]*>\s*<span[^>]*>([\d\s]+)</span>', html)
        if not m:
            m = re.search(r'([\d\s]+)\s*Annonces', html)
        if not m:
            return None
        digits = re.sub(r"[^\d]", "", m.group(1))
        return int(digits) if digits else None

    # -------------------------------------------------------------- detail
    def detail_url(self, ad: dict) -> Optional[str]:
        return ad.get("url")

    def parse_detail(self, html: str, ad: dict) -> dict:
        out = dict(ad)
        images = []
        for url in re.findall(r'https?://[^"\']*execute-api[^"\']*', html):
            if url not in images:
                images.append(url)
        if not images:
            for url in re.findall(r'<img[^>]+src="(https?://[^"]+\.(?:jpg|jpeg|png|webp)[^"]*)"', html):
                if url not in images:
                    images.append(url)
        if images:
            out["images"] = [{"url": u, "url_full_hd": u, "thumb": u} for u in images[:40]]
            out["nb_images"] = len(out["images"])
        m = re.search(r'<div[^>]+class="[^"]*(?:description|desc-detail)[^"]*"[^>]*>(.*?)</div>',
                      html, re.S)
        if m:
            desc = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", m.group(1))).strip()
            if len(desc) > 20:
                out["description"] = desc
        # tableau d'attributs : libelle -> valeur
        for label, key, cast in (("Kilom\u00e9trage", "mileage_km", _num),
                                 ("Ann\u00e9e", "year", _num),
                                 ("Puissance fiscale", "fiscal_power", None),
                                 ("Carburant", "fuel", None),
                                 ("Bo\u00eete", "gearbox", None),
                                 ("Ville", "city", None)):
            m = re.search(rf"{label}[^<]*</[^>]+>\s*(?:<[^>]+>\s*)*([^<]+)", html)
            if not m:
                continue
            value = re.sub(r"\s+", " ", m.group(1)).strip()
            if not value:
                continue
            if cast:
                val = cast(value)
                if val:
                    out[key] = val
            else:
                out[key] = value
        # telephone
        m = re.search(r'(?:tel|phone|telephone)[^\d]{0,20}(0\d{8,9})', html, re.I)
        if m:
            out["phone"] = m.group(1)
        return out

    # -------------------------------------------------------------- images
    def image_url(self, img: dict, variant: str = "images") -> str:
        return img.get("url") or ""

"""Adaptateur SiaraCash (siaracash.ma).

Site Next.js (App Router) : la liste des annonces est exposee dans un bloc
JSON-LD `ItemList` (URL + titre), et chaque fiche details expose un schema
org `Vehicle` complet (marque, modele, annee, km, carburant, boite, prix...).

Pagination simple : /voiture-occasion-maroc?page=N (environ 12 annonces par page).
"""

from __future__ import annotations

import html as _html
import json
import re
from typing import Optional
from urllib.parse import unquote

from ..core.site import SiteAdapter, make_ad

BASE = "https://siaracash.ma"
LISTING = f"{BASE}/voiture-occasion-maroc"

# telephone expose sur toutes les fiches (degavail)
DEFAULT_PHONE = "+212522918796"


def _num(text: str) -> Optional[int]:
    digits = re.sub(r"[^\d]", "", text or "")
    return int(digits) if digits else None


def _price(text: str) -> Optional[int]:
    """Parse "270\u202f000 DH" -> 270000."""
    if not text:
        return None
    m = re.search(r"(\d[\d\s\u202f\u00a0]+)", text)
    if not m:
        return None
    digits = re.sub(r"[^\d]", "", m.group(1))
    return int(digits) if digits else None


def _int(s) -> Optional[int]:
    if isinstance(s, (int, float)):
        return int(s)
    if isinstance(s, str):
        return _num(s)
    return None


class SiaracashAdapter(SiteAdapter):
    name = "siaracash"
    label = "SiaraCash.ma"
    default_category = "voiture-occasion-maroc"
    categories = ["voiture-occasion-maroc"]
    modes = ["list", "rescan", "details"]

    # ------------------------------------------------------------- listing
    def reset_pagination(self) -> None:
        self._page = 0

    def listing_url(self, category: str, offset: int) -> str:
        self._page = getattr(self, "_page", 0) + 1
        return LISTING if self._page <= 1 else f"{LISTING}?page={self._page}"

    def total_ads(self, html: str) -> Optional[int]:
        m = re.search(r"d.?annonces[^<]*?(\d+)\s*v.{1,2}hicule", html, re.I)
        if not m:
            m = re.search(r"(\d+)\s*v.\s*hicules?[^<]*?(?:au\s*Maroc|occasion)", html, re.I)
        if m:
            try:
                return int(m.group(1))
            except ValueError:
                pass
        # filet : "253 véhicules" n'importe ou, si le titre l'annonce
        m = re.search(r"(\d+)\s*véhicule", html)
        if m:
            try:
                return int(m.group(1))
            except ValueError:
                pass
        return None

    def parse_listing(self, html: str, category: str = "") -> list[dict]:
        out = []
        seen = set()
        for m in re.finditer(
                r'"@type"\s*:\s*"ListItem"\s*,[^]]*?"url"\s*:\s*"(https://siaracash\.ma/voiture/[^"]+)"\s*,[^]]*?"name"\s*:\s*"([^"]*)"', html, re.S):
            url = m.group(1).rstrip()
            title = _html.unescape(m.group(2))
            ad_id = url.rstrip("/").split("/")[-1]
            if not ad_id or ad_id in seen:
                continue
            seen.add(ad_id)
            brand = model = None
            parts = title.split()
            if parts:
                brand = parts[0]
                # annee en dernier token si numerique
                if len(parts) >= 2 and re.fullmatch(r"(?:19|20)\d{2}", parts[-1]):
                    year = int(parts[-1])
                else:
                    year = None
                # model : reste sans la marque (et l'annee si presente)
                tail = parts[1:]
                if year is not None and tail and tail and re.fullmatch(r"(?:19|20)\d{2}", tail[-1]):
                    tail = tail[:-1]
                model = " ".join(tail) if tail else None
            ad = make_ad(
                ad_id=ad_id,
                list_id=None,
                title=title or None,
                url=url,
                brand=brand.title() if brand else None,
                model=model.title() if model else None,
                year=year if "year" in locals() else None,
                category="Voiture d'occasion",
                seller="SiaraCash",
                seller_type="Pro",
                phone=DEFAULT_PHONE,
            )
            out.append(ad)
        return out

    # ------------------------------------------------------------- details
    def detail_url(self, ad: dict) -> Optional[str]:
        return ad.get("url")

    def parse_detail(self, html: str, ad: dict) -> dict:
        # 1. schema.org Vehicle
        m = re.search(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', html, re.S)
        ld = {}
        if m:
            try:
                data = json.loads(m.group(1))
                if isinstance(data, list):
                    data = data[0]
                if isinstance(data, dict) and data.get("@type") == "Vehicle":
                    ld = data
            except (json.JSONDecodeError, TypeError):
                ld = {}
        # si plusieurs blocs JSON-LD, prendre celui Vehicle
        if not ld:
            for mm in re.finditer(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', html, re.S):
                try:
                    data = json.loads(mm.group(1))
                    if isinstance(data, dict) and data.get("@type") == "Vehicle":
                        ld = data
                        break
                except (json.JSONDecodeError, TypeError):
                    pass

        merged = {}

        name = ld.get("name")
        if name:
            merged["title"] = _html.unescape(name)

        brand = ld.get("brand")
        if isinstance(brand, dict):
            brand = brand.get("name")
        if brand:
            merged["brand"] = brand

        if ld.get("model"):
            merged["model"] = ld["model"]

        if ld.get("vehicleModelDate"):
            y = _num(str(ld.get("vehicleModelDate")))
            if y:
                merged["year"] = y

        mileage = ld.get("mileageFromOdometer")
        if isinstance(mileage, dict):
            mileage = mileage.get("value")
        if mileage is not None:
            merged["mileage_km"] = _int(mileage)

        if ld.get("fuelType"):
            merged["fuel"] = ld["fuelType"]

        if ld.get("vehicleTransmission"):
            merged["vehicleTransmission"] = ld["vehicleTransmission"]
            merged["gearbox"] = ld["vehicleTransmission"]

        if ld.get("bodyType"):
            merged["body_type"] = ld["bodyType"]
            merged["category"] = ld["bodyType"]

        if ld.get("color"):
            merged["colour"] = ld["color"]
            merged["color"] = ld["color"]

        offers = ld.get("offers") if isinstance(ld.get("offers"), dict) else None
        if isinstance(offers, dict):
            price = _price(str(offers.get("price")))
            if price is not None:
                merged["price"] = price
                merged["price_str"] = f"{price:,}".replace(",", " ") + " DH"
            currency = offers.get("priceCurrency")
            if currency:
                merged["currency"] = currency

        imgs = ld.get("image")
        if isinstance(imgs, str):
            imgs = [imgs]
        if isinstance(imgs, list) and imgs:
            images = [{"url": u, "url_full_hd": u, "thumb": u} for u in imgs
                      if isinstance(u, str)]
            if images:
                merged["images"] = images
                merged["nb_images"] = len(images)

        # 2. bloc cle-valeur de la page (specs detaillees)
        kvs = re.findall(
            r'<span[^>]*class="[^"]*text-\[var\(--cc-muted\)\][^"]*"[^>]*>([^<]+)</span>'
            r'\s*<span[^>]*class="[^"]*font-semibold[^"]*"[^>]*>([^<]+)</span>',
            html, re.S)
        specs = {}
        for k, v in kvs:
            k = _html.unescape(k).strip()
            v = _html.unescape(v).strip()
            if k and v and k not in specs:
                specs[k] = v

        def g(key):
            # recherche insensible a la casse et aux accents approx
            for kk, vv in specs.items():
                if kk.strip().lower() == key.strip().lower():
                    return vv
            return None

        if g("Marque"):
            merged.setdefault("brand", g("Marque"))
        if g("Modèle"):
            merged.setdefault("model", g("Modèle"))
        if g("Finition") and merged.get("title"):
            pass
        if g("Année"):
            y = _num(g("Année"))
            if y:
                merged["year"] = y
        if g("Kilométrage"):
            merged["mileage_km"] = _num(g("Kilométrage"))
        if g("Carburant"):
            merged["fuel"] = g("Carburant")
        if g("Boîte"):
            merged["gearbox"] = g("Boîte")
        if g("Carrosserie"):
            merged.setdefault("body_type", g("Carrosserie"))
        if g("Couleur"):
            merged["colour"] = g("Couleur")
        if g("Puissance fiscale"):
            fp = _num(g("Puissance fiscale"))
            if fp is not None:
                merged["fiscal_power"] = fp
        if g("Portes"):
            merged["doors"] = _num(g("Portes"))
        if g("Places"):
            merged["seats"] = _num(g("Places"))
        if g("État"):
            merged["cond"] = g("État")
        if g("Origine"):
            merged["origin"] = g("Origine")
        if g("Ville"):
            merged["city"] = g("Ville")
        if g("Prix du véhicule"):
            p = _price(g("Prix du véhicule"))
            if p is not None:
                merged["price"] = p
                merged["price_str"] = f"{p:,}".replace(",", " ") + " DH"

        # premier main: detection "Xème main"
        main_m = re.search(r"(\d)(?:er|ème|eme)\s*main", html, re.I)
        if main_m:
            merged["first_hand"] = int(main_m.group(1)) == 1

        ad_id = ad.get("ad_id")
        if ad_id:
            merged["ad_id"] = ad_id
        merged["url"] = ld.get("url") or ad.get("url")
        merged.setdefault("title", ad.get("title"))
        merged.setdefault("brand", ad.get("brand"))
        merged.setdefault("model", ad.get("model"))
        merged.setdefault("year", ad.get("year"))
        merged.setdefault("seller", "SiaraCash")
        merged.setdefault("seller_type", "Pro")
        merged.setdefault("phone", DEFAULT_PHONE)
        merged["details_fetched"] = True
        return merged

    def image_url(self, img: dict, variant: str = "images") -> str:
        return img.get("url") or img.get("url_full_hd") or ""

    def image_id(self, url: str) -> str:
        import os
        import re as _re
        name = os.path.basename(url.split("?")[0])
        return _re.sub(r"[^0-9A-Za-z_.-]", "_", name) or "img"

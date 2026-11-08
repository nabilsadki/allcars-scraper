"""Adaptateur Auto Reda (autoreda.ma).

Site WordPress equipe du plugin "Vehica" : le catalogue est expose par une
API REST (wp-json/vehica/v1/cars) qui renvoie l'ensemble des voitures en
une seule requete.
"""

from __future__ import annotations

import html as _html
import json
import re
from typing import Any, Optional

from ..core.site import SiteAdapter, make_ad

BASE = "https://autoreda.ma"
API = f"{BASE}/wp-json/vehica/v1/cars"


def _kc(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()


def _display(attr: dict) -> str:
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


def _price(text: str) -> Optional[int]:
    if not text:
        return None
    # "450 000.00DH" -> 450000 (garde la partie entiere)
    m = re.search(r"([\d\s]+)(?:[.,]\d+)?", text)
    if not m:
        return None
    digits = re.sub(r"\s+", "", m.group(1))
    return int(digits) if digits else None


def _num(text: str) -> Optional[int]:
    digits = re.sub(r"[^\d]", "", text or "")
    return int(digits) if digits else None


class AutoredaAdapter(SiteAdapter):
    name = "autoreda"
    label = "Auto Reda"
    default_category = "voitures"
    categories = ["voitures"]
    modes = ["list", "rescan", "details"]

    # ------------------------------------------------------------- listing
    def listing_url(self, category: str, offset: int) -> str:
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
        attrs = {}
        for attr in (car.get("attributes") or []):
            label = (attr.get("name") or "").strip()
            if label:
                attrs[label.lower()] = _display(attr)

        title = car.get("name") or ""
        title = _html.unescape(title)

        year = None
        y = attrs.get("année") or attrs.get("annee")
        if y:
            ym = re.search(r"\d{4}", y)
            if ym:
                year = int(ym.group(0))

        price_str = attrs.get("prix")
        price = _price(price_str) if price_str else None

        ad = make_ad(
            ad_id=car.get("id"),
            title=title,
            url=car.get("url"),
            price=price,
            price_str=price_str,
            year=year,
            mileage_km=_num(attrs.get("kilométrage") or attrs.get("kilometrage")),
            brand=attrs.get("marque") or None,
            model=attrs.get("modèle") or attrs.get("modele") or None,
            fuel=attrs.get("carburant") or None,
            gearbox=attrs.get("transmission") or attrs.get("boîte de vitesses") or None,
            fiscal_power=_num(attrs.get("puissance fiscale")),
            city=None,
            seller_type="Professionnel",
            seller=car.get("user", {}).get("name") if isinstance(car.get("user"), dict) else None,
            seller_id=car.get("user", {}).get("id") if isinstance(car.get("user"), dict) else None,
            category="Voiture d'occasion",
            description=car.get("description"),
        )
        options = []
        opt_raw = attrs.get("option")
        if opt_raw:
            options = [o.strip() for o in opt_raw.split(",") if o.strip()]
        if options:
            ad["option_labels"] = options
            ad["options"] = {o: True for o in options}
        # seller_type from condition maybe
        ad.setdefault("seller_type", "Professionnel")
        return ad

    # ------------------------------------------------------------- details
    def detail_url(self, ad: dict) -> Optional[str]:
        return ad.get("url")

    def parse_detail(self, html: str, ad: dict) -> dict:
        merged = {}
        h1 = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.S)
        if h1:
            merged["title"] = _kc(_html.unescape(re.sub(r"<[^>]+>", " ", h1.group(1))))
        title = merged.get("title") or ad.get("title")
        if title:
            mm = re.search(r"(\d{4})\s*[-–]\s*(\d{4})", title)
            if mm:
                merged.setdefault("year", int(mm.group(1)))
        # description short
        desc_m = re.search(r"<p[^>]*>(.*?)</p>", html, re.S)
        if desc_m:
            merged.setdefault("description", _kc(re.sub(r"<[^>]+>", " ", desc_m.group(1))))
        # images
        imgs = sorted({u.split("?")[0] for u in re.findall(r"https://autoreda\.ma/wp-content/uploads/[^\"]+\.(?:jpe?g|png|webp)", html)})
        full = [u for u in imgs if not re.search(r"-\d+x\d+\.", u)]
        if full:
            merged["images"] = [{"url": u, "url_full_hd": u, "thumb": u} for u in full]
            merged["nb_images"] = len(full)
        elif imgs:
            merged["images"] = [{"url": u, "url_full_hd": u, "thumb": u} for u in imgs[:10]]
            merged["nb_images"] = len(merged["images"])
        merged["details_fetched"] = True
        merged["ad_id"] = ad.get("ad_id")
        merged["url"] = ad.get("url") or merged.get("url")
        merged.setdefault("seller", ad.get("seller"))
        merged.setdefault("seller_id", ad.get("seller_id"))
        merged.setdefault("seller_type", "Professionnel")
        return merged

    def clean_images(self, images, ad=None):
        out = []
        for img in super().clean_images(images, ad):
            u = img.get("url") or ""
            # logo de l'organisation (JSON-LD) et visuel "Sans-titre"
            if "db70d53a-ff2c" in u or "/Sans-titre-" in u:
                continue
            out.append(img)
        return out

    def image_url(self, img, variant="images"):
        return img.get("url") or img.get("url_full_hd") or ""

    def image_id(self, url):
        import os, re as _re
        name = os.path.basename(url.split("?")[0])
        return _re.sub(r"[^0-9A-Za-z_.-]", "_", name) or "img"

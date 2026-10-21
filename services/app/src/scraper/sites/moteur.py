"""Adaptateur Moteur.ma (moteur.ma).

Site PHP sur mesure (Blade/Laravel). La liste des annonces est rendue cote
serveur dans des cartes `ads-index-card` :

    <div class="card ... ads-index-card">
      <a href=".../detail-annonce/707810/land-rover-range-rover-sport.html">
      <img src="..." class="cover-image ads-index-media-img">
      <h5 class="... ads-index-title"> Range Rover Sport </h5>
      <span class="timeago" data-time="2026-10-21 23:43:58">il y a 2 minutes</span>
      <h4 class="... ad-price-grid"> Appeler pour le prix </h4>
      <p class="... ad-desc"> ... </p>
      <div class="ad-meta">
        <span><i class="fa fa-calendar"></i> 2023</span>
        <span><i class="fa fa-cog"></i> Automatique</span>
        <span><i class="fa fa-tachometer"></i> Essence</span>
        <span><i class="fa fa-road"></i> 38,000 km</span>

La page detail contient un bloc "Informations Vehicule" (marque, modele,
annee, km, motorisation, transmission, carrosserie, couleur, portes...),
la galerie (`ad-gallery-slide`) et le telephone (dans le script de la page).
"""

from __future__ import annotations

import re
from typing import Optional

from ..core.site import SiteAdapter, make_ad

BASE = "https://www.moteur.ma"
LISTING = f"{BASE}/fr/voiture/achat-voiture-occasion/"

#: annonces par page (observe sur le site)
PAGE_SIZE = 30

_CARD_RE = re.compile(r'<div class="card[^"]*ads-index-card"[^>]*>(.*?)(?=<div class="ad-col|</section>)',
                      re.S)
_AD_URL_RE = re.compile(
    r'href="(https://www\.moteur\.ma/[^"]*detail-annonce/(\d+)/[^"]*)"')


def _num(text: str) -> Optional[int]:
    digits = re.sub(r"[^\d]", "", text or "")
    return int(digits) if digits else None


def _text(frag: str, pattern: str) -> Optional[str]:
    m = re.search(pattern, frag, re.S)
    if not m:
        return None
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", m.group(1))).strip()


def _icon_value(frag: str, icon: str) -> Optional[str]:
    """Valeur associee a une icone Font Awesome (fa-calendar, fa-cog...).

    Attention : la carte contient aussi "fa-calendar-o" (date de
    publication) : le motif exclut les variantes suffixees (-o, -r...).
    """
    m = re.search(rf'<i class="fa {icon}(?![\w-])[^"]*"[^>]*>\s*</i>\s*([^<]*)', frag)
    if not m:
        return None
    return re.sub(r"\s+", " ", m.group(1)).strip() or None


class MoteurAdapter(SiteAdapter):
    name = "moteur"
    label = "Moteur.ma"
    default_category = "achat-voiture-occasion"
    categories = ["achat-voiture-occasion"]
    page_size_fixed = True

    # ------------------------------------------------------------- listing
    def reset_pagination(self) -> None:
        self._page = 0

    def listing_url(self, category: str, offset: int) -> str:
        # pagination par query : ?page=N (30 annonces par page)
        self._page = getattr(self, "_page", 0) + 1
        return LISTING if self._page <= 1 else f"{LISTING}?page={self._page}"

    def parse_listing(self, html: str, category: str = "") -> list[dict]:
        out = []
        seen = set()
        for m in _CARD_RE.finditer(html):
            ad = self._normalize(m.group(1))
            if ad and ad["ad_id"] not in seen:
                seen.add(ad["ad_id"])
                out.append(ad)
        return out

    def _normalize(self, frag: str) -> Optional[dict]:
        um = _AD_URL_RE.search(frag)
        if not um:
            return None
        url, ad_id = um.group(1), um.group(2)
        title = _text(frag, r'class="[^"]*ads-index-title[^"]*"[^>]*>(.*?)</h5>')
        city = _text(frag, r'fa-map-marker[^"]*"[^>]*>\s*</i>\s*([^<]+)</a>')
        date = _text(frag, r'class="timeago"[^>]*data-time="([^"]+)"')
        price = _text(frag, r'class="[^"]*ad-price-grid[^"]*"[^>]*>(.*?)</h4>')
        desc = _text(frag, r'class="[^"]*ad-desc[^"]*"[^>]*>(.*?)</p>')
        img = _text(frag, r'<img[^>]+src="([^"]+)"[^>]*class="[^"]*ads-index-media-img')
        if not img:
            img = _text(frag, r'class="[^"]*ads-index-media-img[^"]*"[^>]*src="([^"]+)"')
        year = _icon_value(frag, "fa-calendar")
        gearbox = _icon_value(frag, "fa-cog")
        fuel = _icon_value(frag, "fa-tachometer")
        km = _icon_value(frag, "fa-road")

        price_num = None
        if price and re.search(r"\d", price):
            price_num = _num(price)
        ad = make_ad(
            ad_id=ad_id,
            title=title or None,
            url=url,
            price=price_num,
            price_str=price if price_num else None,
            mileage_km=_num(km),
            year=_num(year),
            gearbox=gearbox,
            fuel=fuel,
            city=city or None,
            description=desc or None,
            category="Voiture d'occasion",
        )
        if date:
            ad["publish_date"] = date.strip()
        if img:
            ad["images"] = [{"url": img, "url_full_hd": img, "thumb": img}]
            ad["nb_images"] = 1
        return ad

    def total_ads(self, html: str) -> Optional[int]:
        """Total : "Affichage 1 a 30 sur 124814 annonces"."""
        m = re.search(r"Affichage\s+\d+\s*à\s*\d+\s+sur\s+([\d\s]+)\s+annonces", html)
        if not m:
            m = re.search(r"sur\s+([\d\s]+)\s+annonces", html)
        if not m:
            return None
        return _num(m.group(1))

    # -------------------------------------------------------------- detail
    def detail_url(self, ad: dict) -> Optional[str]:
        return ad.get("url")

    def parse_detail(self, html: str, ad: dict) -> dict:
        out = dict(ad)
        # titre
        title = _text(html, r'class="[^"]*ad-hero-title[^"]*"[^>]*>(.*?)</h1>')
        if title:
            out["title"] = title
        # prix
        price = _text(html, r'class="[^"]*ad-hero-price[^"]*"[^>]*>(.*?)</div>')
        if not price:
            price = _text(html, r'class="[^"]*ad-hero-call-box[^"]*"[^>]*>(.*?)</button>')
        if price and re.search(r"\d", price):
            out["price"] = _num(price)
            out["price_str"] = price
        # informations vehicule : "Marque: Land Rover" ...
        specs = []
        for label, key, cast in (
                ("Marque", "brand", None), ("Modèle", "model", None),
                ("Année", "year", _num), ("Kilométrage", "mileage_km", _num),
                ("Motorisation", "fuel", None), ("Transmission", "gearbox", None),
                ("Carrosserie", "body", None), ("Couleur", "color", None),
                ("Nombre de portes", "doors", _num),
                ("Puissance fiscale", "fiscal_power", None),
                ("Version", "version", None)):
            # structure : <td><span class="font-weight-bold">Marque:</span></td>
            #             <td>Land Rover</td>
            m = re.search(rf"{label}\s*:?\s*</[^>]+>\s*(?:</[^>]+>\s*)*"
                          rf"<td[^>]*>\s*([^<]+)", html)
            if not m:
                continue
            value = re.sub(r"\s+", " ", m.group(1)).strip()
            if not value or value.upper() == "N/A":
                continue
            specs.append({"key": key or label, "label": label, "value": value})
            if key == "brand":
                out["brand"] = value
            elif key == "model":
                out["model"] = value
            elif key == "year":
                out["year"] = cast(value)
            elif key == "mileage_km":
                out["mileage_km"] = cast(value)
            elif key == "fuel":
                out["fuel"] = value
            elif key == "gearbox":
                out["gearbox"] = value
            elif key == "doors":
                out["doors"] = cast(value)
            elif key == "fiscal_power":
                out["fiscal_power"] = value
        if specs:
            out["specs"] = specs
            labels = [f"{s['label']} : {s['value']}" for s in specs]
            out["option_labels"] = labels
            out["options"] = {s["key"]: s["value"] for s in specs}
        # galerie
        images = []
        for url in re.findall(r'class="[^"]*ad-gallery-slide[^"]*"[^>]*>\s*<img[^>]+src="([^"]+)"',
                              html):
            if url not in images:
                images.append(url)
        for url in re.findall(r'class="[^"]*ad-gallery-slide[^"]*"[^>]*>\s*<img[^>]+data-src="([^"]+)"',
                              html):
            if url not in images:
                images.append(url)
        if images:
            out["images"] = [{"url": u, "url_full_hd": u, "thumb": u} for u in images]
            out["nb_images"] = len(images)
        # telephone : present dans le script de la page (tel:06XXXXXXXX)
        m = re.search(r"tel:(0\d{8,9})", html)
        if m:
            out["phone"] = m.group(1)
        # description
        desc = _text(html, r'class="[^"]*ad-detail-description[^"]*"[^>]*>(.*?)</div>')
        if not desc:
            desc = _text(html, r'id="description"[^>]*>(.*?)</div>')
        if desc and len(desc) > 20:
            out["description"] = desc
        return out

    # -------------------------------------------------------------- images
    def image_url(self, img: dict, variant: str = "images") -> str:
        return img.get("url") or ""

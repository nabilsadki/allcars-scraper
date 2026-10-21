"""Adaptateur Otoclic (otoclic.com).

WordPress + plugin "Apus Listing". Les cartes encodent les taxonomies
dans leurs classes CSS, ce qui donne beaucoup de donnees sans parsing :

    listing_make-peugeot listing_model-33 listing_transmission-automatique
    listing_fuel_type-diesel listing_feature-abs listing_feature-bluetooth ...

La galerie complete est dans un attribut data-* de la carte (liste des
URL -wm-XX-).
"""

from __future__ import annotations

import re
from typing import Optional

from ..core.site import SiteAdapter, make_ad

BASE = "https://otoclic.com"
LISTING = f"{BASE}/acheter-voiture-occasion-maroc/"

_FUEL = {
    "diesel": "Diesel", "essence": "Essence", "electrique": "Électrique",
    "hybride": "Hybride", "gaz": "Gaz", "essence-melange": "Essence",
}
_TRANS = {"automatique": "Automatique", "manuelle": "Manuelle", "meca": "Mécanique"}


def _pretty(slug: str) -> str:
    return " ".join(w.capitalize() for w in re.split(r"[-_]", slug) if w)


def _num(text: str) -> Optional[int]:
    digits = re.sub(r"[^\d]", "", text or "")
    return int(digits) if digits else None


class OtoclicAdapter(SiteAdapter):
    name = "otoclic"
    label = "Otoclic"
    default_category = "acheter-voiture-occasion-maroc"
    categories = ["acheter-voiture-occasion-maroc"]
    page_size_fixed = True

    # ------------------------------------------------------------- listing
    def listing_url(self, category: str, offset: int) -> str:
        page = offset // 12 + 1 if offset else 1
        return f"{LISTING}page/{page}/" if page > 1 else LISTING

    def parse_listing(self, html: str, category: str = "") -> list[dict]:
        out = []
        seen = set()
        for m in re.finditer(
                r'<(?:div|article) class="([^"]*listing-grid listing-item[^"]*)"([^>]*)>(.*?)'
                r'(?=<(?:div|article) class="[^"]*listing-grid listing-item|</section>)',
                html, re.S):
            classes = m.group(1)
            attrs = m.group(2)
            frag = m.group(3)
            ad = self._normalize(classes, frag, attrs)
            if ad and ad["ad_id"] not in seen:
                seen.add(ad["ad_id"])
                out.append(ad)
        return out

    def _normalize(self, classes: str, frag: str, attrs: str = "") -> Optional[dict]:
        def tax(prefix: str) -> Optional[str]:
            m = re.search(rf"\b{prefix}-([a-z0-9\-_]+)\b", classes)
            return m.group(1) if m else None

        make = tax("listing_make")
        model_slug = tax("listing_model")
        fuel_slug = tax("listing_fuel_type")
        trans_slug = tax("listing_transmission")
        features = re.findall(r"\blisting_feature-([a-z0-9\-_]+)\b", classes)

        title = self._text(frag, r'class="listing-title"[^>]*>.*?<a[^>]*>(.*?)</a>')
        if not title:
            title = self._text(frag, r'<img[^>]+alt="([^"]+)"')
        url = self._first(frag, r'<a class="listing-image" href="([^"]+)"')
        if not url:
            url = self._first(frag, r'<h2 class="listing-title"><a href="([^"]+)"')
        if not url:
            return None
        ad_id = url.rstrip("/").split("/")[-1]

        km = self._text(frag, r'listing-meta[^"]*mileage[^"]*"[^>]*>.*?value-suffix">(.*?)</span>')
        price = self._text(frag, r'class="price-text">(.*?)</span>')
        year = self._text(frag, r'flaticon-calendar-1.*?value-suffix">(.*?)</span>')
        city = self._text(frag, r'listing-meta[^"]*(?:location|city)[^"]*"[^>]*>.*?value-suffix">(.*?)</span>')
        version = self._text(frag, r'class="listing-meta with-icon tagline"[^>]*>.*?value-suffix">(.*?)</span>')

        brand = _pretty(make) if make else None
        model = None
        if title and brand and title.lower().startswith(brand.lower()):
            model = title[len(brand):].strip()
        elif model_slug and not model_slug.isdigit():
            model = _pretty(model_slug)
        elif title:
            model = title.strip()

        ad = make_ad(
            ad_id=ad_id,
            title=title or None,
            url=url,
            price=_num(price),
            price_str=(price + " Dhs") if price else None,
            mileage_km=_num(km),
            year=_num(year),
            brand=brand,
            model=model,
            fuel=_FUEL.get((fuel_slug or "").lower(), _pretty(fuel_slug) if fuel_slug else None),
            gearbox=_TRANS.get((trans_slug or "").lower(), _pretty(trans_slug) if trans_slug else None),
            city=city.strip() if city else None,
            category="Voiture d'occasion",
        )
        if version:
            ad["options"] = {"version": version.strip()}
            ad["option_labels"] = [version.strip()]
        if features:
            labels = [_pretty(f) for f in features]
            ad["options"] = {**ad.get("options", {}), "equipements": labels}
            ad["option_labels"] = list(ad.get("option_labels", [])) + labels
        # galerie : attribut data-images (JSON)
        uniq = []
        m = re.search(r'data-images="([^"]+)"', attrs or "") or \
            re.search(r'data-images="([^"]+)"', frag)
        if m:
            import html as _html
            import json as _json
            try:
                for g in _json.loads(_html.unescape(m.group(1))):
                    g = str(g).replace("\\/", "/")
                    if g not in uniq:
                        uniq.append(g)
            except Exception:
                pass
        if not uniq:
            for g in re.findall(r'https://otoclic\.com/wp-content/uploads/[^"\']+?'
                                r'\.(?:webp|jpg|jpeg|png)', frag):
                if g not in uniq:
                    uniq.append(g)
        if uniq:
            ad["images"] = [{"url": g, "url_full_hd": g, "thumb": g} for g in uniq[:60]]
            ad["nb_images"] = len(ad["images"])
        return ad

    @staticmethod
    def _text(frag: str, pattern: str) -> Optional[str]:
        m = re.search(pattern, frag, re.S)
        if not m:
            return None
        return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", m.group(1))).strip()

    @staticmethod
    def _first(frag: str, pattern: str) -> Optional[str]:
        m = re.search(pattern, frag, re.S)
        return m.group(1) if m else None

    # -------------------------------------------------------------- detail
    def detail_url(self, ad: dict) -> Optional[str]:
        return ad.get("url")

    def parse_detail(self, html: str, ad: dict) -> dict:
        out = dict(ad)
        images = [i["url"] for i in (ad.get("images") or [])]
        for url in re.findall(r'https://otoclic\.com/wp-content/uploads/[^"\']+?'
                              r'\.(?:webp|jpg|jpeg|png)', html):
            if url not in images:
                images.append(url)
        if images:
            out["images"] = [{"url": u, "url_full_hd": u, "thumb": u} for u in images[:60]]
            out["nb_images"] = len(out["images"])
        m = re.search(r'<div[^>]+class="[^"]*listing-description[^"]*"[^>]*>(.*?)</div>',
                      html, re.S)
        if not m:
            m = re.search(r'<div[^>]+id="description"[^>]*>(.*?)</div>', html, re.S)
        if m:
            desc = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", m.group(1))).strip()
            if len(desc) > 20:
                out["description"] = desc
        # date de publication
        m = re.search(r'article:published_time"\s+content="(\d{4}-\d{2}-\d{2})', html)
        if m:
            out["publish_date"] = m.group(1)
        # telephone
        m = re.search(r'(?:telephone|tel)[^\d]{0,20}(0\d{8,9})', html, re.I)
        if m:
            out["phone"] = m.group(1)
        # annee / km / ville depuis les taxonomies de la fiche
        for label, key, cast in (("Année", "year", _num), ("Kilométrage", "mileage_km", _num)):
            m = re.search(rf'{label}[^<]*</[^>]+>\s*<[^>]+>\s*([^<]+)', html)
            if m:
                val = cast(re.sub(r"<[^>]+>", " ", m.group(1)))
                if val:
                    out[key] = val
        return out

    # -------------------------------------------------------------- images
    def image_url(self, img: dict, variant: str = "images") -> str:
        return img.get("url") or ""

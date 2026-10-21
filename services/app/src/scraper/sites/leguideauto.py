"""Adaptateur Le Guide Auto (leguideauto.ma).

Serveur classique (PHP) : les cartes contiennent une table d'attributs
(Energie, Boite de vitesse, Puissance fiscale, Kilometrage, Annee de mise
en circulation) et la ville est un badge.

    /voitures-occasion/annonce/<marque-modele-ville>/<id>
"""

from __future__ import annotations

import re
from typing import Optional

from ..core.site import SiteAdapter, make_ad, relative_date

BASE = "https://www.leguideauto.ma"
LISTING = f"{BASE}/voitures-occasion"

_CARD_RE = re.compile(r'<div class="card bg-primary shadow">(.*?)(?=<div class="card bg-primary shadow">|</section>)',
                      re.S)


def _num(text: str) -> Optional[int]:
    digits = re.sub(r"[^\d]", "", text or "")
    return int(digits) if digits else None


def _text(frag: str, pattern: str) -> Optional[str]:
    m = re.search(pattern, frag, re.S)
    if not m:
        return None
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", m.group(1))).strip()


class LeguideautoAdapter(SiteAdapter):
    name = "leguideauto"
    label = "Le Guide Auto"
    default_category = "voitures-occasion"
    categories = ["voitures-occasion"]
    page_size_fixed = True

    # ------------------------------------------------------------- listing
    def reset_pagination(self) -> None:
        self._page = 0

    def listing_url(self, category: str, offset: int) -> str:
        # pagination par chemin : /voitures-occasion/2 , /voitures-occasion/3 ...
        self._page = getattr(self, "_page", 0) + 1
        return LISTING if self._page <= 1 else f"{LISTING}/{self._page}"

    def parse_listing(self, html: str, category: str = "") -> list[dict]:
        out = []
        seen = set()
        for m in _CARD_RE.finditer(html):
            frag = m.group(1)
            ad = self._normalize(frag)
            if ad and ad["ad_id"] not in seen:
                seen.add(ad["ad_id"])
                out.append(ad)
        return out

    def _normalize(self, frag: str) -> Optional[dict]:
        url = _text(frag, r'<a href="(https://www\.leguideauto\.ma/voitures-occasion/annonce/[^"]+)"')
        if not url:
            return None
        ad_id = url.rstrip("/").split("/")[-1]
        city = _text(frag, r'title="Ville">\s*([^<]+)</span>')
        brand = _text(frag, r'class="font-weight-light text-uppercase">([^<]+)</span>')
        model = _text(frag, r'class="font-weight-light text-uppercase">[^<]*</span>\s*([^<]+)</h3>')
        version = _text(frag, r'<h5 class="font-weight-normal mb-0">([^<]+)</h5>')
        price = _text(frag, r'badge-pill badge-dark-lighten fs-14">\s*([\d\s]+)\s*Dhs')
        km = _text(frag, r'Kilométrage</h6>\s*<p>([^<]+)</p>')
        year = _text(frag, r'Année de mise en circulation</h6>\s*<p>([^<]+)</p>')
        fuel = _text(frag, r'Énergie</h6>\s*<p>([^<]+)</p>')
        gearbox = _text(frag, r'Boîte de vitesse</h6>\s*<p>([^<]+)</p>')
        power = _text(frag, r'Puissance fiscale</h6>\s*<p>([^<]+)</p>')
        body = _text(frag, r'Carrosserie</h6>\s*<p>([^<]+)</p>')
        img = _text(frag, r'data-src="([^"]+)"')
        if not img:
            img = _text(frag, r'<img[^>]+src="([^"]+)"')

        title = " ".join(x for x in (brand, model, version) if x).strip()
        ad = make_ad(
            ad_id=ad_id,
            title=title or None,
            url=url,
            price=_num(price),
            price_str=(f"{_num(price):,}".replace(",", " ") + " Dhs") if price else None,
            mileage_km=(_num(re.split(r"\u2248|\u2013|\u2192|\bo\b", km)[0] if km else None) or None),
            year=_num(year),
            brand=brand.strip() if brand else None,
            model=model.strip() if model else None,
            fuel=fuel.strip() if fuel else None,
            gearbox=gearbox.strip() if gearbox else None,
            city=city.strip() if city else None,
            fiscal_power=power.strip() if power else None,
            category="Voiture d'occasion",
        )
        options = {}
        labels = []
        if body:
            options["carrosserie"] = body
            labels.append(f"Carrosserie {body}")
        if version:
            options["version"] = version.strip()
            labels.append(version.strip())
        ad["options"] = options
        ad["option_labels"] = labels
        if img:
            big = img.replace("/medium/", "/big/").replace("/small/", "/big/")
            ad["images"] = [{"url": img, "url_full_hd": big, "thumb": img}]
            ad["nb_images"] = 1
        return ad

    def total_ads(self, html: str) -> Optional[int]:
        m = re.search(r"([\d\s]+)\s*annonces trouvées", html)
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
        for url in re.findall(r'https://www\.leguideauto\.ma/contents/announcements/pictures/[^"\']+'
                              r'\.(?:webp|jpg|jpeg|png)', html):
            if url not in images:
                images.append(url)
        if images:
            out["images"] = [{"url": u, "url_full_hd": u, "thumb": u} for u in images[:40]]
            out["nb_images"] = len(out["images"])
        # date de publication ("il y a 2 jours")
        m = re.search(r'Date de publication</div>\s*<p>([^<]+)</p>', html)
        if m:
            rel = relative_date(m.group(1))
            if rel:
                out["publish_date"] = rel
        m = re.search(r'<div[^>]+class="[^"]*(?:description|annonce-description)[^"]*"[^>]*>(.*?)</div>',
                      html, re.S)
        if m:
            desc = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", m.group(1))).strip()
            if len(desc) > 20:
                out["description"] = desc
        return out

    # -------------------------------------------------------------- images
    def image_url(self, img: dict, variant: str = "images") -> str:
        return img.get("url") or ""

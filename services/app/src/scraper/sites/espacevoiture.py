"""Adaptateur Espacevoiture.com (espacevoiture.com).

Petite annonces auto marocaines. Page d'accueil /voitures.html + pagination
/voitures/pageN.html (10 annonces par page).
"""

from __future__ import annotations

import re
from typing import Optional
from urllib.parse import urljoin

from ..core.site import SiteAdapter, make_ad

BASE = "https://www.espacevoiture.com"
LISTING = f"{BASE}/voitures.html"


def _kc(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()


def _num(text: str) -> Optional[int]:
    digits = re.sub(r"[^\d]", "", text or "")
    return int(digits) if digits else None


def _price(text: str) -> Optional[int]:
    if not text:
        return None
    digits = re.sub(r"[^\d]", "", text)
    return int(digits) if digits else None


class EspacevoitureAdapter(SiteAdapter):
    name = "espacevoiture"
    label = "Espacevoiture.com"
    default_category = "voitures"
    categories = ["voitures"]
    modes = ["list", "rescan", "details"]

    def __init__(self):
        self._page = 0
        super().__init__()

    def reset_pagination(self) -> None:
        self._page = 0

    def listing_url(self, category: str, offset: int) -> str:
        self._page = getattr(self, "_page", 0) + 1
        if self._page <= 1:
            return f"{BASE}/voitures.html"
        return f"{BASE}/voitures/page{self._page}.html"

    def parse_listing(self, html: str, category: str = "") -> list[dict]:
        out = []
        seen = set()
        for row in re.findall(r'<tr>\s*<td width="140">.*?</tr>', html, re.S):
            url_m = re.search(r'href="(/voitures/[^"]+/annonce-[^"]+\.html)"', row)
            if not url_m:
                continue
            url = urljoin(BASE, url_m.group(1))
            ad_id_m = re.search(r"/annonce-([^./]+)\.html", row)
            ad_id = ad_id_m.group(1) if ad_id_m else url.rstrip("/").split("/")[-1].split(".")[0]
            if not ad_id or ad_id in seen:
                continue
            seen.add(ad_id)

            title_m = re.search(r'<a href="/voitures/[^"]*/annonce-[^"]+\.html"[^>]*>\s*([^<]+?)\s*</a>', row)
            title = _kc(title_m.group(1)) if title_m else None

            price_m = re.search(r'<span class="prix">\s*(.*?)\s*</span>', row, re.S)
            price = _price(price_m.group(1)) if price_m else None

            year_m = re.search(r'<span class="annee">\s*(.*?)\s*</span>', row, re.S)
            year = _num(year_m.group(1)) if year_m else None

            km_m = re.search(r'<span class="km">\s*(.*?)\s*</span>', row, re.S)
            km = _num(km_m.group(1)) if km_m else None

            img_m = re.search(r'<img[^>]+src="(https://image\.espacevoiture\.com[^"]+)"', row)
            thumb = img_m.group(1) if img_m else None

            brand = model = None
            if title:
                tparts = title.split()
                if tparts:
                    brand = tparts[0].title()
                    if len(tparts) > 1:
                        model = tparts[1].title()

            ad = make_ad(
                ad_id=ad_id,
                list_id=None,
                title=title,
                url=url,
                price=price,
                price_str=(f"{price:,}".replace(",", " ") + " DH") if price else None,
                year=year,
                mileage_km=km,
                brand=brand,
                model=model,
                category="Voiture d'occasion",
            )
            if thumb:
                ad["images"] = [{"url": thumb, "url_full_hd": thumb, "thumb": thumb}]
                ad["nb_images"] = 1
            out.append(ad)
        return out

    def total_ads(self, html: str) -> Optional[int]:
        m = re.search(r'Nombre d\'annonces\s*:\s*<b>\s*([\d\s]+)\s*</b>', html)
        if m:
            return _num(m.group(1))
        return None

    def detail_url(self, ad: dict) -> Optional[str]:
        return ad.get("url")

    def parse_detail(self, html: str, ad: dict) -> dict:
        merged = {}
        tg = re.search(r'<h2 class="titreGlobal">(.*?)</h2>', html, re.S)
        if tg:
            title = _kc(tg.group(1))
            mm = re.match(r'^(.*?)\s+voiture\s+à\s+(.+)$', title, re.I)
            if mm:
                merged["title"] = _kc(mm.group(1))
                merged["city"] = _kc(mm.group(2)).title()
            else:
                m2 = re.match(r'^(.*?)\s+a vendre(?:\s*-\s*(.+))?$', title, re.I)
                if m2:
                    merged["title"] = _kc(m2.group(1))
                    if m2.group(2):
                        merged["city"] = _kc(m2.group(2)).title()
                else:
                    merged["title"] = title

        title = merged.get("title") or ad.get("title")
        if title:
            tparts = title.split()
            if tparts:
                merged.setdefault("brand", tparts[0].title())
                if len(tparts) > 1:
                    merged.setdefault("model", tparts[1].title())
            ym = re.search(r"\b(?:19|20)\d{2}\b", title)
            if ym:
                merged.setdefault("year", int(ym.group(0)))

        spec = {}
        for m in re.finditer(r'<dt>\s*(.*?)\s*</dt>\s*<dd[^>]*>\s*(.*?)\s*</dd>', html, re.S):
            k = _kc(re.sub(r'<[^>]+>', '', m.group(1))).lower()
            v = _kc(re.sub(r'<[^>]+>', ' ', m.group(2))).strip()
            if k:
                spec[k] = v

        def g(key):
            for kk, vv in spec.items():
                if kk == key.lower():
                    return vv
            return None

        km = g("kilométrage") or g("kilometrage")
        if km:
            k = _num(km)
            if k is not None:
                merged["mileage_km"] = k
        if g("puissance fiscale"):
            merged["fiscal_power"] = _num(g("puissance fiscale"))
        if g("énergie") or g("energie"):
            merged["fuel"] = (g("énergie") or g("energie")).title()
        if g("boîte de vitesse") or g("boite de vitesse"):
            merged["gearbox"] = (g("boîte de vitesse") or g("boite de vitesse")).title()
        if g("modèle") or g("modele"):
            merged.setdefault("model", (g("modèle") or g("modele")).title())
        if g("ville/annnonce") or g("ville/annonce"):
            merged.setdefault("city", (g("ville/annnonce") or g("ville/annonce")).title())
        if not merged.get("year"):
            ym2 = re.search(r'ann[ée]e\s+(\d{4})', html, re.I)
            if ym2:
                merged["year"] = int(ym2.group(1))
        price = None
        pm = g("prix")
        if pm:
            price = _price(pm)
        if not price:
            pm2 = re.search(r'Prix.*?<dd[^>]*>\s*([\d\s.]+)\s*DHS', html, re.S)
            if pm2:
                price = _price(pm2.group(1))
        if price:
            merged["price"] = price
            merged["price_str"] = f"{price:,}".replace(",", " ") + " DHS"

        desc = re.search(r'<p id="presentation">(.*?)</p>', html, re.S)
        if desc:
            merged["description"] = _kc(re.sub(r'<[^>]+>', ' ', desc.group(1)))

        seller_m = re.search(r'<dt>Nom &amp; Prénom</dt>\s*<dd>\s*(.*?)\s*</dd>', html, re.S)
        if seller_m:
            merged["seller"] = _kc(seller_m.group(1))
        for needle in ("Téléphone", "Telephone"):
            phone_m = re.search(r'<dt>' + needle + r'</dt>\s*<dd>\s*(.*?)\s*</dd>', html, re.S)
            if phone_m:
                merged["phone"] = _kc(phone_m.group(1))
                break
        merged.setdefault("phone", ad.get("phone"))
        merged.setdefault("seller_type", None)

        imgs = sorted(set(re.findall(r"https://image\.espacevoiture\.com/dynamique/images/original/[^\"]+", html)))
        imgs = [u.split("?")[0] for u in imgs]
        if imgs:
            merged["images"] = [{"url": u, "url_full_hd": u, "thumb": u} for u in imgs]
            merged["nb_images"] = len(imgs)
        else:
            thumbs = sorted(set(re.findall(r"https://image\.espacevoiture\.com/dynamique/images/small/[^\"]+", html)))
            thumbs = [u.split("?")[0] for u in thumbs]
            if thumbs:
                merged["images"] = [{"url": u, "url_full_hd": u, "thumb": u} for u in thumbs]
                merged["nb_images"] = len(thumbs)

        merged["details_fetched"] = True
        merged["ad_id"] = ad.get("ad_id")
        merged["url"] = ad.get("url") or merged.get("url")
        return merged

    def image_url(self, img, variant="images"):
        return img.get("url") or img.get("url_full_hd") or ""

    def image_id(self, url):
        import os, re as _re
        name = os.path.basename(url.split("?")[0])
        return _re.sub(r"[^0-9A-Za-z_.-]", "_", name) or "img"

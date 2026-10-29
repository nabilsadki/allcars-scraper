"""Adaptateur Wandaloo.com (wandaloo.com/occasion).

Petites annonces auto marocaines. La liste utilise des blocs
`<!-- result-item -->` avec titre, prix et caracteristiques basic.
La fiche details exposes un panneau `param`/`value` complet (motorisation,
putssance fiscale, couleur, etat, equipements...) et une galerie photo.
"""

from __future__ import annotations

import re
from typing import Optional
from urllib.parse import unquote

from ..core.site import SiteAdapter, make_ad

BASE = "https://www.wandaloo.com"
LISTING = f"{BASE}/occasion/"


def _num(text: str) -> Optional[int]:
    digits = re.sub(r"[^\d]", "", text or "")
    return int(digits) if digits else None


def _price(text: str) -> Optional[int]:
    if not text:
        return None
    # "128.000" -> 128000 ; "128 000" -> 128000
    digits = re.sub(r"[^\d]", "", text)
    return int(digits) if digits else None


def _kc(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()


class WandalooAdapter(SiteAdapter):
    name = "wandaloo"
    label = "Wandaloo.com"
    default_category = "voiture-occasion"
    categories = ["voiture-occasion"]
    modes = ["list", "rescan", "details"]

    # ------------------------------------------------------------- listing
    def reset_pagination(self) -> None:
        self._page = 0

    def listing_url(self, category: str, offset: int) -> str:
        self._page = getattr(self, "_page", 0) + 1
        return f"{BASE}/occasion/?pg={self._page}"

    def parse_listing(self, html: str, category: str = "") -> list[dict]:
        out = []
        seen = set()
        # each block: <!-- result-item --> ... </li>
        for frag in re.findall(r"<!-- result-item -->(.*?)Afficher", html, re.S):
            url_m = re.search(r'href="(https://www\.wandaloo\.com/occasion/[^"]+\.html)"', frag)
            if not url_m:
                continue
            url = url_m.group(1)
            ad_id_m = re.search(r"/(\d+)\.html", url)
            ad_id = ad_id_m.group(1) if ad_id_m else url.rstrip("/").split("/")[-1].split(".")[0]
            if not ad_id or ad_id in seen:
                continue
            seen.add(ad_id)

            title_m = re.search(r'<p class="titre">\s*<a[^>]*>(.*?)</a>', frag, re.S)
            title = _kc(title_m.group(1)) if title_m else None

            price_m = re.search(r'<p class="prix">\s*<span>(.*?)</span>', frag, re.S)
            price = _price(price_m.group(1)) if price_m else None

            city_m = re.search(r'<span class="city">\s*<i[^>]*></i>\s*(.*?)\s*</span>', frag, re.S)
            city = _kc(city_m.group(1)) if city_m else None

            date_m = re.search(r'class="dateHeure"[^>]*>(?:<!--[^>]*-->)?\s*(.*?)\s*</span>', frag, re.S)
            date_txt = _kc(date_m.group(1)) if date_m else None
            list_time = None  # textual, pas de parsing complexe ici

            seller_type = None
            if re.search(r'class="pro"', frag):
                seller_type = "Pro"
            if re.search(r'class="particulier"', frag, re.I):
                seller_type = "Particulier"

            img_m = re.search(r'<img src="(https://www\.wandaloo\.com/files/[^"]+)"', frag)
            thumb = img_m.group(1) if img_m else None

            # details ul
            fuel = year = km = fiscal = None
            det_m = re.search(r'<ul class="detail">(.*?)</ul>', frag, re.S)
            if det_m:
                lis = re.findall(r'<li>\s*(.*?)\s*</li>', det_m.group(1), re.S)
                for li in lis:
                    v = _kc(li)
                    if not v:
                        continue
                    if "km" in v.lower():
                        km = _num(v)
                    elif "cv" in v.lower():
                        fiscal = _num(v)
                    elif re.fullmatch(r"(19|20)\d{2}", v):
                        year = int(v)
                    elif v.lower() in ("diesel", "essence", "hybride", "electrique",
                                      "electrique", "gpl", "g.les"):
                        fuel = v.title() if "" else v
                    else:
                        # carburant inconnu : on garde tel quel si pas deja set
                        if fuel is None and len(v) < 20:
                            fuel = v.title()

            # brand/model from title (YEAR often present)
            brand = model = None
            if title:
                tparts = title.split()
                if tparts:
                    brand = tparts[0].title()
                    # coupe a la premiere annee 4 chiffres -> le model reste propre
                    idx = next((i for i in range(1, len(tparts)) if re.fullmatch(r"(?:19|20)\d{2}", tparts[i])), -1)
                    model_tokens = tparts[1:idx] if idx > 1 else tparts[1:2]
                    if model_tokens:
                        model = " ".join(model_tokens).title()
                    if year is None:
                        ym = re.search(r"\b(?:19|20)\d{2}\b", title)
                        if ym:
                            year = int(ym.group(0))

            ad = make_ad(
                ad_id=ad_id,
                list_id=None,
                title=title,
                url=url,
                price=price,
                price_str=(f"{price:,}".replace(",", " ") + " DH") if price else None,
                year=year,
                mileage_km=km,
                fuel=fuel,
                brand=brand,
                model=model,
                city=city,
                seller_type=seller_type,
                category="Voiture d'occasion",
            )
            if thumb:
                ad["images"] = [{"url": thumb, "url_full_hd": thumb, "thumb": thumb}]
                ad["nb_images"] = 1
            ad["list_time"] = date_txt
            out.append(ad)
        return out

    # ------------------------------------------------------------- details
    def detail_url(self, ad: dict) -> Optional[str]:
        return ad.get("url")

    def parse_detail(self, html: str, ad: dict) -> dict:
        merged = {}
        title_m = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.S)
        if title_m:
            title = _kc(title_m.group(1))
            merged["title"] = title
        title = merged.get("title") or ad.get("title")
        if title:
            tparts = title.split()
            if tparts:
                merged.setdefault("brand", tparts[0].title())
                ym = re.search(r"\b(?:19|20)\d{2}\b", title)
                if ym:
                    merged.setdefault("year", int(ym.group(0)))
                idx = next((i for i in range(1, len(tparts)) if re.fullmatch(r"(?:19|20)\d{2}", tparts[i])), -1)
                model_tokens = tparts[1:idx] if idx > 1 else tparts[1:2]
                if model_tokens:
                    merged.setdefault("model", " ".join(model_tokens).title())

        # price
        price_m = re.search(r"([\d\.\s]+)\s*<sup>DH</sup>", html)
        if price_m:
            price = _price(price_m.group(1))
            if price:
                merged["price"] = price
                merged["price_str"] = f"{price:,}".replace(",", " ") + " DH"

        # specs param/value
        spec = {}
        for m in re.finditer(r'<p class="param">\s*(.*?)\s*</p>\s*<p class="value">\s*(.*?)\s*</p>', html, re.S):
            k = _kc(re.sub(r'<[^>]+>', '', m.group(1))).strip().lower()
            v = _kc(re.sub(r'<[^>]+>', ' ', m.group(2))).strip()
            if k:
                spec[k] = v

        def g(key):
            for kk, vv in spec.items():
                if kk == key.lower():
                    return vv
            return None

        if g("mise en circulation"):
            y = _num(g("mise en circulation"))
            if y:
                merged["year"] = y
        if g("motorisation"):
            merged["fuel"] = g("motorisation").title()
        if g("transmission") or g("transmision"):
            merged["gearbox"] = (g("transmission") or g("transmision")).title()
        if g("puissance fiscale"):
            fp = _num(g("puissance fiscale"))
            if fp is not None:
                merged["fiscal_power"] = fp
        if g("couleur extérieure") or g("couleur exterieure"):
            merged["colour"] = g("couleur extérieure") or g("couleur exterieure")
        if g("etat du véhicule") or g("etat du vehicule"):
            merged["cond"] = g("etat du véhicule") or g("etat du vehicule")
        if g("ville"):
            merged["city"] = g("ville").title()

        # Main / first hand
        main_txt = g("1ère main") or g("1ere main")
        if main_txt:
            merged["first_hand"] = "premi" in main_txt.lower()

        # mileage / boite via bloc titre-tag (l'autre style de spec)
        for km in re.finditer(r'<p class="titre">\s*(.*?)\s*</p>\s*<p class="tag">\s*(.*?)\s*</p>', html, re.S):
            kk = _kc(km.group(1)).lower()
            vv = _kc(km.group(2))
            if "kilom" in kk and _num(vv):
                merged.setdefault("mileage_km", _num(vv))
            if ("transmiss" in kk or "transm" in kk or ("bo" + "ite") in kk):
                merged.setdefault("gearbox", vv.title())
            if kk == "ville":
                merged.setdefault("city", vv.title())

        # equipment (keys with non-empty value excluding '-' and empty)
        equip = []
        skip = {"mise en circulation", "motorisation", "transmission", "transmision",
                "puissance fiscale", "puissance dynamique", "couleur extérieure",
                "couleur exterieure", "etat du véhicule", "etat du vehicule", "ville",
                "1ère main", "1ere main", "dédouanée", "dedouanee"}
        for k, v in spec.items():
            if k in skip:
                continue
            if v and v.strip() not in ("", "-"):
                equip.append(k.title())
        if equip:
            merged["option_labels"] = equip
            merged["options"] = {e: True for e in equip}

        # seller
        seller_m = re.search(r"Toutes les annonces de ce vendeur\">\s*(.*?)\s*</a>", html, re.S)
        if seller_m:
            merged["seller"] = _kc(seller_m.group(1))
        seller_type_m = re.search(r'adress">\s*[^<]*<br\s*/?>\s*([\d\s]{9,})\s*</p>', html, re.S)
        if seller_type_m:
            merged["phone"] = seller_type_m.group(1).strip()
        # seller type from detail key Vendeur if any
        # stocker origin? none
        # images (those containing ad_id)
        ad_id = ad.get("ad_id")
        if ad_id:
            imgs = sorted(set(re.findall(
                rf"https://www\.wandaloo\.com/files/Voiture-Occasion/[^\"']*{ad_id}[^\"']*",
                html)))
            imgs = [u.split("?")[0] for u in imgs]
            imgs = [u for u in imgs if re.search(r"\.(webp|jpe?g|png)$", u, re.I)]
            if imgs:
                merged["images"] = [{"url": u, "url_full_hd": u, "thumb": u} for u in imgs]
                merged["nb_images"] = len(imgs)
        merged.setdefault("seller_type", None)
        merged.setdefault("phone", ad.get("phone"))
        merged["details_fetched"] = True
        # tel fallback (scan)
        if not merged.get("phone"):
            m = re.search(r"\b(0[567]\d{8})\b", html)
            if m:
                merged["phone"] = m.group(1)
        ad_id2 = ad.get("ad_id")
        if ad_id2:
            merged["ad_id"] = ad_id2
        merged["url"] = ad.get("url") or merged.get("url")
        return merged

    def image_url(self, img, variant="images"):
        return img.get("url") or img.get("url_full_hd") or ""

    def image_id(self, url):
        import os, re as _re
        name = os.path.basename(url.split("?")[0])
        return _re.sub(r"[^0-9A-Za-z_.-]", "_", name) or "img"

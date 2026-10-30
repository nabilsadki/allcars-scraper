"""Adaptateur Autocash (site Next.js + sitemap)."""

from __future__ import annotations

import json
import re
from typing import Any

from ..core.site import SiteAdapter, make_ad


class AutocashAdapter(SiteAdapter):
    name = "autocash"
    label = "Autocash"
    default_category = "voitures"
    categories = ["voitures"]
    modes = ["list", "rescan", "details"]

    SITEMAP_URL = "https://www.autocash.ma/achat/voitures/sitemap/0.xml"
    BASE_URL = "https://www.autocash.ma"

    def __init__(self):
        super().__init__()
        self._done = False

    def reset_pagination(self) -> None:
        self._done = False

    def listing_url(self, category: str, offset: int) -> str:
        if self._done:
            return "https://www.autocash.ma/robots.txt"
        return self.SITEMAP_URL

    def total_ads(self, html: str) -> int | None:
        ids = self._sitemap_ids(html)
        if ids is None:
            return None
        return len(ids)

    def parse_listing(self, html: str, category: str = "") -> list[dict]:
        if "<urlset" not in (html or ""):
            return []
        ids = self._sitemap_ids(html) or []
        self._done = True
        out = []
        for ad_id in ids:
            out.append(make_ad(
                ad_id=str(ad_id),
                list_id=int(ad_id),
                title=None,
                description=None,
                price=None,
                city=None,
                brand=None,
                model=None,
                year=None,
                mileage_km=None,
                km=None,
                fuel=None,
                gear=None,
                category="voitures",
                images=[],
                nb_images=0,
                details_fetched=False,
                url=f"{self.BASE_URL}/fr/achat/voiture/{ad_id}",
                link=f"{self.BASE_URL}/fr/achat/voiture/{ad_id}",
            ))
        return out

    def _sitemap_ids(self, html: str) -> list[str] | None:
        if not html or "<urlset" not in html:
            return None
        ids = []
        seen = set()
        for m in re.finditer(r"<loc>\s*https?://[^<]*/voiture/(\d+)", html):
            value = m.group(1)
            if value not in seen:
                seen.add(value)
                ids.append(value)
        return ids

    def detail_url(self, ad: dict) -> str | None:
        ad_id = ad.get("ad_id")
        if not ad_id:
            return None
        return f"{self.BASE_URL}/fr/achat/voiture/{ad_id}"

    def _row(self, html: str, label: str) -> str | None:
        m = re.search(re.escape(label) + r".*?</p><p[^>]*>(.*?)</p>", html, re.S)
        if not m:
            return None
        value = re.sub(r"<[^>]+>", " ", m.group(1))
        value = re.sub(r"\s+", " ", value).strip()
        return value or None

    def parse_detail(self, html: str, ad: dict) -> dict:
        result = dict(ad)
        try:
            title_match = re.search(r"<title>(.*?)</title>", html, re.S)
            title = title_match.group(1).strip() if title_match else result.get("title")
            title = title.replace(" - autocash", "").strip() if title else result.get("title")
        except Exception:
            title = result.get("title")

        brand = self._row(html, "Marque")
        model = self._row(html, "Modèle")
        finish = self._row(html, "Finition")
        year_raw = self._row(html, "Année")
        category = self._row(html, "Catégorie")
        km_raw = self._row(html, "Kilométrage")
        origin = self._row(html, "Origine")
        city = self._row(html, "Ville")
        color = self._row(html, "Couleur")
        fiscal = self._row(html, "Chevaux fiscaux")
        fuel = self._row(html, "Carburant")
        gear = self._row(html, "Transmission")
        first_hand = self._row(html, "Première main")

        km_match = re.search(r"([\d\s]+)\s*km", km_raw or "")
        km = int(re.sub(r"\D", "", km_match.group(1))) if km_match else result.get("km")

        year = None
        if year_raw:
            ym = re.search(r"(\d{4})", year_raw)
            if ym:
                year = int(ym.group(1))

        price = None
        pm = re.search(r'\\"price\\":(\d+)', html)
        if pm:
            try:
                price = int(pm.group(1))
            except (TypeError, ValueError):
                price = None

        # images
        imgs = []
        seen = set()
        for u in re.findall(r"https://d7vr8seuelqbd\.cloudfront\.net/[A-Za-z0-9_./-]+", html):
            u = u.rstrip(".,;:!?")
            if u in seen:
                continue
            seen.add(u)
            imgs.append({"url": u, "url_full_hd": u})
        if not imgs:
            imgs = result.get("images") or []

        result.update({
            "title": title or result.get("title"),
            "brand": brand or result.get("brand"),
            "model": model or result.get("model"),
            "version": finish or result.get("version"),
            "year": year if year is not None else result.get("year"),
            "mileage_km": km,
            "km": km,
            "fuel": fuel or result.get("fuel"),
            "gear": gear or result.get("gear"),
            "gearbox": gear or result.get("gear"),
            "category": category or result.get("category"),
            "city": city or result.get("city"),
            "color": color or result.get("color"),
            "fiscal_power": fiscal,
            "origin": origin or result.get("origin"),
            "first_hand": first_hand,
            "body_type": category,
            "price": price if price is not None else result.get("price"),
            "description": None,
            "details_fetched": True,
            "images": imgs,
            "nb_images": len(imgs),
        })
        return result

    def enrich(self, ad, client):
        return ad

    def image_url(self, img, variant="images"):
        if isinstance(img, dict):
            return img.get("url") or img.get("link")
        return str(img)

    def image_id(self, url: str) -> str:
        import os, re
        name = os.path.basename(url.split("?")[0])
        return re.sub(r"[^0-9A-Za-z_.-]", "_", name) or "img"
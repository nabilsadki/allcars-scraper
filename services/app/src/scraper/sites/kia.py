"""Adaptateur Kia Occasion (multimarques), API REST bo-api.kiaoccasion.ma."""

from __future__ import annotations

import json
from typing import Any

from ..core.site import SiteAdapter, make_ad


class KiaAdapter(SiteAdapter):
    name = "kia"
    label = "Kia Occasion"
    default_category = "vehicules-occasion"
    categories = ["vehicules-occasion"]
    modes = ["list", "rescan", "details"]

    API_BASE = "https://kiaoccasion.ma/api/vehicules"
    PAGE_SIZE = 48

    def listing_url(self, category: str, offset: int) -> str:
        page = (offset // self.PAGE_SIZE) + 1
        return (f"{self.API_BASE}?paginate=1&limit={self.PAGE_SIZE}"
                f"&page={page}")

    def reset_pagination(self) -> None:
        pass

    def total_ads(self, html: str) -> int | None:
        try:
            data = json.loads(html)
            return int(data.get("meta", {}).get("total"))
        except (ValueError, TypeError, json.JSONDecodeError):
            return None

    def parse_listing(self, html: str, category: str = "") -> list[dict]:
        try:
            data = json.loads(html)
        except json.JSONDecodeError:
            return []
        out: list[dict] = []
        for item in data.get("data", []) or []:
            price_new = (item.get("prix") or {}).get("nouveau")
            price_old = (item.get("prix") or {}).get("ancien")
            images = []
            for im in item.get("images") or []:
                url = im.get("url") or im.get("image")
                if not url:
                    continue
                sizes = im.get("sizes") or {}
                images.append({
                    "url": url,
                    "url_full_hd": sizes.get("1600x1200") or sizes.get("1024x768") or url,
                })
            if not images and item.get("image"):
                images.append({"url": item["image"], "url_full_hd": item["image"]})
            ad = make_ad(
                ad_id=str(item.get("id")),
                list_id=item.get("id"),
                title=item.get("title") or f'{item.get("marque", "")} {item.get("modele", "")}'.strip(),
                description=None,
                price=price_new if price_new is not None else price_old,
                old_price=price_old,
                category=item.get("category"),
                city=None,
                brand=item.get("marque"),
                model=item.get("modele"),
                year=item.get("annee"),
                mileage_km=item.get("kilometrage"),
                km=item.get("kilometrage"),
                fuel=item.get("energie"),
                gear=None,
                gearbox=None,
                color=None,
                doors=item.get("nb_place"),
                options=None,
                option_labels=item.get("equipements") or [],
                specs=[],
                images=images,
                nb_images=len(images),
                is_premium=bool(item.get("is_promo")),
                is_urgent=False,
                is_hot_deal=bool(item.get("is_vente_flash")),
                url=None,
                details_fetched=False,
                link=None,
            )
            out.append(ad)
        return out

    def detail_url(self, ad: dict) -> str | None:
        ad_id = ad.get("ad_id")
        if not ad_id:
            return None
        return f"https://kiaoccasion.ma/api/vehicules/{ad_id}"

    def parse_detail(self, html: str, ad: dict) -> dict:
        try:
            item = json.loads(html)
        except json.JSONDecodeError:
            return ad
        if not isinstance(item, dict) or item.get("id") is None:
            return ad

        price_new = (item.get("prix") or {}).get("nouveau")
        price_old = (item.get("prix") or {}).get("ancien")
        images = []
        for im in item.get("images") or []:
            url = im.get("url") or im.get("image")
            if not url:
                continue
            sizes = im.get("sizes") or {}
            images.append({
                "url": url,
                "url_full_hd": sizes.get("1600x1200") or sizes.get("1024x768") or url,
            })
        if not images and item.get("image"):
            images.append({"url": item["image"], "url_full_hd": item["image"]})

        gear = item.get("boite")
        if gear == "Auto":
            gear = "Automatique"
        elif gear == "Manuelle":
            gear = "Manuelle"

        ad = dict(ad)
        ad.update({
            "title": item.get("title") or ad.get("title"),
            "description": item.get("description"),
            "price": price_new if price_new is not None else price_old,
            "old_price": price_old,
            "city": (item.get("point_of_sale") or {}).get("ville"),
            "area": None,
            "seller": (item.get("point_of_sale") or {}).get("name"),
            "seller_type": "Professionnel",
            "seller_id": str((item.get("point_of_sale") or {}).get("id")) if (item.get("point_of_sale") or {}).get("id") is not None else None,
            "phone": (item.get("point_of_sale") or {}).get("phone"),
            "brand": item.get("marque") or ad.get("brand"),
            "model": item.get("modele") or ad.get("model"),
            "year": item.get("annee") or ad.get("year"),
            "mileage_km": item.get("kilometrage"),
            "km": item.get("kilometrage"),
            "fuel": item.get("energie") or ad.get("fuel"),
            "gear": gear or ad.get("gear"),
            "gearbox": gear,
            "body_type": item.get("carrosserie"),
            "color": item.get("couleur"),
            "doors": item.get("nb_place"),
            "fiscal_power": str(item.get("puissance_cv")) if item.get("puissance_cv") is not None else None,
            "cv": item.get("puissance_cv"),
            "category": item.get("category") or ad.get("category"),
            "options": None,
            "option_labels": item.get("equipements") or ad.get("option_labels") or [],
            "images": images or ad.get("images") or [],
            "nb_images": len(images) if images else ad.get("nb_images"),
            "details_fetched": True,
            "link": None,
            "url": None,
        })
        if images:
            ad["images"] = images
            ad["nb_images"] = len(images)
        # conserve title si present
        if item.get("version"):
            ad["version"] = ad.get("version") or item.get("title")
        return ad

    def enrich(self, ad, client):
        return ad

    def image_url(self, img, variant="images"):
        sizes = {}
        if isinstance(img, dict):
            sizes = img.get("sizes") or {}
        if isinstance(sizes, dict):
            if variant == "card":
                return sizes.get("400x300") or (img.get("url") if isinstance(img, dict) else None)
            if variant == "full_hd":
                return sizes.get("1600x1200") or sizes.get("1024x768") or (img.get("url") if isinstance(img, dict) else None)
            return sizes.get("800x600") or (img.get("url") if isinstance(img, dict) else None)
        return img.get("url") if isinstance(img, dict) else str(img)

    def image_id(self, url: str) -> str:
        import os, re
        name = os.path.basename(url.split("?")[0])
        return re.sub(r"[^0-9A-Za-z_.-]", "_", name) or "img"
"""Adaptateur Avito.ma.

Site Next.js : les donnees sont dans le payload `__NEXT_DATA__` du HTML
serveur. Cloudflare est passe grace a l'impersonation TLS (curl_cffi).

Detail des acquisitions (mesures du 2026-10-21) :
  * page de liste : 1 requete = ~38 annonces
  * pagination   : ?o=<offset>
  * URL de detail : dans le HTML de la liste, suffixees par le listId
  * marque/modele: API GraphQL graphql.avito.ma (143 marques)
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

from ..core.site import SiteAdapter, make_ad

BASE = "https://www.avito.ma"

_NEXT_DATA_RE = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)
_DETAIL_URL_RE = re.compile(r'(?:https?:)?//www\.avito\.ma(/fr/[^"\'<>\s]+?\.htm)')
_LIST_ID_IN_URL_RE = re.compile(r'_(\d{6,})\.htm$')

# cles d'equipements (groupe 'extra' de params)
_EQUIPMENT_KEYS = {
    "car_abs", "car_airbags", "cd_mp3_bt", "car_reverse_camera", "car_ac",
    "car_esp", "car_rims", "car_speed_limiter", "car_onboard_computer",
    "car_reverse_radar", "car_cruise_control", "car_leather", "car_navigation",
    "car_central_locking", "car_electric_windows", "car_roof",
    "car_start_stop", "car_tow_hook", "car_alarm", "car_keyless",
}


def _pval(p: dict) -> Any:
    for k in ("textValue", "booleanValue", "numberValue", "value"):
        if p.get(k) is not None:
            return p[k]
    return None


def _params_to_dict(ad: dict) -> dict:
    out: dict[str, Any] = {}
    params = ad.get("params") or {}
    for group in ("primary", "secondary", "extra"):
        for p in params.get(group) or []:
            pid = p.get("id") or p.get("key")
            if not pid:
                continue
            val = _pval(p)
            if val is None:
                continue
            out[pid] = bool(val) if group == "extra" else val
    return out


def _specs(ad: dict) -> list[dict]:
    out = []
    for p in ((ad.get("params") or {}).get("primary") or []):
        key = p.get("id") or p.get("key")
        val = _pval(p)
        if not key or val is None:
            continue
        out.append({"key": key, "label": p.get("label") or p.get("name"), "value": val})
    return out


def _option_labels(ad: dict) -> list[str]:
    labels = []
    for p in ((ad.get("params") or {}).get("extra") or []):
        if not _pval(p):
            continue
        label = p.get("label") or p.get("name") or p.get("id") or p.get("key")
        if label:
            labels.append(str(label))
    return labels


def _images(ad: dict) -> list[dict]:
    media = ad.get("media") or {}
    imgs = (media.get("media") or {}).get("images") or []
    if not imgs:
        imgs = ad.get("images") or []
    out = []
    for im in imgs:
        if not isinstance(im, dict):
            continue
        paths = im.get("paths") or {}
        std = paths.get("standard")
        if not std:
            continue
        full = paths.get("fullHd") or re.sub(r"\?t=\w+", "?t=full_hd", std)
        out.append({
            "url": std,
            "url_full_hd": full,
            "thumb": paths.get("largeThumbnail") or paths.get("smallThumbnail"),
        })
    return out


def normalize_ad(raw: dict) -> dict:
    """Normalise une annonce brute Next.js."""
    price = raw.get("price") or {}
    seller = raw.get("seller") or {}
    loc = raw.get("location") or {}
    params = _params_to_dict(raw)
    images = _images(raw)
    city = (loc.get("city") or {}).get("name")
    area = (loc.get("area") or {}).get("name")
    cat = (raw.get("category") or {}).get("name")
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", raw.get("title") or "").strip("_")[:60]
    url = None
    if raw.get("adId") and city and cat:
        from urllib.parse import quote
        url = (f"{BASE}/fr/{quote(city)}/{quote(cat)}/"
               f"{quote(slug)}__{raw['adId']}.htm")
    return make_ad(
        ad_id=raw.get("adId"),
        list_id=raw.get("listId"),
        title=raw.get("title"),
        description=raw.get("description"),
        price=price.get("withoutCurrency"),
        price_str=price.get("withCurrency"),
        old_price=(raw.get("oldPrice") or {}).get("withoutCurrency"),
        discount=raw.get("discount"),
        category=cat,
        type=(raw.get("type") or {}).get("name"),
        city=city,
        area=area,
        seller=seller.get("name"),
        seller_type=raw.get("sellerType"),
        seller_id=seller.get("storeId"),
        phone=(seller.get("phone") or {}).get("number"),
        list_time=raw.get("listTime"),
        year=params.get("regdate"),
        mileage_km=_as_int(params.get("mileage_exact")),
        gearbox=params.get("bv"),
        fuel=params.get("fuel"),
        brand=params.get("brand"),
        model=params.get("model"),
        doors=_as_int(params.get("doors")),
        origin=params.get("v_origin"),
        first_hand=params.get("first_owner"),
        cond=params.get("auto_condition"),
        fiscal_power=params.get("pfiscale"),
        options={k: v for k, v in params.items() if k in _EQUIPMENT_KEYS},
        option_labels=_option_labels(raw),
        specs=_specs(raw),
        images=images,
        nb_images=len(images),
        is_premium=bool(raw.get("isPremium")),
        is_urgent=bool(raw.get("isUrgent")),
        is_hot_deal=bool(raw.get("isHotDeal")),
        url=url,
    )


def _as_int(v: Any) -> Optional[int]:
    if v is None:
        return None
    try:
        return int(str(v).replace(" ", "").replace("km", "").strip())
    except (TypeError, ValueError):
        return None


def find_ad(node: Any, ad_id: Any) -> Optional[dict]:
    """Cherche l'annonce par adId dans l'arbre JSON."""
    if ad_id is None:
        return None
    if isinstance(node, dict):
        if node.get("adId") == ad_id:
            return node
        for v in node.values():
            r = find_ad(v, ad_id)
            if r:
                return r
    elif isinstance(node, list):
        for v in node:
            r = find_ad(v, ad_id)
            if r:
                return r
    return None


class AvitoAdapter(SiteAdapter):
    name = "avito"
    label = "Avito.ma"
    default_category = "maroc/voitures-à_vendre"
    categories = [
        "maroc/voitures-à_vendre",
        "maroc/immobilier",
        "maroc/telephones",
        "maroc/informatique",
    ]
    page_size_fixed = False

    # ------------------------------------------------------------- catalogue
    def catalog_fetcher(self, client):
        """Rend le referentiel marques/modeles (API GraphQL)."""
        from ..core.catalog import GRAPHQL_URL, BRANDS_QUERY, MODELS_QUERY

        def fetch():
            brands = client.post_json(
                GRAPHQL_URL,
                {"query": BRANDS_QUERY, "variables": {"keyword": "", "categoryId": 2010}},
            )
            out = {}
            for b in (brands.get("data", {}).get("getGenericBrands", {}) or {}).get("brands", []):
                entry = {"id": b["value"], "label": b["label"], "models": {}}
                try:
                    m = client.post_json(GRAPHQL_URL, {
                        "query": MODELS_QUERY,
                        "variables": {"keyword": "", "brandId": b["value"], "categoryId": 2010},
                    })
                    for mo in (m.get("data", {}).get("getGenericModels", {}) or {}).get("models", []):
                        entry["models"][mo["value"]] = mo["label"]
                except Exception:
                    pass
                out[b["value"]] = entry
            return {"brands": out, "fetched_at": 0}

        return fetch

    def enrich(self, ad: dict, client) -> dict:
        """Deduit marque/modele du titre via le catalogue GraphQL."""
        cache = getattr(client, "_avito_catalog", None)
        if cache is None:
            try:
                cat = self.catalog_fetcher(client)
                cache = cat()
                try:
                    setattr(client, "_avito_catalog", cache)
                except Exception:
                    pass
            except Exception:
                cache = {"brands": {}}
        from ..core.catalog import Catalog
        c = Catalog(client)
        c._data = cache
        c._build_index()
        brand, model = c.parse_brand_model(ad.get("title") or "")
        if brand:
            ad["brand"], ad["model"] = brand, model
        return ad

    # ------------------------------------------------------------- listing
    def listing_url(self, category: str, offset: int) -> str:
        url = f"{BASE}/fr/{category}"
        if offset:
            url += f"?o={offset}"
        return url

    def parse_listing(self, html: str) -> list[dict]:
        data = self._next_data(html)
        props = data.get("props", {}).get("pageProps", {})
        fsr = props.get("componentProps", {}).get("facetedSearchResponse", {})
        raw_ads = fsr.get("ads", {}).get("details", []) or []
        urls = self._detail_urls(html)
        out = []
        for raw in raw_ads:
            ad = normalize_ad(raw)
            lid = raw.get("listId")
            if lid is not None:
                ad["url"] = urls.get(str(lid)) or ad.get("url")
            out.append(ad)
        return out

    def total_ads(self, html: str) -> Optional[int]:
        try:
            data = self._next_data(html)
            fsr = data["props"]["pageProps"]["componentProps"]["facetedSearchResponse"]
            return (fsr.get("count") or {}).get("total")
        except Exception:
            return None

    # -------------------------------------------------------------- detail
    def detail_url(self, ad: dict) -> Optional[str]:
        return ad.get("url")

    def parse_detail(self, html: str, ad: dict) -> dict:
        data = self._next_data(html)
        props = data.get("props", {}).get("pageProps", {})
        # la fiche complete est dans componentProps.adInfo.ad ; componentProps.ads[]
        # ne contient que la version "liste" (sans specs)
        full = ((props.get("componentProps") or {}).get("adInfo") or {}).get("ad")
        if not isinstance(full, dict) or not (full.get("params") or {}).get("primary"):
            cand = find_ad(props, ad.get("ad_id"))
            if cand and (cand.get("params") or {}).get("primary"):
                full = cand
        if not isinstance(full, dict):
            return {}
        out = normalize_ad(full)
        out["url"] = ad.get("url")
        return out

    # -------------------------------------------------------------- images
    def image_url(self, img: dict, variant: str = "images") -> str:
        suffix = {"card": "?t=card", "images": "?t=images", "full_hd": "?t=full_hd"}[variant]
        base = (img.get("url") or "").split("?")[0]
        return base + suffix

    # -------------------------------------------------------------- utils
    @staticmethod
    def _next_data(html: str) -> dict:
        m = _NEXT_DATA_RE.search(html)
        if not m:
            raise ValueError("__NEXT_DATA__ absent (structure Next.js modifiee ?)")
        return json.loads(m.group(1))

    @staticmethod
    def _detail_urls(html: str) -> dict[str, str]:
        out: dict[str, str] = {}
        for path in _DETAIL_URL_RE.findall(html):
            m = _LIST_ID_IN_URL_RE.search(path)
            if m:
                out.setdefault(m.group(1), BASE + path)
        return out

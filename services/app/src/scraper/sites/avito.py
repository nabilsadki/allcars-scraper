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

import hashlib
import json
import os
import re
import time
from typing import Any, Optional

from ..core.site import SiteAdapter, make_ad

BASE = "https://www.avito.ma"

# --- decoupage en tranches (plafond de pagination) -------------------------
# Mesures du 2026-11-01 : Avito ne laisse voir que les ~2,8 % premiers
# resultats d'une requete. `?o=` au-dela renvoie une page vide, avec un
# plafond dur d'environ 4 000 annonces :
#     offset_max ~= min(4000, total / 36)      (verifie sur 5 jeux de filtres)
# Une requete de T annonces rend donc environ T/36 + 38 annonces, et non T.
#
# Consequence : pour couvrir un gros catalogue il faut multiplier les
# requetes *differentes* (ville x tranche de prix) plutot que paginer plus
# loin. On decoupe chaque ville en sous-tranches de ~SLICE_TARGET annonces :
# une sous-tranche tient dans une page, donc elle est couverte en entier.
SLICE_TARGET = int(os.environ.get("AVITO_SLICE_TARGET", "40"))
#: plafond de pagination mesure : offset_max ~= min(4000, total / 36)
PAGINATION_RATIO = 36
PAGINATION_HARD_CAP = 4000
#: nombre maximal de requetes de sondage pour construire les tranches
SLICE_PROBE_BUDGET = int(os.environ.get("AVITO_SLICE_PROBES", "9000"))
#: duree de validite du cache de tranches (secondes) : la distribution des
#: prix bouge lentement, un mois suffit (recalcule si le total derive de 10 %)
SLICE_CACHE_TTL = int(os.environ.get("AVITO_SLICE_CACHE_TTL", str(30 * 24 * 3600)))
SLICE_CACHE_DIR = os.environ.get("AVITO_SLICE_CACHE_DIR", "/data/cache")
#: borne haute des prix pour la bissection (DH) : 99,9 % du catalogue est
#: en dessous (91 annonces au-dela sur 96 658)
PRICE_MAX = 20_000_000

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
        # image de couverture de la boutique (placeholder), pas une photo
        if "/stores-covers/" in std:
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


def _first_word(text: str) -> str:
    """Premier mot significatif, sans accents : 'voitures-à_vendre' -> 'voitures'."""
    import unicodedata
    t = unicodedata.normalize("NFKD", text or "")
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = re.sub(r"[^a-zA-Z0-9]+", " ", t).strip().lower()
    return t.split()[0] if t.split() else ""


def _slugify(text: str) -> str:
    """'Casablanca' -> 'casablanca', 'Aïn Sebaa' -> 'ain_sebaa'."""
    import unicodedata
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return text.replace("-", "_")


def _price(n: int) -> str:
    """150000 -> '150 000' (libelle lisible dans la progression)."""
    return f"{int(n):,}".replace(",", " ")


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
    #: total global du catalogue (renseigne par slices())
    global_total: int = 0
    #: nombre d'annonces ecartees (hors categorie) depuis le demarrage
    log_sponsored: int = 0
    #: resume du decoupage en tranches (log)
    log_slices: str = ""

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
        """Deduit marque/modele du titre via le catalogue GraphQL.

        Le catalogue (et son index) est memorise sur le client : sans cela
        l'index (7 000 modeles) serait reconstruit a chaque annonce.
        """
        cat = getattr(client, "_avito_catalog_obj", None)
        if cat is None:
            from ..core.catalog import Catalog, catalog_path_for
            cat = Catalog(client, path=catalog_path_for(self.name))
            try:
                cat.fetch()          # charge le cache disque s'il existe
            except Exception:
                return ad
            try:
                setattr(client, "_avito_catalog_obj", cat)
            except Exception:
                pass
        brand, model = cat.parse_brand_model(ad.get("title") or "")
        if brand:
            ad["brand"], ad["model"] = brand, model
        return ad

    # ------------------------------------------------------------- listing
    def listing_url(self, category: str, offset: int) -> str:
        url = f"{BASE}/fr/{category}"
        if offset:
            # la tranche peut deja porter une query (?price=...) : on ajoute
            url += ("&" if "?" in url else "?") + f"o={offset}"
        return url

    def parse_listing(self, html: str, category: str = "") -> list[dict]:
        data = self._next_data(html)
        props = data.get("props", {}) or {}
        page_props = props.get("pageProps", {}) or {}
        comp = page_props.get("componentProps", {}) or {}
        fsr = comp.get("facetedSearchResponse") or {}
        raw_ads = (fsr.get("ads") or {}).get("details") or []
        urls = self._detail_urls(html)
        out = []
        for raw in raw_ads:
            ad = normalize_ad(raw)
            lid = raw.get("listId")
            if lid is not None:
                ad["url"] = urls.get(str(lid)) or ad.get("url")
            out.append(ad)
        kept = self._filter_category(out, category)
        if len(kept) != len(out):
            self.log_sponsored += len(out) - len(kept)
        return kept

    # ------------------------------------------------------------- filtre
    def _filter_category(self, ads: list[dict], category: str) -> list[dict]:
        """Ecarte les annonces qui n'appartiennent pas a la categorie demandee.

        Avito injecte des annonces sponsorisees d'autres categories dans les
        pages de resultats (immobilier, services a la personne...). On
        compare le premier mot du slug de categorie (dans l'URL ou dans le
        nom de la categorie) a celui de la categorie demandee.
        """
        if not category:
            return ads
        key = _first_word(category.rstrip("/").split("/")[-1])
        if not key:
            return ads
        out = []
        for ad in ads:
            url = ad.get("url") or ""
            m = re.search(r"/fr/[^/]+/([^/]+)/", url)
            if m and _first_word(m.group(1)) == key:
                out.append(ad)
                continue
            if _first_word(ad.get("category") or "") == key:
                out.append(ad)
        return out

    def slices(self, category: str, client=None) -> list[tuple[str, str]]:
        """Decoupe la categorie en tranches : villes x tranches de prix.

        Avito ne rend que les ~2,8 % premiers resultats d'une requete (voir
        les constantes en haut du module) : une ville de 35 000 annonces n'en
        laisse voir qu'environ 1 000. On decoupe donc chaque ville en
        sous-tranches de prix d'environ SLICE_TARGET annonces : une
        sous-tranche tient dans une page, donc elle est couverte en entier.

        Le chemin attendu est du type "maroc/voitures-a-vendre".
        """
        if not client:
            return [(category, category)]
        try:
            html = client.get(f"{BASE}/fr/{category}")
            data = self._next_data(html)
            fsr = data["props"]["pageProps"]["componentProps"]["facetedSearchResponse"]
            # total global du catalogue (pour la barre de progression)
            try:
                self.global_total = int((fsr.get("count") or {}).get("total") or 0)
            except Exception:
                self.global_total = 0
            cities = []
            for f in fsr.get("filters") or []:
                if f.get("key") != "city":
                    continue
                for v in f.get("values") or []:
                    name = v.get("value")
                    cnt = v.get("count")
                    if not name or cnt in (None, "0"):
                        continue
                    cities.append((str(name), int(cnt)))
                break
        except Exception:
            return [(category, category)]
        if not cities:
            return [(category, category)]
        # on garde les grandes villes d'abord (le plus d'annonces)
        cities.sort(key=lambda x: -x[1])

        cached = self._load_slice_cache(category)
        if cached:
            self.log_slices = f"{len(cached)} tranches (cache)"
            return cached

        root, _, cat = category.partition("/")
        budget = [SLICE_PROBE_BUDGET]
        out: list[tuple[str, str]] = []
        for name, cnt in cities:
            slug = _slugify(name)
            path = f"{root}/{slug}/{cat}"
            if cnt <= SLICE_TARGET * 2:
                out.append((name, path))
                continue
            sub = self._city_price_slices(name, path, client, budget)
            if sub:
                # filet de securite : les annonces sans prix (~21 % du
                # catalogue) n'apparaissent dans aucun filtre de prix
                out.append((f"{name} (toutes)", path))
                out.extend(sub)
            else:
                out.append((name, path))
        out.append((category, category))   # la categorie globale en dernier
        # un decoupage interrompu (budget epuise) reste utilisable : on le
        # met en cache, mais pour 24 h seulement, le temps de le refaire
        if budget[0] < SLICE_PROBE_BUDGET:
            complete = budget[0] > 0
            ttl = SLICE_CACHE_TTL if complete else min(SLICE_CACHE_TTL, 86400)
            self._save_slice_cache(category, out, ttl=ttl)
        self.log_slices = (f"{len(out)} tranches "
                           f"({SLICE_PROBE_BUDGET - budget[0]} sondages"
                           + ("" if budget[0] > 0 else ", budget epuise") + ")")
        return out

    # --------------------------------------------------- tranches de prix
    def _city_price_slices(self, label: str, path: str, client,
                           budget: list[int]) -> list[tuple[str, str]]:
        """Sous-tranches de prix d'une ville (bissection recursive)."""
        priced = self._slice_count(client, f"{path}?price=0-{PRICE_MAX}", budget)
        if not priced:
            return []
        out = self._bisect(label, path, int(priced), client, budget, 0, PRICE_MAX, 0)
        return out or []

    def _bisect(self, label: str, path: str, total: int, client,
                budget: list[int], lo: int, hi: int,
                depth: int) -> list[tuple[str, str]]:
        """Bissecte [lo, hi] jusqu'a obtenir des tranches <= SLICE_TARGET."""
        # largeur minimale 10 DH : au-dela les annonces partagent le meme
        # prix et aucun filtre ne peut les separer
        if (total <= SLICE_TARGET or depth >= 26 or hi - lo <= 10
                or budget[0] <= 0):
            return [(f"{label} {_price(lo)}-{_price(hi)} DH",
                     f"{path}?price={lo}-{hi}")]
        mid = (lo + hi) // 2
        left = f"{path}?price={lo}-{mid}"
        n_left = self._slice_count(client, left, budget)
        if n_left is None:
            # budget epuise ou erreur reseau : on garde la tranche entiere
            return [(f"{label} {_price(lo)}-{_price(hi)} DH", left)]
        out: list[tuple[str, str]] = []
        if n_left > 0:
            out += self._bisect(label, path, int(n_left), client, budget,
                                lo, mid, depth + 1)
        n_right = total - int(n_left)
        if n_right > 0:
            out += self._bisect(label, path, n_right, client, budget,
                                mid + 1, hi, depth + 1)
        return out

    def _slice_count(self, client, path: str, budget: list[int]) -> Optional[int]:
        """Nombre d'annonces d'une requete (consomme 1 sondage)."""
        if budget[0] <= 0:
            return None
        budget[0] -= 1
        done = SLICE_PROBE_BUDGET - budget[0]
        if done % 50 == 0:
            p = getattr(self, "progress", None)
            if p is not None:
                try:
                    p.update(current_slice=f"decoupage des tranches ({done} sondages)")
                except Exception:
                    pass
        try:
            html = client.get(f"{BASE}/fr/{path}")
            n = self.total_ads(html)
            return int(n) if n is not None else None
        except Exception:
            return None

    # ------------------------------------------------------- cache tranches
    def _slice_cache_path(self, category: str) -> str:
        h = hashlib.sha1(category.encode("utf-8")).hexdigest()[:10]
        # la version du decoupage fait partie du nom : changer SLICE_TARGET
        # ou PRICE_MAX invalide le cache automatiquement
        return os.path.join(SLICE_CACHE_DIR,
                            f"avito_slices_{h}_{SLICE_TARGET}_{PRICE_MAX}.json")

    def _load_slice_cache(self, category: str) -> Optional[list[tuple[str, str]]]:
        try:
            with open(self._slice_cache_path(category), encoding="utf-8") as fh:
                data = json.load(fh)
            created = float(data.get("created") or 0)
            ttl = int(data.get("ttl") or SLICE_CACHE_TTL)
            if time.time() - created > ttl:
                return None
            # le catalogue bouge : on recalcule si le total a derive de >10 %
            old_total = int(data.get("global_total") or 0)
            if self.global_total and old_total:
                if abs(self.global_total - old_total) > 0.10 * old_total:
                    return None
            slices = data.get("slices") or []
            if not slices:
                return None
            return [(str(a), str(b)) for a, b in slices]
        except Exception:
            return None

    def _save_slice_cache(self, category: str,
                          slices: list[tuple[str, str]],
                          ttl: Optional[int] = None) -> None:
        path = self._slice_cache_path(category)
        try:
            os.makedirs(SLICE_CACHE_DIR, exist_ok=True)
            tmp = f"{path}.tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"created": time.time(),
                           "ttl": int(ttl or SLICE_CACHE_TTL),
                           "global_total": self.global_total,
                           "slices": slices}, fh, ensure_ascii=False)
            os.replace(tmp, path)
        except Exception:
            pass

    def total_ads(self, html: str) -> Optional[int]:
        try:
            data = self._next_data(html)
            props = data.get("props", {}) or {}
            page_props = props.get("pageProps", {}) or {}
            comp = page_props.get("componentProps", {}) or {}
            fsr = comp.get("facetedSearchResponse") or {}
            return (fsr.get("count") or {}).get("total")
        except Exception:
            return None

    def max_offset(self, total: Optional[int]) -> Optional[int]:
        """Avito ne pagine que les ~1/36 premiers resultats d'une requete.

        Mesure du 2026-11-01 sur 5 jeux de filtres (total -> offset max) :
        830 -> 23, 11 771 -> 327, 34 907 -> 970, 45 880 -> 1 274,
        141 477 -> 3 930 (plafond dur ~4 000).
        """
        if not total:
            return None
        return min(PAGINATION_HARD_CAP, int(total) // PAGINATION_RATIO)

    # -------------------------------------------------------------- detail
    def detail_url(self, ad: dict) -> Optional[str]:
        return ad.get("url")

    def redirect_means_removed(self, url: str, final: str) -> bool:
        """Avito redirige une annonce retiree vers la liste de la categorie.

        Constat (2026-11-12) : une fiche supprimee repond 200 apres une
        redirection vers /fr/maroc/voitures_d_occasion-a_vendre (ou vers
        l'accueil). C'est le signal de retrait du site, equivalent a un 404.
        """
        from urllib.parse import unquote, urlsplit
        path = unquote(urlsplit(final or "").path or "").strip("/").lower()
        if path in ("", "fr"):
            return True                                    # accueil
        # liste de la categorie : pas de suffixe "_<listId>.htm"
        return "voitures" in path and not re.search(r"_\d{6,}\.htm$", path)

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

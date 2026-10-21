"""
Client HTTP avito.ma — sans navigateur.

Faits vérifiés (2026-10-20) :
  * curl_cffi (impersonation TLS) passe le challenge Cloudflare sans navigateur,
    sans proxy et sans cf_clearance -> HTTP 200 direct.
  * Les pages sont rendues côté serveur (Next.js) : les données sont dans
    <script id="__NEXT_DATA__"> du HTML brut.
  * Page de liste : ~38 annonces par requête, pagination ?o=<offset>.
  * Les URL de détail ne sont PAS dans __NEXT_DATA__ : elles sont dans le HTML
    (balises <a href>), suffixées par le listId de l'annonce.
    -> mapping listId -> URL, 38/38 trouvés.
  * Page détail : meme __NEXT_DATA__ + seller.phone.number + description.

Aucune donnée n'est écrite en base par ce module : il ne fait que lire.
"""

from __future__ import annotations

import json
import random
import re
import time
from typing import Any, Iterator, Optional

from curl_cffi import requests

BASE = "https://www.avito.ma"

_NEXT_DATA_RE = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)
_DETAIL_URL_RE = re.compile(
    r'(?:https?:)?//www\.avito\.ma(/fr/[^"\'<>\s]+?\.htm)'
)
_LIST_ID_IN_URL_RE = re.compile(r'_(\d{6,})\.htm$')

USER_AGENTS = ["chrome", "firefox", "safari", "chrome", "firefox"]

# cles d'equipements (groupe 'extra' de params)
_EQUIPMENT_KEYS = {
    "car_abs", "car_airbags", "cd_mp3_bt", "car_reverse_camera", "car_ac",
    "car_esp", "car_rims", "car_speed_limiter", "car_onboard_computer",
    "car_reverse_radar", "car_cruise_control", "car_leather", "car_navigation",
    "car_central_locking", "car_electric_windows", "car_roof",
    "car_start_stop", "car_tow_hook", "car_alarm", "car_keyless",
}


class CloudflareBlocked(RuntimeError):
    """Cloudflare a renvoyé la page de challenge au lieu du contenu."""


class AvitoClient:
    """Client HTTP pour avito.ma.

    delay/jitter : pauses entre requêtes (anti-rate-limit).
    verbose      : logs sur stdout.
    """

    def __init__(
        self,
        delay: float = 1.5,
        jitter: float = 0.8,
        timeout: int = 30,
        retries: int = 3,
        verbose: bool = True,
    ):
        self.session = requests.Session()
        self.delay = delay
        self.jitter = jitter
        self.timeout = timeout
        self.retries = retries
        self.verbose = verbose
        self._last_call = 0.0
        self._i = 0
        self.catalog_total = None
        self.stats = {"requests": 0, "blocked": 0, "errors": 0, "ads": 0}

    # ------------------------------------------------------------------ util
    def log(self, msg: str) -> None:
        if self.verbose:
            print(msg, flush=True)

    def _wait(self) -> None:
        elapsed = time.time() - self._last_call
        pause = self.delay + random.uniform(0, self.jitter) - elapsed
        if pause > 0:
            time.sleep(pause)
        self._last_call = time.time()

    def _get(self, url: str) -> str:
        """GET avec retry, rotation du profil TLS et détection du challenge."""
        last_err: Optional[Exception] = None
        for attempt in range(1, self.retries + 1):
            self._wait()
            imp = USER_AGENTS[self._i % len(USER_AGENTS)]
            self._i += 1
            try:
                r = self.session.get(
                    url,
                    impersonate=imp,
                    timeout=self.timeout,
                    headers={
                        "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
                        "Accept": (
                            "text/html,application/xhtml+xml,application/xml;q=0.9,"
                            "image/avif,image/webp,*/*;q=0.8"
                        ),
                    },
                )
                self.stats["requests"] += 1
            except Exception as e:
                self.stats["errors"] += 1
                last_err = e
                self.log(f"  ! reseau ({imp}) {type(e).__name__} - essai {attempt}/{self.retries}")
                time.sleep(2 * attempt)
                continue

            if r.status_code == 200 and "Just a moment" not in r.text:
                return r.text

            if "Just a moment" in r.text or r.status_code in (403, 429, 503):
                self.stats["blocked"] += 1
                last_err = CloudflareBlocked(f"HTTP {r.status_code} / challenge")
                backoff = 5 * attempt + random.uniform(0, 5)
                self.log(
                    f"  ! Cloudflare ({imp}) HTTP {r.status_code} - backoff {backoff:.0f}s "
                    f"(essai {attempt}/{self.retries})"
                )
                time.sleep(backoff)
                continue

            self.stats["errors"] += 1
            last_err = RuntimeError(f"HTTP {r.status_code}")
            self.log(f"  ! HTTP {r.status_code} - essai {attempt}/{self.retries}")
            time.sleep(2 * attempt)

        raise last_err or RuntimeError("echec requete")

    @staticmethod
    def _next_data(html: str) -> dict:
        m = _NEXT_DATA_RE.search(html)
        if not m:
            raise ValueError("__NEXT_DATA__ absent (structure Next.js modifiee ?)")
        return json.loads(m.group(1))

    @staticmethod
    def _detail_urls(html: str) -> dict[str, str]:
        """Mappe listId -> URL de detail, extraites des balises <a href>."""
        out: dict[str, str] = {}
        for path in _DETAIL_URL_RE.findall(html):
            m = _LIST_ID_IN_URL_RE.search(path)
            if m:
                out.setdefault(m.group(1), BASE + path)
        return out

    # ---------------------------------------------------------------- public
    def listing_page(self, category_path: str, offset: int = 0) -> dict:
        """Recupere une page de resultats.

        category_path : chemin sans /fr/ (ex: maroc/voitures-à_vendre)
        offset        : pagination (0, 38, 76, ...)
        """
        url = f"{BASE}/fr/{category_path}"
        if offset:
            url += f"?o={offset}"
        html = self._get(url)
        data = self._next_data(html)
        props = data.get("props", {}).get("pageProps", {})
        fsr = props.get("componentProps", {}).get("facetedSearchResponse", {})
        raw_ads = fsr.get("ads", {}).get("details", []) or []
        total = (fsr.get("count") or {}).get("total", 0)
        urls = self._detail_urls(html)

        ads = []
        for a in raw_ads:
            ad = normalize_ad(a)
            lid = a.get("listId")
            if lid is not None:
                ad["url"] = urls.get(str(lid)) or ad.get("url")
            ads.append(ad)
        self.stats["ads"] += len(ads)
        return {"ads": ads, "total": total, "offset": offset, "url": url}

    def iter_listing(
        self,
        category_path: str,
        max_ads: int = 0,
        page_size: int = 38,
    ) -> Iterator[dict]:
        """Parcourt toutes les pages d'une categorie (max_ads=0 -> tout)."""
        offset = 0
        seen = 0
        total = None
        self.catalog_total = None
        while True:
            page = self.listing_page(category_path, offset)
            if total is None:
                total = page["total"]
                self.catalog_total = total
                self.log(f"  catalogue : {total} annonces")
            ads = page["ads"]
            if not ads:
                break
            for ad in ads:
                yield ad
                seen += 1
                if max_ads and seen >= max_ads:
                    self.log(f"  plafond atteint ({seen})")
                    return
            offset += len(ads)
            if total and offset >= total:
                break
            if len(ads) < page_size:
                break

    def detail(self, ad: dict) -> dict:
        """Fiche complete d'une annonce (telephone, description complete)."""
        url = ad.get("url")
        if not url:
            return ad
        html = self._get(url)
        data = self._next_data(html)
        props = data.get("props", {}).get("pageProps", {})
        # la fiche complete est dans componentProps.adInfo.ad (params.primary
        # = les specs affichees) ; componentProps.ads[] ne contient que la
        # version "liste". Le adId peut y etre null : on se fie au titre.
        full = ((props.get("componentProps") or {}).get("adInfo") or {}).get("ad")
        if not isinstance(full, dict) or not (full.get("params") or {}).get("primary"):
            cand = find_ad(props, ad.get("ad_id"))
            if cand and (cand.get("params") or {}).get("primary"):
                full = cand
        if not isinstance(full, dict):
            return ad
        merged = dict(ad)
        det = normalize_ad(full)
        # on n'ecrase jamais une valeur connue par None : la page detail
        # n'a pas toujours la meme richnesse que la liste
        for k, v in det.items():
            if v is None or v == [] or v == {}:
                continue
            merged[k] = v
        merged["url"] = url
        merged["details_fetched"] = True
        if merged.get("images"):
            merged["nb_images"] = len(merged["images"])
        return merged


# ------------------------------------------------------------------ helpers
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


def _pval(p: dict) -> Any:
    """Valeur d'un parametre, quel que soit le schema de la reponse."""
    for k in ("textValue", "booleanValue", "numberValue", "value"):
        if p.get(k) is not None:
            return p[k]
    return None


def _params_to_dict(ad: dict) -> dict:
    """Aplatit les 3 groupes de parametres en dict simple.

    primary  : les specs affichees sur la page detail
               (Marque, Modele, Nombre de portes, Origine, Premiere main,
                Puissance fiscale, Etat, Secteur, Type)  -> cles 'key'/'value'
    secondary: Annee-modele, Kilometrage, Boite, Carburant -> 'id'/'textValue'
    extra    : equipements (ABS, Airbags, Clim...)           -> 'id'/'booleanValue'

    Attention : la fiche detail renvoie les equipements sous forme de
    chaines ('1') et la page de liste sous forme de booleens : on normalise
    en booleen pour que les deux soient comparables.
    """
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
            if group == "extra":
                val = bool(val) if not isinstance(val, (int, float)) else bool(val)
            out[pid] = val
    return out


def _specs(ad: dict) -> list[dict]:
    """Specs affichees (params.primary) : [{key, label, value}]."""
    out = []
    for p in ((ad.get("params") or {}).get("primary") or []):
        key = p.get("id") or p.get("key")
        val = _pval(p)
        if not key or val is None:
            continue
        out.append({"key": key, "label": p.get("label") or p.get("name"), "value": val})
    return out


def _option_labels(ad: dict) -> list[str]:
    """Libelles lisibles des equipements (groupe 'extra' uniquement)."""
    labels = []
    for p in ((ad.get("params") or {}).get("extra") or []):
        if not _pval(p):
            continue
        label = p.get("label") or p.get("name") or p.get("id") or p.get("key")
        if label:
            labels.append(str(label))
    return labels


def _images(ad: dict) -> list[dict]:
    """Images de l'annonce, avec la variante full resolution (?t=full_hd).

    Deux formes selon la page :
      * liste   : media.media.images[] (paths.standard / largeThumbnail)
      * detail  : images[] (paths.standard / fullHd / smallThumbnail)
    """
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


def normalize_ad(ad: dict) -> dict:
    """Normalise une annonce brute Next.js en dict exploitable."""
    price = ad.get("price") or {}
    seller = ad.get("seller") or {}
    phone = (seller.get("phone") or {}).get("number")
    loc = ad.get("location") or {}
    params = _params_to_dict(ad)
    images = _images(ad)
    return {
        "ad_id": ad.get("adId"),
        "list_id": ad.get("listId"),
        "title": ad.get("title"),
        "description": ad.get("description"),
        "price": price.get("withoutCurrency"),
        "price_str": price.get("withCurrency"),
        "old_price": (ad.get("oldPrice") or {}).get("withoutCurrency"),
        "discount": ad.get("discount"),
        "category": (ad.get("category") or {}).get("name"),
        "type": (ad.get("type") or {}).get("name"),
        "city": (loc.get("city") or {}).get("name"),
        "area": (loc.get("area") or {}).get("name"),
        "seller": seller.get("name"),
        "seller_type": ad.get("sellerType"),
        "seller_id": seller.get("storeId"),
        "phone": phone,
        "list_time": ad.get("listTime"),
        "year": _as_int(params.get("regdate")),
        "mileage_km": _as_int(params.get("mileage_exact")),
        "gearbox": params.get("bv"),
        "fuel": params.get("fuel"),
        # specs de la page detail (params.primary)
        "brand": params.get("brand"),
        "model": params.get("model"),
        "doors": _as_int(params.get("doors")),
        "origin": params.get("v_origin"),
        "first_hand": params.get("first_owner"),
        "cond": params.get("auto_condition"),
        "fiscal_power": params.get("pfiscale"),
        "sector": params.get("205"),
        "ad_type": params.get("SELL"),
        # options = equipements uniquement (groupe 'extra')
        "options": {
            k: v for k, v in params.items()
            if k in _EQUIPMENT_KEYS
        },
        "option_labels": _option_labels(ad),
        "specs": _specs(ad),
        "images": images,
        "nb_images": len(images),
        "is_premium": bool(ad.get("isPremium")),
        "is_urgent": bool(ad.get("isUrgent")),
        "is_hot_deal": bool(ad.get("isHotDeal")),
        "url": None,
        "friendly_url": ad.get("friendlyUrl"),
        "details_fetched": False,
    }


def _as_int(v: Any) -> Optional[int]:
    if v is None:
        return None
    try:
        return int(str(v).replace(" ", "").replace("km", "").strip())
    except (TypeError, ValueError):
        return None

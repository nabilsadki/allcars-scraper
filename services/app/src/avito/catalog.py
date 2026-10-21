"""Catalogue marques/modeles — via l'API GraphQL d'Avito.

L'API de listing ne fournit pas la marque/le modele : ils sont deduits du titre.
On recupere donc le referentiel officiel via graphql.avito.ma :
    getGenericBrands(categoryId: 2010)  -> 143 marques
    getGenericModels(categoryId: 2010, brandId: X) -> modeles de la marque

Le referentiel est mis en cache dans /data/catalog.json (volume Docker) et
rafraichi uniquement s'il est absent ou trop vieux.

Requetes vues dans l'ancien projet PHP (voitures-maroc-sy/php_scripts/
crawl_avito_voitures_brands_models.php).
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Optional

GRAPHQL_URL = "https://graphql.avito.ma/graphql"
CATEGORY_ID = 2010  # Voitures d'occasion

BRANDS_QUERY = """
query ($keyword: String, $categoryId: Int) {
  getGenericBrands(keyword: $keyword, categoryId: $categoryId) {
    count
    brands { label value __typename }
    __typename
  }
}
"""

MODELS_QUERY = """
query ($keyword: String, $brandId: String, $categoryId: Int) {
  getGenericModels(keyword: $keyword, brandId: $brandId, categoryId: $categoryId) {
    count
    models { label value __typename }
    __typename
  }
}
"""

CATALOG_PATH = os.environ.get("CATALOG_PATH", "/data/catalog.json")
CATALOG_TTL = int(os.environ.get("CATALOG_TTL", str(60 * 60 * 24 * 7)))  # 7 jours


def _contains_word(text: str, needle: str) -> bool:
    """True si needle apparait dans text sur des frontières de mots."""
    if not needle:
        return False
    return bool(re.search(rf"(?:^|\s){re.escape(needle)}(?:\s|$)", text))


def _norm(s: str) -> str:
    s = (s or "").lower()
    s = s.replace("-", " ")
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


class Catalog:
    """Referentiel marques/modeles, avec cache JSON."""

    def __init__(self, client, path: str = CATALOG_PATH, ttl: int = CATALOG_TTL):
        self.client = client
        self.path = path
        self.ttl = ttl
        self._data: Optional[dict] = None
        self._brand_index: Optional[list] = None
        self._model_index: Optional[list] = None

    # ------------------------------------------------------------------ cache
    def _load_cache(self) -> Optional[dict]:
        try:
            if not os.path.exists(self.path):
                return None
            if time.time() - os.path.getmtime(self.path) > self.ttl:
                return None
            with open(self.path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None

    def _save_cache(self, data: dict) -> None:
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
        except Exception:
            pass

    # ------------------------------------------------------------------ fetch
    def _gql(self, query: str, variables: dict) -> dict:
        r = self.client.session.post(
            GRAPHQL_URL,
            json={"query": query, "variables": variables},
            impersonate="chrome",
            timeout=40,
            headers={
                "Content-Type": "application/json",
                "Origin": "https://www.avito.ma",
                "Referer": "https://www.avito.ma/",
            },
        )
        data = r.json()
        if "errors" in data:
            raise RuntimeError(f"GraphQL: {data['errors'][0].get('message')}")
        return data.get("data", {})

    def fetch(self, force: bool = False) -> dict:
        """Charge (ou rafraichit) le referentiel marques/modeles."""
        if not force:
            cached = self._load_cache()
            if cached:
                self._data = cached
                self._build_index()
                return cached
        self.client.log("  chargement du catalogue marques/modeles (GraphQL)...")
        brands = self._gql(BRANDS_QUERY, {"keyword": "", "categoryId": CATEGORY_ID})
        raw = brands.get("getGenericBrands", {}).get("brands", [])
        out = {"fetched_at": time.time(), "brands": {}}
        for b in raw:
            label = b["label"]
            entry = {"id": b["value"], "label": label, "models": {}}
            try:
                m = self._gql(MODELS_QUERY, {
                    "keyword": "", "brandId": b["value"], "categoryId": CATEGORY_ID
                })
                for mo in m.get("getGenericModels", {}).get("models", []):
                    entry["models"][mo["value"]] = mo["label"]
            except Exception as e:
                self.client.log(f"    ! modeles {label}: {e}")
            out["brands"][b["value"]] = entry
            self.client.log(
                f"    {label}: {len(entry['models'])} modeles"
            )
        self._save_cache(out)
        self._data = out
        self._build_index()
        return out

    # ------------------------------------------------------------------ index
    def _build_index(self) -> None:
        """Construit les index marque et modele (longueur decroissante)."""
        idx = []
        midx = []
        if not self._data:
            self._brand_index, self._model_index = idx, midx
            return
        for entry in self._data.get("brands", {}).values():
            n = _norm(entry["label"])
            if n:
                idx.append((n, entry))
                # alias : 1er mot seul ("mercedes benz" -> "mercedes")
                first = n.split()[0]
                if len(first) >= 4:
                    idx.append((first, entry))
            for mlabel in (entry.get("models") or {}).values():
                nm = _norm(mlabel)
                if len(nm) >= 2:
                    midx.append((nm, entry["label"], mlabel))
        idx.sort(key=lambda x: -len(x[0]))
        midx.sort(key=lambda x: -len(x[0]))
        self._brand_index = idx
        self._model_index = midx

    @property
    def data(self) -> dict:
        if self._data is None:
            self.fetch()
        return self._data or {}

    # ------------------------------------------------------------------ match
    def parse_brand_model(self, title: str) -> tuple[Optional[str], Optional[str]]:
        """Deduit (marque, modele) du titre d'une annonce.

        Ordre : modele d'abord (plus discriminant, ex. "Range Rover Evoque"
        -> Land Rover), puis marque.
        """
        if self._brand_index is None:
            self._build_index()
        t = _norm(title)
        if not t:
            return None, None

        # 1) modele : le plus long qui apparait dans le titre
        best = None
        for nm, brand_label, model_label in self._model_index or []:
            if _contains_word(t, nm):
                # si la marque est aussi dans le titre, on privilegie ce couple
                if best is None or len(nm) > len(best[0]):
                    best = (nm, brand_label, model_label)
                if _contains_word(t, _norm(brand_label)):
                    break
        if best:
            return best[1], best[2]

        # 2) marque
        for nbrand, entry in self._brand_index or []:
            if _contains_word(t, nbrand):
                return entry["label"], self._match_model(t, entry)
        return None, None

    def _match_model(self, norm_title: str, brand_entry: dict) -> Optional[str]:
        best = None
        best_len = 0
        for label in (brand_entry.get("models") or {}).values():
            nm = _norm(label)
            if len(nm) < 3:
                continue
            if _contains_word(norm_title, nm):
                if len(nm) > best_len:
                    best, best_len = label, len(nm)
        return best

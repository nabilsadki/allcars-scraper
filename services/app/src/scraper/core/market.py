"""Statistiques de marche : prix median par segment.

Pour savoir si une annonce est "en dessous du marche", on compare son prix
au prix median des annonces du meme segment :
  1. meme marque + meme modele + meme annee  (segment le plus fin)
  2. meme marque + meme annee                 (repli)
Un segment de moins de N annonces n'est pas exploitable (bruit).

Les statistiques sont calculees en SQL (AVG/COUNT) sur l'ensemble de la base,
puis chargees en memoire : une seule requete au demarrage du calcul.
"""

from __future__ import annotations

from typing import Any, Optional


class MarketStats:
    """Prix de reference par segment."""

    def __init__(self, db, min_sample: int = 5):
        self.db = db
        self.min_sample = min_sample
        self.by_model: dict[tuple, list] = {}
        self.by_brand: dict[tuple, list] = {}
        self.load()

    def load(self) -> None:
        """Charge les moyennes de prix par segment."""
        try:
            rows = self.db.query(
                "SELECT brand, model, year, COUNT(*) AS n, AVG(price) AS avg_p, "
                "MIN(price) AS min_p, MAX(price) AS max_p "
                "FROM ads WHERE site='avito' AND price IS NOT NULL "
                "AND brand IS NOT NULL AND year IS NOT NULL "
                "GROUP BY brand, model, year"
            )
        except Exception:
            rows = []
        for r in rows:
            if not r.get("n") or r["n"] < 2:
                continue
            self.by_model[(r["brand"], r["model"], r["year"])] = r

        try:
            rows = self.db.query(
                "SELECT brand, year, COUNT(*) AS n, AVG(price) AS avg_p "
                "FROM ads WHERE site='avito' AND price IS NOT NULL "
                "AND brand IS NOT NULL AND year IS NOT NULL "
                "GROUP BY brand, year"
            )
        except Exception:
            rows = []
        for r in rows:
            if not r.get("n") or r["n"] < 2:
                continue
            self.by_brand[(r["brand"], r["year"])] = r

    # ------------------------------------------------------------------ api
    def segment_bounds(self, ad: dict) -> Optional[tuple[float, float, float, int]]:
        """Retourne (min, max, moyenne, n) du segment de l'annonce ; None sinon."""
        brand, model, year = ad.get("brand"), ad.get("model"), ad.get("year")
        if not (brand and year):
            return None
        ref = self.by_model.get((brand, model, year))
        if not ref or ref.get("n", 0) < self.min_sample:
            ref = self.by_brand.get((brand, year))
        if not ref or ref.get("n", 0) < self.min_sample:
            return None
        mn = ref.get("min_p"); mx = ref.get("max_p"); avg = ref.get("avg_p")
        n = ref.get("n", 0)
        return (mn, mx, avg, n)

    def price_ratio(self, ad: dict) -> tuple[Optional[float], Optional[str], int]:
        """(prix / prix de reference, libelle du segment, taille de l'echantillon)."""
        price = ad.get("price")
        brand, model, year = ad.get("brand"), ad.get("model"), ad.get("year")
        if not price or not brand or not year:
            return None, None, 0

        ref = self.by_model.get((brand, model, year))
        label = f"{brand} {model} {year}"
        if not ref or ref["n"] < self.min_sample:
            ref = self.by_brand.get((brand, year))
            label = f"{brand} {year}"
        if not ref or ref["n"] < self.min_sample:
            return None, None, 0
        avg = ref.get("avg_p")
        if not avg:
            return None, None, 0
        return price / float(avg), label, int(ref["n"])

    def stats(self) -> dict:
        return {
            "segments_modele": len(self.by_model),
            "segments_marque": len(self.by_brand),
        }

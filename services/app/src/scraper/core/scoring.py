"""Scoring des annonces.

Deux generations cohabitent :

* `note` (legacy) : portage fidele de l'ancien projet
  (voitures-maroc-sy / Crawler.php::get_note_avito + CalculatorCommand.php).
  Inchange, pour compatibilite avec l'ancien back-office.

* `note_v2` : axes orientes "aide aux acheteurs" :
    - note_etat   : qualite reelle du vehicule
    - note_achat  : rapport qualite/prix (le plus utile pour un acheteur)
    - note_global : combinaison des deux
    - note_confiance : part des criteres reellement connus (0-100 %)

Points cle du v2 :
  * une donnee manquante n'est PAS notee 0 : elle est exclue du calcul et
    compte dans la confiance (le legacy penalisait l'info manquante)
  * les poids et seuils sont dans un JSON de configuration, modifiable sans
    rebuild
"""

from __future__ import annotations

import json
import os
import re
import unicodedata
from datetime import datetime
from typing import Any, Optional

DEFAULT_CONFIG_PATH = os.environ.get("SCORING_CONFIG", "/app/config/scoring.json")

# ---------------------------------------------------------------- notes legacy
AGE_SCORES = {
    0: 20, 1: 20, 2: 19, 3: 18, 4: 17, 5: 16, 6: 15, 7: 13,
    8: 11, 9: 11, 10: 10, 11: 10, 12: 10, 13: 9, 14: 7, 15: 5, 16: 3, 17: 1,
}
KM_SCORES = [(10_000, 20), (50_000, 16), (100_000, 13),
             (160_000, 10), (200_000, 8), (250_000, 5)]
RATIO_SCORES = [(8_000, 20), (10_000, 18), (12_000, 17), (14_000, 15),
                (16_000, 13), (18_000, 11), (22_000, 10), (25_000, 8),
                (27_000, 6), (30_000, 3)]
FUEL_SCORES = {"Hybride": 20, "Diesel": 15, "Essence": 10, "Electrique": 5}
GEAR_SCORES = {"Automatique": 20, "Manuelle": 10}
CV_SCORE_LOW, CV_SCORE_MID, CV_SCORE_HIGH = 20, 12, 5

COMFORT_SPECS = [
    "CD/MP3/Bluetooth", "Climatisation", "Système de navigation/GPS",
    "Vitres électriques", "Sièges cuir", "Ordinateur de bord",
    "Verrouillage centralisé à distance", "Radar de recul", "Caméra de recul",
]
SAFETY_SPECS = [
    "ABS", "ESP", "Régulateur de vitesse", "Airbags", "Limiteur de vitesse",
]
LOOK_SPECS = ["Jantes aluminium"]
SUNROOF_SPEC = "Toit ouvrant"

WEIGHTS = {
    "annee": 3, "km": 3, "ratio": 2, "cv": 3,
    "toit": 1, "carburant": 3, "boite": 2, "specs": 2,
}
TOTAL_WEIGHT = 19


def _bucket(value: float, table: list[tuple[float, int]]) -> int:
    for limit, score in table:
        if value < limit:
            return score
    return 0


def score_specs(ad: dict) -> float:
    labels = set(ad.get("option_labels") or [])
    confort = sum(1 for s in COMFORT_SPECS if s in labels)
    securite = sum(1 for s in SAFETY_SPECS if s in labels)
    look = sum(1 for s in LOOK_SPECS if s in labels)
    total = 0.0
    if confort:
        total += (confort * 20 / 9) * 2
    if securite:
        total += (securite * 20 / 5) * 3
    if look:
        total += look * 20
    return round(total / 6, 2)


def score_cv(ad: dict) -> int:
    raw = ad.get("fiscal_power") or ""
    try:
        cv = int(str(raw).split()[0])
    except (ValueError, IndexError):
        return 0
    if cv < 8:
        return CV_SCORE_LOW
    if cv < 11:
        return CV_SCORE_MID
    if cv < 15:
        return CV_SCORE_HIGH
    return 0


def score_age(ad: dict, today: Optional[datetime] = None) -> int:
    year = _num(ad.get("year"))
    if not year:
        return 0
    current = (today or datetime.now()).year
    return AGE_SCORES.get(current - int(year), 0)


def score_ratio(ad: dict, age_years: int, max_km_per_year: float = 30000,
                floor: float = 2) -> int:
    """Note sur le kilometrage annuel moyen (coherence age/km).

    Au-dela de max_km_per_year la note ne tombe pas a zero mais au
    plancher : une incoherence doit etre signalee, pas effacer le critere.
    """
    km = ad.get("mileage_km")
    if km is None:
        return 0
    if not age_years:
        return 20
    ratio = km / age_years
    if ratio > max_km_per_year:
        return int(floor)
    return _bucket(ratio, RATIO_SCORES)


def _num(v: Any) -> Optional[float]:
    """Convertit une valeur en nombre (tolere '90000 km', '2012', 2012...)."""
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    m = re.search(r"-?\d+(?:[.,]\d+)?", str(v))
    return float(m.group(0).replace(",", ".")) if m else None


def compute_notes(ad: dict, today: Optional[datetime] = None) -> dict:
    """Sous-notes legacy + note globale legacy."""
    year = _num(ad.get("year"))
    year = int(year) if year is not None else None
    age_years = (today or datetime.now()).year - year if year else 0
    km = _num(ad.get("mileage_km"))
    notes = {
        "annee": score_age(ad, today),
        "km": _bucket(km, KM_SCORES) if km is not None else 0,
        "ratio": score_ratio(ad, age_years),
        "cv": score_cv(ad),
        "toit": 20 if SUNROOF_SPEC in (ad.get("option_labels") or []) else 0,
        "carburant": FUEL_SCORES.get(ad.get("fuel"), 0),
        "boite": GEAR_SCORES.get(ad.get("gearbox"), 0),
        "specs": score_specs(ad),
    }
    weighted = sum(notes[k] * w for k, w in WEIGHTS.items())
    notes["global"] = round(weighted / TOTAL_WEIGHT, 2)
    notes["km_year"] = round(km / age_years) if (km and age_years) else None
    notes["age_years"] = age_years
    return notes


# ------------------------------------------------------------------- notes v2
def load_config(path: str = DEFAULT_CONFIG_PATH) -> dict:
    """Charge la configuration des poids/seuils (valeurs par defaut si absente)."""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _lookup(table: dict, value: Any) -> Optional[float]:
    """Cherche une valeur (insensible a la casse/accents-espaces)."""
    if value is None:
        return None
    v = str(value).strip()
    if v in table:
        return float(table[v])
    key = " ".join(v.lower().split())
    for k, val in table.items():
        if " ".join(str(k).lower().split()) == key:
            return float(val)
    return None


def _score_by_thresholds(value: float, seuils: list[dict], mode: str) -> Optional[float]:
    """Seuils type {max: X, score: S} ou {ratio/min: X, score: S}."""
    for s in seuils:
        if mode == "max" and value <= s["max"]:
            return float(s["score"])
        if mode == "min" and value >= s["min"]:
            return float(s["score"])
        if mode == "ratio" and value <= s["ratio"]:
            return float(s["score"])
    return 0.0


def _weighted_mean(scores: dict, weights: dict) -> tuple[Optional[float], float]:
    """Moyenne ponderee sur les criteres connus + part de poids couverte."""
    num = den = 0.0
    for key, sc in scores.items():
        if sc is None:
            continue
        w = float(weights.get(key, 0))
        if w <= 0:
            continue
        num += float(sc) * w
        den += w
    total_w = sum(float(w) for w in weights.values() if w > 0)
    if den <= 0:
        return None, 0.0
    return round(num / den, 2), round(100 * den / total_w, 1)




def _norm_score_text(v) -> str:
    if v is None:
        return ""
    if not isinstance(v, str):
        v = str(v)
    return unicodedata.normalize("NFKD", v).encode("ascii", "ignore").decode("ascii").lower()


def _is_non_dedouanee(ad: dict) -> bool:
    """True si l'annonce indique un vehicule non dedouane."""
    values = []
    for key in ("origin", "cond", "body_type", "description"):
        v = ad.get(key)
        if isinstance(v, str):
            values.append(v)
    raw = ad.get("raw_json")
    if isinstance(raw, dict):
        for v in raw.values():
            if isinstance(v, str):
                values.append(v)
    blob = " ".join(values)
    n = _norm_score_text(blob)
    return any(k in n for k in ("pas encore dedouan", "non dedouan", "not custom cleared"))


# surcharge estimee des frais de dedouanement pour le scoring achat
CUSTOMS_SURCHARGE = 0.50


def compute_notes_v2(ad: dict, market: Any = None,
                     config: Optional[dict] = None,
                     today: Optional[datetime] = None) -> dict:
    """Notes v2 : etat, achat, global, confiance + detail des criteres.

    market : objet optionnel exposant le prix median du segment
             (voir market.MarketStats).
    """
    cfg = config if config is not None else load_config()
    etat_cfg = cfg.get("etat", {})
    achat_cfg = cfg.get("achat", {})
    glob_cfg = cfg.get("global", {})

    legacy = compute_notes(ad)
    age_years = legacy["age_years"]
    year_num = _num(ad.get("year"))
    if year_num:
        age_years = (today or datetime.now()).year - int(year_num)

    # ---------------------------------------------------------- axe etat
    achat_info: dict[str, Any] = {}
    flags: list[str] = []

    etat_weights = etat_cfg.get("weights", {})
    ratio_cfg = etat_cfg.get("ratio", {})
    km_v = ad.get("mileage_km")
    if km_v and age_years and km_v / age_years > float(ratio_cfg.get("max", 30000)):
        flags.append(f"km/an incoherent ({round(km_v / age_years)} km/an)")
    etat_scores = {
        "etat": _lookup(etat_cfg.get("etat_values", {}), ad.get("cond")),
        "first_hand": _lookup(etat_cfg.get("first_hand_values", {}), ad.get("first_hand")),
        "origine": _lookup(etat_cfg.get("origine_values", {}), ad.get("origin")),
        "doors": _lookup(etat_cfg.get("doors_values", {}), ad.get("doors")),
        "year": legacy["annee"] or None,
        "km": legacy["km"] or None,
        "ratio": score_ratio(ad, age_years,
                             float(ratio_cfg.get("max", 30000)),
                             float(ratio_cfg.get("floor", 2))) or None,
        "specs": legacy["specs"] or None,
    }
    note_etat, conf_etat = _weighted_mean(etat_scores, etat_weights)

    # ---------------------------------------------------------- axe achat
    raw_price = _num(ad.get("price"))
    non_dedouanee = _is_non_dedouanee(ad)
    if non_dedouanee and raw_price:
        effective_price = raw_price * (1.0 + CUSTOMS_SURCHARGE)
        flags.append(f"non dedouane : dedouanement estime +{int(CUSTOMS_SURCHARGE * 100)} pourcent")
    else:
        effective_price = raw_price
    price = effective_price
    achat_weights = achat_cfg.get("weights", {})
    achat_scores: dict[str, Optional[float]] = {
        "prix_vs_marche": None,
        "prix_par_annee_restante": None,
        "prix_par_km": None,
        "valeur_pour_prix": None,
    }
    market_info: dict[str, Any] = {}

    if price:
        # le prix vs le marche a besoin des statistiques de marche
        if market is not None:
            market_ad = dict(ad)
            market_ad["price"] = price
            ratio, ref, ref_n = market.price_ratio(market_ad)
            if ratio:
                market_info = {"ratio": round(ratio, 3), "reference": ref, "n": ref_n}
                achat_scores["prix_vs_marche"] = _score_by_thresholds(
                    ratio, achat_cfg.get("prix_vs_marche", {}).get("seuils", []), "ratio")
        # les autres criteres de prix ne dependent que de l'annonce
        # cout par annee de vie RESTANTE (on paie moins par annee qui reste)
        life = float(achat_cfg.get("prix_par_annee_restante", {}).get("life_years", 15))
        remaining = max(1, int(life - age_years))
        achat_scores["prix_par_annee_restante"] = _score_by_thresholds(
            price / remaining,
            achat_cfg.get("prix_par_annee_restante", {}).get("seuils", []), "max")
        achat_info["annees_restantes"] = remaining
        achat_info["par_annee_restante"] = round(price / remaining)
        achat_info["prix_effectif"] = round(price)
        achat_info["douane_surcharge"] = _is_non_dedouanee(ad)
        km = ad.get("mileage_km")
        if km:
            achat_scores["prix_par_km"] = _score_by_thresholds(
                price / km,
                achat_cfg.get("prix_par_km", {}).get("seuils", []), "max")
        if note_etat:
            achat_scores["valeur_pour_prix"] = _score_by_thresholds(
                note_etat / (price / 100000.0),
                achat_cfg.get("valeur_pour_prix", {}).get("seuils", []), "min")

    note_achat, conf_achat = _weighted_mean(achat_scores, achat_weights)

    # ---------------------------------------------------------- global
    # Poids FIXES (etat 65 %, achat 35 %) : un axe non evaluable (pas de
    # prix -> achat impossible) ne fait pas monter la note, il la plafonne.
    # Sans ce garde-fou, une annonce "sur demande" avec un tres bon etat
    # sortait a 20/20, ce qui est trompeur pour un acheteur.
    w_e = float(glob_cfg.get("etat_weight", 0.65))
    w_a = float(glob_cfg.get("achat_weight", 0.35))
    total_w = w_e + w_a
    if note_etat is None and note_achat is None:
        note_global = None
    else:
        note_global = round(
            (w_e * (note_etat or 0.0) + w_a * (note_achat or 0.0)) / total_w, 2)
    confiance = round((conf_etat * w_e + conf_achat * w_a) / (w_e + w_a), 1)

    return {
        "note_etat": note_etat,
        "note_achat": note_achat,
        "note_global": note_global,
        "note_confiance": confiance,
        "confiance_etat": conf_etat,
        "confiance_achat": conf_achat,
        "detail_etat": {k: v for k, v in etat_scores.items()},
        "detail_achat": {k: v for k, v in achat_scores.items()},
        "achat_info": achat_info,
        "flags": flags,
        "marche": market_info,
        "legacy": legacy,
    }

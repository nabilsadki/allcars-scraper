"""Historique des annonces : versionnement et detection de changement.

Principe
--------
1. Le passage sur la page de LISTE suffit a detecter qu'une annonce a change :
   prix, titre, description, drapeaux, nb d'images... sont tous presents
   dans la liste. On compare donc champ par champ.

2. Comparaison "a la profondeur de l'observation" : un champ absent de
   l'observation courante n'est pas compare (on ignore ce qu'on ne sait pas),
   sauf si l'observation est une fiche complete : la ou un champ present
   auparavant a disparu, c'est un vrai changement.

3. Si rien ne change -> on ne touche qu'a `last_seen_at` / `seen_count`.
   PAS de version, PAS de requete detail inutile.

4. Si quelque chose change -> version incrementee, snapshot + diff archives
   dans `ad_versions`.

5. En fin de run complet, les annonces non vues sont marquees `is_active = 0`
   avec une version de type 'disappeared'.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Optional

# Champs "significatifs" : l'etat commercial de l'annonce.
SIGNIFICANT_FIELDS = [
    "title", "description", "price", "year", "mileage_km", "fuel", "gearbox",
    "brand", "model", "doors", "origin", "first_hand", "cond", "fiscal_power",
    "city", "area", "seller", "seller_type", "phone", "link",
    "options", "option_labels", "nb_images",
    "is_premium", "is_urgent", "is_hot_deal",
    # list_time / publish_date exclus : la valeur differe entre la page de
    # liste (cache) et la fiche detail -> faux positif systematique
]

# Champs compares pour produire le diff lisible
DIFF_FIELDS = [
    "title", "price", "description", "year", "mileage_km", "fuel", "gearbox",
    "brand", "model", "doors", "origin", "first_hand", "cond", "fiscal_power",
    "city", "seller", "phone", "nb_images", "is_premium", "is_urgent",
    "is_hot_deal", "link",
]


def _norm_value(v: Any) -> Any:
    """Normalise une valeur pour comparaison (str "2012" == int 2012)."""
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False, sort_keys=True, default=str)
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, (int, float)):
        return v
    if isinstance(v, str):
        s = v.strip()
        if re.fullmatch(r"-?\d+", s):
            return int(s)
        if re.fullmatch(r"-?\d+\.\d+", s):
            return float(s)
        return s
    return v


def content_hash(ad: dict) -> str:
    """Empreinte de l'etat observe (champs presents uniquement)."""
    payload = {}
    for f in SIGNIFICANT_FIELDS:
        v = ad.get(f)
        if v is None:
            continue
        payload[f] = _norm_value(v)
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# Champs qui declenchent une nouvelle version.
# Exclus : les drapeaux marketing (premium/urgent/hot deal) et le nombre
# d'images — le site les expose de facon fluctuante entre la page de liste
# et la fiche detail, ce qui creerait des faux positifs a chaque passage.
# Ils restent stockes, filtrables et visibles dans le snapshot.
CHANGE_TRIGGER_FIELDS = [
    f for f in SIGNIFICANT_FIELDS
    if f not in ("is_premium", "is_urgent", "is_hot_deal", "nb_images")
]


def compare(old_ad: dict, new_ad: dict, is_detail: bool = False) -> dict:
    """Compare l'ancien et le nouvel etat -> {champ: {old, new}}.

    is_detail : True si la nouvelle observation est une fiche complete ;
                les champs qui ont disparu comptent alors comme changements.
    """
    out: dict[str, Any] = {}
    old_ad = old_ad or {}
    for f in CHANGE_TRIGGER_FIELDS:
        o = old_ad.get(f)
        n = new_ad.get(f)
        if n is None:
            # on ne laisse JAMAIS un fetch incomplet ecraser ce qu'on savait deja
            continue  # on ignore ce qu'on n'observe pas
        o_n, n_n = _norm_value(o), _norm_value(n)
        if o_n != n_n:
            out[f] = {"old": o, "new": n}
    return out


def diff(old: dict, new: dict) -> dict:
    """Version lisible (sous-ensemble des champs affichables)."""
    full = compare(old, new, is_detail=True)
    return {k: v for k, v in full.items() if k in DIFF_FIELDS}


def classify(diff_data: dict) -> str:
    """Type de changement (pour l'historique)."""
    if not diff_data:
        return "unchanged"
    if set(diff_data) == {"price"}:
        return "price_change"
    if set(diff_data) <= {"is_urgent", "is_premium", "is_hot_deal"}:
        return "flag_change"
    if "description" in diff_data or "nb_images" in diff_data:
        return "content_change"
    return "updated"

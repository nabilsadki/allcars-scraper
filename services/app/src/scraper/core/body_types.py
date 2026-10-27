from typing import Optional


_MODEL_TYPE: dict[str, str] = {
    "dacia duster": "SUV", "dacia sandero": "Citadine", "dacia logan": "Berline",
    "dacia lodgy": "Monospace", "dacia jogger": "SUV", "dacia dokker": "Utilitaire",
    "renault clio": "Citadine", "renault megane": "Berline", "renault scenic": "Berline",
    "renault kadjar": "SUV", "renault kaptur": "SUV", "renault kjar": "SUV",
    "peugeot 208": "Citadine", "peugeot 2008": "SUV", "peugeot 308": "Berline",
    "peugeot 3008": "SUV", "peugeot 5008": "SUV", "peugeot 508": "Berline",
    "peugeot 107": "Citadine", "peugeot 108": "Citadine", "peugeot partner": "Berline",
    "volkswagen golf": "Citadine", "volkswagen polo": "Citadine",
    "volkswagen passat": "Berline", "volkswagen tiguan": "SUV",
    "volkswagen touareg": "SUV", "volkswagen jetta": "Berline",
    "volkswagen t-cross": "SUV", "volkswagen t-roc": "SUV", "volkswagen touran": "Monospace",
    "volkswagen caddy": "Utilitaire",
    "mercedes-benz classe a": "Citadine", "mercedes-benz classe b": "Citadine",
    "mercedes-benz classe c": "Berline", "mercedes-benz classe e": "Berline",
    "mercedes-benz classe s": "Berline", "mercedes-benz gla": "SUV",
    "mercedes-benz glc": "SUV", "mercedes-benz gle": "SUV", "mercedes-benz cls": "Berline",
    "mercedes-benz cla": "Berline",
    "bmw serie 1": "Compact", "bmw serie 2": "Compact", "bmw serie 3": "Berline",
    "bmw serie 4": "Berline", "bmw serie 5": "Berline", "bmw serie 6": "Berline",
    "bmw x1": "SUV", "bmw x3": "SUV", "bmw x4": "SUV", "bmw x5": "SUV", "bmw x6": "SUV",
    "hyundai i10": "Citadine", "hyundai i20": "Citadine", "hyundai i30": "Compact",
    "hyundai tucson": "SUV", "hyundai santa fe": "SUV", "hyundai kona": "SUV",
    "hyundai elantra": "Berline", "hyundai sonata": "Berline", "hyundai h1": "Utilitaire",
    "kia picanto": "Citadine", "kia rio": "Citadine", "kia sportage": "SUV",
    "kia sorento": "SUV", "kia ceed": "Compact", "kia niro": "SUV",
    "toyota yaris": "Citadine", "toyota corolla": "Berline", "toyota camry": "Berline",
    "toyota rav4": "SUV", "toyota land cruiser": "SUV", "toyota fortuner": "SUV",
    "toyota hilux": "Pick-up", "toyota prius": "Berline",
    "nissan qashqai": "SUV", "nissan juke": "SUV", "nissan micra": "Citadine",
    "nissan x-trail": "SUV", "nissan patrol": "SUV",
    "skoda fabia": "Citadine", "skoda octavia": "Berline", "skoda superb": "Berline",
    "skoda kodiaq": "SUV", "skoda karoq": "SUV",
    "citroen c3": "Citadine", "citroen c4": "Compact", "citroen c5 aircross": "SUV",
    "citroen berlingo": "Utilitaire",
    "ford fiesta": "Citadine", "ford focus": "Compact", "ford mondeo": "Berline",
    "ford kuga": "SUV", "ford ranger": "Pick-up", "ford transit": "Utilitaire",
    "opel corsa": "Citadine", "opel astra": "Compact", "opel insignia": "Berline",
    "opel mokka": "SUV", "opel grandland": "SUV",
    "honda civic": "Compact", "honda cr-v": "SUV", "honda jazz": "Citadine",
    "audi a1": "Citadine", "audi a3": "Compact", "audi a4": "Berline",
    "audi a5": "Berline", "audi a6": "Berline", "audi q3": "SUV",
    "audi q5": "SUV", "audi q7": "SUV", "audi q8": "SUV",
    "fiat 500": "Citadine", "fiat punto": "Citadine",
    "seat ibiza": "Citadine", "seat leon": "Compact", "seat ateca": "SUV",
    "mitsubishi outlander": "SUV", "mitsubishi pajero": "SUV", "mitsubishi l200": "Pick-up",
    "mitsubishi asx": "SUV", "mitsubishi mirage": "Citadine",
    "range rover sport": "SUV", "land rover discovery": "SUV",
    "porsche cayenne": "SUV", "porsche macan": "SUV", "porsche panamera": "Berline",
    "volvo v40": "Compact", "volvo xc40": "SUV", "volvo xc60": "SUV", "volvo s60": "Berline",
}

_KEYWORDS = [
    ("duster", "SUV"), ("qashqai", "SUV"), ("tiguan", "SUV"), ("tucson", "SUV"),
    ("sportage", "SUV"), ("cayenne", "SUV"), ("macan", "SUV"), ("rav4", "SUV"),
    ("fortuner", "SUV"), ("land cruiser", "SUV"), ("discovery", "SUV"),
    ("juke", "SUV"), ("x-trail", "SUV"), ("pathfinder", "SUV"),
    ("mokka", "SUV"), ("grandland", "SUV"), ("2008", "SUV"),
    ("3008", "SUV"), ("5008", "SUV"), ("suv", "SUV"), ("4x4", "SUV"),
    ("rangerv", "Pick-up"), ("hilux", "Pick-up"), ("ranger", "Pick-up"), ("l200", "Pick-up"),
    ("berlingo", "Utilitaire"), ("partner", "Utilitaire"),
    ("caddy", "Utilitaire"), ("vito", "Utilitaire"), ("transit", "Utilitaire"),
    ("clio", "Citadine"), ("yaris", "Citadine"), ("micra", "Citadine"),
    ("picanto", "Citadine"), ("golf", "Compact"), ("polo", "Citadine"),
    ("logan", "Berline"), ("passat", "Berline"), ("classe c", "Berline"), ("classe e", "Berline"),
]


def infer_body_type(ad: dict) -> Optional[str]:
    explicit = ad.get("body_type") or ad.get("carrosserie") or ad.get("body")
    if isinstance(explicit, str) and explicit.strip():
        low = explicit.strip().lower()
        mapping = {
            "berline": "Berline", "citadine": "Citadine", "suv": "SUV",
            "compact": "Compact", "compacte": "Compact",
            "break": "Break", "coupé": "Coupé", "cabrio": "Cabriolet",
            "monospace": "Monospace", "pick-up": "Pick-up",
            "utilitaire": "Utilitaire", "4x4": "SUV",
        }
        for cand, canonical in mapping.items():
            if cand in low:
                return canonical

    brand = str(ad.get("brand") or "").strip().lower()
    model = str(ad.get("model") or "").strip().lower()
    title = str(ad.get("title") or "").strip().lower()
    key_variants = [f"{brand} {model}".strip(), model, title]
    for k in key_variants:
        if k and k in _MODEL_TYPE:
            return _MODEL_TYPE[k]
    for k in key_variants:
        kl = k.strip()
        for needle, typ in _MODEL_TYPE.items():
            if kl.startswith(needle + " ") and len(needle) > 4:
                return typ
            if kl.startswith(needle) and len(needle) > 8:
                return typ
    hay = " ".join(key_variants)
    for needle, typ in _KEYWORDS:
        if needle in hay:
            return typ
    return None
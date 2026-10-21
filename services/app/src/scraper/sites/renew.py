"""Adaptateur Renew Maroc (renew.ma).

Le site est une application Nuxt (Vue) : le catalogue est rendu cote
serveur et l'etat complet est serialise dans le payload `window.__NUXT__`
(format "devalue" : les valeurs sont referencees par des parametres).

Ce module implementent un lecteur de ce format, ce qui donne acces a
l'integralite du catalogue (412 voitures) en UNE seule requete, avec les
equipements, les visuels, la date de mise en circulation, etc.

L'API etant cote client, seule l'URL de la fiche est connue : elle est
construite a partir du modele et de l'identifiant argus.
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

from ..core.site import SiteAdapter, make_ad

BASE = "https://www.renew.ma"
LISTING = f"{BASE}/catalogue"
PHOTO_BASE = "https://prod.renew.ma/renew/argus/datas/photos/"


# ------------------------------------------------------------------ devalue
def _balanced_end(text: str, start: int) -> int:
    """Index de la fin de l'objet/literal qui commence a `start`."""
    depth = 0
    instr = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if esc:
            esc = False
            continue
        if ch == "\\":
            esc = True
            continue
        if ch == '"':
            instr = not instr
            continue
        if instr:
            continue
        if ch in "{[(":
            depth += 1
        elif ch in "}])":
            depth -= 1
            if depth == 0:
                return i
    return -1


def _protect(text: str) -> str:
    """Rend un literal JS compatible JSON (identifiants -> marqueurs)."""
    out = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == '"':
            j = i + 1
            while j < n:
                if text[j] == "\\":
                    j += 2
                    continue
                if text[j] == '"':
                    break
                j += 1
            out.append(text[i:j + 1])
            i = j + 1
            continue
        if ch.isalpha() or ch in "_$":
            j = i
            while j < n and (text[j].isalnum() or text[j] in "_$"):
                j += 1
            word = text[i:j]
            if word in ("true", "false", "null"):
                out.append(word)
            elif text[j:j + 1] == ":":
                out.append('"%s"' % word)
            else:
                out.append('"@@%s@@"' % word)
            i = j
            continue
        out.append(ch)
        i += 1
    return "".join(out)


class NuxtState:
    """Etat serialise dans `window.__NUXT__`."""

    def __init__(self, html: str):
        self.values: dict[str, Any] = {}
        self.body = ""
        self._parse(html)

    def _parse(self, html: str) -> None:
        i = html.find("window.__NUXT__")
        if i < 0:
            return
        j = html.find("</script>", i)
        payload = html[i:j if j > 0 else len(html)]
        m = re.search(r"\(function\(([^)]*)\)\{", payload)
        if not m:
            return
        params = [x.strip() for x in m.group(1).split(",")]
        body_start = m.end()
        depth = 1
        instr = False
        esc = False
        body_end = body_start
        for k in range(body_start, len(payload)):
            ch = payload[k]
            if esc:
                esc = False
                continue
            if ch == "\\":
                esc = True
                continue
            if ch == '"':
                instr = not instr
                continue
            if instr:
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    body_end = k
                    break
        self.body = payload[body_start:body_end]
        rest = payload[body_end + 1:]
        k = rest.find("(")
        if k < 0:
            return
        args_end = _balanced_end(rest, k)
        if args_end < 0:
            return
        args_str = rest[k + 1:args_end]
        args_str = re.sub(r"\bundefined\b", "null", args_str)
        args_str = re.sub(r"\bArray\((\d+)\)", "null", args_str)
        try:
            args = json.loads("[" + args_str + "]")
        except Exception:
            args = []
        self.values = dict(zip(params, args))

    # -------------------------------------------------------------- helpers
    def resolve(self, node: Any, depth: int = 0) -> Any:
        if depth > 8:
            return None
        if isinstance(node, list):
            return [self.resolve(v, depth + 1) for v in node]
        if isinstance(node, dict):
            return {k: self.resolve(v, depth + 1) for k, v in node.items()}
        if isinstance(node, str) and node.startswith("@@") and node.endswith("@@"):
            name = node[2:-2]
            if name in self.values:
                return self.resolve(self.values[name], depth + 1)
            return None
        return node

    def parse_literal(self, text: str) -> Any:
        text = text.strip()
        if re.fullmatch(r"[A-Za-z_$][\w$]*", text):
            name = text
            return self.resolve(self.values.get(name)) if name in self.values else None
        txt = re.sub(r"\bundefined\b", "null", text)
        try:
            return self.resolve(json.loads(_protect(txt)))
        except Exception:
            return None

    def objects(self) -> list[dict]:
        """Tous les objets « voiture » de l'etat.

        Deux formes coexistent : des litteraux d'objet `{id_offre:...}` et
        des objets construits par affectations `rS.id_offre=...`.
        """
        out = []
        seen = set()
        body = self.body

        # 1) litteraux d'objet contenant id_offre
        pos = 0
        while True:
            pos = body.find("id_offre:", pos)
            if pos < 0:
                break
            # remonter a l'accolade ouvrante
            start = body.rfind("{", 0, pos)
            if start < 0:
                pos += 10
                continue
            end = _balanced_end(body, start)
            if end < 0:
                pos += 10
                continue
            obj = self.parse_literal(body[start:end + 1])
            if isinstance(obj, dict) and obj.get("id_offre"):
                key = obj["id_offre"]
                if key not in seen:
                    seen.add(key)
                    out.append(obj)
            pos = end

        # 2) objets construits par affectations
        objs: dict[str, dict] = {}
        for m in re.finditer(r"(\w+)\.(\w+)=", body):
            var, field = m.group(1), m.group(2)
            start = m.end()
            end = _balanced_end(body, start - 1) if body[start - 1] in "{[(" else -1
            if end < 0:
                # valeur simple : jusqu'au ';'
                depth = 0
                instr = False
                esc = False
                end = start
                for k in range(start, len(body)):
                    ch = body[k]
                    if esc:
                        esc = False
                        continue
                    if ch == "\\":
                        esc = True
                        continue
                    if ch == '"':
                        instr = not instr
                        continue
                    if instr:
                        continue
                    if ch in "{[(":
                        depth += 1
                    elif ch in "}])":
                        depth -= 1
                    elif ch == ";" and depth == 0:
                        end = k
                        break
            objs.setdefault(var, {})[field] = self.parse_literal(body[start:end])
        for var, obj in objs.items():
            if obj.get("id_offre") and obj["id_offre"] not in seen:
                seen.add(obj["id_offre"])
                out.append(obj)
        return out


# ------------------------------------------------------------------ adaptateur
def _num(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    digits = re.sub(r"[^\d]", "", str(value))
    return int(digits) if digits else None


class RenewAdapter(SiteAdapter):
    name = "renew"
    label = "Renew Maroc"
    default_category = "voiture-occasion"
    categories = ["voiture-occasion"]
    page_size_fixed = True

    # ------------------------------------------------------------- listing
    def listing_url(self, category: str, offset: int) -> str:
        return LISTING

    def parse_listing(self, html: str, category: str = "") -> list[dict]:
        state = NuxtState(html)
        cars = state.objects()
        out = []
        for car in cars:
            ad = self._normalize(car)
            if ad:
                out.append(ad)
        return out

    def _normalize(self, car: dict) -> Optional[dict]:
        argus = car.get("argusid")
        if not argus:
            return None
        marque = (car.get("marque") or "").strip()
        modele = (car.get("model") or "").strip()
        version = (car.get("version") or "").strip()
        title = " ".join(x for x in (marque, modele, version) if x).strip()
        if not title:
            title = f"{marque} {car.get('categorie', '')}".strip()
        price = _num(car.get("prix"))
        images = []
        for vis in (car.get("visuels") or []):
            fname = vis.get("file_name") if isinstance(vis, dict) else None
            if fname:
                images.append(f"{PHOTO_BASE}{fname}")
        default_img = car.get("default_img")
        if default_img:
            images.insert(0, f"{PHOTO_BASE}{default_img}")
        # deduplication
        seen = set()
        images = [i for i in images if not (i in seen or seen.add(i))]
        ad = make_ad(
            ad_id=str(argus),
            title=title,
            url=f"{BASE}/voiture-occasion/{modele or marque}-{argus}",
            price=price,
            price_str=(f"{price:,}".replace(",", " ") + " DH") if price else None,
            mileage_km=_num(car.get("kilometrage")),
            year=_num(car.get("annee")),
            brand=marque.title() if marque else None,
            model=modele.title() if modele else None,
            fuel=(car.get("motorisation") or "").strip().title() or None,
            city=(car.get("ville") or "").strip().title() or None,
            category=(car.get("categorie") or "Voiture d'occasion").strip(),
        )
        ad["list_id"] = str(argus)
        # version (descriptif) + equipements
        options = {}
        labels = []
        if version:
            options["version"] = version
            labels.append(version)
        for key in ("couleur", "garantie", "statutstock", "puissance_moteur", "offre_du_moment"):
            val = car.get(key)
            if val:
                options[key] = val
        equip = car.get("equipements")
        if isinstance(equip, dict):
            for cat, items in equip.items():
                if isinstance(items, list):
                    for it in items:
                        if isinstance(it, str) and it:
                            options[f"equip_{cat}"] = options.get(f"equip_{cat}", []) + [it] \
                                if isinstance(options.get(f"equip_{cat}"), list) else [it]
                            labels.append(it)
        elif isinstance(equip, list):
            for it in equip:
                if isinstance(it, str) and it:
                    labels.append(it)
        ad["options"] = options
        ad["option_labels"] = labels
        # dates
        date = car.get("date")
        if isinstance(date, str) and re.fullmatch(r"\d{2}/\d{2}/\d{4}", date):
            ad["publish_date"] = date[6:10] + "-" + date[3:5] + "-" + date[0:2]
        if images:
            ad["images"] = [{"url": u, "url_full_hd": u, "thumb": u} for u in images]
            ad["nb_images"] = len(images)
        tel = car.get("tel")
        if isinstance(tel, str) and tel.strip():
            ad["phone"] = tel.strip()
        return ad

    def total_ads(self, html: str) -> Optional[int]:
        state = NuxtState(html)
        cars = state.objects()
        return len(cars) or None

    # -------------------------------------------------------------- detail
    def detail_url(self, ad: dict) -> Optional[str]:
        return ad.get("url")

    def parse_detail(self, html: str, ad: dict) -> dict:
        out = dict(ad)
        images = [i["url"] for i in (ad.get("images") or [])]
        for url in re.findall(r'https://prod\.renew\.ma/renew/argus/datas/photos/[^"\']+', html):
            if url not in images:
                images.append(url)
        if images:
            out["images"] = [{"url": u, "url_full_hd": u, "thumb": u} for u in images]
            out["nb_images"] = len(images)
        m = re.search(r'<div[^>]+class="[^"]*car-description[^"]*"[^>]*>(.*?)</div>', html, re.S)
        if m:
            desc = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", m.group(1))).strip()
            if len(desc) > 20:
                out["description"] = desc
        return out

    # -------------------------------------------------------------- images
    def image_url(self, img: dict, variant: str = "images") -> str:
        return img.get("url") or ""

"""Lecture du suivi de progression partage avec le scraper.

Le scraper ecrit /data/progress/<site>.json (voir scraper/core/progress.py)
et lit /data/scrape_job.json pour ses parametres.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any, Optional

PROGRESS_DIR = os.environ.get("PROGRESS_DIR", "/data/progress")
JOB_PATH = os.environ.get("SCRAPE_JOB_FILE", "/data/scrape_job.json")


def _parse_dt(value: str) -> Optional[datetime]:
    if not value:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(value[:19], fmt)
        except ValueError:
            continue
    return None


def read_all() -> list[dict]:
    """Tous les etats de progression (un par site), le plus recent d'abord."""
    out = []
    try:
        names = [f for f in os.listdir(PROGRESS_DIR) if f.endswith(".json")]
    except Exception:
        return out
    for name in names:
        try:
            with open(os.path.join(PROGRESS_DIR, name), encoding="utf-8") as fh:
                data = json.load(fh)
            out.append(enrich(data))
        except Exception:
            continue
    out.sort(key=lambda d: d.get("updated_at") or "", reverse=True)
    return out


def read_progress(site: Optional[str] = None) -> Optional[dict]:
    """Etat de progression d'un site (ou le plus recent si site est None)."""
    states = read_all()
    if not states:
        return None
    if site:
        for st in states:
            if st.get("site") == site:
                return st
        return None
    return states[0]


def enrich(state: dict) -> dict:
    """Complete l'etat brut : temps ecoule, vitesse, temps restant, %."""
    st = dict(state or {})
    st.setdefault("site", "")
    st.setdefault("status", "unknown")
    st.setdefault("listed", 0)
    st.setdefault("total", 0)
    st.setdefault("blocked", 0)
    st.setdefault("errors", 0)
    st.setdefault("requests", 0)
    st.setdefault("details", 0)
    st.setdefault("inserted", 0)
    st.setdefault("updated", 0)
    st.setdefault("unchanged", 0)
    st.setdefault("slices_done", 0)
    st.setdefault("slices_total", 0)
    st.setdefault("current_slice", "")

    started = _parse_dt(st.get("started_at") or "")
    finished = _parse_dt(st.get("finished_at") or "")
    now = datetime.now()
    if started:
        elapsed = (finished - started).total_seconds() if finished else (now - started).total_seconds()
    else:
        elapsed = float(st.get("elapsed_seconds") or 0)
    st["elapsed_seconds"] = int(max(0, elapsed))

    listed = int(st.get("listed") or 0)
    total = int(st.get("total") or 0)
    rate = (listed / elapsed) if elapsed > 1 else 0.0
    st["rate_per_min"] = round(rate * 60, 1)
    if total and listed < total and rate > 0:
        st["eta_seconds"] = int((total - listed) / rate)
    else:
        st["eta_seconds"] = None
    st["percent"] = round(min(100.0, listed * 100.0 / total), 1) if total else None
    st["running"] = st.get("status") == "running"
    return st


def read_job() -> dict:
    """Parametres du job currently configure."""
    job = {
        "site": "avito",
        "category": "maroc/voitures-à_vendre",
        "mode": "list",
        "details": False,
        "limit": 0,
        "delay": 1.5,
        "jitter": 0.8,
        "slices": True,
    }
    try:
        with open(JOB_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            job.update(data)
    except Exception:
        pass
    return job


def write_job(job: dict) -> None:
    """Ecrit les parametres du job (lus au demarrage du scraper)."""
    os.makedirs(os.path.dirname(JOB_PATH), exist_ok=True)
    tmp = JOB_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(job, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, JOB_PATH)


def alerts(state: Optional[dict], container_state: str = "") -> list[dict]:
    """Alertes a afficher : erreur, blocage serveur, etat du conteneur."""
    out: list[dict] = []
    if not state:
        if container_state in ("exited", "absent"):
            out.append({"level": "warn",
                        "title": "Aucun scrap en cours",
                        "detail": "Le conteneur du scraper n'est pas lancé."})
        return out
    status = state.get("status")
    blocked = int(state.get("blocked") or 0)
    errors = int(state.get("errors") or 0)
    if status == "error":
        out.append({"level": "error", "title": "Le scrap s'est terminé en erreur",
                    "detail": state.get("error") or "Erreur inconnue"})
    if blocked:
        out.append({
            "level": "error",
            "title": f"Blocage serveur detecte ({blocked})",
            "detail": "Le serveur a renvoye un challenge Cloudflare / un code "
                      "403-429-503 pendant le run. Ralentir (delay/jitter) "
                      "puis relancer."})
    if errors:
        out.append({"level": "warn",
                    "title": f"{errors} erreur(s) reseau pendant le run",
                    "detail": "Des requetes ont echoue (elles sont rejouees "
                              "automatiquement, mais le rythme est a surveiller)."})
    if status == "stopped":
        out.append({"level": "warn", "title": "Scrap arrete manuellement",
                    "detail": state.get("error") or "Arret demande depuis l'interface."})
    if status == "running" and container_state == "exited":
        out.append({"level": "error",
                    "title": "Le conteneur est arrete alors que le suivi indique "
                             "un run en cours",
                    "detail": "Le processus a probablement ete tue (redemarrage "
                              "de la machine). Relancez le scrap."})
    return out

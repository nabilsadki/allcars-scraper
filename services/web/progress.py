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
JOB_DIR = os.environ.get("SCRAPE_JOB_DIR", "/data/jobs")


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


def pick_primary(states: list, container_state: str = "") -> Optional[dict]:
    """Choisit l'etat a mettre en avant.

    Priorite : le site en cours de scrape (status=running), sinon le plus
    recemment mis a jour.
    """
    if not states:
        return None
    running = [s for s in states if s.get("running")]
    if running:
        return max(running, key=lambda s: s.get("updated_at") or "")
    return states[0]


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
    st.setdefault("db_errors", 0)
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


def job_path(site: str) -> str:
    """Un fichier de job par site (les sites sont independants)."""
    return os.path.join(JOB_DIR, f"{site or 'avito'}.json")


def read_job(site: str = "") -> dict:
    """Parametres du job configure pour un site."""
    site = site or "avito"
    job = {
        "site": site,
        "category": "maroc/voitures-à_vendre",
        "mode": "list",
        "details": False,
        "limit": 0,
        "delay": 1.5,
        "jitter": 0.8,
        "slices": True,
    }
    try:
        with open(job_path(site), encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            job.update(data)
            job["site"] = site
    except Exception:
        pass
    return job


def write_job(job: dict) -> None:
    """Ecrit les parametres du job d'un site."""
    path = job_path(job.get("site", "avito"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(job, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


#: libelles du conteneur selon son etat et celui du dernier run
CONTAINER_LABELS = {
    "running": ("en cours", "s-running"),
    "exited": ("arrete", "s-stopped"),
    "created": ("pret a demarrer", "s-created"),
    "absent": ("absent", "s-absent"),
    "unknown": ("inconnu", "s-unknown"),
}


def container_status(container_state: str, state: Optional[dict]) -> tuple[str, str]:
    """(libelle, classe CSS) du conteneur, en tenant compte du dernier run.

    Un conteneur "exited" n'est pas forcement un probleme : si le dernier
    run s'est termine normalement, on affiche "termine".
    """
    label, css = CONTAINER_LABELS.get(container_state, (container_state or "?", "s-unknown"))
    if container_state in ("exited", "absent"):
        # le conteneur est ephemere : "absent" veut dire "run termine"
        status = (state or {}).get("status")
        if status == "success":
            return "termine", "s-done"
        if status == "error":
            return "erreur", "s-error"
        if status == "stopped":
            return "arrete", "s-stopped"
        if container_state == "absent":
            return "termine", "s-done"
        return "arrete", "s-stopped"
    if container_state == "running":
        if state and state.get("status") == "error":
            return "erreur", "s-error"
        return "en cours", "s-running"
    return label, css


#: au-dela de ce delai, les alertes d'un run termine ne sont plus affichees
ALERT_TTL = 15 * 60


def _state_age(state: Optional[dict]) -> float:
    """Age en secondes du dernier evenement d'un run."""
    if not state:
        return 1e9
    stamp = state.get("finished_at") or state.get("updated_at")
    dt = _parse_dt(stamp) if stamp else None
    if dt is None:
        return 1e9
    return (datetime.now() - dt).total_seconds()


def alerts(state: Optional[dict], container_state: str = "",
           states: Optional[list] = None) -> list[dict]:
    """Alertes a afficher : erreur, blocage serveur, etat du conteneur.

    `states` = etats de tous les sites : on signale aussi les sites qui
    sont en erreur alors qu'un autre site tourne.
    """
    out: list[dict] = []
    for st in (states or []):
        if st.get("status") == "error" and (not state or st.get("site") != state.get("site")):
            out.append({
                "level": "error",
                "title": f"Erreur sur {st.get('site')}",
                "detail": st.get("error") or "Le run s'est termine en erreur"})
        elif int(st.get("db_errors") or 0) and _state_age(st) < ALERT_TTL:
            out.append({
                "level": "warn",
                "title": f"{st.get('db_errors')}annonce(s) non enregistree(s) "
                         f"sur {st.get('site')}",
                "detail": "Ecriture en base en echec (MySQL redemarre ou "
                          "concurrent) : ces annonces sont ignorees pour ce "
                          "run et seront reprises au prochain."})
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
    if blocked and _state_age(state) < ALERT_TTL:
        out.append({
            "level": "error",
            "title": f"Blocage serveur detecte ({blocked})",
            "detail": "Le serveur a renvoye un challenge Cloudflare / un code "
                      "403-429-503 pendant le run. Ralentir (delay/jitter) "
                      "puis relancer."})
    if errors and _state_age(state) < ALERT_TTL:
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

"""Pilotage des scrapes : un conteneur ephemere par site.

L'interface ne cree pas les conteneurs elle-meme : elle appelle
`docker compose run`, ce qui garantit que la configuration reste celle du
fichier docker-compose.yml (image, reseau, volumes, variables).

Chaque site a son propre conteneur, demarre a la demande et supprime
automatiquement a la fin du run (--rm) : il n'y a jamais plus de
conteneurs que de scrapes en cours.

    allcars-scrape-avito      scraper du site avito
    allcars-scrape-tomobila   scraper du site tomobila
"""

from __future__ import annotations

import json
import os
import subprocess
from typing import Optional

#: repertoire du projet compose (montre dans le conteneur web)
PROJECT_DIR = os.environ.get("COMPOSE_PROJECT_DIR", "/mnt/4TB/allcars-scraper")
COMPOSE_FILE = os.path.join(PROJECT_DIR, "docker-compose.yml")
#: service compose utilise pour lancer un scrap
SERVICE = os.environ.get("SCRAPER_SERVICE", "app")
#: prefixe des noms de conteneurs
NAME_PREFIX = os.environ.get("SCRAPER_PREFIX", "allcars-scrape")
REINDEX_CONTAINER = os.environ.get("REINDEX_CONTAINER", "allcars-reindex-site")
REINDEX_SERVICE = os.environ.get("REINDEX_SERVICE", "app")
SITE_REINDEX_URL = os.environ.get("SITE_REINDEX_URL", "http://allcars-site-web:8080/api/reindex").replace("/api/reindex","/api/reindex/status").rstrip("/")
DOCKER_BIN = os.environ.get("DOCKER_BIN", "docker")

#: etats considers comme "en cours"
RUNNING_STATES = ("running", "restarting")


class DockerCliError(RuntimeError):
    """Erreur lors de l'appel au CLI docker."""


def container_name(site: str) -> str:
    """Nom du conteneur de scrape d'un site."""
    return f"{NAME_PREFIX}-{site}"


def _run(args: list[str], timeout: int = 120) -> subprocess.CompletedProcess:
    cmd = [DOCKER_BIN] + args
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise DockerCliError(f"timeout : {' '.join(cmd)}")
    except FileNotFoundError:
        raise DockerCliError(
            "CLI docker introuvable dans le conteneur web : impossible de "
            "piloter les scrapes")


def _compose(args: list[str], timeout: int = 180) -> subprocess.CompletedProcess:
    cmd = [DOCKER_BIN, "compose", "-f", COMPOSE_FILE] + args
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                              cwd=PROJECT_DIR)
    except subprocess.TimeoutExpired:
        raise DockerCliError(f"timeout : {' '.join(cmd)}")
    except FileNotFoundError:
        raise DockerCliError("CLI docker introuvable dans le conteneur web")


# ------------------------------------------------------------------ etats
def state(site: str) -> str:
    """Etat du conteneur de scrape d'un site.

    'running' | 'exited' | 'created' | 'absent' (deja supprime : le run
    est fini) | 'unknown'
    """
    name = container_name(site)
    res = _run(["inspect", "-f", "{{.State.Status}}", name], timeout=30)
    if res.returncode != 0:
        return "absent"
    out = (res.stdout or "").strip()
    return out or "unknown"


def states(sites: list[str]) -> dict:
    """Etat des conteneurs pour plusieurs sites."""
    return {s: state(s) for s in sites}


def is_running(site: str) -> bool:
    return state(site) in RUNNING_STATES


# ------------------------------------------------------------------ actions
def start(site: str, job: dict) -> str:
    """Demarre un scrape pour un site (conteneur ephemere).

    job : site, category, mode, details, limit, delay, jitter, slices
    Retourne un message decrivant l'action.
    """
    name = container_name(site)
    # un ancien conteneur du meme nom blockerait la creation
    _run(["rm", "-f", name], timeout=60)

    # _compose() ajoute deja "compose -f <fichier>"
    args = ["run", "-d", "--rm", "--name", name]
    args.append(SERVICE)
    args += _cli_args(job)

    res = _compose(args, timeout=240)
    if res.returncode != 0:
        raise DockerCliError((res.stderr or res.stdout or "echec du demarrage")
                             .strip()[:500])
    return f"Scrap {site} demarre ({name})"


def _cli_args(job: dict) -> list[str]:
    """Traduit le job en arguments de la CLI du scraper."""
    site = job.get("site") or "avito"
    mode = job.get("mode") or "list"
    args = [mode, "--site", site, "--db"]
    category = (job.get("category") or "").strip()
    if category:
        args += ["--category", category]
    try:
        limit = int(job.get("limit") or 0)
    except (TypeError, ValueError):
        limit = 0
    if limit > 0:
        args += ["--limit", str(limit)]
    try:
        delay = float(job.get("delay") or 1.5)
        jitter = float(job.get("jitter") or 0.8)
    except (TypeError, ValueError):
        delay, jitter = 1.5, 0.8
    args += ["--delay", str(delay), "--jitter", str(jitter)]
    if job.get("details") and mode != "details":
        args.append("--details")
    if not job.get("slices", True):
        args.append("--no-slices")
    return args


def stop(site: str, timeout: int = 10) -> str:
    """Arrete le scrape d'un site (les autres sites continuent)."""
    name = container_name(site)
    if state(site) == "absent":
        raise DockerCliError(f"Aucun scrape en cours pour {site}")
    res = _run(["stop", "-t", str(timeout), name], timeout=timeout + 30)
    if res.returncode != 0:
        raise DockerCliError((res.stderr or res.stdout or "echec de l'arret")
                             .strip()[:400])
    return f"Scrap {site} arrete"


def restart(site: str, job: dict) -> str:
    """Relance le scrape d'un site."""
    stop(site)
    return start(site, job)


def logs(site: str, tail: int = 120) -> list[str]:
    """Dernieres lignes de log du scrape d'un site."""
    name = container_name(site)
    res = _run(["logs", "--tail", str(tail), name], timeout=60)
    if res.returncode != 0:
        return []
    lines = (res.stdout or "").splitlines()
    return [ln for ln in lines if ln.strip()]


def list_containers(prefix: str = "allcars") -> list[dict]:
    """Etat de tous les conteneurs du projet (prefixe de nom).

    Retourne une liste : nom, etat, image, statut, age, site (pour les
    conteneurs de scrape).
    """
    res = _run(["ps", "-a", "--format", "{{json .}}"], timeout=30)
    out = []
    names = []
    for line in (res.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except ValueError:
            continue
        name = (item.get("Names") or "").strip()
        if not name.startswith(prefix):
            continue
        names.append(name)
        site = ""
        if name.startswith(NAME_PREFIX + "-"):
            site = name[len(NAME_PREFIX) + 1:]
        out.append({
            "name": name,
            "site": site,
            "state": (item.get("State") or "").lower(),
            "status": item.get("Status") or "",
            "image": (item.get("Image") or "").split(":")[0],
            "running_for": item.get("RunningFor") or "",
            "created_at": item.get("CreatedAt") or "",
        })
    # l'image peut apparaitre sous forme d'identifiant : on resout le nom
    if names:
        insp = _run(["inspect", "--format", "{{.Name}}|{{.Config.Image}}"]
                    + names, timeout=30)
        real = {}
        for line in (insp.stdout or "").splitlines():
            if "|" in line:
                key, _, img = line.partition("|")
                real[key.lstrip("/")] = img
        for item in out:
            img = real.get(item["name"])
            if img:
                item["image"] = img.split(":")[0]
    # --- ordre d'affichage : web, db, puis les scraps ---
    # web et db sont epingles en premier ; les scraps sont tries par
    # activite (en marche d'abord) puis par date de creation decroissante
    pinned = [c for c in out if c["name"] in ("allcars-web", "allcars-db")]
    pinned.sort(key=lambda c: 0 if c["name"] == "allcars-web" else 1)
    scraps = [c for c in out
              if c["name"].startswith("allcars-scrape-")]
    scraps.sort(key=lambda c: (c.get("created_at") or ""), reverse=True)
    scraps.sort(key=lambda c: 0 if c.get("running") else 1, reverse=False)
    others = [c for c in out if c["name"] not in ("allcars-web", "allcars-db")
              and not c["name"].startswith("allcars-scrape-")]
    others.sort(key=lambda c: c["name"])
    return pinned + scraps + others


def cleanup(site: str) -> None:
    """Supprime le conteneur d'un site (s'il existe)."""
    _run(["rm", "-f", container_name(site)], timeout=60)


# ------------------------------------------------------------------ reindex
def reindex_state() -> str:
    """Etat du conteneur de reindex ('running' | 'exited' | 'absent' ...)."""
    name = REINDEX_CONTAINER
    res = _run(["inspect", "-f", "{{.State.Status}}", name], timeout=30)
    if res.returncode != 0:
        return "absent"
    out = (res.stdout or "").strip()
    return out or "unknown"


def launch_reindex() -> str:
    """Lance la reindexation en arriere-plan."""
    name = REINDEX_CONTAINER
    if reindex_state() in RUNNING_STATES:
        return f"Reindexation deja en cours ({name})"
    _run(["rm", "-f", name], timeout=60)
    args = ["run", "-d", "--rm", "--name", name, SERVICE,
            "reindex-site"]
    res = _compose(args, timeout=240)
    if res.returncode != 0:
        raise DockerCliError((res.stderr or res.stdout or "echec du lancement")
                             .strip()[:500])
    return f"Reindexation demarree ({name})"

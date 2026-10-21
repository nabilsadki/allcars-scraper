"""Pilotage des scrapes : un conteneur ephemere par site.

L'interface ne cree pas les conteneurs elle-meme : elle appelle
`docker compose run`, ce qui garantit que la configuration reste celle du
fichier docker-compose.yml (image, reseau, volumes, variables).

Chaque site a son propre conteneur, demarre a la demande et supprime
automatiquement a la fin du run (--rm) : il n'y a jamais plus de
conteneurs que de scrapes en cours.

    avito-scraper-avito      scraper du site avito
    avito-scraper-tomobila   scraper du site tomobila
"""

from __future__ import annotations

import os
import shlex
import subprocess
from typing import Optional

#: repertoire du projet compose (montre dans le conteneur web)
PROJECT_DIR = os.environ.get("COMPOSE_PROJECT_DIR", "/mnt/4TB/scraper-avito")
COMPOSE_FILE = os.path.join(PROJECT_DIR, "docker-compose.yml")
#: service compose utilise pour lancer un scrap
SERVICE = os.environ.get("SCRAPER_SERVICE", "app")
#: prefixe des noms de conteneurs
NAME_PREFIX = os.environ.get("SCRAPER_PREFIX", "avito-scraper")
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


def cleanup(site: str) -> None:
    """Supprime le conteneur d'un site (s'il existe)."""
    _run(["rm", "-f", container_name(site)], timeout=60)

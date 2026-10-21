"""Suivi de progression live, partage avec l'interface web.

Le scraper ecrit un fichier JSON par site dans le volume partage :

    /data/progress/<site>.json

L'interface web le lit pour afficher la barre de progression, le temps
ecoule, l'estimation du temps restant et les alertes (erreurs, blocages
serveur). L'ecriture est atomique (fichier temporaire + rename) pour que
le web ne lise jamais un JSON tronque.
"""

from __future__ import annotations

import json
import os
import signal
import tempfile
import time
from datetime import datetime
from typing import Any, Optional

PROGRESS_DIR = os.environ.get("PROGRESS_DIR", "/data/progress")

#: statuts possibles
RUNNING = "running"
SUCCESS = "success"
ERROR = "error"
STOPPED = "stopped"

#: progresses actifs (pour les handlers de signal)
_ACTIVE: list["Progress"] = []


def install_signal_handlers() -> None:
    """Marque le run comme 'stopped' quand le processus est arrete.

    Le conteneur du scraper est arrete par l'interface web (docker stop) :
    le fichier de progression doit alors indiquer un arret propre, pas une
    erreur.
    """

    def handler(signum, frame):
        for prog in list(_ACTIVE):
            try:
                prog.finish(STOPPED, f"interrompu (signal {signum})")
            except Exception:
                pass
        os._exit(128 + signum)

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, handler)
        except Exception:
            pass


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class Progress:
    """Etat d'avancement d'un run, ecrit sur disque en continu."""

    def __init__(self, site: str, mode: str = "", category: str = "",
                 total: int = 0, container: str = "",
                 path: Optional[str] = None):
        self.site = site
        self.path = path or os.path.join(PROGRESS_DIR, f"{site}.json")
        self.state: dict[str, Any] = {
            "site": site,
            "mode": mode,
            "category": category,
            "status": RUNNING,
            "started_at": _now(),
            "updated_at": _now(),
            "finished_at": None,
            "elapsed_seconds": 0,
            "listed": 0,
            "details": 0,
            "inserted": 0,
            "updated": 0,
            "unchanged": 0,
            "requests": 0,
            "blocked": 0,
            "errors": 0,
            "db_errors": 0,
            "total": total or 0,
            "slices_total": 0,
            "slices_done": 0,
            "current_slice": "",
            "error": None,
            "pid": os.getpid(),
            "container": container or os.environ.get("HOSTNAME", ""),
        }
        self._t0 = time.time()
        _ACTIVE.append(self)
        self.write()

    # ------------------------------------------------------------------ api
    def update(self, **kwargs) -> None:
        """Met a jour des compteurs et reecrit le fichier."""
        for key, value in kwargs.items():
            if value is not None:
                self.state[key] = value
        self.state["elapsed_seconds"] = int(time.time() - self._t0)
        self.state["updated_at"] = _now()
        self.write()

    def stats(self, client=None, db=None) -> None:
        """Copie les compteurs du client HTTP et de la base."""
        if client is not None:
            st = getattr(client, "stats", None) or {}
            self.state["requests"] = int(st.get("requests", 0))
            self.state["blocked"] = int(st.get("blocked", 0))
            self.state["errors"] = int(st.get("errors", 0))
        if db is not None:
            c = getattr(db, "counters", None) or {}
            self.state["inserted"] = int(c.get("inserted", 0))
            self.state["updated"] = int(c.get("updated", 0))
            self.state["unchanged"] = int(c.get("unchanged", 0))

    def finish(self, status: str = SUCCESS, error: Optional[str] = None) -> None:
        """Cloture le run (succes, erreur ou arret)."""
        if self in _ACTIVE:
            _ACTIVE.remove(self)
        self.state["status"] = status
        self.state["error"] = (error or None)
        self.state["finished_at"] = _now()
        self.state["elapsed_seconds"] = int(time.time() - self._t0)
        self.state["updated_at"] = _now()
        self.write()

    def write(self) -> None:
        """Ecriture atomique du fichier de progression."""
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(self.path),
                                       prefix=".progress-", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self.state, fh, ensure_ascii=False, default=str)
            os.replace(tmp, self.path)
        except Exception:
            pass   # le suivi ne doit jamais faire echouer le scrape


def read_progress(site: Optional[str] = None) -> Optional[dict]:
    """Lit un fichier de progression (le plus recent si site est None)."""
    try:
        if site:
            path = os.path.join(PROGRESS_DIR, f"{site}.json")
            files = [path]
        else:
            files = [os.path.join(PROGRESS_DIR, f)
                     for f in os.listdir(PROGRESS_DIR) if f.endswith(".json")]
    except Exception:
        return None
    best = None
    for path in files:
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception:
            continue
        if best is None or data.get("updated_at", "") > best.get("updated_at", ""):
            best = data
    return best

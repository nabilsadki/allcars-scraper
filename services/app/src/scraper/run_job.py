"""Point d'entree du service `scraper` (lance par l'interface web).

Le parametre du job est lu dans un fichier JSON partage avec l'interface :

    /data/scrape_job.json

Exemple :

    {"site": "avito", "category": "maroc/voitures-a-vendre",
     "mode": "list", "details": false, "limit": 0,
     "delay": 1.5, "jitter": 0.8, "slices": true}

Le fichier est ecrit par l'interface web (bouton "Demarrer" / "Relancer"),
ce qui permet de changer de parametres sans reconstruire l'image.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime

from .core.db import Database
from .core.http import HttpClient
from .core.pipeline import run_details, run_list, run_rescan
from .core.progress import install_signal_handlers
from .core.registry import get_site, publish
from . import sites as _sites  # noqa: F401  (enregistre les adaptateurs)

JOB_PATH = os.environ.get("SCRAPE_JOB_FILE", "/data/scrape_job.json")

DEFAULTS = {
    "site": "avito",
    "category": "maroc/voitures-à_vendre",
    "mode": "list",          # list | rescan | details
    "details": False,
    "limit": 0,
    "delay": 1.5,
    "jitter": 0.8,
    "slices": True,
    "mark_missing": False,
}


def load_job() -> dict:
    job = dict(DEFAULTS)
    try:
        with open(JOB_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            job.update({k: v for k, v in data.items() if v is not None})
    except Exception as e:
        print(f"  ! job illisible ({e}) - valeurs par defaut", flush=True)
    return job


def main() -> int:
    install_signal_handlers()
    job = load_job()
    publish()          # l'interface web a besoin des categories/modes par site
    site_name = job.get("site", "avito")
    mode = job.get("mode", "list")
    category = job.get("category", "")
    details = bool(job.get("details"))
    limit = int(job.get("limit") or 0)

    print(f"=== job : {site_name} / {mode} / {category or '(categorie par defaut)'} "
          f"/ details={details} / slices={bool(job.get('slices', True))} ===",
          flush=True)

    site = get_site(site_name)
    site.use_slices = bool(job.get("slices", True))
    if not category:
        category = site.default_category

    client = HttpClient(delay=float(job.get("delay", 1.5)),
                        jitter=float(job.get("jitter", 0.8)),
                        verbose=True)
    db = Database()
    db.ensure_schema()

    started = datetime.now()
    run_id = db.start_run({"started_at": started, "site": site.name,
                           "category": category, "mode": mode})
    error = None
    res: dict = {}
    try:
        if mode == "rescan":
            res = run_rescan(site, client, db, category=category, limit=limit,
                             details=details, mark_missing=bool(job.get("mark_missing")),
                             quiet=False, run_id=run_id)
            listed = res.get("seen", 0)
        elif mode == "details":
            res = run_details(site, client, db, limit=limit or 50,
                              category=category, quiet=False)
            listed = 0
        else:
            res = run_list(site, client, db, category=category, limit=limit,
                           details=details, quiet=False, run_id=run_id)
            listed = res.get("listed", 0)
        error = res.get("error")
    except Exception as e:                      # pragma: no cover
        error = f"{type(e).__name__}: {e}"
        print(f"  ERREUR : {error}", file=sys.stderr)
    finally:
        db.finish_run(run_id, {
            "finished_at": datetime.now(),
            "ads_listed": listed,
            "ads_details": res.get("details", 0),
            "ads_inserted": db.counters.get("inserted", 0),
            "ads_updated": db.counters.get("updated", 0),
            "ads_unchanged": db.counters.get("unchanged", 0),
            "ads_disappeared": db.counters.get("disappeared", 0),
            "http_requests": client.stats["requests"],
            "blocked": client.stats["blocked"],
            "status": "error" if error else "success",
            "error": error,
        })
        db.close()
    return 1 if error else 0


if __name__ == "__main__":
    sys.exit(main())

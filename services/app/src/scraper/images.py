"""Telechargement des images — separe du scraping des annonces.

Le scraping collecte les annonces (et leurs URLs d'images) ; le
telechargement des fichiers est une operation distincte, lancable quand on
veut, avec sa propre cadence et son propre choix de qualite.

Variantes disponibles (mesurees) :
    card     ~20 Ko/image  ->  ~21 Go pour le catalogue complet
    images   ~42 Ko/image  ->  ~45 Go
    full_hd ~205 Ko/image  -> ~216 Go

Stockage : /data/images/{ad_id}/{image_id}.{ext} dans le volume Docker.
Base : la table ad_images est completee (path_local, variant, bytes, sha1).
"""

from __future__ import annotations

import hashlib
import os
import re
import time
from typing import Any, Optional

VARIANTS = {
    "card": {"suffix": "?t=card", "label": "miniature (~20 Ko)"},
    "images": {"suffix": "?t=images", "label": "standard (~42 Ko)"},
    "full_hd": {"suffix": "?t=full_hd", "label": "pleine resolution (~205 Ko)"},
}


def image_url_for(url: str, variant: str) -> str:
    """URL de l'image dans la variante demandee."""
    suffix = VARIANTS.get(variant, VARIANTS["images"])["suffix"]
    base = url.split("?")[0]
    return base + suffix


def image_id(url: str) -> str:
    """Identifiant stable d'une image (son nom de fichier sur content.avito.ma)."""
    name = os.path.basename(url.split("?")[0])
    return re.sub(r"[^0-9A-Za-z_.-]", "_", name) or "img"


class ImageFetcher:
    """Telecharge les images manquantes et les reference en base."""

    def __init__(self, client, db, variant: str = "images", per_ad: int = 0,
                 delay: float = 1.0, jitter: float = 0.5,
                 out_dir: str = "/data/images", force: bool = False,
                 verbose: bool = True):
        if variant not in VARIANTS:
            raise ValueError(f"variante inconnue: {variant} "
                             f"(au choix: {', '.join(VARIANTS)})")
        self.client = client
        self.db = db
        self.variant = variant
        self.per_ad = per_ad
        self.delay = delay
        self.jitter = jitter
        self.out_dir = out_dir
        self.force = force
        self.verbose = verbose
        self.stats = {"ads": 0, "downloaded": 0, "skipped": 0, "errors": 0,
                      "bytes": 0, "changed": 0}

    def log(self, msg: str) -> None:
        if self.verbose:
            print(msg, flush=True)

    # ---------------------------------------------------------------- base
    def pending_ads(self, limit: int = 0, category: str = "") -> list[dict]:
        """Annonces dont toutes (ou certaines) images ne sont pas telechargees."""
        where = ["a.site = 'avito'"]
        params: list[Any] = []
        if category:
            where.append("a.category = %s")
            params.append(category)
        if not self.force:
            # les images deja telechargees dans cette variante sont ignorees
            where.append(
                "EXISTS (SELECT 1 FROM ad_images i WHERE i.ad_id = a.ad_id "
                "AND (i.path_local IS NULL OR i.variant <> %s))"
            )
            params.append(self.variant)
        sql = (f"SELECT a.ad_id, a.title FROM ads a WHERE {' AND '.join(where)} "
               "ORDER BY a.ad_id LIMIT %s")
        params.append(limit if limit else 100000)
        return self.db.query(sql, tuple(params))

    def ad_images(self, ad_id) -> list[dict]:
        return self.db.query(
            "SELECT id, link, link_full, path_local, variant, bytes, sha1 "
            "FROM ad_images WHERE ad_id = %s ORDER BY is_main DESC, id",
            (ad_id,),
        )

    # ------------------------------------------------------------ download
    def fetch_ad(self, ad_id) -> None:
        rows = self.ad_images(ad_id)
        if not rows:
            return
        ad_dir = os.path.join(self.out_dir, str(ad_id))
        n = 0
        for r in rows:
            if self.per_ad and n >= self.per_ad:
                break
            n += 1
            url = image_url_for(r["link"] or r["link_full"] or "", self.variant)
            if not url:
                continue
            ext = os.path.splitext(url.split("?")[0])[1] or ".jpg"
            # la variante est dans le nom : chaque qualite a son propre fichier
            # (changer de variante ne doit pas ecraser l'autre)
            name = image_id(url) + "_" + self.variant + ext
            path = os.path.join(ad_dir, name)

            if not self.force and os.path.exists(path) and r.get("path_local"):
                self.stats["skipped"] += 1
                continue

            try:
                resp = self.client.session.get(
                    url, impersonate="chrome", timeout=self.client.timeout
                )
            except Exception as e:
                self.stats["errors"] += 1
                self.log(f"    ! erreur reseau ({e.__class__.__name__})")
                time.sleep(self.delay)
                continue

            if resp.status_code != 200 or not resp.content:
                self.stats["errors"] += 1
                time.sleep(self.delay)
                continue

            data = resp.content
            sha1 = hashlib.sha1(data).hexdigest()

            # l'image a-t-elle change sur le site ?
            # (on ne compare qu'a la meme variante : changer de qualite
            #  change le sha1 sans que la photo ait change)
            changed = (r.get("variant") == self.variant
                       and bool(r.get("sha1")) and r["sha1"] != sha1)

            os.makedirs(ad_dir, exist_ok=True)
            with open(path, "wb") as f:
                f.write(data)

            self.db.cur.execute(
                "UPDATE ad_images SET path_local = %s, variant = %s, bytes = %s, "
                "sha1 = %s, downloaded_at = NOW() WHERE id = %s",
                (path, self.variant, len(data), sha1, r["id"]),
            )
            self.stats["downloaded"] += 1
            self.stats["bytes"] += len(data)
            if changed:
                self.stats["changed"] += 1
                self._record_image_change(ad_id, r["id"], r.get("sha1"), sha1)
            time.sleep(self.delay + (self.jitter * (0.5 if self.delay else 0)))
        self.stats["ads"] += 1

    def _record_image_change(self, ad_id: int, img_id: int,
                             old_sha1: Optional[str], new_sha1: str) -> None:
        """Archive une version de l'annonce : une photo a ete remplacee."""
        import json
        try:
            row = self.db.query(
                "SELECT version FROM ads WHERE ad_id = %s", (ad_id,))
            version = int(row[0]["version"] or 1) + 1
            self.db.cur.execute(
                "UPDATE ads SET version = %s WHERE ad_id = %s", (version, ad_id))
            self.db.cur.execute(
                "INSERT INTO ad_versions (ad_id, version, change_type, diff_json, "
                "snapshot_json) VALUES (%s,%s,'image_change',%s,%s)",
                (ad_id, version,
                 json.dumps({"image": {"old": old_sha1, "new": new_sha1}}),
                 json.dumps({"image_changed": True})),
            )
            self.log(f"    -> photo remplacee (v{version})")
        except Exception as e:
            self.log(f"    ! version non archivee : {e}")

    # ------------------------------------------------------------------ run
    def run(self, limit: int = 0, category: str = "") -> dict:
        ads = self.pending_ads(limit, category)
        self.log(f"  {len(ads)} annonces a traiter "
                 f"({VARIANTS[self.variant]['label']})")
        t0 = time.time()
        for i, a in enumerate(ads, 1):
            self.fetch_ad(a["ad_id"])
            if self.verbose and i % 20 == 0:
                mb = self.stats["bytes"] / 1048576
                self.log(f"  ... {i}/{len(ads)} annonces | "
                         f"{self.stats['downloaded']} images | {mb:.0f} Mo")
        self.db.commit()
        dt = time.time() - t0
        self.log(f"  termine : {self.stats['downloaded']} images "
                 f"({self.stats['bytes']/1048576:.0f} Mo) en {dt/60:.1f} min"
                 + (f" | {self.stats['errors']} erreurs" if self.stats["errors"] else ""))
        return self.stats

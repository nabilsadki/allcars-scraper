"""Acces MySQL : pool de connexions, upsert versionne, lecture."""

from __future__ import annotations

import json
import time
import os
from datetime import datetime
from typing import Any, Optional

import pymysql
from pymysql.cursors import DictCursor

from .history import classify, compare, content_hash, diff
from .schema import SCHEMA


def _cfg() -> dict:
    return {
        "host": os.environ.get("DB_HOST", "db"),
        "port": int(os.environ.get("DB_PORT", "3306")),
        "user": os.environ.get("DB_USER", "avito"),
        "password": os.environ.get("DB_PASSWORD", "avito"),
        "database": os.environ.get("DB_NAME", "allcars"),
        "charset": "utf8mb4",
        "autocommit": False,
        "cursorclass": DictCursor,
    }


#: codes MySQL "le serveur n'est plus la" -> on reconnecte et on rejoue
CONNECTION_ERRORS = (2003, 2006, 2013, 2055)


class Database:
    """Wrapper MySQL minimaliste (pas d'ORM : le schema est stable)."""

    def __init__(self, **overrides):
        self.cfg = _cfg()
        self.cfg.update(overrides)
        self.conn = self._connect()
        self.cur = self.conn.cursor()
        self._session_setup()
        # compteurs de la session
        self.counters = {
            "inserted": 0, "updated": 0, "unchanged": 0,
            "versions": 0, "disappeared": 0,
        }

    def _connect(self):
        """Connexion au serveur, avec quelques tentatives (demarrage en cours)."""
        last = None
        for attempt in range(6):
            try:
                return pymysql.connect(**self.cfg)
            except pymysql.err.OperationalError as e:
                last = e
                time.sleep(1.0 * (attempt + 1))
        raise last  # pragma: no cover

    def _with_retry(self, fn, *args, **kwargs):
        """Execute une operation, en se reconnectant si le lien est perdu.

        Le serveur MySQL peut etre redemarre (changement de configuration
        compose, maintenance) : on rejoue l'operation au lieu de perdre
        le run.
        """
        last = None
        for attempt in range(6):
            try:
                return fn(*args, **kwargs)
            except pymysql.err.OperationalError as e:
                code = e.args[0] if e.args else 0
                if code not in CONNECTION_ERRORS and attempt >= 5:
                    raise
                last = e
                time.sleep(1.0 * (attempt + 1))
                try:
                    self.reconnect()
                except Exception:
                    pass
        raise last  # pragma: no cover

    def _session_setup(self) -> None:
        """Reglages de session (isolation, delai d'attente des verrous).

        READ COMMITTED : plusieurs scrapes ecrivent en meme temps, InnoDB
        prend beaucoup moins de verrous de trou (gap locks) -> moins de
        "lock wait timeout" / deadlocks.
        Le delai d'attente est court : on rejoue vite plutot que d'etre
        bloque 50 s sur une transaction concurrente.
        """
        try:
            self.cur.execute("SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED")
            self.cur.execute("SET SESSION innodb_lock_wait_timeout = 10")
        except Exception:
            pass

    def safe_exec(self, sql: str, params: tuple = (), many: bool = False) -> None:
        """Execute une requete, en se reconnectant si le lien est perdu.

        Le serveur MySQL peut etre redemarre (changement de configuration
        compose, mise a jour) : les scrapes en cours ne doivent pas mourir.
        """
        for attempt in range(4):
            try:
                if many:
                    self.cur.executemany(sql, params)
                else:
                    self.cur.execute(sql, params)
                return
            except pymysql.err.InterfaceError:
                if attempt >= 3:
                    raise
                self.reconnect()
            except pymysql.err.OperationalError as e:
                code = e.args[0] if e.args else 0
                if code not in CONNECTION_ERRORS or attempt >= 3:
                    raise
                # le serveur MySQL a ete redemarre / est momentanement
                # injoignable : on patiente et on se reconnecte
                time.sleep(1.5 * (attempt + 1))
                self.reconnect()

    def reconnect(self) -> None:
        """Retablit la connexion (MySQL redemarre, reseau coupe...)."""
        try:
            self.conn.close()
        except Exception:
            pass
        self.conn = self._connect()
        self.cur = self.conn.cursor()
        self._session_setup()

    # ------------------------------------------------------------------ base
    def ensure_schema(self) -> None:
        """Applique le schema (CREATE TABLE IF NOT EXISTS) + migrations."""
        self._with_retry(self._ensure_schema)

    def _ensure_schema(self) -> None:
        cur = self.conn.cursor()
        db_name = self.cfg.get("database") or "allcars"
        # la base est creee et selectionnee ici (le nom vient de la
        # configuration, plus du schema)
        try:
            cur.execute(f"CREATE DATABASE IF NOT EXISTS `{db_name}` "
                        "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
            cur.execute(f"USE `{db_name}`")
        except Exception:
            pass
        for stmt in _split_sql(SCHEMA):
            cur.execute(stmt)
        self.conn.commit()
        cur.close()
        self.migrate()

    def migrate(self) -> list[str]:
        """Ajoute les colonnes manquantes sur une base deja creee."""
        wanted = {
            "ads": [
                ("version", "INT NOT NULL DEFAULT 1"),
                ("content_hash", "CHAR(64) NULL"),
                ("seen_count", "INT DEFAULT 0"),
                ("is_active", "TINYINT(1) DEFAULT 1"),
                ("first_seen_at", "DATETIME NULL"),
                ("last_seen_at", "DATETIME NULL"),
                ("note_etat", "DECIMAL(4,2) NULL"),
                ("note_achat", "DECIMAL(4,2) NULL"),
                ("note_global", "DECIMAL(4,2) NULL"),
                ("note_confiance", "DECIMAL(4,2) NULL"),
                ("fiscal_power", "VARCHAR(64) NULL"),
            ("body_type", "VARCHAR(64) NULL"),
                ("sector", "VARCHAR(128) NULL"),
                ("list_id", "BIGINT NULL"),
                ("area", "VARCHAR(128) NULL"),
                ("seller_type", "VARCHAR(64) NULL"),
                ("seller_id", "VARCHAR(64) NULL"),
                ("doors", "INT NULL"),
                ("origin", "VARCHAR(64) NULL"),
                ("first_hand", "VARCHAR(16) NULL"),
                ("cond", "VARCHAR(64) NULL"),
            ],
            "ad_images": [
                ("path_local", "VARCHAR(512) NULL"),
                ("variant", "VARCHAR(16) NULL"),
                ("bytes", "INT NULL"),
                ("sha1", "CHAR(40) NULL"),
                ("downloaded_at", "DATETIME NULL"),
                ("link_full", "VARCHAR(1024) NULL"),
            ],
        }
        added = []
        for table, cols in wanted.items():
            existing = {r["COLUMN_NAME"] for r in self.query(
                "SELECT COLUMN_NAME FROM information_schema.columns "
                "WHERE table_schema = DATABASE() AND table_name = %s", (table,))}
            for name, ddl in cols:
                if name in existing:
                    continue
                try:
                    self.cur.execute(
                        f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
                    added.append(f"{table}.{name}")
                except Exception:
                    pass
        # --- cle primaire composite (site, ad_id) + ad_id alphanumerique ---
        # ad_id peut etre un slug ou un identifiant interne du site ("dITj3MBMiY")
        try:
            self.cur.execute(
                "SELECT DATA_TYPE FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'ads' "
                "AND COLUMN_NAME = 'ad_id'")
            row = self.cur.fetchone()
            is_varchar = bool(row) and str(row.get("DATA_TYPE", "")).lower() == "varchar"

            self.cur.execute("SHOW KEYS FROM ads WHERE Key_name = 'PRIMARY'")
            pk = [r["Column_name"] for r in self.cur.fetchall()]
            if pk == ["ad_id"]:
                self.cur.execute("ALTER TABLE ads DROP PRIMARY KEY")
                pk = []
                added.append("ads.PK(site,ad_id)")
            if not is_varchar:
                # passage en VARCHAR : il faut retirer la PK avant de modifier
                if pk:
                    self.cur.execute("ALTER TABLE ads DROP PRIMARY KEY")
                    pk = []
                for table in ("ads", "ad_images", "ad_versions"):
                    self.cur.execute(
                        f"ALTER TABLE {table} MODIFY ad_id VARCHAR(64) NOT NULL")
                    added.append(f"{table}.ad_id VARCHAR")
                is_varchar = True
            if not pk:
                self.cur.execute("ALTER TABLE ads ADD PRIMARY KEY (site, ad_id)")
                added.append("ads.PK(site,ad_id)")
        except Exception:
            try:
                self.conn.rollback()
            except Exception:
                pass
        for table, col, ddl in (
            ("ad_images", "site", "VARCHAR(32) NOT NULL DEFAULT 'avito'"),
            ("ad_versions", "site", "VARCHAR(32) NOT NULL DEFAULT 'avito'"),
            ("scrape_runs", "site", "VARCHAR(32) NULL"),
        ):
            try:
                self.cur.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
                added.append(f"{table}.{col}")
            except Exception:
                pass
        try:
            self.cur.execute("UPDATE scrape_runs SET site = 'avito' "
                             "WHERE site IS NULL")
        except Exception:
            pass
        if added:
            self.conn.commit()
        return added

    def ping(self) -> bool:
        self.cur.execute("SELECT 1")
        return self.cur.fetchone() is not None

    def close(self) -> None:
        try:
            self.conn.commit()
        except Exception:
            pass
        self.conn.close()

    def commit(self) -> None:
        self._with_retry(self._commit)

    def _commit(self) -> None:
        self.conn.commit()

    # ------------------------------------------------------------------ ecriture
    def _row_from_ad(self, ad: dict, site: str = "avito") -> dict:
        return {
            "ad_id": ad.get("ad_id"),
            "list_id": ad.get("list_id"),
            "site": site,
            "category": ad.get("category"),
            "title": ad.get("title"),
            "price": ad.get("price"),
            "publish_date": _to_dt(ad.get("list_time")),
            "city": ad.get("city"),
            "area": ad.get("area"),
            "sector": ad.get("sector"),
            "seller": ad.get("seller"),
            "seller_type": ad.get("seller_type"),
            "seller_id": ad.get("seller_id"),
            "phone": ad.get("phone"),
            "link": ad.get("url"),
            "description": ad.get("description"),
            "fuel": ad.get("fuel"),
            "gear": ad.get("gearbox"),
            "km": _as_km(ad.get("mileage_km")
                          if ad.get("mileage_km") is not None
                          else ad.get("km")),
            "year": _as_int(ad.get("year")),
            "brand": ad.get("brand"),
            "model": ad.get("model"),
            "doors": ad.get("doors"),
            "body_type": ad.get("body_type"),
            "origin": ad.get("origin"),
            "first_hand": ad.get("first_hand"),
            "cond": ad.get("cond"),
            "fiscal_power": ad.get("fiscal_power"),
            "options_json": json.dumps(ad.get("options") or {}, ensure_ascii=False),
            # les champs prives ("_raw_html"...) ne sont jamais stockes :
            # ils gonfleraient la base de plusieurs Go
            "raw_json": json.dumps(
                {k: v for k, v in ad.items() if not k.startswith("_")},
                ensure_ascii=False, default=str),
            "nb_images": ad.get("nb_images", 0),
            "is_premium": int(bool(ad.get("is_premium"))),
            "is_urgent": int(bool(ad.get("is_urgent"))),
            "is_hot_deal": int(bool(ad.get("is_hot_deal"))),
            "details_fetched": int(bool(ad.get("details_fetched"))),
            "note": ad.get("note"),
            "note_year": ad.get("note_year"),
            "note_km": ad.get("note_km"),
            "note_ratio": ad.get("note_ratio"),
            "note_cv": ad.get("note_cv"),
            "note_sunroof": ad.get("note_sunroof"),
            "note_fuel": ad.get("note_fuel"),
            "note_gear": ad.get("note_gear"),
            "note_specs": ad.get("note_specs"),
            "km_year": ad.get("km_year"),
            "note_etat": ad.get("note_etat"),
            "note_achat": ad.get("note_achat"),
            "note_global": ad.get("note_global"),
            "note_confiance": ad.get("note_confiance"),
        }

    def check(self, ad: dict, site: str = "avito") -> tuple[str, dict]:
        """Compare sans ecrire : ('new' | 'changed' | 'unchanged', diff)."""
        ad_id = ad.get("ad_id")
        self.cur.execute("SELECT * FROM ads WHERE site = %s AND ad_id = %s",
                         (site, ad_id))
        existing = self.cur.fetchone()
        if not existing:
            return "new", {}
        old_state = self._previous_state(existing)
        changes = compare(old_state, ad, is_detail=bool(ad.get("details_fetched")))
        return ("changed" if changes else "unchanged"), changes

    def upsert_ad(self, ad: dict, site: str = "avito") -> str:
        """Insere ou met a jour une annonce en gerant les versions.

        Retourne : 'new' | 'updated' | 'unchanged'

        MySQL peut renvoyer un deadlock (1213) ou un lock timeout (1205)
        quand deux scrapps travaillent en meme temps : on rejoue l'ecriture
        au lieu de perdre l'annonce (et le run).
        """
        for attempt in range(8):
            try:
                return self._upsert_ad(ad, site)
            except pymysql.err.InterfaceError:
                # connexion perdue (MySQL redemarre, reseau coupe) : on se
                # reconnecte et on rejoue l'ecriture
                if attempt >= 7:
                    raise
                self.reconnect()
                continue
            except pymysql.err.OperationalError as e:
                code = e.args[0] if e.args else 0
                if code in CONNECTION_ERRORS and attempt < 7:
                    # MySQL a ete redemarre : on se reconnecte et on rejoue
                    time.sleep(1.5 * (attempt + 1))
                    self.reconnect()
                    continue
                if code in (1213, 1205) and attempt < 7:
                    # deux scrapps en parallele se disputent les memes lignes :
                    # on rejoue avec un petit delai
                    time.sleep(0.3 * (attempt + 1))
                    try:
                        self.conn.rollback()
                    except Exception:
                        pass
                    continue
                raise

    def _upsert_ad(self, ad: dict, site: str = "avito") -> str:
        ad_id = ad.get("ad_id")
        h = content_hash(ad)
        self.cur.execute(
            "SELECT * FROM ads WHERE site = %s AND ad_id = %s", (site, ad_id)
        )
        existing = self.cur.fetchone()

        if not existing:
            row = self._row_from_ad(ad, site)
            row["version"] = 1
            row["content_hash"] = h
            cols = list(row.keys())
            ph = ", ".join(["%s"] * len(cols))
            names = ", ".join(cols)
            upd = ", ".join(f"{c}=VALUES({c})" for c in cols if c not in ("ad_id",))
            self.cur.execute(
                f"INSERT INTO ads ({names}) VALUES ({ph}) ON DUPLICATE KEY UPDATE {upd}",
                [row[c] for c in cols],
            )
            self._add_version(ad, existing=None, change_type="new", version=1,
                              site=site)
            self.counters["inserted"] += 1
            self.counters["versions"] += 1
            return "new"

        # comparaison a la profondeur de l'observation : on ignore les champs
        # non observes (une passe "liste" ne peut pas conclure sur le km, etc.)
        old_state = self._previous_state(existing)
        is_detail = bool(ad.get("details_fetched"))
        changes = compare(old_state, ad, is_detail=is_detail)

        if not changes:
            # rien n'a change : on ne touche qu'aux compteurs de visionnage
            # (et aux notes, qui dependent de l'annee en cours : elles
            #  evoluent sans que l'annonce soit modifiee)
            # les colonnes "non observees" mais renseignees sont rafraichies
            extra = {}
            if ad.get("publish_date") and not existing.get("publish_date"):
                extra["publish_date"] = ad["publish_date"]
            sets = "".join(f"{c} = %s, " for c in extra)
            self.cur.execute(
                f"UPDATE ads SET {sets}last_seen_at = NOW(), "
                "seen_count = seen_count + 1, is_active = 1 "
                "WHERE site = %s AND ad_id = %s",
                list(extra.values()) + [site, ad_id],
            )
            self._update_scores(ad_id, ad, site)
            self.counters["unchanged"] += 1
            return "unchanged"

        version = int(existing.get("version") or 1) + 1
        row = self._row_from_ad(ad, site)
        # ne JAMAIS ecraser un champ connu par un null (fetch incomplet ;
        # les nouveaux renseignements sont ajoutes, les anciens preserves)
        for c in list(row.keys()):
            if row.get(c) is None and existing.get(c) is not None:
                row[c] = existing.get(c)
        row["version"] = version
        row["content_hash"] = h
        cols = [c for c in row if c != "ad_id"]
        sets = ", ".join(f"{c} = %s" for c in cols)
        self.cur.execute(
            f"UPDATE ads SET {sets}, last_seen_at = NOW(), "
            "seen_count = seen_count + 1, is_active = 1 "
            "WHERE site = %s AND ad_id = %s",
            [row[c] for c in cols] + [site, ad_id],
        )
        self._add_version(ad, existing=existing, change_type=None, version=version,
                          changes=changes, site=site)
        self.counters["updated"] += 1
        self.counters["versions"] += 1
        return "updated"

    def _update_scores(self, ad_id: int, ad: dict, site: str = "avito") -> None:
        """Met a jour les colonnes de scoring (sans incrementer la version)."""
        cols = ["note", "note_year", "note_km", "note_ratio", "note_cv",
                "note_sunroof", "note_fuel", "note_gear", "note_specs",
                "km_year", "note_etat", "note_achat", "note_global",
                "note_confiance"]
        sets, vals = [], []
        for c in cols:
            if c in ad and ad.get(c) is not None:
                sets.append(f"{c} = %s")
                vals.append(ad[c])
        # le detail des notes (v2 + drapeaux) vit dans raw_json : on le rafraichit
        sets.append("raw_json = %s")
        vals.append(json.dumps(ad, ensure_ascii=False, default=str))
        if not sets:
            return
        try:
            self.cur.execute(
                f"UPDATE ads SET {', '.join(sets)} WHERE site = %s AND ad_id = %s",
                vals + [site, ad_id],
            )
        except Exception:
            pass

    def _add_version(self, ad: dict, existing: Optional[dict],
                     change_type: Optional[str], version: int,
                     changes: Optional[dict] = None,
                     site: str = "avito") -> None:
        """Archive la version courante + le diff avec la version precedente."""
        if existing is None:
            d = {}
            ctype = "new"
        else:
            d = changes if changes is not None else {}
            ctype = change_type or classify(d)
        snapshot = {
            k: ad.get(k) for k in (
                "title", "description", "price", "year", "mileage_km", "fuel",
                "gearbox", "brand", "model", "doors", "origin", "first_hand",
                "cond", "fiscal_power", "city", "area", "seller", "phone",
                "url", "nb_images", "note",
            )
        }
        self.cur.execute(
            "INSERT INTO ad_versions (site, ad_id, version, change_type, diff_json, "
            "snapshot_json, content_hash, details_fetched) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                site,
                ad.get("ad_id"),
                version,
                ctype,
                json.dumps(d, ensure_ascii=False, default=str),
                json.dumps(snapshot, ensure_ascii=False, default=str),
                content_hash(ad),
                int(bool(ad.get("details_fetched"))),
            ),
        )

    @staticmethod
    def _previous_state(existing: dict) -> dict:
        """Etat precedent de l'annonce (depuis raw_json), ou la ligne SQL."""
        raw = existing.get("raw_json")
        if raw:
            try:
                return json.loads(raw)
            except Exception:
                pass
        return Database._state_of(existing)

    @staticmethod
    def _state_of(src: dict) -> dict:
        """Etat comparable (colonnes SQL -> cles du dict d'annonce)."""
        if "options_json" in src:  # ligne SQL
            return {
                "title": src.get("title"),
                "description": src.get("description"),
                "price": src.get("price"),
                "year": src.get("year"),
                "mileage_km": src.get("km"),
                "fuel": src.get("fuel"),
                "gearbox": src.get("gear"),
                "brand": src.get("brand"),
                "model": src.get("model"),
                "doors": src.get("doors"),
                "origin": src.get("origin"),
                "first_hand": src.get("first_hand"),
                "cond": src.get("cond"),
                "fiscal_power": src.get("fiscal_power"),
                "city": src.get("city"),
                "seller": src.get("seller"),
                "phone": src.get("phone"),
                "nb_images": src.get("nb_images"),
                "is_premium": src.get("is_premium"),
                "is_urgent": src.get("is_urgent"),
                "is_hot_deal": src.get("is_hot_deal"),
                "link": src.get("link"),
            }
        return src  # deja un dict d'annonce

    def mark_disappeared(self, ad_ids: list[int], site: str = "avito") -> int:
        """Marque comme inactives les annonces non vues lors d'un run complet."""
        if not ad_ids:
            return 0
        n = 0
        for ad_id in ad_ids:
            self.cur.execute(
                "SELECT ad_id, version, is_active FROM ads WHERE site = %s "
                "AND ad_id = %s AND is_active = 1",
                (site, ad_id),
            )
            row = self.cur.fetchone()
            if not row:
                continue
            version = int(row.get("version") or 1) + 1
            self.cur.execute(
                "UPDATE ads SET is_active = 0, version = %s WHERE site = %s AND ad_id = %s",
                (version, site, ad_id),
            )
            self.cur.execute(
                "INSERT INTO ad_versions (site, ad_id, version, change_type, diff_json, "
                "snapshot_json) VALUES (%s,%s,%s,'disappeared',%s,%s)",
                (site, ad_id, version,
                 json.dumps({"is_active": {"old": 1, "new": 0}}),
                 json.dumps({"is_active": 0}, ensure_ascii=False)),
            )
            n += 1
        self.counters["disappeared"] += n
        return n

    def upsert_images(self, ad_id, images: list[dict],
                      site: str = "avito") -> None:
        """Enregistre les photos.

        Un re-scrape doit remplacer, pas ajouter : on SUPPRIME d'abord les
        photos deja stockees pour ce (site, ad_id), sinon l'annonce
        s'affiche avec un melange des anciennes et nouvelles images.
        """
        # effacer le lot precedent (peu importe le nombre de nouvelles images)
        try:
            self.safe_exec("DELETE FROM ad_images WHERE site = %s AND ad_id = %s",
                          (site, ad_id))
        except Exception:
            # meme en cas d'echec du delete, on tente l'insertion
            pass
        rows = [(site, ad_id, img.get("url"), img.get("url_full_hd"),
                 1 if i == 0 else 0)
                for i, img in enumerate(images or [])]
        if not rows:
            return
        self.safe_exec(
            "INSERT INTO ad_images (site, ad_id, link, link_full, is_main) "
            "VALUES (%s,%s,%s,%s,%s) "
            "ON DUPLICATE KEY UPDATE link_full=VALUES(link_full), is_main=VALUES(is_main)",
            rows, many=True)

    def start_run(self, run: dict) -> int:
        """Ouvre une ligne d'historique (status=running) et renvoie son id."""
        return self._with_retry(self._start_run, run)

    def _start_run(self, run: dict) -> int:
        self.cur.execute(
            "INSERT INTO scrape_runs (started_at, site, category, mode, status) "
            "VALUES (%s,%s,%s,%s,'running')",
            (
                _to_dt(run.get("started_at")) or datetime.now(),
                run.get("site") or "avito",
                run.get("category"),
                run.get("mode"),
            ),
        )
        self.conn.commit()
        return int(self.cur.lastrowid)

    def update_run(self, run_id: int, run: dict) -> None:
        """Met a jour les compteurs d'un run en cours (historique live)."""
        if not run_id:
            return
        self._with_retry(self._update_run, run_id, run)

    def _update_run(self, run_id: int, run: dict) -> None:
        self.cur.execute(
            "UPDATE scrape_runs SET ads_listed = %s, ads_details = %s, "
            "ads_inserted = %s, ads_updated = %s, ads_unchanged = %s, "
            "http_requests = %s, blocked = %s WHERE id = %s",
            (
                run.get("ads_listed", 0),
                run.get("ads_details", 0),
                run.get("ads_inserted", 0),
                run.get("ads_updated", 0),
                run.get("ads_unchanged", 0),
                run.get("http_requests", 0),
                run.get("blocked", 0),
                run_id,
            ),
        )
        self.conn.commit()

    def finish_run(self, run_id: int, run: dict) -> None:
        """Met a jour la ligne d'historique a la fin du run."""
        self._with_retry(self._finish_run, run_id, run)

    def _finish_run(self, run_id: int, run: dict) -> None:
        self.cur.execute(
            "UPDATE scrape_runs SET finished_at = %s, ads_listed = %s, "
            "ads_details = %s, ads_inserted = %s, ads_updated = %s, "
            "ads_unchanged = %s, ads_disappeared = %s, http_requests = %s, "
            "blocked = %s, status = %s, error = %s WHERE id = %s",
            (
                _to_dt(run.get("finished_at")) or datetime.now(),
                run.get("ads_listed", 0),
                run.get("ads_details", 0),
                run.get("ads_inserted", 0),
                run.get("ads_updated", 0),
                run.get("ads_unchanged", 0),
                run.get("ads_disappeared", 0),
                run.get("http_requests", 0),
                run.get("blocked", 0),
                run.get("status") or "success",
                (run.get("error") or "")[:2000] or None,
                run_id,
            ),
        )
        self.conn.commit()

    def touch_run(self, run: dict) -> None:
        self._with_retry(self._touch_run, run)

    def _touch_run(self, run: dict) -> None:
        self.cur.execute(
            "INSERT INTO scrape_runs (started_at, finished_at, site, category, mode, "
            "ads_listed, ads_details, ads_inserted, ads_updated, ads_unchanged, "
            "ads_disappeared, http_requests, blocked, status, error) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                _to_dt(run.get("started_at")) or datetime.now(),
                _to_dt(run.get("finished_at")),
                run.get("site") or "avito",
                run.get("category"),
                run.get("mode"),
                run.get("ads_listed", 0),
                run.get("ads_details", 0),
                run.get("ads_inserted", 0),
                run.get("ads_updated", 0),
                run.get("ads_unchanged", 0),
                run.get("ads_disappeared", 0),
                run.get("http_requests", 0),
                run.get("blocked", 0),
                run.get("status"),
                (run.get("error") or "")[:2000] or None,
            ),
        )
        self.conn.commit()

    # ------------------------------------------------------------------ lecture
    def query(self, sql: str, params: tuple = ()) -> list[dict]:
        return self._with_retry(self._query, sql, params)

    def _query(self, sql: str, params: tuple = ()) -> list[dict]:
        self.cur.execute(sql, params)
        return list(self.cur.fetchall())

    def count_ads(self) -> int:
        return self.query("SELECT COUNT(*) AS n FROM ads")[0]["n"]

    def known_ad_ids(self, category: str = None, site: str = "avito") -> list[int]:
        """Ad_ids deja connus (pour detecter les disparitions)."""
        if category:
            rows = self.query(
                "SELECT ad_id FROM ads WHERE site = %s AND is_active = 1 "
                "AND (category = %s OR %s IS NULL)",
                (site, category, category),
            )
        else:
            rows = self.query(
                "SELECT ad_id FROM ads WHERE site = %s AND is_active = 1", (site,))
        return [r["ad_id"] for r in rows]


def _split_sql(sql: str) -> list[str]:
    """Decoupe un script SQL en instructions.

    Attention : un ';' dans un commentaire ('-- ...') couperait le script en
    plein. On retire donc les commentaires avant de decouper.
    """
    lines = []
    for line in sql.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("--"):
            continue
        lines.append(line)
    body = "\n".join(lines)
    return [s.strip() for s in body.split(";") if s.strip()]


def _to_dt(v: Any) -> Optional[datetime]:
    if not v:
        return None
    s = str(v).replace("T", " ")
    if s.endswith("Z"):
        s = s[:-1]
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s[:26], fmt)
        except ValueError:
            continue
    return None


def _as_int(v: Any) -> Optional[int]:
    if v is None:
        return None
    try:
        return int(str(v).replace(" ", "").replace("km", "").strip())
    except (TypeError, ValueError):
        return None

def _as_km(v: Any) -> Optional[int]:
    """Kilometrage coerce et borde (les valeurs fofloues du site sont jetees)."""
    n = _as_int(v)
    if n is None:
        return None
    if n < 0 or n > 5_000_000:
        return None
    return n


"""Automate scrap : scheduler + queue partages (MySQL).

La thread daemon tourne dans le process web (~60 s de cadence) :
  1. pour chaque site enabled dont `next_due <= now`,
     - enfile un job `scrape_queue` (priority 3, trigger scheduled)
     - calcule `next_due = now + cadence_hours`
  2. traite la file (priority ASC) : lance le conteneur du site siloquement
     (en une seule passes), le suit, mipoit le statut, ecrit la trace.
"""
import os
import json
import time
import datetime
import threading
import subprocess
import traceback

_DAEMON = {"thread": None, "lock": threading.Lock(), "enabled": False}


def _now():
    return datetime.datetime.now()


def ensure_tables():
    import app
    import app as _app
    db = _app.db()
    cur = db.cursor()
    try:
        cur.execute("""CREATE TABLE IF NOT EXISTS site_schedule (
          site VARCHAR(32) PRIMARY KEY,
          enabled TINYINT(1) NOT NULL DEFAULT 0,
          cadence_hours INT NOT NULL DEFAULT 24,
          last_success_at DATETIME NULL,
          last_mode VARCHAR(32) NULL,
          next_due DATETIME NULL,
          created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
          updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
        )""")
        cur.execute("""CREATE TABLE IF NOT EXISTS scrape_queue (
          id BIGINT PRIMARY KEY AUTO_INCREMENT,
          site VARCHAR(32) NOT NULL,
          mode VARCHAR(32) NOT NULL DEFAULT 'rescan',
          payload_json TEXT NULL,
          priority TINYINT NOT NULL DEFAULT 3,
          status ENUM('pending','running','done','failed','cancelled','retries_failed') NOT NULL DEFAULT 'pending',
          enqueued_at DATETIME NOT NULL,
          started_at DATETIME NULL,
          finished_at DATETIME NULL,
          error TEXT NULL,
          container_name VARCHAR(128) NULL,
          trigger_source ENUM('manual','scheduled','daemon') NOT NULL DEFAULT 'manual',
          INDEX idx_priority_status_enqueued (priority,status,enqueued_at)
        )""")
        for s in app.known_sites():
            name = s.get("name")
            if not name:
                continue
            cur.execute("INSERT IGNORE INTO site_schedule (site) VALUES (%s)", (name,))
            # default pour un nouveau site : cadence 0 => disabled, next_due now
        for _ in range(5):
            try:
                cur.execute("UPDATE site_schedule SET next_due = NOW() "
                            "WHERE next_due IS NULL")
                db.commit()
                break
            except Exception:
                try:
                    db.rollback()
                except Exception:
                    pass
                time.sleep(0.5)
    finally:
        cur.close()


def _q(db=None, sql="", params=()):
    import app as _app
    db = _app.db()
    cur = db.cursor()
    try:
        cur.execute(sql, params)
        out = cur.fetchall() if cur.description else []
    finally:
        try: cur.close()
        except Exception: pass
        try: db.close()
        except Exception: pass
    return out


def _exec(db=None, sql="", params=()):
    import app as _app
    db = _app.db()
    cur = db.cursor()
    try:
        cur.execute(sql, params)
        db.commit()
    finally:
        try: cur.close()
        except Exception: pass
        try: db.close()
        except Exception: pass


def get_schedules():
    import app
    import app as _app
    db = _app.db()
    return _q(db, "SELECT site,enabled,cadence_hours,last_success_at,last_mode,next_due "
                  "FROM site_schedule ORDER BY site")


def set_enabled(site, enabled):
    import app
    import app as _app
    db = _app.db()
    _exec(db, "UPDATE site_schedule SET enabled=%s, next_due=NOW() WHERE site=%s",
          (1 if enabled else 0, site))


def set_cadence(site, hours):
    import app
    import app as _app
    db = _app.db()
    if hours < 1: hours = 1
    if hours > 720: hours = 720
    _exec(db, "UPDATE site_schedule SET cadence_hours=%s WHERE site=%s", (hours, site))


def enqueue(site, mode="rescan", payload=None, priority=2, trigger_source="manual"):
    import app
    import app as _app
    db = _app.db()
    payload_json = json.dumps(payload or {})
    cur = db.cursor()
    cur.execute("""INSERT INTO scrape_queue
        (site, mode, payload_json, priority, status, enqueued_at, trigger_source)
        VALUES (%s, %s, %s, %s, 'pending', NOW(), %s)""",
        (site, mode, payload_json, int(priority), trigger_source))
    db.commit()
    new_id = cur.lastrowid
    cur.close()
    return new_id


def get_queue(limit=40):
    import app
    import app as _app
    db = _app.db()
    return _q(db, "SELECT id,site,mode,priority,status,enqueued_at,started_at,"
                  "finished_at,error,container_name FROM scrape_queue "
                  "ORDER BY priority ASC, enqueued_at ASC LIMIT %s", (limit,))


def daemon_start():
    with _DAEMON["lock"]:
        thr = _DAEMON["thread"]
        if _DAEMON["enabled"] and thr is not None and thr.is_alive():
            return  # deja actif
        # (re)demarre un thread meme si un precedent a crashe
        from threading import Thread
        _DAEMON["enabled"] = True
        _DAEMON["thread"] = Thread(target=_loop, daemon=True,
                                   name="scrape-daemon")
        _DAEMON["thread"].start()
        _DAEMON["thread"] = threading.Thread(target=_loop, daemon=True, name="scrape-daemon")
        _DAEMON["thread"].start()


def daemon_stop():
    with _DAEMON["lock"]:
        _DAEMON["enabled"] = False
    # thread joins on next poll (<=60s)


def daemon_status():
    with _DAEMON["lock"]:
        return {"enabled": _DAEMON["enabled"],
                "alive": bool(_DAEMON["thread"]) and _DAEMON["thread"].is_alive()}


def _loop():
    import app
    ensure_tables()
    while _DAEMON["enabled"]:
        try:
            import app as _app
            db = _app.db()
            # phase 1: enqueue due sites
            now = _now()
            print("LOOP", now.isoformat(), flush=True)
            due = _q(db, "SELECT site, cadence_hours, enabled FROM site_schedule "
                          "WHERE enabled=1 AND (next_due IS NULL OR next_due <= NOW())", ())
            print("  due raw=", due, flush=True)
            for _r in due:
                site = _r["site"]; cadence = _r["cadence_hours"]; enabled = _r["enabled"]
                enqueue(site, mode="rescan", payload={"mode": "rescan"}, priority=3,
                        trigger_source="scheduled")
                print("  enqueued", site, flush=True)
                _exec(db, "UPDATE site_schedule SET next_due = DATE_ADD(NOW(), "
                          "INTERVAL %s HOUR) WHERE site=%s",
                      (int(cadence or 24), site))
            # phase 2: run queue head
            rows = _q(db, "SELECT id,site,mode,payload_json FROM scrape_queue "
                           "WHERE status='pending' ORDER BY priority ASC, "
                           "enqueued_at ASC LIMIT 1")
            if rows:
                print("PROCESSING queue row", rows[0].get('id'), flush=True)
                _row = rows[0]
                rid = _row["id"]; site = _row["site"]; mode = _row["mode"]
                payload_json = _row.get("payload_json")
                if not payload_json or not str(payload_json).strip():
                    payload = {}
                else:
                    payload = json.loads(payload_json)
                    payload.setdefault("mode", mode)
                job = app.progress_reader.read_job(site)
                job["site"] = site
                for k, v in (payload or {}).items():
                    job[k] = v if not isinstance(v, (list, dict)) else v
                app.progress_reader.write_job(job)
                try:
                    import scrapectl
                    name = scrapectl.start(site, job)
                    _exec(db, "UPDATE scrape_queue SET status='running', started_at=NOW(), "
                              "container_name=%s WHERE id=%s", (scrapectl.container_name(site), rid))
                    # wait for completion in background thread per queue item
                    threading.Thread(target=_watch, args=(site, rid), daemon=True).start()
                except Exception as e:
                    _exec(db, "UPDATE scrape_queue SET status='failed', error=%s, "
                              "finished_at=NOW() WHERE id=%s", (str(e)[:400], rid))
            time.sleep(15)
        except Exception:
            traceback.print_exc()
            time.sleep(60)


def _watch(site, queue_id):
    import app, scrapectl
    import time as t
    import app as _app
    db = _app.db()
    while _DAEMON["enabled"]:
        try:
            st = scrapectl.state(site)
        except Exception:
            st = "absent"
        if st not in ("running", "created",):
            # done / terminated
            progress = None
            try:
                db2 = app.db()
                cur = db2.cursor()
                cur.execute("SELECT status, mode FROM scrape_runs WHERE site=%s "
                            "ORDER BY id DESC LIMIT 1", (site,))
                row = cur.fetchone()
                cur.close(); db2.close()
                status = (row or {}).get("status") or "done"
                final_mode = (row or {}).get("mode") if row else "auto"
            except Exception:
                status = "done"
                final_mode = "auto"
            new_status = "done" if status in ("success", None) else status
            _exec(db, "UPDATE scrape_queue SET status=%s, finished_at=NOW() "
                      "WHERE id=%s", (new_status, queue_id))
            # update site_schedule.last_success if successful
            if status in ("success", "retries_failed"):
                _exec(db, "UPDATE site_schedule SET last_success_at=NOW(), "
                          "last_mode=%s WHERE site=%s",
                      (final_mode, site))
            return
        t.sleep(10)

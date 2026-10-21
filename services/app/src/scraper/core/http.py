"""Client HTTP generique (curl_cffi) : rate limiting, retry, anti-Cloudflare.

Utilisable par n'importe quel adaptateur de site. La rotation du profil
TLS (chrome / firefox / safari) et la detection du challenge Cloudflare
("Just a moment...") sont communes a tous les sites.
"""

from __future__ import annotations

import random
import time
from typing import Any, Callable, Optional

from curl_cffi import requests

USER_AGENTS = ["chrome", "firefox", "safari", "chrome", "firefox"]

DEFAULT_HEADERS = {
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
}


class CloudflareBlocked(RuntimeError):
    """Cloudflare a renvoyé la page de challenge au lieu du contenu."""


class HttpClient:
    """Client HTTP avec pause anti-rate-limit, retry et backoff.

    delay/jitter : pause entre deux requetes.
    verbose      : logs.
    """

    def __init__(
        self,
        delay: float = 1.5,
        jitter: float = 0.8,
        timeout: int = 30,
        retries: int = 3,
        verbose: bool = True,
        headers: Optional[dict] = None,
    ):
        self.delay = delay
        self.jitter = jitter
        self.timeout = timeout
        self.retries = retries
        self.verbose = verbose
        self.headers = dict(DEFAULT_HEADERS)
        if headers:
            self.headers.update(headers)
        self._last_call = 0.0
        self._i = 0
        self.stats = {"requests": 0, "blocked": 0, "errors": 0}

    def log(self, msg: str) -> None:
        if self.verbose:
            print(msg, flush=True)

    def _wait(self) -> None:
        elapsed = time.time() - self._last_call
        pause = self.delay + random.uniform(0, self.jitter) - elapsed
        if pause > 0:
            time.sleep(pause)
        self._last_call = time.time()

    def _request(self, method: str, url: str, **kwargs) -> Any:
        last_err: Optional[Exception] = None
        for attempt in range(1, self.retries + 1):
            self._wait()
            imp = USER_AGENTS[self._i % len(USER_AGENTS)]
            self._i += 1
            try:
                # curl_cffi : l'impersonation se regle par requete
                hdrs = dict(self.headers)
                hdrs.update(kwargs.pop("headers", None) or {})
                fn = requests.get if method.upper() == "GET" else requests.post
                r = fn(url, impersonate=imp, timeout=self.timeout, headers=hdrs, **kwargs)
                self.stats["requests"] += 1
            except Exception as e:
                self.stats["errors"] += 1
                last_err = e
                self.log(f"  ! reseau ({imp}) {type(e).__name__} - essai {attempt}/{self.retries}")
                time.sleep(2 * attempt)
                continue

            text = getattr(r, "text", "") or ""
            if r.status_code == 200 and "Just a moment" not in text:
                return r

            if "Just a moment" in text or r.status_code in (403, 429, 503):
                self.stats["blocked"] += 1
                last_err = CloudflareBlocked(f"HTTP {r.status_code} / challenge")
                backoff = 5 * attempt + random.uniform(0, 5)
                self.log(f"  ! Cloudflare ({imp}) HTTP {r.status_code} - backoff {backoff:.0f}s "
                         f"(essai {attempt}/{self.retries})")
                time.sleep(backoff)
                continue

            self.stats["errors"] += 1
            last_err = RuntimeError(f"HTTP {r.status_code}")
            self.log(f"  ! HTTP {r.status_code} - essai {attempt}/{self.retries}")
            time.sleep(2 * attempt)

        raise last_err or RuntimeError("echec requete")

    # ------------------------------------------------------------------ api
    def get(self, url: str, **kwargs) -> str:
        """GET -> texte."""
        return self._request("GET", url, **kwargs).text

    def get_bytes(self, url: str, **kwargs) -> bytes:
        """GET -> octets (images, fichiers)."""
        return self._request("GET", url, **kwargs).content

    def get_json(self, url: str, **kwargs) -> Any:
        """GET -> JSON."""
        return self._request("GET", url, **kwargs).json()

    def post_json(self, url: str, payload: Any, **kwargs) -> Any:
        """POST JSON -> JSON."""
        kwargs.setdefault("json", payload)
        kwargs["headers"] = {**kwargs.get("headers", {}),
                             "Content-Type": "application/json"}
        return self._request("POST", url, **kwargs).json()

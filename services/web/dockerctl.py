"""Pilotage du conteneur scraper depuis l'interface web.

L'interface parle directement a l'API Docker via le socket unix (aucune
dependance supplementaire : on utilise http.client sur un socket AF_UNIX).

Le service `scraper` du compose est cree (mais pas lance) au demarrage :
l'interface peut ensuite le demarrer / l'arreter / le relancer par son nom.
"""

from __future__ import annotations

import http.client
import json
import os
import socket
from typing import Any, Optional

SOCKET_PATH = os.environ.get("DOCKER_SOCKET", "/var/run/docker.sock")
CONTAINER_NAME = os.environ.get("SCRAPER_CONTAINER", "avito-scraper")


class DockerError(RuntimeError):
    """Erreur de l'API Docker."""


class UnixHTTPConnection(http.client.HTTPConnection):
    """Connexion HTTP sur un socket unix."""

    def __init__(self, socket_path: str, timeout: float = 30):
        super().__init__("localhost", timeout=timeout)
        self.socket_path = socket_path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self.socket_path)
        self.sock = sock


class Docker:
    """Client minimal de l'API Docker (socket unix)."""

    def __init__(self, socket_path: str = SOCKET_PATH,
                 container: str = CONTAINER_NAME):
        self.socket_path = socket_path
        self.container = container

    # ------------------------------------------------------------- bas niveau
    def _request(self, method: str, path: str, body: Any = None) -> Any:
        raw = self._request_raw(method, path, body)
        if raw is None:
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except ValueError:
            return raw.decode("utf-8", "replace")

    def _request_raw(self, method: str, path: str, body: Any = None) -> Optional[bytes]:
        """Requete brute (octets) : necessaire pour le flux de logs."""
        if not os.path.exists(self.socket_path):
            raise DockerError(
                f"socket Docker introuvable ({self.socket_path}) : "
                "le pilotage du scraper est indisponible")
        payload = None
        headers = {}
        if body is not None:
            payload = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        conn = UnixHTTPConnection(self.socket_path, timeout=30)
        try:
            conn.request(method, path, body=payload, headers=headers)
            resp = conn.getresponse()
            raw = resp.read()
            if resp.status >= 400:
                raise DockerError(f"{method} {path} -> HTTP {resp.status}: "
                                  f"{raw[:300].decode('utf-8', 'replace')}")
            return raw or None
        finally:
            conn.close()

    # ------------------------------------------------------------------ api
    def inspect(self, name: Optional[str] = None) -> Optional[dict]:
        """Etat du conteneur (None s'il n'existe pas)."""
        try:
            return self._request("GET", f"/containers/{name or self.container}/json")
        except DockerError as e:
            if "404" in str(e):
                return None
            raise

    def state(self) -> str:
        """'running', 'exited', 'created', 'absent'..."""
        info = self.inspect()
        if not info:
            return "absent"
        return (info.get("State") or {}).get("Status", "unknown")

    def start(self, name: Optional[str] = None) -> None:
        self._request("POST", f"/containers/{name or self.container}/start")

    def stop(self, name: Optional[str] = None, timeout: int = 10) -> None:
        self._request("POST", f"/containers/{name or self.container}/stop?t={timeout}")

    def restart(self, name: Optional[str] = None, timeout: int = 10) -> None:
        self._request("POST", f"/containers/{name or self.container}/restart?t={timeout}")

    def logs(self, tail: int = 120, name: Optional[str] = None) -> str:
        """Dernieres lignes de log (stdout+stderr)."""
        try:
            raw = self._request_raw(
                "GET",
                f"/containers/{name or self.container}/logs"
                f"?stdout=1&stderr=1&tail={tail}")
        except DockerError:
            return ""
        if not raw:
            return ""
        if raw[:1] in (b"{", b"["):        # deja du JSON
            try:
                data = json.loads(raw.decode("utf-8"))
                return data if isinstance(data, str) else json.dumps(data)
            except ValueError:
                pass
        # le daemon renvoie un flux multiplexe :
        #   [type(1) 0 0 0 | taille(4, big endian)] puis les donnees
        out = bytearray()
        pos = 0
        while pos + 8 <= len(raw):
            size = int.from_bytes(raw[pos + 4:pos + 8], "big")
            pos += 8
            out += raw[pos:pos + size]
            pos += size
        if not out:                          # flux non multiplexe
            out = raw
        return out.decode("utf-8", "replace")

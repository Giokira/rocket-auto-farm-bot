"""Server di validazione delle licenze (lato VPS).

Il mod non contiene la logica protetta: la scarica da qui a ogni avvio. Questo
server riceve token + username, controlla nel database del bot che il token sia
di un ordine completato, non revocato e intestato a quello username, e solo
allora restituisce i byte del payload (le classi della mod, gia' offuscate).

Gira come processo separato dal bot Telegram, ma legge lo stesso database
(orders.db, in sola lettura). Ascolta su localhost: davanti ci va un reverse
proxy (nginx/caddy) che mette il TLS. Nessun segreto di firma da custodire: la
"chiave" e' solo un token opaco, la sua validita' e' una riga nel database.

Nota di sicurezza: la validazione online alza il muro contro il crack (il jar
distribuito e' vuoto, la logica arriva solo dopo un token valido) ma non lo
rende impossibile: chi ha un token valido puo' catturare i byte ricevuti e
ridistribuirli. Non esiste protezione client-side che regga contro chi ha una
licenza vera e capacita' di reverse engineering.
"""

import json
import logging
import sqlite3
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

logger = logging.getLogger(__name__)

# Stato che un ordine deve avere perche' il token valga. Allineato a
# database.STATUS_COMPLETED, ripetuto qui per non importare tutto il modulo del
# bot in un processo che deve solo leggere.
_COMPLETED = "completato"
_LICENSE_TYPE = "licenza_file"

# Limite grezzo per IP: la validazione e' rara (un avvio ogni tanto), quindi
# qualsiasi raffica e' sospetta. Non e' una difesa forte, solo un freno.
_RATE_MAX = 30
_RATE_WINDOW = 60


class _Validator:
    """Racchiude l'accesso al DB e il payload, cosi' l'handler resta sottile."""

    def __init__(self, db_path: Path, payload_path: Path):
        self.db_path = Path(db_path)
        self.payload_path = Path(payload_path)

    def payload(self) -> bytes | None:
        if not self.payload_path.is_file():
            return None
        return self.payload_path.read_bytes()

    def check(self, token: str, username: str) -> tuple[bool, str]:
        """True se il token e' spendibile per quello username.

        Ritorna (ok, motivo). Il motivo resta interno ai log: al client si
        risponde sempre allo stesso modo, per non far capire cosa e' fallito.
        """
        token = (token or "").strip()
        username = (username or "").strip()
        if not token or not username:
            return False, "token o username vuoto"

        # Sola lettura: il server di licenze non scrive mai nel database.
        conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, timeout=5)
        try:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT mc_username, status, revoked_at, product_type "
                "FROM orders WHERE license_key = ? LIMIT 1",
                (token,),
            ).fetchone()
        finally:
            conn.close()

        if row is None:
            return False, "token inesistente"
        if row["product_type"] != _LICENSE_TYPE:
            return False, "non e' un ordine di licenza"
        if row["status"] != _COMPLETED:
            return False, f"ordine in stato {row['status']}"
        if row["revoked_at"]:
            return False, "licenza revocata"
        # Lo username Minecraft e' trattato senza distinzione di maiuscole: e'
        # cosi' che il server di gioco identifica l'account.
        if (row["mc_username"] or "").strip().lower() != username.lower():
            return False, "username non corrispondente"
        return True, "ok"


def build_handler(validator: _Validator):
    hits: dict[str, list[float]] = {}

    def rate_ok(ip: str) -> bool:
        now = time.monotonic()
        window = [t for t in hits.get(ip, []) if now - t < _RATE_WINDOW]
        window.append(now)
        hits[ip] = window
        return len(window) <= _RATE_MAX

    class Handler(BaseHTTPRequestHandler):
        server_version = "lic/1.0"

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _deny(self) -> None:
            # Risposta unica per ogni fallimento: token errato, username errato,
            # ordine non pagato, revocato... il client non deve distinguerli.
            self._send(403, b'{"ok":false}', "application/json")

        def do_POST(self) -> None:  # noqa: N802 (nome imposto da BaseHTTPRequestHandler)
            if self.path.rstrip("/") != "/validate":
                self._send(404, b'{"ok":false}', "application/json")
                return

            ip = self.client_address[0]
            if not rate_ok(ip):
                logger.warning("Rate limit per %s", ip)
                self._send(429, b'{"ok":false}', "application/json")
                return

            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > 8192:
                self._deny()
                return

            try:
                data = json.loads(self.rfile.read(length).decode("utf-8"))
                token = str(data["token"])
                username = str(data["username"])
            except (ValueError, KeyError, UnicodeDecodeError):
                self._deny()
                return

            try:
                ok, reason = validator.check(token, username)
            except sqlite3.Error as exc:
                logger.error("Errore DB nella validazione: %s", exc)
                self._send(503, b'{"ok":false}', "application/json")
                return

            if not ok:
                logger.info("Validazione negata (%s) per user=%r ip=%s", reason, username, ip)
                self._deny()
                return

            payload = validator.payload()
            if payload is None:
                logger.error("Payload assente in %s", validator.payload_path)
                self._send(503, b'{"ok":false}', "application/json")
                return

            logger.info("Validazione ok per user=%r ip=%s (%d byte)", username, ip, len(payload))
            self._send(200, payload, "application/octet-stream")

        def do_GET(self) -> None:  # noqa: N802
            # Solo un health check, senza toccare il database.
            if self.path.rstrip("/") in ("/health", ""):
                self._send(200, b'{"ok":true}', "application/json")
            else:
                self._send(404, b'{"ok":false}', "application/json")

        def log_message(self, *args) -> None:
            # Silenzia il log di default di BaseHTTPRequestHandler: usiamo il nostro.
            pass

    return Handler


def build_server(db_path: Path, payload_path: Path, host: str, port: int) -> ThreadingHTTPServer:
    validator = _Validator(db_path, payload_path)
    return ThreadingHTTPServer((host, port), build_handler(validator))


def main() -> None:
    import config
    from utils.logger import setup_logging

    setup_logging()
    host = config.LICENSE_HOST
    port = config.LICENSE_PORT
    server = build_server(config.DB_PATH, config.PAYLOAD_PATH, host, port)
    logger.info("Server licenze in ascolto su %s:%d (payload: %s)",
                host, port, config.PAYLOAD_PATH)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Server licenze fermato.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

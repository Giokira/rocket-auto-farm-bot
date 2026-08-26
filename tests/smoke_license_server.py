"""Test del server di validazione licenze, senza rete reale ne' bot.

Crea un database temporaneo con qualche ordine, avvia il server su una porta a
caso in un thread e verifica che il payload esca solo per un token valido e
intestato allo username giusto.
"""

import json
import sqlite3
import sys
import tempfile
import threading
import urllib.request
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import license_server as ls


def _make_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE orders (
            id INTEGER PRIMARY KEY,
            license_key TEXT,
            mc_username TEXT,
            status TEXT,
            revoked_at TEXT,
            product_type TEXT
        );
        """
    )
    rows = [
        # token,        mc_username, status,        revoked_at, product_type
        ("TOK-OK",       "Steve",     "completato",  None,       "licenza_file"),
        ("TOK-REVOKED",  "Alex",      "completato",  "2026-01-01", "licenza_file"),
        ("TOK-PENDING",  "Bob",       "in_verifica", None,       "licenza_file"),
        ("TOK-INGAME",   "Carl",      "completato",  None,       "valuta_ingame"),
    ]
    conn.executemany(
        "INSERT INTO orders (license_key, mc_username, status, revoked_at, product_type) "
        "VALUES (?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    conn.close()


def _post(url: str, token: str, username: str):
    body = json.dumps({"token": token, "username": username}).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def run() -> None:
    tmp = Path(tempfile.mkdtemp())
    db_path = tmp / "orders.db"
    payload_path = tmp / "payload.bin"
    payload_bytes = b"PAYLOAD-CLASSES-BLOB"
    payload_path.write_bytes(payload_bytes)
    _make_db(db_path)

    server = ls.build_server(db_path, payload_path, "127.0.0.1", 0)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"

    try:
        # 1. token valido, username giusto -> payload
        status, body = _post(f"{base}/validate", "TOK-OK", "Steve")
        assert status == 200, f"atteso 200, ottenuto {status}"
        assert body == payload_bytes, "payload non corrispondente"
        print("  ok  token valido -> payload servito")

        # 2. username case-insensitive
        status, _ = _post(f"{base}/validate", "TOK-OK", "steve")
        assert status == 200, f"case-insensitive fallito: {status}"
        print("  ok  username case-insensitive")

        # 3. username sbagliato -> 403
        status, _ = _post(f"{base}/validate", "TOK-OK", "Mallory")
        assert status == 403, f"username errato doveva dare 403, dato {status}"
        print("  ok  username errato -> 403")

        # 4. token inesistente -> 403
        status, _ = _post(f"{base}/validate", "TOK-NOPE", "Steve")
        assert status == 403, f"token inesistente doveva dare 403, dato {status}"
        print("  ok  token inesistente -> 403")

        # 5. licenza revocata -> 403
        status, _ = _post(f"{base}/validate", "TOK-REVOKED", "Alex")
        assert status == 403, f"revocato doveva dare 403, dato {status}"
        print("  ok  licenza revocata -> 403")

        # 6. ordine non completato -> 403
        status, _ = _post(f"{base}/validate", "TOK-PENDING", "Bob")
        assert status == 403, f"non pagato doveva dare 403, dato {status}"
        print("  ok  ordine non completato -> 403")

        # 7. ordine di valuta (non licenza) -> 403
        status, _ = _post(f"{base}/validate", "TOK-INGAME", "Carl")
        assert status == 403, f"non-licenza doveva dare 403, dato {status}"
        print("  ok  ordine non-licenza -> 403")

        # 8. health check senza toccare il DB
        with urllib.request.urlopen(f"{base}/health", timeout=5) as resp:
            assert resp.status == 200
        print("  ok  health check")

        # 9. payload assente -> 503, mai un 200 vuoto
        payload_path.unlink()
        status, _ = _post(f"{base}/validate", "TOK-OK", "Steve")
        assert status == 503, f"payload assente doveva dare 503, dato {status}"
        print("  ok  payload assente -> 503 (mai consegna vuota)")

    finally:
        server.shutdown()
        server.server_close()

    print("smoke_license_server: tutti i casi ok")


if __name__ == "__main__":
    run()

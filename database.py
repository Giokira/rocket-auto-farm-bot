"""Accesso al database SQLite (async, via aiosqlite).

Tabelle:
  orders      -> un record per ogni ordine
  file_cache  -> memorizza il file_id di Telegram dopo il primo upload,
                 cosi' le consegne successive non ricaricano il file dal disco.

Note di design:
  - paypal_txn_id ha un indice UNIQUE parziale: lo stesso riferimento di
    pagamento non puo' essere associato a due ordini diversi (anti-riuso). In
    SQLite piu' valori NULL sono ammessi in un indice UNIQUE, quindi gli ordini
    non ancora pagati non si bloccano tra loro.
  - i riferimenti di pagamento sono normalizzati (trim + maiuscolo) prima di
    essere salvati o confrontati, altrimenti "8ab..." e "8AB..." aggirerebbero
    il vincolo.
  - order_ref e' la forma normalizzata del codice ordine (senza trattino, tutto
    maiuscolo): e' cio' che il compratore incolla nella causale e cio' su cui
    l'admin cerca quando legge il movimento su PayPal.
  - le migrazioni sono additive (ALTER TABLE ADD COLUMN): un database creato da
    una versione precedente continua a funzionare senza perdere ordini.
"""

import re
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, AsyncIterator

import aiosqlite

import config

# ---- Stati possibili di un ordine ----
STATUS_WAITING_PAYMENT = "in_attesa_pagamento"      # creato, riferimento non ancora ricevuto
STATUS_VERIFYING = "in_verifica"                    # riferimento ricevuto, attende l'admin
STATUS_TO_DELIVER_INGAME = "da_consegnare_in_gioco"  # pagato, valuta da consegnare a mano
STATUS_COMPLETED = "completato"                     # consegnato
STATUS_REJECTED = "rifiutato"                       # admin ha respinto
STATUS_CANCELLED = "annullato"                      # annullato dall'utente
STATUS_REFUNDED = "rimborsato"                      # rimborsato dall'admin
STATUS_DISPUTED = "contestato"                      # disputa / chargeback aperto

STATUS_LABELS = {
    STATUS_WAITING_PAYMENT: "In attesa di pagamento",
    STATUS_VERIFYING: "In verifica",
    STATUS_TO_DELIVER_INGAME: "Da consegnare in gioco",
    STATUS_COMPLETED: "Completato",
    STATUS_REJECTED: "Rifiutato",
    STATUS_CANCELLED: "Annullato",
    STATUS_REFUNDED: "Rimborsato",
    STATUS_DISPUTED: "Contestato",
}

# Stati che impegnano l'admin o il compratore: contano come "ordine aperto".
ACTIVE_STATUSES = (STATUS_WAITING_PAYMENT, STATUS_VERIFYING, STATUS_TO_DELIVER_INGAME)

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    order_code    TEXT    NOT NULL UNIQUE,
    order_ref     TEXT,
    user_id       INTEGER NOT NULL,
    username      TEXT,
    mc_username   TEXT,
    product_id    TEXT    NOT NULL,
    product_name  TEXT    NOT NULL,
    product_type  TEXT,
    price         REAL    NOT NULL,
    currency      TEXT    NOT NULL DEFAULT 'EUR',
    status        TEXT    NOT NULL,
    paypal_txn_id TEXT,
    license_key   TEXT,
    admin_note    TEXT,
    revoked_at    TEXT,
    created_at    TEXT    NOT NULL,
    updated_at    TEXT    NOT NULL,
    delivered_at  TEXT
);

CREATE TABLE IF NOT EXISTS file_cache (
    product_id TEXT PRIMARY KEY,
    file_id    TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

# Colonne aggiunte dopo la prima versione: applicate a database gia' esistenti.
MIGRATIONS: list[tuple[str, str]] = [
    ("order_ref", "ALTER TABLE orders ADD COLUMN order_ref TEXT"),
    ("mc_username", "ALTER TABLE orders ADD COLUMN mc_username TEXT"),
    ("product_type", "ALTER TABLE orders ADD COLUMN product_type TEXT"),
    ("paypal_txn_id", "ALTER TABLE orders ADD COLUMN paypal_txn_id TEXT"),
    ("license_key", "ALTER TABLE orders ADD COLUMN license_key TEXT"),
    ("revoked_at", "ALTER TABLE orders ADD COLUMN revoked_at TEXT"),
]

INDEXES = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_paypal_txn
    ON orders(paypal_txn_id) WHERE paypal_txn_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_ref
    ON orders(order_ref) WHERE order_ref IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_orders_user   ON orders(user_id);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
CREATE INDEX IF NOT EXISTS idx_orders_mcuser ON orders(mc_username);
"""

# Alfabeto senza caratteri ambigui (niente O/0, I/1, tutto maiuscolo): il codice
# viene ricopiato a mano nella causale del pagamento, e deve restare univoco
# anche se il compratore lo scrive in minuscolo.
_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_CODE_LEN = 5


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize_payment_id(payment_id: str) -> str:
    """Forma canonica di un riferimento di pagamento (ID PayPal, gift card...)."""
    return re.sub(r"\s+", "", payment_id.strip()).upper()


def normalize_ref(order_code: str) -> str:
    """Forma canonica del codice ordine: solo lettere/cifre, maiuscolo.

    Serve a ritrovare l'ordine anche se il compratore scrive "ord xy7qk" o
    "ORD_Xy7Qk" nella causale.
    """
    return re.sub(r"[^A-Za-z0-9]", "", order_code).upper()


def generate_order_code() -> str:
    """Codice ordine corto e non indovinabile, es. ORD-XY7QK."""
    body = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(_CODE_LEN))
    return f"ORD-{body}"


@asynccontextmanager
async def _connect() -> AsyncIterator[aiosqlite.Connection]:
    """Apre una connessione, la configura e la chiude sempre.

    Deve restare un context manager: un oggetto Connection di aiosqlite non
    puo' essere atteso due volte (il suo thread interno si avvia una sola
    volta), quindi la forma "async with await connect()" va evitata.
    """
    conn = await aiosqlite.connect(config.DB_PATH)
    try:
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA journal_mode=WAL")
        yield conn
    finally:
        await conn.close()


async def _existing_columns(conn: aiosqlite.Connection) -> set[str]:
    cur = await conn.execute("PRAGMA table_info(orders)")
    return {row["name"] for row in await cur.fetchall()}


async def init_db() -> None:
    async with _connect() as conn:
        await conn.executescript(SCHEMA)

        # Migrazione additiva per i database creati da versioni precedenti.
        columns = await _existing_columns(conn)
        for name, ddl in MIGRATIONS:
            if name not in columns:
                await conn.execute(ddl)

        # Gli indici si creano DOPO le colonne, altrimenti su un vecchio
        # database l'indice su paypal_txn_id fallirebbe.
        await conn.executescript(INDEXES)

        # Backfill: gli ordini vecchi non hanno order_ref.
        await conn.execute(
            "UPDATE orders SET order_ref = UPPER(REPLACE(order_code, '-', '')) "
            "WHERE order_ref IS NULL"
        )
        await conn.commit()


# --------------------------------------------------------------------------
# Ordini
# --------------------------------------------------------------------------

async def create_order(
    user_id: int,
    username: str | None,
    mc_username: str,
    product_id: str,
    product_name: str,
    product_type: str,
    price: float,
    currency: str = "EUR",
) -> dict[str, Any]:
    """Crea un ordine in stato "in attesa di pagamento" e lo restituisce."""
    now = _now()
    async with _connect() as conn:
        # Ritenta in caso di collisione del codice ordine (corto, quindi va
        # messa in conto anche se rara).
        for _ in range(12):
            code = generate_order_code()
            try:
                await conn.execute(
                    """INSERT INTO orders
                       (order_code, order_ref, user_id, username, mc_username,
                        product_id, product_name, product_type, price, currency,
                        status, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (code, normalize_ref(code), user_id, username, mc_username,
                     product_id, product_name, product_type, price, currency,
                     STATUS_WAITING_PAYMENT, now, now),
                )
                await conn.commit()
                break
            except aiosqlite.IntegrityError:
                continue
        else:
            raise RuntimeError("Impossibile generare un codice ordine univoco")

        cur = await conn.execute("SELECT * FROM orders WHERE order_code = ?", (code,))
        row = await cur.fetchone()
        return dict(row)


async def get_order(order_code: str) -> dict[str, Any] | None:
    async with _connect() as conn:
        cur = await conn.execute("SELECT * FROM orders WHERE order_code = ?", (order_code,))
        row = await cur.fetchone()
        return dict(row) if row else None


async def get_order_by_ref(text: str) -> dict[str, Any] | None:
    """Ritrova un ordine dalla causale letta su PayPal, in qualsiasi grafia."""
    async with _connect() as conn:
        cur = await conn.execute(
            "SELECT * FROM orders WHERE order_ref = ?", (normalize_ref(text),)
        )
        row = await cur.fetchone()
        return dict(row) if row else None


async def get_user_orders(user_id: int, limit: int = 10) -> list[dict[str, Any]]:
    async with _connect() as conn:
        cur = await conn.execute(
            "SELECT * FROM orders WHERE user_id = ? ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        )
        return [dict(r) for r in await cur.fetchall()]


async def get_orders_by_status(status: str, limit: int = 50) -> list[dict[str, Any]]:
    async with _connect() as conn:
        cur = await conn.execute(
            "SELECT * FROM orders WHERE status = ? ORDER BY id ASC LIMIT ?",
            (status, limit),
        )
        return [dict(r) for r in await cur.fetchall()]


async def search_orders(text: str, limit: int = 10) -> list[dict[str, Any]]:
    """Cerca per codice ordine, causale, username Minecraft o ID pagamento."""
    ref = normalize_ref(text)
    pay = normalize_payment_id(text)
    like = f"%{text.strip().lower()}%"
    async with _connect() as conn:
        cur = await conn.execute(
            """SELECT * FROM orders
               WHERE order_ref = ? OR paypal_txn_id = ?
                  OR LOWER(mc_username) LIKE ? OR LOWER(username) LIKE ?
               ORDER BY id DESC LIMIT ?""",
            (ref, pay, like, like, limit),
        )
        return [dict(r) for r in await cur.fetchall()]


async def get_open_order_for_user(user_id: int) -> dict[str, Any] | None:
    """Ordine piu' recente dell'utente ancora in attesa del pagamento."""
    async with _connect() as conn:
        cur = await conn.execute(
            "SELECT * FROM orders WHERE user_id = ? AND status = ? ORDER BY id DESC LIMIT 1",
            (user_id, STATUS_WAITING_PAYMENT),
        )
        row = await cur.fetchone()
        return dict(row) if row else None


async def count_active_orders(user_id: int) -> int:
    """Ordini non ancora chiusi: impedisce code infinite di ordini aperti."""
    placeholders = ",".join("?" for _ in ACTIVE_STATUSES)
    async with _connect() as conn:
        cur = await conn.execute(
            f"SELECT COUNT(*) AS n FROM orders WHERE user_id = ? "
            f"AND status IN ({placeholders})",
            (user_id, *ACTIVE_STATUSES),
        )
        row = await cur.fetchone()
        return int(row["n"])


async def payment_id_exists(payment_id: str) -> dict[str, Any] | None:
    """Restituisce l'ordine che usa gia' questo riferimento, se esiste."""
    async with _connect() as conn:
        cur = await conn.execute(
            "SELECT * FROM orders WHERE paypal_txn_id = ?",
            (normalize_payment_id(payment_id),),
        )
        row = await cur.fetchone()
        return dict(row) if row else None


async def attach_payment_id(order_code: str, payment_id: str) -> tuple[bool, str]:
    """Associa il riferimento di pagamento e porta l'ordine in "in verifica".

    Ritorna (ok, motivo). Il vincolo UNIQUE e' la rete di sicurezza definitiva
    contro il riuso, anche con due messaggi inviati nello stesso istante.
    """
    value = normalize_payment_id(payment_id)
    now = _now()
    async with _connect() as conn:
        try:
            cur = await conn.execute(
                """UPDATE orders
                   SET paypal_txn_id = ?, status = ?, updated_at = ?
                   WHERE order_code = ? AND status = ?""",
                (value, STATUS_VERIFYING, now, order_code, STATUS_WAITING_PAYMENT),
            )
            if cur.rowcount == 0:
                await conn.rollback()
                return False, "ordine_non_valido"
            await conn.commit()
            return True, "ok"
        except aiosqlite.IntegrityError:
            await conn.rollback()
            return False, "pagamento_gia_usato"


async def set_license_key(order_code: str, license_key: str) -> None:
    async with _connect() as conn:
        await conn.execute(
            "UPDATE orders SET license_key = ?, updated_at = ? WHERE order_code = ?",
            (license_key, _now(), order_code),
        )
        await conn.commit()


async def set_status(
    order_code: str,
    status: str,
    admin_note: str | None = None,
    mark_delivered: bool = False,
) -> bool:
    now = _now()
    async with _connect() as conn:
        cur = await conn.execute(
            """UPDATE orders
               SET status = ?,
                   admin_note = COALESCE(?, admin_note),
                   updated_at = ?,
                   delivered_at = CASE WHEN ? THEN ? ELSE delivered_at END
               WHERE order_code = ?""",
            (status, admin_note, now, 1 if mark_delivered else 0, now, order_code),
        )
        await conn.commit()
        return cur.rowcount > 0


async def mark_revoked(order_code: str, note: str | None = None) -> bool:
    """Segna la licenza come revocata.

    Nota: e' solo una annotazione. Una chiave gia' consegnata continua a
    funzionare offline: la revoca reale richiede un controllo lato mod.
    """
    now = _now()
    async with _connect() as conn:
        cur = await conn.execute(
            """UPDATE orders SET revoked_at = ?, updated_at = ?,
                                 admin_note = COALESCE(?, admin_note)
               WHERE order_code = ?""",
            (now, now, note, order_code),
        )
        await conn.commit()
        return cur.rowcount > 0


async def stats() -> dict[str, int]:
    async with _connect() as conn:
        cur = await conn.execute("SELECT status, COUNT(*) AS n FROM orders GROUP BY status")
        return {r["status"]: r["n"] for r in await cur.fetchall()}


# --------------------------------------------------------------------------
# Cache file_id Telegram
# --------------------------------------------------------------------------

async def get_cached_file_id(product_id: str) -> str | None:
    async with _connect() as conn:
        cur = await conn.execute(
            "SELECT file_id FROM file_cache WHERE product_id = ?", (product_id,)
        )
        row = await cur.fetchone()
        return row["file_id"] if row else None


async def set_cached_file_id(product_id: str, file_id: str) -> None:
    async with _connect() as conn:
        await conn.execute(
            """INSERT INTO file_cache (product_id, file_id, updated_at) VALUES (?, ?, ?)
               ON CONFLICT(product_id) DO UPDATE SET file_id = excluded.file_id,
                                                    updated_at = excluded.updated_at""",
            (product_id, file_id, _now()),
        )
        await conn.commit()

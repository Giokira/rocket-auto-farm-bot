"""Smoke test del layer database: ordini, anti-riuso pagamenti, migrazione."""
import asyncio
import os
import pathlib
import sqlite3
import sys

os.environ["DB_PATH"] = "smoke_test.db"
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import config  # noqa: E402
import database as db  # noqa: E402

TXN_A = "8AB12345CD678901E"
TXN_B = "9ZY98765XW543210V"


async def main() -> None:
    for suffix in ("", "-wal", "-shm"):
        p = pathlib.Path(str(config.DB_PATH) + suffix)
        if p.exists():
            p.unlink()
    await db.init_db()

    o1 = await db.create_order(111, "mario", "Steve_99", "licenza_mod",
                               "Licenza mod", "licenza_file", 10.0, "EUR")
    o2 = await db.create_order(222, None, "Alex", "ingame_5m",
                               "5M in-game", "valuta_ingame", 15.0, "EUR")
    assert o1["order_code"] != o2["order_code"]
    assert o1["status"] == db.STATUS_WAITING_PAYMENT
    assert o1["mc_username"] == "Steve_99" and o1["product_type"] == "licenza_file"
    assert o1["order_ref"] == db.normalize_ref(o1["order_code"])
    print("create_order              OK", o1["order_code"], o2["order_code"])

    # Il codice ordine e' corto e usa solo caratteri non ambigui.
    assert o1["order_code"].startswith("ORD-") and len(o1["order_code"]) == 9
    assert set(o1["order_code"][4:]) <= set(db._CODE_ALPHABET)
    print("codice ordine corto       OK")

    # La causale si ritrova anche scritta male.
    found = await db.get_order_by_ref(o1["order_code"].lower().replace("-", " "))
    assert found and found["order_code"] == o1["order_code"]
    print("get_order_by_ref          OK")

    ok, why = await db.attach_payment_id(o1["order_code"], TXN_A)
    assert ok, why
    print("attach_payment_id         OK")

    # Stesso ID, grafia diversa, su altro ordine -> respinto
    ok, why = await db.attach_payment_id(o2["order_code"], f" {TXN_A.lower()} ")
    assert not ok and why == "pagamento_gia_usato", (ok, why)
    print("anti-riuso pagamento      OK ->", why)

    dup = await db.payment_id_exists(TXN_A.lower())
    assert dup and dup["order_code"] == o1["order_code"]
    print("payment_id_exists         OK")

    # Ordine gia' in verifica non riaccetta un riferimento
    ok, why = await db.attach_payment_id(o1["order_code"], TXN_B)
    assert not ok and why == "ordine_non_valido", (ok, why)
    print("stato errato rifiutato    OK ->", why)

    assert await db.count_active_orders(111) == 1
    assert (await db.get_open_order_for_user(111)) is None
    assert (await db.get_open_order_for_user(222))["order_code"] == o2["order_code"]
    print("query utente              OK")

    pend = await db.get_orders_by_status(db.STATUS_VERIFYING)
    assert len(pend) == 1 and pend[0]["order_code"] == o1["order_code"]
    print("elenco in_verifica        OK")

    await db.set_license_key(o1["order_code"], "GKR1-" + "a" * 40)
    await db.set_status(o1["order_code"], db.STATUS_COMPLETED, "test", mark_delivered=True)
    done = await db.get_order(o1["order_code"])
    assert done["status"] == db.STATUS_COMPLETED and done["delivered_at"]
    assert done["license_key"].startswith("GKR1-")
    print("chiave + completamento    OK")

    await db.mark_revoked(o1["order_code"], "test revoca")
    assert (await db.get_order(o1["order_code"]))["revoked_at"]
    print("mark_revoked              OK")

    hits = await db.search_orders("steve_99")
    assert hits and hits[0]["order_code"] == o1["order_code"]
    hits = await db.search_orders(TXN_A)
    assert hits and hits[0]["order_code"] == o1["order_code"]
    print("search_orders             OK")

    await db.set_cached_file_id("licenza_mod", "FILEID1")
    await db.set_cached_file_id("licenza_mod", "FILEID2")
    assert await db.get_cached_file_id("licenza_mod") == "FILEID2"
    print("cache file_id (upsert)    OK")

    print("stats:", await db.stats())

    # ---- Migrazione da uno schema vecchio (versione crypto) ----
    old_path = config.DB_PATH.parent / "smoke_legacy.db"
    for suffix in ("", "-wal", "-shm"):
        p = pathlib.Path(str(old_path) + suffix)
        if p.exists():
            p.unlink()
    legacy = sqlite3.connect(old_path)
    legacy.executescript("""
        CREATE TABLE orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_code TEXT NOT NULL UNIQUE,
            user_id INTEGER NOT NULL,
            username TEXT,
            product_id TEXT NOT NULL,
            product_name TEXT NOT NULL,
            price REAL NOT NULL,
            currency TEXT NOT NULL DEFAULT 'USDT',
            status TEXT NOT NULL,
            tx_hash TEXT,
            admin_note TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            delivered_at TEXT
        );
        INSERT INTO orders (order_code, user_id, username, product_id, product_name,
                            price, currency, status, tx_hash, created_at, updated_at)
        VALUES ('ORD-20260101-ABC123', 777, 'vecchio', 'mymod', 'MyMod',
                9.99, 'USDT', 'completato', 'deadbeef', '2026-01-01', '2026-01-01');
    """)
    legacy.commit()
    legacy.close()

    original = config.DB_PATH
    config.DB_PATH = old_path
    try:
        await db.init_db()
        migrated = await db.get_order("ORD-20260101-ABC123")
        assert migrated is not None, "ordine storico perso nella migrazione"
        assert migrated["price"] == 9.99 and migrated["status"] == "completato"
        assert migrated["order_ref"] == "ORD20260101ABC123", migrated["order_ref"]
        for col in ("mc_username", "license_key", "product_type", "paypal_txn_id"):
            assert col in migrated, f"colonna {col} non aggiunta"
        # Il database migrato resta scrivibile e i nuovi vincoli funzionano.
        nuovo = await db.create_order(888, None, "Herobrine", "ingame_1m",
                                      "1M in-game", "valuta_ingame", 5.0, "EUR")
        ok, _ = await db.attach_payment_id(nuovo["order_code"], TXN_A)
        assert ok
        print("migrazione DB vecchio     OK (ordine storico intatto)")
    finally:
        config.DB_PATH = original

    # ---- Rate limiter ----
    from utils.ratelimit import RateLimiter
    rl = RateLimiter(max_calls=2, per_seconds=60)
    assert rl.hit(1)[0] and rl.hit(1)[0]
    allowed, retry = rl.hit(1)
    assert not allowed and retry > 0
    assert rl.hit(2)[0]  # utente diverso, contatore indipendente
    print("rate limiter              OK (retry_after=%ds)" % retry)

    # ---- Validatori ----
    from handlers.common import is_valid_mc_username
    assert is_valid_mc_username("Steve_99")
    assert is_valid_mc_username("  abc  ")
    assert not is_valid_mc_username("ab")             # troppo corto
    assert not is_valid_mc_username("a" * 17)         # troppo lungo
    assert not is_valid_mc_username("Ciao Mondo")     # spazio
    assert not is_valid_mc_username("nome-utente")    # trattino
    assert not is_valid_mc_username("drop;table")
    print("username Minecraft        OK")

    print("\nTUTTI I TEST PASSATI")


asyncio.run(main())

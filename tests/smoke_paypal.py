"""Verifica automatica PayPal e keygen: nessuna rete, nessuna chiave privata reale."""
import asyncio
import os
import pathlib
import sys

os.environ.update({
    "TELEGRAM_BOT_TOKEN": "123456:AAFakeTokenForLocalTestOnly_abcdefghijk",
    "ADMIN_USER_ID": "424242",
    "PAYPAL_EMAIL": "pagamenti@example.com",
    "PAYPAL_MODE": "beni_servizi",
    "PAYPAL_CLIENT_ID": "fake-client-id",
    "PAYPAL_SECRET": "fake-secret",
    "DB_PATH": "paypal_test.db",
})
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import config  # noqa: E402
import paypal  # noqa: E402

PREZZO = 10.00


def order_payload(value="10.00", currency="EUR", status="COMPLETED",
                  payee="pagamenti@example.com"):
    return {
        "id": "8AB12345CD678901E",
        "status": status,
        "purchase_units": [{
            "amount": {"value": value, "currency_code": currency},
            "payee": {"email_address": payee},
        }],
    }


def capture_payload(value="10.00", currency="EUR", status="COMPLETED",
                    payee="pagamenti@example.com"):
    return {
        "id": "8AB12345CD678901E",
        "status": status,
        "amount": {"value": value, "currency_code": currency},
        "payee": {"email_address": payee},
    }


async def main() -> None:
    assert config.paypal_auto_verify_enabled(), "verifica attesa attiva in beni_servizi"

    ok = paypal.evaluate(order_payload(), PREZZO)
    assert ok.ok and not ok.degraded, ok
    print("pagamento corretto        OK ->", ok.label)

    # Importo insufficiente
    basso = paypal.evaluate(order_payload(value="4.99"), PREZZO)
    assert not basso.ok and "inferiore" in basso.reason, basso
    print("importo basso             OK ->", basso.reason)

    # Destinatario sbagliato: soldi finiti su un altro conto
    altro = paypal.evaluate(order_payload(payee="ladro@example.com"), PREZZO)
    assert not altro.ok and "destinatario" in altro.reason, altro
    print("destinatario errato       OK ->", altro.reason)

    # Valuta diversa
    usd = paypal.evaluate(order_payload(currency="USD"), PREZZO)
    assert not usd.ok and "valuta" in usd.reason, usd
    print("valuta errata             OK ->", usd.reason)

    # Pagamento non completato
    pend = paypal.evaluate(order_payload(status="PENDING"), PREZZO)
    assert not pend.ok and "stato" in pend.reason, pend
    print("stato non completato      OK ->", pend.reason)

    # Un centesimo in piu' o l'arrotondamento non devono far fallire
    assert paypal.evaluate(order_payload(value="10.01"), PREZZO).ok
    assert paypal.evaluate(order_payload(value="9.995"), PREZZO).ok
    print("tolleranza arrotondamento OK")

    # Stessa logica sulla forma "capture"
    assert paypal.evaluate(capture_payload(), PREZZO).ok
    assert not paypal.evaluate(capture_payload(value="1.00"), PREZZO).ok
    print("formato capture           OK")

    # In Amici e Famiglia la verifica automatica non parte nemmeno.
    config.PAYPAL_MODE = config.MODE_FRIENDS
    assert not config.paypal_auto_verify_enabled()
    result = await paypal.verify_payment("8AB12345CD678901E", PREZZO)
    assert result.degraded and not result.ok, result
    print("amici_famiglia degrada    OK ->", result.label)
    config.PAYPAL_MODE = config.MODE_GOODS

    # ---- Keygen: costruzione comando ed estrazione chiave, senza eseguire nulla ----
    import keygen
    cmd = keygen.build_command("Steve_99")
    assert cmd[-1] == "Steve_99" and "{username}" not in " ".join(cmd), cmd
    print("build_command             OK ->", " ".join(cmd))

    # Percorso con spazi fra virgolette: le virgolette non devono finire nel
    # nome dell'eseguibile, altrimenti il comando non parte.
    config.KEYGEN_CMD = r'"C:\Program Files\Java\bin\java.exe" tools/Keygen.java {username}'
    quoted = keygen.build_command("Steve_99")
    assert quoted[0] == r"C:\Program Files\Java\bin\java.exe", quoted
    assert quoted[-1] == "Steve_99"
    config.KEYGEN_CMD = "java tools/Keygen.java {username}"
    print("percorso fra virgolette   OK ->", quoted[0])

    # Senza il segnaposto il comando genererebbe sempre la stessa chiave.
    config.KEYGEN_CMD = "java tools/Keygen.java"
    try:
        keygen.build_command("Steve_99")
        raise AssertionError("doveva rifiutare KEYGEN_CMD senza {username}")
    except keygen.KeygenError:
        pass
    config.KEYGEN_CMD = "java tools/Keygen.java {username}"
    print("KEYGEN_CMD senza segnaposto OK")

    chiave = "GKR1-" + "A" * 40
    assert keygen.extract_key(f"rumore\n{chiave}\naltro rumore") == chiave
    assert keygen.extract_key("nessuna chiave qui") is None
    print("extract_key               OK")

    # Senza chiave privata sul disco la generazione fallisce di proposito.
    config.PRIVATE_KEY_PATH = pathlib.Path("percorso/che/non/esiste.key")
    try:
        await keygen.generate_license_key("Steve_99")
        raise AssertionError("doveva fallire senza chiave privata")
    except keygen.KeygenError as exc:
        assert "non trovata" in str(exc)
        print("senza chiave privata      OK ->", str(exc)[:60])

    # Username non valido: rifiutato prima di costruire qualsiasi comando.
    for cattivo in ("ab", "nome con spazi", "a; rm -rf /", ""):
        try:
            await keygen.generate_license_key(cattivo)
            raise AssertionError(f"doveva rifiutare {cattivo!r}")
        except keygen.KeygenError:
            pass
    print("username rifiutato        OK (nessun comando costruito)")

    print("\nPAYPAL + KEYGEN OK")


asyncio.run(main())

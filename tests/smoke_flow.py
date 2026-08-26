"""Simulazione del flusso completo senza rete: bot finto, keygen finto, handler reali."""
import asyncio
import os
import pathlib
import sys
import types

ADMIN = 424242
CLIENTE = 111222
ALTRO = 999333

os.environ.update({
    "TELEGRAM_BOT_TOKEN": "123456:AAFakeTokenForLocalTestOnly_abcdefghijk",
    "ADMIN_USER_ID": str(ADMIN),
    "PAYPAL_EMAIL": "pagamenti@example.com",
    "PAYPAL_MODE": "amici_famiglia",
    # Questo test copre il flusso PayPal (ID transazione): forza quel metodo.
    "PAYMENT_METHOD": "paypal",
    "DB_PATH": "flow_test.db",
})
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import catalog  # noqa: E402
import config  # noqa: E402
config.validate()

import database as db  # noqa: E402
import delivery  # noqa: E402
from handlers import admin as admin_h, ordine as ordine_h  # noqa: E402

TXN = "8AB12345CD678901E"
TXN2 = "7QW65432ZX109876M"
TXN3 = "6MN11223KL445566P"
FAKE_KEY = "GKR1-" + "Zm9vYmFyYmF6cXV4" * 3

sent: list[tuple] = []
keygen_calls: list[str] = []
keygen_should_fail = False


# --------------------------------------------------------------------------
# Bot finto
# --------------------------------------------------------------------------

class FakeDocument:
    def __init__(self, filename):
        self.file_id = f"FILEID::{filename}"


class FakeMessage:
    def __init__(self, chat_id, text=None):
        self.chat_id = chat_id
        self.message_id = 1
        self.text = text
        self.photo = None
        self.document = None

    async def reply_html(self, text, reply_markup=None):
        sent.append(("reply", self.chat_id, text))
        return self

    async def reply_text(self, text, reply_markup=None):
        sent.append(("reply", self.chat_id, text))
        return self


class FakeQuery:
    def __init__(self, data, chat_id):
        self.data = data
        self.message = FakeMessage(chat_id)
        self.alerts: list[str] = []

    async def answer(self, text=None, show_alert=False):
        if text:
            self.alerts.append(text)

    async def edit_message_text(self, text, parse_mode=None, reply_markup=None):
        sent.append(("edit", self.message.chat_id, text))


class FakeBot:
    async def send_message(self, chat_id, text, parse_mode=None, reply_markup=None):
        sent.append(("msg", chat_id, text))
        return FakeMessage(chat_id, text)

    async def send_document(self, chat_id, document, caption=None,
                            parse_mode=None, filename=None):
        name = filename or getattr(document, "name", str(document))
        sent.append(("doc", chat_id, name))
        msg = FakeMessage(chat_id)
        msg.document = FakeDocument(name)
        return msg

    async def forward_message(self, chat_id, from_chat_id, message_id):
        sent.append(("fwd", chat_id, str(message_id)))
        return FakeMessage(chat_id)


class FakePhoto:
    def __init__(self, uid):
        self.file_unique_id = uid


def new_ctx():
    """Un context per utente: user_data e' per-utente anche in PTB."""
    return types.SimpleNamespace(bot=FakeBot(), user_data={}, args=[])


def fake_update(user_id, username=None, query_data=None, text=None, photo_uid=None):
    upd = types.SimpleNamespace()
    upd.effective_user = types.SimpleNamespace(
        id=user_id, username=username, first_name="Tester", full_name="Tester"
    )
    upd.callback_query = FakeQuery(query_data, user_id) if query_data else None
    upd.message = None
    if text is not None:
        upd.message = FakeMessage(user_id, text)
    if photo_uid is not None:
        upd.message = FakeMessage(user_id)
        upd.message.photo = [FakePhoto(photo_uid)]
    return upd


async def fake_keygen(username: str) -> str:
    keygen_calls.append(username)
    if keygen_should_fail:
        from keygen import KeygenError
        raise KeygenError("chiave privata non trovata (simulato)")
    return FAKE_KEY


def texts_to(dest):
    return [t for kind, who, t in sent if who == dest]


# --------------------------------------------------------------------------
# Test
# --------------------------------------------------------------------------

async def main() -> None:
    global keygen_should_fail

    for suffix in ("", "-wal", "-shm"):
        p = pathlib.Path(str(config.DB_PATH) + suffix)
        if p.exists():
            p.unlink()
    await db.init_db()

    # Questo test copre il modello offline (keygen locale): forza la modalita'
    # e usa un keygen finto, cosi' nessuna chiave privata reale e' coinvolta.
    config.LICENSE_MODE = config.MODE_LICENSE_OFFLINE
    delivery.generate_license_key = fake_keygen

    licenza = catalog.get_product("licenza_mod")
    created_dummy = False
    if not licenza.file_path.is_file():
        licenza.file_path.write_bytes(b"PK\x03\x04 finto jar per test")
        created_dummy = True

    try:
        ctx = new_ctx()

        # 1) Compra: chiede lo username, nessun ordine ancora creato
        sent.clear()
        await ordine_h.buy_callback(
            fake_update(CLIENTE, "mario", query_data="ord:buy:licenza_mod"), ctx)
        assert await db.get_user_orders(CLIENTE) == []
        assert ctx.user_data[ordine_h.PENDING_KEY] == "licenza_mod"
        assert "username Minecraft" in sent[-1][2]
        print("1. compra chiede username OK")

        # 2) Username invalido: rifiutato, nessun ordine
        sent.clear()
        for cattivo in ("ab", "a" * 17, "nome con spazi", "no-trattini"):
            await ordine_h.text_message(fake_update(CLIENTE, "mario", text=cattivo), ctx)
            assert await db.get_user_orders(CLIENTE) == [], f"ordine creato con {cattivo!r}"
            assert "Username non valido" in sent[-1][2]
        assert ctx.user_data.get(ordine_h.PENDING_KEY) == "licenza_mod"
        print("2. username invalido      OK (4 varianti rifiutate)")

        # 3) Username valido: ordine creato, istruzioni di pagamento neutre
        sent.clear()
        await ordine_h.text_message(fake_update(CLIENTE, "mario", text="Steve_99"), ctx)
        orders = await db.get_user_orders(CLIENTE)
        assert len(orders) == 1, orders
        order = orders[0]
        assert order["mc_username"] == "Steve_99"
        assert order["product_type"] == catalog.TYPE_LICENSE
        assert order["currency"] == "EUR" and order["price"] == 10.00
        assert ordine_h.PENDING_KEY not in ctx.user_data
        pay_text = sent[-1][2]
        assert order["order_code"] in pay_text
        assert "10,00 EUR" in pay_text
        assert "pagamenti@example.com" in pay_text
        assert "Amici e Famiglia" in pay_text and "NON Beni e servizi" in pay_text
        # Causale neutra: nel messaggio di pagamento non compare il prodotto.
        for parola in ("Licenza mod", "mod", "cheat", "hack"):
            assert parola.lower() not in pay_text.lower(), f"{parola!r} nel testo di pagamento"
        assert "Steve_99" in texts_to(CLIENTE)[0]  # conferma username separata
        print("3. ordine", order["order_code"], "creato, causale neutra OK")

        # 4) Riferimento di pagamento malformato
        sent.clear()
        await ordine_h.text_message(fake_update(CLIENTE, text="boh"), ctx)
        assert (await db.get_order(order["order_code"]))["status"] == db.STATUS_WAITING_PAYMENT
        assert "Non riconosco" in sent[-1][2]
        print("4. rif. malformato        OK")

        # 4b) Il codice ordine non e' un riferimento di pagamento.
        # Con le gift card spente lo blocca gia' il formato; con le gift card
        # accese il codice passerebbe il formato, quindi serve il controllo
        # esplicito sul codice ordine.
        sent.clear()
        await ordine_h.text_message(fake_update(CLIENTE, text=order["order_code"]), ctx)
        assert "non sembra un ID transazione" in sent[-1][2]

        config.GIFTCARD_ENABLED = True
        try:
            sent.clear()
            await ordine_h.text_message(
                fake_update(CLIENTE, text=order["order_code"]), ctx)
            await ordine_h.text_message(
                fake_update(CLIENTE, text=order["order_code"].lower().replace("-", "")), ctx)
            assert all("codice ordine" in t for t in texts_to(CLIENTE)), texts_to(CLIENTE)
        finally:
            config.GIFTCARD_ENABLED = False

        ancora = await db.get_order(order["order_code"])
        assert ancora["status"] == db.STATUS_WAITING_PAYMENT
        assert ancora["paypal_txn_id"] is None, "codice ordine accettato come pagamento!"
        print("4b. codice ordine != pagamento OK")

        # 5) ID valido -> in verifica + notifica admin
        sent.clear()
        await ordine_h.text_message(fake_update(CLIENTE, "mario", text=TXN.lower()), ctx)
        order = await db.get_order(order["order_code"])
        assert order["status"] == db.STATUS_VERIFYING
        assert order["paypal_txn_id"] == TXN  # normalizzato in maiuscolo
        admin_msgs = texts_to(ADMIN)
        assert admin_msgs and "10,00 EUR" in admin_msgs[-1] and "Steve_99" in admin_msgs[-1]
        print("5. pagamento registrato + admin notificato OK")

        # 6) Altro utente prova a riusare lo stesso ID
        sent.clear()
        ctx2 = new_ctx()
        await ordine_h.buy_callback(
            fake_update(ALTRO, "furbo", query_data="ord:buy:licenza_mod"), ctx2)
        await ordine_h.text_message(fake_update(ALTRO, "furbo", text="Alex"), ctx2)
        await ordine_h.text_message(fake_update(ALTRO, "furbo", text=TXN), ctx2)
        altro = (await db.get_user_orders(ALTRO))[0]
        assert altro["status"] == db.STATUS_WAITING_PAYMENT, "ID riusato!"
        assert altro["paypal_txn_id"] is None
        assert "gia' registrato" in sent[-1][2]
        assert not any(k == "doc" for k, _, _ in sent), "consegna a fronte di riuso!"
        print("6. riuso ID pagamento     OK (bloccato, nessuna consegna)")

        # 7) Non-admin prova a confermare
        sent.clear()
        intruso = fake_update(ALTRO, query_data=f"adm:ok:{order['order_code']}")
        await admin_h.approve_callback(intruso, ctx2)
        assert (await db.get_order(order["order_code"]))["status"] == db.STATUS_VERIFYING
        assert intruso.callback_query.alerts == ["Non autorizzato."]
        assert not keygen_calls, "keygen invocato da un non-admin!"
        assert not any(k == "doc" for k, _, _ in sent)
        print("7. guardia admin          OK")

        # 8) Keygen fallito -> ordine aperto, niente consegna
        sent.clear()
        keygen_should_fail = True
        await admin_h.approve_callback(
            fake_update(ADMIN, query_data=f"adm:ok:{order['order_code']}"), ctx)
        keygen_should_fail = False
        assert (await db.get_order(order["order_code"]))["status"] == db.STATUS_VERIFYING
        assert (await db.get_order(order["order_code"]))["license_key"] is None
        assert not any(k == "doc" for k, _, _ in sent), "file inviato con keygen rotto!"
        assert "Consegna NON riuscita" in texts_to(ADMIN)[-1]
        print("8. keygen fallito         OK (ordine resta in verifica)")

        # 9) Conferma admin -> chiave + jar + giokiradd.key + completato
        sent.clear()
        keygen_calls.clear()
        await admin_h.approve_callback(
            fake_update(ADMIN, query_data=f"adm:ok:{order['order_code']}"), ctx)
        docs = [name for kind, who, name in sent if kind == "doc" and who == CLIENTE]
        assert keygen_calls == ["Steve_99"], keygen_calls
        assert any(d.endswith(".jar") for d in docs), docs
        assert delivery.KEY_FILENAME in docs, docs
        final = await db.get_order(order["order_code"])
        assert final["status"] == db.STATUS_COMPLETED and final["delivered_at"]
        assert final["license_key"] == FAKE_KEY
        assert FAKE_KEY in texts_to(CLIENTE)[-1]  # chiave anche come testo copiabile
        print("9. conferma licenza       OK (jar +", delivery.KEY_FILENAME, "+ chiave)")

        # 10) Seconda conferma: niente rigenerazione, niente riconsegna
        sent.clear()
        keygen_calls.clear()
        dup = fake_update(ADMIN, query_data=f"adm:ok:{order['order_code']}")
        await admin_h.approve_callback(dup, ctx)
        assert keygen_calls == [], "chiave rigenerata!"
        assert not any(k == "doc" for k, _, _ in sent), "consegnato due volte!"
        assert dup.callback_query.alerts == ["Ordine gia' completato."]
        print("10. idempotenza conferma  OK")

        # 11) Valuta in gioco: nessun file, stato intermedio
        sent.clear()
        ctx3 = new_ctx()
        await ordine_h.buy_callback(
            fake_update(CLIENTE, "mario", query_data="ord:buy:ingame_5m"), ctx3)
        await ordine_h.text_message(fake_update(CLIENTE, "mario", text="Steve_99"), ctx3)
        await ordine_h.text_message(fake_update(CLIENTE, "mario", text=TXN2), ctx3)
        ingame = [o for o in await db.get_user_orders(CLIENTE)
                  if o["product_id"] == "ingame_5m"][0]
        assert ingame["status"] == db.STATUS_VERIFYING and ingame["price"] == 15.00

        sent.clear()
        await admin_h.approve_callback(
            fake_update(ADMIN, query_data=f"adm:ok:{ingame['order_code']}"), ctx3)
        ingame = await db.get_order(ingame["order_code"])
        assert ingame["status"] == db.STATUS_TO_DELIVER_INGAME, ingame["status"]
        assert not any(k == "doc" for k, _, _ in sent), "file inviato per la valuta!"
        assert "in gioco" in texts_to(CLIENTE)[-1]
        assert "Steve_99" in texts_to(ADMIN)[-1]
        print("11. valuta: conferma      OK (nessun file, da_consegnare_in_gioco)")

        # 12) Non-admin non puo' chiudere la consegna in gioco
        sent.clear()
        intruso2 = fake_update(ALTRO, query_data=f"adm:done:{ingame['order_code']}")
        await admin_h.delivered_callback(intruso2, ctx3)
        assert (await db.get_order(ingame["order_code"]))["status"] == db.STATUS_TO_DELIVER_INGAME
        assert intruso2.callback_query.alerts == ["Non autorizzato."]
        print("12. guardia su Consegnato OK")

        # 13) Admin preme Consegnato -> completato + avviso al compratore
        sent.clear()
        await admin_h.delivered_callback(
            fake_update(ADMIN, query_data=f"adm:done:{ingame['order_code']}"), ctx3)
        ingame = await db.get_order(ingame["order_code"])
        assert ingame["status"] == db.STATUS_COMPLETED and ingame["delivered_at"]
        assert "Consegna effettuata" in texts_to(CLIENTE)[-1]
        print("13. Consegnato -> completato OK")

        # 14) Screenshot come prova di pagamento, non riutilizzabile
        sent.clear()
        ctx4 = new_ctx()
        await ordine_h.buy_callback(
            fake_update(CLIENTE, "mario", query_data="ord:buy:ingame_1m"), ctx4)
        await ordine_h.text_message(fake_update(CLIENTE, "mario", text="Steve_99"), ctx4)
        await ordine_h.photo_message(fake_update(CLIENTE, "mario", photo_uid="AQADfoto1"), ctx4)
        shot_order = [o for o in await db.get_user_orders(CLIENTE)
                      if o["product_id"] == "ingame_1m"][0]
        assert shot_order["status"] == db.STATUS_VERIFYING
        assert shot_order["paypal_txn_id"] == "IMGAQADFOTO1"
        assert any(k == "fwd" and who == ADMIN for k, who, _ in sent), "screenshot non inoltrato"

        # ALTRO ha ancora un ordine in attesa (pagamento rifiutato per riuso).
        # Ora si accetta un solo ordine aperto per utente, quindi va chiuso prima
        # di aprirne un altro.
        pendente = await db.get_open_order_for_user(ALTRO)
        if pendente:
            await db.set_status(pendente["order_code"], db.STATUS_CANCELLED)

        ctx5 = new_ctx()
        await ordine_h.buy_callback(
            fake_update(ALTRO, "furbo", query_data="ord:buy:ingame_1m"), ctx5)
        await ordine_h.text_message(fake_update(ALTRO, "furbo", text="Alex"), ctx5)
        sent.clear()
        await ordine_h.photo_message(fake_update(ALTRO, "furbo", photo_uid="AQADfoto1"), ctx5)
        riuso = [o for o in await db.get_user_orders(ALTRO)
                 if o["product_id"] == "ingame_1m"][0]
        assert riuso["status"] == db.STATUS_WAITING_PAYMENT, "screenshot riusato!"
        assert "gia' usato" in sent[-1][2]
        print("14. screenshot            OK (accettato una volta sola)")

        # 15) Rifiuto + rimborso + revoca
        sent.clear()
        await admin_h.reject_callback(
            fake_update(ADMIN, query_data=f"adm:no:{shot_order['order_code']}"), ctx4)
        assert (await db.get_order(shot_order["order_code"]))["status"] == db.STATUS_REJECTED
        assert any(who == CLIENTE for _, who, _ in sent), "utente non avvisato del rifiuto"

        sent.clear()
        await admin_h.refund_callback(
            fake_update(ADMIN, query_data=f"adm:refund:{order['order_code']}"), ctx)
        assert (await db.get_order(order["order_code"]))["status"] == db.STATUS_REFUNDED
        await admin_h.revoke_callback(
            fake_update(ADMIN, query_data=f"adm:revoke:{order['order_code']}"), ctx)
        revocato = await db.get_order(order["order_code"])
        assert revocato["revoked_at"], "revoca non annotata"
        print("15. rifiuto/rimborso/revoca OK")

        # 15b) /rimborso accetta il codice in qualsiasi grafia
        sent.clear()
        ctx_cmd = new_ctx()
        ctx_cmd.args = [ingame["order_code"].lower().replace("-", "")]
        await admin_h.refund_command(fake_update(ADMIN, text="/rimborso"), ctx_cmd)
        assert (await db.get_order(ingame["order_code"]))["status"] == db.STATUS_REFUNDED
        # e ripristino lo stato per i controlli successivi
        await db.set_status(ingame["order_code"], db.STATUS_COMPLETED)
        print("15b. /rimborso senza trattino OK")

        # 16) Non-admin non puo' rimborsare ne' revocare
        sent.clear()
        for cb, handler in ((f"adm:refund:{ingame['order_code']}", admin_h.refund_callback),
                            (f"adm:revoke:{order['order_code']}", admin_h.revoke_callback)):
            u = fake_update(ALTRO, query_data=cb)
            await handler(u, ctx2)
            assert u.callback_query.alerts == ["Non autorizzato."]
        assert (await db.get_order(ingame["order_code"]))["status"] == db.STATUS_COMPLETED
        print("16. guardia rimborso/revoca OK")

        # 17) Rate limiting sul bottone compra
        ordine_h.order_limiter.reset(555)
        ctx6 = new_ctx()
        blocked = False
        for _ in range(config.ORDER_RATE_LIMIT_MAX + 2):
            u = fake_update(555, query_data="ord:buy:ingame_1m")
            await ordine_h.buy_callback(u, ctx6)
            if any("Troppe richieste" in a for a in u.callback_query.alerts):
                blocked = True
                break
        assert blocked, "rate limiting non scattato"
        print("17. rate limiting         OK")

        print("\nFLUSSO COMPLETO OK")
    finally:
        if created_dummy:
            licenza.file_path.unlink()


asyncio.run(main())

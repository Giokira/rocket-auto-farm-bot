"""Verifica che l'applicazione si costruisca e che i callback_data raggiungano
l'handler giusto (nessun pattern che ne oscura un altro)."""
import os
import pathlib
import sys

os.environ.update({
    "TELEGRAM_BOT_TOKEN": "123456:AAFakeTokenForLocalTestOnly_abcdefghijk",
    "ADMIN_USER_ID": "424242",
    "PAYPAL_EMAIL": "test@example.com",
    "DB_PATH": "smoke_test.db",
})
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import config  # noqa: E402
warnings = config.validate()
assert config.PAYPAL_MODE == config.MODE_FRIENDS, "default atteso: amici_famiglia"
assert not config.paypal_auto_verify_enabled(), "verifica API va disattivata in A&F"
assert any("amici_famiglia" in w for w in warnings), warnings
print("default amici_famiglia + avviso: OK")

import bot as botmod  # noqa: E402
from telegram.ext import CallbackQueryHandler, CommandHandler, MessageHandler  # noqa: E402

app = botmod.build_application()
handlers = app.handlers[0]
print("handler registrati:", len(handlers))

commands = sorted(c for h in handlers if isinstance(h, CommandHandler) for c in h.commands)
print("comandi:", commands)
assert commands == ["admin", "catalogo", "cerca", "disputa", "help",
                    "ordine", "rimborso", "start"], commands

msg_handlers = [h for h in handlers if isinstance(h, MessageHandler)]
assert len(msg_handlers) == 2, "atteso un handler foto e uno testo"
assert handlers.index(msg_handlers[-1]) == len(handlers) - 1, "il testo deve stare in fondo"
print("MessageHandler in coda: OK")


from telegram import CallbackQuery, Chat, Update, User  # noqa: E402

_user = User(id=1, first_name="Test", is_bot=False)
_chat = Chat(id=1, type="private")


def _update(data):
    return Update(
        update_id=1,
        callback_query=CallbackQuery(
            id="1", from_user=_user, chat_instance="ci", data=data
        ),
    )


def route(data):
    upd = _update(data)
    for h in handlers:
        if isinstance(h, CallbackQueryHandler) and h.check_update(upd):
            return h.callback.__name__
    return None


cases = {
    "cat:list": "catalog_callback",
    "cat:show:licenza_mod": "product_callback",
    "ord:buy:licenza_mod": "buy_callback",
    "ord:mine": "my_orders_callback",
    "ord:cancel:ORD-AB12C": "cancel_order_callback",
    "ord:gift:ORD-AB12C": "giftcard_callback",
    "nav:home": "home_callback",
    "adm:list": "admin_list_callback",
    "adm:show:ORD-AB12C": "admin_detail_callback",
    "adm:ok:ORD-AB12C": "approve_callback",
    "adm:no:ORD-AB12C": "reject_callback",
    "adm:done:ORD-AB12C": "delivered_callback",
    "adm:refund:ORD-AB12C": "refund_callback",
    "adm:disp:ORD-AB12C": "dispute_callback",
    "adm:revoke:ORD-AB12C": "revoke_callback",
}
for data, expected in cases.items():
    got = route(data)
    assert got == expected, f"{data} -> {got}, atteso {expected}"
    print(f"routing {data:24s} -> {got}")

# I decoratori devono conservare il nome originale (functools.wraps)
from handlers.admin import approve_callback, reject_callback  # noqa: E402
assert approve_callback.__name__ == "approve_callback"
assert reject_callback.__name__ == "reject_callback"

# Il catalogo deve contenere i quattro prodotti richiesti, con i prezzi giusti.
import catalog  # noqa: E402
attesi = {
    "licenza_mod": ("Licenza mod", 10.00, catalog.TYPE_LICENSE),
    "ingame_1m": ("1M in-game", 5.00, catalog.TYPE_INGAME),
    "ingame_5m": ("5M in-game", 15.00, catalog.TYPE_INGAME),
    "ingame_10m": ("10M in-game", 20.00, catalog.TYPE_INGAME),
}
assert set(catalog.PRODUCTS) == set(attesi), set(catalog.PRODUCTS)
for pid, (nome, prezzo, ptype) in attesi.items():
    p = catalog.get_product(pid)
    assert (p.name, p.price, p.product_type, p.currency) == (nome, prezzo, ptype, "EUR"), p
assert catalog.get_product("licenza_mod").price_label == "10,00 EUR"
print("catalogo (4 prodotti, EUR): OK")

print("\nAPP OK")

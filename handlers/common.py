"""Pezzi condivisi tra i vari handler: escaping, tastiere, formattazione ordini.

Tutti i messaggi usano parse_mode=HTML (non MarkdownV2): con HTML basta
escapare < > & con html.escape, mentre MarkdownV2 richiede di escapare una
dozzina di caratteri e si rompe sui nomi utente con underscore.
"""

import html
import re
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

import catalog
import database as db

# Username Minecraft: 3-16 caratteri, lettere, cifre e underscore.
MC_USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,16}$")

# ID transazione PayPal: 17 caratteri alfanumerici nella pratica; si accetta un
# intervallo un po' piu' largo per non rifiutare formati legittimi.
PAYPAL_TXN_RE = re.compile(r"^[A-Z0-9]{12,24}$")

# Codice gift card: ammette anche i trattini dei codici a blocchi.
GIFTCARD_RE = re.compile(r"^[A-Z0-9][A-Z0-9-]{7,40}$")

# ---- callback_data usati nelle tastiere inline ----
CB_CATALOG = "cat:list"
CB_PRODUCT = "cat:show:"          # + product_id
CB_BUY = "ord:buy:"               # + product_id
CB_MY_ORDERS = "ord:mine"
CB_CANCEL_ORDER = "ord:cancel:"   # + order_code
CB_GIFTCARD = "ord:gift:"         # + order_code
CB_HOME = "nav:home"
CB_ADMIN_LIST = "adm:list"
CB_ADMIN_APPROVE = "adm:ok:"      # + order_code
CB_ADMIN_REJECT = "adm:no:"       # + order_code
CB_ADMIN_DELIVERED = "adm:done:"  # + order_code (valuta consegnata in gioco)
CB_ADMIN_REFUND = "adm:refund:"   # + order_code
CB_ADMIN_DISPUTE = "adm:disp:"    # + order_code
CB_ADMIN_REVOKE = "adm:revoke:"   # + order_code
CB_ADMIN_DETAIL = "adm:show:"     # + order_code

PAY_PAYPAL = "paypal"
PAY_GIFTCARD = "giftcard"


def esc(value: Any) -> str:
    """Rende sicuro qualsiasi valore dentro un messaggio HTML."""
    return html.escape(str(value if value is not None else ""))


def money(amount: float, currency: str = "EUR") -> str:
    """Formato italiano: 10,00 EUR."""
    return f"{float(amount):.2f}".replace(".", ",") + f" {currency}"


def is_valid_mc_username(text: str) -> bool:
    return bool(MC_USERNAME_RE.match(text.strip()))


def payment_kind(text: str) -> str | None:
    """Riconosce il tipo di riferimento di pagamento incollato dall'utente.

    Ritorna PAY_PAYPAL, PAY_GIFTCARD oppure None se il formato non e' credibile.
    """
    value = db.normalize_payment_id(text)
    if PAYPAL_TXN_RE.match(value):
        return PAY_PAYPAL
    if GIFTCARD_RE.match(value):
        return PAY_GIFTCARD
    return None


def user_label(user) -> str:
    """Etichetta leggibile di un utente Telegram, per log e notifiche admin."""
    if user is None:
        return "sconosciuto"
    if user.username:
        return f"@{user.username}"
    return user.full_name or str(user.id)


def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Vedi catalogo", callback_data=CB_CATALOG)],
        [InlineKeyboardButton("I miei ordini", callback_data=CB_MY_ORDERS)],
    ])


def back_home_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Torna al menu", callback_data=CB_HOME)],
    ])


def status_label(status: str) -> str:
    return db.STATUS_LABELS.get(status, status)


def type_label(product_type: str | None) -> str:
    return catalog.TYPE_LABELS.get(product_type or "", product_type or "?")


def format_order(order: dict[str, Any], for_admin: bool = False) -> str:
    """Riepilogo di un ordine. La versione admin aggiunge utente e pagamento."""
    lines = [
        f"<b>Ordine {esc(order['order_code'])}</b>",
        f"Prodotto: {esc(order['product_name'])}",
        f"Importo: {esc(money(order['price'], order['currency']))}",
        f"Stato: <b>{esc(status_label(order['status']))}</b>",
    ]
    if order.get("mc_username"):
        lines.append(f"Username Minecraft: <b>{esc(order['mc_username'])}</b>")
    if for_admin:
        uname = f"@{order['username']}" if order["username"] else "(nessun username)"
        lines.append(f"Tipo: {esc(type_label(order.get('product_type')))}")
        lines.append(f"Utente: {esc(uname)} (id <code>{order['user_id']}</code>)")
        if order.get("paypal_txn_id"):
            lines.append(f"Rif. pagamento: <code>{esc(order['paypal_txn_id'])}</code>")
        if order.get("license_key"):
            lines.append(f"Chiave: <code>{esc(order['license_key'])}</code>")
        if order.get("revoked_at"):
            lines.append(f"<b>LICENZA REVOCATA</b> il {esc(order['revoked_at'])}")
    lines.append(f"Creato: {esc(order['created_at'])}")
    if order.get("delivered_at"):
        lines.append(f"Consegnato: {esc(order['delivered_at'])}")
    if for_admin and order.get("admin_note"):
        lines.append(f"Nota: {esc(order['admin_note'])}")
    return "\n".join(lines)


async def answer_and_edit(query, text: str, keyboard: InlineKeyboardMarkup | None = None) -> None:
    """Risponde al callback e sostituisce il messaggio.

    Se il messaggio originale contiene una foto non si puo' usare edit_text:
    in quel caso si invia un messaggio nuovo.
    """
    await query.answer()
    if query.message and (query.message.photo or query.message.document):
        await query.message.reply_html(text, reply_markup=keyboard)
    else:
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard)

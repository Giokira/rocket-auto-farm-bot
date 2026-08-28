"""Pannello admin: verifica pagamenti, consegna, rimborsi e dispute.

L'accesso e' ristretto all'ADMIN_USER_ID letto dal .env. Il controllo e'
ripetuto su OGNI handler (comando e callback): un callback_data e' visibile
nel client Telegram e chiunque potrebbe provare a rispedirlo.
"""

import functools
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes

import catalog
import config
import database as db
from handlers.common import (
    CB_ADMIN_APPROVE,
    CB_ADMIN_DELIVERED,
    CB_ADMIN_DETAIL,
    CB_ADMIN_DISPUTE,
    CB_ADMIN_LIST,
    CB_ADMIN_REFUND,
    CB_ADMIN_REJECT,
    CB_ADMIN_REVOKE,
    esc,
    format_order,
    money,
    status_label,
)
from orderflow import confirm_and_deliver, mark_ingame_delivered
from utils.logger import log_event

logger = logging.getLogger(__name__)


def admin_only(func):
    """Blocca chiunque non sia l'admin e registra i tentativi di accesso."""

    @functools.wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user = update.effective_user
        if user is None or user.id != config.ADMIN_USER_ID:
            log_event("accesso_admin_negato", user=getattr(user, "id", "?"))
            if update.callback_query:
                await update.callback_query.answer("Non autorizzato.", show_alert=True)
            elif update.message:
                # Risposta neutra: non rivelare l'esistenza del pannello.
                await update.message.reply_text("Comando non riconosciuto.")
            return None
        return await func(update, context, *args, **kwargs)

    return wrapper


def _short(order_code: str) -> str:
    """Coda del codice, per far stare l'etichetta dentro un bottone."""
    return order_code.split("-")[-1]


async def _resolve_order(text: str) -> dict | None:
    """Trova un ordine dal codice scritto a mano.

    Accetta sia "ORD-XY7QK" sia la forma normalizzata "ordxy7qk": il codice si
    legge dalla causale su PayPal, dove il compratore puo' averlo ricopiato in
    qualsiasi grafia.
    """
    return (await db.get_order(text.strip().upper())) or (await db.get_order_by_ref(text))


# --------------------------------------------------------------------------
# Pannello
# --------------------------------------------------------------------------

async def _panel_content() -> tuple[str, InlineKeyboardMarkup]:
    verifying = await db.get_orders_by_status(db.STATUS_VERIFYING, limit=20)
    ingame = await db.get_orders_by_status(db.STATUS_TO_DELIVER_INGAME, limit=20)
    counters = await db.stats()

    lines = ["<b>Pannello admin</b>", ""]
    if counters:
        lines.append(
            "Riepilogo: "
            + ", ".join(f"{status_label(s)}: {n}" for s, n in sorted(counters.items()))
        )
        lines.append("")

    rows: list[list[InlineKeyboardButton]] = []

    if verifying:
        lines.append(f"<b>Pagamenti da verificare ({len(verifying)})</b>")
        for order in verifying:
            uname = f"@{order['username']}" if order["username"] else str(order["user_id"])
            lines.append(
                f"\n<code>{esc(order['order_code'])}</code> - {esc(order['product_name'])}"
                f" - {esc(money(order['price'], order['currency']))}\n"
                f"da {esc(uname)} | MC: <b>{esc(order.get('mc_username') or '?')}</b>\n"
                f"rif. <code>{esc(order.get('paypal_txn_id') or '-')}</code>"
            )
            rows.append([
                InlineKeyboardButton(f"OK {_short(order['order_code'])}",
                                     callback_data=f"{CB_ADMIN_APPROVE}{order['order_code']}"),
                InlineKeyboardButton(f"NO {_short(order['order_code'])}",
                                     callback_data=f"{CB_ADMIN_REJECT}{order['order_code']}"),
            ])
        lines.append("")

    if ingame:
        lines.append(f"<b>Da consegnare in gioco ({len(ingame)})</b>")
        for order in ingame:
            lines.append(
                f"\n<code>{esc(order['order_code'])}</code> - "
                f"{esc(order['product_name'])} a <b>{esc(order.get('mc_username') or '?')}</b>"
            )
            rows.append([
                InlineKeyboardButton(f"Consegnato {_short(order['order_code'])}",
                                     callback_data=f"{CB_ADMIN_DELIVERED}{order['order_code']}"),
            ])
        lines.append("")

    if not verifying and not ingame:
        lines.append("Niente in sospeso.")

    lines.append(
        "\nComandi: /cerca &lt;testo&gt; - /rimborso &lt;codice&gt; - /disputa &lt;codice&gt;"
    )
    rows.append([InlineKeyboardButton("Aggiorna", callback_data=CB_ADMIN_LIST)])
    return "\n".join(lines), InlineKeyboardMarkup(rows)


@admin_only
async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text, keyboard = await _panel_content()
    await update.message.reply_html(text, reply_markup=keyboard)


@admin_only
async def admin_list_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    text, keyboard = await _panel_content()
    await query.answer("Aggiornato")
    try:
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
    except TelegramError:
        # "Message is not modified": nulla e' cambiato dall'ultimo refresh.
        pass


def _order_actions_keyboard(order: dict) -> InlineKeyboardMarkup:
    """Azioni disponibili su un ordine, in base allo stato e al tipo."""
    code = order["order_code"]
    rows: list[list[InlineKeyboardButton]] = []

    if order["status"] == db.STATUS_VERIFYING:
        rows.append([
            InlineKeyboardButton("Conferma", callback_data=f"{CB_ADMIN_APPROVE}{code}"),
            InlineKeyboardButton("Rifiuta", callback_data=f"{CB_ADMIN_REJECT}{code}"),
        ])
    if order["status"] == db.STATUS_TO_DELIVER_INGAME:
        rows.append([
            InlineKeyboardButton("Consegnato", callback_data=f"{CB_ADMIN_DELIVERED}{code}"),
        ])

    rows.append([
        InlineKeyboardButton("Rimborsato", callback_data=f"{CB_ADMIN_REFUND}{code}"),
        InlineKeyboardButton("Contestato", callback_data=f"{CB_ADMIN_DISPUTE}{code}"),
    ])
    if (order.get("product_type") == catalog.TYPE_LICENSE) and not order.get("revoked_at"):
        rows.append([
            InlineKeyboardButton("Segna licenza revocata",
                                 callback_data=f"{CB_ADMIN_REVOKE}{code}"),
        ])
    return InlineKeyboardMarkup(rows)


@admin_only
async def admin_detail_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    order_code = query.data[len(CB_ADMIN_DETAIL):]
    order = await db.get_order(order_code)
    if order is None:
        await query.answer("Ordine inesistente.", show_alert=True)
        return
    await query.answer()
    await query.message.reply_html(
        format_order(order, for_admin=True),
        reply_markup=_order_actions_keyboard(order),
    )


@admin_only
async def search_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/cerca <codice | causale | username MC | id pagamento>."""
    if not context.args:
        await update.message.reply_html(
            "Uso: <code>/cerca ORDXY7QK</code> oppure username o ID pagamento."
        )
        return

    query_text = " ".join(context.args)
    results = await db.search_orders(query_text, limit=5)
    if not results:
        await update.message.reply_html(f"Nessun ordine trovato per {esc(query_text)}.")
        return

    for order in results:
        await update.message.reply_html(
            format_order(order, for_admin=True),
            reply_markup=_order_actions_keyboard(order),
        )


# --------------------------------------------------------------------------
# Conferma / rifiuto / consegna
# --------------------------------------------------------------------------

@admin_only
async def approve_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Conferma il pagamento e avvia la consegna adatta al tipo di prodotto."""
    query = update.callback_query
    order_code = query.data[len(CB_ADMIN_APPROVE):]
    order = await db.get_order(order_code)

    if order is None:
        await query.answer("Ordine inesistente.", show_alert=True)
        return

    if order["status"] == db.STATUS_COMPLETED:
        await query.answer("Ordine gia' completato.", show_alert=True)
        return

    if order["status"] == db.STATUS_TO_DELIVER_INGAME:
        await query.answer("Gia' confermato: manca la consegna in gioco.", show_alert=True)
        return

    if order["status"] != db.STATUS_VERIFYING:
        await query.answer(
            f"Stato non confermabile: {status_label(order['status'])}", show_alert=True
        )
        return

    await query.answer("Elaborazione in corso...")

    ok, message = await confirm_and_deliver(
        context.bot, order, actor_id=update.effective_user.id
    )

    # Il messaggio di esito rimanda a un'azione ("premi Consegnato"): la tastiera
    # va ricostruita sullo stato NUOVO, altrimenti il bottone non compare e
    # l'unico modo per chiudere l'ordine sarebbe riaprire /admin.
    updated = await db.get_order(order_code)
    await query.message.reply_html(
        (f"Ordine <b>{esc(order_code)}</b>: {message}") if ok
        else (f"Ordine <b>{esc(order_code)}</b>\n{esc(message)}"),
        reply_markup=_order_actions_keyboard(updated) if updated else None,
    )


@admin_only
async def delivered_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Chiude un ordine di valuta dopo il pagamento in gioco."""
    query = update.callback_query
    order_code = query.data[len(CB_ADMIN_DELIVERED):]
    order = await db.get_order(order_code)

    if order is None:
        await query.answer("Ordine inesistente.", show_alert=True)
        return
    if order["status"] == db.STATUS_COMPLETED:
        await query.answer("Ordine gia' completato.", show_alert=True)
        return
    if order["status"] != db.STATUS_TO_DELIVER_INGAME:
        await query.answer(
            f"Stato inatteso: {status_label(order['status'])}", show_alert=True
        )
        return

    await query.answer("Chiusura ordine...")
    ok, message = await mark_ingame_delivered(
        context.bot, order, actor_id=update.effective_user.id
    )
    await query.message.reply_html(esc(message) if ok else f"Non riuscito: {esc(message)}")


@admin_only
async def reject_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Rifiuta l'ordine. Il riferimento resta legato: non e' riutilizzabile."""
    query = update.callback_query
    order_code = query.data[len(CB_ADMIN_REJECT):]
    order = await db.get_order(order_code)

    if order is None:
        await query.answer("Ordine inesistente.", show_alert=True)
        return

    if order["status"] not in (db.STATUS_VERIFYING, db.STATUS_WAITING_PAYMENT):
        await query.answer(
            f"Stato non rifiutabile: {status_label(order['status'])}", show_alert=True
        )
        return

    await db.set_status(order_code, db.STATUS_REJECTED, admin_note="Rifiutato manualmente")
    log_event("ordine_rifiutato", order=order_code, user=order["user_id"],
              rif=order.get("paypal_txn_id"), admin=update.effective_user.id)

    await query.answer("Ordine rifiutato")
    try:
        await context.bot.send_message(
            chat_id=order["user_id"],
            text=(
                f"L'ordine <b>{esc(order_code)}</b> e' stato rifiutato: non ho trovato "
                "un pagamento corrispondente.\n\n"
                "Se ritieni ci sia un errore rispondi qui indicando il codice ordine."
            ),
            parse_mode=ParseMode.HTML,
        )
    except TelegramError:
        logger.warning("Impossibile avvisare l'utente del rifiuto di %s", order_code)

    await query.message.reply_html(f"Ordine <b>{esc(order_code)}</b> rifiutato.")


# --------------------------------------------------------------------------
# Rimborsi, dispute, revoche
# --------------------------------------------------------------------------

async def _set_problem_status(update: Update, order_code: str, status: str,
                              note: str) -> tuple[str, dict | None]:
    """Segna rimborso o disputa. Ritorna (messaggio, ordine aggiornato)."""
    order = await _resolve_order(order_code)
    if order is None:
        return f"Ordine {order_code} inesistente.", None

    order_code = order["order_code"]
    await db.set_status(order_code, status, admin_note=note)
    log_event("ordine_" + status, order=order_code, user=order["user_id"],
              rif=order.get("paypal_txn_id"), importo=order["price"],
              admin=update.effective_user.id)

    extra = ""
    if order.get("product_type") == catalog.TYPE_LICENSE and order.get("license_key"):
        extra = (
            "\nLa chiave era gia' stata consegnata: puoi segnarla revocata, ma "
            "una chiave gia' in mano al cliente continua a funzionare offline."
        )
    updated = await db.get_order(order_code)
    return f"Ordine {order_code} segnato come {status_label(status)}.{extra}", updated


async def _problem_command(update: Update, context: ContextTypes.DEFAULT_TYPE,
                           status: str, note: str, usage: str) -> None:
    if not context.args:
        await update.message.reply_html(f"Uso: <code>{usage}</code>")
        return
    message, order = await _set_problem_status(update, context.args[0], status, note)
    await update.message.reply_html(
        esc(message),
        reply_markup=_order_actions_keyboard(order) if order else None,
    )


@admin_only
async def refund_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/rimborso <codice ordine>."""
    await _problem_command(update, context, db.STATUS_REFUNDED,
                           "Rimborsato dall'admin", "/rimborso ORD-XY7QK")


@admin_only
async def dispute_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/disputa <codice ordine>."""
    await _problem_command(update, context, db.STATUS_DISPUTED,
                           "Disputa/chargeback aperto", "/disputa ORD-XY7QK")


@admin_only
async def refund_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    code = query.data[len(CB_ADMIN_REFUND):]
    await query.answer()
    message, _ = await _set_problem_status(update, code, db.STATUS_REFUNDED,
                                           "Rimborsato dall'admin")
    await query.message.reply_html(esc(message))


@admin_only
async def dispute_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    code = query.data[len(CB_ADMIN_DISPUTE):]
    await query.answer()
    message, _ = await _set_problem_status(update, code, db.STATUS_DISPUTED,
                                           "Disputa/chargeback aperto")
    await query.message.reply_html(esc(message))


@admin_only
async def revoke_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Annota la revoca di una licenza.

    E' solo una annotazione: la chiave e' una firma offline e continua a
    funzionare. Serve a tenere traccia di chi e' stato revocato, e a ricordarsi
    di invalidarla nella prossima versione della mod.
    """
    query = update.callback_query
    code = query.data[len(CB_ADMIN_REVOKE):]
    order = await db.get_order(code)

    if order is None:
        await query.answer("Ordine inesistente.", show_alert=True)
        return
    if order.get("product_type") != catalog.TYPE_LICENSE:
        await query.answer("Non e' un ordine di licenza.", show_alert=True)
        return

    await db.mark_revoked(code, note="Licenza revocata dall'admin")
    log_event("licenza_revocata", order=code, user=order["user_id"],
              mc_user=order.get("mc_username"), admin=update.effective_user.id)

    await query.answer("Revoca annotata")
    await query.message.reply_html(
        f"Licenza dell'ordine <b>{esc(code)}</b> segnata come revocata "
        f"(username <b>{esc(order.get('mc_username') or '?')}</b>).\n"
        "Ricorda: la chiave gia' consegnata continua a funzionare offline. "
        "Per bloccarla davvero serve una blocklist nella prossima build della mod."
    )


def register(app) -> None:
    app.add_handler(CommandHandler("admin", admin_command))
    app.add_handler(CommandHandler("cerca", search_command))
    app.add_handler(CommandHandler("rimborso", refund_command))
    app.add_handler(CommandHandler("disputa", dispute_command))
    app.add_handler(CallbackQueryHandler(admin_list_callback, pattern=f"^{CB_ADMIN_LIST}$"))
    app.add_handler(CallbackQueryHandler(admin_detail_callback, pattern=f"^{CB_ADMIN_DETAIL}"))
    app.add_handler(CallbackQueryHandler(approve_callback, pattern=f"^{CB_ADMIN_APPROVE}"))
    app.add_handler(CallbackQueryHandler(reject_callback, pattern=f"^{CB_ADMIN_REJECT}"))
    app.add_handler(CallbackQueryHandler(delivered_callback, pattern=f"^{CB_ADMIN_DELIVERED}"))
    app.add_handler(CallbackQueryHandler(refund_callback, pattern=f"^{CB_ADMIN_REFUND}"))
    app.add_handler(CallbackQueryHandler(dispute_callback, pattern=f"^{CB_ADMIN_DISPUTE}"))
    app.add_handler(CallbackQueryHandler(revoke_callback, pattern=f"^{CB_ADMIN_REVOKE}"))

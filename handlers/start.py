"""Handler /start, /help e ritorno al menu principale."""

import logging

from telegram import Update
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes

import config
from handlers.common import CB_HOME, esc, main_menu_keyboard, user_label
from utils.logger import log_event

logger = logging.getLogger(__name__)

WELCOME = (
    "Ciao {name}!\n\n"
    "Da qui puoi acquistare licenza e valuta in gioco.\n\n"
    "<b>Come funziona</b>\n"
    "1. Apri il catalogo e scegli\n"
    "2. Premi <b>Compra</b> e indica il tuo <b>username Minecraft</b>\n"
    "3. Compri un buono regalo Amazon dell'importo esatto e mi mandi il codice\n"
    "4. Verifico a mano il pagamento\n"
    "5. Quando vuoi ricevere mod e licenza, la richiedi con un bottone: te la mando dopo l'ok\n\n"
    "Comandi: /catalogo /ordine /help"
)


def _help_text() -> str:
    pay = (
        "Pagamento con <b>buono regalo Amazon</b> a codice.\n"
        "Compri il buono dell'importo esatto e mi mandi <b>solo il codice</b>."
    )

    return (
        "<b>Comandi disponibili</b>\n"
        "/start - menu principale\n"
        "/catalogo - vedi cosa e' in vendita\n"
        "/ordine - stato dei tuoi ordini\n"
        "/help - questo messaggio\n\n"
        "<b>Username Minecraft</b>\n"
        "Viene chiesto prima del pagamento. La chiave di licenza funziona solo "
        "con quell'username, e la valuta in gioco viene consegnata a quel nome: "
        "controlla di scriverlo giusto.\n\n"
        f"<b>Pagamento</b>\n{pay}\n\n"
        "<b>Dopo il pagamento</b>\n"
        "Verifico il codice a mano. Per mod e licenza, l'ordine passa in stato "
        "\"pagato\": quando vuoi riceverli premi il bottone <b>Richiedi mod e licenza</b> "
        "e appena confermo te li mando qui in chat. Per la valuta in gioco la ricevi "
        "in automatico appena confermo.\n\n"
        "Problemi con un ordine? Scrivimi il codice ordine."
    )


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    log_event("start", user=user.id, username=user_label(user))
    await update.message.reply_html(
        WELCOME.format(name=esc(user.first_name or "")),
        reply_markup=main_menu_keyboard(),
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_html(_help_text(), reply_markup=main_menu_keyboard())


async def home_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Bottone "Torna al menu"."""
    query = update.callback_query
    await query.answer()
    text = WELCOME.format(name=esc(update.effective_user.first_name or ""))
    if query.message and query.message.photo:
        await query.message.reply_html(text, reply_markup=main_menu_keyboard())
    else:
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=main_menu_keyboard())


def register(app) -> None:
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CallbackQueryHandler(home_callback, pattern=f"^{CB_HOME}$"))

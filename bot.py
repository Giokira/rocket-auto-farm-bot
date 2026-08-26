"""Entry point del bot.

Avvio:  python bot.py

Compiti di questo file, e nient'altro:
  1. configurare i log
  2. validare la configurazione (.env)
  3. creare le tabelle del database
  4. registrare gli handler
  5. avviare il polling
"""

import asyncio
import html
import logging
import traceback

from telegram import BotCommand, Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import Application, ContextTypes

import config
import database as db
from handlers import admin, catalogo, ordine, start
from utils.logger import log_event, setup_logging

logger = logging.getLogger(__name__)

PUBLIC_COMMANDS = [
    BotCommand("start", "Menu principale"),
    BotCommand("catalogo", "Vedi cosa e' in vendita"),
    BotCommand("ordine", "Stato dei tuoi ordini"),
    BotCommand("help", "Come funziona"),
]

# Avvisi di configurazione non bloccanti, inoltrati all'admin all'avvio.
STARTUP_WARNINGS: list[str] = []


async def on_startup(app: Application) -> None:
    """Eseguito una volta prima del polling."""
    await db.init_db()
    await app.bot.set_my_commands(PUBLIC_COMMANDS)

    me = await app.bot.get_me()
    log_event("bot_avviato", bot=me.username, admin=config.ADMIN_USER_ID)
    logger.info("Bot @%s avviato. Database: %s", me.username, config.DB_PATH)

    mode = ("Amici e Famiglia" if config.PAYPAL_MODE == config.MODE_FRIENDS
            else "Beni e servizi")
    verify = "attiva" if config.paypal_auto_verify_enabled() else "manuale"
    lines = [
        "Bot avviato e pronto. Usa /admin per il pannello.",
        f"PayPal: {mode} | verifica: {verify} | destinatario: {config.paypal_destination()}",
    ]
    lines += [f"Avviso: {w}" for w in STARTUP_WARNINGS]

    try:
        await app.bot.send_message(
            chat_id=config.ADMIN_USER_ID,
            text="\n".join(lines),
        )
    except TelegramError:
        # Tipico al primo avvio: l'admin non ha ancora fatto /start al bot.
        logger.warning(
            "Impossibile scrivere all'admin (%s). Aprigli una chat con /start.",
            config.ADMIN_USER_ID,
        )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Cattura ogni eccezione non gestita: il bot non deve mai morire in silenzio."""
    logger.error("Eccezione durante un update", exc_info=context.error)

    tb = "".join(traceback.format_exception(
        type(context.error), context.error, context.error.__traceback__
    ))[-1500:]

    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "Si e' verificato un errore imprevisto. Riprova tra poco; "
                "se l'ordine era in corso non e' andato perso."
            )
        except TelegramError:
            pass

    try:
        await context.bot.send_message(
            chat_id=config.ADMIN_USER_ID,
            text=f"<b>Errore nel bot</b>\n<pre>{html.escape(tb)}</pre>",
            parse_mode=ParseMode.HTML,
        )
    except TelegramError:
        pass


def build_application() -> Application:
    app = Application.builder().token(config.BOT_TOKEN).post_init(on_startup).build()

    # L'ordine conta: gli handler di ordine.py includono un MessageHandler che
    # cattura tutto il testo libero, quindi va registrato per ultimo.
    start.register(app)
    catalogo.register(app)
    admin.register(app)
    ordine.register(app)

    app.add_error_handler(error_handler)
    return app


def main() -> None:
    setup_logging()
    try:
        warnings = config.validate()
    except RuntimeError as exc:
        logger.error("%s", exc)
        raise SystemExit(1)

    for warning in warnings:
        logger.warning("%s", warning)
    STARTUP_WARNINGS.extend(warnings)

    app = build_application()
    logger.info("Avvio polling...")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, asyncio.CancelledError):
        logging.getLogger(__name__).info("Bot fermato manualmente.")

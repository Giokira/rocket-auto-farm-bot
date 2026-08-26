"""Handler del catalogo: elenco prodotti raggruppati per tipo e scheda prodotto."""

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TelegramError
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes

import catalog
from handlers.common import (
    CB_BUY,
    CB_CATALOG,
    CB_HOME,
    CB_PRODUCT,
    esc,
)

logger = logging.getLogger(__name__)


def _catalog_keyboard() -> InlineKeyboardMarkup:
    """Un bottone per prodotto, licenze prima e valuta dopo."""
    rows = []
    for ptype in (catalog.TYPE_LICENSE, catalog.TYPE_INGAME):
        for p in catalog.products_by_type(ptype):
            rows.append([InlineKeyboardButton(
                f"{p.name} - {p.price_label}", callback_data=f"{CB_PRODUCT}{p.id}"
            )])
    rows.append([InlineKeyboardButton("Torna al menu", callback_data=CB_HOME)])
    return InlineKeyboardMarkup(rows)


def _product_keyboard(product_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Compra", callback_data=f"{CB_BUY}{product_id}")],
        [InlineKeyboardButton("Torna al catalogo", callback_data=CB_CATALOG)],
    ])


def _catalog_text() -> str:
    lines = ["<b>Catalogo</b>", ""]
    for ptype in (catalog.TYPE_LICENSE, catalog.TYPE_INGAME):
        products = catalog.products_by_type(ptype)
        if not products:
            continue
        lines.append(f"<b>{esc(catalog.TYPE_LABELS[ptype])}</b>")
        for p in products:
            lines.append(f"- {esc(p.name)}: <b>{esc(p.price_label)}</b>")
        lines.append("")
    lines.append("Scegli una voce per i dettagli.")
    return "\n".join(lines)


def _product_text(product: catalog.Product) -> str:
    head = f"<b>{esc(product.name)}</b>"
    if product.needs_file:
        head += f" v{esc(product.version)}\nPer Minecraft {esc(product.mc_version)}"

    if product.product_type == catalog.TYPE_LICENSE:
        consegna = (
            "Consegna: file + chiave di licenza inviati in chat dopo la verifica "
            "del pagamento. La chiave e' legata al tuo username Minecraft."
        )
    else:
        consegna = (
            "Consegna: valuta accreditata in gioco sul tuo username, a mano, "
            "dopo la verifica del pagamento."
        )

    return (
        f"{head}\n\n"
        f"{esc(product.description)}\n\n"
        f"Prezzo: <b>{esc(product.price_label)}</b>\n"
        f"{consegna}"
    )


async def _send_catalog(update: Update) -> None:
    products = catalog.all_products()
    text = _catalog_text() if products else "Il catalogo e' momentaneamente vuoto."

    if update.callback_query:
        query = update.callback_query
        await query.answer()
        if query.message and query.message.photo:
            await query.message.reply_html(text, reply_markup=_catalog_keyboard())
        else:
            await query.edit_message_text(
                text, parse_mode="HTML", reply_markup=_catalog_keyboard()
            )
    else:
        await update.message.reply_html(text, reply_markup=_catalog_keyboard())


async def catalogo_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _send_catalog(update)


async def catalog_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _send_catalog(update)


async def product_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Scheda prodotto: screenshot (se presente) + descrizione + bottone Compra."""
    query = update.callback_query
    product_id = query.data[len(CB_PRODUCT):]
    product = catalog.get_product(product_id)

    if product is None:
        await query.answer("Prodotto non disponibile", show_alert=True)
        return

    await query.answer()
    text = _product_text(product)
    keyboard = _product_keyboard(product.id)
    shot = product.screenshot_path

    if shot and shot.is_file():
        try:
            with shot.open("rb") as fh:
                await context.bot.send_photo(
                    chat_id=query.message.chat_id,
                    photo=fh,
                    caption=text,
                    parse_mode="HTML",
                    reply_markup=keyboard,
                )
            return
        except TelegramError:
            # Screenshot rotto o troppo grande: non deve impedire la vendita.
            logger.warning("Invio screenshot fallito per %s", product.id, exc_info=True)
    elif shot:
        logger.warning("Screenshot mancante sul disco: %s", shot)

    if query.message and query.message.photo:
        await query.message.reply_html(text, reply_markup=keyboard)
    else:
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard)


def register(app) -> None:
    app.add_handler(CommandHandler("catalogo", catalogo_command))
    app.add_handler(CallbackQueryHandler(catalog_callback, pattern=f"^{CB_CATALOG}$"))
    app.add_handler(CallbackQueryHandler(product_callback, pattern=f"^{CB_PRODUCT}"))

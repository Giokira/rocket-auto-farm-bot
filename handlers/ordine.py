"""Handler degli ordini: username Minecraft, creazione, pagamento, elenco.

Percorso completo:
  bottone "Compra"      -> chiede lo username Minecraft
  username valido       -> create_order()  -> stato "in attesa di pagamento"
  riferimento pagamento -> attach_payment_id() -> "in verifica" + notifica admin
  (conferma e consegna avvengono in handlers/admin.py, via orderflow.py)

Lo stato "sto aspettando lo username" vive in context.user_data, quindi in
memoria: se il bot riparte l'utente ripreme Compra. Nessun ordine viene perso,
perche' l'ordine nasce solo dopo che lo username e' stato accettato.
"""

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import catalog
import config
import database as db
from handlers.common import (
    CB_ADMIN_APPROVE,
    CB_ADMIN_DELIVER_APPROVE,
    CB_ADMIN_DELIVER_REJECT,
    CB_ADMIN_REJECT,
    CB_BUY,
    CB_CANCEL_ORDER,
    CB_CATALOG,
    CB_MY_ORDERS,
    CB_REQUEST_DELIVERY,
    GIFTCARD_RE,
    PAY_GIFTCARD,
    esc,
    format_order,
    is_valid_mc_username,
    main_menu_keyboard,
    money,
    request_delivery_keyboard,
    user_label,
    user_link,
)
from orderflow import (
    approve_delivery_request,
    confirm_and_deliver,
    reject_delivery_request,
    request_delivery,
)
from utils.logger import log_event
from utils.ratelimit import RateLimiter

logger = logging.getLogger(__name__)

# Un solo limiter condiviso da /ordine e dal bottone Compra: e' il volume di
# richieste dell'utente a contare, non il canale da cui arrivano.
order_limiter = RateLimiter(
    max_calls=config.ORDER_RATE_LIMIT_MAX,
    per_seconds=config.ORDER_RATE_LIMIT_SECONDS,
)

# Massimo di ordini aperti contemporaneamente per utente (anti-flood sul DB).
MAX_ACTIVE_ORDERS = 3

# Chiave in context.user_data con il prodotto in attesa di username.
PENDING_KEY = "pending_product"


# --------------------------------------------------------------------------
# Testi
# --------------------------------------------------------------------------

def _ask_username_text(product: catalog.Product) -> str:
    return (
        f"<b>{esc(product.name)}</b> - {esc(product.price_label)}\n\n"
        "Prima di procedere mi serve il tuo <b>username Minecraft</b>.\n"
        "Scrivilo qui sotto: 3-16 caratteri, solo lettere, numeri e underscore.\n\n"
        + (
            "La chiave di licenza viene generata su quel nome e non funziona con altri."
            if product.needs_license_key
            else "La valuta viene consegnata in gioco a quel nome."
        )
    )


def _username_confirm_text(order: dict, product: catalog.Product) -> str:
    if product.needs_license_key:
        warning = (
            "La chiave di licenza sara' valida <b>solo</b> per questo username. "
            "Un nome sbagliato significa una chiave inutilizzabile, e non e' rimborsabile."
        )
    else:
        warning = (
            "La valuta verra' consegnata in gioco <b>a questo username</b>. "
            "Se il nome e' sbagliato i soldi finiscono a un altro giocatore, "
            "e non sono recuperabili."
        )
    return (
        f"Username registrato: <b>{esc(order['mc_username'])}</b>\n"
        f"{warning}\n\n"
        "Se e' sbagliato annulla l'ordine e rifallo."
    )


def _giftcard_text(order: dict) -> str:
    """Istruzioni per il pagamento con buono a codice (Amazon).

    Nessun nome mostrato: il compratore manda un codice, l'admin lo riscatta.
    """
    product = catalog.get_product(order["product_id"])
    negotiable = bool(product and product.price_negotiable)

    if negotiable:
        amount_line = (
            "Importo: <b>da concordare in chat</b> prima di comprare il buono.\n"
        )
        extra = (
            "Scrivimi qui in chat per concordare l'importo, poi compra un "
            "<b>buono regalo Amazon</b> (amazon.it) da quella cifra esatta."
        )
    else:
        amount = esc(money(order["price"], order["currency"]))
        amount_line = f"Importo esatto: <b>{amount}</b>\n"
        extra = config.GIFTCARD_INSTRUCTIONS or (
            "Compra un <b>buono regalo Amazon</b> (amazon.it) da esattamente "
            f"<b>{amount}</b>."
        )

    return (
        f"<b>Ordine {esc(order['order_code'])}</b>\n"
        f"{amount_line}\n"
        "<b>Come pagare</b>\n"
        f"{extra}\n"
        "Poi <b>incolla qui il codice</b> del buono "
        "(formato tipo <code>XXXX-XXXXXXX-XXXX</code>).\n\n"
        "<b>Attenzione</b>\n"
        "- Manda <b>solo il codice</b>, non lo screenshot dell'ordine Amazon.\n"
        "- Ogni codice vale per <b>un solo</b> ordine.\n"
        "- Verifico riscattando il codice: se e' gia' usato, errato o di importo "
        "diverso, l'ordine non parte.\n\n"
        "<b>Dopo</b>\n"
        "Ricevi tutto qui in chat appena confermo."
    )


def _payment_text(order: dict) -> str:
    """Istruzioni di pagamento: solo buono regalo Amazon a codice."""
    return _giftcard_text(order)


def _pending_order_keyboard(order_code: str) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("Annulla ordine",
                              callback_data=f"{CB_CANCEL_ORDER}{order_code}")],
        [InlineKeyboardButton("Torna al catalogo", callback_data=CB_CATALOG)],
    ]
    return InlineKeyboardMarkup(rows)


async def _check_rate_limit(user_id: int, notify) -> bool:
    """True se l'utente puo' procedere; altrimenti avvisa e ritorna False."""
    allowed, retry_after = order_limiter.hit(user_id)
    if not allowed:
        log_event("rate_limit", user=user_id, retry_after=retry_after)
        await notify(f"Troppe richieste. Riprova tra {retry_after} secondi.")
    return allowed


# --------------------------------------------------------------------------
# Compra -> username -> ordine
# --------------------------------------------------------------------------

async def buy_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Primo passo: niente ordine finche' non conosco lo username Minecraft."""
    query = update.callback_query
    user = update.effective_user
    product_id = query.data[len(CB_BUY):]

    async def alert(text: str) -> None:
        await query.answer(text, show_alert=True)

    if not await _check_rate_limit(user.id, alert):
        return

    product = catalog.get_product(product_id)
    if product is None:
        await alert("Prodotto non disponibile.")
        return

    # Se manca il file il bot non potrebbe consegnare: meglio bloccare subito
    # l'ordine che incassare e lasciare il cliente in attesa.
    available, why = product.is_available()
    if not available:
        logger.error("Prodotto non vendibile (%s): %s", product.id, why)
        await alert("Prodotto temporaneamente non disponibile. Riprova piu' tardi.")
        await _notify_admin_text(
            context,
            f"ATTENZIONE: vendite bloccate per <b>{esc(product.id)}</b>\n"
            f"Motivo: {esc(why)}",
        )
        return

    # Un solo ordine in attesa di pagamento per volta: il riferimento che il
    # compratore incolla si abbina sempre all'unico ordine aperto, senza
    # ambiguita' se ne avesse due in sospeso.
    waiting = await db.get_open_order_for_user(user.id)
    if waiting is not None:
        await alert(
            f"Hai gia' un ordine in attesa di pagamento ({waiting['order_code']}). "
            "Pagalo o annullalo prima di aprirne un altro."
        )
        return

    if await db.count_active_orders(user.id) >= MAX_ACTIVE_ORDERS:
        await alert("Hai gia' troppi ordini aperti. Completali o annullali prima.")
        return

    context.user_data[PENDING_KEY] = product.id
    await query.answer()
    await context.bot.send_message(
        chat_id=query.message.chat_id,
        text=_ask_username_text(product),
        parse_mode=ParseMode.HTML,
    )


async def _handle_username(update: Update, context: ContextTypes.DEFAULT_TYPE,
                           text: str) -> None:
    """Riceve lo username Minecraft e crea l'ordine."""
    user = update.effective_user
    product_id = context.user_data.get(PENDING_KEY)
    product = catalog.get_product(product_id)

    if product is None:
        context.user_data.pop(PENDING_KEY, None)
        await update.message.reply_html(
            "Il prodotto non e' piu' disponibile. Riapri il catalogo.",
            reply_markup=main_menu_keyboard(),
        )
        return

    if not is_valid_mc_username(text):
        await update.message.reply_html(
            "Username non valido.\n"
            "Deve avere da <b>3 a 16 caratteri</b> e contenere solo lettere, "
            "numeri e underscore (_). Niente spazi ne' simboli.\n"
            "Riscrivilo qui sotto."
        )
        return

    # Ricontrollo qui, non solo in buy_callback: fra il bottone e questo messaggio
    # l'utente potrebbe aver aperto un ordine per altra via. Un solo ordine in
    # attesa di pagamento alla volta.
    waiting = await db.get_open_order_for_user(user.id)
    if waiting is not None:
        context.user_data.pop(PENDING_KEY, None)
        await update.message.reply_html(
            f"Hai gia' un ordine in attesa di pagamento (<b>{esc(waiting['order_code'])}</b>). "
            "Pagalo o annullalo prima di aprirne un altro.",
            reply_markup=main_menu_keyboard(),
        )
        return

    mc_username = text.strip()
    order = await db.create_order(
        user_id=user.id,
        username=user.username,
        mc_username=mc_username,
        product_id=product.id,
        product_name=product.name,
        product_type=product.product_type,
        price=product.price,
        currency=product.currency,
    )
    context.user_data.pop(PENDING_KEY, None)

    log_event("ordine_creato", order=order["order_code"], user=user.id,
              username=user_label(user), product=product.id,
              tipo=product.product_type, mc_user=mc_username, price=product.price)

    await update.message.reply_html(_username_confirm_text(order, product))
    await context.bot.send_message(
        chat_id=update.message.chat_id,
        text=_payment_text(order),
        parse_mode=ParseMode.HTML,
        reply_markup=_pending_order_keyboard(order["order_code"]),
    )


async def cancel_order_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    order_code = query.data[len(CB_CANCEL_ORDER):]
    order = await db.get_order(order_code)

    # Controllo di proprieta': il callback_data e' visibile al client, quindi
    # un utente potrebbe rispedirlo con il codice di un ordine altrui.
    if order is None or order["user_id"] != update.effective_user.id:
        await query.answer("Ordine non trovato.", show_alert=True)
        return

    if order["status"] != db.STATUS_WAITING_PAYMENT:
        await query.answer(
            "Questo ordine non e' piu' annullabile: e' gia' in lavorazione.",
            show_alert=True,
        )
        return

    await db.set_status(order_code, db.STATUS_CANCELLED)
    log_event("ordine_annullato", order=order_code, user=update.effective_user.id)
    await query.answer("Ordine annullato.")
    await query.edit_message_text(
        f"Ordine <b>{esc(order_code)}</b> annullato.",
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu_keyboard(),
    )


# --------------------------------------------------------------------------
# Riferimento di pagamento
# --------------------------------------------------------------------------

async def _handle_payment_reference(update: Update, context: ContextTypes.DEFAULT_TYPE,
                                    order: dict, raw: str, kind: str) -> None:
    """Valida, registra e notifica un riferimento di pagamento."""
    user = update.effective_user
    value = db.normalize_payment_id(raw)

    # Il codice ordine assomiglia a un codice gift card: senza questo controllo
    # un utente che reincolla il proprio codice invece dell'ID transazione
    # manderebbe l'ordine in verifica con un riferimento inutile.
    if await db.get_order_by_ref(raw):
        await update.message.reply_html(
            "Quello e' un <b>codice ordine</b>, non un codice di gift card.\n"
            "Qui serve il <b>codice del buono regalo Amazon</b>, oppure uno "
            "screenshot della ricevuta."
        )
        return

    # Controllo esplicito: consente un messaggio d'errore chiaro. Il vincolo
    # UNIQUE sul DB resta comunque l'ultima barriera in caso di doppio invio.
    existing = await db.payment_id_exists(value)
    if existing:
        log_event("pagamento_duplicato", order=order["order_code"], user=user.id,
                  rif=value, gia_su=existing["order_code"])
        await update.message.reply_html(
            "Questo riferimento di pagamento risulta gia' registrato su un altro "
            "ordine e non puo' essere riutilizzato.\n"
            "Se pensi si tratti di un errore scrivimi indicando il codice "
            f"<b>{esc(order['order_code'])}</b>."
        )
        return

    ok, reason = await db.attach_payment_id(order["order_code"], value)
    if not ok:
        if reason == "pagamento_gia_usato":
            await update.message.reply_html(
                "Questo riferimento risulta gia' registrato su un altro ordine."
            )
        else:
            await update.message.reply_html(
                "L'ordine non e' piu' in attesa di pagamento. Usa /ordine per lo stato."
            )
        return

    log_event("pagamento_ricevuto", order=order["order_code"], user=user.id,
              username=user_label(user), metodo=kind, rif=value,
              importo=order["price"])

    await update.message.reply_html(
        f"Riferimento ricevuto. L'ordine <b>{esc(order['order_code'])}</b> e' ora "
        "<b>in verifica</b>.\n\n"
        "Controllo il pagamento appena possibile: quando confermo ricevi tutto "
        "qui in chat. Non serve fare altro.",
        reply_markup=main_menu_keyboard(),
    )

    updated = await db.get_order(order["order_code"])
    await _verify_and_notify(context, updated, kind)


async def _verify_and_notify(context: ContextTypes.DEFAULT_TYPE, order: dict,
                             kind: str) -> None:
    """Notifica all'admin il nuovo pagamento da verificare a mano."""
    await _notify_admin_new_payment(context, order, None)


async def text_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Unico punto d'ingresso per il testo libero.

    Priorita': 1) username atteso, 2) riferimento di pagamento per un ordine
    aperto, 3) messaggio di aiuto. Senza un ordine aperto non si tocca il DB.
    """
    if update.message is None:
        return  # post di canale/gruppo senza message: niente da fare qui

    user = update.effective_user
    text = (update.message.text or "").strip()

    if context.user_data.get(PENDING_KEY):
        await _handle_username(update, context, text)
        return

    order = await db.get_open_order_for_user(user.id)
    if order is None:
        await update.message.reply_html(
            "Non ho ordini in attesa di pagamento per te.\n"
            "Apri il catalogo per acquistare, oppure usa /ordine per lo stato.",
            reply_markup=main_menu_keyboard(),
        )
        return

    # Unico metodo: ogni testo libero e' il codice del buono regalo Amazon.
    if not GIFTCARD_RE.match(db.normalize_payment_id(text)):
        await update.message.reply_html(
            "Non riconosco un codice valido.\n"
            "Incolla qui il <b>codice</b> del buono regalo Amazon "
            "(tipo <code>XXXX-XXXXXXX-XXXX</code>), solo il codice."
        )
        return
    await _handle_payment_reference(update, context, order, text, PAY_GIFTCARD)


async def photo_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Screenshot della ricevuta come prova di pagamento.

    L'unicita' si basa sul file_unique_id della foto: impedisce di rimandare
    lo stesso identico file su un secondo ordine. La verifica vera resta
    quella dell'admin.
    """
    if update.message is None:
        return  # post di canale/gruppo senza message: niente da fare qui

    user = update.effective_user
    order = await db.get_open_order_for_user(user.id)
    if order is None:
        await update.message.reply_html(
            "Non ho ordini in attesa di pagamento per te.",
            reply_markup=main_menu_keyboard(),
        )
        return

    photo = update.message.photo[-1]
    reference = f"IMG{photo.file_unique_id}".upper()

    existing = await db.payment_id_exists(reference)
    if existing:
        await update.message.reply_html(
            "Questo screenshot risulta gia' usato su un altro ordine."
        )
        return

    ok, _ = await db.attach_payment_id(order["order_code"], reference)
    if not ok:
        await update.message.reply_html(
            "L'ordine non e' piu' in attesa di pagamento. Usa /ordine per lo stato."
        )
        return

    log_event("pagamento_ricevuto", order=order["order_code"], user=user.id,
              metodo="screenshot", rif=reference, importo=order["price"])

    await update.message.reply_html(
        f"Screenshot ricevuto. L'ordine <b>{esc(order['order_code'])}</b> e' ora "
        "<b>in verifica</b>."
    )

    updated = await db.get_order(order["order_code"])
    try:
        await context.bot.forward_message(
            chat_id=config.ADMIN_USER_ID,
            from_chat_id=update.message.chat_id,
            message_id=update.message.message_id,
        )
    except TelegramError:
        logger.warning("Inoltro screenshot all'admin fallito", exc_info=True)
    await _notify_admin_new_payment(context, updated, None)


# --------------------------------------------------------------------------
# Notifiche all'admin
# --------------------------------------------------------------------------

async def _notify_admin_text(context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    """Messaggio all'admin. Un errore qui non deve far fallire il flusso utente."""
    try:
        await context.bot.send_message(
            chat_id=config.ADMIN_USER_ID, text=text, parse_mode=ParseMode.HTML
        )
    except TelegramError:
        logger.error("Notifica admin fallita", exc_info=True)


async def _notify_admin_new_payment(context: ContextTypes.DEFAULT_TYPE, order: dict,
                                    verification) -> None:
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("Conferma", callback_data=f"{CB_ADMIN_APPROVE}{order['order_code']}"),
        InlineKeyboardButton("Rifiuta", callback_data=f"{CB_ADMIN_REJECT}{order['order_code']}"),
    ]])

    howto = (
        "Riscatta il <b>codice</b> (buono Amazon) sul tuo account. "
        "Se e' valido e dell'importo giusto, Conferma; altrimenti Rifiuta."
    )

    text = (
        "<b>Nuovo pagamento da verificare</b>\n\n"
        + format_order(order, for_admin=True)
        + f"\n\n{esc(order['product_name'])} - atteso "
        + f"<b>{esc(money(order['price'], order['currency']))}</b>\n"
        + howto
    )
    try:
        await context.bot.send_message(
            chat_id=config.ADMIN_USER_ID,
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
        )
    except TelegramError:
        logger.error("Notifica nuovo pagamento all'admin fallita", exc_info=True)


# --------------------------------------------------------------------------
# Richiesta di consegna mod/licenza (dopo pagamento confermato)
# --------------------------------------------------------------------------

async def request_delivery_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Il cliente chiede di ricevere mod e licenza per un ordine gia' pagato."""
    query = update.callback_query
    order_code = query.data[len(CB_REQUEST_DELIVERY):]
    order = await db.get_order(order_code)

    if order is None or order["user_id"] != update.effective_user.id:
        await query.answer("Ordine non trovato.", show_alert=True)
        return

    ok, reason = await request_delivery(order)
    if not ok:
        await query.answer(reason, show_alert=True)
        return

    await query.answer("Richiesta inviata.")
    await query.message.reply_html(
        f"Richiesta inviata per l'ordine <b>{esc(order_code)}</b>.\n"
        "Appena l'admin conferma ricevi mod e licenza qui in chat."
    )

    user = update.effective_user
    uname = f"@{order['username']}" if order["username"] else user_label(user)
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("Accetta", callback_data=f"{CB_ADMIN_DELIVER_APPROVE}{order_code}"),
        InlineKeyboardButton("Rifiuta", callback_data=f"{CB_ADMIN_DELIVER_REJECT}{order_code}"),
    ]])
    await _notify_admin_text(
        context,
        "<b>Richiesta mod e licenza</b>\n\n"
        + format_order(order, for_admin=True)
        + f"\n\nCliente: {user_link(order['user_id'], uname)}",
    )
    try:
        await context.bot.send_message(
            chat_id=config.ADMIN_USER_ID,
            text=f"Accetti la richiesta per <b>{esc(order_code)}</b>?",
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
        )
    except TelegramError:
        logger.error("Notifica richiesta consegna all'admin fallita", exc_info=True)


async def admin_approve_delivery_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if update.effective_user.id != config.ADMIN_USER_ID:
        await query.answer("Non autorizzato.", show_alert=True)
        return
    order_code = query.data[len(CB_ADMIN_DELIVER_APPROVE):]
    order = await db.get_order(order_code)
    if order is None:
        await query.answer("Ordine inesistente.", show_alert=True)
        return

    await query.answer("Elaborazione in corso...")
    ok, message = await approve_delivery_request(
        context.bot, order, actor_id=update.effective_user.id
    )
    await query.message.reply_html(
        (f"Ordine <b>{esc(order_code)}</b>: {esc(message)}") if ok
        else (f"Ordine <b>{esc(order_code)}</b>\n{esc(message)}")
    )


async def admin_reject_delivery_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if update.effective_user.id != config.ADMIN_USER_ID:
        await query.answer("Non autorizzato.", show_alert=True)
        return
    order_code = query.data[len(CB_ADMIN_DELIVER_REJECT):]
    order = await db.get_order(order_code)
    if order is None:
        await query.answer("Ordine inesistente.", show_alert=True)
        return

    await query.answer("Richiesta rifiutata.")
    ok, message = await reject_delivery_request(
        context.bot, order, actor_id=update.effective_user.id
    )
    await query.message.reply_html(esc(message) if ok else f"Non riuscito: {esc(message)}")


# --------------------------------------------------------------------------
# Elenco ordini dell'utente
# --------------------------------------------------------------------------

def _my_orders_text(orders: list[dict]) -> str:
    if not orders:
        return "Non hai ancora nessun ordine.\nApri il catalogo per iniziare."
    blocks = [format_order(o) for o in orders]
    return "<b>I tuoi ordini</b>\n\n" + "\n\n".join(blocks)


async def ordine_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user

    async def reply(text: str) -> None:
        await update.message.reply_text(text)

    if not await _check_rate_limit(user.id, reply):
        return

    orders = await db.get_user_orders(user.id, limit=10)
    await update.message.reply_html(_my_orders_text(orders), reply_markup=main_menu_keyboard())


async def my_orders_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = update.effective_user

    async def alert(text: str) -> None:
        await query.answer(text, show_alert=True)

    if not await _check_rate_limit(user.id, alert):
        return

    orders = await db.get_user_orders(user.id, limit=10)
    await query.answer()
    text = _my_orders_text(orders)
    if query.message and query.message.photo:
        await query.message.reply_html(text, reply_markup=main_menu_keyboard())
    else:
        await query.edit_message_text(
            text, parse_mode=ParseMode.HTML, reply_markup=main_menu_keyboard()
        )


def register(app) -> None:
    app.add_handler(CommandHandler("ordine", ordine_command))
    app.add_handler(CallbackQueryHandler(buy_callback, pattern=f"^{CB_BUY}"))
    app.add_handler(CallbackQueryHandler(cancel_order_callback, pattern=f"^{CB_CANCEL_ORDER}"))
    app.add_handler(CallbackQueryHandler(request_delivery_callback, pattern=f"^{CB_REQUEST_DELIVERY}"))
    app.add_handler(CallbackQueryHandler(admin_approve_delivery_callback,
                                         pattern=f"^{CB_ADMIN_DELIVER_APPROVE}"))
    app.add_handler(CallbackQueryHandler(admin_reject_delivery_callback,
                                         pattern=f"^{CB_ADMIN_DELIVER_REJECT}"))
    app.add_handler(CallbackQueryHandler(my_orders_callback, pattern=f"^{CB_MY_ORDERS}$"))
    app.add_handler(MessageHandler(filters.PHOTO, photo_message))
    # Registrato per ultimo: cattura il testo libero, che in questo bot
    # significa "username Minecraft" o "riferimento di pagamento".
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_message))

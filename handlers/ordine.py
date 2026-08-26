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
import paypal
from handlers.common import (
    CB_ADMIN_APPROVE,
    CB_ADMIN_REJECT,
    CB_BUY,
    CB_CANCEL_ORDER,
    CB_CATALOG,
    CB_GIFTCARD,
    CB_MY_ORDERS,
    GIFTCARD_RE,
    PAY_GIFTCARD,
    PAY_PAYPAL,
    esc,
    format_order,
    is_valid_mc_username,
    main_menu_keyboard,
    money,
    payment_kind,
    user_label,
)
from orderflow import confirm_and_deliver
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
    amount = esc(money(order["price"], order["currency"]))
    extra = config.GIFTCARD_INSTRUCTIONS or (
        "Compra un <b>buono regalo Amazon</b> (amazon.it) da esattamente "
        f"<b>{amount}</b>."
    )
    return (
        f"<b>Ordine {esc(order['order_code'])}</b>\n"
        f"Importo esatto: <b>{amount}</b>\n\n"
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
    """Istruzioni di pagamento, secondo il metodo configurato."""
    if config.giftcard_mode():
        return _giftcard_text(order)

    dest = config.paypal_destination()
    head = (
        f"<b>Ordine {esc(order['order_code'])}</b>\n"
        f"Importo esatto: <b>{esc(money(order['price'], order['currency']))}</b>\n"
        f"Destinatario PayPal: <code>{esc(dest)}</code>\n\n"
    )

    if config.PAYPAL_MODE == config.MODE_FRIENDS:
        body = (
            "<b>Come pagare</b>\n"
            f"Invia l'importo a <code>{esc(dest)}</code> scegliendo "
            "<b>Amici e Famiglia</b> (NON Beni e servizi).\n"
            f"Nella causale scrivi <b>solo</b> il codice: <code>{esc(order['order_code'])}</code>\n"
            "Nient'altro: niente descrizioni, niente nomi.\n\n"
            "<b>Attenzione</b>\n"
            "- Un pagamento inviato come <b>Beni e servizi</b> viene rifiutato.\n"
            "- Amici e Famiglia non prevede protezione acquisti: e' un pagamento "
            "fra persone che si fidano.\n"
            "- Senza il codice in causale non riesco ad abbinare il pagamento "
            "al tuo ordine.\n\n"
        )
    else:
        body = (
            "<b>Come pagare</b>\n"
            f"Invia l'importo a <code>{esc(dest)}</code> come "
            "<b>Beni e servizi</b>.\n"
            f"Nella causale scrivi <b>solo</b> il codice: <code>{esc(order['order_code'])}</code>\n\n"
            "<b>Attenzione</b>\n"
            "- Invia l'importo esatto: le eventuali commissioni sono a tuo carico.\n"
            "- Senza il codice in causale non riesco ad abbinare il pagamento.\n\n"
        )

    tail = (
        "<b>Dopo il pagamento</b>\n"
        "Incolla qui l'<b>ID transazione</b> PayPal (nei dettagli del movimento), "
        "oppure manda uno <b>screenshot</b> della ricevuta.\n"
        "Verifico a mano e ricevi tutto qui in chat."
    )
    return head + body + tail


def _pending_order_keyboard(order_code: str) -> InlineKeyboardMarkup:
    rows = []
    # In modalita' gift card le istruzioni SONO gia' quelle del buono: il bottone
    # avrebbe senso solo come ripiego quando il metodo principale e' PayPal.
    if config.GIFTCARD_ENABLED and not config.giftcard_mode():
        rows.append([InlineKeyboardButton("Paga con gift card",
                                          callback_data=f"{CB_GIFTCARD}{order_code}")])
    rows.append([InlineKeyboardButton("Annulla ordine",
                                      callback_data=f"{CB_CANCEL_ORDER}{order_code}")])
    rows.append([InlineKeyboardButton("Torna al catalogo", callback_data=CB_CATALOG)])
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


async def giftcard_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Istruzioni per il metodo alternativo, se abilitato in configurazione."""
    query = update.callback_query
    order_code = query.data[len(CB_GIFTCARD):]
    order = await db.get_order(order_code)

    if order is None or order["user_id"] != update.effective_user.id:
        await query.answer("Ordine non trovato.", show_alert=True)
        return
    if not config.GIFTCARD_ENABLED:
        await query.answer("Metodo non disponibile.", show_alert=True)
        return

    await query.answer()
    instructions = config.GIFTCARD_INSTRUCTIONS or (
        "Contattami per concordare il tipo di gift card accettato."
    )
    await query.message.reply_html(
        f"<b>Ordine {esc(order_code)}</b>\n"
        f"Importo: <b>{esc(money(order['price'], order['currency']))}</b>\n\n"
        f"{esc(instructions)}\n\n"
        "Quando l'hai acquistata incolla qui il <b>codice</b> della gift card. "
        "Ogni codice puo' essere usato per un solo ordine."
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
            "Quello e' un <b>codice ordine</b>: va scritto nella causale del "
            "pagamento, non qui.\n"
            "Qui serve l'<b>ID transazione</b> che PayPal assegna al movimento, "
            "oppure uno screenshot della ricevuta."
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
    """Verifica automatica (se possibile) e notifica all'admin."""
    verification = None
    if kind == PAY_PAYPAL and config.paypal_auto_verify_enabled():
        verification = await paypal.verify_payment(
            order["paypal_txn_id"], order["price"], order["currency"]
        )
        log_event("verifica_paypal", order=order["order_code"],
                  esito="ok" if verification.ok else "ko",
                  degradata=verification.degraded, motivo=verification.reason)

        if verification.ok and config.PAYPAL_AUTOCONFIRM:
            ok, message = await confirm_and_deliver(
                context.bot, order, actor_id=0, note="Auto-confermato dall'API PayPal"
            )
            await _notify_admin_text(
                context,
                f"<b>Auto-conferma</b> ordine <code>{esc(order['order_code'])}</code>\n"
                f"{esc(message)}",
            )
            if ok:
                return  # gia' gestito: niente bottoni di conferma

    await _notify_admin_new_payment(context, order, verification)


async def text_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Unico punto d'ingresso per il testo libero.

    Priorita': 1) username atteso, 2) riferimento di pagamento per un ordine
    aperto, 3) messaggio di aiuto. Senza un ordine aperto non si tocca il DB.
    """
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

    # In modalita' gift card ogni testo libero e' il codice del buono: si accetta
    # se ha la forma di un codice, senza passare dal riconoscimento PayPal.
    if config.giftcard_mode():
        if not GIFTCARD_RE.match(db.normalize_payment_id(text)):
            await update.message.reply_html(
                "Non riconosco un codice valido.\n"
                "Incolla qui il <b>codice</b> del buono regalo Amazon "
                "(tipo <code>XXXX-XXXXXXX-XXXX</code>), solo il codice."
            )
            return
        await _handle_payment_reference(update, context, order, text, PAY_GIFTCARD)
        return

    kind = payment_kind(text)
    if kind is None:
        await update.message.reply_html(
            "Non riconosco un riferimento di pagamento valido.\n"
            "L'<b>ID transazione PayPal</b> e' una stringa di lettere e numeri "
            "(circa 17 caratteri), la trovi nei dettagli del movimento.\n"
            "In alternativa manda uno <b>screenshot</b> della ricevuta."
        )
        return

    if kind == PAY_GIFTCARD and not config.GIFTCARD_ENABLED:
        await update.message.reply_html(
            "Quello non sembra un ID transazione PayPal.\n"
            "Controlla di aver copiato l'ID del movimento, oppure manda uno "
            "screenshot della ricevuta."
        )
        return

    await _handle_payment_reference(update, context, order, text, kind)


async def photo_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Screenshot della ricevuta come prova di pagamento.

    L'unicita' si basa sul file_unique_id della foto: impedisce di rimandare
    lo stesso identico file su un secondo ordine. La verifica vera resta
    quella dell'admin sul conto PayPal.
    """
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

    if config.giftcard_mode():
        howto = (
            "Riscatta il <b>codice</b> (buono Amazon) sul tuo account. "
            "Se e' valido e dell'importo giusto, Conferma; altrimenti Rifiuta."
        )
    elif config.PAYPAL_MODE == config.MODE_FRIENDS:
        howto = (
            "Controlla su PayPal: importo, causale uguale al codice ordine, mittente."
        )
    else:
        howto = "Controlla la transazione su PayPal, poi conferma o rifiuta."
    if verification is not None:
        howto = f"{verification.label}\n{howto}"

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
    app.add_handler(CallbackQueryHandler(giftcard_callback, pattern=f"^{CB_GIFTCARD}"))
    app.add_handler(CallbackQueryHandler(my_orders_callback, pattern=f"^{CB_MY_ORDERS}$"))
    app.add_handler(MessageHandler(filters.PHOTO, photo_message))
    # Registrato per ultimo: cattura il testo libero, che in questo bot
    # significa "username Minecraft" o "riferimento di pagamento".
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_message))

"""Verifica automatica di un pagamento PayPal (opzionale).

Funziona SOLO per i pagamenti Beni e servizi: i pagamenti personali
(Amici e Famiglia) non passano dalle Orders/Payments API, quindi in quella
modalita' la verifica automatica e' disattivata a monte da
config.paypal_auto_verify_enabled() e ogni controllo resta all'admin.

Se l'API non e' configurata, non risponde o restituisce un formato inatteso,
il risultato e' "degradato": il bot non blocca nulla e si torna alla conferma
manuale.
"""

import base64
import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

import config

logger = logging.getLogger(__name__)

_TIMEOUT = httpx.Timeout(15.0)


@dataclass
class VerificationResult:
    ok: bool                       # pagamento verificato e conforme
    degraded: bool = False         # verifica non eseguibile: si torna al manuale
    reason: str = ""               # motivo leggibile, mostrato all'admin
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def label(self) -> str:
        if self.degraded:
            return f"Verifica automatica non disponibile ({self.reason})"
        return ("Verificato dall'API PayPal" if self.ok
                else f"NON verificato: {self.reason}")


async def _access_token(client: httpx.AsyncClient) -> str:
    basic = base64.b64encode(
        f"{config.PAYPAL_CLIENT_ID}:{config.PAYPAL_SECRET}".encode()
    ).decode()
    resp = await client.post(
        f"{config.PAYPAL_API_BASE}/v1/oauth2/token",
        headers={"Authorization": f"Basic {basic}",
                 "Content-Type": "application/x-www-form-urlencoded"},
        content="grant_type=client_credentials",
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


async def _fetch_transaction(client: httpx.AsyncClient, token: str,
                             txn_id: str) -> dict[str, Any] | None:
    """Prova prima come ordine Checkout, poi come cattura di pagamento."""
    headers = {"Authorization": f"Bearer {token}"}
    for path in (f"/v2/checkout/orders/{txn_id}", f"/v2/payments/captures/{txn_id}"):
        resp = await client.get(f"{config.PAYPAL_API_BASE}{path}", headers=headers)
        if resp.status_code == 200:
            return resp.json()
        if resp.status_code not in (403, 404):
            resp.raise_for_status()
    return None


def _extract_amount_and_payee(payload: dict[str, Any]) -> tuple[float | None, str, dict]:
    """Normalizza le due forme di risposta (order vs capture)."""
    units = payload.get("purchase_units") or []
    if units:
        unit = units[0]
        amount = unit.get("amount") or {}
        payments = (unit.get("payments") or {}).get("captures") or []
        if not amount and payments:
            amount = payments[0].get("amount") or {}
        payee = unit.get("payee") or {}
    else:  # risposta di tipo capture
        amount = payload.get("amount") or {}
        payee = payload.get("payee") or {}

    try:
        value = float(amount.get("value"))
    except (TypeError, ValueError):
        value = None
    return value, (amount.get("currency_code") or ""), payee


def evaluate(payload: dict[str, Any], expected_price: float,
             expected_currency: str = "EUR") -> VerificationResult:
    """Controlla stato, importo, valuta e destinatario. Testabile senza rete."""
    status = (payload.get("status") or "").upper()
    value, currency, payee = _extract_amount_and_payee(payload)
    details = {"status": status, "amount": value, "currency": currency,
               "payee": payee.get("email_address") or payee.get("merchant_id") or ""}

    if status not in ("COMPLETED", "CAPTURED"):
        return VerificationResult(False, reason=f"stato {status or 'sconosciuto'}",
                                  details=details)
    if value is None:
        return VerificationResult(False, reason="importo illeggibile", details=details)
    if currency.upper() != expected_currency.upper():
        return VerificationResult(False, reason=f"valuta {currency}", details=details)
    # Tolleranza di un centesimo: gli arrotondamenti di PayPal non devono
    # far fallire un pagamento corretto.
    if value + 0.01 < expected_price:
        return VerificationResult(
            False,
            reason=f"importo {value:.2f} inferiore ai {expected_price:.2f} attesi",
            details=details,
        )

    expected_payee = (config.PAYPAL_MERCHANT_ID or config.PAYPAL_EMAIL).strip().lower()
    got_payee = str(details["payee"]).strip().lower()
    if expected_payee and got_payee and got_payee != expected_payee:
        return VerificationResult(False, reason=f"destinatario {got_payee}", details=details)

    return VerificationResult(True, reason="importo, valuta e destinatario corretti",
                              details=details)


async def verify_payment(txn_id: str, expected_price: float,
                         expected_currency: str = "EUR") -> VerificationResult:
    """Verifica un pagamento. Non solleva mai: degrada al manuale."""
    if not config.paypal_auto_verify_enabled():
        return VerificationResult(
            False, degraded=True,
            reason="non configurata o modalita' Amici e Famiglia",
        )

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            token = await _access_token(client)
            payload = await _fetch_transaction(client, token, txn_id)
    except Exception as exc:  # rete, credenziali, formato: mai bloccante
        logger.warning("Verifica PayPal non riuscita: %s", exc)
        return VerificationResult(False, degraded=True, reason=str(exc)[:120])

    if payload is None:
        return VerificationResult(False, reason="transazione non trovata su PayPal")

    return evaluate(payload, expected_price, expected_currency)

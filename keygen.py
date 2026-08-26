"""Generazione della chiave di licenza tramite il tool esterno.

La chiave e' la firma Ed25519 del testo "GKR1|<username>", restituita come
stringa "GKR1-<base64url>". La firma la produce un tool esterno (KEYGEN_CMD)
che legge la chiave privata: questo modulo si limita a invocarlo e a leggerne
l'output.

Sicurezza:
  - la chiave privata non viene mai letta, loggata o trasmessa da qui;
  - il comando parte SENZA shell e lo username e' validato a monte
    (solo [A-Za-z0-9_]), quindi non c'e' spazio per iniezioni;
  - se la chiave privata non e' presente sulla macchina, la generazione
    fallisce con un messaggio esplicito: il bot su VPS non deve generare nulla.
"""

import asyncio
import logging
import re
import shlex

import config

logger = logging.getLogger(__name__)

# Riga cercata nell'output del tool.
KEY_RE = re.compile(r"\bGKR1-[A-Za-z0-9_-]{16,}\b")

# Stesso vincolo applicato lato handler; ripetuto qui perche' questo modulo
# costruisce una riga di comando e non deve fidarsi del chiamante.
USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,16}$")


class KeygenError(Exception):
    """Generazione della chiave fallita: l'ordine non va completato."""


def build_command(username: str) -> list[str]:
    """Espande KEYGEN_CMD sostituendo {username}, senza passare da una shell."""
    template = config.KEYGEN_CMD
    if "{username}" not in template:
        raise KeygenError(
            "KEYGEN_CMD non contiene il segnaposto {username}: "
            f"valore attuale {template!r}"
        )
    # posix=False preserva i backslash dei percorsi Windows, ma lascia anche le
    # virgolette dentro il token: vanno tolte, altrimenti finirebbero nel nome
    # dell'eseguibile (es. KEYGEN_CMD='"C:\Program Files\...\java.exe" ...').
    parts = []
    for raw in shlex.split(template, posix=False):
        if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
            raw = raw[1:-1]
        parts.append(raw.replace("{username}", username))
    return parts


def extract_key(output: str) -> str | None:
    match = KEY_RE.search(output)
    return match.group(0) if match else None


async def generate_license_key(username: str) -> str:
    """Esegue il keygen e ritorna la chiave. Solleva KeygenError se fallisce."""
    if not USERNAME_RE.match(username or ""):
        raise KeygenError(f"Username Minecraft non valido: {username!r}")

    if not config.PRIVATE_KEY_PATH.is_file():
        raise KeygenError(
            f"Chiave privata non trovata in {config.PRIVATE_KEY_PATH}. "
            "Le chiavi vanno generate sulla macchina fidata che la custodisce: "
            "genera li' la licenza e consegnala a mano."
        )

    cmd = build_command(username)
    logger.info("Keygen per %s: %s", username, cmd[0])

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(config.KEYGEN_CWD),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise KeygenError(
            f"Comando keygen non eseguibile ({cmd[0]}): {exc}. "
            "Controlla KEYGEN_CMD e che java sia nel PATH."
        ) from exc
    except OSError as exc:
        raise KeygenError(f"Avvio del keygen fallito: {exc}") from exc

    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=config.KEYGEN_TIMEOUT
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise KeygenError(
            f"Keygen scaduto dopo {config.KEYGEN_TIMEOUT}s per {username}"
        )

    out = stdout.decode("utf-8", "replace")
    err = stderr.decode("utf-8", "replace")

    if proc.returncode != 0:
        # Si riporta solo la coda dell'errore: l'output del tool non contiene
        # la chiave privata, ma non c'e' motivo di versarlo tutto nei log.
        raise KeygenError(
            f"Keygen uscito con codice {proc.returncode}: {err.strip()[-300:] or 'nessun dettaglio'}"
        )

    key = extract_key(out) or extract_key(err)
    if not key:
        raise KeygenError(
            "Nessuna riga GKR1-... trovata nell'output del keygen. "
            f"Output: {out.strip()[-200:] or '(vuoto)'}"
        )
    return key

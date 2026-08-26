# Bot Telegram - vendita licenza e valuta in gioco

Bot per vendere una licenza mod Minecraft e valuta in gioco, pagamento con
**buono a codice (Amazon)** e **conferma manuale** dell'admin. Due tipi di
prodotto, due consegne diverse:

| Tipo | Prodotti | Cosa succede alla conferma |
|---|---|---|
| `licenza_file` | Licenza mod - 10,00 EUR | il server licenze rilascia un token, invia `.jar` + `license.key` |
| `valuta_ingame` | 1M (5,00) / 5M (15,00) / 10M (20,00) EUR | nessun file: l'admin paga in gioco e poi preme **Consegnato** |

Prezzi e nomi si cambiano in [catalog.py](catalog.py), mai sul database.

## I due processi

Il bot lavora insieme a un secondo processo, il **server licenze**
([license_server.py](license_server.py)): la mod lo chiama a ogni avvio con
token + username, e solo se validi riceve la logica del modulo. Nessuna
chiave privata da custodire: la validita' di un token e' solo una riga
nell'ordine (`orders.db`).

Per il deploy completo (VPS, systemd, reverse proxy TLS) vedi
[DEPLOY.md](DEPLOY.md).

## Setup rapido (locale)

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -r requirements.txt

copy .env.example .env          # Windows  (cp su Linux/macOS)
```

Compila **solo** `TELEGRAM_BOT_TOKEN` e `ADMIN_USER_ID` — tutto il resto ha
gia' un default che funziona (vedi [env.schema.json](env.schema.json)).

Metti il jar della mod in `files/` col nome indicato in
[catalog.py](catalog.py) (default `files/rocket-auto-farm.jar`), e il payload
della mod in `payload/rocket-auto-farm.bin`.

Avvio (due processi separati):

```bash
python bot.py
python license_server.py
```

Al primo avvio del bot apri una chat dal tuo account admin e manda `/start`,
altrimenti il bot non puo' inviarti le notifiche.

## Variabili .env

Schema completo, machine-readable, in [env.schema.json](env.schema.json).

### Obbligatorie

| Variabile | A cosa serve |
|---|---|
| `TELEGRAM_BOT_TOKEN` | token da @BotFather |
| `ADMIN_USER_ID` | il tuo user_id Telegram (da @userinfobot) |

### Pagamento (buono a codice)

| Variabile | Default | Note |
|---|---|---|
| `PAYMENT_METHOD` | `giftcard` | metodo principale |
| `GIFTCARD_ENABLED` | `true` | |
| `GIFTCARD_INSTRUCTIONS` | vuoto | testo mostrato al compratore; vuoto = default per un buono Amazon |

Il compratore incolla il **codice**, mai screenshot o dati Amazon. **Tu**
riscatti il codice a mano su amazon.it prima di premere Conferma: e' la sola
verifica reale, e non ha alternativa automatica (Amazon non espone API di
riscatto). Il codice usato non e' riutilizzabile su un secondo ordine (indice
UNIQUE).

### Licenza (modello online)

| Variabile | Default | Note |
|---|---|---|
| `LICENSE_MODE` | `online` | token validato dal server licenze |
| `TOKEN_PREFIX` | `GKR1-` | solo cosmetico |
| `LICENSE_HOST` | `127.0.0.1` | il server licenze ascolta in locale |
| `LICENSE_PORT` | `8787` | |
| `PAYLOAD_PATH` | `payload/rocket-auto-farm.bin` | file servito dopo un token valido |

### Altre

`DB_PATH`, `FILES_DIR`, `ORDER_RATE_LIMIT_MAX`, `ORDER_RATE_LIMIT_SECONDS`, `LOG_LEVEL`.

### Avanzato, non usato in questo setup

- **PayPal** (`PAYMENT_METHOD=paypal`): alternativa al buono a codice. Mostra
  il tuo nome al compratore (KYC). Vedi i commenti in `.env.example` e
  [paypal.py](paypal.py).
- **Licenza offline** (`LICENSE_MODE=offline`): chiave Ed25519 firmata da un
  keygen locale invece del server. Richiede una chiave privata che **non va
  mai su un VPS**. Vedi [tools/README.md](tools/README.md) e
  [keygen.py](keygen.py).

## Flusso di acquisto

1. `/start` -> catalogo -> **Compra**
2. il bot chiede lo **username Minecraft** (3-16 caratteri, `A-Z a-z 0-9 _`)
3. viene creato l'ordine con un codice corto tipo `ORD-XY7QK` (un solo ordine
   aperto per volta)
4. istruzioni: importo esatto, e cosa comprare (buono Amazon)
5. l'utente incolla il **codice** del buono
6. l'ordine passa *in verifica* e arriva la notifica all'admin
7. l'admin **riscatta il codice su amazon.it** e, se valido, preme **Conferma**
8. consegna secondo il tipo, poi stato *completato*

Lo username viene ripetuto al compratore con l'avviso che la chiave vale solo
per quel nome (o che la valuta finisce a quel nome).

## Comandi admin

| Comando | Cosa fa |
|---|---|
| `/admin` | pannello: pagamenti da verificare + ordini da consegnare in gioco |
| `/cerca <testo>` | cerca per codice, causale, username Minecraft o riferimento |
| `/rimborso <codice>` | segna l'ordine come rimborsato |
| `/disputa <codice>` | segna l'ordine come contestato |

Dai risultati di `/cerca` e dal pannello si raggiungono i bottoni Conferma,
Rifiuta, Consegnato, Rimborsato, Contestato e **Segna licenza revocata**.

La revoca e' reale nel modello online: il server licenze smette di servire il
payload per quel token, la mod smette di funzionare al prossimo avvio.

## Struttura

| File | Ruolo |
|---|---|
| `bot.py` | entry point del bot: log, validazione config, handler, polling |
| `license_server.py` | processo separato: valida token+username, serve il payload |
| `config.py` | lettura `.env`, avvisi di configurazione, nessun segreto hardcoded |
| `catalog.py` | prodotti, tipi, prezzi |
| `database.py` | SQLite async, migrazioni additive, anti-riuso pagamenti |
| `orderflow.py` | conferma + consegna: unico punto che cambia stato dopo la consegna |
| `delivery.py` | consegna ramificata per tipo (licenza / valuta), emette il token online |
| `keygen.py` | modello offline: esecuzione del tool esterno che firma la chiave |
| `paypal.py` | metodo PayPal alternativo, verifica automatica opzionale |
| `handlers/start.py` | `/start`, `/help`, menu |
| `handlers/catalogo.py` | `/catalogo`, schede prodotto |
| `handlers/ordine.py` | username, creazione ordine, pagamento, `/ordine` |
| `handlers/admin.py` | pannello, conferme, consegne, rimborsi, revoche |
| `utils/logger.py` | `logs/bot.log` + audit su `logs/audit.log` |
| `utils/ratelimit.py` | rate limiting in memoria per utente |
| `tests/` | smoke test eseguibili senza rete |

## Stati di un ordine

```
in_attesa_pagamento -> in_verifica -+-> completato                (licenza)
                                    |
                                    +-> da_consegnare_in_gioco -> completato
       |                     |
   annullato             rifiutato          rimborsato / contestato (in qualsiasi momento)
```

## Test

```bash
.venv\Scripts\python.exe tests\run_all.py
```

oppure una suite alla volta:

| Suite | Copre |
|---|---|
| `tests/smoke_db.py` | CRUD, anti-riuso pagamenti, migrazione da schema vecchio, validatori |
| `tests/smoke_app.py` | registrazione handler, routing dei callback, catalogo |
| `tests/smoke_flow.py` | flusso completo con bot finto (metodo PayPal, per esercitare quel ramo) |
| `tests/smoke_paypal.py` | valutazione risposte API PayPal e contratto del keygen (modello offline) |
| `tests/smoke_license_server.py` | server licenze: token valido/invalido/revocato/scaduto |

Nessuna rete reale, nessun token vero, nessuna chiave privata reale: i
database di prova (`*_test.db`, `flow_test.db`) sono ignorati da git.

## Backup e note operative

Da salvare, fuori dal repository:

- `orders.db` - ordini, token emessi, riferimenti di pagamento;
- `files/` - il jar che vendi;
- `payload/` - il payload della mod (necessario perche' il server licenze
  risponda).

Altre note:

- **uptime**: nel modello online, se il server licenze e' giu' la mod non
  parte per nessuno (nessun percorso offline, per scelta). Vedi DEPLOY.md per
  i servizi systemd con riavvio automatico;
- il **rate limit e' in memoria**: si azzera a ogni riavvio del bot. Serve
  contro lo spam, non e' una difesa da abusi mirati;
- anche l'attesa dello username vive in memoria: se il bot riparte in quel
  momento, l'utente ripreme Compra. Nessun ordine viene perso, perche' l'ordine
  nasce solo dopo che lo username e' stato accettato;
- un riferimento di pagamento gia' usato non e' riutilizzabile (indice UNIQUE),
  neanche dopo un rifiuto;
- se la consegna fallisce l'ordine resta aperto, cosi' puoi ritentare;
- ogni evento importante finisce in `logs/audit.log`: ordine creato, pagamento
  ricevuto, token emesso, consegna, conferma, rifiuto, rimborso, revoca.

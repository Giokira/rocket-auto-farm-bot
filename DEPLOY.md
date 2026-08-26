# Deploy su VPS

Guida per mettere in produzione il bot Telegram + il server di validazione licenze.
Serve un VPS Linux (anche minuscolo: 256 MB RAM, 1 vCPU, ~3 GB disco bastano) e un
**dominio** che punti all'IP del VPS.

Due processi girano sul VPS:

- **bot Telegram** (`bot.py`) — vendite, ordini, consegna.
- **server licenze** (`license_server.py`) — la mod ci chiede il payload a ogni avvio.

Il server licenze ascolta solo su `127.0.0.1`; davanti ci va un reverse proxy con TLS
(Caddy) che espone `https://<dominio>/validate`.

---

## 0. Prima di salire sul VPS: build di produzione della mod

Sul PC di sviluppo, con il **dominio** deciso:

1. In `src/main/java/com/rocketfarm/loader/RemoteLoader.java` metti l'URL vero:
   ```java
   private static final String SERVER_URL = "https://licenze.TUODOMINIO.it/validate";
   ```
2. Ricompila:
   ```
   ./gradlew build
   ```
3. Escono:
   - `build/libs/RocketAutoFarm-0.1.0-shell.jar` → il jar da **vendere** (va in `files/`).
   - `build/payload/rocket-auto-farm.bin` → il payload, va **sul VPS**.

---

## 1. Utente e file sul VPS

```bash
sudo adduser --disabled-password --gecos "" rocket
sudo su - rocket

# copia qui la cartella bot/ (scp/rsync/git). Struttura attesa:
#   /home/rocket/rocketbot/            <- contenuto di bot/
#   /home/rocket/rocketbot/files/rocket-auto-farm.jar     (lo shell jar)
#   /home/rocket/rocketbot/payload/rocket-auto-farm.bin   (il payload)
mkdir -p /home/rocket/rocketbot/payload
```

## 2. Ambiente Python

```bash
cd /home/rocket/rocketbot
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## 3. Configurazione `.env`

```bash
cp .env.example .env
nano .env
chmod 600 .env
```

Valori minimi:

```
TELEGRAM_BOT_TOKEN=...        # da @BotFather
ADMIN_USER_ID=...            # il tuo id, da @userinfobot
PAYMENT_METHOD=giftcard
GIFTCARD_INSTRUCTIONS=Compra un buono regalo Amazon (amazon.it) dell'importo esatto.

LICENSE_MODE=online
LICENSE_HOST=127.0.0.1
LICENSE_PORT=8787
PAYLOAD_PATH=payload/rocket-auto-farm.bin
```

> Niente PayPal, niente chiave privata, **niente credenziali Amazon**: i buoni si
> riscattano a mano da account tuo, il bot non tocca Amazon.

Prova che parta a mano (Ctrl+C per fermare):

```bash
.venv/bin/python bot.py
.venv/bin/python license_server.py
```

## 4. Servizi systemd (riavvio automatico)

Come **root**, crea i due file.

`/etc/systemd/system/rocketbot.service`:

```ini
[Unit]
Description=Rocket Auto Farm - Telegram bot
After=network-online.target
Wants=network-online.target

[Service]
User=rocket
WorkingDirectory=/home/rocket/rocketbot
ExecStart=/home/rocket/rocketbot/.venv/bin/python bot.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

`/etc/systemd/system/rocketlicense.service`:

```ini
[Unit]
Description=Rocket Auto Farm - license server
After=network-online.target
Wants=network-online.target

[Service]
User=rocket
WorkingDirectory=/home/rocket/rocketbot
ExecStart=/home/rocket/rocketbot/.venv/bin/python license_server.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

Attiva:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now rocketbot rocketlicense
sudo systemctl status rocketlicense --no-pager
journalctl -u rocketlicense -f    # log dal vivo
```

## 5. Reverse proxy con TLS (Caddy — la via facile)

Caddy fa da solo il certificato Let's Encrypt, basta che il dominio punti al VPS.

```bash
# installa Caddy (Debian/Ubuntu)
sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt update && sudo apt install -y caddy
```

`/etc/caddy/Caddyfile`:

```
licenze.TUODOMINIO.it {
    reverse_proxy 127.0.0.1:8787
}
```

```bash
sudo systemctl restart caddy
# prova:
curl https://licenze.TUODOMINIO.it/health   # -> {"ok":true}
```

L'URL da mettere nella mod (passo 0) è quindi `https://licenze.TUODOMINIO.it/validate`.

### Alternativa: nginx + certbot

<details>
<summary>se preferisci nginx</summary>

```nginx
# /etc/nginx/sites-available/rocket
server {
    server_name licenze.TUODOMINIO.it;
    location / { proxy_pass http://127.0.0.1:8787; }
    listen 80;
}
```

```bash
sudo ln -s /etc/nginx/sites-available/rocket /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
sudo apt install -y certbot python3-certbot-nginx
sudo certbot --nginx -d licenze.TUODOMINIO.it   # aggiunge il TLS
```
</details>

## 6. Firewall

```bash
sudo ufw allow 22
sudo ufw allow 80
sudo ufw allow 443
sudo ufw enable
```

Il server licenze resta su `127.0.0.1`: non aprire la 8787 verso l'esterno, ci arriva
solo il proxy.

---

## Aggiornare la mod (nuova versione della logica)

Il bello del modello online: la logica è **solo il payload**. Per aggiornarla:

1. Ricompila sul PC di sviluppo → nuovo `build/payload/rocket-auto-farm.bin`.
2. Sostituisci il file sul VPS (in `payload/`).
3. `sudo systemctl restart rocketlicense`.

I client scaricano il nuovo payload al prossimo avvio. Nessun jar da ridistribuire —
**a meno che** tu non cambi il guscio (URL, loader): in quel caso ricompila e ridai il
`-shell.jar` ai compratori.

## Manutenzione

- Log: `journalctl -u rocketbot -f` e `journalctl -u rocketlicense -f`.
- Backup periodico di `orders.db`.
- **Uptime**: se il VPS o `rocketlicense` sono giù, la mod non parte per nessuno (nessun
  offline, per scelta). `Restart=always` copre i crash; per i riavvii macchina i servizi
  ripartono da soli (`enable`).
- Revocare una licenza: nel bot, sull'ordine, "Segna licenza revocata" → il server smette
  di servire il payload a quel token.
</content>

# cloudflare-ddns

Production-ready Python DDNS updater for the Cloudflare `A` record at `sh.ketencek.com.au`.

The service checks the current public IPv4 address from `https://api.ipify.org`, compares it with the matching Cloudflare `A` record, and updates Cloudflare only when the address has changed. It never queries or modifies `AAAA` records.

## Requirements

- Raspberry Pi 5
- Debian or Raspberry Pi OS
- Python 3.11+
- Cloudflare zone with `sh.ketencek.com.au` as an `A` record
- Cloudflare API token with least-privilege DNS edit access
- GitHub repository with Actions enabled

## Project Layout

```text
cloudflare-ddns/
├── ddns.py
├── requirements.txt
├── .env.example
├── .gitignore
├── README.md
├── systemd/
│   ├── cloudflare-ddns.service
│   └── cloudflare-ddns.timer
└── .github/
    └── workflows/
        └── deploy.yml
```

## Cloudflare Token Creation

Create a Cloudflare API token with the minimum required permissions:

1. Open Cloudflare Dashboard.
2. Go to `My Profile` -> `API Tokens`.
3. Create a custom token.
4. Grant `Zone` -> `DNS` -> `Edit`.
5. Limit zone resources to the zone that contains `ketencek.com.au`.
6. Save the generated token securely.

The token is used only from `.env` on the Raspberry Pi. Do not commit it to Git.

## Environment Variables

Create `/home/salih/projects/cloudflare-ddns/.env` on the Raspberry Pi:

```bash
CLOUDFLARE_API_TOKEN=your_cloudflare_api_token
CLOUDFLARE_ZONE_ID=your_cloudflare_zone_id
DNS_RECORD_NAME=sh.ketencek.com.au
```

`CLOUDFLARE_ZONE_ID` is available in the Cloudflare dashboard on the zone overview page.

## Initial Raspberry Pi Setup

Run these commands on the Raspberry Pi:

```bash
sudo apt update
sudo apt install -y git python3 python3-venv

sudo mkdir -p /home/salih/projects/cloudflare-ddns
sudo chown salih:salih /home/salih/projects/cloudflare-ddns

cd /home/salih/projects/cloudflare-ddns
git clone <your_repository_url> .

python3 -m venv venv
./venv/bin/python -m pip install --upgrade pip
./venv/bin/pip install -r requirements.txt

cp .env.example .env
nano .env
chmod 600 .env
```

Replace `<your_repository_url>` with the GitHub repository URL for this project.

## Manual Testing

Run one update cycle:

```bash
cd /home/salih/projects/cloudflare-ddns
./venv/bin/python ddns.py
echo $?
```

Exit codes:

- `0`: success; no update needed or update completed
- `1`: missing or invalid configuration
- `2`: public IP or network request failed after retries
- `3`: Cloudflare API returned an error
- `4`: unexpected error

Logs are JSON lines. Example:

```json
{"level":"INFO","message":"Current public IPv4 detected","logger":"cloudflare_ddns","public_ip":"203.0.113.10"}
```

## Systemd Installation

Install and start the timer:

```bash
cd /home/salih/projects/cloudflare-ddns
sudo cp systemd/cloudflare-ddns.service /etc/systemd/system/cloudflare-ddns.service
sudo cp systemd/cloudflare-ddns.timer /etc/systemd/system/cloudflare-ddns.timer
sudo systemctl daemon-reload
sudo systemctl enable --now cloudflare-ddns.timer
```

Check status:

```bash
systemctl status cloudflare-ddns.timer
systemctl list-timers cloudflare-ddns.timer
```

Run immediately:

```bash
sudo systemctl start cloudflare-ddns.service
```

View logs:

```bash
journalctl -u cloudflare-ddns.service -n 100 --no-pager
journalctl -u cloudflare-ddns.service -f
```

The timer runs once per hour.

## GitHub Actions Setup

Add these GitHub repository secrets:

- `RASPBERRY_HOST`: Raspberry Pi hostname or IP address
- `RASPBERRY_PORT`: SSH port, usually `22`
- `RASPBERRY_USER`: SSH user, usually `salih`
- `RASPBERRY_SSH_KEY`: private SSH key with access to the Raspberry Pi

Optional email notification secrets:

- `SMTP_HOST`: SMTP server hostname
- `SMTP_PORT`: SMTP port, usually `587` for STARTTLS or `465` for SSL
- `SMTP_USER`: SMTP username
- `SMTP_PASSWORD`: SMTP password or app password
- `MAIL_FROM`: sender email address
- `MAIL_TO`: recipient email address

The workflow runs on pushes to `main` and performs:

1. Checkout repository.
2. SSH into the Raspberry Pi.
3. `cd /home/salih/projects/cloudflare-ddns`.
4. `git pull origin main`.
5. Create `venv` if missing.
6. Install Python requirements.
7. Copy systemd service files.
8. Reload systemd.
9. Enable and restart the timer.
10. Send a deployment email notification when SMTP secrets are configured.

The Raspberry Pi user must be able to run the required `sudo systemctl` and `sudo cp` commands. Configure sudo policy for `salih` according to your security requirements.

## Security Notes

- `.env` is ignored by Git and must never be committed.
- Cloudflare API tokens must never be committed.
- GitHub deployment uses GitHub Secrets only.
- Use a Cloudflare API token limited to DNS edit access for the relevant zone.
- Keep `/home/salih/projects/cloudflare-ddns/.env` readable only by the service owner:

```bash
chmod 600 /home/salih/projects/cloudflare-ddns/.env
```

## Troubleshooting

Check whether the timer is active:

```bash
systemctl status cloudflare-ddns.timer
```

Check the last service run:

```bash
systemctl status cloudflare-ddns.service
```

Follow logs:

```bash
journalctl -u cloudflare-ddns.service -f
```

Validate the public IPv4 service:

```bash
curl -4 https://api.ipify.org
```

Validate Cloudflare credentials by running:

```bash
cd /home/salih/projects/cloudflare-ddns
./venv/bin/python ddns.py
```

Common causes of failure:

- `.env` missing or unreadable.
- Incorrect `CLOUDFLARE_ZONE_ID`.
- API token does not have `Zone DNS Edit` permission.
- `DNS_RECORD_NAME` does not exist as an `A` record.
- More than one matching `A` record exists for the same name.
- Raspberry Pi has no working IPv4 internet connection.

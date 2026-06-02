# WOLAPP — Home Control (Pi)

Flask HUD on the Raspberry Pi: Wake-on-LAN, schedules, Swedish holidays calendar, and **ntfy** phone notifications when your main PC goes online/offline or when scheduled events fire.

## Deploy on Pi

```bash
# Typical path
cd /home/foreigndev/wolapp
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt   # if you add one; else install flask deps used by app.py
cp env.ntfy.example .env          # edit secrets locally — never commit .env
sudo cp wolapp.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now wolapp
```

Reach the dashboard over **Tailscale** (e.g. `http://<pi-tailscale-ip>:8080/`). Set `NTFY_CLICK_URL` in `.env` to that URL so notification taps open the HUD.

## ntfy

- Subscribe your phone to the private topic configured in `.env` as `NTFY_URL`.
- Terminal-style plain-text pushes (no markdown).
- Custom images: public raw icons in this repo — see [static/ICON_HOSTING.md](static/ICON_HOSTING.md).
- Full phone setup: [PHONE_NOTIFICATIONS.md](PHONE_NOTIFICATIONS.md).

## Icons (raw GitHub)

| Asset | URL |
|-------|-----|
| Android `Icon` | https://raw.githubusercontent.com/ForeigndevX/pi/main/static/wolapp-icon.png |
| `Attach` (512) | https://raw.githubusercontent.com/ForeigndevX/pi/main/static/wolapp-notify-512.png |
| iOS home screen | https://raw.githubusercontent.com/ForeigndevX/pi/main/static/apple-touch-icon.png |

## Layout

- `app.py` — Flask app, schedules, ntfy helpers
- `index.html` — HUD UI
- `swedish_holidays.py` — holiday data
- `static/` — icons, PWA manifest
- `data/` — **not in git** (schedules on device)

## License

Personal home automation; use at your own risk.

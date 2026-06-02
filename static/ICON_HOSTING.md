# Hosting WOLAPP icons for ntfy (public HTTPS)

ntfy servers fetch `Icon` and `Attach` images from the **public internet**. Your Pi Tailscale URL (`http://100.x.x.x:8080/static/...`) is not enough unless you expose HTTPS (e.g. Tailscale Funnel).

## Files in this repo

| File | Size | Use |
|------|------|-----|
| `wolapp-icon.png` | 192x192 | ntfy `Icon` (Android small icon) |
| `wolapp-notify-512.png` | 512x512 | ntfy `Attach` (expanded; iOS/Android) |
| `apple-touch-icon.png` | 180x180 | Safari **Add to Home Screen** |

## GitHub raw (ForeigndevX/pi)

Public URLs (branch `main`):

```bash
NTFY_ICON_URL=https://raw.githubusercontent.com/ForeigndevX/pi/main/static/wolapp-icon.png
NTFY_ATTACH_URL=https://raw.githubusercontent.com/ForeigndevX/pi/main/static/wolapp-notify-512.png
```

1. Add both lines to `/home/foreigndev/wolapp/.env` on the Pi.
2. `sudo systemctl restart wolapp`
3. HUD -> **TEST NOTIFY**, or POST `/api/notify/test` on the dashboard.

## Verify fetch

```bash
curl -sI "https://raw.githubusercontent.com/ForeigndevX/pi/main/static/wolapp-icon.png" | head -5
curl -sI "https://raw.githubusercontent.com/ForeigndevX/pi/main/static/wolapp-notify-512.png" | head -5
```

Expect `HTTP/2 200` and `content-type: image/png`.

## iOS reminder

- `Icon` does not replace the ntfy app icon on iOS (Android only).
- `Attach` may show terminal art below the message when expanded.
- Home screen icon: Safari -> HUD -> **Add to Home Screen** (`apple-touch-icon.png`).

Repo: https://github.com/ForeigndevX/pi

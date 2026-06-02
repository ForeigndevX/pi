# Phone notifications (ntfy / iOS)

## Rich image (Attach header)

WOLAPP sends an optional **Attach** URL (see `NTFY_ATTACH_URL` in `.env`) so ntfy can show a large preview on the lock screen.

### Black square on iOS (attachment failed visually)

If the PNG is almost entirely **pure black** (`#000`), iOS often renders a **empty black box** on the right even when ntfy reports a successful attachment.

**Fix applied:** `static/wolapp-notify-512.png` was regenerated with a **#1a1a1a** background and **#ccc** `>_` terminal art (not a #000 fill). Regenerate with `scripts/gen_notify_icons.py` if you change branding.

**Server safeguard:** `app.py` validates the attach URL (HTTP 200, image content-type, at least 1KB) before sending **Attach**. If validation fails, the notification is sent **without Attach** (plain text only) so iOS does not show a broken image tile.

### Small icon still looks like the ntfy app

On many iOS builds, the **Icon** header does not replace the app icon in the notification list; you may still see the ntfy logo. That is an ntfy/iOS limitation, not a WOLAPP bug. **Attach** controls the large right-side preview when it works.

## Test

On the Pi (or with curl):

```bash
curl -s -X POST http://127.0.0.1:8080/api/notify/test
```

Manual ntfy test with attach:

```bash
curl -s -X POST \
  -H "Title: attach test" \
  -H "Attach: https://raw.githubusercontent.com/ForeigndevX/pi/main/static/wolapp-notify-512.png" \
  -d "test" \
  https://ntfy.sh/foreigndev_homecontrol
```

After updating icons on GitHub, wait ~2 minutes for CDN cache or push a new commit to refresh `raw.githubusercontent.com` etags.

## JPEG alternative

If a device still mis-renders PNG previews, host `wolapp-notify-512.jpg` (generated alongside PNG) and set `NTFY_ATTACH_URL` to the `.jpg` URL.

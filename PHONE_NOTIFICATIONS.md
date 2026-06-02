# Phone notifications for Home Control (WOLAPP)

The Pi pushes alerts when your PC goes online/offline, when a schedule fires, or when you tap **TEST NOTIFY** on the HUD.

## Terminal-style pushes (plain text)

All WOLAPP notifications use the same **plain-text terminal** layout (no markdown — ntfy on Android/iOS often shows `**` and `|` literally):

- **Title** — short uppercase, e.g. `MAIN PC >> ONLINE` or `WOLAPP >> TEST SIGNAL` (no emoji in title)
- **Body** — ASCII box lines, `SYSTEM OVERVIEW`, `>>` headline, dot leaders (`TIME ... 20:48:59`, `STAT ... ONLINE`)
- **Tap** — opens the dashboard via `Click` / `NTFY_CLICK_URL` only (no action buttons, no tag emojis)

We do **not** send `Markdown`, `Tags`, or `Actions` headers to ntfy. We **do** send optional `Icon` and `Attach` headers when `NTFY_ICON_URL` / `NTFY_ATTACH_URL` are set (see below).

Subscribe once to the private topic below; every alert type (test, PC up/down, schedule, wake) shares this format.

## iOS: why the icon stays ntfy (teal `>_`)

Per [ntfy documentation](https://docs.ntfy.sh/publish/#icons), the **`Icon` header only affects Android**. On **iOS**, the small icon on the left of each notification is always the **ntfy app icon** — there is no supported way to replace it per topic while using the ntfy iOS app.

| What you want | Works on iOS with ntfy? | What to do |
|---------------|-------------------------|------------|
| Replace lock-screen **app** icon | **No** | Use a different push app (e.g. **Pushover** with its own icon) or accept ntfy branding |
| Show terminal art **inside** the notification | **Sometimes** | Set `NTFY_ATTACH_URL` to a public HTTPS PNG (512×512); expand/long-press the notification — image appears **below** the text, not as the app icon |
| Terminal icon on **home screen** | **Yes** (not ntfy) | Safari → open HUD → Share → **Add to Home Screen** (`apple-touch-icon.png`) |

**Android:** set `NTFY_ICON_URL` (and optionally `NTFY_ATTACH_URL`) to public HTTPS URLs — see [static/ICON_HOSTING.md](static/ICON_HOSTING.md).

## Notification icon summary

| Platform | Small left icon | Custom image in notification |
|----------|-----------------|------------------------------|
| **Android (ntfy)** | Custom if `NTFY_ICON_URL` set | `NTFY_ATTACH_URL` in expanded view |
| **iOS (ntfy)** | Always ntfy app icon | `Attach` image below text when expanded (if URL set) |
| **Safari PWA** | N/A (not a push) | Home screen uses `/static/apple-touch-icon.png` |

Terminal-themed assets on the Pi at `/home/foreigndev/wolapp/static/`:

- `wolapp-icon.png` (192×192) — ntfy `Icon` (Android)
- `wolapp-notify-512.png` (512×512) — ntfy `Attach` (iOS expanded / Android rich)
- `apple-touch-icon.png` (180×180) — iOS home-screen shortcut
- `favicon.ico` — browser tab
- `manifest.webmanifest` — PWA name **WOLAPP**, black theme

### Enable custom ntfy images (Android + iOS Attach)

The Pi serves files at `http://<tailscale-ip>:8080/static/...`, but **ntfy’s servers must fetch from the public internet**. Full steps: **[static/ICON_HOSTING.md](static/ICON_HOSTING.md)**.

Quick `.env` example (after you have working HTTPS URLs):

```bash
NTFY_ICON_URL=https://raw.githubusercontent.com/ForeigndevX/pi/main/static/wolapp-icon.png
NTFY_ATTACH_URL=https://raw.githubusercontent.com/ForeigndevX/pi/main/static/wolapp-notify-512.png
```

Add to `/home/foreigndev/wolapp/.env`, then `sudo systemctl restart wolapp` and **TEST NOTIFY**.

**Current Pi note (Jun 2026):** `NTFY_ICON_URL` / `NTFY_ATTACH_URL` are unset until you publish icons — pushes still work; only custom imagery is missing.

## Recommended: ntfy (Android & iOS)

1. Install the **ntfy** app on your phone ([ntfy.sh](https://ntfy.sh) or F-Droid / App Store).
2. Create a **private topic** (long random name). Subscribe to it in the app.
   - **Pi (Jun 2026):** topic `foreigndev_homecontrol` — subscribe in ntfy to receive WOLAPP pushes.
3. On the Pi, edit `/home/foreigndev/wolapp/.env` and set:
   ```bash
   NTFY_URL=https://ntfy.sh/foreigndev_homecontrol  # example: Pi uses this topic
   ```
   (Use your self-hosted URL if you run your own ntfy server.)
4. Restart the service: `sudo systemctl restart wolapp`
5. Open the HUD → **SCHEDULE | CALENDAR** → **[ TEST NOTIFY ]**. You should see title **`WOLAPP >> TEST SIGNAL`** and plain body with `STAT ... LINK_OK` (no `**`, tables, or tag emojis).

Scheduled tasks with action **Notify** or **Both** send **`SCHEDULE >> EVENT`** (or **`SCHEDULE >> REMINDER`** for early alerts); wake actions also send **`WOLAPP >> WAKE SENT`**.

Each schedule can define **reminder offsets** (`notify_offsets_minutes`): minutes *before* the event when a push is sent (e.g. `10080` = 1 week, `1440` = 1 day, `120` = 2 hours, `0` = at event time). Pick offsets in the HUD under **\[ NOTIFY \]** when adding or editing an event. The Pi scheduler checks every 30s; reminder bodies include a **WHEN** line (`IN 1 WEEK`, `IN 2 HOURS`, `NOW`) plus category and optional **NOTE**. At event time, offset `0` fires the reminder (if selected) and the configured **action** (notify / wake / both) runs as before.

## Alternative: Pushover (iOS-friendly custom app icon)

The ntfy iOS app icon cannot be changed per notification. **Pushover** uses its own iOS app icon and supports custom notification sounds/images via their API.

Optional placeholders in `env.ntfy.example` (`PUSHOVER_USER_KEY`, `PUSHOVER_API_TOKEN`) — **not wired in app.py** unless you ask to enable it. Typical setup: Pushover app on iPhone + Pi script or bridge that POSTs to Pushover instead of (or in addition to) ntfy.

## Alternative: HTTP on your phone (Android)

If you run Tasker, MacroDroid, or a small HTTP listener on Tailscale:

1. Note your phone Tailscale IP: **100.123.104.48** (reachable from the Pi when both are on Tailscale).
2. Optional check from Pi: `ping -c 2 100.123.104.48`
3. Configure your app to accept `POST` JSON: `{"title":"...","message":"..."}`
4. Add to Pi `.env`:
   ```bash
   NOTIFY_PHONE_URL=http://100.123.104.48:8080/notify
   ```
   (Replace port/path with what your listener expects.)
5. Restart `wolapp` and use **TEST NOTIFY**.

You can use **both** `NTFY_URL` and `NOTIFY_PHONE_URL`; the Pi will try each.

## Data & deploy notes

- Schedules are stored at `/home/foreigndev/wolapp/data/schedules.json` (not in git).
- Add `data/` to deploy ignore / backup separately.
- In-app schedules are separate from systemd `wolapp-wake.timer` (no conflict).

## API access

POST routes (wake, shutdown, schedules, **TEST NOTIFY**, etc.) do **not** use HTTP Basic auth. Reach the dashboard only over **Tailscale** (same mesh as the Pi).

## Troubleshooting

| Symptom | Fix |
|--------|-----|
| TEST NOTIFY returns “not configured” | Set `NTFY_URL` in `.env`, restart wolapp |
| ntfy works on PC events but not test | Auth required — enter HUD credentials when prompted |
| No push on schedule | Check schedule enabled, datetime in the future (or past for one-shot), action includes Notify |
| Phone HTTP never fires | Listener must bind on Tailscale; firewall must allow Pi → phone |
| iPhone still shows ntfy teal icon | **Expected** — iOS ignores `Icon`; use `NTFY_ATTACH_URL` for expanded image or Safari **Add to Home Screen** for HUD icon |
| No image below text on iOS | Set `NTFY_ATTACH_URL` to public HTTPS 512 PNG; long-press notification |
| Android icon unchanged | Set `NTFY_ICON_URL` to **public HTTPS**; see ICON_HOSTING.md |
| Attach/Icon never appear | ntfy server cannot reach your URL — `curl -sI` the URL from the internet |

## Manual ntfy test (Icon + Attach)

From any machine (replace topic and URLs):

```bash
curl -d "WOLAPP manual icon test" \
  -H "Title: WOLAPP >> ICON TEST" \
  -H "Icon: https://YOUR_PUBLIC/wolapp-icon.png" \
  -H "Attach: https://YOUR_PUBLIC/wolapp-notify-512.png" \
  "https://ntfy.sh/YOUR_TOPIC"
```

On Android you should see the custom small icon when `Icon` is valid. On iOS expect ntfy app icon; expanded notification may show the `Attach` image.


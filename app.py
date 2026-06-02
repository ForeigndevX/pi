import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta
from functools import wraps
from zoneinfo import ZoneInfo

from flask import Flask, render_template, jsonify, request

from swedish_holidays import HOLIDAY_YEAR_MAX, HOLIDAY_YEAR_MIN, holidays_for_year

app = Flask(__name__)

# ----- Version & paths -----
WOLAPP_DIR = os.path.dirname(os.path.abspath(__file__))
VERSION_PATH = os.path.join(WOLAPP_DIR, "VERSION")
DEPLOY_TS_PATH = os.path.join(WOLAPP_DIR, "deploy_timestamp.txt")
MAC_CACHE_PATH = os.path.join(WOLAPP_DIR, "last_mac.cache")
IP_CACHE_PATH = os.path.join(WOLAPP_DIR, "last_ip.cache")
LAST_OFFLINE_CACHE = os.path.join(WOLAPP_DIR, "last_offline.cache")
LAST_ONLINE_CACHE = os.path.join(WOLAPP_DIR, "last_online.cache")

MONITORED_SERVICES = ("wolapp", "ttyd-pi", "ttyd-pc", "tailscaled")


def _load_dotenv():
    """Load .env into os.environ for local dev (systemd uses EnvironmentFile)."""
    env_path = os.path.join(WOLAPP_DIR, ".env")
    if not os.path.isfile(env_path):
        return
    with open(env_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = val


_load_dotenv()

PC_MAC_DEFAULT = os.environ.get("PC_MAC", "AA:BB:CC:11:22:33")
PC_IP_DEFAULT = os.environ.get("PC_IP", "192.168.88.100")
PC_USER = os.environ.get("PC_USER", "yourwindowsuser")
AUTH_USER = os.environ.get("AUTH_USER", "").strip()
AUTH_PASS = os.environ.get("AUTH_PASS", "").strip()
NTFY_URL = os.environ.get("NTFY_URL", "").strip()
NTFY_CLICK_URL = os.environ.get(
    "NTFY_CLICK_URL", "http://100.88.171.98:8080/"
).strip()
NTFY_ICON_URL = os.environ.get("NTFY_ICON_URL", "").strip()
NTFY_ATTACH_URL = os.environ.get("NTFY_ATTACH_URL", "").strip()
NOTIFY_PHONE_URL = os.environ.get("NOTIFY_PHONE_URL", "").strip()
NOTIFY_PHONE_NTFY_TOPIC = os.environ.get("NOTIFY_PHONE_NTFY_TOPIC", "").strip()

DATA_DIR = os.path.join(WOLAPP_DIR, "data")
SCHEDULES_PATH = os.path.join(DATA_DIR, "schedules.json")
DEVICES_PATH = os.path.join(DATA_DIR, "devices.json")
SCAN_COOLDOWN_SEC = 60
SCAN_PING_WORKERS = 24
STOCKHOLM = ZoneInfo("Europe/Stockholm")
SCHEDULE_REPEAT = frozenset((
    "none", "daily", "weekly", "monthly", "yearly", "weekdays", "weekends",
    "every_2_weeks", "every_3_months",
))
SCHEDULE_ACTIONS = frozenset(("notify", "wake", "both"))
SCHEDULE_CATEGORIES = frozenset((
    "general", "girlfriend", "family", "work", "health", "other",
))
NOTIFY_OFFSET_MAX = 8
NOTIFY_GRACE_SECONDS = 60

_cache_lock = threading.Lock()
_schedules_lock = threading.Lock()
_devices_lock = threading.Lock()
_scan_lock = threading.Lock()
_last_scan_ts = 0.0
_last_scan_results = []
_last_pc_online = {"value": None}
_presence_lock = threading.Lock()

# ----- MAC helpers -----
MAC_RE = re.compile(r"([0-9a-fA-F]{2}(?:[:-][0-9a-fA-F]{2}){5})")


def app_version():
    try:
        with open(VERSION_PATH, encoding="utf-8") as f:
            v = f.read().strip()
            if v:
                return v
    except OSError:
        pass
    try:
        return time.strftime("%Y%m%d", time.localtime(os.path.getmtime(__file__)))
    except OSError:
        return "unknown"


def deploy_timestamp():
    try:
        with open(DEPLOY_TS_PATH, encoding="utf-8") as f:
            return f.read().strip() or None
    except OSError:
        return None


def normalize_mac(raw):
    if not raw:
        return None
    m = MAC_RE.search(raw)
    if not m:
        return None
    s = m.group(1).lower().replace("-", ":")
    if s in ("00:00:00:00:00:00", "ff:ff:ff:ff:ff:ff"):
        return None
    if int(s.split(":")[0], 16) & 0x01:
        return None
    return s


def read_arp_mac(ip):
    try:
        r = subprocess.run(["ip", "neigh", "show", ip],
                           capture_output=True, text=True, timeout=2)
        line = r.stdout.strip()
        if not line:
            return None
        if "FAILED" in line or "INCOMPLETE" in line:
            return None
        return normalize_mac(line)
    except Exception:
        return None


def load_cached_mac():
    try:
        with open(MAC_CACHE_PATH, encoding="utf-8") as f:
            return normalize_mac(f.read().strip())
    except FileNotFoundError:
        return None
    except Exception:
        return None


def save_cached_mac(mac):
    tmp = MAC_CACHE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(mac)
    os.replace(tmp, MAC_CACHE_PATH)


def current_mac():
    primary = get_primary_device()
    if primary and primary.get("mac"):
        return primary["mac"]
    with _cache_lock:
        cached = load_cached_mac()
    if cached:
        return cached
    return normalize_mac(PC_MAC_DEFAULT) or PC_MAC_DEFAULT


def update_mac_if_changed(new_mac, source):
    nm = normalize_mac(new_mac)
    if not nm:
        return False, None
    with _cache_lock:
        old = load_cached_mac()
        if old == nm:
            return False, nm
        save_cached_mac(nm)
    print(f"[mac] {source}: {old} -> {nm}", flush=True)
    _sync_primary_device_record(mac=nm)
    return True, nm


# ----- IP helpers -----
def normalize_ip(raw):
    if not raw:
        return None
    try:
        ip = ipaddress.IPv4Address(raw.strip())
    except (ValueError, ipaddress.AddressValueError):
        return None
    if ip.is_loopback or ip.is_multicast or ip.is_unspecified or ip.is_link_local:
        return None
    return str(ip)


def load_cached_ip():
    try:
        with open(IP_CACHE_PATH, encoding="utf-8") as f:
            return normalize_ip(f.read().strip())
    except FileNotFoundError:
        return None
    except Exception:
        return None


def save_cached_ip(ip):
    tmp = IP_CACHE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(ip)
    os.replace(tmp, IP_CACHE_PATH)


def current_ip():
    primary = get_primary_device()
    if primary and primary.get("ip"):
        return primary["ip"]
    with _cache_lock:
        cached = load_cached_ip()
    if cached:
        return cached
    return normalize_ip(PC_IP_DEFAULT) or PC_IP_DEFAULT


def update_ip_if_changed(new_ip, source):
    ni = normalize_ip(new_ip)
    if not ni:
        return False, None
    with _cache_lock:
        old = load_cached_ip()
        if old == ni:
            return False, ni
        save_cached_ip(ni)
    print(f"[ip]  {source}: {old} -> {ni}", flush=True)
    _sync_primary_device_record(ip=ni)
    return True, ni


# ----- WoL devices (multi-host) -----
def _default_devices():
    return []


def _migrate_device_item(item):
    if not isinstance(item, dict):
        return item
    item.setdefault("wol_enabled", True)
    item.setdefault("ssh_shutdown", False)
    item.setdefault("notes", "")
    item.setdefault("last_seen", None)
    if "is_primary" not in item:
        item["is_primary"] = False
    mac = normalize_mac(item.get("mac"))
    if mac:
        item["mac"] = mac
    ip = normalize_ip(item.get("ip"))
    if ip:
        item["ip"] = ip
    name = (item.get("name") or "Device").strip()
    item["name"] = name[:80] or "Device"
    return item


def load_devices():
    _ensure_data_dir()
    if not os.path.isfile(DEVICES_PATH):
        return _migrate_devices_from_legacy()
    try:
        with open(DEVICES_PATH, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return [_migrate_device_item(x) for x in data if isinstance(x, dict)]
    except (OSError, json.JSONDecodeError) as e:
        print(f"[devices] load failed: {e}", flush=True)
    return _migrate_devices_from_legacy()


def save_devices(devices):
    _ensure_data_dir()
    tmp = DEVICES_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(devices, f, indent=2)
        f.write("\n")
    os.replace(tmp, DEVICES_PATH)


def _migrate_devices_from_legacy():
    """Build devices.json from caches / env when missing."""
    mac = load_cached_mac() or normalize_mac(PC_MAC_DEFAULT)
    ip = load_cached_ip() or normalize_ip(PC_IP_DEFAULT)
    if not mac:
        return _default_devices()
    dev = {
        "id": str(uuid.uuid4()),
        "name": "MAIN PC",
        "mac": mac,
        "ip": ip,
        "last_seen": None,
        "wol_enabled": True,
        "ssh_shutdown": True,
        "ssh_user": PC_USER or None,
        "notes": "Migrated from single-PC caches",
        "is_primary": True,
    }
    devices = [dev]
    try:
        save_devices(devices)
        print("[devices] created devices.json from legacy caches", flush=True)
    except OSError as e:
        print(f"[devices] migrate save failed: {e}", flush=True)
    return devices


def get_primary_device(devices=None):
    if devices is None:
        devices = load_devices()
    for d in devices:
        if d.get("is_primary"):
            return d
    return devices[0] if devices else None


def device_by_id(device_id, devices=None):
    if devices is None:
        devices = load_devices()
    for d in devices:
        if d.get("id") == device_id:
            return d
    return None


def _sync_primary_device_record(mac=None, ip=None):
    with _devices_lock:
        devices = load_devices()
        primary = get_primary_device(devices)
        if not primary:
            return
        changed = False
        if mac:
            nm = normalize_mac(mac)
            if nm and primary.get("mac") != nm:
                primary["mac"] = nm
                changed = True
        if ip:
            ni = normalize_ip(ip)
            if ni and primary.get("ip") != ni:
                primary["ip"] = ni
                changed = True
        if changed:
            save_devices(devices)


def _device_reachability(dev):
    ip = normalize_ip(dev.get("ip"))
    if not ip:
        return False, None
    return reachability(ip)


def _enrich_device_status(dev):
    online, latency = _device_reachability(dev)
    out = dict(dev)
    out["online"] = online
    out["latency_ms"] = latency
    if online:
        out["last_seen"] = int(time.time())
    return out


def _validate_device_payload(data, *, require_mac=False):
    if not isinstance(data, dict):
        return None, "invalid body"
    name = (data.get("name") or "").strip()
    if not name:
        return None, "name required"
    mac = normalize_mac(data.get("mac"))
    if require_mac and not mac:
        return None, "valid mac required"
    ip = normalize_ip(data.get("ip"))
    wol = data.get("wol_enabled", True)
    if not isinstance(wol, bool):
        wol = str(wol).lower() in ("1", "true", "yes", "on")
    ssh_shutdown = data.get("ssh_shutdown", False)
    if not isinstance(ssh_shutdown, bool):
        ssh_shutdown = str(ssh_shutdown).lower() in ("1", "true", "yes", "on")
    ssh_user = (data.get("ssh_user") or "").strip() or None
    notes = (data.get("notes") or "").strip()[:500]
    item = {
        "name": name[:80],
        "mac": mac,
        "ip": ip,
        "wol_enabled": wol,
        "ssh_shutdown": ssh_shutdown,
        "ssh_user": ssh_user,
        "notes": notes,
        "last_seen": data.get("last_seen"),
        "is_primary": bool(data.get("is_primary")),
    }
    return item, None


def _lan_default_route():
    try:
        r = subprocess.run(["ip", "route", "show", "default"],
                           capture_output=True, text=True, timeout=2)
        for line in r.stdout.splitlines():
            parts = line.split()
            if "dev" in parts:
                idx = parts.index("dev")
                iface = parts[idx + 1]
                gw = normalize_ip(parts[1]) if parts else None
                return iface, gw
    except Exception:
        pass
    return _primary_iface(), None


def _iface_ipv4_network(iface):
    try:
        r = subprocess.run(["ip", "-4", "addr", "show", "dev", iface],
                           capture_output=True, text=True, timeout=2)
        for line in r.stdout.splitlines():
            line = line.strip()
            if "inet " not in line:
                continue
            token = line.split()[1]
            net = ipaddress.IPv4Interface(token).network
            return net
    except Exception:
        pass
    return None


def _parse_ip_neigh(iface=None):
    hosts = {}
    try:
        cmd = ["ip", "neigh", "show"]
        if iface:
            cmd.extend(["dev", iface])
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        for line in r.stdout.splitlines():
            parts = line.split()
            if len(parts) < 4:
                continue
            ip = normalize_ip(parts[0])
            if not ip:
                continue
            state = parts[-1] if parts else ""
            if state in ("FAILED", "INCOMPLETE"):
                continue
            mac = None
            for i, p in enumerate(parts):
                if p == "lladdr" and i + 1 < len(parts):
                    mac = normalize_mac(parts[i + 1])
                    break
            if not mac:
                continue
            hosts[ip] = {
                "ip": ip,
                "mac": mac,
                "state": state,
                "hostname": None,
                "source": "neigh",
            }
    except Exception as e:
        print(f"[scan] ip neigh failed: {e}", flush=True)
    return hosts


def _ping_host(ip):
    try:
        r = subprocess.run(
            ["ping", "-c", "1", "-W", "1", ip],
            capture_output=True, text=True, timeout=2,
        )
        return r.returncode == 0
    except Exception:
        return False


def _resolve_hostname(ip):
    try:
        name, _, _ = socket.gethostbyaddr(ip)
        if name:
            return name.split(".")[0]
    except (socket.herror, socket.gaierror, OSError):
        pass
    return None


def scan_lan_hosts(*, do_ping_sweep=True):
    """Discover hosts on the Pi's LAN /24. Does not prove WoL capability."""
    iface, gateway = _lan_default_route()
    if not iface:
        return {"ok": False, "error": "no LAN interface"}, []
    net = _iface_ipv4_network(iface)
    if not net:
        return {"ok": False, "error": "could not determine subnet"}, []
    hosts = _parse_ip_neigh(iface)
    if do_ping_sweep and net.prefixlen <= 24:
        candidates = [str(h) for h in net.hosts()
                      if str(h) not in hosts and h != net.network_address]
        with ThreadPoolExecutor(max_workers=SCAN_PING_WORKERS) as pool:
            futures = {pool.submit(_ping_host, ip): ip for ip in candidates}
            for fut in as_completed(futures):
                ip = futures[fut]
                try:
                    if fut.result():
                        pass
                except Exception:
                    pass
        hosts.update(_parse_ip_neigh(iface))
    for ip, row in hosts.items():
        if not row.get("hostname"):
            row["hostname"] = _resolve_hostname(ip)
    saved_macs = {d.get("mac") for d in load_devices() if d.get("mac")}
    results = []
    for ip in sorted(hosts, key=lambda x: ipaddress.IPv4Address(x)):
        row = hosts[ip]
        results.append({
            "ip": row["ip"],
            "mac": row["mac"],
            "hostname": row.get("hostname"),
            "state": row.get("state"),
            "saved": row["mac"] in saved_macs,
            "wol_capable": None,
        })
    meta = {
        "ok": True,
        "iface": iface,
        "subnet": str(net),
        "gateway": gateway,
        "count": len(results),
        "arp_scan_available": bool(shutil.which("arp-scan")),
        "note": "Scan lists IP/MAC on LAN; WoL must be confirmed per device (BIOS + test wake).",
    }
    return meta, results


def wake_device_mac(mac):
    nm = normalize_mac(mac)
    if not nm:
        return False, None, "invalid mac"
    r = subprocess.run(["wakeonlan", nm], capture_output=True, text=True)
    ok = r.returncode == 0
    return ok, nm, r.stdout


# ----- Presence timestamps -----
def _read_ts_cache(path):
    try:
        with open(path, encoding="utf-8") as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def _write_ts_cache(path, ts):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(str(int(ts)))
    os.replace(tmp, path)



def _ntfy_priority_value(priority):
    """Map symbolic or numeric priority to ntfy 1–5 string."""
    if priority is None:
        return "3"
    if isinstance(priority, int):
        return str(max(1, min(5, priority)))
    s = str(priority).strip().lower()
    if s.isdigit():
        return str(max(1, min(5, int(s))))
    return {
        "min": "1",
        "low": "2",
        "default": "3",
        "high": "4",
        "max": "5",
        "urgent": "5",
    }.get(s, "3")


def _pi_hostname_label():
    try:
        return socket.gethostname().split(".")[0].upper()
    except OSError:
        return "PI"


def _iso_short():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _format_mac_display(mac):
    m = (mac or "").upper()
    if len(m) <= 17:
        return m
    return f"{m[:8]}…{m[-5:]}"


def _time_hms():
    return datetime.now().strftime("%H:%M:%S")


def _field_key(label):
    return re.sub(r"[^A-Z0-9]+", "_", (label or "").upper()).strip("_") or "FIELD"


def _terminal_plain_body(headline, detail_rows):
    """Lock-screen plain text (no markdown; ntfy mobile often ignores Markdown)."""
    host = _pi_hostname_label()
    bar = "━━━━━━━━━━━━━━━━"
    lines = [
        bar,
        " SYSTEM OVERVIEW",
        bar,
        f">> {headline}",
        "",
        f"TIME ... {_time_hms()}",
        f"HOST ... {host}",
    ]
    for label, value in detail_rows:
        lines.append(f"{_field_key(label)} ... {value}")
    lines.append(bar)
    return "\n".join(lines)


def _terminal_markdown_body(headline, detail_rows):
    """Plain terminal body (ntfy Markdown header is optional; body stays readable)."""
    return _terminal_plain_body(headline, detail_rows)


def _truncate_note(text, limit=80):
    s = (text or "").strip()
    if not s:
        return ""
    if len(s) <= limit:
        return s
    return s[:limit] + "…"


def format_terminal_notification(kind, **kwargs):
    """
    Build ntfy payload: title, message, priority, click (plain text only).
    kind: test | pc_online | pc_offline | schedule | wake | generic
    """
    click = kwargs.get("click") or NTFY_CLICK_URL or None
    ip = kwargs.get("ip") or current_ip()
    latency = kwargs.get("latency_ms")
    latency_s = f"{latency} ms" if latency is not None else "—"

    if kind == "test":
        return {
            "title": "WOLAPP >> TEST SIGNAL",
            "message": _terminal_plain_body(
                "TEST SIGNAL",
                [("STAT", "LINK_OK")],
            ),
            "priority": 3,
            "click": click,
        }

    if kind == "pc_online":
        return {
            "title": "MAIN PC >> ONLINE",
            "message": _terminal_plain_body(
                "PC STATUS CHANGE",
                [
                    ("STAT", "ONLINE"),
                    ("IP", ip),
                    ("LATENCY", latency_s),
                ],
            ),
            "priority": 3,
            "click": click,
        }

    if kind == "pc_offline":
        last_seen = kwargs.get("last_seen") or _iso_short()
        return {
            "title": "MAIN PC >> OFFLINE",
            "message": _terminal_plain_body(
                "PC STATUS CHANGE",
                [
                    ("STAT", "OFFLINE"),
                    ("IP", ip),
                    ("LAST SEEN", last_seen),
                ],
            ),
            "priority": 4,
            "click": click,
        }

    if kind == "schedule":
        sched_title = kwargs.get("schedule_title") or "Event"
        action = (kwargs.get("action") or "NOTIFY").upper()
        when_label = (kwargs.get("when_label") or "NOW").upper()
        rows = [
            ("WHEN", when_label),
            ("EVENT", sched_title),
        ]
        category = (kwargs.get("category") or "").strip().lower()
        if category and category != "general":
            rows.append(("CATEGORY", category.upper()))
        if action and kwargs.get("include_action", True):
            rows.append(("ACTION", action))
        note = _truncate_note(kwargs.get("description"))
        if note:
            rows.append(("NOTE", note))
        title_suffix = "REMINDER" if kwargs.get("is_reminder") else "EVENT"
        return {
            "title": f"SCHEDULE >> {title_suffix}",
            "message": _terminal_plain_body("SCHEDULED EVENT", rows),
            "priority": 3,
            "click": click,
        }

    if kind == "wake":
        mac = _format_mac_display(kwargs.get("mac") or current_mac())
        return {
            "title": "WOLAPP >> WAKE SENT",
            "message": _terminal_plain_body(
                "WAKE-ON-LAN",
                [
                    ("TARGET MAC", mac),
                    ("IP", ip),
                ],
            ),
            "priority": 3,
            "click": click,
        }

    # generic fallback
    title = kwargs.get("title") or "WOLAPP >> ALERT"
    headline = kwargs.get("headline") or "SYSTEM ALERT"
    detail = kwargs.get("detail") or kwargs.get("message") or "—"
    rows = kwargs.get("rows")
    if rows is None:
        rows = [("DETAIL", str(detail))]
    return {
        "title": title,
        "message": _terminal_plain_body(headline, rows),
        "priority": kwargs.get("priority", 3),
        "click": click,
    }


def send_ntfy_payload(payload):
    """POST plain-text terminal notification to NTFY_URL."""
    if not NTFY_URL:
        return False
    title = payload.get("title") or "WOLAPP >> ALERT"
    message = payload.get("message") or ""
    headers = {
        "Title": title,
        "Priority": _ntfy_priority_value(payload.get("priority", 3)),
    }
    click = payload.get("click")
    if click:
        headers["Click"] = click
    if NTFY_ICON_URL:
        headers["Icon"] = NTFY_ICON_URL
    attach_url = NTFY_ATTACH_URL or NTFY_ICON_URL
    if attach_url:
        headers["Attach"] = attach_url
    try:
        req = urllib.request.Request(
            NTFY_URL,
            data=message.encode("utf-8"),
            headers=headers,
            method="POST",
        )
        urllib.request.urlopen(req, timeout=8)
        return True
    except (urllib.error.URLError, OSError, ValueError) as e:
        print(f"[ntfy] notify failed: {e}", flush=True)
        return False


def _notify_phone_http(title, message):
    if not NOTIFY_PHONE_URL:
        return False
    try:
        payload = json.dumps({"title": title, "message": message}).encode("utf-8")
        req = urllib.request.Request(
            NOTIFY_PHONE_URL,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=8)
        return True
    except (urllib.error.URLError, OSError, ValueError) as e:
        print(f"[notify] phone HTTP failed: {e}", flush=True)
        return False


def push_terminal_notification(kind, **kwargs):
    """Format + send via ntfy and optional phone HTTP."""
    payload = format_terminal_notification(kind, **kwargs)
    ok = send_ntfy_payload(payload)
    if NOTIFY_PHONE_URL:
        _notify_phone_http(payload["title"], payload["message"])
    return ok


# ----- Schedules -----
def _ensure_data_dir():
    os.makedirs(DATA_DIR, mode=0o750, exist_ok=True)


def _default_schedules():
    return []


def _migrate_schedule_item(item):
    """Normalize legacy schedule records on load."""
    if not isinstance(item, dict):
        return item
    offsets = item.get("notify_offsets_minutes")
    if not isinstance(offsets, list) or not offsets:
        item["notify_offsets_minutes"] = [0]
    else:
        clean = []
        for o in offsets:
            try:
                n = int(o)
                if n >= 0:
                    clean.append(n)
            except (TypeError, ValueError):
                continue
        item["notify_offsets_minutes"] = sorted(set(clean), reverse=True) or [0]
    if len(item["notify_offsets_minutes"]) > NOTIFY_OFFSET_MAX:
        item["notify_offsets_minutes"] = item["notify_offsets_minutes"][:NOTIFY_OFFSET_MAX]
    cat = (item.get("category") or "general").strip().lower()
    item["category"] = cat if cat in SCHEDULE_CATEGORIES else "general"
    if not isinstance(item.get("last_notified"), dict):
        item["last_notified"] = {}
    return item


def load_schedules():
    _ensure_data_dir()
    if not os.path.isfile(SCHEDULES_PATH):
        return _default_schedules()
    try:
        with open(SCHEDULES_PATH, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return [_migrate_schedule_item(x) for x in data]
    except (OSError, json.JSONDecodeError) as e:
        print(f"[schedules] load failed: {e}", flush=True)
    return _default_schedules()


def save_schedules(schedules):
    _ensure_data_dir()
    tmp = SCHEDULES_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(schedules, f, indent=2)
        f.write("\n")
    os.replace(tmp, SCHEDULES_PATH)


def _now_local():
    return datetime.now(STOCKHOLM).replace(tzinfo=None, microsecond=0)


def _parse_schedule_dt(raw):
    if not raw or not isinstance(raw, str):
        return None
    s = raw.strip()
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is not None:
            dt = dt.astimezone().replace(tzinfo=None)
        return dt.replace(microsecond=0)
    except ValueError:
        return None


def _format_schedule_dt(dt):
    return dt.replace(microsecond=0).isoformat(timespec="seconds")


def _normalize_notify_offsets(raw):
    if not isinstance(raw, list) or not raw:
        return [0]
    clean = []
    for o in raw:
        try:
            n = int(o)
            if n >= 0:
                clean.append(n)
        except (TypeError, ValueError):
            continue
    if not clean:
        return None
    unique = sorted(set(clean), reverse=True)
    if len(unique) > NOTIFY_OFFSET_MAX:
        return None
    return unique


def _validate_schedule_item(data, require_id=False):
    if not isinstance(data, dict):
        return None, "invalid body"
    title = (data.get("title") or "").strip()
    if not title:
        return None, "title required"
    dt = _parse_schedule_dt(data.get("datetime"))
    if dt is None:
        return None, "invalid datetime"
    repeat = (data.get("repeat") or "none").strip().lower()
    if repeat not in SCHEDULE_REPEAT:
        return None, "invalid repeat"
    action = (data.get("action") or "notify").strip().lower()
    if action not in SCHEDULE_ACTIONS:
        return None, "invalid action"
    category = (data.get("category") or "general").strip().lower()
    if category not in SCHEDULE_CATEGORIES:
        return None, "invalid category"
    offsets = _normalize_notify_offsets(data.get("notify_offsets_minutes"))
    if offsets is None:
        return None, "invalid notify_offsets_minutes (need 1–8 non-negative ints)"
    enabled = data.get("enabled", True)
    if not isinstance(enabled, bool):
        enabled = str(enabled).lower() in ("1", "true", "yes", "on")
    description = (data.get("description") or "").strip()
    if len(description) > 500:
        description = description[:500]
    last_notified = data.get("last_notified")
    if not isinstance(last_notified, dict):
        last_notified = {}
    item = {
        "title": title[:200],
        "datetime": _format_schedule_dt(dt),
        "repeat": repeat,
        "action": action,
        "category": category,
        "enabled": enabled,
        "description": description,
        "notify_offsets_minutes": offsets,
        "last_run": data.get("last_run"),
        "last_notified": last_notified,
    }
    if require_id:
        sid = (data.get("id") or "").strip()
        if not sid:
            sid = str(uuid.uuid4())
        item["id"] = sid
    return item, None


def _add_months(dt, months):
    import calendar
    y = dt.year + (dt.month - 1 + months) // 12
    m = (dt.month - 1 + months) % 12 + 1
    last = calendar.monthrange(y, m)[1]
    d = min(dt.day, last)
    return dt.replace(year=y, month=m, day=d)


def _advance_repeat(dt, repeat):
    if repeat == "daily":
        return dt + timedelta(days=1)
    if repeat == "weekly":
        return dt + timedelta(weeks=1)
    if repeat == "every_2_weeks":
        return dt + timedelta(weeks=2)
    if repeat == "monthly":
        return _add_months(dt, 1)
    if repeat == "every_3_months":
        return _add_months(dt, 3)
    if repeat == "yearly":
        try:
            return dt.replace(year=dt.year + 1)
        except ValueError:
            return dt.replace(year=dt.year + 1, month=2, day=28)
    if repeat == "weekdays":
        nxt = dt + timedelta(days=1)
        while nxt.weekday() >= 5:
            nxt += timedelta(days=1)
        return nxt
    if repeat == "weekends":
        nxt = dt + timedelta(days=1)
        while nxt.weekday() < 5:
            nxt += timedelta(days=1)
        return nxt
    return None


def _when_label_for_offset(minutes):
    if minutes <= 0:
        return "NOW"
    if minutes < 60:
        return f"IN {minutes} MIN"
    if minutes < 1440:
        h = minutes // 60
        return f"IN {h} HOUR" if h == 1 else f"IN {h} HOURS"
    if minutes < 10080:
        d = minutes // 1440
        return f"IN {d} DAY" if d == 1 else f"IN {d} DAYS"
    if minutes < 43200:
        w = minutes // 10080
        return f"IN {w} WEEK" if w == 1 else f"IN {w} WEEKS"
    if minutes == 43200:
        return "IN 1 MONTH"
    d = minutes // 1440
    return f"IN {d} DAYS"


def _last_notified_key(occurrence_key, offset):
    return f"{occurrence_key}:{offset}"


def _occurrence_key(schedule_id, event_dt):
    return f"{schedule_id}:{_format_schedule_dt(event_dt)}"


def _was_notified(item, occurrence_key, offset):
    ln = item.get("last_notified") or {}
    return ln.get(_last_notified_key(occurrence_key, offset)) is not None


def _mark_notified(item, occurrence_key, offset, when=None):
    if "last_notified" not in item or not isinstance(item["last_notified"], dict):
        item["last_notified"] = {}
    ts = when or _now_local()
    item["last_notified"][_last_notified_key(occurrence_key, offset)] = _format_schedule_dt(ts)
    if len(item["last_notified"]) > 48:
        sorted_keys = sorted(item["last_notified"], key=lambda k: item["last_notified"][k])
        for k in sorted_keys[: len(item["last_notified"]) - 40]:
            item["last_notified"].pop(k, None)


def _push_schedule_reminder(title, action, description, category, offset_minutes, *, include_action):
    push_terminal_notification(
        "schedule",
        schedule_title=title,
        action=_schedule_action_label(action),
        description=description,
        category=category,
        when_label=_when_label_for_offset(offset_minutes),
        is_reminder=offset_minutes > 0,
        include_action=include_action,
    )


def _schedule_action_label(action):
    return {"notify": "NOTIFY", "wake": "WAKE", "both": "NOTIFY+WAKE"}.get(
        action, (action or "NOTIFY").upper()
    )


def _run_schedule_action(action, title, description="", *, category="general", skip_notify=False):
    if not skip_notify and action in ("notify", "both"):
        push_terminal_notification(
            "schedule",
            schedule_title=title,
            action=_schedule_action_label(action),
            description=description,
            category=category,
            when_label="NOW",
            is_reminder=False,
            include_action=True,
        )
    if action in ("wake", "both"):
        mac = current_mac()
        r = subprocess.run(["wakeonlan", mac], capture_output=True, text=True)
        if r.returncode != 0:
            print(f"[scheduler] wake failed for {title}: {r.stderr}", flush=True)
        else:
            push_terminal_notification("wake", mac=mac)


def _notify_window_open(now, notify_at, event_dt):
    """True if we should fire a pre-event reminder now."""
    if now < notify_at:
        return False
    if now < event_dt:
        return True
    return (now - notify_at).total_seconds() <= NOTIFY_GRACE_SECONDS


def tick_schedules():
    now = _now_local()
    with _schedules_lock:
        schedules = load_schedules()
        changed = False
        for item in schedules:
            if not item.get("enabled", True):
                continue
            due = _parse_schedule_dt(item.get("datetime"))
            if due is None:
                continue
            repeat = item.get("repeat") or "none"
            offsets = item.get("notify_offsets_minutes") or [0]
            title = item.get("title") or "Schedule"
            action = item.get("action") or "notify"
            category = item.get("category") or "general"
            description = (item.get("description") or "").strip()
            occ_key = _occurrence_key(item.get("id") or "", due)

            # Pre-event reminders (offsets > 0, or offset 0 before event time)
            for offset in offsets:
                try:
                    off = int(offset)
                except (TypeError, ValueError):
                    continue
                notify_at = due - timedelta(minutes=off)
                if off == 0:
                    continue
                if not _notify_window_open(now, notify_at, due):
                    continue
                if _was_notified(item, occ_key, off):
                    continue
                print(
                    f"[scheduler] reminder {title!r} offset={off}m occ={due.isoformat()}",
                    flush=True,
                )
                _push_schedule_reminder(
                    title, action, description, category, off, include_action=False,
                )
                _mark_notified(item, occ_key, off, now)
                changed = True

            # Event time: offset 0 notify + action
            if due > now:
                continue
            last_run = item.get("last_run")
            if last_run:
                try:
                    lr = datetime.fromisoformat(str(last_run).replace("Z", "+00:00"))
                    if lr.tzinfo is not None:
                        lr = lr.astimezone(STOCKHOLM).replace(tzinfo=None)
                    if lr >= due:
                        continue
                except ValueError:
                    pass

            log_extra = f" note={description[:40]!r}" if description else ""
            print(f"[scheduler] firing {title!r} ({action}){log_extra}", flush=True)

            if 0 in offsets and not _was_notified(item, occ_key, 0):
                _push_schedule_reminder(
                    title, action, description, category, 0, include_action=True,
                )
                _mark_notified(item, occ_key, 0, now)

            _run_schedule_action(
                action, title, description,
                category=category,
                skip_notify=(0 in offsets),
            )
            item["last_run"] = _format_schedule_dt(now)
            changed = True
            if repeat == "none":
                item["enabled"] = False
            else:
                nxt = _advance_repeat(due, repeat)
                while nxt and nxt <= now:
                    nxt = _advance_repeat(nxt, repeat)
                if nxt:
                    item["datetime"] = _format_schedule_dt(nxt)
        if changed:
            save_schedules(schedules)


def _scheduler_loop():
    while True:
        try:
            tick_schedules()
        except Exception as e:
            print(f"[scheduler] tick error: {e}", flush=True)
        time.sleep(30)


_scheduler_started = False


def start_scheduler():
    global _scheduler_started
    if _scheduler_started:
        return
    _scheduler_started = True
    t = threading.Thread(target=_scheduler_loop, name="wolapp-scheduler", daemon=True)
    t.start()
    print("[scheduler] background thread started (30s interval)", flush=True)


start_scheduler()


def update_presence_timestamps(online, ip=None, latency_ms=None):
    now = time.time()
    host_ip = ip or current_ip()
    with _presence_lock:
        prev = _last_pc_online["value"]
        _last_pc_online["value"] = online
        if prev is None:
            return
        if prev and not online:
            _write_ts_cache(LAST_OFFLINE_CACHE, now)
            print("[presence] PC went offline", flush=True)
            last_seen = datetime.fromtimestamp(now).strftime("%Y-%m-%d %H:%M:%S")
            push_terminal_notification(
                "pc_offline",
                ip=host_ip,
                last_seen=last_seen,
            )
        elif not prev and online:
            _write_ts_cache(LAST_ONLINE_CACHE, now)
            print("[presence] PC came online", flush=True)
            push_terminal_notification(
                "pc_online",
                ip=host_ip,
                latency_ms=latency_ms,
            )


def presence_info(online):
    last_offline = _read_ts_cache(LAST_OFFLINE_CACHE)
    last_online = _read_ts_cache(LAST_ONLINE_CACHE)
    out = {"last_offline_ts": last_offline, "last_online_ts": last_online}
    if not online and last_offline:
        out["last_seen_ts"] = last_offline
    if online and last_online:
        out["online_since_ts"] = last_online
    return out


# ----- Auth (disabled; reach Pi via Tailscale only) -----
def auth_enabled():
    return False


def require_auth(f):
    return f


# ----- System helpers -----
def tcp_probe_ms(host, port, timeout=0.3):
    start = time.perf_counter()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return (time.perf_counter() - start) * 1000.0
    except (OSError, socket.timeout):
        return None


def icmp_ping_ms(host):
    try:
        r = subprocess.run(["ping", "-c", "1", "-W", "1", host],
                           capture_output=True, text=True, timeout=2)
        if r.returncode != 0:
            return None
        m = re.search(r"time=([\d.]+)\s*ms", r.stdout)
        if m:
            return float(m.group(1))
    except Exception:
        return None
    return None


def reachability(host):
    """Slim probe: SMB 445, SSH 22, then ping. Ping-first if last state was offline."""
    with _presence_lock:
        was_offline = _last_pc_online["value"] is False

    if was_offline:
        p = icmp_ping_ms(host)
        if p is not None:
            return True, round(p, 1)

    for port in (445, 22):
        ms = tcp_probe_ms(host, port)
        if ms is not None:
            return True, round(ms, 1)

    if not was_offline:
        p = icmp_ping_ms(host)
        if p is not None:
            return True, round(p, 1)
    return False, None


def cpu_temp():
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            return round(int(f.read().strip()) / 1000, 1)
    except Exception:
        return None


def uptime():
    with open("/proc/uptime") as f:
        secs = float(f.read().split()[0])
    d, r = divmod(int(secs), 86400)
    h, r = divmod(r, 3600)
    m, _ = divmod(r, 60)
    return f"{d}d {h}h {m}m"


def uptime_seconds():
    with open("/proc/uptime") as f:
        return int(float(f.read().split()[0]))


def mem_usage():
    info = {}
    with open("/proc/meminfo") as f:
        for line in f:
            k, v = line.split(":", 1)
            info[k] = int(v.strip().split()[0])
    used = info["MemTotal"] - info["MemAvailable"]
    return round(used / info["MemTotal"] * 100, 1)


def load_avg():
    return os.getloadavg()[0]


def disk_pct(path="/"):
    try:
        u = shutil.disk_usage(path)
        return round(100 * u.used / u.total, 1)
    except Exception:
        return None


def swap_pct():
    try:
        info = {}
        with open("/proc/meminfo") as f:
            for line in f:
                k, v = line.split(":", 1)
                info[k] = int(v.strip().split()[0])
        total = info.get("SwapTotal", 0)
        if total == 0:
            return None
        free = info.get("SwapFree", 0)
        return round(100 * (total - free) / total, 1)
    except Exception:
        return None


def throttled_info():
    """Returns bool throttled_now and raw hex from vcgencmd if available."""
    try:
        r = subprocess.run(["vcgencmd", "get_throttled"],
                           capture_output=True, text=True, timeout=1)
        if r.returncode != 0:
            return {"throttled": False, "raw": None}
        m = re.search(r"0x([0-9a-fA-F]+)", r.stdout)
        raw = int(m.group(1), 16) if m else 0
        # Any current throttle bit in low byte
        now = bool(raw & 0xF)
        return {"throttled": now, "raw": f"0x{raw:x}"}
    except Exception:
        return {"throttled": False, "raw": None}


def wifi_info():
    try:
        with open("/proc/net/wireless") as f:
            lines = f.readlines()
        for line in lines[2:]:
            parts = line.split()
            if not parts:
                continue
            iface = parts[0].rstrip(":")
            try:
                level = int(float(parts[3].rstrip(".")))
            except (ValueError, IndexError):
                continue
            ssid = None
            try:
                r = subprocess.run(["iwgetid", "-r", iface],
                                   capture_output=True, text=True, timeout=1)
                if r.returncode == 0:
                    ssid = r.stdout.strip() or None
            except Exception:
                pass
            return {"iface": iface, "signal_dbm": level, "ssid": ssid}
        return None
    except Exception:
        return None


_net_sample = {"ts": 0.0, "rx": 0, "tx": 0, "iface": None}
_net_sample_lock = threading.Lock()


def _primary_iface():
    try:
        with open("/proc/net/dev") as f:
            for line in f.readlines()[2:]:
                parts = line.split()
                if not parts:
                    continue
                name = parts[0].rstrip(":")
                if name == "lo" or name.startswith(("docker", "tail", "br-", "veth")):
                    continue
                if int(parts[1]) > 0:
                    return name
    except Exception:
        pass
    return None


def net_throughput():
    global _net_sample
    iface = _net_sample.get("iface") or _primary_iface()
    if not iface:
        return None
    try:
        with open(f"/sys/class/net/{iface}/statistics/rx_bytes") as f:
            rx = int(f.read())
        with open(f"/sys/class/net/{iface}/statistics/tx_bytes") as f:
            tx = int(f.read())
    except Exception:
        return None
    now = time.time()
    with _net_sample_lock:
        last = dict(_net_sample)
        _net_sample = {"ts": now, "rx": rx, "tx": tx, "iface": iface}
    if last["ts"] == 0 or (now - last["ts"]) < 0.5:
        return {"iface": iface, "rx_bps": None, "tx_bps": None}
    dt = now - last["ts"]
    return {
        "iface": iface,
        "rx_bps": max(0, int((rx - last["rx"]) / dt)),
        "tx_bps": max(0, int((tx - last["tx"]) / dt)),
    }


_ts_cache = {"data": None, "ts": 0.0}
_ts_cache_lock = threading.Lock()


def _tailscale_daemon_active():
    try:
        r = subprocess.run(["systemctl", "is-active", "tailscaled"],
                           capture_output=True, text=True, timeout=2)
        return r.stdout.strip() == "active"
    except Exception:
        return False


def _parse_tailscale_status_json(data):
    """Normalize tailscale status --json across CLI versions."""
    if not isinstance(data, dict):
        return None
    self_node = data.get("Self") or data.get("self") or {}
    if not isinstance(self_node, dict):
        self_node = {}
    peers = data.get("Peer") or data.get("Peers") or data.get("peer") or {}
    if not isinstance(peers, dict):
        peers = {}
    online = 0
    for p in peers.values():
        if not isinstance(p, dict):
            continue
        if p.get("Online") or p.get("online"):
            online += 1
    self_ips = self_node.get("TailscaleIPs") or self_node.get("tailscaleIPs") or []
    if not self_ips:
        addrs = self_node.get("Addresses") or []
        self_ips = [a.split("/")[0] for a in addrs if isinstance(a, str) and "/" in a]
    backend = (data.get("BackendState") or data.get("backendState") or "").strip()
    running = backend.lower() == "running"
    if not running and backend.lower() in ("", "unknown") and _tailscale_daemon_active():
        running = True
    return {
        "running": running,
        "self_ip": self_ips[0] if self_ips else None,
        "peers_total": len(peers),
        "peers_online": online,
        "backend_state": backend or None,
    }


def tailscale_info():
    now = time.time()
    with _ts_cache_lock:
        cached = _ts_cache["data"]
        age = now - _ts_cache["ts"]
        if cached is not None and _ts_cache["ts"] > 0 and age < 10:
            return cached
        if cached is None and _ts_cache["ts"] > 0 and age < 3:
            return None
    info = None
    try:
        r = subprocess.run(["tailscale", "status", "--json"],
                           capture_output=True, text=True, timeout=3)
        if r.returncode == 0 and r.stdout.strip():
            info = _parse_tailscale_status_json(json.loads(r.stdout))
    except Exception as e:
        print(f"[tailscale] status failed: {e}", flush=True)
    if info is None and _tailscale_daemon_active():
        info = {
            "running": True,
            "self_ip": None,
            "peers_total": 0,
            "peers_online": 0,
            "backend_state": "daemon-active",
        }
    with _ts_cache_lock:
        _ts_cache["data"] = info
        _ts_cache["ts"] = now
    return info


_svc_cache = {"data": None, "ts": 0.0}
_svc_cache_lock = threading.Lock()


def systemd_services_state():
    now = time.time()
    with _svc_cache_lock:
        if _svc_cache["ts"] > 0 and (now - _svc_cache["ts"]) < 30:
            return _svc_cache["data"]
    states = {}
    for unit in MONITORED_SERVICES:
        try:
            r = subprocess.run(["systemctl", "is-active", unit],
                               capture_output=True, text=True, timeout=2)
            states[unit] = (r.stdout.strip() or "unknown")
        except Exception:
            states[unit] = "unknown"
    with _svc_cache_lock:
        _svc_cache["data"] = states
        _svc_cache["ts"] = now
    return states


def build_status_payload():
    ip = current_ip()
    pc_online, pc_latency = reachability(ip)
    update_presence_timestamps(pc_online, ip=ip, latency_ms=pc_latency)
    learned = read_arp_mac(ip)
    if learned:
        update_mac_if_changed(learned, "arp")
    primary = get_primary_device()
    thr = throttled_info()
    return {
        "pc_online": pc_online,
        "pc_latency_ms": pc_latency,
        "pc_mac": current_mac(),
        "pc_ip": ip,
        "primary_device_id": primary.get("id") if primary else None,
        "presence": presence_info(pc_online),
        "pi": {
            "temp": cpu_temp(),
            "uptime": uptime(),
            "memory": mem_usage(),
            "disk": disk_pct(),
            "swap": swap_pct(),
            "load": round(load_avg(), 2),
            "wifi": wifi_info(),
            "net": net_throughput(),
        },
        "tailscale": tailscale_info(),
        "services": systemd_services_state(),
        "meta": {
            "version": app_version(),
            "deployed_at": deploy_timestamp(),
            "python": sys.version.split()[0],
            "auth_required": auth_enabled(),
        },
        "ts": int(time.time()),
    }


def ssh_pc_command(remote_args):
    ip = current_ip()
    cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
           f"{PC_USER}@{ip}"] + remote_args
    return subprocess.run(cmd, capture_output=True, text=True, timeout=15), ip


# ----- Routes -----
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/status")
def status():
    return jsonify(build_status_payload())


@app.route("/api/health")
def health():
    thr = throttled_info()
    swap = swap_pct()
    return jsonify({
        "ok": True,
        "uptime": uptime_seconds(),
        "swap_percent": swap,
        "throttled": thr["throttled"],
        "version": app_version(),
        "ts": int(time.time()),
    })


@app.route("/api/wake", methods=["POST"])
@require_auth
def wake():
    data = request.get_json(silent=True) or {}
    device_id = (data.get("device_id") or request.args.get("device_id") or "").strip()
    mac = None
    if device_id:
        dev = device_by_id(device_id)
        if not dev:
            return jsonify({"ok": False, "error": "device not found"}), 404
        if not dev.get("wol_enabled", True):
            return jsonify({"ok": False, "error": "WoL disabled for device"}), 400
        mac = dev.get("mac")
    else:
        mac = current_mac()
    ok, nm, out = wake_device_mac(mac)
    if ok:
        tip = current_ip()
        if device_id:
            dev = device_by_id(device_id)
            if dev and dev.get("ip"):
                tip = dev["ip"]
        push_terminal_notification("wake", mac=nm, ip=tip)
    return jsonify({"ok": ok, "mac": nm, "device_id": device_id or None, "out": out})


@app.route("/api/devices", methods=["GET"])
def list_devices():
    merge_scan = request.args.get("merge_scan", "0").strip().lower() in ("1", "true", "yes")
    with _devices_lock:
        devices = [_enrich_device_status(d) for d in load_devices()]
    out = {"ok": True, "devices": devices, "primary_device_id": (
        get_primary_device(devices).get("id") if devices else None
    )}
    if merge_scan:
        global _last_scan_results
        with _scan_lock:
            out["last_scan"] = list(_last_scan_results)
    return jsonify(out)


@app.route("/api/devices", methods=["POST"])
@require_auth
def create_device():
    data = request.get_json(silent=True) or {}
    item, err = _validate_device_payload(data, require_mac=True)
    if err:
        return jsonify({"ok": False, "error": err}), 400
    item["id"] = str(uuid.uuid4())
    if item.get("is_primary"):
        item["ssh_shutdown"] = item.get("ssh_shutdown", True)
    with _devices_lock:
        devices = load_devices()
        if item.get("is_primary"):
            for d in devices:
                d["is_primary"] = False
        elif not devices:
            item["is_primary"] = True
        devices.append(item)
        save_devices(devices)
        if item.get("is_primary"):
            if item.get("mac"):
                update_mac_if_changed(item["mac"], "devices")
            if item.get("ip"):
                update_ip_if_changed(item["ip"], "devices")
    return jsonify({"ok": True, "device": item}), 201


@app.route("/api/devices/<device_id>", methods=["PUT"])
@require_auth
def update_device(device_id):
    data = request.get_json(silent=True) or {}
    with _devices_lock:
        devices = load_devices()
        for i, existing in enumerate(devices):
            if existing.get("id") != device_id:
                continue
            merged = {**existing, **data}
            item, err = _validate_device_payload(merged, require_mac=bool(existing.get("mac")))
            if err:
                return jsonify({"ok": False, "error": err}), 400
            item["id"] = device_id
            if item.get("is_primary"):
                for d in devices:
                    d["is_primary"] = d.get("id") == device_id
            item["is_primary"] = item.get("is_primary") or existing.get("is_primary")
            devices[i] = item
            save_devices(devices)
            if item.get("is_primary"):
                if item.get("mac"):
                    update_mac_if_changed(item["mac"], "devices")
                if item.get("ip"):
                    update_ip_if_changed(item["ip"], "devices")
            return jsonify({"ok": True, "device": item})
    return jsonify({"ok": False, "error": "not found"}), 404


@app.route("/api/devices/<device_id>", methods=["DELETE"])
@require_auth
def delete_device(device_id):
    with _devices_lock:
        devices = load_devices()
        target = device_by_id(device_id, devices)
        if not target:
            return jsonify({"ok": False, "error": "not found"}), 404
        if target.get("is_primary") and len(devices) <= 1:
            return jsonify({"ok": False, "error": "cannot delete the only primary device"}), 400
        new_list = [d for d in devices if d.get("id") != device_id]
        if target.get("is_primary") and new_list:
            new_list[0]["is_primary"] = True
        save_devices(new_list)
    return jsonify({"ok": True})


@app.route("/api/devices/scan", methods=["POST"])
@require_auth
def scan_devices():
    global _last_scan_ts, _last_scan_results
    now = time.time()
    with _scan_lock:
        if _last_scan_ts > 0 and (now - _last_scan_ts) < SCAN_COOLDOWN_SEC:
            wait = int(SCAN_COOLDOWN_SEC - (now - _last_scan_ts))
            return jsonify({
                "ok": False,
                "error": "rate limited",
                "retry_after_sec": max(1, wait),
            }), 429
        _last_scan_ts = now
    do_ping = True
    data = request.get_json(silent=True) or {}
    if str(data.get("ping_sweep", "1")).lower() in ("0", "false", "no"):
        do_ping = False
    meta, results = scan_lan_hosts(do_ping_sweep=do_ping)
    if not meta.get("ok"):
        return jsonify(meta), 400
    with _scan_lock:
        _last_scan_results = results
    meta["hosts"] = results
    return jsonify(meta)


@app.route("/api/devices/<device_id>/wake", methods=["POST"])
@require_auth
def wake_device(device_id):
    dev = device_by_id(device_id)
    if not dev:
        return jsonify({"ok": False, "error": "not found"}), 404
    if not dev.get("wol_enabled", True):
        return jsonify({"ok": False, "error": "WoL disabled for device"}), 400
    ok, mac, out = wake_device_mac(dev.get("mac"))
    if ok:
        push_terminal_notification("wake", mac=mac, ip=dev.get("ip"))
    return jsonify({"ok": ok, "mac": mac, "device_id": device_id, "out": out})


@app.route("/api/shutdown", methods=["POST"])
@require_auth
def shutdown_pc():
    r, ip = ssh_pc_command(["shutdown", "/s", "/t", "0"])
    return jsonify({"ok": r.returncode == 0, "ip": ip,
                    "out": r.stdout, "err": r.stderr})


@app.route("/api/restart", methods=["POST"])
@require_auth
def restart_pc():
    r, ip = ssh_pc_command(["shutdown", "/r", "/t", "0"])
    return jsonify({"ok": r.returncode == 0, "ip": ip,
                    "out": r.stdout, "err": r.stderr})


@app.route("/api/update-pc", methods=["POST"])
@require_auth
def update_pc_endpoint():
    data = request.get_json(silent=True) or {}
    raw_mac = data.get("mac") or request.form.get("mac") or request.args.get("mac")
    raw_ip = data.get("ip") or request.form.get("ip") or request.args.get("ip")

    if not raw_mac and not raw_ip:
        return jsonify({"ok": False, "error": "need mac and/or ip"}), 400

    out = {"ok": True}
    if raw_mac:
        nm = normalize_mac(raw_mac)
        if not nm:
            return jsonify({"ok": False, "error": f"invalid mac: {raw_mac}"}), 400
        changed, mac = update_mac_if_changed(nm, "push")
        out["mac"] = mac
        out["mac_changed"] = changed
    if raw_ip:
        ni = normalize_ip(raw_ip)
        if not ni:
            return jsonify({"ok": False, "error": f"invalid ip: {raw_ip}"}), 400
        changed, ip = update_ip_if_changed(ni, "push")
        out["ip"] = ip
        out["ip_changed"] = changed
    return jsonify(out)


@app.route("/api/holidays")
def list_holidays():
    try:
        year = int(request.args.get("year", datetime.now(STOCKHOLM).year))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "invalid year"}), 400
    if year < HOLIDAY_YEAR_MIN or year > HOLIDAY_YEAR_MAX:
        return jsonify({
            "ok": False,
            "error": f"year must be {HOLIDAY_YEAR_MIN}–{HOLIDAY_YEAR_MAX}",
        }), 400
    include_optional = request.args.get("include_optional", "1").strip().lower() not in (
        "0", "false", "no",
    )
    holidays = []
    for h in holidays_for_year(year):
        if not include_optional and not h.get("public", True):
            continue
        holidays.append({
            "date": h["date"],
            "name_sv": h["name_sv"],
            "name_en": h["name_en"],
            "optional": not h.get("public", True),
        })
    return jsonify({"ok": True, "year": year, "holidays": holidays})


@app.route("/api/schedules", methods=["GET"])
def list_schedules():
    with _schedules_lock:
        return jsonify({"ok": True, "schedules": load_schedules()})


@app.route("/api/schedules", methods=["POST"])
@require_auth
def create_schedule():
    data = request.get_json(silent=True) or {}
    item, err = _validate_schedule_item(data)
    if err:
        return jsonify({"ok": False, "error": err}), 400
    item["id"] = str(uuid.uuid4())
    item["last_run"] = None
    item.setdefault("last_notified", {})
    with _schedules_lock:
        schedules = load_schedules()
        schedules.append(item)
        save_schedules(schedules)
    return jsonify({"ok": True, "schedule": item}), 201


@app.route("/api/schedules/<schedule_id>", methods=["PUT"])
@require_auth
def update_schedule(schedule_id):
    data = request.get_json(silent=True) or {}
    with _schedules_lock:
        schedules = load_schedules()
        for i, existing in enumerate(schedules):
            if existing.get("id") != schedule_id:
                continue
            item, err = _validate_schedule_item({**existing, **data})
            if err:
                return jsonify({"ok": False, "error": err}), 400
            item["id"] = schedule_id
            if "last_run" in data:
                item["last_run"] = data.get("last_run")
            elif "last_run" in existing:
                item["last_run"] = existing.get("last_run")
            if "last_notified" in data:
                item["last_notified"] = data.get("last_notified") or {}
            elif "last_notified" in existing:
                item["last_notified"] = existing.get("last_notified") or {}
            schedules[i] = item
            save_schedules(schedules)
            return jsonify({"ok": True, "schedule": item})
    return jsonify({"ok": False, "error": "not found"}), 404


@app.route("/api/schedules/<schedule_id>", methods=["DELETE"])
@require_auth
def delete_schedule(schedule_id):
    with _schedules_lock:
        schedules = load_schedules()
        new_list = [s for s in schedules if s.get("id") != schedule_id]
        if len(new_list) == len(schedules):
            return jsonify({"ok": False, "error": "not found"}), 404
        save_schedules(new_list)
    return jsonify({"ok": True})


@app.route("/api/notify/test", methods=["POST"])
@require_auth
def notify_test():
    if not NTFY_URL and not NOTIFY_PHONE_URL:
        return jsonify({
            "ok": False,
            "error": "no notifier configured",
            "hint": "Set NTFY_URL in .env (see env.ntfy.example)",
        }), 503
    sent = push_terminal_notification("test")
    sample = format_terminal_notification("test")
    return jsonify({
        "ok": sent,
        "ntfy_configured": bool(NTFY_URL),
        "phone_url_configured": bool(NOTIFY_PHONE_URL),
        "preview": {"title": sample["title"], "message": sample["message"]},
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, threaded=True)

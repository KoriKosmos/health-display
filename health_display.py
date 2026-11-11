#!/usr/bin/env python3
"""Health Display: Pulsoid heart rate -> Discord Rich Presence (official local IPC, no user token).

Runs on the same Linux machine as the Discord desktop app. Python 3 stdlib only.
Config: ~/.config/health-display/config.env (KEY=VALUE), overridable by environment vars.
"""
import json, os, socket, struct, sys, time, urllib.error, urllib.parse, urllib.request, uuid

CONFIG_PATH = os.path.expanduser(os.environ.get("HEALTH_DISPLAY_CONFIG", "~/.config/health-display/config.env"))

def load_config():
    cfg = {
        "PULSOID_TOKEN": "", "DISCORD_APP_ID": "",
        "DETAILS": "❤️ {bpm} bpm", "STATE": "min {min} · avg {avg} · max {max}",
        "IMAGE": "on", "IMG_TEXT": "{bpm}", "IMG_BG": "ED4245", "IMG_FG": "FFFFFF",
        "POLL_INTERVAL": "3", "UPDATE_INTERVAL": "15", "STALE_AFTER": "60",
    }
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    cfg[k.strip()] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    for k in cfg:
        if k in os.environ:
            cfg[k] = os.environ[k]
    return cfg

C = load_config()

POLL = float(C["POLL_INTERVAL"]); UPDATE = float(C["UPDATE_INTERVAL"]); STALE = float(C["STALE_AFTER"])

def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)

# ------------------------------------------------------------------ Pulsoid
class HeartRate:
    def __init__(self):
        self.bpm = None; self.measured = 0; self.mn = None; self.mx = None
        self.total = 0; self.n = 0; self.last_ts = None

    def poll(self):
        req = urllib.request.Request(
            "https://dev.pulsoid.net/api/v1/data/heart_rate/latest",
            headers={"Authorization": f"Bearer {C['PULSOID_TOKEN']}", "User-Agent": "Health-Display"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                d = json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 412:  # account has no heart-rate data yet
                return
            raise RuntimeError(f"HTTP {e.code} {e.read().decode(errors='replace')[:200]}")
        ts = d.get("measured_at"); bpm = int(d.get("data", {}).get("heart_rate") or 0)
        if not bpm or ts == self.last_ts:
            return
        self.last_ts = ts; self.bpm = bpm; self.measured = ts / 1000
        self.mn = bpm if self.mn is None else min(self.mn, bpm)
        self.mx = bpm if self.mx is None else max(self.mx, bpm)
        self.total += bpm; self.n += 1

    @property
    def live(self):
        return self.bpm is not None and time.time() - self.measured <= STALE

    def fill(self, tpl):
        avg = round(self.total / self.n) if self.n else "?"
        return (tpl.replace("{bpm}", str(self.bpm or "?")).replace("{min}", str(self.mn or "?"))
                   .replace("{max}", str(self.mx or "?")).replace("{avg}", str(avg)))

# ------------------------------------------------------------------ Google Health API
GOOGLE_API = "https://health.googleapis.com/v4/users/me"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_REDIRECT = "http://localhost:8765/"
GOOGLE_SCOPES = [f"https://www.googleapis.com/auth/googlehealth.{s}.readonly" for s in (
    "activity_and_fitness", "health_metrics_and_measurements", "sleep", "location", "nutrition",
    "reproductive_health", "logged_symptoms", "mindfulness", "ecg", "irn", "profile")]
GOOGLE_TOKEN_PATH = os.path.expanduser(os.environ.get(
    "HEALTH_DISPLAY_GOOGLE_TOKEN", "~/.local/state/health-display/google-token.json"))

def http_json(url, data=None, headers=None, method=None):
    body = None
    if isinstance(data, dict) and headers and headers.get("Content-Type") == "application/json":
        body = json.dumps(data).encode()
    elif data is not None:
        body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body, method=method,
                                 headers={"User-Agent": "Health-Display", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code} {e.read().decode(errors='replace')[:500]}")

class GoogleHealth:
    def __init__(self):
        self.access = None; self.expires = 0

    @staticmethod
    def configured():
        return bool(C.get("GOOGLE_CLIENT_ID") and C.get("GOOGLE_CLIENT_SECRET"))

    @staticmethod
    def auth_url():
        return "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode({
            "client_id": C["GOOGLE_CLIENT_ID"], "redirect_uri": GOOGLE_REDIRECT,
            "response_type": "code", "scope": " ".join(GOOGLE_SCOPES),
            "access_type": "offline", "prompt": "consent", "include_granted_scopes": "true"})

    @staticmethod
    def exchange(code):
        tok = http_json(GOOGLE_TOKEN_URL, {
            "code": code, "client_id": C["GOOGLE_CLIENT_ID"], "client_secret": C["GOOGLE_CLIENT_SECRET"],
            "redirect_uri": GOOGLE_REDIRECT, "grant_type": "authorization_code"})
        if "refresh_token" not in tok:
            raise RuntimeError("no refresh_token returned; revoke access and run auth again")
        os.makedirs(os.path.dirname(GOOGLE_TOKEN_PATH), exist_ok=True)
        fd = os.open(GOOGLE_TOKEN_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"refresh_token": tok["refresh_token"], "scope": tok.get("scope", "")}, f)
        return tok.get("scope", "")

    def token(self):
        if self.access and time.time() < self.expires - 60:
            return self.access
        try:
            with open(GOOGLE_TOKEN_PATH) as f:
                refresh = json.load(f)["refresh_token"]
        except FileNotFoundError:
            raise RuntimeError("not signed in; run: health-display auth")
        tok = http_json(GOOGLE_TOKEN_URL, {
            "refresh_token": refresh, "client_id": C["GOOGLE_CLIENT_ID"],
            "client_secret": C["GOOGLE_CLIENT_SECRET"], "grant_type": "refresh_token"})
        self.access = tok["access_token"]; self.expires = time.time() + int(tok.get("expires_in", 3600))
        return self.access

    def get(self, path, params=None):
        url = f"{GOOGLE_API}/{path}" + ("?" + urllib.parse.urlencode(params) if params else "")
        return http_json(url, headers={"Authorization": f"Bearer {self.token()}", "Accept": "application/json"})

    def post(self, path, body):
        return http_json(f"{GOOGLE_API}/{path}", body, method="POST", headers={
            "Authorization": f"Bearer {self.token()}", "Accept": "application/json",
            "Content-Type": "application/json"})

def cmd_auth(args):
    if not GoogleHealth.configured():
        sys.exit(f"Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET in {CONFIG_PATH}")
    if args:  # redirect URL (or bare code) pasted from the browser
        arg = args[0]
        code = urllib.parse.parse_qs(urllib.parse.urlparse(arg).query).get("code", [arg])[0]
    else:
        import http.server, threading
        got = {}
        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                got["code"] = q.get("code", [None])[0]; got["error"] = q.get("error", [None])[0]
                self.send_response(200); self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers(); self.wfile.write("Health Display: you can close this tab.".encode())
            def log_message(self, *a): pass
        srv = http.server.HTTPServer(("127.0.0.1", 8765), H)
        threading.Thread(target=srv.handle_request, daemon=True).start()
        print("Open this URL, approve access, then either let it redirect here or paste the\n"
              "address of the page it lands on (http://localhost:8765/?...) below:\n\n"
              f"{GoogleHealth.auth_url()}\n")
        def read_stdin():
            try:
                line = input("> ").strip()
                if line:
                    got.setdefault("code", urllib.parse.parse_qs(urllib.parse.urlparse(line).query).get("code", [line])[0])
            except EOFError:
                pass
        threading.Thread(target=read_stdin, daemon=True).start()
        while not got.get("code") and not got.get("error"):
            time.sleep(0.2)
        if got.get("error"):
            sys.exit(f"auth failed: {got['error']}")
        code = got["code"]
    scopes = GoogleHealth.exchange(code)
    print(f"Signed in. Token saved to {GOOGLE_TOKEN_PATH}\nGranted: {scopes}")

PROBE_TYPES = [
    "steps", "active-minutes", "active-zone-minutes", "activity-level", "active-energy-burned",
    "total-calories", "distance", "floors", "altitude", "sedentary-period", "exercise",
    "heart-rate", "daily-resting-heart-rate", "heart-rate-variability", "daily-heart-rate-variability",
    "daily-heart-rate-zones", "time-in-heart-rate-zone", "calories-in-heart-rate-zone",
    "oxygen-saturation", "daily-oxygen-saturation", "daily-respiratory-rate", "respiratory-rate-sleep-summary",
    "daily-sleep-temperature-derivations", "skin-temperature-sensors", "core-body-temperature",
    "vo2-max", "daily-vo2-max", "run-vo2-max", "sleep", "weight", "height", "body-fat",
    "blood-glucose", "hydration-log", "nutrition-log", "food", "moods", "symptoms",
    "menstrual-period", "ovulation-test", "electrocardiogram", "irregular-rhythm-notification",
    "swim-lengths-data", "mindfulness", "mindfulness-session", "stress", "stress-score",
]

def cmd_probe(args):
    """Dump the newest data point of each data type, to see what the account actually has."""
    g = GoogleHealth()
    try:
        print("identity:", json.dumps(g.get("identity")))
        print("pairedDevices:", json.dumps(g.get("pairedDevices"))[:800])
    except RuntimeError as e:
        print("identity/devices:", e)
    for t in args or PROBE_TYPES:
        try:
            d = g.get(f"dataTypes/{t}/dataPoints", {"pageSize": 2})
            pts = d.get("dataPoints", [])
            print(f"\n== {t}: {len(pts)} point(s){' (more)' if d.get('nextPageToken') else ''}")
            for p in pts:
                print(json.dumps(p)[:1500])
        except RuntimeError as e:
            print(f"\n== {t}: {str(e)[:300]}")

# ------------------------------------------------------------------ Discord IPC
def ipc_candidates():
    base = os.environ.get("XDG_RUNTIME_DIR") or os.environ.get("TMPDIR") or "/tmp"
    for sub in ("", "app/com.discordapp.Discord", "app/com.discordapp.DiscordCanary",
                "snap.discord", "snap.discord-canary", ".flatpak/dev.vencord.Vesktop/xdg-run"):
        for i in range(10):
            yield os.path.join(base, sub, f"discord-ipc-{i}")

class DiscordIPC:
    def __init__(self, app_id):
        self.app_id = app_id; self.sock = None

    def connect(self):
        for path in ipc_candidates():
            if not os.path.exists(path):
                continue
            try:
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); s.settimeout(10)
                s.connect(path); self.sock = s
                self._send(0, {"v": 1, "client_id": self.app_id})
                op, data = self._recv()
                if data.get("evt") == "READY":
                    log(f"discord: connected via {path} as {data['data']['user']['username']}")
                    return True
                log(f"discord: handshake rejected: {data}"); self.close()
            except OSError:
                self.close()
        return False

    def _send(self, op, payload):
        body = json.dumps(payload).encode()
        self.sock.sendall(struct.pack("<II", op, len(body)) + body)

    def _recv(self):
        hdr = self._read(8); op, length = struct.unpack("<II", hdr)
        return op, json.loads(self._read(length))

    def _read(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("discord closed the connection")
            buf += chunk
        return buf

    def set_activity(self, activity):
        self._send(1, {"cmd": "SET_ACTIVITY", "args": {"pid": os.getpid(), "activity": activity},
                       "nonce": str(uuid.uuid4())})
        op, data = self._recv()
        if op == 2:
            raise ConnectionError(f"discord closed: {data}")
        if data.get("evt") == "ERROR":
            log(f"discord: SET_ACTIVITY error: {data.get('data')}")

    def close(self):
        try:
            self.sock and self.sock.close()
        except OSError:
            pass
        self.sock = None

# ------------------------------------------------------------------ main loop
def tile_url(bpm):
    text = urllib.parse.quote(C["IMG_TEXT"].replace("{bpm}", str(bpm)))
    return f"https://placehold.co/512x512/{C['IMG_BG']}/{C['IMG_FG']}/png?text={text}&font=montserrat"

def build_activity(hr, started):
    if not hr.live:
        return None
    a = {"type": 0, "details": hr.fill(C["DETAILS"]), "timestamps": {"start": int(started)}}
    if C["STATE"]:
        a["state"] = hr.fill(C["STATE"])
    if C["IMAGE"].lower() != "off":
        a["assets"] = {"large_image": tile_url(hr.bpm), "large_text": f"{hr.bpm} bpm"}
    return a

def main():
    if not C["PULSOID_TOKEN"] or not C["DISCORD_APP_ID"]:
        sys.exit(f"Set PULSOID_TOKEN and DISCORD_APP_ID in {CONFIG_PATH}")
    hr = HeartRate(); ipc = DiscordIPC(C["DISCORD_APP_ID"])
    started = time.time(); last_key = "init"; last_push = 0; next_poll = 0; warned = False
    while True:
        now = time.time()
        if now >= next_poll:
            next_poll = now + POLL
            try:
                hr.poll()
            except Exception as e:
                log(f"pulsoid: {e}")
        if ipc.sock is None:
            if not ipc.connect():
                if not warned:
                    log("discord: desktop app not found, retrying every 15s"); warned = True
                time.sleep(15); continue
            warned = False; last_key = "init"
        act = build_activity(hr, started)
        key = json.dumps({k: v for k, v in (act or {}).items() if k != "timestamps"}, sort_keys=True)
        if key != last_key and (now - last_push >= UPDATE or last_key == "init" or act is None):
            try:
                ipc.set_activity(act)
                log(f"presence: {act['details']}" if act else "presence: cleared (no HR data)")
                last_key = key; last_push = now
            except (OSError, ConnectionError) as e:
                log(f"discord: {e}"); ipc.close(); continue
        time.sleep(1)

if __name__ == "__main__":
    cmd, rest = (sys.argv[1], sys.argv[2:]) if len(sys.argv) > 1 else ("run", [])
    try:
        {"run": lambda a: main(), "auth": cmd_auth, "probe": cmd_probe}.get(
            cmd, lambda a: sys.exit("usage: health-display [run | auth [redirect-url] | probe [type...]]"))(rest)
    except KeyboardInterrupt:
        pass

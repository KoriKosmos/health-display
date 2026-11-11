#!/usr/bin/env python3
"""Health Display: Pulsoid heart rate -> Discord Rich Presence (official local IPC, no user token).

Runs on the same Linux machine as the Discord desktop app. Python 3 stdlib only.
Config: ~/.config/health-display/config.env (KEY=VALUE), overridable by environment vars.
"""
import json, os, re, socket, struct, sys, time, urllib.error, urllib.parse, urllib.request, uuid

CONFIG_PATH = os.path.expanduser(os.environ.get("HEALTH_DISPLAY_CONFIG", "~/.config/health-display/config.env"))

def load_config():
    cfg = {
        "PULSOID_TOKEN": "", "DISCORD_APP_ID": "",
        "DETAILS": "❤️ {bpm} bpm", "STATE": "min {min} · avg {avg} · max {max}",
        "IMAGE": "on", "IMG_TEXT": "{bpm}", "IMG_HOVER": "{bpm} bpm", "IMG_BG": "ED4245", "IMG_FG": "FFFFFF",
        "POLL_INTERVAL": "3", "UPDATE_INTERVAL": "15", "STALE_AFTER": "60",
        "GOOGLE_CLIENT_ID": "", "GOOGLE_CLIENT_SECRET": "", "GOOGLE_INTERVAL": "300",
        "STAT_MAX_AGE_DAYS": "1.5", "ROLLUP_SOURCES": "google-wearables",
        "SOURCE_DEFAULT": "watch,oura,ultrahuman,*,samsung,phone",
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

    def values(self):
        v = {"bpm": self.bpm, "min": self.mn, "max": self.mx,
             "avg": round(self.total / self.n) if self.n else None}
        return {k: str(x) for k, x in v.items() if x is not None}

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

# Health Connect apps with a short name; any other app is identified by its package name.
APP_ALIASES = {
    "com.ouraring.oura": "oura", "com.ultrahuman.android": "ultrahuman",
    "com.sec.android.app.shealth": "samsung", "com.google.android.apps.fitness": "phone",
    "com.android.healthconnect.phone": "phone",
}

def source_name(ds):
    """Short name for the device/app a data point came from (watch, oura, phone, <package>...)."""
    if ds.get("platform") == "FITBIT":
        return "phone" if (ds.get("device") or {}).get("displayName") == "MobileTrack" else "watch"
    pkg = (ds.get("application") or {}).get("packageName")
    return APP_ALIASES.get(pkg, pkg or (ds.get("platform") or "unknown").lower())

def hm(minutes):
    minutes = int(round(float(minutes)))
    return f"{minutes // 60}h{minutes % 60:02d}" if minutes >= 60 else f"{minutes}m"

def body_of(p):
    return next(v for k, v in p.items() if k not in ("dataSource", "name"))

class HealthStats:
    """Daily stats from the Google Health API, refreshed in a background thread.

    Every device/app is a fallback for the others: today's totals come from whichever
    source counted the most, overnight stats from the most recent night any source
    recorded (SOURCE_* order breaks ties). `values` maps placeholder -> display string;
    missing or stale stats are simply absent.
    """
    def __init__(self):
        self.g = GoogleHealth(); self.values = {}
        self.max_age = float(C["STAT_MAX_AGE_DAYS"])
        self.family = f"users/me/dataSourceFamilies/{C['ROLLUP_SOURCES']}"

    def rank(self, group, src):
        """Position of a source in the preference list; '*' stands for any unlisted source."""
        order = [x.strip() for x in (C.get(f"SOURCE_{group}") or C["SOURCE_DEFAULT"]).split(",") if x.strip()]
        if src in order:
            return order.index(src)
        return order.index("*") if "*" in order else len(order)

    def best(self, points, group, recency, window=0):
        """Most recent point; points within `window` of it compete on source preference."""
        if not points:
            return None
        newest = max(recency(p) for p in points)
        close = [p for p in points if newest - recency(p) <= window]
        return min(close, key=lambda p: (self.rank(group, source_name(p.get("dataSource", {}))), -recency(p)))

    def list(self, dtype, flt, size=10000):
        out, token = [], None
        for _ in range(20):
            d = self.g.get(f"dataTypes/{dtype}/dataPoints", {"filter": flt, "pageSize": size, **({"pageToken": token} if token else {})})
            out += d.get("dataPoints", []); token = d.get("nextPageToken")
            if not token:
                return out
        return out

    def rollup_today(self, dtype):
        t = time.localtime(); n = time.localtime(time.time() + 86400)
        r = self.g.post(f"dataTypes/{dtype}/dataPoints:dailyRollUp", {
            "range": {"start": {"date": {"year": t.tm_year, "month": t.tm_mon, "day": t.tm_mday}},
                      "end": {"date": {"year": n.tm_year, "month": n.tm_mon, "day": n.tm_mday}}},
            "windowSizeDays": 1, "dataSourceFamily": self.family})
        for p in r.get("rollupDataPoints", []):
            d = p["civilStartTime"]["date"]
            if (d["year"], d["month"], d["day"]) == (t.tm_year, t.tm_mon, t.tm_mday):
                return p
        return None

    def today_by_source(self, dtype, value):
        """Today's total per data feed: {(name, platform, package, device): total}."""
        body = dtype.replace("-", "_")
        totals = {}  # keyed per feed, so e.g. two phone apps counting the same steps aren't added up
        for p in self.list(dtype, f'{body}.interval.civil_start_time >= "{time.strftime("%Y-%m-%dT00:00:00")}"'):
            ds = p.get("dataSource", {})
            feed = (source_name(ds), ds.get("platform"), (ds.get("application") or {}).get("packageName"),
                    (ds.get("device") or {}).get("displayName"))
            totals[feed] = totals.get(feed, 0) + value(body_of(p))
        return totals

    def pick_total(self, group, totals):
        """Source that counted the most today (the device actually worn); ties -> preference."""
        totals = {k: x for k, x in totals.items() if x > 0}
        if not totals:
            return None, None
        feed = min(totals, key=lambda k: (-totals[k], self.rank(group, k[0])))
        return feed[0], totals[feed]

    @staticmethod
    def since(days, civil=False):
        ts = time.time() - days * 86400
        return time.strftime("%Y-%m-%dT%H:%M:%S" if civil else "%Y-%m-%dT%H:%M:%SZ",
                             time.localtime(ts) if civil else time.gmtime(ts))

    def daily(self, dtype, group, days=None):
        """Most recent daily record from any source; same-day records -> preference. Returns (body, source)."""
        d = time.strftime("%Y-%m-%d", time.localtime(time.time() - (days or self.max_age) * 86400))
        body = dtype.replace("-", "_")
        def day(p):
            x = body_of(p)["date"]; return time.mktime((x["year"], x["month"], x["day"], 0, 0, 0, 0, 0, -1))
        p = self.best(self.list(dtype, f'{body}.date >= "{d}"'), group, day)
        return (body_of(p), source_name(p.get("dataSource", {}))) if p else (None, None)

    def refresh(self):
        v = {}
        def step(name, fn):
            try:
                fn()
            except Exception as e:
                log(f"google: {name}: {e}")
        # --- today's totals: the source with the highest count wins (no double counting)
        def today():
            src, n = self.pick_total("STEPS", self.today_by_source("steps", lambda b: int(b["count"])))
            if src: v["steps"] = f"{n:,}"; v["steps_src"] = src
            src, n = self.pick_total("DISTANCE", self.today_by_source("distance", lambda b: int(b["millimeters"])))
            if src: v["distance"] = f"{n / 1e6:.1f}"
            src, n = self.pick_total("ACTIVE", self.today_by_source(
                "active-minutes", lambda b: sum(int(x["activeMinutes"]) for x in b["activeMinutesByActivityLevel"])))
            if src: v["active"] = str(n)
            src, n = self.pick_total("ACTIVE", self.today_by_source(
                "active-minutes", lambda b: sum(int(x["activeMinutes"]) for x in b["activeMinutesByActivityLevel"]
                                                if x["activityLevel"] in ("MODERATE", "VIGOROUS"))))
            v["active_mv"] = str(n or 0)
            src, n = self.pick_total("CALORIES", self.today_by_source("active-energy-burned", lambda b: b.get("kcal", 0)))
            if src: v["active_kcal"] = f"{round(n):,}"
            src, n = self.pick_total("AZM", self.today_by_source("active-zone-minutes", lambda b: int(b["activeZoneMinutes"])))
            v["azm"] = str(n or 0)
        step("today", today)
        def rollups():  # only available as rollups; watch/Google sources
            if (p := self.rollup_today("total-calories")): v["calories"] = f"{round(p['totalCalories']['kcalSum']):,}"
            if (p := self.rollup_today("floors")) and "floors" in p:
                v["floors"] = str(next(iter(p["floors"].values())))
        step("rollups", rollups)
        # --- overnight metrics: most recent night from any source
        def overnight():
            d, src = self.daily("daily-resting-heart-rate", "RHR")
            if d: v["rhr"] = str(d["beatsPerMinute"]); v["rhr_src"] = src
            d, src = self.daily("daily-heart-rate-variability", "HRV")
            if d: v["hrv"] = str(round(d["averageHeartRateVariabilityMilliseconds"])); v["hrv_src"] = src
            d, _ = self.daily("daily-oxygen-saturation", "SPO2")
            if d: v["spo2"] = f"{d['averagePercentage']:.0f}"
            d, _ = self.daily("daily-respiratory-rate", "RESP")
            if d: v["resp"] = f"{d['breathsPerMinute']:.1f}"
            d, _ = self.daily("daily-sleep-temperature-derivations", "TEMP")
            if d: v["temp_delta"] = f"{d['nightlyTemperatureCelsius'] - d['baselineTemperatureCelsius']:+.1f}"
            d, _ = self.daily("daily-vo2-max", "VO2", days=30)
            if d: v["vo2max"] = f"{d['vo2Max']:.0f}"; v["fitness"] = d.get("cardioFitnessLevel", "").replace("_", " ").title()
        step("overnight", overnight)
        def sleep():
            pts = self.list("sleep", f'sleep.interval.end_time >= "{self.since(self.max_age)}"', 25)
            ends = lambda p: time.mktime(time.strptime(p["sleep"]["interval"]["endTime"][:19], "%Y-%m-%dT%H:%M:%S"))
            p = self.best(pts, "SLEEP", ends, window=4 * 3600)  # sessions ending within 4h = same night
            if not p:
                return
            s = p["sleep"]["summary"]; st = {x["type"]: int(x["minutes"]) for x in s.get("stagesSummary", [])}
            asleep = int(s["minutesAsleep"]); period = int(s.get("minutesInSleepPeriod") or 0)
            v["sleep"] = hm(asleep); v["sleep_src"] = source_name(p.get("dataSource", {}))
            if period: v["sleep_eff"] = str(round(100 * asleep / period))
            for k in ("DEEP", "REM", "LIGHT", "AWAKE"):
                if k in st: v[k.lower()] = hm(st[k])
        step("sleep", sleep)
        # --- other records
        def other():
            pts = self.list("exercise", f'exercise.interval.civil_start_time >= "{self.since(1, civil=True)}"', 25)
            if pts:
                e = max(pts, key=lambda p: p["exercise"]["interval"]["startTime"])["exercise"]
                v["workout"] = e.get("displayName") or e.get("exerciseType", "").title()
                if e.get("activeDuration"): v["workout_time"] = hm(float(e["activeDuration"].rstrip("s")) / 60)
            pts = self.list("weight", f'weight.sample_time.physical_time >= "{self.since(30)}"', 50)
            if pts:
                w = max(pts, key=lambda p: p["weight"]["sampleTime"]["physicalTime"])["weight"]
                v["weight"] = f"{w['weightGrams'] / 1000:.1f}"
            pts = self.list("nutrition-log", f'nutrition_log.interval.civil_start_time >= "{time.strftime("%Y-%m-%dT00:00:00")}"', 500)
            if pts:
                v["kcal_in"] = f"{round(sum(p['nutritionLog'].get('energy', {}).get('kcal', 0) for p in pts)):,}"
            pts = self.list("blood-glucose", f'blood_glucose.sample_time.physical_time >= "{self.since(1)}"', 50)
            if pts:
                bg = max(pts, key=lambda p: p["bloodGlucose"]["sampleTime"]["physicalTime"])["bloodGlucose"]
                mg = bg["bloodGlucoseMilligramsPerDeciliter"]
                v["glucose"] = f"{mg / 18.016:.1f}"; v["glucose_mgdl"] = f"{mg:.0f}"
            pts = self.list("electrocardiogram", f'electrocardiogram.interval.start_time >= "{self.since(1)}"', 10)
            if pts:
                e = max(pts, key=lambda p: p["electrocardiogram"]["interval"]["startTime"])["electrocardiogram"]
                v["ecg"] = e.get("resultClassification", "").replace("_", " ").capitalize()
        step("other", other)
        self.values = v
        log("google: " + (", ".join(f"{k}={x}" for k, x in sorted(v.items())) or "no stats"))

    def run_forever(self):
        while True:
            try:
                self.refresh()
            except Exception as e:
                log(f"google: {e}")
            time.sleep(float(C["GOOGLE_INTERVAL"]))

def cmd_stats(args):
    """Fetch and print the display values once."""
    HealthStats().refresh()

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

def render(tpl, values):
    """Fill {placeholders}; drop any ' · '-separated segment that references a missing value."""
    out = []
    for seg in tpl.split(" · "):
        names = re.findall(r"\{(\w+)\}", seg)
        if all(n in values for n in names):
            out.append(re.sub(r"\{(\w+)\}", lambda m: values[m.group(1)], seg))
    text = " · ".join(x for x in out if x.strip())
    return text[:128] if len(text) >= 2 else ""

def build_activity(hr, stats, started):
    if not hr.live:
        return None
    values = {**(stats.values if stats else {}), **hr.values()}
    a = {"type": 0, "timestamps": {"start": int(started)}}
    if (details := render(C["DETAILS"], values)):
        a["details"] = details
    if (state := render(C["STATE"], values)):
        a["state"] = state
    if C["IMAGE"].lower() != "off":
        a["assets"] = {"large_image": tile_url(hr.bpm)}
        if (hover := render(C["IMG_HOVER"], values)):
            a["assets"]["large_text"] = hover
    return a

def main():
    if not C["PULSOID_TOKEN"] or not C["DISCORD_APP_ID"]:
        sys.exit(f"Set PULSOID_TOKEN and DISCORD_APP_ID in {CONFIG_PATH}")
    hr = HeartRate(); ipc = DiscordIPC(C["DISCORD_APP_ID"]); stats = None
    if GoogleHealth.configured() and os.path.exists(GOOGLE_TOKEN_PATH):
        import threading
        stats = HealthStats(); threading.Thread(target=stats.run_forever, daemon=True).start()
    elif GoogleHealth.configured():
        log(f"google: not signed in ({GOOGLE_TOKEN_PATH} missing); run: health-display auth")
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
        act = build_activity(hr, stats, started)
        key = json.dumps({k: v for k, v in (act or {}).items() if k != "timestamps"}, sort_keys=True)
        if key != last_key and (now - last_push >= UPDATE or last_key == "init" or act is None):
            try:
                ipc.set_activity(act)
                log(f"presence: {act.get('details', '')} | {act.get('state', '')}" if act else "presence: cleared (no HR data)")
                last_key = key; last_push = now
            except (OSError, ConnectionError) as e:
                log(f"discord: {e}"); ipc.close(); continue
        time.sleep(1)

if __name__ == "__main__":
    cmd, rest = (sys.argv[1], sys.argv[2:]) if len(sys.argv) > 1 else ("run", [])
    try:
        {"run": lambda a: main(), "auth": cmd_auth, "stats": cmd_stats, "probe": cmd_probe}.get(
            cmd, lambda a: sys.exit("usage: health-display [run | auth [redirect-url] | stats | probe [type...]]"))(rest)
    except KeyboardInterrupt:
        pass

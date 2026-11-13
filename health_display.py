#!/usr/bin/env python3
"""Health Display: Pulsoid heart rate -> Discord Rich Presence (official local IPC, no user token).

Runs on the same Linux machine as the Discord desktop app. Python 3 stdlib only.
Config: ~/.config/health-display/config.env (KEY=VALUE), overridable by environment vars.
"""
import calendar, datetime, hmac, json, os, re, socket, struct, sys, threading, time, urllib.error, urllib.parse, urllib.request, uuid

CONFIG_PATH = os.path.expanduser(os.environ.get("HEALTH_DISPLAY_CONFIG", "~/.config/health-display/config.env"))

def load_config():
    cfg = {
        "PULSOID_TOKEN": "", "DISCORD_APP_ID": "",
        "DETAILS": "❤️ {bpm} bpm", "STATE": "min {min} · avg {avg} · max {max}",
        "IMAGE": "on", "IMG_TEXT": "{bpm}", "IMG_HOVER": "{bpm} bpm", "IMG_BG": "ED4245", "IMG_FG": "FFFFFF",
        "POLL_INTERVAL": "3", "UPDATE_INTERVAL": "15", "STALE_AFTER": "60",
        "GOOGLE_CLIENT_ID": "", "GOOGLE_CLIENT_SECRET": "", "GOOGLE_INTERVAL": "300",
        "HR_FALLBACK_MAX_AGE": "900", "HR_FALLBACK_INTERVAL": "60",
        "PUSH_TOKEN": "", "PUSH_URL": "", "PUSH_PORT": "8766", "PUSH_STALE_AFTER": "60",
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

# ------------------------------------------------------------------ Oura API v2
OURA_API = "https://api.ouraring.com/v2/usercollection"
# Oura moved OAuth to moi.ouraring.com; the documented api.ouraring.com/oauth/token rejects its codes.
OURA_TOKEN_URL = "https://moi.ouraring.com/oauth/v2/ext/oauth-token"
OURA_SCOPES = ["personal", "daily", "heartrate", "workout", "session", "spo2", "heart_health", "stress"]
OURA_TOKEN_PATH = os.path.expanduser(os.environ.get(
    "HEALTH_DISPLAY_OURA_TOKEN", "~/.local/state/health-display/oura-token.json"))

def save_secret_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)

class Oura:
    """Oura API v2 via OAuth. Refresh tokens are single-use, so each refresh stores the new one."""
    def __init__(self):
        self.lock = threading.Lock()

    @staticmethod
    def configured():
        return bool(C.get("OURA_CLIENT_ID") and C.get("OURA_CLIENT_SECRET"))

    @staticmethod
    def auth_url():
        return "https://cloud.ouraring.com/oauth/authorize?" + urllib.parse.urlencode({
            "client_id": C["OURA_CLIENT_ID"], "redirect_uri": GOOGLE_REDIRECT,
            "response_type": "code", "scope": " ".join(OURA_SCOPES)})

    @staticmethod
    def _store(tok):
        save_secret_json(OURA_TOKEN_PATH, {"refresh_token": tok["refresh_token"], "access_token": tok["access_token"],
                                           "expires_at": time.time() + int(tok.get("expires_in", 86400)),
                                           "scope": tok.get("scope", "")})
        return tok.get("scope", "")

    @staticmethod
    def exchange(code):
        return Oura._store(http_json(OURA_TOKEN_URL, {
            "grant_type": "authorization_code", "code": code, "redirect_uri": GOOGLE_REDIRECT,
            "client_id": C["OURA_CLIENT_ID"], "client_secret": C["OURA_CLIENT_SECRET"]}))

    def token(self):
        with self.lock:
            try:
                with open(OURA_TOKEN_PATH) as f:
                    t = json.load(f)
            except FileNotFoundError:
                raise RuntimeError("not signed in; run: health-display auth oura")
            if time.time() < t.get("expires_at", 0) - 300:
                return t["access_token"]
            self._store(http_json(OURA_TOKEN_URL, {
                "grant_type": "refresh_token", "refresh_token": t["refresh_token"],
                "client_id": C["OURA_CLIENT_ID"], "client_secret": C["OURA_CLIENT_SECRET"]}))
            with open(OURA_TOKEN_PATH) as f:
                return json.load(f)["access_token"]

    def get(self, collection, **params):
        """All documents of a collection (follows next_token)."""
        out, token = [], None
        for _ in range(20):
            q = {**params, **({"next_token": token} if token else {})}
            d = http_json(f"{OURA_API}/{collection}?{urllib.parse.urlencode(q)}",
                          headers={"Authorization": f"Bearer {self.token()}"})
            out += d.get("data", []); token = d.get("next_token")
            if not token:
                break
        return out

# ------------------------------------------------------------------ Ultrahuman
ULTRAHUMAN_API = "https://partner.ultrahuman.com/api/v1/partner/daily_metrics"

class Ultrahuman:
    @staticmethod
    def configured():
        return bool(C.get("ULTRAHUMAN_TOKEN"))

    def day(self, date):
        """{metric type: object} for one day (YYYY-MM-DD)."""
        d = http_json(f"{ULTRAHUMAN_API}?date={date}", headers={"Authorization": C["ULTRAHUMAN_TOKEN"]})
        return {m["type"]: m.get("object") or {} for m in (d.get("data", {}).get("metrics", {}).get(date) or [])}

# ------------------------------------------------------------------ sign-in
def cmd_auth(args):
    provider = args[0] if args and args[0] in ("google", "oura") else "google"
    args = args[1:] if args and args[0] in ("google", "oura") else args
    api = {"google": GoogleHealth, "oura": Oura}[provider]
    if not api.configured():
        sys.exit(f"Set {provider.upper()}_CLIENT_ID and {provider.upper()}_CLIENT_SECRET in {CONFIG_PATH}")
    if args:  # redirect URL (or bare code) pasted from the browser
        arg = args[0]
        code = urllib.parse.parse_qs(urllib.parse.urlparse(arg).query).get("code", [arg])[0]
    else:
        import http.server
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
              f"{api.auth_url()}\n")
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
    scopes = api.exchange(code)
    print(f"Signed in to {provider}. Token saved to {OURA_TOKEN_PATH if provider == 'oura' else GOOGLE_TOKEN_PATH}\nGranted: {scopes}")

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
    """Daily stats from Google Health, Ultrahuman and Oura, refreshed in a background thread.

    Every device/app/API is a fallback for the others: each offers candidate values,
    today's totals come from whichever source counted the most, overnight stats and
    scores from the most recent night/day any source recorded (SOURCE_* order breaks ties). `values` maps placeholder -> display string;
    missing or stale stats are simply absent.
    """
    def __init__(self):
        self.g = GoogleHealth(); self.values = {}
        self.google = GoogleHealth.configured() and os.path.exists(GOOGLE_TOKEN_PATH)
        self.uh = Ultrahuman() if Ultrahuman.configured() else None
        self.oura = Oura() if Oura.configured() else None
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

    @staticmethod
    def since(days, civil=False):
        ts = time.time() - days * 86400
        return time.strftime("%Y-%m-%dT%H:%M:%S" if civil else "%Y-%m-%dT%H:%M:%SZ",
                             time.localtime(ts) if civil else time.gmtime(ts))

    def refresh(self):
        v = {}; cands = []
        def step(name, fn):
            try:
                fn()
            except Exception as e:
                log(f"stats: {name}: {e}")
        def offer(group, kind, values, source, when=0.0, amount=0, window=0):
            """A candidate value set; one per group wins in merge() below."""
            values = {k: str(x) for k, x in values.items() if x not in (None, "")}
            if values:
                cands.append({"group": group, "kind": kind, "values": values, "source": source,
                              "when": when, "amount": amount, "window": window})
        day_epoch = lambda d: time.mktime(time.strptime(d[:10], "%Y-%m-%d"))
        iso_epoch = lambda t: datetime.datetime.fromisoformat(t.replace("Z", "+00:00")).timestamp()
        today = time.strftime("%Y-%m-%d")
        recent_days = [time.strftime("%Y-%m-%d", time.localtime(time.time() - i * 86400))
                       for i in range(int(self.max_age) + 1)]
        fresh_day = lambda d, days=None: day_epoch(d) >= time.time() - (days or self.max_age) * 86400 - 86400
        SLEEP_WINDOW = 4 * 3600  # sessions ending within 4h of each other are the same night

        # --- Google Health: today's totals per feed
        def g_today():
            for dtype, group, key, fn, fmt in (
                    ("steps", "STEPS", "steps", lambda b: int(b["count"]), lambda n: f"{n:,}"),
                    ("distance", "DISTANCE", "distance", lambda b: int(b["millimeters"]), lambda n: f"{n / 1e6:.1f}"),
                    ("active-minutes", "ACTIVE", "active",
                     lambda b: sum(int(x["activeMinutes"]) for x in b["activeMinutesByActivityLevel"]), str),
                    ("active-minutes", "ACTIVE_MV", "active_mv",
                     lambda b: sum(int(x["activeMinutes"]) for x in b["activeMinutesByActivityLevel"]
                                   if x["activityLevel"] in ("MODERATE", "VIGOROUS")), str),
                    ("active-energy-burned", "CALORIES", "active_kcal", lambda b: b.get("kcal", 0), lambda n: f"{round(n):,}"),
                    ("active-zone-minutes", "AZM", "azm", lambda b: int(b["activeZoneMinutes"]), str)):
                for feed, n in self.today_by_source(dtype, fn).items():
                    if n > 0:
                        offer(group, "total", {key: fmt(n), f"{key}_src": feed[0]}, feed[0], amount=n)
        if self.google: step("google today", g_today)
        def g_rollups():  # only available as rollups; watch/Google sources
            if (p := self.rollup_today("total-calories")): v["calories"] = f"{round(p['totalCalories']['kcalSum']):,}"
            if (p := self.rollup_today("floors")) and "floors" in p:
                v["floors"] = str(next(iter(p["floors"].values())))
        if self.google: step("google rollups", g_rollups)
        # --- Google Health: overnight/daily records
        def g_daily():
            since = time.strftime("%Y-%m-%d", time.localtime(time.time() - self.max_age * 86400))
            for dtype, group, fn, days in (
                    ("daily-resting-heart-rate", "RHR", lambda d: {"rhr": d["beatsPerMinute"]}, None),
                    ("daily-heart-rate-variability", "HRV", lambda d: {"hrv": round(d["averageHeartRateVariabilityMilliseconds"])}, None),
                    ("daily-oxygen-saturation", "SPO2", lambda d: {"spo2": f"{d['averagePercentage']:.0f}"}, None),
                    ("daily-respiratory-rate", "RESP", lambda d: {"resp": f"{d['breathsPerMinute']:.1f}"}, None),
                    ("daily-sleep-temperature-derivations", "TEMP",
                     lambda d: {"temp_delta": f"{d['nightlyTemperatureCelsius'] - d['baselineTemperatureCelsius']:+.1f}"}, None),
                    ("daily-vo2-max", "VO2", lambda d: {"vo2max": f"{d['vo2Max']:.0f}",
                                                        "fitness": d.get("cardioFitnessLevel", "").replace("_", " ").title()}, 30)):
                body = dtype.replace("-", "_")
                d0 = since if not days else time.strftime("%Y-%m-%d", time.localtime(time.time() - days * 86400))
                for p in self.list(dtype, f'{body}.date >= "{d0}"'):
                    b = body_of(p); dt = b["date"]
                    vals = fn(b)
                    if group in ("RHR", "HRV"):
                        vals[f"{group.lower()}_src"] = source_name(p.get("dataSource", {}))
                    offer(group, "latest", vals, source_name(p.get("dataSource", {})),
                          when=time.mktime((dt["year"], dt["month"], dt["day"], 0, 0, 0, 0, 0, -1)))
        if self.google: step("google daily", g_daily)
        def g_sleep():
            for p in self.list("sleep", f'sleep.interval.end_time >= "{self.since(self.max_age)}"', 25):
                s = p["sleep"]["summary"]; st = {x["type"]: int(x["minutes"]) for x in s.get("stagesSummary", [])}
                asleep = int(s["minutesAsleep"]); period = int(s.get("minutesInSleepPeriod") or 0)
                src = source_name(p.get("dataSource", {}))
                vals = {"sleep": hm(asleep), "sleep_src": src,
                        "sleep_eff": round(100 * asleep / period) if period else None,
                        **{k.lower(): hm(st[k]) for k in ("DEEP", "REM", "LIGHT", "AWAKE") if k in st}}
                offer("SLEEP", "latest", vals, src, when=iso_epoch(p["sleep"]["interval"]["endTime"]), window=SLEEP_WINDOW)
        if self.google: step("google sleep", g_sleep)

        # --- Ultrahuman API
        def ultrahuman():
            for date in recent_days:
                m = self.uh.day(date)
                val = lambda t, k="value": (m.get(t) or {}).get(k)
                if date == today:
                    if (n := val("steps", "total")): offer("STEPS", "total", {"steps": f"{round(n):,}", "steps_src": "ultrahuman"}, "ultrahuman", amount=n)
                    if (n := val("active_minutes")): offer("ACTIVE", "total", {"active": n}, "ultrahuman", amount=n)
                    offer("MOVEMENT", "latest", {"movement": val("movement_index")}, "ultrahuman", when=day_epoch(date))
                w = day_epoch(date)
                offer("RHR", "latest", {"rhr": val("sleep_rhr"), "rhr_src": "ultrahuman"} if val("sleep_rhr") else {}, "ultrahuman", when=w)
                offer("HRV", "latest", {"hrv": val("avg_sleep_hrv"), "hrv_src": "ultrahuman"} if val("avg_sleep_hrv") else {}, "ultrahuman", when=w)
                offer("RECOVERY", "latest", {"recovery": val("recovery_index")}, "ultrahuman", when=w)
                offer("VO2", "latest", {"vo2max": val("vo2_max")}, "ultrahuman", when=w)
                sl = m.get("sleep") or {}
                if sl.get("index_valid"):
                    g = lambda k, f: (sl.get(k) or {}).get(f)
                    total, bed = g("total_sleep", "minutes"), g("time_in_bed", "minutes")
                    offer("SLEEP", "latest", {
                        "sleep": hm(total) if total else None, "sleep_src": "ultrahuman",
                        "sleep_score": g("sleep_score", "score"), "sleep_eff": g("sleep_efficiency", "percentage"),
                        "deep": hm(g("deep_sleep", "minutes")) if g("deep_sleep", "minutes") else None,
                        "rem": hm(g("rem_sleep", "minutes")) if g("rem_sleep", "minutes") else None,
                        "light": hm(g("light_sleep", "minutes")) if g("light_sleep", "minutes") else None,
                        "awake": hm(bed - total) if total and bed else None},
                        "ultrahuman", when=sl.get("bedtime_end") or w, window=SLEEP_WINDOW)
                    if (x := g("spo2", "value")): offer("SPO2", "latest", {"spo2": x}, "ultrahuman", when=w)
                    if (x := g("temperature_deviation", "celsius")) is not None:
                        offer("TEMP", "latest", {"temp_delta": f"{x:+.1f}"}, "ultrahuman", when=w)
        if self.uh:
            step("ultrahuman", ultrahuman)

        # --- Oura API
        def oura():
            def og(collection, **params):  # one collection failing (e.g. a missing scope) mustn't drop the rest
                try:
                    return self.oura.get(collection, **params)
                except RuntimeError as e:
                    log(f"stats: oura {collection}: {e}")
                    return []
            q = {"start_date": recent_days[-1], "end_date": time.strftime("%Y-%m-%d", time.localtime(time.time() + 86400))}
            scores = {d["day"]: d.get("score") for d in og("daily_sleep", **q)}
            for d in og("sleep", **q):
                if d.get("type") != "long_sleep" or not fresh_day(d["day"]):
                    continue
                mins = lambda k: hm(d[k] / 60) if d.get(k) else None
                offer("SLEEP", "latest", {
                    "sleep": mins("total_sleep_duration"), "sleep_src": "oura", "sleep_score": scores.get(d["day"]),
                    "sleep_eff": d.get("efficiency"), "deep": mins("deep_sleep_duration"), "rem": mins("rem_sleep_duration"),
                    "light": mins("light_sleep_duration"), "awake": mins("awake_time")},
                    "oura", when=iso_epoch(d["bedtime_end"]), window=SLEEP_WINDOW)
                w = day_epoch(d["day"])
                if d.get("lowest_heart_rate"): offer("RHR", "latest", {"rhr": d["lowest_heart_rate"], "rhr_src": "oura"}, "oura", when=w)
                if d.get("average_hrv"): offer("HRV", "latest", {"hrv": d["average_hrv"], "hrv_src": "oura"}, "oura", when=w)
            for d in og("daily_readiness", **q):
                if fresh_day(d["day"]):
                    offer("READINESS", "latest", {"readiness": d.get("score")}, "oura", when=day_epoch(d["day"]))
                    if d.get("temperature_deviation") is not None:
                        offer("TEMP", "latest", {"temp_delta": f"{d['temperature_deviation']:+.1f}"}, "oura", when=day_epoch(d["day"]))
            for d in og("daily_stress", **q):
                if fresh_day(d["day"]):
                    offer("STRESS", "latest", {
                        "stress": (d.get("day_summary") or "").title() or None,
                        "stress_high": hm(d["stress_high"] / 60) if d.get("stress_high") is not None else None,
                        "recovery_high": hm(d["recovery_high"] / 60) if d.get("recovery_high") is not None else None},
                        "oura", when=day_epoch(d["day"]))
            for d in og("daily_resilience", **q):
                if fresh_day(d["day"]):
                    offer("RESILIENCE", "latest", {"resilience": (d.get("level") or "").title() or None}, "oura", when=day_epoch(d["day"]))
            for d in og("daily_activity", **q):
                if d["day"] == today:
                    if d.get("steps"): offer("STEPS", "total", {"steps": f"{d['steps']:,}", "steps_src": "oura"}, "oura", amount=d["steps"])
                    if d.get("active_calories"): offer("CALORIES", "total", {"active_kcal": f"{d['active_calories']:,}"}, "oura", amount=d["active_calories"])
                    offer("ACTIVITY_SCORE", "latest", {"activity_score": d.get("score")}, "oura", when=day_epoch(today))
            for d in og("daily_spo2", **q):
                if fresh_day(d["day"]) and (x := (d.get("spo2_percentage") or {}).get("average")):
                    offer("SPO2", "latest", {"spo2": f"{x:.0f}"}, "oura", when=day_epoch(d["day"]))
            for d in og("vO2_max", start_date=time.strftime("%Y-%m-%d", time.localtime(time.time() - 30 * 86400)), end_date=q["end_date"]):
                if d.get("vo2_max"):
                    offer("VO2", "latest", {"vo2max": f"{d['vo2_max']:.0f}"}, "oura", when=day_epoch(d["day"]))
        if self.oura and os.path.exists(OURA_TOKEN_PATH):
            step("oura", oura)

        # --- merge: per group, highest total or most recent record wins; preference breaks ties
        groups = {}
        for c in cands:
            groups.setdefault(c["group"], []).append(c)
        for g, cs in groups.items():
            if cs[0]["kind"] == "total":
                best = min(cs, key=lambda c: (-c["amount"], self.rank(g, c["source"])))
            else:
                newest = max(c["when"] for c in cs)
                best = min([c for c in cs if newest - c["when"] <= c["window"]],
                           key=lambda c: (self.rank(g, c["source"]), -c["when"]))
            v.update(best["values"])

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
        if self.google: step("google other", other)
        self.values = v
        log("stats: " + (", ".join(f"{k}={x}" for k, x in sorted(v.items())) or "no stats"))

    def refresh_hr(self):
        """Newest heart-rate sample from any device (watch, rings...) within HR_FALLBACK_MAX_AGE."""
        max_age = float(C["HR_FALLBACK_MAX_AGE"]); cutoff = time.time() - max_age; samples = []  # (bpm, epoch, source)
        if self.google:
            for p in self.list("heart-rate", f'heart_rate.sample_time.physical_time >= "{self.since(max_age / 86400)}"'):
                t = calendar.timegm(time.strptime(p["heartRate"]["sampleTime"]["physicalTime"][:19], "%Y-%m-%dT%H:%M:%S"))
                samples.append((int(p["heartRate"]["beatsPerMinute"]), t, source_name(p.get("dataSource", {}))))
        if self.uh:
            for x in (self.uh.day(time.strftime("%Y-%m-%d")).get("hr") or {}).get("values") or []:
                if x.get("value"):
                    samples.append((int(x["value"]), x["timestamp"], "ultrahuman"))
        if self.oura and os.path.exists(OURA_TOKEN_PATH):
            for x in self.oura.get("heartrate", start_datetime=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(cutoff))):
                samples.append((int(x["bpm"]), x["timestamp_unix"] / 1000 if x.get("timestamp_unix") else
                                datetime.datetime.fromisoformat(x["timestamp"].replace("Z", "+00:00")).timestamp(), "oura"))
        samples = [x for x in samples if x[1] >= cutoff]
        if not samples:
            self.recent_hr = None; return
        newest = max(x[1] for x in samples)
        self.recent_hr = min([x for x in samples if newest - x[1] <= 60], key=lambda x: (self.rank("HR", x[2]), -x[1]))

    def run_hr_forever(self):
        while True:
            try:
                self.refresh_hr()
            except Exception as e:
                log(f"google: heart rate: {e}")
            time.sleep(float(C["HR_FALLBACK_INTERVAL"]))

    def fallback_hr(self):
        """(bpm, age_seconds, source) of a recent non-live reading, or None."""
        r = getattr(self, "recent_hr", None)
        if r and time.time() - r[1] <= float(C["HR_FALLBACK_MAX_AGE"]):
            return r[0], max(0, time.time() - r[1]), r[2]
        return None

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

# ------------------------------------------------------------------ phone push (docs/server-push.md in health-display-android)
PUSH_MAX_BODY = 64 * 1024

class PhonePush:
    """Receives value pushes from Health Display for Android on POST /push.

    While the last valid push is younger than PUSH_STALE_AFTER (server clock), the
    phone is "active" and its values replace the server's own Pulsoid/Google reads.
    """
    def __init__(self, token, port, stale):
        self.token = token.encode(); self.port = port; self.stale = stale
        self.lock = threading.Lock(); self.payload = None; self.received = 0; self.ip = None
        self.rejected = set()

    def reject(self, cause, ip):
        with self.lock:
            first = cause not in self.rejected; self.rejected.add(cause)
        if first:
            log(f"phone: rejected push from {ip}: {cause} (further '{cause}' rejections not logged)")

    def accept(self, payload, ip):
        with self.lock:
            self.payload = payload; self.received = time.time(); self.ip = ip

    def current(self):
        """(payload, ip) of the latest push while the phone is active, else (None, None)."""
        with self.lock:
            if self.payload is not None and time.time() - self.received < self.stale:
                return self.payload, self.ip
        return None, None

    @staticmethod
    def validate(d):
        if not isinstance(d, dict):
            return None, "body is not a JSON object"
        missing = [k for k in ("v", "sent_at", "live", "measured_at", "values") if k not in d]
        if missing:
            return None, f"missing fields: {', '.join(missing)}"
        if d["v"] != 1:
            return None, f"unsupported version v={d['v']!r}"
        if not isinstance(d["live"], bool) or not isinstance(d["values"], dict):
            return None, "bad field types"
        return {**d, "values": {str(k): str(x) for k, x in d["values"].items() if x is not None}}, None

    def start(self):
        import http.server
        push = self
        class Handler(http.server.BaseHTTPRequestHandler):
            server_version = "Health-Display"
            def client_ip(self):
                h = self.headers
                return (h.get("CF-Connecting-IP") or (h.get("X-Forwarded-For") or "").split(",")[0].strip()
                        or h.get("X-Real-IP") or self.client_address[0])
            def reply(self, code):
                self.send_response(code); self.send_header("Content-Length", "0")
                if code == 405:
                    self.send_header("Allow", "POST")
                self.end_headers()
            def route(self):
                return urllib.parse.urlparse(self.path).path == "/push"
            def do_POST(self):
                ip = self.client_ip()
                if not self.route():
                    return self.reply(404)
                auth = self.headers.get("Authorization", "")
                given = auth[7:].strip().encode() if auth.startswith("Bearer ") else b""
                if not hmac.compare_digest(given, push.token):
                    push.reject("bad or missing token", ip); return self.reply(401)
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    length = -1
                if length < 0 or length > PUSH_MAX_BODY:
                    push.reject("body too large or bad Content-Length", ip); return self.reply(413 if length > 0 else 400)
                try:
                    data = json.loads(self.rfile.read(length) or b"null")
                except ValueError:
                    push.reject("body is not JSON", ip); return self.reply(400)
                payload, err = PhonePush.validate(data)
                if err:
                    push.reject(err, ip); return self.reply(400)
                push.accept(payload, ip); self.reply(204)
            def other(self):
                self.reply(405 if self.route() else 404)
            do_GET = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = other
            def do_HEAD(self):
                self.reply(405 if self.route() else 404)
            def log_message(self, *a):
                pass
        srv = http.server.ThreadingHTTPServer(("0.0.0.0", self.port), Handler)
        srv.daemon_threads = True
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        log(f"phone: listening for pushes on :{self.port}/push")

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

def build_activity(hr, stats, started, phone=None):
    """Presence from the phone's latest push while it's active and live, else from our own sources."""
    if phone is not None and phone["live"]:
        values = phone["values"]; bpm = values.get("bpm")
    elif hr.live:  # also when the phone isn't live: our own sources (incl. ring fallback) may still have data
        values = {**(stats.values if stats else {}), **hr.values()}; bpm = hr.bpm
    elif stats and (fb := stats.fallback_hr()):  # no live HR: recent reading from any device, marked as such
        bpm = f"~{fb[0]}"; mins = round(fb[1] / 60)
        values = {**stats.values, **hr.values(), "bpm": bpm, "bpm_src": fb[2],
                  "bpm_age": "just now" if mins < 1 else f"{mins} min ago"}
    else:
        return None
    a = {"type": 0, "timestamps": {"start": int(started)}}
    if (details := render(C["DETAILS"], values)):
        a["details"] = details
    if (state := render(C["STATE"], values)):
        a["state"] = state
    if C["IMAGE"].lower() != "off" and bpm:
        a["assets"] = {"large_image": tile_url(bpm)}
        if (hover := render(C["IMG_HOVER"], values)):
            a["assets"]["large_text"] = hover
    return a

def main():
    if not C["PULSOID_TOKEN"] or not C["DISCORD_APP_ID"]:
        sys.exit(f"Set PULSOID_TOKEN and DISCORD_APP_ID in {CONFIG_PATH}")
    hr = HeartRate(); ipc = DiscordIPC(C["DISCORD_APP_ID"]); stats = None
    if (GoogleHealth.configured() and os.path.exists(GOOGLE_TOKEN_PATH)) or Ultrahuman.configured() \
            or (Oura.configured() and os.path.exists(OURA_TOKEN_PATH)):
        stats = HealthStats(); threading.Thread(target=stats.run_forever, daemon=True).start()
        if float(C["HR_FALLBACK_MAX_AGE"]) > 0:
            threading.Thread(target=stats.run_hr_forever, daemon=True).start()
    elif GoogleHealth.configured():
        log(f"google: not signed in ({GOOGLE_TOKEN_PATH} missing); run: health-display auth")
    push = None
    if C["PUSH_TOKEN"]:
        push = PhonePush(C["PUSH_TOKEN"], int(C["PUSH_PORT"]), float(C["PUSH_STALE_AFTER"])); push.start()
    started = time.time(); last_key = "init"; last_push = 0; next_poll = 0; warned = False; phone_active = False
    while True:
        now = time.time()
        phone, phone_ip = push.current() if push else (None, None)
        if (phone is not None) != phone_active:
            phone_active = phone is not None
            log(f"phone: active (pushes from {phone_ip})" if phone_active else "phone: inactive, using own sources")
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
        act = build_activity(hr, stats, started, phone)
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
            cmd, lambda a: sys.exit("usage: health-display [run | auth [google|oura] [redirect-url] | stats | probe [type...]]"))(rest)
    except KeyboardInterrupt:
        pass

#!/usr/bin/env python3
"""Pulsoid heart rate -> Discord Rich Presence (official local IPC, no user token).

Runs on the same Linux machine as the Discord desktop app. Python 3 stdlib only.
Config: ~/.config/hr-rpc/config.env (KEY=VALUE), overridable by environment vars.
"""
import json, os, socket, struct, sys, time, urllib.error, urllib.parse, urllib.request, uuid

CONFIG_PATH = os.path.expanduser(os.environ.get("HR_RPC_CONFIG", "~/.config/hr-rpc/config.env"))

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
if not C["PULSOID_TOKEN"] or not C["DISCORD_APP_ID"]:
    sys.exit(f"Set PULSOID_TOKEN and DISCORD_APP_ID in {CONFIG_PATH}")

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
            headers={"Authorization": f"Bearer {C['PULSOID_TOKEN']}", "User-Agent": "hr-rpc"})
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
    try:
        main()
    except KeyboardInterrupt:
        pass

# Health Display

Live heart rate (Pulsoid) plus daily health stats (Google Health API) as
Discord Rich Presence, using Discord's official local RPC socket (no user
token, no selfbot). Python 3 standard library only.

## Setup

1. Create a Discord application at https://discord.com/developers/applications.
   Its **name** is what shows as "Playing <name>" (e.g. "Heart Rate").
2. Get a Pulsoid token with scope `data:heart_rate:read` at https://pulsoid.net/ui/keys.
3. Copy `config.env.example` to `config.env` and fill in `PULSOID_TOKEN` and `DISCORD_APP_ID`.

### Google Health stats (optional)

Shows steps, active minutes, resting HR, HRV, sleep and more from a Pixel Watch /
Fitbit, plus anything other apps (Oura, Ultrahuman, …) write to Health Connect.

1. In a Google Cloud project, enable the **Google Health API**, configure the
   OAuth consent screen (add yourself as a test user and the
   `googlehealth.*.readonly` scopes) and create an OAuth client of type
   **Web application** with redirect URI `http://localhost:8765/`.
2. Put `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` in `config.env`.
3. Run `health-display auth google`, open the printed URL and approve. If the browser
   isn't on the same machine, the redirect page fails to load — paste its full
   address back into the prompt (or pass it as an argument).
4. `health-display stats` prints the values it can show.

In Testing mode Google expires the sign-in after 7 days; re-run `auth` then.
Placeholders and per-stat source selection are documented in `config.env.example`.

### Oura and Ultrahuman (optional)

Health Connect only carries the rings' raw data. Their APIs add sleep score,
readiness, stress and resilience (Oura) and Recovery / Movement Index
(Ultrahuman), and fresher ring heart rate for the HR fallback. They join the
same source merge as everything else.

- **Ultrahuman:** create a personal token at https://vision.ultrahuman.com/developer
  and set `ULTRAHUMAN_TOKEN`.
- **Oura:** create an app at https://cloud.ouraring.com/oauth/applications with
  redirect URI `http://localhost:8765/`, set `OURA_CLIENT_ID` and
  `OURA_CLIENT_SECRET`, then run `health-display auth oura` (same paste-the-URL
  flow as Google). Requires an active Oura membership.

### Phone push (optional)

When [Health Display for Android](https://github.com/KoriKosmos/health-display-android)
is running, it can push its values here so the server's presence shows exactly
what the phone shows. When the pushes stop for `PUSH_STALE_AFTER` seconds (60),
the server falls back to its own Pulsoid/Google reads.

1. Generate a shared secret: `openssl rand -hex 32`.
2. In `config.env` set `PUSH_TOKEN` to it and `PUSH_URL` to the public address of
   the server's `/push` endpoint (e.g. `https://health.example.com/push`, reverse
   proxied to port `PUSH_PORT`, default 8766). Optional: `PUSH_PORT`,
   `PUSH_STALE_AFTER`.
3. Import the same `config.env` on the phone; `PUSH_URL` is only used there.

The server listens on `POST /push` only while `PUSH_TOKEN` is set. Test it with:

```sh
curl -i -X POST https://health.example.com/push \
  -H "Authorization: Bearer $PUSH_TOKEN" -H 'Content-Type: application/json' \
  -d '{"v":1,"sent_at":0,"live":true,"measured_at":0,"values":{"bpm":"77","steps":"1,234"}}'
```

It should return `204`. The protocol is specified in the Android repo's
[`docs/server-push.md`](https://github.com/KoriKosmos/health-display-android/blob/main/docs/server-push.md).

## Run in a container (headless server)

Runs the official Discord desktop client in a browser-accessible desktop
(`linuxserver/baseimage-selkies`) with `health_display.py` alongside it.

```sh
cp .env.example .env   # set WEB_PASSWORD
docker compose up -d --build
```

Open `https://<host>:3011`, log in to Discord once; the profile persists in `./data`.
For Google sign-in: `docker exec -it -u abc health-display health-display auth`.
`root/defaults/discord-session` relaunches Discord if it hangs or exits.

## Run on a Linux desktop

With the Discord app installed and running:

```sh
./install.sh
systemctl --user enable --now health-display
journalctl --user -u health-display -f
```

## Configuration

See `config.env.example`: presence text templates (`{bpm} {min} {max} {avg}`),
the generated BPM image tile, and poll/update/stale intervals.

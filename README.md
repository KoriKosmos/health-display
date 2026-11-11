# hr-rpc

Pulsoid heart rate → Discord Rich Presence, using Discord's official local RPC
socket (no user token, no selfbot). Python 3 standard library only.

## Setup

1. Create a Discord application at https://discord.com/developers/applications.
   Its **name** is what shows as "Playing <name>" (e.g. "Heart Rate").
2. Get a Pulsoid token with scope `data:heart_rate:read` at https://pulsoid.net/ui/keys.
3. Copy `config.env.example` to `config.env` and fill in `PULSOID_TOKEN` and `DISCORD_APP_ID`.

## Run in a container (headless server)

Runs the official Discord desktop client in a browser-accessible desktop
(`linuxserver/baseimage-selkies`) with `hr_rpc.py` alongside it.

```sh
cp .env.example .env   # set WEB_PASSWORD
docker compose up -d --build
```

Open `https://<host>:3011`, log in to Discord once; the profile persists in `./data`.
`root/defaults/discord-session` relaunches Discord if it hangs or exits.

## Run on a Linux desktop

With the Discord app installed and running:

```sh
./install.sh
systemctl --user enable --now hr-rpc
journalctl --user -u hr-rpc -f
```

## Configuration

See `config.env.example`: presence text templates (`{bpm} {min} {max} {avg}`),
the generated BPM image tile, and poll/update/stale intervals.

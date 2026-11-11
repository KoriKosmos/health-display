#!/bin/sh
# Installs Health Display as a systemd user service (hr-rpc) that starts with your desktop session.
set -e
here="$(cd "$(dirname "$0")" && pwd)"
mkdir -p ~/.local/bin ~/.config/hr-rpc ~/.config/systemd/user
install -m 755 "$here/hr_rpc.py" ~/.local/bin/hr-rpc
if [ ! -f ~/.config/hr-rpc/config.env ]; then
  install -m 600 "$here/config.env.example" ~/.config/hr-rpc/config.env
  echo "Created ~/.config/hr-rpc/config.env — fill in PULSOID_TOKEN and DISCORD_APP_ID."
fi
cat > ~/.config/systemd/user/hr-rpc.service <<'EOF'
[Unit]
Description=Health Display (Pulsoid heart rate -> Discord Rich Presence)
After=graphical-session.target
PartOf=graphical-session.target

[Service]
ExecStart=%h/.local/bin/hr-rpc
Restart=on-failure
RestartSec=10

[Install]
WantedBy=graphical-session.target
EOF
systemctl --user daemon-reload
echo "Then run: systemctl --user enable --now hr-rpc   (logs: journalctl --user -u hr-rpc -f)"

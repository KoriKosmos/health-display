#!/bin/sh
# Installs Health Display as a systemd user service that starts with your desktop session.
set -e
here="$(cd "$(dirname "$0")" && pwd)"
mkdir -p ~/.local/bin ~/.config/health-display ~/.config/systemd/user
install -m 755 "$here/health_display.py" ~/.local/bin/health-display
if [ ! -f ~/.config/health-display/config.env ]; then
  install -m 600 "$here/config.env.example" ~/.config/health-display/config.env
  echo "Created ~/.config/health-display/config.env — fill in PULSOID_TOKEN and DISCORD_APP_ID."
fi
cat > ~/.config/systemd/user/health-display.service <<'EOF'
[Unit]
Description=Health Display (Pulsoid heart rate -> Discord Rich Presence)
After=graphical-session.target
PartOf=graphical-session.target

[Service]
ExecStart=%h/.local/bin/health-display
Restart=on-failure
RestartSec=10

[Install]
WantedBy=graphical-session.target
EOF
systemctl --user daemon-reload
echo "Then run: systemctl --user enable --now health-display   (logs: journalctl --user -u health-display -f)"

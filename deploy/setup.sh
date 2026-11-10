#!/usr/bin/env bash
# One-time setup of the bot on a fresh Ubuntu 24.04 server (AWS EC2 or Lightsail).
# Run as the default "ubuntu" user from inside the cloned repo:
#     git clone https://github.com/chendude404/DiscBot.git && cd DiscBot && bash deploy/setup.sh
# Safe to run again (e.g. after a git pull that changes requirements.txt).
#
# Before starting the service, copy from your PC (they're gitignored):
#     .env    (add CLAUDE_CODE_OAUTH_TOKEN=... from `claude setup-token`)
#     data/   (data.json, archive.json, my_schedule.py)
set -euo pipefail
cd "$(dirname "$0")/.."
REPO="$(pwd)"

sudo apt-get update
sudo apt-get install -y python3-venv python3-pip git curl ca-certificates

# Node 22 for `npx brightspace-mcp-server` (Brightspace announcements)
if ! command -v node >/dev/null || [ "$(node -v | cut -d. -f1 | tr -d v)" -lt 20 ]; then
    curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
    sudo apt-get install -y nodejs
fi

# 1 GB instances run out of memory while Claude Code starts; add 2 GB of swap once.
if ! swapon --show | grep -q /swapfile; then
    sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile
    sudo mkswap /swapfile && sudo swapon /swapfile
    echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
fi

python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
mkdir -p data

sed -e "s|__USER__|$(whoami)|g" -e "s|__REPO__|$REPO|g" deploy/discbot.service \
    | sudo tee /etc/systemd/system/discbot.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable discbot

echo
echo "Installed. Once .env and data/ are copied in:"
echo "    sudo systemctl start discbot        # start (and on every boot)"
echo "    journalctl -u discbot -f            # live log"

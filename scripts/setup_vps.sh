#!/usr/bin/env bash
# =============================================================================
# VPS Bootstrap Script
# Run ONCE on a fresh Ubuntu 22.04 VPS as root (or with sudo).
# Sets up Docker, creates the app user, configures firewall, and
# creates the application directory structure.
#
# Usage:
#   wget -O setup.sh https://raw.githubusercontent.com/your-repo/main/scripts/setup_vps.sh
#   chmod +x setup.sh
#   sudo bash setup.sh
# =============================================================================

set -euo pipefail

APP_USER="recon"
APP_DIR="/opt/drift-recon"
GITHUB_REPO="your-github-username/drift-recon"  # CHANGE THIS

# ── Colors ─────────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()    { echo -e "${GREEN}[INFO]${NC} $1"; }
warning() { echo -e "${YELLOW}[WARN]${NC} $1"; }
error()   { echo -e "${RED}[ERROR]${NC} $1"; exit 1; }

[[ $EUID -ne 0 ]] && error "Run as root: sudo bash $0"

# ── System update ──────────────────────────────────────────────────────────────
info "Updating system packages..."
apt-get update -qq
apt-get upgrade -y -qq
apt-get install -y -qq \
    curl wget git unzip jq \
    ufw fail2ban \
    logrotate \
    htop iotop \
    ca-certificates gnupg lsb-release

# ── Docker ─────────────────────────────────────────────────────────────────────
info "Installing Docker..."
if ! command -v docker &>/dev/null; then
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
        | gpg --dearmor -o /usr/share/keyrings/docker-archive-keyring.gpg

    echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/docker-archive-keyring.gpg] \
        https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" \
        > /etc/apt/sources.list.d/docker.list

    apt-get update -qq
    apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-compose-plugin
    systemctl enable docker
    systemctl start docker
    info "Docker installed: $(docker --version)"
else
    info "Docker already installed: $(docker --version)"
fi

# ── App user ───────────────────────────────────────────────────────────────────
info "Creating application user: $APP_USER"
if ! id "$APP_USER" &>/dev/null; then
    useradd --system --create-home --shell /bin/bash "$APP_USER"
    usermod -aG docker "$APP_USER"
    info "User $APP_USER created"
else
    warning "User $APP_USER already exists"
fi

# Add current SSH keys to app user for deployment
if [[ -d /root/.ssh ]]; then
    mkdir -p /home/$APP_USER/.ssh
    cp /root/.ssh/authorized_keys /home/$APP_USER/.ssh/authorized_keys 2>/dev/null || true
    chown -R $APP_USER:$APP_USER /home/$APP_USER/.ssh
    chmod 700 /home/$APP_USER/.ssh
    chmod 600 /home/$APP_USER/.ssh/authorized_keys 2>/dev/null || true
fi

# ── App directory ──────────────────────────────────────────────────────────────
info "Creating application directory: $APP_DIR"
mkdir -p "$APP_DIR"/{nginx/ssl,nginx/conf.d,postgres,scripts}
chown -R "$APP_USER:$APP_USER" "$APP_DIR"

# ── Firewall ───────────────────────────────────────────────────────────────────
info "Configuring UFW firewall..."
ufw --force reset
ufw default deny incoming
ufw default allow outgoing
ufw allow ssh
ufw allow 80/tcp
ufw allow 443/tcp
# Block direct access to app ports (only accessible via nginx)
ufw deny 8000/tcp
ufw deny 8501/tcp
ufw deny 5432/tcp
ufw deny 6379/tcp
ufw --force enable
info "Firewall configured"

# ── Fail2ban (basic SSH brute-force protection) ────────────────────────────────
info "Configuring fail2ban..."
cat > /etc/fail2ban/jail.local <<'EOF'
[DEFAULT]
bantime = 3600
findtime = 600
maxretry = 5

[sshd]
enabled = true
port = ssh
logpath = %(sshd_log)s
backend = systemd
EOF
systemctl enable fail2ban
systemctl restart fail2ban

# ── Swap (important on low-memory VPS) ────────────────────────────────────────
info "Configuring swap..."
if ! swapon --show | grep -q /swapfile; then
    fallocate -l 2G /swapfile
    chmod 600 /swapfile
    mkswap /swapfile
    swapon /swapfile
    echo '/swapfile none swap sw 0 0' >> /etc/fstab
    # Reduce swappiness (prefer RAM, only swap when necessary)
    echo 'vm.swappiness=10' >> /etc/sysctl.conf
    sysctl -p
    info "2GB swap configured"
else
    info "Swap already configured"
fi

# ── Log rotation ───────────────────────────────────────────────────────────────
info "Configuring log rotation for Docker containers..."
cat > /etc/logrotate.d/docker-containers <<'EOF'
/var/lib/docker/containers/*/*.log {
    rotate 7
    daily
    compress
    delaycompress
    missingok
    notifempty
    copytruncate
}
EOF

# ── Automated security updates ─────────────────────────────────────────────────
info "Enabling automatic security updates..."
apt-get install -y -qq unattended-upgrades
echo 'Unattended-Upgrade::Automatic-Reboot "false";' \
    >> /etc/apt/apt.conf.d/50unattended-upgrades

# ── Docker daemon config ───────────────────────────────────────────────────────
info "Configuring Docker daemon..."
cat > /etc/docker/daemon.json <<'EOF'
{
    "log-driver": "json-file",
    "log-opts": {
        "max-size": "50m",
        "max-file": "5"
    },
    "live-restore": true
}
EOF
systemctl reload docker

# ── GHCR login ─────────────────────────────────────────────────────────────────
info "To pull images from GHCR, run:"
echo "  echo \$GITHUB_TOKEN | docker login ghcr.io -u \$GITHUB_USER --password-stdin"

# ── Summary ────────────────────────────────────────────────────────────────────
info "
=======================================================
 VPS Bootstrap complete!

 Next steps:
 1. As ${APP_USER}: cd ${APP_DIR}
 2. Clone your repo:
      git clone https://github.com/${GITHUB_REPO} .
 3. Copy and fill in environment:
      cp .env.example .env && nano .env
 4. Add SSL certificates to nginx/ssl/:
      nginx/ssl/fullchain.pem
      nginx/ssl/privkey.pem
    (Use: certbot certonly --standalone -d yourdomain.com)
 5. Start the stack:
      docker compose up -d
 6. Check logs:
      docker compose logs -f api
=======================================================
"

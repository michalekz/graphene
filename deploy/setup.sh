#!/usr/bin/env bash
# Graphene Intel — VPS Setup Script
# Tested on RHEL 10 / Ubuntu 24.04
# Run as root: bash /opt/grafene/deploy/setup.sh

set -euo pipefail

PROJECT_DIR="/opt/grafene"
LOG_DIR="/var/log/graphene-intel"
DATA_DIR="${PROJECT_DIR}/data"
VENV="${PROJECT_DIR}/.venv"

echo "=== Graphene Intel Setup ==="
echo "Project: ${PROJECT_DIR}"
echo ""

# ── System dependencies ──────────────────────────────────────────────────────
echo "[1/6] Installing system packages..."
if command -v dnf &>/dev/null; then
    # RHEL/CentOS/Fedora
    dnf install -y python3.12 python3.12-pip python3.12-devel git curl
elif command -v apt-get &>/dev/null; then
    # Ubuntu/Debian
    apt-get update -qq
    apt-get install -y python3.12 python3.12-venv python3-pip git curl
fi

# ── uv (fast Python package manager) ────────────────────────────────────────
echo "[2/6] Installing uv..."
if ! command -v uv &>/dev/null; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
fi
echo "uv: $(uv --version)"

# ── Python virtual environment ───────────────────────────────────────────────
echo "[3/6] Creating venv and installing dependencies..."
cd "${PROJECT_DIR}"
uv venv --python python3.12 "${VENV}"
uv pip install --python "${VENV}/bin/python" -e ".[dev]"

# ── Directories ──────────────────────────────────────────────────────────────
echo "[4/6] Creating directories..."
mkdir -p "${LOG_DIR}" "${DATA_DIR}"

# ── Environment file ─────────────────────────────────────────────────────────
echo "[5/6] Setting up .env..."
if [ ! -f "${PROJECT_DIR}/.env" ]; then
    cp "${PROJECT_DIR}/.env.example" "${PROJECT_DIR}/.env"
    echo ""
    echo "⚠️  Please edit ${PROJECT_DIR}/.env and fill in:"
    echo "    ANTHROPIC_API_KEY=..."
    echo "    TELEGRAM_BOT_TOKEN=..."
    echo "    TELEGRAM_CHAT_ID=..."
    echo ""
fi

# ── Crontab ──────────────────────────────────────────────────────────────────
echo "[6/6] Installing crontab..."
crontab "${PROJECT_DIR}/deploy/crontab"
echo "Crontab installed. Current:"
crontab -l

echo ""
echo "=== Setup Complete ==="
echo ""
echo "Next steps:"
echo "  1. Edit .env: nano ${PROJECT_DIR}/.env"
echo "  2. Test Telegram: ${VENV}/bin/python scripts/setup_telegram.py"
echo "  3. Run first collection: ${VENV}/bin/python scripts/collect.py"
echo "  4. Run first evaluation: ${VENV}/bin/python scripts/evaluate.py"
echo "  5. Check logs: tail -f ${LOG_DIR}/collect.log"

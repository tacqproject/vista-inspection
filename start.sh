#!/bin/bash
# Spusti backend i frontend najednou. Ukonceni: Ctrl+C zastavi oboje.

cd "$(dirname "$0")"

# Aktivuj docker skupinu, pokud ji session jeste nema (napr. pred odhlasenim/prihlasenim)
if ! id -nG | grep -qw docker && getent group docker | grep -qw "$USER"; then
    exec sg docker -c "$(realpath "$0")"
fi

export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"

# Zabij pripadne bezici instance z drivejska
# (pkill zabije i uvicorn reloader, ktery by si port jinak obnovil)
pkill -f "uvicorn app.main:app" 2>/dev/null
pkill -f "$PWD/frontend/node_modules/.bin/vite" 2>/dev/null
fuser -k 8000/tcp 5173/tcp 2>/dev/null
sleep 1

trap 'kill 0' EXIT

(cd backend && .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload) &
(cd frontend && npm run dev) &

echo ""
echo "  VISTA bezi:  http://10.10.1.80:5173  (backend :8000)"
echo "  Zastaveni:   Ctrl+C"
echo ""

wait

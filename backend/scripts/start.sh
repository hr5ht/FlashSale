#!/bin/sh
# Container start: run migrations, start the server, and (when SEED_DEMO=true)
# add the demo data through the API once the server answers /health.
set -e

alembic upgrade head

if [ "${SEED_DEMO:-false}" != "true" ]; then
    exec uvicorn app.main:app --host 0.0.0.0 --port 8000
fi

uvicorn app.main:app --host 0.0.0.0 --port 8000 &
server=$!
trap 'kill -TERM "$server" 2>/dev/null' TERM INT

python - <<'EOF'
import time
import urllib.request

for _ in range(60):
    try:
        urllib.request.urlopen("http://127.0.0.1:8000/health", timeout=2)
        break
    except OSError:
        time.sleep(0.5)
else:
    raise SystemExit("server did not become healthy, skipping demo seed")
EOF
python scripts/seed_demo.py --base-url http://127.0.0.1:8000 \
    || echo "demo seed failed; the server is still running"

set +e
wait "$server"
status=$?
# A trapped signal interrupts the first wait; wait again for a clean shutdown.
if kill -0 "$server" 2>/dev/null; then
    wait "$server"
    status=$?
fi
exit "$status"

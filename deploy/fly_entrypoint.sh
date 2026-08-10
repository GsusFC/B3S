#!/bin/sh
set -eu

# The mounted encrypted volume root is fixed by the container runtime, not by
# the web uid. Keep it sticky and let the fd-relative helper prepare only the
# two application directories without following or chowning existing paths.
chown root:root /data
chmod 1777 /data
/usr/local/bin/python /usr/local/lib/b3s/prepare_vault_volume.py

if [ -z "${RELEASE_COMMAND:-}" ] \
    && [ "${B3S_VAULT_WORKER_ENABLED:-}" = "true" ]; then
    exec /usr/local/bin/python /usr/local/lib/b3s/vault_worker_supervisor.py "$@"
fi

# Inline worker capabilities are forbidden. Scrub legacy/mis-staged values
# before the default-off web process or a Fly release_command Machine starts.
unset \
    B3S_VAULT_WORKER_PRIVATE_KEY_B64 \
    B3S_VAULT_WORKER_PUBLIC_KEY_REGISTRY_JSON \
    B3S_VAULT_WORKER_INGEST_DSN \
    B3S_VAULT_WORKER_EXA_API_KEY \
    PGOPTIONS PGSERVICE PGHOST PGPORT PGHOSTADDR PGDATABASE PGUSER \
    PGPASSWORD PGSSLMODE PGCHANNELBINDING PGSERVICEFILE PGSYSCONFDIR \
    PGREQUIRESSL PGTARGETSESSIONATTRS PGAPPNAME
exec gosu b3s "$@"

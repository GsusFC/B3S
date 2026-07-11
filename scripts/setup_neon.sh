#!/usr/bin/env bash
set -euo pipefail

if [[ -z "${NEON_API_KEY:-}" ]]; then
  echo "NEON_API_KEY no está definido en este terminal" >&2
  exit 1
fi

api="https://console.neon.tech/api/v2"
name="${NEON_PROJECT_NAME:-b3s-history}"
region="${NEON_REGION:-aws-eu-central-1}"

if [[ -n "${NEON_DATABASE_URL:-}" ]]; then
  if command -v fly >/dev/null 2>&1 && [[ "${SET_FLY_SECRET:-1}" == "1" ]]; then
    fly secrets set B3S_DATABASE_URL="$NEON_DATABASE_URL" -a "${FLY_APP:-b3s}"
    echo "Secreto B3S_DATABASE_URL configurado en Fly"
  else
    echo "Fly CLI no disponible; configura B3S_DATABASE_URL manualmente"
  fi
  exit 0
fi

project_payload="$(jq -nc \
  --arg name "$name" \
  --arg region "$region" \
  '{project: {name: $name, region_id: $region}}')"

json="$(curl -fsS -X POST "$api/projects" \
  -H "Authorization: Bearer $NEON_API_KEY" \
  -H 'Content-Type: application/json' \
  -d "$project_payload")" \
  || { echo "Neon rechazó la creación del proyecto (HTTP 400); créalo desde console.neon.tech y vuelve a ejecutar con NEON_DATABASE_URL; no se modificó Fly" >&2; exit 1; }

project_id="$(printf '%s' "$json" | jq -er '.project.id')"
connection_uri="$(curl -fsS "$api/projects/$project_id/connection_uri" \
  -H "Authorization: Bearer $NEON_API_KEY" | jq -er '.uri')" \
  || { echo "No se pudo obtener el connection URI; no se modificó Fly" >&2; exit 1; }

printf '%s\n' "Proyecto Neon creado: $project_id"
if command -v fly >/dev/null 2>&1 && [[ "${SET_FLY_SECRET:-1}" == "1" ]]; then
  fly secrets set B3S_DATABASE_URL="$connection_uri" -a "${FLY_APP:-b3s}"
  echo "Secreto B3S_DATABASE_URL configurado en Fly (la URI no se imprimió)"
else
  echo "Fly CLI no disponible; configura B3S_DATABASE_URL manualmente sin imprimir la URI"
fi
printf '%s\n' "Después ejecuta: fly deploy --remote-only -a b3s"

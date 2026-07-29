# Evidence scoring recovery review v1

## Veredicto

La procedencia reproducible y la identidad correcta no bastan para reutilizar
una evidencia histórica en scoring. También hay que demostrar que la cita
satisface el contrato semántico del tile concreto.

Esta capa cierra ese hueco sin activar producción:

- las recuperaciones se deduplican por marca, rúbrica, tile y evidencia;
- cada candidato incluye la condición y el contrato vigente del tile;
- las decisiones son `accepted`, `disputed`, `rejected` o `revoked`;
- los eventos forman una cadena append-only con secuencia y predecesor;
- una revocación devuelve la asociación a estado pendiente;
- solo un `accepted` vigente puede modificar el score revisado de sombra;
- `runtime_effect=false`, `authority=false` y
  `automatic_scoring_effect=false`.

El preview candidato sigue mostrando qué ocurriría si todas las asociaciones
se aceptaran. El score revisado de sombra muestra únicamente las asociaciones
humanamente aceptadas. Ninguno de los dos altera un informe o score operativo.

`PostgresHistoryRepository.get_evidence_scoring_memory_preview()` reconstruye
ambas lecturas desde los informes persistidos. Mientras no exista un evento
semántico aceptado, el preview candidato puede mostrar un delta, pero
`reviewed_shadow.scoring.score_delta` permanece en `0`.

## Journal PostgreSQL y API

La revisión operativa se guarda en
`b3s_history.evidence_scoring_recovery_review_events`. La tabla es append-only:
cada decisión tiene secuencia, predecesor, revisor, actor, justificación,
versión e idempotencia. No existe fallback de escritura a JSON ni a memoria.
Si PostgreSQL no está disponible, la API de escritura responde `503`.

Los recursos autenticados son:

```text
GET  /api/v1/brands/{domain}/evidence-scoring-memory-preview
GET  /api/v1/brands/{domain}/evidence-scoring-recovery-reviews
POST /api/v1/brands/{domain}/evidence-scoring-recovery-reviews
```

El primer recurso expone en paralelo:

- `scoring`: resultado candidato si se aceptaran todas las asociaciones;
- `reviewed_shadow.scoring`: resultado que incorpora únicamente decisiones
  `accepted` vigentes;
- `recovery_review_candidates`: sujetos exactos que pueden revisarse;
- `recovery_review.journal`: eventos vigentes aplicables y eventos obsoletos.

Una escritura requiere la credencial `evidence:adjudicate`, un
`Idempotency-Key` y `expected_current_event_id`. El revisor no procede del
JSON del cliente: el servidor lo liga a `B3S_EVIDENCE_REVIEWER_ID`.

Ejemplo de primera decisión:

```bash
curl -sS -X POST \
  -H "Authorization: Bearer $B3S_EVIDENCE_ADJUDICATION_TOKEN" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: scoring-review-<candidate-fingerprint>" \
  -d '{
    "subject_id": "<candidate_fingerprint>",
    "case_id": "<case_id>",
    "decision": "accepted",
    "expected_current_event_id": null,
    "reason_code": "tile_contract_satisfied",
    "rationale": "La cita satisface específicamente el contrato del tile.",
    "evaluator_version": "manual-review-v1"
  }' \
  http://127.0.0.1:8000/api/v1/brands/example.com/evidence-scoring-recovery-reviews
```

Para una decisión posterior, `expected_current_event_id` debe contener el
`event.id` vigente. Reutilizar la misma clave con otra solicitud o escribir
desde una lectura obsoleta responde `409`.

El preview se reconstruye después de cada proceso o reinicio usando los
informes inmutables y la decisión vigente de cada sujeto. Si una evolución de
la rúbrica o de la evidencia cambia el candidato, el evento anterior se
conserva en el journal pero aparece como obsoleto y no modifica ni siquiera el
score revisado de sombra.

## Prueba PostgreSQL de durabilidad

La integración
`test_postgres_scoring_recovery_survives_restart_and_revocation` ejecuta el
ciclo completo contra una base PostgreSQL real:

1. aplica todas las migraciones, incluidas `007–008`;
2. persiste dos informes de una marca, con evidencia `MG1` en el primero y un
   punto ciego en el segundo;
3. reconstruye el candidato de recuperación y confirma que sigue pendiente;
4. añade un evento `accepted`;
5. crea otra instancia de `PostgresHistoryRepository` y confirma que el delta
   revisado se reconstruye desde informes y journal;
6. añade `revoked`;
7. crea una tercera instancia y confirma que el delta revisado vuelve a cero,
   mientras ambos eventos permanecen en el journal.

La prueba solo se habilita contra una base desechable porque elimina el esquema
aislado `b3s_history` al empezar y al terminar:

```bash
B3S_TEST_DATABASE_URL=postgresql://... \
B3S_ALLOW_SCHEMA_DROP=1 \
./.venv/bin/python -m pytest \
  tests/test_b3s_history.py::test_postgres_scoring_recovery_survives_restart_and_revocation \
  -q
```

El 29 de julio de 2026 se ejecutó correctamente sobre un clúster temporal
PostgreSQL 14: `1 passed`. La CI de PR #27 volvió a ejecutar la suite con el
servicio `postgres:16`, `B3S_TEST_DATABASE_URL` y
`B3S_ALLOW_SCHEMA_DROP=1`; terminó con `2287 passed` y un único skip ajeno a
PostgreSQL. Esto demuestra el contrato de persistencia y reconstrucción tanto
en PostgreSQL 14 como en el major 16 usado por producción.

La integración
`test_release_migrate_only_cli_is_complete_and_idempotent` cubre además la
entrada usada por el `release_command`: ejecuta
`import_b3s_reports_postgres.py --migrate-only` sin proporcionar
`--database-url`, comprueba que la primera ejecución aplica `001–008`, que la
segunda aplica cero migraciones y que las tablas de ambos journals semánticos
existen. Esto valida la ruta CLI y su idempotencia; todavía no equivale a
ejecutar el release real dentro de una imagen desplegada en Fly.

## Flujo de archivo para lotes históricos

Generar una plantilla sin firma desde el archivo histórico:

```bash
./.venv/bin/python scripts/brand3_sqlite_memory_backfill.py \
  /ruta/a/brand3.sqlite3 \
  --current-reports-dir data/reports \
  --write-review-template docs/scoring_recovery_reviews.jsonl
```

Evaluar eventos completados:

```bash
./.venv/bin/python scripts/brand3_sqlite_memory_backfill.py \
  /ruta/a/brand3.sqlite3 \
  --current-reports-dir data/reports \
  --reviews docs/scoring_recovery_reviews.jsonl
```

El JSONL es una vía de evaluación offline. No sustituye al journal PostgreSQL
usado por la API y nunca actúa como fallback de producción.

Una primera decisión válida tiene esta forma:

```json
{
  "schema_version": "evidence-scoring-recovery-review-event-v1",
  "case_id": "<candidate case id>",
  "candidate_fingerprint": "<candidate fingerprint>",
  "event_id": "<stable unique event id>",
  "sequence": 1,
  "previous_event_id": null,
  "decision": "accepted",
  "reviewer_id": "gsus",
  "rationale": "La cita satisface específicamente el contrato del tile.",
  "reviewed_at": "2026-07-29T17:00:00+02:00",
  "runtime_effect": false,
  "authority": false
}
```

Para sustituir una decisión se añade otro evento con `sequence=2` y
`previous_event_id` apuntando al evento vigente. Para revocarla se usa
`decision=revoked`; no se edita ni elimina el evento original.

## Validación real sobre Brand3

El archivo de 778 MB produce:

- 29 escaneos compatibles;
- 20 capturas únicas;
- 13 dominios;
- 1.400 ocurrencias de evidencia normalizadas en los 20 informes;
- 174 evidencias mecánicamente elegibles después de deduplicación y filtros;
- 2 asociaciones semánticas únicas capaces de modificar el scoring candidato;
- 0 asociaciones aceptadas actualmente;
- delta revisado de sombra: `0`.

Las 174 no son decisiones humanas ni evidencia ya autorizada para scoring.
Describen la salida de reglas mecánicas de elegibilidad. Las 1.400 tampoco son
evidencias únicas: incluyen ocurrencias repetidas entre capturas y componentes.
El archivo no contiene `claim_slot_id`, variantes, relaciones ni mappings
semánticos persistidos, por lo que esas estructuras no se reconstruyen
retroactivamente.

Los dos casos son:

1. `archetype.fund`, `coherencia.C10`: la cita describe experiencia del equipo,
   pero no demuestra que producto y marca sean inseparables.
2. `vercel.com`, `coherencia.C5`: la cita demuestra una promesa operativa,
   pero no basta por sí sola para probar que la personalidad ejecuta los
   valores declarados o inferidos.

La primera asociación parece un falso positivo semántico. La segunda sigue
siendo plausible pero no demostrada con el contexto congelado. Permanecen
pendientes hasta que el revisor emita eventos atribuibles.

## Límite de activación

Completar estas dos revisiones valida el comportamiento del lote histórico,
pero no demuestra generalización. La activación seguirá bloqueada hasta adoptar
una política de promoción y validar más marcas, tiles, polaridades y cambios
temporales reales.

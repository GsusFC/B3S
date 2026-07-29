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

## Flujo

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
- 174 evidencias reproducibles;
- 2 asociaciones semánticas únicas capaces de modificar el scoring candidato;
- 0 asociaciones aceptadas actualmente;
- delta revisado de sombra: `0`.

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

# Brand3 SQLite archive import v1

## Veredicto

El archivo histórico de Brand3 sí puede ampliar la historia persistida de B3S,
pero no aporta por sí solo nuevos claims semánticos ni autoriza scoring. La
importación conserva únicamente informes SV9 compatibles y los separa del
workspace operativo.

La frontera fija es:

- origen SQLite abierto en modo de solo lectura;
- validación completa antes de conectar a PostgreSQL;
- `dry-run` por defecto y escritura solo con `--apply`;
- destino obligatorio `b3s-archive`, nunca el workspace operativo `b3s`;
- una evaluación vigente por captura histórica;
- ID de informe estable derivado de la captura, no de la reevaluación;
- reimportación idempotente y conflicto de contenido rechazado;
- `runtime_effect=false`, `authority=false` y
  `automatic_scoring_effect=false`.

## Qué se importa

El adaptador reutiliza el contrato de informes históricos de B3S. Solo
normaliza filas de `sv9_scans` cuya versión de rúbrica coincide con la versión
SV9 solicitada. Para cada captura persistida mantiene la evaluación compatible
más reciente.

Esto separa dos hechos:

- una captura es una observación histórica de la marca;
- una reevaluación del mismo `source_run_id` es otra lectura de esa captura,
  no una observación nueva.

`runs.started_at` conserva el momento de observación.
`sv9_scans.created_at` conserva el momento de evaluación. El adaptador no usa
la segunda fecha para hacer que una captura antigua parezca nueva.

El ID de importación se deriva de `source_run_id`. Si una copia posterior del
archivo añade otra reevaluación a una captura que ya se importó, el mismo ID
con otro hash provoca un conflicto inmutable. El comando falla de forma
cerrada: no inserta una segunda captura con la misma fecha de observación.

Las puntuaciones antiguas de cinco dimensiones no se convierten a SV9. Tampoco
se inventan `claim_slot_id`, variantes, relaciones o mappings para archivos que
nunca los persistieron.

## Uso

Primero se inspecciona el plan sin escribir:

```bash
./.venv/bin/python scripts/import_brand3_sqlite_postgres.py \
  /ruta/al/archivo/brand3.sqlite3
```

El JSON resultante incluye:

- fingerprint del manifiesto normalizado;
- número de evaluaciones compatibles;
- número de capturas seleccionadas y revisiones excluidas;
- marcas, evidencias, componentes y tiles que se importarían;
- intervalo temporal observado;
- workspace de destino y límites de autoridad.

La escritura requiere una acción explícita:

```bash
./.venv/bin/python scripts/import_brand3_sqlite_postgres.py \
  /ruta/al/archivo/brand3.sqlite3 \
  --database-url "$B3S_DATABASE_URL" \
  --apply
```

El comando aplica primero las migraciones y después importa los informes en
orden de observación. Cada informe se confirma en su propia transacción. Si
uno falla, el resultado es `partial`; los anteriores permanecen importados y
una repetición segura continúa gracias a la idempotencia.

No debe ejecutarse contra una base persistente o de producción sin una
decisión explícita sobre el destino. El workspace está aislado de las lecturas
por defecto, pero sigue ocupando almacenamiento real.

## Validación del archivo real

El 29 de julio de 2026 se ejecutó el `dry-run` sobre el archivo Brand3 de
813.932.544 bytes (778 MiB en disco):

| Medida | Resultado |
| --- | ---: |
| Evaluaciones SV9 compatibles | 29 |
| Capturas históricas seleccionadas | 20 |
| Reevaluaciones excluidas como nuevas observaciones | 9 |
| Marcas | 13 |
| Registros de evidencia normalizados | 1.400 |
| Evaluaciones de componente | 200 |
| Veredictos de tile | 1.345 |

Los 1.400 registros son ocurrencias normalizadas dentro de los 20 informes.
No equivalen a 1.400 evidencias únicas, aceptadas o aptas para scoring. El
análisis de memoria deduplica y aplica reglas posteriores; sobre este archivo
encontró 174 evidencias mecánicamente elegibles y solo dos asociaciones de
recuperación candidatas, ambas todavía sin aceptación humana.

La importación completa se probó en un PostgreSQL 14 temporal:

- primer `--apply`: 20 importados, 0 fallos;
- segundo `--apply`: 0 importados, 20 sin cambios;
- persistencia: 13 marcas, 20 capturas y 20 snapshots;
- workspace operativo `b3s`: sin exposición de esas marcas;
- SHA-256 del SQLite: sin cambios.

El clúster temporal se eliminó al terminar. No se importó el archivo a una base
persistente ni a producción.

## Qué demuestra y qué no

Demuestra que la historia recuperable del antecesor puede persistirse sin
confundir reevaluación con captura, sin duplicarse y sin contaminar las
lecturas operativas.

No demuestra que la evidencia sea semánticamente correcta para un tile, que
una fuente sea independiente, que un claim siga vigente ni que el scoring
deba reutilizarla. Esas decisiones siguen perteneciendo a las capas de
identidad, claims, mappings y revisión humana.

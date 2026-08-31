# ADR v2 — Memoria operativa incremental del Evidence Vault

- **Estado:** Núcleo v2 implementado; el perfil revisado head-031 para `b3s-vault` configura captura operacional y publicación autoritativa SV9 con memoria, pendiente de aceptación VA5 separada y despliegue explícito. El shadow de juicio SV9 post-publicación legado, verified-raw, worker y diagnostics están desactivados; no se ha realizado mutación live. C7 usa el ciclo normal de baldosa.
- **Fecha:** 2026-08-06
- **Ámbito inicial:** Vault; validación runtime en `b3s-pr71-vault`
- **Sustituye:** `evidence_vault_canonical_memory_adr_v1.md`
- **Fuera de alcance inicial:** producción y el score diagnóstico del scanner

## 1. Decisión

El Vault deja de tratar cada scan como una evaluación nueva de toda la marca.
La operación normal conserva una memoria aceptada, adquiere evidencia nueva y
analiza únicamente el delta relevante.

```text
sin baseline → baseline → memoria N1 → score canónico N1
con baseline N → incremental_refresh → captura + delta
solicitud explícita → diagnostic_full → reporte no canónico
```

El scanner observa. El Vault recuerda y compara. El LLM analiza trabajo nuevo.
Una política versionada o una decisión humana concede autoridad. El scorer
canónico es código determinista.

## 2. Baseline y proyección

El baseline es la primera memoria operativa persistente y versionada de
estados aceptados de una marca. No exige una estrategia completa ni ochenta
baldosas demostradas.

```text
memoria de marca = evidencia, relaciones y estados aceptados; puede ser parcial
proyección de scoring = contabiliza las 80 baldosas del registro aplicable
```

El primer scan válido puede activar automáticamente N1. Las propuestas no
aceptadas permanecen en un overlay y no contaminan memoria ni score.

El paquete congelado es identificable y auditable. Su proyección y score son
reproducibles sin volver a llamar al modelo. No se exige que una nueva
ejecución del LLM produzca el mismo candidato.

## 3. Ejes de estado

Los cuatro ejes son independientes:

```text
semantic_state:  ok | no | sin_evidencia | contradiction
authority_state: pending | accepted | rejected
review_state:    none | required | in_review | resolved
lifecycle_state: active | superseded
```

`superseded` se deriva de la cadena de adopciones. Nunca se modifica un evento
histórico para aplicarlo. Una propuesta puede ser semánticamente `ok`, seguir
`pending` y no requerir humano porque aún pueda resolverla una política.

El scoring exige conjuntamente:

```text
semantic_state = ok
AND authority_state = accepted
AND lifecycle_state = active
AND score_eligible = true
```

## 4. Memoria aceptada y overlay candidato

El paquete puede contener material aceptado, pendiente, rechazado y
contradictorio. La versión canónica se deriva solo del subconjunto aceptado.

```text
paquete congelado
├── accepted subset → canonical_memory N → canonical score N
└── candidate overlay → pending | contradiction | preview sin autoridad
```

Modificar el overlay cambia:

```text
candidate_overlay_version
candidate_packet_fingerprint
candidate_preview_identity, si cambia el preview
```

Sin `adoption_event` no cambian:

```text
canonical_memory_version
evaluation_identity
canonical_score
```

El preview nunca reutiliza `evaluation_identity`. Una contradicción sin
resolver usa `candidate_preview_points=null`; cero solo es válido si una
política versionada declara un lower bound conservador.

## 5. Cardinalidad y cobertura

La proyección contabiliza exactamente las baldosas del registro —80 en la
rúbrica actual—. No obliga a almacenar ochenta decisiones positivas ni a
revisar ochenta formularios.

```text
accepted_tile_count + unresolved_tile_count = 80
pending_change_tile_count es subconjunto de accepted_tile_count
contradiction_count = contradiction_on_accepted_count
                    + contradiction_on_unresolved_count
```

Las contradicciones son diagnósticos superpuestos, no una tercera partición.
Se distinguen pendientes iniciales y cambios pendientes sobre estados
canónicos existentes.

Cada evaluación publica al menos:

```text
accepted_tile_count
accepted_ok_count
accepted_no_count
accepted_sin_evidencia_count
unresolved_tile_count
pending_initial_tile_count
pending_change_tile_count
contradiction_on_accepted_count
contradiction_on_unresolved_count
contradiction_count
tile_authority_coverage_ratio
score_weight_authority_coverage_ratio
score_completeness = partial | complete
canonical_score_status = current | pending_reassessment
```

Un `sin_evidencia` aceptado aporta cobertura de autoridad, pero no
evaluabilidad positiva o negativa.

## 6. Contradicciones y neutralidad

Sin baseline, una contradicción queda pendiente, requiere revisión y tiene
preview nulo; el resto aceptado puede formar N1 y producir score. Con baseline
N, una contradicción nueva vive en el overlay: memoria y score N permanecen.

La política es neutral respecto a la dirección:

```text
cambio demostrado por predicates deterministas
  → adopción automática, suba o baje
cambio interpretativo, ambiguo o contradictorio
  → revisión humana, suba o baje
```

Un cambio relevante pendiente produce
`canonical_score_status=pending_reassessment`; no invalida la última
evaluación autorizada. `not_reacquired` produce `coverage_loss`. Una ausencia
solo produce `no` si el contrato define la prueba y la cobertura es suficiente.

## 7. Modos operativos

### `baseline`

- Entrada: no existe memoria canónica.
- Hace adquisición integral, normalización, deduplicación y proyección inicial.
- El LLM puede proponer relaciones y estados.
- Persiste captura, evidencia, paquete, accepted subset, overlay y adopción.
- Devuelve N1, score de estados aceptados y cobertura de autoridad.
- Solo las reglas deterministas habilitadas reciben autoridad automática.

### `incremental_refresh`

- Entrada: existe memoria N.
- Persiste captura y evidencia, deduplica y calcula el delta.
- Usa cero LLM si no existe trabajo nuevo.
- Si hay trabajo, analiza solo evidencia, baldosas y dependencias afectadas.
- Sin impacto conserva N y no genera score ni reporte estratégico.
- Con adopción produce N+1 y una evaluación canónica nueva.

### `diagnostic_full`

- Requiere solicitud explícita.
- Puede ejecutar los diez componentes y las 80 baldosas.
- Persiste un reporte diagnóstico separado y no canónico.
- Nunca adopta memoria por efecto lateral.

Durante la validación estos modos solo se habilitan en
`BRAND3_ENVIRONMENT=vault`. Esta es una capability genérica del pipeline, no un
gate de C7. Producción no cambia hasta una autorización de despliegue explícita.

## 8. Persistencia capture-only

No se crea otro ledger. La observación técnica usa:

```text
scan_runs
captures
evidence_records
```

La operación es transaccional e idempotente. No crea `evaluation_runs` ni
`report_snapshots` ficticios y no relaja el importador de reportes completos.

Cada refresh conserva la observación JSON exacta, su fingerprint, el hash del
payload de captura, outcomes de adquisición, evidencia normalizada y el plan
completo con delta, `canonical impact` y shortlist de baldosas afectadas. El
plan queda incluido en la observación inmutable y duplicado en metadata para
consulta operativa.

El estado del trabajo es explícito: `pending | not_required | completed`.
Un retry del mismo `source_scan_id` recupera el plan exacto en vez de convertir
una captura no analizada en “cero LLM”. La transición a `completed` usa CAS
sobre `operation_plan_fingerprint` y conserva el fingerprint del resultado.
Una importación posterior del reporte completo se adjunta al mismo capture solo
si la identidad de marca y el conjunto exacto de evidencia coinciden; no crea
otro scan/capture.

## 9. Autoridad por perfiles

Las baldosas referencian perfiles compartidos; no se implementan ochenta
microprogramas. Cada asignación declara perfil, modo de adopción, fuentes,
cobertura, validación semántica, dependencias, downgrade y versión.

Estados de calibración:

```text
deterministic
calibrated_policy_guarded
shadow_insufficient_data
known_false_positive
human_required
```

La autoridad automática exige ausencia de falsos positivos conocidos, muestra
suficiente y diversa, rendimiento aceptable, replay determinista, fuente
representada y guardrails del tile. La confianza del LLM nunca basta y otro
LLM no constituye por sí solo validación independiente. `coherencia.C8`
comienza como `human_required`.

La calibración separa diseño y validación por marca. Con SoccerSolver y Causa
Prima, leave-one-brand-out sirve para falsificar pero no para afirmar
generalización; las políticas semánticas permanecen
`shadow_insufficient_data`.

El artefacto versiona corpus, split, marcas de diseño/validación, política de
labels, perfil, criterio, conteos, cobertura de fuentes, falsos positivos y
resultados por marca, fuente y baldosa.

## 10. Adopción, scoring e identidades

`adoption_event` es el nombre de producto del evento append-only que activa
una versión. Puede emitirlo una política o un humano. Incluye memoria, paquete,
parent, actor, política y momento. Es idempotente y usa comparación optimista.

```text
score_input_fingerprint = hash(canonical_json({
  derived_tile_state_fingerprint,
  tile_contract_registry_fingerprint,
  reducer_policy_fingerprint,
  aggregation_policy_fingerprint
}))

evaluation_identity = hash(canonical_json({
  canonical_memory_version,
  score_input_fingerprint
}))
```

Cada N nueva crea una nueva `evaluation_identity`. Si coincide
`score_input_fingerprint`, se reutiliza el cálculo mediante
`reused_from_evaluation_identity`; nunca se reutiliza la identidad anterior.

Antes de reutilizar una evaluación `operational_v2`, el repositorio reproyecta
la memoria histórica exacta y valida el evento de promoción que la adoptó; luego
rederiva el cálculo desde las baldosas aceptadas. Antes de alimentar un reporte,
toma el lock de marca y exige que `canonical_memory_version`,
`adoption_event_id` y `evaluation_identity` sigan siendo exactamente los que
devuelve la activación del scan. Una witness v1 enlaza por fingerprint la
proyección completa de memoria, el evento de promoción y la derivación del
score. Una fila internamente autoconsistente no es autoridad si su score,
breakdown, evento o memoria difieren de esa rederivación. La witness no cambia
el payload persistido `evidence-vault-operational-score-evaluation-v2`, no llama
al LLM y se reproduce en replay.

## 11. Aceptación y despliegue

La implementación debe demostrar primero en el entorno aislado
`b3s-pr71-vault`, antes de autorizar `b3s-vault`:

```text
primer scan → baseline parcial → 80 baldosas → score + cobertura
refresh sin delta → cero LLM → misma memoria y score → sin reporte nuevo
delta determinista → adopción neutral → N+1
delta ambiguo → overlay → memoria y score N permanecen
resolución → N+1 → evaluation_identity nueva
```

SoccerSolver es el primer caso y Causa Prima la falsificación con otra marca.
No se permiten condicionales por dominio, marca o URL.

El despliegue previsto es Vault-only y gradual. La configuración aislada PR71 declara
`BRAND3_VAULT_OPERATIONAL_PIPELINE_ENABLED=true`: esa capability conecta el
scanner web con preparación, ejecución, activación y proyección operacional. No
activa C7 ni autoriza producción. El perfil revisado head-031 de `b3s-vault`
queda configurado con `BRAND3_VAULT_OPERATIONAL_PIPELINE_ENABLED=true`,
`BRAND3_VAULT_SV9_AUTHORITY_SCANNER_ENABLED=true`,
`BRAND3_VAULT_SV9_JUDGMENT_SHADOW_ENABLED=false`,
`BRAND3_VAULT_VERIFIED_RAW_ACQUISITION_SHADOW_ENABLED=false`,
`B3S_VAULT_WORKER_ENABLED=false` y
`B3S_VAULT_SV9_SHADOW_DIAGNOSTICS_ENABLED=false`.
Desactivar la capability en PR71 conserva capturas, eventos y evaluaciones y
devuelve ese entorno al diagnóstico explícito.

The VA4D code/profile cutover remains separate from VA5 acceptance and any
explicit deployment. Final integration acceptance and explicit deployment are
independent release decisions; this ADR records no live mutation.
The profile is configured, not live: authority remains contingent on separate
VA5 acceptance and explicit deployment; no live mutation has occurred.

## 12. Estado real de implementación y límites de activación

El núcleo v2 implementado reutiliza las tablas existentes de paquetes,
eventos, evaluaciones y capturas. La persistencia valida bajo lock de marca:
parent actual, marca, políticas, fuente `canonical_v1` registrada, reducción
canónica, autoridad determinista recalculada y atribución del evento de
adopción. Un paquete no puede declarar su propia autoridad mediante hashes o
estados suministrados por el caller.

Una memoria v1 existente puede iniciar una proyección v2 separada mediante la
operación explícita de baseline. El ledger v1 no se reescribe ni se presenta
como v2; solo el subconjunto de relaciones revisadas que satisface la matriz
determinista actual puede entrar en N1. Un baseline provisional vacío puede
evolucionar después mediante el método incremental y CAS sobre su parent.

`prepare_vault_scan_after_capture()` produce y persiste planes reproducibles.
Las migraciones 015/016 y `execute_vault_operation_plan()` implementan ya un
consumidor Vault-only con claim, lease, heartbeat, fencing por generación,
resultado inmutable y finalización idempotente. El plan congela además el
contexto de identidad owned de la captura completa mediante
`semantic_context.context_fingerprint`; el consumidor clasifica solo los
fingerprints elegibles de la shortlist y registra por fingerprint los estados
`semantic_candidate`, `ineligible_label_type` o `non_material_identity`.
Limita las parejas evidence→tile, persiste el resultado semántico antes de
registrar paquetes y, tras un crash en
`result_persisted`, reconstruye y finaliza sin una segunda llamada LLM. El
resultado nunca contiene autoridad ni score y el paquete operacional generado
mantiene `has_accepted_change=false`.

Las propuestas nacidas de capture-only se registran como
`operational_source_v2`. Una revisión humana explícita genera eventos durables
por relación, un source `operational_reviewed_v2` y, solo para relaciones
aceptadas no contradictorias, un paquete con autoridad humana. La adopción usa
el mismo CAS de marca; los rechazos no crean memoria, las contradicciones
permanecen pendientes y esta ruta no calcula score por efecto lateral.

El scanner web invoca estas rutas únicamente cuando concurren
`BRAND3_ENVIRONMENT=vault` y la capability exacta
`BRAND3_VAULT_OPERATIONAL_PIPELINE_ENABLED=true`. La publicación autoritativa
requiere además `BRAND3_VAULT_SV9_AUTHORITY_SCANNER_ENABLED=true`; esa bandera
solo está configurada en el `fly.vault.toml` revisado head-031. El perfil de
`b3s-vault` queda configurado para captura operacional y publicación SV9 con
memoria, sujeta a aceptación VA5 separada y despliegue explícito; el shadow de
juicio post-publicación legado permanece desactivado, al igual que verified-raw,
worker y diagnostics, y el score diagnóstico live permanece intacto. Core
(`fly.toml`) y PR71 (`fly.pr71-vault.toml`) no activan la autoridad. No se ha
realizado mutación live. Cualquier despliegue del pipeline genérico en otro
entorno requiere una revisión y autorización separadas. C7 no añade una
bandera, allowlist, readiness ni ventana de activación propias.


## 13. Validación de campo v1

El corpus congelado en
`fixtures/evidence_vault_field_validation_v1/` contiene tres observaciones
normalizadas de SoccerSolver, dos de Causa Prima y un golden downstream
legado de Causa Prima. Su clasificación contractual es
`normalized_evidence_pack_only`: no es un replay de adquisición raw, no tiene
autoridad y no produce efecto runtime.

`validate_evidence_vault_field_replay()` y
`scripts/validate_evidence_vault_field_replay.py` verifican offline integridad,
baseline, no-op y deltas materiales. `scripts/run_evidence_vault_field_shadow.py`
puede ejecutar los baselines contra PostgreSQL local y un LLM configurado, pero
rechaza DSN no local, exige opt-in explícito y nunca revisa, adopta ni puntúa.

La primera ejecución de campo obligó a endurecer el contrato:

- el baseline divide el trabajo semántico en llamadas de como máximo 120
  parejas; un refresh incremental conserva un único workset de ese tamaño;
- el prompt ofrece candidatos literales derivados por servidor y toda cita
  retenida debe seguir siendo un substring continuo exacto de la evidencia;
- cada salida inválida del modelo se descarta fail-closed, se reason-codea y se
  conserva solo mediante hash;
- una shortlist amplia prioriza tiles forzados, se limita a 24 por evidencia y
  persiste todos los tiles omitidos;
- labels, shortlists y truncaciones quedan en el resultado inmutable y el
  repositorio los vuelve a derivar antes de aceptar el resultado.
- los IDs de relación son únicos en todo el paquete, no solo dentro de cada
  tile, para impedir que una decisión humana se replique sobre otro tile;
- el writer shadow rechaza rutas libpq efectivas no locales, overrides de
  entorno/query y cualquier token que no coincida con la base desechable.

La validación histórica conservada en `audits/evidence_vault_field_validation_v1/`
completó ambas marcas sin autoridad, memoria aceptada ni score. Los worksheets
debían ser resueltos por un humano identificado antes de probar la adopción real.
Ese resultado histórico no es un gate actual de C7 ni autoriza despliegues.


## 14. Revisión humana de campo v1

`gsus` resolvió las 19 relaciones congeladas el
`2026-08-07T09:59:45+02:00`: 14 `accept` y 5 `reject`. El importador verifica
todas las columnas inmutables contra el resultado semántico, conserva el
instante humano en los eventos append-only y rechaza DSN o overrides libpq no
locales antes de escribir.

En PostgreSQL desechable, SoccerSolver adoptó 7 relaciones sobre 6 tiles y
obtuvo score sparse 6; Causa Prima adoptó 7 relaciones sobre 4 tiles y obtuvo
score sparse 4. Los rechazos no aparecen en memoria aceptada y una segunda
ejecución reprodujo los mismos paquetes, eventos, versiones y evaluaciones.
Ninguno tuvo efecto en scanner o producción.

Estos scores no continúan el journal legado: ambas marcas partieron sin parent
v2. Por ello Magnetismo de Causa Prima queda 0/10 en este baseline aislado. El
lote no contiene relaciones `MG*`, `C7`, `V3`, `VA4` o `I9`; el siguiente gate
debe ser un suplemento shadow estrecho y, por separado, una prueba real de
migración de lineage legado si se pretende conservar aquel score.

## 15. Suplemento de cobertura y N+1 de Causa Prima

El suplemento de cobertura es una fuente explícita distinta de una operación
de adquisición. `validate_coverage_supplement_artifact()` liga request,
resultado, snapshots de evidencia y citas al pack normalizado exacto.
`build_coverage_supplement_source_candidate()` deriva el paquete completo de
80 tiles desde el parent activo y convierte únicamente las relaciones
enumeradas en basis `unreviewed`. El repositorio no acepta un paquete construido
por el caller: lo deriva bajo lock y lo registra como `operational_source_v2`
con `source_kind=coverage_supplement`, resolución content-addressed y todos los
flags de autoridad/runtime a false. No se fabrica un scan ni una operación para
satisfacer el lineage.

`gsus` revisó las cuatro relaciones MG10 el
`2026-08-07T11:43:10+02:00`. Las cuatro fuentes son publishers externos que
cubren la misma ronda pre-seed. Son cuatro identidades documentales, pero un
solo evento económico y un solo tile: la multiplicidad no añade puntos. La
revisión durable creó cuatro eventos y un N+1 de Causa Prima con parent
`1423ffd3...78c4`, versión `b1bafe84...887a`, secuencia 2, cinco tiles aceptados
y score sparse 6. MG10 aporta un punto raw de Magnetismo, ponderado x2 por la
rúbrica. Registro, revisión, adopción y score son idempotentes y continúan sin
efecto en scanner o producción.

La hoja independiente de ocho tiles tiene otro contrato:
`coverage_assessment_only_no_adoption`, y todas sus filas conservan
`adoption_eligible=false`. Aceptar esas disposiciones confirma candidatos para
MG1/MG3/MG5/MG10, un juicio compuesto para C7 y `sin_evidencia` acotado para
V3/VA4/I9. No crea relaciones ni autoridad. MG1/MG3/MG5 requieren un nuevo
paquete de relaciones exactas y revisión relation-scope; C7 requiere antes un
contrato de autoridad conjunta para varias evidencias.

La prueba también cerró un hueco N+1: varios builders emitían flags
`replaces_*` que el core canónico no admite. La presencia de `coverage_refs` o
`unresolved_refs` ya expresa reemplazo. Los flags fueron eliminados y una
regresión verifica que N+1 conserva la base aceptada y añade únicamente la
relación revisada.

El preview legado de Causa Prima continúa fuera del lineage v2. No puede usarse
como parent ni como input de score. Una migración real exige congelar los tres
packs posteriores y construir un seed/export evidence→tile revisado que cree
una génesis v2 parentless nueva.
## 16. Relaciones exactas y autoridad compuesta de C7

Una evaluación de cobertura confirmada no se convierte en autoridad por
reutilizar su decisión. El suplemento `evidence-vault-exact-relation-supplement-v1`
proyecta cada hallazgo semántico a identidades nuevas y pendientes, ligadas al
pack normalizado, al worksheet de assessment ya revisado, al parent Vault
activo y a los fingerprints vigentes de registry, reducer y aggregation. El
artefacto y su source packet permanecen sin autoridad hasta una revisión nueva
del scope exacto.

`MG1`, `MG3` y `MG5` son grupos atómicos de una relación. `C7` es un grupo
`all_of` de dos relaciones: una fuente web propia y un perfil social externo
capturado, con `source_identity_id` distintos y un `claim_id` común igual al
`group_id`. Las dos filas se conservan en la basis canónica; no se fabrica una
evidencia combinada. El operador vive fuera del reducer, en el artefacto, la
resolución inmutable y `manifest.coverage_summary.decision_groups`. Por tanto,
`claim_id` por sí solo nunca significa `all_of`.

Antes de insertar eventos y de nuevo durante la proyección pura del source
revisado se exige que:

- el set de decisiones resuelva exactamente todas las relaciones pendientes;
- cada miembro aparezca en un solo grupo y en el tile declarado;
- los miembros `all_of` compartan grupo/claim y una única decisión y rationale;
- `accept` promueva ambos miembros y `reject` descarte ambos;
- una decisión parcial o mixta falle sin insertar eventos.

Se reutilizan `operational_source_v2`, `operational_reviewed_v2`,
`operational_v2` y la tabla existente de relation reviews; no hay nuevo packet
kind ni migración. La resolución del source es la prueba durable del operador
compuesto.

La revisión exacta de Causa Prima aceptó los tres grupos MG atómicos y el grupo
C7 conjunto. La adopción desechable de secuencia 3 creó cinco eventos de
relación, memoria
`7b94b1062aa01b1cb259944b3cd67bef0fac614a130d90e65326b53d169b1a7f` y
score sparse 14: Magnetismo v2 contiene `MG1`, `MG3`, `MG5` y `MG10`; `MG7` y
`MG8` no se importaron desde legacy. C7 conserva dos filas de basis pero aporta
un solo tile.

Una mutación futura de cualquiera de los dos miembros C7 debe invalidar o
reabrir el grupo completo. El contrato congela
`member_change_requires_review=true`. La rama apilada de lifecycle v1 implementa
la detección contra deltas durables, la reapertura atómica, la supresión del
score, la persistencia de `pending_reassessment` y la resolución mediante un
grupo `all_of` nuevo; véase `evidence_vault_c7_group_lifecycle_v1.md`. La
reapertura cambia solo el estado y los puntos ordinarios de C7; no bloquea el
scanner, el informe, la API/UI ni un despliegue.

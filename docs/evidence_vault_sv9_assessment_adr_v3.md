# ADR v3 — Kernel puro de assessment SV9 y ortogonalidad de verification

- **Estado:** Aceptado e implementado como contrato shadow
- **Fecha:** 2026-08-13
- **Ámbito:** aritmética SV9 compartida por Scanner y Evidence Vault
- **Rúbrica fija de este incremento:** `baldosas-v3-1`
- **No implica:** backfill, read path, API pública, worker, cambio de producto ni cutover. La migración 025 solo define un ledger append-only no autoritativo; la 027 añade un único writer interno y no cambia esa autoridad.

## 1. Decisión

SV9 tiene una única frontera de aritmética pura:
`src/sv9/assessment_kernel.py`. El kernel recibe un vector semántico completo,
lo valida fail-closed y calcula el score sin leer Scanner, Vault, evidencias,
verification, persistencia ni modelos.

```text
assessment semántico exacto (80 baldosas)
        │
        ▼
SV9 assessment kernel
        ├── score y breakdown
        ├── base_average y Magnetism cap
        ├── assessment_fingerprint
        └── score_fingerprint
```

Scanner y Vault conservan sus propias responsabilidades antes y después de esa
frontera. Ninguno vuelve a implementar la suma, pesos o cap para assessments
completos.

## 2. Contrato de entrada

La entrada contiene exactamente 80 filas, una por baldosa del registro
`baldosas-v3-1`:

```json
{
  "component_key": "mission",
  "tile_id": "M1",
  "tile_key": "mission.M1",
  "assessment_state": "ok"
}
```

Estados válidos:

```text
ok | no | sin_evidencia
```

`ok` aporta un punto base. `no` y `sin_evidencia` aportan cero, pero mantienen
identidad semántica distinta. `contradiction`, estados de verification y
cualquier coerción implícita quedan fuera del kernel.

El orden recibido no tiene significado: el kernel ordena por el registro
canónico antes de calcular y hashear. Falla cerrado ante:

- número distinto de 80;
- ID desconocido, duplicado o ausente;
- `component_key` o `tile_key` que no correspondan al registro;
- estado fuera de catálogo;
- campos extra o faltantes.

Un fallo técnico no se convierte en `no`. Sin assessment exacto no hay score
nuevo.

## 3. Contrato de salida v1

`sv9-assessment-output-v1` publica:

```text
rubric_version
assessment_vector_version
scoring_policy_version
tile_contract_registry_fingerprint
tile_count = 80
tiles (snapshot normalizado en orden canónico)
sv9_score
component_breakdown
base_average
magnetism_capped
assessment_fingerprint
score_fingerprint
```

`tiles` conserva las 80 filas ya normalizadas; no es solo un hash. Esto permite
recomponer el assessment sin consultar Scanner, Vault ni la rúbrica histórica.
`validate_sv9_assessment_output(output)` valida campos y versiones exactos,
recalcula desde ese snapshot y compara registro, cálculo y ambos fingerprints.
Cualquier alteración falla cerrada.

El breakdown conserva, por componente, conteos de los tres estados, score raw,
score efectivo, multiplicador, puntos y máximo. La política continúa siendo:

- cada `ok` enciende una baldosa independiente;
- Magnetism y Coherencia pesan ×2;
- si la media normalizada de los ocho componentes base es menor que 4 y
  Magnetism tiene más de cinco `ok`, su score efectivo se limita a cinco;
- el máximo total es 100.

## 4. Identidades reproducibles

`tile_contract_registry_fingerprint` es una identidad neutral derivada del
contrato ejecutable completo: orden e IDs, componente, escala, multiplicador y
definición íntegra de cada baldosa. Detecta drift aunque se olvide actualizar
manualmente una versión.

`assessment_fingerprint` identifica el vector semántico canónico junto con
`rubric_version`, `assessment_vector_version`, `scoring_policy_version` y
`tile_contract_registry_fingerprint`. Cambiar `no` por `sin_evidencia` cambia
esta identidad aunque ambos aporten cero.

`score_fingerprint` identifica los inputs semánticos y el cálculo resultante:
referencia `assessment_fingerprint`, el fingerprint del registro, versiones de
rúbrica/política, score, breakdown, media base y cap. No incluye verification,
reviewer, freshness, disputes, timestamps ni autoridad de persistencia.

Así, dos sistemas que adaptan el mismo vector obtienen el mismo assessment y
score; sus estados de verification pueden ser distintos sin falsificar drift
numérico.

## 5. Scanner: adaptador shadow fail-closed

`build_scanner_sv9_assessment(components)` solo entrega `available` si existen
los diez componentes en `STATUS_SCORED` y cada perfil contiene exactamente sus
baldosas válidas. Entonces adapta los perfiles al kernel.

Si un componente está `not_detected`, `not_evaluated`, tiene perfil incompleto,
duplicado o incompatible, devuelve:

```text
availability = unavailable
tile_count = 0
expected_tile_count = 80
tiles = null
sv9_score = null
component_breakdown = null
assessment_fingerprint = null
score_fingerprint = null
reason_codes = [...]
```

No fabrica 80 `no`: distinguir `no` de `sin_evidencia` requiere evaluación por
baldosa. Corregir esa producción en el evaluator es un incremento posterior.

Para scans completos, `aggregate()` usa el resultado del kernel para score,
media y cap. Temporalmente conserva una ruta legacy explícita para scans
incompletos porque el modelo/store público actual representa componentes
`not_detected` o `not_evaluated` como cero. Esa compatibilidad no convierte el
assessment shadow en disponible ni autoriza su reutilización canónica.

## 6. Vault: autoridad fuera, aritmética dentro

Vault sigue siendo responsable de probar autoridad, linaje, reducción,
cobertura y exactitud de la proyección. En la frontera canónica,
`build_vault_sv9_assessment_from_tile_states` conserva la validación estricta
del contrato Vault y adapta 80 estados ya resueltos al kernel, devolviendo el
snapshot completo neutral. `calculate_score_from_tile_states` permanece como
wrapper compatible del formato de cálculo legacy. Los envelopes, fingerprints
de memoria, eventos de adopción y witnesses permanecen fuera.

La delegación garantiza paridad Scanner/Vault para un vector igual sin hacer
que Scanner herede autoridad de Vault ni que Vault confíe en un score del
Scanner. No demuestra todavía que el pipeline operational de Vault haya
separado assessment de verification; ese cierre queda bloqueado por las deudas
de la sección 9.

### 6.1 Vault operational: adaptador semántico shadow enlazado

`build_operational_semantic_shadow_assessment()` recibe un packet operational
v2 completo **y** su source candidate packet completo. Valida ambos, su
fingerprint, marca, parent y políticas. `scoring_projection` no es una fuente
de autoridad: sus 80 filas, incluidos canonical/authority/review/lifecycle,
`score_eligible`, overlay y coverage, se rederivan íntegramente desde
`accepted_memory`, `candidate_overlay` y el source candidate. Para el único
caso sin overlay permitido —un refresh incremental `no_change` de una baldosa
ya accepted— la fila accepted anterior aporta la autoridad y debe coincidir en
contenido canónico (`semantic_state`, `basis`, `coverage_refs`,
`unresolved_refs`) con el candidate nuevo. Conserva deliberadamente su
fingerprint de source y su `source_delta_kind` anteriores; no se exige que
coincidan con el packet refresh. El `review_state` de la proyección se conserva
solo si es `none` o `resolved`; cualquier otro valor falla. Fuera de ese caso,
si una disposición no está disponible en esos artefactos, o si la proyección no
coincide exactamente, falla cerrada. En particular, una proyección que declare
M1 `accepted` y canónica `ok` no es válida si `accepted_memory` no contiene M1.
Solo entonces adapta el vector del source al kernel. El resultado es
`evidence-vault-operational-semantic-assessment-shadow-v1`, no tiene autoridad
ni efectos de runtime. La migración 025 puede conservar una observación exacta en
un ledger append-only no autoritativo; no añade writer, read path ni exposición.

`authority_coverage` permanece como observación separada: no filtra ni cambia
el vector. `semantic_provenance_fingerprint` nombra exclusivamente la identidad
semántica del kernel (vector/kernel fingerprint) y los IDs estáticos de contrato
semántico (registry/reducer/aggregation). No incorpora source candidate packet
fingerprint, basis, review, authority, lifecycle ni candidate overlay. El
fingerprint del source packet permanece en su campo separado; por eso cambios
de review o autoridad sin cambio semántico no alteran los fingerprints del
kernel ni el de procedencia semántica.

El caller debe proporcionar explícitamente
`expected_parent_canonical_memory_version`. Su omisión devuelve unavailable con
`expected_parent_required`; el valor explícito, incluido `None`, debe ser siempre
el parent inmutable del packet. El writer confiable adquiere el lock de adopción
y admite solo dos estados: el packet todavía está construido sobre el current
durable, o el último evento de adopción validado adoptó exactamente ese packet y
produjo exactamente el current durable. Antes de la adopción, cualquier packet
exacto construido sobre el current conserva el CAS original. Una vez que el
current avanza, compartir parent o source no basta: un sibling y cualquier
productor anterior quedan stale y fallan antes del kernel y del lookup de replay. Por tanto, una fila `stale_candidate_parent` solo podría
provenir de un futuro camino de importación auditado de forma independiente, no
de este writer CAS confiable. Si una baldosa es `contradiction`, queda unavailable
con `contradiction_requires_semantic_reassessment`: no se inventa score,
vector ni fingerprints. C7 y C8 siguen siendo baldosas semánticas ordinarias
para aritmética; su `verification_requirement` es respectivamente
`owned_web_plus_external_social` y `human_required`. Ninguna baldosa accepted,
incluidas las ordinarias, pasa a `verified` solo por authority: este packet no
contiene un binding de verificación de evidencia. Salvo `contradiction`
(`disputed`), lifecycle `superseded` (`stale`) o authority `rejected`
(`unverifiable`), el estado queda `pending`. Esos requirements, y los estados
pending/verified/disputed/stale/unverifiable, no participan en aritmética.

Una fila accepted de origen `human` exige un `decision_event_id` no vacío. Una
de origen `policy` exige `authority_matrix_fingerprint` y
`authority_decision_fingerprint` SHA-256 válidos, sin `decision_event_id`; el
profile no puede estar vacío ni ser `unassigned`. Esto es validación estructural
conservadora, no validación completa de la decisión policy: el packet conserva
sus digests, pero no el registro inmutable de decisión ni un binding que permita
recalcular que autorizó ese candidate histórico. Esa limitación es irreducible
en este adaptador y requiere consultar/verificar ese registro externo.

## 7. Assessment y verification son ejes ortogonales

```text
assessment_state:   ok | no | sin_evidencia
verification_state: verified | pending | disputed | stale | unverifiable
```

Assessment responde qué estado semántico se puntúa. Verification responde qué
nivel de comprobación externa o humana tiene ese assessment. Verification no
es un cuarto estado de puntuación y no entra en ninguno de los dos
fingerprints del kernel. `human_required` no es un `verification_state`: es un
verification/review requirement de política que normalmente deja el estado en
`pending` hasta su resolución.

Una transición de verification por sí sola no suma, resta, limita ni anula
puntos. Si una disputa o revisión concluye que el estado semántico debe
cambiar, se crea un assessment nuevo; ese vector nuevo sí obtiene identidades y
score nuevos.

### C7

C7 es una baldosa ordinaria de Coherencia. Vale un punto base con peso ×2 como
C1–C10. No es capability gate, condición de cutover ni excepción aritmética.

### C8

C8 también es una baldosa ordinaria en assessment y aritmética. Su política
impone el verification/review requirement `human_required` cuando se pretende
afirmar que la experiencia real cumple la promesa. Mientras la revisión no se
resuelva, su `verification_state` es `pending` (o `unverifiable` si la
comprobación no puede realizarse). El requirement pertenece solo a verification:
no enciende C8, no la apaga, no convierte un fallo técnico en cero y no altera
el cap. El assessment seguirá las reglas de evidencia de la rúbrica; la
verificación humana se registra aparte.

## 8. Disputes, stale, freshness e inheritance

- **Disputes:** abren o actualizan verification. El último assessment autorizado
  permanece reproducible. Una resolución que cambie semántica produce un vector
  nuevo; no se reescribe el anterior.
- **Stale:** describe vigencia de verification/evidencia, no un valor de baldosa.
  Marcar algo stale no modifica retrospectivamente score ni fingerprints.
- **Freshness:** una política versionada puede solicitar reacquisición o bloquear
  la publicación de un assessment nuevo. No puede editar aritmética. Hasta que
  exista un vector completo nuevo, el adaptador devuelve unavailable o el Vault
  conserva la última evaluación autorizada según su lifecycle.
- **Inheritance:** un estado semántico aceptado puede heredarse con su linaje.
  Si el vector y las versiones son iguales, el resultado del kernel es igual.
  Verification solo se hereda cuando su propia política, alcance, evidencia y
  vigencia lo permiten; nunca se deduce del score. En particular,
  `human_required` de C8 no se satisface por herencia implícita.

## 9. Límites de este incremento

Este ADR introduce kernel, adaptadores, paridad y tests. No:

- migra scores históricos ni rellena fingerprints nuevos;
- añade un read path, API pública, worker o exposición para el ledger shadow;
- cambia schemas de store/model o payloads públicos existentes;
- cambia el read path de Scanner o Vault;
- activa un cutover, deploy gate o runtime de producción;
- corrige la clasificación `not_detected` tile por tile;
- fusiona verification con assessment;
- otorga autoridad canónica a resultados del Scanner.

### Deudas bloqueantes

- `evidence_vault_operational_scoring._scoring_inputs()` todavía aplica un
  authority filter y proyecta baldosas no aceptadas como `sin_evidencia`. Esa
  proyección no es todavía un assessment v3 ni un contrato público. En
  particular, no prueba separación entre score y verification operational.
- Scanner todavía ejecuta `apply_source_policy()` antes del kernel y esa
  política puede demotar `ok` a `sin_evidencia`. Debe clasificarse
  explícitamente como política de assessment o moverse fuera del vector si en
  realidad representa verification/authority.
- El preview de `evidence_scoring_memory_preview._aggregate_scores()` sigue con
  aritmética legacy y no consume el snapshot v3.

Estas deudas bloquean cualquier writer canónico/público, exposición en read
paths y cutover. No bloquean el writer administrativo interno de la migración
027, que solo conserva observaciones shadow sin autoridad ni runtime effect.
Cada deuda requiere un incremento con fixtures de paridad y decisión de
autoridad. Hasta entonces no debe afirmarse que el Vault operational ya separa
score de verification en su producto público.

Cualquier writer canónico/público, exposición pública o cutover necesita una
decisión y validación separadas. La migración 025 no autoriza ninguno de ellos;
la capability privada de 027 tampoco los añade.


## 10. Hardening forward-only del ledger (migración 026)

La migración 026 no modifica 025: añade una identidad única portable con
`COALESCE(expected_parent_canonical_memory_version, '')`; el valor vacío no
pasa el CHECK de fingerprint, por lo que dos observaciones del mismo parent
inicial `NULL` no pueden coexistir. También revoca `EXECUTE` público de los helpers de
validación y de los trigger functions.

La base valida el shape exacto de vector, requirements, counts y output, y que
las `tiles` del output sean exactamente el vector semántico persistido. Deriva
los requirements desde ese vector y desde `scoring_projection`: C7/C8, y los
estados contradiction/superseded/rejected, tienen las reglas de la sección 6.
No implementa ni declara implementar el cálculo canónico ni fingerprints del
kernel en SQL. No existe writer ni consumer público; un writer futuro de
confianza deberá ejecutar `validate_sv9_assessment_output` y rederivar el
shadow operacional completo antes de insertar. Esa condición no es una nueva
capacidad concedida por esta migración.


## 11. Writer interno append-only (migración 027)

La migración 027 añade únicamente
`PostgresHistoryRepository.append_evidence_vault_operational_sv9_shadow_assessment`.
El caller aporta el dominio, el fingerprint del packet operational y el parent
inmutable del packet explícito (incluido `None`); no aporta evento, current,
vector, output, requirements ni identidad. El repository bloquea la misma llave
de schema migration en modo shared y verifica el head exacto dentro de la misma
transacción; el migrator usa el modo exclusivo de esa llave. Después bloquea la
llave de adopción operational, recupera los dos packets exactos y proyecta la
cadena de autoridad completa. Admite el packet como candidato del current o como su
productor directo solo cuando el último evento lo adoptó exactamente; después
rederiva y valida el shadow y el output del kernel, y persiste una identidad UUID
estable. El mismo packet evaluado antes o justo después de su adopción conserva
la misma identidad; un replay solo devuelve el receipt si todo el contenido
inmutable coincide. Un sibling o un evento posterior fallan cerrados.

La capability PostgreSQL dedicada
`b3s_history_vault_sv9_shadow_writer` es `NOLOGIN NOINHERIT`, no posee objetos
ni hereda roles. Tiene `USAGE` de schema, `SELECT` solo sobre el journal de migraciones,
workspace, brand, packets/adopciones necesarios y el ledger para comparar
replay, y `INSERT` solo
sobre el ledger. No obtiene `UPDATE`, `DELETE`, `TRUNCATE`, `REFERENCES`,
`TRIGGER` ni ejecución de los dos trigger functions privados. PostgreSQL exige
`EXECUTE` sobre los tres predicados inmutables usados por CHECK al insertar; no
son un read ni write path adicional. La tabla y esos
helpers/trigger functions se transfieren al owner estable
`b3s_history_vault_provenance_owner`; el runtime-read y el scanner no reciben
membresía ni grants de esta capability.

La migración 027 añade únicamente el writer interno: no añade read path, API pública, worker ni cutover. El receipt es acotado y no devuelve packet payload,
vector semántico ni requirements como una vía de lectura. El ledger conserva
`authority = false` y ambos runtime effects en `false`; no es una evaluación
canónica ni autoriza su consumo por Scanner o producto.

## 12. Descubrimiento acotado para un control plane externo

El siguiente incremento no añade una nueva autoridad ni una migración 028.
`PostgresHistoryRepository.discover_evidence_vault_operational_sv9_shadow_work_items`
consulta, con el head exacto protegido por el lock shared de migraciones, como
máximo 100 identidades pendientes. Para cada brand considera solo el último
evento `operational_v2`, proyecta y valida toda la cadena, recupera el packet
exacto y exige que ese evento y ese packet hayan producido directamente el
current. Una observación ya presente para el mismo packet y parent queda fuera
del resultado. El orden por dominio e identidad de brand es determinista.

El resultado de discovery contiene exclusivamente dominio, fingerprint del
packet operational y parent inmutable. Es advisory: entre discovery y ejecución
puede avanzar la adopción. Por eso el append conserva la única decisión de
freshness y vuelve a verificar head, packets, cadena y causalidad bajo el lock de
promoción. Una carrera puede terminar en replay o rechazo, nunca convertir un
sibling o productor histórico en admisible.

El comando one-shot
`scripts/run_evidence_vault_operational_sv9_shadow_work_items.py` ejecuta esa
capability desde un límite administrativo externo. Usa únicamente
`B3S_PR71_SV9_SHADOW_WRITER_DATABASE_URL`, valida el URL y atesta en cada conexión
TLS, host, database, `current_user`, project y branch exactos de PR71. El modo
predeterminado es dry-run. `--append` procesa exactamente un work item para que
un error no oculte persistencia parcial de un batch; un scheduler futuro puede
invocarlo repetidamente y apoyarse en discovery + replay idempotente.

El output atómico solo expone identidades acotadas, estado y outcome. No incluye
DSN, payload, evidencia, tiles, score, UUID del assessment ni requirements.
Este incremento no crea ni activa scheduler, hook de scanner, ruta web/API, RPC,
trigger, `SECURITY DEFINER`, grant nuevo o integración de scoring/ranking. La
activación de un scheduler requiere un control plane y secret boundary separados
del proceso Fly web/scanner, revisión y autorización operativa propias.

# ADR v3 — Kernel puro de assessment SV9 y ortogonalidad de verification

- **Estado:** Aceptado e implementado como contrato shadow
- **Fecha:** 2026-08-13
- **Ámbito:** aritmética SV9 compartida por Scanner y Evidence Vault
- **Rúbrica fija de este incremento:** `baldosas-v3-1`
- **No implica:** migración, backfill, cambio de read path ni cutover de producto

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

Estas deudas bloquean migración, exposición en read paths y cutover. Cada una
requiere un incremento con fixtures de paridad y decisión de autoridad. Hasta
entonces no debe afirmarse que el Vault operational ya separa score de
verification.

Cualquier migración, exposición pública o cutover necesita una decisión y
validación separadas.

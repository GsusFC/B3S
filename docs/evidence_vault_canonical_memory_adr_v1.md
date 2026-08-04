# ADR v1 — Memoria canónica, autoridad y scoring determinista del Evidence Vault

- **Estado:** Aprobado como contrato arquitectónico; implementación pendiente
- **Fecha:** 2026-08-04
- **Ámbito inicial de autoridad:** `b3s-vault`
- **Fuera de alcance:** producción y scanner actual

## Veredicto

El Evidence Vault no necesita otro ledger ni una reconstrucción. Los contratos
shadow existentes ya acumulan, deduplican y comparan evidencia; preservan
decisiones humanas vinculadas a candidatos exactos; y proyectan relaciones
versionadas entre evidencia, claims opcionales y baldosas.

La capacidad pendiente es una frontera explícita de autoridad que transforme
un paquete candidato reproducible en memoria canónica adoptada y permita
puntuar exclusivamente esa memoria mediante un reductor y un agregador
deterministas.

La arquitectura aprobada es:

```text
Scanner e historiales inmutables
              ↓
Shadow Vault: acumulación, deduplicación y comparación
              ↓
Paquete candidato reproducible
              ↓
Promoción humana del fingerprint exacto
              ↓
Memoria canónica de la marca
              ↓
Reductor determinista por baldosa
              ↓
Agregador determinista y evaluación inmutable
```

Este ADR no concede autoridad retrospectiva a ningún evento existente, no
activa efectos automáticos sobre el scanner y no elige ninguno de los scores
históricos de SoccerSolver como baseline.

Documentos preexistentes conservados por esta decisión:

- [`evidence_ledger_shadow.md`](evidence_ledger_shadow.md)
- [`evidence_memory_snapshot_v1.md`](evidence_memory_snapshot_v1.md)
- [`evidence_accepted_memory_v1.md`](evidence_accepted_memory_v1.md)
- [`evidence_claim_tile_ledger_v1.md`](evidence_claim_tile_ledger_v1.md)
- [`evidence_claim_tile_review_v1.md`](evidence_claim_tile_review_v1.md)
- [`evidence_vault_field_pilot_2026_08_02.md`](evidence_vault_field_pilot_2026_08_02.md)

## 1. Invariantes de autoridad e inmutabilidad

1. El scanner actual continúa funcionando sin modificaciones y sus resultados
   siguen siendo diagnósticos históricos, no memoria canónica.
2. El Shadow Vault puede observar, acumular, deduplicar, comparar y proponer,
   pero no puede modificar estado autorizado.
3. Ninguna evidencia nueva afecta a la memoria canónica antes de una promoción
   válida.
4. La falta de readquisición no elimina ni debilita automáticamente evidencia
   previamente promovida.
5. Los claims son opcionales. Una evidencia puede relacionarse directamente
   con una baldosa cuando el contrato aplicable permita demostrar la relación.
6. Estado semántico y autoridad son dimensiones independientes.
7. Los paquetes candidatos son inmutables y permanecen siempre con
   `authority_state="pending_review"`.
8. `authority_state="accepted"` nunca se escribe en el paquete. Se deriva del
   journal append-only de promociones.
9. Los eventos históricos mantienen `authority=false`. No se editan ni se
   reinterpretan para concederles autoridad retrospectiva.
10. Un evento de promoción nuevo puede tener `authority=true` únicamente con
    `authority_scope="b3s-vault"`. Debe declarar además
    `production_runtime_effect=false` y `scanner_runtime_effect=false`.
11. La memoria canónica cambia únicamente al proyectar una promoción válida.
12. Una repetición con la misma identidad semántica de evidencia y la misma
    procedencia no aumenta soporte, no cambia la memoria canónica y no altera
    el score. Una evidencia distinta y realmente independiente que sostenga la
    misma conclusión puede producir `strengthened`.
13. Los scores históricos `45`, `64` y `62` de SoccerSolver permanecen como
    diagnósticos inmutables. Ninguno se adopta automáticamente.

## 2. Schema del paquete candidato atómico

La unidad de revisión es un paquete completo, no un fichero de decisiones
aislado. El paquete debe contener exactamente el conjunto de baldosas definido
por el registro aplicable —80 en la rúbrica actual— y toda la procedencia
necesaria para revisar cada estado que pueda afectar al score.

Manifest mínimo:

```json
{
  "schema_version": "...",
  "brand_identity": "...",
  "parent_canonical_memory_version": null,
  "authority_state": "pending_review",
  "candidate_memory_version": "...",
  "accepted_memory_candidate_version": "...",
  "reviewed_memory_candidate_version": "...",
  "review_packet_set_fingerprint": "...",
  "rubric_version": "...",
  "tile_contract_registry_fingerprint": "...",
  "reducer_policy_fingerprint": "...",
  "aggregation_policy_fingerprint": "...",
  "coverage_summary": {},
  "unresolved_items": [],
  "derived_tile_state_fingerprint": "..."
}
```

Cada baldosa candidata debe declarar al menos:

```json
{
  "tile_id": "mission.m1",
  "candidate_state": "ok",
  "authority_state": "pending_review",
  "basis": [
    {
      "relation_id": "...",
      "evidence_id": "...",
      "claim_id": null,
      "polarity": "supports",
      "decision_event_id": "..."
    }
  ],
  "provenance_status": "accepted_basis",
  "previous_canonical_state": "sin_evidencia",
  "single_source_dependency": false,
  "changes_tile_state": true,
  "changes_score": true,
  "coverage_refs": [],
  "unresolved_refs": []
}
```

`claim_id=null` es válido. El paquete no duplica texto o mappings cuando estos
pueden resolverse de forma determinista mediante identificadores y
fingerprints inmutables.

Los valores permitidos de `provenance_status` son, como mínimo:

```text
accepted_basis
scanner_derived_unreviewed
no_accepted_basis
contradiction
coverage_unresolved
```

El paquete debe exponer:

- las 80 baldosas candidatas;
- el fundamento aceptado de cada estado;
- las conclusiones derivadas exclusivamente del scanner y aún no revisadas;
- contradicciones y huecos de cobertura;
- dependencias de una única evidencia o fuente;
- diferencias frente a la memoria canónica anterior;
- todas las diferencias que puedan alterar el score.

Firmar el paquete autoriza el conjunto exacto mostrado. No está permitido
heredar silenciosamente conclusiones del último informe.

## 3. Canonicalización, fingerprints e identidades

Todo fingerprint semántico se calcula sobre JSON canónico con:

- claves ordenadas;
- codificación UTF-8;
- representación estable de números, booleanos y `null`;
- arrays ordenados por las claves estables definidas por el schema cuando el
  orden de captura no sea semántico;
- exclusión de timestamps, IDs de ejecución, contadores de observación y otros
  diagnósticos volátiles salvo que el contrato los declare semánticos.

El fingerprint de un paquete no puede incluirse recursivamente en su propio
input:

```text
candidate_packet_fingerprint =
  hash(canonical_json(candidate_packet_without_fingerprint))
```

Se distinguen cuatro identidades:

1. `candidate_memory_version`: identidad shadow ya existente de la memoria
   semántica candidata.
2. `candidate_packet_fingerprint`: identidad exacta del artefacto que ve y
   firma el revisor, incluidos el contexto de cobertura y los unresolved
   items presentados.
3. `canonical_memory_version`: identidad del contenido semántico promovido,
   incluida su procedencia adoptada; excluye revisor, fecha, evento y
   diagnósticos volátiles.
4. `derived_tile_state_fingerprint`: identidad ordenada de los estados de las
   baldosas derivados de una memoria bajo una política declarada.

Una promoción `strengthened` añade procedencia canónica aunque no cambie una
baldosa. Por tanto, debe cambiar `canonical_memory_version`.

Cambiar únicamente cobertura observacional, última readquisición o número de
repeticiones puede cambiar el fingerprint diagnóstico del paquete, pero no
debe cambiar por sí solo `candidate_memory_version` ni
`canonical_memory_version`.

Toda validación falla cerrada si:

- un fingerprint no coincide con el contenido canónico;
- falta alguna versión o fingerprint obligatorio;
- una baldosa no pertenece al registro declarado;
- el conjunto no coincide exactamente con las baldosas del registro;
- el paquete mezcla versiones incompatibles;
- el contenido firmado difiere del contenido presentado;
- una referencia de evidencia, relación, claim opcional o revisión no puede
  resolverse de forma determinista.

## 4. Tabla determinista del reductor

Estados semánticos permitidos:

```text
ok
no
sin_evidencia
contradiction
```

La autoridad se representa por separado:

```text
unreviewed
pending_review
accepted
superseded
```

El paquete candidato usa siempre `pending_review`. `accepted` y `superseded`
son estados derivados por las proyecciones del journal.

Polaridades canónicas mínimas:

```text
supports
contradicts
demonstrates_absence
invalidates_candidate
irrelevant
```

Tabla de reducción:

| Entradas aceptadas | Estado candidato |
| --- | --- |
| Soporte únicamente | `ok` |
| `contradicts` o `demonstrates_absence` únicamente | `no` |
| Sin soporte ni contraevidencia válida | `sin_evidencia` |
| Soporte y contraevidencia válida | `contradiction` |
| Solo candidatos invalidados o rechazados | `sin_evidencia` |

Reglas adicionales:

- `invalidates_candidate` elimina la capacidad de esa evidencia concreta para
  sostener la baldosa; no crea contraevidencia.
- El término histórico `weakens` es ambiguo y nunca se interpreta
  automáticamente como `no`. Requiere una decisión nueva que lo clasifique en
  una polaridad canónica.
- `not_reacquired` conserva el estado canónico anterior y produce, como
  máximo, un diagnóstico `coverage_loss` en shadow.
- La retirada verificada del único soporte produce un candidato a
  `sin_evidencia`; no modifica automáticamente la baldosa canónica.
- Un estado del scanner sin fundamento humano aceptado queda como
  `scanner_derived_unreviewed` y no puede contribuir al scoring canónico.

El reductor debe ser puro: mismas entradas canónicas, mismo registro de
baldosas y misma política producen exactamente el mismo resultado ordenado.

## 5. Cobertura y pruebas de ausencia

`demonstrates_absence` solo es válido cuando el contrato de la baldosa define
explícitamente cómo probar la ausencia y la adquisición demuestra cobertura
suficiente para ejecutar esa prueba.

Cada uso debe incluir:

```text
absence_test_contract_id
coverage_assessment_id
coverage_status = sufficient
tested_scope
observed_result
```

No encontrar una frase, página o señal durante un escaneo nunca basta. Tampoco
bastan:

- un viewport incompleto;
- un límite de páginas agotado;
- una URL no seleccionada;
- un bloqueo de robots, timeout o error del proveedor;
- una extracción parcial;
- una página anteriormente disponible que no fue readquirida.

Cuando la cobertura es insuficiente:

```text
coverage_status = insufficient
polarity = invalidates_candidate | irrelevant
delta = coverage_loss
```

La polaridad se aplica al candidato concreto de prueba de ausencia, no a
evidencia histórica válida. Ese resultado no puede producir `no` ni invalidar
una memoria ya promovida.
La suficiencia de cobertura es específica de cada contrato de baldosa; no se
deduce de un número global de páginas.

## 6. Evento de promoción, idempotencia y concurrencia

La autoridad nace en un evento append-only separado del paquete:

```json
{
  "schema_version": "...",
  "event_type": "canonical_memory_promotion",
  "event_id": "...",
  "sequence": 1,
  "previous_event_id": null,
  "brand_identity": "...",
  "candidate_packet_fingerprint": "...",
  "promotion_policy_fingerprint": "...",
  "parent_canonical_memory_version": null,
  "promoted_canonical_memory_version": "...",
  "decision": "promote",
  "reviewer_id": "...",
  "reviewed_at": "...",
  "rationale": "...",
  "authority": true,
  "authority_scope": "b3s-vault",
  "production_runtime_effect": false,
  "scanner_runtime_effect": false
}
```

Validaciones obligatorias:

1. El paquete existe y su fingerprint coincide.
2. `schema_version` identifica un contrato resoluble del evento.
3. `promotion_policy_fingerprint` coincide con las reglas inmutables usadas
   para validar y proyectar la promoción.
4. Todas las referencias y versiones del paquete son resolubles.
5. El conjunto de baldosas es completo.
6. No existen contradicciones relevantes ni unresolved items bloqueantes.
7. `parent_canonical_memory_version` coincide con la versión canónica vigente.
8. La versión promovida coincide con la proyección semántica exacta del
   paquete.
9. `reviewer_id`, `reviewed_at`, `rationale` y `event_id` están presentes.

Revisión en v1:

- Una promoción requiere exactamente un revisor promotor atribuible.
- El propio evento de promoción representa esa decisión autorizada.
- Una petición idéntica no añade otra firma: se resuelve mediante la regla de
  idempotencia y devuelve el evento existente.
- Una futura doble firma se modelará mediante eventos append-only de
  atestación que referencien la promoción. No duplicará ni modificará el evento
  promovido.

Idempotencia:

- Repetir la misma promoción del mismo paquete contra el mismo parent devuelve
  el evento ya existente y no crea otra versión.
- El mismo paquete no puede producir dos versiones canónicas diferentes.

Concurrencia:

- La promoción usa comparación y escritura sobre
  `parent_canonical_memory_version`.
- Dos promociones diferentes contra el mismo parent no pueden ganar.
- La segunda falla cerrada y debe reconstruir su delta contra la nueva versión
  vigente.

Las correcciones y retiradas posteriores crean nuevos candidatos y nuevos
eventos. Nunca editan ni eliminan promociones anteriores.

## 7. Proyección de memoria canónica

La memoria canónica es el estado operativo reproducible adoptado bajo una
política concreta. No pretende ser verdad objetiva sobre la marca.

La proyección se deriva exclusivamente de la cadena válida de promociones:

```text
promoción aceptada
        ↓
contenido semántico del paquete exacto
        ↓
canonical_memory_version
        ↓
estado y procedencia canónicos por baldosa
```

Debe conservar:

- evidencia promovida y su procedencia;
- claims promovidos cuando existan;
- relaciones directas evidencia → baldosa;
- relaciones evidencia → claim → baldosa;
- estado semántico de cada baldosa;
- decisiones que permiten reconstruir cada relación;
- parent y sucesión de versiones canónicas.

No debe incorporar automáticamente:

- el último resultado del scanner;
- candidatos no revisados;
- repeticiones;
- `not_reacquired`;
- scores históricos;
- timestamps o métricas de adquisición como contenido semántico.

La nueva evidencia produce deltas contra la memoria canónica vigente:

```text
no_change
strengthened
candidate_update
contradiction
coverage_loss
verified_deprecation
```

Reglas de autoridad de los deltas:

- `no_change` permanece en shadow y no requiere promoción.
- `coverage_loss` permanece en shadow y no altera la versión vigente.
- `strengthened` requiere promoción para añadir la nueva procedencia a memoria
  canónica, aunque la baldosa y el score no cambien.
- `candidate_update`, `verified_deprecation` y toda resolución de
  `contradiction` requieren promoción si pretenden cambiar contenido canónico.

## 8. Identidad del scoring y reutilización inmutable

El scoring consume únicamente una memoria canónica promovida. El reductor y
el agregador son deterministas y están identificados por fingerprints de
contenido, no solo por un SHA de Git.

Se separan dos identidades:

```text
score_input_fingerprint =
  hash(canonical_json({
    "derived_tile_state_fingerprint": "...",
    "tile_contract_registry_fingerprint": "...",
    "reducer_policy_fingerprint": "...",
    "aggregation_policy_fingerprint": "..."
  }))
```

```text
evaluation_identity =
  hash(canonical_json({
    "canonical_memory_version": "...",
    "score_input_fingerprint": "..."
  }))
```

`rubric_version` se conserva como etiqueta humana y de auditoría. La semántica
ejecutable queda comprometida por los fingerprints de registro, reductor y
agregador.

Consecuencias:

- Cada nueva `canonical_memory_version` genera una nueva evaluación inmutable.
- Una promoción `strengthened` cambia `canonical_memory_version` y
  `evaluation_identity`, aunque el resultado numérico sea idéntico.
- Si `score_input_fingerprint` ya existe, el cálculo y su desglose pueden
  reutilizarse.
- La nueva evaluación debe registrar esa reutilización mediante una referencia
  como `reused_from_evaluation_identity`; no suplanta la evaluación anterior.
- El mismo `evaluation_identity` nunca puede devolver dos scores o desgloses
  diferentes.
- Un cambio real en las baldosas, contratos, reductor o agregador debe cambiar
  `score_input_fingerprint`.

El resultado inmutable contiene, como mínimo:

```text
evaluation_identity
canonical_memory_version
score_input_fingerprint
derived_tile_state_fingerprint
score
component_breakdown
reused_from_evaluation_identity | null
created_at
```

## 9. Contradicciones antes y después del baseline

Una contradicción en shadow nunca invalida por sí sola un estado promovido.

Sin baseline:

```text
contradiction
    → candidato bloqueado
    → canonical_memory_version = null
    → canonical_score = null
```

Con baseline N:

```text
contradiction
    → candidato N+1 bloqueado
    → candidate_score = null
    → canonical_memory N sigue vigente
    → canonical_score N sigue vigente
```

Una contradicción es relevante y bloqueante cuando puede alterar el estado de
una baldosa puntuable, su fundamento promovible o el resultado agregado. Las
contradicciones meramente diagnósticas deben quedar visibles en
`unresolved_items`, pero solo pueden ser no bloqueantes si el contrato explica
por qué no afectan al contenido canónico propuesto.

Resolver una contradicción exige una decisión atribuible que determine qué
evidencia o relación puede participar en la reducción. La resolución genera
un paquete nuevo y, si se adopta, una nueva promoción. Nunca modifica el
paquete bloqueado ni la memoria anterior.

## 10. Criterios de aceptación: SoccerSolver y segunda marca

SoccerSolver es el primer caso de aceptación integral del contrato, no una
excepción en el código.

### Baseline de SoccerSolver

El candidato inicial debe:

1. reconstruir toda la evidencia histórica recuperable sin convertirla por
   antigüedad en evidencia aceptada;
2. deduplicar semánticamente conservando procedencia y observaciones;
3. retener evidencia `not_reacquired` sin borrarla ni degradarla;
4. incorporar decisiones humanas existentes y mappings revisados;
5. admitir relaciones directas evidencia → baldosa cuando no sea necesario un
   claim;
6. contener exactamente las 80 baldosas del registro aplicable;
7. distinguir fundamento aceptado, inferencia no revisada, ausencia de
   fundamento, contradicción y cobertura insuficiente;
8. mostrar todas las baldosas que puntúan, cambian, dependen de una única
   fuente o presentan contradicciones;
9. producir estados provisionales deterministas y, opcionalmente, un
   `candidate_score_preview` sin autoridad, sin elegir los scores históricos
   `45`, `64` o `62`;
10. bloquear promoción y mantener `canonical_score=null` mientras exista una
    contradicción relevante.

Tras una promoción válida, la primera memoria y evaluación canónicas deben ser
reproducibles desde cero usando únicamente el paquete firmado, los contratos
versionados y el journal.

### Pruebas de invariantes sobre SoccerSolver

- Reordenar historiales no cambia identidades semánticas.
- Repetir la misma identidad semántica de evidencia y procedencia no cambia
  memoria ni score canónicos; una fuente independiente se clasifica
  separadamente como posible `strengthened`.
- Omitir una página durante una readquisición produce `coverage_loss` y no
  elimina evidencia promovida.
- Un `weakens` histórico no produce `no`.
- Una ausencia sin contrato y cobertura suficiente no produce `no`.
- Una contradicción nueva bloquea N+1 y conserva baseline N.
- Una promoción `strengthened` cambia memoria y evaluación, conserva
  `score_input_fingerprint` cuando las baldosas son idénticas y reutiliza el
  resultado numérico.
- Alterar un byte semántico del paquete después de la firma hace fallar la
  promoción.
- Dos promociones concurrentes contra el mismo parent no pueden crear dos
  sucesores vigentes.
- El scanner y la producción permanecen sin cambios observables.

### Validación con una segunda marca

El mismo flujo debe ejecutarse después con CausaPrima, u otra marca con
historial suficiente elegida antes de comenzar la prueba, sin flags, reglas o
ramas específicas por marca.

La segunda validación debe demostrar:

- construcción del mismo schema de 80 baldosas;
- promoción mediante el mismo tipo de evento;
- reproducción determinista de memoria y score;
- estabilidad ante repetición y pérdida de adquisición;
- generación de un delta posterior contra su propia memoria canónica;
- ausencia de condicionales por dominio o marca en la implementación.

## Decisión final

La implementación futura queda limitada a:

1. contrato atómico del paquete candidato;
2. reductor con la semántica definida en este ADR;
3. evento de promoción append-only;
4. proyección de memoria canónica;
5. identidad y resultado de scoring determinista;
6. baseline completo de SoccerSolver;
7. validación posterior con una segunda marca.

No se construirá otro ledger. No se modificará el scanner actual. No se
trasladará la deriva del scanner a memoria canónica. La autoridad inicial
pertenecerá exclusivamente a `b3s-vault`.

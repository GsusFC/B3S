# Evidence accepted memory v1

## Veredicto

La memoria de evidencia aceptada ya tiene un contrato ejecutable que coincide
con el comportamiento de producto buscado:

> Una evidencia con aceptación humana vigente permanece en memoria. Una nueva
> variante se añade como otra identidad y no sustituye silenciosamente a la
> anterior.

La implementación vive en
`src/services/evidence_accepted_memory.py`. Es una selección candidata sobre
los informes inmutables y el journal de adjudicaciones existente. No es todavía
memoria canónica ni afecta al scanner:

```text
runtime_effect = false
authority = false
automatic_scoring_effect = false
canonical_evidence_available = false
```

## Regla de selección

Solo entra en la selección una identidad de evidencia cuyo evento vigente sea
`accepted`.

El evento vigente debe incluir ID, secuencia positiva, revisor, razón,
justificación, versión del evaluador y timestamp válido. Además debe declarar
`runtime_effect=false` y `authority=false`. Una aceptación sin atribución o
que pretenda autoridad falla antes de construir la selección.

- `proposed`, `disputed` y `rejected` quedan fuera;
- `revoked` retira la evidencia de la selección vigente;
- una adquisición posterior fallida no equivale a revocación;
- una nueva cita o contenido en el mismo documento crea otra identidad;
- aceptar esa nueva identidad la añade junto a las anteriores;
- ninguna variante se convierte automáticamente en sustituta.

La proyección no inventa aceptaciones deterministas para evidencia propia. La
propiedad del dominio, la repetición o una identidad positiva pueden proponer
elegibilidad, pero no reemplazan el evento humano exigido por esta capa.

## Dos fingerprints distintos

`accepted_memory_candidate_version` representa únicamente el conjunto
semántico aceptado:

- dominio de marca;
- identidad de evidencia;
- documento y pasaje;
- slot opcional;
- clase y tipo de fuente;
- hash del contenido.

Excluye deliberadamente:

- presencia en el último scan;
- número de observaciones;
- fechas;
- ID, revisor y justificación del evento;
- orden de entrada.

Por eso una repetición exacta, un fallo de adquisición o una nueva variante no
aceptada no cambian esa versión.

`state_fingerprint` sí cubre los diagnósticos completos. Puede cambiar mientras
la memoria aceptada permanece idéntica.

## Revocación y trazabilidad

La selección vigente no es un almacén destructivo. Los informes y eventos
originales permanecen append-only en PostgreSQL.

Una revocación explícita cambia la versión candidata y elimina la identidad de
la vista aceptada, pero no elimina:

- la evidencia del snapshot histórico;
- la observación que la contenía;
- el evento de aceptación;
- el evento de revocación.

La diferencia entre “no apareció otra vez” y “un humano revocó su aceptación”
queda así formalizada.

## Garantías ejecutables

Las regresiones prueban:

1. aceptación estable tras repetición exacta;
2. retención tras un scan sin evidencia;
3. variante nueva no revisada sin sustitución;
4. dos variantes aceptadas coexistiendo en el mismo documento;
5. revocación explícita modificando solo la selección vigente;
6. rechazo fail-closed de adjudicaciones que apuntan a evidencia inexistente;
7. ausencia de texto de evidencia en el payload público;
8. aceptación sin atribución o con autoridad rechazada fail-closed;
9. orden de informes irrelevante para el resultado;
10. una nueva revisión `accepted` del mismo sujeto cambia la trazabilidad, no
    la versión semántica;
11. autoridad y scoring siempre deshabilitados.

El stress harness incorpora la misma propiedad como su garantía ejecutable
número 30 bajo `evidence-memory-stress-policy-v16`.

## Límite de promoción

Este bloque resuelve la semántica de acumulación, no la promoción.

Sigue pendiente adoptar formalmente una política de evidencia canónica y
resolver por separado:

- corroboración por claim;
- selección canónica entre variantes de un claim;
- mappings claim→tile revisados y promovidos;
- evaluación versionada sobre esa memoria.

Hasta entonces, `accepted_memory_candidate_version` es un artefacto de sombra
útil para regresión y diagnóstico, no una entrada de producción.

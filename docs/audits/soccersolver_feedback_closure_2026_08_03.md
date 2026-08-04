# Cierre del feedback B3S · SoccerSolver

Fecha de contraste: 2026-08-03.

Este documento responde punto por punto al informe
`FLOC_Feedback_B3S_CasoSoccerSolver (1).md`. Distingue entre el defecto
original, el estado del scanner desplegado y el trabajo que todavía falta. Un
cambio no se considera resuelto solo porque exista infraestructura relacionada.

Estado de este cierre: las correcciones descritas como “implementadas” están
en la rama local `fix/soccersolver-feedback-closure`. Todavía no se han
commiteado, desplegado ni validado mediante un nuevo scan de campo.

## Evidencia contrastada

- A: `3476699ee3df`, build `28560763f5e6`, 45/100, 2026-07-30.
- B: `71cc5ff62c08`, build `67b7093617c5`, 64/100, 9 de 22 páginas.
- C: `4ea97d74f9fa`, build `93a6663b9eb1`, 62/100, 18 de 22 páginas.
- Último contraste: `848bd19b37ae`, build `a5fa894f43f3`, 64/100,
  18 de 22 páginas y `evaluation_drift` no canónico.
- Sitemap público consultado el 2026-08-03: 22 URLs, sin elementos
  `lastmod`.

## Matriz de cierre

| Punto | Veredicto | Evidencia y respuesta | Cierre requerido |
|---|---|---|---|
| P1 · robots y sitemap | Resuelto para scans nuevos | El colector lee `robots.txt`, resuelve índices y sitemaps, aplica `Disallow`, prueba fallbacks y conserva diagnóstico. B descubrió las 22 URLs; C capturó 18. | Mantener las pruebas genéricas. No hace falta otro crawler específico de SoccerSolver. |
| P2 · páginas y evidencia material | Parcial por límite histórico | C y el último scan capturan `/media-and-press`, la serie editorial, `/investment-round` y `/new-way-of-signings`. `/betis-partner` y `/gimnastic-agree` ya no están en el sitemap actual y nunca fueron capturadas por los snapshots históricos: no se puede afirmar que se hayan recuperado. La rama amplía de 6 a 12 chunks la conservación por subpágina solo en vault, mantiene finito el límite y declara las omisiones restantes. | Implementado y probado con copy distintivo tardío. Falta el scan de campo; las dos páginas históricas solo son recuperables desde una fuente externa verificable o un archivo web, no desde los scans existentes. |
| P3 · ausente frente a no encontrado | Parcial | Existen los estados `positive_evidence`, `implied_not_explicit`, `probable_absent`, `verified_absent` e `insufficient_acquisition`. La rama hace visible ese estado en cada tarjeta y retiene los scores cuando la clasificación impide publicarlos. El valor bruto puede seguir siendo 0 para `probable_absent`; ya no se publica como medición si el run tiene deriva o regresión de adquisición. | Presentación corregida. La semántica de agregación de ausencias aún exige una decisión explícita de rúbrica. |
| P4 · idioma por página | Parcial | La captura persiste idioma observado por página, distribución y mismatch con el HTML; la UI y Markdown muestran un hallazgo cuando hay mezcla. Esa señal todavía no altera Coherencia. En el sitio actual se observaron 17 páginas en inglés y una indeterminada; las páginas españolas citadas por el feedback ya no forman parte de la captura actual. | Mantener el hallazgo determinista. No convertirlo en puntos hasta definir qué baldosa y qué contrato de evidencia debe debilitar. |
| P5 · presencia y jerarquía | Parcial, con mejor enfoque | Cada componente ya conserva `presence_status`, `hierarchy_status`, superficies y estado `homepage`, `linked_from_home` o `sitemap_only`; la UI tiene una sección “Presencia y jerarquía”. No existe un segundo score 0–10. | Conservar jerarquía categórica y verificable. Añadir otro score LLM introduciría otra fuente de deriva sin aportar precisión. |
| P6 · transparencia de cobertura | Resuelto en la rama | B y C ya muestran N/M, URLs visitadas/no visitadas y motivo. La rama añade el alcance elegible y, por separado, los fragmentos conservados/omitidos en superficies con muestreo parcial. | Validar la presentación contra un scan de campo. No confundir nunca cobertura de URL con cobertura de contenido. |
| P7 · fecha, recencia y reescaneo | Resuelto en la rama con una limitación externa | La rama compara `lastmod` con la fecha del scan, alerta si el sitemap es posterior y ofrece reescaneo con la misma URL/marca. | El sitemap actual de SoccerSolver no publica `lastmod`, por lo que esa fuente no puede detectar recencia hasta que el sitio la proporcione. |
| P8 · determinismo o incertidumbre | Mitigado; causa de fondo pendiente | C y `848bd19b37ae` tienen evidencia material equivalente y evaluaciones distintas. Una política única retiene el score global y los scores de componentes en informe, Markdown, portada, historial y API ante deriva, regresión de adquisición, evidencia candidata, error de comparación, contrato incompatible o resultado inválido; los valores brutos siguen auditables. | La deriva ya no se publica como verdad. La estabilización de fondo sigue dependiendo de evidencia/claims/baldosas persistentes y validadas; temperatura 0 por sí sola no garantiza determinismo. |
| P9 · confianza por convergencia | Pendiente | La confianza actual deriva de evidencia/puntos ciegos o de la lectura LLM; no mide varianza entre pasadas y no pondera el global por convergencia. | No renombrar la confianza existente. Añadir convergencia solo cuando haya observaciones comparables suficientes. |
| P10 · score y cobertura | Formulación monotónica incorrecta; mitigación implementada | Más evidencia puede bajar legítimamente un score si contradice o debilita una afirmación. La rama retiene resultados divergentes cuando la evidencia es equivalente, pero aún no explica qué evidencia causó cada cambio cuando el corpus sí cambia. | Implementado el fail-closed para evidencia equivalente. Pendiente el delta causal por componente para evidencia distinta. |
| P11 · baseline y rúbrica | Resuelto en la rama | Se expone y fingerprinta rúbrica, modelo, evaluador, schema candidato, prompt, shortlist, etiquetado y autoridad del gate. Un contrato distinto se clasifica `contract_mismatch` y no se compara numéricamente. Una repetición fiable y estable del contrato nuevo crea el nuevo baseline para no quedar bloqueados en la rúbrica anterior. | Validar la transición en campo con dos runs del mismo contrato. |
| P12 · literal frente a inferido | Alegación concreta incorrecta; presentación resuelta | MG1 de C cita literalmente el snapshot: `99% Faster scouting & signings process`; el snapshot no contiene `99.93% Faster`, por lo que no hubo ese redondeo en el evaluador. La rama etiqueta “cita literal” e “interpretación” y el Markdown enumera las citas positivas persistidas. | El hueco histórico de `99.93%` sigue siendo de adquisición/conservación, no de literalidad del evaluador. |
| P13 · 22 frente a 27 y presupuesto | Resuelto para el estado observable | El sitemap público del 3 de agosto contiene exactamente 22 URLs. C omite solo `/contact-us`, términos, privacidad y `/404` por baja prioridad. La rama muestra denominador total y elegible y el manifest conserva el universo observado en el momento del scan. | El “27” histórico no se puede reconstruir con el sitemap actual; no se usa como denominador sin su manifest original. |

## Correcciones raíz de este cierre

1. Un resultado `evaluation_drift` conserva sus números para auditoría, pero
   no los presenta como score publicable. Lo mismo ocurre cuando hay
   `acquisition_regression`, evidencia nueva aún candidata, error de
   comparación, contrato incompatible o resultado inválido.
2. El informe expone el contrato exacto con el que fue interpretado y evaluado;
   contratos distintos no son series comparables.
3. El perfil vault conserva más contenido de cada página capturada sin elevar
   el presupuesto de páginas ni modificar producción.
4. La UI distingue cita literal del snapshot de síntesis interpretativa.
5. La cobertura distingue universo total y páginas elegibles, y ofrece
   reescaneo/alerta de recencia cuando los datos del sitemap lo permiten.
6. La cobertura de URL queda separada de la conservación de contenido: el
   informe declara cuántos fragmentos se retuvieron y omitieron en cada
   superficie sometida a muestreo parcial.

## Fuera de este cambio

- No se declara que la serie editorial sea por sí sola una visión. Demuestra
  una idea de categoría y una voz propia; una visión exige además un estado
  futuro deseado o una dirección de transformación.
- No se crean claims vacíos por cada evidencia. La cardinalidad sigue siendo
  evidencia → cero o más claims.
- No se crea un segundo almacén de evidencia para SoccerSolver.
- No se activa `runtime_effect` ni `authority` para el vault.

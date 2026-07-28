# Brand3 Local vs Deploy Regression Harness

## Veredicto

Sí necesitamos una prueba local contra deploy para saber si un cambio degrada el resultado real. Los tests unitarios protegen contratos, pero no comparan el comportamiento completo de Brand Audit + Magnetism Scanner frente a producción.

El harness vive fuera del pipeline productivo:

```text
scripts/compare_local_deploy_pipeline.py
```

## Qué compara

El script puede trabajar en dos modos:

- `--mode api`: usa la API autenticada del Scanner. Es el modo más fuerte porque devuelve JSON de Scanner, Evidence, Methodology y Audit.
- `--mode web`: usa el formulario público y compara páginas visibles. No necesita token y sirve para detectar fallos operativos en deploy.
- `--mode auto`: usa API si encuentra `BRAND3_SCANNER_API_TOKEN`; si no, cae a modo web.

Para decisiones de refactor, usar `--mode api`. El modo web es útil como smoke test de producto público, pero mezcla contenido, plantilla HTML, traducciones y heurísticas de scraping de texto visible. No debe ser la señal principal para decidir si readiness, publicación, Research Pack o TLDR se han degradado.

En modo API, el script usa la API del Scanner como entrada común:

1. `POST /api/v1/scanner` con la misma URL en local y deploy.
2. Espera a que ambos scans estén `ready` o `failed`.
3. Lee:
   - `/api/v1/scanner/{id}`
   - `/api/v1/scanner/{id}/result`
   - `/api/v1/scanner/{id}/evidence`
   - `/api/v1/scanner/{id}/methodology`
   - `/api/v1/scanner/{id}/audit`
4. Normaliza señales comparables de Scanner y Brand Audit.
5. Escribe JSON y Markdown con diferencias.

Esto cubre Brand Audit porque un scan por URL crea o adjunta un `source_run_id`, y el endpoint `/audit` devuelve el snapshot del audit asociado.

## Señales que vigila

- `scanner_readiness.status`
- `publication_decision.status`
- `publication_decision.publishable`
- `report_readiness.report_mode`
- `magnetism_score`
- `coherence_score`
- `composite_score`
- cuadrante
- versión de pipeline
- número de fuentes/evidencias normalizadas
- bloques TLDR detectados
- cambios de detección por bloque TLDR
- `scan_mode.mode`
- `scan_mode.comparable`
- `generated_with.evidence_graph`
- `generated_with.analyst_pass`
- `generated_with.research_pack_quality`
- `methodology.research_pack_source`
- `methodology.tldr_generation_mode`
- `methodology.analysis_error`

El Markdown final incluye una sección `Contract Signals` por caso. Esa sección es la lectura rápida para saber si local y deploy están usando el mismo contrato operativo, aunque los textos visibles o los scores varíen por ruido de proveedor.

El Markdown tambien puede incluir `Score Diagnostics`. Esa seccion muestra, cuando la API lo expone, los breakdowns internos usados para `scanner.score_coherence` y `scanner.score_magnetism`. Es la primera lectura para distinguir:

- variacion de completitud TLDR;
- variacion de alineacion semantica;
- variacion por contradicciones detectadas;
- variacion real de magnetismo frente a variacion de coherencia.

Si solo cambia `scanner.score_coherence` pero los contratos coinciden, revisar primero `scanner.score_coherence_breakdown` antes de tratarlo como regresion de pipeline.

## Cómo ejecutarlo

Arranca local:

```bash
scripts/run_web_dev_macos.sh
```

En otra terminal, sin token:

```bash
./.venv/bin/python scripts/compare_local_deploy_pipeline.py \
  --mode web \
  --url https://www.sklum.com \
  --local-base http://127.0.0.1:8000 \
  --deploy-base https://b3s.fly.dev
```

Con token de Scanner API:

```bash
export BRAND3_SCANNER_API_TOKEN=...

./.venv/bin/python scripts/compare_local_deploy_pipeline.py \
  --mode api \
  --url https://www.sklum.com \
  --local-base http://127.0.0.1:8000 \
  --deploy-base https://b3s.fly.dev
```

Salida:

```text
scratch/local_vs_deploy_pipeline_compare/latest.md
scratch/local_vs_deploy_pipeline_compare/latest.json
```

## Interpretación

Un resultado sin diferencias materiales no demuestra que la lectura sea buena; demuestra que local no se ha desviado de deploy según los umbrales actuales.

Un `critical` significa que no deberíamos publicar o desplegar sin revisar:

- local/deploy falla;
- deploy falla aunque local complete;
- cambia readiness;
- cambia publicación;
- Audit deja de ser publicable;
- Scanner deja de ser publicable;
- un scan deja de ser comparable.

Un `warning` exige revisión humana, no bloqueo automático:

- delta de score mayor de 5 puntos;
- cambio de cuadrante;
- cambio de versión de pipeline;
- caída relevante en fuentes/evidencias;
- cambio de detección TLDR.
- cambio de fuente Research Pack;
- cambio de modo TLDR;
- cambio en `generated_with`;
- aparece o cambia `analysis_error`.

## Uso recomendado

Antes de desplegar cambios en adquisición, readiness, Research Pack, Analyst Pass o Scanner:

```bash
./.venv/bin/python scripts/compare_local_deploy_pipeline.py \
  --mode api \
  --url https://www.sklum.com \
  --url https://www.langchain.com \
  --url https://www.netlify.com
```

Mantener un set pequeño:

- una marca conocida con buena evidencia;
- una marca con datos pobres o caso problemático, como SKLUM;
- una marca SaaS/tech usada como fixture mental;
- una marca con riesgo de colisión de entidad.

## Limitaciones

- Consume ejecución real de proveedores y LLM en local y deploy.
- Si deploy ya incluye el mismo commit, la comparación mide entorno/datos/proveedores, no diferencia de código.
- Si hay variabilidad LLM, conviene comparar dos ejecuciones deploy-deploy para establecer ruido base.
- No sustituye fixtures ni tests unitarios; es una prueba de regresión operativa.
- El modo API requiere `BRAND3_SCANNER_API_TOKEN` válido en local y deploy.
- El modo web no expone todas las señales de contrato; si aparece `score_magnetism: None`, no significa necesariamente que el Scanner no haya calculado magnetismo, sino que el HTML visible no permitió extraerlo de forma robusta.

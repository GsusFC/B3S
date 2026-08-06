# Suplementos de relaciones directas de evidencia v1

Este contrato corrige omisiones conocidas del mapper sin modificar el scanner,
inventar evidencia ni otorgar autoridad al shadow vault.

## Alcance

Un suplemento registra candidatos adicionales `evidencia → baldosa` usando el
mismo schema, interfaz de revisión, journal append-only y resolver que las
relaciones descubiertas automáticamente.

El suplemento:

- solo puede reutilizar pasajes cuya identidad humana ya está aceptada;
- puede agrupar varias entradas para una relación multi-pasaje;
- conserva las citas, URLs e identidades de evidencia exactas;
- queda vinculado a la marca, rúbrica y contexto de generación;
- permanece siempre con `authority=false` y `runtime_effect=false`;
- no modifica reports, decisiones anteriores, scoring operativo ni memoria
  canónica.

El fingerprint global de identidad y el conjunto base de candidatos se
conservan como procedencia de generación. No invalidan una relación por cambios
no relacionados. La relación sigue vigente mientras coincidan marca y rúbrica,
y todas las identidades exactas citadas continúen aceptadas.

Si el mapper aprende después la misma relación exacta, ambas rutas se
deduplican por `case_id` y `candidate_fingerprint`; no se crea una segunda
decisión.

## Frontera de seguridad

El registro falla de forma cerrada cuando:

1. una propuesta referencia evidencia que no está en el preview acumulativo;
2. una identidad citada no está aceptada;
3. la cita, URL, fuente o agrupación no puede reconstruirse exactamente;
4. cambia la rúbrica o la marca;
5. existe una colisión con contenido diferente;
6. se altera el paquete después de calcular su fingerprint.

Un paquete registrado solo añade candidatos pendientes. Una relación empieza a
formar parte del candidato de memoria cuando su evento humano exacto queda
`accepted`. Aun entonces sigue sin autoridad hasta la promoción posterior del
paquete canónico completo.

## Operación

El fichero de propuesta contiene únicamente referencias a evidencia ya
aceptada:

```json
{
  "schema_version": "evidence-scoring-recovery-supplement-proposal-v1",
  "relations": [
    {
      "component_key": "magnetism",
      "tile_id": "MG2",
      "passages": [
        {
          "evidence_id": "<sha256>",
          "quote": "Extracto literal del report inmutable"
        }
      ]
    }
  ]
}
```

La previsualización no escribe:

```text
python scripts/evidence_scoring_recovery_supplement.py \
  --domain example.com \
  --proposals /ruta/proposals.json
```

El registro append-only requiere `--register`. El comando solo imprime
fingerprints, IDs de caso y contadores; no expone las citas protegidas en la
salida.

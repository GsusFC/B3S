# Pools de claves Exa y Firecrawl

B3S Scanner conserva las variables singulares existentes y permite añadir cualquier número de
credenciales mediante variables plurales:

```dotenv
EXA_API_KEY=clave-actual
EXA_API_KEYS=clave-adicional-1,clave-adicional-2
FIRECRAWL_API_KEY=clave-actual
FIRECRAWL_API_KEYS=clave-adicional-1,clave-adicional-2
```

Las listas admiten valores separados por comas o saltos de línea. Scanner elimina duplicados,
mantiene la clave primaria en primera posición y distribuye las llamadas mediante round-robin
thread-safe. Si una llamada falla, prueba las demás claves del pool una vez antes de degradar al
fallback existente. Los errores y diagnósticos nunca incluyen las credenciales.

## Despliegue en Fly.io

Las variables plurales deben guardarse como secretos, sin eliminar las singulares existentes:

```bash
fly secrets set EXA_API_KEYS="clave-adicional-1,clave-adicional-2"
fly secrets set FIRECRAWL_API_KEYS="clave-adicional-1,clave-adicional-2"
```

Después del despliegue, `fly secrets list` permite comprobar los nombres y digests sin revelar los
valores.

## Límites reales

- Exa permite configurar límites por clave, pero los créditos contratados pueden pertenecer a la
  cuenta o equipo que emitió la credencial.
- Firecrawl aplica sus límites por team. Varias claves del mismo team rotan, pero no suman cuota.
  Para sumar capacidad deben pertenecer a teams con cuotas independientes.

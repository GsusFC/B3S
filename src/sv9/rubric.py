"""SV9 rubric (baldosas v3.1): components, tiles, pairs, and scoring rules as data.

This module is the SV9 counterpart of the legacy `src/dimensions.py`. It defines
the 9 scored components plus Coherencia, their 80 independent tiles (baldosas),
the pair groupings, the x2 multipliers, the Magnetism cap rule, and the
confidence thresholds.

Design source of truth: docs/baldosas-v3.1.md.

Rules encoded here:
- TILE MODEL (v3.1, replaces the cumulative ladders of v2). Each tile is an
  independent key characteristic that is met or not. A component's score is the
  number of lit tiles — there is no order and no dependency between tiles. The
  tile texts in `condition` are FINAL COPY (briefing section 3), shown verbatim
  in the canvas and the calibration form.
- THREE STATES, TWO MEANINGS OF ZERO. A tile is `ok` (lit, +1 point), `no`
  (off: the snapshot proves the brand does not communicate or meet it — a brand
  failure) or `sin_evidencia` (blind spot: the snapshot structurally cannot hold
  the proof — not a brand failure). `no` and `sin_evidencia` both score 0 but are
  never conflated in the report or the UI.
- CONFIDENCE. A component drops to `media` confidence with 2 `sin_evidencia`
  tiles and to `baja` with 3 or more.
- Every score and calibration record is traced against RUBRIC_VERSION. Any
  wording change to a tile must bump the version (governance: tiles change by
  calibration, not by intuition; see docs/baldosas-v3.1.md section 5).
- The LLM judges tiles and quotes evidence. The score, the x2 multipliers, the
  confidence index, and the Magnetism cap are all computed by code
  (src/sv9/aggregator.py), never by the model.
"""

from __future__ import annotations

RUBRIC_VERSION = "baldosas-v3-1"

# The model label persisted on every scan and shown in the ranking during the
# migration window. Scans from earlier rubric versions are labelled "v2".
MODEL_LABEL = "v3.1"
LEGACY_MODEL_LABEL = "v2"

# Model routing (deploy brief section 2.6): the 8 base components run on the
# fast Flash tier; Magnetism and Coherencia — the two fine judgments that weigh
# 40/100 and concentrate the bias — run on the reasoning tier. The concrete
# model ids are parameterized in src/config.py so the routing can be measured in
# regression (Flash vs reasoning on these two components).
REASONING_COMPONENTS = ("magnetism", "coherencia")

# Component statuses (same lifecycle as v2).
STATUS_SCORED = "scored"            # detectado: evaluated against the tiles
STATUS_NOT_DETECTED = "not_detected"  # no_detectado: diagnosis, scores 0
STATUS_NOT_EVALUATED = "not_evaluated"  # no_evaluado: technical failure, scores 0, retryable

# Tile states (briefing section 1, rule 3).
ESTADO_OK = "ok"
ESTADO_NO = "no"
ESTADO_SIN_EVIDENCIA = "sin_evidencia"
TILE_ESTADOS = (ESTADO_OK, ESTADO_NO, ESTADO_SIN_EVIDENCIA)

# Confidence thresholds (briefing section 1, rule 4; section 4).
CONFIDENCE_ALTA = "alta"
CONFIDENCE_MEDIA = "media"
CONFIDENCE_BAJA = "baja"
CONFIDENCE_MEDIA_BLIND_SPOTS = 2
CONFIDENCE_BAJA_BLIND_SPOTS = 3

# Magnetism cap rule (briefing section 1, rule 5): if the normalized mean of the
# 8 base components is below this threshold (0-10 scale), the lit Magnetism tiles
# are capped at MAGNETISM_CAP_VALUE (10/20 after the x2).
MAGNETISM_CAP_BASE_THRESHOLD = 4.0
MAGNETISM_CAP_VALUE = 5

# Coherencia review queue rule: scans with Coherencia at or below this score
# enter the priority human review queue. Internal only.
COHERENCIA_REVIEW_THRESHOLD = 3

# Presentation order. Calculation order is independent.
PRESENTATION_ORDER = [
    "mission",
    "vision",
    "values",
    "attributes",
    "value_proposition",
    "personality",
    "brand_idea",
    "core_purpose",
    "magnetism",
    "coherencia",
]

# The 8 base components used for the Magnetism cap mean (everything except
# magnetism and coherencia).
BASE_COMPONENTS = [
    "mission",
    "vision",
    "values",
    "attributes",
    "value_proposition",
    "personality",
    "brand_idea",
    "core_purpose",
]

# C8's evaluator note lives inside the tile definition (prompt técnico, step 1).
_C8_NOTE = (
    "El scanner no puede probar el producto. Por defecto sin_evidencia, salvo que "
    "el snapshot contenga pruebas sociales contundentes: reviews, casos de estudio "
    "detallados o demos comprobables."
)

TILE_EVIDENCE_CONTRACT_VERSION = "tile-evidence-contract-v0-1"

_VALUE_PROPOSITION_TILE_CONTRACTS = {
    "P1": {
        "ok": "La primera pantalla o superficie principal dice qué producto/servicio ofrece.",
        "no": "La hero solo contiene claim aspiracional, marca, menú o categoría vaga.",
        "sin_evidencia": "Solo aplica si no hay captura usable de superficie propia principal.",
        "strong_sources": "owned_copy, hero, homepage, product page",
        "reject": "No usar prensa o descripción externa para encender la claridad de la hero.",
    },
    "P2": {
        "ok": "Una persona ajena a la categoría puede entender qué se vende y para qué sirve.",
        "no": "Depende de jerga, siglas o claims que solo entiende un insider.",
        "sin_evidencia": "Raro; usar solo si la captura textual es ilegible o incompleta.",
        "strong_sources": "owned_copy, product description, homepage",
        "reject": "No confundir familiaridad del evaluador con claridad del snapshot.",
    },
    "P3": {
        "ok": "El texto expresa resultado, cambio o beneficio para el usuario/cliente.",
        "no": "Solo lista features, categorías, productos o tecnología.",
        "sin_evidencia": "Usar si el snapshot solo conserva navegación/catálogo sin claims.",
        "strong_sources": "owned_copy, customer-facing copy, case snippets",
        "reject": "No convertir una feature en beneficio si el resultado no está dicho o implicado por mecanismo literal.",
    },
    "P4": {
        "ok": "El público está nombrado o queda inequívoco por producto, contexto y lenguaje.",
        "no": "El producto podría ser para varias audiencias incompatibles sin resolverlo.",
        "sin_evidencia": "Usar si faltan superficies de producto/audiencia suficientes.",
        "strong_sources": "owned_copy, product page, pricing, external profile",
        "reject": "No exigir persona explícita si el producto y contexto delimitan claramente al comprador.",
    },
    "P5": {
        "ok": "Aparece dolor, deseo, necesidad, tensión de categoría o frontera tecnológica.",
        "no": "La oferta se presenta como inventario, utilidad o claim positivo sin conflicto.",
        "sin_evidencia": "Usar si el snapshot no incluye suficiente copy argumental.",
        "strong_sources": "owned_copy, manifesto, product narrative, external product description",
        "reject": "No aceptar tensión genérica como 'mejor', 'innovador' o 'fácil'.",
    },
    "P6": {
        "ok": "La frase o mecanismo no podría firmarlo un competidor directo sin cambiar nada.",
        "no": "La promesa es categoría estándar o intercambiable.",
        "sin_evidencia": "Usar cuando no hay cohorte o alternativas para comparar mínimamente.",
        "strong_sources": "owned_copy plus competitor/context evidence",
        "reject": "No premiar una palabra propia si la promesa sigue siendo genérica.",
    },
    "P7": {
        "ok": "El snapshot explica explícitamente por qué elegirla frente a alternativas.",
        "no": "Hay alternativas visibles y la marca no formula diferencial.",
        "sin_evidencia": "Si no hay evidencia de cohorte/competidores, marcar punto ciego.",
        "strong_sources": "comparison copy, competitor pages, external category context",
        "reject": "No inferir diferencial solo porque la marca tiene una feature.",
    },
    "P8": {
        "ok": "El cómo de la propuesta está nombrado: método, mecanismo, tecnología, moneda, ritual o sistema.",
        "no": "Promete resultado sin explicar cómo lo produce.",
        "sin_evidencia": "Usar si solo hay claim resumido sin páginas de producto/metodología.",
        "strong_sources": "owned_copy, product page, documentation, external product explanation",
        "reject": "No aceptar 'IA', 'plataforma' o 'ecosistema' como mecanismo por sí solos.",
    },
    "P9": {
        "ok": "La primera pantalla muestra prueba: cifras, clientes, logos, auditorías, ratings, casos o garantías.",
        "no": "La prueba existe quizá en otra parte, pero no aparece en primer impacto.",
        "sin_evidencia": "Si no hay captura fiable de primera pantalla.",
        "strong_sources": "homepage/hero visual or text capture",
        "reject": "No usar prensa profunda para encender una baldosa de primera pantalla.",
    },
    "P10": {
        "ok": "La promesa se apoya en dato, caso, garantía, auditoría, benchmark o demostración verificable.",
        "no": "La promesa queda sin prueba verificable dentro del snapshot.",
        "sin_evidencia": "Usar si el tipo de prueba requeriría acceso privado no capturado.",
        "strong_sources": "owned proof, third-party proof, audits, case studies, metrics",
        "reject": "No aceptar adjetivos de confianza como prueba.",
    },
}

_MAGNETISM_TILE_CONTRACTS = {
    "MG1": {
        "ok": "Existe un elemento verbal o visual que retiene atención: promesa fuerte, tensión, imagen, mecanismo o prueba social.",
        "no": "La superficie es meramente descriptiva, catálogo o corporate boilerplate.",
        "sin_evidencia": "Solo si falta captura usable de superficie principal.",
        "strong_sources": "hero, owned_copy, visual signal, product mechanism",
        "reject": "No encender solo por diseño pulido sin una razón concreta de retención.",
    },
    "MG2": {
        "ok": "Se identifica el mecanismo dominante: dolor, deseo, asombro, pertenencia o estatus.",
        "no": "Hay claim atractivo pero no se entiende por qué retiene.",
        "sin_evidencia": "Usar si la evidencia solo muestra navegación o inventario.",
        "strong_sources": "owned_copy, product mechanism, community/product proof",
        "reject": "No inventar mecanismo psicológico sin señal literal o visual descrita.",
    },
    "MG3": {
        "ok": "Hay gancho claro vinculado a tensión real de la audiencia.",
        "no": "Hay claim, pero no conflicto, deseo o problema concreto.",
        "sin_evidencia": "Usar si falta contexto de audiencia/tensión.",
        "strong_sources": "hero, tagline, campaign copy, product narrative",
        "reject": "No confundir eslogan descriptivo con hook.",
    },
    "MG4": {
        "ok": "La marca plantea una historia: antes/después, enemigo, conflicto, conquista o transformación.",
        "no": "Solo enumera beneficios o features.",
        "sin_evidencia": "Usar si el snapshot no incluye suficiente narrativa.",
        "strong_sources": "owned_copy, manifesto, about, product story",
        "reject": "No aceptar una lista de ventajas como tensión narrativa.",
    },
    "MG5": {
        "ok": "Hay frase, imagen, nombre, moneda, ritual o concepto recordable.",
        "no": "Nada queda como unidad memorable; todo es genérico.",
        "sin_evidencia": "Usar si solo hay extracción textual pobre sin visuales ni hero.",
        "strong_sources": "owned_copy, hero, visual identity, named product mechanics",
        "reject": "No premiar nombres descriptivos de categoría.",
    },
    "MG6": {
        "ok": "La huella crea curiosidad para seguir: reto, recompensa, demo, ranking, mapa, prueba, historia o contraste.",
        "no": "La página se entiende y se agota sin pedir exploración.",
        "sin_evidencia": "Usar si no hay navegación/superficies suficientes.",
        "strong_sources": "owned_copy, UX flows, product pages, app mechanics",
        "reject": "No confundir cantidad de enlaces con deseo de explorar.",
    },
    "MG7": {
        "ok": "La presentación hace el producto más deseable: recompensa, acceso, rendimiento, belleza, ahorro, estatus o experiencia.",
        "no": "El producto se describe de forma funcional sin aumentar deseo.",
        "sin_evidencia": "Usar si falta visual/product packaging suficiente.",
        "strong_sources": "hero, product copy, visual evidence, mechanism copy",
        "reject": "No encender por utilidad básica sin deseo añadido.",
    },
    "MG8": {
        "ok": "Da razones para preferirla aunque existan alternativas con más features o incumbentes.",
        "no": "La preferencia no está argumentada: solo 'somos buenos/mejores'.",
        "sin_evidencia": "Si no hay alternativa/cohorte o contexto competitivo suficiente.",
        "strong_sources": "owned differentiator, external comparison, category context",
        "reject": "No inferir preferencia desde una feature aislada.",
    },
    "MG9": {
        "ok": "Hay señales de pertenencia, orgullo, rango, comunidad visible, ranking, estatus, territorio o identidad compartida.",
        "no": "La marca podría generar comunidad, pero no lo comunica ni lo muestra.",
        "sin_evidencia": "Si la prueba depende de comunidad privada, uso real o canales no capturados.",
        "strong_sources": "community pages, app mechanics, social proof, rankings, public user artifacts",
        "reject": "No aceptar 'tenemos usuarios' como pertenencia/estatus sin señal de orgullo o posición.",
    },
    "MG10": {
        "ok": "Hay atracción observable: prensa, inversión, comunidad, talento, adopción, repos, partners o terceros hablando sin empuje directo.",
        "no": "No aparece tracción ni eco externo aunque el snapshot podría mostrarlo.",
        "sin_evidencia": "Si no se capturaron fuentes externas/sociales necesarias.",
        "strong_sources": "external_proof, news, funding, repositories, social/community proof",
        "reject": "No usar claims propios de tracción si no están respaldados o contextualizados.",
    },
}

_MISSION_TILE_CONTRACTS = {
    "M1": {
        "ok": "Hay una misión explícita o una misión encarnada en un mecanismo de producto repetible.",
        "no": "Solo hay descripción de producto, catálogo, claim aspiracional o categoría.",
        "sin_evidencia": "Si no hay superficie propia suficiente para leer qué hace hoy la marca.",
        "strong_sources": "owned_copy, homepage, about, product page",
        "reject": "No exigir la palabra misión si el mecanismo literal explica qué cambio ejecuta hoy.",
    },
    "M2": {
        "ok": "La misión usa ángulo, mecanismo, audiencia o tensión que no es intercambiable.",
        "no": "Podría firmarla cualquier competidor: liderar, transformar, facilitar, innovar.",
        "sin_evidencia": "Si falta contexto mínimo de categoría/oferta.",
        "strong_sources": "owned_copy, product mechanism, category context",
        "reject": "No premiar verbos grandes sin especificidad.",
    },
    "M3": {
        "ok": "Conecta con un problema, deseo, fricción, frontera tecnológica o conflicto de categoría.",
        "no": "Solo afirma actividad o beneficio sin problema detrás.",
        "sin_evidencia": "Si el snapshot solo conserva inventario/navegación.",
        "strong_sources": "owned_copy, product narrative, external product description",
        "reject": "No reducir problema real a dolor cotidiano; puede ser de infraestructura, industria o mundo.",
    },
    "M4": {
        "ok": "La misión no contradice propósito, visión ni propuesta; explica el camino operativo de la marca.",
        "no": "La misión promete una cosa y la oferta/propósito apuntan a otra.",
        "sin_evidencia": "Si faltan los bloques necesarios para contrastar.",
        "strong_sources": "mission, value_proposition, core_purpose, vision",
        "reject": "No apagar por tono técnico si el encaje estratégico es claro.",
    },
    "M5": {
        "ok": "La misión sugiere liderazgo, redefinición o cambio de reglas en la categoría.",
        "no": "Solo ejecuta utilidad actual sin ambición de categoría.",
        "sin_evidencia": "Si no hay suficiente contexto de categoría o futuro.",
        "strong_sources": "owned_copy, manifesto, vision/context, external category evidence",
        "reject": "No encender por palabras como líder/revolucionar si no hay dirección concreta.",
    },
}

_VISION_TILE_CONTRACTS = {
    "V1": {
        "ok": "Hay destino, futuro deseado o cambio de estado identificable.",
        "no": "Solo hay misión presente, claim comercial o roadmap de producto.",
        "sin_evidencia": "Si no hay superficies estratégicas suficientes.",
        "strong_sources": "owned_copy, about, manifesto, founder/press context",
        "reject": "No aceptar crecimiento empresarial como visión por sí solo.",
    },
    "V2": {
        "ok": "El destino se formula con suficiente concreción: qué cambia, para quién o en qué mercado.",
        "no": "Usa abstracciones genéricas como transformar la industria sin aterrizar.",
        "sin_evidencia": "Si el snapshot solo conserva claim corto sin contexto.",
        "strong_sources": "owned_copy, vision/about, external founder quote",
        "reject": "No confundir ambición con concreción.",
    },
    "V3": {
        "ok": "La visión se distingue de la categoría por ángulo, tesis o futuro propio.",
        "no": "Coincide con el futuro estándar que cualquier competidor declararía.",
        "sin_evidencia": "Si no hay cohorte/contexto competitivo suficiente.",
        "strong_sources": "owned_copy plus category/competitor context",
        "reject": "No exigir comparación directa si el ángulo propio es literal y claro.",
    },
    "V4": {
        "ok": "Se entiende cómo la misión actual conduce al destino futuro.",
        "no": "El futuro aparece desconectado de lo que la marca hace hoy.",
        "sin_evidencia": "Si misión o visión no están detectadas con contenido suficiente.",
        "strong_sources": "mission, vision, value_proposition, product mechanism",
        "reject": "No apagar por falta de roadmap si hay cadena estratégica clara.",
    },
    "V5": {
        "ok": "La visión dice algo sobre hacia dónde debe ir el mercado o la categoría.",
        "no": "Solo habla de escala, expansión o éxito de la empresa.",
        "sin_evidencia": "Si no hay evidencia de categoría o tesis de mercado.",
        "strong_sources": "owned strategic copy, manifesto, external category positioning",
        "reject": "No aceptar objetivo interno de negocio como visión de categoría.",
    },
}

_VALUES_TILE_CONTRACTS = {
    "VA1": {
        "ok": "Los valores están declarados o se infieren consistentemente de tono, decisiones, producto o estándares.",
        "no": "Solo hay adjetivos sueltos, claims de calidad o tono genérico.",
        "sin_evidencia": "Si el snapshot no contiene suficiente comportamiento/copy para inferir principios.",
        "strong_sources": "owned_copy, about/culture, product decisions, public commitments",
        "reject": "No exigir una lista titulada valores si las decisiones los demuestran.",
    },
    "VA2": {
        "ok": "Los valores tienen ángulo propio o una postura reconocible.",
        "no": "Son valores comodín: transparencia, innovación, calidad, confianza sin ejecución específica.",
        "sin_evidencia": "Si no hay contexto suficiente para diferenciar el ángulo.",
        "strong_sources": "owned_copy, commitments, product tradeoffs, category context",
        "reject": "No premiar vocabulario noble sin comportamiento concreto.",
    },
    "VA3": {
        "ok": "El tono transmite los valores aunque no se lean como lista.",
        "no": "El tono contradice o no expresa los principios alegados.",
        "sin_evidencia": "Si falta suficiente copy de tono o superficies variadas.",
        "strong_sources": "homepage, about, product copy, microcopy, social copy",
        "reject": "No confundir personalidad llamativa con valores perceptibles.",
    },
    "VA4": {
        "ok": "Hay decisiones, producto, prueba o copy que ejecuta esos valores.",
        "no": "Los valores aparecen declarados pero no se ven actuados.",
        "sin_evidencia": "Si la prueba requeriría producto interno o decisiones no capturadas.",
        "strong_sources": "product evidence, public commitments, audits, policies, case proof",
        "reject": "No aceptar promesas como demostración.",
    },
    "VA5": {
        "ok": "Los valores implican una renuncia o postura que puede repeler a no-clientes/no-talento.",
        "no": "Todo es universalmente aceptable y sin coste.",
        "sin_evidencia": "Si no hay postura, tradeoff o conflicto visible.",
        "strong_sources": "manifesto, opinionated copy, product constraints, pricing/positioning choices",
        "reject": "No forzar polarización agresiva; basta una frontera clara.",
    },
}

_ATTRIBUTES_TILE_CONTRACTS = {
    "A1": {
        "ok": "Hay características tangibles de la marca/producto identificables.",
        "no": "Solo hay tono, valores o claims sin atributos concretos.",
        "sin_evidencia": "Si falta información suficiente de producto/experiencia.",
        "strong_sources": "owned_copy, product pages, feature descriptions, visual evidence",
        "reject": "No usar valores abstractos como atributos tangibles.",
    },
    "A2": {
        "ok": "Los atributos son específicos y describen propiedades reconocibles.",
        "no": "Se limitan a adjetivos comodín como innovador, simple, premium, confiable.",
        "sin_evidencia": "Si el snapshot no contiene detalle suficiente.",
        "strong_sources": "product copy, technical/product description, visual/system evidence",
        "reject": "No premiar acumulación de adjetivos.",
    },
    "A3": {
        "ok": "Los atributos se pueden comprobar en producto, experiencia, prueba o evidencia visual/textual.",
        "no": "No hay forma de verificar lo que se afirma.",
        "sin_evidencia": "Si comprobarlo requiere acceso privado al producto no capturado.",
        "strong_sources": "product pages, screenshots, docs, audits, demos, case studies",
        "reject": "No aceptar autodeclaraciones sin superficie comprobable.",
    },
    "A4": {
        "ok": "Los atributos se distinguen frente a alternativas o normas de categoría.",
        "no": "Son atributos esperables de cualquier actor de la categoría.",
        "sin_evidencia": "Si no hay cohorte, competidores o contexto externo suficiente.",
        "strong_sources": "competitor/context evidence, comparison copy, external profiles",
        "reject": "No inferir diferencial desde familiaridad previa del evaluador.",
    },
    "A5": {
        "ok": "La marca usa esos atributos como argumento de venta o parte de su narrativa.",
        "no": "Los atributos existen pero quedan como datos sueltos o especificaciones.",
        "sin_evidencia": "Si solo hay extracción técnica sin copy comercial.",
        "strong_sources": "homepage, product pages, sales copy, external positioning",
        "reject": "No encender por listar una feature si no argumenta valor.",
    },
}

_PERSONALITY_TILE_CONTRACTS = {
    "PE1": {
        "ok": "Hay tono reconocible en vocabulario, ritmo, postura o forma de hablar.",
        "no": "El copy suena a plantilla neutra o descripción corporativa sin voz.",
        "sin_evidencia": "Si solo hay datos técnicos/inventario y casi no hay copy expresivo.",
        "strong_sources": "owned_copy, microcopy, social copy, product copy",
        "reject": "No confundir tono profesional con ausencia de personalidad.",
    },
    "PE2": {
        "ok": "Evita lugares comunes de su categoría o los usa con giro propio.",
        "no": "Repite tics previsibles de categoría sin ángulo.",
        "sin_evidencia": "Si no hay suficiente cohorte o texto de categoría para compararlo.",
        "strong_sources": "owned_copy, category context, competitor/context evidence",
        "reject": "No penalizar lenguaje técnico si es preciso y consistente.",
    },
    "PE3": {
        "ok": "Se puede nombrar un arquetipo/postura: rebelde, sabio, creador, explorador, técnico implacable, etc.",
        "no": "No emerge patrón de comportamiento o tono estable.",
        "sin_evidencia": "Si el snapshot es demasiado escaso para inferir arquetipo.",
        "strong_sources": "owned_copy, visual language, product behavior, social copy",
        "reject": "No forzar un arquetipo clásico si la evidencia solo muestra categoría.",
    },
    "PE4": {
        "ok": "La voz se mantiene entre superficies propias capturadas.",
        "no": "La voz cambia de una superficie a otra de forma incoherente.",
        "sin_evidencia": "Si solo hay una superficie o una captura parcial.",
        "strong_sources": "multiple owned pages, homepage, product/pricing/about",
        "reject": "No exigir uniformidad exacta; evaluar continuidad de carácter.",
    },
    "PE5": {
        "ok": "Botones, formularios, errores o detalles de interfaz conservan la misma voz.",
        "no": "El microcopy existe pero cae en genérico/desconectado.",
        "sin_evidencia": "Si el snapshot no captura microcopy o estados de producto.",
        "strong_sources": "product screenshots, forms, CTA copy, UI states",
        "reject": "No inferir microcopy desde párrafos de marketing.",
    },
    "PE6": {
        "ok": "La voz sobrevive en redes, producto, comunidad u otras superficies externas.",
        "no": "Hay superficies externas capturadas y la voz se diluye o contradice.",
        "sin_evidencia": "Si no hay redes/producto/canales externos capturados.",
        "strong_sources": "social profiles, product surfaces, community pages, external owned channels",
        "reject": "No usar menciones de terceros como voz de la marca.",
    },
    "PE7": {
        "ok": "Aparecen giros, ritmo, humor, dureza, metáforas o vocabulario propios.",
        "no": "No hay rasgos lingüísticos distinguibles.",
        "sin_evidencia": "Si la captura textual es insuficiente para evaluar estilo.",
        "strong_sources": "owned_copy, campaign copy, repeated phrases, microcopy",
        "reject": "No contar nombres de producto genéricos como rasgo propio.",
    },
    "PE8": {
        "ok": "La personalidad ejecuta valores, idea de marca o propuesta sin contradicción.",
        "no": "La voz contradice lo que la marca dice ser o vender.",
        "sin_evidencia": "Si faltan valores/idea/propuesta detectados para contrastar.",
        "strong_sources": "personality plus values/brand_idea/value_proposition",
        "reject": "No exigir calidez; la coherencia puede ser fría, dura o técnica.",
    },
    "PE9": {
        "ok": "Sin logo, el tono o sistema verbal seguiría siendo reconocible.",
        "no": "Al tapar la marca, el copy podría ser de cualquier competidor.",
        "sin_evidencia": "Si falta suficiente volumen de copy propio.",
        "strong_sources": "owned_copy, repeated vocabulary, campaign/system language",
        "reject": "No encender por una frase aislada si no hay sistema reconocible.",
    },
    "PE10": {
        "ok": "Genera frase, claim, idea o recurso verbal que alguien podría repetir o citar.",
        "no": "El contenido es correcto pero no recordable/citable.",
        "sin_evidencia": "Si solo hay contenido funcional o técnico sin copy final.",
        "strong_sources": "hero, campaign copy, manifesto, named concepts",
        "reject": "No aceptar claims largos y genéricos como citables.",
    },
}

_BRAND_IDEA_TILE_CONTRACTS = {
    "I1": {
        "ok": "Existe identidad visual o verbal mínimamente diseñada, no plantilla sin alterar.",
        "no": "La marca parece tema genérico, catálogo o UI sin identidad propia.",
        "sin_evidencia": "Si no hay captura visual ni suficiente copy de identidad.",
        "strong_sources": "visual evidence, logo, color/type system, owned copy",
        "reject": "No encender por tener logo si todo lo demás es plantilla.",
    },
    "I2": {
        "ok": "Logo, color, tipografía, tono o componentes trabajan como sistema.",
        "no": "Los elementos existen pero no parecen coordinados.",
        "sin_evidencia": "Si faltan suficientes superficies visuales.",
        "strong_sources": "visual evidence, multiple pages, design system signals",
        "reject": "No confundir consistencia por defecto de plantilla con sistema de marca.",
    },
    "I3": {
        "ok": "La identidad se distingue de códigos estándar de su categoría.",
        "no": "Reproduce estética genérica del sector.",
        "sin_evidencia": "Si falta contexto visual de categoría/cohorte.",
        "strong_sources": "visual evidence plus category context",
        "reject": "No premiar rareza visual si no ayuda a distinguir la marca.",
    },
    "I4": {
        "ok": "Hay concepto detectable: metáfora, idea, tensión, sistema o vocabulario organizador.",
        "no": "Solo hay estilo o producto sin idea conectiva.",
        "sin_evidencia": "Si la evidencia no contiene narrativa ni visual suficiente.",
        "strong_sources": "owned_copy, brand idea block, visual metaphor, product mechanism",
        "reject": "No aceptar mood estético como concepto por sí solo.",
    },
    "I5": {
        "ok": "El concepto se ejecuta en la web principal, producto o sistema visual/verbal.",
        "no": "El concepto se declara pero no aparece ejecutado.",
        "sin_evidencia": "Si solo hay declaración sin superficies de ejecución.",
        "strong_sources": "homepage, product pages, visual system, repeated copy",
        "reject": "No encender por claim conceptual aislado.",
    },
    "I6": {
        "ok": "Hay decisiones visuales intencionadas que expresan una dirección.",
        "no": "Los visuales son decorativos, stock o puramente funcionales.",
        "sin_evidencia": "Si falta captura visual usable.",
        "strong_sources": "visual evidence, screenshots, media assets",
        "reject": "No premiar imágenes bonitas si no construyen dirección.",
    },
    "I7": {
        "ok": "El visual traduce propósito, personalidad o propuesta.",
        "no": "El visual cuenta otra historia o no cuenta ninguna.",
        "sin_evidencia": "Si faltan visuales o los bloques estratégicos necesarios.",
        "strong_sources": "visual evidence plus purpose/personality/value_proposition",
        "reject": "No inferir estrategia visual desde colores aislados.",
    },
    "I8": {
        "ok": "El sistema se sostiene en varias superficies capturadas.",
        "no": "Cada superficie parece de una marca distinta o pierde consistencia.",
        "sin_evidencia": "Si solo hay una superficie.",
        "strong_sources": "multiple pages, social/product visuals, asset set",
        "reject": "No confundir repetición mecánica de plantilla con consistencia de marca.",
    },
    "I9": {
        "ok": "Aparece un universo propio: códigos, mundo visual/verbal, rituales o símbolos reconocibles.",
        "no": "Hay identidad básica pero no mundo propio.",
        "sin_evidencia": "Si falta amplitud visual/verbal para evaluar universo.",
        "strong_sources": "visual system, named mechanics, campaign/product language",
        "reject": "No encender por una sola ilustración o imagen decorativa.",
    },
    "I10": {
        "ok": "La identidad aumenta valor percibido, confianza o deseo por encima de las features.",
        "no": "El envoltorio no eleva o incluso devalúa la oferta.",
        "sin_evidencia": "Si no hay suficiente evidencia visual/product packaging.",
        "strong_sources": "visual evidence, product presentation, premium proof, conversion surfaces",
        "reject": "No confundir precio alto o claims premium con valor percibido.",
    },
}

_CORE_PURPOSE_TILE_CONTRACTS = {
    "PR1": {
        "ok": "Aparece un porqué, explícito o inferible desde una decisión/mecanismo central.",
        "no": "Solo se explica qué vende o cómo funciona.",
        "sin_evidencia": "Si faltan superficies estratégicas suficientes.",
        "strong_sources": "owned_copy, about, manifesto, product mechanism",
        "reject": "No aceptar misión funcional como propósito si no explica por qué existe.",
    },
    "PR2": {
        "ok": "El porqué está escrito o formulado de forma directa.",
        "no": "Solo puede intuirse entre líneas.",
        "sin_evidencia": "Si no hay copy estratégico suficiente.",
        "strong_sources": "about, manifesto, homepage strategic copy",
        "reject": "No encender con inferencia de producto aunque PR1 sí pueda encender.",
    },
    "PR3": {
        "ok": "Responde por qué existe la marca más allá de vender/operar el producto.",
        "no": "El supuesto propósito se queda en utilidad, feature o categoría.",
        "sin_evidencia": "Si falta suficiente narrativa de marca.",
        "strong_sources": "purpose/about copy, founder narrative, product philosophy",
        "reject": "No premiar 'ayudamos a X a hacer Y' si solo describe propuesta.",
    },
    "PR4": {
        "ok": "El porqué tiene ángulo propio y evita lugares comunes universales.",
        "no": "Usa fórmulas tipo hacer el mundo mejor, empoderar, transformar sin especificidad.",
        "sin_evidencia": "Si no hay contexto suficiente para juzgar propiedad.",
        "strong_sources": "owned_copy plus category context",
        "reject": "No encender por solemnidad o lenguaje grandilocuente.",
    },
    "PR5": {
        "ok": "El propósito se conecta con categoría, producto y decisiones reales de la oferta.",
        "no": "El propósito podría pertenecer a otra categoría.",
        "sin_evidencia": "Si falta información de producto/categoría.",
        "strong_sources": "purpose, product pages, value proposition, category context",
        "reject": "No aceptar propósito abstracto desconectado del negocio.",
    },
    "PR6": {
        "ok": "El porqué explica o justifica lo que venden.",
        "no": "Propósito y propuesta conviven pero no se sostienen mutuamente.",
        "sin_evidencia": "Si falta propuesta de valor detectada.",
        "strong_sources": "core_purpose plus value_proposition",
        "reject": "No encender si solo hay coincidencia temática superficial.",
    },
    "PR7": {
        "ok": "Propósito, misión y visión forman una cadena causal legible.",
        "no": "Las piezas están presentes pero no encajan.",
        "sin_evidencia": "Si misión o visión faltan.",
        "strong_sources": "core_purpose, mission, vision",
        "reject": "No exigir wording idéntico; evaluar continuidad estratégica.",
    },
    "PR8": {
        "ok": "El tono, visual o producto transmiten el porqué sin depender del about.",
        "no": "El porqué solo vive en un párrafo aislado.",
        "sin_evidencia": "Si faltan visuales/superficies expresivas.",
        "strong_sources": "visual evidence, homepage, product experience, tone",
        "reject": "No encender por repetir el propósito textual en varias páginas.",
    },
    "PR9": {
        "ok": "Una decisión pública de producto, negocio, comunidad o prueba ejecuta el propósito.",
        "no": "No hay decisión verificable que lo actúe.",
        "sin_evidencia": "Si el snapshot no incluye decisiones públicas o producto suficiente.",
        "strong_sources": "product decisions, policies, partnerships, audits, public proof",
        "reject": "No aceptar una promesa como decisión ejecutada.",
    },
    "PR10": {
        "ok": "Terceros o evidencias externas validan que el porqué se traduce en reputación.",
        "no": "Hay propósito pero no prueba reputacional disponible pese a fuentes externas.",
        "sin_evidencia": "Si no se capturaron suficientes terceros o histórico.",
        "strong_sources": "external_proof, news, reviews, partnerships, community proof",
        "reject": "No usar autopromoción como reputación.",
    },
}

_COHERENCIA_TILE_CONTRACTS = {
    "C1": {
        "ok": "No hay contradicción grave entre promesa, producto, tono y experiencia capturada.",
        "no": "Una promesa central queda negada por producto, copy, visual o evidencia externa.",
        "sin_evidencia": "Raro; usar si faltan demasiadas piezas para evaluar contradicción.",
        "strong_sources": "all components, product evidence, visual/copy consistency",
        "reject": "No marcar contradicción por simple incompletitud.",
    },
    "C2": {
        "ok": "Los mensajes principales conviven sin pisarse.",
        "no": "Hay mensajes parciales que compiten o diluyen la lectura.",
        "sin_evidencia": "Si hay muy pocas superficies/mensajes.",
        "strong_sources": "owned pages, tldr blocks, cross-surface copy",
        "reject": "No exigir una sola frase; puede haber arquitectura con varias capas.",
    },
    "C3": {
        "ok": "El propósito explica la misión o la misión ejecuta el propósito.",
        "no": "Propósito y misión apuntan a lógicas distintas.",
        "sin_evidencia": "Si falta propósito o misión.",
        "strong_sources": "core_purpose, mission",
        "reject": "No penalizar ausencia de propósito como contradicción; marcar sin_evidencia si falta.",
    },
    "C4": {
        "ok": "La misión y la propuesta de valor se refuerzan.",
        "no": "Lo que persiguen y lo que venden no encaja.",
        "sin_evidencia": "Si falta misión o propuesta.",
        "strong_sources": "mission, value_proposition",
        "reject": "No exigir que misión y propuesta usen el mismo lenguaje.",
    },
    "C5": {
        "ok": "La personalidad ejecuta valores declarados o inferidos.",
        "no": "El tono contradice los valores o los deja sin vida.",
        "sin_evidencia": "Si faltan valores o personalidad.",
        "strong_sources": "values, personality, tone evidence",
        "reject": "No convertir preferencia estética en incoherencia.",
    },
    "C6": {
        "ok": "Diseño y copy cuentan la misma historia estratégica.",
        "no": "Visual y texto proyectan marcas distintas.",
        "sin_evidencia": "Si falta captura visual suficiente.",
        "strong_sources": "visual evidence, brand_idea, copy blocks",
        "reject": "No exigir literalidad; el diseño puede apoyar por atmósfera/sistema.",
    },
    "C7": {
        "ok": "El discurso se mantiene al cambiar de canal.",
        "no": "Web, redes, producto o prensa comunican identidades incompatibles.",
        "sin_evidencia": "Si no hay canales externos/sociales/producto capturados.",
        "strong_sources": "social/owned channels, external profiles, product surfaces",
        "reject": "No usar prensa de terceros como voz propia salvo para contraste externo.",
    },
    "C8": {
        "ok": "Hay prueba de que la experiencia real cumple la promesa: reviews, demos, casos, uso o producto observable.",
        "no": "La experiencia observable contradice lo prometido.",
        "sin_evidencia": "Por defecto si el scanner no puede probar producto/uso real.",
        "strong_sources": "product demo, reviews, case studies, screenshots, external user proof",
        "reject": "No encender por promesa de producto sin evidencia de experiencia.",
    },
    "C9": {
        "ok": "Las piezas se amplifican: propósito, oferta, personalidad, visual y magnetismo se apoyan.",
        "no": "Las piezas conviven pero no se refuerzan.",
        "sin_evidencia": "Si demasiados componentes clave faltan.",
        "strong_sources": "all component texts, tile profiles, visual/copy evidence",
        "reject": "No confundir ausencia de ruido con refuerzo mutuo.",
    },
    "C10": {
        "ok": "Producto y marca son difíciles de separar; el mecanismo/producto expresa la idea de marca.",
        "no": "La marca parece capa externa intercambiable sobre el producto.",
        "sin_evidencia": "Si no hay suficiente producto/experiencia visual o textual.",
        "strong_sources": "product mechanism, brand_idea, value_proposition, visual/product evidence",
        "reject": "No encender por naming o estética si el producto no lleva la marca dentro.",
    },
}

COMPONENTS = {

    "mission": {
        "label": "Misión",
        "tldr_key": "mission",
        "scale": 5,
        "multiplier": 1,
        "pair": "mission_vision",
        "question": "¿Qué hace la marca concretamente hoy y para qué?",
        "level_zero": "No detectada en ninguna superficie pública.",
        "tiles": [
            {"id": "M1", "name": "Detectada", "condition": "Hay una misión identificable en superficie pública propia.", "evidence_contract": _MISSION_TILE_CONTRACTS["M1"]},
            {"id": "M2", "name": "Propia", "condition": "No es intercambiable con cualquier marca de su categoría (“ser líderes” no enciende).", "evidence_contract": _MISSION_TILE_CONTRACTS["M2"]},
            {"id": "M3", "name": "Anclada", "condition": "Conecta con un problema real: del usuario, de la categoría, del mundo o una frontera tecnológica.", "evidence_contract": _MISSION_TILE_CONTRACTS["M3"]},
            {"id": "M4", "name": "Coherente", "condition": "No contradice propósito ni propuesta; encaja en el discurso.", "evidence_contract": _MISSION_TILE_CONTRACTS["M4"]},
            {"id": "M5", "name": "Ambiciosa", "condition": "Marca un camino que lidera o redefine su categoría.", "evidence_contract": _MISSION_TILE_CONTRACTS["M5"]},
        ],
    },

    "vision": {
        "label": "Visión",
        "tldr_key": "vision",
        "scale": 5,
        "multiplier": 1,
        "pair": "mission_vision",
        "question": "¿Qué futuro o cambio de categoría intenta construir la marca?",
        "level_zero": "No detectada.",
        "tiles": [
            {"id": "V1", "name": "Detectada", "condition": "Hay un destino identificable.", "evidence_contract": _VISION_TILE_CONTRACTS["V1"]},
            {"id": "V2", "name": "Concreta", "condition": "El destino se nombra; “transformar la industria” no enciende.", "evidence_contract": _VISION_TILE_CONTRACTS["V2"]},
            {"id": "V3", "name": "Propia", "condition": "Distinguible de la visión de su competencia.", "evidence_contract": _VISION_TILE_CONTRACTS["V3"]},
            {"id": "V4", "name": "Conectada", "condition": "Se entiende el camino entre la misión de hoy y el destino.", "context_needs": ["mission"], "evidence_contract": _VISION_TILE_CONTRACTS["V4"]},
            {"id": "V5", "name": "De categoría", "condition": "Define hacia dónde va el mercado, no solo la empresa.", "evidence_contract": _VISION_TILE_CONTRACTS["V5"]},
        ],
    },

    "values": {
        "label": "Valores",
        "tldr_key": "values",
        "scale": 5,
        "multiplier": 1,
        "pair": "values_attributes",
        "question": "¿Qué valores defiende la marca a través de lo que dice o hace?",
        "level_zero": "No detectados.",
        "tiles": [
            {"id": "VA1", "name": "Detectados", "condition": "Declarados o inferibles en tono y decisiones del snapshot.", "evidence_contract": _VALUES_TILE_CONTRACTS["VA1"]},
            {"id": "VA2", "name": "Propios", "condition": "Con ángulo; “transparencia, innovación, calidad” no enciende.", "evidence_contract": _VALUES_TILE_CONTRACTS["VA2"]},
            {"id": "VA3", "name": "Perceptibles", "condition": "El tono los transmite sin leer ninguna lista.", "evidence_contract": _VALUES_TILE_CONTRACTS["VA3"]},
            {"id": "VA4", "name": "Demostrados", "condition": "Copy, producto o decisiones públicas del snapshot los ejecutan.", "evidence_contract": _VALUES_TILE_CONTRACTS["VA4"]},
            {"id": "VA5", "name": "Polarizantes", "condition": "Repelen activamente a quien no es su cliente o talento ideal.", "evidence_contract": _VALUES_TILE_CONTRACTS["VA5"]},
        ],
    },

    "attributes": {
        "label": "Atributos",
        "tldr_key": "attributes",
        "scale": 5,
        "multiplier": 1,
        "pair": "values_attributes",
        "question": "¿Qué atributos demuestra la marca de forma consistente?",
        "level_zero": "No detectados.",
        "tiles": [
            {"id": "A1", "name": "Detectados", "condition": "Características tangibles identificables.", "evidence_contract": _ATTRIBUTES_TILE_CONTRACTS["A1"]},
            {"id": "A2", "name": "Específicos", "condition": "Sin adjetivos comodín.", "evidence_contract": _ATTRIBUTES_TILE_CONTRACTS["A2"]},
            {"id": "A3", "name": "Verificables", "condition": "Comprobables en producto o experiencia según el snapshot.", "evidence_contract": _ATTRIBUTES_TILE_CONTRACTS["A3"]},
            {"id": "A4", "name": "Diferenciales", "condition": "Distintos frente a alternativas. Requiere evidencia de cohorte en el snapshot; si no la hay, sin_evidencia.", "blind_spot": True, "evidence_contract": _ATTRIBUTES_TILE_CONTRACTS["A4"]},
            {"id": "A5", "name": "Integrados", "condition": "La marca los usa como argumento de venta, no solo los lista.", "evidence_contract": _ATTRIBUTES_TILE_CONTRACTS["A5"]},
        ],
    },

    "value_proposition": {
        "label": "Propuesta de valor",
        "tldr_key": "value_proposition",
        "scale": 10,
        "multiplier": 1,
        "pair": None,
        "question": "¿Qué ofrece la marca, a quién, y qué cambia para esa audiencia?",
        "level_zero": "Ausente: la hero no dice qué venden.",
        "tiles": [
            {"id": "P1", "name": "Detectada", "condition": "La hero dice qué venden.", "evidence_contract": _VALUE_PROPOSITION_TILE_CONTRACTS["P1"]},
            {"id": "P2", "name": "Clara", "condition": "Se entiende sin conocer la categoría.", "evidence_contract": _VALUE_PROPOSITION_TILE_CONTRACTS["P2"]},
            {"id": "P3", "name": "En beneficios", "condition": "Habla de resultado, no solo de features.", "evidence_contract": _VALUE_PROPOSITION_TILE_CONTRACTS["P3"]},
            {"id": "P4", "name": "Público inequívoco", "condition": "Nombrado, o evidente por producto y contexto.", "evidence_contract": _VALUE_PROPOSITION_TILE_CONTRACTS["P4"]},
            {"id": "P5", "name": "Tensión nombrada", "condition": "Se sabe qué dolor, deseo, necesidad o frontera tecnológica resuelve o rompe.", "evidence_contract": _VALUE_PROPOSITION_TILE_CONTRACTS["P5"]},
            {"id": "P6", "name": "Propia", "condition": "La frase no vale para su competencia.", "evidence_contract": _VALUE_PROPOSITION_TILE_CONTRACTS["P6"]},
            {"id": "P7", "name": "Diferencial explícito", "condition": "Dice por qué ella y no las alternativas. Requiere cohorte; si no, sin_evidencia.", "blind_spot": True, "evidence_contract": _VALUE_PROPOSITION_TILE_CONTRACTS["P7"]},
            {"id": "P8", "name": "Mecanismo propio", "condition": "El cómo es identificable y difícil de copiar.", "evidence_contract": _VALUE_PROPOSITION_TILE_CONTRACTS["P8"]},
            {"id": "P9", "name": "Prueba a la vista", "condition": "Proof signals en la primera pantalla.", "evidence_contract": _VALUE_PROPOSITION_TILE_CONTRACTS["P9"]},
            {"id": "P10", "name": "Promesa verificable", "condition": "Datos, casos o garantías irrefutables en el snapshot.", "evidence_contract": _VALUE_PROPOSITION_TILE_CONTRACTS["P10"]},
        ],
    },

    "personality": {
        "label": "Personalidad / Arquetipo",
        "tldr_key": "personality",
        "scale": 10,
        "multiplier": 1,
        "pair": None,
        "question": "¿Qué personalidad ejecuta la marca a través de tono, vocabulario, comportamiento y postura visual?",
        "level_zero": "Robótica: plantilla B2B estándar.",
        "tiles": [
            {"id": "PE1", "name": "Voz detectable", "condition": "Hay un tono, no una plantilla.", "evidence_contract": _PERSONALITY_TILE_CONTRACTS["PE1"]},
            {"id": "PE2", "name": "Sin clichés", "condition": "No cae en los tics de su categoría.", "evidence_contract": _PERSONALITY_TILE_CONTRACTS["PE2"]},
            {"id": "PE3", "name": "Arquetipo identificable", "condition": "Se puede nombrar: rebelde, sabio, creador, maverick…", "evidence_contract": _PERSONALITY_TILE_CONTRACTS["PE3"]},
            {"id": "PE4", "name": "Consistente entre páginas", "condition": "El tono no cambia de la home al pricing.", "evidence_contract": _PERSONALITY_TILE_CONTRACTS["PE4"]},
            {"id": "PE5", "name": "Consistente en microcopy", "condition": "Botones, errores y detalles hablan igual.", "evidence_contract": _PERSONALITY_TILE_CONTRACTS["PE5"]},
            {"id": "PE6", "name": "Consistente en redes y producto", "condition": "La voz sobrevive fuera de la web.", "evidence_contract": _PERSONALITY_TILE_CONTRACTS["PE6"]},
            {"id": "PE7", "name": "Rasgos propios", "condition": "Giros, ritmo, humor o dureza reconocibles.", "evidence_contract": _PERSONALITY_TILE_CONTRACTS["PE7"]},
            {"id": "PE8", "name": "Coherente con valores e idea", "condition": "La personalidad ejecuta lo que la marca dice ser. Temperatura libre.", "evidence_contract": _PERSONALITY_TILE_CONTRACTS["PE8"]},
            {"id": "PE9", "name": "Test del logo tapado", "condition": "Se reconoce quién habla sin ver la marca.", "evidence_contract": _PERSONALITY_TILE_CONTRACTS["PE9"]},
            {"id": "PE10", "name": "Citable", "condition": "Genera contenido que otros recuerdan o imitan.", "evidence_contract": _PERSONALITY_TILE_CONTRACTS["PE10"]},
        ],
    },

    "brand_idea": {
        "label": "Idea de marca",
        "tldr_key": "brand_idea",
        "scale": 10,
        "multiplier": 1,
        "pair": None,
        # The visual tiles (I1-I3, I6-I9) are judgeable from visual evidence
        # alone even when no conceptual brand idea was detected in text, so the
        # component evaluates on signals when detection is empty.
        "evaluate_on_signals": True,
        "question": "¿Qué idea conceptual conecta categoría, oferta, expresión y metáfora?",
        "level_zero": "Plantilla sin alterar: identidad visual nula.",
        "tiles": [
            {"id": "I1", "name": "Identidad existente", "condition": "No es una plantilla sin alterar.", "evidence_contract": _BRAND_IDEA_TILE_CONTRACTS["I1"]},
            {"id": "I2", "name": "Sistema", "condition": "Logo, color y tipografía funcionan como conjunto.", "evidence_contract": _BRAND_IDEA_TILE_CONTRACTS["I2"]},
            {"id": "I3", "name": "No genérica", "condition": "Se distingue de la estética estándar de su categoría.", "evidence_contract": _BRAND_IDEA_TILE_CONTRACTS["I3"]},
            {"id": "I4", "name": "Concepto detectable", "condition": "Hay una idea detrás, declarada o evidente.", "evidence_contract": _BRAND_IDEA_TILE_CONTRACTS["I4"]},
            {"id": "I5", "name": "Concepto ejecutado", "condition": "La idea se ve en la web principal, no solo se declara.", "evidence_contract": _BRAND_IDEA_TILE_CONTRACTS["I5"]},
            {"id": "I6", "name": "Dirección de arte", "condition": "Decisiones visuales intencionadas, no decorativas.", "evidence_contract": _BRAND_IDEA_TILE_CONTRACTS["I6"]},
            {"id": "I7", "name": "Traduce la estrategia", "condition": "El visual expresa propósito y personalidad.", "evidence_contract": _BRAND_IDEA_TILE_CONTRACTS["I7"]},
            {"id": "I8", "name": "Consistente", "condition": "El sistema se sostiene en todas las superficies del snapshot.", "evidence_contract": _BRAND_IDEA_TILE_CONTRACTS["I8"]},
            {"id": "I9", "name": "Universo propio", "condition": "Estética reconocible como suya.", "evidence_contract": _BRAND_IDEA_TILE_CONTRACTS["I9"]},
            {"id": "I10", "name": "Eleva el precio percibido", "condition": "El envoltorio hace al producto parecer mejor de lo que sus features justifican.", "evidence_contract": _BRAND_IDEA_TILE_CONTRACTS["I10"]},
        ],
    },

    "core_purpose": {
        "label": "Propósito",
        "tldr_key": "core_purpose",
        "scale": 10,
        "multiplier": 1,
        "pair": None,
        "question": "¿Por qué existe la marca más allá del producto?",
        "level_zero": "Ningún rastro del porqué en ninguna superficie pública.",
        "tiles": [
            {"id": "PR1", "name": "Detectado", "condition": "Hay un porqué en alguna superficie.", "evidence_contract": _CORE_PURPOSE_TILE_CONTRACTS["PR1"]},
            {"id": "PR2", "name": "Explícito", "condition": "Escrito, no solo intuible.", "evidence_contract": _CORE_PURPOSE_TILE_CONTRACTS["PR2"]},
            {"id": "PR3", "name": "Más allá del qué", "condition": "Responde por qué existen, no qué hacen.", "evidence_contract": _CORE_PURPOSE_TILE_CONTRACTS["PR3"]},
            {"id": "PR4", "name": "Propio", "condition": "“Hacer el mundo mejor” no enciende.", "evidence_contract": _CORE_PURPOSE_TILE_CONTRACTS["PR4"]},
            {"id": "PR5", "name": "Anclado", "condition": "Conecta con su categoría y su producto.", "evidence_contract": _CORE_PURPOSE_TILE_CONTRACTS["PR5"]},
            {"id": "PR6", "name": "Conectado a la propuesta", "condition": "El porqué justifica el qué venden.", "context_needs": ["value_proposition"], "evidence_contract": _CORE_PURPOSE_TILE_CONTRACTS["PR6"]},
            {"id": "PR7", "name": "Cadena completa", "condition": "Propósito, misión y visión se sostienen juntos.", "context_needs": ["mission", "vision"], "evidence_contract": _CORE_PURPOSE_TILE_CONTRACTS["PR7"]},
            {"id": "PR8", "name": "Respirado", "condition": "Tono y diseño lo transmiten sin leer el “about”.", "evidence_contract": _CORE_PURPOSE_TILE_CONTRACTS["PR8"]},
            {"id": "PR9", "name": "Demostrado", "condition": "Al menos una decisión de negocio pública del snapshot lo ejecuta.", "evidence_contract": _CORE_PURPOSE_TILE_CONTRACTS["PR9"]},
            {"id": "PR10", "name": "Reputación", "condition": "Varias decisiones verificables por terceros; el porqué es su prueba social.", "evidence_contract": _CORE_PURPOSE_TILE_CONTRACTS["PR10"]},
        ],
    },

    "magnetism": {
        "label": "Magnetism",
        "tldr_key": "magnetism",
        "scale": 10,
        "multiplier": 2,
        "pair": None,
        "question": "¿Qué frase, tensión o promesa retiene, y por qué mecanismo: dolor, deseo, asombro, pertenencia o estatus?",
        "level_zero": "Invisible: nada retiene la atención.",
        "tiles": [
            {"id": "MG1", "name": "Retiene", "condition": "Algo detiene el scroll, verbal o visual.", "evidence_contract": _MAGNETISM_TILE_CONTRACTS["MG1"]},
            {"id": "MG2", "name": "Mecanismo identificable", "condition": "Se sabe cuál opera: dolor, deseo, asombro, pertenencia o estatus.", "evidence_contract": _MAGNETISM_TILE_CONTRACTS["MG2"]},
            {"id": "MG3", "name": "Hook", "condition": "Gancho claro anclado a una tensión real de su audiencia.", "evidence_contract": _MAGNETISM_TILE_CONTRACTS["MG3"]},
            {"id": "MG4", "name": "Tensión narrativa", "condition": "Hay historia, no solo descripción.", "evidence_contract": _MAGNETISM_TILE_CONTRACTS["MG4"]},
            {"id": "MG5", "name": "Memorable", "condition": "Una frase o imagen que se queda.", "evidence_contract": _MAGNETISM_TILE_CONTRACTS["MG5"]},
            {"id": "MG6", "name": "Invita a explorar", "condition": "La huella pide seguir navegando.", "evidence_contract": _MAGNETISM_TILE_CONTRACTS["MG6"]},
            {"id": "MG7", "name": "Genera deseo", "condition": "El packaging hace al producto parecer superior.", "evidence_contract": _MAGNETISM_TILE_CONTRACTS["MG7"]},
            {"id": "MG8", "name": "Genera preferencia", "condition": "Da razones para elegirla frente a alternativas con más features.", "evidence_contract": _MAGNETISM_TILE_CONTRACTS["MG8"]},
            {"id": "MG9", "name": "Pertenencia o estatus", "condition": "Señales de orgullo: comunidad que presume o posición que se exhibe.", "blind_spot": True, "evidence_contract": _MAGNETISM_TILE_CONTRACTS["MG9"]},
            {"id": "MG10", "name": "Gravedad propia", "condition": "Atrae talento, prensa o comunidad sin empujar, según evidencia del snapshot.", "blind_spot": True, "evidence_contract": _MAGNETISM_TILE_CONTRACTS["MG10"]},
        ],
    },

    "coherencia": {
        "label": "Coherencia",
        "tldr_key": None,  # No detection of its own: reads the whole.
        "scale": 10,
        "multiplier": 2,
        "pair": None,
        "question": "¿Los componentes cuentan la misma historia entre sí y en todos los espacios digitales?",
        "level_zero": "Contradicciones graves: prometen simplicidad y el producto es complejo.",
        "tiles": [
            {"id": "C1", "name": "Sin contradicciones graves", "condition": "No prometen simplicidad con un producto laberíntico.", "evidence_contract": _COHERENCIA_TILE_CONTRACTS["C1"]},
            {"id": "C2", "name": "Sin contradicciones parciales", "condition": "Los mensajes clave no se pisan entre sí.", "evidence_contract": _COHERENCIA_TILE_CONTRACTS["C2"]},
            {"id": "C3", "name": "Propósito-misión", "condition": "El porqué y el camino encajan.", "evidence_contract": _COHERENCIA_TILE_CONTRACTS["C3"]},
            {"id": "C4", "name": "Misión-propuesta", "condition": "Lo que persiguen y lo que venden encajan.", "evidence_contract": _COHERENCIA_TILE_CONTRACTS["C4"]},
            {"id": "C5", "name": "Valores en el tono", "condition": "La personalidad ejecuta los valores declarados o inferidos.", "evidence_contract": _COHERENCIA_TILE_CONTRACTS["C5"]},
            {"id": "C6", "name": "Diseño-copy", "condition": "El visual cuenta la misma historia que el texto.", "evidence_contract": _COHERENCIA_TILE_CONTRACTS["C6"]},
            {"id": "C7", "name": "Web-redes", "condition": "El discurso sobrevive al cambio de canal.", "evidence_contract": _COHERENCIA_TILE_CONTRACTS["C7"]},
            {"id": "C8", "name": "Marca-producto", "condition": "La experiencia real cumple lo que la marca proyecta.", "note": _C8_NOTE, "blind_spot": True, "evidence_contract": _COHERENCIA_TILE_CONTRACTS["C8"]},
            {"id": "C9", "name": "Refuerzo mutuo", "condition": "Las piezas se apoyan entre sí, no solo conviven.", "evidence_contract": _COHERENCIA_TILE_CONTRACTS["C9"]},
            {"id": "C10", "name": "Inseparable", "condition": "Imposible distinguir dónde acaba el producto y empieza la marca.", "evidence_contract": _COHERENCIA_TILE_CONTRACTS["C10"]},
        ],
    },
}


def component_max_points(key: str) -> int:
    """Points this component contributes to the Brand3 Score at its ceiling."""
    spec = COMPONENTS[key]
    return spec["scale"] * spec["multiplier"]


def component_points(key: str, score: int) -> int:
    """Points contributed by a component given its 0-scale score."""
    spec = COMPONENTS[key]
    return score * spec["multiplier"]


def tile_ids(key: str) -> list[str]:
    """The tile IDs for a component, in presentation order."""
    return [tile["id"] for tile in COMPONENTS[key]["tiles"]]


def tile_index() -> dict[str, dict[str, str]]:
    """Flat {tile_id: {component, name, condition}} for validation and reports."""
    index: dict[str, dict[str, str]] = {}
    for key, spec in COMPONENTS.items():
        for tile in spec["tiles"]:
            index[tile["id"]] = {
                "component": key,
                "name": tile["name"],
                "condition": tile["condition"],
            }
    return index


def confidence_from_blind_spots(blind_spot_count: int) -> str:
    """Component confidence from its count of `sin_evidencia` tiles."""
    if blind_spot_count >= CONFIDENCE_BAJA_BLIND_SPOTS:
        return CONFIDENCE_BAJA
    if blind_spot_count >= CONFIDENCE_MEDIA_BLIND_SPOTS:
        return CONFIDENCE_MEDIA
    return CONFIDENCE_ALTA


# --- Integrity checks (same spirit as legacy dimensions.py) ---

assert set(PRESENTATION_ORDER) == set(COMPONENTS), "Presentation order must cover every component exactly once"
assert len(PRESENTATION_ORDER) == 10, "SV9 is 9 scored components plus Coherencia"
assert set(BASE_COMPONENTS) == set(COMPONENTS) - {"magnetism", "coherencia"}, \
    "Base components are everything except Magnetism and Coherencia"

_total = sum(component_max_points(key) for key in COMPONENTS)
assert _total == 100, f"Brand3 Score must total 100, got {_total}"

_all_tiles = 0
_seen_ids: set[str] = set()
for _key, _spec in COMPONENTS.items():
    assert len(_spec["tiles"]) == _spec["scale"], \
        f"Component '{_key}' must have exactly {_spec['scale']} tiles, got {len(_spec['tiles'])}"
    for _tile in _spec["tiles"]:
        assert _tile["id"] not in _seen_ids, f"Duplicate tile id {_tile['id']}"
        _seen_ids.add(_tile["id"])
        assert _tile["condition"].strip(), f"Tile {_tile['id']} has an empty condition"
        assert _tile["name"].strip(), f"Tile {_tile['id']} has an empty name"
        _all_tiles += 1
    for _ctx_key in {c for t in _spec["tiles"] for c in t.get("context_needs", [])}:
        assert _ctx_key in COMPONENTS, f"Component '{_key}' references unknown component '{_ctx_key}'"

assert _all_tiles == 80, f"The baldosas model has exactly 80 tiles, got {_all_tiles}"

_pairs: dict[str, list[str]] = {}
for _key, _spec in COMPONENTS.items():
    if _spec["pair"]:
        _pairs.setdefault(_spec["pair"], []).append(_key)
for _pair_name, _members in _pairs.items():
    assert len(_members) == 2, f"Pair '{_pair_name}' must have exactly 2 members, got {_members}"
    assert all(COMPONENTS[m]["scale"] == 5 for m in _members), f"Pair '{_pair_name}' members must be 0-5 scale"

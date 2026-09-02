(() => {
  "use strict";

  const marker = document.querySelector("[data-b3s-webmcp-surface]");
  if (!marker) return;

  const modelContext = navigator.modelContext || document.modelContext;
  if (!modelContext || typeof modelContext.registerTool !== "function") return;

  const surface = marker.dataset.b3sWebmcpSurface;
  const reviewDomain = marker.dataset.b3sWebmcpDomain || "";
  const SAFE_ID = /^[A-Za-z0-9._:-]{1,180}$/;
  const REVIEW_STATES = new Set([
    "actionable",
    "pending",
    "disputed",
    "accepted",
    "rejected",
    "all",
  ]);

  function compactText(value, maxLength = 4000) {
    const normalized = String(value == null ? "" : value)
      .replace(/\s+/g, " ")
      .trim();
    return normalized.length <= maxLength
      ? normalized
      : `${normalized.slice(0, Math.max(0, maxLength - 1))}…`;
  }

  function nodeText(node, maxLength = 4000) {
    return compactText(node?.textContent || "", maxLength);
  }

  function parseNumber(value) {
    const match = String(value || "").replace(",", ".").match(/[-+]?\d+(?:\.\d+)?/);
    return match ? Number(match[0]) : null;
  }

  function closedInput(value, allowedKeys, requiredKeys = []) {
    const input = value == null ? {} : value;
    if (typeof input !== "object" || Array.isArray(input)) {
      throw new Error("La entrada debe ser un objeto JSON.");
    }
    const unknown = Object.keys(input).filter((key) => !allowedKeys.includes(key));
    if (unknown.length) throw new Error(`Campos no admitidos: ${unknown.join(", ")}.`);
    const missing = requiredKeys.filter((key) => !Object.prototype.hasOwnProperty.call(input, key));
    if (missing.length) throw new Error(`Faltan campos obligatorios: ${missing.join(", ")}.`);
    return input;
  }

  function stringArg(input, key, { required = false, minLength = 0, maxLength = 4000, defaultValue = "" } = {}) {
    const value = input[key];
    if (value == null) {
      if (required) throw new Error(`${key} es obligatorio.`);
      return defaultValue;
    }
    if (typeof value !== "string") throw new Error(`${key} debe ser texto.`);
    const normalized = value.trim();
    if (normalized.length < minLength || normalized.length > maxLength) {
      throw new Error(`${key} debe tener entre ${minLength} y ${maxLength} caracteres.`);
    }
    return normalized;
  }

  function integerArg(input, key, { minimum, maximum, defaultValue }) {
    const value = input[key];
    if (value == null) return defaultValue;
    if (!Number.isInteger(value) || value < minimum || value > maximum) {
      throw new Error(`${key} debe ser un entero entre ${minimum} y ${maximum}.`);
    }
    return value;
  }

  function booleanArg(input, key, defaultValue = false) {
    const value = input[key];
    if (value == null) return defaultValue;
    if (typeof value !== "boolean") throw new Error(`${key} debe ser booleano.`);
    return value;
  }

  function safeIdentifier(value, key) {
    const identifier = String(value || "").trim();
    if (!SAFE_ID.test(identifier)) throw new Error(`${key} no es válido.`);
    return identifier;
  }

  function safeDomain(value) {
    const domain = String(value || "").trim().toLowerCase();
    if (
      !domain ||
      domain.length > 255 ||
      domain.includes("/") ||
      domain.includes("\\") ||
      /\s/.test(domain) ||
      /[\u0000-\u001f\u007f]/.test(domain)
    ) {
      throw new Error("domain debe ser un dominio sin protocolo ni ruta.");
    }
    return domain;
  }

  function requireConfirmation(input, action) {
    if (input.confirm !== true) {
      throw new Error(`${action} requiere confirm=true tras una petición explícita del usuario.`);
    }
  }

  function keyValues(root) {
    if (!root) return {};
    const terms = [...root.querySelectorAll("dt")];
    const values = [...root.querySelectorAll("dd")];
    return Object.fromEntries(
      terms.slice(0, 60).map((term, index) => [
        nodeText(term, 160),
        nodeText(values[index], 2500),
      ])
    );
  }

  function unique(values, limit = 50) {
    return [...new Set(values.filter(Boolean))].slice(0, limit);
  }

  function bounded(value, depth = 0) {
    if (depth > 7) return null;
    if (value == null || typeof value === "number" || typeof value === "boolean") return value;
    if (typeof value === "string") return compactText(value, 5000);
    if (Array.isArray(value)) return value.slice(0, 100).map((item) => bounded(item, depth + 1));
    if (typeof value === "object") {
      return Object.fromEntries(
        Object.entries(value)
          .slice(0, 100)
          .map(([key, item]) => [compactText(key, 160), bounded(item, depth + 1)])
      );
    }
    return compactText(value, 5000);
  }

  function responseError(response, payload) {
    const detail = payload && typeof payload === "object"
      ? payload.reason || payload.detail || payload.error || payload.message
      : payload;
    return new Error(compactText(detail || `B3S Vault respondió con HTTP ${response.status}.`, 700));
  }

  function assertSameOriginFinalUrl(response) {
    const finalUrl = new URL(response.url, window.location.origin);
    if (finalUrl.origin !== window.location.origin) {
      throw new Error("B3S Vault redirigió la operación fuera del origen actual.");
    }
    if (finalUrl.pathname.startsWith("/auth/") || finalUrl.pathname === "/vault/review/login") {
      throw new Error("La sesión requerida ha caducado. Inicia sesión en B3S Vault y repite la acción.");
    }
    return finalUrl;
  }

  async function fetchDocument(path, signal) {
    const response = await fetch(path, {
      credentials: "same-origin",
      headers: { Accept: "text/html" },
      signal,
    });
    const finalUrl = assertSameOriginFinalUrl(response);
    const markup = await response.text();
    if (!response.ok) throw responseError(response, markup);
    return {
      document: new DOMParser().parseFromString(markup, "text/html"),
      finalUrl,
    };
  }

  async function fetchJson(path, options = {}) {
    const headers = new Headers(options.headers || {});
    headers.set("Accept", "application/json");
    const response = await fetch(path, {
      ...options,
      credentials: "same-origin",
      headers,
    });
    assertSameOriginFinalUrl(response);
    const rawPayload = await response.text();
    let payload = rawPayload;
    try {
      payload = JSON.parse(rawPayload);
    } catch {
      // Preserve bounded text for honest same-origin errors.
    }
    if (!response.ok) throw responseError(response, payload);
    return payload;
  }

  function parseIndexDocument(doc) {
    return [...doc.querySelectorAll("table.reports-table tbody tr[data-row-href]")].map((row) => {
      const cells = [...row.querySelectorAll("td")];
      const brandLink = cells[0]?.querySelector('a[href^="/brand/"]');
      const brandPath = brandLink?.getAttribute("href") || row.dataset.rowHref || "";
      const coverage = nodeText(cells[2], 80).match(/(\d+)\s*\/\s*(\d+)/);
      return {
        brandName: nodeText(brandLink, 240),
        domain: decodeURIComponent(brandPath.replace(/^\/brand\//, "").split(/[?#]/)[0] || ""),
        url: nodeText(cells[0]?.querySelector(".brand-cell-url"), 2048),
        score: parseNumber(cells[1]?.querySelector("strong")?.textContent),
        publishable: !nodeText(cells[1], 240).toLowerCase().includes("diagnóstico"),
        detectedComponents: coverage ? Number(coverage[1]) : null,
        componentCount: coverage ? Number(coverage[2]) : null,
        notDetected: [...(cells[3]?.querySelectorAll(".chip") || [])]
          .map((chip) => nodeText(chip, 160))
          .filter((label) => label && label.toLowerCase() !== "none"),
        brandPath,
      };
    });
  }

  async function listBrandAnalyses(rawInput, context = {}) {
    const input = closedInput(rawInput, ["query", "limit"]);
    const query = stringArg(input, "query", { maxLength: 240 }).toLowerCase();
    const limit = integerArg(input, "limit", { minimum: 1, maximum: 100, defaultValue: 25 });
    const { document: doc } = await fetchDocument("/", context.signal);
    const allRows = parseIndexDocument(doc);
    const matchingRows = query
      ? allRows.filter((row) => [row.brandName, row.domain, row.url].join(" ").toLowerCase().includes(query))
      : allRows;
    return {
      total: allRows.length,
      matched: matchingRows.length,
      analyses: matchingRows.slice(0, limit),
    };
  }

  function findBrandSection(doc, label) {
    return [...doc.querySelectorAll(".brand-profile-block")].find(
      (section) => nodeText(section.querySelector(".section-head .label"), 160) === label
    );
  }

  function parseBrandDocument(doc, requestedDomain) {
    const currentReportPath = doc
      .querySelector('.brand-profile-terminal a[href^="/report/"]')
      ?.getAttribute("href") || "";
    const stateSection = findBrandSection(doc, "estado");
    const linksSection = findBrandSection(doc, "links_detectados");
    const historySection = findBrandSection(doc, "historial_de_scans");
    const history = [...(historySection?.querySelectorAll("tbody tr") || [])]
      .slice(0, 100)
      .map((row) => {
        const cells = [...row.querySelectorAll("td")];
        return {
          date: nodeText(cells[0], 40),
          score: parseNumber(cells[1]?.textContent),
          publishable: !nodeText(cells[1], 200).toLowerCase().includes("diagnóstico"),
          canonicalStatus: nodeText(cells[2], 160),
          comparison: nodeText(cells[3], 160),
          reportId: nodeText(cells[4], 180),
          selectedSv9: [...(cells[5]?.querySelectorAll(".chip") || [])].some((chip) => nodeText(chip, 80) === "SV9"),
          latestAttempt: [...(cells[5]?.querySelectorAll(".chip") || [])].some((chip) => nodeText(chip, 120) === "último intento"),
        };
      });
    const vaultRoot = doc.querySelector("#memoria-vault");
    const vaultMetrics = Object.fromEntries(
      [...doc.querySelectorAll("#memoria-vault .vault-memory-metric")]
        .slice(0, 60)
        .map((metric) => [
          nodeText(metric.querySelector("span"), 180),
          nodeText(metric.querySelector("strong"), 180),
        ])
        .filter(([key]) => key)
    );
    const reviewQueue = [...doc.querySelectorAll("#cola-revision-vault table tbody tr")]
      .slice(0, 100)
      .map((row) => {
        const cells = [...row.querySelectorAll("td")];
        return {
          tile: nodeText(cells[0], 160),
          change: nodeText(cells[1], 500),
          priority: nodeText(cells[2], 160),
          channel: nodeText(cells[3], 240),
          state: nodeText(cells[4], 200),
          nextAction: nodeText(cells[5], 1500),
        };
      });
    return {
      brandName: nodeText(doc.querySelector(".brand-profile-title"), 240),
      domain: nodeText(doc.querySelector(".brand-profile-domain-link"), 300) || requestedDomain,
      url: doc.querySelector(".brand-profile-domain-link")?.getAttribute("href") || "",
      score: parseNumber(doc.querySelector(".brand-profile-score")?.textContent),
      status: nodeText(doc.querySelector(".brand-profile-terminal-value"), 700),
      currentReportId: currentReportPath.replace(/^\/report\//, "").split(/[?#]/)[0] || null,
      state: keyValues(stateSection?.querySelector("dl")),
      evidenceUrls: unique(
        [...(linksSection?.querySelectorAll("a[href]") || [])].map((anchor) => anchor.href),
        30
      ),
      history,
      vaultMemory: {
        present: Boolean(vaultRoot),
        metrics: vaultMetrics,
        reviewQueue,
        reviewPath: doc.querySelector('a[href^="/vault/review/"]')?.getAttribute("href") || null,
        contract: nodeText(vaultRoot?.querySelector(".vault-memory-contract"), 1200),
      },
    };
  }

  async function getBrandAnalysis(rawInput, context = {}) {
    const input = closedInput(rawInput, ["domain"], ["domain"]);
    const domain = safeDomain(stringArg(input, "domain", { required: true, minLength: 1, maxLength: 255 }));
    const { document: doc } = await fetchDocument(`/brand/${encodeURIComponent(domain)}?lang=es`, context.signal);
    return parseBrandDocument(doc, domain);
  }

  async function readReportMarkdown(rawInput, context = {}) {
    const input = closedInput(rawInput, ["reportId", "maxChars"], ["reportId"]);
    const reportId = safeIdentifier(
      stringArg(input, "reportId", { required: true, minLength: 1, maxLength: 180 }),
      "reportId"
    );
    const maxChars = integerArg(input, "maxChars", {
      minimum: 1000,
      maximum: 60000,
      defaultValue: 30000,
    });
    const response = await fetch(`/report/${encodeURIComponent(reportId)}.md`, {
      credentials: "same-origin",
      headers: { Accept: "text/markdown" },
      signal: context.signal,
    });
    assertSameOriginFinalUrl(response);
    const markdown = await response.text();
    if (!response.ok) throw responseError(response, markdown);
    return {
      reportId,
      characterCount: markdown.length,
      truncated: markdown.length > maxChars,
      markdown: markdown.length > maxChars
        ? `${markdown.slice(0, maxChars)}\n\n[truncated]`
        : markdown,
    };
  }

  async function rawScanStatus(scanId, signal) {
    return fetchJson(`/api/scan/${encodeURIComponent(scanId)}`, { signal });
  }

  async function getScanStatus(rawInput, context = {}) {
    const input = closedInput(rawInput, ["scanId"], ["scanId"]);
    const scanId = safeIdentifier(
      stringArg(input, "scanId", { required: true, minLength: 1, maxLength: 180 }),
      "scanId"
    );
    return bounded(await rawScanStatus(scanId, context.signal));
  }

  async function startScan(rawInput, context = {}) {
    const input = closedInput(
      rawInput,
      ["url", "brandName", "allowDegradedFallback", "confirm"],
      ["url", "confirm"]
    );
    requireConfirmation(input, "start_scan");
    const url = stringArg(input, "url", { required: true, minLength: 1, maxLength: 2048 });
    if (/[\u0000-\u001f\u007f]/.test(url)) throw new Error("url no es válida.");
    const brandName = stringArg(input, "brandName", { maxLength: 240 });
    const allowDegradedFallback = booleanArg(input, "allowDegradedFallback", false);
    const form = new FormData();
    form.set("url", url);
    form.set("brand_name", brandName);
    if (allowDegradedFallback) form.set("allow_degraded_fallback", "true");

    const response = await fetch("/scan", {
      method: "POST",
      body: form,
      credentials: "same-origin",
      headers: { Accept: "text/html" },
      signal: context.signal,
    });
    const finalUrl = assertSameOriginFinalUrl(response);
    const body = await response.text();
    if (!response.ok) throw responseError(response, body);
    if (finalUrl.pathname === "/") {
      throw new Error(compactText(finalUrl.searchParams.get("error") || "B3S Vault rechazó el scan.", 700));
    }
    const match = finalUrl.pathname.match(/^\/(scan|report)\/([^/]+)$/);
    if (!match) throw new Error("B3S Vault no devolvió un identificador verificable de scan o informe.");
    const scanId = decodeURIComponent(match[2]);
    const status = bounded(await rawScanStatus(scanId, context.signal));
    return {
      started: true,
      scanId,
      resourceType: match[1],
      status,
    };
  }

  async function continueDegradedScan(rawInput, context = {}) {
    const input = closedInput(rawInput, ["scanId", "confirm"], ["scanId", "confirm"]);
    requireConfirmation(input, "continue_degraded_scan");
    const scanId = safeIdentifier(
      stringArg(input, "scanId", { required: true, minLength: 1, maxLength: 180 }),
      "scanId"
    );
    const before = await rawScanStatus(scanId, context.signal);
    if (before?.state !== "blocked" || before?.acquisition_gate?.can_continue !== true) {
      throw new Error("El scan no está bloqueado en un estado que permita continuar degradado.");
    }
    const operation = await fetchJson(`/api/scan/${encodeURIComponent(scanId)}/continue`, {
      method: "POST",
      signal: context.signal,
    });
    const status = await rawScanStatus(scanId, context.signal);
    return bounded({ beforeState: before.state, operation, status });
  }

  async function cancelScan(rawInput, context = {}) {
    const input = closedInput(rawInput, ["scanId", "confirm"], ["scanId", "confirm"]);
    requireConfirmation(input, "cancel_scan");
    const scanId = safeIdentifier(
      stringArg(input, "scanId", { required: true, minLength: 1, maxLength: 180 }),
      "scanId"
    );
    const before = await rawScanStatus(scanId, context.signal);
    if (!["running", "blocked"].includes(before?.state)) {
      throw new Error(`El scan está en estado ${compactText(before?.state || "unknown", 80)} y no se puede cancelar.`);
    }
    const operation = await fetchJson(`/api/scan/${encodeURIComponent(scanId)}/cancel`, {
      method: "POST",
      signal: context.signal,
    });
    const status = await rawScanStatus(scanId, context.signal);
    return bounded({ beforeState: before.state, operation, status });
  }

  function parseReviewDocument(doc, domain, state) {
    const summary = Object.fromEntries(
      [...doc.querySelectorAll(".vault-review-summary .vault-memory-metric")]
        .map((metric) => [
          nodeText(metric.querySelector("span"), 180),
          parseNumber(metric.querySelector("strong")?.textContent),
        ])
        .filter(([key]) => key)
    );
    const items = [...doc.querySelectorAll(".vault-review-card")].slice(0, 100).map((card) => {
      const columns = [...card.querySelectorAll(".vault-review-grid > section")];
      const form = card.querySelector("form.vault-review-decision-form");
      return {
        caseId: card.id,
        tile: nodeText(card.querySelector(".section-head .label"), 240),
        decision: nodeText(card.querySelector(".section-head .chip"), 180),
        tileCondition: nodeText(columns[0]?.querySelector("p"), 3500),
        tileContract: keyValues(columns[0]?.querySelector("dl")),
        quote: nodeText(card.querySelector(".vault-review-quote"), 5000),
        sourceUrls: unique(
          [...card.querySelectorAll(".brand-profile-evidence-list a[href]")].map((anchor) => anchor.href),
          20
        ),
        evidenceMetadata: keyValues(columns[1]?.querySelector("dl")),
        canReview: Boolean(form),
        reviewPrompt: nodeText(form?.querySelector("p strong"), 2500),
      };
    });
    return {
      domain,
      state,
      reviewer: nodeText(doc.querySelector(".page-head .muted.mono"), 240),
      summary,
      journalReady: ![...doc.querySelectorAll("p.error")].some((node) =>
        nodeText(node, 600).includes("journal PostgreSQL no está disponible")
      ),
      items,
    };
  }

  async function listReviewCases(rawInput, context = {}) {
    const input = closedInput(rawInput, ["state"]);
    if (!reviewDomain) throw new Error("No hay un dominio de revisión asociado a esta página.");
    const state = stringArg(input, "state", { maxLength: 20, defaultValue: "actionable" });
    if (!REVIEW_STATES.has(state)) throw new Error("state no es válido.");
    const currentState = new URLSearchParams(window.location.search).get("state") || "actionable";
    if (state === currentState) return parseReviewDocument(document, reviewDomain, state);
    const { document: doc } = await fetchDocument(
      `/vault/review/${encodeURIComponent(reviewDomain)}?state=${encodeURIComponent(state)}`,
      context.signal
    );
    return parseReviewDocument(doc, reviewDomain, state);
  }

  async function prepareReviewDecision(rawInput) {
    const input = closedInput(rawInput, ["caseId", "decision", "rationale"], ["caseId", "decision", "rationale"]);
    if (!reviewDomain) throw new Error("No hay un dominio de revisión asociado a esta página.");
    const caseId = safeIdentifier(
      stringArg(input, "caseId", { required: true, minLength: 1, maxLength: 180 }),
      "caseId"
    );
    const decision = stringArg(input, "decision", { required: true, minLength: 1, maxLength: 20 });
    if (!["accepted", "disputed", "rejected"].includes(decision)) {
      throw new Error("decision debe ser accepted, disputed o rejected.");
    }
    const rationale = stringArg(input, "rationale", { required: true, minLength: 12, maxLength: 2000 });
    const card = [...document.querySelectorAll(".vault-review-card")].find(
      (candidate) => candidate.id === caseId
    );
    const form = card?.querySelector("form.vault-review-decision-form");
    if (!card || !form) throw new Error("El caso no está visible o ya no admite una decisión.");
    const radio = [...form.querySelectorAll('input[name="decision"]')].find(
      (inputNode) => inputNode.value === decision
    );
    const textarea = form.querySelector('textarea[name="rationale"]');
    if (!radio || !textarea) throw new Error("La decisión solicitada no está permitida para este caso.");

    radio.checked = true;
    radio.dispatchEvent(new Event("change", { bubbles: true }));
    textarea.value = rationale;
    textarea.dispatchEvent(new Event("input", { bubbles: true }));
    card.scrollIntoView({ behavior: "smooth", block: "center" });
    textarea.focus({ preventScroll: true });
    return {
      prepared: radio.checked && textarea.value === rationale,
      submitted: false,
      requiresHumanSubmit: true,
      domain: reviewDomain,
      caseId,
      decision: radio.value,
      rationaleLength: textarea.value.length,
    };
  }

  const readOnlyAnnotations = { readOnlyHint: true, untrustedContentHint: true };
  const mutationAnnotations = { readOnlyHint: false, untrustedContentHint: true };

  const flocTools = [
    {
      name: "b3s_list_brand_analyses",
      description: "Lista los análisis visibles en B3S Vault y permite filtrarlos por marca, dominio o URL. No ejecuta capturas ni modifica estado.",
      inputSchema: {
        type: "object",
        properties: {
          query: { type: "string", maxLength: 240 },
          limit: { type: "integer", minimum: 1, maximum: 100, default: 25 },
        },
        additionalProperties: false,
      },
      annotations: readOnlyAnnotations,
      execute: listBrandAnalyses,
    },
    {
      name: "b3s_get_brand_analysis",
      description: "Devuelve score, estado, evidencia enlazada, historial y memoria Vault de una marca sin reanalizar ni cambiar autoridad.",
      inputSchema: {
        type: "object",
        properties: {
          domain: { type: "string", minLength: 1, maxLength: 255 },
        },
        required: ["domain"],
        additionalProperties: false,
      },
      annotations: readOnlyAnnotations,
      execute: getBrandAnalysis,
    },
    {
      name: "b3s_read_report_markdown",
      description: "Lee la exportación Markdown persistida de un informe B3S por ID. Devuelve contenido acotado y no ejecuta una nueva evaluación.",
      inputSchema: {
        type: "object",
        properties: {
          reportId: { type: "string", minLength: 1, maxLength: 180 },
          maxChars: { type: "integer", minimum: 1000, maximum: 60000, default: 30000 },
        },
        required: ["reportId"],
        additionalProperties: false,
      },
      annotations: readOnlyAnnotations,
      execute: readReportMarkdown,
    },
    {
      name: "b3s_get_scan_status",
      description: "Consulta el estado, las fases y la adquisición de un scan existente de B3S Vault sin cambiarlo.",
      inputSchema: {
        type: "object",
        properties: {
          scanId: { type: "string", minLength: 1, maxLength: 180 },
        },
        required: ["scanId"],
        additionalProperties: false,
      },
      annotations: readOnlyAnnotations,
      execute: getScanStatus,
    },
    {
      name: "b3s_start_scan",
      description: "Inicia una adquisición real en B3S Vault y puede consumir créditos. Úsala sólo tras petición explícita del usuario y con confirm=true.",
      inputSchema: {
        type: "object",
        properties: {
          url: { type: "string", minLength: 1, maxLength: 2048 },
          brandName: { type: "string", maxLength: 240 },
          allowDegradedFallback: { type: "boolean", default: false },
          confirm: { type: "boolean", const: true },
        },
        required: ["url", "confirm"],
        additionalProperties: false,
      },
      annotations: mutationAnnotations,
      execute: startScan,
    },
    {
      name: "b3s_continue_degraded_scan",
      description: "Continúa un scan bloqueado con adquisición degradada. Revalida el estado actual y exige confirm=true.",
      inputSchema: {
        type: "object",
        properties: {
          scanId: { type: "string", minLength: 1, maxLength: 180 },
          confirm: { type: "boolean", const: true },
        },
        required: ["scanId", "confirm"],
        additionalProperties: false,
      },
      annotations: mutationAnnotations,
      execute: continueDegradedScan,
    },
    {
      name: "b3s_cancel_scan",
      description: "Cancela un scan en ejecución o bloqueado. Revalida el estado actual y exige confirm=true porque interrumpe la operación.",
      inputSchema: {
        type: "object",
        properties: {
          scanId: { type: "string", minLength: 1, maxLength: 180 },
          confirm: { type: "boolean", const: true },
        },
        required: ["scanId", "confirm"],
        additionalProperties: false,
      },
      annotations: mutationAnnotations,
      execute: cancelScan,
    },
  ];

  const reviewTools = [
    {
      name: "b3s_list_vault_review_cases",
      description: "Lista los casos de la revisión protegida actual con contrato de baldosa, cita, fuentes y estado. No registra decisiones.",
      inputSchema: {
        type: "object",
        properties: {
          state: {
            type: "string",
            enum: ["actionable", "pending", "disputed", "accepted", "rejected", "all"],
            default: "actionable",
          },
        },
        additionalProperties: false,
      },
      annotations: readOnlyAnnotations,
      execute: listReviewCases,
    },
    {
      name: "b3s_prepare_vault_review_decision",
      description: "Rellena una decisión y su justificación en el formulario protegido, pero no la envía ni la firma. El revisor humano mantiene el control final.",
      inputSchema: {
        type: "object",
        properties: {
          caseId: { type: "string", minLength: 1, maxLength: 180 },
          decision: { type: "string", enum: ["accepted", "disputed", "rejected"] },
          rationale: { type: "string", minLength: 12, maxLength: 2000 },
        },
        required: ["caseId", "decision", "rationale"],
        additionalProperties: false,
      },
      annotations: mutationAnnotations,
      execute: prepareReviewDecision,
    },
  ];

  const tools = surface === "floc" ? flocTools : surface === "review" ? reviewTools : [];
  for (const tool of tools) {
    Promise.resolve(modelContext.registerTool(tool)).catch((error) => {
      console.warn(`[B3S Vault WebMCP] No se pudo registrar ${tool.name}.`, error);
    });
  }
})();

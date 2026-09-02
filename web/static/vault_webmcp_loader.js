(() => {
  "use strict";

  const isVault = window.location.origin === "https://b3s-vault.fly.dev";
  const isOptInLocal = ["localhost", "127.0.0.1"].includes(window.location.hostname)
    && new URLSearchParams(window.location.search).get("webmcp") === "1";
  if (!isVault && !isOptInLocal) return;

  const script = document.createElement("script");
  script.src = "/static/vault_webmcp.js";
  script.defer = true;
  document.head.appendChild(script);
})();

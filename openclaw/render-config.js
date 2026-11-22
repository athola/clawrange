// Render openclaw.json from the template on every container start.
//
// Template-owned keys (provider URL, PROXY_AUTH_TOKEN, timezone, tool
// policy) are refreshed so a rotated token or a changed SCHEDULER_TZ takes
// effect on restart. Keys OpenClaw adds at runtime (commands, controlUi,
// meta, ...) survive, because the rendered template is deep-merged over the
// existing file instead of replacing it. Arrays are replaced, not merged.
//
// Usage: node render-config.js <template> <target>
// Env:   PROXY_AUTH_TOKEN, OPENCLAW_TZ (default UTC)
"use strict";
const fs = require("fs");

function substitute(text, env) {
  // split/join on already-parsed string values: a token may contain any
  // character, including quotes and backslashes, without breaking the JSON.
  return text
    .split("${PROXY_AUTH_TOKEN}")
    .join(env.PROXY_AUTH_TOKEN || "")
    .split("${OPENCLAW_TZ}")
    .join(env.OPENCLAW_TZ || "UTC");
}

function render(value, env) {
  if (typeof value === "string") return substitute(value, env);
  if (Array.isArray(value)) return value.map((v) => render(v, env));
  if (value !== null && typeof value === "object") {
    const out = {};
    for (const key of Object.keys(value)) out[key] = render(value[key], env);
    return out;
  }
  return value;
}

function isObject(v) {
  return v !== null && typeof v === "object" && !Array.isArray(v);
}

function merge(base, over) {
  for (const key of Object.keys(over)) {
    if (isObject(base[key]) && isObject(over[key])) merge(base[key], over[key]);
    else base[key] = over[key];
  }
  return base;
}

function main(templatePath, targetPath, env) {
  if (!env.PROXY_AUTH_TOKEN) {
    // Rendered anyway so the gateway still starts, but every proxied LLM
    // call will get 401 from workflows until the token is set.
    console.error(
      "render-config: WARNING PROXY_AUTH_TOKEN is empty; the provider apiKey " +
        "will be blank and the workflows proxy will reject every request"
    );
  }
  const rendered = render(JSON.parse(fs.readFileSync(templatePath, "utf8")), env);
  let current = {};
  try {
    current = JSON.parse(fs.readFileSync(targetPath, "utf8"));
  } catch (err) {
    if (err.code !== "ENOENT") {
      // The file holds keys OpenClaw wrote at runtime (paired devices,
      // command settings). Replacing it with the bare template would drop
      // them silently, so stop and let the operator repair or remove it.
      throw new Error(
        `refusing to overwrite unreadable ${targetPath} (${err.message}); ` +
          "fix or delete it and restart"
      );
    }
  }
  const merged = merge(current, rendered);
  // OpenClaw 2026.11.5 retired heartbeat config for automation jobs and
  // rejects a config that still has the keys. The merge keeps them from an
  // older install, and only doctor can turn them into the job, so stop
  // rather than delete them and hide a skipped migration.
  const agents = merged.agents || {};
  const stale = Object.entries(agents.entries || {})
    .filter(([, a]) => isObject(a) && "heartbeat" in a)
    .map(([id]) => `agents.entries.${id}.heartbeat`);
  if (isObject(agents.defaults) && "heartbeat" in agents.defaults) {
    stale.unshift("agents.defaults.heartbeat");
  }
  if (stale.length) {
    throw new Error(
      `${targetPath} still has retired keys (${stale.join(", ")}); ` +
        "stop the container and run `openclaw doctor --fix --yes` to migrate " +
        "them into an automation job (docs/DEPLOYMENT_GUIDE.md, Upgrading OpenClaw)"
    );
  }
  const tmp = `${targetPath}.tmp`;
  fs.writeFileSync(tmp, JSON.stringify(merged, null, 2) + "\n", {
    mode: 0o600,
  });
  fs.renameSync(tmp, targetPath);
}

if (require.main === module) {
  try {
    main(process.argv[2], process.argv[3], process.env);
  } catch (err) {
    console.error(`render-config: ${err.message}`);
    process.exit(1);
  }
}
module.exports = { render, merge, main };

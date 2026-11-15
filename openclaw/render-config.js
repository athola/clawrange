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
  const rendered = render(JSON.parse(fs.readFileSync(templatePath, "utf8")), env);
  let current = {};
  try {
    current = JSON.parse(fs.readFileSync(targetPath, "utf8"));
  } catch (err) {
    if (err.code !== "ENOENT") {
      console.error(`render-config: ignoring unreadable ${targetPath}: ${err.message}`);
    }
  }
  const tmp = `${targetPath}.tmp`;
  fs.writeFileSync(tmp, JSON.stringify(merge(current, rendered), null, 2) + "\n", {
    mode: 0o600,
  });
  fs.renameSync(tmp, targetPath);
}

if (require.main === module) {
  main(process.argv[2], process.argv[3], process.env);
}
module.exports = { render, merge, main };

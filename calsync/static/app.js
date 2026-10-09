"use strict";

const $ = (id) => document.getElementById(id);
let googleConfigured = false;

function setBadge(el, connected) {
  el.textContent = connected ? "Connected" : "Not connected";
  el.classList.toggle("ok", !!connected);
}

async function refreshStatus() {
  let s;
  try {
    s = await (await fetch("/status")).json();
  } catch (e) {
    return;
  }
  googleConfigured = !!s.google_configured;

  setBadge($("ms-badge"), s.microsoft);
  $("ms-sub").textContent = s.microsoft ? "Connected" : "Not connected";
  $("ms-connect").textContent = s.microsoft ? "Reconnect" : "Connect";
  $("ms-disconnect").classList.toggle("hidden", !s.microsoft);

  setBadge($("google-badge"), s.google);
  $("google-sub").textContent = s.google
    ? "Connected" + (s.google_calendar_id ? "" : " (calendar pending)")
    : googleConfigured ? "Client configured — not signed in" : "Not connected";
  $("google-connect").textContent = s.google ? "Reconnect" : "Connect";
  $("google-signout").classList.toggle("hidden", !s.google);
  $("google-hint").classList.toggle("hidden", googleConfigured || s.google);

  // Status line + sync state.
  const lr = s.last_run;
  $("last-run").textContent = lr
    ? `Last sync ${lr.at} — ${lr.summary}`
    : "No sync yet.";
  $("sync-spin").classList.toggle("hidden", !s.syncing);
  $("sync-now").disabled = s.syncing || !(s.microsoft && s.google);

  const err = s.error || (lr && lr.errors && lr.errors.length ? lr.errors[0] : null);
  $("sync-error").textContent = err || "";
  $("sync-error").classList.toggle("hidden", !err);
}

async function loadSettings() {
  let c;
  try {
    c = await (await fetch("/settings")).json();
  } catch (e) {
    return;
  }
  $("s-calname").value = c.google_calendar_name || "";
  $("s-detail").value = c.detail || "full";
  $("s-skip").checked = !!c.skip_declined;
  $("s-past").value = c.window_past_days;
  $("s-future").value = c.window_future_days;
  $("s-auto").checked = !!c.auto_sync;
  $("s-interval").value = String(c.poll_interval_minutes || 10);
}

async function loadLogs() {
  let d;
  try {
    d = await (await fetch("/logs")).json();
  } catch (e) {
    return;
  }
  $("logs").textContent = d.lines && d.lines.length ? d.lines.join("\n") : "No activity yet.";
  $("logs").scrollTop = $("logs").scrollHeight;
}

// ---- Microsoft -----------------------------------------------------------
$("ms-connect").addEventListener("click", async () => {
  $("ms-connect").disabled = true;
  let r;
  try {
    r = await (await fetch("/ms/start", { method: "POST" })).json();
  } catch (e) {
    $("ms-connect").disabled = false;
    return;
  }
  $("ms-connect").disabled = false;
  if (r.error) {
    $("ms-wait").textContent = "Error: " + r.error;
    $("ms-flow").classList.remove("hidden");
    return;
  }
  $("ms-code").textContent = r.user_code;
  $("ms-link").href = r.verification_uri;
  $("ms-flow").classList.remove("hidden");
  pollMs();
});

async function pollMs() {
  let r;
  try {
    r = await (await fetch("/ms/poll")).json();
  } catch (e) {
    setTimeout(pollMs, 3000);
    return;
  }
  if (r.status === "pending") {
    setTimeout(pollMs, 3000);
  } else if (r.status === "done") {
    $("ms-wait").textContent = "✓ Signed in.";
    $("ms-flow").classList.add("hidden");
    refreshStatus();
  } else {
    $("ms-wait").textContent = "Error: " + (r.error || "sign-in failed");
  }
}

$("ms-disconnect").addEventListener("click", async () => {
  if (!confirm("Disconnect Microsoft 365?")) return;
  await fetch("/ms/disconnect", { method: "POST" });
  refreshStatus();
});

// ---- Google --------------------------------------------------------------
$("google-connect").addEventListener("click", () => {
  if (googleConfigured) location.href = "/google/start";
  else $("google-file").click();
});

$("google-file").addEventListener("change", async (e) => {
  const file = e.target.files[0];
  if (!file) return;
  let data;
  try {
    data = JSON.parse(await file.text());
  } catch (err) {
    alert("That doesn't look like a Google client JSON file.");
    return;
  }
  if (data.installed) {
    alert(
      "This is a 'Desktop app' client (localhost redirect only), which can't be " +
      "used from the container. Create a 'Web application' client, add the redirect " +
      "URI shown, download its JSON, and use that file."
    );
    return;
  }
  const c = data.web;
  if (!c || !c.client_id || !c.client_secret) {
    alert("Couldn't find a Web-application client id and secret in that file.");
    return;
  }
  $("gf-id").value = c.client_id;
  $("gf-secret").value = c.client_secret;
  $("google-form").submit();
});

$("google-signout").addEventListener("click", async () => {
  if (!confirm("Sign out of Google? (The copied calendar stays in your account.)")) return;
  await fetch("/google/signout", { method: "POST" });
  refreshStatus();
});

// ---- Settings ------------------------------------------------------------
$("settings-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const body = new URLSearchParams(new FormData($("settings-form"))).toString();
  $("save-settings").disabled = true;
  try {
    const r = await (await fetch("/settings", {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body,
    })).json();
    $("saved-note").textContent = r.ok ? "Saved ✓" : "Error: " + (r.error || "");
  } catch (err) {
    $("saved-note").textContent = "Error saving";
  }
  $("save-settings").disabled = false;
  setTimeout(() => ($("saved-note").textContent = ""), 3000);
});

// ---- Sync now ------------------------------------------------------------
$("sync-now").addEventListener("click", async () => {
  $("sync-now").disabled = true;
  try {
    const r = await (await fetch("/sync", { method: "POST" })).json();
    if (r.error) $("sync-error").textContent = r.error, $("sync-error").classList.remove("hidden");
  } catch (e) {}
  // Poll status/logs a few times while the sync runs.
  let n = 0;
  const t = setInterval(() => {
    refreshStatus();
    loadLogs();
    if (++n > 20) clearInterval(t);
  }, 2000);
});

// Query-flag messages from the Google redirect.
const params = new URLSearchParams(location.search);
if (params.has("e")) {
  const msg = {
    google_missing: "Upload your Web-application client JSON to connect Google.",
    google_denied: "Google sign-in was declined.",
    google_state: "Google sign-in expired — try again.",
    google_exchange: "Could not complete Google sign-in. Check the client and redirect URI.",
  }[params.get("e")];
  if (msg) alert(msg);
  history.replaceState({}, "", "/");
}

refreshStatus();
loadSettings();
loadLogs();
setInterval(refreshStatus, 8000);
setInterval(loadLogs, 15000);

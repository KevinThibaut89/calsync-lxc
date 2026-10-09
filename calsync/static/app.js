"use strict";

const $ = (id) => document.getElementById(id);

async function refreshStatus() {
  let s;
  try {
    s = await (await fetch("/status")).json();
  } catch (e) {
    return;
  }
  setBadge($("ms-badge"), s.microsoft);
  setBadge($("google-badge"), s.google);
  if (s.microsoft && s.google) {
    $("done").classList.remove("hidden");
  }
}

function setBadge(el, connected) {
  el.textContent = connected ? "Connected" : "Not connected";
  el.classList.toggle("ok", !!connected);
}

$("ms-connect").addEventListener("click", async () => {
  $("ms-connect").disabled = true;
  let r;
  try {
    r = await (await fetch("/ms/start", { method: "POST" })).json();
  } catch (e) {
    $("ms-connect").disabled = false;
    return;
  }
  if (r.error) {
    $("ms-wait").textContent = "Error: " + r.error;
    $("ms-flow").classList.remove("hidden");
    return;
  }
  $("ms-code").textContent = r.user_code;
  const link = $("ms-link");
  link.href = r.verification_uri;
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
    refreshStatus();
  } else {
    $("ms-wait").textContent = "Error: " + (r.error || "sign-in failed");
  }
}

// Reflect ?ok / ?e query flags into a quick message, then clean the URL.
const params = new URLSearchParams(location.search);
if (params.has("e")) {
  const msg = {
    google_missing: "Enter both the client ID and secret.",
    google_denied: "Google sign-in was declined.",
    google_state: "Google sign-in expired — try again.",
    google_exchange: "Could not complete Google sign-in. Check the client and redirect URI.",
  }[params.get("e")];
  if (msg) alert(msg);
  history.replaceState({}, "", "/");
}

refreshStatus();

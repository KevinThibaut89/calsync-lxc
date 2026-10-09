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

// Google: a single button that reads the downloaded client JSON and submits it,
// so there's nothing to type. The browser parses the file and POSTs the two
// fields to /google/save, which 302-redirects on to Google's consent screen.
$("google-connect").addEventListener("click", () => $("google-file").click());

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
      "used from the container. In Google Cloud create an OAuth client of type " +
      "'Web application', add the redirect URI shown above, download its JSON, " +
      "and use that file."
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

// Reflect ?ok / ?e query flags into a quick message, then clean the URL.
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

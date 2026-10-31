(async function () {
  const msg = document.getElementById("msg"), q = new URLSearchParams(location.search), token = sessionStorage.getItem("token");
  if (!token) {
    sessionStorage.setItem("sso_next", location.pathname + location.search);
    msg.textContent = "Please sign in to the portal first; redirecting…"; setTimeout(() => { location.href = "/"; }, 800); return;
  }
  const body = { client_id: q.get("client_id") || "", redirect_uri: q.get("redirect_uri") || "", state: q.get("state") || "", nonce: q.get("nonce") || "",
    code_challenge: q.get("code_challenge") || "", code_challenge_method: q.get("code_challenge_method") || "S256" };
  const r = await fetch("/api/sso/authorize", { method: "POST", headers: { "Content-Type": "application/json", Authorization: "Bearer " + token }, body: JSON.stringify(body) });
  const d = await r.json().catch(() => ({}));
  if (r.ok) location.replace(d.redirect);
  else msg.textContent = typeof d.detail === "string" ? d.detail : "Sign-on was refused.";
})();

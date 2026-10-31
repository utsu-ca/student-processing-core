(function () {
  const token = sessionStorage.getItem("token"), $ = id => document.getElementById(id);
  const err = $("err"), result = $("result"), input = $("uuid");
  if (!token) { err.textContent = "Sign in to the portal as an admin first."; return; }
  let busy = false;
  function show(d) {
    result.replaceChildren();
    const c = document.createElement("div"); c.className = "card";
    const msg = document.createElement("h2"); msg.className = d.already_checked_in ? "badge" : "ok"; msg.textContent = (d.already_checked_in ? "⚠ " : "✔ ") + d.message;
    const n = document.createElement("p"); n.style.fontSize = "1.6rem"; n.textContent = d.name + (d.pronouns ? " (" + d.pronouns + ")" : "");
    const meta = document.createElement("p"); meta.textContent = "Legal name: " + d.legal_name + (d.proxy ? " · voting by proxy" : "") + " · " + new Date(d.checked_in_at * 1000).toLocaleTimeString();
    c.append(msg, n, meta); result.append(c);
    const li = document.createElement("li"); li.textContent = d.name + (d.pronouns ? " (" + d.pronouns + ")" : "") + " — " + (d.already_checked_in ? "duplicate" : "checked in"); $("recent").prepend(li);
  }
  async function checkin(value) {
    if (busy || !value.trim()) return; busy = true; err.textContent = "";
    try {
      const r = await fetch("/api/admin/agm/checkin", { method: "POST", headers: { "Content-Type": "application/json", Authorization: "Bearer " + token }, body: JSON.stringify({ uuid: value.trim() }) });
      const d = await r.json().catch(() => ({}));
      if (r.ok) { show(d); input.value = ""; } else { result.replaceChildren(); err.textContent = typeof d.detail === "string" ? d.detail : "Check-in failed"; }
    } finally { busy = false; input.focus(); }
  }
  $("go").onclick = () => checkin(input.value);
  input.onkeydown = e => { if (e.key === "Enter") checkin(input.value); };  // hardware QR scanners type the code then press Enter
  $("scan").onclick = async () => {
    if (!("BarcodeDetector" in window)) { err.textContent = "Camera QR scanning is not supported in this browser; use a scanner or paste the code."; return; }
    const v = $("cam"), det = new BarcodeDetector({ formats: ["qr_code"] });
    v.srcObject = await navigator.mediaDevices.getUserMedia({ video: { facingMode: "environment" } }); v.hidden = false;
    let last = "";
    setInterval(async () => { const f = await det.detect(v).catch(() => []); if (f[0] && f[0].rawValue !== last) { last = f[0].rawValue; checkin(last); } else if (!f[0]) last = ""; }, 400);
  };
})();

(function () {
  const { el, render } = PortalForms;
  const main = document.getElementById("main"), nav = document.getElementById("nav");
  let token = sessionStorage.getItem("token"), me = null;

  async function api(path, opts = {}) {
    const h = Object.assign({ "Content-Type": "application/json" }, token ? { Authorization: "Bearer " + token } : {});
    const r = await fetch("/api" + path, Object.assign({ headers: h }, opts, opts.body !== undefined ? { body: JSON.stringify(opts.body) } : {}));
    const body = r.status === 204 ? null : await r.json().catch(() => null);
    if (!r.ok) { const d = body && body.detail; throw new Error(typeof d === "string" ? d : d && d.error ? d.error : JSON.stringify(d || r.status)); }
    return body;
  }
  const msg = (cls, t) => el("p", { className: cls }, t);
  const card = (...k) => { const c = el("div", { className: "card" }, ...k); main.append(c); return c; };
  const field = (label, props) => { const i = el("input", props); return [el("label", {}, label), i, i]; };
  const act = (host, fn) => async () => { try { await fn(); } catch (e) { host.querySelector(".status")?.remove(); host.append(el("p", { className: "err status" }, e.message)); } };

  function setNav() {
    nav.replaceChildren();
    const link = (t, f) => { const b = el("button", {}, t); b.onclick = f; nav.append(b); };
    if (!token) return link("Sign in", authView);
    link("Forms", formsView); link("My applications", statusView); link("Account", accountView);
    if (me && me.role === "admin") { link("Admin: forms", adminForms); link("Admin: verifications", adminVerifications); }
    link("Sign out", () => { token = null; me = null; sessionStorage.removeItem("token"); setNav(); authView(); });
  }

  function authView() {
    main.replaceChildren();
    const [l1, u, _u] = field("Username", { autocomplete: "username" }), [l2, c, _c] = field("Email code", { autocomplete: "one-time-code" });
    const c1 = card(el("h2", {}, "Sign in"), l1, u);
    const send = el("button", { className: "primary" }, "Email me a code"); send.onclick = act(c1, async () => { await api("/auth/login", { method: "POST", body: { username: u.value } }); c1.append(msg("ok status", "If the account exists, a code was sent.")); });
    const go = el("button", { className: "primary" }, "Sign in"); go.onclick = act(c1, async () => {
      const r = await api("/auth/token", { method: "POST", body: { username: u.value, code: c.value } });
      token = r.token; sessionStorage.setItem("token", token); await start();
    });
    c1.append(send, l2, c, go);
    const b = el("button", { className: "primary" }, "Create an account"), r = el("button", { className: "primary" }, "Account recovery");
    b.onclick = registerView; r.onclick = recoveryView; c1.append(el("hr"), b, r);
  }

  function registerView() {
    main.replaceChildren();
    const f = {}; const c = card(el("h2", {}, "Create an account"));
    [["username", "Username"], ["email", "Email"], ["first_name", "Legal first name"], ["last_name", "Legal last name"], ["preferred_name", "Preferred name (optional)"], ["student_number", "Student number"]]
      .forEach(([k, l]) => { const [a, i] = field(l, {}); f[k] = i; c.append(a, i); });
    const [lc, code] = field("Email code", {});
    const go = el("button", { className: "primary" }, "Register"); go.onclick = act(c, async () => {
      const body = Object.fromEntries(Object.entries(f).map(([k, i]) => [k, i.value]));
      await api("/auth/register", { method: "POST", body }); c.append(msg("ok status", "Check your email for a code."), lc, code, ver);
    });
    const ver = el("button", { className: "primary" }, "Verify email"); ver.onclick = act(c, async () => { await api("/auth/verify-email", { method: "POST", body: { username: f.username.value, code: code.value } }); authView(); });
    c.append(go);
  }

  async function scanWidget(host) {
    const ch = await api("/verification/challenge");
    const video = el("video", { autoplay: true, playsInline: true, muted: true }), shots = {};
    host.append(el("p", {}, "Hold your face, then your student ID, to the camera. Write or say this phrase in the capture: " + ch.phrase), video);
    const stream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: "user" }, audio: false });
    video.srcObject = stream;
    ["face", "id_card"].forEach(k => {
      const b = el("button", { className: "primary" }, "Capture " + (k === "face" ? "face" : "ID card")), img = el("img", { className: "shot" });
      b.onclick = () => { const cv = el("canvas"); cv.width = 640; cv.height = Math.round(640 * (video.videoHeight || 480) / (video.videoWidth || 640)); cv.getContext("2d").drawImage(video, 0, 0, cv.width, cv.height); shots[k] = cv.toDataURL("image/jpeg", 0.8); img.src = shots[k]; };
      host.append(b, img);
    });
    return { challenge: ch.challenge, shots, stop: () => stream.getTracks().forEach(t => t.stop()) };
  }

  async function accountView() {
    main.replaceChildren(); await refreshMe();
    const u = me.user, c = card(el("h2", {}, "Account"), el("p", {}, `${u.first_name} ${u.last_name} · ${u.email} · ID ending ${u.student_number} · `), el("span", { className: "badge" }, me.status));
    const [l, pn] = field("Preferred name", { value: u.preferred_name }); const save = el("button", { className: "primary" }, "Save");
    save.onclick = act(c, async () => { await api("/me", { method: "PATCH", body: { preferred_name: pn.value } }); c.append(msg("ok status", "Saved")); });
    c.append(l, pn, save);
    if (me.status === "pending_review" || me.status === "pending_email") {
      const v = card(el("h2", {}, "Verify your identity")); const [l1, div] = field("Division codes (comma separated, e.g. UTSG)", {});
      const [l2, fn] = field("Legal first name", { value: u.first_name }), [l3, ln] = field("Legal last name", { value: u.last_name });
      v.append(l1, div, l2, fn, l3, ln); let w;
      const start = el("button", { className: "primary" }, "Start camera"); start.onclick = act(v, async () => { w = await scanWidget(v); });
      const sub = el("button", { className: "primary" }, "Submit for review"); sub.onclick = act(v, async () => {
        await api("/verification", { method: "POST", body: { first_name: fn.value, last_name: ln.value, division_codes: div.value.split(",").map(s => s.trim()), face: w.shots.face, id_card: w.shots.id_card, challenge: w.challenge } });
        w.stop(); v.append(msg("ok status", "Submitted. An administrator will review it."));
      });
      v.append(start, sub);
    }
  }

  async function formsView() {
    main.replaceChildren();
    const list = await api("/forms");
    for (const f of list) { const b = el("button", { className: "primary" }, "Open"); b.onclick = () => formView(f.id); card(el("h3", {}, f.title), el("span", { className: "badge" }, "v" + f.version), el("br"), b); }
  }

  // The whole form is reconstructed client side from the cached static JSON; the server is only asked to store the submission.
  async function formView(id) {
    main.replaceChildren();
    const f = await (await fetch(`/api/forms/${id}/static`)).json();
    const prev = (await api("/me/submissions")).find(s => s.form_id === id);
    const c = card(el("h2", {}, f.schema.title), el("p", {}, f.schema.description || "")), host = el("div"); c.append(host);
    const r = render(host, f.schema, prev ? prev.data : {});
    const b = el("button", { className: "primary" }, "Submit"); c.append(b);
    b.onclick = act(c, async () => {
      const { data, errors } = r.getValues(); r.setErrors(errors); if (Object.keys(errors).length) return;
      await api(`/forms/${id}/submission`, { method: "PUT", body: { version: f.version, hash: f.hash, data } });
      statusView();
    });
  }

  async function statusView() {
    main.replaceChildren(); const subs = await api("/me/submissions");
    if (!subs.length) card(el("p", {}, "No applications yet."));
    for (const s of subs) card(el("h3", {}, s.form_id), el("span", { className: "badge" }, s.status), el("p", {}, s.note || ""),
      el("ul", {}, ...s.history.map(h => el("li", {}, `${new Date(h.at * 1000).toLocaleString()} — ${h.status} ${h.note || ""}`))));
  }

  async function adminForms() {
    main.replaceChildren();
    for (const f of await api("/admin/forms")) { const b = el("button", { className: "primary" }, "Edit"); b.onclick = () => editor(f.id); card(el("h3", {}, f.id), el("span", { className: "badge" }, f.published_version ? "published v" + f.published_version : "unpublished"), el("br"), b); }
    const n = el("button", { className: "primary" }, "New form from template"); n.onclick = () => editor(prompt("New form id (a-z, 0-9, _)"), true); main.append(n);
  }

  async function editor(id, isNew) {
    main.replaceChildren();
    const f = isNew ? { rev: 0, draft_yaml: `id: ${id}\nversion: 1\ntitle: New form\nfields:\n  - {id: name, type: text, label: Name, required: true}\n` } : await api("/admin/forms/" + id);
    let rev = f.rev, schema = null;
    const c = card(el("h2", {}, "Edit " + id)), ta = el("textarea", { className: "yaml", value: f.draft_yaml, spellcheck: false }), prevHost = el("div", { className: "card" });
    c.append(el("div", { className: "row" }, ta, prevHost));
    const status = el("p", {}); c.append(status);
    const preview = async () => { try { schema = (await api("/admin/forms/convert", { method: "POST", body: { yaml: ta.value } })).schema; render(prevHost, schema, {}); status.className = "ok"; status.textContent = "valid"; } catch (e) { status.className = "err"; status.textContent = e.message; } };
    let t; ta.oninput = () => { clearTimeout(t); t = setTimeout(preview, 400); }; preview();
    // WYSIWYG: add fields to the schema and regenerate the YAML.
    const add = el("select", {}, ...["text", "textarea", "email", "number", "date", "select", "radio", "multiselect", "checkbox", "info"].map(x => el("option", { value: x }, x)));
    const addB = el("button", { className: "primary" }, "Add field"); addB.onclick = act(c, async () => {
      const s = schema || (await api("/admin/forms/convert", { method: "POST", body: { yaml: ta.value } })).schema, type = add.value;
      const nf = { id: "field_" + (s.fields.length + 1), type, label: "New " + type }; if (["select", "radio", "multiselect"].includes(type)) nf.options = ["Option 1", "Option 2"];
      s.fields.push(nf); ta.value = (await api("/admin/forms/convert", { method: "POST", body: { schema: s } })).yaml; preview();
    });
    const saveB = el("button", { className: "primary" }, "Save draft"); saveB.onclick = act(c, async () => { const r = await api(`/admin/forms/${id}/draft`, { method: "PUT", body: { yaml: ta.value, base_rev: rev } }); rev = r.rev; status.className = "ok"; status.textContent = "saved rev " + rev; });
    const pub = el("button", { className: "primary" }, "Publish"), unp = el("button", { className: "primary" }, "Unpublish");
    pub.onclick = act(c, async () => { await api(`/admin/forms/${id}/publish`, { method: "POST" }); status.textContent = "published"; });
    unp.onclick = act(c, async () => { await api(`/admin/forms/${id}/unpublish`, { method: "POST" }); status.textContent = "unpublished"; });
    c.append(add, addB, saveB, pub, unp);
    if (!isNew) { // realtime sync of edits made by other admins
      const es = new EventSource(`/api/admin/forms/${id}/events?token=${encodeURIComponent(token)}`);
      es.onmessage = ev => { const d = JSON.parse(ev.data); if (d.yaml && d.rev !== rev) { rev = d.rev; ta.value = d.yaml; preview(); status.textContent = "updated by another admin (rev " + d.rev + ")"; } };
      const stop = () => es.close(); nav.addEventListener("click", stop, { once: true });
    }
    const subs = await (isNew ? [] : api(`/admin/forms/${id}/submissions`));
    for (const s of subs) {
      const sel = el("select", {}, ...["submitted", "under_review", "needs_info", "accepted", "rejected"].map(x => el("option", { value: x, selected: x === s.status }, x))), note = el("input", { placeholder: "note" }), b = el("button", { className: "primary" }, "Set status");
      const cc = card(el("strong", {}, `${s.applicant.first_name} ${s.applicant.last_name} (${s.applicant.preferred_name}) ***${s.applicant.student_number}`), el("pre", {}, JSON.stringify(s.data, null, 2)), sel, note, b);
      b.onclick = act(cc, async () => { await api(`/admin/submissions/${id}/${s.uuid}/status`, { method: "POST", body: { status: sel.value, note: note.value } }); cc.append(msg("ok status", "updated")); });
    }
  }

  async function adminVerifications() {
    main.replaceChildren(); const list = await api("/admin/verifications");
    if (!list.length) card(el("p", {}, "Nothing pending."));
    for (const v of list) {
      const c = card(el("strong", {}, `${v.kind}: ${v.first_name} ${v.last_name}`), el("p", {}, "Divisions: " + v.division_codes.join(", ")), el("pre", {}, JSON.stringify(v.changes)));
      const show = el("button", { className: "primary" }, "Show scans"); show.onclick = act(c, async () => { const m = await api(`/admin/verifications/${v.id}/media`); c.append(el("div", { className: "row" }, el("img", { className: "shot", src: m.face }), el("img", { className: "shot", src: m.id_card }))); });
      const yes = el("button", { className: "primary" }, "Approve"), no = el("button", { className: "primary" }, "Reject");
      yes.onclick = act(c, async () => { await api(`/admin/verifications/${v.id}/decision`, { method: "POST", body: { approve: true } }); adminVerifications(); });
      no.onclick = act(c, async () => { await api(`/admin/verifications/${v.id}/decision`, { method: "POST", body: { approve: false } }); adminVerifications(); });
      c.append(show, yes, no);
    }
  }

  function recoveryView() {
    main.replaceChildren(); const c = card(el("h2", {}, "Account recovery"));
    const [l1, u] = field("Username", {}), [l2, sn] = field("Last 4 digits of student number", { maxLength: 4 });
    c.append(l1, u, l2, sn);
    const s = el("button", { className: "primary" }, "Email me a sign-in code (email on file)"); s.onclick = act(c, async () => { await api("/recovery/start", { method: "POST", body: { username: u.value, student_number_trunc: sn.value } }); c.append(msg("ok status", "If the details match, a code was sent. Sign in with it.")); });
    c.append(s, el("hr"), el("h3", {}, "Change email or legal name (requires face + ID scan)"));
    const [l3, ne] = field("New email (optional)", {}), [l4, otp] = field("Code sent to new email", {}), [l5, fn] = field("Legal first name", {}), [l6, ln] = field("Legal last name", {}), [l7, dv] = field("Division codes (comma separated)", {});
    const o = el("button", { className: "primary" }, "Send code to new email"); o.onclick = act(c, async () => { await api("/recovery/email-otp", { method: "POST", body: { username: u.value, student_number_trunc: sn.value, new_email: ne.value } }); c.append(msg("ok status", "Code sent")); });
    c.append(l3, ne, o, l4, otp, l5, fn, l6, ln, l7, dv); let w;
    const cam = el("button", { className: "primary" }, "Start camera"); cam.onclick = act(c, async () => { w = await scanWidget(c); });
    const go = el("button", { className: "primary" }, "Submit request"); go.onclick = act(c, async () => {
      await api("/recovery/request", { method: "POST", body: { username: u.value, student_number_trunc: sn.value, new_email: ne.value || null, otp: otp.value || null, first_name: fn.value, last_name: ln.value, division_codes: dv.value.split(",").map(x => x.trim()), face: w.shots.face, id_card: w.shots.id_card, challenge: w.challenge } });
      w.stop(); c.append(msg("ok status", "Submitted for administrator review."));
    });
    c.append(cam, go);
  }

  async function refreshMe() { try { me = await api("/me"); } catch (e) { token = null; sessionStorage.removeItem("token"); me = null; } }
  async function start() { if (token) await refreshMe(); setNav(); token ? (me.status === "verified" ? formsView() : accountView()) : authView(); }
  start();
})();

// Client-side form engine: renders a form from its static JSON definition and validates/sanitises
// with the same rules the server enforces. No innerHTML is used; all values go through textContent/value.
(function (root) {
  const CTRL = /[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f\u200b-\u200f\u202a-\u202e\u2066-\u2069]/g;
  function clean(s, max, multiline) {
    s = String(s).normalize("NFC");
    if (!multiline) s = s.replace(/\s+/g, " ");
    return s.replace(CTRL, "").trim().slice(0, max);
  }
  function visible(f, data) {
    const c = f.show_if; if (!c) return true;
    const v = data[c.field];
    return Array.isArray(v) ? v.includes(c.equals) : v === c.equals;
  }
  // returns {data, errors}
  function validate(schema, raw) {
    const data = {}, errors = {};
    for (const f of schema.fields) {
      if (f.type === "info" || !visible(f, data)) continue;
      let v = raw[f.id];
      const empty = v == null || v === "" || (Array.isArray(v) && !v.length) || (f.type === "checkbox" && v === false);
      if (empty) { if (f.required) errors[f.id] = "required"; else if (f.type === "checkbox") data[f.id] = false; continue; }
      const t = f.type;
      if (["text", "textarea", "email", "date", "select", "radio"].includes(t)) {
        v = clean(v, t === "textarea" ? 5000 : 500, t === "textarea");
        if ((t === "select" || t === "radio") && !f.options.includes(v)) errors[f.id] = "not an allowed option";
        else if (t === "email" && !/^[^@\s<>]{1,64}@[^@\s<>]{1,255}\.[^@\s<>]{2,}$/.test(v)) errors[f.id] = "invalid email";
        else if (t === "date" && !/^\d{4}-\d{2}-\d{2}$/.test(v)) errors[f.id] = "date must be YYYY-MM-DD";
        else if ((t === "text" || t === "textarea") && f.min != null && v.length < f.min) errors[f.id] = "at least " + f.min + " characters";
        else if ((t === "text" || t === "textarea") && f.max != null && v.length > f.max) errors[f.id] = "at most " + f.max + " characters";
        else if (f.pattern && !new RegExp("^(?:" + f.pattern + ")$").test(v)) errors[f.id] = "invalid format";
      } else if (t === "number") {
        v = Number(v);
        if (!Number.isFinite(v)) errors[f.id] = "must be a number";
        else if ((f.min != null && v < f.min) || (f.max != null && v > f.max)) errors[f.id] = "out of range";
      } else if (t === "multiselect") {
        if (!Array.isArray(v) || v.some(x => !f.options.includes(x))) errors[f.id] = "invalid selection";
      }
      if (!errors[f.id]) data[f.id] = v;
    }
    return { data, errors };
  }
  function el(tag, props, ...kids) {
    const e = document.createElement(tag);
    for (const [k, v] of Object.entries(props || {})) k in e ? (e[k] = v) : e.setAttribute(k, v);
    for (const k of kids) e.append(k);
    return e;
  }
  // Renders into `host`; returns {getValues, setErrors}. onChange fires on any edit (for live preview / conditionals).
  function render(host, schema, initial, onChange) {
    const values = Object.assign({}, initial || {}), wraps = {}, errs = {};
    host.replaceChildren();
    const refresh = () => schema.fields.forEach(f => { wraps[f.id].hidden = !visible(f, values); });
    const changed = (id, v) => { values[id] = v; refresh(); onChange && onChange(values); };
    for (const f of schema.fields) {
      const w = el("div"), lab = el("label", {}, f.label + (f.required ? " *" : ""));
      if (f.help) lab.append(el("div", { className: "help" }, f.help));
      let input;
      if (f.type === "info") { w.append(el("p", {}, f.label)); }
      else {
        w.append(lab);
        const cur = values[f.id];
        if (f.type === "textarea") input = el("textarea", { rows: 5, value: cur || "" });
        else if (f.type === "select") { input = el("select", {}, el("option", { value: "" }, "—"), ...f.options.map(o => el("option", { value: o }, o))); input.value = cur || ""; }
        else if (f.type === "radio") input = el("div", {}, ...f.options.map(o => { const r = el("input", { type: "radio", name: f.id, checked: cur === o }); r.onchange = () => changed(f.id, o); return el("label", { className: "opt" }, r, " " + o); }));
        else if (f.type === "multiselect") input = el("div", {}, ...f.options.map(o => { const c = el("input", { type: "checkbox", checked: (cur || []).includes(o) }); c.onchange = () => changed(f.id, f.options.filter(x => x === o ? c.checked : (values[f.id] || []).includes(x))); return el("label", { className: "opt" }, c, " " + o); }));
        else if (f.type === "checkbox") { input = el("input", { type: "checkbox", checked: !!cur }); input.onchange = () => changed(f.id, input.checked); }
        else input = el("input", { type: f.type === "number" ? "number" : f.type === "date" ? "date" : f.type === "email" ? "email" : "text", value: cur ?? "" });
        if (["INPUT", "TEXTAREA", "SELECT"].includes(input.tagName) && input.type !== "checkbox") input.oninput = () => changed(f.id, input.value);
        w.append(input);
        errs[f.id] = el("div", { className: "err" }); w.append(errs[f.id]);
      }
      wraps[f.id] = w; host.append(w);
    }
    refresh();
    return {
      getValues: () => validate(schema, values),
      setErrors: e => { for (const id in errs) errs[id].textContent = e[id] || ""; },
    };
  }
  root.PortalForms = { validate, render, clean, visible, el };
})(window);

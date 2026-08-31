async function api(path, opts) {
  const res = await fetch("/api" + path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch (e) {}
    throw new Error(detail);
  }
  return res.status === 204 ? null : res.json();
}

async function doLogin(ev) {
  ev.preventDefault();
  const f = ev.target;
  const err = document.getElementById("err");
  err.hidden = true;
  try {
    await api("/login", {
      method: "POST",
      body: JSON.stringify({ username: f.username.value, password: f.password.value }),
    });
    location.href = "/";
  } catch (e) {
    err.textContent = e.message;
    err.hidden = false;
  }
  return false;
}

async function logout() {
  try { await api("/logout", { method: "POST" }); } catch (e) {}
  location.href = "/login";
}

function reloadSoon() { setTimeout(() => location.reload(), 250); }

async function msgAction(id, action) {
  try { await api(`/messages/${id}/${action}`, { method: "POST" }); reloadSoon(); }
  catch (e) { alert(e.message); }
}
async function msgDelete(id) {
  if (!confirm("Delete message " + id + "? This removes it and its spooled body.")) return;
  try { await api(`/messages/${id}`, { method: "DELETE" }); location.href = "/messages"; }
  catch (e) { alert(e.message); }
}
async function providerAction(name, action) {
  try { await api(`/providers/${name}/${action}`, { method: "POST" }); reloadSoon(); }
  catch (e) { alert(e.message); }
}
async function createAccount(ev) {
  ev.preventDefault();
  const f = ev.target;
  try {
    await api("/accounts", {
      method: "POST",
      body: JSON.stringify({
        username: f.username.value,
        password: f.password.value,
        description: f.description.value || null,
      }),
    });
    reloadSoon();
  } catch (e) { alert(e.message); }
  return false;
}
async function acctToggle(id) {
  try { await api(`/accounts/${id}/toggle`, { method: "POST" }); reloadSoon(); }
  catch (e) { alert(e.message); }
}
async function acctDelete(id) {
  if (!confirm("Delete this account? Its services will no longer authenticate.")) return;
  try { await api(`/accounts/${id}`, { method: "DELETE" }); reloadSoon(); }
  catch (e) { alert(e.message); }
}
async function acctPassword(id) {
  const pw = prompt("New password (min 8 chars):");
  if (!pw) return;
  try {
    await api(`/accounts/${id}/password`, {
      method: "POST", body: JSON.stringify({ password: pw }),
    });
    alert("Password updated.");
  } catch (e) { alert(e.message); }
}

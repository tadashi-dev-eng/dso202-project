// services/web-frontend/src/api.js -- TaskFlow
// Thin client for the gateway. Every call goes to the same origin under
// /api; the access token rides in the Authorization header and is renewed
// with the refresh token once when a call returns 401.

const KEY = "taskflow.session";

export function loadSession() {
  try {
    return JSON.parse(localStorage.getItem(KEY)) || null;
  } catch {
    return null;
  }
}

function saveSession(session) {
  try {
    if (session) localStorage.setItem(KEY, JSON.stringify(session));
    else localStorage.removeItem(KEY);
  } catch {
    /* private window: session lives in memory only */
  }
}

let session = loadSession();
let onLogout = () => {};

export function setLogoutHandler(fn) {
  onLogout = fn;
}

export class ApiError extends Error {
  constructor(status, detail) {
    super(typeof detail === "string" ? detail : JSON.stringify(detail));
    this.status = status;
  }
}

async function refresh() {
  if (!session?.refresh_token) return false;
  const resp = await fetch("/api/auth/refresh", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ refresh_token: session.refresh_token }),
  });
  if (!resp.ok) return false;
  session = await resp.json();
  saveSession(session);
  return true;
}

export async function api(path, { method = "GET", body, raw = false, retry = true } = {}) {
  const headers = {};
  if (session?.access_token) headers.Authorization = `Bearer ${session.access_token}`;
  let payload = body;
  if (body && !(body instanceof FormData)) {
    headers["Content-Type"] = "application/json";
    payload = JSON.stringify(body);
  }
  const resp = await fetch(`/api${path}`, { method, headers, body: payload });
  if (resp.status === 401 && retry && !path.startsWith("/auth/")) {
    if (await refresh()) return api(path, { method, body, raw, retry: false });
    logout();
    throw new ApiError(401, "session expired");
  }
  if (!resp.ok) {
    let detail = resp.statusText;
    try {
      detail = (await resp.json()).detail ?? detail;
    } catch {
      /* not JSON */
    }
    throw new ApiError(resp.status, detail);
  }
  if (raw) return resp;
  if (resp.status === 204) return null;
  return resp.json();
}

export async function login(email, password) {
  session = await api("/auth/login", { method: "POST", body: { email, password } });
  saveSession(session);
  return session;
}

export async function register(email, password, display_name) {
  session = await api("/auth/register", {
    method: "POST",
    body: { email, password, display_name },
  });
  saveSession(session);
  return session;
}

export function logout() {
  if (session?.refresh_token) {
    fetch("/api/auth/logout", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ refresh_token: session.refresh_token }),
    }).catch(() => {});
  }
  session = null;
  saveSession(null);
  onLogout();
}

// Optional services: a feature panel that calls one of these and gets a
// 502/503/404-unknown-resource simply hides itself, so the UI works while
// a service is scaled to zero or not yet deployed.
export async function optional(path, fallback, opts) {
  try {
    return await api(path, opts);
  } catch (err) {
    if (err.status === 401) throw err;
    return fallback;
  }
}

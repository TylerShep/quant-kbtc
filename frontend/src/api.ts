const TOKEN_KEY = 'kbtc_api_token';

export function getToken(): string {
  return localStorage.getItem(TOKEN_KEY) || '';
}

export function setToken(t: string): void {
  if (t) localStorage.setItem(TOKEN_KEY, t);
  else localStorage.removeItem(TOKEN_KEY);
}

export function clearToken(): void {
  localStorage.removeItem(TOKEN_KEY);
}

function buildHeaders(init: RequestInit, token: string): Headers {
  const headers = new Headers(init.headers);
  if (token) headers.set('Authorization', `Bearer ${token}`);
  if (init.body && !headers.has('Content-Type')) headers.set('Content-Type', 'application/json');
  return headers;
}

/** Fetch wrapper for state-mutating dashboard calls. Attaches the bearer token
 * and re-prompts on 401 so the user can update an expired/wrong token without
 * a full page reload. */
export async function authedFetch(input: RequestInfo, init: RequestInit = {}): Promise<Response> {
  const token = getToken();
  const res = await fetch(input, { ...init, headers: buildHeaders(init, token) });
  if (res.status !== 401) return res;

  clearToken();
  const next = window.prompt('Dashboard API token (set DASHBOARD_API_TOKEN on the server):');
  if (!next) return res;
  setToken(next);
  return fetch(input, { ...init, headers: buildHeaders(init, next) });
}

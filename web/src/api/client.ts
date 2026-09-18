/** The single door to the backend.
 *
 *  Every /v1 route wants `Authorization: Bearer <token>`. A 401 means the
 *  stored token is wrong or gone, so it is thrown as a distinct error type
 *  that the app turns back into the sign-in gate rather than an error banner.
 */

const TOKEN_KEY = "seekr_token";
const LEGACY_TOKEN_KEY = "rip_token";

export function getToken(): string {
  return (
    localStorage.getItem(TOKEN_KEY) || localStorage.getItem(LEGACY_TOKEN_KEY) || ""
  );
}

export function setToken(value: string): void {
  localStorage.setItem(TOKEN_KEY, value.trim());
}

export function clearToken(): void {
  localStorage.removeItem(TOKEN_KEY);
  localStorage.removeItem(LEGACY_TOKEN_KEY);
}

export class UnauthorizedError extends Error {
  constructor() {
    super("unauthorized");
    this.name = "UnauthorizedError";
  }
}

/** Fires when any request is rejected, so the app can drop to the gate from
 *  wherever it happens to be. */
const unauthorizedListeners = new Set<() => void>();

export function onUnauthorized(fn: () => void): () => void {
  unauthorizedListeners.add(fn);
  return () => unauthorizedListeners.delete(fn);
}

/** A backend that is not running is the single most common failure in
 *  development, and it does not look like one from the browser: `npm run dev`
 *  proxies /v1 to the API, so a refused connection arrives as an opaque 500
 *  from Vite rather than a network error. Naming it costs one string compare
 *  and saves the half hour otherwise spent reading "request failed (500)". */
const BACKEND_DOWN =
  "Cannot reach the Seekr API. Start it with `python -m rip.cli serve` " +
  "(it listens on port 8000, which is where `npm run dev` forwards /v1).";

/** Vite answers a failed proxy hop with a bare 500 and an EMPTY body, while a
 *  genuine server error always carries one ("Internal Server Error" at
 *  minimum). That difference is the only signal available here, and it is a
 *  reliable one. */
function looksUnreachable(status: number, body: string): boolean {
  if (status < 500) return false;
  return body.trim() === "" || /ECONNREFUSED|ECONNRESET|EHOSTUNREACH|proxy error/i.test(body);
}

/** Does this deployment require a token? Nothing is sent, so the answer is
 *  the server's own: 200 means RIP_API_TOKEN is unset and the UI should not
 *  ask for one; 401 means it should. A backend that cannot be reached is not
 *  an answer — assume a token is wanted rather than showing the app as open. */
export async function authRequired(): Promise<boolean> {
  try {
    const res = await fetch("/v1/auth");
    return res.status === 401;
  } catch {
    return true;
  }
}

export async function api<T = unknown>(
  path: string,
  opts: RequestInit = {},
): Promise<T> {
  let res: Response;
  try {
    res = await fetch(path, {
      ...opts,
      // no header at all when there is no token: an empty "Bearer " is a
      // malformed credential, not the absence of one
      headers: {
        ...(opts.headers || {}),
        ...(getToken() ? { Authorization: "Bearer " + getToken() } : {}),
      },
    });
  } catch {
    // fetch only rejects on a transport failure — DNS, refused socket, CORS.
    throw new Error(BACKEND_DOWN);
  }

  if (res.status === 401) {
    unauthorizedListeners.forEach((fn) => fn());
    throw new UnauthorizedError();
  }

  if (!res.ok) {
    // Read the body once, as text: an error is not guaranteed to be JSON, and
    // the non-JSON ones are exactly the interesting failures.
    const body = await res.text().catch(() => "");
    if (looksUnreachable(res.status, body)) throw new Error(BACKEND_DOWN);
    let detail = "";
    try {
      const parsed = JSON.parse(body);
      detail =
        typeof parsed.detail === "string" ? parsed.detail : JSON.stringify(parsed.detail);
    } catch {
      detail = body.trim().slice(0, 300);
    }
    throw new Error(detail ? `${detail} (${res.status})` : `request failed (${res.status})`);
  }

  return (await res.json()) as T;
}

/** POST/DELETE with a JSON body, which is most of the write surface. */
export function apiSend<T = unknown>(
  path: string,
  method: "POST" | "DELETE",
  body?: unknown,
): Promise<T> {
  return api<T>(path, {
    method,
    ...(body === undefined
      ? {}
      : {
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        }),
  });
}

export function isUnauthorized(e: unknown): boolean {
  return e instanceof UnauthorizedError;
}

export function errorMessage(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}


/** Fetch a binary response (a PDF) with the bearer token in the header. */
export async function apiBlob(path: string): Promise<Blob> {
  const res = await fetch(path, { headers: { Authorization: `Bearer ${getToken()}` } });
  if (res.status === 401) {
    clearToken();
    throw new Error("unauthorized");
  }
  if (!res.ok) throw new Error(`request failed (${res.status})`);
  return res.blob();
}

/** Fetch a raw-text response (the HTML dossier) with the bearer token in the
 *  header. Cannot reuse api<T>(), which always parses JSON — this endpoint
 *  returns a complete standalone HTML document instead. */
export async function apiText(path: string): Promise<string> {
  const res = await fetch(path, { headers: { Authorization: `Bearer ${getToken()}` } });
  if (res.status === 401) {
    clearToken();
    throw new Error("unauthorized");
  }
  if (!res.ok) throw new Error(`request failed (${res.status})`);
  return res.text();
}

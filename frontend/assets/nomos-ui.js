/* =============================================================================
   Nomos · Shared UI helpers (vanilla ES module)
   -----------------------------------------------------------------------------
   Pure utility functions for formatting and a thin fetch wrapper. No framework,
   no global mutation beyond the toast stack inserted into <body>.

   Exports:
     formatTimestamp(iso)       → "23 Apr 2026, 18:04 UTC"
     formatRelative(iso)        → "3 minutes ago" | "2 days ago" | absolute date
     formatDecimalRate(decStr)  → "1.930%"
     formatHashShort(hex)       → "a3f2…b7e1"
     apiFetch(path, opts)       → { ok, status, data, error }
     toast(message, kind)       → void   (kind: "info" | "warn" | "error")
   ========================================================================== */

const MONTHS_SHORT = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                      "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

function _pad(n) { return n < 10 ? "0" + n : "" + n; }

/**
 * Format an ISO-8601 timestamp as "23 Apr 2026, 18:04 UTC".
 * Returns "—" for null/undefined/invalid.
 */
export function formatTimestamp(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return "—";
  const day = d.getUTCDate();
  const mon = MONTHS_SHORT[d.getUTCMonth()];
  const yr  = d.getUTCFullYear();
  const hh  = _pad(d.getUTCHours());
  const mm  = _pad(d.getUTCMinutes());
  return `${day} ${mon} ${yr}, ${hh}:${mm} UTC`;
}

/**
 * Format an ISO-8601 timestamp as a relative string: "just now",
 * "3 minutes ago", "2 days ago". Falls back to absolute date for >7 days.
 */
export function formatRelative(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return "—";
  const now = new Date();
  const deltaSec = Math.round((now.getTime() - d.getTime()) / 1000);

  if (deltaSec < 0) {
    // future timestamp — show absolute
    return formatTimestamp(iso);
  }
  if (deltaSec < 30)        return "just now";
  if (deltaSec < 60)        return `${deltaSec} seconds ago`;
  const min = Math.round(deltaSec / 60);
  if (min < 60)             return min === 1 ? "1 minute ago" : `${min} minutes ago`;
  const hr = Math.round(min / 60);
  if (hr < 24)              return hr === 1 ? "1 hour ago" : `${hr} hours ago`;
  const day = Math.round(hr / 24);
  if (day <= 7)             return day === 1 ? "1 day ago" : `${day} days ago`;
  return formatTimestamp(iso);
}

/**
 * Format a decimal-fraction string ("0.0193") as a percent ("1.930%").
 * Three decimal places. Returns "—" for invalid input.
 */
export function formatDecimalRate(decimalString) {
  if (decimalString === null || decimalString === undefined) return "—";
  const n = Number(decimalString);
  if (!Number.isFinite(n)) return "—";
  return (n * 100).toFixed(3) + "%";
}

/**
 * Format a 64-char SHA-256 hex digest as "a3f2…b7e1".
 * Returns the input unchanged for non-string / short input.
 */
export function formatHashShort(hex) {
  if (typeof hex !== "string") return "—";
  if (hex.length < 8) return hex;
  return hex.slice(0, 4) + "…" + hex.slice(-4);
}

/**
 * Thin fetch wrapper with JSON parsing, 5s timeout, and a uniform return
 * shape: { ok, status, data, error }. Never throws.
 */
export async function apiFetch(path, opts = {}) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), opts.timeoutMs || 5000);
  try {
    const res = await fetch(path, {
      method: opts.method || "GET",
      headers: { "Accept": "application/json", ...(opts.headers || {}) },
      body: opts.body,
      signal: controller.signal,
    });
    clearTimeout(timeout);
    let data = null;
    let error = null;
    const ct = res.headers.get("content-type") || "";
    if (ct.includes("application/json")) {
      try { data = await res.json(); }
      catch (e) { error = "INVALID_JSON"; }
    } else {
      data = await res.text();
    }
    if (!res.ok && data && typeof data === "object" && data.code) {
      error = data.code;
    } else if (!res.ok && !error) {
      error = "HTTP_" + res.status;
    }
    return { ok: res.ok, status: res.status, data, error };
  } catch (e) {
    clearTimeout(timeout);
    const aborted = e && e.name === "AbortError";
    return {
      ok: false,
      status: 0,
      data: null,
      error: aborted ? "TIMEOUT" : "NETWORK_ERROR",
    };
  }
}

/**
 * Show a transient toast at bottom-left. Auto-dismisses after 4s.
 *   kind ∈ "info" | "warn" | "error"
 */
export function toast(message, kind = "info") {
  let stack = document.querySelector(".toast-stack");
  if (!stack) {
    stack = document.createElement("div");
    stack.className = "toast-stack";
    document.body.appendChild(stack);
  }
  const el = document.createElement("div");
  el.className = "toast" + (kind === "warn"  ? " toast-warn"
                          : kind === "error" ? " toast-error" : "");
  el.textContent = message;
  stack.appendChild(el);
  setTimeout(() => {
    el.style.transition = "opacity .25s";
    el.style.opacity = "0";
    setTimeout(() => el.remove(), 280);
  }, 4000);
}

/**
 * DocketScan proxy — Cloudflare Worker.
 *
 * Holds the NVIDIA API key (so it never reaches the browser), calls a vision
 * model twice in parallel and reconciles the answers. The response mimics
 * the Python app's POST /api/scan contract, so the same UI works against
 * either backend.
 *
 * Deploy:
 *   cd worker
 *   npx wrangler login
 *   npx wrangler secret put NVIDIA_API_KEY     # paste nvapi-...
 *   npx wrangler deploy
 */

const NIM_URL = "https://integrate.api.nvidia.com/v1/chat/completions";
const MAX_BYTES = 6 * 1024 * 1024;
const NIM_TIMEOUT_MS = 45000;   // measured 8-12s; generous for queue spikes

const PROMPT = `You are a precise OCR engine reading a courier docket (shipping label).

Return ONLY minified JSON, no prose, no markdown:
{"courier":"<company name or null>","date":"<YYYY-MM-DD or null>","tracking_number":"<exact characters or null>"}

Rules:
- Transcribe the tracking number character by character exactly as printed.
- Never guess or complete a pattern. If a character is illegible use "?".
- The date is the docket/booking date, not a "since 1985" or terms date.
- Ignore any overlaid annotations, stickers or highlights added on top of the photo.`;

function cors(env, request) {
  const allowed = (env.ALLOWED_ORIGINS || "*").split(",").map((s) => s.trim()).filter(Boolean);
  const origin = request.headers.get("Origin") || "";
  const allow = allowed.includes("*") ? "*" : (allowed.includes(origin) ? origin : allowed[0] || "");
  return {
    "Access-Control-Allow-Origin": allow,
    "Access-Control-Allow-Methods": "POST, GET, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
    "Access-Control-Max-Age": "86400",
    "Vary": "Origin",
  };
}

const json = (body, headers, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", ...headers },
  });

function toBase64(buf) {
  const bytes = new Uint8Array(buf);
  let bin = "";
  const chunk = 0x8000;
  for (let i = 0; i < bytes.length; i += chunk) {
    bin += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
  }
  return btoa(bin);
}

async function askNim(env, b64, mime) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), NIM_TIMEOUT_MS);
  try {
  const res = await fetch(NIM_URL, {
    signal: ctrl.signal,
    method: "POST",
    headers: {
      Authorization: `Bearer ${env.NVIDIA_API_KEY}`,
      "Content-Type": "application/json",
      Accept: "application/json",
    },
    body: JSON.stringify({
      model: env.MODEL || "google/gemma-4-31b-it",
      messages: [{
        role: "user",
        content: [
          { type: "text", text: PROMPT },
          { type: "image_url", image_url: { url: `data:${mime};base64,${b64}` } },
        ],
      }],
      temperature: 0,
      max_tokens: 2500,   // reasoning models need headroom before emitting JSON
    }),
  });

  if (!res.ok) {
    const detail = (await res.text()).slice(0, 300);
    throw new Error(`NIM ${res.status}: ${detail}`);
  }
  const payload = await res.json();
  const msg = payload?.choices?.[0]?.message || {};
  // reasoning models put the answer in `content`, thinking in `reasoning_content`
  return String(msg.content || msg.reasoning_content || "");
  } finally {
    clearTimeout(timer);
  }
}

function extractJson(text) {
  const fenced = text.match(/```(?:json)?\s*([\s\S]*?)```/i);
  const body = fenced ? fenced[1] : text;
  const start = body.indexOf("{");
  const end = body.lastIndexOf("}");
  if (start === -1 || end === -1 || end <= start) return {};
  try {
    return JSON.parse(body.slice(start, end + 1));
  } catch {
    return {};
  }
}

const cleanTracking = (v) =>
  String(v ?? "").toUpperCase().replace(/[^A-Z0-9?]/g, "").slice(0, 24);

function normDate(v) {
  const s = String(v ?? "").trim();
  const iso = s.match(/^(\d{4})-(\d{2})-(\d{2})$/);
  if (iso) return s;
  const dmy = s.match(/^(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{2,4})$/);
  if (dmy) {
    let [, d, m, y] = dmy.map(Number);
    if (y < 100) y += y <= 40 ? 2000 : 1900;
    const dt = new Date(Date.UTC(y, m - 1, d));
    if (dt.getUTCFullYear() === y && dt.getUTCMonth() === m - 1 && dt.getUTCDate() === d) {
      return `${y}-${String(m).padStart(2, "0")}-${String(d).padStart(2, "0")}`;
    }
  }
  return null;
}

/** Reconcile N independent readings of the same docket. */
function reconcileAll(samples, elapsedMs, filename) {
  const norm = samples.map((s) => ({
    courier: String(s.courier || "").trim(),
    date: normDate(s.date),
    tracking: cleanTracking(s.tracking_number),
  }));

  // ---- tracking: majority over the samples that produced an answer --------
  const tracks = norm.map((s) => s.tracking).filter(Boolean);
  const counts = {};
  for (const t of tracks) counts[t] = (counts[t] || 0) + 1;
  let value = null;
  let bestCount = 0;
  for (const [t, c] of Object.entries(counts)) {
    if (c > bestCount || (c === bestCount && value && t.length > value.length)) {
      value = t;
      bestCount = c;
    }
  }

  let uncertain = [];
  const suggestions = [];
  let confidence = 0.0;
  if (value) {
    if (tracks.length >= 2 && bestCount >= 2) {
      confidence = bestCount === tracks.length ? 0.92 : 0.75;
      for (const t of tracks) {
        if (t === value || t.length !== value.length) continue;
        const diff = [...value].map((c, i) => (c === t[i] ? -1 : i)).filter((i) => i >= 0);
        if (diff.length) {
          if (!suggestions.includes(t)) suggestions.push(t);
          for (const i of diff) if (!uncertain.includes(i)) uncertain.push(i);
        }
      }
      if (uncertain.length) confidence = Math.min(confidence, 0.65);
    } else {
      confidence = 0.45;            // only one sample answered
    }
  }
  uncertain.sort((a, b) => a - b);

  // ---- courier / date: majority ------------------------------------------
  const mode = (list) => {
    const c = {};
    for (const v of list) if (v) c[v] = (c[v] || 0) + 1;
    let best = null;
    let n = 0;
    for (const [v, k] of Object.entries(c)) if (k > n) { best = v; n = k; }
    return [best, n];
  };

  const [courier, courierVotes] = mode(norm.map((s) => s.courier));
  const [date, dateVotes] = mode(norm.map((s) => s.date));
  const answered = norm.filter((s) => s.tracking || s.courier || s.date).length || 1;

  const needsReview = [];
  if (confidence < 0.9 || uncertain.length) needsReview.push("tracking");
  if (!date || dateVotes < 2) needsReview.push("date");
  if (!courier || courierVotes < 2) needsReview.push("courier");

  return {
    filename: filename || "docket",
    ok: true,
    courier: {
      value: courier,
      confidence: courier ? Math.min(0.95, 0.5 + (courierVotes / answered) * 0.45) : 0.0,
      source: "nim",
    },
    date: {
      value: date,
      raw: norm.find((s) => s.date)?.date || null,
      confidence: date ? Math.min(0.95, 0.5 + (dateVotes / answered) * 0.45) : 0.0,
      label_nearby: true,
    },
    tracking: {
      value,
      confidence,
      agreement: tracks.length ? bestCount / tracks.length : 0,
      uncertain_positions: uncertain,
      suggestions: suggestions.filter((s) => s !== value),
      readings: tracks,
      alternatives: [],
    },
    amount: null,
    needs_review: needsReview,
    ocr_ms: elapsedMs,
    line_count: 0,
    raw_text: tracks.map((r, i) => `reading ${i + 1}: ${r}`).join("\n"),
    engine: "nvidia-nim",
    samples_answered: tracks.length,
  };
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const headers = cors(env, request);

    if (request.method === "OPTIONS") return new Response(null, { status: 204, headers });

    if (url.pathname === "/api/health") {
      return json({ ok: true, engine: "nvidia-nim", model: env.MODEL || "google/gemma-4-31b-it", auth_required: false }, headers);
    }

    if (url.pathname === "/api/scan" && request.method === "POST") {
      if (!env.NVIDIA_API_KEY) return json({ ok: false, error: "NVIDIA_API_KEY secret is not set" }, headers, 500);

      const ctype = request.headers.get("Content-Type") || "";
      if (!ctype.startsWith("image/")) {
        return json({ ok: false, error: "send the raw image body with an image/* Content-Type" }, headers, 415);
      }
      const buf = await request.arrayBuffer();
      if (buf.byteLength === 0) return json({ ok: false, error: "empty body" }, headers, 400);
      if (buf.byteLength > MAX_BYTES) return json({ ok: false, error: "image larger than 6 MB" }, headers, 413);

      const started = Date.now();
      const b64 = toBase64(buf);
      try {
        // three independent samples in parallel; a truncated reasoning reply
        // simply loses a vote instead of degrading the whole result
        const settled = await Promise.allSettled([
          askNim(env, b64, ctype).then(extractJson),
          askNim(env, b64, ctype).then(extractJson),
          askNim(env, b64, ctype).then(extractJson),
        ]);
        const samples = settled
          .filter((r) => r.status === "fulfilled" && r.value && Object.keys(r.value).length)
          .map((r) => r.value);
        if (!samples.length) {
          const why = settled
            .map((r) => (r.status === "rejected" ? String(r.reason?.message || r.reason) : "no JSON in reply"))
            .join(" | ");
          return json({ ok: false, error: `vision model failed: ${why}` }, headers, 502);
        }
        const result = reconcileAll(samples, Date.now() - started, url.searchParams.get("filename"));
        return json({ ok: true, count: 1, results: [result] }, headers);
      } catch (err) {
        return json({ ok: false, error: String(err && err.message || err) }, headers, 502);
      }
    }

    return json({ ok: false, error: "not found" }, headers, 404);
  },
};

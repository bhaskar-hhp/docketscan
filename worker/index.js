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
  const res = await fetch(NIM_URL, {
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
      max_tokens: 1200,
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

/** Reconcile two independent readings of the same docket. */
function reconcile(a, b, elapsedMs, filename) {
  const ta = cleanTracking(a.tracking_number);
  const tb = cleanTracking(b.tracking_number);
  const readings = [ta, tb].filter(Boolean);

  let value = ta || tb || null;
  let uncertain = [];
  let suggestions = [];
  let confidence = 0.0;

  if (ta && tb) {
    if (ta === tb) {
      confidence = 0.92;                       // two independent calls agree
    } else if (ta.length === tb.length) {
      uncertain = [...ta].map((c, i) => (c === tb[i] ? -1 : i)).filter((i) => i >= 0);
      confidence = 0.55;
      suggestions = [tb];
      value = ta;                              // first reading is the default
    } else {
      confidence = 0.4;
      suggestions = [tb];
    }
  } else if (value) {
    confidence = 0.45;                         // only one call produced an answer
  }

  const courierA = (a.courier || "").trim();
  const courierB = (b.courier || "").trim();
  const courier = courierA || courierB || null;
  const courierAgree = courierA && courierB && courierA.toLowerCase() === courierB.toLowerCase();

  const dateA = normDate(a.date);
  const dateB = normDate(b.date);
  const date = dateA || dateB;
  const dateAgree = dateA && dateB && dateA === dateB;

  const needsReview = [];
  if (confidence < 0.9 || uncertain.length) needsReview.push("tracking");
  if (!date || !dateAgree) needsReview.push("date");
  if (!courier || !courierAgree) needsReview.push("courier");

  return {
    filename: filename || "docket",
    ok: true,
    courier: {
      value: courier,
      confidence: courierAgree ? 0.92 : (courier ? 0.6 : 0.0),
      source: "nim",
    },
    date: {
      value: date,
      raw: a.date || b.date || null,
      confidence: dateAgree ? 0.92 : (date ? 0.6 : 0.0),
      label_nearby: true,
    },
    tracking: {
      value,
      confidence: confidence,
      agreement: ta && tb ? (ta === tb ? 1 : 0.5) : 0.5,
      uncertain_positions: uncertain,
      suggestions: suggestions.filter((s) => s && s !== value),
      readings,
      alternatives: [],
    },
    amount: null,
    needs_review: needsReview,
    ocr_ms: elapsedMs,
    line_count: 0,
    raw_text: readings.map((r, i) => `reading ${i + 1}: ${r}`).join("\n"),
    engine: "nvidia-nim",
    model: a.__model || null,
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
        const [a, b] = await Promise.all([
          askNim(env, b64, ctype).then(extractJson),
          askNim(env, b64, ctype).then(extractJson),
        ]);
        const result = reconcile(a, b, Date.now() - started, url.searchParams.get("filename"));
        return json({ ok: true, count: 1, results: [result] }, headers);
      } catch (err) {
        return json({ ok: false, error: String(err && err.message || err) }, headers, 502);
      }
    }

    return json({ ok: false, error: "not found" }, headers, 404);
  },
};

const http = require("http");
const https = require("https");
const crypto = require("crypto");
const fs = require("fs");
const path = require("path");

// ─── Config ──────────────────────────────────────────────────────────────────
const GEMINI_API_KEY   = process.env.GEMINI_API_KEY   || "";
const DEEPSEEK_API_KEY = process.env.DEEPSEEK_API_KEY || "";
const AUTH_USER        = process.env.AUTH_USER        || "admin";
const AUTH_PASSWORD    = process.env.AUTH_PASSWORD    || "changeme";
const MEALIE_BASE      = "http://192.168.10.30:9925";
const MEALIE_TOKEN     = process.env.MEALIE_TOKEN || "";
const PORT             = 3100;
const SESSION_TTL_MS   = 8 * 60 * 60 * 1000;

// ─── Session store ────────────────────────────────────────────────────────────
const sessions = new Map();

function createSession() {
  const id = crypto.randomBytes(32).toString("hex");
  sessions.set(id, { createdAt: Date.now() });
  return id;
}

function isValidSession(id) {
  if (!id) return false;
  const s = sessions.get(id);
  if (!s) return false;
  if (Date.now() - s.createdAt > SESSION_TTL_MS) { sessions.delete(id); return false; }
  return true;
}

function getSessionId(req) {
  const match = (req.headers.cookie || "").match(/session=([a-f0-9]{64})/);
  return match ? match[1] : null;
}

// ─── Helpers ──────────────────────────────────────────────────────────────────
function readBody(req) {
  return new Promise((resolve) => {
    let data = "";
    req.on("data", (c) => (data += c));
    req.on("end", () => resolve(data));
  });
}

function jsonResp(res, status, obj, extraHeaders = {}) {
  res.writeHead(status, { "Content-Type": "application/json", "Access-Control-Allow-Origin": "*", ...extraHeaders });
  res.end(JSON.stringify(obj));
}

function redirect(res, location, extraHeaders = {}) {
  res.writeHead(302, { Location: location, ...extraHeaders });
  res.end();
}

function serveLoginPage(res) {
  const html = fs.readFileSync(path.join(__dirname, "login.html"), "utf8");
  res.writeHead(200, { "Content-Type": "text/html" });
  res.end(html);
}

// ─── AI helpers ───────────────────────────────────────────────────────────────
function httpsPost(hostname, urlPath, headers, payload) {
  return new Promise((resolve, reject) => {
    const data = JSON.stringify(payload);
    const req = https.request(
      { hostname, path: urlPath, method: "POST",
        headers: { "Content-Type": "application/json", "Content-Length": Buffer.byteLength(data), ...headers } },
      (res) => {
        let body = "";
        res.on("data", (c) => (body += c));
        res.on("end", () => {
          try { resolve({ status: res.statusCode, body: JSON.parse(body) }); }
          catch { resolve({ status: res.statusCode, body }); }
        });
      }
    );
    req.on("error", reject);
    req.write(data);
    req.end();
  });
}

async function handleGemini(body) {
  const model = body.model || "gemini-2.5-flash-lite";
  const urlPath = `/v1beta/models/${model}:generateContent?key=${GEMINI_API_KEY}`;
  const payload = {
    contents: body.messages.map((m) => ({
      role: m.role === "assistant" ? "model" : "user",
      parts: [{ text: m.content }],
    })),
    generationConfig: { temperature: body.temperature ?? 0.7, maxOutputTokens: body.max_tokens ?? 8192 },
  };
  const res = await httpsPost("generativelanguage.googleapis.com", urlPath, {}, payload);
  if (res.status !== 200) return { error: res.body, status: res.status };
  return { content: res.body?.candidates?.[0]?.content?.parts?.[0]?.text || "" };
}

async function handleDeepSeek(body) {
  const payload = {
    model: body.model || "deepseek-v4-flash",
    messages: body.messages,
    temperature: body.temperature ?? 0.7,
    max_tokens: body.max_tokens ?? 8192,
  };
  const res = await httpsPost("api.deepseek.com", "/v1/chat/completions",
    { Authorization: `Bearer ${DEEPSEEK_API_KEY}` }, payload);
  if (res.status !== 200) return { error: res.body, status: res.status };
  return { content: res.body?.choices?.[0]?.message?.content || "" };
}

// ─── Mealie proxy ─────────────────────────────────────────────────────────────
// Repassa qualquer chamada /mealie/* para o Mealie interno
// O token vem do env ou do header X-Mealie-Token enviado pelo cliente
function mealieProxy(req, res, mealieToken) {
  // req.url preserva query string completa
  const mealiePathFull = req.url.replace(/^\/mealie/, "");
  console.log(`[Mealie proxy] ${req.method} ${mealiePathFull}`);
  const url = new URL(MEALIE_BASE + mealiePathFull);

  return new Promise((resolve) => {
    const options = {
      hostname: url.hostname,
      port: url.port || 80,
      path: url.pathname + url.search,
      method: req.method,
      headers: {
        "Authorization": "Bearer " + mealieToken,
        "Content-Type": "application/json",
        "Accept": "application/json",
      },
    };

    const proxyReq = http.request(options, (proxyRes) => {
      let body = "";
      proxyRes.on("data", (c) => (body += c));
      proxyRes.on("end", () => {
        res.writeHead(proxyRes.statusCode, {
          "Content-Type": "application/json",
          "Access-Control-Allow-Origin": "*",
        });
        res.end(body);
        resolve();
      });
    });

    proxyReq.on("error", (e) => {
      res.writeHead(502, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ error: "Mealie unreachable: " + e.message }));
      resolve();
    });

    // Repassa o body para métodos que têm payload
    if (["POST", "PUT", "PATCH"].includes(req.method)) {
      readBody(req).then((data) => {
        proxyReq.write(data);
        proxyReq.end();
      });
    } else {
      proxyReq.end();
    }
  });
}

// ─── Server ───────────────────────────────────────────────────────────────────
const server = http.createServer(async (req, res) => {
  const url = req.url.split("?")[0];

  // CORS preflight
  if (req.method === "OPTIONS") {
    res.writeHead(204, {
      "Access-Control-Allow-Origin": "*",
      "Access-Control-Allow-Methods": "POST, GET, PUT, DELETE, OPTIONS",
      "Access-Control-Allow-Headers": "Content-Type, X-Mealie-Token",
    });
    return res.end();
  }

  // Health — sem auth
  if (url === "/health") return jsonResp(res, 200, { status: "ok" });

  // Login page — sem auth
  if (req.method === "GET" && url === "/login") return serveLoginPage(res);

  // Auth: login
  if (req.method === "POST" && url === "/auth/login") {
    const raw = await readBody(req);
    let body;
    try { body = JSON.parse(raw); } catch { return jsonResp(res, 400, { error: "Invalid JSON" }); }
    if (body.username === AUTH_USER && body.password === AUTH_PASSWORD) {
      const sid = createSession();
      return jsonResp(res, 200, { ok: true }, {
        "Set-Cookie": `session=${sid}; HttpOnly; SameSite=Strict; Path=/; Max-Age=${SESSION_TTL_MS / 1000}`,
      });
    }
    return jsonResp(res, 401, { error: "Credenciais inválidas" });
  }

  // Auth: logout
  if (req.method === "POST" && url === "/auth/logout") {
    const sid = getSessionId(req);
    if (sid) sessions.delete(sid);
    return jsonResp(res, 200, { ok: true }, { "Set-Cookie": "session=; HttpOnly; Path=/; Max-Age=0" });
  }

  // Auth: check
  if (req.method === "GET" && url === "/auth/check") {
    return jsonResp(res, 200, { authenticated: isValidSession(getSessionId(req)) });
  }

  // ── Guard: rotas protegidas ───────────────────────────────────────────────
  if (!isValidSession(getSessionId(req))) {
    if (url.startsWith("/api/") || url.startsWith("/mealie/")) {
      return jsonResp(res, 401, { error: "Não autenticado" });
    }
    return redirect(res, "/login");
  }

  // Já logado tentando acessar /login
  if (url === "/login") return redirect(res, "/");

  // ── Mealie proxy ──────────────────────────────────────────────────────────
  if (url.startsWith("/mealie/")) {
    const token = req.headers["x-mealie-token"] || MEALIE_TOKEN;
    return mealieProxy(req, res, token);
  }

  // ── API: Gemini ───────────────────────────────────────────────────────────
  if (req.method === "POST" && url === "/api/gemini") {
    if (!GEMINI_API_KEY) return jsonResp(res, 500, { error: "GEMINI_API_KEY não configurada" });
    const raw = await readBody(req);
    let body;
    try { body = JSON.parse(raw); } catch { return jsonResp(res, 400, { error: "Invalid JSON" }); }
    try {
      const result = await handleGemini(body);
      return jsonResp(res, result.error ? result.status || 500 : 200, result);
    } catch (e) { return jsonResp(res, 500, { error: e.message }); }
  }

  // ── API: DeepSeek ─────────────────────────────────────────────────────────
  if (req.method === "POST" && url === "/api/deepseek") {
    if (!DEEPSEEK_API_KEY) return jsonResp(res, 500, { error: "DEEPSEEK_API_KEY não configurada" });
    const raw = await readBody(req);
    let body;
    try { body = JSON.parse(raw); } catch { return jsonResp(res, 400, { error: "Invalid JSON" }); }
    try {
      const result = await handleDeepSeek(body);
      return jsonResp(res, result.error ? result.status || 500 : 200, result);
    } catch (e) { return jsonResp(res, 500, { error: e.message }); }
  }

  jsonResp(res, 404, { error: "Not found" });
});

server.listen(PORT, () => {
  console.log(`AI Proxy running on port ${PORT}`);
  console.log(`User: ${AUTH_USER}`);
  console.log(`Gemini: ${GEMINI_API_KEY ? "configured" : "NOT configured"}`);
  console.log(`DeepSeek: ${DEEPSEEK_API_KEY ? "configured" : "NOT configured"}`);
  console.log(`Mealie: ${MEALIE_TOKEN ? "token configured" : "token via header"}`);
});
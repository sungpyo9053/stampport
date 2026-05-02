// Production: served from https://reviewdr.kr/stampport-control/ — the API
// lives behind nginx at /stampport-control-api/ on the same origin.
// Dev: hit the local FastAPI on :8000 directly so devs don't need nginx.
const API_BASE = import.meta.env.PROD
  ? "/stampport-control-api/"
  : "http://localhost:8000/";

function apiUrl(path) {
  const cleanPath = String(path || "").replace(/^\/+/, "");
  return `${API_BASE}${cleanPath}`;
}

async function parseJsonResponse(res, url) {
  const contentType = res.headers.get("content-type") || "";

  if (!contentType.includes("application/json")) {
    const body = await res.text();
    throw new Error(
      `API returned non-JSON. url=${url}, status=${res.status}, contentType=${contentType}, body=${body.slice(0, 120)}`
    );
  }

  if (!res.ok) {
    const body = await res.text();
    throw new Error(
      `API request failed. url=${url}, status=${res.status}, body=${body.slice(0, 120)}`
    );
  }

  return res.json();
}

async function getJson(path) {
  const url = apiUrl(path);
  const res = await fetch(url, {
    headers: { Accept: "application/json" },
  });
  return parseJsonResponse(res, url);
}

async function postJson(path, body = {}) {
  const url = apiUrl(path);
  const res = await fetch(url, {
    method: "POST",
    headers: {
      Accept: "application/json",
      "Content-Type": "application/json",
    },
    body: JSON.stringify(body),
  });
  return parseJsonResponse(res, url);
}

export const fetchAgents = () => getJson("/agents");
export const fetchTasks = () => getJson("/tasks");
export const fetchEvents = (limit = 200) => getJson(`/events?limit=${limit}`);
export const runDemo = () => postJson("/tasks/run-demo");
export const resetDemo = () => {
  const url = apiUrl("/demo/reset");
  return fetch(url, {
    method: "DELETE",
    headers: { Accept: "application/json" },
  }).then((res) => parseJsonResponse(res, url));
};

export const fetchFactoryStatus = () => getJson("/factory/status");
export const fetchFactoryEvents = (limit = 30) =>
  getJson(`/factory/events?limit=${limit}`);
export const startFactory = () => postJson("/factory/start");
export const pauseFactory = () => postJson("/factory/pause");
export const resumeFactory = () => postJson("/factory/resume");
export const stopFactory = () => postJson("/factory/stop");
export const resetFactory = () => postJson("/factory/reset");
export const setDesiredFactoryStatus = (desired_status) =>
  postJson("/factory/desired", { desired_status });
export const setContinuousMode = (enabled) =>
  postJson("/factory/continuous", { enabled });

// Backward-compatible name used by ControlDock.jsx
export const setFactoryContinuous = setContinuousMode;

export const fetchRunners = () => getJson("/runners/");
export const sendRunnerCommand = (runnerId, command, payload = {}) =>
  postJson(`/runners/${encodeURIComponent(runnerId)}/commands`, {
    command,
    payload,
  });

export const startAutopilot = (runnerId, payload = {}) =>
  sendRunnerCommand(runnerId, "start_autopilot", payload);

export const stopAutopilot = (runnerId, reason = "operator stop") =>
  sendRunnerCommand(runnerId, "stop_autopilot", { reason });

export { API_BASE };

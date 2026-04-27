// Production: served from https://reviewdr.kr/stampport-control/ — the API
// lives behind nginx at /stampport-control-api/ on the same origin.
// Dev: hit the local FastAPI on :8000 directly so devs don't need nginx.
const API_BASE = import.meta.env.PROD
  ? "/stampport-control-api"
  : "http://localhost:8000";

async function getJson(path) {
  const res = await fetch(`${API_BASE}${path}`, { headers: { Accept: "application/json" } });
  if (!res.ok) {
    throw new Error(`GET ${path} failed: ${res.status}`);
  }
  return res.json();
}

async function postJson(path, body) {
  const res = await fetch(`${API_BASE}${path}`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Accept: "application/json",
    },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    throw new Error(`POST ${path} failed: ${res.status}`);
  }
  return res.json();
}

async function deleteJson(path) {
  const res = await fetch(`${API_BASE}${path}`, {
    method: "DELETE",
    headers: { Accept: "application/json" },
  });
  if (!res.ok) {
    throw new Error(`DELETE ${path} failed: ${res.status}`);
  }
  return res.json();
}

export const fetchAgents = () => getJson("/agents");
export const fetchTasks = () => getJson("/tasks");
export const fetchEvents = (limit = 200) => getJson(`/events?limit=${limit}`);
export const runDemo = () => postJson("/tasks/run-demo");
export const resetDemo = () => deleteJson("/demo/reset");

// Factory lifecycle
export const fetchFactoryStatus = () => getJson("/factory/status");
export const fetchFactoryEvents = (limit = 30) =>
  getJson(`/factory/events?limit=${limit}`);
export const startFactory  = () => postJson("/factory/start");
export const pauseFactory  = () => postJson("/factory/pause");
export const resumeFactory = () => postJson("/factory/resume");
export const stopFactory   = () => postJson("/factory/stop");
export const resetFactory  = () => postJson("/factory/reset");
export const setFactoryDesired = (desired_status) =>
  postJson("/factory/desired", { desired_status });
export const setFactoryContinuous = (enabled) =>
  postJson("/factory/continuous", { enabled });

// Runners
export const fetchRunners = () => getJson("/runners/");
export const sendRunnerCommand = (runnerId, command, payload = {}) =>
  postJson(`/runners/${encodeURIComponent(runnerId)}/commands`, {
    command,
    payload,
  });

export { API_BASE };

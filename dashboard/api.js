// ─── Auth ───────────────────────────────────────────────────────────────────
// server.py (Fase 0) requires X-Agentic-Token on every /api/* endpoint except
// /api/status. The token is kept in sessionStorage rather than localStorage so
// it does not persist on disk and dies when the tab closes.
const TOKEN_KEY = 'agentic_token';

function getToken() {
  return sessionStorage.getItem(TOKEN_KEY) || '';
}

function setToken(value) {
  if (value) sessionStorage.setItem(TOKEN_KEY, value);
  else sessionStorage.removeItem(TOKEN_KEY);
}

function authHeaders(extra = {}) {
  const headers = { ...extra };
  const token = getToken();
  if (token) headers['X-Agentic-Token'] = token;
  return headers;
}

function RateLimitError(retryAfter) {
  this.name = 'RateLimitError';
  this.retryAfter = retryAfter;
  this.message = `Rate limit exceeded. Retry in ${retryAfter}s.`;
}
RateLimitError.prototype = Object.create(Error.prototype);

function AuthError(message) {
  this.name = 'AuthError';
  this.message = message;
}
AuthError.prototype = Object.create(Error.prototype);

/**
 * Handle a non-OK response uniformly: surface 429 as a RateLimitError carrying
 * Retry-After so callers can show a countdown, and 401 as an AuthError so the
 * UI can prompt for the token instead of rendering a generic failure.
 */
async function handleErrorResponse(r) {
  if (r.status === 429) {
    const retryAfter = parseInt(r.headers.get('Retry-After') || '30', 10);
    throw new RateLimitError(retryAfter);
  }
  if (r.status === 401) {
    const e = await r.json().catch(() => ({}));
    throw new AuthError(e.detail || 'Unauthorized — API token required.');
  }
  const e = await r.json().catch(() => ({}));
  throw new Error(e.detail || `Request failed: ${r.status}`);
}

const api = {
  async get(path) {
    const r = await fetch(path, { headers: authHeaders() });
    if (!r.ok) await handleErrorResponse(r);
    return r.json();
  },
  async post(path, body = {}, controller) {
    const opts = {
      method: 'POST',
      headers: authHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify(body),
    };
    if (controller) opts.signal = controller.signal;
    const r = await fetch(path, opts);
    if (!r.ok) await handleErrorResponse(r);
    return r.json();
  },
  async put(path, body = {}) {
    const r = await fetch(path, {
      method: 'PUT',
      headers: authHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify(body),
    });
    if (!r.ok) await handleErrorResponse(r);
    return r.json();
  },
  async patch(path, body = {}) {
    const r = await fetch(path, {
      method: 'PATCH',
      headers: authHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify(body),
    });
    if (!r.ok) await handleErrorResponse(r);
    return r.json();
  },
  async del(path) {
    const r = await fetch(path, { method: 'DELETE', headers: authHeaders() });
    if (!r.ok) await handleErrorResponse(r);
    return r.json();
  },
  getStatus: () => api.get('/api/status'),
  getBrain: () => api.get('/api/brain'),
  getBrainFile: (name) => api.get(`/api/brain/${encodeURIComponent(name)}`),
  updateBrainFile: (name, content) => api.put(`/api/brain/${encodeURIComponent(name)}`, { content }),
  getSkills: () => api.get('/api/skills'),
  getSkill: (name) => api.get(`/api/skills/${encodeURIComponent(name)}`),
  runSkill: (name, input = '', agent = 'auto') => api.post(`/api/skills/${encodeURIComponent(name)}/run`, { input, agent }),
  getSkillEval: (name) => api.get(`/api/skills/${encodeURIComponent(name)}/eval`),
  getJobs: () => api.get('/api/scheduler/jobs'),
  createJob: (job) => api.post('/api/scheduler/jobs', job),
  deleteJob: (id) => api.del(`/api/scheduler/jobs/${encodeURIComponent(id)}`),
  getAudit: (limit = 100) => api.get(`/api/audit?limit=${limit}`),
  getCost: () => api.get('/api/cost'),
  recordCost: (data) => api.post('/api/cost/record', data),
  getPlugins: () => api.get('/api/plugins'),
  installPlugin: (name) => api.post('/api/plugins/install', { name }),
  getBackups: () => api.get('/api/backups'),
  createBackup: () => api.post('/api/backup'),
  restoreBackup: (file) => api.post('/api/backup/restore', { file }),
  getPrompts: () => api.get('/api/prompts'),
  getSettings: () => api.get('/api/settings'),
  updateSettings: (settings) => api.put('/api/settings', { settings }),
  getStandards: () => api.get('/api/standards'),
  discoverStandards: () => api.post('/api/standards/discover'),
  chat: (agent, message, controller) => api.post('/api/chat', { agent, message }, controller),
  chatWithFile: async (agent, message, file, controller) => {
    const form = new FormData();
    form.append('agent', agent);
    form.append('message', message || '');
    form.append('file', file);
    const opts = { method: 'POST', body: form, headers: authHeaders() };
    if (controller) opts.signal = controller.signal;
    const r = await fetch('/api/chat/upload', opts);
    if (!r.ok) await handleErrorResponse(r);
    return r.json();
  },
  getChatHistory: () => api.get('/api/chat/history'),
  searchChatHistory: (q, agent, limit) => {
    const params = new URLSearchParams();
    if (q) params.set('q', q);
    if (agent) params.set('agent', agent);
    if (limit) params.set('limit', limit);
    return api.get(`/api/chat/history?${params.toString()}`);
  },
  // Kanban
  getKanbanBoard: (status) => api.get(status ? `/api/kanban/board?status=${encodeURIComponent(status)}` : '/api/kanban/board'),
  getKanbanTask: (id) => api.get(`/api/kanban/tasks/${encodeURIComponent(id)}`),
  createKanbanTask: (data) => api.post('/api/kanban/tasks', data),
  updateKanbanTask: (id, data) => api.patch(`/api/kanban/tasks/${encodeURIComponent(id)}`, data),
  completeKanbanTask: (id, summary) => api.post(`/api/kanban/tasks/${encodeURIComponent(id)}/complete`, { summary }),
  blockKanbanTask: (id, reason) => api.post(`/api/kanban/tasks/${encodeURIComponent(id)}/block`, { reason }),
  unblockKanbanTask: (id) => api.post(`/api/kanban/tasks/${encodeURIComponent(id)}/unblock`, {}),
  addKanbanComment: (id, message) => api.post(`/api/kanban/tasks/${encodeURIComponent(id)}/comments`, { message }),
  linkKanbanTasks: (parentId, childId) => api.post('/api/kanban/links', { parent_id: parentId, child_id: childId }),
  unlinkKanbanTasks: (parentId, childId) => api.del(`/api/kanban/links?parent_id=${encodeURIComponent(parentId)}&child_id=${encodeURIComponent(childId)}`),
  dispatchKanban: () => api.post('/api/kanban/dispatch', {}),
  specifyKanbanTask: (id) => api.post(`/api/kanban/tasks/${encodeURIComponent(id)}/specify`, {}),
  decomposeKanbanTask: (id) => api.post(`/api/kanban/tasks/${encodeURIComponent(id)}/decompose`, {}),
  // Goals
  getGoals: () => api.get('/api/goals'),
  createGoal: (data) => api.post('/api/goals', data),
  updateGoal: (id, data) => api.put(`/api/goals/${encodeURIComponent(id)}`, data),
  deleteGoal: (id) => api.del(`/api/goals/${encodeURIComponent(id)}`),
  // Journal
  getJournalEntries: () => api.get('/api/journal/entries'),
  getJournalEntry: (date) => api.get(`/api/journal/entries/${encodeURIComponent(date)}`),
  saveJournalEntry: (date, content) => api.put(`/api/journal/entries/${encodeURIComponent(date)}`, { content }),
  searchJournal: (query) => api.get(`/api/journal/search?q=${encodeURIComponent(query)}`),
  // Agent Health
  getAgentHealth: () => api.get('/api/agents/health'),
  getAgentStats: (name) => api.get(`/api/agents/${encodeURIComponent(name)}/stats`),
  refreshAgentHealth: () => api.post('/api/agents/health/refresh', {}),
  // Smart Router
  suggestRouter: (task) => api.post('/api/router/suggest', { task }),
  routeTask: (task, agent) => api.post('/api/router/route', { task, agent }),
  // Learning Analytics
  getSkillAnalytics: () => api.get('/api/analytics/skills'),
  getTrendAnalytics: () => api.get('/api/analytics/trends'),
  // Session Replay
  listSessions: () => api.get('/api/sessions/list'),
  getSessionReplay: (id) => api.get(`/api/sessions/${encodeURIComponent(id)}/replay`),
  // v0.3.0: Scheduler Events
  getSchedulerEvents: (limit) => api.get(`/api/scheduler/events?limit=${limit || 50}`),
  triggerJob: (id) => api.post(`/api/scheduler/trigger/${encodeURIComponent(id)}`, {}),
  sendWebhook: (data) => api.post('/api/webhook', data),
  // v0.3.0: Error Tracking
  getErrors: (limit, category) => api.get(`/api/errors?limit=${limit || 50}${category ? `&category=${encodeURIComponent(category)}` : ''}`),
  reportError: (data) => api.post('/api/errors/report', data),
  clearErrors: () => api.del('/api/errors'),
  // v0.3.0: Circuit Breaker
  getCircuitBreaker: () => api.get('/api/circuit-breaker'),
  tripCircuitBreaker: (agent) => api.post('/api/circuit-breaker/trip', { agent }),
  resetCircuitBreaker: (agent) => api.post('/api/circuit-breaker/reset', { agent }),
  // v0.3.0: PWA
  getManifest: () => api.get('/manifest.json'),
  // v0.4.0: Memory Knowledge Graph
  getMemoryGraph: () => api.get('/api/memory/graph'),
  // v0.4.0: Code Diff Viewer
  getDiff: (file, ref = 'HEAD') => api.get(`/api/diff?file=${encodeURIComponent(file)}&ref=${encodeURIComponent(ref)}`),
};

// ─── Token prompt ───────────────────────────────────────────────────────────
// server.py prints AGENCY_API_TOKEN at startup when it is not configured in the
// environment. The operator pastes it here once; it lives in sessionStorage.

function ensureToken() {
  if (getToken()) return true;

  const input = window.prompt(
    'This API requires an access token.\n\n' +
    'It is printed by the server on startup as AGENCY_API_TOKEN.\n' +
    'It is kept in sessionStorage and cleared when this tab closes.',
    ''
  );
  if (!input) return false;

  setToken(input.trim());
  return true;
}

// Probe a token-protected endpoint: a 200 means the token is good, a 401 means
// it is not. /api/status is deliberately NOT used here because it is public
// and would return 200 regardless of the token's validity.
async function verifyToken() {
  try {
    const r = await fetch('/api/skills', { headers: authHeaders() });
    if (r.status === 401) {
      setToken('');
      showToast('Invalid token.', 'error');
      return false;
    }
    return r.ok;
  } catch {
    return false;
  }
}

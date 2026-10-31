export class ApiError extends Error {
  constructor(message, status, unavailable = false) { super(message); this.status = status; this.unavailable = unavailable; }
}
export async function api(path, { method = 'GET', body, signal } = {}) {
  const form = body instanceof FormData;
  const response = await fetch(path, { method, signal, headers: body !== undefined && !form ? { 'Content-Type': 'application/json' } : {}, body: body === undefined ? undefined : form ? body : JSON.stringify(body) });
  const type = response.headers.get('content-type') || '';
  const data = type.includes('json') ? await response.json() : await response.text();
  if (!response.ok) {
    const detail = typeof data === 'object' ? data.detail : data;
    const message = Array.isArray(detail) ? detail.map(d => `${d.loc?.slice(1).join(' ') || 'Input'}: ${d.msg}`).join('; ') : String(detail || `Request failed (${response.status})`);
    throw new ApiError(message, response.status, data.backend_unavailable);
  }
  return data;
}
export const post = (path, body) => api(path, { method: 'POST', body });
export const put = (path, body) => api(path, { method: 'PUT', body });
export const patch = (path, body) => api(path, { method: 'PATCH', body });
export const del = path => api(path, { method: 'DELETE' });
export function query(path, values) {
  const params = new URLSearchParams(Object.entries(values).filter(([, v]) => v !== '' && v !== undefined && v !== null));
  return `${path}${params.size ? '?' + params : ''}`;
}
export async function upload(path, file) { const body = new FormData(); body.append('file', file); return api(path, { method: 'POST', body }); }
export function realtime(onMessage, onState) {
  let socket, timer, stopped = false, attempt = 0;
  function connect() {
    if (stopped) return;
    socket = new WebSocket(`${location.protocol === 'https:' ? 'wss:' : 'ws:'}//${location.host}/ws`);
    socket.onopen = () => { attempt = 0; onState(true); };
    socket.onmessage = event => { try { onMessage(JSON.parse(event.data)); } catch { /* Ignore malformed frames. */ } };
    socket.onclose = () => { onState(false); if (!stopped) timer = setTimeout(connect, Math.min(30000, 1000 * 2 ** attempt++)); };
    socket.onerror = () => socket.close();
  }
  connect();
  const heartbeat = setInterval(() => { if (socket?.readyState === WebSocket.OPEN) socket.send('ping'); }, 20000);
  return () => { stopped = true; clearTimeout(timer); clearInterval(heartbeat); socket?.close(); };
}

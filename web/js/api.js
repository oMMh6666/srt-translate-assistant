/** 后端接口封装：统一错误处理（非 2xx 直接抛，由调用方 guard 住）。 */

const JSON_HEADERS = { "Content-Type": "application/json" };

async function request(path, opts = {}) {
  const res = await fetch(path, { headers: JSON_HEADERS, ...opts });
  if (!res.ok) {
    const msg = await res.text().catch(() => "");
    throw new Error(`${res.status} ${msg.slice(0, 200)}`);
  }
  return res.json();
}

const post = (path, body) => request(path, { method: "POST", body: JSON.stringify(body) });
const put = (path, body) => request(path, { method: "PUT", body: JSON.stringify(body) });
const enc = (id) => encodeURIComponent(id);

export const api = {
  // 应用
  config: () => request("/api/config"),
  uploadSrt: (filename, content) => post("/api/upload-srt", { filename, content }),

  // 任务
  jobs: () => request("/api/jobs"),
  createJob: (body) => post("/api/jobs", body),
  jobDetail: (id, pairs = false) => request(`/api/jobs/${enc(id)}?pairs=${pairs ? 1 : 0}`),
  jobProgress: (id) => request(`/api/jobs/${enc(id)}/progress`),
  startJob: (id, resume = true) => post(`/api/jobs/${enc(id)}/start?resume=${resume}`),
  pauseJob: (id) => post(`/api/jobs/${enc(id)}/pause`),
  deleteJob: (id) => request(`/api/jobs/${enc(id)}`, { method: "DELETE" }),
  saveParams: (id, params) => put(`/api/jobs/${enc(id)}/params`, params),
  savePrompt: (id, name, content) => put(`/api/jobs/${enc(id)}/prompts`, { name, content }),

  // 翻译记录
  apiLogs: (id) => request(`/api/jobs/${enc(id)}/apilogs`),
  apiLogDetail: (id, logId) => request(`/api/jobs/${enc(id)}/apilogs/${logId}`),
  downloadUrl: (id, kind) => `/api/jobs/${enc(id)}/download?kind=${kind}`,

  // Key
  keys: () => request("/api/keys"),
  addKeys: (items) => post("/api/keys", items),
  updateKey: (id, body) => put(`/api/keys/${id}`, body),
  bulkKeys: (ids, action) => post("/api/keys/bulk", { ids, action }),

  // 全局模板
  templates: () => request("/api/prompts/templates"),
  readTemplate: (name) => request(`/api/prompts/templates/${enc(name)}`),
  saveTemplate: (name, content) => post("/api/prompts/templates", { name, content }),
  deleteTemplate: (name) => request(`/api/prompts/templates/${enc(name)}`, { method: "DELETE" }),
};

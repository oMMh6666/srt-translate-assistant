/** SSE 客户端：命名事件 + 自动重连（浏览器自动带 Last-Event-ID）。 */

const EVENT_TYPES = ["log", "batch", "status", "export"];

export class JobEventStream {
  /**
   * @param {object} handlers {log, batch, status, export: fn, end, error}
   *   export 是保留字，用 handlers.exportEvent 接收。
   */
  constructor(handlers = {}) {
    this.handlers = handlers;
    this.source = null;
    this.jobId = null;
    this.connected = false;
  }

  connect(jobId) {
    if (!jobId) return;
    if (this.jobId === jobId && this.source) return;  // 已经是这条任务的流
    this.close();

    this.jobId = jobId;
    const es = new EventSource(`/api/jobs/${encodeURIComponent(jobId)}/events`);
    this.source = es;

    const onEvent = (type) => (ev) => {
      let payload = {};
      try { payload = JSON.parse(ev.data); } catch { /* 忽略坏帧 */ }
      // export 是保留字，监听方法名叫 exportEvent
      const fn = type === "export" ? this.handlers.exportEvent : this.handlers[type];
      fn?.(payload);
    };

    EVENT_TYPES.forEach((t) => es.addEventListener(t, onEvent(t)));

    es.addEventListener("end", (ev) => {
      let payload = {};
      try { payload = JSON.parse(ev.data); } catch { /* ignore */ }
      this.close();
      this.handlers.end?.(payload);
    });

    es.onopen = () => { this.connected = true; };
    // EventSource 会自己按服务端给的 retry 重连，这里只把状态标出来给调用方兜底用
    es.onerror = () => {
      this.connected = false;
      this.handlers.error?.();
    };
  }

  close() {
    if (this.source) {
      this.source.close();
      this.source = null;
    }
    this.connected = false;
  }

  closeAll() {
    this.close();
    this.jobId = null;
  }
}

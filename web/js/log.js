/** 底部「运行日志」区。 */

const LEVELS = ["info", "warn", "error", "ok"];

export function log(message, level = "info") {
  const box = document.querySelector("#logs");
  if (!box) return;
  const div = document.createElement("div");
  div.className = "lv-" + (LEVELS.includes(level) ? level : "info");
  const t = new Date().toLocaleTimeString("zh-CN", { hour12: false });
  div.textContent = `[${t}] ${message}`;
  box.appendChild(div);
  box.scrollTop = box.scrollHeight;
}

export function clearLog() {
  const box = document.querySelector("#logs");
  if (box) box.innerHTML = "";
}

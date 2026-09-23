/** DOM 与文本小工具。 */

import { log } from "./log.js";

export const $ = (s) => document.querySelector(s);
export const $$ = (s) => Array.from(document.querySelectorAll(s));

export function escapeHtml(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

export function fmtLen(n) {
  n = Number(n || 0);
  return n >= 1024 ? `${(n / 1024).toFixed(1)}K` : String(n);
}

/** 库里存的是压缩 JSON，展开成缩进好看些；不是 JSON 就原样显示。 */
export function prettyPayload(text) {
  if (!text) return "";
  try {
    return JSON.stringify(JSON.parse(text), null, 2);
  } catch {
    return text;
  }
}

/** 复制文本：优先用剪贴板 API，没权限就退回「隐藏 textarea + execCommand」。 */
export async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch { /* 无剪贴板权限时退回手动选中 */ }

  const ta = document.createElement("textarea");
  ta.value = text;
  ta.style.position = "fixed";
  ta.style.opacity = "0";
  document.body.appendChild(ta);
  ta.select();
  const ok = document.execCommand("copy");
  ta.remove();
  return ok;
}

export function fillSelect(sel, pairs) {
  const el = $(sel);
  el.innerHTML = "";
  pairs.forEach(([v, t]) => {
    const o = document.createElement("option");
    o.value = v;
    o.textContent = t;
    el.appendChild(o);
  });
}

export function fillDataList(sel, items) {
  const el = $(sel);
  el.innerHTML = "";
  items.forEach((v) => {
    const o = document.createElement("option");
    o.value = v;
    el.appendChild(o);
  });
}

/** 包一层：把异常收进底部日志，别让一个定时器/点击把页面搞死。 */
export async function guard(fn, name) {
  try {
    return await fn();
  } catch (e) {
    log(`${name}失败：${e.message}`, "error");
    return undefined;
  }
}

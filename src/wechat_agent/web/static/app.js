(function () {
  "use strict";
  document.documentElement.classList.add("js");
  var STORAGE_KEY = "wechatAgentDesktopToken";
  var urlToken = new URLSearchParams(window.location.search).get("token") || "";
  var state = { token: urlToken || sessionStorage.getItem(STORAGE_KEY) || "" };
  if (state.token) sessionStorage.setItem(STORAGE_KEY, state.token);
  var timezone = (document.body && document.body.dataset.timezone) || "Asia/Shanghai";
  var toastTimer = null;

  function esc(value) {
    return String(value === null || value === undefined ? "" : value).replace(/[&<>"']/g, function (character) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[character];
    });
  }

  function toast(message, isError) {
    var element = document.getElementById("toast");
    if (!element) return;
    element.textContent = String(message || "");
    element.className = "toast show" + (isError ? " error" : "");
    if (toastTimer) clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { element.className = "toast"; }, 2800);
  }

  function authHeaders() {
    return state.token ? { Authorization: "Bearer " + state.token } : {};
  }

  async function request(path, options, retried) {
    options = options || {};
    var headers = Object.assign({}, authHeaders(), options.headers || {});
    var response;
    try {
      response = await fetch(path, Object.assign({}, options, { headers: headers, credentials: "same-origin" }));
    } catch (error) {
      throw new Error("无法连接控制台：" + error.message);
    }
    if (response.status === 401 && !retried) {
      var supplied = window.prompt("请输入 DESKTOP_TOKEN 访问口令", "");
      if (supplied) {
        state.token = supplied;
        sessionStorage.setItem(STORAGE_KEY, supplied);
        return request(path, options, true);
      }
      throw new Error("未授权");
    }
    var text = await response.text();
    var data = null;
    if (text) {
      try { data = JSON.parse(text); } catch (error) { data = text; }
    }
    if (!response.ok) {
      var message = data && typeof data === "object" ? data.error || data.detail : data;
      throw new Error(message || "请求失败（" + response.status + "）");
    }
    return data;
  }

  function post(path, body) {
    return request(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
  }

  function fmtTime(value) {
    if (!value) return "";
    var date = new Date(value);
    if (isNaN(date.getTime())) return String(value);
    return date.toLocaleString("zh-CN", { timeZone: timezone, hour12: false });
  }

  var phaseLabels = { running: "正在工作", starting: "正在启动", stopping: "正在关闭", stopped: "已关闭", error: "出现错误" };

  function setConnection(ok, text) {
    var element = document.getElementById("lastUpdate");
    if (!element) return;
    element.className = "connection " + (ok ? "ok" : "error");
    var label = element.querySelector("span");
    if (label) label.textContent = text;
  }

  async function refreshShell() {
    try {
      var status = await request("/api/status");
      var dot = document.getElementById("sidebarDot");
      var label = document.getElementById("sidebarStatus");
      if (dot) dot.classList.toggle("on", Boolean(status.thread_alive));
      if (label) label.textContent = status.thread_alive ? "助手运行中" : phaseLabels[status.phase] || status.phase || "未知";
      setConnection(true, "本地已连接");
      document.body.dataset.threadAlive = status.thread_alive ? "1" : "0";
      return status;
    } catch (error) {
      setConnection(false, "连接失败");
      return null;
    }
  }

  document.addEventListener("DOMContentLoaded", function () {
    refreshShell();
    setInterval(refreshShell, 5000);
    var form = document.getElementById("memoryScope");
    if (form) {
      form.querySelectorAll("select").forEach(function (select) {
        select.addEventListener("change", function () {
          if (select.name === "account_id") {
            var group = form.querySelector('select[name="group_id"]');
            if (group) group.value = "";
          }
          form.submit();
        });
      });
    }
    document.addEventListener("keydown", function (event) {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
        var search = document.getElementById("memberSearch");
        if (search) {
          event.preventDefault();
          search.focus();
        }
      }
    });
  });

  window.Console = { state: state, request: request, post: post, toast: toast, esc: esc, fmtTime: fmtTime, refreshShell: refreshShell, phaseLabels: phaseLabels };
})();

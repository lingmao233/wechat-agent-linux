(function () {
  "use strict";
  var C = window.Console;
  if (!C) return;
  var $ = function (id) { return document.getElementById(id); };
  var toggle = $("agentToggle");
  if (toggle) {
    toggle.addEventListener("change", async function () {
      var starting = toggle.checked;
      toggle.disabled = true;
      try {
        await C.post(starting ? "/api/start" : "/api/stop", {});
        C.toast(starting ? "正在启动助手" : "正在停止助手");
        setTimeout(function () { location.reload(); }, 700);
      } catch (error) {
        toggle.checked = !starting;
        toggle.disabled = false;
        C.toast(error.message, true);
      }
    });
  }

  document.querySelectorAll("[data-quick-env]").forEach(function (input) {
    input.addEventListener("change", async function () {
      var values = {};
      values[input.dataset.quickEnv] = input.checked ? "true" : "false";
      input.disabled = true;
      try {
        await C.post("/api/config", { values: values });
        C.toast("已保存并重载");
      } catch (error) {
        input.checked = !input.checked;
        C.toast(error.message, true);
      } finally {
        input.disabled = false;
      }
    });
  });

  function setText(id, value) {
    var element = $(id);
    if (element) element.textContent = value;
  }

  async function refresh() {
    if (document.hidden) return;
    try {
      var status = await C.request("/api/status");
      var stats = status.stats || {};
      setText("heroPhase", C.phaseLabels[status.phase] || status.phase || "未知");
      setText("seenCount", stats.seen || 0);
      setText("replyCount", stats.replied || 0);
      setText("failedCount", stats.failed || 0);
      var dot = $("heroDot");
      if (dot) dot.classList.toggle("on", Boolean(status.thread_alive));
      var hint = $("heroHint");
      if (hint && status.last_error) hint.textContent = status.last_error;
    } catch (error) {
      return;
    }
  }
  setInterval(refresh, 3000);
  refresh();
})();

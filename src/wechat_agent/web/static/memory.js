(function () {
  "use strict";
  var C = window.Console;
  if (!C) return;
  var $ = function (id) { return document.getElementById(id); };

  var search = $("memberSearch");
  if (search) {
    var rows = Array.prototype.slice.call(document.querySelectorAll("[data-member-row]"));
    var groups = Array.prototype.slice.call(document.querySelectorAll("[data-member-group]"));
    search.addEventListener("input", function () {
      var query = search.value.trim().toLocaleLowerCase();
      var visible = 0;
      rows.forEach(function (row) {
        var matches = !query || String(row.dataset.search || "").indexOf(query) >= 0;
        row.hidden = !matches;
        if (matches) visible += 1;
      });
      groups.forEach(function (group) {
        var count = Array.prototype.filter.call(group.querySelectorAll("[data-member-row]"), function (row) { return !row.hidden; }).length;
        group.hidden = count === 0;
      });
      var countLabel = $("memberResultCount");
      if (countLabel) countLabel.textContent = visible + " 位成员";
      var empty = $("memberSearchEmpty");
      if (empty) empty.hidden = visible !== 0;
    });
  }

  var traceChoice = $("traceChoice");
  if (traceChoice) traceChoice.addEventListener("change", function () { if (traceChoice.form) traceChoice.form.submit(); });

  document.querySelectorAll("[data-revert-event]").forEach(function (button) {
    button.addEventListener("click", async function () {
      var reason = window.prompt("撤销原因（必填，会写进审计记录）", "");
      if (!reason || !reason.trim()) return;
      button.disabled = true;
      try {
        await C.post("/api/emotions/events/" + encodeURIComponent(button.dataset.revertEvent) + "/revert", {
          account_id: button.dataset.account,
          group_id: button.dataset.group,
          reason: reason.trim(),
          expected_version: Number(button.dataset.expectedVersion),
        });
        C.toast("已撤销，页面将刷新");
        setTimeout(function () { location.reload(); }, 500);
      } catch (error) {
        C.toast(error.message, true);
        button.disabled = false;
      }
    });
  });

  var retry = $("retryAnalysis");
  if (retry) retry.addEventListener("click", async function () {
    retry.disabled = true;
    try {
      var result = await C.post("/api/memory/retry", { account_id: retry.dataset.account, group_id: retry.dataset.group });
      C.toast("已重试 " + (result.retried || 0) + " 项失败任务");
      setTimeout(function () { location.reload(); }, 600);
    } catch (error) {
      C.toast(error.message, true);
      retry.disabled = false;
    }
  });

  async function replay(dryRun) {
    var result = $("replayResult");
    var since = $("replaySince");
    var until = $("replayUntil");
    var button = dryRun ? $("replayDryRun") : $("replayApply");
    if (!since.value || !until.value) {
      C.toast("请先选择开始和结束时间", true);
      return;
    }
    if (!dryRun && !window.confirm("应用当前账号、会话与明确时间范围的历史处理？不会回填情绪或发送消息。")) return;
    try {
      result.textContent = "正在执行…";
      var data = await C.post("/api/memory/replay", {
        account_id: button.dataset.account,
        group_id: button.dataset.group,
        since: new Date(since.value).toISOString(),
        until: new Date(until.value).toISOString(),
        dry_run: dryRun,
      });
      result.textContent = JSON.stringify(data, null, 2);
      C.toast(dryRun ? "预演完成" : "已应用历史处理");
    } catch (error) {
      result.textContent = "失败：" + error.message;
      C.toast(error.message, true);
    }
  }
  if ($("replayDryRun")) $("replayDryRun").addEventListener("click", function () { replay(true); });
  if ($("replayApply")) $("replayApply").addEventListener("click", function () { replay(false); });
})();

(function () {
  "use strict";
  var C = window.Console;
  if (!C) return;
  var $ = function (id) { return document.getElementById(id); };
  var tabs = Array.prototype.slice.call(document.querySelectorAll("[data-log-tab]"));
  var panels = Array.prototype.slice.call(document.querySelectorAll("[data-log-panel]"));
  function activate(name) {
    tabs.forEach(function (tab) { tab.classList.toggle("active", tab.dataset.logTab === name); });
    panels.forEach(function (panel) { panel.hidden = panel.dataset.logPanel !== name; });
  }
  tabs.forEach(function (tab) { tab.addEventListener("click", function () { activate(tab.dataset.logTab); }); });
  if (tabs.length) activate(tabs[0].dataset.logTab);

  function renderEvents(events) {
    var element = $("logEvents");
    if (!element) return;
    if (!events || !events.length) {
      element.innerHTML = '<div class="empty-inline">暂无实时记录。</div>';
      return;
    }
    element.innerHTML = events.slice(0, 60).map(function (item) {
      var level = String(item.level || "INFO");
      return '<div class="event-row"><span class="event-dot ' + C.esc(level.toLowerCase()) + '"></span><div><div class="event-meta"><span>' + C.esc(item.source || "desktop") + " · " + C.esc(level) + "</span><time>" + C.esc(C.fmtTime(item.at)) + "</time></div><p>" + C.esc(item.message || "") + "</p></div></div>";
    }).join("");
  }
  function badge(status) {
    var kind = status === "sent" ? " green" : status === "failed" || status === "blocked" ? " red" : "";
    var labels = { sent: "已发送", skipped: "已跳过", failed: "发送失败", processing: "处理中", blocked: "已拦截" };
    return '<span class="chip' + kind + '">' + C.esc(labels[status] || status || "") + "</span>";
  }
  function renderReplies(replies) {
    var body = document.querySelector("#ledgerReplies tbody");
    if (!body) return;
    if (!replies || !replies.length) {
      body.innerHTML = '<tr><td colspan="5"><div class="empty-inline">还没有回复记录。</div></td></tr>';
      return;
    }
    body.innerHTML = replies.map(function (row) {
      return "<tr><td>" + C.esc(C.fmtTime(row.updated_at)) + '<small class="table-subtext">' + C.esc(row.member_name || "未知") + " · " + C.esc(row.group_name || "") + "</small></td><td>" + badge(row.status) + '<small class="table-subtext">' + C.esc(row.reason || "") + (row.basis ? " · " + C.esc(row.basis) : "") + '</small></td><td>' + C.esc(row.text || "") + "</td><td>" + C.esc(row.reply_text || "—") + "</td><td>" + C.esc(row.attempt_count || 0) + "</td></tr>";
    }).join("");
  }
  function renderAudits(audits) {
    var body = document.querySelector("#ledgerAudits tbody");
    if (!body) return;
    if (!audits || !audits.length) {
      body.innerHTML = '<tr><td colspan="5"><div class="empty-inline">还没有发送审计记录。</div></td></tr>';
      return;
    }
    body.innerHTML = audits.map(function (row) {
      return "<tr><td>" + C.esc(C.fmtTime(row.created_at)) + "</td><td>" + C.esc(row.action_type || "") + "</td><td>" + C.esc(row.group_id || "") + '<small class="table-subtext">' + C.esc(row.target_member_id || "") + '</small></td><td>' + (row.allowed ? '<span class="chip green">允许</span>' : '<span class="chip red">拦截</span>') + '<small class="table-subtext">' + C.esc(row.reason || "") + '</small></td><td>' + C.esc(row.text || "") + "</td></tr>";
    }).join("");
  }
  function setLive(ok) {
    var state = $("liveState");
    if (!state) return;
    state.className = "connection " + (ok ? "ok" : "error");
    var label = state.querySelector("span");
    if (label) label.textContent = ok ? "每 2 秒刷新" : "连接失败";
  }
  async function refresh() {
    if (document.hidden) return;
    try {
      var live = await C.request("/api/live");
      renderEvents(live.events || []);
      renderReplies(live.replies || []);
      renderAudits(live.audit || []);
      setLive(true);
    } catch (error) {
      setLive(false);
    }
  }
  setInterval(refresh, 2000);
  refresh();
})();

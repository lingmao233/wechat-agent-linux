(function () {
  "use strict";

  var wxSelect = document.getElementById("bridgeWxContact");
  var dySelect = document.getElementById("bridgeDyConversation");
  var form = document.getElementById("bridgeBindForm");
  var hint = document.getElementById("bridgeHint");
  var refreshButton = document.getElementById("bridgeRefreshConversations");
  var bindButton = document.getElementById("bridgeBindBtn");
  if (!form || !wxSelect || !dySelect) return;

  var contacts = [];

  function setHint(text, error) {
    if (!hint) return;
    hint.textContent = text;
    hint.className = "platform-form-hint" + (error ? " error" : "");
  }

  function option(value, label, dataset) {
    var element = document.createElement("option");
    element.value = value;
    element.textContent = label;
    Object.keys(dataset || {}).forEach(function (key) {
      element.dataset[key] = dataset[key];
    });
    return element;
  }

  async function loadContacts() {
    wxSelect.innerHTML = '<option value="">加载中…</option>';
    try {
      var data = await Console.request("/api/wechat/private-chats");
      contacts = (data.contacts || []).filter(function (item) {
        var id = String(item.chat_id || "");
        return id && id.indexOf("@chatroom") < 0;
      });
      wxSelect.innerHTML = '<option value="">选择微信联系人…</option>';
      contacts.forEach(function (item) {
        wxSelect.appendChild(option(item.chat_id, item.name || item.chat_id, { name: item.name || "" }));
      });
      if (!contacts.length) {
        wxSelect.innerHTML = '<option value="">没有可用的一对一联系人</option>';
      }
    } catch (error) {
      wxSelect.innerHTML = '<option value="">加载失败</option>';
      setHint("微信联系人加载失败：" + error.message, true);
    }
  }

  async function loadConversations(force) {
    dySelect.innerHTML = '<option value="">正在读取抖音会话…</option>';
    if (refreshButton) refreshButton.disabled = true;
    try {
      var url = "/api/douyin-bridge/conversations" + (force ? "?refresh=1" : "");
      var data = await Console.request(url);
      var rows = data.conversations || [];
      dySelect.innerHTML = '<option value="">选择抖音私聊…</option>';
      rows.forEach(function (item) {
        dySelect.appendChild(option(item.name, item.name, { conversationId: item.conversation_id || "" }));
      });
      if (!rows.length) {
        dySelect.innerHTML = '<option value="">抖音会话列表为空</option>';
      }
      setHint("已读取 " + rows.length + " 个抖音私聊。选择后点「绑定」。", false);
    } catch (error) {
      dySelect.innerHTML = '<option value="">读取失败，稍后重试</option>';
      setHint("抖音会话读取失败：" + error.message + "（桥必须在运行，且抖音已登录）", true);
    } finally {
      if (refreshButton) refreshButton.disabled = false;
    }
  }

  async function loadBindings() {
    try {
      var data = await Console.request("/api/douyin-bridge/bindings");
      renderBindings(data.bindings || []);
    } catch (error) {
      Console.toast("绑定列表加载失败：" + error.message, true);
    }
  }

  function renderBindings(rows) {
    var table = document.getElementById("bridgeBindingTable");
    if (!table) return;
    var tbody = table.querySelector("tbody");
    tbody.innerHTML = "";
    if (!rows.length) {
      var empty = document.createElement("tr");
      empty.innerHTML = '<td colspan="4"><div class="empty-inline">还没有绑定。选择上面的两个下拉框，点「绑定」。</div></td>';
      tbody.appendChild(empty);
      return;
    }
    rows.forEach(function (row) {
      var tr = document.createElement("tr");
      tr.dataset.bindingRow = String(row.id);
      tr.innerHTML =
        "<td>" + Console.esc(row.wx_name || row.wxid) + '<small class="table-subtext">' + Console.esc(row.wxid) + "</small></td>" +
        "<td>" + Console.esc(row.peer_name || row.conversation_id) + '<small class="table-subtext">' + Console.esc(row.conversation_id) + "</small></td>" +
        '<td><span class="chip ' + (row.status === "active" ? "ok" : "warn") + '">' + Console.esc(row.status) + "</span></td>" +
        '<td><button class="btn ghost small" type="button" data-binding-delete="' + Console.esc(row.id) + '">解绑</button></td>';
      tbody.appendChild(tr);
    });
  }

  form.addEventListener("submit", async function (event) {
    event.preventDefault();
    var wxid = wxSelect.value;
    var peerName = dySelect.value;
    var selected = wxSelect.options[wxSelect.selectedIndex];
    if (!wxid || !peerName) {
      Console.toast("先选择微信联系人和抖音私聊", true);
      return;
    }
    var wxText = (selected && selected.textContent) || wxid;
    if (!window.confirm("确认绑定：\n抖音「" + peerName + "」\n↔ 微信「" + wxText + "」")) {
      return;
    }
    bindButton.disabled = true;
    try {
      await Console.post("/api/douyin-bridge/bindings", {
        wxid: wxid,
        wx_name: (selected && selected.dataset.name) || "",
        peer_name: peerName,
      });
      Console.toast("绑定成功");
      await loadBindings();
      setHint("已绑定「" + peerName + "」↔「" + ((selected && selected.textContent) || wxid) + "」。", false);
    } catch (error) {
      Console.toast("绑定失败：" + error.message, true);
      setHint("绑定失败：" + error.message, true);
    } finally {
      bindButton.disabled = false;
    }
  });

  document.addEventListener("click", async function (event) {
    var deleteButton = event.target.closest("[data-binding-delete]");
    if (deleteButton) {
      var id = deleteButton.getAttribute("data-binding-delete");
      if (!window.confirm("确定解绑这条记录？")) return;
      deleteButton.disabled = true;
      try {
        var result = await Console.post("/api/douyin-bridge/bindings/delete", { binding_id: Number(id) });
        if (result && result.deleted === false) {
          throw new Error("记录不存在或已解绑");
        }
        Console.toast("已解绑");
        await loadBindings();
      } catch (error) {
        Console.toast("解绑失败：" + error.message, true);
        deleteButton.disabled = false;
      }
      return;
    }
    var refresh = event.target.closest("#bridgeRefreshConversations");
    if (refresh) {
      await loadConversations(true);
    }
  });

  function describeWatchdog(watchdog) {
    if (!watchdog || watchdog.enabled === false) {
      return "已关闭";
    }
    if (watchdog.blocked) {
      return "需人工处理：" + watchdog.blocked;
    }
    var level = Number(watchdog.level || 0);
    var streak = Number(watchdog.streak || 0);
    var threshold = Number(watchdog.threshold || 0);
    var label = level >= 2 ? "已重启浏览器" : level === 1 ? "已重载页面" : "正常";
    if (streak) {
      label += "（连续失败 " + streak + (threshold ? "/" + threshold : "") + "）";
    }
    return label;
  }

  async function loadStatus() {
    try {
      var data = await Console.request("/api/douyin-bridge");
      var chat = data.chat || {};
      var last = data.last_result || {};
      var counts = data.counts || {};
      var watchdog = data.watchdog || {};
      var connected = Boolean(chat.connected);
      var badge = document.getElementById("bridgeRunBadge");
      if (badge) {
        badge.textContent = connected ? "抖音已连接" : "抖音未连接";
        badge.className = "soft-badge" + (connected ? " on" : "");
      }
      var connectedEl = document.getElementById("bridgeRunConnected");
      if (connectedEl) connectedEl.textContent = connected ? "已连接" : (chat.last_error || "未连接");
      var conversationEl = document.getElementById("bridgeRunConversation");
      if (conversationEl) conversationEl.textContent = chat.conversation || "—";
      var scanEl = document.getElementById("bridgeRunScan");
      if (scanEl) {
        scanEl.textContent = "扫描 " + (last.scanned || 0) + " / 点开 " + (last.opened || 0) + " / 新 " + (last.new || 0);
      }
      var countsEl = document.getElementById("bridgeRunCounts");
      if (countsEl) {
        countsEl.textContent = (data.bindings_count || 0) + " 绑定 / 已投递 " + (counts.delivered || 0) + " / 失败 " + (counts.failed || 0);
      }
      var watchdogEl = document.getElementById("bridgeRunWatchdog");
      if (watchdogEl) {
        watchdogEl.textContent = describeWatchdog(watchdog);
      }
      var errorEl = document.getElementById("bridgeRunError");
      if (errorEl) {
        var error = data.last_error || chat.last_error || "";
        var blocked = watchdog.blocked || "";
        if (blocked) {
          errorEl.textContent = "需要人工处理：" + blocked;
        } else {
          errorEl.textContent = error ? "最近错误：" + error : "";
        }
        errorEl.className = "platform-form-hint" + (error || blocked ? " error" : "");
      }
    } catch (error) {
      var badge = document.getElementById("bridgeRunBadge");
      if (badge) {
        badge.textContent = "状态读取失败";
        badge.className = "soft-badge";
      }
    }
  }

  loadContacts();
  loadConversations();
  loadBindings();
  loadStatus();
  setInterval(loadStatus, 5000);
})();

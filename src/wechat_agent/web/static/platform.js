(function () {
  "use strict";
  var C = window.Console;
  if (!C) return;
  var $ = function (id) { return document.getElementById(id); };
  var PLATFORM_LABELS = { douyin: "抖音", xiaohongshu: "小红书" };
  var ACTION_LABELS = { comment: "评论", reply: "回复", browse: "浏览", like: "点赞" };
  var STATUS_LABELS = { dry_run: "dry-run 计划", queued: "待确认", approved: "已批准", executing: "执行中", sent: "已发送", failed: "发送失败", blocked: "已拦截", skipped: "已跳过" };
  var STATUS_KINDS = { dry_run: "amber", queued: "accent", approved: "accent", executing: "accent", sent: "green", failed: "red", blocked: "red", skipped: "" };
  var KIND_LABELS = { browse: "浏览", comment_seen: "看到评论", comment_posted: "已发评论", mention: "@提及" };
  var state = { identities: [], allowlist: {}, actions: [], accounts: {}, memoryFilter: {} };
  var initialized = false;

  function esc(value) { return C.esc(value); }
  function platformLabel(value) { return PLATFORM_LABELS[value] || value || "未知平台"; }
  function text(value, limit) {
    var result = String(value === null || value === undefined ? "" : value).trim();
    if (limit && result.length > limit) result = result.slice(0, limit - 1) + "…";
    return result;
  }
  function firstList(payload, keys) {
    keys = keys || ["rows", "items", "data"];
    if (Array.isArray(payload)) return payload;
    if (payload && typeof payload === "object") {
      for (var index = 0; index < keys.length; index += 1) {
        if (Array.isArray(payload[keys[index]])) return payload[keys[index]];
      }
      for (var second = 0; second < keys.length; second += 1) {
        var nested = payload[keys[second]];
        if (nested && typeof nested === "object" && !Array.isArray(nested)) {
          if (Array.isArray(nested.rows)) return nested.rows;
          if (Array.isArray(nested.items)) return nested.items;
        }
      }
    }
    return [];
  }
  function idsOf(values) {
    if (typeof values === "string") values = values.split(/[,，]/);
    if (!Array.isArray(values)) return [];
    var result = [];
    values.forEach(function (entry) {
      if (entry && typeof entry === "object") entry = entry.user_id || entry.id || entry.platform_user_id || "";
      var value = String(entry === null || entry === undefined ? "" : entry).trim();
      if (value && result.indexOf(value) < 0) result.push(value);
    });
    return result;
  }
  function setText(element, value) { if (element) element.textContent = value; }
  function safeUrl(value) {
    var url = text(value);
    return /^https?:\/\//i.test(url) ? url : "";
  }
  function toast(message, isError) { C.toast(message, isError); }
  async function safeGet(path) {
    try { return await C.request(path); } catch (error) { return null; }
  }

  // ---------------------------------------------------------------- 账号状态
  function accountStateText(info) {
    if (!info || typeof info !== "object") return { label: "未配置", kind: "" };
    if (info.challenge) return { label: "需要人工处理", kind: "red" };
    if (info.last_error) return { label: "异常", kind: "red" };
    if (info.logged_in) return { label: "已登录", kind: "green" };
    if (info.connected) return { label: "已打开 · 待扫码", kind: "amber" };
    return { label: "未登录", kind: "" };
  }
  function allowlistHtml(ids) {
    var values = idsOf(ids);
    if (!values.length) return '<span class="muted">暂无</span>';
    return values.map(function (value) { return '<code class="platform-code">' + esc(value) + "</code>"; }).join("");
  }
  function renderAccounts() {
    document.querySelectorAll("[data-platform-card]").forEach(function (card) {
      var platform = card.getAttribute("data-platform-card");
      var info = state.accounts[platform] || {};
      var status = accountStateText(info);
      var badge = card.querySelector("[data-platform-state]");
      if (badge) {
        badge.className = "chip " + status.kind;
        badge.textContent = status.label;
      }
      setText(card.querySelector("[data-platform-login]"), info.logged_in ? "已登录" : "未登录");
      setText(card.querySelector("[data-platform-connected]"), info.connected ? "已打开" : "未打开");
      var allow = card.querySelector("[data-platform-allowlist]");
      if (allow) allow.innerHTML = allowlistHtml(info.mention_allowlist_ids);
    });
  }
  function renderAllowlistStrip() {
    var box = $("platformAllowlist");
    if (!box) return;
    var keys = Object.keys(PLATFORM_LABELS);
    Object.keys(state.allowlist).forEach(function (platform) {
      if (keys.indexOf(platform) < 0) keys.push(platform);
    });
    box.innerHTML = keys.map(function (platform) {
      var ids = idsOf(state.allowlist[platform]);
      var body = ids.length ? '<div class="chip-list">' + ids.map(function (value) {
        return '<code class="platform-code">' + esc(value) + "</code>";
      }).join("") + "</div>" : '<span class="muted">暂无白名单 ID</span>';
      return '<div class="allowlist-group" data-allowlist-platform="' + esc(platform) + '"><span class="chip accent">' + esc(platformLabel(platform)) + "</span>" + body + "</div>";
    }).join("");
  }
  function mergeAllowlist(payload) {
    if (payload && typeof payload === "object" && !Array.isArray(payload)) {
      ["mention_allowlist", "mention_allowlists", "allowlists", "effective_mention_allowlists", "allowlist_ids"].forEach(function (key) {
        var value = payload[key];
        if (!value || typeof value !== "object" || Array.isArray(value)) return;
        Object.keys(value).forEach(function (platform) {
          var entry = value[platform];
          if (entry && typeof entry === "object" && !Array.isArray(entry)) {
            entry = entry.ids || entry.user_ids || entry.targets || entry.allowlist_ids || [];
          }
          var ids = idsOf(entry);
          if (ids.length) state.allowlist[platform] = ids;
        });
      });
    }
  }
  async function refreshPlatforms() {
    var data = await safeGet("/api/platforms");
    if (!data || typeof data !== "object" || Array.isArray(data)) return;
    state.accounts = data;
    Object.keys(data).forEach(function (platform) {
      var info = data[platform];
      if (info && typeof info === "object" && !state.allowlist[platform]) {
        state.allowlist[platform] = idsOf(info.mention_allowlist_ids);
      }
    });
    renderAccounts();
    renderAllowlistStrip();
  }
  async function platformCommand(button, path) {
    var card = button.closest("[data-platform-card]");
    if (!card) return;
    var platform = card.getAttribute("data-platform-card");
    button.disabled = true;
    try {
      var result = await C.post(path, { platform: platform });
      var status = accountStateText(result);
      toast(platformLabel(platform) + "：" + status.label);
      await refreshPlatforms();
      await refreshIdentities();
    } catch (error) {
      toast(error.message, true);
    } finally {
      button.disabled = false;
    }
  }

  // ---------------------------------------------------------------- 账号关联
  function normalizeIdentity(row) {
    if (!row || typeof row !== "object") row = { id: row };
    var platform = text(row.platform);
    return {
      id: text(row.id !== undefined && row.id !== null ? row.id : row.identity_id || row.binding_id),
      platform: platform,
      platform_label: platformLabel(platform),
      platform_user_id: text(row.platform_user_id || row.user_id || row.platform_uid),
      platform_user_name: text(row.platform_user_name || row.user_name || row.display_name),
      member_id: text(row.member_id || row.wxid),
      member_name: text(row.member_name),
    };
  }
  function renderIdentityTable() {
    var body = document.querySelector("#identityTable tbody");
    var count = $("identityCount");
    if (count) count.textContent = state.identities.length + " 条关联";
    if (!body) return;
    if (!state.identities.length) {
      body.innerHTML = '<tr><td colspan="5"><div class="empty-inline">还没有绑定记录。先在表格上方绑定一个平台账号。</div></td></tr>';
      return;
    }
    body.innerHTML = state.identities.map(function (row) {
      return "<tr><td><span class=\"chip accent\">" + esc(row.platform_label) + "</span></td><td><code class=\"platform-code\">" + esc(row.platform_user_id) + "</code></td><td>" + esc(row.platform_user_name || "—") + "</td><td>" + esc(row.member_name || "—") + '<small class="table-subtext">' + esc(row.member_id || "—") + "</small></td><td><button class=\"btn ghost small\" type=\"button\" data-identity-delete=\"" + esc(row.id) + '">解绑</button></td></tr>';
    }).join("");
  }
  function updateCardAllowlists() {
    document.querySelectorAll("[data-platform-card]").forEach(function (card) {
      var platform = card.getAttribute("data-platform-card");
      var ids = idsOf(state.allowlist[platform]);
      if (!ids.length) return;
      var allow = card.querySelector("[data-platform-allowlist]");
      if (allow) allow.innerHTML = allowlistHtml(ids);
    });
  }
  async function refreshIdentities() {
    var data = await safeGet("/api/platform-identities");
    if (data === null) {
      var hint = $("identityHint");
      if (hint) hint.textContent = "关联接口尚未就绪（后端合并后自动可用）；当前仍可查看平台状态。";
      return;
    }
    var rows = firstList(data, ["identities", "rows", "items", "bindings", "data"]);
    state.identities = rows.map(normalizeIdentity);
    state.identities.forEach(function (row) {
      if (!row.platform || !row.platform_user_id) return;
      var ids = idsOf(state.allowlist[row.platform]);
      if (ids.indexOf(row.platform_user_id) < 0) ids.push(row.platform_user_id);
      state.allowlist[row.platform] = ids;
    });
    mergeAllowlist(data);
    renderIdentityTable();
    renderAllowlistStrip();
    updateCardAllowlists();
  }
  async function loadMembers() {
    var data = await safeGet("/api/members");
    var members = data && Array.isArray(data.members) ? data.members : [];
    var select = $("identityMemberSelect");
    if (!select) return;
    var seen = {};
    var options = ['<option value="">从成员列表选择…</option>'];
    members.forEach(function (member) {
      var memberId = text(member && member.member_id);
      if (!memberId || seen[memberId]) return;
      seen[memberId] = true;
      var memberName = text(member.member_name) || memberId;
      options.push('<option value="' + esc(memberId) + '" data-member-name="' + esc(memberName) + '">' + esc(memberName) + "（" + esc(memberId) + "）</option>");
    });
    select.innerHTML = options.join("");
    select.setAttribute("data-loaded", "1");
  }
  async function bindIdentity(event) {
    event.preventDefault();
    var platformSelect = $("identityPlatform");
    var userInput = $("identityUserId");
    var nameInput = $("identityUserName");
    var memberInput = $("identityMemberId");
    var memberNameInput = $("identityMemberName");
    var payload = {
      platform: platformSelect ? platformSelect.value : "",
      platform_user_id: userInput ? userInput.value.trim() : "",
      platform_user_name: nameInput ? nameInput.value.trim() : "",
      member_id: memberInput ? memberInput.value.trim() : "",
      member_name: memberNameInput ? memberNameInput.value.trim() : "",
    };
    if (!payload.platform_user_id || !payload.member_id) {
      toast("平台账号 ID 与微信 member_id 必填", true);
      return;
    }
    var button = $("identityBindBtn");
    if (button) button.disabled = true;
    try {
      await C.post("/api/platform-identities", payload);
      toast("已绑定 " + platformLabel(payload.platform) + " 账号 " + payload.platform_user_id);
      if (userInput) userInput.value = "";
      if (nameInput) nameInput.value = "";
      await Promise.all([refreshIdentities(), refreshPlatforms()]);
    } catch (error) {
      toast(error.message, true);
    } finally {
      if (button) button.disabled = false;
    }
  }
  async function deleteIdentity(button) {
    var identityId = button.getAttribute("data-identity-delete");
    if (!window.confirm("解绑这条平台账号关联？解绑后该平台 ID 不再进入 @ 白名单。")) return;
    button.disabled = true;
    try {
      await C.post("/api/platform-identities/delete", { id: identityId });
      toast("已解绑");
      await Promise.all([refreshIdentities(), refreshPlatforms()]);
    } catch (error) {
      toast(error.message, true);
      button.disabled = false;
    }
  }

  // ---------------------------------------------------------------- 浏览与评论
  function renderBrowseResults(platform, items) {
    var box = $("browseResults");
    if (!box) return;
    if (!items.length) {
      box.innerHTML = '<div class="empty-inline">没有浏览到内容。请确认浏览器已打开并登录，再换一个关键词。</div>';
      return;
    }
    box.innerHTML = items.map(function (item) {
      if (!item || typeof item !== "object") item = { title: item };
      var itemId = text(item.item_id || item.id);
      var title = text(item.title) || itemId || "未命名内容";
      var author = text(item.author_name || item.author_id);
      var url = safeUrl(item.url || item.item_url);
      var summary = text(item.text || item.summary, 240);
      return '<article class="browse-item" data-item-id="' + esc(itemId) + '"><div class="browse-item-head"><div><strong>' + esc(title) + "</strong><small>" + esc(platformLabel(platform)) + (author ? " · " + esc(author) : "") + "</small></div>" + (url ? '<a class="text-link" href="' + esc(url) + '" target="_blank" rel="noopener noreferrer">打开原页</a>' : "") + '</div><p class="browse-summary">' + esc(summary || "（无摘要）") + '</p><div class="browse-item-actions"><button class="btn ghost small" type="button" data-read-comments="' + esc(itemId) + '" data-platform="' + esc(platform) + '">读评论</button><button class="btn ghost small" type="button" data-write-comment="' + esc(itemId) + '" data-platform="' + esc(platform) + '" data-item-title="' + esc(title) + '">写评论</button><span class="muted">item_id: ' + esc(itemId) + "</span></div><div class=\"browse-comments\" hidden></div></article>";
    }).join("");
  }
  async function browse() {
    var platformSelect = $("browsePlatform");
    var queryInput = $("browseQuery");
    var limitInput = $("browseLimit");
    var button = $("browseBtn");
    var results = $("browseResults");
    var platform = platformSelect ? platformSelect.value : "douyin";
    var query = queryInput ? queryInput.value.trim() : "";
    var limit = Math.max(1, Math.min(20, Number(limitInput && limitInput.value) || 5));
    if (button) button.disabled = true;
    if (results) results.innerHTML = '<div class="empty-inline">正在浏览，打开浏览器后首次加载可能需要几秒…</div>';
    try {
      var data = await C.post("/api/platforms/browse", { platform: platform, query: query, limit: limit });
      renderBrowseResults(platform, firstList(data, ["items", "results", "rows", "data"]));
    } catch (error) {
      if (results) results.innerHTML = '<div class="empty-inline">浏览失败：' + esc(error.message) + "。请确认浏览器已打开且已完成扫码登录。</div>";
      toast(error.message, true);
    } finally {
      if (button) button.disabled = false;
    }
  }
  async function readComments(button) {
    var item = button.closest(".browse-item");
    if (!item) return;
    var box = item.querySelector(".browse-comments");
    button.disabled = true;
    try {
      var data = await C.post("/api/platform-comments", {
        platform: button.getAttribute("data-platform"),
        item_id: button.getAttribute("data-read-comments"),
        limit: 20,
      });
      var comments = firstList(data, ["comments", "items", "rows", "data"]);
      if (box) {
        box.hidden = false;
        box.innerHTML = comments.length ? comments.map(function (comment) {
          if (!comment || typeof comment !== "object") comment = { text: comment };
          return '<div class="browse-comment"><strong>' + esc(comment.author_name || comment.author_id || "匿名") + "</strong><p>" + esc(comment.text || "") + "</p></div>";
        }).join("") : '<div class="empty-inline">没有读到评论。</div>';
      }
    } catch (error) {
      toast("读取评论失败：" + error.message, true);
    } finally {
      button.disabled = false;
    }
  }
  function mentionOptionsHtml(platform) {
    var seen = {};
    var rows = state.identities.filter(function (row) {
      if (row.platform !== platform || !row.platform_user_id || seen[row.platform_user_id]) return false;
      seen[row.platform_user_id] = true;
      return true;
    });
    if (!rows.length) return "";
    return rows.map(function (row) {
      var name = row.platform_user_name || row.platform_user_id;
      return '<label class="mention-option"><input type="checkbox" data-mention-id="' + esc(row.platform_user_id) + '" data-mention-name="' + esc(name) + '"><span>' + esc(name) + "<small>" + esc(row.platform_user_id) + "</small></span></label>";
    }).join("");
  }
  function openComposer(button) {
    var item = button.closest(".browse-item");
    if (!item) return;
    var existing = item.querySelector(".comment-composer");
    if (existing) {
      existing.hidden = false;
      return;
    }
    var platform = button.getAttribute("data-platform");
    var itemId = button.getAttribute("data-write-comment");
    var title = text(button.getAttribute("data-item-title")) || itemId;
    var mentions = mentionOptionsHtml(platform);
    var composer = document.createElement("div");
    composer.className = "comment-composer";
    composer.innerHTML = '<label class="composer-field"><span>评论文本</span><textarea rows="3" maxlength="1000">' + esc("看了「" + title + "」，很有共鸣。") + "</textarea></label>" +
      '<div class="composer-row"><label class="composer-check"><input type="checkbox" class="composer-dryrun" checked> dry-run（不真实发送）</label><span class="muted">真实发送还需要写开关 + 台账确认</span></div>' +
      (mentions ? '<div class="composer-mentions"><span class="card-kicker">@ 选择（来自已绑定关联）</span><div class="mention-options">' + mentions + "</div></div>" : '<p class="muted">该平台还没有绑定关联，不能选择 @ 目标；可先在「账号关联」绑定。</p>') +
      '<div class="composer-actions"><button class="btn small" type="button" data-plan-action data-platform="' + esc(platform) + '" data-item-id="' + esc(itemId) + '">生成评论计划</button><button class="btn ghost small" type="button" data-close-composer>收起</button></div>' +
      '<div class="composer-status" role="status" aria-live="polite"></div>';
    item.appendChild(composer);
    var textarea = composer.querySelector("textarea");
    composer.querySelector("[data-close-composer]").addEventListener("click", function () { composer.hidden = true; });
    composer.querySelector("[data-plan-action]").addEventListener("click", function (event) {
      planComment(event.currentTarget, composer);
    });
    composer.querySelectorAll("[data-mention-id]").forEach(function (checkbox) {
      checkbox.addEventListener("change", function () {
        if (!checkbox.checked || !textarea) return;
        var name = checkbox.getAttribute("data-mention-name");
        if (textarea.value.indexOf("@" + name) >= 0) return;
        textarea.value = textarea.value.replace(/\s*$/, "") + " @" + name + " ";
        textarea.focus();
      });
    });
    if (textarea) textarea.focus();
  }
  async function planComment(button, composer) {
    var textarea = composer.querySelector("textarea");
    var body = textarea ? textarea.value.trim() : "";
    var dryRun = Boolean(composer.querySelector(".composer-dryrun") && composer.querySelector(".composer-dryrun").checked);
    var mentionIds = [];
    var missing = [];
    composer.querySelectorAll("[data-mention-id]").forEach(function (checkbox) {
      if (!checkbox.checked) return;
      mentionIds.push(checkbox.getAttribute("data-mention-id"));
      var mentionName = checkbox.getAttribute("data-mention-name") || "";
      if (mentionName && body.indexOf("@" + mentionName) < 0) missing.push(mentionName);
    });
    if (!body) { toast("评论文本不能为空", true); return; }
    if (missing.length) { toast("正文需要包含 @" + missing.join("、"), true); return; }
    var status = composer.querySelector(".composer-status");
    if (status) { status.className = "composer-status pending"; status.textContent = "正在生成计划…"; }
    button.disabled = true;
    try {
      var receipt = await C.post("/api/platform-actions/plan", {
        platform: button.getAttribute("data-platform"),
        action_type: "comment",
        item_id: button.getAttribute("data-item-id"),
        text: body,
        mention_ids: mentionIds,
        dry_run: dryRun,
      });
      if (status) {
        status.className = "composer-status ok";
        status.textContent = "已入动作台账：action_key " + (receipt.action_key || "—") + " · 状态 " + (receipt.status || "—") + (receipt.duplicate ? "（已存在，未重复创建）" : "");
      }
      toast(dryRun ? "已生成 dry-run 评论计划" : "已生成待确认评论计划");
      await refreshActions();
    } catch (error) {
      if (status) { status.className = "composer-status error"; status.textContent = "计划失败：" + error.message; }
      toast(error.message, true);
    } finally {
      button.disabled = false;
    }
  }

  // ---------------------------------------------------------------- 动作台账
  function statusBadge(status, dryRun) {
    var label = STATUS_LABELS[status] || status || "未知";
    var kind = STATUS_KINDS[status] || "";
    return '<span class="chip ' + kind + '">' + esc(label) + "</span>" + (dryRun ? '<small class="table-subtext">dry-run · 不会真实发送</small>' : "");
  }
  function renderActions() {
    var body = document.querySelector("#actionTable tbody");
    var count = $("actionCount");
    if (count) count.textContent = state.actions.length + " 条";
    if (!body) return;
    if (!state.actions.length) {
      body.innerHTML = '<tr><td colspan="6"><div class="empty-inline">还没有平台动作记录。在「浏览与评论」里生成一条计划后会出现。</div></td></tr>';
      return;
    }
    body.innerHTML = state.actions.map(function (row) {
      var platform = text(row.platform);
      var status = text(row.status);
      var dryRun = Boolean(row.dry_run);
      var canExecute = (status === "queued" || status === "approved") && !dryRun;
      return '<tr data-action-key="' + esc(row.action_key) + '"><td>' + esc(ACTION_LABELS[row.action_type] || row.action_type || "动作") + '<small class="table-subtext">' + esc(platformLabel(platform)) + "</small></td><td><code class=\"platform-code\">" + esc(row.target_id || row.parent_comment_id || "—") + "</code>" + (row.target_url ? '<small class="table-subtext">' + esc(row.target_url) + "</small>" : "") + "</td><td>" + esc(row.text || "") + (row.reason ? '<small class="table-subtext">' + esc(row.reason) + "</small>" : "") + "</td><td>" + statusBadge(status, dryRun) + "</td><td>" + esc(C.fmtTime(row.updated_at || row.created_at) || "—") + "</td><td>" + (canExecute ? '<button class="btn small" type="button" data-execute-action="' + esc(row.action_key) + '">确认执行</button>' : '<span class="muted">—</span>') + "</td></tr>";
    }).join("");
  }
  async function refreshActions() {
    var data = await safeGet("/api/platform-actions");
    if (data === null) return;
    state.actions = firstList(data, ["actions", "rows", "items", "data"]);
    renderActions();
  }
  async function executeAction(button) {
    var actionKey = button.getAttribute("data-execute-action");
    if (!window.confirm("确认执行该平台动作？\n\n这会用可见浏览器真实发送，且无法撤回。请确认目标页面与文本无误。")) return;
    button.disabled = true;
    try {
      var result = await C.post("/api/platform-actions/execute", { action_key: actionKey, confirm: true });
      toast("执行结果：" + ((result && result.status) || "已提交"));
      await Promise.all([refreshActions(), refreshMemories(), refreshPlatforms()]);
    } catch (error) {
      toast(error.message, true);
      button.disabled = false;
    }
  }

  // ---------------------------------------------------------------- 平台记忆
  function normalizeMemory(row) {
    if (!row || typeof row !== "object") row = { text: row };
    var platform = text(row.platform);
    return {
      id: text(row.id),
      member_id: text(row.member_id),
      member_name: text(row.member_name),
      platform: platform,
      platform_label: platformLabel(platform),
      kind: text(row.kind),
      kind_label: KIND_LABELS[row.kind] || text(row.kind) || "记录",
      kind_kind: row.kind === "comment_posted" ? "green" : row.kind === "mention" ? "accent" : "",
      title: text(row.title, 120),
      text: text(row.text, 220),
      query: text(row.query, 80),
      item_id: text(row.item_id),
      url: safeUrl(row.item_url || row.url),
      created_at: row.created_at || row.updated_at || "",
    };
  }
  function renderMemories() {
    var box = $("memoryList");
    var count = $("memoryCount");
    if (count) count.textContent = state.memories.length + " 条";
    if (!box) return;
    if (!state.memories.length) {
      box.innerHTML = '<div class="empty-inline">还没有平台记忆记录。</div>';
      return;
    }
    var groups = {};
    var order = [];
    state.memories.forEach(function (row) {
      var key = row.member_id + "\u0001" + row.platform;
      if (!groups[key]) {
        groups[key] = { member_id: row.member_id, member_name: row.member_name || row.member_id || "未关联成员", platform: row.platform, platform_label: row.platform_label, rows: [] };
        order.push(key);
      }
      if (!groups[key].member_name && row.member_name) groups[key].member_name = row.member_name;
      groups[key].rows.push(row);
    });
    box.innerHTML = order.map(function (key) {
      var group = groups[key];
      var rows = group.rows.map(function (row) {
        var meta = [];
        if (row.query) meta.push("关键词：" + esc(row.query));
        if (row.url) meta.push('<a class="text-link" href="' + esc(row.url) + '" target="_blank" rel="noopener noreferrer">' + esc(row.url) + "</a>");
        if (row.created_at) meta.push(esc(C.fmtTime(row.created_at)));
        var snippet = row.text && row.text !== row.title ? '<small class="memory-snippet">' + esc(row.text) + "</small>" : "";
        return '<div class="memory-row"><span class="chip ' + row.kind_kind + '">' + esc(row.kind_label) + "</span><div><p>" + esc(row.title || row.item_id || "—") + "</p>" + snippet + "<small>" + meta.join(" · ") + "</small></div></div>";
      }).join("");
      return '<div class="memory-group"><div class="memory-group-head"><span class="person-avatar">' + esc((group.member_name || "?").slice(0, 1)) + "</span><div><strong>" + esc(group.member_name) + "</strong><small>" + esc(group.member_id || "未关联成员") + '</small></div><span class="chip accent">' + esc(group.platform_label) + "</span></div>" + rows + "</div>";
    }).join("");
  }
  async function refreshMemories() {
    var params = [];
    var memberInput = $("memoryMemberId");
    var platformSelect = $("memoryPlatform");
    var member = memberInput ? memberInput.value.trim() : "";
    var platform = platformSelect ? platformSelect.value : "";
    if (member) params.push("member_id=" + encodeURIComponent(member));
    if (platform) params.push("platform=" + encodeURIComponent(platform));
    params.push("limit=50");
    var data = await safeGet("/api/platform-memories?" + params.join("&"));
    if (data === null) {
      var box = $("memoryList");
      if (box) box.innerHTML = '<div class="empty-inline">平台记忆接口尚未就绪（后端合并后自动可用）。</div>';
      return;
    }
    state.memories = firstList(data, ["memories", "rows", "items", "data"]).map(normalizeMemory);
    renderMemories();
  }

  // ---------------------------------------------------------------- 主动浏览
  function renderProactive(data) {
    if (!data || typeof data !== "object" || Array.isArray(data)) return;
    var enabled = data.enabled === true || /^(1|true|yes|on)$/i.test(String(data.enabled === undefined ? "" : data.enabled).trim());
    setText($("proactiveEnabled"), enabled ? "已开启" : "已关闭");
    var interval = data.interval_minutes;
    setText($("proactiveInterval"), interval === null || interval === undefined || interval === "" ? "—" : interval + " 分钟");
    setText($("proactiveQuiet"), data.quiet_hours || "—");
    setText($("proactiveLastRun"), data.last_run_at ? C.fmtTime(data.last_run_at) : "还没有运行记录");
    var keywords = data.keywords;
    if (typeof keywords === "string") keywords = keywords.split(/[,，]/);
    if (!Array.isArray(keywords)) keywords = [];
    keywords = keywords.map(function (value) { return String(value || "").trim(); }).filter(Boolean);
    if (!keywords.length && Array.isArray(data.effective_keywords)) {
      keywords = data.effective_keywords.map(function (value) { return String(value || "").trim(); }).filter(Boolean);
    }
    setText($("proactiveKeywords"), keywords.length ? keywords.join("、") : "未设置（使用人设 / 成员兴趣记忆取词）");
    var recentBox = $("proactiveRecent");
    if (recentBox) {
      var recent = firstList(data, ["recent_results", "results", "recent"]);
      recentBox.innerHTML = recent.length ? recent.slice(0, 5).map(function (row) {
        if (!row || typeof row !== "object") row = { text: row };
        var label = row.text || row.summary || row.title || row.message || "";
        if (!label) {
          label = ["reason", "platform", "query", "items", "dry_run"].map(function (key) {
            return row[key] === null || row[key] === undefined || row[key] === "" ? "" : key + "=" + row[key];
          }).filter(Boolean).join(" · ") || "已运行";
        }
        return '<div class="memory-row"><span class="chip">记录</span><div><p>' + esc(label) + "</p><small>" + esc(C.fmtTime(row.at || row.created_at || row.started_at) || "") + "</small></div></div>";
      }).join("") : '<p class="muted">还没有运行记录。</p>';
    }
  }
  async function refreshProactive() {
    var data = await safeGet("/api/platform-proactive");
    if (data === null) return;
    renderProactive(data);
  }
  async function runProactive() {
    var button = $("runProactive");
    if (button) button.disabled = true;
    try {
      var result = await C.post("/api/platform-proactive/run", {});
      var summary = "";
      if (result && typeof result === "object") {
        if (typeof result.summary === "string") summary = result.summary;
        else if (typeof result.reason === "string") {
          summary = result.reason;
          if (result.platform) summary += " · " + platformLabel(result.platform);
          if (result.items !== undefined && result.items !== null) summary += " · " + result.items + " 条";
        } else if (typeof result.message === "string") summary = result.message;
      } else if (typeof result === "string") {
        summary = result;
      }
      toast("已跑一轮主动浏览" + (summary ? "：" + summary : ""));
      await Promise.all([refreshProactive(), refreshActions(), refreshMemories(), refreshPlatforms()]);
    } catch (error) {
      toast(error.message, true);
    } finally {
      if (button) button.disabled = false;
    }
  }

  // ---------------------------------------------------------------- 事件接线
  async function refreshAll() {
    await Promise.all([refreshPlatforms(), refreshIdentities(), refreshActions(), refreshMemories(), refreshProactive()]);
  }
  function bindEvents() {
    var form = $("identityForm");
    if (form) form.addEventListener("submit", bindIdentity);
    var memberSelect = $("identityMemberSelect");
    if (memberSelect) {
      memberSelect.addEventListener("change", function () {
        var option = memberSelect.options[memberSelect.selectedIndex];
        var memberInput = $("identityMemberId");
        var nameInput = $("identityMemberName");
        if (memberInput && memberSelect.value) memberInput.value = memberSelect.value;
        if (nameInput && option && option.getAttribute("data-member-name")) nameInput.value = option.getAttribute("data-member-name");
      });
      memberSelect.addEventListener("focus", function () {
        if (!memberSelect.getAttribute("data-loaded")) loadMembers();
      }, { once: true });
    }
    var browseButton = $("browseBtn");
    if (browseButton) browseButton.addEventListener("click", browse);
    var browseQuery = $("browseQuery");
    if (browseQuery) browseQuery.addEventListener("keydown", function (event) {
      if (event.key === "Enter") { event.preventDefault(); browse(); }
    });
    var memoryForm = $("memoryFilter");
    if (memoryForm) memoryForm.addEventListener("submit", function (event) { event.preventDefault(); refreshMemories(); });
    var refreshMemoriesButton = $("refreshMemories");
    if (refreshMemoriesButton) refreshMemoriesButton.addEventListener("click", function () { refreshMemories(); });
    var refreshActionsButton = $("refreshActions");
    if (refreshActionsButton) refreshActionsButton.addEventListener("click", function () { refreshActions(); });
    var runProactiveButton = $("runProactive");
    if (runProactiveButton) runProactiveButton.addEventListener("click", runProactive);
    document.addEventListener("click", function (event) {
      var target = event.target instanceof Element ? event.target.closest("button") : null;
      if (!target) return;
      if (target.hasAttribute("data-platform-refresh-all")) { refreshAll(); return; }
      if (target.hasAttribute("data-platform-open")) { platformCommand(target, "/api/platforms/open"); return; }
      if (target.hasAttribute("data-platform-close")) { platformCommand(target, "/api/platforms/close"); return; }
      if (target.hasAttribute("data-platform-refresh")) { refreshPlatforms(); return; }
      if (target.hasAttribute("data-identity-delete")) { deleteIdentity(target); return; }
      if (target.hasAttribute("data-read-comments")) { readComments(target); return; }
      if (target.hasAttribute("data-write-comment")) { openComposer(target); return; }
      if (target.hasAttribute("data-execute-action")) { executeAction(target); return; }
    });
    setInterval(function () {
      if (!document.hidden) refreshPlatforms();
    }, 15000);
  }

  function init() {
    if (initialized) return;
    initialized = true;
    bindEvents();
    loadMembers();
    refreshAll();
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();

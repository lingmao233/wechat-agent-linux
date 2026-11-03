(function () {
  "use strict";
  var C = window.Console;
  if (!C) return;

  function splitList(value) {
    return String(value || "").split(/[,，]/).map(function (item) { return item.trim(); }).filter(Boolean);
  }
  function joinList(items) {
    var seen = {};
    return items.filter(function (item) {
      if (!item || seen[item]) return false;
      seen[item] = true;
      return true;
    }).join(",");
  }
  function collect(form) {
    var data = new FormData(form);
    var values = {};
    var clear = [];
    data.forEach(function (value, key) {
      if (key === "clear_secret") clear.push(String(value));
      else values[key] = String(value);
    });
    return { values: values, clear: clear };
  }
  function rememberToken(values, clear) {
    if (values.DESKTOP_TOKEN) {
      C.state.token = values.DESKTOP_TOKEN;
      sessionStorage.setItem("wechatAgentDesktopToken", values.DESKTOP_TOKEN);
    } else if (clear.indexOf("DESKTOP_TOKEN") >= 0) {
      C.state.token = "";
      sessionStorage.removeItem("wechatAgentDesktopToken");
    }
  }
  async function save(form, button, hintId) {
    var payload = collect(form);
    var hint = document.getElementById(hintId);
    button.disabled = true;
    try {
      var result = await C.post("/api/config", { values: payload.values, clear_secrets: payload.clear });
      rememberToken(payload.values, payload.clear);
      var message = result.restart_required ? "已保存；地址或端口变更需要重启控制台" : result.restarted ? "已保存，助手已自动重载" : "已保存，助手配置已重载";
      if (hint) hint.textContent = message;
      C.toast(message);
    } catch (error) {
      C.toast(error.message, true);
    } finally {
      button.disabled = false;
    }
  }
  document.querySelectorAll("form[data-settings-form]").forEach(function (form) {
    form.addEventListener("submit", function (event) {
      event.preventDefault();
      var button = form.querySelector('button[type="submit"]:not([data-clear-secret])');
      if (button) save(form, button, form.dataset.hint || "");
    });
  });
  document.querySelectorAll("button[data-clear-secret]").forEach(function (button) {
    button.addEventListener("click", async function (event) {
      event.preventDefault();
      var env = button.dataset.clearSecret;
      if (!window.confirm("清空 " + env + "？保存后立即生效。")) return;
      button.disabled = true;
      try {
        await C.post("/api/config", { values: {}, clear_secrets: [env] });
        C.toast("已清空 " + env);
        setTimeout(function () { location.reload(); }, 500);
      } catch (error) {
        C.toast(error.message, true);
        button.disabled = false;
      }
    });
  });

  document.querySelectorAll("[data-probability-range]").forEach(function (range) {
    var number = document.getElementById(range.dataset.probabilityRange);
    var field = range.closest(".probability-field");
    var output = field ? field.querySelector("[data-probability-output]") : null;
    function sync(source, target) {
      var value = Math.max(0, Math.min(1, Number(source.value) || 0));
      if (target) target.value = String(value);
      if (output) output.textContent = Math.round(value * 100) + "%";
      range.style.backgroundSize = Math.round(value * 100) + "% 100%";
    }
    range.addEventListener("input", function () { sync(range, number); });
    if (number) number.addEventListener("input", function () { sync(number, range); });
    sync(range, null);
  });

  function syncContact(checkbox, input) {
    if (!input) return;
    var contactId = checkbox.dataset.contactId || "";
    var contactName = checkbox.dataset.contactName || "";
    var list = splitList(input.value);
    if (checkbox.checked) {
      if (contactId && list.indexOf(contactId) < 0) list.push(contactId);
      else if (!contactId && contactName && list.indexOf(contactName) < 0) list.push(contactName);
    } else {
      list = list.filter(function (item) { return item !== contactId && item !== contactName; });
    }
    input.value = joinList(list);
  }
  var loadButton = document.getElementById("loadPrivateChats");
  if (loadButton) loadButton.addEventListener("click", async function () {
    var box = document.getElementById("privateContactList");
    var input = document.querySelector('input[name="WECHAT_PRIVATE_CHAT_NAMES"]');
    loadButton.disabled = true;
    box.innerHTML = '<span class="muted">正在读取联系人…</span>';
    try {
      var data = await C.request("/api/wechat/private-chats");
      if (!data.supported) {
        box.innerHTML = '<p class="private-picker-hint">' + C.esc(data.hint || "当前平台不支持联系人枚举，请直接填写 wxid。") + "</p>";
        return;
      }
      if (!data.ready) {
        box.innerHTML = '<p class="private-picker-hint">读取失败：' + C.esc(data.error || "未知原因") + "</p>";
        return;
      }
      var contacts = data.contacts || [];
      if (!contacts.length) {
        box.innerHTML = '<p class="private-picker-hint">没有读到联系人；可以继续手动填写稳定 wxid。</p>';
        return;
      }
      var current = splitList(input ? input.value : "");
      box.innerHTML = '<div class="private-contacts">' + contacts.map(function (contact) {
        var checked = current.indexOf(contact.chat_id) >= 0 || current.indexOf(contact.name) >= 0;
        return '<label class="private-contact"><input type="checkbox" data-contact-id="' + C.esc(contact.chat_id) + '" data-contact-name="' + C.esc(contact.name) + '"' + (checked ? " checked" : "") + '><span><strong>' + C.esc(contact.name || contact.chat_id) + "</strong><small>" + C.esc(contact.chat_id) + "</small></span></label>";
      }).join("") + '</div><p class="private-picker-hint">勾选只修改白名单草稿，点击「保存本页设置」后生效。</p>';
      box.querySelectorAll("input[data-contact-id]").forEach(function (checkbox) {
        checkbox.addEventListener("change", function () { syncContact(checkbox, input); });
      });
    } catch (error) {
      box.innerHTML = '<p class="private-picker-hint">读取失败：' + C.esc(error.message) + "</p>";
    } finally {
      loadButton.disabled = false;
    }
  });

  // ---- Persona file / folder upload ----------------------------------------
  var TEXT_EXTENSIONS = [".md", ".markdown", ".txt", ".json", ".yaml", ".yml", ".toml", ".ini", ".csv"];
  var MAX_UPLOAD_FILES = 200;
  var MAX_UPLOAD_BYTES = 5 * 1024 * 1024;
  var personaBox = document.getElementById("personaUpload");
  if (personaBox) {
    var dropZone = document.getElementById("personaDrop");
    var fileInput = document.getElementById("personaFileInput");
    var dirInput = document.getElementById("personaDirInput");
    var nameInput = document.getElementById("personaNameInput");
    var overwriteBox = document.getElementById("personaOverwrite");
    var applyBox = document.getElementById("personaApply");
    var uploadButton = document.getElementById("personaUploadBtn");
    var refreshButton = document.getElementById("personaRefreshBtn");
    var statusBox = document.getElementById("personaStatus");
    var currentBox = document.getElementById("personaCurrent");
    var personaListBox = document.getElementById("personaList");
    var countBadge = document.getElementById("personaCount");
    var uploadEndpoint = personaBox.dataset.uploadEndpoint || "/api/personas/upload";
    var catalogEndpoint = personaBox.dataset.catalogEndpoint || "/api/personas";
    var selection = [];
    var uploading = false;

    function setStatus(text, kind) {
      if (!statusBox) return;
      statusBox.textContent = String(text || "");
      statusBox.className = "persona-status" + (kind ? " " + kind : "");
    }
    function formatSize(size) {
      var bytes = Number(size) || 0;
      if (bytes < 1024) return bytes + " B";
      if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + " KB";
      return (bytes / (1024 * 1024)).toFixed(2) + " MB";
    }
    function normalizePath(raw) {
      return String(raw || "").replace(/\\/g, "/").replace(/^\/+/, "").split("/").filter(function (part) {
        return part && part !== ".";
      }).join("/");
    }
    function isTextPath(path) {
      var lower = String(path || "").toLowerCase();
      return TEXT_EXTENSIONS.some(function (extension) { return lower.slice(-extension.length) === extension; });
    }
    function sanitizeName(value) {
      var name = String(value || "").replace(/[^A-Za-z0-9._\u4e00-\u9fff-]/g, "_").replace(/^[.]+/, "").slice(0, 64);
      return name;
    }
    function readText(file) {
      if (file && typeof file.text === "function") return file.text();
      return new Promise(function (resolve, reject) {
        var reader = new FileReader();
        reader.onload = function () { resolve(String(reader.result || "")); };
        reader.onerror = function () { reject(reader.error || new Error("无法读取文件")); };
        reader.readAsText(file, "utf-8");
      });
    }
    function readEntry(entry, prefix) {
      prefix = prefix || "";
      if (entry.isFile) {
        var filePath = normalizePath(prefix ? prefix + "/" + entry.name : entry.name);
        return new Promise(function (resolve) {
          entry.file(function (file) {
            resolve([{ path: filePath, file: file }]);
          }, function () { resolve([]); });
        });
      }
      if (!entry.isDirectory) return Promise.resolve([]);
      var dirPrefix = normalizePath(prefix ? prefix + "/" + entry.name : entry.name);
      return new Promise(function (resolve) {
        var reader = entry.createReader();
        var children = [];
        function readBatch() {
          reader.readEntries(function (entries) {
            if (!entries || !entries.length) {
              Promise.all(children.map(function (child) { return readEntry(child, dirPrefix); })).then(function (groups) {
                resolve([].concat.apply([], groups));
              }, function () { resolve([]); });
              return;
            }
            children = children.concat(Array.prototype.slice.call(entries));
            readBatch();
          }, function () { resolve([]); });
        }
        readBatch();
      });
    }
    function topFolderOf(entries) {
      var tops = {};
      entries.forEach(function (item) {
        var top = String(item.path || "").split("/")[0];
        if (top) tops[top] = true;
      });
      var names = Object.keys(tops);
      if (names.length !== 1) return "";
      var top = names[0];
      var nested = entries.some(function (item) { return item.path !== top; });
      return nested ? top : "";
    }
    function stripTopFolder(entries, topFolder) {
      if (!topFolder) return entries;
      return entries.map(function (item) {
        return { path: item.path.split("/").slice(1).join("/"), file: item.file };
      }).filter(function (item) { return Boolean(item.path); });
    }
    function updateCount() {
      if (countBadge) {
        countBadge.textContent = selection.length ? "已选 " + selection.length + " 个文件" : "未选择文件";
        countBadge.classList.toggle("on", selection.length > 0);
      }
      uploadButton.disabled = uploading || !selection.length;
    }
    function addSelection(items, suggestedName) {
      var skipped = 0;
      var accepted = [];
      items.forEach(function (item) {
        if (!item || !item.path) return;
        if (!isTextPath(item.path)) { skipped += 1; return; }
        accepted.push(item);
      });
      var merged = {};
      selection.forEach(function (item) { merged[item.path] = item; });
      accepted.forEach(function (item) { merged[item.path] = item; });
      selection = Object.keys(merged).sort().map(function (key) { return merged[key]; });
      if (!nameInput.value.trim() && (suggestedName || selection.length)) {
        var fallbackName = String(selection[0].path).replace(/^.*\//, "").replace(/\.[^.]+$/, "");
        if (["SKILL", "README", "INDEX"].indexOf(fallbackName.toUpperCase()) >= 0) fallbackName = "";
        nameInput.value = sanitizeName(suggestedName || fallbackName);
      }
      updateCount();
      var parts = ["已选择 " + selection.length + " 个文本文件"];
      if (skipped) parts.push("已忽略 " + skipped + " 个非文本文件");
      parts.push("支持 " + TEXT_EXTENSIONS.join(" "));
      setStatus(parts.join("；") + "。", skipped ? "warn" : "");
    }
    function handleDropped(droppedFiles) {
      var items = Array.prototype.map.call(droppedFiles, function (file) {
        return { path: normalizePath(file.webkitRelativePath || file.name), file: file };
      });
      addSelection(stripTopFolder(items, topFolderOf(items)), topFolderOf(items));
    }
    function applyConfigValues(values) {
      ["PERSONA_NAME", "PERSONA_FILE"].forEach(function (env) {
        if (!(env in values)) return;
        var input = document.querySelector('input[name="' + env + '"]');
        if (input) input.value = values[env] === null || values[env] === undefined ? "" : String(values[env]);
      });
    }
    function renderCatalog(data) {
      var current = (data && data.current) || {};
      if (currentBox) {
        currentBox.innerHTML = '<span class="persona-current-label">当前人设</span><strong>' + C.esc(current.name || "未设置") + '</strong><code>' + C.esc(current.file || "") + '</code><span class="soft-badge">' + (current.enabled ? "已启用" : "未启用") + "</span>";
      }
      if (!personaListBox) return;
      var personas = (data && data.personas) || [];
      if (!personas.length) {
        personaListBox.hidden = true;
        personaListBox.innerHTML = "";
        return;
      }
      personaListBox.hidden = false;
      personaListBox.innerHTML = personas.map(function (persona) {
        var files = (persona.files || []).map(function (file) {
          return '<li class="persona-file' + (file.main ? " main" : "") + '"><span>' + C.esc(file.path) + '</span><small>' + formatSize(file.size) + (file.main ? " · 主文件" : "") + "</small></li>";
        }).join("");
        return '<div class="persona-item" data-name="' + C.esc(persona.name) + '"><div class="persona-item-head"><strong>' + C.esc(persona.name) + "</strong><span>" + C.esc(persona.path || "") + "</span><small>" + C.esc(C.fmtTime(persona.updatedAt)) + '</small></div><ul class="persona-files">' + files + "</ul></div>";
      }).join("") + ((data && data.truncated) ? '<p class="persona-hint">文件较多，这里只显示前 500 个。</p>' : "");
    }
    async function refreshCatalog() {
      try {
        var data = await C.request(catalogEndpoint);
        renderCatalog(data);
        return data;
      } catch (error) {
        setStatus("读取人设列表失败：" + error.message, "error");
        return null;
      }
    }
    async function upload() {
      var name = nameInput.value.trim();
      if (!name) {
        setStatus("请填写目标人设名称。", "error");
        nameInput.focus();
        return;
      }
      if (!selection.length) {
        setStatus("请先选择要上传的文件或文件夹。", "error");
        return;
      }
      if (selection.length > MAX_UPLOAD_FILES) {
        setStatus("一次最多上传 " + MAX_UPLOAD_FILES + " 个文件，当前已选 " + selection.length + " 个。", "error");
        return;
      }
      var files = [];
      var totalBytes = 0;
      try {
        for (var index = 0; index < selection.length; index += 1) {
          var content = await readText(selection[index].file);
          content = content.replace(/^\uFEFF/, "");
          var encoded = new TextEncoder().encode(content).length;
          totalBytes += encoded;
          files.push({ path: selection[index].path, content: content });
          if (totalBytes > MAX_UPLOAD_BYTES) break;
        }
      } catch (error) {
        setStatus("读取本地文件失败：" + error.message, "error");
        return;
      }
      if (files.length > MAX_UPLOAD_FILES) {
        setStatus("一次最多上传 " + MAX_UPLOAD_FILES + " 个文件。", "error");
        return;
      }
      if (totalBytes > MAX_UPLOAD_BYTES) {
        setStatus("文件总大小超过 5 MB，请删减后再试。", "error");
        return;
      }
      uploading = true;
      uploadButton.disabled = true;
      uploadButton.textContent = "正在上传…";
      setStatus("正在上传 " + files.length + " 个文件…", "pending");
      try {
        var result = await C.post(uploadEndpoint, {
          name: name,
          files: files,
          overwrite: Boolean(overwriteBox && overwriteBox.checked),
          apply: Boolean(applyBox && applyBox.checked),
        });
        var extra = [];
        if (result.applied) extra.push(result.restarted ? "已写入 .env 并重载助手" : "已写入 .env，下次启动生效");
        else extra.push("未写入 .env");
        setStatus("上传成功：" + (result.created ? "已创建" : "已合并") + "人设「" + (result.name || name) + "」共 " + files.length + " 个文件；主文件 " + (result.mainFile || "") + "；" + extra.join("，") + "。", "ok");
        if (result.applied && result.config && result.config.values) applyConfigValues(result.config.values);
        selection = [];
        updateCount();
        nameInput.value = "";
        await refreshCatalog();
        C.toast("人设「" + (result.name || name) + "」上传成功");
      } catch (error) {
        setStatus("上传失败：" + error.message, "error");
        C.toast("上传失败：" + error.message, true);
      } finally {
        uploading = false;
        uploadButton.textContent = "开始上传";
        updateCount();
      }
    }
    if (dropZone) {
      dropZone.addEventListener("dragover", function (event) {
        event.preventDefault();
        dropZone.classList.add("dragover");
      });
      dropZone.addEventListener("dragleave", function () { dropZone.classList.remove("dragover"); });
      dropZone.addEventListener("drop", function (event) {
        event.preventDefault();
        dropZone.classList.remove("dragover");
        var transfer = event.dataTransfer;
        if (!transfer) return;
        var entries = [];
        var files = [];
        if (transfer.items && transfer.items.length) {
          for (var index = 0; index < transfer.items.length; index += 1) {
            var item = transfer.items[index];
            if (item.kind !== "file") continue;
            var entry = item.webkitGetAsEntry ? item.webkitGetAsEntry() : null;
            if (entry) entries.push(entry);
            else if (item.getAsFile) {
              var dropped = item.getAsFile();
              if (dropped) files.push(dropped);
            }
          }
        }
        if (!entries.length && !files.length && transfer.files) files = Array.prototype.slice.call(transfer.files);
        if (entries.length) {
          setStatus("正在读取拖入的文件…", "pending");
          Promise.all(entries.map(function (entry) { return readEntry(entry); })).then(function (groups) {
            var collected = [].concat.apply([], groups);
            var topFolder = topFolderOf(collected);
            addSelection(stripTopFolder(collected, topFolder), topFolder);
          }, function (error) {
            setStatus("读取拖入的文件失败：" + (error && error.message ? error.message : "未知原因"), "error");
          });
        } else if (files.length) {
          handleDropped(files);
        }
      });
      dropZone.addEventListener("click", function (event) {
        if (event.target.closest("label,button,input")) return;
        fileInput.click();
      });
    }
    if (fileInput) {
      fileInput.addEventListener("change", function () {
        var picked = Array.prototype.map.call(fileInput.files || [], function (file) {
          return { path: normalizePath(file.name), file: file };
        });
        addSelection(picked, "");
        fileInput.value = "";
      });
    }
    if (dirInput) {
      dirInput.addEventListener("change", function () {
        var raw = Array.prototype.map.call(dirInput.files || [], function (file) {
          return { path: normalizePath(file.webkitRelativePath || file.name), file: file };
        });
        var topFolder = topFolderOf(raw);
        addSelection(stripTopFolder(raw, topFolder), topFolder);
        dirInput.value = "";
      });
    }
    if (uploadButton) uploadButton.addEventListener("click", upload);
    if (refreshButton) refreshButton.addEventListener("click", function () {
      refreshButton.disabled = true;
      setStatus("正在刷新人设列表…", "pending");
      refreshCatalog().then(function (data) {
        refreshButton.disabled = false;
        if (data) setStatus("人设列表已刷新。", "");
      });
    });
    if (personaListBox) {
      personaListBox.addEventListener("click", function (event) {
        var item = event.target.closest(".persona-item");
        if (!item) return;
        nameInput.value = item.dataset.name || "";
        if (overwriteBox) overwriteBox.checked = true;
        setStatus("已填入人设「" + nameInput.value + "」；选择文件后勾选覆盖即可补充或更新。", "");
      });
    }
    if (nameInput) {
      nameInput.addEventListener("keydown", function (event) {
        if (event.key !== "Enter") return;
        event.preventDefault();
        if (!uploadButton.disabled) upload();
      });
    }
    updateCount();
    refreshCatalog();
  }
})();

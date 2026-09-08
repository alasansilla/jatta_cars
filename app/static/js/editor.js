/* Inline page editor for signed-in staff.
   Regions are marked up in the templates:
     data-edit="key"                 editable text (a setting, or "vehicle:3:field")
     data-edit-multiline             the text is stored as paragraphs
     data-edit-plain                 strip inline markup while editing
     data-edit-list="key"            a list; each data-edit-item is one line
     data-edit-image="key"           click to replace the picture
     data-edit-icon="key"            click to choose a different icon
   Saving reloads the page, so what you see afterwards is what the server renders. */
(function () {
  "use strict";

  var bar = document.getElementById("editbar");
  if (!bar) return;

  var csrf = bar.dataset.csrf;
  var toggleBtn = document.getElementById("edit-toggle");
  var saveBtn = document.getElementById("edit-save");
  var cancelBtn = document.getElementById("edit-cancel");
  var statusEl = document.getElementById("edit-status");
  var filePicker = document.getElementById("edit-file");

  var editing = false;
  var changes = {};
  var pendingImageTarget = null;
  var iconCache = null;
  var choiceCache = null;
  var popover = null;

  function setStatus(text, state) {
    statusEl.textContent = text || "";
    if (state) statusEl.dataset.state = state;
    else delete statusEl.dataset.state;
  }

  function markChanged(key, value, el) {
    changes[key] = value;
    if (el) el.classList.add("is-changed");
    setStatus(Object.keys(changes).length + " unsaved change" +
      (Object.keys(changes).length === 1 ? "" : "s"), "dirty");
  }

  function isDirty() {
    return Object.keys(changes).length > 0;
  }

  /* ---- text regions ---- */

  function textOf(el) {
    if (el.hasAttribute("data-edit-multiline")) {
      var blocks = el.querySelectorAll(":scope > p");
      if (blocks.length) {
        return Array.prototype.map.call(blocks, function (p) {
          return p.innerText.trim();
        }).join("\n\n");
      }
    }
    return el.innerText.replace(/ /g, " ").trim();
  }

  function enableText(el) {
    var original = textOf(el);
    el.dataset.editOriginal = original;

    // Editing works on plain text; the server re-renders the markup on save.
    if (el.hasAttribute("data-edit-multiline") || el.hasAttribute("data-edit-plain")) {
      el.dataset.editHtml = el.innerHTML;
      el.textContent = original;
      el.style.whiteSpace = "pre-wrap";
    }

    el.setAttribute("contenteditable", "plaintext-only");
    if (!el.textContent.trim() && el.dataset.editPlaceholder) el.classList.add("is-empty");
  }

  function disableText(el) {
    el.removeAttribute("contenteditable");
    el.style.whiteSpace = "";
    el.classList.remove("is-empty", "is-changed");
  }

  document.addEventListener("input", function (event) {
    var el = event.target.closest("[data-edit]");
    if (!editing || !el) return;
    el.classList.toggle("is-empty", !el.textContent.trim());
    var value = textOf(el);
    if (value !== el.dataset.editOriginal) markChanged(el.dataset.edit, value, el);
  });

  // Single-line regions should not grow new lines from Enter.
  document.addEventListener("keydown", function (event) {
    if (!editing) return;
    var el = event.target.closest("[data-edit]");
    if (!el) return;
    if (event.key === "Enter" && !el.hasAttribute("data-edit-multiline")) {
      event.preventDefault();
      el.blur();
    }
    if (event.key === "Escape") el.blur();
  });

  // Keep pasted content plain, whatever it came from.
  document.addEventListener("paste", function (event) {
    if (!editing) return;
    var el = event.target.closest("[data-edit], [data-edit-item] span");
    if (!el) return;
    event.preventDefault();
    var text = (event.clipboardData || window.clipboardData).getData("text/plain");
    document.execCommand("insertText", false, text);
  });

  // Links must not navigate while their text is being edited.
  document.addEventListener("click", function (event) {
    if (!editing) return;
    var link = event.target.closest("a");
    if (link && (link.hasAttribute("data-edit") || link.closest("[data-edit], [data-edit-item]"))) {
      event.preventDefault();
    }
  });

  /* ---- lists ---- */

  function listValue(list) {
    return Array.prototype.map.call(list.querySelectorAll("[data-edit-item]"), function (item) {
      var span = item.querySelector("span");
      return (span ? span.innerText : item.innerText).trim();
    }).filter(Boolean).join("\n");
  }

  function syncList(list) {
    markChanged(list.dataset.editList, listValue(list), list);
  }

  function decorateItem(list, item) {
    var span = item.querySelector("span");
    if (!span) {
      span = document.createElement("span");
      span.textContent = item.textContent.trim();
      item.textContent = "";
      item.appendChild(span);
    }
    span.setAttribute("contenteditable", "plaintext-only");
    span.addEventListener("input", function () { syncList(list); });

    var tools = document.createElement("span");
    tools.className = "edit-item-tools";
    var remove = document.createElement("button");
    remove.type = "button";
    remove.className = "edit-chip";
    remove.title = "Remove this item";
    remove.textContent = "×";
    remove.addEventListener("click", function () {
      item.remove();
      syncList(list);
    });
    tools.appendChild(remove);
    item.appendChild(tools);
  }

  function enableList(list) {
    var first = list.querySelector("[data-edit-item]");
    list.dataset.itemTemplate = first ? first.outerHTML : '<li data-edit-item><span></span></li>';

    Array.prototype.forEach.call(list.querySelectorAll("[data-edit-item]"), function (item) {
      decorateItem(list, item);
    });

    var add = document.createElement("button");
    add.type = "button";
    add.className = "edit-add js-edit-add";
    add.textContent = "+ Add item";
    add.addEventListener("click", function () {
      var holder = document.createElement("div");
      holder.innerHTML = list.dataset.itemTemplate;
      var item = holder.firstElementChild;
      var span = item.querySelector("span");
      if (span) span.textContent = "New item";
      list.appendChild(item);
      decorateItem(list, item);
      syncList(list);
      if (span) {
        span.focus();
        document.getSelection().selectAllChildren(span);
      }
    });
    list.insertAdjacentElement("afterend", add);
  }

  /* ---- images ---- */

  function pickImage(el) {
    pendingImageTarget = el;
    filePicker.value = "";
    filePicker.click();
  }

  filePicker.addEventListener("change", function () {
    var file = filePicker.files[0];
    if (!file || !pendingImageTarget) return;

    var target = pendingImageTarget;
    pendingImageTarget = null;

    var body = new FormData();
    body.append("file", file);
    setStatus("Uploading " + file.name + "…");

    fetch("/admin/api/upload", { method: "POST", headers: { "X-CSRF-Token": csrf }, body: body })
      .then(function (response) { return response.json().then(function (data) {
        if (!response.ok) throw new Error(data.error || "Upload failed.");
        return data;
      }); })
      .then(function (data) {
        target.src = data.url;
        target.removeAttribute("srcset");
        markChanged(target.dataset.editImage, data.path, target);
      })
      .catch(function (error) { setStatus(error.message, "error"); });
  });

  /* ---- icons ---- */

  function closePopover() {
    if (popover) { popover.remove(); popover = null; }
  }

  function openIconPicker(el) {
    closePopover();
    var current = el.dataset.iconValue || "";

    function build(icons) {
      popover = document.createElement("div");
      popover.className = "edit-popover";
      Object.keys(icons).forEach(function (name) {
        var button = document.createElement("button");
        button.type = "button";
        button.title = name;
        button.innerHTML = icons[name];
        button.setAttribute("aria-pressed", String(name === current));
        button.addEventListener("click", function () {
          el.innerHTML = icons[name];
          el.dataset.iconValue = name;
          markChanged(el.dataset.editIcon, name, el);
          closePopover();
        });
        popover.appendChild(button);
      });
      document.body.appendChild(popover);
      var box = el.getBoundingClientRect();
      popover.style.top = (window.scrollY + box.bottom + 8) + "px";
      popover.style.left = (window.scrollX + box.left) + "px";
    }

    if (iconCache) { build(iconCache); return; }
    fetch("/admin/api/icons")
      .then(function (r) { return r.json(); })
      .then(function (data) { iconCache = data; build(data); })
      .catch(function () { setStatus("Could not load the icons.", "error"); });
  }

  /* ---- one-of-a-set fields (category, transmission, fuel) ---- */

  function openChoicePicker(el) {
    closePopover();
    var field = el.dataset.editChoice.split(":").pop();
    var current = el.innerText.trim();

    function build(sets) {
      var options = sets[field] || [];
      popover = document.createElement("div");
      popover.className = "edit-popover edit-popover--list";
      options.forEach(function (value) {
        var button = document.createElement("button");
        button.type = "button";
        button.textContent = value;
        button.setAttribute("aria-pressed", String(value === current));
        button.addEventListener("click", function () {
          el.textContent = value;
          markChanged(el.dataset.editChoice, value, el);
          closePopover();
        });
        popover.appendChild(button);
      });
      document.body.appendChild(popover);
      var box = el.getBoundingClientRect();
      popover.style.top = (window.scrollY + box.bottom + 8) + "px";
      popover.style.left = (window.scrollX + box.left) + "px";
    }

    if (choiceCache) { build(choiceCache); return; }
    fetch("/admin/api/choices")
      .then(function (r) { return r.json(); })
      .then(function (data) { choiceCache = data; build(data); })
      .catch(function () { setStatus("Could not load the options.", "error"); });
  }

  /* ---- adding and removing cars ---- */

  function addVehicle(button) {
    if (isDirty() && !window.confirm(
      "Save or discard your other changes first? Adding a car reloads the page.")) return;
    button.disabled = true;
    setStatus("Adding a car…");
    fetch("/admin/api/vehicle/new", {
      method: "POST", headers: { "X-CSRF-Token": csrf }
    })
      .then(function (r) { return r.json().then(function (d) {
        if (!r.ok) throw new Error(d.error || "Could not add a car."); return d; }); })
      .then(function (data) {
        changes = {};
        window.location = data.url + "?added=1";
      })
      .catch(function (error) { button.disabled = false; setStatus(error.message, "error"); });
  }

  function removeVehicle(button) {
    var id = button.dataset.editRemoveVehicle;
    var label = button.dataset.vehicleName || "this car";
    if (!window.confirm("Remove " + label + " from the fleet? This cannot be undone.")) return;
    button.disabled = true;
    fetch("/admin/api/vehicle/" + id + "/delete", {
      method: "POST", headers: { "X-CSRF-Token": csrf }
    })
      .then(function (r) { return r.json().then(function (d) {
        if (!r.ok) throw new Error(d.error || "Could not remove it."); return d; }); })
      .then(function (data) { changes = {}; window.location = data.url; })
      .catch(function (error) { button.disabled = false; setStatus(error.message, "error"); });
  }

  function toggleListed(button) {
    var id = button.dataset.editListedVehicle;
    button.disabled = true;
    fetch("/admin/api/vehicle/" + id + "/listed", {
      method: "POST", headers: { "X-CSRF-Token": csrf }
    })
      .then(function (r) { return r.json().then(function (d) {
        if (!r.ok) throw new Error(d.error || "Could not change it."); return d; }); })
      .then(function (data) {
        button.disabled = false;
        button.textContent = data.listed ? "Hide from the site" : "Show on the site";
        setStatus(data.listed ? "This car is now on the site." : "This car is now hidden.", "saved");
      })
      .catch(function (error) { button.disabled = false; setStatus(error.message, "error"); });
  }

  document.addEventListener("click", function (event) {
    if (!editing) return;
    var image = event.target.closest("[data-edit-image]");
    if (image) { event.preventDefault(); pickImage(image); return; }
    var icon = event.target.closest("[data-edit-icon]");
    if (icon) { event.preventDefault(); openIconPicker(icon); return; }
    var choice = event.target.closest("[data-edit-choice]");
    if (choice) { event.preventDefault(); openChoicePicker(choice); return; }
    var add = event.target.closest("[data-edit-add-vehicle]");
    if (add) { event.preventDefault(); addVehicle(add); return; }
    var remove = event.target.closest("[data-edit-remove-vehicle]");
    if (remove) { event.preventDefault(); removeVehicle(remove); return; }
    var listed = event.target.closest("[data-edit-listed-vehicle]");
    if (listed) { event.preventDefault(); toggleListed(listed); return; }
    if (popover && !event.target.closest(".edit-popover")) closePopover();
  });

  /* ---- mode switching ---- */

  function startEditing() {
    editing = true;
    document.body.classList.add("is-editing");
    document.querySelectorAll("[data-edit]").forEach(enableText);
    document.querySelectorAll("[data-edit-list]").forEach(enableList);
    document.querySelectorAll("[data-edit-only]").forEach(function (el) { el.hidden = false; });
    toggleBtn.hidden = true;
    saveBtn.hidden = false;
    cancelBtn.hidden = false;
    setStatus("Click any text, picture or icon to change it.");
  }

  function stopEditing() {
    editing = false;
    document.body.classList.remove("is-editing");
    document.querySelectorAll("[data-edit]").forEach(disableText);
    document.querySelectorAll("[data-edit-item] span").forEach(function (span) {
      span.removeAttribute("contenteditable");
    });
    document.querySelectorAll(".js-edit-add, .edit-item-tools").forEach(function (el) { el.remove(); });
    document.querySelectorAll("[data-edit-only]").forEach(function (el) { el.hidden = true; });
    closePopover();
    changes = {};
    toggleBtn.hidden = false;
    saveBtn.hidden = true;
    cancelBtn.hidden = true;
    setStatus("");
  }

  toggleBtn.addEventListener("click", startEditing);

  cancelBtn.addEventListener("click", function () {
    if (isDirty() && !window.confirm("Discard your unsaved changes?")) return;
    if (isDirty()) { changes = {}; window.location.reload(); return; }
    stopEditing();
  });

  saveBtn.addEventListener("click", function () {
    if (!isDirty()) { stopEditing(); return; }

    var payload = { settings: {}, records: {} };
    Object.keys(changes).forEach(function (key) {
      if (key.indexOf(":") === -1) payload.settings[key] = changes[key];
      else payload.records[key] = changes[key];
    });

    saveBtn.disabled = true;
    setStatus("Saving…");

    fetch("/admin/api/save", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
      body: JSON.stringify(payload)
    })
      .then(function (response) { return response.json().then(function (data) {
        if (!response.ok) throw new Error(data.error || "Could not save.");
        return data;
      }); })
      .then(function (data) {
        changes = {};
        setStatus("Saved. Reloading…", "saved");
        window.location.reload();
      })
      .catch(function (error) {
        saveBtn.disabled = false;
        setStatus(error.message, "error");
      });
  });

  window.addEventListener("beforeunload", function (event) {
    if (editing && isDirty()) {
      event.preventDefault();
      event.returnValue = "";
    }
  });
})();

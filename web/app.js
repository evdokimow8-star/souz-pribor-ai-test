"use strict";
const $ = (id) => document.getElementById(id);
const EXAMPLES = ["testo-510", "testo-511"];
const CUSTOM = "custom";
const KEYS = [...EXAMPLES, CUSTOM];
const STORAGE = "pribor-studio-workspace";
const STORAGE_LIMIT = 800000;
const wide = matchMedia("(min-width: 1000px)");
const samples = new Map();
const pendingDraftFields = new Map();
const storedDraftBaselines = new Map();
const storageWarning = $("storage-warning").textContent;
let pendingSelection = null;
let storageWriteQueued = false;
let storageWriteRequested = false;
let workspaceDatabase = null;
let workspace = { version: 2, selected: "testo-510", entries: {} };
let config = { ready: false },
  activeView = "card",
  inputMode = "form";
let busy = false,
  loadingExample = true,
  storageAvailable = true,
  formInvalid = false;
let toastTimer,
  generationError = false;
const entry = () => workspace.entries[workspace.selected];
const charCount = (value) => [...value].length;
const blankProduct = () => ({
  name: "",
  manufacturer: "",
  description: "",
  specs: {},
  source_url: "",
  notes: [],
});
const blankEntry = () => ({
  raw: JSON.stringify(blankProduct(), null, 2),
  result: null,
  edits: null,
  mode: "form",
});
const icon = (name) => {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  const use = document.createElementNS(svg.namespaceURI, "use");
  svg.setAttribute("aria-hidden", "true");
  use.setAttribute("href", "#i-" + name);
  svg.append(use);
  return svg;
};
function node(tag, text, cls) {
  const el = document.createElement(tag);
  if (text != null) el.textContent = text;
  if (cls) el.className = cls;
  return el;
}
function validUrl(value) {
  try {
    if (typeof value !== "string" || !/^https?:\/\//i.test(value)) return "";
    const u = new URL(value);
    return ["https:", "http:"].includes(u.protocol) &&
      !u.username &&
      !u.password
      ? u.href
      : "";
  } catch {
    return "";
  }
}
function clone(value) {
  return JSON.parse(JSON.stringify(value));
}
function canonical(value) {
  if (Array.isArray(value)) return value.map(canonical);
  if (value && typeof value === "object")
    return Object.fromEntries(
      Object.keys(value)
        .sort()
        .map((k) => [k, canonical(value[k])]),
    );
  return value;
}
function fingerprint(raw) {
  try {
    return JSON.stringify(canonical(parseProduct(raw)));
  } catch {
    return "invalid:" + raw;
  }
}
function hasDuplicateKeys(raw) {
  let i = 0;
  const ws = () => {
    while (/\s/.test(raw[i] || "") && i < raw.length) i++;
  };
  const str = () => {
    const start = i++;
    while (i < raw.length) {
      if (raw[i] === "\\") {
        i += 2;
        continue;
      }
      if (raw[i++] === '"') break;
    }
    return JSON.parse(raw.slice(start, i));
  };
  function value(depth = 0) {
    if (depth > 50) throw new Error("Слишком глубокая структура JSON.");
    ws();
    if (raw[i] === "{") {
      const keys = new Set();
      i++;
      ws();
      if (raw[i] === "}") {
        i++;
        return;
      }
      while (i < raw.length) {
        ws();
        const key = str();
        if (keys.has(key))
          throw new Error(
            "В JSON повторяется ключ «" + key + "». Оставьте одно значение.",
          );
        keys.add(key);
        ws();
        i++;
        value(depth + 1);
        ws();
        if (raw[i++] === "}") return;
      }
    } else if (raw[i] === "[") {
      i++;
      ws();
      if (raw[i] === "]") {
        i++;
        return;
      }
      while (i < raw.length) {
        value(depth + 1);
        ws();
        if (raw[i++] === "]") return;
      }
    } else if (raw[i] === '"') str();
    else {
      while (i < raw.length && !/[\s,\]}]/.test(raw[i])) i++;
    }
  }
  value();
}
function parseProduct(raw, allowIncomplete = false) {
  let p;
  try {
    p = JSON.parse(raw);
  } catch {
    throw new Error("Некорректный JSON. Проверьте кавычки, запятые и скобки.");
  }
  hasDuplicateKeys(raw);
  if (!p || typeof p !== "object" || Array.isArray(p))
    throw new Error("Нужен JSON-объект с данными одного прибора.");
  const allowed = [
    "name",
    "manufacturer",
    "description",
    "specs",
    "source_url",
    "notes",
  ];
  const extra = Object.keys(p).filter((k) => !allowed.includes(k));
  if (extra.length)
    throw new Error("Неизвестные поля: " + extra.join(", ") + ".");
  function text(v, label, max, required = false) {
    if (v == null && !required) return "";
    if (typeof v !== "string" || charCount(v) > max)
      throw new Error(
        "Поле «" + label + "»: нужен текст до " + max + " символов.",
      );
    if (/[\u0000-\u0008\u000b\u000c\u000e-\u001f]/.test(v))
      throw new Error("Уберите служебные символы в поле «" + label + "».");
    if (/[\uD800-\uDFFF]/u.test(v))
      throw new Error("Исправьте повреждённый символ в поле «" + label + "».");
    if (required && !v.trim())
      throw new Error("Заполните поле «" + label + "».");
    return v.trim();
  }
  const specs = p.specs === undefined ? {} : p.specs;
  if (
    !specs ||
    typeof specs !== "object" ||
    Array.isArray(specs) ||
    Object.keys(specs).length > 40
  )
    throw new Error(
      "Характеристики должны быть объектом, не более 40 параметров.",
    );
  const normalized = Object.create(null);
  for (const [key, val] of Object.entries(specs)) {
    const name = text(key, "Название характеристики", 100, true);
    if (Object.hasOwn(normalized, name))
      throw new Error("Есть две характеристики с названием «" + name + "».");
    normalized[name] = text(val, name, 600);
  }
  const source = text(p.source_url, "Источник", 1500);
  if (source && !validUrl(source))
    throw new Error(
      "Укажите обычную ссылку на источник, начинающуюся с https:// или http://.",
    );
  const notes = p.notes === undefined ? [] : p.notes;
  if (!Array.isArray(notes) || notes.length > 10)
    throw new Error("Можно добавить до 10 замечаний, каждое с новой строки.");
  return {
    name: text(p.name, "Название", 160, !allowIncomplete),
    manufacturer: text(p.manufacturer, "Производитель", 160),
    description: text(p.description, "Описание", 6000),
    specs: normalized,
    source_url: source,
    notes: notes.map((n) => text(n, "Замечание", 600, true)),
  };
}
function safeResult(result) {
  const record = (v) =>
    v !== null && typeof v === "object" && !Array.isArray(v);
  const string = (v, max = 10000) =>
    typeof v === "string" && charCount(v) <= max;
  const strings = (v, max = 50, chars = 10000) =>
    Array.isArray(v) && v.length <= max && v.every((s) => string(s, chars));
  const block = (v) =>
    record(v) && string(v.text, 4000) && strings(v.fact_ids, 50, 100);
  if (
    !record(result) ||
    !string(result.raw, STORAGE_LIMIT) ||
    !string(result.markdown, 100000)
  )
    return false;
  const c = result.card;
  return (
    record(c) &&
    string(c.seo_title, 70) &&
    string(c.seo_description, 1200) &&
    string(c.commercial, 2000) &&
    string(c.source_url, 1500) &&
    Array.isArray(c.characteristics) &&
    c.characteristics.length <= 40 &&
    c.characteristics.every(
      (s) => record(s) && string(s.name, 100) && string(s.value, 600),
    ) &&
    strings(c.search_queries, 5, 140) &&
    strings(c.missing_fields) &&
    strings(c.warnings) &&
    record(c.validation) &&
    Number.isInteger(c.validation.title_characters) &&
    c.validation.title_characters >= 0 &&
    Number.isInteger(c.validation.description_characters) &&
    c.validation.description_characters >= 0 &&
    typeof c.validation.requires_editor_review === "boolean" &&
    string(c.validation.numeric_and_technical_check, 50) &&
    record(c.generation) &&
    (c.generation.model == null || string(c.generation.model, 300)) &&
    (c.generation.first_model == null || string(c.generation.first_model, 300)) &&
    (c.generation.reported_cost_usd == null ||
      (Number.isFinite(c.generation.reported_cost_usd) &&
        c.generation.reported_cost_usd >= 0)) &&
    record(c.evidence) &&
    record(c.evidence.facts) &&
    Object.keys(c.evidence.facts).length <= 50 &&
    Object.values(c.evidence.facts).every((v) => string(v)) &&
    record(c.evidence.draft) &&
    block(c.evidence.draft.seo_title) &&
    Array.isArray(c.evidence.draft.description_paragraphs) &&
    c.evidence.draft.description_paragraphs.length <= 10 &&
    c.evidence.draft.description_paragraphs.every(block) &&
    Array.isArray(c.evidence.draft.search_queries) &&
    c.evidence.draft.search_queries.length <= 5 &&
    c.evidence.draft.search_queries.every(block) &&
    block(c.evidence.draft.commercial)
  );
}
function parseWorkspace(raw) {
  if (!raw) return;
  if (raw.length > STORAGE_LIMIT) throw new Error("Размер");
  const saved = JSON.parse(raw);
  if (
    saved.version !== 2 ||
    !KEYS.includes(saved.selected) ||
    !saved.entries ||
    typeof saved.entries !== "object"
  )
    return;
  const clean = {};
  for (const key of KEYS) {
    const e = saved.entries[key];
    if (!e || typeof e.raw !== "string") continue;
    clean[key] = {
      raw: e.raw,
      result: safeResult(e.result) ? e.result : null,
      edits: null,
      mode: e.mode === "json" ? "json" : "form",
    };
    if (
      e.edits &&
      typeof e.edits.seo_title === "string" &&
      typeof e.edits.seo_description === "string" &&
      typeof e.edits.commercial === "string" &&
      charCount(e.edits.seo_title) <= 70 &&
      charCount(e.edits.seo_description) <= 1200 &&
      charCount(e.edits.commercial) <= 2000
    )
      clean[key].edits = {
        seo_title: e.edits.seo_title,
        seo_description: e.edits.seo_description,
        commercial: e.edits.commercial,
      };
  }
  return { version: 2, selected: saved.selected, entries: clean };
}
function legacyWorkspace() {
  try {
    return parseWorkspace(
      localStorage.getItem(STORAGE + "-v3") ??
        localStorage.getItem(STORAGE + "-v2"),
    );
  } catch {
    return null;
  }
}
function openWorkspaceDatabase() {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open(STORAGE, 1);
    let abandoned = false;
    const fail = () => {
      abandoned = true;
      clearTimeout(timeout);
      reject(new Error("Хранилище недоступно"));
    };
    const timeout = setTimeout(fail, 1000);
    request.onblocked = fail;
    request.onerror = fail;
    request.onupgradeneeded = () =>
      request.result.createObjectStore("workspace");
    request.onsuccess = () => {
      clearTimeout(timeout);
      if (abandoned) request.result.close();
      else resolve(request.result);
    };
  });
}
function workspaceTransaction(mode, update) {
  return new Promise((resolve, reject) => {
    const transaction = workspaceDatabase.transaction("workspace", mode);
    const store = transaction.objectStore("workspace");
    const request = store.get("snapshot");
    let stored;
    request.onsuccess = () => {
      try {
        stored = parseWorkspace(request.result);
        if (update) {
          const replacement = update(stored);
          if (replacement) store.put(replacement, "snapshot");
        }
      } catch {
        transaction.abort();
      }
    };
    transaction.oncomplete = () => resolve(stored);
    transaction.onabort = () =>
      reject(transaction.error || new Error("Запись отменена"));
    transaction.onerror = () => reject(transaction.error || request.error);
  });
}
async function restoreWorkspace() {
  const legacy = legacyWorkspace();
  try {
    workspaceDatabase = await openWorkspaceDatabase();
    const saved = await workspaceTransaction("readwrite", (current) =>
      !current && legacy ? JSON.stringify(legacy) : null,
    );
    workspace = saved || legacy || workspace;
  } catch {
    workspace = legacy || workspace;
    storageAvailable = false;
    workspaceDatabase?.close();
    workspaceDatabase = null;
    updateStorageStatus(false);
  }
  for (const [key, draft] of Object.entries(workspace.entries))
    storedDraftBaselines.set(key, { ...draft });
}
function updateStorageStatus(conflict) {
  $("storage-warning").hidden = storageAvailable;
  $("storage-warning").textContent = conflict
    ? "Этот черновик изменён в другой вкладке. Ваши правки остались здесь. Скопируйте исходный JSON и скачайте нужную карточку перед перезагрузкой."
    : storageWarning;
  $("draft-status").textContent = storageAvailable
    ? "Сохранено в браузере"
    : "Не сохранено";
}
function persist(fields, select = false) {
  const key = workspace.selected;
  if (entry()) {
    const pending = pendingDraftFields.get(key) || new Set();
    for (const field of fields) pending.add(field);
    pendingDraftFields.set(key, pending);
  }
  if (select) pendingSelection = key;
  if (!workspaceDatabase) return;
  storageWriteRequested = true;
  if (!storageWriteQueued) void flushWorkspace();
}
async function flushWorkspace() {
  storageWriteRequested = false;
  storageWriteQueued = true;
  if (storageAvailable) $("draft-status").textContent = "Сохраняется…";
  let conflict = false;
  let selection;
  const written = [];
  try {
    await workspaceTransaction("readwrite", (stored) => {
      const saved = stored || {
        version: 2,
        selected: workspace.selected,
        entries: {},
      };
      selection = pendingSelection;
      if (selection) saved.selected = selection;
      for (const [key, pending] of pendingDraftFields) {
        const draft = workspace.entries[key];
        const latest = saved.entries[key];
        const baseline = storedDraftBaselines.get(key);
        const compared = new Set(pending);
        if (pending.has("edits")) compared.add("result");
        if (
          [...compared].some(
            (field) =>
              JSON.stringify(latest?.[field]) !==
                JSON.stringify(baseline?.[field]) &&
              JSON.stringify(latest?.[field]) !== JSON.stringify(draft[field]),
          )
        ) {
          conflict = true;
          continue;
        }
        saved.entries[key] ||= { ...draft };
        for (const field of pending) saved.entries[key][field] = draft[field];
        written.push([key, [...pending], { ...draft }]);
      }
      const serialized = JSON.stringify(saved);
      if (serialized.length > STORAGE_LIMIT) throw new Error("Размер");
      return serialized;
    });
    if (pendingSelection === selection) pendingSelection = null;
    for (const [key, fields, draft] of written) {
      const baseline = storedDraftBaselines.get(key) || { ...draft };
      const pending = pendingDraftFields.get(key);
      for (const field of fields) {
        baseline[field] = draft[field];
        if (workspace.entries[key][field] === draft[field])
          pending.delete(field);
      }
      storedDraftBaselines.set(key, baseline);
      if (pending.size === 0) pendingDraftFields.delete(key);
    }
    storageAvailable = !conflict;
  } catch {
    storageAvailable = false;
  } finally {
    storageWriteQueued = false;
    updateStorageStatus(conflict);
    if (storageWriteRequested) void flushWorkspace();
  }
}
function announce(text) {
  $("announcement").textContent = text;
}
function toast(text) {
  clearTimeout(toastTimer);
  $("toast").textContent = text;
  $("toast").hidden = false;
  toastTimer = setTimeout(() => {
    $("toast").hidden = true;
  }, 3500);
}
function clearError() {
  $("error").hidden = true;
  $("error").replaceChildren();
  generationError = false;
}
function showError(err, focus = true) {
  const box = $("error");
  box.replaceChildren(
    node(
      "strong",
      err.message || "Не удалось выполнить действие. Ваши данные сохранены.",
    ),
  );
  if (Array.isArray(err.issues) && err.issues.length) {
    const list = node("ul");
    err.issues.forEach((x) => list.append(node("li", x)));
    box.append(list);
  }
  box.hidden = false;
  if (focus) {
    box.focus({ preventScroll: true });
    box.scrollIntoView({ block: "nearest", behavior: "auto" });
  }
}
async function api(path, options = {}) {
  const controller = new AbortController();
  const timer = setTimeout(
    () => controller.abort(),
    options.method === "POST" ? 75000 : 18000,
  );
  try {
    const response = await fetch(
      new URL(path.replace(/^\/+/, ""), new URL(".", location.href)),
      { ...options, signal: controller.signal },
    );
    let data;
    try {
      data = await response.json();
    } catch {
      throw new Error(
        "Сервис вернул неполный ответ. Данные сохранены. Попробуйте позднее.",
      );
    }
    if (!response.ok)
      throw (
        data.error ||
        new Error("Сервис временно недоступен. Ваши данные сохранены.")
      );
    return data;
  } catch (err) {
    if (err.name === "AbortError")
      throw new Error(
        options.method === "POST"
          ? "Ответ не получен вовремя. Предыдущая карточка сохранена. Автоматического повтора не будет; новый запуск может быть платным."
          : "Сервис отвечает слишком долго. Попробуйте загрузить пример снова.",
      );
    if (err instanceof TypeError)
      throw new Error(
        "Не удалось связаться с сервисом. Проверьте интернет. Ваши данные и предыдущая карточка сохранены.",
      );
    throw err;
  } finally {
    clearTimeout(timer);
  }
}
function isStale() {
  return (
    !!entry()?.result &&
    (formInvalid ||
      fingerprint(entry().raw) !== fingerprint(entry().result.raw))
  );
}
function syncNavigation() {
  const desktop = wide.matches;
  if (desktop && activeView === "data") activeView = "card";
  $("tab-data").hidden = desktop;
  for (const view of ["card", "data", "checks"]) {
    const tab = $("tab-" + view),
      panel = $("panel-" + view);
    const selected = activeView === view;
    tab.setAttribute("aria-selected", String(selected));
    tab.tabIndex = selected ? 0 : -1;
    panel.hidden = view === "data" ? !desktop && !selected : !selected;
  }
  $("panel-data").setAttribute("role", desktop ? "region" : "tabpanel");
  $("panel-data").setAttribute(
    "aria-labelledby",
    desktop ? "data-heading" : "tab-data",
  );
  $("result-actions").hidden =
    !entry()?.result || (!desktop && activeView === "data");
}
function setView(view, focus = false) {
  activeView = view;
  syncNavigation();
  if (focus) {
    const target =
      view === "data"
        ? $("data-heading")
        : view === "checks"
          ? $("checks-heading")
          : entry()?.result
            ? $("seo-title")
            : $("empty");
    target.setAttribute("tabindex", "-1");
    target.focus({ preventScroll: true });
    const scrollTarget =
      view === "card" && entry()?.result
        ? matchMedia("(min-width: 601px)").matches
          ? $("result-actions")
          : document.querySelector(".product-header")
        : target;
    scrollTarget.scrollIntoView({ block: "start", behavior: "auto" });
  }
}
function updateControls() {
  const locked = busy || loadingExample;
  $("generate").disabled = locked || !config.ready || !entry();
  $("generate-label").textContent = busy
    ? "ИИ готовит карточку…"
    : "Создать карточку с ИИ";
  $("data-heading").textContent =
    workspace.selected === CUSTOM ? "Данные вашего прибора" : "Исходные данные";
  $("reset-draft-label").textContent =
    workspace.selected === CUSTOM
      ? "Очистить свой прибор"
      : "Сбросить правки примера";
  $("form-fields").disabled = locked;
  $("product-json").disabled = locked;
  for (const id of [
    "mode-form",
    "mode-json",
    "apply-json",
    "reset-draft",
    "add-spec",
    "edit-result",
  ])
    $(id).disabled = locked;
  document.querySelectorAll("[data-example]").forEach((b) => {
    b.disabled = locked;
    b.setAttribute(
      "aria-pressed",
      String(b.dataset.example === workspace.selected),
    );
  });
  const stale = isStale();
  $("stale-notice").hidden = !stale;
  if ($("result-state"))
    $("result-state").textContent = entry()?.edits
      ? "Ручная редакция"
      : stale
        ? "Предыдущая версия"
        : "Соответствует исходным данным";
  $("loading").hidden = !busy;
  $("panel-card").setAttribute("aria-busy", String(busy || loadingExample));
  $("checks-count").hidden = !entry()?.result;
  for (const key of KEYS) {
    const sample = samples.get(key),
      saved = workspace.entries[key];
    const dirty =
      sample &&
      saved &&
      fingerprint(saved.raw) !== fingerprint(JSON.stringify(sample.product));
    const dot = document.querySelector(
      '[data-example="' + key + '"] .draft-dot',
    );
    if (dot) dot.hidden = !dirty;
  }
  syncNavigation();
}
function specField(name, value, index) {
  const row = node("div", null, "spec-field");
  row.dataset.specRow = "";
  const key = node("input");
  key.type = "text";
  key.value = name;
  key.maxLength = 100;
  key.className = "spec-name";
  key.dataset.specName = "";
  key.setAttribute("aria-label", "Название характеристики " + (index + 1));
  const val = node("textarea");
  val.value = value ?? "";
  val.rows = 2;
  val.maxLength = 600;
  val.dataset.specValue = "";
  val.setAttribute("aria-label", "Значение характеристики " + (index + 1));
  val.placeholder = "Нет данных — оставьте пустым";
  const remove = node("button", null, "remove-spec");
  remove.type = "button";
  remove.setAttribute(
    "aria-label",
    "Удалить характеристику " + (name || index + 1),
  );
  remove.append(icon("close"));
  remove.addEventListener("click", () => {
    row.remove();
    onFormInput();
    $("add-spec").focus();
  });
  row.append(key, val, remove);
  return row;
}
function fillForm(p) {
  $("product-name").value = p.name || "";
  $("product-manufacturer").value = p.manufacturer || "";
  $("product-description").value = p.description || "";
  $("product-source").value = p.source_url || "";
  $("product-notes").value = (p.notes || []).join("\n");
  $("spec-fields").replaceChildren(
    ...Object.entries(p.specs || {}).map(([k, v], i) => specField(k, v, i)),
  );
}
function setMode(mode, validate = true) {
  if (mode === "form" && validate) {
    try {
      fillForm(parseProduct($("product-json").value, true));
      formInvalid = false;
    } catch (err) {
      showError(err);
      return false;
    }
  }
  inputMode = mode;
  if (entry()) entry().mode = mode;
  $("form-fields").hidden = mode !== "form";
  $("json-editor").hidden = mode !== "json";
  $("mode-form").setAttribute("aria-pressed", String(mode === "form"));
  $("mode-json").setAttribute("aria-pressed", String(mode === "json"));
  if (entry() && validate) persist(["mode"]);
  return true;
}
function fillCurrentInput() {
  formInvalid = false;
  const e = entry();
  $("product-json").value = e.raw;
  try {
    fillForm(parseProduct(e.raw, true));
    setMode(e.mode || "form", false);
  } catch {
    setMode("json", false);
    formInvalid = true;
  }
}
function onFormInput() {
  if (!entry() || busy) return;
  const specs = Object.create(null);
  let duplicate = false;
  document.querySelectorAll("[data-spec-row]").forEach((row) => {
    const key = row.querySelector("[data-spec-name]").value;
    if (Object.hasOwn(specs, key)) duplicate = true;
    specs[key] = row.querySelector("[data-spec-value]").value || null;
  });
  const p = {
    name: $("product-name").value,
    manufacturer: $("product-manufacturer").value,
    description: $("product-description").value,
    specs,
    source_url: $("product-source").value,
    notes: $("product-notes")
      .value.split("\n")
      .filter((x) => x.trim()),
  };
  // Preserve every row while a duplicate is being edited; never silently drop a parameter.
  const rows = [...document.querySelectorAll("[data-spec-row]")].map((row) => [
    row.querySelector("[data-spec-name]").value,
    row.querySelector("[data-spec-value]").value || null,
  ]);
  let raw = JSON.stringify(p, null, 2);
  if (duplicate) {
    const specRaw =
      "{\n" +
      rows
        .map(([k, v]) => "    " + JSON.stringify(k) + ": " + JSON.stringify(v))
        .join(",\n") +
      "\n  }";
    raw = raw.replace(/"specs": \{[\s\S]*?\n  \}/, '"specs": ' + specRaw);
  }
  entry().raw = raw;
  $("product-json").value = raw;
  formInvalid = duplicate;
  clearError();
  persist(["raw"]);
  updateControls();
}
function remarks(card) {
  return [...new Set([...card.missing_fields, ...card.warnings])];
}
function currentCard() {
  if (!entry()?.result) return null;
  const c = clone(entry().result.card);
  if (entry().edits) {
    Object.assign(c, entry().edits);
    c.editor_revision = {
      manual: true,
      needs_review: true,
      original_model_title: entry().result.card.seo_title,
    };
    c.validation.title_characters = [...c.seo_title].length;
    c.validation.description_characters = [...c.seo_description].length;
    c.validation.numeric_and_technical_check = "needs_editor_review";
    c.validation.requires_editor_review = true;
    c.warnings.push(
      "Текст изменён редактором после ответа модели. Проверьте факты и формулировки повторно.",
    );
  }
  return c;
}
function evidenceBlocks(c) {
  const d = c.evidence.draft;
  return [
    d.seo_title,
    ...d.description_paragraphs,
    ...d.search_queries,
    d.commercial,
  ].filter((b) => b && typeof b.text === "string" && Array.isArray(b.fact_ids));
}
function evidenceButton(index) {
  const b = node("button", "Проверить по источнику", "evidence-action");
  b.type = "button";
  b.dataset.evidenceIndex = String(index);
  b.prepend(icon("shield"));
  b.addEventListener("click", () => {
    setView("checks");
    const details = document.querySelector(
      '[data-evidence-block="' + index + '"]',
    );
    if (details) {
      details.open = true;
      details.classList.add("focus-evidence");
      details.querySelector("summary").focus({ preventScroll: true });
      details.scrollIntoView({ block: "start", behavior: "auto" });
      setTimeout(() => details.classList.remove("focus-evidence"), 1800);
    }
  });
  return b;
}
function renderResult() {
  const c = currentCard();
  $("result").hidden = !c;
  $("empty").hidden = !!c;
  $("checks-empty").hidden = !!c;
  $("checks-content").hidden = !c;
  if (!c) {
    if (workspace.selected === CUSTOM) {
      $("empty").querySelector("h2").textContent = "Создайте карточку с ИИ";
      $("empty").querySelector("p").textContent =
        "Заполните данные вашего прибора и нажмите «Создать карточку с ИИ». Здесь появится результат.";
      $("saved").hidden = true;
    }
    updateControls();
    return;
  }
  let resultProduct;
  try {
    resultProduct = parseProduct(entry().result.raw);
  } catch {
    resultProduct = {};
  }
  const manual = !!entry().edits,
    warnings = remarks(c),
    stale = isStale();
  $("seo-title").textContent = c.seo_title;
  $("product-maker").textContent = resultProduct.manufacturer || "Прибор";
  $("title-count").textContent = c.validation.title_characters + " / 70";
  $("description-count").textContent =
    c.validation.description_characters + " / 800–1200";
  $("manual-notice").hidden = !manual;
  $("draft-badge").querySelector("span").textContent = manual
    ? "Текст изменён вручную"
    : "Формат и числа проверены";
  $("draft-badge").classList.toggle("manual", manual);
  const savedLabel =
    c.generation.editor_reviewed === true
      ? "Пример ИИ · вычитан редактором"
      : "Пример ответа ИИ · бесплатно";
  $("generation-mode").textContent = manual
    ? "Ваша редакция"
    : c.generation.viewing_saved_result
      ? savedLabel
      : "Создано с ИИ · новый ответ";
  $("mobile-generation-mode").textContent = manual
    ? "Ваша редакция · черновик"
    : c.generation.viewing_saved_result
      ? savedLabel
      : "Создано с ИИ · черновик";
  const matchingSample = EXAMPLES.find(
    (k) =>
      resultProduct.name === k.replace("-", " ") &&
      resultProduct.source_url?.includes("/" + k + "/"),
  );
  $("product-photo").hidden = !matchingSample;
  $("product-placeholder").hidden = !!matchingSample;
  if (matchingSample) {
    $("product-photo").src = "./assets/" + matchingSample + ".jpg";
    $("product-photo").alt =
      "Прибор " + resultProduct.name + " из каталога Союз-Прибор";
  }
  const paragraphs = c.seo_description.split("\n\n").filter(Boolean);
  const desc = $("seo-description");
  desc.replaceChildren();
  paragraphs.forEach((text, index) => {
    desc.append(node("p", text));
    if (!manual && index === paragraphs.length - 1)
      desc.append(evidenceButton(1));
  });
  $("specs").replaceChildren(
    ...c.characteristics.map((s) => {
      const r = node("div");
      r.append(
        node("dt", s.name),
        node("dd", s.value, s.value === "данных нет" ? "no-data" : ""),
      );
      return r;
    }),
  );
  $("queries").replaceChildren(...c.search_queries.map((s) => node("li", s)));
  $("commercial").textContent = c.commercial;
  $("checks-count").textContent = warnings.length;
  $("checks-count").setAttribute("aria-label", "Уточнений: " + warnings.length);
  $("review-warnings").textContent = warnings.length
    ? warnings.length + " для уточнения"
    : "Посмотреть проверки";
  $("warnings-box").hidden = !warnings.length;
  $("warning-summary-text").textContent = warnings[0] || "";
  $("warnings").replaceChildren(
    ...(warnings.length
      ? warnings
      : [
          "Пропущенных данных не обнаружено. Смысловую точность текста проверяет редактор.",
        ]
    ).map((s) => node("li", s)),
  );
  $("check-status").querySelector("h3").textContent = manual
    ? "Ручная редакция требует проверки"
    : "Формальные проверки пройдены";
  $("check-status").querySelector("p").textContent = manual
    ? "Данные и связи ниже относятся к исходному ответу модели. После ручной правки заново проверьте формулировки и числа."
    : "Проверены структура, длина, числа и технические обозначения. Характеристики перенесены из входа.";
  $("evidence-help").textContent = manual
    ? "Здесь сохранены доказательства для исходного ответа модели, до вашей правки."
    : "Раскройте фрагмент, чтобы сопоставить его с исходными сведениями.";
  const source = validUrl(c.source_url);
  $("source").hidden = !source;
  if (source) {
    $("source").href = source;
    $("source-host").textContent = new URL(source).hostname;
  } else $("source").removeAttribute("href");
  $("evidence-list").replaceChildren(
    ...evidenceBlocks(c).map((block, index) => {
      const d = node("details", null, "evidence-entry");
      d.dataset.evidenceBlock = String(index);
      d.append(node("summary", block.text));
      const facts = node("div");
      block.fact_ids.forEach((id) => {
        if (!Object.hasOwn(c.evidence.facts, id)) return;
        const row = node("div", null, "fact-row");
        const labels = {
          name: "Название",
          manufacturer: "Производитель",
          description: "Описание из источника",
        };
        row.append(
          node("strong", labels[id] || "Исходная характеристика"),
          node("p", String(c.evidence.facts[id])),
        );
        facts.append(row);
      });
      d.append(facts);
      return d;
    }),
  );
  const cost = c.generation.reported_cost_usd;
  const firstModel = c.generation.first_model;
  const rows = [
    ...(firstModel
      ? [["Модель черновика", firstModel], ["Модель исправления", c.generation.model]]
      : [["Модель", c.generation.model || "Не указана"]]),
    [
      "Режим",
      c.generation.viewing_saved_result
        ? "Сохранённый ответ API"
        : "Новый ответ API",
    ],
    [
      firstModel ? "Общая стоимость двух вызовов" : "Стоимость, сообщённая API",
      cost == null
        ? firstModel ? "Стоимость одного или обоих вызовов неизвестна" : "Не сообщена"
        : "$" + Number(cost).toFixed(6),
    ],
    [
      "Текущее состояние",
      manual
        ? "Ручная редакция"
        : stale
          ? "Предыдущая версия"
          : "Соответствует исходным данным",
    ],
  ];
  $("request-details").replaceChildren(
    ...rows.map(([k, v]) => {
      const r = node("div");
      const value = node("dd", v);
      if (k === "Текущее состояние") value.id = "result-state";
      r.append(node("dt", k), value);
      return r;
    }),
  );
  updateControls();
}
async function loadSample(key) {
  if (!EXAMPLES.includes(key)) throw new Error("Готовый пример не найден.");
  if (samples.has(key)) return samples.get(key);
  const data = await api("/api/examples/" + key);
  if (
    !data.product ||
    !data.card ||
    !safeResult({
      card: data.card,
      markdown: data.markdown,
      raw: JSON.stringify(data.product),
    })
  )
    throw new Error(
      "Сохранённый пример временно недоступен. Попробуйте ещё раз.",
    );
  samples.set(key, data);
  return data;
}
async function refreshStoredDraft(key) {
  if (!workspaceDatabase || pendingDraftFields.has(key)) return;
  try {
    const latest = (await workspaceTransaction("readonly"))?.entries[key];
    if (latest && !pendingDraftFields.has(key)) {
      workspace.entries[key] = latest;
      storedDraftBaselines.set(key, { ...latest });
    }
  } catch {
    // Keep the in-memory draft available if storage cannot be read.
  }
}
function isOldSample(result) {
  return (
    result?.card.generation.viewing_saved_result === true &&
    result.card.generation.editor_reviewed !== true
  );
}
async function selectExample(key, focus = true) {
  if (busy || loadingExample || !KEYS.includes(key)) return;
  clearError();
  loadingExample = true;
  updateControls();
  const previous = workspace.selected;
  try {
    await refreshStoredDraft(key);
    if (key === CUSTOM) {
      workspace.selected = CUSTOM;
      workspace.entries[CUSTOM] ??= blankEntry();
      fillCurrentInput();
      renderResult();
      persist([], focus);
      setView(entry().result ? "card" : "data", focus);
      announce(
        "Введите данные своего прибора. Новый запрос к ИИ выполняется только кнопкой создания карточки.",
      );
      return;
    }
    const currentResult = workspace.entries[key]?.result;
    let sample = samples.get(key);
    if (!currentResult) {
      sample = await loadSample(key);
      await refreshStoredDraft(key);
    } else if (isOldSample(currentResult) && !sample) {
      // An older saved sample can be refreshed without losing offline access.
      sample = await loadSample(key).catch(() => null);
      await refreshStoredDraft(key);
    }
    workspace.selected = key;
    if (!workspace.entries[key])
      workspace.entries[key] = {
        raw: JSON.stringify(sample.product, null, 2),
        result: null,
        edits: null,
        mode: "form",
      };
    const e = entry();
    const needsResult =
      !!sample &&
      (!e.result ||
        (isOldSample(e.result) &&
          sample.card.generation.editor_reviewed === true));
    if (needsResult)
      e.result = {
        card: clone(sample.card),
        markdown: sample.markdown,
        raw: JSON.stringify(sample.product),
      };
    fillCurrentInput();
    renderResult();
    persist(needsResult ? ["result"] : [], focus);
    setView("card", focus);
    announce(
      "Открыта карточка " +
        key.replace("-", " ") +
        ". Просмотр без запроса к модели.",
    );
  } catch (err) {
    workspace.selected = previous;
    showError(err, focus);
    if (!entry()?.result) {
      $("empty").querySelector("h2").textContent = "Пример не загрузился";
      $("empty").querySelector("p").textContent =
        "Ваши сохранённые правки остаются в браузере. Проверьте интернет и попробуйте снова.";
      $("saved").hidden = false;
    }
  } finally {
    loadingExample = false;
    updateControls();
  }
}
function applyConfig(value) {
  config = value || { ready: false };
  if (config.public_demo) {
    const budget = config.budget;
    const modelName = {
      "google/gemini-3-flash-preview": "Gemini 3 Flash Preview",
      "google/gemini-3.8-flash": "Gemini 3.8 Flash",
    }[config.model] || config.model || "Модель";
    const repairNote = config.model === "google/gemini-3.8-flash"
      ? " Если проверка отклонит черновик, Gemini 4 Argon может исправить его одним дополнительным запросом из бюджета демо."
      : "";
    $("api-help").textContent = config.ready
      ? modelName + " через OpenRouter. До 3 запусков за 15 минут." + repairNote
      : budget?.unavailable_reason === "unconfirmed_cost"
        ? "Часть бюджета зарезервирована за запросами, стоимость которых ещё не подтверждена. Готовые примеры можно просматривать и скачивать."
        : "Новая генерация сейчас недоступна. Готовые примеры можно просматривать и скачивать.";
    $("budget-details").textContent = budget
      ? "Бюджет демо: $" +
        Number(budget.limit_usd).toFixed(2) +
        ". Доступно: $" +
        Number(budget.remaining_usd).toFixed(3) +
        ". Учёт расхода ведётся по стоимости, сообщённой API."
      : "";
  } else {
    $("api-help").textContent = config.ready
      ? "Один платный запрос к настроенному API. Автоматических повторов нет."
      : "Готовые примеры работают без ключа. Для новой генерации настройте API в .env и перезапустите локальный сервис.";
    $("budget-details").textContent =
      "Просмотр сохранённых примеров не обращается к модели. Новая генерация — отдельный запрос.";
  }
  $("retry-config").hidden = !!config.ready;
  updateControls();
}
async function refreshConfig() {
  try {
    applyConfig(await api("/api/config"));
  } catch {
    config = { ready: false };
    $("api-help").textContent =
      "Не удалось проверить генерацию. Сохранённые примеры остаются доступны.";
    $("retry-config").hidden = false;
    updateControls();
  }
}
async function generateCard() {
  if (busy || loadingExample || !config.ready || !entry()) return;
  clearError();
  const raw = entry().raw;
  try {
    const product = parseProduct(raw);
    if (charCount(product.name) > 70)
      throw new Error(
        "Сократите название прибора до 70 символов: оно должно целиком поместиться в SEO-заголовок.",
      );
    const maximum = config.public_demo ? 16000 : 32768;
    if (new TextEncoder().encode(raw).length > maximum)
      throw new Error(
        "Данные слишком велики для одного запроса (предел " +
          maximum +
          " байт). Сократите описание или замечания. Введённый текст не обрезан.",
      );
  } catch (err) {
    setView("data");
    showError(err);
    return;
  }
  const key = workspace.selected;
  let completed = false;
  busy = true;
  updateControls();
  setView("card", true);
  announce("Готовим черновик и проверяем его по исходным данным.");
  try {
    const data = await api("/api/generate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: raw,
    });
    if (!safeResult({ card: data.card, markdown: data.markdown, raw }))
      throw new Error("Получен неполный ответ. Предыдущая карточка сохранена.");
    workspace.entries[key].result = {
      card: data.card,
      markdown: data.markdown,
      raw,
    };
    workspace.entries[key].edits = null;
    persist(["result", "edits"]);
    renderResult();
    completed = true;
    announce("ИИ создал новую карточку. Посмотрите сведения для уточнения.");
  } catch (err) {
    generationError = true;
    renderResult();
    showError(err);
  } finally {
    busy = false;
    updateControls();
    if (completed) setView("card", true);
    if (config.public_demo) await refreshConfig();
  }
}
function plainText(c) {
  const list = remarks(c);
  return [
    c.seo_title,
    c.seo_description,
    "Характеристики",
    ...c.characteristics.map((x) => x.name + ": " + x.value),
    "Поисковые запросы",
    c.search_queries.join("\n"),
    "Для менеджера",
    c.commercial,
    ...(list.length ? ["Уточнить перед публикацией", list.join("\n")] : []),
    ...(validUrl(c.source_url) ? ["Источник: " + c.source_url] : []),
  ].join("\n\n");
}
function markdownText(c) {
  if (!entry().edits) return entry().result.markdown;
  return [
    "# " + c.seo_title,
    c.seo_description,
    "## Характеристики",
    ...c.characteristics.map((x) => "- **" + x.name + "**: " + x.value),
    "## Поисковые запросы",
    ...c.search_queries.map((x) => "- " + x),
    "## Для менеджера",
    c.commercial,
    "## Уточнить перед публикацией",
    ...remarks(c).map((x) => "- " + x),
    "Ручная редакция. Перед публикацией проверьте факты.",
    ...(validUrl(c.source_url) ? ["Источник: " + c.source_url] : []),
  ].join("\n\n");
}
function download(format) {
  const c = currentCard();
  if (!c) return;
  if (isStale())
    c.export_context = {
      previous_version: true,
      message: "Исходные данные изменены после получения этой карточки.",
    };
  let contents =
    format === "json" ? JSON.stringify(c, null, 2) : markdownText(c);
  if (format !== "json" && isStale())
    contents = "> Предыдущая версия: исходные данные изменены.\n\n" + contents;
  const blob = new Blob([contents], {
    type:
      format === "json"
        ? "application/json;charset=utf-8"
        : "text/markdown;charset=utf-8",
  });
  const url = URL.createObjectURL(blob),
    a = node("a");
  let name = workspace.selected;
  if (name === CUSTOM) {
    try {
      name =
        parseProduct(entry().result.raw)
          .name.toLowerCase()
          .normalize("NFKC")
          .replace(/[^\p{L}\p{N}]+/gu, "-")
          .replace(/^-|-$/g, "")
          .slice(0, 80) || "pribor";
    } catch {
      name = "pribor";
    }
  }
  a.href = url;
  a.download = name + "-card." + (format === "json" ? "json" : "md");
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1500);
  $("export-menu").open = false;
  announce("Файл " + a.download + " подготовлен.");
}
document
  .querySelectorAll("[data-view]")
  .forEach((b) => b.addEventListener("click", () => setView(b.dataset.view)));
document.querySelector(".view-tabs").addEventListener("keydown", (e) => {
  if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(e.key)) return;
  const tabs = [...document.querySelectorAll("[data-view]")].filter(
      (b) => !b.hidden,
    ),
    i = tabs.indexOf(document.activeElement);
  if (i < 0) return;
  e.preventDefault();
  const next =
    e.key === "Home"
      ? 0
      : e.key === "End"
        ? tabs.length - 1
        : (i + (e.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length;
  setView(tabs[next].dataset.view);
  tabs[next].focus();
});
wide.addEventListener("change", syncNavigation);
document
  .querySelectorAll("[data-open-view]")
  .forEach((b) =>
    b.addEventListener("click", () => setView(b.dataset.openView, true)),
  );
document
  .querySelectorAll("[data-example]")
  .forEach((b) =>
    b.addEventListener("click", () => selectExample(b.dataset.example)),
  );
$("form-fields").addEventListener("input", onFormInput);
$("add-spec").addEventListener("click", () => {
  if ($("spec-fields").children.length >= 40) {
    showError(new Error("Можно добавить до 40 характеристик."));
    return;
  }
  const row = specField("", "", $("spec-fields").children.length);
  $("spec-fields").append(row);
  row.querySelector("input").focus();
  onFormInput();
});
$("product-json").addEventListener("input", () => {
  if (!entry()) return;
  entry().raw = $("product-json").value;
  formInvalid = false;
  clearError();
  persist(["raw"]);
  updateControls();
});
$("mode-form").addEventListener("click", () => setMode("form"));
$("mode-json").addEventListener("click", () => setMode("json"));
$("apply-json").addEventListener("click", () => {
  clearError();
  if (setMode("form")) {
    updateControls();
    toast("JSON применён к полям.");
    $("product-name").focus();
  }
});
$("show-data").addEventListener("click", () => setView("data", true));
$("generate").addEventListener("click", generateCard);
$("retry-config").addEventListener("click", refreshConfig);
$("saved").addEventListener("click", () => selectExample(workspace.selected));
$("reset-draft").addEventListener("click", async () => {
  if (busy || loadingExample) return;
  if (workspace.selected === CUSTOM) {
    if (
      !confirm(
        "Очистить данные и результат вашего прибора? Готовые примеры сохранятся.",
      )
    )
      return;
    workspace.entries[CUSTOM] = blankEntry();
    clearError();
    fillCurrentInput();
    persist(["raw", "result", "edits", "mode"]);
    renderResult();
    setView("data", true);
    return;
  }
  if (
    !confirm(
      "Сбросить ваши правки этого примера и вернуть исходные данные и сохранённую карточку?",
    )
  )
    return;
  loadingExample = true;
  updateControls();
  let sample;
  try {
    sample = await loadSample(workspace.selected);
  } catch (err) {
    showError(err);
    return;
  } finally {
    loadingExample = false;
    updateControls();
  }
  workspace.entries[workspace.selected] = {
    raw: JSON.stringify(sample.product, null, 2),
    result: {
      card: clone(sample.card),
      markdown: sample.markdown,
      raw: JSON.stringify(sample.product),
    },
    edits: null,
    mode: "form",
  };
  clearError();
  fillCurrentInput();
  persist(["raw", "result", "edits", "mode"]);
  renderResult();
  toast("Исходный пример восстановлен.");
});
$("copy").addEventListener("click", async () => {
  const c = currentCard();
  if (!c) return;
  const value =
    (isStale() ? "Предыдущая версия: исходные данные изменены.\n\n" : "") +
    plainText(c);
  try {
    await navigator.clipboard.writeText(value);
    toast("Текст карточки скопирован.");
  } catch {
    toast("Браузер запретил копирование. Скачайте Markdown.");
  }
});
$("json").addEventListener("click", () => download("json"));
$("markdown").addEventListener("click", () => download("md"));
$("export-menu").addEventListener("toggle", () => {
  if ($("export-menu").open) $("toast").hidden = true;
});
document.addEventListener("click", (e) => {
  if (!$("export-menu").contains(e.target)) $("export-menu").open = false;
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && $("export-menu").open) {
    $("export-menu").open = false;
    $("export-menu").querySelector("summary").focus();
  }
});
$("edit-result").addEventListener("click", () => {
  const c = currentCard();
  if (!c) return;
  $("edit-title").value = c.seo_title;
  $("edit-description").value = c.seo_description;
  $("edit-commercial").value = c.commercial;
  $("edit-error").hidden = true;
  $("edit-dialog").showModal();
});
$("close-edit").addEventListener("click", () => $("edit-dialog").close());
$("edit-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const edits = {
    seo_title: $("edit-title").value.trim(),
    seo_description: $("edit-description").value.trim(),
    commercial: $("edit-commercial").value.trim(),
  };
  if (
    !edits.seo_title ||
    charCount(edits.seo_title) > 70 ||
    charCount(edits.seo_description) < 800 ||
    charCount(edits.seo_description) > 1200 ||
    !edits.commercial
  ) {
    $("edit-error").textContent =
      "Заголовок — до 70 символов; описание — от 800 до 1200. Заполните блок для менеджера.";
    $("edit-error").hidden = false;
    return;
  }
  entry().edits = edits;
  persist(["edits"]);
  renderResult();
  $("edit-dialog").close();
  setView("card", true);
  toast("Правка сохранена. Проверьте формулировки перед публикацией.");
});
$("restore-text").addEventListener("click", () => {
  if (!entry()?.result) return;
  entry().edits = null;
  persist(["edits"]);
  renderResult();
  $("edit-dialog").close();
  setView("card", true);
  toast("Восстановлен исходный ответ модели.");
});
// Keep the final fields and notices above the action bar, including enlarged text.
if (typeof ResizeObserver !== "undefined") {
  const bars = [$("result-actions"), document.querySelector(".generate-block")];
  const observer = new ResizeObserver(() => {
    const height = Math.max(
      70,
      ...bars.map((bar) => bar.getBoundingClientRect().height),
    );
    document.documentElement.style.setProperty(
      "--action-height",
      height + "px",
    );
  });
  bars.forEach((bar) => observer.observe(bar));
}
(async () => {
  updateControls();
  await restoreWorkspace();
  // Existing work can be read even if the configuration endpoint is temporarily unavailable.
  if (entry()) {
    try {
      fillCurrentInput();
      renderResult();
    } catch {
      entry().result = null;
      entry().edits = null;
    }
  }
  loadingExample = false;
  await Promise.allSettled([
    refreshConfig(),
    selectExample(workspace.selected, false),
  ]);
  updateControls();
})();

"use strict";
const $ = (id) => document.getElementById(id);
let config = { ready: false },
  currentExample = null,
  initialInput = "",
  card = null,
  md = "",
  busy = false,
  toastTimer;
function node(tag, text, cls) {
  const el = document.createElement(tag);
  if (text != null) el.textContent = text;
  if (cls) el.className = cls;
  return el;
}
async function api(url, options) {
  const response = await fetch(
    new URL(url.replace(/^\/+/, ""), new URL(".", location.href)),
    options,
  );
  let body;
  try {
    body = await response.json();
  } catch {
    throw {
      message: "Сервис вернул некорректный ответ. Попробуйте ещё раз вручную.",
    };
  }
  if (!response.ok)
    throw body.error || { message: "Не удалось выполнить запрос." };
  return body;
}
function clearError() {
  $("error").hidden = true;
  $("error").replaceChildren();
}
function error(err) {
  const box = $("error");
  box.replaceChildren(
    node("strong", err.message || "Не удалось связаться с локальным сервисом."),
  );
  if (err.issues?.length) {
    const ul = node("ul");
    err.issues.forEach((s) => ul.append(node("li", s)));
    box.append(ul);
  }
  box.hidden = false;
}
function controls() {
  $("generate").disabled = busy || !config.ready;
  $("saved").disabled =
    busy ||
    !currentExample ||
    $("product-json").value !== initialInput ||
    !currentExample.card;
  document
    .querySelectorAll("[data-example]")
    .forEach((b) => (b.disabled = busy));
  $("product-json").disabled = busy;
}
function invalidate() {
  card = null;
  md = "";
  $("result").hidden = true;
  $("empty").hidden = false;
  $("loading").hidden = true;
  $("draft-badge").textContent = "Ожидает данных";
  $("draft-badge").classList.remove("done");
}
function setBusy(value) {
  busy = value;
  controls();
  $("loading").hidden = !value;
  $("empty").hidden = value || !!card;
  $("result").hidden = value || !card;
  document
    .querySelector(".output-pane")
    .setAttribute("aria-busy", String(value));
  if (value) {
    $("draft-badge").textContent = "Генерация";
    $("draft-badge").classList.remove("done");
  }
}
function render(data, markdownText) {
  card = data;
  md = markdownText;
  $("empty").hidden = true;
  $("loading").hidden = true;
  $("result").hidden = false;
  $("draft-badge").textContent = "Проверки пройдены";
  $("draft-badge").classList.add("done");
  $("seo-title").textContent = card.seo_title;
  $("title-count").textContent = `${card.validation.title_characters} / 70`;
  $("description-count").textContent =
    `${card.validation.description_characters} / 1200`;
  $("seo-description").replaceChildren(
    ...card.seo_description.split("\n\n").map((t) => node("p", t)),
  );
  $("specs").replaceChildren(
    ...card.characteristics.map((s) => {
      const row = node("div");
      row.append(
        node("dt", s.name),
        node("dd", s.value, s.value === "данных нет" ? "no-data" : null),
      );
      return row;
    }),
  );
  $("queries").replaceChildren(
    ...card.search_queries.map((s) => node("li", s)),
  );
  $("commercial").textContent = card.commercial;
  const warnings = [...card.missing_fields, ...card.warnings];
  $("warnings").replaceChildren(...warnings.map((s) => node("li", s)));
  $("warnings-box").hidden = !warnings.length;
  $("generation-mode").textContent = card.generation.viewing_saved_result
    ? "Сохранённый ответ API · без нового запроса"
    : `Ответ API · ${card.generation.model}`;
  const cost = card.generation.reported_cost_usd;
  $("generation-cost").textContent =
    cost == null
      ? "Стоимость не сообщена API"
      : `Стоимость ответа: $${Number(cost).toFixed(6)}`;
  const draft = card.evidence.draft,
    blocks = [
      draft.seo_title,
      ...draft.description_paragraphs,
      ...draft.search_queries,
      draft.commercial,
    ];
  $("evidence-list").replaceChildren(
    ...blocks.map((block) => {
      const el = node("div", null, "evidence-entry");
      el.append(
        node("p", block.text),
        node(
          "small",
          block.fact_ids
            .map((id) => `${id}: ${card.evidence.facts[id]}`)
            .join(" · "),
        ),
      );
      return el;
    }),
  );
  $("source").hidden = !card.source_url;
  if (card.source_url) $("source").href = card.source_url;
}
async function selectExample(id) {
  if (busy) return;
  clearError();
  invalidate();
  try {
    const data = await api("/api/examples/" + id);
    currentExample = data;
    initialInput = JSON.stringify(data.product, null, 2);
    $("product-json").value = initialInput;
    document
      .querySelectorAll("[data-example]")
      .forEach((b) =>
        b.setAttribute("aria-pressed", String(b.dataset.example === id)),
      );
    controls();
  } catch (err) {
    error(err);
  }
}
document
  .querySelectorAll("[data-example]")
  .forEach((b) =>
    b.addEventListener("click", () => selectExample(b.dataset.example)),
  );
$("product-json").addEventListener("input", () => {
  invalidate();
  clearError();
  controls();
});
$("saved").addEventListener("click", () => {
  clearError();
  if (currentExample && $("product-json").value === initialInput)
    render(currentExample.card, currentExample.markdown);
});
$("generate").addEventListener("click", async () => {
  if (busy || !config.ready) return;
  clearError();
  let input;
  try {
    input = JSON.parse($("product-json").value);
  } catch {
    error({ message: "Некорректный JSON. Проверьте кавычки и запятые." });
    return;
  }
  invalidate();
  setBusy(true);
  try {
    const data = await api("/api/generate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: $("product-json").value,
    });
    render(data.card, data.markdown);
  } catch (err) {
    error(err);
    invalidate();
  } finally {
    if (config.public_demo) {
      try {
        applyConfig(await api("/api/config"));
      } catch {}
    }
    setBusy(false);
  }
});
function toast(text) {
  clearTimeout(toastTimer);
  $("toast").textContent = text;
  $("toast").hidden = false;
  toastTimer = setTimeout(() => ($("toast").hidden = true), 3500);
}
$("copy").addEventListener("click", async () => {
  if (!card) return;
  try {
    await navigator.clipboard.writeText(md);
    toast("Текст карточки скопирован");
  } catch {
    toast("Браузер запретил копирование. Скачайте Markdown.");
  }
});
function download(format) {
  if (!card) return;
  const blob = new Blob(
    [format === "json" ? JSON.stringify(card, null, 2) : md],
    {
      type:
        format === "json"
          ? "application/json;charset=utf-8"
          : "text/markdown;charset=utf-8",
    },
  );
  const url = URL.createObjectURL(blob);
  const a = node("a");
  a.href = url;
  a.download = `product-card.${format === "json" ? "json" : "md"}`;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
$("json").addEventListener("click", () => download("json"));
$("markdown").addEventListener("click", () => download("md"));
function applyConfig(value) {
  config = value;
  $("connection").textContent = config.ready
    ? "API подключён"
    : "Просмотр примеров";
  $("connection").classList.toggle("ready", config.ready);
  if (config.public_demo)
    document.querySelector("footer span").textContent =
      "Демонстрация на сервере · Python · LLM API";
  $("api-help").textContent = config.public_demo
    ? `Публичное демо: до 3 генераций за 15 минут. Остаток общего бюджета: $${Number(config.budget.remaining_usd).toFixed(3)}. Просмотр готовых примеров бесплатный.`
    : config.ready
      ? `Модель: ${config.model}. Новая генерация выполняет один платный запрос к вашему API.`
      : "Примеры открываются бесплатно. Для новой генерации задайте ключ и модель в .env, затем перезапустите сервис.";
}
(async () => {
  try {
    applyConfig(await api("/api/config"));
    await selectExample("testo-510");
    controls();
  } catch (err) {
    $("connection").textContent = "Нет соединения";
    error(err);
  }
})();

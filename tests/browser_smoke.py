"""Локальная приёмка мобильного интерфейса без вызовов платных моделей.

Нужны уже работающий loopback-сервер, Python Playwright и Microsoft Edge.
Вся генерация перехватывается до отправки серверу. Внешняя сеть запрещена.
"""
import argparse
import copy
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from playwright.sync_api import expect, sync_playwright


VIEWPORTS = [(320, 568), (360, 640), (390, 844), (430, 932),
             (768, 1024), (1024, 768), (1440, 1000)]
READ_WORKSPACE = """() => new Promise((resolve, reject) => {
    const open = indexedDB.open('pribor-studio-workspace');
    open.onerror = () => reject(open.error);
    open.onsuccess = () => {
        const db = open.result;
        const tx = db.transaction('workspace');
        const read = tx.objectStore('workspace').get('snapshot');
        tx.oncomplete = () => { db.close(); resolve(read.result || ''); };
        tx.onabort = () => { db.close(); reject(tx.error); };
    };
})"""
FIELD_MARKER = "Testo — сохранённый тестовый черновик"
DESCRIPTION_MARKER = "Проверочное описание прибора для локальной проверки интерфейса."
GENERATED_MARKER = "Синтетический ответ для проверки интерфейса. Платного запроса не было."


class IsolatedNetwork:
    """Пропускает только GET loopback; генерацию всегда заменяет тестовым ответом."""

    def __init__(self, url, prefix, axe_path):
        self.base = urlsplit(url)
        self.prefix = prefix.rstrip("/")
        self.axe_path = axe_path
        self.blocked = []
        self.generated = []
        self.pending = None
        self.ready = True

    def __call__(self, route):
        request = route.request
        target = urlsplit(request.url)
        if target.scheme in {"data", "blob"}:
            route.continue_()
            return
        if target.netloc != self.base.netloc:
            self.blocked.append({"method": request.method, "host": target.hostname})
            route.abort()
            return
        path = target.path
        if self.prefix:
            if not path.startswith(self.prefix + "/"):
                route.abort()
                return
            path = path[len(self.prefix):]
        if path.endswith("/__test_axe__.js") and self.axe_path:
            route.fulfill(path=str(self.axe_path), content_type="text/javascript")
            return
        if path.endswith("/api/generate"):
            # Ни один POST не достигает даже локального сервера с возможным API-ключом.
            if request.method != "POST" or self.pending is not None:
                route.abort()
                return
            self.generated.append(request.post_data_json)
            self.pending = route
            return
        if request.method != "GET":
            self.blocked.append({"method": request.method, "host": target.hostname})
            route.abort()
            return
        if path.endswith("/api/config"):
            route.fulfill(json={
                "ready": self.ready, "model": "synthetic-browser-test", "public_demo": True,
                "budget": {"limit_usd": 0.5, "spent_usd": 0, "held_usd": 0,
                           "remaining_usd": 0.5, "can_generate": self.ready},
            })
            return
        if self.prefix:
            route.continue_(url=urlunsplit(target._replace(path=path)))
        else:
            route.continue_()

    def finish(self, payload, status=200):
        assert self.pending is not None, "UI не отправил ожидаемый тестовый запрос"
        pending, self.pending = self.pending, None
        pending.fulfill(status=status, json=payload)

    def close(self):
        if self.pending is not None:
            self.pending.abort()
            self.pending = None


def geometry(locator):
    return locator.evaluate("""el => {
        const r=el.getBoundingClientRect(), s=getComputedStyle(el);
        const x=Math.max(0,Math.min(innerWidth-1,r.x+r.width/2));
        const y=Math.max(0,Math.min(innerHeight-1,r.y+r.height/2));
        const hit=document.elementFromPoint(x,y);
        return {x:r.x,y:r.y,width:r.width,height:r.height,bottom:r.bottom,right:r.right,
            viewportWidth:innerWidth,viewportHeight:innerHeight,fontSize:parseFloat(s.fontSize),
            visibleCenter:!!hit && (hit===el || el.contains(hit)),
            text:el.textContent.trim().slice(0,160)};
    }""")


def in_first_view(locator, description, minimum_height=None):
    """Не прокручивает страницу: измеряет то, что человек действительно видит."""
    expect(locator).to_be_visible()
    value = geometry(locator)
    viewport = locator.page.viewport_size
    value["layoutViewportWidth"] = value["viewportWidth"]
    if viewport:
        value["viewportWidth"] = viewport["width"]
        value["viewportHeight"] = viewport["height"]
    assert value["x"] >= -1 and value["right"] <= value["viewportWidth"] + 1, (
        f"{description}: выходит за ширину экрана", value)
    assert value["y"] >= -1 and value["bottom"] <= value["viewportHeight"] + 1, (
        f"{description}: требуется дополнительная прокрутка", value)
    assert value["visibleCenter"], (f"{description}: перекрыт другим элементом", value)
    if minimum_height is not None:
        assert value["height"] >= minimum_height - 1, (
            f"{description}: неудобная высота области нажатия", value)
    return value


def no_overflow(page, stage):
    result = page.evaluate("""expectedWidth => ({
        viewport:expectedWidth,layoutViewport:innerWidth,document:document.documentElement.scrollWidth,
        offenders:[...document.querySelectorAll('body *')].filter(el=>{
            const s=getComputedStyle(el),r=el.getBoundingClientRect();
            return s.display!=='none' && s.visibility!=='hidden' && r.width>0 &&
                (r.right>expectedWidth+1 || r.left < -1) && !el.closest('[hidden]');
        }).slice(0,8).map(el=>({tag:el.tagName,id:el.id,cls:el.className}))
    })""", page.viewport_size["width"])
    assert result["document"] <= result["viewport"] + 1, (
        f"Горизонтальное переполнение: {stage}", result)
    return result


def readable_control(locator, description):
    bounds = in_first_view(locator, description)
    clipping = locator.evaluate("""root => {
        const box=root.getBoundingClientRect(), clipped=[];
        const walker=document.createTreeWalker(root,NodeFilter.SHOW_TEXT);
        while(walker.nextNode()) {
            const node=walker.currentNode, parent=node.parentElement;
            if(!node.textContent.trim() || !parent || !parent.getClientRects().length)continue;
            const style=getComputedStyle(parent);
            if(style.visibility==='hidden'||style.display==='none')continue;
            const range=document.createRange();range.selectNodeContents(node);
            for(const r of range.getClientRects()) {
                if(r.width>0 && (r.left<box.left-1 || r.right>box.right+1 ||
                    r.top<box.top-1 || r.bottom>box.bottom+1)) {
                    clipped.push({text:node.textContent.trim(),x:r.x,y:r.y,
                        right:r.right,bottom:r.bottom});
                }
            }
        }
        return {clipped,scrollWidth:root.scrollWidth,clientWidth:root.clientWidth};
    }""")
    assert not clipping["clipped"], (f"{description}: текст выходит из области элемента", clipping)
    assert clipping["scrollWidth"] <= clipping["clientWidth"] + 1, (
        f"{description}: скрытое внутреннее переполнение", clipping)
    return {"bounds": bounds, "textClipping": clipping}


def show_view(page, view):
    if view == "data" and not page.locator("#tab-data").is_visible():
        # В широком редакторе исходные данные постоянно находятся слева.
        expect(page.locator("#panel-data")).to_be_visible()
        page.locator("#data-heading").scroll_into_view_if_needed()
        return
    page.locator(f"#tab-{view}").click()
    expect(page.locator(f"#panel-{view}")).to_be_visible()
    expect(page.locator(f"#tab-{view}")).to_have_attribute("aria-selected", "true")


def screenshot(page, out, width, stage, full=False):
    page.screenshot(path=str(out / f"{width}-{stage}.png"), full_page=full)


def input_sizes(page, width):
    if width >= 1000:
        return []
    sizes = page.locator("#panel-data input, #panel-data textarea, #panel-data select").evaluate_all(
        "els=>els.filter(e=>e.getClientRects().length).map(e=>({id:e.id,"
        "font:parseFloat(getComputedStyle(e).fontSize)}))")
    assert sizes, "Не найдены поля мобильной формы"
    assert all(item["font"] >= 16 for item in sizes), ("Поля меньше 16 px", sizes)
    return sizes


def accessibility(page, args):
    if not args.axe:
        return {"run": False, "reason": "Путь --axe не передан"}
    if not page.evaluate("typeof window.axe === 'object'"):
        page.add_script_tag(url=args.url + "/__test_axe__.js")
    violations = page.evaluate("""async()=>{
        const r=await axe.run(document,{runOnly:{type:'tag',
            values:['wcag2a','wcag2aa','wcag21aa']}});
        return r.violations.map(v=>({id:v.id,impact:v.impact,targets:v.nodes.map(n=>n.target)}));
    }""")
    assert not violations, ("Нарушения автоматической проверки доступности", violations)
    return {"run": True, "violations": violations}


def inspect_first_view(page, width, out):
    expect(page.locator("#result")).to_be_visible()
    expect(page.locator("#seo-title")).to_contain_text("testo 510")
    assert page.evaluate("scrollY") <= 1, "Открытие неожиданно прокручивает страницу"
    checks = {
        "title": in_first_view(page.locator("#seo-title"), "Готовый пример"),
        "copy": in_first_view(page.locator("#copy"), "Копирование результата", 44),
    }
    for view in (("card", "data", "checks") if width < 1000 else ("card", "checks")):
        checks["tab-" + view] = in_first_view(
            page.locator(f"#tab-{view}"), f"Навигация «{view}»", 44)
    expect(page.locator("#tab-card")).to_have_attribute("aria-selected", "true")
    if width < 1000:
        expect(page.locator("#panel-data")).to_be_hidden()
    else:
        expect(page.locator("#panel-data")).to_be_visible()
    no_overflow(page, "Первый экран")
    screenshot(page, out, width, "first-viewport")
    return checks


def inspect_navigation(page, args, width, out):
    checks = {}
    show_view(page, "checks")
    expect(page.locator("#warnings")).to_contain_text("Гарантия")
    assert page.locator("#evidence-list").inner_text().strip(), "Нет связей с исходными фактами"
    checks["checksOverflow"] = no_overflow(page, "Проверка фактов")
    checks["checksAxe"] = accessibility(page, args)
    screenshot(page, out, width, "checks-viewport")
    show_view(page, "data")
    expect(page.locator("#product-name")).to_have_value("testo 510")
    expect(page.locator("#product-manufacturer")).to_have_value("Testo")
    assert page.locator("#spec-fields [data-spec-row]").count() > 0
    checks["inputSizes"] = input_sizes(page, width)
    checks["generateInData"] = in_first_view(page.locator("#generate"), "Генерация из формы", 44)
    checks["dataOverflow"] = no_overflow(page, "Форма исходных данных")
    checks["dataAxe"] = accessibility(page, args)
    screenshot(page, out, width, "data-viewport")
    if width <= 600:
        page.locator("#product-notes").scroll_into_view_if_needed()
        page.locator("#product-notes").focus()
        checks["lastFieldUsable"] = in_first_view(page.locator("#product-notes"), "Последнее длинное поле")
        checks["generateAfterLongFields"] = in_first_view(page.locator("#generate"), "Генерация после длинных полей", 44)
        screenshot(page, out, width, "data-last-field")
    page.locator("#mode-json").click()
    expect(page.locator("#product-json")).to_be_visible()
    assert json.loads(page.locator("#product-json").input_value())["name"] == "testo 510"
    checks["jsonInputSizes"] = input_sizes(page, width)
    no_overflow(page, "Режим JSON")
    page.locator("#mode-form").click()
    show_view(page, "card")
    checks["cardAxe"] = accessibility(page, args)
    page.evaluate("window.scrollTo(0,document.documentElement.scrollHeight)")
    if width <= 600:
        checks["copyAtEnd"] = in_first_view(page.locator("#copy"), "Копирование в конце карточки", 44)
    screenshot(page, out, width, "end-viewport")
    return checks


def inspect_keyboard(page):
    show_view(page, "card")
    tab = page.locator("#tab-card")
    tab.focus()
    tab.press("ArrowRight")
    if page.locator("#tab-data").is_visible():
        expect(page.locator("#tab-data")).to_be_focused()
        expect(page.locator("#tab-data")).to_have_attribute("aria-selected", "true")
        page.locator("#tab-data").press("ArrowRight")
    expect(page.locator("#tab-checks")).to_be_focused()
    page.locator("#tab-checks").press("Home")
    expect(page.locator("#tab-card")).to_be_focused()
    assert page.locator("#tab-card").evaluate(
        "el=>{const s=getComputedStyle(el);return s.outlineStyle!=='none'||s.boxShadow!=='none'}"
    ), "Клавиатурный фокус не имеет видимого оформления"
    return {"tabsArrowKeys": True, "homeKey": True, "visibleFocus": True}


def stored(page, text):
    page.wait_for_function(
        f"async text => (await ({READ_WORKSPACE})()).includes(text)",
        arg=text, timeout=5000)


def inspect_work_preservation(page, network, out, width):
    show_view(page, "data")
    title = page.locator("#seo-title").text_content()
    page.locator("#product-manufacturer").fill(FIELD_MARKER)
    page.locator("#product-name").focus()
    stored(page, FIELD_MARKER)
    assert json.loads(page.locator("#product-json").input_value())["manufacturer"] == FIELD_MARKER
    show_view(page, "card")
    expect(page.locator("#stale-notice")).to_be_visible()
    expect(page.locator("#seo-title")).to_have_text(title)
    screenshot(page, out, width, "stale-result")
    page.locator('[data-example="testo-511"]').click()
    expect(page.locator("#seo-title")).to_contain_text("testo 511")
    page.locator('[data-example="testo-510"]').click()
    show_view(page, "data")
    expect(page.locator("#product-manufacturer")).to_have_value(FIELD_MARKER)
    page.reload(wait_until="networkidle")
    show_view(page, "data")
    expect(page.locator("#product-manufacturer")).to_have_value(FIELD_MARKER)
    assert len(network.generated) == 0, "Просмотр или редактирование запускает генерацию"

    page.locator("#mode-json").click()
    before = page.locator("#product-json").input_value()
    page.locator("#product-json").fill('{"name":"testo 510",')
    page.locator("#apply-json").click()
    expect(page.locator("#error")).to_be_visible()
    assert len(network.generated) == 0, "Некорректный JSON отправлен в генерацию"
    screenshot(page, out, width, "json-error")
    expect(page.locator("#product-manufacturer")).to_have_value(FIELD_MARKER)
    draft = json.loads(before)
    draft["description"] = DESCRIPTION_MARKER
    page.locator("#product-json").fill(json.dumps(draft, ensure_ascii=False, indent=2))
    page.locator("#apply-json").click()
    page.locator("#mode-form").click()
    expect(page.locator("#product-description")).to_have_value(DESCRIPTION_MARKER)
    expect(page.locator("#error")).to_be_hidden()
    stored(page, DESCRIPTION_MARKER)
    return {"formJsonSync": True, "staleResultRetained": True, "perExampleDraft": True,
            "reloadPreservesDraft": True, "invalidJsonPreservesForm": True, "jsonFormSync": True}


def inspect_generation(page, network, sample, out, width):
    show_view(page, "card")
    title = page.locator("#seo-title").inner_text()
    show_view(page, "data")
    page.locator("#generate").click()
    expect(page.locator("#loading")).to_be_visible()
    expect(page.locator("#generate")).to_be_disabled()
    screenshot(page, out, width, "loading-viewport")
    network.finish({"error": {"message": "Синтетическая ошибка API. Сохранённый результат доступен.",
                              "issues": [], "attempt_count": 2,
                              "reported_cost_usd": 0.00246}}, status=503)
    expect(page.locator("#error")).to_contain_text("Синтетическая ошибка API")
    expect(page.locator("#error")).to_contain_text("Попыток — 2")
    expect(page.locator("#error")).to_contain_text("$0.002460")
    expect(page.locator("#generate")).to_be_enabled()
    show_view(page, "card")
    expect(page.locator("#result")).to_be_visible()
    expect(page.locator("#seo-title")).to_have_text(title)
    expect(page.locator("#stale-notice")).to_be_visible()
    screenshot(page, out, width, "generation-error-retained-result")

    fixture = copy.deepcopy(sample)
    fixture["card"]["commercial"] = GENERATED_MARKER
    fixture["card"]["generation"].update({"viewing_saved_result": False,
        "mode": "live_api", "model": "synthetic-browser-test", "reported_cost_usd": 0.012345,
        "attempt_count": 2, "elapsed_seconds": 21.5})
    fixture["markdown"] = sample["markdown"] + "\n\n" + GENERATED_MARKER + "\n"
    show_view(page, "data")
    page.locator("#generate").click()
    expect(page.locator("#loading")).to_be_visible()
    network.finish({"card": fixture["card"], "markdown": fixture["markdown"]})
    expect(page.locator("#panel-card")).to_be_visible()
    expect(page.locator("#commercial")).to_contain_text(GENERATED_MARKER)
    expect(page.locator("#loading")).to_be_hidden()
    expect(page.locator("#generate")).to_be_enabled()
    expect(page.locator("#stale-notice")).to_be_hidden()
    expect(page.locator("#error")).to_be_hidden()
    expect(page.locator("#request-details")).to_contain_text("Общая стоимость двух попыток")
    expect(page.locator("#request-details")).to_contain_text("$0.012345")
    expect(page.locator("#request-details")).to_contain_text("21.5 с")
    assert page.evaluate("""()=>{
        const active=document.activeElement;
        return !!active && active!==document.body &&
            (document.getElementById('panel-card').contains(active)||active.id==='tab-card');
    }"""), "После генерации фокус не сообщает о переходе к результату"
    in_first_view(page.locator("#seo-title"), "Результат после генерации")
    mode = (page.locator("#mobile-generation-mode")
            if page.locator("#mobile-generation-mode").is_visible()
            else page.locator("#generation-mode"))
    in_first_view(mode, "Происхождение ответа после генерации")
    screenshot(page, out, width, "generated-result-viewport")
    assert len(network.generated) == 2, "Один клик породил лишние запросы генерации"
    assert network.generated[-1]["description"] == DESCRIPTION_MARKER
    return fixture


def inspect_exports(page, fixture, out, width):
    # Проверяем намерение записи, не затрагивая системный буфер пользователя.
    page.evaluate("navigator.clipboard.writeText=async value=>{window.__copied=value}")
    page.locator("#copy").click()
    page.wait_for_function("typeof window.__copied==='string'")
    plain = page.evaluate("window.__copied")
    assert fixture["card"]["seo_title"] in plain
    assert fixture["card"]["seo_description"] in plain
    assert GENERATED_MARKER in plain
    assert not re.search(r"(?m)^\s*#{1,6}\s|\*\*|\[[^\]]+\]\(https?://", plain), (
        "Кнопка чистого текста копирует Markdown", plain[:250])
    (out / f"{width}-copied.txt").write_text(plain, encoding="utf-8")
    filenames = {}
    for button, extension in [("json", "json"), ("markdown", "md")]:
        if page.locator("#export-menu").get_attribute("open") is None:
            page.locator("#export-menu summary").click()
        with page.expect_download() as event:
            page.locator("#" + button).click()
        download = event.value
        filenames[button] = download.suggested_filename
        assert "testo" in download.suggested_filename.lower(), "Имя файла не содержит модель товара"
        path = out / f"{width}-download.{extension}"
        download.save_as(str(path))
        value = path.read_text("utf-8")
        if extension == "json":
            exported = json.loads(value)
            assert exported["seo_title"] == fixture["card"]["seo_title"]
            assert exported["commercial"] == GENERATED_MARKER
        else:
            assert value == fixture["markdown"]
    page.reload(wait_until="networkidle")
    show_view(page, "card")
    expect(page.locator("#commercial")).to_contain_text(GENERATED_MARKER)
    return {"plainText": True, "json": True, "markdown": True, "filenames": filenames,
            "generatedResultSurvivesReload": True,
            "clipboardTransport": "изолированная имитация writeText"}


def inspect_large_text(page, out, width):
    show_view(page, "card")
    page.evaluate("window.scrollTo(0,0)")
    # Синтетическая проверка устойчивости, не аппаратный тест iOS/Android.
    page.evaluate("""()=>{
        const values=[...document.querySelectorAll('body *')]
            .map(el=>[el,parseFloat(getComputedStyle(el).fontSize)]);
        values.forEach(([el,size])=>{if(Number.isFinite(size))el.style.fontSize=(size*2)+'px'});
    }""")
    page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
    result = no_overflow(page, "Текст увеличен вдвое")
    controls = {"copy": readable_control(page.locator("#copy"), "Копирование при 200%"),
                "download": readable_control(page.locator("#export-menu summary"), "Скачать при 200%")}
    screenshot(page, out, width, "text-200-viewport")
    page.locator("#export-menu summary").click()
    controls["markdown"] = readable_control(page.locator("#markdown"), "Markdown в увеличенном меню")
    controls["json"] = readable_control(page.locator("#json"), "JSON в увеличенном меню")
    screenshot(page, out, width, "text-200-export-open")
    page.keyboard.press("Escape")
    expect(page.locator("#export-menu")).not_to_have_attribute("open", "")
    page.evaluate("window.scrollTo(0,document.documentElement.scrollHeight)")
    controls["copyAtEnd"] = readable_control(page.locator("#copy"), "Копирование в конце при 200%")
    controls["downloadAtEnd"] = readable_control(page.locator("#export-menu summary"), "Скачать в конце при 200%")
    controls["footer"] = in_first_view(page.locator(".footer a"), "Последняя ссылка над увеличенной панелью действий")
    screenshot(page, out, width, "text-200-end")
    page.reload(wait_until="networkidle")
    return {"simulation": "двукратное увеличение вычисленных размеров текста", "controls": controls, **result}


def inspect_manual_revision(page, network, fixture, args, out, width):
    show_view(page, "card")
    original_specs = page.locator("#specs").inner_text()
    calls_before = len(network.generated)
    page.locator("#edit-result").click()
    expect(page.locator("#edit-dialog")).to_be_visible()
    assert page.evaluate("document.getElementById('edit-dialog').contains(document.activeElement)"), (
        "Фокус не находится внутри открытой редакторской формы")
    page.keyboard.press("Escape")
    expect(page.locator("#edit-dialog")).to_be_hidden()
    expect(page.locator("#edit-result")).to_be_focused()
    page.locator("#edit-result").click()
    title = "Манометр testo 510 — редакторская версия"
    commercial = "Текст уточнён редактором. Это бесплатная локальная правка результата."
    page.locator("#edit-title").fill(title)
    page.locator("#edit-commercial").fill(commercial)
    accessibility(page, args)
    no_overflow(page, "Ручная редакция")
    screenshot(page, out, width, "manual-editor")
    page.locator("#edit-form").get_by_role("button", name="Сохранить правку", exact=True).click()
    expect(page.locator("#edit-dialog")).to_be_hidden()
    expect(page.locator("#seo-title")).to_have_text(title)
    expect(page.locator("#manual-notice")).to_be_visible()
    expect(page.locator("#draft-badge")).to_contain_text("вручную")
    assert page.locator("#specs").inner_text() == original_specs, "Правка текста изменила исходные характеристики"
    show_view(page, "checks")
    expect(page.locator("#check-status")).to_contain_text("требует проверки")
    expect(page.locator("#evidence-help")).to_contain_text("исходного ответа модели")
    page.locator("#export-menu summary").click()
    with page.expect_download() as event:
        page.locator("#json").click()
    path = out / f"{width}-manual-revision.json"
    event.value.save_as(str(path))
    exported = json.loads(path.read_text("utf-8"))
    assert exported["editor_revision"]["manual"] is True
    assert exported["editor_revision"]["needs_review"] is True
    assert exported["characteristics"] == fixture["card"]["characteristics"]
    assert exported["evidence"] == fixture["card"]["evidence"]
    page.reload(wait_until="networkidle")
    expect(page.locator("#seo-title")).to_have_text(title)
    page.locator("#edit-result").click()
    page.locator("#restore-text").click()
    expect(page.locator("#seo-title")).to_have_text(fixture["card"]["seo_title"])
    expect(page.locator("#manual-notice")).to_be_hidden()
    expect(page.locator("#commercial")).to_contain_text(GENERATED_MARKER)
    assert len(network.generated) == calls_before, "Ручная редакция вызвала платный путь генерации"
    return {"focusContained": True, "escapeAndReturnFocus": True, "markedAsManual": True,
            "factsAndEvidenceUnchanged": True, "reloadPreservesRevision": True,
            "originalAnswerRestored": True, "extraModelCalls": 0}


def main():
    parser = argparse.ArgumentParser(description="Приёмка UI без платных запросов")
    parser.add_argument("--url", default="http://127.0.0.1:8765")
    parser.add_argument("--output", default=".local/browser")
    parser.add_argument("--axe", type=Path)
    parser.add_argument("--mount-prefix", default="")
    parser.add_argument("--widths", help="Подмножество ширин через запятую для целевой перепроверки")
    args = parser.parse_args()
    args.url = args.url.rstrip("/")
    parsed = urlsplit(args.url)
    if parsed.hostname not in {"127.0.0.1", "localhost"} or parsed.scheme != "http":
        parser.error("Разрешён только локальный HTTP-сервер на loopback")
    if args.axe and not args.axe.is_file():
        parser.error("Файл axe не найден; сценарий не скачивает зависимости")
    selected = {int(w) for w in args.widths.split(",")} if args.widths else None
    viewports = [(w, h) for w, h in VIEWPORTS if selected is None or w in selected]
    if not viewports:
        parser.error("Не выбрана поддерживаемая ширина")
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)
    proof = {"passed": False, "at": datetime.now(timezone.utc).isoformat(), "url": args.url,
             "views": [], "paidModelCalls": 0, "productionWrites": 0, "browserClosed": False,
             "generationTransport": "все POST перехвачены локально", "realPhoneChecked": False}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="msedge", headless=True)
        try:
            for width, height in viewports:
                context = browser.new_context(viewport={"width": width, "height": height},
                    locale="ru-RU", reduced_motion="reduce", accept_downloads=True,
                    is_mobile=width<768, has_touch=width<1000, service_workers="block")
                network = IsolatedNetwork(args.url, args.mount_prefix, args.axe)
                context.route("**/*", network)
                page = context.new_page()
                page.set_default_timeout(7000)
                page_errors = []
                page.on("pageerror", lambda error: page_errors.append(str(error)))
                view = {"width": width, "height": height, "passed": False}
                proof["views"].append(view)
                try:
                    page.goto(args.url + "/", wait_until="networkidle")
                    view["firstViewport"] = inspect_first_view(page, width, out)
                    view["navigation"] = inspect_navigation(page, args, width, out)
                    # Полные сценарии обязательно проверяются на телефоне и компьютере.
                    if width in {320, 390, 1440}:
                        view["keyboard"] = inspect_keyboard(page)
                        sample = page.evaluate("""()=>fetch(new URL('api/examples/testo-510',
                            new URL('.',location.href))).then(r=>r.json())""")
                        view["drafts"] = inspect_work_preservation(page, network, out, width)
                        fixture = inspect_generation(page, network, sample, out, width)
                        view["exports"] = inspect_exports(page, fixture, out, width)
                        view["manualRevision"] = inspect_manual_revision(
                            page, network, fixture, args, out, width)
                        if width == 390:
                            view["largeText"] = inspect_large_text(page, out, width)
                    assert not page_errors, ("Ошибки JavaScript", page_errors)
                    view["passed"] = True
                except Exception as error:
                    view["failure"] = str(error)
                    screenshot(page, out, width, "failure")
                    raise
                finally:
                    view["pageErrors"] = page_errors
                    view["blockedExternalRequests"] = network.blocked
                    view["mockedGenerationRequests"] = len(network.generated)
                    network.close()
                    context.close()
            proof["passed"] = True
        finally:
            browser.close()
            proof["browserClosed"] = True
            (out / "browser-proof.json").write_text(
                json.dumps(proof, ensure_ascii=False, indent=2), encoding="utf-8")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps({"проверка_пройдена": proof["passed"], "ширины": [w for w, _ in viewports],
                      "платных_вызовов": 0, "доказательства": str(out / "browser-proof.json")},
                     ensure_ascii=False))


if __name__ == "__main__":
    main()

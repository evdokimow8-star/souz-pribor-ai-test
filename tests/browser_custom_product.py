"""Проверяет независимый черновик прибора и запуск ИИ с тестовым ответом."""
import argparse
import json
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

from pribor.domain import markdown, validate_draft
from tests.browser_smoke import IsolatedNetwork, READ_WORKSPACE, in_first_view, no_overflow, show_view
from tests.test_domain import draft, product


def edit_workspace(page, transform):
    page.evaluate("""() => new Promise((resolve, reject) => {
        const open = indexedDB.open('pribor-studio-workspace');
        open.onerror = () => reject(open.error);
        open.onsuccess = () => {
            const db = open.result;
            const tx = db.transaction('workspace', 'readwrite');
            const store = tx.objectStore('workspace');
            const read = store.get('snapshot');
            read.onsuccess = () => {
                const state = JSON.parse(read.result);
                const result = (""" + transform + """)(state);
                if (result === null) store.delete('snapshot');
                else store.put(JSON.stringify(state), 'snapshot');
            };
            tx.oncomplete = () => { db.close(); resolve(); };
            tx.onabort = () => { db.close(); reject(tx.error); };
        };
    })""")


def verify_saved_sample_migration(page, context, url):
    endpoint = "/api/examples/testo-510"
    sample = page.request.get(url + endpoint).json()
    sample["card"]["generation"]["editor_reviewed"] = True
    assert sample["card"]["generation"]["model"] == "google/gemini-4-argon"
    edit_workspace(page, """state => {
        const generation = state.entries['testo-510'].result.card.generation;
        generation.model = 'google/gemini-3-flash-preview';
        generation.editor_reviewed = true;
    }""")
    page.reload(wait_until="networkidle")
    generation = json.loads(page.evaluate(READ_WORKSPACE))["entries"]["testo-510"]["result"]["card"]["generation"]
    assert generation["model"] == "google/gemini-4-argon"
    writer = context.new_page()
    writer.goto(url + "/", wait_until="networkidle")
    edit_workspace(page, """state => {
        delete state.entries['testo-510'].result.card.generation.editor_reviewed;
    }""")

    def revised_sample(route):
        edit_workspace(writer, """state => {
            const entry = state.entries['testo-510'];
            const raw = JSON.parse(entry.raw);
            raw.manufacturer = 'Правка во время запроса примера';
            entry.raw = JSON.stringify(raw);
            entry.edits = {
                seo_title: 'Редакторская версия',
                seo_description: entry.result.card.seo_description,
                commercial: entry.result.card.commercial,
            };
        }""")
        route.fulfill(json=sample)

    page.route("**" + endpoint, revised_sample)
    page.reload(wait_until="networkidle")
    expect(page.locator("#seo-title")).to_have_text("Редакторская версия")
    expect(page.locator("#product-manufacturer")).to_have_value("Правка во время запроса примера")
    expect(page.locator("#draft-status")).to_have_text("Сохранено в браузере")
    assert json.loads(page.evaluate(READ_WORKSPACE))["entries"]["testo-510"]["result"]["card"]["generation"]["editor_reviewed"]
    writer.close()


def verify_legacy_storage_migration(page):
    edit_workspace(page, """state => {
        localStorage.setItem('pribor-studio-workspace-v2', JSON.stringify(state));
        return null;
    }""")
    page.reload(wait_until="networkidle")
    expect(page.locator("#seo-title")).to_have_text("Редакторская версия")
    assert page.evaluate(READ_WORKSPACE)
    page.evaluate("""() => {
        const key = 'pribor-studio-workspace-v2';
        const stale = JSON.parse(localStorage.getItem(key));
        stale.entries['testo-510'].edits = null;
        localStorage.setItem(key, JSON.stringify(stale));
    }""")
    page.reload(wait_until="networkidle")
    expect(page.locator("#seo-title")).to_have_text("Редакторская версия")


def verify_unavailable_storage(browser, url):
    context = browser.new_context()
    try:
        network = IsolatedNetwork(url, "", None)
        context.route("**/*", network)
        context.add_init_script("indexedDB.open = () => ({})")
        page = context.new_page()
        page.goto(url + "/", wait_until="domcontentloaded")
        expect(page.locator("#new-product")).to_be_disabled()
        expect(page.locator("#storage-warning")).to_be_visible()
        expect(page.locator("#new-product")).to_be_enabled()
        page.locator("#new-product").click()
        page.locator("#product-name").fill("Черновик в памяти")
        expect(page.locator("#product-name")).to_have_value("Черновик в памяти")
        expect(page.locator("#draft-status")).to_have_text("Не сохранено")
        assert not network.generated
    finally:
        context.close()


def run(url, out):
    instrument = product()
    fixture = validate_draft(instrument, draft())
    fixture["generation"] = {"mode": "live_api", "model": "synthetic-test", "reported_cost_usd": 0}
    proof = {"passed": False, "paidModelCalls": 0, "screens": []}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="msedge", headless=True)
        try:
            for width, height in ((320, 568), (390, 844), (1440, 900)):
                context = browser.new_context(viewport={"width": width, "height": height},
                                              is_mobile=width < 600, has_touch=width < 600,
                                              locale="ru-RU", accept_downloads=True)
                network = IsolatedNetwork(url, "", None)
                context.route("**/*", network)
                page = context.new_page()
                errors, custom_gets = [], []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.on("request", lambda request: custom_gets.append(request.url)
                        if "/api/examples/custom" in request.url else None)
                page.goto(url + "/", wait_until="networkidle")
                expect(page.locator(".brand p")).to_contain_text("ИИ создаёт карточку")
                in_first_view(page.locator("#new-product"), "Ввод своего прибора", 44)
                page.screenshot(path=str(out / f"{width}-first.png"))
                page.locator("#new-product").click()
                expect(page.locator("#data-heading")).to_have_text("Данные вашего прибора")
                expect(page.locator("#product-name")).to_have_value("")
                expect(page.locator("#result")).to_be_hidden()
                in_first_view(page.locator("#generate"), "Запуск ИИ для своего прибора", 44)
                no_overflow(page, "Форма своего прибора")
                page.screenshot(path=str(out / f"{width}-own-product.png"))
                page.locator("#generate").click()
                expect(page.locator("#error")).to_contain_text("Название")
                assert not network.generated
                long_name = "Д" * 71
                page.locator("#product-name").fill(long_name)
                page.locator("#product-description").fill(instrument.description)
                page.locator("#generate").click()
                expect(page.locator("#error")).to_contain_text("Сократите название прибора до 70")
                expect(page.locator("#product-name")).to_have_value(long_name)
                assert not network.generated
                page.locator("#product-name").fill(instrument.name)
                page.locator("#product-manufacturer").fill(instrument.manufacturer)
                page.locator("#product-description").fill(instrument.description)
                page.locator("#mode-json").click()
                assert json.loads(page.locator("#product-json").input_value())["name"] == instrument.name
                page.locator("#product-json").fill(json.dumps(instrument.as_dict(), ensure_ascii=False))
                page.locator("#apply-json").click()
                expect(page.locator("#product-name")).to_have_value(instrument.name)
                page.locator('[data-example="testo-511"]').click()
                expect(page.locator("#seo-title")).to_contain_text("testo 511")
                page.locator("#new-product").click()
                expect(page.locator("#product-name")).to_have_value(instrument.name)
                assert not network.generated
                page.locator("#generate").click()
                expect(page.locator("#loading")).to_be_visible()
                assert network.generated == [instrument.as_dict()]
                output_card = fixture
                if width == 1440:
                    title = fixture["seo_title"].ljust(70, "🔧")
                    output_card = {**fixture, "seo_title": title, "validation": {
                        **fixture["validation"], "title_characters": len(title)}}
                network.finish({"card": output_card, "markdown": markdown(output_card)})
                expect(page.locator("#seo-title")).to_contain_text(instrument.name)
                expect(page.locator("#loading")).to_be_hidden()
                mode = page.locator("#mobile-generation-mode" if width < 600 else "#generation-mode")
                expect(mode).to_contain_text("Создано с ИИ")
                expect(page.locator("#specs")).to_contain_text("данных нет")
                expect(page.locator("#product-photo")).to_be_hidden()
                page.locator("#export-menu summary").click()
                with page.expect_download() as download:
                    page.locator("#json").click()
                assert download.value.suggested_filename == "тест-42-card.json"
                saved = json.loads(Path(download.value.path()).read_text("utf-8"))
                assert saved["seo_title"] == output_card["seo_title"]
                page.reload(wait_until="networkidle")
                expect(page.locator("#seo-title")).to_contain_text(instrument.name)
                show_view(page, "data")
                expect(page.locator("#product-name")).to_have_value(instrument.name)
                if width == 1440:
                    second = context.new_page()
                    second.goto(url + "/", wait_until="networkidle")
                    show_view(second, "data")
                    expect(second.locator("#product-name")).to_have_value(instrument.name)
                    updated_name = instrument.name + " — последняя правка"
                    page.evaluate("""() => {
                        const open = indexedDB.open('pribor-studio-workspace');
                        open.onsuccess = () => {
                            const db = open.result;
                            const tx = db.transaction('workspace', 'readwrite');
                            const store = tx.objectStore('workspace');
                            function hold() {
                                window.holdingDraftTransaction = true;
                                if (!window.releaseDraftTransaction)
                                    store.get('snapshot').onsuccess = hold;
                            }
                            store.get('snapshot').onsuccess = hold;
                            tx.oncomplete = () => db.close();
                        };
                    }""")
                    page.wait_for_function("window.holdingDraftTransaction")
                    page.locator("#product-name").fill(updated_name)
                    second.locator("#product-manufacturer").fill("Несохранённая правка")
                    page.evaluate("window.releaseDraftTransaction = true")
                    expect(page.locator("#draft-status")).to_have_text("Сохранено в браузере")
                    expect(second.locator("#storage-warning")).to_contain_text("изменён в другой вкладке")
                    expect(second.locator("#product-manufacturer")).to_have_value("Несохранённая правка")
                    second.reload(wait_until="networkidle")
                    show_view(second, "data")
                    expect(second.locator("#product-name")).to_have_value(updated_name)
                    second.locator('[data-example="testo-511"]').click()
                    show_view(second, "data")
                    second.locator("#product-manufacturer").fill("Правка в другой вкладке")
                    expect(second.locator("#draft-status")).to_have_text("Сохранено в браузере")
                    second.close(run_before_unload=True)
                    page.reload(wait_until="networkidle")
                    show_view(page, "data")
                    expect(page.locator("#product-manufacturer")).to_have_value("Правка в другой вкладке")
                    page.locator("#new-product").click()
                    show_view(page, "data")
                    expect(page.locator("#product-name")).to_have_value(updated_name)
                    expect(page.locator("#seo-title")).to_contain_text(instrument.name)
                    proof["twoTabsPreserveIndependentDrafts"] = True
                    proof["indexedDBSerializesConcurrentDraftWrites"] = True
                    proof["conflictingDraftStaysVisibleWithoutOverwritingStorage"] = True
                    writer = context.new_page()
                    writer.goto(url + "/", wait_until="networkidle")
                    page.locator("#generate").click()
                    expect(page.locator("#loading")).to_be_visible()
                    edit_workspace(writer, """state => {
                        const entry = state.entries.custom;
                        entry.edits = {
                            seo_title: 'Правка редактора во время генерации',
                            seo_description: entry.result.card.seo_description,
                            commercial: entry.result.card.commercial,
                        };
                    }""")
                    network.finish({"card": fixture, "markdown": markdown(fixture)})
                    expect(page.locator("#storage-warning")).to_contain_text("изменён в другой вкладке")
                    writer.close()
                    page.reload(wait_until="networkidle")
                    expect(page.locator("#seo-title")).to_have_text("Правка редактора во время генерации")
                    show_view(page, "data")
                    proof["pendingGenerationDoesNotOverwriteParallelManualEdit"] = True
                page.once("dialog", lambda dialog: dialog.accept())
                page.locator("#reset-draft").click()
                expect(page.locator("#product-name")).to_have_value("")
                expect(page.locator("#result")).to_be_hidden()
                page.reload(wait_until="networkidle")
                expect(page.locator("#product-name")).to_be_visible()
                expect(page.locator("#product-name")).to_have_value("")
                page.locator('[data-example="testo-510"]').click()
                expect(page.locator("#seo-title")).to_contain_text("testo 510")
                if width == 1440:
                    verify_saved_sample_migration(page, context, url)
                    proof["sampleMigrationPreservesDraftAndEdits"] = True
                    verify_legacy_storage_migration(page)
                    proof["legacyTabCannotOverwriteMigratedWorkspace"] = True
                expected_posts = 2 if width == 1440 else 1
                assert len(network.generated) == expected_posts, network.generated
                assert not errors, errors
                assert not custom_gets, custom_gets
                # Локальный антивирус может добавлять свой скрипт. Он остаётся заблокирован.
                external = [request for request in network.blocked if request != {
                    "method": "GET", "host": "me.kis.v2.scr.kaspersky-labs.com"}]
                assert not external, external
                proof["screens"].append({"width": width, "height": height, "passed": True,
                                         "simulatedPosts": expected_posts, "customExampleRequests": 0,
                                         "blockedBrowserInjection": len(network.blocked)})
                context.close()
            verify_unavailable_storage(browser, url)
            proof["blockedStorageAllowsInMemoryDraftAfterStartup"] = True
        finally:
            browser.close()
    proof["passed"] = True
    (out / "custom-product-proof.json").write_text(json.dumps(proof, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(proof))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    run(args.url.rstrip("/"), args.output)

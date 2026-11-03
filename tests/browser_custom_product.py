"""Проверяет независимый черновик прибора и запуск ИИ с тестовым ответом."""
import argparse
import json
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

from pribor.domain import markdown, validate_draft
from tests.browser_smoke import IsolatedNetwork, in_first_view, no_overflow, show_view
from tests.test_domain import draft, product


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
                network.finish({"card": fixture, "markdown": markdown(fixture)})
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
                assert saved["seo_title"] == fixture["seo_title"]
                page.reload(wait_until="networkidle")
                expect(page.locator("#seo-title")).to_contain_text(instrument.name)
                show_view(page, "data")
                expect(page.locator("#product-name")).to_have_value(instrument.name)
                page.once("dialog", lambda dialog: dialog.accept())
                page.locator("#reset-draft").click()
                expect(page.locator("#product-name")).to_have_value("")
                expect(page.locator("#result")).to_be_hidden()
                page.reload(wait_until="networkidle")
                expect(page.locator("#product-name")).to_be_visible()
                expect(page.locator("#product-name")).to_have_value("")
                page.locator('[data-example="testo-510"]').click()
                expect(page.locator("#seo-title")).to_contain_text("testo 510")
                assert len(network.generated) == 1, network.generated
                assert not errors, errors
                assert not custom_gets, custom_gets
                # Локальный антивирус может добавлять свой скрипт. Он остаётся заблокирован.
                external = [request for request in network.blocked if request != {
                    "method": "GET", "host": "me.kis.v2.scr.kaspersky-labs.com"}]
                assert not external, external
                proof["screens"].append({"width": width, "height": height, "passed": True,
                                         "simulatedPosts": 1, "customExampleRequests": 0,
                                         "blockedBrowserInjection": len(network.blocked)})
                context.close()
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

"""Дополнительная проверка UI. Нужен установленный Playwright и Chromium/Edge.

Запускать с уже работающим локальным сервером. Платные запросы заменяются фикстурой.
"""
import argparse
import json
import re
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8765")
    parser.add_argument("--output", default=".local/browser")
    parser.add_argument("--axe")
    args = parser.parse_args()
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)
    expected = urlsplit(args.url)
    assert expected.hostname in {"127.0.0.1", "localhost"}
    proof = {"passed": False, "widths": [], "paidModelCalls": 0,
             "browserClosed": False, "externalRequests": [], "mockedGeneration": True}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="msedge", headless=True)
        try:
            for width, height in [(1440, 1000), (1280, 900), (390, 844), (320, 800)]:
                context = browser.new_context(viewport={"width": width, "height": height},
                    locale="ru-RU", reduced_motion="reduce", accept_downloads=True,
                    is_mobile=width<600, has_touch=width<600, service_workers="block")
                errors = []
                def route(request):
                    target = urlsplit(request.request.url)
                    if target.scheme in {"data", "blob"}:
                        request.continue_()
                    elif target.netloc != expected.netloc:
                        proof["externalRequests"].append(target.hostname)
                        request.abort()
                    else:
                        request.continue_()
                context.route("**/*", route)
                page = context.new_page()
                page.on("pageerror", lambda err: errors.append(str(err)))
                try:
                    page.goto(args.url, wait_until="networkidle")
                    page.get_by_role("button", name="testo 510").wait_for()
                    expect(page.locator('#product-json')).to_have_value(re.compile('testo 510'))
                    assert page.get_by_role("button", name="Подготовить карточку").is_disabled()
                    assert "Просмотр примеров" in page.locator("#connection").inner_text()
                    page.screenshot(path=str(out/f"{width}-empty.png"), full_page=True)
                    for key in ["testo-510", "testo-511"]:
                        page.locator(f'[data-example="{key}"]').click()
                        expect(page.locator('#product-json')).to_have_value(re.compile(key.replace('-', ' ')))
                        page.get_by_role("button", name="Посмотреть готовый пример").click()
                        page.locator("#result").wait_for(state="visible")
                        assert key.replace("-", " ") in page.locator("#seo-title").inner_text()
                        assert page.locator("#specs .no-data").inner_text()=="данных нет"
                        assert "Гарантия" in page.locator("#warnings").inner_text()
                        assert "Сохранённый ответ API" in page.locator("#generation-mode").inner_text()
                        assert page.locator("#source").get_attribute("href").endswith("/"+key+"/")
                    page.locator('[data-example="testo-510"]').click()
                    expect(page.locator('#product-json')).to_have_value(re.compile('testo 510'))
                    page.get_by_role("button", name="Посмотреть готовый пример").click()
                    page.evaluate("window.scrollTo(0,0)")
                    page.screenshot(path=str(out/f"{width}-result.png"), full_page=True)
                    page.locator("summary").click()
                    assert page.locator("#evidence-list .evidence-entry").count()>=8
                    assert page.evaluate("document.documentElement.scrollWidth === innerWidth")
                    violations=[]
                    if args.axe:
                        context.route(args.url+"/__test_axe__.js", lambda r:r.fulfill(
                            path=args.axe, content_type="text/javascript"))
                        page.add_script_tag(url=args.url+"/__test_axe__.js")
                        violations=page.evaluate("""async()=>{const r=await axe.run(document,{runOnly:{type:'tag',values:['wcag2a','wcag2aa','wcag21aa']}});return r.violations.map(v=>({id:v.id,impact:v.impact,targets:v.nodes.map(n=>n.target)}))}""")
                        assert not violations, violations
                    if width==1440:
                        sample=page.request.get(args.url+"/api/examples/testo-510").json()
                        with page.expect_download() as event:
                            page.get_by_role("button", name="JSON ↓", exact=True).click()
                        event.value.save_as(str(out/"download.json"))
                        downloaded=json.loads((out/"download.json").read_text("utf-8"))
                        assert downloaded["seo_title"]==sample["card"]["seo_title"]
                        with page.expect_download() as event:
                            page.get_by_role("button", name="Markdown ↓", exact=True).click()
                        event.value.save_as(str(out/"download.md"))
                        assert (out/"download.md").read_text("utf-8")==sample["markdown"]
                        page.evaluate("navigator.clipboard.writeText=async(value)=>{window.__copied=value}")
                        page.get_by_role("button", name="Копировать текст").click()
                        assert page.evaluate("window.__copied")==sample["markdown"]
                        page.locator("#product-json").fill('{"name":"Тест"}')
                        assert page.locator("#saved").is_disabled()
                        assert page.locator("#result").is_hidden()
                        # Только UI-имитация генерации; внешний API запрещён маршрутизатором.
                        context.route("**/api/config", lambda r:r.fulfill(json={"ready":True,"model":"test-fixture"}))
                        context.route("**/api/generate", lambda r:r.fulfill(status=429,json={"error":{"message":"Тест: лимит API", "issues":[]}}))
                        page.reload(wait_until="networkidle")
                        expect(page.locator('#generate')).to_be_enabled()
                        page.locator("#product-json").fill('invalid JSON')
                        page.locator("#generate").click()
                        assert "Некорректный JSON" in page.locator("#error").inner_text()
                        page.locator("#product-json").fill('{"name":"Тест"}')
                        page.locator("#generate").click()
                        expect(page.locator('#error')).to_contain_text('Тест: лимит API')
                        assert page.locator("#generate").is_enabled()
                        context.unroute("**/api/generate")
                        fixture = sample["card"] | {"generation": sample["card"]["generation"] | {"model":"test-fixture"}}
                        context.route("**/api/generate", lambda r:r.fulfill(json={"card":fixture,"markdown":sample["markdown"]}))
                        page.locator("#generate").click()
                        page.locator("#result").wait_for(state="visible")
                        assert page.locator("#generate").is_enabled()
                    assert not errors, errors
                    proof["widths"].append({"width":width,"examplesChecked":2,"overflow":False,
                                            "pageErrors":errors,"axeViolations":violations})
                finally:
                    context.close()
            proof["passed"]=True
        finally:
            browser.close()
            proof["browserClosed"]=True
            (out/"browser-proof.json").write_text(json.dumps(proof,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps(proof,ensure_ascii=True))


if __name__ == "__main__":
    main()

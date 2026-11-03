"""Локальная веб-форма. Только loopback, без CMS, хранилища и фоновых вызовов."""
from __future__ import annotations

import argparse
import hashlib
import json
import ipaddress
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from .domain import AppError, Product, markdown, parse_json, validate_draft
from .llm import Config, generate, load_env
from .budget import Budget

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ("testo-510", "testo-511")


def input_hash(product):
    return hashlib.sha256(json.dumps(product.as_dict(), ensure_ascii=False,
                                    sort_keys=True).encode("utf-8")).hexdigest()


def example_data(key):
    if key not in EXAMPLES:
        raise AppError("not_found", "Пример не найден.", 404)
    product = Product.from_dict(parse_json((ROOT / "examples/inputs" / f"{key}.json").read_text("utf-8")))
    result_path = ROOT / "examples/results" / f"{key}.json"
    card = None
    if result_path.exists():
        saved = parse_json(result_path.read_text("utf-8"))
        if saved.get("input_sha256") != input_hash(product):
            raise AppError("example_stale", "Сохранённый пример не соответствует исходным данным.", 409)
        card = validate_draft(product, saved["draft"])
        card["generation"] = saved["generation"] | {"viewing_saved_result": True}
    return {"product": product.as_dict(), "card": card,
            "markdown": markdown(card) if card else None}


def create_server(port=8765, config=None, public_origin=None, budget=None):
    config = config or Config.from_env()
    gate = threading.BoundedSemaphore(1)
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    if bool(public_origin) != bool(budget):
        raise ValueError("Для публичного режима нужны origin и постоянный бюджет")
    if public_origin:
        origin = urlsplit(public_origin)
        if origin.scheme != "https" or not origin.hostname or origin.path or origin.query or origin.fragment or origin.username:
            raise ValueError("Нужен origin HTTPS без пути")
        allowed_hosts.add(origin.netloc)

    class Handler(BaseHTTPRequestHandler):
        server_version = "Pribor/1.0"

        def log_message(self, fmt, *args):
            pass  # Не записываем входные данные, ответы модели и ключи в логи.

        def security_headers(self):
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; "
                             "style-src 'self'; img-src 'self' data:; connect-src 'self'; "
                             "frame-ancestors 'none'; base-uri 'none'; form-action 'self'")

        def json(self, data, status=200):
            raw = json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.security_headers()
            self.end_headers()
            self.wfile.write(raw)

        def guard(self, write=False):
            if self.headers.get("Host") not in allowed_hosts:
                raise AppError("invalid_host", "Доступ разрешён только через локальный адрес.", 403)
            origin = self.headers.get("Origin")
            origins = {public_origin} if public_origin else {f"http://{h}" for h in allowed_hosts}
            if origin and origin not in origins:
                raise AppError("invalid_origin", "Запрос с постороннего сайта заблокирован.", 403)
            if write and self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
                raise AppError("invalid_content_type", "Отправьте application/json.", 415)

        def do_GET(self):
            try:
                self.guard()
                path = urlsplit(self.path).path
                if path == "/api/config":
                    snapshot = budget.snapshot() if budget else None
                    self.json({"ready": config.ready and (not snapshot or snapshot["can_generate"]),
                               "model": config.model if config.ready else None,
                               "example_ids": EXAMPLES, "public_demo": bool(budget), "budget": snapshot})
                    return
                if path.startswith("/api/examples/"):
                    self.json(example_data(path.rsplit("/", 1)[-1]))
                    return
                assets = {"/": "index.html", "/app.js": "app.js", "/style.css": "style.css"}
                if path not in assets:
                    raise AppError("not_found", "Страница не найдена.", 404)
                asset = ROOT / "web" / assets[path]
                raw = asset.read_bytes()
                mime = {".js": "text/javascript", ".css": "text/css", ".html": "text/html"}[asset.suffix]
                self.send_response(200)
                self.send_header("Content-Type", mime + "; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.security_headers()
                self.end_headers()
                self.wfile.write(raw)
            except AppError as exc:
                self.json(exc.payload(), exc.status)
            except sqlite3.Error:
                self.json(AppError("budget_unavailable", "Учёт бюджета временно недоступен. Новая генерация заблокирована.", 503).payload(), 503)
            except (OSError, KeyError, ValueError):
                self.json(AppError("internal_error", "Не удалось загрузить данные сервиса.", 500).payload(), 500)

        def do_POST(self):
            acquired = False
            try:
                self.guard(write=True)
                if urlsplit(self.path).path != "/api/generate":
                    raise AppError("not_found", "Метод не найден.", 404)
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    raise AppError("invalid_length", "Некорректный размер запроса.", 400) from None
                maximum = 16000 if budget else 32768
                if not 1 <= length <= maximum:
                    raise AppError("body_size", f"Размер JSON должен быть от 1 до {maximum} байт.", 413)
                self.connection.settimeout(10)
                try:
                    data = parse_json(self.rfile.read(length).decode("utf-8"))
                except UnicodeError:
                    raise AppError("invalid_encoding", "Используйте кодировку UTF-8.", 400) from None
                product = Product.from_dict(data)
                acquired = gate.acquire(blocking=False)
                if not acquired:
                    raise AppError("busy", "Генерация уже выполняется. Дождитесь результата.", 429)
                if budget:
                    raw_ip = self.headers.get("X-Pribor-Client-IP", self.client_address[0])
                    try:
                        client_ip = str(ipaddress.ip_address(raw_ip))
                    except ValueError:
                        raise AppError("invalid_client", "Некорректный адрес клиента.", 400) from None
                    card = budget.generate(product, config, client_ip)
                else:
                    card = generate(product, config)
                self.json({"card": card, "markdown": markdown(card)})
            except AppError as exc:
                self.json(exc.payload(), exc.status)
            except sqlite3.Error:
                self.json(AppError("budget_unavailable", "Учёт бюджета временно недоступен. Новая генерация заблокирована.", 503).payload(), 503)
            except (TimeoutError, OSError, ValueError):
                self.json(AppError("request_failed", "Запрос прерван. Повторите вручную.", 500).payload(), 500)
            finally:
                if acquired:
                    gate.release()

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    # При port=0 тест получает фактический свободный порт.
    allowed_hosts.update({f"127.0.0.1:{server.server_port}", f"localhost:{server.server_port}"})
    return server


def main():
    parser = argparse.ArgumentParser(description="AI-помощник контент-менеджера")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--public-origin")
    parser.add_argument("--budget-file", type=Path)
    parser.add_argument("--budget-usd", default="0.50")
    parser.add_argument("--initial-spent-usd", default="0")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("Порт должен быть от 1 до 65535")
    load_env(ROOT / ".env")
    config = Config.from_env()
    budget = Budget(args.budget_file,args.budget_usd,args.initial_spent_usd) if args.budget_file else None
    with create_server(args.port, config, args.public_origin, budget) as server:
        print(f"Откройте http://127.0.0.1:{args.port}", flush=True)
        print("API настроен." if config.ready else "Доступны сохранённые примеры. Для генерации настройте .env.", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\nСервис остановлен.")


if __name__ == "__main__":
    main()

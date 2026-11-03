"""Постоянный бюджет публичного демо. Не хранит тексты или открытые IP."""
from __future__ import annotations

import hashlib
import hmac
import io
import json
import math
import secrets
import sqlite3
import time
import urllib.request
import uuid
from decimal import Decimal, ROUND_CEILING
from contextlib import contextmanager
from pathlib import Path

from .domain import AppError, parse_json
from .llm import NoRedirect, generate

RESERVE = 100_000  # $0.10; консервативно больше верхней границы одного запроса.


def micro(value):
    amount = Decimal(str(value))
    if not amount.is_finite() or amount < 0:
        raise ValueError("Некорректная сумма бюджета")
    return int((amount * 1_000_000).to_integral_value(rounding=ROUND_CEILING))


class Budget:
    def __init__(self, path: Path, limit_usd="0.50", initial_spent_usd="0"):
        self.path = str(path)
        limit, initial = micro(limit_usd), micro(initial_spent_usd)
        if not 0 <= initial <= limit or limit > 500_000:
            raise ValueError("Демо ограничено суммой до $0.50")
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS config (id INTEGER PRIMARY KEY, cap INTEGER, initial INTEGER, salt TEXT)")
            db.execute("CREATE TABLE IF NOT EXISTS attempts (id TEXT PRIMARY KEY, client TEXT, at REAL, amount INTEGER, state TEXT)")
            db.execute("INSERT OR IGNORE INTO config VALUES (1,?,?,?)", (limit, initial, secrets.token_hex(32)))
            row = db.execute("SELECT cap,initial,salt FROM config WHERE id=1").fetchone()
            if row[:2] != (limit, initial):
                raise ValueError("Существующий бюджет нельзя изменить параметрами запуска")
            self.salt = row[2].encode()

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path, timeout=5)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def snapshot(self):
        with self.connect() as db:
            cap, initial = db.execute("SELECT cap,initial FROM config WHERE id=1").fetchone()
            spent = initial + db.execute("SELECT COALESCE(SUM(amount),0) FROM attempts WHERE state='confirmed'").fetchone()[0]
            held = db.execute("SELECT COALESCE(SUM(amount),0) FROM attempts WHERE state='reserved'").fetchone()[0]
        return {"limit_usd": cap/1_000_000, "spent_usd": spent/1_000_000,
                "held_usd": held/1_000_000, "remaining_usd": max(0,cap-spent-held)/1_000_000,
                "can_generate": cap-spent-held >= RESERVE}

    def reserve(self, client):
        identity = hmac.new(self.salt, client.encode(), hashlib.sha256).hexdigest()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            cap, initial = db.execute("SELECT cap,initial FROM config WHERE id=1").fetchone()
            used = initial + db.execute("SELECT COALESCE(SUM(amount),0) FROM attempts").fetchone()[0]
            if used + RESERVE > cap:
                raise AppError("demo_budget", "Бюджета демо недостаточно для нового запроса. Готовые примеры доступны.", 429)
            now = time.time()
            count = db.execute("SELECT COUNT(*) FROM attempts WHERE client=? AND at>?", (identity,now-900)).fetchone()[0]
            if count >= 3:
                raise AppError("demo_rate_limit", "В демо доступно 3 генерации за 15 минут. Готовые примеры доступны без ограничений.", 429)
            ticket = uuid.uuid4().hex
            db.execute("INSERT INTO attempts VALUES (?,?,?,?,?)", (ticket,identity,now,RESERVE,'reserved'))
        return ticket

    def settle(self, ticket, cost):
        if cost is None:
            return  # Неизвестная стоимость остаётся зарезервированной и после перезапуска.
        amount = micro(cost)
        with self.connect() as db:
            db.execute("UPDATE attempts SET amount=?,state='confirmed' WHERE id=? AND state='reserved'",
                       (amount,ticket))

    def generate(self, product, config, client, opener=None):
        direct = config.base_url == "https://openrouter.ai/api/v1"
        gateway = config.base_url == "https://tgw.ai-kabinet.ru/openrouter/api/v1" and bool(config.gateway_token)
        if not (direct or gateway) or config.model != "google/gemini-3-flash-preview":
            raise AppError("demo_config", "Публичный режим требует согласованной модели и API.", 503)
        if not product.description and not any(product.specs.values()):
            raise AppError("insufficient_data", "Добавьте назначение или характеристики прибора.", issues=product.missing())
        ticket = self.reserve(client)
        state = {"cost": None}
        transport = opener or urllib.request.build_opener(NoRedirect())

        class Recorder:
            def open(self, request, timeout):
                payload = json.loads(request.data)
                payload["provider"]["max_price"] = {"prompt":1, "completion":3}
                payload["reasoning"] = {"effort":"low", "exclude":True}
                request.data = json.dumps(payload,ensure_ascii=False).encode("utf-8")
                # Не более 64 КБ входа + 3500 выходных токенов при ценовом потолке:
                # верхняя оценка < $0.10, включая запас на служебные токены.
                if len(request.data) > 64000:
                    state["cost"] = 0
                    raise AppError("demo_size", "Слишком большой запрос для публичного демо.", 413)
                with transport.open(request,timeout=timeout) as response:
                    raw = response.read(262145)
                try:
                    envelope = parse_json(raw.decode("utf-8"))
                    cost = envelope.get("usage",{}).get("cost")
                    if isinstance(cost,(int,float)) and not isinstance(cost,bool) and math.isfinite(cost) and cost>=0:
                        state["cost"] = cost
                except (AppError,UnicodeError,AttributeError):
                    pass
                return io.BytesIO(raw)

        try:
            return generate(product,config,Recorder())
        except AppError as exc:
            if exc.code in {"provider_auth","provider_balance","provider_rate_limit","not_configured"}:
                state["cost"] = 0
            raise
        finally:
            self.settle(ticket,state["cost"])

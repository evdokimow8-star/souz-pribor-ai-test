"""Постоянный бюджет публичного демо. Не хранит тексты или открытые IP."""
from __future__ import annotations

import hashlib
import hmac
import io
import json
import secrets
import sqlite3
import time
import urllib.request
import uuid
from dataclasses import replace
from decimal import Decimal, ROUND_CEILING
from contextlib import contextmanager
from pathlib import Path

from .domain import AppError, parse_json
from .llm import NoRedirect, generate, reported_cost

RESERVE = 100_000  # $0.10; консервативно больше верхней границы одного запроса.
DEMO_COMPLETION_PRICE_CAPS = {
    "google/gemini-3-flash-preview": 3,
    "google/gemini-3.8-flash": 3.75,
}
REPAIR_MODEL = "google/gemini-4-argon"


def micro(value):
    amount = Decimal(str(value))
    if not amount.is_finite() or amount < 0:
        raise ValueError("Некорректная сумма бюджета")
    return int((amount * 1_000_000).to_integral_value(rounding=ROUND_CEILING))


class Budget:
    def __init__(self, path: Path, limit_usd="0.50", initial_spent_usd="0", *,
                 requests_per_window=3):
        self.path = str(path)
        limit, initial = micro(limit_usd), micro(initial_spent_usd)
        if not 0 <= initial <= limit or limit > 1_000_000:
            raise ValueError("Демо ограничено суммой до $1")
        if type(requests_per_window) is not int or not 1 <= requests_per_window <= 10:
            raise ValueError("Лимит запросов за 15 минут должен быть целым числом от 1 до 10")
        self.requests_per_window = requests_per_window
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
            cap, initial, confirmed, held, held_count = db.execute("""
                SELECT cap, initial,
                       COALESCE(SUM(CASE WHEN state='confirmed' THEN amount ELSE 0 END), 0),
                       COALESCE(SUM(CASE WHEN state='reserved' THEN amount ELSE 0 END), 0),
                       COUNT(CASE WHEN state='reserved' THEN 1 END)
                FROM config LEFT JOIN attempts ON 1=1 WHERE config.id=1
            """).fetchone()
        spent = initial + confirmed
        remaining = max(0, cap - spent - held)
        reason = None
        if remaining < RESERVE:
            reason = "unconfirmed_cost" if cap - spent >= RESERVE else "budget_exhausted"
        return {"limit_usd": cap/1_000_000, "spent_usd": spent/1_000_000,
                "held_usd": held/1_000_000, "remaining_usd": remaining/1_000_000,
                "can_generate": reason is None, "held_attempts": held_count,
                "unavailable_reason": reason}

    def reserve(self, client):
        identity = (hmac.new(self.salt, client.encode(), hashlib.sha256).hexdigest()
                    if client is not None else None)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            cap, initial = db.execute("SELECT cap,initial FROM config WHERE id=1").fetchone()
            confirmed, held = db.execute("""
                SELECT COALESCE(SUM(CASE WHEN state='confirmed' THEN amount ELSE 0 END), 0),
                       COALESCE(SUM(CASE WHEN state='reserved' THEN amount ELSE 0 END), 0)
                FROM attempts
            """).fetchone()
            spent = initial + confirmed
            if spent + held + RESERVE > cap:
                if spent + RESERVE <= cap:
                    raise AppError("demo_pending_cost", "Новый запрос недоступен: часть бюджета зарезервирована по запросам без подтверждённой стоимости. Готовые примеры доступны.", 429)
                raise AppError("demo_budget", "Бюджета демо недостаточно для нового запроса. Готовые примеры доступны.", 429)
            now = time.time()
            if identity is not None:
                count = db.execute("SELECT COUNT(*) FROM attempts WHERE client=? AND at>?", (identity,now-900)).fetchone()[0]
                if count >= self.requests_per_window:
                    raise AppError("demo_rate_limit", f"Лимит генераций за 15 минут — {self.requests_per_window}. Готовые примеры доступны без ограничений.", 429)
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
        completion_price_cap = DEMO_COMPLETION_PRICE_CAPS.get(config.model)
        if not (direct or gateway) or completion_price_cap is None:
            raise AppError("demo_config", "Публичный режим требует согласованной модели и API.", 503)
        if not product.description and not any(product.specs.values()):
            raise AppError("insufficient_data", "Добавьте назначение или характеристики прибора.", issues=product.missing())
        started = time.monotonic()
        costs = []
        try:
            return self._generate_attempt(product, config, client, opener, costs,
                                          {"prompt": 1, "completion": completion_price_cap}, 64000)
        except AppError as rejected:
            if config.model != "google/gemini-3.8-flash" or rejected.code != "draft_rejected":
                raise
            remaining_time = min(config.timeout, 60) - (time.monotonic() - started)
            if remaining_time < 1:
                raise
            # Диагностика может содержать куски текста модели после второго двоеточия.
            # Для исправления нужны только поле и тип нарушения.
            issues = [": ".join(issue.split(": ", 2)[:2])[:180] for issue in rejected.issues[:20]]
            try:
                repair_config = replace(config, model=REPAIR_MODEL, timeout=remaining_time)
                card = self._generate_attempt(product, repair_config, None,
                                              opener, costs, {"prompt": 2, "completion": 10}, 30000,
                                              repair_issues=issues)
            except AppError as repair_error:
                if repair_error.code in {"demo_budget", "demo_pending_cost", "demo_size"}:
                    raise rejected from None
                raise
        card["generation"].update(
            first_model=config.model,
            elapsed_seconds=round(time.monotonic() - started, 2),
            reported_cost_usd=float(sum(Decimal(str(cost)) for cost in costs))
            if all(cost is not None for cost in costs) else None,
            cost_note="Сумма стоимости двух вызовов API; null, если стоимость хотя бы одного неизвестна.")
        return card

    def _generate_attempt(self, product, config, client, opener, costs, price_cap, max_bytes,
                          *, repair_issues=()):
        state = {"cost": None}
        transport = opener or urllib.request.build_opener(NoRedirect())
        ticket = self.reserve(client)

        class Recorder:
            def open(self, request, timeout):
                payload = json.loads(request.data)
                payload["provider"]["max_price"] = price_cap
                payload["reasoning"] = {"effort":"low", "exclude":True}
                request.data = json.dumps(payload,ensure_ascii=False).encode("utf-8")
                # 64 КБ и $1/$3.75 для Flash, 30 КБ и $2/$10 для Argon.
                # При 3500 выходных токенов оба предела укладываются в резерв $0.10.
                if len(request.data) > max_bytes:
                    state["cost"] = 0
                    raise AppError("demo_size", "Слишком большой запрос для публичного демо.", 413)
                with transport.open(request,timeout=timeout) as response:
                    raw = response.read(262145)
                try:
                    envelope = parse_json(raw.decode("utf-8"))
                    state["cost"] = reported_cost(envelope)
                except (AppError,UnicodeError):
                    pass
                return io.BytesIO(raw)

        try:
            return generate(product, config, Recorder(), repair_issues=repair_issues)
        except AppError as exc:
            if exc.code in {"provider_auth","provider_balance","provider_rate_limit","not_configured"}:
                state["cost"] = 0
            raise
        finally:
            self.settle(ticket,state["cost"])
            costs.append(state["cost"])

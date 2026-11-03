"""Нормализация входа и независимая от модели проверка результата."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from decimal import Decimal
from urllib.parse import urlsplit


class AppError(Exception):
    def __init__(self, code: str, message: str, status: int = 422, issues=None):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status
        self.issues = issues or []

    def payload(self):
        return {"error": {"code": self.code, "message": self.message, "issues": self.issues}}


def parse_json(raw: str):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Повторяющийся ключ JSON")
            result[key] = value
        return result

    try:
        return json.loads(raw, object_pairs_hook=unique,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, TypeError, RecursionError):
        raise AppError("invalid_json", "Некорректный JSON. Проверьте кавычки, запятые и ключи.") from None


def text(value, label, limit=4000, optional=False):
    if value is None and optional:
        return ""
    if not isinstance(value, str) or len(value) > limit:
        raise AppError("invalid_input", f"Поле «{label}» должно быть строкой до {limit} символов.")
    value = value.strip()
    if not value and not optional:
        raise AppError("missing_input", f"Заполните поле «{label}».")
    if any(ord(c) < 32 and c not in "\n\t\r" for c in value):
        raise AppError("invalid_input", f"Недопустимые управляющие символы: {label}.")
    return value


@dataclass(frozen=True)
class Product:
    name: str
    manufacturer: str
    specs: dict
    description: str
    source_url: str
    notes: list

    @classmethod
    def from_dict(cls, data):
        if not isinstance(data, dict):
            raise AppError("invalid_input", "Ожидается JSON-объект с данными одного товара.")
        extra = set(data) - {"name", "manufacturer", "specs", "description", "source_url", "notes"}
        if extra:
            raise AppError("invalid_input", "Неизвестные поля: " + ", ".join(sorted(extra)))
        name = text(data.get("name"), "name", 160)
        manufacturer = text(data.get("manufacturer", ""), "manufacturer", 160, True)
        description = text(data.get("description", ""), "description", 6000, True)
        specs = data.get("specs", {})
        if not isinstance(specs, dict) or len(specs) > 40:
            raise AppError("invalid_input", "specs — объект, не более 40 характеристик.")
        normalized = {}
        for key, value in specs.items():
            key = text(key, "название характеристики", 100)
            if key in normalized:
                raise AppError("invalid_input", "Названия характеристик совпадают после удаления пробелов.")
            normalized[key] = text(value, key, 600, True)
        source = text(data.get("source_url", ""), "source_url", 1500, True)
        if source:
            try:
                url = urlsplit(source)
                valid = url.scheme in {"http", "https"} and url.hostname and not url.username and not url.password
            except ValueError:
                valid = False
            if not valid:
                raise AppError("invalid_input", "source_url должен быть обычной ссылкой HTTP(S).")
        notes = data.get("notes", [])
        if not isinstance(notes, list) or len(notes) > 10:
            raise AppError("invalid_input", "notes — список, не более 10 замечаний.")
        return cls(name, manufacturer, normalized, description, source,
                   [text(n, "замечание", 600) for n in notes])

    def facts(self):
        facts = {"name": self.name}
        if self.manufacturer:
            facts["manufacturer"] = self.manufacturer
        for i, (key, value) in enumerate(self.specs.items(), 1):
            if value:
                facts[f"spec_{i}"] = f"{key}: {value}"
        if self.description:
            facts["description"] = self.description
        return facts

    def missing(self):
        missing = []
        if not self.manufacturer:
            missing.append("Производитель: данных нет")
        if not self.description:
            missing.append("Описание назначения: данных нет")
        if not self.specs:
            missing.append("Технические характеристики: данных нет")
        missing.extend(f"{key}: данных нет" for key, value in self.specs.items() if not value)
        return missing

    def as_dict(self):
        return {"name": self.name, "manufacturer": self.manufacturer, "specs": self.specs,
                "description": self.description, "source_url": self.source_url, "notes": self.notes}


BLOCK_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {"text": {"type": "string"},
                   "fact_ids": {"type": "array", "items": {"type": "string"}}},
    "required": ["text", "fact_ids"],
}
OUTPUT_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "seo_title": BLOCK_SCHEMA,
        "description_paragraphs": {"type": "array", "minItems": 3, "maxItems": 3,
            "items": {"type": "object", "additionalProperties": False,
                "properties": {"text": {"type": "string", "minLength": 270, "maxLength": 360},
                               "fact_ids": {"type": "array", "items": {"type": "string"}}},
                "required": ["text", "fact_ids"]}},
        "search_queries": {"type": "array", "items": BLOCK_SCHEMA},
        "commercial": BLOCK_SCHEMA,
    },
    "required": ["seo_title", "description_paragraphs", "search_queries", "commercial"],
}

NUMBERS = re.compile(r"\d+(?:[.,]\d+)?")
TECH = re.compile(r"(?<![\w])(?:IP\d{2}|RS[- ]?\d+|USB|Bluetooth|Wi-Fi|GSM|GPRS)(?![\w])")
QUANTITY_UNIT = re.compile(r"\d\s*(МПа|мПа|кПа|гПа|Па|мбар|бар|кВ|мВ|В|мА|А|кГц|Гц|мм|см|м|кг|г|°C|%)(?![\w])")
PROHIBITED = re.compile(r"(?:гарантированн|лучший|самый точный|высок\w* точн|минимизир|сертифицир|взрывозащищ|"
                        r"в наличии|бесплатн\w* доставк|срок поставки)", re.IGNORECASE)
FEATURES = [re.compile(p, re.IGNORECASE) for p in (
    r"жидкокристал|\bLCD\b|\bЖКИ?\b", r"\bOLED\b", r"\bLED\b|светодиодн",
    r"приобрета\w* отдельно|не входит в (?:стандартн\w* )?комплект|не входит в (?:стандартн\w* )?поставку",
)]


def numbers(value):
    return {str(Decimal(v.replace(",", ".")).normalize()) for v in NUMBERS.findall(value)}


def technical_tokens(value):
    return set(TECH.findall(value)) | set(QUANTITY_UNIT.findall(value))


def validate_draft(product: Product, draft: dict):
    """Проверяем формат, ссылки на факты, новые числа и технические обозначения.

    Это ограниченный фактологический барьер, не доказательство смысла всех фраз.
    """
    issues, facts = [], product.facts()
    if not isinstance(draft, dict) or set(draft) != set(OUTPUT_SCHEMA["required"]):
        raise AppError("invalid_model_output", "Модель вернула неправильную структуру.", 502)

    def block(value, field, max_len=2000):
        if not isinstance(value, dict) or set(value) != {"text", "fact_ids"}:
            issues.append(f"{field}: требуется текст и ссылки на исходные факты")
            return ""
        content, refs = value["text"], value["fact_ids"]
        if not isinstance(content, str) or not content.strip() or len(content) > max_len:
            issues.append(f"{field}: пустой или слишком длинный текст")
            return ""
        if (not isinstance(refs, list) or not refs or len(refs) > len(facts)
                or any(not isinstance(ref, str) or ref not in facts for ref in refs)):
            issues.append(f"{field}: неизвестные или отсутствующие ссылки на факты")
            return content
        source = "\n".join(facts[ref] for ref in refs)
        unknown = numbers(content) - numbers(source)
        if unknown:
            issues.append(f"{field}: числа отсутствуют в указанных фактах: {', '.join(sorted(unknown))}")
        extra_tech = technical_tokens(content) - technical_tokens(source)
        if extra_tech:
            issues.append(f"{field}: неподтверждённые обозначения: {', '.join(sorted(extra_tech))}")
        if PROHIBITED.search(content) and not PROHIBITED.search(source):
            issues.append(f"{field}: неподтверждённое коммерческое или техническое обещание")
        if any(pattern.search(content) and not pattern.search(source) for pattern in FEATURES):
            issues.append(f"{field}: неподтверждённая деталь конструкции или комплектации")
        for key, value in product.specs.items():
            if (value and "погрешност" in key.casefold() and
                    (";" in value or value.count("±") > 1) and
                    "погрешност" in content.casefold() and numbers(content) and value not in content):
                issues.append(f"{field}: сложную погрешность нельзя пересказывать частично; см. таблицу")
        if re.search(r"<[^>]*>|https?://|\]\(", content):
            issues.append(f"{field}: разметка и ссылки в тексте не допускаются")
        return content.strip()

    title = block(draft["seo_title"], "seo_title", 70)
    if product.name.casefold() not in title.casefold():
        issues.append("seo_title: отсутствует точное название товара")
    paragraphs = draft["description_paragraphs"]
    if not isinstance(paragraphs, list) or not 2 <= len(paragraphs) <= 5:
        issues.append("description_paragraphs: требуется от 2 до 5 абзацев")
        paragraphs = []
    description = "\n\n".join(block(p, f"description_paragraphs[{i}]")
                              for i, p in enumerate(paragraphs))
    if not 800 <= len(description) <= 1200:
        issues.append(f"seo_description: {len(description)} символов, требуется 800–1200")
    queries = draft["search_queries"]
    if not isinstance(queries, list) or not 3 <= len(queries) <= 5:
        issues.append("search_queries: требуется от 3 до 5 запросов")
        queries = []
    query_texts = [block(q, f"search_queries[{i}]", 140) for i, q in enumerate(queries)]
    if len({q.casefold() for q in query_texts}) != len(query_texts):
        issues.append("search_queries: запросы повторяются")
    commercial = block(draft["commercial"], "commercial", 800)
    if issues:
        raise AppError("draft_rejected", "Черновик не прошёл проверку. Публикация заблокирована.",
                       422, issues)
    characteristics = [{"name": k, "value": v or "данных нет"}
                       for k, v in product.specs.items()]
    return {"seo_title": title, "seo_description": description,
            "characteristics": characteristics, "search_queries": query_texts,
            "commercial": commercial, "missing_fields": product.missing(),
            "warnings": product.notes,
            "source_url": product.source_url,
            "evidence": {"facts": facts, "draft": draft},
            "validation": {"title_characters": len(title),
                           "description_characters": len(description),
                           "numeric_and_technical_check": "passed",
                           "requires_editor_review": True}}


def markdown(card):
    lines = [f"# {card['seo_title']}", "", card["seo_description"], "", "## Характеристики", ""]
    lines.extend(f"- {s['name']}: {s['value']}" for s in card["characteristics"])
    lines += ["", "## Поисковые запросы", ""]
    lines.extend(f"- {q}" for q in card["search_queries"])
    lines += ["", "## Для менеджера", "", card["commercial"]]
    if card["missing_fields"] or card["warnings"]:
        lines += ["", "## Уточнить перед публикацией", ""]
        lines.extend(f"- {s}" for s in card["missing_fields"] + card["warnings"])
    if card["source_url"]:
        lines += ["", "Источник: " + card["source_url"]]
    lines += ["", "Черновик: требуется проверка редактором.", ""]
    return "\n".join(lines)

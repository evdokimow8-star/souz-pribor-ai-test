import copy
import unittest

from pribor.domain import AppError, Product, markdown, parse_json, validate_draft


def product():
    return Product.from_dict({"name": "Тест 42", "manufacturer": "Завод",
        "specs": {"Давление": "0 ... 100 гПа", "Защита": "IP40", "Питание": None},
        "description": "Переносной манометр для контроля давления воздуха."})


def draft():
    paragraph = ("Прибор используется для контроля давления воздуха. "
                 "Исходные данные позволяют подготовить описание назначения прибора. "
                 "Перед выбором исполнения необходимо проверить условия применения и состав поставки. ")
    block = lambda t: {"text": t, "fact_ids": ["name", "manufacturer", "spec_1", "spec_2", "description"]}
    return {"seo_title": block("Тест 42 — манометр давления"),
            "description_paragraphs": [block(paragraph * 2), block(paragraph * 2)],
            "search_queries": [block("Тест 42"), block("манометр Завод"), block("манометр воздуха")],
            "commercial": block("Для контроля давления воздуха. Уточните условия применения.")}


class DomainTests(unittest.TestCase):
    def reject(self, candidate, code="draft_rejected"):
        with self.assertRaises(AppError) as error:
            validate_draft(product(), candidate)
        self.assertEqual(error.exception.code, code)

    def test_valid_and_exact_specs(self):
        result = validate_draft(product(), draft())
        self.assertEqual(result["characteristics"][0]["value"], "0 ... 100 гПа")
        self.assertEqual(result["characteristics"][2]["value"], "данных нет")
        self.assertIn("Питание: данных нет", result["missing_fields"])
        self.assertTrue(result["validation"]["requires_editor_review"])

    def test_new_number_rejected(self):
        candidate = draft()
        candidate["commercial"]["text"] = "Работает до 250 гПа."
        self.reject(candidate)

    def test_changed_unit_with_known_numbers_rejected(self):
        candidate = draft()
        candidate["commercial"]["text"] = "Диапазон 0 ... 100 МПа."
        self.reject(candidate)

    def test_russian_preposition_is_not_a_volt_unit(self):
        candidate = draft()
        candidate["commercial"] = {"text": "В приборе используются данные измерения давления.",
                                   "fact_ids": ["description"]}
        validate_draft(product(), candidate)

    def test_new_unit_without_space_rejected(self):
        candidate = draft()
        candidate["commercial"]["text"] = "Диапазон 0...100МПа."
        self.reject(candidate)

    def test_changed_ip_rejected(self):
        candidate = draft()
        candidate["commercial"]["text"] = "Защита IP68."
        self.reject(candidate)

    def test_unprovided_interface_rejected(self):
        candidate = draft()
        candidate["commercial"]["text"] = "Связь по Bluetooth."
        self.reject(candidate)

    def test_number_must_be_in_cited_fact(self):
        candidate = draft()
        candidate["commercial"] = {"text": "Прибор Тест 42.", "fact_ids": ["manufacturer"]}
        self.reject(candidate)

    def test_unknown_and_missing_fact_ids(self):
        for refs in [[], ["does_not_exist"], [None], "name"]:
            with self.subTest(refs=refs):
                candidate = draft()
                candidate["commercial"]["fact_ids"] = refs
                self.reject(candidate)

    def test_title_limit_and_name(self):
        for title in ["Другой прибор", "Тест 42 " + "а" * 70]:
            with self.subTest(title=title):
                candidate = draft()
                candidate["seo_title"]["text"] = title
                self.reject(candidate)

    def test_short_description_rejected(self):
        candidate = draft()
        candidate["description_paragraphs"] = [{"text": "Манометр.", "fact_ids": ["description"]}] * 2
        self.reject(candidate)

    def test_duplicate_queries(self):
        candidate = draft()
        candidate["search_queries"][2] = copy.deepcopy(candidate["search_queries"][0])
        self.reject(candidate)

    def test_html_and_unsupported_promise(self):
        for value in ["<script>alert(42)</script>", "Гарантированно лучший прибор.", "Есть в наличии."]:
            candidate = draft()
            candidate["commercial"]["text"] = value
            self.reject(candidate)

    def test_extra_model_field(self):
        candidate = draft()
        candidate["price"] = 100
        self.reject(candidate, "invalid_model_output")

    def test_missing_option_not_treated_as_excluded_from_package(self):
        for value in ["Трубка приобретается отдельно.", "Трубка не входит в комплект.",
                      "Прибор оснащён жидкокристаллическим дисплеем."]:
            candidate = draft()
            candidate["commercial"]["text"] = value
            self.reject(candidate)

    def test_invalid_input_types(self):
        for data in [None, [], {}, {"name": ""}, {"name": "a", "specs": []},
                     {"name": "a", "specs": {"x": 0}}, {"name": "a", "notes": "text"},
                     {"name": "a", "unknown": "field"}, {"name": "a", "source_url": "javascript:alert(1)"}]:
            with self.subTest(data=data), self.assertRaises(AppError):
                Product.from_dict(data)

    def test_malformed_source_url(self):
        with self.assertRaises(AppError):
            Product.from_dict({"name": "Прибор", "source_url": "https://[bad"})

    def test_duplicate_and_non_finite_json(self):
        for raw in ['{"name":"a","name":"b"}', '{"a":NaN}', 'broken', '[']:
            with self.subTest(raw=raw), self.assertRaises(AppError):
                parse_json(raw)

    def test_missing_data_and_null(self):
        data = Product.from_dict({"name": "Прибор", "manufacturer": None, "description": None})
        self.assertEqual(len(data.missing()), 3)

    def test_markdown_has_required_sections_and_missing(self):
        content = markdown(validate_draft(product(), draft()))
        for expected in ["# Тест 42", "## Характеристики", "## Поисковые запросы", "## Для менеджера", "Питание: данных нет"]:
            self.assertIn(expected, content)


if __name__ == "__main__":
    unittest.main()

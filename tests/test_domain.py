import copy
import unittest

from pribor.domain import AppError, OUTPUT_SCHEMA, Product, markdown, parse_json, validate_draft


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

    def test_description_schema_and_validator_agree_on_two_to_five_paragraphs(self):
        schema = OUTPUT_SCHEMA["properties"]["description_paragraphs"]
        self.assertEqual((schema["minItems"], schema["maxItems"]), (2, 5))
        self.assertNotIn("minLength", schema["items"]["properties"]["text"])
        self.assertNotIn("maxLength", schema["items"]["properties"]["text"])
        source = draft()["description_paragraphs"][0]
        text = " ".join(p["text"] for p in draft()["description_paragraphs"])
        for count in range(2, 6):
            with self.subTest(count=count):
                candidate = draft()
                width = len(text) // count
                candidate["description_paragraphs"] = [
                    {"text": text[i * width:(i + 1) * width] if i < count - 1 else text[i * width:],
                     "fact_ids": source["fact_ids"]} for i in range(count)]
                result = validate_draft(product(), candidate)
                self.assertTrue(800 <= len(result["seo_description"]) <= 1200)
        for count in (1, 6):
            candidate = draft()
            candidate["description_paragraphs"] = [source] * count
            self.reject(candidate)

    def test_description_total_length_stays_mandatory(self):
        for size in (799, 1201):
            candidate = draft()
            candidate["description_paragraphs"] = [
                {"text": "а" * 400, "fact_ids": ["name"]},
                {"text": "а" * (size - 402), "fact_ids": ["name"]}]
            self.reject(candidate)

    def test_new_number_rejected(self):
        candidate = draft()
        candidate["commercial"]["text"] = "Работает до 250 гПа."
        self.reject(candidate)

    def test_changed_unit_with_known_numbers_rejected(self):
        candidate = draft()
        candidate["commercial"]["text"] = "Диапазон 0 ... 100 МПа."
        self.reject(candidate)

    def test_temperature_sign_and_number_unit_pair_are_preserved(self):
        original = product().as_dict()
        original["specs"]["Рабочая температура"] = "0 ... +50 °C"
        instrument = Product.from_dict(original)
        candidate = draft()
        for text in ("Температура до −50 °C.", "Диапазон давления до 50 гПа.",
                     "Давление от 100 до 0 гПа.", "Температура от 50 °C до 0 °C.",
                     "Температура от 50 до 0 градусов Цельсия.",
                     "Корпус из нержавеющей стали.",
                     "Прибор оснащён встроенной памятью и сенсорным экраном.",
                     "Все операции по настройке высоты и переключению режимов выполняет пользователь."):
            with self.subTest(text=text):
                candidate["commercial"] = {"text": text, "fact_ids": ["spec_1", "spec_4"]}
                with self.assertRaises(AppError) as error:
                    validate_draft(instrument, candidate)
                self.assertEqual(error.exception.code, "draft_rejected")
        candidate["commercial"] = {"text": "Давление от 0 гПа до 100 гПа. Температура до 50 °C.",
                                   "fact_ids": ["spec_1", "spec_4"]}
        validate_draft(instrument, candidate)

    def test_name_must_fit_the_title_before_generation(self):
        self.assertEqual(len(Product.from_dict({"name": "а" * 70}).name), 70)
        with self.assertRaises(AppError) as error:
            Product.from_dict({"name": "а" * 71})
        self.assertEqual(error.exception.code, "invalid_input")

    def test_json_rejects_unpaired_surrogates_in_keys_and_values(self):
        for raw in (r'{"name":"\ud800"}', r'{"\ud800":"name"}'):
            with self.subTest(raw=raw), self.assertRaises(AppError):
                parse_json(raw)
        self.assertEqual(parse_json(r'{"name":"\ud83d\ude00"}')["name"], "😀")

    def test_russian_preposition_is_not_a_volt_unit(self):
        candidate = draft()
        candidate["commercial"] = {"text": "В приборе используются данные измерения давления.",
                                   "fact_ids": ["description"]}
        validate_draft(product(), candidate)

    def test_new_unit_without_space_rejected(self):
        candidate = draft()
        candidate["commercial"]["text"] = "Диапазон 0...100МПа."
        self.reject(candidate)

    def test_spaced_compound_units_in_queries_preserve_value_and_citation(self):
        original = product().as_dict()
        original["specs"]["Скорость"] = "0 ... 30 м/с"
        original["specs"]["Момент"] = "0,5 Н·м"
        instrument = Product.from_dict(original)
        for value, refs in (("скорость от 0 до 30 м / с", ["spec_4"]),
                            ("момент 0.5 Н · м", ["spec_5"])):
            candidate = draft()
            candidate["search_queries"][2] = {"text": value, "fact_ids": refs}
            validate_draft(instrument, candidate)
        for value, refs in (("скорость от 30 до 0 м / с", ["spec_4"]),
                            ("скорость до −30 м / с", ["spec_4"]),
                            ("скорость до 30 м", ["spec_4"]),
                            ("скорость до 30 м / с", ["description"]),
                            ("момент 0.5 Н · м", ["spec_4"])):
            with self.subTest(value=value, refs=refs):
                candidate = draft()
                candidate["search_queries"][2] = {"text": value, "fact_ids": refs}
                with self.assertRaises(AppError):
                    validate_draft(instrument, candidate)

    def test_fraction_spacing_does_not_allow_reversal_conversion_or_sign_change(self):
        original = product().as_dict()
        original["specs"]["Привод"] = "1/4 дюйма"
        original["specs"]["Диапазон длины"] = "−1/4 ... +3/4 м"
        instrument = Product.from_dict(original)
        for value in ("Привод 1 / 4 дюйма.", "Диапазон от −1 / 4 до +3 / 4 м."):
            candidate = draft()
            candidate["commercial"] = {"text": value, "fact_ids": ["spec_4", "spec_5"]}
            validate_draft(instrument, candidate)
        for value in ("Привод 4 / 1 дюйма.", "Привод 0,25 дюйма.",
                      "Привод −1 / 4 дюйма.", "Диапазон от +3 / 4 до −1 / 4 м.",
                      "Диапазон от +1 / 4 до +3 / 4 м."):
            with self.subTest(value=value):
                candidate = draft()
                candidate["commercial"] = {"text": value, "fact_ids": ["spec_4", "spec_5"]}
                with self.assertRaises(AppError):
                    validate_draft(instrument, candidate)

    def test_explicit_unknown_feature_requires_matching_missing_field(self):
        original = product().as_dict()
        original["specs"]["Память"] = None
        original["specs"]["Тип дисплея"] = None
        instrument = Product.from_dict(original)
        for value in ("Данных о встроенной памяти нет.",
                      "Сведений о наличии встроенной памяти нет.",
                      "Наличие встроенной памяти не подтверждено.",
                      "Данных о сенсорном экране нет."):
            with self.subTest(value=value):
                candidate = draft()
                candidate["commercial"] = {"text": value, "fact_ids": ["name"]}
                validate_draft(instrument, candidate)
        for value in ("Встроенной памяти нет.",
                      "Прибор оснащён встроенной памятью. Данных о встроенной памяти нет.",
                      "Данных о встроенной памяти нет, но она сохраняет измерения.",
                      "Данных о встроенной памяти нет. Прибор имеет сенсорный экран.",
                      "Данных о встроенной памяти на 100 значений нет.",
                      "Данных о встроенной памяти нет. Корпус из нержавеющей стали."):
            with self.subTest(value=value):
                candidate = draft()
                candidate["commercial"] = {"text": value, "fact_ids": ["name"]}
                with self.assertRaises(AppError):
                    validate_draft(instrument, candidate)
        candidate = draft()
        candidate["commercial"] = {"text": "Данных о встроенной памяти нет.", "fact_ids": ["name"]}
        self.reject(candidate)  # Пустое питание — не основание говорить о памяти.
        original["specs"]["Память"] = "16000 значений"
        with self.assertRaises(AppError):
            validate_draft(Product.from_dict(original), candidate)
        original["specs"]["Память"] = None
        original["description"] += " У прибора встроенная память."
        with self.assertRaises(AppError):
            validate_draft(Product.from_dict(original), candidate)

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

"""Офлайн-тесты промтов и разбора ответов ИИ, без сети."""
import json

from conftest import QUIZ, run  # noqa: F401


def test_parse_json_plain(make_bot):
    bot, *_ = make_bot
    assert bot.parse_json_loose('{"a": 1}') == {"a": 1}


def test_parse_json_fenced(make_bot):
    """Модель завернула JSON в ```json ... ``` — парсер вытаскивает."""
    bot, *_ = make_bot
    txt = "```json\n{\"topic\": \"Дроби\"}\n```"
    assert bot.parse_json_loose(txt) == {"topic": "Дроби"}


def test_parse_json_with_chatter(make_bot):
    """Модель добавила слова до/после JSON — берём блок между первой и последней скобкой."""
    bot, *_ = make_bot
    txt = 'Вот тест: {"topic": "Смутное время"} Надеюсь, поможет!'
    assert bot.parse_json_loose(txt) == {"topic": "Смутное время"}


def test_parse_json_garbage_raises(make_bot):
    bot, *_ = make_bot
    try:
        bot.parse_json_loose("никакого JSON тут нет")
        assert False, "должно было упасть"
    except ValueError:
        pass


def test_normalize_quiz_good(make_bot):
    bot, *_ = make_bot
    quiz = bot.normalize_quiz(json.loads(json.dumps(QUIZ)))
    assert quiz["topic"] == "Дроби" and len(quiz["questions"]) == 5
    for q in quiz["questions"]:
        assert len(q["options"]) == 3 and 0 <= q["correct"] < 3 and q["subtopic"]


def test_normalize_quiz_fixes_broken_questions(make_bot):
    """Модель вернула вопрос с 4 вариантами и вопрос без темы — плохие отбрасываются,
    пустая подтема подменяется темой."""
    bot, *_ = make_bot
    raw = {"topic": "Дроби", "questions": [
        {"q": "Хороший вопрос?", "options": ["а", "б", "в"], "correct": 1, "subtopic": ""},
        {"q": "Ещё хороший?", "options": ["а", "б", "в"], "correct": 0, "subtopic": "числитель"},
        {"q": "И ещё?", "options": ["а", "б", "в"], "correct": 2, "subtopic": "знаменатель"},
        {"q": "Сломанный", "options": ["а", "б", "в", "г"], "correct": 0, "subtopic": "х"},
        {"q": "Без correct", "options": ["а", "б", "в"], "correct": None, "subtopic": "х"},
    ]}
    quiz = bot.normalize_quiz(raw)
    assert len(quiz["questions"]) == 3
    assert quiz["questions"][0]["subtopic"] == "Дроби"  # пустая подтема подменена темой


def test_normalize_quiz_rejects_junk(make_bot):
    bot, *_ = make_bot
    for junk in ([], {"questions": []}, {"questions": [{"q": "в", "options": [], "correct": 0}]}):
        try:
            bot.normalize_quiz(junk)
            assert False, f"должно было упасть: {junk}"
        except (ValueError, AttributeError, TypeError):
            pass


def test_sensible_input(make_bot):
    bot, *_ = make_bot
    assert bot.is_sensible_input("Дроби, 6 класс")
    assert bot.is_sensible_input("Смутное время")
    assert not bot.is_sensible_input("ааа")
    assert not bot.is_sensible_input("123")
    assert not bot.is_sensible_input("?!-")
    assert not bot.is_sensible_input("  ")
    assert bot.is_sensible_input("ыыы 123 много букв")   # смешанный — пропускаем к ИИ


def test_prompts_json_loads(make_bot):
    """Промты — валидный JSON, схема ответа описана в каждом промте."""
    bot, *_ = make_bot
    assert set(bot.PROMPTS) == {"quiz", "explain", "followup"}
    assert '{"topic"' in bot.PROMPTS["quiz"] and '"questions"' in bot.PROMPTS["quiz"]
    assert '"explanation"' in bot.PROMPTS["explain"]
    for key in ("class", "subtopic", "topic"):
        assert "{" + key + "}" in bot.PROMPTS["explain"]
        assert "{" + key + "}" in bot.PROMPTS["followup"]


def test_fill_template(make_bot):
    bot, *_ = make_bot
    filled = bot.fill("А{class}Б{subtopic}В{x}", **{"class": 7, "subtopic": "дроби", "x": 1})
    assert filled == "А7БдробиВ1"

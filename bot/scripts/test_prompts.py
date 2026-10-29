"""Живая проверка промтов на реальном ИИ (пункт 3 ТЗ).

Запускается, когда есть ключ ИИ (.env):
    python scripts/test_prompts.py          # 10 запусков quiz на 3 вводах
    python scripts/test_prompts.py --all    # + explain и followup

Критерий из ТЗ: 10 из 10 запусков дают валидный JSON, вопросы по тексту,
минимум 2 вопроса на понимание (считаем по промту: ИИ сам размечает, мы проверяем
формат и количество; содержательный контроль — глазами по выводу).
"""
import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# подхватываем .env, если запущено без Docker
env_file = Path(__file__).resolve().parent.parent / ".env"
if env_file.exists():
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

import bot  # noqa: E402

# Три ввода из ТЗ: название темы, текст параграфа (Википедия), короткая тема
INPUTS = [
    ("название темы", "Дроби, 6 класс"),
    ("текст параграфа",
     "Смутное время — период русской истории с 1598 по 1613 год, ознаменовавшийся "
     "стихийными бедствиями, гражданской войной, польско-шведской интервенцией и "
     "утратой государственной независимости. Династический кризис начался после "
     "смерти царя Фёдора Ивановича, не оставившего наследника. В 1605 году Лжедмитрий I "
     "при поддержке польских магнатов занял московский престол, но был убит боярами. "
     "Затем правил Василий Шуйский, свергнутый в 1610 году. В 1612 году Второе "
     "народное ополчение под предводительством Кузьмы Минина и Дмитрия Пожарского "
     "освободило Москву от польских интервентов. В 1613 году Земский собор избрал "
     "царём Михаила Фёдоровича Романова, что положило начало новой династии и концу Смуты."),
    ("короткая тема", "Смутное время"),
]

RUNS = 10


async def check_quiz():
    ok = 0
    for i in range(RUNS):
        label, text = INPUTS[i % len(INPUTS)]
        try:
            quiz = await bot.ai_json(bot.PROMPTS["quiz"], f"Класс: 7\nВвод ученика: {text}")
            quiz = bot.normalize_quiz(quiz)
            n_understand = 0
            for q in quiz["questions"]:
                ql = q["q"].lower()
                if any(w in ql for w in ("почему", "что из этого", "какой вывод", "зачем", "следует")):
                    n_understand += 1
            print(f"[{i+1}/{RUNS}] {label}: OK · тема «{quiz['topic']}», "
                  f"{len(quiz['questions'])} вопросов, на понимание ~{n_understand}")
            print("   " + quiz["questions"][0]["q"])
            ok += 1
        except Exception as e:
            print(f"[{i+1}/{RUNS}] {label}: FAIL — {e}")
    return ok


async def check_rest():
    res = await bot.ai_json(bot.fill(bot.PROMPTS["explain"], **{"class": 7, "subtopic": "итоги Смуты", "topic": "Смутное время"}),
                            "Способ: simple\nКонтекст: нет", 0.8)
    print("explain:", "OK" if res.get("explanation") else "FAIL", "—", str(res.get("explanation"))[:80])
    res = await bot.ai_json(bot.fill(bot.PROMPTS["followup"], **{"class": 7, "subtopic": "итоги Смуты",
                                                                 "topic": "Смутное время", "previous_questions": "—"}),
                            "Сгенерируй.")
    qs = bot.normalize_questions(res.get("questions"), 2)
    print("followup:", "OK" if len(qs) >= 2 else "FAIL", "—", qs[0]["q"] if qs else "")


async def main():
    if not bot.AI_KEY:
        print("Нет AI_API_KEY — живая проверка невозможна. Заполните .env и повторите.")
        raise SystemExit(1)
    ok = await check_quiz()
    if "--all" in sys.argv:
        await check_rest()
    print(f"\nИтог: {ok}/{RUNS} валидных ответов quiz")
    raise SystemExit(0 if ok == RUNS else 2)


if __name__ == "__main__":
    asyncio.run(main())

"""Тесты «Грызлика»: сценарий целиком, уровни, серия дней, сбои ИИ."""
import asyncio
import datetime as dt

from conftest import upd_start, upd_msg, upd_cb, run

# в QUIZ из conftest правильные ответы: [0, 1, 0, 0, 0]
# ответы ученика "ans:0" на все вопросы дают 4/5 (ошибся в q2 «сложение дробей»)


# ---------- полный сценарий: тема → тест → диагностика → объяснение → добивающие ----------
def test_full_scenario(make_bot):
    bot, g, api, ai = make_bot

    async def scenario():
        await g.handle(upd_start())                      # «Начать» → спрашиваем класс
        await g.handle(upd_msg("7", mid=1))              # вводим класс текстом
        await g.handle(upd_cb("feed", cb_id=2))          # кнопка «Покормить»
        await g.handle(upd_msg("Дроби, 6 класс", mid=2)) # тема

        for i in range(5):                               # 5 ответов «ans:0» → 4 верных
            await g.handle(upd_cb("ans:0", cb_id=10 + i))

        u = bot.get_user(1)
        assert u["state"]["mode"] == "after"
        assert api.has_text("Итог: 4/5")
        assert api.has_text("Западает: сложение дробей")
        assert api.has_text("Знаешь: знаменатель")

        await g.handle(upd_cb("explain:0", cb_id=20))     # разбираем слабую подтему
        assert api.has_text("Как объяснить")
        await g.handle(upd_cb("mode:simple", cb_id=21))   # «Проще»
        assert api.has_text("Проверь себя")
        await g.handle(upd_cb("followup", cb_id=22))      # «Понял, проверь меня»

        # 2 добивающих вопроса, оба верно (correct=0 у обоих)
        await g.handle(upd_cb("ans:0", cb_id=23))
        await g.handle(upd_cb("ans:0", cb_id=24))
        return bot.get_user(1)

    u = run(scenario())
    assert u["xp"] == 4 * 10 + 30                        # 4 верных * 10 + 30 за закрытие
    assert u["weak"] == []                               # слабая подтема удалена
    assert u["closed"][0]["subtopic"] == "сложение дробей"
    assert api.has_text("Тема «сложение дробей» закрыта")
    assert api.has_text("Грызлик объелся")
    assert len(api.answers) == 11                        # каждое нажатие отвечено /answers


def test_full_scenario_followup_not_closed(make_bot):
    """Если добивающие отвечены неверно — тема остаётся в слабых, +5 опыта."""
    bot, g, api, ai = make_bot

    async def scenario():
        await g.handle(upd_start())
        await g.handle(upd_msg("7", mid=1))
        await g.handle(upd_cb("feed", cb_id=2))
        await g.handle(upd_msg("Дроби", mid=2))
        for i in range(5):
            await g.handle(upd_cb("ans:0", cb_id=10 + i))
        await g.handle(upd_cb("explain:0", cb_id=20))
        await g.handle(upd_cb("mode:example", cb_id=21))
        await g.handle(upd_cb("followup", cb_id=22))
        await g.handle(upd_cb("ans:1", cb_id=23))        # оба неверно
        await g.handle(upd_cb("ans:1", cb_id=24))
        return bot.get_user(1)

    u = run(scenario())
    assert u["xp"] == 4 * 10 + 5
    assert any(w["subtopic"] == "сложение дробей" for w in u["weak"])
    assert u["closed"] == []
    assert api.has_text("Почти! +5 опыта")


# ---------- идемпотентность: повторный апдейт не начисляет опыт дважды ----------
def test_duplicate_update_no_double_xp(make_bot):
    bot, g, api, ai = make_bot

    async def scenario():
        await g.handle(upd_start())
        await g.handle(upd_cb("class:7", cb_id=1))
        await g.handle(upd_msg("Дроби", mid=2))
        for i in range(4):
            await g.handle(upd_cb("ans:0", cb_id=10 + i))
        # последний ответ «доставляется повторно» — тот же callback_id и mid
        dup = upd_cb("ans:0", cb_id=14)
        await g.handle(dup)
        await g.handle(dup)
        return bot.get_user(1)

    u = run(scenario())
    assert u["xp"] == 4 * 10                             # ровно один раз
    assert api.count_text("Итог: 4/5") == 1


# ---------- устойчивость: сбой ИИ не роняет бота ----------
def test_ai_failure_recovers(make_bot):
    bot, g, api, ai = make_bot
    ai.fail = True

    async def scenario():
        await g.handle(upd_start())
        await g.handle(upd_cb("class:7", cb_id=1))
        await g.handle(upd_msg("Дроби", mid=1))          # ИИ «лежит»
        return bot.get_user(1)

    u = run(scenario())
    assert api.has_text("подавился")
    # кнопка «Попробовать ещё раз» пришла вместе с сообщением об ошибке
    assert any("Попробовать ещё раз" in b[0] for row in api.last()["buttons"] for b in row)
    assert u["state"]["mode"] == "feed_input"

    ai.fail = False                                      # ИИ «поднялся»

    async def retry():
        await g.handle(upd_cb("retry_gen", cb_id=5))     # кнопка «Попробовать ещё раз»

    run(retry())
    assert api.has_text("Вопрос 1 из 5")


def test_nonsense_input_asked_to_clarify(make_bot):
    bot, g, api, ai = make_bot

    async def scenario():
        await g.handle(upd_start())
        await g.handle(upd_cb("class:7", cb_id=1))
        await g.handle(upd_msg("ааа", mid=1))
        await g.handle(upd_msg("123", mid=2))
        await g.handle(upd_msg("??!", mid=3))

    run(scenario())
    assert api.count_text("Не понял, что покормить") == 3
    assert ai.calls == []                                # ИИ не дёргали зря


def test_long_input_truncated(make_bot):
    bot, g, api, ai = make_bot
    paragraph = ("Дроби в математике — это числа, показывающие части целого. " * 150)  # > 6000 символов

    async def scenario():
        await g.handle(upd_start())
        await g.handle(upd_cb("class:7", cb_id=1))
        await g.handle(upd_msg(paragraph, mid=1))

    run(scenario())
    assert api.has_text("длинноват")                     # предупреждение
    assert len(ai.calls[0]["user"].split("Ввод ученика: ")[1]) == 6000


# ---------- опыт и уровни ----------
def test_level_thresholds(make_bot):
    bot, g, api, ai = make_bot
    bot.get_user(1, 100)
    for xp, expected in [(0, 1), (149, 1), (150, 2), (449, 2),
                         (450, 3), (900, 4), (1799, 4), (1800, 5)]:
        u = bot.get_user(1)
        u["xp"], u["level"] = xp, 1
        bot.save_user(u)
        u = bot.get_user(1)
        grew = bot.feed(u, 0)
        assert u["level"] == expected, f"xp={xp}: уровень {u['level']} != {expected}"
        assert grew == (expected > 1)


def test_level_up_message_and_image(make_bot):
    bot, g, api, ai = make_bot

    async def scenario():
        await g.handle(upd_start())
        u = bot.get_user(1)
        u["xp"], u["level"] = 145, 1                     # до «Малыша» не хватает 5
        bot.save_user(u)
        await g.handle(upd_cb("class:7", cb_id=1))
        await g.handle(upd_msg("Дроби", mid=1))
        for i in range(5):                               # +40 опыта → уровень 2
            await g.handle(upd_cb("ans:0", cb_id=10 + i))
        return bot.get_user(1)

    u = run(scenario())
    assert u["level"] == 2
    assert api.has_text("Я вырос! Теперь я Малыш")
    with_image = [s for s in api.sent if s["image"]]
    assert with_image and with_image[-1]["image"].endswith("pet_2.png")


# ---------- серия дней ----------
def test_streak(make_bot):
    bot, g, api, ai = make_bot

    def feed_today():
        u = bot.get_user(1)
        bot.feed(u, 10)                                  # feed синхронный, сохраняем отдельно
        bot.save_user(u)
        return bot.get_user(1)

    d = dt.date(2026, 10, 1)
    bot.get_user(1, 100)
    bot.today = lambda: d
    assert feed_today()["streak"] == 1                   # первая кормёжка

    bot.today = lambda: d + dt.timedelta(days=1)
    assert feed_today()["streak"] == 2                   # кормили вчера: серия растёт

    bot.today = lambda: d + dt.timedelta(days=3)         # день пропущен
    assert feed_today()["streak"] == 1                   # серия началась заново

    bot.today = lambda: d + dt.timedelta(days=3)         # повторная кормёжка в тот же день
    assert feed_today()["streak"] == 1                   # серия не растёт дважды за день


# ---------- ежедневное снижение сытости и потеря уровня ----------
def test_daily_decay(make_bot):
    bot, g, api, ai = make_bot
    today = dt.date(2026, 10, 10)
    bot.today = lambda: today

    # кормили вчера: сытость −30, уровень не теряется
    u = bot.get_user(1, 100)
    u.update({"level": 3, "xp": 500, "hunger": 100, "streak": 4,
              "last_feed": (today - dt.timedelta(days=1)).isoformat()})
    bot.save_user(u)
    u = bot.get_user(1)
    assert bot.daily_decay(u) is None                    # daily_decay меняет словарь
    bot.save_user(u)                                     # сохранение — за вызывающим кодом
    u = bot.get_user(1)
    assert u["hunger"] == 70 and u["level"] == 3

    # не кормили 3 дня: минус уровень, опыт на порог, серия сгорела
    u.update({"last_feed": (today - dt.timedelta(days=3)).isoformat(), "hunger": 100,
              "level": 3, "xp": 500, "streak": 4})
    bot.save_user(u)
    u = bot.get_user(1)
    msg = bot.daily_decay(u)
    bot.save_user(u)
    u = bot.get_user(1)
    assert u["level"] == 2 and u["xp"] == 150 and u["streak"] == 0  # xp = порог уровня 2
    assert "уменьшился" in msg

    # уровень 1 ниже не падает
    u.update({"level": 1, "xp": 0, "last_feed": (today - dt.timedelta(days=5)).isoformat()})
    bot.save_user(u)
    u = bot.get_user(1)
    bot.daily_decay(u)
    bot.save_user(u)
    assert bot.get_user(1)["level"] == 1


def test_hunger_reminder_message(make_bot):
    """Голодный питомец получает текст напоминания про кормёж."""
    bot, g, api, ai = make_bot
    today = dt.date(2026, 10, 10)
    bot.today = lambda: today
    u = bot.get_user(1, 100)
    u.update({"class": 7, "level": 2, "xp": 150, "hunger": 60, "streak": 2,
              "last_feed": (today - dt.timedelta(days=2)).isoformat()})
    bot.save_user(u)
    u = bot.get_user(1)
    msg = bot.daily_decay(u)
    bot.save_user(u)
    assert msg and "Покорми" in msg
    assert bot.get_user(1)["hunger"] == 30


# ---------- повтор слабых тем через 3 и 7 дней ----------
def test_repeat_candidate_3_and_7_days(make_bot):
    bot, g, api, ai = make_bot
    today = dt.date(2026, 10, 10)
    bot.today = lambda: today
    u = bot.get_user(1, 100)
    u["weak"] = [
        {"topic": "Дроби", "subtopic": "сложение дробей",
         "added": (today - dt.timedelta(days=3)).isoformat(), "r": 0},
    ]
    bot.save_user(u)

    w = bot.repeat_candidate(bot.get_user(1))            # 3 дня → первое предложение
    assert w and w["subtopic"] == "сложение дробей"
    u = bot.get_user(1)
    u["weak"][0]["r"] = 1                                # уже предлагали один раз
    bot.save_user(u)

    bot.today = lambda: today + dt.timedelta(days=2)     # прошло 5 дней — рано для второго
    assert bot.repeat_candidate(bot.get_user(1)) is None
    bot.today = lambda: today + dt.timedelta(days=4)     # 7 дней → второе предложение
    w = bot.repeat_candidate(bot.get_user(1))
    assert w and w["subtopic"] == "сложение дробей"
    u = bot.get_user(1)
    u["weak"][0]["r"] = 2
    bot.save_user(u)
    assert bot.repeat_candidate(bot.get_user(1)) is None # больше не предлагаем


# ---------- прогресс /stats ----------
def test_stats(make_bot):
    bot, g, api, ai = make_bot

    async def scenario():
        await g.handle(upd_start())
        u = bot.get_user(1)
        u.update({"class": 7, "xp": 200, "level": 2, "streak": 3, "hunger": 90})
        u["closed"] = [{"topic": "Дроби", "subtopic": "сложение дробей", "date": "2026-10-01"}]
        bot.save_user(u)
        await g.handle(upd_msg("/stats", mid=1))         # командой
        await g.handle(upd_cb("stats", cb_id=2))         # кнопкой из меню
        await g.handle(upd_msg("Смутное время", mid=2))  # и после этого бот жив

    run(scenario())
    assert api.count_text("📈 Мой прогресс") == 2
    assert api.has_text("До «Подросток»: 250 опыта")     # 450 − 200
    assert api.has_text("Серия: 3 дн. подряд")
    assert api.has_text("Дроби / сложение дробей")


# ---------- клавиатура исходного сообщения убирается после нажатия ----------
def test_callback_strips_keyboard(make_bot):
    bot, g, api, ai = make_bot

    async def scenario():
        await g.handle(upd_start())
        await g.handle(upd_cb("class:7", cb_id=1))

    run(scenario())
    assert api.answers
    edit = api.answers[0]["edit_message"]
    assert edit is not None and edit["attachments"] == []  # кнопки сняты


# ---------- приветствие с маскотом, меню — с питомцем ----------
def test_greeting_mascot(make_bot):
    bot, g, api, ai = make_bot

    async def scenario():
        await g.handle(upd_start())                      # новый ученик → опрос класса
        await g.handle(upd_msg("7", mid=1))              # класс установлен
        await g.handle(upd_msg("/start", mid=2))         # повторный вход → меню

    run(scenario())
    assert api.sent[0]["image"] and api.sent[0]["image"].endswith("mascot.png")
    assert any(s["image"] and s["image"].endswith("pet_1.png") for s in api.sent)


# ---------- сбой отправки одному ученику не мешает остальным ----------
def test_reminder_loop_isolated_failure(make_bot):
    bot, g, api, ai = make_bot

    class FlakyMax(type(api)):
        fail_chat = 100                                  # отправка в этот чат падает

        async def send(self, chat_id, text, buttons=None, image_path=None):
            if chat_id == self.fail_chat:
                raise RuntimeError("сеть отвалилась (тест)")
            await super().send(chat_id, text, buttons, image_path)

    async def scenario():
        for uid, chat in [(1, 100), (2, 200)]:
            u = bot.get_user(uid, chat)
            u.update({"class": 7, "last_feed": "2020-01-01", "hunger": 10, "level": 2})
            bot.save_user(u)
        g.max = FlakyMax()
        g.max.sent = []
        bot.REMIND_HOUR = 0                              # час настал всегда
        task = asyncio.create_task(g.reminders())
        await asyncio.sleep(0.5)                         # один проход цикла
        task.cancel()
        return g.max

    flaky = run(scenario())
    assert any("Покорми" in s["text"] for s in flaky.sent)   # второй ученик получил

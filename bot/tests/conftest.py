"""Общие фикстуры: временная база, мок MAX API, мок ИИ, конструкторы апдейтов."""
import asyncio
import copy
import importlib
import json
import sys
from pathlib import Path

import pytest

BOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BOT_DIR))


class FakeMax:
    """Мок MAX API: ничего не отправляет в сеть, всё запоминает."""

    def __init__(self):
        self.sent = []       # [{'chat_id', 'text', 'buttons', 'image'}]
        self.answers = []    # ответы на нажатия кнопок (POST /answers)

    async def send(self, chat_id, text, buttons=None, image_path=None):
        self.sent.append({"chat_id": chat_id, "text": text or "",
                          "buttons": buttons or [], "image": image_path})

    async def answer_callback(self, callback_id, text=None, edit_message=None):
        self.answers.append({"callback_id": callback_id, "notification": text,
                             "edit_message": edit_message})

    async def get_me(self):
        return {"user_id": 1, "name": "Грызлик (тест)"}

    async def close(self):
        pass

    def texts(self):
        return [s["text"] for s in self.sent]

    def last(self):
        return self.sent[-1] if self.sent else None

    def has_text(self, fragment):
        return any(fragment in t for t in self.texts())

    def count_text(self, fragment):
        """Сколько сообщений содержат фрагмент."""
        return sum(1 for t in self.texts() if fragment in t)


def run(coro):
    return asyncio.run(coro)


def upd_start(chat_id=100, user_id=1):
    """Апдейт bot_started: пользователь нажал «Начать»."""
    return {"update_type": "bot_started", "chat_id": chat_id,
            "user": {"user_id": user_id, "first_name": "Тест"},
            "user_locale": "ru", "timestamp": 1000}


def upd_msg(text, chat_id=100, user_id=1, mid=1):
    """Апдейт message_created: текстовое сообщение от ученика."""
    return {"update_type": "message_created",
            "message": {"sender": {"user_id": user_id},
                        "recipient": {"chat_id": chat_id, "chat_type": "dialog", "user_id": user_id},
                        "body": {"mid": str(mid), "seq": mid, "text": text},
                        "timestamp": 2000 + mid},
            "user_locale": "ru"}


def upd_cb(payload, chat_id=100, user_id=1, cb_id=1, mid=50):
    """Апдейт message_callback: нажатие inline-кнопки."""
    return {"update_type": "message_callback",
            "callback": {"callback_id": str(cb_id), "timestamp": 3000 + cb_id,
                         "user": {"user_id": user_id}, "payload": payload},
            "message": {"recipient": {"chat_id": chat_id, "chat_type": "dialog"},
                        "body": {"mid": str(mid), "text": "вопрос"},
                        "timestamp": 2500},
            "user_locale": "ru"}


QUIZ = {  # эталонный ответ ИИ по промту quiz: 5 вопросов, 3 варианта
    "topic": "Дроби",
    "questions": [
        {"q": "Что показывает знаменатель дроби?", "options": ["На сколько частей делят", "Сколько частей взяли", "Сумму частей"],
         "correct": 0, "subtopic": "знаменатель"},
        {"q": "Чему равна 1/2 + 1/4?", "options": ["2/6", "3/4", "1/6"],
         "correct": 1, "subtopic": "сложение дробей"},
        {"q": "Какая дробь больше: 2/3 или 3/5?", "options": ["2/3", "3/5", "равны"],
         "correct": 0, "subtopic": "сравнение дробей"},
        {"q": "Что такое числитель?", "options": ["Верхняя часть", "Нижняя часть", "Черта"],
         "correct": 0, "subtopic": "числитель"},
        {"q": "Сократи 4/8", "options": ["1/2", "2/3", "4/4"],
         "correct": 0, "subtopic": "сокращение"},
    ],
}

EXPLAIN = {"explanation": "Дробь — как пицца: знаменатель говорит, на сколько кусков разрезали.",
           "check_question": "Что показывает знаменатель?"}

FOLLOWUP = {"questions": [
    {"q": "На сколько частей делит знаменатель 7?", "options": ["7", "5", "10"], "correct": 0},
    {"q": "У Маши 3/4 шоколадки, у Пети 1/4. Сколько всего?", "options": ["1", "3/8", "1/2"], "correct": 0},
]}


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    """Перезагружаем bot с чистой временной базой."""
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    import bot
    importlib.reload(bot)
    return bot


@pytest.fixture
def make_bot(fresh_db):
    """Grizlik с фейковым MAX и переключаемым фейковым ИИ. yield (bot, g, api, ai)."""
    bot = fresh_db
    api = FakeMax()

    class FakeAI:
        """По умолчанию — корректный JSON; .fail=True имитирует сбой ИИ."""
        fail = False
        calls = []

        async def __call__(self, system, user, temperature=0.5, attempts=3):
            self.calls.append({"system": system, "user": user, "temperature": temperature})
            if self.fail:
                raise bot.AiError("ИИ недоступен (тест)")
            if "объясняющий учитель" in system:
                return copy.deepcopy(EXPLAIN)
            if "ровно 2" in system or "Не повторяй" in system:
                return copy.deepcopy(FOLLOWUP)
            return copy.deepcopy(QUIZ)

    ai = FakeAI()
    orig = bot.ai_json
    bot.ai_json = ai
    g = bot.Grizlik(api=api)
    yield bot, g, api, ai
    bot.ai_json = orig

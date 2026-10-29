"""Грызлик — питомец, который питается знаниями.

Чат-бот для MAX (Bot API, long polling) + любой OpenAI-совместимый ИИ.
Запуск: MAX_BOT_TOKEN=... AI_API_KEY=... python bot.py  (или docker compose up)
"""
import asyncio, json, logging, os, random, re, sqlite3, ssl, time
import datetime as dt
from pathlib import Path

import httpx

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("grizlik")

# ---------- настройки ----------
# TODO: вынести лимиты и дефолты в конфиг
MAX_TOKEN = os.getenv("MAX_BOT_TOKEN", "").strip()
MAX_API = os.getenv("MAX_API_BASE", "https://platform-api2.max.ru")
AI_KEY = os.getenv("AI_API_KEY", "").strip()
AI_BASE = os.getenv("AI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
AI_MODEL = os.getenv("AI_MODEL", "gpt-4o-mini")
AI_JSON_MODE = os.getenv("AI_JSON_MODE", "0") == "1"  # response_format умеют не все провайдеры
REMIND_HOUR = int(os.getenv("REMIND_HOUR", "18"))
TZ = dt.timezone(dt.timedelta(hours=int(os.getenv("TZ_OFFSET", "3"))))  # Москва = 3

MAX_TEXT_LEN = 4000     # жёсткий лимит текста сообщения в MAX
SEND_TEXT_LIMIT = 3900  # режем с запасом
MAX_INPUT_LEN = 6000    # наш лимит на параграф от ученика
UPDATE_TYPES = "bot_started,message_created,message_callback"

BOT_DIR = Path(__file__).parent
ROOT_CA = BOT_DIR / "certs" / "russian_trusted_root_ca.pem"
PROMPTS = json.loads((BOT_DIR / "prompts.json").read_text(encoding="utf-8"))


def db_path() -> Path:
    # из окружения — чтобы тесты подменяли базу
    return Path(os.getenv("DB_PATH", str(BOT_DIR / "data" / "grizlik.db")))


def fill(template, **kw):
    # {name} меняем сами: в промтах полно фигурных скобок от JSON, str.format тут ломается
    for k, v in kw.items():
        template = template.replace("{" + k + "}", str(v))
    return template


LEVELS = [(1, "Яйцо", 0), (2, "Малыш", 150), (3, "Подросток", 450), (4, "Взрослый", 900), (5, "Мудрец", 1800)]
PHRASES = {
    "ok": ["Ням! Вкусно!", "Хрум-хрум, ещё!", "Это было сочно!", "Мне нравится, как ты думаешь!"],
    "bad": ["Ммм, вкусная ошибка! Разберём.", "Не страшно — ошибки тоже питательные.", "Чуть-чуть мимо. Сейчас поймём."],
}

# ---------- база ----------
def db():
    p = db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(p)
    c.row_factory = sqlite3.Row
    c.execute("""CREATE TABLE IF NOT EXISTS users(
        user_id INTEGER PRIMARY KEY, chat_id INTEGER, class INTEGER DEFAULT 0,
        xp INTEGER DEFAULT 0, level INTEGER DEFAULT 1, hunger INTEGER DEFAULT 100,
        streak INTEGER DEFAULT 0, last_feed TEXT DEFAULT '', weak TEXT DEFAULT '[]',
        state TEXT DEFAULT '{}', last_remind TEXT DEFAULT '', closed TEXT DEFAULT '[]')""")
    c.execute("CREATE TABLE IF NOT EXISTS seen(k TEXT PRIMARY KEY, ts REAL)")
    c.execute("CREATE TABLE IF NOT EXISTS uploads(path TEXT PRIMARY KEY, token TEXT, ts REAL)")
    # базы, созданные старой версией бота
    cols = {r["name"] for r in c.execute("PRAGMA table_info(users)")}
    if "closed" not in cols:
        c.execute("ALTER TABLE users ADD COLUMN closed TEXT DEFAULT '[]'")
    return c


def get_user(uid, chat_id=None):
    with db() as c:
        row = c.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()
        if not row:
            c.execute("INSERT INTO users(user_id, chat_id) VALUES(?,?)", (uid, chat_id))
            c.commit()
            row = c.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()
        elif chat_id and row["chat_id"] != chat_id:
            c.execute("UPDATE users SET chat_id=? WHERE user_id=?", (chat_id, uid))
            c.commit()
    u = dict(row)
    for f in ("weak", "state", "closed"):
        try:
            u[f] = json.loads(u[f])
        except Exception:
            u[f] = []
    return u


def save_user(u):
    with db() as c:
        c.execute("""UPDATE users SET chat_id=?, class=?, xp=?, level=?, hunger=?, streak=?, last_feed=?, weak=?, state=?, last_remind=?, closed=?
                     WHERE user_id=?""",
                  (u["chat_id"], u["class"], u["xp"], u["level"], u["hunger"], u["streak"], u["last_feed"],
                   json.dumps(u["weak"], ensure_ascii=False), json.dumps(u["state"], ensure_ascii=False),
                   u["last_remind"], json.dumps(u["closed"], ensure_ascii=False), u["user_id"]))
        c.commit()


def is_seen(key):
    # INSERT OR IGNORE атомарен: второй раз тот же ключ не вставится
    with db() as c:
        cur = c.execute("INSERT OR IGNORE INTO seen(k, ts) VALUES(?,?)", (key, time.time()))
        c.commit()
        return cur.rowcount == 0


def cleanup_seen(days=7):
    with db() as c:
        c.execute("DELETE FROM seen WHERE ts < ?", (time.time() - days * 86400,))
        c.commit()


def get_upload_token(path):
    p = str(Path(path).resolve())
    with db() as c:
        row = c.execute("SELECT token FROM uploads WHERE path=?", (p,)).fetchone()
    return row["token"] if row else None


def set_upload_token(path, token):
    p = str(Path(path).resolve())
    with db() as c:
        c.execute("INSERT OR REPLACE INTO uploads(path, token, ts) VALUES(?,?,?)", (p, token, time.time()))
        c.commit()

# ---------- SSL ----------
def max_ssl_context():
    # platform-api2.max.ru подписан корневым сертификатом Минцифры — добавляем его
    # к системным, проверку сертификатов не отключаем
    ctx = ssl.create_default_context()
    ca = Path(os.getenv("MAX_ROOT_CA_PATH", "")) if os.getenv("MAX_ROOT_CA_PATH") else ROOT_CA
    if ca.exists():
        ctx.load_verify_locations(cafile=str(ROOT_CA))
    else:
        log.warning("Файл %s не найден — если запросы к MAX упадут с SSL-ошибкой, "
                    "положите корневой сертификат Минцифры в certs/ или задайте MAX_ROOT_CA_PATH", ca)
    return ctx

# ---------- MAX API ----------
class Max:
    def __init__(self):
        self.h = httpx.AsyncClient(
            base_url=MAX_API,
            headers={"Authorization": MAX_TOKEN},  # без Bearer
            timeout=40,
            verify=max_ssl_context(),
        )

    async def close(self):
        await self.h.aclose()

    def _keyboard(self, buttons):
        # строка кнопок = список (текст, payload)
        rows = [[{"type": "callback", "text": str(t).strip()[:64] or "…", "payload": str(p)[:1024]}
                 for t, p in row] for row in buttons]
        return {"type": "inline_keyboard", "payload": {"buttons": rows}}

    async def send(self, chat_id, text, buttons=None, image_path=None):
        if not chat_id:
            log.error("send: нет chat_id, сообщение не отправлено")
            return
        attachments = []
        if image_path and Path(image_path).exists():
            # картинку грузим один раз, токен живёт в БД
            tok = get_upload_token(image_path) or await self.upload_image(image_path)
            if tok:
                attachments.append({"type": "image", "payload": {"token": tok}})
        if buttons:
            attachments.append(self._keyboard(buttons))
        text = text or ""
        parts, rest = [], text
        while len(rest) > SEND_TEXT_LIMIT:
            cut = max(rest.rfind("\n", 0, SEND_TEXT_LIMIT), rest.rfind(" ", 0, SEND_TEXT_LIMIT))
            if cut < SEND_TEXT_LIMIT // 2:
                cut = SEND_TEXT_LIMIT
            parts.append(rest[:cut].rstrip())
            rest = rest[cut:].lstrip()
        parts.append(rest)
        for i, part in enumerate(parts):
            body = {"text": part}
            if i == 0 and attachments:
                body["attachments"] = attachments
            await self._post_message(chat_id, body)

    async def _post_message(self, chat_id, body):
        for attempt in range(3):
            try:
                r = await self.h.post("/messages", params={"chat_id": chat_id}, json=body)
                if r.status_code == 400:
                    try:
                        # attachment.not.ready: картинка после upload готовится асинхронно
                        if r.json().get("code") == "attachment.not.ready" and attempt < 2:
                            await asyncio.sleep(1 + attempt)
                            continue
                    except Exception:
                        pass
                if r.status_code >= 400:
                    log.error("send %s %s", r.status_code, r.text[:300])
                return
            except Exception as e:
                log.error("send error: %s", e)
                if attempt < 2:
                    await asyncio.sleep(1 + attempt)
                return

    async def answer_callback(self, callback_id, text=None, edit_message=None):
        # MAX требует ответ на каждое нажатие кнопки; edit_message попутно снимает клавиатуру
        body = {}
        if text:
            body["notification"] = str(text)[:200]
        if edit_message is not None:
            body["message"] = edit_message
        try:
            await self.h.post("/answers", params={"callback_id": callback_id}, json=body)
        except Exception as e:
            log.error("answer_callback: %s", e)

    async def upload_image(self, path):
        for attempt in range(3):
            try:
                r = await self.h.post("/uploads", params={"type": "image"})  # type=photo в API больше нет
                r.raise_for_status()
                url = r.json()["url"]  # хост загрузки свой для каждого типа — берём из ответа
                with open(path, "rb") as f:
                    up = await httpx.AsyncClient(timeout=60).post(
                        url, files={"data": (Path(path).name, f, "image/png")})
                if up.status_code >= 400:
                    raise RuntimeError(f"upload {up.status_code} {up.text[:200]}")
                j = up.json()
                tok = j.get("token") or next(iter(j.get("photos", {}).values()), {}).get("token")
                if tok:
                    set_upload_token(path, tok)
                    return tok
                raise RuntimeError(f"в ответе загрузки нет токена: {str(j)[:200]}")
            except Exception as e:
                log.warning("upload %s: попытка %d не удалась: %s", Path(path).name, attempt + 1, e)
                if attempt == 2:
                    return None
                await asyncio.sleep(1 + attempt)

    async def get_me(self):
        r = await self.h.get("/me")
        r.raise_for_status()
        return r.json()

    async def updates(self, marker):
        params = {"limit": 100, "timeout": 30, "types": UPDATE_TYPES}
        if marker is not None:
            params["marker"] = marker
        r = await self.h.get("/updates", params=params, timeout=50)  # read-timeout > polling timeout
        r.raise_for_status()
        return r.json()

# ---------- ИИ ----------
class AiError(Exception):
    pass


def parse_json_loose(txt):
    # модели любят заворачивать JSON в ``` и подкидывать текст вокруг
    t = txt.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t, flags=re.M).strip()
    try:
        return json.loads(t)
    except Exception:
        a, b = t.find("{"), t.rfind("}")
        if a >= 0 and b > a:
            return json.loads(t[a:b + 1])
        raise ValueError(f"ИИ вернул не JSON: {t[:120]!r}")


async def ai_json(system, user, temperature=0.5, attempts=3):
    if not AI_KEY:
        raise AiError("AI_API_KEY не задан")
    last = None
    async with httpx.AsyncClient(timeout=90) as c:
        for attempt in range(attempts):
            try:
                body = {"model": AI_MODEL, "temperature": temperature,
                        "messages": [{"role": "system", "content": system},
                                     {"role": "user", "content": user}]}
                if AI_JSON_MODE:
                    body["response_format"] = {"type": "json_object"}
                r = await c.post(f"{AI_BASE}/chat/completions",
                                 headers={"Authorization": f"Bearer {AI_KEY}"}, json=body)
                if r.status_code == 429 or r.status_code >= 500:
                    raise AiError(f"ИИ вернул {r.status_code}")
                r.raise_for_status()
                txt = r.json()["choices"][0]["message"]["content"]
                return parse_json_loose(txt)
            except Exception as e:
                last = e
                log.warning("ai_json попытка %d/%d не удалась: %s", attempt + 1, attempts, e)
                if attempt < attempts - 1:
                    await asyncio.sleep(1.5 * (attempt + 1))
    raise AiError(f"ИИ недоступен или отвечает не-JSON: {last}")


def normalize_questions(qs, need_min):
    # у модели бывает 4 варианта или wrong correct — плохие вопросы выбрасываем
    out = []
    if not isinstance(qs, list):
        raise ValueError("questions — не список")
    for q in qs:
        if not isinstance(q, dict):
            continue
        qt = str(q.get("q", "")).strip()
        opts = [str(o).strip()[:40] for o in (q.get("options") or []) if str(o).strip()]
        try:
            corr = int(q.get("correct"))
        except (TypeError, ValueError):
            continue
        if not qt or len(opts) != 3 or not 0 <= corr < 3:
            continue
        out.append({"q": qt[:120], "options": opts, "correct": corr,
                    "subtopic": str(q.get("subtopic") or "").strip()[:30]})
    if len(out) < need_min:
        raise ValueError(f"годных вопросов {len(out)} < {need_min}")
    return out


def normalize_quiz(quiz, min_q=3, max_q=5):
    if not isinstance(quiz, dict):
        raise ValueError("quiz — не объект")
    topic = str(quiz.get("topic") or "Тема").strip()[:60] or "Тема"
    qs = normalize_questions(quiz.get("questions"), min_q)[:max_q]
    for q in qs:
        if not q["subtopic"]:
            q["subtopic"] = topic
    return {"topic": topic, "questions": qs}


def is_sensible_input(text):
    # «ааа» и «123» до ИИ не доводим
    t = text.strip()
    if len(t) < 2:
        return False
    if sum(ch.isalpha() for ch in t) < 2:
        return False
    compact = re.sub(r"[\s.,!?—-]", "", t).lower()
    if len(set(compact)) <= 1:
        return False
    return True

# ---------- питомец ----------
def level_name(lv):
    return next(n for l, n, _ in LEVELS if l == lv)


def today():
    return dt.datetime.now(TZ).date()


def how_much_left(u):
    # сколько опыта до следующего уровня; None — максимум
    if u["level"] >= 5:
        return None
    thr = next(thr for l, _, thr in LEVELS if l == u["level"] + 1)
    return max(0, thr - u["xp"])


def feed(u, gained_xp):
    t = today()
    last = dt.date.fromisoformat(u["last_feed"]) if u["last_feed"] else None
    if last == t - dt.timedelta(days=1):
        u["streak"] += 1
    elif last != t:
        u["streak"] = 1
    u["last_feed"] = t.isoformat()
    u["hunger"] = 100
    u["xp"] += gained_xp
    new = max(l for l, _, thr in LEVELS if u["xp"] >= thr)
    grew = new > u["level"]
    u["level"] = max(u["level"], new)
    return grew


def daily_decay(u):
    # раз в сутки: сытость −30, за 2+ дня без еды — минус уровень
    t = today()
    last = dt.date.fromisoformat(u["last_feed"]) if u["last_feed"] else None
    if last == t:
        return None
    u["hunger"] = max(0, u["hunger"] - 30)
    if last and last < t - dt.timedelta(days=1) and u["level"] > 1:
        u["level"] -= 1
        u["xp"] = next(thr for l, _, thr in LEVELS if l == u["level"])
        u["streak"] = 0
        return "Я немного уменьшился без еды… Покорми меня одной темой — всего 5 минут!"
    if u["hunger"] <= 40:
        return f"Я проголодался! Серия {u['streak']} дней под угрозой — один тест, и я сыт."
    return None


def repeat_candidate(u):
    # интервальное повторение: предлагаем слабое через 3 и 7 дней, максимум 2 раза
    for w in u["weak"]:
        days = (today() - dt.date.fromisoformat(w["added"])).days
        r = w.get("r", 0)
        if (days >= 3 and r == 0) or (days >= 7 and r == 1):
            return w
    return None


def pet_image(u):
    return str(BOT_DIR / f"pet_{u['level']}.png")


MASCOT = str(BOT_DIR / "mascot.png")


def home_text(u):
    weak = ", ".join(w["subtopic"] for w in u["weak"][:3]) or "пока нет"
    return (f"🐹 {level_name(u['level'])} · уровень {u['level']}\n"
            f"Сытость {u['hunger']}% · Серия {u['streak']} дн. · Опыт {u['xp']}\n"
            f"Слабые места: {weak}")


def home_buttons(u):
    rows = [[("🍽 Покормить (новая тема)", "feed")]]
    if u["weak"]:
        rows.append([("🔁 Повторить слабое", "repeat"), ("📊 Слабые места", "weak")])
    rows.append([("📈 Мой прогресс", "stats"), ("⚙️ Сменить класс", "class")])
    return rows


def stats_text(u):
    nxt = how_much_left(u)
    lvl_line = f"Уровень {u['level']} — {level_name(u['level'])}"
    if nxt is None:
        lvl_line += " (максимальный!)"
    else:
        lvl_line += f"\nДо «{level_name(u['level'] + 1)}»: {nxt} опыта"
    closed = u["closed"][-5:]
    closed_line = "\n".join(f"✅ {c['topic']} / {c['subtopic']}" for c in reversed(closed)) or "пока нет"
    return (f"📈 Мой прогресс\n\n{lvl_line}\nВсего опыта: {u['xp']}\n"
            f"Сытость: {u['hunger']}%\nСерия: {u['streak']} дн. подряд\n"
            f"Слабых мест в памяти: {len(u['weak'])}\n\nЗакрытые темы:\n{closed_line}")

# ---------- обработчики ----------
class Grizlik:
    def __init__(self, api=None):
        self.max = api or Max()
        self._locks = {}  # апдейты одного ученика — строго по очереди

    def lock(self, uid):
        if uid not in self._locks:
            self._locks[uid] = asyncio.Lock()
        return self._locks[uid]

    # --- экраны ---
    async def home(self, u):
        await self.max.send(u["chat_id"], home_text(u), home_buttons(u), pet_image(u))

    async def greeting(self, u):
        if u["class"]:
            await self.max.send(u["chat_id"], "О, привет снова! Я подрос, пока тебя не было 🐹", None, MASCOT)
            return await self.home(u)
        return await self.ask_class(u)

    async def ask_class(self, u):
        u["state"] = {"mode": "class"}
        save_user(u)
        await self.max.send(u["chat_id"], "Привет! Я Грызлик — питаюсь знаниями 🐹\nВ каком ты классе?", [
            [(str(k), f"class:{k}") for k in range(5, 9)],
            [(str(k), f"class:{k}") for k in range(9, 12)]], MASCOT)

    # --- текст от ученика ---
    async def on_text(self, u, text):
        st = u["state"]
        mode = st.get("mode")
        if mode == "class":
            digits = re.sub(r"\D", "", text)
            if digits and 1 <= int(digits) <= 11:
                u["class"] = int(digits)
                u["state"] = {"mode": "feed_input"}
                save_user(u)
                return await self.max.send(u["chat_id"], f"Отлично, {u['class']} класс! "
                                                         "Напиши тему или вставь текст параграфа — и я поем 🍽")
            return await self.max.send(u["chat_id"], "Напиши номер класса цифрой, например 7 🙂")
        if not u["class"]:
            return await self.ask_class(u)
        if mode == "feed_input":
            return await self.generate(u, text)
        if mode == "quiz":
            # во время теста отвечают кнопками — повторяем текущий вопрос
            await self.max.send(u["chat_id"], "Во время теста отвечай кнопками 🙂 Вот текущий вопрос:")
            return await self.question(u)
        if mode == "explain":
            return await self.explain_menu(u, st["subtopic"], st["topic"], st.get("src", ""))
        return await self.home(u)

    # --- генерация теста ---
    async def generate(self, u, user_input, focus=None):
        user_input = user_input.strip()
        if not is_sensible_input(user_input):
            return await self.max.send(u["chat_id"],
                "Не понял, что покормить 🤔 Напиши тему словами («Дроби», «Смутное время») "
                "или пришли текст параграфа.", [[("В меню", "home")]])
        if len(user_input) > MAX_INPUT_LEN:
            user_input = user_input[:MAX_INPUT_LEN]
            await self.max.send(u["chat_id"], f"Текст длинноват — я прожую первые {MAX_INPUT_LEN} символов 🐹")
        await self.max.send(u["chat_id"], "Ням… жую параграф, секунду 🍃")
        # 300 символов: короче — считаем названием темы (ИИ отвечает по школьной программе),
        # длиннее — параграф, и тогда отвечать можно только по фактам из текста
        src = user_input if len(user_input) > 300 else ""
        try:
            quiz = await ai_json(PROMPTS["quiz"],
                                 f"Класс: {u['class']}\nВвод ученика: {user_input}"
                                 + (f"\nСделай акцент на подтеме: {focus}" if focus else ""))
            quiz = normalize_quiz(quiz)
        except Exception as e:
            log.error("quiz не сгенерирован: %s", e)
            u["state"] = {"mode": "feed_input", "last_input": user_input}
            save_user(u)
            return await self.max.send(u["chat_id"], "Грызлик подавился 😵 Похоже, ИИ прилёг отдохнуть. "
                                                     "Попробуй ещё раз через минутку.", [
                [("🔄 Попробовать ещё раз", "retry_gen")], [("В меню", "home")]])
        log.info("quiz для user %s по теме «%s»: %d вопросов", u["user_id"], quiz["topic"], len(quiz["questions"]))
        u["state"] = {"mode": "quiz", "quiz": quiz, "i": 0, "correct": 0, "results": [], "src": src}
        save_user(u)
        await self.question(u)

    async def question(self, u):
        st = u["state"]
        q = st["quiz"]["questions"][st["i"]]
        await self.max.send(u["chat_id"],
                            f"Вопрос {st['i'] + 1} из {len(st['quiz']['questions'])}\n\n{q['q']}",
                            [[(o, f"ans:{k}")] for k, o in enumerate(q["options"])])

    async def on_answer(self, u, k):
        st = u["state"]
        q = st["quiz"]["questions"][st["i"]]
        ok = k == q["correct"]
        st["correct"] += int(ok)
        st["results"].append({"subtopic": q.get("subtopic", st["quiz"]["topic"]), "q": q["q"], "correct": ok})
        msg = random.choice(PHRASES["ok"]) if ok else \
            f"{random.choice(PHRASES['bad'])}\nПравильно: {q['options'][q['correct']]}"
        await self.max.send(u["chat_id"], msg)
        st["i"] += 1
        if st["i"] < len(st["quiz"]["questions"]):
            save_user(u)
            return await self.question(u)
        return await self.diagnosis(u)

    async def diagnosis(self, u):
        st = u["state"]
        topic = st["quiz"]["topic"]
        weak = sorted({r["subtopic"] for r in st["results"] if not r["correct"]})
        knows = sorted({r["subtopic"] for r in st["results"] if r["correct"]} - set(weak))
        for w in weak:
            if not any(x["topic"] == topic and x["subtopic"] == w for x in u["weak"]):
                u["weak"].append({"topic": topic, "subtopic": w, "added": today().isoformat(), "r": 0})
        grew = feed(u, st["correct"] * 10)
        text = (f"Итог: {st['correct']}/{len(st['results'])} 🍽\n"
                f"✅ Знаешь: {', '.join(knows) or '—'}\n"
                f"🔎 Западает: {', '.join(weak) or 'ничего! Грызлик объелся'}\n"
                f"+{st['correct'] * 10} опыта · серия {u['streak']} дн.")
        if grew:
            text += f"\n\n🎉 Я вырос! Теперь я {level_name(u['level'])}!"
            log.info("user %s вырос до уровня %d", u["user_id"], u["level"])
        btns = [[(f"Разобрать «{w}»", f"explain:{i}")] for i, w in enumerate(weak[:3])] + [[("В меню", "home")]]
        u["state"] = {"mode": "after", "topic": topic, "weak": weak, "src": st.get("src", "")}
        save_user(u)
        await self.max.send(u["chat_id"], text, btns, pet_image(u) if grew else None)

    # --- объяснение ---
    async def explain_menu(self, u, subtopic, topic, src=""):
        u["state"] = {"mode": "explain", "topic": topic, "subtopic": subtopic, "src": src}
        save_user(u)
        await self.max.send(u["chat_id"], f"Как объяснить «{subtopic}»?",
                            [[("Проще", "mode:simple"), ("На примере", "mode:example"), ("По-другому", "mode:another")]])

    async def explain(self, u, mode):
        st = u["state"]
        try:
            sysm = fill(PROMPTS["explain"], **{"class": u["class"], "subtopic": st["subtopic"], "topic": st["topic"]})
            res = await ai_json(sysm, f"Способ: {mode}\nКонтекст: {st.get('src') or 'нет'}", 0.8)
            if not isinstance(res, dict) or not str(res.get("explanation", "")).strip():
                raise ValueError("плохой формат объяснения")
        except Exception as e:
            log.error("explain не сгенерирован: %s", e)
            return await self.max.send(u["chat_id"], "Грызлик задумался и потерял мысль 😵 Попробуй ещё разок.", [
                [("🔄 Попробовать ещё раз", "mode:" + mode)], [("В меню", "home")]])
        await self.max.send(u["chat_id"], f"{res['explanation']}\n\n🤔 Проверь себя: {res.get('check_question', '')}",
                            [[("Понял, проверь меня", "followup")],
                             [("Объясни иначе", "explain_again"), ("В меню", "home")]])

    async def followup(self, u):
        st = u["state"]
        try:
            prev = "; ".join(r["q"] for r in u["state"].get("results", [])[:5]) or "—"
            sysm = fill(PROMPTS["followup"], **{"class": u["class"], "subtopic": st["subtopic"],
                                                "topic": st["topic"], "previous_questions": prev})
            res = await ai_json(sysm, "Сгенерируй.")
            qs = normalize_questions(res.get("questions"), 2)[:3]
        except Exception as e:
            log.error("followup не сгенерирован: %s", e)
            return await self.max.send(u["chat_id"], "Не получилось придумать вопросы, вернёмся позже 😵", [
                [("🔄 Попробовать ещё раз", "followup")], [("В меню", "home")]])
        quiz = {"topic": st["topic"], "questions": [dict(q, subtopic=st["subtopic"]) for q in qs]}
        u["state"] = {"mode": "quiz", "quiz": quiz, "i": 0, "correct": 0, "results": [],
                      "src": st.get("src", ""), "topic": st["topic"], "closing": st["subtopic"]}
        save_user(u)
        await self.question(u)

    # --- нажатия кнопок ---
    async def on_callback(self, u, payload, cb_id, message=None):
        st = u["state"]

        def strip_keyboard():
            # кнопки снимаем, чтобы нельзя было нажать второй раз
            if message:
                text = ((message.get("body") or {}).get("text") or "​")
                return {"text": text, "attachments": []}
            return None

        await self.max.answer_callback(cb_id, edit_message=strip_keyboard())

        if payload.startswith("class:"):
            u["class"] = int(payload[6:])
            u["state"] = {"mode": "feed_input"}
            save_user(u)
            return await self.max.send(u["chat_id"], f"Отлично, {u['class']} класс! "
                                                     "Напиши тему или вставь текст параграфа — и я поем 🍽")
        if payload == "home":
            u["state"] = {}
            save_user(u)
            return await self.home(u)
        if payload == "class":
            return await self.ask_class(u)
        if payload == "stats":
            return await self.max.send(u["chat_id"], stats_text(u), [[("В меню", "home")]])
        if payload == "feed":
            u["state"] = {"mode": "feed_input"}
            save_user(u)
            return await self.max.send(u["chat_id"], "Напиши тему (например «Дроби» или «Смутное время») "
                                                     "или вставь текст параграфа.")
        if payload == "retry_gen":  # повтор той же темы после сбоя ИИ
            last = st.get("last_input")
            if last:
                return await self.generate(u, last)
            u["state"] = {"mode": "feed_input"}
            save_user(u)
            return await self.max.send(u["chat_id"], "Напиши тему или вставь текст параграфа.")
        if payload == "weak":
            if not u["weak"]:
                return await self.home(u)
            btns = [[(f"{w['topic']} / {w['subtopic']}", f"weakitem:{i}")] for i, w in enumerate(u["weak"][:8])] \
                   + [[("В меню", "home")]]
            return await self.max.send(u["chat_id"], "Твои слабые места. Нажми, чтобы разобрать:", btns)
        if payload.startswith("weakitem:"):
            w = u["weak"][int(payload[9:])]
            return await self.explain_menu(u, w["subtopic"], w["topic"])
        if payload == "repeat":
            if not u["weak"]:
                return await self.home(u)
            w = u["weak"][0]
            return await self.generate(u, w["topic"], focus=w["subtopic"])
        if payload.startswith("ans:") and st.get("mode") == "quiz":
            if "closing" in st:  # добивающие вопросы после объяснения идут отдельно
                return await self.on_closing_answer(u, int(payload[4:]))
            return await self.on_answer(u, int(payload[4:]))
        if payload.startswith("explain:") and st.get("mode") == "after":
            w = st["weak"][int(payload[8:])]
            return await self.explain_menu(u, w, st["topic"], st.get("src", ""))
        if payload.startswith("mode:") and st.get("mode") == "explain":
            return await self.explain(u, payload[5:])
        if payload == "explain_again" and st.get("mode") == "explain":
            return await self.explain_menu(u, st["subtopic"], st["topic"], st.get("src", ""))
        if payload == "followup" and st.get("mode") == "explain":
            return await self.followup(u)
        return await self.home(u)

    # --- добивающие вопросы (после объяснения) ---
    async def on_closing_answer(self, u, k):
        st = u["state"]
        q = st["quiz"]["questions"][st["i"]]
        ok = k == q["correct"]
        st["correct"] += int(ok)
        st["results"].append({"subtopic": st["closing"], "q": q["q"], "correct": ok})
        st["i"] += 1
        msg = random.choice(PHRASES["ok"]) if ok else \
            f"{random.choice(PHRASES['bad'])}\nПравильно: {q['options'][q['correct']]}"
        await self.max.send(u["chat_id"], msg)
        if st["i"] < len(st["quiz"]["questions"]):
            save_user(u)
            return await self.question(u)
        if st["correct"] == len(st["quiz"]["questions"]):
            u["weak"] = [w for w in u["weak"] if w["subtopic"] != st["closing"]]
            u["closed"].append({"topic": st["topic"], "subtopic": st["closing"], "date": today().isoformat()})
            grew = feed(u, 30)
            txt = f"🎉 Тема «{st['closing']}» закрыта! +30 опыта. Грызлик объелся!" \
                  + (f"\nЯ вырос — теперь {level_name(u['level'])}!" if grew else "")
            log.info("user %s закрыл тему «%s»", u["user_id"], st["closing"])
        else:
            grew = False
            feed(u, 5)
            txt = "Почти! +5 опыта. Вернёмся к этому через пару дней."
        u["state"] = {}
        save_user(u)
        await self.max.send(u["chat_id"], txt, [[("В меню", "home")]], pet_image(u) if grew else None)

    # ---------- циклы ----------
    async def poll(self):
        marker = None
        log.info("Грызлик запущен, слушаю MAX…")
        while True:
            try:
                data = await self.max.updates(marker)
                marker = data.get("marker", marker)
                for upd in data.get("updates", []):
                    task = asyncio.create_task(self.handle(upd))
                    _TASKS.add(task)  # иначе GC может собрать задачу до завершения
                    task.add_done_callback(_TASKS.discard)
            except Exception as e:
                log.error("poll error: %s", e)
                await asyncio.sleep(3)
            else:
                await asyncio.sleep(0.4)  # MAX не даёт опрашивать чаще раза в 300 мс

    async def handle(self, upd):
        try:
            t = upd.get("update_type")
            uid, chat_id, key = None, None, None
            if t == "bot_started":
                uid, chat_id = upd["user"]["user_id"], upd["chat_id"]
            elif t == "message_created":
                m = upd["message"]
                uid = (m.get("sender") or {}).get("user_id")
                chat_id = (m.get("recipient") or {}).get("chat_id")
                key = "msg:" + str((m.get("body") or {}).get("mid", ""))
            elif t == "message_callback":
                cb = upd["callback"]
                uid = (cb.get("user") or {}).get("user_id")
                m = upd.get("message") or {}  # сообщение могли удалить — тогда None
                chat_id = (m.get("recipient") or {}).get("chat_id")
                key = "cb:" + str(cb.get("callback_id", ""))
            else:
                return
            if uid is None:
                return
            if key and is_seen(key):
                # MAX иногда доставляет апдейт повторно — без этой проверки опыт уходил бы дважды
                log.info("апдейт %s уже обработан, пропускаю", key)
                return
            async with self.lock(uid):
                u = get_user(uid, chat_id)
                if t == "bot_started":
                    await self.greeting(u)
                elif t == "message_created":
                    m = upd["message"]
                    text = (m.get("body") or {}).get("text") or ""
                    low = text.strip().lower()
                    if not text:
                        return await self.max.send(u["chat_id"], "Я пока понимаю только текст 🙂")
                    if low in ("/start", "start", "меню", "/menu"):
                        u["state"] = {}
                        save_user(u)
                        return await self.greeting(u)
                    if low in ("/stats", "прогресс", "мой прогресс"):
                        return await self.max.send(u["chat_id"], stats_text(u), [[("В меню", "home")]])
                    await self.on_text(u, text)
                else:  # message_callback
                    cb = upd["callback"]
                    await self.on_callback(u, cb.get("payload", ""), cb.get("callback_id", ""),
                                           upd.get("message"))
        except Exception:
            log.exception("handle: апдейт не обработан, бот продолжает работу")

    async def reminders(self):
        # обход раз в 10 минут: каждому ученику не больше одного напоминания в день
        # TODO: с несколькими репликами бота планировщик надо выносить наружу
        while True:
            try:
                now = dt.datetime.now(TZ)
                if now.hour >= REMIND_HOUR:
                    t = today().isoformat()
                    with db() as c:
                        ids = [r[0] for r in c.execute(
                            "SELECT user_id FROM users WHERE class>0 AND last_remind!=?", (t,))]
                    for uid in ids:
                        try:  # сбой у одного не мешает остальным
                            u = get_user(uid)
                            msg = daily_decay(u)
                            u["last_remind"] = t
                            w = repeat_candidate(u)
                            if w is not None:
                                w["r"] = w.get("r", 0) + 1
                            save_user(u)
                            if msg and u["chat_id"]:
                                btns = [[("🍽 Покормить", "feed")]]
                                if w:
                                    btns.append([(f"🔁 Перекусить: {w['subtopic']}"[:64], "repeat")])
                                await self.max.send(u["chat_id"], msg, btns, pet_image(u))
                            elif w and u["chat_id"]:
                                await self.max.send(u["chat_id"], f"🖱 Перекусим? Повторим «{w['subtopic']}» — "
                                                                   "старые слабые места лучше грызть заново!",
                                                    [[(f"🔁 Перекусить: {w['subtopic']}"[:64], "repeat")],
                                                     [("Позже", "home")]])
                        except Exception:
                            log.exception("reminders: не удалось напомнить user %s, продолжаю", uid)
            except Exception:
                log.exception("reminders: обход не удался, продолжаю")
            await asyncio.sleep(600)

    async def run(self):
        me = None
        try:
            me = await self.max.get_me()
            log.info("Токен принят, бот: %s (user_id=%s)", me.get("name"), me.get("user_id"))
        except Exception as e:
            log.error("Не удалось проверить токен (/me): %s — продолжаю, попробую в цикле опроса", e)
        cleanup_seen()
        try:
            await asyncio.gather(self.poll(), self.reminders())
        finally:
            await self.max.close()


_TASKS = set()


async def main():
    if not MAX_TOKEN:
        log.error("Нет токена MAX: задайте MAX_BOT_TOKEN (выдаёт чат-бот «Идея фикс» "
                  "или business.max.ru → Чат-боты).")
        raise SystemExit(1)
    if not AI_KEY:
        log.warning("AI_API_KEY не задан: бот запустится, но кормить питомца не получится.")
    g = Grizlik()
    await g.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Остановлено вручную")

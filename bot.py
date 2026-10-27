import asyncio, os, re, sqlite3, sys
from datetime import date, datetime
from zoneinfo import ZoneInfo

try:
    from aiogram import BaseMiddleware, Bot, Dispatcher, F
    from aiogram.filters import Command, CommandObject, CommandStart
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.state import State, StatesGroup
    from aiogram.fsm.storage.memory import MemoryStorage
    from aiogram.types import (BotCommand, CallbackQuery, InlineKeyboardButton, KeyboardButton,
                               MenuButtonDefault, Message, ReplyKeyboardMarkup)
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    from dotenv import load_dotenv
except ModuleNotFoundError as e:
    sys.exit(f"Не найден модуль «{e.name}». Запусти run.bat (или: python -m pip install -r requirements.txt).\n"
             f"Сейчас бот запущен в Python: {sys.executable}")

load_dotenv()
TOKEN = os.environ["BOT_TOKEN"]
DB = os.getenv("DB_PATH", "starosta.db")
TZ = ZoneInfo(os.getenv("TZ_NAME", "Europe/Moscow"))
ALLOWED = {int(x) for x in os.getenv("ALLOWED_IDS", "").replace(" ", "").split(",") if x}

bot = Bot(TOKEN)
dp = Dispatcher(storage=MemoryStorage())

B_MARK, B_STATS, B_STUD, B_ADD = "📝 Отметить пару", "📊 Прогулы", "👥 Студенты", "➕ Добавить студентов"
B_ABS, B_FIX = "🚫 Отсутствующие", "✏️ Исправить пару"
MENU = ReplyKeyboardMarkup(resize_keyboard=True, keyboard=[
    [KeyboardButton(text=B_MARK), KeyboardButton(text=B_STATS)],
    [KeyboardButton(text=B_ABS), KeyboardButton(text=B_FIX)],
    [KeyboardButton(text=B_STUD), KeyboardButton(text=B_ADD)]])
HELP = ("Бот для старосты: считает прогулы.\n\n"
        "1. ➕ Добавь студентов — впиши ФИО, по одному в строке.\n"
        "2. 📝 На каждой паре отмечай, кого нет: нажатие по кругу ⬜ был → ❌ прогул → 🟡 уважительная.\n"
        "3. 📊 Смотри, у кого сколько прогулов.\n"
        "4. 🚫 Выбери день — бот покажет, кто отсутствовал.\n\n"
        "Команды: /add ФИО, /mark [дд.мм], /stats")

# ───────────────────────── БД (всё привязано к owner = telegram id старосты) ─────────────────────────
SCHEMA = """
CREATE TABLE IF NOT EXISTS students(id INTEGER PRIMARY KEY, owner INTEGER, name TEXT);
CREATE TABLE IF NOT EXISTS sessions(id INTEGER PRIMARY KEY, owner INTEGER, day TEXT);
CREATE TABLE IF NOT EXISTS marks(session_id INTEGER, student_id INTEGER, status TEXT,
    PRIMARY KEY(session_id, student_id));
"""


def q(sql, a=(), one=False):
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    try:
        cur = c.execute(sql, a)
        if sql.lstrip().upper().startswith("INSERT"):
            c.commit()
            return cur.lastrowid
        rows = [dict(r) for r in cur.fetchall()]
        c.commit()
        return (rows[0] if rows else None) if one else rows
    finally:
        c.close()


def students_of(o):
    return sorted(q("SELECT id, name FROM students WHERE owner=?", (o,)), key=lambda s: s["name"].casefold())


def add_students(o, text):
    have = {s["name"].casefold() for s in students_of(o)}
    added, dup = [], []
    for line in text.splitlines():
        n = " ".join(line.split()).lstrip("-•*0123456789.) ")[:80]  # убираем «1.» и маркеры списка
        if len(n) < 2:
            continue
        if n.casefold() in have:
            dup.append(n)
            continue
        q("INSERT INTO students(owner,name) VALUES(?,?)", (o, n))
        have.add(n.casefold())
        added.append(n)
    return added, dup


def parse_mark(args):
    """Пусто = сегодня, иначе дд.мм или дд.мм.гг(гг). Возвращает ISO-дату или None."""
    today, text = datetime.now(TZ).date(), (args or "").strip()
    if not text:
        return today.isoformat()
    m = re.fullmatch(r"(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?", text)
    if not m:
        return None
    y = int(m[3]) if m[3] else today.year
    try:
        return date(y + 2000 if y < 100 else y, int(m[2]), int(m[1])).isoformat()
    except ValueError:
        return None


def fmt_day(iso):
    return f"{iso[8:10]}.{iso[5:7]}"


# ───────────────────────── Доступ только для старосты ─────────────────────────
class Guard(BaseMiddleware):
    async def __call__(self, handler, event, data):
        u = data.get("event_from_user")
        if ALLOWED and (not u or u.id not in ALLOWED):
            if isinstance(event, CallbackQuery):
                await event.answer("Бот только для старосты", show_alert=True)
            elif isinstance(event, Message):
                await event.answer("Этот бот только для старосты.")
            return
        return await handler(event, data)


dp.message.outer_middleware(Guard())
dp.callback_query.outer_middleware(Guard())


class AddSt(StatesGroup):
    names = State()


# ───────────────────────── Отметка пары ─────────────────────────
ICON = {None: "⬜", "absent": "❌", "excused": "🟡"}
NEXT = {None: "absent", "absent": "excused", "excused": None}


def mark_view(o, sid):
    s = q("SELECT * FROM sessions WHERE id=? AND owner=?", (sid, o), one=True)
    st = {m["student_id"]: m["status"] for m in q("SELECT student_id, status FROM marks WHERE session_id=?", (sid,))}
    b = InlineKeyboardBuilder()
    for x in students_of(o):
        b.row(InlineKeyboardButton(text=f"{ICON[st.get(x['id'])]} {x['name'][:40]}", callback_data=f"t:{sid}:{x['id']}"))
    b.row(InlineKeyboardButton(text="✅ Готово", callback_data="done"),
          InlineKeyboardButton(text="🗑 Удалить пару", callback_data=f"x:{sid}"))
    absent, excused = list(st.values()).count("absent"), list(st.values()).count("excused")
    text = (f"Пара {fmt_day(s['day'])}\n"
            f"Нажимай на ФИО: ⬜ был → ❌ прогул → 🟡 уважительная\n"
            f"Прогулов: {absent} · уважительных: {excused}")
    return text, b.as_markup()


async def open_session(m: Message, day):
    o = m.chat.id
    if not students_of(o):
        return await m.answer("Сначала добавь студентов: ➕ Добавить студентов")
    sid = q("INSERT INTO sessions(owner,day) VALUES(?,?)", (o, day))
    text, kb = mark_view(o, sid)
    await m.answer(text, reply_markup=kb)


@dp.callback_query(F.data.startswith("t:"))
async def toggle(cq: CallbackQuery):
    o = cq.from_user.id
    _, sid, stid = cq.data.split(":")
    ok = q("SELECT 1 x FROM sessions WHERE id=? AND owner=?", (sid, o), one=True) and \
        q("SELECT 1 x FROM students WHERE id=? AND owner=?", (stid, o), one=True)
    if not ok:
        return await cq.answer("Не найдено", show_alert=True)
    cur = q("SELECT status FROM marks WHERE session_id=? AND student_id=?", (sid, stid), one=True)
    nxt = NEXT[cur["status"] if cur else None]
    if nxt:
        q("INSERT OR REPLACE INTO marks VALUES(?,?,?)", (sid, stid, nxt))
    else:
        q("DELETE FROM marks WHERE session_id=? AND student_id=?", (sid, stid))
    text, kb = mark_view(o, int(sid))
    await cq.message.edit_text(text, reply_markup=kb)
    await cq.answer()


@dp.callback_query(F.data == "done")
async def done(cq: CallbackQuery):
    await cq.message.edit_reply_markup(reply_markup=None)
    await cq.answer("Сохранено ✅")


@dp.callback_query(F.data.startswith("x:"))
async def drop_session(cq: CallbackQuery):
    sid = cq.data[2:]
    if q("SELECT 1 x FROM sessions WHERE id=? AND owner=?", (sid, cq.from_user.id), one=True):
        q("DELETE FROM marks WHERE session_id=?", (sid,))
        q("DELETE FROM sessions WHERE id=?", (sid,))
    await cq.message.edit_text("Пара удалена, прогулы за неё не учитываются.")
    await cq.answer()


@dp.callback_query(F.data.startswith("o:"))
async def reopen(cq: CallbackQuery):
    o = cq.from_user.id
    if not q("SELECT 1 x FROM sessions WHERE id=? AND owner=?", (cq.data[2:], o), one=True):
        return await cq.answer("Не найдено", show_alert=True)
    text, kb = mark_view(o, int(cq.data[2:]))
    await cq.message.answer(text, reply_markup=kb)
    await cq.answer()


# ───────────────────────── Статистика, студенты, история ─────────────────────────
def stats_text(o):
    if not students_of(o):
        return "Список пуст. Добавь студентов: ➕ Добавить студентов"
    total = q("SELECT COUNT(*) n FROM sessions WHERE owner=?", (o,), one=True)["n"]
    rows = q("""SELECT s.name, SUM(m.status='absent') a, SUM(m.status='excused') e
                FROM students s LEFT JOIN marks m ON m.student_id=s.id WHERE s.owner=? GROUP BY s.id""", (o,))
    rows.sort(key=lambda r: (-(r["a"] or 0), -(r["e"] or 0), r["name"].casefold()))
    out = [f"📊 Прогулы · пар отмечено: {total}\n"]
    for i, r in enumerate(rows, 1):
        a, e = r["a"] or 0, r["e"] or 0
        out.append(f"{i}. {r['name']} — {a}" + (f" (+{e} уваж.)" if e else ""))
    return "\n".join(out)


async def send_long(m: Message, text):
    while text:
        cut = len(text)
        if cut > 4000:
            cut = text.rfind("\n", 0, 4000)
            if cut <= 0:
                cut = 4000
        await m.answer(text[:cut])
        text = text[cut:].lstrip("\n")


def students_view(o):
    sts = students_of(o)
    if not sts:
        return "Список пуст. Добавь студентов: ➕ Добавить студентов", None
    b = InlineKeyboardBuilder()
    for x in sts:
        b.row(InlineKeyboardButton(text=f"🗑 {x['name'][:50]}", callback_data=f"d:{x['id']}"))
    return f"Студентов: {len(sts)}\nНажми на ФИО, чтобы удалить.", b.as_markup()


@dp.callback_query(F.data.startswith("d:"))
async def ask_delete(cq: CallbackQuery):
    s = q("SELECT * FROM students WHERE id=? AND owner=?", (cq.data[2:], cq.from_user.id), one=True)
    if not s:
        return await cq.answer("Не найдено", show_alert=True)
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="Да, удалить", callback_data=f"dy:{s['id']}"),
          InlineKeyboardButton(text="Отмена", callback_data="dn"))
    await cq.message.edit_text(f"Удалить «{s['name']}»? Его прогулы тоже удалятся.", reply_markup=b.as_markup())
    await cq.answer()


@dp.callback_query(F.data.startswith("dy:") | (F.data == "dn"))
async def do_delete(cq: CallbackQuery):
    o = cq.from_user.id
    if cq.data.startswith("dy:") and q("SELECT 1 x FROM students WHERE id=? AND owner=?", (cq.data[3:], o), one=True):
        q("DELETE FROM marks WHERE student_id=?", (cq.data[3:],))
        q("DELETE FROM students WHERE id=?", (cq.data[3:],))
    text, kb = students_view(o)
    await cq.message.edit_text(text, reply_markup=kb)
    await cq.answer()


# ───────────────────────── Команды и кнопки меню ─────────────────────────
@dp.message(CommandStart())
async def start(m: Message, state: FSMContext):
    await state.clear()
    await m.answer(HELP, reply_markup=MENU)


@dp.message(Command("cancel"))
async def cancel(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("Отменено.", reply_markup=MENU)


@dp.message(F.text == B_ADD)
async def add_btn(m: Message, state: FSMContext):
    await state.set_state(AddSt.names)
    await m.answer("Пришли ФИО студентов — по одному в строке, можно целым списком.\n"
                   "Например:\nИванов Иван Иванович\nПетрова Анна Сергеевна\n\n/cancel — отмена")


@dp.message(Command("add"))
async def add_cmd(m: Message, command: CommandObject, state: FSMContext):
    await state.clear()
    if not command.args:
        return await add_btn(m, state)
    await reply_added(m, command.args)


async def reply_added(m: Message, text):
    added, dup = add_students(m.chat.id, text)
    msg = (f"Добавлено ({len(added)}):\n" + "\n".join(added)) if added else "Никого не добавил."
    if dup:
        msg += "\n\nУже были в списке: " + ", ".join(dup)
    await m.answer(msg, reply_markup=MENU)


@dp.message(F.text == B_MARK)
async def mark_btn(m: Message, state: FSMContext):
    await state.clear()
    await open_session(m, parse_mark(""))


@dp.message(Command("mark"))
async def mark_cmd(m: Message, command: CommandObject, state: FSMContext):
    await state.clear()
    p = parse_mark(command.args)
    if not p:
        return await m.answer("Не понял дату. Пример: /mark 25.10")
    await open_session(m, p)


@dp.message(F.text == B_STATS)
@dp.message(Command("stats"))
async def stats(m: Message, state: FSMContext):
    await state.clear()
    await send_long(m, stats_text(m.chat.id))


@dp.message(F.text == B_STUD)
async def students_btn(m: Message, state: FSMContext):
    await state.clear()
    text, kb = students_view(m.chat.id)
    await m.answer(text, reply_markup=kb)


@dp.message(F.text == B_FIX)
async def fix_btn(m: Message, state: FSMContext):
    await state.clear()
    rows = q("""SELECT s.id, s.day,
                (SELECT COUNT(*) FROM sessions s2 WHERE s2.owner=s.owner AND s2.day=s.day AND s2.id<=s.id) k,
                (SELECT COUNT(*) FROM marks k WHERE k.session_id=s.id AND k.status='absent') n
                FROM sessions s WHERE s.owner=? ORDER BY s.day DESC, s.id DESC LIMIT 10""", (m.chat.id,))
    if not rows:
        return await m.answer("Пар пока нет.")
    b = InlineKeyboardBuilder()
    for r in rows:
        b.row(InlineKeyboardButton(text=f"{fmt_day(r['day'])} · пара {r['k']} · Н: {r['n']}", callback_data=f"o:{r['id']}"))
    await m.answer("Последние пары — нажми, чтобы поправить отметки:", reply_markup=b.as_markup())


@dp.message(F.text == B_ABS)
async def days_btn(m: Message, state: FSMContext):
    await state.clear()
    days = q("SELECT DISTINCT day FROM sessions WHERE owner=? ORDER BY day DESC LIMIT 30", (m.chat.id,))
    if not days:
        return await m.answer("Пар пока нет.")
    b = InlineKeyboardBuilder()
    for d in days:
        b.button(text=fmt_day(d["day"]), callback_data=f"a:{d['day']}")
    b.adjust(4)
    await m.answer("Выбери день:", reply_markup=b.as_markup())


@dp.callback_query(F.data.startswith("a:"))
async def absent_on_day(cq: CallbackQuery):
    day = cq.data[2:]
    rows = q("""SELECT s.name, MIN(CASE m.status WHEN 'absent' THEN 0 ELSE 1 END) pri
                FROM marks m JOIN sessions x ON x.id=m.session_id JOIN students s ON s.id=m.student_id
                WHERE x.owner=? AND x.day=? GROUP BY s.id""", (cq.from_user.id, day))
    rows.sort(key=lambda r: r["name"].casefold())
    if rows:
        lines = [f"{i}. {r['name']}" + (" (уваж.)" if r["pri"] else "") for i, r in enumerate(rows, 1)]
        text = f"🚫 Отсутствовали {fmt_day(day)} ({len(rows)}):\n" + "\n".join(lines)
    else:
        text = f"{fmt_day(day)}: отсутствующих нет 🎉"
    await cq.message.answer(text)
    await cq.answer()


@dp.message(AddSt.names, F.text)
async def got_names(m: Message, state: FSMContext):
    await state.clear()
    await reply_added(m, m.text)


async def main():
    sqlite3.connect(DB).executescript(SCHEMA)
    await bot.set_chat_menu_button(menu_button=MenuButtonDefault())  # убрать кнопку Mini App, если она была
    await bot.set_my_commands([BotCommand(command="mark", description="Отметить пару: /mark [дд.мм]"),
                               BotCommand(command="stats", description="Прогулы по студентам"),
                               BotCommand(command="add", description="Добавить студентов по ФИО"),
                               BotCommand(command="cancel", description="Отмена")])
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())

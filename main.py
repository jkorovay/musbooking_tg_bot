import os
import asyncio
import json
from datetime import datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
    SwitchInlineQueryChosenChat,
)
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from dotenv import load_dotenv

# ========== ЗАГРУЗКА .env ==========
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")

# Читаем список ID из .env (формат: 123456789,987654321)
allowed_ids_raw = os.getenv("ALLOWED_IDS", "")
ALLOWED_IDS = [
    int(x.strip())
    for x in allowed_ids_raw.split(",")
    if x.strip().isdigit()
]

# ID администратора, который видит логи в консоли
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

CHECK_INTERVAL_SEC = 60 * 5  # проверка каждые 5 минут
SEEN_FILE = "seen_slots.json"
ASSIGNMENTS_FILE = "assignments.json"
MOSCOW_TZ = ZoneInfo("Europe/Moscow")

# ----- Константы для API -----
# ROOM_ID и OPTION_ID — приватные идентификаторы студии на платформе MusBooking.
# Хранятся в .env, в репозиторий не кладутся (см. .env.example).
BASE_CALC_RANGE_URL = "https://partner.musbooking.com/api/orders/calc-range"
ROOM_ID = os.getenv("ROOM_ID", "")
OPTION_ID = os.getenv("OPTION_ID", "")
DAYS = 14

BOOKINGS_URL = (
    f"https://partner.musbooking.com/api/days/future?room={ROOM_ID}"
)

latest_slots_data: list[dict] = []
morning_reminder_sent_date: str = ""  # дата (YYYY-MM-DD) последней отправленной утренней напоминалки в 7:00


# ========== CALLBACK DATA ==========
class AssignStudent(CallbackData, prefix="assign", sep="|"):
    slot_dt: str  # ISO datetime строка слота

class ViewDate(CallbackData, prefix="view"):
    date: str  # YYYY-MM-DD

class BackToDates(CallbackData, prefix="back_dates"):
    pass


# ========== FSM ==========
class AssignFlow(StatesGroup):
    waiting_name = State()


# ========== ДИНАМИЧЕСКИЙ calc-range URL ==========
def build_calc_range_url() -> str:
    now = datetime.now()
    date_str = now.strftime("%Y-%m-%d+%H:%M:%S")
    return (
        f"{BASE_CALC_RANGE_URL}"
        f"?room={ROOM_ID}"
        f"&date={date_str}"
        f"&days={DAYS}"
        f"&option={OPTION_ID}"
    )


# ========== ХРАНЕНИЕ АКТУАЛЬНЫХ СЛОТОВ ==========
def load_seen() -> set:
    try:
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    except FileNotFoundError:
        return set()


def save_seen(seen: set) -> None:
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(list(seen), f, ensure_ascii=False)


# ========== ХРАНЕНИЕ НАЗНАЧЕНИЙ ==========
def load_assignments() -> dict:
    try:
        with open(ASSIGNMENTS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def save_assignment(slot_dt: str, student_name: str) -> None:
    data = load_assignments()
    data[slot_dt] = {
        "student": student_name,
        "assigned_at": datetime.now().isoformat(timespec="seconds"),
    }
    with open(ASSIGNMENTS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ========== ФИЛЬТР СЛОТОВ ==========
def is_slot_matching(dt: datetime) -> bool:
    """Условия: будни, время 15:00–20:00."""
    if dt.weekday() > 4:
        return False
    start = dtime(15, 0)
    end = dtime(20, 0)
    return start <= dt.time() <= end


def build_busy_intervals(bookings: list) -> list:
    intervals = []
    for b in bookings:
        if "dateFrom" not in b or "dateTo" not in b:
            continue
        try:
            start = datetime.fromisoformat(b["dateFrom"])
            end = datetime.fromisoformat(b["dateTo"])
            status = b.get("status", 1)
            intervals.append((start, end, status))
        except:
            continue
    return intervals


def is_datetime_busy(dt: datetime, busy_intervals: list) -> bool:
    for start, end, status in busy_intervals:
        if status == 0 and start <= dt < end:
            return True
    return False


# ========== ХЕНДЛЕРЫ И ПАРСИНГ ==========
def parse_user_datetime(text: str) -> datetime | None:
    text = text.strip()
    try:
        date_part, time_part = text.split("-")
        day, month = int(date_part[:2]), int(date_part[2:4])
        hour, minute = int(time_part[:2]), int(time_part[2:4])
        year = datetime.now().year
        return datetime(year, month, day, hour, minute)
    except:
        return None


async def fetch_json_with_retry(session, url, retries=3):
    for attempt in range(retries):
        try:
            async with session.get(url) as resp:
                resp.raise_for_status()
                return await resp.json()
        except:
            if attempt == retries - 1: raise
            await asyncio.sleep(5)


async def fetch_all_data():
    async with aiohttp.ClientSession() as session:
        slots = await fetch_json_with_retry(session, build_calc_range_url())
        bookings = await fetch_json_with_retry(session, BOOKINGS_URL)
        return slots, bookings


# ========== ОСНОВНОЙ ЦИКЛ МОНИТОРИНГА ==========
async def check_and_notify(bot: Bot):
    global latest_slots_data
    seen_previously = load_seen()
    
    print(f"\n{'='*60}")
    print(f"🔍 Проверка слотов: {datetime.now().strftime('%d.%m.%Y %H:%M:%S')}")
    print(f"📋 Ранее виденных слотов: {len(seen_previously)}")
    
    try:
        slots_data, bookings_data = await fetch_all_data()
        latest_slots_data = slots_data
        print(f"📊 Получено слотов из API: {len(slots_data)}")
        print(f"📅 Получено бронирований: {len(bookings_data)}")
    except Exception as e:
        print(f"❌ Ошибка загрузки данных: {e}")
        return

    busy_intervals = build_busy_intervals(bookings_data)
    current_free_slots = set()
    new_slots_found = []  # Список новых слотов для уведомления
    successfully_notified_slots = set()  # Слоты, о которых успешно отправили уведомления

    for item in slots_data:
        if item.get("error"): continue
        
        dt = datetime.fromisoformat(item["date"])
        slot_id = item["date"]

        if not is_slot_matching(dt): continue
        if is_datetime_busy(dt, busy_intervals): continue

        current_free_slots.add(slot_id)

        # Проверяем, является ли слот новым
        if slot_id not in seen_previously:
            new_slots_found.append((slot_id, dt, item))
    
    # Отправляем уведомления о всех новых слотах
    for slot_id, dt, item in new_slots_found:
        # Формируем прямую ссылку на виджет с выбранной датой
        date_param = dt.strftime('%Y-%m-%d')
        widget_url = f"https://widget.musbooking.com/?room={ROOM_ID}&date={date_param}"
        
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[[
                InlineKeyboardButton(text="🎟 Забронировать", url=widget_url),
                InlineKeyboardButton(
                    text="👤 Назначить ученику",
                    callback_data=AssignStudent(slot_dt=slot_id).pack(),
                ),
            ]]
        )

        text = (
            "🎶 **Нашёлся подходящий слот!**\n\n"
            f"📅 Дата: `{dt.strftime('%d.%m.%Y')}`\n"
            f"⏰ Время: `{dt.strftime('%H:%M')}`\n"
            f"⏳ Длительность: {item['hours']} ч.\n"
            f"💰 Цена: {item['totalPrice']} ₽"
        )

        # Отправляем уведомление всем пользователям
        failed_users = []
        notified_users = set()  # Отслеживаем, кому уже отправили
        
        for user_id in ALLOWED_IDS:
            try:
                await bot.send_message(user_id, text, reply_markup=keyboard, parse_mode="Markdown")
                notified_users.add(user_id)
            except Exception as e:
                failed_users.append((user_id, str(e)))
        
        # Если хотя бы одному пользователю доставили — отмечаем слот как обработанный
        if notified_users:
            successfully_notified_slots.add(slot_id)
        
        # Отправляем отчет администратору (только если он ещё не получил уведомление)
        if ADMIN_ID and ADMIN_ID not in notified_users:
            admin_report = f"🆕 *Новый слот:* {dt.strftime('%d.%m.%Y %H:%M')}\n"
            if failed_users:
                admin_report += "\n❌ *Ошибки отправки:*\n"
                for user_id, error in failed_users:
                    admin_report += f"• Пользователь {user_id}: {error}\n"
            else:
                admin_report += f"✅ Уведомление отправлено всем ({len(ALLOWED_IDS)})"
            
            try:
                await bot.send_message(ADMIN_ID, admin_report, parse_mode="Markdown")
            except:
                pass  # Если админу не отправилось, не критично
        
        # Логируем результат отправки
        if notified_users:
            print(f"✅ Слот {dt.strftime('%d.%m.%Y %H:%M')}: уведомлено {len(notified_users)} из {len(ALLOWED_IDS)}")
        else:
            print(f"❌ Слот {dt.strftime('%d.%m.%Y %H:%M')}: никому не удалось отправить уведомление")
    
    # Обновляем список виденных слотов: добавляем только те, о которых успешно уведомили
    # ПЛЮС старые свободные слоты, которые всё ещё свободны (чтобы не потерять их из истории)
    updated_seen = seen_previously.union(successfully_notified_slots)
    # Также добавляем слоты, которые были в seen_previously и всё ещё свободны
    updated_seen = updated_seen.union(seen_previously.intersection(current_free_slots))
    save_seen(updated_seen)
    
    # Итоговая статистика
    print(f"🎯 Свободных подходящих слотов: {len(current_free_slots)}")
    print(f"🆕 Новых слотов обнаружено: {len(new_slots_found)}")
    print(f"✅ Успешно уведомлено о слотах: {len(successfully_notified_slots)}")
    print(f"💾 Сохранено виденных слотов: {len(updated_seen)}")
    print(f"{'='*60}\n")


async def monitor_loop(bot: Bot):
    while True:
        try:
            await check_and_notify(bot)
        except Exception as e:
            print(f"Ошибка цикла: {e}")
        await asyncio.sleep(CHECK_INTERVAL_SEC)


async def morning_reminder_loop(bot: Bot):
    global morning_reminder_sent_date
    while True:
        # Используем московское время
        now = datetime.now(MOSCOW_TZ)
        today_str = now.strftime("%Y-%m-%d")

        # Проверяем, что напоминание ещё не отправлялось сегодня
        if morning_reminder_sent_date != today_str:
            assignments = load_assignments()
            # Фильтруем только слоты на СЕГОДНЯ
            today_slots = {
                slot_dt: info
                for slot_dt, info in assignments.items()
                if slot_dt.startswith(today_str)
            }

            if today_slots:
                # Утреннее напоминание приходит строго в 7:00 по московскому времени
                morning_time = datetime.combine(now.date(), dtime(7, 0), tzinfo=MOSCOW_TZ)

                if now >= morning_time:
                    lines = [f"☀️ *Доброе утро! Расписание на сегодня — {now.strftime('%d.%m.%Y')}*\n"]
                    for slot_dt in sorted(today_slots):
                        info = today_slots[slot_dt]
                        slot_time = datetime.fromisoformat(slot_dt).strftime("%H:%M")
                        lines.append(f"⏰ {slot_time} — {info['student']}")

                    text = "\n".join(lines)
                    
                    # Отправляем каждому пользователю из ALLOWED_IDS
                    failed_users = []
                    notified_users = set()  # Отслеживаем, кому уже отправили
                    
                    for user_id in ALLOWED_IDS:
                        try:
                            await bot.send_message(user_id, text, parse_mode="Markdown")
                            notified_users.add(user_id)
                        except Exception as e:
                            failed_users.append((user_id, str(e)))
                    
                    # Отправляем отчет администратору (только если он ещё не получил расписание)
                    if ADMIN_ID and ADMIN_ID not in notified_users:
                        admin_report = f"🌅 *Утреннее расписание на {now.strftime('%d.%m.%Y')}*\n"
                        if failed_users:
                            admin_report += "\n❌ *Ошибки отправки:*\n"
                            for user_id, error in failed_users:
                                admin_report += f"• Пользователь {user_id}: {error}\n"
                        else:
                            admin_report += f"✅ Расписание отправлено всем ({len(ALLOWED_IDS)})"
                        
                        try:
                            await bot.send_message(ADMIN_ID, admin_report, parse_mode="Markdown")
                        except:
                            pass  # Если админу не отправилось, не критично
                    
                    # Помечаем, что сегодня уже отправили (предотвращает дублирование)
                    morning_reminder_sent_date = today_str

        await asyncio.sleep(60)


# ========== КОМАНДЫ ПОЛЬЗОВАТЕЛЯ ==========
async def cb_assign_student(callback: CallbackQuery, callback_data: AssignStudent, state: FSMContext):
    dt = datetime.fromisoformat(callback_data.slot_dt)
    await state.update_data(slot_dt=callback_data.slot_dt)
    await state.set_state(AssignFlow.waiting_name)
    await callback.message.answer(
        f"Слот: *{dt.strftime('%d.%m.%Y')} в {dt.strftime('%H:%M')}*\n\nВведите имя ученика:",
        parse_mode="Markdown",
    )
    await callback.answer()


async def receive_student_name(message: Message, state: FSMContext):
    data = await state.get_data()
    slot_dt: str = data["slot_dt"]
    student_name = (message.text or "").strip()

    if not student_name:
        await message.answer("Имя не может быть пустым. Введите имя ученика:")
        return

    save_assignment(slot_dt, student_name)
    await state.clear()

    dt = datetime.fromisoformat(slot_dt)
    slot_text = (
        f"🎵 Урок: {dt.strftime('%d.%m.%Y')} в {dt.strftime('%H:%M')}\n"
        "Жду тебя!"
    )
    
    await message.answer(
        f"✅ Слот *{dt.strftime('%d.%m.%Y')} {dt.strftime('%H:%M')}* записан за *{student_name}*\n\n"
        f"Текст для ученика (скопируйте и отправьте):\n\n{slot_text}",
        parse_mode="Markdown",
    )


# ========== АДМИН ПАНЕЛЬ ==========
def build_dates_keyboard() -> tuple[str, InlineKeyboardMarkup]:
    assignments = load_assignments()
    if not assignments:
        return "📭 Назначенных уроков пока нет.", InlineKeyboardMarkup(inline_keyboard=[])

    dates = sorted({slot_dt[:10] for slot_dt in assignments})
    rows, row = [], []
    for date_str in dates:
        dt = datetime.fromisoformat(date_str)
        row.append(InlineKeyboardButton(
            text=dt.strftime("%d.%m"),
            callback_data=ViewDate(date=date_str).pack(),
        ))
        if len(row) == 4:
            rows.append(row)
            row = []
    if row:
        rows.append(row)

    return "📅 Выберите дату чтобы посмотреть расписание:", InlineKeyboardMarkup(inline_keyboard=rows)


async def cmd_schedule(message: Message):
    text, kb = build_dates_keyboard()
    await message.answer(text, reply_markup=kb)


async def cb_view_date(callback: CallbackQuery, callback_data: ViewDate):
    assignments = load_assignments()
    date_str = callback_data.date

    day_slots = {
        slot_dt: info
        for slot_dt, info in assignments.items()
        if slot_dt.startswith(date_str)
    }

    back_kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔙 К датам", callback_data=BackToDates().pack())
    ]])

    if not day_slots:
        await callback.message.edit_text("На этот день уроков нет.", reply_markup=back_kb)
        await callback.answer()
        return

    dt_date = datetime.fromisoformat(date_str)
    lines = [f"📅 *{dt_date.strftime('%d.%m.%Y')}*\n"]
    for slot_dt in sorted(day_slots):
        info = day_slots[slot_dt]
        slot_time = datetime.fromisoformat(slot_dt).strftime("%H:%M")
        lines.append(f"⏰ {slot_time} — {info['student']}")

    await callback.message.edit_text(
        "\n".join(lines),
        reply_markup=back_kb,
        parse_mode="Markdown",
    )
    await callback.answer()


async def cb_back_to_dates(callback: CallbackQuery):
    text, kb = build_dates_keyboard()
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()


async def cmd_check(message: Message):
    text = (message.text or "").strip()
    parts = text.split(maxsplit=1)
    date_str = parts[1] if len(parts) > 1 else parts[0]

    dt = parse_user_datetime(date_str)
    if not dt:
        await message.answer("Формат: ДДММ-ЧЧММ (напр. 1503-1430)")
        return

    if not latest_slots_data:
        await message.answer("Свежих данных пока нет...")
        return

    is_free = any(slot["date"] == dt.isoformat(timespec="seconds") for slot in latest_slots_data)

    if is_free:
        date_param = dt.strftime('%Y-%m-%d')
        url = f"https://widget.musbooking.com/?room={ROOM_ID}&date={date_param}"
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🎟 Забронировать", url=url)]])
        await message.answer(f"✅ Слот на {dt.strftime('%d.%m.%Y %H:%M')} свободен!", reply_markup=kb)
    else:
        await message.answer(f"❌ Слота на {dt.strftime('%d.%m.%Y %H:%M')} сейчас нет.")


async def check_filter(message: Message) -> bool:
    text = (message.text or "").lower().strip()
    prefixes = ("/check ", "check ", "чек ", "проверить ")
    if any(text.startswith(p) for p in prefixes): return True
    return "-" in text and " " not in text and parse_user_datetime(text) is not None


async def schedule_filter(message: Message) -> bool:
    text = (message.text or "").lower().strip()
    return text in ("/schedule", "schedule")


# ========== ЗАПУСК ==========
async def main():
    if not BOT_TOKEN or not ALLOWED_IDS or not ROOM_ID or not OPTION_ID:
        raise RuntimeError("Настрой .env: BOT_TOKEN, ALLOWED_IDS, ROOM_ID, OPTION_ID")

    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())

    dp.message.register(cmd_check, F.from_user.id.in_(ALLOWED_IDS), check_filter)
    dp.message.register(cmd_schedule, F.from_user.id.in_(ALLOWED_IDS), schedule_filter)
    dp.message.register(receive_student_name, F.from_user.id.in_(ALLOWED_IDS), AssignFlow.waiting_name)
    dp.callback_query.register(cb_assign_student, AssignStudent.filter(), F.from_user.id.in_(ALLOWED_IDS))
    dp.callback_query.register(cb_view_date, ViewDate.filter(), F.from_user.id.in_(ALLOWED_IDS))
    dp.callback_query.register(cb_back_to_dates, BackToDates.filter(), F.from_user.id.in_(ALLOWED_IDS))

    asyncio.create_task(monitor_loop(bot))
    asyncio.create_task(morning_reminder_loop(bot))
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
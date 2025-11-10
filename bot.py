from telebot import TeleBot, types
from oauth2client.service_account import ServiceAccountCredentials
import gspread
import datetime
from zoneinfo import ZoneInfo
import time
import threading
import os
BOT_TOKEN = os.getenv("BOT_TOKEN")
bot = TeleBot(BOT_TOKEN)
authorized_users = set()
INVITE_CODE = "АРМСПЕЦТЕХ2025"  # ← й код-приглашение

# === Подключение к Google Sheets ===
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds = ServiceAccountCredentials.from_json_keyfile_name("creds.json", scope)
client = gspread.authorize(creds)

# Лайт-вариант: не валим процесс при старте
spreadsheet = None
sheet_tasks = None
sheet_status = None

init_lock = threading.Lock()
_last_init_ts = 0

def init_sheets(retries=5, delay=2, cooldown=60):
    """
    Инициализация spreadsheet/sheet_tasks/sheet_status с ретраями и кулдауном.
    При 429 делаем длинную паузу, чтобы не долбить квоту.
    Возвращает True/False. Исключения наружу не кидает.
    """
    global spreadsheet, sheet_tasks, sheet_status, _last_init_ts

    if sheet_tasks and sheet_status:
        return True

    with init_lock:
        if sheet_tasks and sheet_status:
            return True

        now = time.time()
        # не чаще, чем раз в cooldown секунд
        if now - _last_init_ts < cooldown:
            return False
        _last_init_ts = now

        backoff = delay
        for attempt in range(1, retries + 1):
            try:
                if spreadsheet is None:
                    sheet_name = os.getenv("SHEET_NAME", "Логистика - Тестовая Таблица")
                    spreadsheet = client.open(sheet_name)

                all_sheets = spreadsheet.worksheets()
                st = None
                ss = None

                # быстрый путь по A1
                for ws in all_sheets:
                    try:
                        a1 = ws.acell('A1').value or ""
                    except Exception:
                        a1 = ""
                    if a1 == "Дата":
                        st = st or ws
                    if a1 == "Водитель":
                        ss = ss or ws

                # безопасная эвристика по первой строке
                if not st or not ss:
                    for ws in all_sheets:
                        try:
                            row1 = ws.row_values(1)
                        except Exception:
                            row1 = []
                        r = [str(x).strip().lower() for x in row1]
                        if not st and ("дата" in r and "задача" in r):
                            st = ws
                        if not ss and (len(r) > 0 and r[0] == "водитель"):
                            ss = ws

                if st and ss:
                    sheet_tasks = st
                    sheet_status = ss
                    print("✅ Sheets initialized")
                    return True
                else:
                    print(f"⚠️ Не найдены листы (попытка {attempt}/{retries})")

            except gspread.exceptions.APIError as e:
                print(f"⚠️ init_sheets APIError (попытка {attempt}/{retries}): {e}")
                # если 429 — длинная пауза и выходим (пусть следующий вызов случится через cooldown)
                time.sleep(65)
                return False
            except Exception as e:
                print(f"⚠️ init_sheets ошибка (попытка {attempt}/{retries}):", e)

            time.sleep(backoff)
            backoff = min(backoff * 2, 30)  # экспоненциальный бэкофф

        return False

users = {}
sent_tasks = {}
sent_additional_tasks = {}

def safe_update(a1_range, values, retries=1, delay=1.5):
    for i in range(retries + 1):
        try:
            sheet_tasks.update(a1_range, values)
            return True
        except Exception as e:
            if i == retries:
                print("Update error:", a1_range, e)
                return False
            time.sleep(delay)

def now_hm():
    # Локальное время Киева с учётом переходов
    return datetime.datetime.now(ZoneInfo("Europe/Kyiv")).strftime("%H:%M")

def append_time_log(row, entry):
    """
    Добавляет строку entry в колонку E (построчно).
    Пример entry: 'Задание: 10:13' или 'Доп. задание 2: 13:19'
    """
    try:
        current = sheet_tasks.cell(row, 5).value or ""   # Колонка E = 5
    except Exception as e:
        print("append_time_log read error:", e)
        current = ""

    new_val = entry if not current else f"{current}\n{entry}"
    ok = safe_update(f'E{row}', [[new_val]])
    if not ok:
        try:
            sheet_tasks.update_cell(row, 5, new_val)
        except Exception as e:
            print("append_time_log write fallback:", e)

@bot.message_handler(commands=['start'])
def start(message):
    chat_id = message.chat.id
    if chat_id in authorized_users:
        bot.send_message(chat_id, "Добрый день! Введите Ваше имя:")
        bot.register_next_step_handler(message, register_name)
    else:
        bot.send_message(chat_id, "Введите код-приглашение:")
        bot.register_next_step_handler(message, check_invite_code)

def check_invite_code(message):
    chat_id = message.chat.id
    if message.text.strip() == INVITE_CODE:
        authorized_users.add(chat_id)
        bot.send_message(chat_id, "Код принят. Введите Ваше имя:")
        bot.register_next_step_handler(message, register_name)
    else:
        bot.send_message(chat_id, "Неверный код. Доступ запрещён.")

def register_name(message):
    chat_id = message.chat.id
    name = (message.text or "").strip()

    # сохраняем локально состояние пользователя
    users[chat_id] = {'name': name, 'waiting': True}
    sent_tasks[chat_id] = set()
    sent_additional_tasks[chat_id] = set()

    # Всегда сразу отвечаем пользователю:
    bot.send_message(chat_id, "Ожидайте задание.", reply_markup=types.ReplyKeyboardRemove())

    # Пробуем инициализировать листы (без падений и без блокировки логики)
    ok = init_sheets()

    # Если листы уже доступны — мягко дописываем имя в список водителей
    if ok and name:
        try:
            names_list = sheet_status.col_values(1)
            if name not in names_list:
                sheet_status.append_row([name])
        except Exception as e:
            print("register_name: не удалось записать имя в лист:", e)

        # Можно сразу попытаться выслать задание (а мониторинг всё равно работает)
        try:
            check_and_send_task(chat_id, name)
        except Exception as e:
            print("register_name: check_and_send_task ошибка:", e)

def monitoring_loop():
    while True:
        if sheet_tasks is None or sheet_status is None:
            init_sheets()
            time.sleep(15)   # было 2 — делаем 15, чтобы не жечь квоту
            continue
        try:
            for chat_id, data in users.items():
                if data.get('waiting'):
                    check_and_send_task(chat_id, data['name'])
        except Exception as e:
            print("Ошибка мониторинга:", e)
        time.sleep(5)

def check_and_send_task(chat_id, name):
    today = datetime.datetime.now(ZoneInfo("Europe/Kyiv")).strftime('%Y-%m-%d')
    try:
        records = sheet_tasks.get_all_values()
    except Exception as e:
        print("Sheets unavailable in check_and_send_task:", e)
        return

    header = records[0]
    rows = records[1:]

    for idx, row in enumerate(rows, start=2):
        row_dict = dict(zip(header, row))
        if (
            row_dict.get('Дата', '').strip() == today and
            row_dict.get('Водитель', '').strip() == name and
            row_dict.get('Статус', '').strip() == ''
        ):
            sent_tasks[chat_id].add(idx)
            users[chat_id]['current_row'] = idx
            users[chat_id]['waiting'] = False
            users[chat_id]['eta_provided'] = True
            text = (
                f"Задача: {row_dict.get('Задача', '')}\n"
                f"Машина: {row_dict.get('Машина', '')}\n"
                f"Время по плану: {row_dict.get('Время (по плану)', '')}"
            )
            markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
            markup.add("✅ Взять задание", "❌ Невозможно взять задание")
            bot.send_message(chat_id, text, reply_markup=markup)

            # +++ ЛОГИРУЕМ ВРЕМЯ ПРИХОДА ОСНОВНОГО ЗАДАНИЯ В E{idx}
            try:
                append_time_log(idx, f"Задание: {now_hm()}")
            except Exception as e:
                print("check_and_send_task: append_time_log error:", e)

            return

@bot.message_handler(func=lambda m: m.text in ["✅ Взять задание", "❌ Невозможно взять задание"])
def process_task_choice(message):
    chat_id = message.chat.id
    if chat_id not in users or 'current_row' not in users[chat_id]:
        bot.send_message(chat_id, "Нет активного задания. Ожидайте новое.")
        users.setdefault(chat_id, {})['waiting'] = True
        return
    row = users[chat_id]['current_row']
    if message.text == "✅ Взять задание":
        now_time = datetime.datetime.now(ZoneInfo("Europe/Kyiv")).strftime("%H:%M")
        # F — время принятия, G оставляем пустым (ETA больше не используем), H — статус
        safe_update(f'F{row}:H{row}', [[now_time, '', "В процессе выполнения"]])
        users[chat_id]['eta_provided'] = True  # чтобы логика доп.заданий не ждала ETA
        bot.send_message(chat_id, "Вы приняли задание.", reply_markup=types.ReplyKeyboardRemove())
        show_task_actions(chat_id)
    elif message.text == "❌ Невозможно взять задание":
        bot.send_message(chat_id, "Укажите причину отказа:", reply_markup=types.ReplyKeyboardRemove())
        bot.register_next_step_handler(message, impossible_reason)

def impossible_reason(message):
    chat_id = message.chat.id
    data = users.get(chat_id, {})
    row = data.get('current_row')

    # Если по какой-то причине нет активной строки — мягко выходим
    if not row:
        bot.send_message(chat_id, "Нет активного задания. Ожидайте новое.")
        users.setdefault(chat_id, {})['waiting'] = True
        return

    try:
        reason = (message.text or "").strip()
        driver_name = data.get('name', "")
        timestamp = datetime.datetime.now(ZoneInfo("Europe/Kyiv")).strftime("%H:%M")

        # 1) читаем текущие комментарии (I-столбец = 9)
        try:
            current_comment = sheet_tasks.cell(row, 9).value or ""
        except Exception as e:
            print("impossible_reason: read comment error:", e)
            current_comment = ""

        new_comment = (
            f"{current_comment}\n{driver_name} ({timestamp}): {reason}"
            if current_comment else
            f"{driver_name} ({timestamp}): {reason}"
        )

        # 2) пишем комментарий безопасно (I{row})
        ok_comment = safe_update(f'I{row}', [[new_comment]])
        if not ok_comment:
            try:
                sheet_tasks.update_cell(row, 9, new_comment)
            except Exception as e:
                print("impossible_reason: update comment fallback error:", e)

        # 3) очищаем Водителя и Машину (B и C), а также статус (H) на всякий случай
        safe_update(f'B{row}:C{row}', [["", ""]])
        safe_update(f'H{row}', [[""]])

        bot.send_message(chat_id, "Комментарий сохранён. Задание возвращено в общий список.")
    except Exception as e:
        # Не даём хендлеру «упасть» — обязательно отвечаем пользователю
        print("impossible_reason unexpected error:", e)
        bot.send_message(chat_id, "Сталася помилка при сохранении причины. Попробуйте ещё раз.")
    finally:
        # В любом случае возвращаем пользователя в состояние ожидания
        sent_tasks[chat_id] = set()
        sent_additional_tasks[chat_id] = set()
        users[chat_id]['waiting'] = True
        users[chat_id].pop('current_row', None)
        bot.send_message(chat_id, "Ожидайте задания.", reply_markup=types.ReplyKeyboardRemove())

def show_task_actions(chat_id):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add("✅ Задание выполнено", "❌ Невозможно выполнить", "💬 Комментарий")
    bot.send_message(chat_id, "Выберите дальнейшее действие:", reply_markup=markup)

@bot.message_handler(func=lambda m: m.text in ["✅ Задание выполнено", "❌ Невозможно выполнить", "💬 Комментарий"])
def process_task_action(message):
    chat_id = message.chat.id
    if chat_id not in users or 'current_row' not in users[chat_id]:
        bot.send_message(chat_id, "Нет активного задания. Ожидайте новое.")
        users.setdefault(chat_id, {})['waiting'] = True
        return
    row = users[chat_id]['current_row']
    name = users[chat_id]['name']
    if message.text == "✅ Задание выполнено":
        now_time = datetime.datetime.now(ZoneInfo("Europe/Kyiv")).strftime("%H:%M")
        safe_update(f'J{row}', [[now_time]])
        safe_update(f'H{row}', [["Выполнено"]])
        bot.send_message(chat_id, "Задание отмечено как выполненное.")
        bot.send_message(chat_id, "Ожидайте задание.", reply_markup=types.ReplyKeyboardRemove())
        users[chat_id]['waiting'] = True
        sent_tasks[chat_id] = set()
        sent_additional_tasks[chat_id] = set()           # ← сброс доп. заданий
        users[chat_id].pop('current_row', None)          # ← очистка текущей строки
        check_and_send_task(chat_id, name)
    elif message.text == "❌ Невозможно выполнить":
        bot.send_message(chat_id, "Укажите причину:", reply_markup=types.ReplyKeyboardRemove())
        bot.register_next_step_handler(message, fail_reason)
    elif message.text == "💬 Комментарий":
        bot.send_message(chat_id, "Введите комментарий:", reply_markup=types.ReplyKeyboardRemove())
        bot.register_next_step_handler(message, add_comment)

def fail_reason(message):
    chat_id = message.chat.id
    row = users[chat_id]['current_row']
    reason = message.text.strip()
    driver_name = users[chat_id]['name']
    timestamp = datetime.datetime.now(ZoneInfo("Europe/Kyiv")).strftime("%H:%M")

    current_comment = sheet_tasks.cell(row, 9).value or ""
    new_comment = f"{current_comment}\n{driver_name} ({timestamp}): {reason}" if current_comment else f"{driver_name} ({timestamp}): {reason}"

    # 1. Сохраняем комментарий
    sheet_tasks.update_cell(row, 9, new_comment)

    # 2. Очищаем поля B:G + H (Водитель, Машина, Задача, Время по плану, Время принятия, Время выполнения, Статус)
    safe_update(f'B{row}:H{row}', [["", "", "", "", "", "", ""]])

    # 3. Обновляем статус водителя
    bot.send_message(chat_id, "Комментарий сохранён. Задание возвращено в общий список.")
    sent_tasks[chat_id] = set()
    users[chat_id]['waiting'] = True
    sent_additional_tasks[chat_id] = set()
    users[chat_id].pop('current_row', None)
    bot.send_message(chat_id, "Ожидайте задания.")

def add_comment(message):
    chat_id = message.chat.id
    row = users[chat_id]['current_row']
    comment = message.text.strip()
    driver_name = users[chat_id]['name']
    timestamp = datetime.datetime.now(ZoneInfo("Europe/Kyiv")).strftime("%H:%M")
    current_comment = sheet_tasks.cell(row, 9).value or ""
    new_comment = f"{current_comment}\n{driver_name} ({timestamp}): {comment}" if current_comment else f"{driver_name} ({timestamp}): {comment}"
    sheet_tasks.update_cell(row, 9, new_comment)
    bot.send_message(chat_id, "Комментарий добавлен.")
    show_task_actions(chat_id)

def monitor_additional():
    while True:
        try:
            if sheet_tasks is None or sheet_status is None:
                init_sheets()
                time.sleep(15)   # было 2 — ставим 15
                continue

            for chat_id, data in users.items():
                if not data.get('waiting') and 'current_row' in data:
                    row = data['current_row']
                    task_status = sheet_tasks.cell(row, 8).value or ""  # Колонка H (статус)

                    if task_status.strip() != "В процессе выполнения":
                        continue  # Пропускаем, если основное задание ещё не взято

                    if chat_id not in sent_additional_tasks:
                        sent_additional_tasks[chat_id] = set()

                    for col_idx, label in [(11, 'Доп. задание 1'), (13, 'Доп. задание 2'), (15, 'Доп. задание 3')]:
                        task = sheet_tasks.cell(row, col_idx).value
                        task_status = sheet_tasks.cell(row, col_idx + 1).value or ""
                        task_key = f"{row}_{col_idx}"

                        if task and not task_status and task_key not in sent_additional_tasks[chat_id]:
                            markup = types.InlineKeyboardMarkup()
                            markup.add(
                                types.InlineKeyboardButton(text="Принять", callback_data=f"accept_{row}_{col_idx}"),
                                types.InlineKeyboardButton(text="Отказаться", callback_data=f"reject_{row}_{col_idx}")
                            )
                            bot.send_message(chat_id, f"{label}:\n{task}", reply_markup=markup)

                            # +++ ЛОГИРУЕМ ВРЕМЯ ПРИХОДА ДОП. ЗАДАНИЯ В E{row}
                            try:
                                append_time_log(row, f"{label}: {now_hm()}")
                            except Exception as e:
                                print("monitor_additional: append_time_log error:", e)

                            sent_additional_tasks[chat_id].add(task_key)
        except Exception as e:
            print("Ошибка в мониторинге доп. заданий:", e)
        time.sleep(12)

@bot.callback_query_handler(func=lambda call: call.data.startswith("accept_") or call.data.startswith("reject_"))
def handle_additional_task(call):
    chat_id = call.message.chat.id
    driver = users[chat_id]['name']
    parts = call.data.split('_')
    action, row, col = parts[0], int(parts[1]), int(parts[2])
    timestamp = datetime.datetime.now(ZoneInfo("Europe/Kyiv")).strftime("%H:%M")

    if action == "accept":
        sheet_tasks.update_cell(row, col + 1, f"{driver} принял задание в {timestamp}")
        bot.answer_callback_query(call.id, text="Задание принято")
        bot.edit_message_reply_markup(chat_id=chat_id, message_id=call.message.message_id, reply_markup=None)

    elif action == "reject":
        users[chat_id]['reject_context'] = {'row': row, 'col': col, 'driver': driver}
        bot.send_message(chat_id, "Укажите причину отказа:")
        bot.register_next_step_handler(call.message, process_reject_comment)
        bot.answer_callback_query(call.id)
        bot.edit_message_reply_markup(chat_id=chat_id, message_id=call.message.message_id, reply_markup=None)

def process_reject_comment(message):
    chat_id = message.chat.id
    context = users[chat_id].get('reject_context')
    if not context:
        bot.send_message(chat_id, "Произошла ошибка при сохранении причины отказа.")
        return
    row = context['row']
    col = context['col']
    driver = context['driver']
    timestamp = datetime.datetime.now(ZoneInfo("Europe/Kyiv")).strftime("%H:%M")
    reason = message.text.strip()
    value = f"{driver} ({timestamp}): {reason}"
    sheet_tasks.update_cell(row, col + 1, value)
    bot.send_message(chat_id, "Причина отказа сохранена.")
    del users[chat_id]['reject_context']
def reject_reason(message, row, col, driver):
    reason = message.text.strip()
    timestamp = datetime.datetime.now(ZoneInfo("Europe/Kyiv")).strftime("%H:%M")
    sheet_tasks.update_cell(row, col + 1, f"{driver} ({timestamp}): {reason}")
    bot.send_message(message.chat.id, "Комментарий сохранён.")

# Перед запуском потоков — разовая попытка инициализации листов
init_sheets()

monitor_thread = threading.Thread(target=monitoring_loop, daemon=True)
monitor_thread.start()

monitor_additional_thread = threading.Thread(target=monitor_additional, daemon=True)
monitor_additional_thread.start()

from flask import Flask, request

app = Flask(__name__)

SPAM_KEYWORDS = [
    "http", "https", "bit.ly", "t.me", "porn", "sex", "casino", "join", 
    "xxx", "onlyfans", "порно", "казино", "вебкам", "эротика"
]

@bot.message_handler(func=lambda m: getattr(m, 'text', None)
                                  and not m.text.startswith('/start')
                                  and (m.chat.id not in authorized_users))
def block_unauthorized(message):
    chat_id = message.chat.id
    text = message.text.lower() if message.text else ""

    if any(w in text for w in SPAM_KEYWORDS):
        print(f"❌ Спам заблокирован от {chat_id}")
        return

    if message.chat.id not in authorized_users:
        print(f"⛔️ Блок: неавторизованный пользователь {message.chat.id}")
        return

@bot.message_handler(content_types=['photo', 'video', 'document'])
def block_media(message):
    if message.chat.id not in authorized_users:
        return
    if message.caption and any(w in message.caption.lower() for w in SPAM_KEYWORDS):
        print(f"❌ Спам с caption от {message.chat.id}")
        return

@app.route('/', methods=['POST'])
def webhook():
    json_str = request.get_data().decode('UTF-8')
    update = types.Update.de_json(json_str)
    bot.process_new_updates([update])
    return '!', 200

@app.route('/', methods=['GET'])
def index():
    return "Bot is running!", 200

if __name__ == '__main__':
    bot.remove_webhook()
    time.sleep(1)
    bot.set_webhook(url='https://logistics-bot-nnxy.onrender.com')
    app.run(host="0.0.0.0", port=10000)

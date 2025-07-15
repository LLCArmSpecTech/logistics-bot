from telebot import TeleBot, types
from oauth2client.service_account import ServiceAccountCredentials
import gspread
import datetime
import time
import threading

# === Подключение к Google Sheets ===
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds = ServiceAccountCredentials.from_json_keyfile_name("creds.json", scope)
client = gspread.authorize(creds)
spreadsheet = client.open("Логистика - Тестовая Таблица")

# Определяем листы
all_sheets = spreadsheet.worksheets()
sheet_tasks = None
sheet_status = None

for ws in all_sheets:
    header = ws.acell('A1').value
    if header == "Дата":
        sheet_tasks = ws
    elif header == "Водитель":
        sheet_status = ws

if not sheet_tasks or not sheet_status:
    raise Exception("Не удалось найти листы. Убедись, что в A1 на одном листе 'Дата', на другом — 'Водитель'.")

bot = TeleBot("8085053958:AAHhjYpYWWuCxiynpoeCxAoz0conVeE_17w")

monitor_started = False

users = {}
sent_tasks = {}
sent_additional_tasks = {}

@bot.message_handler(commands=['start'])
def start(message):
    chat_id = message.chat.id
    bot.send_message(chat_id, "Добрый день! Введите Ваше имя:")
    bot.register_next_step_handler(message, register_name)

def register_name(message):
    chat_id = message.chat.id
    name = message.text.strip()
    users[chat_id] = {'name': name, 'waiting': True}
    names_list = sheet_status.col_values(1)
    if name not in names_list:
        sheet_status.append_row([name])
    sent_tasks[chat_id] = set()
    sent_additional_tasks[chat_id] = set()
    bot.send_message(chat_id, "Ожидайте задания.")

def monitoring_loop():
    while True:
        try:
            for chat_id, data in users.items():
                if data.get('waiting'):
                    check_and_send_task(chat_id, data['name'])
        except Exception as e:
            print("Ошибка мониторинга:", e)
        time.sleep(3)

def check_and_send_task(chat_id, name):
    today = datetime.datetime.now().strftime('%Y-%m-%d')
    records = sheet_tasks.get_all_values()
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
            text = (
                f"Задача: {row_dict.get('Задача', '')}\n"
                f"Машина: {row_dict.get('Машина', '')}\n"
                f"Время по плану: {row_dict.get('Время (по плану)', '')}"
            )
            markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
            markup.add("✅ Взять задание", "❌ Невозможно взять задание")
            bot.send_message(chat_id, text, reply_markup=markup)
            return

@bot.message_handler(func=lambda m: m.text in ["✅ Взять задание", "❌ Невозможно взять задание"])
def process_task_choice(message):
    chat_id = message.chat.id
    row = users[chat_id]['current_row']
    if message.text == "✅ Взять задание":
        now_time = (datetime.datetime.utcnow() + datetime.timedelta(hours=3)).strftime("%H:%M")
        sheet_tasks.update(f'F{row}:H{row}', [[now_time, '', "В процессе выполнения"]])
        bot.send_message(chat_id, "Вы успешно приняли задание. Сколько времени нужно на выполнение?")
        bot.register_next_step_handler(message, save_eta)
    elif message.text == "❌ Невозможно взять задание":
        bot.send_message(chat_id, "Укажите причину отказа:")
        bot.register_next_step_handler(message, impossible_reason)

def save_eta(message):
    chat_id = message.chat.id
    row = users[chat_id]['current_row']
    eta = message.text.strip()
    sheet_tasks.update(f'G{row}', [[eta]])
    show_task_actions(chat_id)

def impossible_reason(message):
    chat_id = message.chat.id
    row = users[chat_id]['current_row']
    reason = message.text.strip()
    driver_name = users[chat_id]['name']
    timestamp = (datetime.datetime.utcnow() + datetime.timedelta(hours=3)).strftime("%H:%M")
    current_comment = sheet_tasks.cell(row, 9).value or ""
    new_comment = f"{current_comment}\n{driver_name} ({timestamp}): {reason}" if current_comment else f"{driver_name} ({timestamp}): {reason}"
    sheet_tasks.update_cell(row, 9, new_comment)
    sheet_tasks.update(f'B{row}', [[""]])
    bot.send_message(chat_id, "Комментарий сохранён. Задание возвращено в общий список.")
    sent_tasks[chat_id] = set()
    users[chat_id]['waiting'] = True
    bot.send_message(chat_id, "Ожидайте задания.")

def show_task_actions(chat_id):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add("✅ Задание выполнено", "❌ Невозможно выполнить", "💬 Комментарий")
    bot.send_message(chat_id, "Выберите дальнейшее действие:", reply_markup=markup)

@bot.message_handler(func=lambda m: m.text in ["✅ Задание выполнено", "❌ Невозможно выполнить", "💬 Комментарий"])
def process_task_action(message):
    chat_id = message.chat.id
    row = users[chat_id]['current_row']
    name = users[chat_id]['name']
    if message.text == "✅ Задание выполнено":
        now_time = (datetime.datetime.utcnow() + datetime.timedelta(hours=3)).strftime("%H:%M")
        sheet_tasks.update(f'J{row}', [[now_time]])
        sheet_tasks.update(f'H{row}', [["Выполнено"]])
        bot.send_message(chat_id, "Задание отмечено как выполненное.")
        bot.send_message(chat_id, "Ожидайте задание.")
        users[chat_id]['waiting'] = True
        sent_tasks[chat_id] = set()
        check_and_send_task(chat_id, name)
    elif message.text == "❌ Невозможно выполнить":
        bot.send_message(chat_id, "Укажите причину:")
        bot.register_next_step_handler(message, fail_reason)
    elif message.text == "💬 Комментарий":
        bot.send_message(chat_id, "Введите комментарий:")
        bot.register_next_step_handler(message, add_comment)

def fail_reason(message):
    chat_id = message.chat.id
    row = users[chat_id]['current_row']
    reason = message.text.strip()
    driver_name = users[chat_id]['name']
    timestamp = (datetime.datetime.utcnow() + datetime.timedelta(hours=3)).strftime("%H:%M")

    current_comment = sheet_tasks.cell(row, 9).value or ""
    new_comment = f"{current_comment}\n{driver_name} ({timestamp}): {reason}" if current_comment else f"{driver_name} ({timestamp}): {reason}"

    # 1. Сохраняем комментарий
    sheet_tasks.update_cell(row, 9, new_comment)

    # 2. Очищаем поля B:G + H (Водитель, Машина, Задача, Время по плану, Время принятия, Время выполнения, Статус)
    sheet_tasks.update(f'B{row}:H{row}', [["", "", "", "", "", "", ""]])

    # 3. Обновляем статус водителя
    bot.send_message(chat_id, "Комментарий сохранён. Задание возвращено в общий список.")
    sent_tasks[chat_id] = set()
    users[chat_id]['waiting'] = True
    bot.send_message(chat_id, "Ожидайте задания.")

def add_comment(message):
    chat_id = message.chat.id
    row = users[chat_id]['current_row']
    comment = message.text.strip()
    driver_name = users[chat_id]['name']
    timestamp = (datetime.datetime.utcnow() + datetime.timedelta(hours=3)).strftime("%H:%M")
    current_comment = sheet_tasks.cell(row, 9).value or ""
    new_comment = f"{current_comment}\n{driver_name} ({timestamp}): {comment}" if current_comment else f"{driver_name} ({timestamp}): {comment}"
    sheet_tasks.update_cell(row, 9, new_comment)
    bot.send_message(chat_id, "Комментарий добавлен.")
    show_task_actions(chat_id)

def monitor_additional():
    while True:
        try:
            for chat_id, data in users.items():
                if not data.get('waiting') and 'current_row' in data:
                    row = data['current_row']
                    
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
                            sent_additional_tasks[chat_id].add(task_key)
        except Exception as e:
            print("Ошибка в мониторинге доп. заданий:", e)
        time.sleep(10)

@bot.callback_query_handler(func=lambda call: call.data.startswith("accept_") or call.data.startswith("reject_"))
def handle_additional_task(call):
    chat_id = call.message.chat.id
    driver = users[chat_id]['name']
    parts = call.data.split('_')
    action, row, col = parts[0], int(parts[1]), int(parts[2])
    timestamp = (datetime.datetime.utcnow() + datetime.timedelta(hours=3)).strftime("%H:%M")

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
    timestamp = (datetime.datetime.utcnow() + datetime.timedelta(hours=3)).strftime("%H:%M")
    reason = message.text.strip()
    value = f"{driver} ({timestamp}): {reason}"
    sheet_tasks.update_cell(row, col + 1, value)
    bot.send_message(chat_id, "Причина отказа сохранена.")
    del users[chat_id]['reject_context']
def reject_reason(message, row, col, driver):
    reason = message.text.strip()
    timestamp = (datetime.datetime.utcnow() + datetime.timedelta(hours=3)).strftime("%H:%M")
    sheet_tasks.update_cell(row, col + 1, f"{driver} ({timestamp}): {reason}")
    bot.send_message(message.chat.id, "Комментарий сохранён.")

from flask import Flask, request

app = Flask(__name__)

@app.route('/', methods=['POST'])
def webhook():
    global monitor_started
    json_str = request.get_data().decode('UTF-8')
    update = types.Update.de_json(json_str)
    bot.process_new_updates([update])

    if not monitor_started:
        print("🔁 Перезапуск фоновых потоков")
        threading.Thread(target=monitoring_loop, daemon=True).start()
        threading.Thread(target=monitor_additional, daemon=True).start()
        monitor_started = True

    return '!', 200

@app.route('/', methods=['GET'])
def index():
    return "Bot is running!", 200

if __name__ == '__main__':
    bot.remove_webhook()
    time.sleep(1)
    bot.set_webhook(url='https://logistics-bot-nnxy.onrender.com')
    app.run(host="0.0.0.0", port=10000)
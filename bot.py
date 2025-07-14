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

# === Telegram Bot ===
bot = TeleBot("8085053958:AAHhjYpYWWuCxiynpoeCxAoz0conVeE_17w")

# Хранилище пользователей
users = {}
additional_task_pending = {}
sent_tasks = {}
sent_additional_tasks = {}

# Команда старт
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

# Мониторинг задач
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
    tasks = sheet_tasks.get_all_records()
    for idx, row in enumerate(tasks, start=2):
        if row['Дата'] == today and row['Водитель'] == name and not row['Статус']:
            task_id = idx
            if task_id not in sent_tasks.get(chat_id, set()):
                sent_tasks[chat_id].add(task_id)
                users[chat_id]['current_row'] = idx
                users[chat_id]['waiting'] = False
                text = (f"Задача: {row['Задача']}\n"
                        f"Машина: {row['Машина']}\n"
                        f"Время по плану: {row['Время (по плану)']}")
                markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
                markup.add("✅ Взять задание", "❌ Невозможно взять задание")
                bot.send_message(chat_id, text, reply_markup=markup)
            return

# Мониторинг до 3 дополнительных заданий
def monitor_additional():
    while True:
        try:
            for chat_id, data in users.items():
                if not data.get('waiting'):
                    row = data['current_row']
                    tasks = {
                        1: {'task_col': 11, 'result_col': 12},  # J, L
                        2: {'task_col': 13, 'result_col': 14},  # M, N
                        3: {'task_col': 15, 'result_col': 16}   # O, P
                    }

                    for i, conf in tasks.items():
                        task_text = sheet_tasks.cell(row, conf['task_col']).value
                        if not task_text:
                            continue

                        task_text = task_text.strip()
                        result = sheet_tasks.cell(row, conf['result_col']).value

                        key = (chat_id, i)  # указываем номер задания для исключений
                        if result or key in sent_additional_tasks:
                            continue  # Уже выполнено или отказано

                        # Отправляем задание
                        markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
                        markup.add("✅ Принять доп. задание", "❌ Отказаться от доп. задания")
                        bot.send_message(chat_id, f"Дополнительное задание #{i}:\n{task_text}", reply_markup=markup)

                        additional_task_pending[chat_id] = {
                            'row': row,
                            'col': conf['result_col'],
                            'comment_col': conf['result_col'] - 1,
                            'task_number': i
                        }
                        sent_additional_tasks.add(key)
                        break  # Отправляем по одному за раз
        except Exception as e:
            print("Ошибка в мониторинге дополнительного задания:", e)

        time.sleep(3)

# Обработка принятия/отказа от задания
@bot.message_handler(func=lambda m: m.text in ["✅ Принять доп. задание", "❌ Отказаться от доп. задания"])
def handle_additional_task_response(message):
    chat_id = message.chat.id
    if chat_id not in additional_task_pending:
        bot.send_message(chat_id, "Нет активного дополнительного задания.")
        return

    row = additional_task_pending[chat_id]['row']
    result_col = additional_task_pending[chat_id]['col']
    comment_col = additional_task_pending[chat_id]['comment_col']
    task_number = additional_task_pending[chat_id]['task_number']
    now_time = (datetime.datetime.utcnow() + datetime.timedelta(hours=3)).strftime("%H:%M")
    driver_name = users[chat_id]['name']

    if message.text == "✅ Принять доп. задание":
        sheet_tasks.update_cell(row, result_col, f"Водитель принял доп. задание {task_number} в {now_time}")
        bot.send_message(chat_id, f"Доп. задание {task_number} принято.")
        del additional_task_pending[chat_id]
        show_task_actions(chat_id)
    else:
        bot.send_message(chat_id, "Укажите причину отказа:")
        bot.register_next_step_handler(message, handle_additional_task_decline_reason)

def save_eta(message):
    chat_id = message.chat.id
    row = users[chat_id]['current_row']
    eta = message.text.strip()
    sheet_tasks.update(f'G{row}', [[eta]])
    show_task_actions(chat_id)
    if chat_id in additional_task_pending:
        markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
        markup.add("✅ Принять доп. задание", "❌ Отказаться от доп. задания")
        bot.send_message(chat_id, f"Дополнительное задание:\n{additional_task_pending[chat_id]['text']}", reply_markup=markup)


def impossible_reason(message):
    chat_id = message.chat.id
    row = users[chat_id]['current_row']
    reason = message.text.strip()
    driver_name = users[chat_id]['name']
    current_comment = sheet_tasks.cell(row, 9).value or ""
    now_time = (datetime.datetime.utcnow() + datetime.timedelta(hours=3)).strftime("%H:%M")
    new_comment = f"{current_comment}\n{driver_name} ({now_time}): {reason}" if current_comment else f"{driver_name} ({now_time}): {reason}"
    sheet_tasks.update(f'I{row}', [[new_comment]])
    sheet_tasks.update(f'B{row}', [[""]])
    bot.send_message(chat_id, "Комментарий сохранён. Задание возвращено в общий список.")
    sent_tasks[chat_id] = set()
    users[chat_id]['waiting'] = True
    bot.send_message(chat_id, "Ожидайте задания.")

# Кнопки после принятия задания
def show_task_actions(chat_id):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add("✅ Задание выполнено", "❌ Невозможно выполнить", "💬 Комментарий")
    bot.send_message(chat_id, "Выберите дальнейшее действие:", reply_markup=markup)

# Обработка дальнейших действий
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
        sent_tasks[chat_id] = set()  # 🔧 очищаем список отправленных
    elif message.text == "❌ Невозможно выполнить":
        bot.send_message(chat_id, "Укажите причину:")
        bot.register_next_step_handler(message, fail_reason)
    elif message.text == "💬 Комментарий":
        bot.send_message(chat_id, "Введите комментарий:")
        bot.register_next_step_handler(message, add_comment)

@bot.message_handler(func=lambda m: m.text in ["✅ Принять доп. задание", "❌ Отказаться от доп. задания"])
def handle_additional_task_response(message):
    chat_id = message.chat.id
    if chat_id not in additional_task_pending:
        bot.send_message(chat_id, "Нет активного дополнительного задания.")
        return

    row = additional_task_pending[chat_id]['row']
    now_time = (datetime.datetime.utcnow() + datetime.timedelta(hours=3)).strftime("%H:%M")

    if message.text == "✅ Принять доп. задание":
        sheet_tasks.update(f'L{row}', [[f"Водитель принял задание в {now_time}"]])
        bot.send_message(chat_id, "Дополнительное задание принято.")
        del additional_task_pending[chat_id]
    else:
        bot.send_message(chat_id, "Укажите причину отказа:")
        bot.register_next_step_handler(message, handle_additional_task_decline_reason)

def fail_reason(message):
    chat_id = message.chat.id
    row = users[chat_id]['current_row']
    reason = message.text.strip()
    driver_name = users[chat_id]['name']
    current_comment = sheet_tasks.cell(row, 9).value or ""
    now_time = (datetime.datetime.utcnow() + datetime.timedelta(hours=3)).strftime("%H:%M")
    new_comment = f"{current_comment}\n{driver_name} ({now_time}): {reason}" if current_comment else f"{driver_name} ({now_time}): {reason}"
    sheet_tasks.update(f'I{row}', [[new_comment]])
    sheet_tasks.update(f'B{row}', [[""]])
    sheet_tasks.update(f'H{row}', [[""]])
    sheet_tasks.update(f'F{row}', [[""]])
    sheet_tasks.update(f'G{row}', [[""]])
    bot.send_message(chat_id, "Комментарий сохранён. Задание возвращено в общий список.")
    sent_tasks[chat_id] = set()
    users[chat_id]['waiting'] = True
    bot.send_message(chat_id, "Ожидайте задания.")

def handle_additional_task_decline_reason(message):
    chat_id = message.chat.id
    if chat_id not in additional_task_pending:
        bot.send_message(chat_id, "Доп. задание уже обработано.")
        return

    row = additional_task_pending[chat_id]['row']
    result_col = additional_task_pending[chat_id]['col']
    comment_col = additional_task_pending[chat_id]['comment_col']
    reason = message.text.strip()
    now_time = (datetime.datetime.utcnow() + datetime.timedelta(hours=3)).strftime("%H:%M")
    driver_name = users[chat_id]['name']

    # Комментарий в comment_col (M или O), статус отказа — в N или P
    sheet_tasks.update_cell(row, comment_col, f"{driver_name} ({now_time}): {reason}")
    sheet_tasks.update_cell(row, result_col, f"Отказ от доп. задания в {now_time}")
    bot.send_message(chat_id, "Отказ от доп. задания зафиксирован.")
    del additional_task_pending[chat_id]
    show_task_actions(chat_id)

def add_comment(message):
    chat_id = message.chat.id
    row = users[chat_id]['current_row']
    reason = message.text.strip()
    driver_name = users[chat_id]['name']
    current_comment = sheet_tasks.cell(row, 9).value or ""
    now_time = (datetime.datetime.utcnow() + datetime.timedelta(hours=3)).strftime("%H:%M")
    new_comment = f"{current_comment}\n{driver_name} ({now_time}): {reason}" if current_comment else f"{driver_name} ({now_time}): {reason}"
    sheet_tasks.update(f'I{row}', [[new_comment]])
    bot.send_message(chat_id, "Комментарий сохранён.")

# Запуск мониторингов
monitor_thread = threading.Thread(target=monitoring_loop, daemon=True)
monitor_thread.start()

monitor_additional_thread = threading.Thread(target=monitor_additional, daemon=True)
monitor_additional_thread.start()

from flask import Flask, request

app = Flask(__name__)

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
    # Замените URL ниже на ваш URL от Render, например: https://logistics-bot-nnxy.onrender.com
    webhook_url = 'https://logistics-bot-nnxy.onrender.com'

    bot.remove_webhook()
    time.sleep(1)
    bot.set_webhook(url=webhook_url)

    app.run(host="0.0.0.0", port=10000)

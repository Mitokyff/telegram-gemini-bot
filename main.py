import telegram
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes, CommandHandler, ConversationHandler
import google.generativeai as genai
import asyncio
import os
import requests
import uuid
from google.api_core.exceptions import ResourceExhausted
import sqlite3

# --- Konfiguration ---
TELEGRAM_TOKEN = "7463890992:AAGHWvIR-XzO-VCdBR5bEr-UbIY6XcKI30I"
GEMINI_API_KEY = "AIzaSyAnnsWGT0ykhKWAT9ryWN-esPhW-RMTYa8"

# --- Gemini API-Initialisierung ---
genai.configure(api_key=GEMINI_API_KEY)

# --- Liste der Modelle für den Fallback ---
# Die Reihenfolge ist wichtig: Der Bot versucht, die Modelle in dieser Reihenfolge zu verwenden.
MODELS = [
    "gemini-2.5-flash", 
    "gemini-1.5-flash",
    "gemini-1.5-pro",  
    "gemini-2.5-pro" 
]

# --- Globaler Speicher für Benutzer-Chat-Sitzungen ---
user_chat_sessions = {}

# --- Fallback-Funktion zur Inhaltsgenerierung ---
def generate_content_with_fallback(prompt_parts, current_model_index=0, return_model_instance=False):
    if current_model_index >= len(MODELS):
        raise Exception("Das Kontingent aller verfügbaren Modelle ist ausgeschöpft oder sie sind nicht verfügbar.")

    model_name = MODELS[current_model_index]
    try:
        print(f"Verwende Modell: {model_name}")
        model_instance = genai.GenerativeModel(model_name)
        
        if return_model_instance:
            # Überprüfe die Verfügbarkeit des Modells, bevor die Instanz für die Sitzung zurückgegeben wird
            test_response = model_instance.generate_content("test", stream=False) 
            _ = test_response.text 
            return model_instance 
        else:
            response = model_instance.generate_content(prompt_parts)
            return response.text
            
    except ResourceExhausted as e:
        print(f"Kontingent für {model_name} ausgeschöpft, wechsle zum nächsten Modell...")
        return generate_content_with_fallback(prompt_parts, current_model_index + 1, return_model_instance)
    except Exception as e:
        print(f"Fehler bei Modell {model_name}: {e}. Versuche nächstes...")
        return generate_content_with_fallback(prompt_parts, current_model_index + 1, return_model_instance)

# --- Konstanten ---
MAX_MESSAGE_LENGTH = 4096
USER_DATA_FILE = 'user_data.txt'
DATABASE = 'todo_list.db'

ADD_TASK = 0
DONE_TASK = 1
DELETE_TASK = 2
# --- Hilfsfunktionen ---
def load_known_users():
    if not os.path.exists(USER_DATA_FILE):
        return set()
    with open(USER_DATA_FILE, 'r') as f:
        return set(int(line.strip()) for line in f if line.strip().isdigit())

def save_new_user(user_id):
    with open(USER_DATA_FILE, 'a') as f:
        f.write(f"{user_id}\n")

KNOWN_USERS = load_known_users()

async def send_long_message(update: Update, text: str):
    if not text:
        return
    
    parts = []
    while len(text) > 0:
        if len(text) > MAX_MESSAGE_LENGTH:
            part = text[:MAX_MESSAGE_LENGTH]
            last_newline = part.rfind('\n')
            if last_newline != -1:
                part = text[:last_newline]
                text = text[last_newline+1:]
            else:
                text = text[MAX_MESSAGE_LENGTH:]
            parts.append(part)
        else:
            parts.append(text)
            break
            
    for part in parts:
        try:
            await update.message.reply_text(part)
            await asyncio.sleep(0.5) 
        except Exception as e:
            print(f"Fehler beim Senden eines Nachrichtenteils: {e}")
            await asyncio.sleep(1) 

# --- Befehls-Handler ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_name = update.effective_user.first_name
    
    # Erstelle die Schaltflächen für die Tastatur
    keyboard = [
    ["/list", "/add"],
    ["/done", "/delete"]
]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

    if user_id not in KNOWN_USERS:
        await update.message.reply_text(
            f"Hallo, {user_name}! Willkommen in meinem Bot.\n\n"
            "Du kannst mir einen Text, eine Sprachnachricht oder ein Foto senden.\n\n"
            "Ich habe auch eine Aufgabenliste. Verwende die Schaltflächen unten, um sie zu verwalten.",
            reply_markup=reply_markup
        )
        save_new_user(user_id)
        KNOWN_USERS.add(user_id)
    else:
        await update.message.reply_text(
            f"Willkommen zurück, {user_name}! Wie kann ich dir noch helfen?\n\n"
            "Schick mir einen Text, eine Sprachnachricht oder ein Foto.\n\n"
            "Oder verwende die Schaltflächen, um die Aufgabenliste zu verwalten.",
            reply_markup=reply_markup
        )

# --- SQL-DB-Funktionen für die Aufgabenliste ---
def create_table():
    conn = sqlite3.connect(DATABASE)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY,
            user_id INTEGER,
            task TEXT NOT NULL,
            status TEXT NOT NULL
        )
    ''')
    conn.commit()
    conn.close()

def add_task(user_id, task):
    conn = sqlite3.connect(DATABASE)
    cursor = conn.cursor()
    cursor.execute("INSERT INTO tasks (user_id, task, status) VALUES (?, ?, ?)", (user_id, task, 'pending'))
    conn.commit()
    conn.close()

def get_tasks(user_id):
    conn = sqlite3.connect(DATABASE)
    cursor = conn.cursor()
    cursor.execute("SELECT id, task, status FROM tasks WHERE user_id = ?", (user_id,))
    tasks = cursor.fetchall()
    conn.close()
    return tasks

def complete_task(user_id, task_id):
    conn = sqlite3.connect(DATABASE)
    cursor = conn.cursor()
    # Stelle sicher, dass der Benutzer nur SEINE Aufgabe als erledigt markieren kann
    cursor.execute("UPDATE tasks SET status = 'completed' WHERE user_id = ? AND id = ?", (user_id, task_id))
    conn.commit()
    rows_affected = cursor.rowcount
    conn.close()
    return rows_affected > 0

def delete_task(user_id, task_id):
    conn = sqlite3.connect(DATABASE)
    cursor = conn.cursor()
    # Stelle sicher, dass der Benutzer nur SEINE Aufgabe löschen kann
    cursor.execute("DELETE FROM tasks WHERE user_id = ? AND id = ?", (user_id, task_id))
    conn.commit()
    rows_affected = cursor.rowcount
    conn.close()
    return rows_affected > 0

async def add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Bitte gib die Aufgabe ein, die du hinzufügen möchtest.")
    return ADD_TASK

async def add_task_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    task = update.message.text
    add_task(user_id, task)
    await update.message.reply_text(f'Die Aufgabe "{task}" wurde hinzugefügt.')
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Vorgang abgebrochen.")
    return ConversationHandler.END

async def list_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    tasks = get_tasks(user_id)
    if not tasks:
        await update.message.reply_text("Deine Aufgabenliste ist leer.")
        return
    message = "Deine Aufgabenliste:\n"
    display_number = 1
    for task_id, task, status in tasks:
        status_text = "✅" if status == 'completed' else "⬜️"
        message += f'{status_text} {display_number}. {task}\n'
        display_number += 1 
    
    await update.message.reply_text(message)

async def done_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Bitte gib die Nummer der Aufgabe an, die du bereits erledigt hast.")
    return DONE_TASK

async def done_task_step(update: Update, context: ContextTypes.DEFAULT_TYPE): 
    user_id = update.effective_user.id
    try:
        tasks = get_tasks(user_id)
        if not tasks:
            await update.message.reply_text("Deine Aufgabenliste ist leer.")
            return ConversationHandler.END
        display_number = int(update.message.text)
        task_id_from_db = tasks[display_number - 1][0]
        if complete_task(user_id, task_id_from_db):
            await update.message.reply_text(f'Aufgabe {display_number} erledigt! 🎉')
        else:
            await update.message.reply_text("Eine Aufgabe mit dieser Nummer wurde nicht gefunden oder gehört nicht dir.")
    except (ValueError, IndexError):
        await update.message.reply_text("Bitte gib eine gültige Aufgabennummer ein.")

    return ConversationHandler.END

async def delete_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Bitte gib die Nummer der Aufgabe an, die du löschen möchtest.")
    return DELETE_TASK

async def delete_task_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    try:
        tasks = get_tasks(user_id)
        if not tasks:
            await update.message.reply_text("Deine Aufgabenliste ist leer.")
            return ConversationHandler.END
            
        display_number = int(update.message.text)
        task_id_from_db = tasks[display_number - 1][0]
        if delete_task(user_id, task_id_from_db):
            await update.message.reply_text(f'Aufgabe {display_number} wurde gelöscht.')
        else:
            await update.message.reply_text("Eine Aufgabe mit dieser Nummer wurde nicht gefunden oder gehört nicht dir.")
    except (ValueError, IndexError):
        await update.message.reply_text("Bitte gib eine gültige Aufgabennummer ein.")
        return ConversationHandler.END

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.photo:
        return 

    try:
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=telegram.constants.ChatAction.UPLOAD_PHOTO)
        
        photo_file = await context.bot.get_file(update.message.photo[-1].file_id)
        file_name = f"{uuid.uuid4()}.jpg" 
        await photo_file.download_to_drive(file_name)

        image_file = genai.upload_file(path=file_name)

        prompt_parts = [
            "What is in this photo? If there's text, extract it.",
            image_file
        ]
        
        response_text = await asyncio.to_thread(generate_content_with_fallback, prompt_parts, return_model_instance=False)
        
        os.remove(file_name)

        if response_text:
            await send_long_message(update, response_text.strip())
        else:
            await update.message.reply_text("Entschuldigung, ich konnte das Foto nicht analysieren.")

    except Exception as e:
        print(f"Fehler beim Verarbeiten des Fotos: {e}")
        await update.message.reply_text("Beim Verarbeiten des Fotos ist ein Fehler aufgetreten.")
    finally:
        if 'file_name' in locals() and os.path.exists(file_name):
            os.remove(file_name)
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=telegram.constants.ChatAction.TYPING) 

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    if user_id not in user_chat_sessions:
        try:
            model_for_session = await asyncio.to_thread(generate_content_with_fallback, ["Hello"], return_model_instance=True)
            user_chat_sessions[user_id] = model_for_session.start_chat(history=[])
            print(f"Chat-Sitzung für Benutzer {user_id} mit Modell initialisiert: {model_for_session.model_name}")

        except Exception as e:
            print(f"Fehler beim Initialisieren der Chat-Sitzung für Benutzer {user_id}: {e}")
            await update.message.reply_text("Entschuldigung, beim Vorbereiten des Dialogs ist ein Fehler aufgetreten. Konnte kein funktionierendes Modell finden. Bitte versuche es später noch einmal.")
            await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=telegram.constants.ChatAction.TYPING)
            return 

    current_chat_session = user_chat_sessions[user_id]
    
    user_message = ""
    
    if update.message.voice:
        try:
            await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=telegram.constants.ChatAction.TYPING)
            voice_file = await context.bot.get_file(update.message.voice.file_id)
            file_name = f"{uuid.uuid4()}.ogg"
            await voice_file.download_to_drive(file_name)
            
            audio_file = genai.upload_file(path=file_name)

            transcribed_text = await asyncio.to_thread(generate_content_with_fallback, [
                "Transcribe the spoken words in this audio verbatim. Do not translate. Provide only the transcribed text.",
                audio_file
            ], return_model_instance=False)
            user_message = transcribed_text.strip()

            os.remove(file_name)

        except Exception as e:
            print(f"Fehler beim Verarbeiten der Sprachnachricht: {e}")
            await update.message.reply_text("Beim Verarbeiten der Sprachnachricht ist ein Fehler aufgetreten.")
            await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=telegram.constants.ChatAction.TYPING)
            return 

    elif update.message.text:
        user_message = update.message.text

    if user_message:
        try:
            await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=telegram.constants.ChatAction.TYPING)
            
            response_obj = await asyncio.to_thread(current_chat_session.send_message, user_message)
            response_text = response_obj.text
            
            await send_long_message(update, response_text)
            await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=telegram.constants.ChatAction.TYPING)
        except Exception as e:
            print(f"Fehler beim Generieren der Antwort von Gemini: {e}")
            await update.message.reply_text(f"Beim Generieren einer Antwort ist ein Fehler aufgetreten: {e}. Ich werde versuchen, für den Chat auf ein anderes Modell umzusteigen.")
            
            if user_id in user_chat_sessions:
                del user_chat_sessions[user_id]
            
            await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=telegram.constants.ChatAction.TYPING)


def main():
    print("Bot wird gestartet...")
    
    create_table()
    
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    
    add_handler = ConversationHandler(
        entry_points=[CommandHandler("add", add_start)],
        states={
            ADD_TASK: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_task_step)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    done_handler = ConversationHandler(
        entry_points=[CommandHandler("done", done_command)],
        states={
        DONE_TASK: [MessageHandler(filters.TEXT & ~filters.COMMAND, done_task_step)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    delete_handler = ConversationHandler(
        entry_points=[CommandHandler("delete", delete_command)],
        states={
            DELETE_TASK: [MessageHandler(filters.TEXT & ~filters.COMMAND, delete_task_step)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(add_handler)
    app.add_handler(done_handler)
    app.add_handler(delete_handler)
    app.add_handler(CommandHandler("list", list_command))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.run_polling()
    app.add_handler(MessageHandler(filters.TEXT | filters.VOICE, handle_message))

if __name__ == "__main__":
    main()

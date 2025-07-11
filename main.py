import telegram
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes, CommandHandler
import google.generativeai as genai
import asyncio
import os
import requests
import uuid
from google.api_core.exceptions import ResourceExhausted

# --- Configuration ---
TELEGRAM_TOKEN = "7463890992:AAGHWvIR-XzO-VCdBR5bEr-UbIY6XcKI30I"
GEMINI_API_KEY = "AIzaSyAnnsWGT0ykhKWAT9ryWN-esPhW-RMTYa8"

# --- Gemini API Initialization ---
genai.configure(api_key=GEMINI_API_KEY)

# --- Models List for Fallback ---
# Order matters: the bot will try to use models in this order.
MODELS = [
    "gemini-2.5-flash", 
    "gemini-1.5-flash",
    "gemini-1.5-pro",  
    "gemini-2.5-pro" 
]

# --- Global Storage for User Chat Sessions ---
user_chat_sessions = {}

# --- Fallback Content Generation Function ---
def generate_content_with_fallback(prompt_parts, current_model_index=0, return_model_instance=False):
    if current_model_index >= len(MODELS):
        raise Exception("All available models exhausted quota or are unavailable.")

    model_name = MODELS[current_model_index]
    try:
        print(f"Using model: {model_name}")
        model_instance = genai.GenerativeModel(model_name)
        
        if return_model_instance:
            # Test model availability before returning instance for session
            test_response = model_instance.generate_content("test", stream=False) 
            _ = test_response.text 
            return model_instance 
        else:
            response = model_instance.generate_content(prompt_parts)
            return response.text
            
    except ResourceExhausted as e:
        print(f"Quota exhausted for {model_name}, switching to next model...")
        return generate_content_with_fallback(prompt_parts, current_model_index + 1, return_model_instance)
    except Exception as e:
        print(f"Error with model {model_name}: {e}. Trying next...")
        return generate_content_with_fallback(prompt_parts, current_model_index + 1, return_model_instance)

# --- Constants ---
MAX_MESSAGE_LENGTH = 4096
USER_DATA_FILE = 'user_data.txt'

# --- Helper Functions ---
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
            print(f"Error sending message part: {e}")
            await asyncio.sleep(1) 

# --- /start Command Handler ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_name = update.effective_user.first_name

    if user_id not in KNOWN_USERS:
        await update.message.reply_text(f"Hello, {user_name}! Welcome to my bot. Send me text, a voice message, or a **photo**!")
        save_new_user(user_id)
        KNOWN_USERS.add(user_id)
    else:
        await update.message.reply_text(f"Welcome back, {user_name}! How else can I help? Send me text, a voice message, or a **photo**.")
    
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
            await update.message.reply_text("Sorry, I could not analyze the photo.")

    except Exception as e:
        print(f"Error processing photo: {e}")
        await update.message.reply_text("An error occurred while processing the photo.")
    finally:
        if 'file_name' in locals() and os.path.exists(file_name):
            os.remove(file_name)
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=telegram.constants.ChatAction.TYPING) 

# --- Main Message Handler ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    if user_id not in user_chat_sessions:
        try:
            model_for_session = await asyncio.to_thread(generate_content_with_fallback, ["Hello"], return_model_instance=True)
            user_chat_sessions[user_id] = model_for_session.start_chat(history=[])
            print(f"Chat session for user {user_id} initialized with model: {model_for_session.model_name}")

        except Exception as e:
            print(f"Error initializing chat session for user {user_id}: {e}")
            await update.message.reply_text("Sorry, an error occurred while preparing the dialogue. Could not find a working model. Please try again later.")
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
            print(f"Error processing voice message: {e}")
            await update.message.reply_text("An error occurred while processing the voice message.")
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
            print(f"Error generating response from Gemini: {e}")
            await update.message.reply_text(f"An error occurred while generating a response: {e}. I will try to switch to another model for the chat.")
            
            if user_id in user_chat_sessions:
                del user_chat_sessions[user_id]
            
            await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=telegram.constants.ChatAction.TYPING)

def main():
    print("Bot starting...")
    
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT | filters.VOICE, handle_message))
    app.run_polling()

if __name__ == "__main__":
    main()
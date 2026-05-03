# ============================================================
# АГЕНТ МОНИТОРИНГА TELEGRAM ЧАТОВ — Mazda CX-5
# ============================================================

import os
import asyncio
from telethon import TelegramClient, events
from dotenv import load_dotenv

# Загружаем секреты из .env_monitor (файл ТОЛЬКО на сервере)
load_dotenv('/root/monitor/.env_monitor')

API_ID = int(os.getenv("MONITOR_API_ID"))
API_HASH = os.getenv("MONITOR_API_HASH")
NOTIFY_USERS = os.getenv("MONITOR_NOTIFY_USERS", "").split(",")

# ------------------------------------------------------------
# Чаты для мониторинга
# ------------------------------------------------------------
CHATS = [
    "mazdachinacx5",
    "mazda_club_cx5",
    "kuruma_kurgan",
    "auto_zakazz25",
    "mazda_forum",
    "bambaleilatop",
]

# ------------------------------------------------------------
# Ключевые слова
# ------------------------------------------------------------
KEYWORDS = [
    "купил", "купила", "забрал", "забрала", "взял", "взяла",
    "пригнал", "пригнала", "приехала", "приехал", "забираю",
    "только купил", "новый владелец", "стал владельцем", "на таможне", "антикор", "сигналка",
    "сигнализация", "бронепленка", "защитная пленка", "пленка", "на границе", "на стоянке", "лаборатория",
    "в лабаратории",
    "из китая", "китайская", "китайский", "china", "cn версия",
    "помогите настроить", "как настроить", "русификация",
    "скрытое меню", "как включить", "активировать",
    "не могу найти", 
]

# ------------------------------------------------------------
# Остальной код (логика, обработка сообщений)
# ------------------------------------------------------------
client = TelegramClient("mazda_session", API_ID, API_HASH)

def is_relevant(text: str) -> bool:
    text_lower = text.lower()
    return any(keyword in text_lower for keyword in KEYWORDS)

@client.on(events.NewMessage(chats=CHATS))
async def handler(event):
    message = event.message
    text = message.text or ""

    if not text or len(text) < 10:
        return

    if is_relevant(text):
        try:
            sender = await event.get_sender()
            sender_name = f"@{sender.username}" if sender.username else sender.first_name
        except:
            sender_name = "Неизвестный"

        try:
            chat = await event.get_chat()
            chat_title = getattr(chat, "title", "Неизвестный чат")
        except:
            chat_title = "Неизвестный чат"

        try:
            chat_username = getattr(chat, "username", None)
            msg_link = f"https://t.me/{chat_username}/{message.id}" if chat_username else "Ссылка недоступна"
        except:
            msg_link = "Ссылка недоступна"

        notification = (
            f"🚗 *Потенциальный клиент!*\n\n"
            f"👤 Пользователь: {sender_name}\n"
            f"💬 Написал:\n_{text[:300]}_\n\n"
            f"📍 Чат: {chat_title}\n"
            f"🔗 {msg_link}"
        )

        for username in NOTIFY_USERS:
            try:
                await client.send_message(username, notification, parse_mode="md")
                print(f"✅ Уведомление отправлено @{username} | Чат: {chat_title} | От: {sender_name}")
            except Exception as e:
                print(f"❌ Ошибка отправки @{username}: {e}")

async def main():
    print("🔍 Агент запущен — мониторю чаты...")
    print(f"📋 Слежу за {len(CHATS)} чатами")
    print(f"👥 Уведомления идут: {', '.join(['@' + u for u in NOTIFY_USERS])}")
    print(f"🔑 Ключевых слов: {len(KEYWORDS)}")
    print("Нажми Ctrl+C для остановки\n")
    await client.run_until_disconnected()

with client:
    client.loop.run_until_complete(main())
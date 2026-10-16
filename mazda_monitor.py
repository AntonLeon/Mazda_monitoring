#!/usr/bin/env python3
"""Монитор чатов Mazda CX-5: находит сообщения потенциальных клиентов и уведомляет мастеров.

Запуск:      python3 mazda_monitor.py
Проверка:    python3 mazda_monitor.py --check     (настройки, чаты, сессия — и выход)
Тесты:       python3 selftest.py                  (офлайн, без сети)

Настройки берутся из .env_monitor рядом со скриптом (или из переменных окружения,
переменные окружения важнее). Образец — .env_monitor.example.

Отличия от первой версии:
  * настройки ищутся рядом со скриптом, а не только в /root/monitor/.env_monitor;
  * понятные ошибки вместо TypeError и запроса телефона в консоли;
  * ключевые слова — правила с весами, границами слов, отрицаниями и стоп-словами
    продавцов: уведомления приходят по делу, а не на каждую рекламу;
  * повторы не дублируются, правки сообщений тоже проверяются;
  * FloodWait от Telegram обрабатывается паузой и повтором, а не потерей уведомления;
  * уведомление можно отправлять через бота (@…_bot) — личный аккаунт остаётся только слушать.
"""
from __future__ import annotations

import argparse
import asyncio
import difflib
import hashlib
import json
import logging
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

BASE_DIR = Path(__file__).resolve().parent
LOG = logging.getLogger("mazda_monitor")

DEFAULT_CHATS = (
    "mazdachinacx5",
    "mazda_club_cx5",
    "kuruma_kurgan",
    "auto_zakazz25",
    "mazda_forum",
    "bambaleilatop",
)

# ============================== правила поиска клиентов ==============================
# score — вес категории; чем выше, тем интереснее сообщение.
# Отрицательные веса снижают итог, веса <= -50 означают «не уведомлять никогда»
# (продавцы, реклама, вакансии): иначе мастер тонет в чужих объявлениях.


@dataclass(frozen=True)
class Rule:
    category: str
    score: int
    pattern: "re.Pattern[str]"


RULES: Tuple[Rule, ...] = (
    Rule("купил/забрал машину", 3, re.compile(
        r"\b(купил[аи]?|приобр[её]л[аи]?|забрал[аи]?|забираю|взял[аи]?|пригнал[аи]?|"
        r"машина пришла|пришла машина|жду машину|заказал[аи]? машину)\b", re.I)),
    Rule("таможня/доставка", 3, re.compile(
        r"\b(таможн\w*|растамож\w*|деклараци\w*|на границе|на стоянке|стоянк\w+|"
        r"сбктс|эптс|лаборатори\w*|брокер\w*)\b", re.I)),
    Rule("новый владелец", 2, re.compile(
        r"\b(стал[аи]? владельц\w+|новый владелец|только купил[аи]?|первый автомобиль|первая машина)\b", re.I)),
    Rule("машина из Китая", 1, re.compile(
        r"\b(из китая|китайск\w+|китаец|china|cn[- ]?верси\w*)\b", re.I)),
    Rule("русификация", 4, re.compile(
        r"\b(русификац\w*|руссификац\w*|русский язык|перевод меню|язык меню|локализаци\w*)\b", re.I)),
    Rule("скрытые функции", 3, re.compile(
        r"\b(скрыт\w+ (опци\w+|меню|функци\w+)|активир\w+|разблокир\w+|включить скрыт\w+)\b", re.I)),
    Rule("просьба о помощи", 3, re.compile(
        r"\b(помогите|подскажите|не могу найти|как настроить|как включить|как активировать|что делать)\b", re.I)),
    Rule("доработки", 2, re.compile(
        r"\b(антикор\w*|сигналк\w*|сигнализаци\w*|пленк\w*|бронепленк\w*|шумк\w*|"
        r"полировк\w*|керамик\w*|автозапуск\w*)\b", re.I)),
    Rule("вопрос о цене", 1, re.compile(
        r"\b(сколько стоит|поч[её]м|цена|цены|стоимость|прайс)\b", re.I)),
)

VETO_RULES: Tuple[Rule, ...] = (
    Rule("продажа/реклама", -99, re.compile(
        r"\b(продам|продаю|прода[её]тся|продажа|в наличии|оптом|под заказ|на заказ|скидк\w*|"
        r"акци\w*|рассрочк\w*|выгодн\w+ предложени\w*|звоните|пишите в личку|whatsapp|ватсап|"
        r"подписывайтесь|реклама|барахолк\w*|магазин|доставка по россии)\b", re.I)),
    Rule("поиск работы/вакансия", -99, re.compile(
        r"\b(ищу работу|ваканси\w*|резюме|требуется (мастер|сотрудник|специалист)|нанимаю|подработка)\b", re.I)),
    Rule("отказ от покупки", -5, re.compile(
        r"\b(не купил[аи]?|не взял[аи]?|не забрал[аи]?|передумал[аи]?|отказал\w*ся|отказал\w*ась)\b", re.I)),
)

# Опечатки: клиенты пишут «русификацыя», «антекор», «таможня» через «ж» и т.п.
FUZZY_KEYWORDS: Dict[str, Tuple[int, str]] = {
    "русификация": (4, "русификация"),
    "руссификация": (4, "русификация"),
    "локализация": (4, "русификация"),
    "антикор": (2, "доработки"),
    "сигнализация": (2, "доработки"),
    "таможня": (3, "таможня/доставка"),
    "лаборатория": (3, "таможня/доставка"),
}
# Порог подобран по разбору пар: опечатки «антекор→антикор» (0.857), «лабаратории→лаборатория»
# (0.818) проходят, а чужие слова «классификация→русификация» (0.750),
# «легализация→локализация» (0.818, но другие первые буквы) — нет.
FUZZY_RATIO = 0.80


# ============================== настройки ==============================


class ConfigError(RuntimeError):
    """Проблема в настройках — с понятным текстом для человека."""


def read_env_file(path: Path) -> Dict[str, str]:
    """Простой разбор файла настроек: KEY=VALUE, комментарии, кавычки, export."""
    values: Dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        if line.lower().startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def find_env_file(explicit: Optional[str] = None) -> Optional[Path]:
    """Файл настроек: явный путь, затем рядом со скриптом, затем старый путь на сервере."""
    candidates: List[Path] = []
    if explicit:
        candidates.append(Path(explicit).expanduser())
    else:
        env_pointer = os.getenv("MONITOR_ENV_FILE")
        if env_pointer:
            candidates.append(Path(env_pointer).expanduser())
    candidates += [BASE_DIR / ".env_monitor", Path("/root/monitor/.env_monitor")]
    for path in candidates:
        if path.is_file():
            return path
    return None


def split_list(value: str) -> List[str]:
    """Список из строки: убираем пустые элементы и пробелы, «@» оставляем."""
    items = []
    for chunk in (value or "").replace(";", ",").split(","):
        item = chunk.strip()
        if item:
            items.append(item)
    return items


def to_int(value: str, name: str, default: Optional[int] = None) -> int:
    value = (value or "").strip()
    if not value:
        if default is None:
            raise ConfigError(f"Не задано значение {name} — впишите его в .env_monitor")
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise ConfigError(f"{name} должно быть числом, а получено {value!r}") from exc


@dataclass
class Settings:
    mode: str = "user"
    api_id: Optional[int] = None
    api_hash: Optional[str] = None
    session_path: Path = field(default_factory=lambda: BASE_DIR / "mazda_session")
    notify_users: List[str] = field(default_factory=list)
    chats: List[str] = field(default_factory=lambda: list(DEFAULT_CHATS))
    all_chats: bool = False
    chats_exclude: List[str] = field(default_factory=list)
    bot_token: Optional[str] = None
    min_score: int = 3
    heartbeat_minutes: int = 0
    seen_hours: int = 24
    log_file: Optional[Path] = None
    state_file: Path = field(default_factory=lambda: BASE_DIR / "monitor_state.json")
    env_file: Optional[Path] = None
    dry_run: bool = False

    def summary(self) -> str:
        scope = "все группы аккаунта" if self.all_chats else f"чатов: {len(self.chats)}"
        return (
            f"режим: {self.mode}, {scope}, получателей: {len(self.notify_users)}, "
            f"порог веса: {self.min_score}, уведомления: "
            f"{'через бота' if self.bot_token else 'с личного аккаунта'}, настройки: {self.env_file or 'переменные окружения'}"
        )


def load_settings(env: Optional[Dict[str, str]] = None, explicit_env_file: Optional[str] = None) -> Settings:
    """Собрать настройки: файл .env_monitor, поверх — переменные окружения."""
    values: Dict[str, str] = {}
    env_file: Optional[Path] = None
    if env is None:
        env_file = find_env_file(explicit_env_file)
        if env_file is not None:
            values.update(read_env_file(env_file))
        values.update({k: v for k, v in os.environ.items() if k.startswith("MONITOR_")})
    else:
        values.update(env)

    settings = Settings()
    settings.env_file = env_file

    api_id_raw = (values.get("MONITOR_API_ID") or "").strip()
    api_hash_raw = (values.get("MONITOR_API_HASH") or "").strip()
    settings.api_hash = api_hash_raw or None
    if api_id_raw:
        settings.api_id = to_int(api_id_raw, "MONITOR_API_ID")

    session_value = (values.get("MONITOR_SESSION") or "").strip()
    if session_value:
        path = Path(session_value).expanduser()
        settings.session_path = path if path.is_absolute() else (BASE_DIR / path)
    settings.session_path = settings.session_path.with_suffix("") if settings.session_path.suffix == ".session" \
        else settings.session_path

    notify = split_list(values.get("MONITOR_NOTIFY_USERS", ""))
    if not notify:
        raise ConfigError(
            "Не задан MONITOR_NOTIFY_USERS — кому присылать уведомления. "
            "Укажите юзернеймы или id через запятую, например: 120317642,1882208554"
        )
    settings.notify_users = notify

    chats = split_list(values.get("MONITOR_CHATS", ""))
    if chats and chats[0].strip().lower() in {"all", "*", "все", "any"}:
        # «all» — слушать все группы аккаунта; отдельные чаты в этом режиме не нужны,
        # исключения задаются через MONITOR_CHATS_EXCLUDE
        settings.all_chats = True
        if len(chats) > 1:
            LOG.warning("MONITOR_CHATS=all: список чатов после all игнорируется, "
                        "исключения задавайте в MONITOR_CHATS_EXCLUDE")
        chats = []
    settings.chats = chats or ([] if settings.all_chats else list(DEFAULT_CHATS))
    settings.chats_exclude = split_list(values.get("MONITOR_CHATS_EXCLUDE", ""))

    settings.bot_token = (values.get("MONITOR_BOT_TOKEN") or "").strip() or None

    if settings.bot_token and not settings.notify_users:
        raise ConfigError("Для отправки через бота нужны id получателей в MONITOR_NOTIFY_USERS")

    settings.min_score = to_int(values.get("MONITOR_MIN_SCORE", ""), "MONITOR_MIN_SCORE", default=3)
    settings.heartbeat_minutes = to_int(values.get("MONITOR_HEARTBEAT_MINUTES", ""), "MONITOR_HEARTBEAT_MINUTES", default=0)
    settings.seen_hours = to_int(values.get("MONITOR_SEEN_HOURS", ""), "MONITOR_SEEN_HOURS", default=24)

    log_value = (values.get("MONITOR_LOG_FILE") or "").strip()
    if log_value:
        path = Path(log_value).expanduser()
        settings.log_file = path if path.is_absolute() else (BASE_DIR / path)

    state_value = (values.get("MONITOR_STATE_FILE") or "").strip()
    if state_value:
        path = Path(state_value).expanduser()
        settings.state_file = path if path.is_absolute() else (BASE_DIR / path)

    settings.dry_run = (values.get("MONITOR_DRY_RUN", "").strip().lower() in {"1", "true", "yes", "on"})

    # Режим: для личного аккаунта нужны api_id и api_hash, иначе — только бот
    if settings.api_id and settings.api_hash:
        settings.mode = "user"
    elif settings.bot_token:
        settings.mode = "bot"
    else:
        raise ConfigError(
            "Нет доступов для работы.\n"
            "  • для мониторинга с личного аккаунта нужны MONITOR_API_ID и MONITOR_API_HASH\n"
            "    (my.telegram.org → API development tools);\n"
            "  • либо укажите MONITOR_BOT_TOKEN, если мониторить будет бот."
        )

    if settings.mode == "user" and not session_file_exists(settings.session_path):
        raise ConfigError(
            f"Не найден файл сессии {settings.session_path}.\n"
            "  Скопируйте mazda_session.session на сервер (права 600) — без него Telegram "
            "спросит номер телефона, а в службе это превратится в вечный «работающий» процесс."
        )
    return settings


# ============================== отбор сообщений ==============================


def fuzzy_categories(text: str) -> List[Tuple[str, int]]:
    """Категории, найденные с учётом опечаток (для длинных слов)."""
    found: List[Tuple[str, int]] = []
    words = re.findall(r"[а-яёa-z]{6,}", text.lower())
    if not words:
        return found
    seen = set()
    for word in words:
        for keyword, (score, category) in FUZZY_KEYWORDS.items():
            if category in seen:
                continue
            if abs(len(word) - len(keyword)) > 3:
                continue
            # первые две буквы должны совпадать: иначе «легализация» сойдёт за «локализацию»
            if word[:2] != keyword[:2]:
                continue
            if difflib.SequenceMatcher(None, word, keyword).ratio() >= FUZZY_RATIO:
                found.append((category, score))
                seen.add(category)
    return found


def score_message(text: str) -> Tuple[int, List[str], Optional[str]]:
    """Оценка сообщения: (вес, категории, причина отказа)."""
    categories: List[str] = []
    score = 0

    for rule in VETO_RULES:
        if rule.pattern.search(text):
            if rule.score <= -50:
                return 0, [], rule.category
            score += rule.score
            categories.append(rule.category)

    for rule in RULES:
        if rule.pattern.search(text):
            score += rule.score
            categories.append(rule.category)

    for category, extra in fuzzy_categories(text):
        if category not in categories:
            score += extra
            categories.append(category)

    return score, categories, None


def build_notification(*, sender_name: str, text: str, chat_title: str, chat_username: Optional[str],
                       message_id: int, categories: Sequence[str], score: int) -> str:
    """Текст уведомления: простой текст без разметки, чтобы ничего не «съедалось»."""
    snippet = " ".join((text or "").split())
    if len(snippet) > 300:
        snippet = snippet[:300].rstrip() + "…"
    lines = [
        "🚗 Потенциальный клиент!",
        "",
        f"👤 {sender_name}",
        f"💬 {snippet}",
        "",
        f"📍 {chat_title}",
    ]
    if chat_username:
        lines.append(f"🔗 https://t.me/{chat_username}/{message_id}")
    lines.append(f"🏷 {', '.join(categories)} (вес {score})")
    return "\n".join(lines)


def message_key(chat_id: int, message_id: int) -> str:
    return f"{chat_id}:{message_id}"


def text_key(sender_id: Optional[int], text: str) -> str:
    digest = hashlib.sha1(" ".join((text or "").lower().split()).encode("utf-8")).hexdigest()[:16]
    return f"text:{sender_id}:{digest}"


# ============================== память об отправленном ==============================


class SeenStore:
    """Помнит, что уже отправляли: чтобы не дублировать одно и то же сообщение.

    Файл ограничен по количеству записей и времени жизни (по умолчанию сутки).
    """

    def __init__(self, path: Path, ttl_hours: int = 24, limit: int = 5000) -> None:
        self.path = path
        self.ttl = max(ttl_hours, 0) * 3600
        self.limit = limit
        self._data: Dict[str, float] = {}
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self._data = {str(k): float(v) for k, v in raw.items()}
        except FileNotFoundError:
            pass
        except Exception as exc:  # noqa: BLE001 — испорченный файл не повод не работать
            LOG.warning("Не удалось прочитать %s: %s — начинаю с чистого листа", self.path, exc)

    def _prune(self) -> None:
        if self.ttl:
            edge = time.time() - self.ttl
            self._data = {k: v for k, v in self._data.items() if v >= edge}
        if len(self._data) > self.limit:
            for key, _ in sorted(self._data.items(), key=lambda item: item[1])[: len(self._data) - self.limit]:
                self._data.pop(key, None)

    def seen(self, key: str) -> bool:
        return key in self._data

    def add(self, key: str) -> None:
        self._data[key] = time.time()
        self._prune()
        self.save()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self._data, ensure_ascii=False)
        try:
            handle, temp_path = tempfile.mkstemp(dir=str(self.path.parent), prefix=".seen-")
            with os.fdopen(handle, "w", encoding="utf-8") as fh:
                fh.write(payload)
            os.chmod(temp_path, 0o600)
            os.replace(temp_path, self.path)
        except OSError as exc:
            LOG.warning("Не удалось сохранить %s: %s", self.path, exc)


# ============================== отправка уведомлений ==============================


def send_via_bot(token: str, chat_id: str, text: str, timeout: int = 20) -> None:
    """Отправка через Bot API обычным HTTP — без лишних зависимостей."""
    payload = json.dumps({"chat_id": chat_id, "text": text, "disable_web_page_preview": False}).encode("utf-8")
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        if response.status != 200:
            raise RuntimeError(f"Bot API вернул {response.status}")


class Notifier:
    """Разослать уведомление получателям: через бота или с личного аккаунта."""

    def __init__(self, settings: Settings, client=None) -> None:
        self.settings = settings
        self.client = client
        self.sent = 0

    async def send(self, text: str) -> int:
        delivered = 0
        for target in self.settings.notify_users:
            try:
                if self.settings.bot_token:
                    await asyncio.to_thread(send_via_bot, self.settings.bot_token, target, text)
                else:
                    await self._send_from_account(target, text)
                delivered += 1
                self.sent += 1
                LOG.info("Уведомление отправлено: %s", target)
            except Exception as exc:  # noqa: BLE001 — один получатель не должен ломать остальных
                LOG.error("Не удалось отправить %s: %s", target, exc)
        return delivered

    async def _send_from_account(self, target: str, text: str) -> None:
        from telethon.errors import FloodWaitError  # импорт здесь: без telethon модуль импортируется для тестов

        try:
            await self.client.send_message(target, text)
        except FloodWaitError as exc:
            wait = int(getattr(exc, "seconds", 60)) + 5
            LOG.warning("Telegram просит подождать %s с (лимит отправки) — жду и повторяю", wait)
            await asyncio.sleep(wait)
            await self.client.send_message(target, text)


# ============================== работа с Telegram ==============================


def session_file_exists(path: Path) -> bool:
    """Telethon хранит сессию как <имя>.session, а в настройках пишут имя без расширения."""
    return path.exists() or path.with_suffix(".session").exists()


def normalize_chat_target(raw: str) -> object:
    """@username, ссылка t.me или числовой id — привести к тому, что понимает get_entity."""
    value = raw.strip()
    for prefix in ("https://t.me/", "http://t.me/", "t.me/"):
        if value.startswith(prefix):
            value = value[len(prefix):]
            break
    value = value.strip("/").split("/")[0]
    if re.fullmatch(r"-?\d+", value):
        return int(value)
    return value


async def resolve_chats(client, chats: Sequence[str]) -> Tuple[List[object], List[Tuple[str, str]]]:
    """Разобрать список чатов: часть может не находиться — это не повод не запускаться."""
    resolved: List[object] = []
    failed: List[Tuple[str, str]] = []
    for raw in chats:
        target = normalize_chat_target(raw)
        try:
            entity = await client.get_entity(target)
            resolved.append(entity)
            LOG.info("Чат найден: %s", getattr(entity, "title", raw))
        except Exception as exc:  # noqa: BLE001 — сообщаем и идём дальше
            failed.append((raw, f"{type(exc).__name__}: {exc}"))
            LOG.warning("Чат недоступен (%s): %s", raw, exc)
    return resolved, failed


def ensure_session_permissions(path: Path) -> None:
    """Файл сессии = полный доступ к аккаунту: лишние права убираем."""
    session_file = path if path.suffix == ".session" else path.with_suffix(".session")
    for candidate in (path, session_file):
        try:
            if candidate.exists() and (candidate.stat().st_mode & 0o077):
                os.chmod(candidate, 0o600)
                LOG.warning("Права на %s были слишком широкими — выставил 600", candidate.name)
        except OSError as exc:
            LOG.warning("Не удалось изменить права %s: %s", candidate, exc)


async def list_account_groups(client) -> List[object]:
    """Все группы и каналы, в которых состоит аккаунт (для режима MONITOR_CHATS=all)."""
    groups: List[object] = []
    async for dialog in client.iter_dialogs():
        if dialog.is_group or dialog.is_channel:
            groups.append(dialog.entity)
    return groups


async def resolve_excluded(client, targets: Sequence[str]) -> set:
    """id групп, которые слушать не нужно."""
    excluded = set()
    for raw in targets:
        try:
            entity = await client.get_entity(normalize_chat_target(raw))
            excluded.add(abs(int(getattr(entity, "id", 0))))
        except Exception as exc:  # noqa: BLE001
            LOG.warning("Не удалось разобрать исключение %s: %s", raw, exc)
    return excluded


async def run_user_mode(settings: Settings) -> int:
    """Мониторинг с личного аккаунта (нужны api_id, api_hash и файл сессии)."""
    from telethon import TelegramClient, events

    ensure_session_permissions(settings.session_path)
    client = TelegramClient(str(settings.session_path), settings.api_id, settings.api_hash)
    await client.connect()
    if not await client.is_user_authorized():
        raise ConfigError(
            "Сессия не авторизована. Один раз войдите локально (номер + код) или перенесите "
            "рабочий файл mazda_session.session, после чего перезапустите службу."
        )

    me = await client.get_me()
    LOG.info("Аккаунт: %s (id %s)", getattr(me, "first_name", "?"), getattr(me, "id", "?"))

    excluded: set = set()
    if settings.all_chats:
        # Слушаем все группы аккаунта: новые чаты подхватываются без перезапуска,
        # потому что фильтр по списку не ставится — проверяем внутри обработчика.
        resolved = await list_account_groups(client)
        excluded = await resolve_excluded(client, settings.chats_exclude)
        if not resolved:
            raise ConfigError("Аккаунт не состоит ни в одной группе — слушать нечего.")
        LOG.info("Слушаю все группы аккаунта: %s, исключено: %s", len(resolved), len(excluded))
    else:
        resolved, failed = await resolve_chats(client, settings.chats)
        if failed:
            LOG.warning("Не удалось подключить чатов: %s", ", ".join(name for name, _ in failed))
        if not resolved:
            raise ConfigError(
                "Ни один чат не найден. Проверьте MONITOR_CHATS: аккаунт должен состоять в этих чатах, "
                "а юзернеймы — быть актуальными (надёжнее указывать числовые id)."
            )

    store = SeenStore(settings.state_file, ttl_hours=settings.seen_hours)
    notifier = Notifier(settings, client)
    stats = {"found": 0, "skipped": 0}

    def evaluate(text: str) -> Optional[Tuple[int, List[str]]]:
        """Оценка сообщения: None — не наш случай."""
        score, categories, veto = score_message(text)
        if veto:
            stats["skipped"] += 1
            LOG.info("Пропуск (отсев: %s): %s", veto, text[:60])
            return None
        if score < settings.min_score:
            stats["skipped"] += 1
            LOG.debug("Пропуск (вес %s < %s): %s", score, settings.min_score, text[:60])
            return None
        stats["found"] += 1
        return score, categories

    async def handler(event) -> None:  # noqa: ANN001 — тип события задаёт telethon
        message = event.message
        if getattr(event, "out", False):  # свои же сообщения не пересылаем
            return
        if settings.all_chats:
            # в личных переписках не слушаем; исключённые чаты пропускаем
            if not (getattr(event, "is_group", False) or getattr(event, "is_channel", False)):
                return
            if abs(int(getattr(message, "chat_id", 0))) in excluded:
                return
        text = (message.text or "").strip()
        if len(text) < 10:
            return

        keys = (message_key(message.chat_id, message.id), text_key(getattr(event, "sender_id", None), text))
        if store.seen(keys[0]) or store.seen(keys[1]):
            LOG.debug("Повтор уже отправляли: %s", keys[0])
            return

        scored = evaluate(text)
        if scored is None:
            return
        score, categories = scored

        sender_name = "Неизвестный"
        try:
            sender = await event.get_sender()
            if sender is not None:
                sender_name = f"@{sender.username}" if getattr(sender, "username", None) else \
                    (getattr(sender, "first_name", None) or str(getattr(sender, "id", "Неизвестный")))
        except Exception as exc:  # noqa: BLE001
            LOG.warning("Не удалось определить автора сообщения: %s", exc)

        chat_title = "Неизвестный чат"
        chat_username = None
        try:
            chat = await event.get_chat()
            chat_title = getattr(chat, "title", None) or str(getattr(chat, "id", "Неизвестный чат"))
            chat_username = getattr(chat, "username", None)
        except Exception as exc:  # noqa: BLE001
            LOG.warning("Не удалось определить чат: %s", exc)

        notification = build_notification(
            sender_name=sender_name, text=text, chat_title=chat_title, chat_username=chat_username,
            message_id=message.id, categories=categories, score=score,
        )
        if settings.dry_run:
            LOG.info("[проверка] уведомление не отправлено:\n%s", notification)
        else:
            await notifier.send(notification)
        store.add(keys[0])
        store.add(keys[1])

    if settings.all_chats:
        client.add_event_handler(handler, events.NewMessage())
        client.add_event_handler(handler, events.MessageEdited())
    else:
        client.add_event_handler(handler, events.NewMessage(chats=resolved))
        client.add_event_handler(handler, events.MessageEdited(chats=resolved))
    LOG.info("Монитор запущен: %s", settings.summary())

    async def heartbeat() -> None:
        while True:
            await asyncio.sleep(settings.heartbeat_minutes * 60)
            await notifier.send(
                f"🫀 Монитор жив. Чатов: {len(resolved)}, найдено: {stats['found']}, "
                f"пропущено: {stats['skipped']}."
            )

    heartbeat_task = asyncio.create_task(heartbeat()) if settings.heartbeat_minutes > 0 else None
    try:
        await client.run_until_disconnected()
    finally:
        if heartbeat_task is not None:
            heartbeat_task.cancel()
        await client.disconnect()
        LOG.info("Монитор остановлен. Найдено: %s, пропущено: %s", stats["found"], stats["skipped"])
    return 0


async def check_mode(settings: Settings) -> int:
    """Проверка настроек и чатов: python3 mazda_monitor.py --check."""
    from telethon import TelegramClient

    LOG.info("Настройки: %s", settings.summary())
    ensure_session_permissions(settings.session_path)
    client = TelegramClient(str(settings.session_path), settings.api_id, settings.api_hash)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            LOG.error("Сессия не авторизована — нужен вход или перенос рабочего файла сессии")
            return 2
        me = await client.get_me()
        LOG.info("Аккаунт: %s (id %s, @%s)", getattr(me, "first_name", "?"), getattr(me, "id", "?"),
                 getattr(me, "username", "нет"))
        resolved, failed = await resolve_chats(client, settings.chats)
        LOG.info("Чатов подключено: %s из %s", len(resolved), len(settings.chats))
        for name, error in failed:
            LOG.warning("  не найден: %s (%s)", name, error[:120])
        if not failed:
            LOG.info("Все чаты на месте — можно запускать монитор")
        return 0 if resolved else 2
    finally:
        await client.disconnect()


# ============================== запуск ==============================


def setup_logging(log_file: Optional[Path], verbose: bool = False) -> None:
    handlers: List[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers,
    )
    for noisy in ("telethon", "telethon.network", "telethon.client.updates"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Монитор чатов Mazda CX-5")
    parser.add_argument("--check", action="store_true", help="проверить настройки, сессию и чаты, затем выйти")
    parser.add_argument("--env-file", help="путь к файлу настроек (по умолчанию .env_monitor рядом со скриптом)")
    parser.add_argument("--verbose", action="store_true", help="подробный лог")
    args = parser.parse_args(argv)

    try:
        settings = load_settings(explicit_env_file=args.env_file)
    except ConfigError as exc:
        setup_logging(None)
        LOG.error("%s", exc)
        return 2

    setup_logging(settings.log_file, verbose=args.verbose)
    if args.check:
        if settings.mode != "user":
            LOG.info("Режим «%s»: чаты проверяются только в режиме личного аккаунта", settings.mode)
            return 0
        return asyncio.run(check_mode(settings))

    if settings.mode != "user":
        LOG.error(
            "Режим «%s» пока не поддерживается: для мониторинга чужих чатов нужен личный аккаунт "
            "(MONITOR_API_ID и MONITOR_API_HASH). Бот читает сообщения только там, где он админ "
            "и выключен privacy mode.",
            settings.mode,
        )
        return 2

    try:
        return asyncio.run(run_user_mode(settings))
    except ConfigError as exc:
        LOG.error("%s", exc)
        return 2
    except KeyboardInterrupt:
        LOG.info("Остановлено пользователем")
        return 0


if __name__ == "__main__":
    sys.exit(main())

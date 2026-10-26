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
import csv
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

# Файлы бота с теми, кто уже писал ему: таких людей мастерам показывать не нужно.
# Пути по умолчанию — сервер (бот и монитор рядом) и локальная разработка.
DEFAULT_KNOWN_USER_FILES = (
    Path("/opt/mazda_bot/data/bot_users.json"),
    Path("/opt/mazda_bot/data/contacts.json"),
    Path("/opt/mazda_bot/data/bookings.json"),
    BASE_DIR.parent / "Mazda_bot" / "data" / "bot_users.json",
)


def _resolve_path(value: str, base: Optional[Path] = None) -> Path:
    """Путь из настроек: абсолютный как есть, относительный — от каталога файла настроек."""
    path = Path(value).expanduser()
    if path.is_absolute():
        return path
    return (base or BASE_DIR) / path


# ============================== правила поиска клиентов ==============================
# Словарь лежит в keywords.json рядом со скриптом: его можно править без правки кода.
# При первом запуске файл создаётся сам из значений ниже. Формат:
#   {"rules": [{"category": "...", "weight": 3, "patterns": ["регулярка", ...]}, ...],
#    "veto":  [{"category": "...", "weight": -99, "patterns": [...]}, ...],
#    "fuzzy": {"русификация": [4, "русификация"]}, "fuzzy_ratio": 0.8,
#    "replies": {"русификация": "Здравствуйте! ..."}}
# Веса: чем выше, тем интереснее сообщение. Вес <= -50 — не уведомлять никогда.
# Монитор НИКОГДА не пишет в чаты и не отвечает клиентам — только уведомляет мастеров.


@dataclass(frozen=True)
class Rule:
    category: str
    score: int
    pattern: "re.Pattern[str]"


DEFAULT_RULE_DEFS: Tuple[Tuple[str, int, Tuple[str, ...]], ...] = (
    ("купил/забрал машину", 3, (
        r"\b(купил[аи]?|приобр[её]л[аи]?|забрал[аи]?|забираю|заберу|"
        r"взял[аи]?\s+(машину|авто|мазд\w*|сх[- ]?5|cx[- ]?5)|пригнал[аи]?|"
        r"приехала|машина пришла|пришла машина|жду машину|заказал[аи]? машину|еду забирать|"
        r"в пути|на подходе|фура|контейнер|порт[уа]?|владивосток|оформляю|пересел)\b",)),
    ("таможня/доставка", 3, (
        r"\b(таможн\w*|растамож\w*|растаможил\w*|деклараци\w*|выпуск\w*|выпустил\w*|"
        r"на границе|на стоянке|стоянк\w+|сбктс|эптс|лаборатори\w*|брокер\w*|утильсбор\w*|"
        r"привезл\w+|поставк\w*)\b",)),
    ("русификация", 4, (
        r"\b(русификац\w*|руссификац\w*|руссификац\w*|русский язык|перевод меню|перевести меню|"
        r"меню на китайском|проблемы с языком|язык меню|локализаци\w*|на китайском|по-русски|"
        r"перевести|язык не переключа\w*|под русский|на русском|на русский)\b",)),
    ("скрытые функции", 3, (
        r"\b(скрыт\w+ (опци\w+|меню|функци\w+)|активаци\w*|активир\w+|активиров\w*|разблокир\w+|"
        r"включить скрыт\w+|i-stop|автозапуск\w*)\b",)),
    ("просьба о помощи", 3, (
        r"\b(помогите|подскажите|подскажите пожалуйста|не могу найти|как настроить|как включить|"
        r"как активировать|как перевести|что делать|нужна помощь)\b",)),
    ("сервисный интент", 2, (
        r"\b(где сделать|где активировать|где русифицировать|где можно|где делали|кто делает|"
        r"кто занимается|кто может|ищу мастера|ищу специалиста|есть контакты|посоветуйте|"
        r"порекомендуйте|к кому обратиться|кому отдать|кто в теме|подскажите мастера|"
        r"кто уже делал|есть отзывы|что посоветуете)\b",)),
    ("запись к мастеру", 3, (
        r"\b(записаться|запишусь|хочу записаться|запишите меня|можно записаться|есть окно|"
        r"свободн\w+ окн\w+|свободное время|возьм\w+ в субботу|возьм\w+ в воскресенье)\b",)),
    ("новый владелец", 2, (
        r"\b(стал[аи]? владельц\w+|новый владелец|только купил[аи]?|первый автомобиль|первая машина)\b",)),
    ("доработки", 2, (
        r"\b(антикор\w*|сигналк\w*|сигнализаци\w*|пленк\w*|бронепленк\w*|шумк\w*|полировк\w*|"
        r"керамик\w*|автозапуск\w*|тонировк\w*|защита радиатора|химчистк\w*)\b",)),
    ("модель Mazda CX-5", 1, (
        r"\b(cx[- ]?5|сх[- ]?5|cx5|сх5|мазда\s?сх5|mazda\s?cx[- ]?5)\b",)),
    ("машина из Китая", 1, (
        r"\b(из китая|китайск\w+|китаец|china|cn[- ]?верси\w*)\b",)),
    ("вопрос о цене", 1, (
        r"\b(сколько стоит|сколько будет|сколько по деньгам|поч[её]м|цена|цены|ценник|стоимость|"
        r"прайс|во сколько встанет|по деньгам|бюджет)\b",)),
    ("мультимедиа и меню", 1, (
        r"\b(магнитол\w*|приборк\w*|андроид авто|android auto|меню магнитолы|навигаци\w*|карты)\b",)),
    ("прошивка", 2, (
        r"\b(прошивк\w*|перепрошив\w*|обновление по|обновить по)\b",)),
)

DEFAULT_VETO_DEFS: Tuple[Tuple[str, int, Tuple[str, ...]], ...] = (
    ("продажа/реклама", -99, (
        r"\b(продам|продаю|прода[её]тся|продажа|в наличии|оптом|под заказ|на заказ|скидк\w*|"
        r"акци\w*|рассрочк\w*|выгодн\w+ предложени\w*|звоните|пишите в личку|whatsapp|ватсап|"
        r"подписывайтесь|реклама|барахолк\w*|магазин|доставка по россии|аренда|прокат|сдаю|сда[её]тся)\b",)),
    ("поиск работы/вакансия", -99, (
        r"\b(ищу работу|ваканси\w*|резюме|требуется (мастер|сотрудник|специалист)|нанимаю|подработка)\b",)),
    ("покупка авто, а не услуга", -99, (
        r"\b((куплю|ищу|подбираю)\s+(авто|машину|мазду|сх5|cx5)|подбор авто)\b",)),
    ("отказ от покупки", -5, (
        r"\b(не купил[аи]?|не взял[аи]?|не забрал[аи]?|передумал[аи]?|отказал\w*ся|отказал\w*ась)\b",)),
)


def compile_rules(defs: Sequence[Tuple[str, int, Sequence[str]]]) -> List[Rule]:
    """Собрать правила; кривая регулярка не должна ронять монитор."""
    rules: List[Rule] = []
    for category, weight, patterns in defs:
        if isinstance(patterns, str):
            patterns = [patterns]
        for pattern in patterns:
            if not isinstance(pattern, str) or not pattern.strip():
                continue  # случайно попавшие в список флаги/числа не должны становиться правилом
            try:
                rules.append(Rule(str(category), int(weight), re.compile(pattern, re.I)))
            except re.error as exc:
                LOG.error("Регулярка категории %r пропущена: %s", category, exc)
    return rules


RULES: Tuple[Rule, ...] = tuple(compile_rules(DEFAULT_RULE_DEFS))
VETO_RULES: Tuple[Rule, ...] = tuple(compile_rules(DEFAULT_VETO_DEFS))

# Опечатки: клиенты пишут «русификацыя», «антекор», «таможня» через «ж» и т.п.
FUZZY_KEYWORDS: Dict[str, Tuple[int, str]] = {
    "русификация": (4, "русификация"),
    "руссификация": (4, "русификация"),
    "локализация": (4, "русификация"),
    "активация": (3, "скрытые функции"),
    "антикор": (2, "доработки"),
    "сигнализация": (2, "доработки"),
    "автозапуск": (2, "доработки"),
    "таможня": (3, "таможня/доставка"),
    "лаборатория": (3, "таможня/доставка"),
}
# Порог подобран по разбору пар: опечатки «антекор→антикор» (0.857), «лабаратории→лаборатория»
# (0.818) проходят, а чужие слова «классификация→русификация» (0.750),
# «легализация→локализация» (0.818, но другие первые буквы) — нет.
FUZZY_RATIO = 0.80

# Готовые ответы: их видит мастер в уведомлении и отправляет сам (монитор в чаты не пишет).
REPLY_TEMPLATES: Dict[str, str] = {
    "русификация": "Здравствуйте! Русификация панели и меню на CX-5 из Китая — 35 000 ₽, "
                   "делаем за один день: русский язык, голосовое управление. Напишите город — "
                   "назову ближайшее свободное окно.",
    "купил/забрал машину": "Здравствуйте! Поздравляю с машиной. На CX-5 из Китая в первую очередь "
                           "обычно нужны русификация и активация скрытых опций. Напишите год и город — "
                           "подскажу, что доступно на вашей машине и когда можем принять.",
    "таможня/доставка": "Здравствуйте! Пока машина на таможне, можно записаться заранее: приедете — "
                        "сразу сделаем русификацию и активацию. В каком городе будете получать?",
    "скрытые функции": "Здравствуйте! Активация скрытых опций на CX-5 — 35 000 ₽ за любое количество. "
                       "Напишите год и комплектацию — скажу, что доступно именно на вашей машине.",
    "доработки": "Здравствуйте! По антикору и другим доработкам — подскажите город и модель, "
                 "назову цену и ближайшее окно.",
    "новый владелец": "Здравствуйте! С новым авто! Подскажу, что обычно делают первым делом на CX-5 "
                      "из Китая: русификация и активация скрытых опций. Напишите город и год — сориентирую по цене.",
}
DEFAULT_REPLY = ("Здравствуйте! Я мастер по CX-5 из Китая — русификация и активация скрытых опций. "
                 "Напишите год машины и что нужно — подскажу по цене и срокам.")

KEYWORDS_FILE = BASE_DIR / "keywords.json"
KEYWORDS_VERSION = 4


@dataclass
class KeywordConfig:
    """Скомпилированный словарь: правила, отсевы, опечатки и готовые ответы."""

    rules: List[Rule]
    veto: List[Rule]
    fuzzy: Dict[str, Tuple[int, str]]
    fuzzy_ratio: float
    replies: Dict[str, str]
    source: str = "встроенный"


def default_keywords() -> Dict[str, object]:
    """Словарь по умолчанию — то, что записывается в keywords.json при первом запуске."""
    return {
        "version": KEYWORDS_VERSION,
        "rules": [{"category": category, "weight": weight, "patterns": list(patterns)}
                  for category, weight, patterns in DEFAULT_RULE_DEFS],
        "veto": [{"category": category, "weight": weight, "patterns": list(patterns)}
                 for category, weight, patterns in DEFAULT_VETO_DEFS],
        "fuzzy": {word: [score, category] for word, (score, category) in FUZZY_KEYWORDS.items()},
        "fuzzy_ratio": FUZZY_RATIO,
        "replies": dict(REPLY_TEMPLATES),
    }


def _compile_keywords(raw: Dict[str, object], source: str) -> KeywordConfig:
    def defs(key: str) -> List[Tuple[str, int, Sequence[str]]]:
        result: List[Tuple[str, int, Sequence[str]]] = []
        for item in raw.get(key) or []:  # type: ignore[union-attr]
            if not isinstance(item, dict):
                continue
            patterns = item.get("patterns") or []
            if isinstance(patterns, str):
                patterns = [patterns]
            # только строки: случайно попавшие в файл числа не должны стать правилом
            patterns = [pattern for pattern in patterns if isinstance(pattern, str) and pattern.strip()]
            if not patterns:
                continue
            result.append((str(item.get("category", "без категории")), int(item.get("weight", 0) or 0),
                           patterns))
        return result

    rules = compile_rules(defs("rules")) or list(RULES)
    veto = compile_rules(defs("veto")) or list(VETO_RULES)

    fuzzy: Dict[str, Tuple[int, str]] = {}
    for word, value in (raw.get("fuzzy") or {}).items():  # type: ignore[union-attr]
        if isinstance(value, (list, tuple)) and len(value) == 2:
            fuzzy[str(word)] = (int(value[0]), str(value[1]))
    fuzzy = fuzzy or dict(FUZZY_KEYWORDS)

    replies = {str(key): str(value) for key, value in (raw.get("replies") or {}).items()  # type: ignore[union-attr]
               if isinstance(value, str) and value.strip()}
    return KeywordConfig(rules=rules, veto=veto, fuzzy=fuzzy,
                         fuzzy_ratio=float(raw.get("fuzzy_ratio") or FUZZY_RATIO),
                         replies=replies or dict(REPLY_TEMPLATES), source=source)


_KEYWORDS_CACHE: Optional[KeywordConfig] = None
_KEYWORDS_STAMP: Tuple[int, float] = (-1, 0.0)


def load_keywords(path: Optional[Path] = None, force: bool = False) -> KeywordConfig:
    """Загрузить словарь из keywords.json.

    Файла нет — создаём из значений по умолчанию и работаем. Файл правили — перечитываем
    автоматически (проверяется время изменения, перезапуск не нужен).
    """
    global _KEYWORDS_CACHE, _KEYWORDS_STAMP
    target = Path(path) if path is not None else KEYWORDS_FILE
    try:
        stamp = (target.stat().st_mtime_ns, target.stat().st_size) if target.exists() else (-1, 0.0)
    except OSError:
        stamp = (-1, 0.0)

    if not force and _KEYWORDS_CACHE is not None and stamp == _KEYWORDS_STAMP:
        return _KEYWORDS_CACHE

    if not target.exists():
        data = default_keywords()
        if _atomic_write_json(target, data):
            LOG.info("Создан словарь %s — слова можно править прямо в нём", target.name)
        _KEYWORDS_CACHE = _compile_keywords(data, source=f"{target.name} (создан)")
        _KEYWORDS_STAMP = stamp
        return _KEYWORDS_CACHE

    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("ожидался объект")
        if int(raw.get("version") or 0) != KEYWORDS_VERSION:
            # словарь от старой версии: сохраняем копию и создаём новый (там могли быть
            # сломанные правила), свои слова потом можно перенести из .bak
            backup = target.with_suffix(".json.bak")
            try:
                target.replace(backup)
            except OSError:
                pass
            LOG.warning("Словарь %s старой версии — создан заново (копия: %s)", target.name, backup.name)
            data = default_keywords()
            _atomic_write_json(target, data)
            _KEYWORDS_CACHE = _compile_keywords(data, source=f"{target.name} (обновлён)")
            _KEYWORDS_STAMP = (-1, 0.0)
            return _KEYWORDS_CACHE
    except (OSError, ValueError) as exc:
        LOG.error("Словарь %s не прочитан (%s) — работаю со встроенным", target, exc)
        _KEYWORDS_CACHE = KeywordConfig(list(RULES), list(VETO_RULES), dict(FUZZY_KEYWORDS),
                                        FUZZY_RATIO, dict(REPLY_TEMPLATES), source="встроенный")
        _KEYWORDS_STAMP = stamp
        return _KEYWORDS_CACHE

    _KEYWORDS_CACHE = _compile_keywords(raw, source=target.name)
    _KEYWORDS_STAMP = stamp
    return _KEYWORDS_CACHE


def _atomic_write_json(path: Path, payload: object) -> bool:
    """Записать JSON атомарно (временный файл + os.replace) с правами 600."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
        os.chmod(path, 0o600)
        return True
    except Exception as exc:  # noqa: BLE001
        LOG.error("Не удалось записать %s: %s", path, exc)
        return False


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
    known_users_files: List[Path] = field(default_factory=list)
    ignore_users: List[str] = field(default_factory=list)
    known_users_refresh_minutes: int = 5
    user_cooldown_hours: int = 168  # неделя: об одном человеке сообщаем один раз
    log_csv: Path = field(default_factory=lambda: BASE_DIR / "monitor_candidates.csv")
    daily_limit: int = 0  # 0 — без лимита уведомлений в сутки
    report_hour: int = 9  # час дневного отчёта (0-23), -1 — не присылать
    report_state: Path = field(default_factory=lambda: BASE_DIR / "monitor_report_state.json")
    timezone: str = "Europe/Moscow"
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
        cooldown = (f"повтор о человеке через {self.user_cooldown_hours} ч"
                    if self.user_cooldown_hours else "повторы о человеке разрешены")
        limit = f"лимит {self.daily_limit}/сутки" if self.daily_limit else "без лимита"
        report = (f"отчёт в {self.report_hour}:00 ({self.timezone})"
                  if self.report_hour >= 0 else "без отчёта")
        return (
            f"режим: {self.mode}, {scope}, получателей: {len(self.notify_users)}, {cooldown}, "
            f"{limit}, {report}, "
            f"порог веса: {self.min_score}, уведомления: "
            f"{'через бота' if self.bot_token else 'с личного аккаунта'}, настройки: {self.env_file or 'переменные окружения'}"
        )


def load_settings(env: Optional[Dict[str, str]] = None, explicit_env_file: Optional[str] = None,
                  require_session: bool = True) -> Settings:
    """Собрать настройки: файл .env_monitor, поверх — переменные окружения.

    require_session=False — для служебных команд (например, --forget), которым Telegram не нужен.
    """
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
    base_dir = env_file.parent if env_file is not None else BASE_DIR

    api_id_raw = (values.get("MONITOR_API_ID") or "").strip()
    api_hash_raw = (values.get("MONITOR_API_HASH") or "").strip()
    settings.api_hash = api_hash_raw or None
    if api_id_raw:
        settings.api_id = to_int(api_id_raw, "MONITOR_API_ID")

    session_value = (values.get("MONITOR_SESSION") or "").strip()
    if session_value:
        settings.session_path = _resolve_path(session_value, base_dir)
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
        settings.log_file = _resolve_path(log_value, base_dir)

    state_value = (values.get("MONITOR_STATE_FILE") or "").strip()
    if state_value:
        settings.state_file = _resolve_path(state_value, base_dir)

    settings.dry_run = (values.get("MONITOR_DRY_RUN", "").strip().lower() in {"1", "true", "yes", "on"})

    known = split_list(values.get("MONITOR_KNOWN_USERS_FILE", ""))
    if known:
        settings.known_users_files = [_resolve_path(item, base_dir) for item in known]
    else:
        settings.known_users_files = [path for path in DEFAULT_KNOWN_USER_FILES if path.exists()]
    settings.ignore_users = split_list(values.get("MONITOR_IGNORE_USERS", ""))
    settings.known_users_refresh_minutes = to_int(
        values.get("MONITOR_KNOWN_USERS_REFRESH", ""), "MONITOR_KNOWN_USERS_REFRESH", default=5)
    settings.user_cooldown_hours = to_int(
        values.get("MONITOR_USER_COOLDOWN_HOURS", ""), "MONITOR_USER_COOLDOWN_HOURS", default=168)
    settings.daily_limit = to_int(values.get("MONITOR_DAILY_LIMIT", ""), "MONITOR_DAILY_LIMIT", default=0)
    settings.report_hour = to_int(values.get("MONITOR_REPORT_HOUR", ""), "MONITOR_REPORT_HOUR", default=9)
    settings.timezone = (values.get("MONITOR_TZ") or "Europe/Moscow").strip()
    if settings.report_hour > 23:
        raise ConfigError(f"MONITOR_REPORT_HOUR должно быть 0-23, а получено {settings.report_hour}")
    csv_value = (values.get("MONITOR_LOG_CSV") or "").strip()
    if csv_value:
        settings.log_csv = _resolve_path(csv_value, base_dir)
    report_state_value = (values.get("MONITOR_REPORT_STATE") or "").strip()
    if report_state_value:
        settings.report_state = _resolve_path(report_state_value, base_dir)

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

    if require_session and settings.mode == "user" and not session_file_exists(settings.session_path):
        raise ConfigError(
            f"Не найден файл сессии {settings.session_path}.\n"
            "  Скопируйте mazda_session.session на сервер (права 600) — без него Telegram "
            "спросит номер телефона, а в службе это превратится в вечный «работающий» процесс."
        )
    return settings


# ============================== отбор сообщений ==============================


def fuzzy_categories(text: str, cfg: Optional[KeywordConfig] = None) -> List[Tuple[str, int]]:
    """Категории, найденные с учётом опечаток (для длинных слов)."""
    cfg = cfg or load_keywords()
    found: List[Tuple[str, int]] = []
    words = re.findall(r"[а-яёa-z]{6,}", text.lower())
    if not words:
        return found
    seen = set()
    for word in words:
        for keyword, (score, category) in cfg.fuzzy.items():
            if category in seen:
                continue
            if abs(len(word) - len(keyword)) > 3:
                continue
            # первые две буквы должны совпадать: иначе «легализация» сойдёт за «локализацию»
            if word[:2] != keyword[:2]:
                continue
            if difflib.SequenceMatcher(None, word, keyword).ratio() >= cfg.fuzzy_ratio:
                found.append((category, score))
                seen.add(category)
    return found


def score_message(text: str, cfg: Optional[KeywordConfig] = None) -> Tuple[int, List[str], Optional[str]]:
    """Оценка сообщения: (вес, категории, причина отказа)."""
    cfg = cfg or load_keywords()
    categories: List[str] = []
    score = 0

    for rule in cfg.veto:
        if rule.pattern.search(text):
            if rule.score <= -50:
                return 0, [], rule.category
            score += rule.score
            categories.append(rule.category)

    for rule in cfg.rules:
        if rule.pattern.search(text):
            score += rule.score
            categories.append(rule.category)

    for category, extra in fuzzy_categories(text, cfg):
        if category not in categories:
            score += extra
            categories.append(category)

    return score, categories, None


def suggest_reply(categories: Sequence[str], cfg: Optional[KeywordConfig] = None) -> str:
    """Готовый ответ под ситуацию — мастер копирует и отправляет сам."""
    cfg = cfg or load_keywords()
    weights = {rule.category: rule.score for rule in cfg.rules}
    best: Optional[str] = None
    best_weight = -1
    for category in categories:
        if category in cfg.replies and weights.get(category, 0) > best_weight:
            best, best_weight = category, weights.get(category, 0)
    return cfg.replies.get(best or "", DEFAULT_REPLY) if best else DEFAULT_REPLY


def build_notification(*, sender_name: str, text: str, chat_title: str, chat_username: Optional[str],
                       message_id: int, categories: Sequence[str], score: int,
                       reply: Optional[str] = None) -> str:
    """Текст уведомления: простой текст без разметки, чтобы ничего не «съедалось».

    В конце — готовый ответ: монитор сам в чаты не пишет, мастер копирует и отправляет.
    """
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
    if reply:
        lines += ["", "✍️ Готовый ответ (скопируйте и отправьте):", reply]
    return "\n".join(lines)


USER_KEY_PREFIX = "user:"


def message_key(chat_id: int, message_id: int) -> str:
    return f"{chat_id}:{message_id}"


def human_delta(timestamp: float, now: Optional[float] = None) -> str:
    """«меньше часа назад», «5 ч назад», «3 дн назад» — для понятного лога."""
    hours = max(int(((now or time.time()) - timestamp) // 3600), 0)
    if hours < 1:
        return "меньше часа назад"
    if hours < 24:
        return f"{hours} ч назад"
    return f"{hours // 24} дн назад"


def text_key(sender_id: Optional[int], text: str) -> str:
    digest = hashlib.sha1(" ".join((text or "").lower().split()).encode("utf-8")).hexdigest()[:16]
    return f"text:{sender_id}:{digest}"


# ============================== память об отправленном ==============================


class SeenStore:
    """Помнит, что уже отправляли: чтобы не дублировать одно и то же сообщение.

    Файл ограничен по количеству записей и времени жизни (по умолчанию сутки).
    """

    def __init__(self, path: Path, ttl_hours: int = 24, limit: int = 5000,
                 user_cooldown_hours: int = 168) -> None:
        self.path = path
        self.ttl = max(ttl_hours, 0) * 3600
        self.user_ttl = max(user_cooldown_hours, 0) * 3600
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
        # у записей о людях срок больше: о клиенте сообщаем один раз за кулдаун,
        # а одинаковые тексты сообщений помним сутки
        now = time.time()
        for key, stamp in list(self._data.items()):
            ttl = self.user_ttl if key.startswith(USER_KEY_PREFIX) else self.ttl
            if ttl and now - stamp > ttl:
                self._data.pop(key, None)
        if len(self._data) > self.limit:
            for key, _ in sorted(self._data.items(), key=lambda item: item[1])[: len(self._data) - self.limit]:
                self._data.pop(key, None)

    def seen(self, key: str) -> bool:
        return key in self._data

    def seen_at(self, key: str) -> Optional[float]:
        """Когда мы в последний раз добавляли эту запись (None — не было)."""
        return self._data.get(key)

    def forget(self, needle: str) -> int:
        """Забыть записи про человека: снова сможем о нём сообщить."""
        needle = needle.strip().lstrip("@")
        removed = [key for key in self._data if needle in key]
        for key in removed:
            self._data.pop(key, None)
        if removed:
            self.save()
        return len(removed)

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


def repeat_reason(store: "SeenStore", sender_id: Optional[int], cooldown_hours: int) -> Optional[float]:
    """Когда мы уже сообщали об этом человеке (None — не сообщали или кулдаун выключен).

    Логика продаж: о клиенте мастерам сообщают один раз, а не на каждое его сообщение.
    """
    if not sender_id or cooldown_hours <= 0:
        return None
    return store.seen_at(f"{USER_KEY_PREFIX}{int(sender_id)}")


# ============================== журнал кандидатов и отчёт ==============================

CSV_HEADER = ("time", "action", "reason", "score", "categories", "chat", "author", "link", "text")


def _safe_cell(value: object, limit: int = 300) -> str:
    """Значение для CSV: без переводов строк, с защитой от формул Excel."""
    text = " ".join(str(value or "").split())[:limit]
    if text[:1] in ("=", "+", "-", "@"):
        text = "'" + text
    return text


class CandidateLog:
    """CSV-журнал: что нашли, что отсеяли и почему — по нему видно, как настроить словарь."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def add(self, *, action: str, reason: str = "", score: int = 0, categories: Sequence[str] = (),
            chat: str = "", author: str = "", link: str = "", text: str = "") -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            is_new = not self.path.exists() or self.path.stat().st_size == 0
            with self.path.open("a", encoding="utf-8", newline="") as handle:
                if is_new:
                    try:
                        os.chmod(self.path, 0o600)  # там переписка и юзернеймы клиентов
                    except OSError:
                        pass
                writer = csv.writer(handle)
                if is_new:
                    writer.writerow(CSV_HEADER)
                writer.writerow([
                    time.strftime("%Y-%m-%d %H:%M:%S"), action, reason, score,
                    ", ".join(categories), _safe_cell(chat, 80), _safe_cell(author, 80),
                    _safe_cell(link, 200), _safe_cell(text),
                ])
        except OSError as exc:
            LOG.warning("Не удалось записать кандидата в %s: %s", self.path, exc)

    def rows(self, days: int = 1) -> List[Dict[str, str]]:
        """Записи за последние N суток (по дате в первой колонке)."""
        try:
            with self.path.open("r", encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
        except FileNotFoundError:
            return []
        except OSError as exc:
            LOG.warning("Не удалось прочитать %s: %s", self.path, exc)
            return []
        edge = time.time() - max(days, 1) * 86400
        result = []
        for row in rows:
            try:
                stamp = time.mktime(time.strptime(row.get("time", ""), "%Y-%m-%d %H:%M:%S"))
            except (TypeError, ValueError):
                continue
            if stamp >= edge:
                result.append(row)
        return result

    def count_today(self, action: str) -> int:
        today = time.strftime("%Y-%m-%d")
        return sum(1 for row in self.rows(days=1)
                   if row.get("action") == action and str(row.get("time", "")).startswith(today))


def build_report(rows: Sequence[Dict[str, str]]) -> str:
    """Дневная сводка: сколько нашли, сколько уведомили, что отсеяли."""
    notified = sum(1 for row in rows if row.get("action") == "notified")
    found = sum(1 for row in rows if row.get("action") in {"notified", "found"})
    reasons: Dict[str, int] = {}
    categories: Dict[str, int] = {}
    chats: Dict[str, int] = {}
    for row in rows:
        if row.get("action") == "skipped":
            reason = row.get("reason") or "без причины"
            reasons[reason] = reasons.get(reason, 0) + 1
        for category in str(row.get("categories") or "").split(","):
            category = category.strip()
            if category:
                categories[category] = categories.get(category, 0) + 1
        chat = (row.get("chat") or "").strip()
        if chat and row.get("action") == "notified":
            chats[chat] = chats.get(chat, 0) + 1

    def top(data: Dict[str, int], limit: int = 3) -> str:
        items = sorted(data.items(), key=lambda item: item[1], reverse=True)[:limit]
        return ", ".join(f"{name} {count}" for name, count in items) or "—"

    lines = [
        "📊 Монитор за сутки",
        f"Найдено кандидатов: {found}, уведомлено: {notified}, отсеяно: {sum(reasons.values())}",
        f"Причины отсева: {top(reasons, 4)}",
        f"Категории: {top(categories, 4)}",
        f"Чаты: {top(chats, 3)}",
    ]
    return "\n".join(lines)


class ReportState:
    """Помнит, за какой день отчёт уже отправлен, чтобы не дублировать после перезапуска."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.last_date = ""
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self.last_date = str(raw.get("last_date") or "")
        except (FileNotFoundError, ValueError, OSError):
            pass

    def should_send(self, today: str) -> bool:
        return self.last_date != today

    def mark(self, today: str) -> None:
        self.last_date = today
        _atomic_write_json(self.path, {"last_date": today})


# ============================== те, кто уже писал боту ==============================


class KnownUsers:
    """Люди, которые уже общаются с ботом: их не показываем мастерам как новых клиентов.

    Читает файлы бота (data/bot_users.json, contacts.json, bookings.json) и обновляет
    список раз в несколько минут — бот пишет в них по мере обращений.
    """

    def __init__(self, files: Sequence[Path], extra: Sequence[str] = (), refresh_minutes: int = 5) -> None:
        self.files = list(files)
        self.refresh_seconds = max(refresh_minutes, 0) * 60
        self.ids: set = set()
        self.usernames: set = set()
        self._loaded_at = 0.0
        self._add_extra(extra)
        self.refresh(force=True)

    def _add_extra(self, extra: Sequence[str]) -> None:
        for value in extra:
            item = value.strip()
            if not item:
                continue
            if re.fullmatch(r"-?\d+", item):
                self.ids.add(int(item))
            else:
                self.usernames.add(item.lstrip("@").lower())

    def refresh(self, force: bool = False) -> bool:
        """Обновить список; возвращает True, если читали файлы."""
        if not force and self.refresh_seconds:
            if time.time() - self._loaded_at < self.refresh_seconds:
                return False
        if not force and not self.refresh_seconds:
            return False

        ids: set = set()
        usernames: set = set()
        for path in self.files:
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                continue
            except (OSError, ValueError) as exc:
                LOG.warning("Список известных пользователей %s не прочитан: %s", path, exc)
                continue

            if isinstance(raw, dict):
                for key, value in raw.items():
                    if re.fullmatch(r"-?\d+", str(key)):
                        ids.add(int(key))
                    if isinstance(value, dict):
                        name = str(value.get("username") or "").lstrip("@").lower()
                        if name:
                            usernames.add(name)
            elif isinstance(raw, list):
                for item in raw:
                    if not isinstance(item, dict):
                        continue
                    for key_name in ("user_id", "id", "telegram_id"):
                        candidate = item.get(key_name)
                        if isinstance(candidate, int) or (isinstance(candidate, str) and candidate.isdigit()):
                            ids.add(int(candidate))
                            break
                    name = str(item.get("username") or "").lstrip("@").lower()
                    if name:
                        usernames.add(name)

        self.ids |= ids
        self.usernames |= usernames
        self._loaded_at = time.time()
        return True

    def is_known(self, user_id: Optional[int], username: Optional[str] = None) -> Optional[str]:
        """Причина, по которой человека не показываем мастерам, или None."""
        if user_id is not None and int(user_id) in self.ids:
            return "уже писал боту"
        if username:
            if username.lstrip("@").lower() in self.usernames:
                return "уже писал боту"
        return None

    def describe(self) -> str:
        files = ", ".join(path.name for path in self.files) or "нет файлов"
        return f"известных пользователей: {len(self.ids)} id и {len(self.usernames)} юзернеймов ({files})"


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
        self._dialogs_loaded = False

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

    async def _resolve_target(self, target: str):
        """Кому отправить: юзернейм, id или уже известная сущность.

        Отправка «в никуда» по числовому id не работает: Telethon не знает access_hash,
        пока не увидит диалог. Поэтому при первом промахе один раз загружаем диалоги
        (кэш сущностей) и пробуем снова.
        """
        value = str(target).strip()
        if not value.lstrip("-").isdigit():
            return await self.client.get_entity(value)
        try:
            return await self.client.get_entity(int(value))
        except Exception as exc:  # noqa: BLE001 — пробуем после загрузки диалогов
            LOG.debug("Получатель %s не найден в кэше (%s) — загружаю диалоги", value, exc)
            if not self._dialogs_loaded:
                try:
                    await self.client.get_dialogs(limit=None)
                    self._dialogs_loaded = True
                except Exception as dialogs_exc:  # noqa: BLE001
                    LOG.warning("Не удалось загрузить диалоги аккаунта: %s", dialogs_exc)
            return await self.client.get_entity(int(value))

    async def _send_from_account(self, target: str, text: str) -> None:
        from telethon.errors import FloodWaitError  # импорт здесь: без telethon модуль импортируется для тестов

        entity = await self._resolve_target(target)
        try:
            await self.client.send_message(entity, text)
        except FloodWaitError as exc:
            wait = int(getattr(exc, "seconds", 60)) + 5
            LOG.warning("Telegram просит подождать %s с (лимит отправки) — жду и повторяю", wait)
            await asyncio.sleep(wait)
            await self.client.send_message(entity, text)


# ============================== работа с Telegram ==============================


class MonitorLock:
    """Блокировка файла сессии: второй процесс не должен подключаться тем же аккаунтом.

    Telegram может отозвать ключ, если одна сессия используется из двух мест одновременно,
    поэтому мониторинг и --check не должны работать параллельно.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._handle = None

    def acquire(self) -> bool:
        try:
            import fcntl
        except ImportError:  # не Unix — блокировки нет, работаем как раньше
            return True
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._handle = open(self.path, "w", encoding="utf-8")
            try:
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                self._handle.close()
                self._handle = None
                return False
            self._handle.write(str(os.getpid()))
            self._handle.flush()
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass
            return True
        except OSError as exc:
            LOG.warning("Не удалось создать блокировку %s: %s", self.path, exc)
            return True  # не мешаем работе из-за проблем с файлом блокировки

    def release(self) -> None:
        if self._handle is not None:
            try:
                self._handle.close()
            except OSError:
                pass
            self._handle = None


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
    lock = MonitorLock(settings.session_path.with_suffix(".lock"))
    if not lock.acquire():
        raise ConfigError(
            "Монитор уже запущен (файл сессии занят). Остановите службу перед повторным запуском: "
            "systemctl stop mazda-monitor"
        )

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

    store = SeenStore(settings.state_file, ttl_hours=settings.seen_hours,
                      user_cooldown_hours=settings.user_cooldown_hours)
    notifier = Notifier(settings, client)
    known = KnownUsers(settings.known_users_files, settings.ignore_users,
                       settings.known_users_refresh_minutes)
    cfg = load_keywords()
    LOG.info("Словарь: %s, правил: %s, отсевов: %s", cfg.source, len(cfg.rules), len(cfg.veto))
    LOG.info("Фильтр «уже писал боту»: %s", known.describe())
    stats = {"seen": 0, "found": 0, "skipped": 0, "known": 0, "repeat": 0, "undelivered": 0}

    log = CandidateLog(settings.log_csv)
    report_state = ReportState(settings.report_state)
    LOG.info("Журнал кандидатов: %s", settings.log_csv)

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
        stats["seen"] += 1
        if stats["seen"] % 100 == 0:
            LOG.info("Перехвачено сообщений: %s", stats["seen"])
        if len(text) < 10:
            return
        LOG.debug("Сообщение (чат %s): %s", getattr(message, "chat_id", "?"), text[:60])

        keys = (message_key(message.chat_id, message.id), text_key(getattr(event, "sender_id", None), text))
        if store.seen(keys[0]) or store.seen(keys[1]):
            LOG.debug("Повтор уже отправляли: %s", keys[0])
            return

        sender = None
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
        link = f"https://t.me/{chat_username}/{message.id}" if chat_username else ""

        def log_row(action: str, reason: str = "", score: int = 0,
                    categories: Sequence[str] = ()) -> None:
            log.add(action=action, reason=reason, score=score, categories=categories,
                    chat=chat_title, author=sender_name.lstrip("@"), link=link, text=text)

        # Уже общается с ботом — мастерам сообщать не нужно, это не новый клиент
        known.refresh()
        reason = known.is_known(getattr(sender, "id", None), getattr(sender, "username", None))
        if reason:
            stats["known"] += 1
            LOG.info("Пропуск (%s): %s — %s", reason, sender_name, text[:50])
            log_row("skipped", reason)
            store.add(keys[0])
            return

        # Об этом человеке уже сообщали — второй раз мастеров не дёргаем
        sender_id = getattr(sender, "id", None)
        repeat_at = repeat_reason(store, sender_id, settings.user_cooldown_hours)
        if repeat_at is not None:
            stats["repeat"] += 1
            LOG.info("Пропуск (о нём уже сообщали %s): %s — %s",
                     human_delta(repeat_at), sender_name, text[:50])
            log_row("skipped", f"о нём уже сообщали ({human_delta(repeat_at)})")
            store.add(keys[0])
            return

        score, categories, veto = score_message(text)
        if veto:
            stats["skipped"] += 1
            LOG.info("Пропуск (отсев: %s): %s", veto, text[:60])
            log_row("skipped", f"отсев: {veto}", score, categories)
            return
        if score < settings.min_score:
            stats["skipped"] += 1
            LOG.debug("Пропуск (вес %s < %s): %s", score, settings.min_score, text[:60])
            log_row("skipped", f"вес {score} < {settings.min_score}", score, categories)
            return

        # Дневной лимит: защита от потока (0 — без лимита)
        if settings.daily_limit and log.count_today("notified") >= settings.daily_limit:
            stats["skipped"] += 1
            LOG.info("Пропуск (дневной лимит %s): %s", settings.daily_limit, text[:50])
            log_row("skipped", "дневной лимит", score, categories)
            store.add(keys[0])
            return

        stats["found"] += 1
        notification = build_notification(
            sender_name=sender_name, text=text, chat_title=chat_title, chat_username=chat_username,
            message_id=message.id, categories=categories, score=score,
            reply=suggest_reply(categories),
        )
        if settings.dry_run:
            LOG.info("[проверка] уведомление не отправлено:\n%s", notification)
            log_row("skipped", "проверка (dry-run)", score, categories)
            return  # в проверке ничего не помечаем

        delivered = await notifier.send(notification)
        if not delivered:
            # Не помечаем лид: иначе он «сгорит» на неделю, так и не дойдя до мастера
            stats["undelivered"] += 1
            log_row("skipped", "не доставлено", score, categories)
            LOG.error("Уведомление не дошло до получателей — лид не помечаю, "
                      "повторю при следующем сообщении от этого человека")
            return

        log_row("notified", "отправлено", score, categories)
        store.add(keys[0])
        store.add(keys[1])
        if sender_id:
            store.add(f"{USER_KEY_PREFIX}{int(sender_id)}")  # об этом человеке больше не сообщаем

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
                f"🫀 Монитор жив. Чатов: {len(resolved)}, перехвачено: {stats['seen']}, "
                f"найдено: {stats['found']}, "
                f"отсеяно: {stats['skipped']}, уже писали боту: {stats['known']}, "
                f"повторные о человеке: {stats['repeat']}."
            )

    async def daily_reporter() -> None:
        """Раз в сутки — сводка: сколько нашли, уведомили, отсеяли."""
        while True:
            today = time.strftime("%Y-%m-%d")
            if time.localtime().tm_hour == settings.report_hour and report_state.should_send(today):
                try:
                    await notifier.send(build_report(log.rows(days=1)))
                    report_state.mark(today)
                    LOG.info("Дневной отчёт отправлен")
                except Exception as exc:  # noqa: BLE001 — отчёт не должен ронять монитор
                    LOG.error("Не удалось отправить дневной отчёт: %s", exc)
            await asyncio.sleep(60)

    heartbeat_task = asyncio.create_task(heartbeat()) if settings.heartbeat_minutes > 0 else None
    report_task = asyncio.create_task(daily_reporter()) if settings.report_hour >= 0 else None
    try:
        await client.run_until_disconnected()
    finally:
        if heartbeat_task is not None:
            heartbeat_task.cancel()
        if report_task is not None:
            report_task.cancel()
        await client.disconnect()
        LOG.info("Монитор остановлен. Перехвачено: %s, найдено: %s, отсеяно по правилам: %s, "
                 "не доставлено: %s, уже писали боту: %s, "
                 "повторные о человеке: %s",
                 stats["seen"], stats["found"], stats["skipped"], stats["undelivered"],
                 stats["known"], stats["repeat"])
    return 0


async def check_mode(settings: Settings) -> int:
    """Проверка настроек и чатов: python3 mazda_monitor.py --check."""
    from telethon import TelegramClient

    LOG.info("Настройки: %s", settings.summary())
    ensure_session_permissions(settings.session_path)
    lock = MonitorLock(settings.session_path.with_suffix(".lock"))
    if not lock.acquire():
        LOG.error("Монитор сейчас работает — проверка откроет вторую сессию и может сбить ключ. "
                  "Остановите службу: systemctl stop mazda-monitor, затем повторите --check")
        return 3

    client = TelegramClient(str(settings.session_path), settings.api_id, settings.api_hash)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            LOG.error("Сессия не авторизована — нужен вход или перенос рабочего файла сессии")
            return 2
        me = await client.get_me()
        LOG.info("Аккаунт: %s (id %s, @%s)", getattr(me, "first_name", "?"), getattr(me, "id", "?"),
                 getattr(me, "username", "нет"))

        if settings.all_chats:
            groups = await list_account_groups(client)
            excluded = await resolve_excluded(client, settings.chats_exclude)
            LOG.info("Групп и каналов у аккаунта: %s (исключено: %s)", len(groups), len(excluded))
            for entity in groups[:25]:
                LOG.info("  • %s", getattr(entity, "title", "?") or getattr(entity, "username", "?"))
            if len(groups) > 25:
                LOG.info("  … и ещё %s", len(groups) - 25)
            if not groups:
                LOG.error("Аккаунт не состоит ни в одной группе — слушать нечего, монитор не запустится")
                return 2
            LOG.info("Можно запускать монитор: слушает все группы аккаунта")
            return 0

        resolved, failed = await resolve_chats(client, settings.chats)
        LOG.info("Чатов подключено: %s из %s", len(resolved), len(settings.chats))
        for name, error in failed:
            LOG.warning("  не найден: %s (%s)", name, error[:120])
        if not failed:
            LOG.info("Все чаты на месте — можно запускать монитор")
        known = KnownUsers(settings.known_users_files, settings.ignore_users,
                           settings.known_users_refresh_minutes)
        LOG.info("Фильтр «уже писал боту»: %s", known.describe())
        return 0 if resolved else 2
    finally:
        await client.disconnect()


# ============================== запуск ==============================


def apply_timezone(name: str) -> None:
    """Часовой пояс процесса: иначе на сервере в UTC «сутки» начинаются в 21:00 по Москве."""
    try:
        os.environ["TZ"] = name
        time.tzset()
    except (AttributeError, OSError) as exc:  # Windows/неизвестный пояс — работаем как есть
        LOG.warning("Не удалось применить часовой пояс %s: %s", name, exc)


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
    parser.add_argument("--forget", metavar="ID_ИЛИ_ЮЗЕРНЕЙМ",
                        help="забыть человека: снова сообщать о нём мастерам")
    parser.add_argument("--report", nargs="?", const=1, type=int, metavar="ДНЕЙ",
                        help="показать сводку по журналу кандидатов (по умолчанию за сутки)")
    args = parser.parse_args(argv)

    try:
        settings = load_settings(explicit_env_file=args.env_file,
                                 require_session=not (args.forget or args.report))
    except ConfigError as exc:
        setup_logging(None)
        LOG.error("%s", exc)
        return 2

    apply_timezone(settings.timezone)
    setup_logging(settings.log_file, verbose=args.verbose)

    if args.report is not None:
        rows = CandidateLog(settings.log_csv).rows(days=max(args.report, 1))
        print(build_report(rows))
        return 0

    if args.forget:
        store = SeenStore(settings.state_file, ttl_hours=settings.seen_hours,
                          user_cooldown_hours=settings.user_cooldown_hours)
        removed = store.forget(args.forget)
        LOG.info("Забыто записей про %s: %s", args.forget, removed)
        return 0

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

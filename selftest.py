#!/usr/bin/env python3
"""Офлайн-проверка логики монитора: без сети, без Telegram и без telethon.

Запуск:  python3 selftest.py

Проверяется то, что чаще всего ломается: отбор сообщений (шум против реальных клиентов),
разбор настроек и понятные ошибки, память об отправленном, разбор списка чатов
и текст уведомления.
"""
from __future__ import annotations

import os
import sys
import json
import tempfile
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

import mazda_monitor as mon  # noqa: E402

TMP_DIR = Path(tempfile.mkdtemp(prefix="mazda-monitor-selftest-"))
RESULTS = []


def check(name: str, condition: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(condition), detail))
    print(("✅ " if condition else "❌ ") + name + ("" if condition else f" — {detail}"))


# ------------------------------- отбор сообщений -------------------------------


def test_real_leads_pass():
    leads = [
        "Забрал машину вчера, всё супер, всем спасибо!",
        "Наконец пригнал свою CX-5 из Китая, что нужно сделать в первую очередь?",
        "Подскажите, как настроить Яндекс на магнитоле?",
        "Машина на таможне, скоро забираю",
        "Сколько стоит русификация панели?",
        "Кто знает, сколько стоит пленка на капот?",
        "Активировал скрытые функции, спасибо!",
    ]
    for text in leads:
        score, categories, veto = mon.score_message(text)
        check(f"клиент проходит: {text[:42]!r}", veto is None and score >= 3,
              f"вес {score}, отсев {veto}, категории {categories}")


def test_noise_is_filtered():
    noise = [
        ("Продам бронепленку и антикор, делаю сигнализации", "продажа/реклама"),
        ("Запчасти из Китая оптом, свечи, фильтры, доставка", "продажа/реклама"),
        ("Ищу работу в лаборатории, опыт 5 лет", "поиск работы/вакансия"),
        ("Скидка 30% на антикор до конца недели!", "продажа/реклама"),
        ("Продаю свою CX-5 2021 из Китая, без пробега по РФ", "продажа/реклама"),
        ("Требуется мастер по сигнализациям, вакансия открыта", "поиск работы/вакансия"),
    ]
    for text, expected in noise:
        score, categories, veto = mon.score_message(text)
        check(f"шум отсеян: {text[:42]!r}", veto == expected, f"получили отсев {veto}, вес {score}")


def test_negation_and_weak_matches():
    score, categories, veto = mon.score_message("Я не купил машину, передумал, деньги вернули")
    check("отрицание не превращается в клиента", veto is None and score < 3, f"вес {score}, отсев {veto}")
    score, _, veto = mon.score_message("Всем привет, хорошая погода сегодня")
    check("обычная болтовня не уведомляет", veto is None and score < 3, f"вес {score}")


def test_typos_are_tolerated():
    for text in ["нужна русификацыя панели", "сделайте руссификацыю меню", "интересует антекор"]:
        score, categories, veto = mon.score_message(text)
        check(f"опечатка распознана: {text[:34]!r}", score >= 2 and not veto,
              f"вес {score}, категории {categories}")


def test_weights_priority():
    weak, _, _ = mon.score_message("Что-то про Китай")
    strong, _, _ = mon.score_message("Нужна русификация и активация скрытых опций")
    check("важный запрос весит больше общего упоминания", strong > weak, f"{strong} против {weak}")
    score, categories, _ = mon.score_message("Машина на таможне, жду оформление")
    check("категория таможни определяется", "таможня/доставка" in categories, str(categories))


# ------------------------------- настройки -------------------------------


def test_env_parsing():
    path = TMP_DIR / "env_ok"
    path.write_text(
        "# комментарий\n"
        "MONITOR_API_ID=\"12345\"\n"
        "export MONITOR_API_HASH='abcdef0123456789abcdef0123456789'\n"
        "MONITOR_NOTIFY_USERS= 120317642 , 1882208554 ,, \n"
        "MONITOR_CHATS=@mazdachinacx5, t.me/mazda_club_cx5\n"
        "; ещё комментарий\n",
        encoding="utf-8",
    )
    values = mon.read_env_file(path)
    check("кавычки убираются", values["MONITOR_API_ID"] == "12345", values.get("MONITOR_API_ID", ""))
    check("export разбирается", values["MONITOR_API_HASH"].startswith("abcdef01"))
    check("список получателей чистится",
          mon.split_list(values["MONITOR_NOTIFY_USERS"]) == ["120317642", "1882208554"],
          str(mon.split_list(values["MONITOR_NOTIFY_USERS"])))


def test_settings_validation():
    try:
        mon.load_settings(env={"MONITOR_API_ID": "1", "MONITOR_API_HASH": "x"})
        check("без получателей настройки не принимаются", False, "ошибки не было")
    except mon.ConfigError as exc:
        check("без получателей понятная ошибка", "MONITOR_NOTIFY_USERS" in str(exc), str(exc)[:80])

    try:
        mon.load_settings(env={"MONITOR_NOTIFY_USERS": "1"})
        check("без доступов настройки не принимаются", False, "ошибки не было")
    except mon.ConfigError as exc:
        check("без доступов подсказка про my.telegram.org",
              "my.telegram.org" in str(exc) and "MONITOR_BOT_TOKEN" in str(exc), str(exc)[:90])

    try:
        mon.load_settings(env={
            "MONITOR_NOTIFY_USERS": "1", "MONITOR_API_ID": "1", "MONITOR_API_HASH": "x",
            "MONITOR_SESSION": str(TMP_DIR / "нет-такого-файла"),
        })
        check("отсутствие сессии отлавливается", False, "ошибки не было")
    except mon.ConfigError as exc:
        check("отсутствие сессии — понятная ошибка", "сессии" in str(exc), str(exc)[:90])


def test_settings_ok():
    session = TMP_DIR / "mazda_session.session"
    session.write_bytes(b"stub")
    settings = mon.load_settings(env={
        "MONITOR_API_ID": "12345",
        "MONITOR_API_HASH": "abcdef0123456789abcdef0123456789",
        "MONITOR_NOTIFY_USERS": "120317642,@master",
        "MONITOR_SESSION": str(session.with_suffix("")),
        "MONITOR_MIN_SCORE": "4",
        "MONITOR_STATE_FILE": str(TMP_DIR / "state.json"),
    })
    check("режим личного аккаунта", settings.mode == "user", settings.mode)
    check("порог веса из настроек", settings.min_score == 4, str(settings.min_score))
    check("получатели разобраны", settings.notify_users == ["120317642", "@master"], str(settings.notify_users))
    check("чаты по умолчанию подставлены", len(settings.chats) == len(mon.DEFAULT_CHATS), str(settings.chats))
    check("в описании есть режим и получатели", "режим: user" in settings.summary(), settings.summary())

    bot_settings = mon.load_settings(env={
        "MONITOR_NOTIFY_USERS": "120317642",
        "MONITOR_BOT_TOKEN": "123456:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw",
    })
    check("без api_id, но с токеном бота — режим bot", bot_settings.mode == "bot", bot_settings.mode)


def test_env_file_priority():
    """Переменные окружения важнее файла настроек."""
    session = TMP_DIR / "prio.session"
    session.write_bytes(b"stub")
    path = TMP_DIR / "env_prio"
    path.write_text(
        "MONITOR_API_ID=1\nMONITOR_API_HASH=from-file\n"
        "MONITOR_NOTIFY_USERS=file-user\n"
        f"MONITOR_SESSION={session.with_suffix('')}\n",
        encoding="utf-8",
    )
    saved = os.environ.get("MONITOR_NOTIFY_USERS")
    os.environ["MONITOR_NOTIFY_USERS"] = "env-user"
    try:
        settings = mon.load_settings(explicit_env_file=str(path))
    finally:
        if saved is None:
            os.environ.pop("MONITOR_NOTIFY_USERS", None)
        else:
            os.environ["MONITOR_NOTIFY_USERS"] = saved
    check("переменная окружения перекрывает файл", settings.notify_users == ["env-user"], str(settings.notify_users))


# ------------------------------- уведомление и память -------------------------------


def test_notification_text():
    text = mon.build_notification(
        sender_name="@client", text="Забрал машину, " + "о" * 400, chat_title="Mazda CX-5 Китай",
        chat_username="mazdachinacx5", message_id=777, categories=["купил/забрал машину"], score=3,
    )
    check("в уведомлении есть автор", "@client" in text)
    check("ссылка на сообщение собрана", "https://t.me/mazdachinacx5/777" in text)
    check("длинный текст обрезан многоточием", "…" in text and len(text) < 600, str(len(text)))
    check("нет markdown-разметки, которую можно случайно съесть", "*" not in text and "_" not in text)
    no_link = mon.build_notification(
        sender_name="Иван", text="Пригнал машину из Китая", chat_title="Чат",
        chat_username=None, message_id=1, categories=["машина из Китая"], score=1,
    )
    check("без юзернейма ссылки нет, но уведомление есть", "🔗" not in no_link and "Иван" in no_link)


def test_seen_store():
    path = TMP_DIR / "state.json"
    store = mon.SeenStore(path, ttl_hours=24, limit=3)
    store.add("a")
    check("запись запоминается", store.seen("a"))
    check("чужая запись не считается своей", not store.seen("b"))
    store.add("b")
    store.add("c")
    store.add("d")
    check("размер ограничен", len(store._data) <= 3, str(len(store._data)))
    check("файл создан с правами 600", stat_mode(path) == 0o600, oct(stat_mode(path)))

    reopened = mon.SeenStore(path, ttl_hours=24, limit=10)
    check("память переживает перезапуск", len(reopened._data) >= 1, str(len(reopened._data)))

    old = mon.SeenStore(TMP_DIR / "state_old.json", ttl_hours=1, limit=10)
    old._data["stale"] = time.time() - 7200
    old._prune()
    check("старые записи вычищаются", "stale" not in old._data)
    check("ключ сообщения собирается верно", mon.message_key(-100123, 5) == "-100123:5")
    check("одинаковый текст даёт одинаковый ключ",
          mon.text_key(1, "Привет   мир") == mon.text_key(1, "привет мир"),
          "нормализация регистра и пробелов")


def stat_mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


def test_chat_targets():
    check("юзернейм остаётся юзернеймом", mon.normalize_chat_target("@mazda_forum") == "@mazda_forum")
    check("ссылка превращается в юзернейм",
          mon.normalize_chat_target("https://t.me/mazdachinacx5/123") == "mazdachinacx5")
    check("id остаётся числом", mon.normalize_chat_target("-1001234567890") == -1001234567890)
    check("t.me без схемы разбирается", mon.normalize_chat_target("t.me/auto_zakazz25") == "auto_zakazz25")


def test_all_chats_mode():
    session = TMP_DIR / "all.session"
    session.write_bytes(b"stub")
    common = {
        "MONITOR_API_ID": "1", "MONITOR_API_HASH": "x", "MONITOR_NOTIFY_USERS": "1",
        "MONITOR_SESSION": str(session.with_suffix("")),
    }
    settings = mon.load_settings(env={**common, "MONITOR_CHATS": "all"})
    check("режим «все группы» включается", settings.all_chats is True, str(settings.all_chats))
    check("в этом режиме список чатов пуст", settings.chats == [], str(settings.chats))
    check("в описании видно «все группы»", "все группы аккаунта" in settings.summary(), settings.summary())

    settings = mon.load_settings(env={**common, "MONITOR_CHATS": "all", "MONITOR_CHATS_EXCLUDE": "@наш_чат, -100123"})
    check("исключения разобраны", settings.chats_exclude == ["@наш_чат", "-100123"], str(settings.chats_exclude))

    settings = mon.load_settings(env={**common, "MONITOR_CHATS": "@one,@two"})
    check("обычный список чатов работает как раньше",
          settings.all_chats is False and settings.chats == ["@one", "@two"], str(settings.chats))

    settings = mon.load_settings(env=common)
    check("без MONITOR_CHATS берутся шесть чатов из кода",
          settings.chats == list(mon.DEFAULT_CHATS), str(settings.chats))


def test_known_users_filter():
    """Люди, которые уже писали боту, мастерам не показываются."""
    import json

    bot_users = TMP_DIR / "bot_users.json"
    bot_users.write_text(json.dumps({
        "120317642": {"username": "AntonLeontyev"},
        "555": {"username": ""},
    }), encoding="utf-8")
    contacts = TMP_DIR / "contacts.json"
    contacts.write_text(json.dumps([
        {"user_id": 777, "username": "client_one"},
        {"user_id": 888, "username": None},
    ]), encoding="utf-8")

    known = mon.KnownUsers([bot_users, contacts, TMP_DIR / "нет.json"], ["@Manual_User", "-100500"],
                           refresh_minutes=5)
    check("id из реестра бота отсекается", known.is_known(120317642) == "уже писал боту")
    check("юзернейм сопоставляется без учёта регистра и @",
          known.is_known(1, "@antonleontyev") == "уже писал боту")
    check("id из заявок отсекается", known.is_known(777) == "уже писал боту")
    check("юзернейм из заявок отсекается", known.is_known(None, "client_one") == "уже писал боту")
    check("ручное исключение по id работает", known.is_known(-100500) == "уже писал боту")
    check("ручное исключение по юзернейму работает", known.is_known(None, "manual_user") == "уже писал боту")
    check("новый человек проходит фильтр", known.is_known(999999, "newbie") is None)
    check("пропущенный файл не ломает загрузку", known.is_known(888) == "уже писал боту")
    check("в описании видно число известных", "известных пользователей" in known.describe(), known.describe())

    empty = mon.KnownUsers([], [], refresh_minutes=0)
    check("без файлов фильтр никого не отсекает", empty.is_known(1, "anyone") is None)
    check("без файлов описание корректное", "нет файлов" in empty.describe(), empty.describe())

    session = TMP_DIR / "known.session"
    session.write_bytes(b"stub")
    settings = mon.load_settings(env={
        "MONITOR_API_ID": "1", "MONITOR_API_HASH": "x", "MONITOR_NOTIFY_USERS": "1",
        "MONITOR_SESSION": str(session.with_suffix("")),
        "MONITOR_KNOWN_USERS_FILE": f"{bot_users},{contacts}",
        "MONITOR_IGNORE_USERS": "@boss",
    })
    check("пути к файлам читаются из настроек", settings.known_users_files == [bot_users, contacts],
          str(settings.known_users_files))
    check("ручные исключения читаются из настроек", settings.ignore_users == ["@boss"], str(settings.ignore_users))


def test_relative_paths_from_env_file():
    """Относительные пути считаются от файла настроек, а не от папки скрипта."""
    config_dir = TMP_DIR / "cfg"
    config_dir.mkdir(exist_ok=True)
    (config_dir / "sess.session").write_bytes(b"stub")
    (config_dir / "state.json").write_text("{}", encoding="utf-8")
    env_path = config_dir / "env_rel"
    env_path.write_text(
        "MONITOR_API_ID=1\nMONITOR_API_HASH=x\nMONITOR_NOTIFY_USERS=1\n"
        "MONITOR_SESSION=./sess\nMONITOR_STATE_FILE=state.json\n",
        encoding="utf-8",
    )
    settings = mon.load_settings(explicit_env_file=str(env_path))
    check("сессия ищется рядом с настройками", settings.session_path == config_dir / "sess",
          str(settings.session_path))
    check("файл памяти ищется рядом с настройками", settings.state_file == config_dir / "state.json",
          str(settings.state_file))

    store = mon.SeenStore(settings.state_file, ttl_hours=24, user_cooldown_hours=168)
    store.add(f"{mon.USER_KEY_PREFIX}42")
    check("память читается из того же файла", mon.repeat_reason(store, 42, 168) is not None)


def test_one_notification_per_person():
    """Об одном человеке мастерам сообщаем один раз за кулдаун, а не на каждое сообщение."""
    path = TMP_DIR / "state_repeat.json"
    store = mon.SeenStore(path, ttl_hours=24, limit=1000, user_cooldown_hours=168)
    check("о новом человеке сообщений не было", mon.repeat_reason(store, 555, 168) is None)

    store.add(mon.message_key(-100, 1))
    store.add(f"{mon.USER_KEY_PREFIX}555")
    when = mon.repeat_reason(store, 555, 168)
    check("после уведомления человек считается известным", when is not None)
    check("в логе понятное время", "назад" in mon.human_delta(when), mon.human_delta(when))
    check("кулдаун 0 отключает фильтр", mon.repeat_reason(store, 555, 0) is None)
    check("без id человека фильтр молчит", mon.repeat_reason(store, None, 168) is None)

    # сроки разные: сообщения помним сутки, людей — кулдаун
    old_store = mon.SeenStore(TMP_DIR / "state_ttl.json", ttl_hours=24, limit=1000,
                              user_cooldown_hours=168)
    old_store._data = {
        "text:1:abc": time.time() - 25 * 3600,
        f"{mon.USER_KEY_PREFIX}777": time.time() - 25 * 3600,
        "text:1:def": time.time() - 3600,
    }
    old_store._prune()
    check("старые сообщения вычищаются по суткам", "text:1:abc" not in old_store._data)
    check("запись о человеке живёт дольше суток",
          f"{mon.USER_KEY_PREFIX}777" in old_store._data, str(list(old_store._data)))
    check("свежие сообщения остаются", "text:1:def" in old_store._data)

    removed = store.forget("555")
    check("--forget забывает человека", removed >= 1 and mon.repeat_reason(store, 555, 168) is None,
          f"удалено {removed}")

    session = TMP_DIR / "cooldown.session"
    session.write_bytes(b"stub")
    settings = mon.load_settings(env={
        "MONITOR_API_ID": "1", "MONITOR_API_HASH": "x", "MONITOR_NOTIFY_USERS": "1",
        "MONITOR_SESSION": str(session.with_suffix("")), "MONITOR_USER_COOLDOWN_HOURS": "48",
    })
    check("кулдаун читается из настроек", settings.user_cooldown_hours == 48, str(settings.user_cooldown_hours))
    check("кулдаун виден в описании настроек", "повтор о человеке через 48 ч" in settings.summary(),
          settings.summary())
    check("форматирование времени: часы", mon.human_delta(time.time() - 3 * 3600) == "3 ч назад",
          mon.human_delta(time.time() - 3 * 3600))
    check("форматирование времени: дни", mon.human_delta(time.time() - 50 * 3600) == "2 дн назад",
          mon.human_delta(time.time() - 50 * 3600))


def test_dry_run_flag():
    session = TMP_DIR / "dry.session"
    session.write_bytes(b"stub")
    settings = mon.load_settings(env={
        "MONITOR_API_ID": "1", "MONITOR_API_HASH": "x", "MONITOR_NOTIFY_USERS": "1",
        "MONITOR_SESSION": str(session.with_suffix("")), "MONITOR_DRY_RUN": "true",
    })
    check("режим проверки читается из настроек", settings.dry_run is True)


def test_dictionary_recall():
    """Словарь должен ловить реальные формулировки клиентов и не ловить продавцов."""
    client = [
        "Всем привет, наконец забрал свою из Владивостока",
        "Кто подскажет, как активировать скрытые функции на cx5?",
        "нужна русификация приборки, кто делал?",
        "Машина на растаможке, жду выпуск",
        "Пришла машина, что первым делом делать?",
        "Ищу мастера по активации скрытых опций в Москве",
        "Посоветуйте, где сделать антикор на сх5",
        "Сколько будет стоить русификация панели?",
        "Приехала наконец, в субботу забираю",
        "Кто занимается русификацией в Питере?",
        "Хочу активировать i-stop memory",
        "Активация опций на cx-5 2021, есть кто?",
        "Подскажите, какое масло лить в 2.5?",
        "Мазда сх5 из китая, проблемы с языком",
        "Купил, но меню на китайском, что делать?",
        "Пленка и шумка на новую сх5, цены?",
        "Есть кто по активации в Казани?",
        "Заказал машину, придет в декабре",
        "Сх5 2023, руссификация нужна",
        "Подскажите как перевести меню на русский",
        "Кто уже делал русификацию? Есть отзывы?",
        "Сколько по деньгам русификация и активация?",
        "Хочу записаться, есть окно в субботу?",
        "Растаможили наконец, привезли в салон",
        "Меню магнитолы на китайском, что делать?",
        "Нужна прошивка под русский",
    ]
    missed = []
    for text in client:
        score, categories, veto = mon.score_message(text)
        if veto is not None or score < 3:
            missed.append(f"{text} (вес {score}, отсев {veto})")
    check("словарь ловит все формулировки клиентов", not missed, "; ".join(missed[:4]))

    noise = [
        "Продам зимние шины на cx5, 18 радиус",
        "Запчасти на мазду из Китая оптом",
        "Скидка на антикор 30%!",
        "Сдам гараж в аренду",
        "Ищу машину сх5 до 2 млн, подскажите",
        "Вакансия: требуется мастер по сигнализациям",
        "Аренда бокса для ремонта",
        "Продам запчасти, привезли из Китая",
    ]
    leaked = []
    for text in noise:
        score, _, veto = mon.score_message(text)
        if veto is None and score >= 3:
            leaked.append(f"{text} (вес {score})")
    check("шум не превращается в уведомления", not leaked, "; ".join(leaked))


def test_keywords_file():
    """Словарь живёт в keywords.json: создаётся сам, правится без кода, битое не роняет."""
    folder = TMP_DIR / "kw"
    path = folder / "keywords.json"
    cfg = mon.load_keywords(path)
    check("keywords.json создаётся автоматически", path.exists(), str(path))
    check("в словаре есть правила", len(cfg.rules) >= 10, str(len(cfg.rules)))
    check("в словаре есть отсевы", len(cfg.veto) >= 3, str(len(cfg.veto)))
    check("версия словаря актуальная",
          json.loads(path.read_text(encoding="utf-8")).get("version") == mon.KEYWORDS_VERSION)

    data = json.loads(path.read_text(encoding="utf-8"))
    data["rules"].append({"category": "свой повод", "weight": 5, "patterns": [r"проверк\w+ связи"]})
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    cfg = mon.load_keywords(path)
    score, categories, veto = mon.score_message("напишите про проверку связи", cfg)
    check("правка словаря подхватывается без правки кода", "свой повод" in categories and score >= 5,
          f"вес {score}, категории {categories}")

    data["rules"].append({"category": "битая", "weight": 9, "patterns": ["["]})
    data["rules"].append({"category": "не строка", "weight": 9, "patterns": [2, None]})
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    cfg = mon.load_keywords(path)
    check("кривая регулярка не роняет словарь", len(cfg.rules) >= 10, str(len(cfg.rules)))
    check("числа в patterns не становятся правилами",
          not any(rule.pattern.pattern.strip() == "2" for rule in cfg.rules))

    data["version"] = 1
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    cfg = mon.load_keywords(path)
    check("словарь старой версии пересоздаётся",
          json.loads(path.read_text(encoding="utf-8")).get("version") == mon.KEYWORDS_VERSION, cfg.source)
    check("старый словарь сохраняется в .bak", (folder / "keywords.json.bak").exists())


def test_candidate_log_and_report():
    """Журнал кандидатов и дневная сводка."""
    path = TMP_DIR / "cand.csv"
    log = mon.CandidateLog(path)
    log.add(action="notified", score=4, categories=["русификация"], chat="Mazda CX-5",
            author="@client", link="https://t.me/c/1", text="нужна русификация")
    log.add(action="skipped", reason="отсев: продажа/реклама", score=0, text="продам пленку")
    log.add(action="skipped", reason="вес 2 < 3", score=2, categories=["доработки"], text="антикор сын")
    log.add(action="notified", score=3, categories=["таможня/доставка"], chat="Автоподбор",
            text="машина на таможне")

    rows = log.rows(days=1)
    check("журнал пишет все кандидаты", len(rows) == 4, str(len(rows)))
    check("счётчик уведомлений за сутки", log.count_today("notified") == 2, str(log.count_today("notified")))

    report = mon.build_report(rows)
    check("в сводке есть уведомления", "уведомлено: 2" in report, report)
    check("в сводке есть причина отсева", "продажа/реклама" in report, report)
    check("в сводке есть категории", "русификация" in report, report)

    login = TMP_DIR / "cand_inject.csv"
    log2 = mon.CandidateLog(login)
    log2.add(action="skipped", text="=cmd|' /C calc'!A0")
    log2.add(action="skipped", text="обычный текст")
    content = login.read_text(encoding="utf-8")
    check("формулы Excel обезврежены", "'=cmd" in content, content[:120])
    check("заголовок CSV только один", content.count("time,action") == 1, str(content.count("time,action")))

    empty = mon.build_report([])
    check("пустая сводка не падает", "Монитор за сутки" in empty, empty)


def test_reply_and_report_settings():
    """Готовый ответ в уведомлении и настройки отчёта/лимита."""
    cfg = mon.load_keywords()
    reply = mon.suggest_reply(["русификация"], cfg)
    check("для русификации есть готовый ответ", "35 000" in reply, reply[:80])
    check("для чужих категорий — общий ответ", mon.suggest_reply(["машина из Китая"], cfg) == mon.DEFAULT_REPLY)

    text = mon.build_notification(
        sender_name="@client", text="нужна русификация", chat_title="Чат", chat_username="chat",
        message_id=5, categories=["русификация"], score=4, reply=reply,
    )
    check("в уведомлении есть готовый ответ", "Готовый ответ" in text and "35 000" in text, text[-160:])

    session = TMP_DIR / "limits.session"
    session.write_bytes(b"stub")
    settings = mon.load_settings(env={
        "MONITOR_API_ID": "1", "MONITOR_API_HASH": "x", "MONITOR_NOTIFY_USERS": "1",
        "MONITOR_SESSION": str(session.with_suffix("")),
        "MONITOR_DAILY_LIMIT": "20", "MONITOR_REPORT_HOUR": "9",
        "MONITOR_LOG_CSV": str(TMP_DIR / "cand2.csv"),
    })
    check("дневной лимит читается", settings.daily_limit == 20, str(settings.daily_limit))
    check("час отчёта читается", settings.report_hour == 9, str(settings.report_hour))
    check("журнал кандидатов в настройках", settings.log_csv == TMP_DIR / "cand2.csv", str(settings.log_csv))
    check("лимит и отчёт видны в описании",
          "лимит 20/сутки" in settings.summary() and "отчёт в 9:00" in settings.summary(), settings.summary())

    try:
        mon.load_settings(env={
            "MONITOR_API_ID": "1", "MONITOR_API_HASH": "x", "MONITOR_NOTIFY_USERS": "1",
            "MONITOR_SESSION": str(session.with_suffix("")), "MONITOR_REPORT_HOUR": "25",
        })
        check("неверный час отчёта отлавливается", False, "ошибки не было")
    except mon.ConfigError as exc:
        check("неверный час отчёта — понятная ошибка", "MONITOR_REPORT_HOUR" in str(exc), str(exc)[:80])

    check("часовой пояс по умолчанию — Москва", settings.timezone == "Europe/Moscow", settings.timezone)
    settings_tz = mon.load_settings(env={
        "MONITOR_API_ID": "1", "MONITOR_API_HASH": "x", "MONITOR_NOTIFY_USERS": "1",
        "MONITOR_SESSION": str(session.with_suffix("")), "MONITOR_TZ": "Asia/Yekaterinburg",
    })
    check("часовой пояс читается из настроек", settings_tz.timezone == "Asia/Yekaterinburg",
          settings_tz.timezone)
    mon.apply_timezone("Europe/Moscow")  # не должно падать
    check("часовой пояс применяется без ошибок", True)

    state_path = TMP_DIR / "report_state.json"
    state = mon.ReportState(state_path)
    check("отчёт отправляется один раз в день", state.should_send("2026-10-26"))
    state.mark("2026-10-26")
    check("повторно в тот же день не отправляется", not mon.ReportState(state_path).should_send("2026-10-26"))
    check("на следующий день отправляется снова", mon.ReportState(state_path).should_send("2026-10-27"))


def main() -> int:
    for test in (
        test_real_leads_pass,
        test_noise_is_filtered,
        test_negation_and_weak_matches,
        test_typos_are_tolerated,
        test_weights_priority,
        test_env_parsing,
        test_settings_validation,
        test_settings_ok,
        test_env_file_priority,
        test_notification_text,
        test_seen_store,
        test_chat_targets,
        test_all_chats_mode,
        test_known_users_filter,
        test_relative_paths_from_env_file,
        test_one_notification_per_person,
        test_dictionary_recall,
        test_keywords_file,
        test_candidate_log_and_report,
        test_reply_and_report_settings,
        test_dry_run_flag,
    ):
        print(f"\n--- {test.__name__} ---")
        try:
            test()
        except Exception as exc:  # noqa: BLE001 — тест не должен ронять прогон
            check(f"{test.__name__}: исключение", False, f"{type(exc).__name__}: {exc}")

    failed = [name for name, ok, _ in RESULTS if not ok]
    print("\n" + "=" * 60)
    print(f"Проверок: {len(RESULTS)}, успешно: {len(RESULTS) - len(failed)}, провалено: {len(failed)}")
    if failed:
        print("Провалено:")
        for name in failed:
            detail = next((d for n, ok, d in RESULTS if n == name and not ok), "")
            print(f"  • {name} {('— ' + detail[:200]) if detail else ''}")
        return 1
    print("Все проверки пройдены ✅")
    return 0


if __name__ == "__main__":
    code = main()
    import shutil

    shutil.rmtree(TMP_DIR, ignore_errors=True)
    sys.exit(code)

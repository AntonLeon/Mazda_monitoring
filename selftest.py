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


def test_dry_run_flag():
    session = TMP_DIR / "dry.session"
    session.write_bytes(b"stub")
    settings = mon.load_settings(env={
        "MONITOR_API_ID": "1", "MONITOR_API_HASH": "x", "MONITOR_NOTIFY_USERS": "1",
        "MONITOR_SESSION": str(session.with_suffix("")), "MONITOR_DRY_RUN": "true",
    })
    check("режим проверки читается из настроек", settings.dry_run is True)


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

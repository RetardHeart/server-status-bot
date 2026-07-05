"""
Telegram-бот для мониторинга состояния сервера.

По запросу пользователя (команда или кнопка) собирает метрики хоста
через psutil и присылает читаемый отчёт: CPU, память, диски, сеть,
аптайм, средняя нагрузка и топ процессов по потреблению ресурсов.

Стек: Python 3.10+, aiogram 3.x, psutil.
"""

from __future__ import annotations

import asyncio
import logging
import os
import platform
import socket
import time
from datetime import datetime, timedelta

import psutil
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

# --------------------------------------------------------------------------- #
# Конфигурация                                                                #
# --------------------------------------------------------------------------- #

load_dotenv()  # подхватывает переменные из файла .env, если он есть

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

# Список Telegram user id, которым разрешён доступ.
# Формат в .env: ALLOWED_USER_IDS=123456789,987654321
# Если список пуст — бот отвечает всем (не рекомендуется для прод-сервера).
_raw_ids = os.getenv("ALLOWED_USER_IDS", "").strip()
ALLOWED_USER_IDS: set[int] = {
    int(x) for x in _raw_ids.replace(" ", "").split(",") if x
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
logger = logging.getLogger("server-status-bot")


# --------------------------------------------------------------------------- #
# Утилиты форматирования                                                      #
# --------------------------------------------------------------------------- #

def human_bytes(num: float) -> str:
    """Переводит байты в человекочитаемый вид (КБ, МБ, ГБ...)."""
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ", "ПБ"):
        if abs(num) < 1024.0:
            return f"{num:3.1f} {unit}"
        num /= 1024.0
    return f"{num:.1f} ЭБ"


def bar(percent: float, width: int = 10) -> str:
    """Рисует текстовый прогресс-бар: ███░░░░░░░ 30%."""
    percent = max(0.0, min(100.0, percent))
    filled = int(round(percent / 100 * width))
    return "█" * filled + "░" * (width - filled) + f" {percent:.0f}%"


def human_timedelta(seconds: float) -> str:
    """Форматирует длительность в вид '3д 4ч 12м'."""
    delta = timedelta(seconds=int(seconds))
    days = delta.days
    hours, rem = divmod(delta.seconds, 3600)
    minutes, _ = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}д")
    if hours:
        parts.append(f"{hours}ч")
    parts.append(f"{minutes}м")
    return " ".join(parts)


# --------------------------------------------------------------------------- #
# Сбор метрик                                                                 #
# --------------------------------------------------------------------------- #

def collect_overview() -> str:
    """Краткая сводка по состоянию сервера."""
    hostname = socket.gethostname()
    uname = platform.uname()

    cpu_percent = psutil.cpu_percent(interval=0.5)
    cpu_count = psutil.cpu_count(logical=True)

    mem = psutil.virtual_memory()
    swap = psutil.swap_memory()

    boot_time = psutil.boot_time()
    uptime = time.time() - boot_time

    # Средняя нагрузка (доступна на Linux/Mac).
    try:
        load1, load5, load15 = psutil.getloadavg()
        load_line = f"⚖️ <b>Load avg:</b> {load1:.2f} / {load5:.2f} / {load15:.2f}"
    except (AttributeError, OSError):
        load_line = "⚖️ <b>Load avg:</b> н/д"

    lines = [
        f"🖥 <b>{hostname}</b>",
        f"<i>{uname.system} {uname.release} · {uname.machine}</i>",
        "",
        f"🧠 <b>CPU:</b> {bar(cpu_percent)}  ({cpu_count} ядер)",
        f"💾 <b>RAM:</b> {bar(mem.percent)}  "
        f"({human_bytes(mem.used)} / {human_bytes(mem.total)})",
    ]

    if swap.total > 0:
        lines.append(
            f"🔁 <b>Swap:</b> {bar(swap.percent)}  "
            f"({human_bytes(swap.used)} / {human_bytes(swap.total)})"
        )

    lines += [
        load_line,
        f"⏱ <b>Uptime:</b> {human_timedelta(uptime)}",
        f"🕐 <i>{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</i>",
    ]
    return "\n".join(lines)


def collect_disks() -> str:
    """Использование дисковых разделов."""
    lines = ["💽 <b>Диски</b>", ""]
    for part in psutil.disk_partitions(all=False):
        # Пропускаем виртуальные/системные ФС без интереса.
        if part.fstype == "" or "loop" in part.device:
            continue
        try:
            usage = psutil.disk_usage(part.mountpoint)
        except (PermissionError, OSError):
            continue
        lines.append(
            f"<b>{part.mountpoint}</b>  ({part.fstype})\n"
            f"{bar(usage.percent)}  "
            f"{human_bytes(usage.used)} / {human_bytes(usage.total)}"
        )
        lines.append("")
    return "\n".join(lines).strip() or "Разделы не найдены."


def collect_network() -> str:
    """Сетевая статистика и активные соединения."""
    io = psutil.net_io_counters()
    conns = 0
    try:
        conns = len(psutil.net_connections(kind="inet"))
    except (psutil.AccessDenied, OSError):
        conns = -1

    lines = [
        "🌐 <b>Сеть</b>",
        "",
        f"⬆️ <b>Отправлено:</b> {human_bytes(io.bytes_sent)}",
        f"⬇️ <b>Получено:</b> {human_bytes(io.bytes_recv)}",
        f"📦 <b>Пакеты:</b> ↑{io.packets_sent}  ↓{io.packets_recv}",
    ]
    if conns >= 0:
        lines.append(f"🔌 <b>Активных соединений:</b> {conns}")
    else:
        lines.append("🔌 <b>Активных соединений:</b> н/д (нужны права)")
    return "\n".join(lines)


def collect_top_processes(limit: int = 8) -> str:
    """Топ процессов по CPU и памяти."""
    procs = []
    for p in psutil.process_iter(["pid", "name", "cpu_percent", "memory_percent"]):
        procs.append(p.info)

    # Первый вызов cpu_percent возвращает 0 — даём короткую паузу для замера.
    time.sleep(0.3)
    for p in psutil.process_iter(["pid", "name", "cpu_percent", "memory_percent"]):
        for existing in procs:
            if existing["pid"] == p.info["pid"]:
                existing["cpu_percent"] = p.info["cpu_percent"]
                break

    by_cpu = sorted(procs, key=lambda x: x.get("cpu_percent") or 0, reverse=True)[:limit]
    by_mem = sorted(procs, key=lambda x: x.get("memory_percent") or 0, reverse=True)[:limit]

    def fmt(rows, metric, unit):
        out = []
        for r in rows:
            name = (r.get("name") or "?")[:20]
            val = r.get(metric) or 0
            out.append(f"<code>{val:5.1f}{unit}</code> {name} (pid {r['pid']})")
        return "\n".join(out)

    return (
        "📊 <b>Топ по CPU</b>\n"
        + fmt(by_cpu, "cpu_percent", "%")
        + "\n\n📈 <b>Топ по RAM</b>\n"
        + fmt(by_mem, "memory_percent", "%")
    )


# --------------------------------------------------------------------------- #
# Клавиатура и доступ                                                         #
# --------------------------------------------------------------------------- #

def main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🔄 Обновить сводку", callback_data="overview"),
            ],
            [
                InlineKeyboardButton(text="💽 Диски", callback_data="disks"),
                InlineKeyboardButton(text="🌐 Сеть", callback_data="network"),
            ],
            [
                InlineKeyboardButton(text="📊 Процессы", callback_data="processes"),
            ],
        ]
    )


def is_allowed(user_id: int | None) -> bool:
    """Проверка доступа. Пустой список = доступ всем."""
    if not ALLOWED_USER_IDS:
        return True
    return user_id in ALLOWED_USER_IDS


# --------------------------------------------------------------------------- #
# Хендлеры                                                                    #
# --------------------------------------------------------------------------- #

dp = Dispatcher()


@dp.message(Command("start", "help"))
async def cmd_start(message: Message) -> None:
    if not is_allowed(message.from_user.id):
        await message.answer("⛔️ Доступ запрещён.")
        logger.warning("Отказ в доступе: user_id=%s", message.from_user.id)
        return

    text = (
        "👋 <b>Бот мониторинга сервера</b>\n\n"
        "Доступные команды:\n"
        "/status — краткая сводка\n"
        "/disks — использование дисков\n"
        "/net — сетевая статистика\n"
        "/top — топ процессов\n\n"
        "Или пользуйтесь кнопками ниже 👇"
    )
    await message.answer(text, reply_markup=main_keyboard())


@dp.message(Command("status"))
async def cmd_status(message: Message) -> None:
    if not is_allowed(message.from_user.id):
        await message.answer("⛔️ Доступ запрещён.")
        return
    text = await asyncio.to_thread(collect_overview)
    await message.answer(text, reply_markup=main_keyboard())


@dp.message(Command("disks"))
async def cmd_disks(message: Message) -> None:
    if not is_allowed(message.from_user.id):
        await message.answer("⛔️ Доступ запрещён.")
        return
    text = await asyncio.to_thread(collect_disks)
    await message.answer(text, reply_markup=main_keyboard())


@dp.message(Command("net"))
async def cmd_net(message: Message) -> None:
    if not is_allowed(message.from_user.id):
        await message.answer("⛔️ Доступ запрещён.")
        return
    text = await asyncio.to_thread(collect_network)
    await message.answer(text, reply_markup=main_keyboard())


@dp.message(Command("top"))
async def cmd_top(message: Message) -> None:
    if not is_allowed(message.from_user.id):
        await message.answer("⛔️ Доступ запрещён.")
        return
    text = await asyncio.to_thread(collect_top_processes)
    await message.answer(text, reply_markup=main_keyboard())


@dp.callback_query(F.data.in_({"overview", "disks", "network", "processes"}))
async def on_button(callback: CallbackQuery) -> None:
    if not is_allowed(callback.from_user.id):
        await callback.answer("⛔️ Доступ запрещён.", show_alert=True)
        return

    collectors = {
        "overview": collect_overview,
        "disks": collect_disks,
        "network": collect_network,
        "processes": collect_top_processes,
    }
    await callback.answer("Собираю данные...")
    text = await asyncio.to_thread(collectors[callback.data])

    # Пытаемся отредактировать текущее сообщение; если текст не изменился —
    # Telegram вернёт ошибку, которую можно игнорировать.
    try:
        await callback.message.edit_text(text, reply_markup=main_keyboard())
    except Exception:  # noqa: BLE001 — сообщение не изменилось / устарело
        await callback.message.answer(text, reply_markup=main_keyboard())


# --------------------------------------------------------------------------- #
# Точка входа                                                                 #
# --------------------------------------------------------------------------- #

async def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit(
            "❌ Не задан BOT_TOKEN. Укажите его в переменной окружения "
            "или в файле .env (см. .env.example)."
        )

    if not ALLOWED_USER_IDS:
        logger.warning(
            "ALLOWED_USER_IDS пуст — бот отвечает ВСЕМ пользователям. "
            "Для прод-сервера настоятельно рекомендуется ограничить доступ."
        )

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    logger.info("Бот запущен. Разрешённые пользователи: %s",
                ALLOWED_USER_IDS or "все")
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit) as exc:
        logger.info("Остановка: %s", exc)

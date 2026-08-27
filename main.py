import os
import re
import sqlite3
from datetime import datetime, timezone
from typing import Optional
import asyncio

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv


load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
TABLE_CHANNEL_ID = int(os.getenv("TABLE_CHANNEL_ID", "0"))
ORDER_ROLE_ID = int(os.getenv("ORDER_ROLE_ID", "0"))

DB_FILE = "orders.sqlite3"
MAX_DISCORD_LENGTH = 2000
DEFAULT_BLOCKS = [3, 3, 3]

delay=5


# -----------------------------
# База данных
# -----------------------------

db = sqlite3.connect(DB_FILE)
db.row_factory = sqlite3.Row


def init_db():
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS blocks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            block_number INTEGER NOT NULL UNIQUE,
            position INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS rows (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            block_number INTEGER NOT NULL,
            row_number INTEGER NOT NULL,
            player TEXT NOT NULL DEFAULT '',
            before_coins INTEGER NOT NULL DEFAULT 0,
            paid_coins INTEGER NOT NULL DEFAULT 0,
            link TEXT NOT NULL DEFAULT '',
            changed_by TEXT NOT NULL DEFAULT '',
            changed_at TEXT NOT NULL DEFAULT '',
            changed INTEGER NOT NULL DEFAULT 0,
            UNIQUE(block_number, row_number)
        );

        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS table_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id INTEGER NOT NULL UNIQUE,
            channel_id INTEGER NOT NULL,
            position INTEGER NOT NULL
        );
        """
    )
    db.commit()


def get_setting(key: str) -> Optional[str]:
    row = db.execute(
        "SELECT value FROM settings WHERE key = ?",
        (key,)
    ).fetchone()
    return row["value"] if row else None


def set_setting(key: str, value: str):
    db.execute(
        """
        INSERT INTO settings(key, value)
        VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (key, value)
    )
    db.commit()


def initialize_table():
    exists = db.execute("SELECT COUNT(*) AS count FROM blocks").fetchone()["count"]

    if exists:
        return

    for block_number, row_count in enumerate(DEFAULT_BLOCKS, start=1):
        db.execute(
            "INSERT INTO blocks(block_number, position) VALUES (?, ?)",
            (block_number, block_number)
        )

        for row_number in range(1, row_count + 1):
            db.execute(
                """
                INSERT INTO rows(block_number, row_number)
                VALUES (?, ?)
                """,
                (block_number, row_number)
            )

    db.commit()


# -----------------------------
# Работа со структурой таблицы
# -----------------------------

def blocks():
    return db.execute(
        "SELECT * FROM blocks ORDER BY position"
    ).fetchall()


def get_row(row_name: str):
    match = re.fullmatch(r"(\d+)\.(\d+)", row_name.strip())

    if not match:
        return None

    block_number = int(match.group(1))
    row_number = int(match.group(2))

    return db.execute(
        """
        SELECT *
        FROM rows
        WHERE block_number = ? AND row_number = ?
        """,
        (block_number, row_number)
    ).fetchone()


def normalize_rows(block_number: int):
    current_rows = db.execute(
        """
        SELECT id
        FROM rows
        WHERE block_number = ?
        ORDER BY row_number
        """,
        (block_number,)
    ).fetchall()

    for number, row in enumerate(current_rows, start=1):
        db.execute(
            "UPDATE rows SET row_number = ? WHERE id = ?",
            (number, row["id"])
        )


def normalize_blocks():
    current_blocks = db.execute(
        "SELECT block_number FROM blocks ORDER BY position"
    ).fetchall()

    # Временно меняем номера, чтобы избежать конфликтов UNIQUE.
    for index, row in enumerate(current_blocks, start=1000):
        db.execute(
            "UPDATE blocks SET block_number = ? WHERE block_number = ?",
            (index, row["block_number"])
        )
        db.execute(
            "UPDATE rows SET block_number = ? WHERE block_number = ?",
            (index, row["block_number"])
        )

    temporary = db.execute(
        "SELECT block_number FROM blocks ORDER BY position"
    ).fetchall()

    for new_number, row in enumerate(temporary, start=1):
        old_number = row["block_number"]

        db.execute(
            "UPDATE blocks SET block_number = ? WHERE block_number = ?",
            (new_number, old_number)
        )
        db.execute(
            "UPDATE rows SET block_number = ? WHERE block_number = ?",
            (new_number, old_number)
        )

    db.commit()


def add_row(block_number: int, position: int):
    block = db.execute(
        "SELECT * FROM blocks WHERE block_number = ?",
        (block_number,)
    ).fetchone()

    if not block:
        raise ValueError("Такого блока не существует.")

    count = db.execute(
        "SELECT COUNT(*) AS count FROM rows WHERE block_number = ?",
        (block_number,)
    ).fetchone()["count"]

    if position < 1 or position > count + 1:
        raise ValueError(
            f"Позиция должна быть от 1 до {count + 1}."
        )

    db.execute(
        """
        UPDATE rows
        SET row_number = row_number + 1
        WHERE block_number = ? AND row_number >= ?
        """,
        (block_number, position)
    )

    db.execute(
        """
        INSERT INTO rows(block_number, row_number)
        VALUES (?, ?)
        """,
        (block_number, position)
    )

    db.commit()


def delete_row(row_name: str):
    row = get_row(row_name)

    if not row:
        raise ValueError("Указанная строка не существует.")

    db.execute("DELETE FROM rows WHERE id = ?", (row["id"],))

    db.execute(
        """
        UPDATE rows
        SET row_number = row_number - 1
        WHERE block_number = ? AND row_number > ?
        """,
        (row["block_number"], row["row_number"])
    )

    db.commit()


def add_block(position: int, row_count: int):
    count = db.execute(
        "SELECT COUNT(*) AS count FROM blocks"
    ).fetchone()["count"]

    if position < 1 or position > count + 1:
        raise ValueError(f"Позиция блока должна быть от 1 до {count + 1}.")

    if row_count < 1 or row_count > 100:
        raise ValueError("Количество строк должно быть от 1 до 100.")

    db.execute(
        """
        UPDATE blocks
        SET position = position + 1
        WHERE position >= ?
        """,
        (position,)
    )

    temporary_number = 100000 + count

    db.execute(
        """
        INSERT INTO blocks(block_number, position)
        VALUES (?, ?)
        """,
        (temporary_number, position)
    )

    for row_number in range(1, row_count + 1):
        db.execute(
            """
            INSERT INTO rows(block_number, row_number)
            VALUES (?, ?)
            """,
            (temporary_number, row_number)
        )

    db.commit()
    normalize_blocks()


def delete_block(block_number: int):
    block = db.execute(
        "SELECT * FROM blocks WHERE block_number = ?",
        (block_number,)
    ).fetchone()

    if not block:
        raise ValueError("Такого блока не существует.")

    db.execute(
        "DELETE FROM rows WHERE block_number = ?",
        (block_number,)
    )
    db.execute(
        "DELETE FROM blocks WHERE block_number = ?",
        (block_number,)
    )
    db.execute(
        """
        UPDATE blocks
        SET position = position - 1
        WHERE position > ?
        """,
        (block["position"],)
    )

    db.commit()
    normalize_blocks()


def resize_block(block_number: int, new_count: int):
    if new_count < 1 or new_count > 100:
        raise ValueError("Количество строк должно быть от 1 до 100.")

    current_count = db.execute(
        """
        SELECT COUNT(*) AS count
        FROM rows
        WHERE block_number = ?
        """,
        (block_number,)
    ).fetchone()["count"]

    if current_count == 0:
        raise ValueError("Такого блока не существует.")

    if new_count > current_count:
        for number in range(current_count + 1, new_count + 1):
            db.execute(
                """
                INSERT INTO rows(block_number, row_number)
                VALUES (?, ?)
                """,
                (block_number, number)
            )
    elif new_count < current_count:
        db.execute(
            """
            DELETE FROM rows
            WHERE block_number = ? AND row_number > ?
            """,
            (block_number, new_count)
        )

    db.commit()


# -----------------------------
# Форматирование таблицы
# -----------------------------

def marketplace(link):
    if "funpay" in link:
        return "FunPay"
    
    if "starvell" in link:
        return "Starvell"

    return ""


def format_row(row) -> str:
    player = row["player"] or "-"
    before = row["before_coins"] or 0
    paid = row["paid_coins"] or 0
    link = row["link"] or ""

    # Текст строки остаётся моноширинным,
    # ссылка находится вне блока кода и будет кликабельной
    prefix = (
        f'{row["block_number"]}.{row["row_number"]}. '
        f'{player} | {before}к | {paid}к [{marketplace(link)}]({link})'
    )

    return prefix


def build_table() -> str:
    output = []
    all_blocks = blocks()

    for block_index, block in enumerate(all_blocks):
        block_rows = db.execute(
            """
            SELECT *
            FROM rows
            WHERE block_number = ?
            ORDER BY row_number
            """,
            (block["block_number"],)
        ).fetchall()

        for row_index, row in enumerate(block_rows):
            output.append(format_row(row))

            # Разделитель между строками одного блока
            if row_index < len(block_rows) - 1:
                output.append("----------------------------------------")

        # Разделитель между блоками.
        # Пустая строка добавляется сверху и снизу.
        if block_index < len(all_blocks) - 1:
            output.append("")
            output.append("========================================")
            output.append("")

    return "\n".join(output)




def split_text(text: str, limit: int = 2000):
    lines = text.splitlines()
    parts = []
    current_lines = []
    current_length = 0

    for line in lines:
        line_length = len(line) + 1

        if current_lines and current_length + line_length > limit:
            parts.append("\n".join(current_lines))
            current_lines = []
            current_length = 0

        current_lines.append(line)
        current_length += line_length

    if current_lines:
        parts.append("\n".join(current_lines))

    return parts



# -----------------------------
# Discord
# -----------------------------

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)


class RefreshView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Обновить",
        style=discord.ButtonStyle.primary,
        custom_id="orders_refresh"
    )
    async def refresh(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):
        await update_table_messages()
        await interaction.response.send_message(
            "Таблица обновлена.",
            ephemeral=True,
            delete_after=delay
        )


def is_manager(interaction: discord.Interaction) -> bool:
    permissions = interaction.user.guild_permissions
    return permissions.administrator or permissions.manage_guild


def can_use_order(interaction: discord.Interaction) -> bool:
    if interaction.user.guild_permissions.administrator:
        return True

    if not ORDER_ROLE_ID:
        return False

    return any(role.id == ORDER_ROLE_ID for role in interaction.user.roles)


async def deny(interaction: discord.Interaction):
    text = "У вас нет прав для использования этой команды."

    if interaction.response.is_done():
        await interaction.followup.send(text, ephemeral=True)
    else:
        await interaction.response.send_message(text, ephemeral=True)


async def update_table_messages():
    channel = bot.get_channel(TABLE_CHANNEL_ID)

    if channel is None:
        try:
            channel = await bot.fetch_channel(TABLE_CHANNEL_ID)
        except discord.NotFound:
            raise RuntimeError(
                "Канал с указанным TABLE_CHANNEL_ID не существует."
            )
        except discord.Forbidden:
            raise RuntimeError(
                "Бот не имеет доступа к каналу TABLE_CHANNEL_ID."
            )

    if not isinstance(channel, discord.TextChannel):
        raise RuntimeError(
            "TABLE_CHANNEL_ID должен указывать на текстовый канал."
        )

    permissions = channel.permissions_for(channel.guild.me)

    missing = []

    if not permissions.view_channel:
        missing.append("Просмотр канала")
    if not permissions.send_messages:
        missing.append("Отправка сообщений")
    if not permissions.read_message_history:
        missing.append("Чтение истории сообщений")

    if missing:
        raise RuntimeError(
            "Недостаточно прав в канале: " + ", ".join(missing)
        )

    table = build_table()
    parts = split_text(table)

    saved_messages = db.execute(
        """
        SELECT *
        FROM table_messages
        ORDER BY position
        """
    ).fetchall()

    messages = []

    for item in saved_messages:
        try:
            message = await channel.fetch_message(item["message_id"])
            messages.append(message)
        except discord.NotFound:
            db.execute(
                "DELETE FROM table_messages WHERE message_id = ?",
                (item["message_id"],)
            )
        except discord.Forbidden:
            print(
                f"Нет доступа к сообщению {item['message_id']}. "
                "Проверьте право чтения истории сообщений."
            )

    db.commit()

    for index, part in enumerate(parts):
        if index < len(messages):
            await messages[index].edit(
                content=part,
                view=RefreshView()
            )
        else:
            message = await channel.send(
                content=part,
                view=RefreshView()
            )

            db.execute(
                """
                INSERT INTO table_messages(message_id, channel_id, position)
                VALUES (?, ?, ?)
                """,
                (message.id, channel.id, index)
            )

    for message in messages[len(parts):]:
        try:
            await message.delete()
        except discord.NotFound:
            pass
        except discord.Forbidden:
            print(
                f"Бот не может удалить сообщение {message.id}. "
                "Нужно право «Управление сообщениями»."
            )

        db.execute(
            "DELETE FROM table_messages WHERE message_id = ?",
            (message.id,)
        )

    db.commit()



async def send_result(interaction: discord.Interaction, text: str):
    if len(text) <= 2000:
        await interaction.response.send_message(text, ephemeral=True, delete_after=delay)
    else:
        await interaction.response.send_message(
            text[:2000],
            ephemeral=True,
            delete_after=delay
        )



# -----------------------------
# /order
# -----------------------------

order_group = app_commands.Group(
    name="order",
    description="Управление заказами"
)


@order_group.command(name="add", description="Добавить заказ")
@app_commands.describe(
    row="Номер строки, например 1.1",
    player="Ник игрока",
    before="Количество монет до заказа",
    paid="Количество оплаченных монет",
    link="Ссылка на заказ"
)
async def order_add(
    interaction: discord.Interaction,
    row: str,
    player: str,
    before: int,
    paid: int,
    link: str = ""
):
    if not can_use_order(interaction):
        return await deny(interaction)

    target = get_row(row)

    if not target:
        return await send_result(
            interaction,
            "Ошибка: указанная строка не существует."
        )

    if target["player"] or target["before_coins"] or target["paid_coins"]:
        return await send_result(
            interaction,
            "Эта строка уже занята. Используйте /order change для изменения данных."
        )

    db.execute(
        """
        UPDATE rows
        SET player = ?, before_coins = ?, paid_coins = ?,
            link = ?, changed = 0,
            changed_by = ?, changed_at = ?
        WHERE id = ?
        """,
        (
            player,
            before,
            paid,
            link,
            str(interaction.user),
            datetime.now(timezone.utc).isoformat(),
            target["id"]
        )
    )
    db.commit()

    await update_table_messages()
    await send_result(interaction, "Заказ добавлен.")


@order_group.command(name="change", description="Изменить заказ")
async def order_change(
    interaction: discord.Interaction,
    row: str,
    player: str,
    before: int,
    paid: int,
    link: str = ""
):
    if not can_use_order(interaction):
        return await deny(interaction)

    target = get_row(row)

    if not target:
        return await send_result(
            interaction,
            "Ошибка: указанная строка не существует."
        )

    db.execute(
        """
        UPDATE rows
        SET player = ?, before_coins = ?, paid_coins = ?,
            link = ?, changed = 1,
            changed_by = ?, changed_at = ?
        WHERE id = ?
        """,
        (
            player,
            before,
            paid,
            link,
            str(interaction.user),
            datetime.now(timezone.utc).isoformat(),
            target["id"]
        )
    )
    db.commit()

    await update_table_messages()
    await send_result(interaction, "Строка изменена.")


@order_group.command(name="del", description="Очистить заказ")
async def order_delete(
    interaction: discord.Interaction,
    row: str
):
    if not can_use_order(interaction):
        return await deny(interaction)

    target = get_row(row)

    if not target:
        return await send_result(
            interaction,
            "Ошибка: указанная строка не существует."
        )

    db.execute(
        """
        UPDATE rows
        SET player = '', before_coins = 0, paid_coins = 0, 
            link = '', changed = 0,
            changed_by = ?, changed_at = ?
        WHERE id = ?
        """,
        (
            str(interaction.user),
            datetime.now(timezone.utc).isoformat(),
            target["id"]
        )
    )
    db.commit()

    await update_table_messages()
    await send_result(interaction, "Строка очищена.")


bot.tree.add_command(order_group)


# -----------------------------
# /menu
# -----------------------------

menu_group = app_commands.Group(
    name="menu",
    description="Управление структурой таблицы"
)


@menu_group.command(name="add-row", description="Добавить строку")
async def menu_add_row(
    interaction: discord.Interaction,
    block: int,
    position: int
):
    if not is_manager(interaction):
        return await deny(interaction)

    try:
        add_row(block, position)
        await update_table_messages()
        await send_result(interaction, "Строка добавлена.")
    except ValueError as error:
        await send_result(interaction, str(error))


@menu_group.command(name="del-row", description="Удалить строку")
async def menu_del_row(
    interaction: discord.Interaction,
    row: str,
    confirm: bool = False
):
    if not is_manager(interaction):
        return await deny(interaction)

    target = get_row(row)

    if not target:
        return await send_result(interaction, "Строка не существует.")

    filled = bool(
        target["player"]
        or target["before_coins"]
        or target["paid_coins"]
    )

    if filled and not confirm:
        return await send_result(
            interaction,
            "Строка заполнена. Для удаления укажите confirm:true."
        )

    try:
        delete_row(row)
        await update_table_messages()
        await send_result(interaction, "Строка удалена.")
    except ValueError as error:
        await send_result(interaction, str(error))


@menu_group.command(name="add-block", description="Добавить блок")
async def menu_add_block(
    interaction: discord.Interaction,
    position: int,
    rows: int
):
    if not is_manager(interaction):
        return await deny(interaction)

    try:
        add_block(position, rows)
        await update_table_messages()
        await send_result(interaction, "Блок добавлен.")
    except ValueError as error:
        await send_result(interaction, str(error))


@menu_group.command(name="del-block", description="Удалить блок")
async def menu_del_block(
    interaction: discord.Interaction,
    block: int,
    confirm: bool = True
):
    if not is_manager(interaction):
        return await deny(interaction)

    try:
        delete_block(block)
        await update_table_messages()
        await send_result(interaction, "Блок удалён.")
    except ValueError as error:
        await send_result(interaction, str(error))


@menu_group.command(name="edit-row", description="Изменить количество строк")
async def menu_edit_row(
    interaction: discord.Interaction,
    block: int,
    rows: int,
    confirm: bool = True
):
    if not is_manager(interaction):
        return await deny(interaction)

    current = db.execute(
        """
        SELECT COUNT(*) AS count
        FROM rows
        WHERE block_number = ?
        """,
        (block,)
    ).fetchone()["count"]

    if not current:
        return await send_result(interaction, "Такого блока не существует.")

    if rows < current:
        filled = db.execute(
            """
            SELECT COUNT(*) AS count
            FROM rows
            WHERE block_number = ?
              AND row_number > ?
              AND (
                  player != ''
                  OR before_coins != 0
                  OR paid_coins != 0
              )
            """,
            (block, rows)
        ).fetchone()["count"]

        if filled and not confirm:
            return await send_result(
                interaction,
                "Удаляемые строки заполнены. Повторите с confirm:true."
            )

    try:
        resize_block(block, rows)
        await update_table_messages()
        await send_result(interaction, "Количество строк изменено.")
    except ValueError as error:
        await send_result(interaction, str(error))


@menu_group.command(name="show", description="Показать таблицу и настройки")
async def menu_show(interaction: discord.Interaction):
    if not is_manager(interaction):
        return await deny(interaction)

    block_count = db.execute(
        "SELECT COUNT(*) AS count FROM blocks"
    ).fetchone()["count"]

    row_count = db.execute(
        "SELECT COUNT(*) AS count FROM rows"
    ).fetchone()["count"]

    text = (
        f"Блоков: {block_count}\n"
        f"Строк: {row_count}\n"
        f"Канал таблицы: {TABLE_CHANNEL_ID}\n"
        f"Сообщений таблицы: "
        f"{db.execute('SELECT COUNT(*) AS count FROM table_messages').fetchone()['count']}"
    )

    await send_result(interaction, text)


@menu_group.command(name="refresh", description="Обновить таблицу")
async def menu_refresh(interaction: discord.Interaction):
    if not is_manager(interaction):
        return await deny(interaction)

    await update_table_messages()
    await send_result(interaction, "Таблица обновлена.")


@menu_group.command(name="reset", description="Сбросить таблицу")
async def menu_reset(
    interaction: discord.Interaction,
    confirm: bool = False
):
    if not is_manager(interaction):
        return await deny(interaction)

    if not confirm:
        return await send_result(
            interaction,
            "Все данные будут удалены. Повторите команду с confirm:true."
        )

    db.execute("DELETE FROM rows")
    db.execute("DELETE FROM blocks")
    db.commit()

    initialize_table()
    await update_table_messages()
    await send_result(interaction, "Таблица сброшена.")


bot.tree.add_command(menu_group)


# -----------------------------
# Запуск
# -----------------------------

@bot.event
async def on_ready():
    initialize_table()

    print(f"Бот запущен: {bot.user}")
    print(f"TABLE_CHANNEL_ID: {TABLE_CHANNEL_ID}")

    channel = bot.get_channel(TABLE_CHANNEL_ID)

    if channel is None:
        print("Канал не найден через кеш Discord.")
    else:
        print(
            f"Канал найден: #{channel.name}, "
            f"сервер: {channel.guild.name}, "
            f"тип: {channel.type}"
        )

        permissions = channel.permissions_for(channel.guild.me)

        print("Права бота в канале:")
        print(f"  view_channel: {permissions.view_channel}")
        print(f"  send_messages: {permissions.send_messages}")
        print(f"  embed_links: {permissions.embed_links}")
        print(f"  read_history: {permissions.read_message_history}")
        print(f"  manage_messages: {permissions.manage_messages}")

    try:
        await bot.tree.sync()
        print("Slash-команды синхронизированы.")
    except Exception as error:
        print(f"Ошибка синхронизации команд: {error}")

    bot.add_view(RefreshView())

    try:
        await update_table_messages()
    except Exception as error:
        print(f"Ошибка обновления таблицы: {error}")



if not TOKEN:
    raise RuntimeError("В .env не указан DISCORD_TOKEN")

if not TABLE_CHANNEL_ID:
    raise RuntimeError("В .env не указан TABLE_CHANNEL_ID")


init_db()
bot.run(TOKEN)

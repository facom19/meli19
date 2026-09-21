
import os
import html
import logging
import secrets
import sqlite3
from datetime import datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ============================================================
# CONFIG
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")
BOOTSTRAP_ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID")
DATABASE_FILE = os.getenv("DATABASE_FILE", "shop.db")

# This username is used only as a bootstrap authorization fallback.
# The admin panel itself stores the customer-facing support username
# separately, so changing the displayed username does not lock you out.
DEFAULT_BOOTSTRAP_ADMIN_USERNAME = "berizienuhq"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ============================================================
# DATABASE
# ============================================================

def db():
    conn = sqlite3.connect(DATABASE_FILE)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def table_columns(conn, table_name):
    return {
        row["name"]
        for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    }


def ensure_column(conn, table_name, column_name, definition):
    if column_name not in table_columns(conn, table_name):
        conn.execute(
            f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}"
        )


def init_db():
    conn = db()
    cur = conn.cursor()

    # Core configuration.
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )

    # Generic customer-facing categories.
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS categories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            button_text TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            enabled INTEGER NOT NULL DEFAULT 1,
            sort_order INTEGER NOT NULL DEFAULT 0
        )
        """
    )

    # Exchange currencies / payment methods.
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS currencies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            button_text TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            sort_order INTEGER NOT NULL DEFAULT 0
        )
        """
    )

    # Products/offers.
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            button_text TEXT NOT NULL,
            amount INTEGER NOT NULL DEFAULT 0,
            enabled INTEGER NOT NULL DEFAULT 1,
            sort_order INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    ensure_column(conn, "products", "category_id", "INTEGER")
    ensure_column(conn, "products", "description", "TEXT NOT NULL DEFAULT ''")

    # Price matrix: currency x product.
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS prices (
            currency_id INTEGER NOT NULL,
            product_id INTEGER NOT NULL,
            price TEXT NOT NULL DEFAULT 'NA',
            PRIMARY KEY(currency_id, product_id)
        )
        """
    )

    # Price for arbitrary/custom amounts.
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS custom_prices (
            currency_id INTEGER PRIMARY KEY,
            price TEXT NOT NULL DEFAULT 'NA'
        )
        """
    )

    # Editable customer/admin text templates.
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS texts (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )

    # Editable customer button labels.
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS buttons (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )

    # Orders.
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_number TEXT UNIQUE NOT NULL,
            telegram_id INTEGER NOT NULL,
            telegram_username TEXT,
            telegram_name TEXT,
            category TEXT NOT NULL DEFAULT '',
            currency TEXT NOT NULL,
            product TEXT NOT NULL,
            robux_amount INTEGER NOT NULL DEFAULT 0,
            price TEXT NOT NULL,
            roblox_username TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'awaiting_confirmation',
            created_at TEXT NOT NULL
        )
        """
    )

    # Migrate older order tables without breaking an existing database.
    ensure_column(conn, "orders", "category", "TEXT NOT NULL DEFAULT ''")
    ensure_column(
        conn,
        "orders",
        "status",
        "TEXT NOT NULL DEFAULT 'awaiting_confirmation'",
    )

    # Optional multi-admin list.
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS admins (
            telegram_id INTEGER PRIMARY KEY,
            label TEXT NOT NULL DEFAULT '',
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        )
        """
    )

    # Seed settings. Existing customized values are preserved.
    defaults = {
        "shop_name": "SRPExchange",
        "admin_username": "@berizienuhq",
        "order_recipient_chat_id": BOOTSTRAP_ADMIN_CHAT_ID or "",
        "max_custom_amount": "1000000",
        "delete_user_messages": "1",
    }

    for key, value in defaults.items():
        cur.execute(
            """
            INSERT OR IGNORE INTO settings(key, value)
            VALUES (?, ?)
            """,
            (key, value),
        )

    # Upgrade the old default brand automatically.
    current_name = cur.execute(
        "SELECT value FROM settings WHERE key = 'shop_name'"
    ).fetchone()
    if current_name and current_name["value"] == "R$ EXCHANGE":
        cur.execute(
            "UPDATE settings SET value = 'SRPExchange' WHERE key = 'shop_name'"
        )

    # Seed categories only when none exist.
    category_count = cur.execute(
        "SELECT COUNT(*) AS n FROM categories"
    ).fetchone()["n"]

    if category_count == 0:
        cur.execute(
            """
            INSERT INTO categories
            (name, button_text, description, enabled, sort_order)
            VALUES (?, ?, ?, 1, ?)
            """,
            (
                "Robux",
                "💎 Robux",
                "Choose a Robux amount to exchange.",
                1,
            ),
        )

    # Seed currencies only when none exist.
    currency_count = cur.execute(
        "SELECT COUNT(*) AS n FROM currencies"
    ).fetchone()["n"]

    if currency_count == 0:
        currencies = [
            ("GRAM", "💎 GRAM", 1),
            ("Telegram Stars", "⭐ Telegram Stars", 2),
        ]
        cur.executemany(
            """
            INSERT INTO currencies
            (name, button_text, enabled, sort_order)
            VALUES (?, ?, 1, ?)
            """,
            currencies,
        )

    # Seed products only when none exist.
    product_count = cur.execute(
        "SELECT COUNT(*) AS n FROM products"
    ).fetchone()["n"]

    if product_count == 0:
        category_id = cur.execute(
            "SELECT id FROM categories ORDER BY sort_order, id LIMIT 1"
        ).fetchone()["id"]

        products = [
            ("200 Robux", "200 Robux", 200, category_id, 1),
            ("500 Robux", "500 Robux", 500, category_id, 2),
            ("700 Robux", "700 Robux", 700, category_id, 3),
            ("1,000 Robux", "1,000 Robux", 1000, category_id, 4),
        ]

        cur.executemany(
            """
            INSERT INTO products
            (name, button_text, amount, category_id, enabled, sort_order)
            VALUES (?, ?, ?, ?, 1, ?)
            """,
            products,
        )

    # Backfill old products into the first category if needed.
    first_category = cur.execute(
        "SELECT id FROM categories ORDER BY sort_order, id LIMIT 1"
    ).fetchone()
    if first_category:
        cur.execute(
            """
            UPDATE products
            SET category_id = ?
            WHERE category_id IS NULL
            """,
            (first_category["id"],),
        )

    # Make sure every product/currency combination has a price row.
    currencies = cur.execute("SELECT id FROM currencies").fetchall()
    products = cur.execute("SELECT id FROM products").fetchall()

    for currency in currencies:
        for product in products:
            cur.execute(
                """
                INSERT OR IGNORE INTO prices(currency_id, product_id, price)
                VALUES (?, ?, 'NA')
                """,
                (currency["id"], product["id"]),
            )

    # Seed editable screens if they do not exist yet.
    text_defaults = {
        "welcome": (
            "🛍️ <b>{shop_name}</b>\n\n"
            "Welcome! Choose an option below to get started."
        ),
        "category": (
            "🗂️ <b>SELECT A CATEGORY</b>\n\n"
            "Choose what you would like to exchange."
        ),
        "currency": (
            "💱 <b>SELECT PAYMENT METHOD</b>\n\n"
            "Choose how you want to pay for your exchange."
        ),
        "product": (
            "📦 <b>SELECT YOUR AMOUNT</b>\n\n"
            "Choose an available option below."
        ),
        "custom": (
            "✏️ <b>CUSTOM AMOUNT</b>\n\n"
            "Send the amount of Robux you want.\n\n"
            "Example: <code>2500</code>"
        ),
        "username": (
            "👤 <b>ROBLOX USERNAME</b>\n\n"
            "Send your Roblox username."
        ),
        "how": (
            "ℹ️ <b>HOW IT WORKS</b>\n\n"
            "1️⃣ Choose a category.\n"
            "2️⃣ Choose a payment method.\n"
            "3️⃣ Choose an amount or enter a custom amount.\n"
            "4️⃣ Send your Roblox username.\n"
            "5️⃣ Your order is sent to the team and awaits confirmation."
        ),
        "confirmation": (
            "✅ <b>ORDER SENT</b>\n\n"
            "🔐 Order code: <code>{order_number}</code>\n\n"
            "Your order has been sent and is now <b>awaiting confirmation</b>.\n\n"
            "For your security, only trust a message that references this exact order code.\n\n"
            "<b>{shop_name}</b> will contact you here when your order is confirmed."
        ),
        "cancelled": (
            "❌ <b>CANCELLED</b>\n\n"
            "Your current action has been cancelled."
        ),
        "no_categories": (
            "🗂️ <b>NO CATEGORIES AVAILABLE</b>\n\n"
            "There are currently no exchange categories available."
        ),
        "no_products": (
            "📦 <b>NO PRODUCTS AVAILABLE</b>\n\n"
            "There are currently no products in this category."
        ),
        "invalid_amount": (
            "⚠️ Please enter a valid amount between 1 and {max_custom_amount}."
        ),
        "invalid_username": (
            "⚠️ That doesn't look like a valid Roblox username.\n\n"
            "Please try again."
        ),
        "order_error": (
            "⚠️ <b>ORDER NOT CREATED</b>\n\n"
            "Something went wrong while creating your order. Please try again."
        ),
        "no_session": (
            "Please open the shop again with /start."
        ),
        "admin_new_order": (
            "🔔 <b>NEW ORDER</b>\n\n"
            "🔐 Security code: <code>{order_number}</code>\n"
            "📦 Category: <b>{category}</b>\n"
            "💱 Payment method: <b>{currency}</b>\n"
            "📦 Product: <b>{product}</b>\n"
            "💰 Amount: <b>{amount:,} Robux</b>\n"
            "💵 Price: <b>{price}</b>\n"
            "👤 Roblox username: <code>{roblox_username}</code>\n"
            "📅 Created: <b>{created_at}</b>\n"
            "⏳ Status: <b>Awaiting confirmation</b>\n\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            "👤 Customer: <b>{customer_name}</b>\n"
            "📱 Telegram: <b>{customer_username}</b>\n"
            "🆔 Chat ID: <code>{telegram_id}</code>"
        ),
        "admin_no_recipient": (
            "⚠️ <b>ORDER SAVED — ADMIN NOTIFIED FAILED</b>\n\n"
            "Order <code>{order_number}</code> is stored in the database,\n"
            "but the configured admin chat could not be reached.\n\n"
            "Reason: <code>{error}</code>"
        ),
    }

    # If an old confirmation template exists, keep it unless it still has
    # the old generic wording. New installs use the safer anti-impersonation
    # wording above.
    for key, value in text_defaults.items():
        cur.execute(
            """
            INSERT OR IGNORE INTO texts(key, value)
            VALUES (?, ?)
            """,
            (key, value),
        )

    button_defaults = {
        "exchange": "💱 Exchange",
        "how": "ℹ️ How It Works",
        "custom": "✏️ Custom Amount",
        "back": "↩️ Back",
        "home": "🏠 Main Menu",
        "new_order": "💱 New Order",
        "cancel": "❌ Cancel",
        "categories": "🗂️ Categories",
    }

    for key, value in button_defaults.items():
        cur.execute(
            """
            INSERT OR IGNORE INTO buttons(key, value)
            VALUES (?, ?)
            """,
            (key, value),
        )

    # Bootstrap the current owner as an admin when possible.
    if BOOTSTRAP_ADMIN_CHAT_ID:
        try:
            admin_id = int(BOOTSTRAP_ADMIN_CHAT_ID)
            cur.execute(
                """
                INSERT OR IGNORE INTO admins
                (telegram_id, label, enabled, created_at)
                VALUES (?, ?, 1, ?)
                """,
                (
                    admin_id,
                    "Bootstrap admin",
                    datetime.now().strftime("%d.%m.%Y %H:%M:%S"),
                ),
            )
        except ValueError:
            logger.warning("ADMIN_CHAT_ID is not a numeric Telegram chat ID.")

    conn.commit()
    conn.close()


# ============================================================
# DATABASE HELPERS
# ============================================================

def get_setting(key, default=""):
    conn = db()
    row = conn.execute(
        "SELECT value FROM settings WHERE key = ?",
        (key,),
    ).fetchone()
    conn.close()
    return row["value"] if row else default


def set_setting(key, value):
    conn = db()
    conn.execute(
        """
        INSERT INTO settings(key, value)
        VALUES (?, ?)
        ON CONFLICT(key)
        DO UPDATE SET value = excluded.value
        """,
        (key, str(value)),
    )
    conn.commit()
    conn.close()


def get_text(key, default=""):
    conn = db()
    row = conn.execute(
        "SELECT value FROM texts WHERE key = ?",
        (key,),
    ).fetchone()
    conn.close()
    return row["value"] if row else default


def set_text(key, value):
    conn = db()
    conn.execute(
        """
        INSERT INTO texts(key, value)
        VALUES (?, ?)
        ON CONFLICT(key)
        DO UPDATE SET value = excluded.value
        """,
        (key, value),
    )
    conn.commit()
    conn.close()


def get_button(key, default=None):
    fallback = default if default is not None else key
    conn = db()
    row = conn.execute(
        "SELECT value FROM buttons WHERE key = ?",
        (key,),
    ).fetchone()
    conn.close()
    return row["value"] if row else fallback


def set_button(key, value):
    conn = db()
    conn.execute(
        """
        INSERT INTO buttons(key, value)
        VALUES (?, ?)
        ON CONFLICT(key)
        DO UPDATE SET value = excluded.value
        """,
        (key, value),
    )
    conn.commit()
    conn.close()


def get_categories(enabled_only=False):
    conn = db()
    sql = (
        """
        SELECT * FROM categories
        WHERE enabled = 1
        ORDER BY sort_order, id
        """
        if enabled_only
        else
        """
        SELECT * FROM categories
        ORDER BY sort_order, id
        """
    )
    rows = conn.execute(sql).fetchall()
    conn.close()
    return rows


def get_category(category_id):
    conn = db()
    row = conn.execute(
        "SELECT * FROM categories WHERE id = ?",
        (category_id,),
    ).fetchone()
    conn.close()
    return row


def get_currencies(enabled_only=False):
    conn = db()
    sql = (
        """
        SELECT * FROM currencies
        WHERE enabled = 1
        ORDER BY sort_order, id
        """
        if enabled_only
        else
        """
        SELECT * FROM currencies
        ORDER BY sort_order, id
        """
    )
    rows = conn.execute(sql).fetchall()
    conn.close()
    return rows


def get_currency(currency_id):
    conn = db()
    row = conn.execute(
        "SELECT * FROM currencies WHERE id = ?",
        (currency_id,),
    ).fetchone()
    conn.close()
    return row


def get_products(category_id=None, enabled_only=False):
    conn = db()
    query = "SELECT * FROM products"
    args = []
    conditions = []

    if category_id is not None:
        conditions.append("category_id = ?")
        args.append(category_id)

    if enabled_only:
        conditions.append("enabled = 1")

    if conditions:
        query += " WHERE " + " AND ".join(conditions)

    query += " ORDER BY sort_order, id"

    rows = conn.execute(query, tuple(args)).fetchall()
    conn.close()
    return rows


def get_product(product_id):
    conn = db()
    row = conn.execute(
        "SELECT * FROM products WHERE id = ?",
        (product_id,),
    ).fetchone()
    conn.close()
    return row


def get_price(currency_id, product_id):
    conn = db()
    row = conn.execute(
        """
        SELECT price
        FROM prices
        WHERE currency_id = ?
          AND product_id = ?
        """,
        (currency_id, product_id),
    ).fetchone()
    conn.close()
    return row["price"] if row else "NA"


def set_price(currency_id, product_id, price):
    conn = db()
    conn.execute(
        """
        INSERT INTO prices(currency_id, product_id, price)
        VALUES (?, ?, ?)
        ON CONFLICT(currency_id, product_id)
        DO UPDATE SET price = excluded.price
        """,
        (currency_id, product_id, price.strip() or "NA"),
    )
    conn.commit()
    conn.close()


def get_custom_price(currency_id):
    conn = db()
    row = conn.execute(
        "SELECT price FROM custom_prices WHERE currency_id = ?",
        (currency_id,),
    ).fetchone()
    conn.close()
    return row["price"] if row else "NA"


def set_custom_price(currency_id, price):
    conn = db()
    conn.execute(
        """
        INSERT INTO custom_prices(currency_id, price)
        VALUES (?, ?)
        ON CONFLICT(currency_id)
        DO UPDATE SET price = excluded.price
        """,
        (currency_id, price.strip() or "NA"),
    )
    conn.commit()
    conn.close()


def get_orders(limit=30):
    conn = db()
    rows = conn.execute(
        """
        SELECT *
        FROM orders
        ORDER BY id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    conn.close()
    return rows


def get_order(order_number):
    conn = db()
    row = conn.execute(
        "SELECT * FROM orders WHERE order_number = ?",
        (order_number,),
    ).fetchone()
    conn.close()
    return row


def set_order_status(order_number, status):
    conn = db()
    conn.execute(
        "UPDATE orders SET status = ? WHERE order_number = ?",
        (status, order_number),
    )
    conn.commit()
    conn.close()


def add_admin(telegram_id, label=""):
    conn = db()
    conn.execute(
        """
        INSERT INTO admins(telegram_id, label, enabled, created_at)
        VALUES (?, ?, 1, ?)
        ON CONFLICT(telegram_id)
        DO UPDATE SET label = excluded.label, enabled = 1
        """,
        (
            telegram_id,
            label,
            datetime.now().strftime("%d.%m.%Y %H:%M:%S"),
        ),
    )
    conn.commit()
    conn.close()


def remove_admin(telegram_id):
    conn = db()
    conn.execute(
        "DELETE FROM admins WHERE telegram_id = ?",
        (telegram_id,),
    )
    conn.commit()
    conn.close()


def get_admins():
    conn = db()
    rows = conn.execute(
        """
        SELECT *
        FROM admins
        ORDER BY created_at, telegram_id
        """
    ).fetchall()
    conn.close()
    return rows


# ============================================================
# SECURITY / AUTH
# ============================================================

def is_admin(user):
    if not user:
        return False

    # Preserve the environment bootstrap admin.
    if BOOTSTRAP_ADMIN_CHAT_ID:
        try:
            if user.id == int(BOOTSTRAP_ADMIN_CHAT_ID):
                return True
        except ValueError:
            pass

    if user.username and user.username.lower() == DEFAULT_BOOTSTRAP_ADMIN_USERNAME.lower():
        return True

    conn = db()
    row = conn.execute(
        """
        SELECT 1
        FROM admins
        WHERE telegram_id = ?
          AND enabled = 1
        """,
        (user.id,),
    ).fetchone()
    conn.close()
    return row is not None


# ============================================================
# SESSION / CLEAN SCREEN
# ============================================================

sessions = {}
# Last customer/admin screen message per user. Kept outside sessions so
# clearing a workflow never loses the message that must be deleted next.
screen_messages = {}


def session_for(user_id):
    return sessions.setdefault(user_id, {})


async def safe_delete(bot, chat_id, message_id):
    if not message_id:
        return
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except (BadRequest, Forbidden, TelegramError):
        pass


async def remember_screen(
    bot,
    chat_id,
    user_id,
    text,
    reply_markup=None,
    parse_mode="HTML",
    delete_previous=True,
):
    # IMPORTANT: the screen message must NOT live inside `sessions`.
    # Workflows frequently call sessions.pop(...) when they finish, and
    # doing that used to erase the ID of the previous bot message. The next
    # /start therefore could not delete it, causing messages to pile up.
    previous_message_id = screen_messages.get(user_id)

    if delete_previous and previous_message_id:
        await safe_delete(bot, chat_id, previous_message_id)

    message = await bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode=parse_mode,
        reply_markup=reply_markup,
    )

    screen_messages[user_id] = message.message_id
    return message


async def edit_screen(
    query,
    user_id,
    text,
    reply_markup=None,
    parse_mode="HTML",
):
    bot = query.get_bot()
    chat_id = query.message.chat_id

    try:
        # Inline navigation edits ONE existing screen instead of creating a
        # new message. This is the cleanest Telegram UX.
        await query.edit_message_text(
            text=text,
            parse_mode=parse_mode,
            reply_markup=reply_markup,
        )
        screen_messages[user_id] = query.message.message_id
    except BadRequest as error:
        # Telegram returns this when the requested content is identical.
        if "Message is not modified" in str(error):
            screen_messages[user_id] = query.message.message_id
            return

        # If the old screen disappeared (manual deletion, message expiry,
        # etc.), create exactly ONE replacement and remember its ID.
        try:
            await safe_delete(
                bot,
                chat_id,
                screen_messages.get(user_id),
            )
            message = await bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode=parse_mode,
                reply_markup=reply_markup,
            )
            screen_messages[user_id] = message.message_id
        except TelegramError:
            raise


async def clear_user_message(update):
    """Best-effort cleanup.

    Telegram permissions vary by chat type. In private chats this may fail,
    so the exception is intentionally ignored.
    """
    if not update.message:
        return
    if get_setting("delete_user_messages", "1") != "1":
        return
    await safe_delete(
        update.get_bot(),
        update.effective_chat.id,
        update.message.message_id,
    )


# ============================================================
# FORMATTING
# ============================================================

def clean(value):
    return html.escape(str(value))


def render_text(key, **extra):
    values = {
        "shop_name": clean(get_setting("shop_name", "SRPExchange")),
        "admin_username": clean(get_setting("admin_username", "@berizienuhq")),
        "max_custom_amount": clean(get_setting("max_custom_amount", "1000000")),
        "order_recipient_chat_id": clean(
            get_setting("order_recipient_chat_id", "")
        ),
    }
    values.update(
        {
            key_: (
                clean(value)
                if isinstance(value, str)
                else value
            )
            for key_, value in extra.items()
        }
    )

    template = get_text(key, "")
    if not template:
        return ""

    try:
        return template.format(**values)
    except (KeyError, ValueError, IndexError):
        # A malformed admin template should never crash the bot.
        return template


# ============================================================
# CUSTOMER KEYBOARDS
# ============================================================

def two_column_keyboard(buttons, bottom=None):
    rows = []
    row = []

    for button in buttons:
        row.append(button)
        if len(row) == 2:
            rows.append(row)
            row = []

    if row:
        rows.append(row)

    if bottom:
        rows.extend(bottom)

    return InlineKeyboardMarkup(rows)


def main_keyboard():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    get_button("exchange", "💱 Exchange"),
                    callback_data="exchange",
                )
            ],
            [
                InlineKeyboardButton(
                    get_button("how", "ℹ️ How It Works"),
                    callback_data="how",
                )
            ],
        ]
    )


def category_keyboard():
    buttons = [
        InlineKeyboardButton(
            category["button_text"],
            callback_data=f"category:{category['id']}",
        )
        for category in get_categories(True)
    ]
    return two_column_keyboard(
        buttons,
        bottom=[
            [
                InlineKeyboardButton(
                    get_button("home", "🏠 Main Menu"),
                    callback_data="home",
                )
            ]
        ],
    )


def currency_keyboard():
    buttons = [
        InlineKeyboardButton(
            currency["button_text"],
            callback_data=f"currency:{currency['id']}",
        )
        for currency in get_currencies(True)
    ]

    return two_column_keyboard(
        buttons,
        bottom=[
            [
                InlineKeyboardButton(
                    get_button("back", "↩️ Back"),
                    callback_data="exchange",
                )
            ]
        ],
    )


def product_keyboard(category_id, currency_id):
    session_products = get_products(category_id, True)
    buttons = []

    for product in session_products:
        price = get_price(currency_id, product["id"])
        label = product["button_text"]

        # Price is appended only when configured, keeping buttons compact.
        if price and price.upper() != "NA":
            label = f"{label} · {price}"

        buttons.append(
            InlineKeyboardButton(
                label,
                callback_data=f"product:{product['id']}",
            )
        )

    buttons.append(
        InlineKeyboardButton(
            get_button("custom", "✏️ Custom Amount"),
            callback_data="custom",
        )
    )

    return two_column_keyboard(
        buttons,
        bottom=[
            [
                InlineKeyboardButton(
                    get_button("back", "↩️ Back"),
                    callback_data="currency_back",
                )
            ]
        ],
    )


def cancel_keyboard():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    get_button("cancel", "❌ Cancel"),
                    callback_data="home",
                )
            ]
        ]
    )


# ============================================================
# ADMIN KEYBOARDS
# ============================================================

def admin_keyboard():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("📦 Products", callback_data="admin_products"),
                InlineKeyboardButton("🗂️ Categories", callback_data="admin_categories"),
            ],
            [
                InlineKeyboardButton("💱 Currencies", callback_data="admin_currencies"),
                InlineKeyboardButton("💰 Prices", callback_data="admin_prices"),
            ],
            [
                InlineKeyboardButton("📝 Texts / Steps", callback_data="admin_texts"),
                InlineKeyboardButton("🔘 Buttons", callback_data="admin_buttons"),
            ],
            [
                InlineKeyboardButton("🏪 Shop Settings", callback_data="admin_settings"),
                InlineKeyboardButton("👮 Admins", callback_data="admin_admins"),
            ],
            [
                InlineKeyboardButton("📋 Orders", callback_data="admin_orders"),
            ],
            [
                InlineKeyboardButton("🏠 Shop Preview", callback_data="home"),
            ],
        ]
    )


def back_to_admin():
    return [
        [
            InlineKeyboardButton("⚙️ Admin Panel", callback_data="admin")
        ]
    ]


# ============================================================
# START / ADMIN
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    await clear_user_message(update)
    sessions.pop(user_id, None)

    # /start is sent by the user; remember that command message may have
    # failed deletion and ignore the failure.
    await remember_screen(
        context.bot,
        update.effective_chat.id,
        user_id,
        render_text("welcome"),
        main_keyboard(),
    )


async def admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user):
        await clear_user_message(update)
        await remember_screen(
            context.bot,
            update.effective_chat.id,
            update.effective_user.id,
            "⛔ <b>ACCESS DENIED</b>",
        )
        return

    await clear_user_message(update)
    sessions.pop(update.effective_user.id, None)

    await remember_screen(
        context.bot,
        update.effective_chat.id,
        update.effective_user.id,
        "⚙️ <b>ADMIN PANEL</b>\n\n"
        "Everything important can be managed from this menu.\n"
        "Changes are saved instantly and used immediately by the shop.",
        admin_keyboard(),
    )


# ============================================================
# CUSTOMER SCREENS
# ============================================================

async def show_exchange(query):
    await edit_screen(
        query,
        query.from_user.id,
        render_text("category"),
        category_keyboard(),
    )


async def show_how(query):
    await edit_screen(
        query,
        query.from_user.id,
        render_text("how"),
        InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        get_button("exchange", "💱 Exchange"),
                        callback_data="exchange",
                    )
                ],
                [
                    InlineKeyboardButton(
                        get_button("back", "↩️ Back"),
                        callback_data="home",
                    )
                ],
            ]
        ),
    )


async def show_categories(query):
    categories = get_categories(True)
    if not categories:
        await edit_screen(
            query,
            query.from_user.id,
            render_text("no_categories"),
            InlineKeyboardMarkup(back_to_admin() if is_admin(query.from_user) else [
                [
                    InlineKeyboardButton(
                        get_button("home", "🏠 Main Menu"),
                        callback_data="home",
                    )
                ]
            ]),
        )
        return

    await edit_screen(
        query,
        query.from_user.id,
        render_text("category"),
        category_keyboard(),
    )


async def show_currency_step(query, category_id):
    category = get_category(category_id)

    if (
        not category
        or not category["enabled"]
    ):
        await query.answer(
            "This category is unavailable.",
            show_alert=True,
        )
        return

    products = get_products(category_id, True)
    if not products:
        await edit_screen(
            query,
            query.from_user.id,
            render_text(
                "no_products",
            ),
            InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            get_button("back", "↩️ Back"),
                            callback_data="exchange",
                        )
                    ]
                ]
            ),
        )
        return

    session_for(query.from_user.id).update(
        {
            "category_id": category_id,
            "category_name": category["name"],
            "waiting": "currency",
        }
    )

    extra = "\n\n" + clean(category["description"]) if category["description"] else ""
    text = render_text("currency") + extra

    await edit_screen(
        query,
        query.from_user.id,
        text,
        currency_keyboard(),
    )


async def show_products_step(query, currency_id):
    session = sessions.get(query.from_user.id, {})
    category_id = session.get("category_id")
    currency = get_currency(currency_id)

    if not category_id or not currency or not currency["enabled"]:
        await query.answer(
            "Please start the order again.",
            show_alert=True,
        )
        return

    products = get_products(category_id, True)
    if not products:
        await edit_screen(
            query,
            query.from_user.id,
            render_text("no_products"),
            InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            get_button("back", "↩️ Back"),
                            callback_data="exchange",
                        )
                    ]
                ]
            ),
        )
        return

    session.update(
        {
            "currency_id": currency_id,
            "currency_name": currency["name"],
            "waiting": "product",
        }
    )

    await edit_screen(
        query,
        query.from_user.id,
        render_text("product"),
        product_keyboard(category_id, currency_id),
    )


# ============================================================
# ADMIN: CATEGORIES
# ============================================================

async def show_admin_categories(query):
    rows = [
        [
            InlineKeyboardButton(
                "➕ Add Category",
                callback_data="add_category",
            )
        ]
    ]

    categories = get_categories(False)
    if categories:
        for category in categories:
            status = "🟢" if category["enabled"] else "🔴"
            rows.append(
                [
                    InlineKeyboardButton(
                        f"{status} {category['button_text']}",
                        callback_data=f"edit_category:{category['id']}",
                    )
                ]
            )
    else:
        rows.append(
            [InlineKeyboardButton("No categories yet", callback_data="noop")]
        )

    rows.extend(back_to_admin())

    await edit_screen(
        query,
        query.from_user.id,
        "🗂️ <b>CATEGORIES</b>\n\n"
        "Create unlimited categories and put products inside them.",
        InlineKeyboardMarkup(rows),
    )


def category_admin_keyboard(category_id):
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🔤 Edit Name",
                    callback_data=f"rename_category:{category_id}",
                ),
                InlineKeyboardButton(
                    "🔘 Edit Button",
                    callback_data=f"category_button:{category_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    "📝 Edit Description",
                    callback_data=f"category_desc:{category_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    "📦 Manage Products",
                    callback_data=f"category_products:{category_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    "🟢 / 🔴 Enable / Disable",
                    callback_data=f"toggle_category:{category_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    "⬆️ Up",
                    callback_data=f"category_up:{category_id}",
                ),
                InlineKeyboardButton(
                    "⬇️ Down",
                    callback_data=f"category_down:{category_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    "🗑️ Delete Category",
                    callback_data=f"delete_category:{category_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    "↩️ Categories",
                    callback_data="admin_categories",
                )
            ],
        ]
    )


async def show_edit_category(query, category_id):
    category = get_category(category_id)
    if not category:
        await query.answer("Category not found.", show_alert=True)
        return

    products = get_products(category_id, False)
    status = "🟢 Enabled" if category["enabled"] else "🔴 Disabled"

    await edit_screen(
        query,
        query.from_user.id,
        "🗂️ <b>EDIT CATEGORY</b>\n\n"
        f"🏷️ Name: <b>{clean(category['name'])}</b>\n"
        f"🔘 Button: <b>{clean(category['button_text'])}</b>\n"
        f"📦 Products: <b>{len(products)}</b>\n"
        f"📌 Status: <b>{status}</b>",
        category_admin_keyboard(category_id),
    )


async def category_product_list(query, category_id):
    category = get_category(category_id)
    if not category:
        await query.answer("Category not found.", show_alert=True)
        return

    rows = [
        [
            InlineKeyboardButton(
                "➕ Add Product",
                callback_data=f"add_product:{category_id}",
            )
        ]
    ]

    products = get_products(category_id, False)
    for product in products:
        status = "🟢" if product["enabled"] else "🔴"
        rows.append(
            [
                InlineKeyboardButton(
                    f"{status} {product['button_text']}",
                    callback_data=f"edit_product:{product['id']}",
                )
            ]
        )

    rows.append(
        [
            InlineKeyboardButton(
                "↩️ Category",
                callback_data=f"edit_category:{category_id}",
            )
        ]
    )

    await edit_screen(
        query,
        query.from_user.id,
        f"📦 <b>{clean(category['name'])}</b>\n\n"
        "Manage the products inside this category.",
        InlineKeyboardMarkup(rows),
    )


# ============================================================
# ADMIN: PRODUCTS
# ============================================================

async def show_admin_products(query):
    rows = []
    categories = get_categories(False)

    for category in categories:
        rows.append(
            [
                InlineKeyboardButton(
                    f"🗂️ {category['name']}",
                    callback_data=f"category_products:{category['id']}",
                )
            ]
        )

    rows.extend(back_to_admin())

    await edit_screen(
        query,
        query.from_user.id,
        "📦 <b>PRODUCTS</b>\n\n"
        "Choose a category to add, edit, disable or remove products.",
        InlineKeyboardMarkup(rows),
    )


def product_admin_keyboard(product_id):
    product = get_product(product_id)
    category_id = product["category_id"] if product else 0

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🔤 Edit Name",
                    callback_data=f"rename_product:{product_id}",
                ),
                InlineKeyboardButton(
                    "🔘 Edit Button",
                    callback_data=f"product_button:{product_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    "🔢 Change Amount",
                    callback_data=f"amount_product:{product_id}",
                ),
                InlineKeyboardButton(
                    "📝 Edit Description",
                    callback_data=f"product_desc:{product_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    "🗂️ Move Category",
                    callback_data=f"move_product:{product_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    "💰 Edit Prices",
                    callback_data=f"product_prices:{product_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    "🟢 / 🔴 Enable / Disable",
                    callback_data=f"toggle_product:{product_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    "⬆️ Up",
                    callback_data=f"product_up:{product_id}",
                ),
                InlineKeyboardButton(
                    "⬇️ Down",
                    callback_data=f"product_down:{product_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    "🗑️ Delete Product",
                    callback_data=f"delete_product:{product_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    "↩️ Products",
                    callback_data=(
                        f"category_products:{category_id}"
                        if category_id
                        else "admin_products"
                    ),
                )
            ],
        ]
    )


async def show_edit_product(query, product_id):
    product = get_product(product_id)
    if not product:
        await query.answer("Product not found.", show_alert=True)
        return

    category = get_category(product["category_id"]) if product["category_id"] else None
    status = "🟢 Enabled" if product["enabled"] else "🔴 Disabled"

    await edit_screen(
        query,
        query.from_user.id,
        "📦 <b>EDIT PRODUCT</b>\n\n"
        f"🏷️ Name: <b>{clean(product['name'])}</b>\n"
        f"🔘 Button: <b>{clean(product['button_text'])}</b>\n"
        f"🔢 Amount: <b>{product['amount']:,} Robux</b>\n"
        f"🗂️ Category: <b>{clean(category['name']) if category else 'None'}</b>\n"
        f"📌 Status: <b>{status}</b>",
        product_admin_keyboard(product_id),
    )


# ============================================================
# ADMIN: CURRENCIES
# ============================================================

async def show_admin_currencies(query):
    rows = [
        [
            InlineKeyboardButton(
                "➕ Add Payment Method",
                callback_data="add_currency",
            )
        ]
    ]

    for currency in get_currencies(False):
        status = "🟢" if currency["enabled"] else "🔴"
        rows.append(
            [
                InlineKeyboardButton(
                    f"{status} {currency['button_text']}",
                    callback_data=f"edit_currency:{currency['id']}",
                )
            ]
        )

    rows.extend(back_to_admin())

    await edit_screen(
        query,
        query.from_user.id,
        "💱 <b>PAYMENT METHODS</b>\n\n"
        "Add, rename, reorder, enable or delete payment methods.",
        InlineKeyboardMarkup(rows),
    )


def currency_admin_keyboard(currency_id):
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🔤 Edit Name",
                    callback_data=f"rename_currency:{currency_id}",
                ),
                InlineKeyboardButton(
                    "🔘 Edit Button",
                    callback_data=f"currency_button:{currency_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    "🟢 / 🔴 Enable / Disable",
                    callback_data=f"toggle_currency:{currency_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    "⬆️ Up",
                    callback_data=f"currency_up:{currency_id}",
                ),
                InlineKeyboardButton(
                    "⬇️ Down",
                    callback_data=f"currency_down:{currency_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    "💰 Edit Prices",
                    callback_data=f"prices_currency:{currency_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    "🗑️ Delete Payment Method",
                    callback_data=f"delete_currency:{currency_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    "↩️ Payment Methods",
                    callback_data="admin_currencies",
                )
            ],
        ]
    )


async def show_edit_currency(query, currency_id):
    currency = get_currency(currency_id)
    if not currency:
        await query.answer("Payment method not found.", show_alert=True)
        return

    status = "🟢 Enabled" if currency["enabled"] else "🔴 Disabled"

    await edit_screen(
        query,
        query.from_user.id,
        "💱 <b>EDIT PAYMENT METHOD</b>\n\n"
        f"🏷️ Name: <b>{clean(currency['name'])}</b>\n"
        f"🔘 Button: <b>{clean(currency['button_text'])}</b>\n"
        f"📌 Status: <b>{status}</b>",
        currency_admin_keyboard(currency_id),
    )


# ============================================================
# ADMIN: PRICES
# ============================================================

async def show_admin_prices(query):
    rows = [
        [
            InlineKeyboardButton(
                f"💰 {currency['name']}",
                callback_data=f"prices_currency:{currency['id']}",
            )
        ]
        for currency in get_currencies(False)
    ]

    rows.append(
        [
            InlineKeyboardButton(
                "↩️ Admin Panel",
                callback_data="admin",
            )
        ]
    )

    await edit_screen(
        query,
        query.from_user.id,
        "💰 <b>PRICE MANAGEMENT</b>\n\n"
        "Prices are linked to payment methods and products.\n"
        "Change a price once and the customer flow updates immediately.",
        InlineKeyboardMarkup(rows),
    )


async def show_currency_prices(query, currency_id):
    currency = get_currency(currency_id)
    if not currency:
        await query.answer("Payment method not found.", show_alert=True)
        return

    rows = []
    for product in get_products(None, False):
        price = get_price(currency_id, product["id"])
        category = (
            get_category(product["category_id"])
            if product["category_id"]
            else None
        )
        prefix = f"{category['name']} · " if category else ""
        rows.append(
            [
                InlineKeyboardButton(
                    f"{prefix}{product['button_text']} → {price}",
                    callback_data=f"set_price:{currency_id}:{product['id']}",
                )
            ]
        )

    custom = get_custom_price(currency_id)
    rows.append(
        [
            InlineKeyboardButton(
                f"✏️ Custom Amount → {custom}",
                callback_data=f"set_custom:{currency_id}",
            )
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(
                "↩️ Payment Methods",
                callback_data="admin_currencies",
            )
        ]
    )

    await edit_screen(
        query,
        query.from_user.id,
        f"💰 <b>PRICES · {clean(currency['name'])}</b>\n\n"
        "Tap any price to change it.",
        InlineKeyboardMarkup(rows),
    )


async def show_product_prices(query, product_id):
    product = get_product(product_id)
    if not product:
        await query.answer("Product not found.", show_alert=True)
        return

    rows = []
    for currency in get_currencies(False):
        rows.append(
            [
                InlineKeyboardButton(
                    f"{currency['name']} → {get_price(currency['id'], product_id)}",
                    callback_data=f"set_price:{currency['id']}:{product_id}",
                )
            ]
        )

    rows.append(
        [
            InlineKeyboardButton(
                "↩️ Product",
                callback_data=f"edit_product:{product_id}",
            )
        ]
    )

    await edit_screen(
        query,
        query.from_user.id,
        f"💰 <b>PRICES · {clean(product['button_text'])}</b>\n\n"
        "Choose a payment method to edit its price.",
        InlineKeyboardMarkup(rows),
    )


# ============================================================
# ADMIN: TEXTS / BUTTONS / SETTINGS
# ============================================================

TEXT_NAMES = {
    "welcome": "👋 Welcome",
    "category": "🗂️ Category Step",
    "currency": "💱 Payment Method Step",
    "product": "📦 Product Step",
    "custom": "✏️ Custom Amount Step",
    "username": "👤 Roblox Username Step",
    "how": "ℹ️ How It Works",
    "confirmation": "✅ Order Sent / Confirmation",
    "cancelled": "❌ Cancelled",
    "no_categories": "🗂️ No Categories",
    "no_products": "📦 No Products",
    "invalid_amount": "⚠️ Invalid Amount",
    "invalid_username": "⚠️ Invalid Username",
    "order_error": "⚠️ Order Error",
    "no_session": "ℹ️ No Session",
    "admin_new_order": "🔔 Admin New Order",
    "admin_no_recipient": "⚠️ Admin Notification Error",
}

BUTTON_NAMES = {
    "exchange": "💱 Exchange",
    "how": "ℹ️ How It Works",
    "custom": "✏️ Custom Amount",
    "back": "↩️ Back",
    "home": "🏠 Main Menu",
    "new_order": "💱 New Order",
    "cancel": "❌ Cancel",
    "categories": "🗂️ Categories",
}


async def show_texts(query):
    rows = []
    for key, label in TEXT_NAMES.items():
        rows.append(
            [
                InlineKeyboardButton(
                    label,
                    callback_data=f"edit_text:{key}",
                )
            ]
        )

    rows.extend(back_to_admin())

    await edit_screen(
        query,
        query.from_user.id,
        "📝 <b>TEXTS / STEPS</b>\n\n"
        "Every customer and admin message is editable here.\n\n"
        "Use the documented placeholders shown before editing.",
        InlineKeyboardMarkup(rows),
    )


async def show_buttons(query):
    rows = []
    for key, label in BUTTON_NAMES.items():
        rows.append(
            [
                InlineKeyboardButton(
                    f"{label}: {get_button(key)}",
                    callback_data=f"edit_button:{key}",
                )
            ]
        )

    rows.extend(back_to_admin())

    await edit_screen(
        query,
        query.from_user.id,
        "🔘 <b>BUTTON EDITOR</b>\n\n"
        "Rename the labels without touching the code.",
        InlineKeyboardMarkup(rows),
    )


async def show_settings(query):
    await edit_screen(
        query,
        query.from_user.id,
        "🏪 <b>SHOP SETTINGS</b>\n\n"
        f"🏷️ Shop name: <b>{clean(get_setting('shop_name'))}</b>\n"
        f"👤 Support username: <b>{clean(get_setting('admin_username'))}</b>\n"
        f"🆔 Order recipient: <code>{clean(get_setting('order_recipient_chat_id') or 'not set')}</code>\n"
        f"🔢 Max custom amount: <b>{clean(get_setting('max_custom_amount', '1000000'))}</b>\n"
        f"🧹 Delete user messages: <b>{'ON' if get_setting('delete_user_messages', '1') == '1' else 'OFF'}</b>\n",
        InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "🏷️ Shop Name",
                        callback_data="change_shop_name",
                    ),
                    InlineKeyboardButton(
                        "👤 Support Username",
                        callback_data="change_admin_username",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        "🆔 Order Recipient",
                        callback_data="change_recipient",
                    ),
                    InlineKeyboardButton(
                        "🔢 Custom Amount Limit",
                        callback_data="change_max_amount",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        "🧹 Toggle Chat Cleanup",
                        callback_data="toggle_cleanup",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "↩️ Admin Panel",
                        callback_data="admin",
                    )
                ],
            ]
        ),
    )


async def show_admins(query):
    admins = get_admins()
    rows = [
        [
            InlineKeyboardButton(
                "➕ Add Admin",
                callback_data="add_admin",
            )
        ]
    ]

    for admin_row in admins:
        label = admin_row["label"] or str(admin_row["telegram_id"])
        rows.append(
            [
                InlineKeyboardButton(
                    f"👮 {label} · {admin_row['telegram_id']}",
                    callback_data=f"remove_admin_confirm:{admin_row['telegram_id']}",
                )
            ]
        )

    rows.extend(back_to_admin())

    await edit_screen(
        query,
        query.from_user.id,
        "👮 <b>ADMINS</b>\n\n"
        "Tap an admin to remove them.\n"
        "The environment bootstrap admin always keeps access.",
        InlineKeyboardMarkup(rows),
    )


# ============================================================
# ADMIN INPUT STARTERS
# ============================================================

def start_admin_action(user_id, action, **data):
    sessions[user_id] = {"action": action, **data}


async def ask_text_input(query, action, prompt, **data):
    start_admin_action(query.from_user.id, action, **data)
    await edit_screen(
        query,
        query.from_user.id,
        prompt,
    )


async def start_add_category(query):
    await ask_text_input(
        query,
        "add_category_name",
        "➕ <b>ADD CATEGORY</b>\n\nSend the category name.\n\nExample: <code>Robux</code>\n\n/cancel to stop.",
    )


async def start_add_product(query, category_id):
    await ask_text_input(
        query,
        "add_product_name",
        "➕ <b>ADD PRODUCT</b>\n\n"
        "Send the product name.\n\n"
        "Example: <code>2,000 Robux</code>",
        category_id=category_id,
    )


async def start_add_currency(query):
    await ask_text_input(
        query,
        "add_currency_name",
        "➕ <b>ADD PAYMENT METHOD</b>\n\n"
        "Send the payment method name.\n\n"
        "Example: <code>USDT</code>",
    )


# ============================================================
# CALLBACK HANDLER
# ============================================================

ADMIN_PREFIXES = (
    "admin",
    "add_",
    "edit_",
    "rename_",
    "amount_",
    "toggle_",
    "delete_",
    "product_",
    "category_",
    "currency_",
    "move_",
    "set_price:",
    "set_custom:",
    "prices_currency:",
    "change_",
    "remove_admin",
)


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data or ""
    user = query.from_user
    user_id = user.id

    # --------------------------------------------------------
    # CUSTOMER
    # --------------------------------------------------------

    if data == "noop":
        return

    if data == "home":
        sessions.pop(user_id, None)
        await edit_screen(
            query,
            user_id,
            render_text("welcome"),
            main_keyboard(),
        )
        return

    if data == "exchange":
        sessions.pop(user_id, None)
        await show_exchange(query)
        return

    if data == "how":
        await show_how(query)
        return

    if data == "currency_back":
        session = sessions.get(user_id)
        if not session:
            await show_exchange(query)
            return
        await show_currency_step(query, session["category_id"])
        return

    if data.startswith("category:"):
        category_id = int(data.split(":", 1)[1])
        category = get_category(category_id)
        if (
            not category
            or not category["enabled"]
        ):
            await query.answer(
                "This category is not currently public.",
                show_alert=True,
            )
            return
        await show_currency_step(query, category_id)
        return

    if data.startswith("currency:"):
        currency_id = int(data.split(":", 1)[1])
        await show_products_step(query, currency_id)
        return

    if data.startswith("product:"):
        product_id = int(data.split(":", 1)[1])
        session = sessions.get(user_id)

        if not session or session.get("waiting") != "product":
            await query.answer(
                "Please start a new order.",
                show_alert=True,
            )
            return

        product = get_product(product_id)
        if (
            not product
            or not product["enabled"]
            or product["category_id"] != session.get("category_id")
        ):
            await query.answer(
                "This product is unavailable.",
                show_alert=True,
            )
            return

        price = get_price(session["currency_id"], product_id)

        session.update(
            {
                "product_id": product_id,
                "product": product["name"],
                "amount": product["amount"],
                "price": price,
                "waiting": "username",
            }
        )

        await edit_screen(
            query,
            user_id,
            render_text("username"),
            cancel_keyboard(),
        )
        return

    if data == "custom":
        session = sessions.get(user_id)
        if not session or not session.get("currency_id"):
            await query.answer(
                "Please start a new order.",
                show_alert=True,
            )
            return

        session["waiting"] = "custom_amount"
        await edit_screen(
            query,
            user_id,
            render_text("custom"),
            cancel_keyboard(),
        )
        return

    # --------------------------------------------------------
    # ADMIN ACCESS CHECK
    # --------------------------------------------------------

    if data.startswith(ADMIN_PREFIXES):
        if not is_admin(user):
            await query.answer(
                "⛔ Access denied.",
                show_alert=True,
            )
            return

    # --------------------------------------------------------
    # ADMIN HOME
    # --------------------------------------------------------

    if data == "admin":
        await edit_screen(
            query,
            user_id,
            "⚙️ <b>ADMIN PANEL</b>\n\n"
            "Manage your shop directly from Telegram.",
            admin_keyboard(),
        )
        return

    # --------------------------------------------------------
    # CATEGORIES
    # --------------------------------------------------------

    if data == "admin_categories":
        await show_admin_categories(query)
        return

    if data == "add_category":
        await start_add_category(query)
        return

    if data.startswith("edit_category:"):
        await show_edit_category(
            query,
            int(data.split(":", 1)[1]),
        )
        return

    if data.startswith("category_products:"):
        await category_product_list(
            query,
            int(data.split(":", 1)[1]),
        )
        return

    if data.startswith("rename_category:"):
        await ask_text_input(
            query,
            "rename_category",
            "🔤 <b>RENAME CATEGORY</b>\n\nSend the new category name.",
            category_id=int(data.split(":", 1)[1]),
        )
        return

    if data.startswith("category_button:"):
        await ask_text_input(
            query,
            "category_button",
            "🔘 <b>CATEGORY BUTTON</b>\n\nSend the new button text.",
            category_id=int(data.split(":", 1)[1]),
        )
        return

    if data.startswith("category_desc:"):
        await ask_text_input(
            query,
            "category_desc",
            "📝 <b>CATEGORY DESCRIPTION</b>\n\n"
            "Send the description shown under the currency step.\n"
            "Send <code>-</code> for no description.",
            category_id=int(data.split(":", 1)[1]),
        )
        return


    if data.startswith("toggle_category:"):
        category_id = int(data.split(":", 1)[1])
        conn = db()
        conn.execute(
            """
            UPDATE categories
            SET enabled = CASE WHEN enabled = 1 THEN 0 ELSE 1 END
            WHERE id = ?
            """,
            (category_id,),
        )
        conn.commit()
        conn.close()
        await show_edit_category(query, category_id)
        return

    if data.startswith("delete_category:"):
        category_id = int(data.split(":", 1)[1])
        category = get_category(category_id)

        if not category:
            await query.answer("Category not found.", show_alert=True)
            return

        product_count = len(get_products(category_id, False))
        if product_count:
            await query.answer(
                "Move or delete the products in this category first.",
                show_alert=True,
            )
            return

        conn = db()
        conn.execute("DELETE FROM categories WHERE id = ?", (category_id,))
        conn.commit()
        conn.close()
        await show_admin_categories(query)
        return

    if data.startswith("category_up:") or data.startswith("category_down:"):
        category_id = int(data.split(":", 1)[1])
        direction = -1 if data.startswith("category_up:") else 1

        conn = db()
        current = conn.execute(
            "SELECT * FROM categories WHERE id = ?",
            (category_id,),
        ).fetchone()

        if current:
            neighbor = conn.execute(
                """
                SELECT * FROM categories
                WHERE (sort_order < ? AND ? = -1)
                   OR (sort_order > ? AND ? = 1)
                ORDER BY sort_order
                LIMIT 1
                """,
                (
                    current["sort_order"],
                    direction,
                    current["sort_order"],
                    direction,
                ),
            ).fetchone()

            if neighbor:
                conn.execute(
                    "UPDATE categories SET sort_order = ? WHERE id = ?",
                    (neighbor["sort_order"], current["id"]),
                )
                conn.execute(
                    "UPDATE categories SET sort_order = ? WHERE id = ?",
                    (current["sort_order"], neighbor["id"]),
                )
                conn.commit()

        conn.close()
        await show_admin_categories(query)
        return

    # --------------------------------------------------------
    # PRODUCTS
    # --------------------------------------------------------

    if data == "admin_products":
        await show_admin_products(query)
        return

    if data.startswith("add_product:"):
        await start_add_product(
            query,
            int(data.split(":", 1)[1]),
        )
        return

    if data.startswith("edit_product:"):
        await show_edit_product(
            query,
            int(data.split(":", 1)[1]),
        )
        return

    if data.startswith("rename_product:"):
        await ask_text_input(
            query,
            "rename_product",
            "🔤 <b>EDIT PRODUCT NAME</b>\n\nSend the new name.",
            product_id=int(data.split(":", 1)[1]),
        )
        return

    if data.startswith("product_button:"):
        await ask_text_input(
            query,
            "product_button",
            "🔘 <b>PRODUCT BUTTON</b>\n\nSend the new button text.",
            product_id=int(data.split(":", 1)[1]),
        )
        return

    if data.startswith("amount_product:"):
        await ask_text_input(
            query,
            "change_product_amount",
            "🔢 <b>CHANGE AMOUNT</b>\n\n"
            "Send the new Robux amount.\n"
            "Example: <code>2500</code>",
            product_id=int(data.split(":", 1)[1]),
        )
        return

    if data.startswith("product_desc:"):
        await ask_text_input(
            query,
            "product_desc",
            "📝 <b>PRODUCT DESCRIPTION</b>\n\n"
            "Send a description, or <code>-</code> for none.",
            product_id=int(data.split(":", 1)[1]),
        )
        return

    if data.startswith("move_product:"):
        product_id = int(data.split(":", 1)[1])
        rows = []
        product = get_product(product_id)

        if not product:
            await query.answer("Product not found.", show_alert=True)
            return

        for category in get_categories(False):
            rows.append(
                [
                    InlineKeyboardButton(
                        f"🗂️ {category['name']}",
                        callback_data=f"move_to:{product_id}:{category['id']}",
                    )
                ]
            )
        rows.append(
            [
                InlineKeyboardButton(
                    "↩️ Product",
                    callback_data=f"edit_product:{product_id}",
                )
            ]
        )
        await edit_screen(
            query,
            user_id,
            "🗂️ <b>MOVE PRODUCT</b>\n\nChoose its new category.",
            InlineKeyboardMarkup(rows),
        )
        return

    if data.startswith("move_to:"):
        _, product_id, category_id = data.split(":")
        product_id = int(product_id)
        category_id = int(category_id)

        conn = db()
        conn.execute(
            "UPDATE products SET category_id = ? WHERE id = ?",
            (category_id, product_id),
        )
        conn.commit()
        conn.close()

        await show_edit_product(query, product_id)
        return


    if data.startswith("toggle_product:"):
        product_id = int(data.split(":", 1)[1])

        conn = db()
        conn.execute(
            """
            UPDATE products
            SET enabled = CASE WHEN enabled = 1 THEN 0 ELSE 1 END
            WHERE id = ?
            """,
            (product_id,),
        )
        conn.commit()
        conn.close()

        await show_edit_product(query, product_id)
        return

    if data.startswith("delete_product:"):
        product_id = int(data.split(":", 1)[1])

        conn = db()
        conn.execute(
            "DELETE FROM prices WHERE product_id = ?",
            (product_id,),
        )
        conn.execute(
            "DELETE FROM products WHERE id = ?",
            (product_id,),
        )
        conn.commit()
        conn.close()

        await show_admin_products(query)
        return

    if data.startswith("product_prices:"):
        await show_product_prices(
            query,
            int(data.split(":", 1)[1]),
        )
        return

    if data.startswith("product_up:") or data.startswith("product_down:"):
        product_id = int(data.split(":", 1)[1])
        direction = -1 if data.startswith("product_up:") else 1

        product = get_product(product_id)
        if product:
            conn = db()
            current = conn.execute(
                "SELECT * FROM products WHERE id = ?",
                (product_id,),
            ).fetchone()

            neighbor = conn.execute(
                """
                SELECT *
                FROM products
                WHERE category_id = ?
                  AND (
                      (sort_order < ? AND ? = -1)
                      OR
                      (sort_order > ? AND ? = 1)
                  )
                ORDER BY sort_order
                LIMIT 1
                """,
                (
                    current["category_id"],
                    current["sort_order"],
                    direction,
                    current["sort_order"],
                    direction,
                ),
            ).fetchone()

            if neighbor:
                conn.execute(
                    "UPDATE products SET sort_order = ? WHERE id = ?",
                    (neighbor["sort_order"], current["id"]),
                )
                conn.execute(
                    "UPDATE products SET sort_order = ? WHERE id = ?",
                    (current["sort_order"], neighbor["id"]),
                )
                conn.commit()
            conn.close()

        await show_edit_product(query, product_id)
        return

    # --------------------------------------------------------
    # CURRENCIES
    # --------------------------------------------------------

    if data == "admin_currencies":
        await show_admin_currencies(query)
        return

    if data == "add_currency":
        await start_add_currency(query)
        return

    if data.startswith("edit_currency:"):
        await show_edit_currency(
            query,
            int(data.split(":", 1)[1]),
        )
        return

    if data.startswith("rename_currency:"):
        await ask_text_input(
            query,
            "rename_currency",
            "🔤 <b>EDIT PAYMENT METHOD NAME</b>\n\nSend the new name.",
            currency_id=int(data.split(":", 1)[1]),
        )
        return

    if data.startswith("currency_button:"):
        await ask_text_input(
            query,
            "currency_button",
            "🔘 <b>PAYMENT BUTTON</b>\n\nSend the new button text.",
            currency_id=int(data.split(":", 1)[1]),
        )
        return

    if data.startswith("toggle_currency:"):
        currency_id = int(data.split(":", 1)[1])
        conn = db()
        conn.execute(
            """
            UPDATE currencies
            SET enabled = CASE WHEN enabled = 1 THEN 0 ELSE 1 END
            WHERE id = ?
            """,
            (currency_id,),
        )
        conn.commit()
        conn.close()
        await show_edit_currency(query, currency_id)
        return

    if data.startswith("delete_currency:"):
        currency_id = int(data.split(":", 1)[1])
        conn = db()
        conn.execute(
            "DELETE FROM prices WHERE currency_id = ?",
            (currency_id,),
        )
        conn.execute(
            "DELETE FROM custom_prices WHERE currency_id = ?",
            (currency_id,),
        )
        conn.execute(
            "DELETE FROM currencies WHERE id = ?",
            (currency_id,),
        )
        conn.commit()
        conn.close()
        await show_admin_currencies(query)
        return

    if data.startswith("prices_currency:"):
        await show_currency_prices(
            query,
            int(data.split(":", 1)[1]),
        )
        return

    if data.startswith("currency_up:") or data.startswith("currency_down:"):
        currency_id = int(data.split(":", 1)[1])
        direction = -1 if data.startswith("currency_up:") else 1

        conn = db()
        current = conn.execute(
            "SELECT * FROM currencies WHERE id = ?",
            (currency_id,),
        ).fetchone()

        if current:
            neighbor = conn.execute(
                """
                SELECT *
                FROM currencies
                WHERE (sort_order < ? AND ? = -1)
                   OR (sort_order > ? AND ? = 1)
                ORDER BY sort_order
                LIMIT 1
                """,
                (
                    current["sort_order"],
                    direction,
                    current["sort_order"],
                    direction,
                ),
            ).fetchone()

            if neighbor:
                conn.execute(
                    "UPDATE currencies SET sort_order = ? WHERE id = ?",
                    (neighbor["sort_order"], current["id"]),
                )
                conn.execute(
                    "UPDATE currencies SET sort_order = ? WHERE id = ?",
                    (current["sort_order"], neighbor["id"]),
                )
                conn.commit()

        conn.close()
        await show_admin_currencies(query)
        return

    # --------------------------------------------------------
    # PRICES
    # --------------------------------------------------------

    if data == "admin_prices":
        await show_admin_prices(query)
        return

    if data.startswith("set_price:"):
        _, currency_id, product_id = data.split(":")
        await ask_text_input(
            query,
            "set_price",
            "💰 <b>SET PRICE</b>\n\n"
            "Send the new price exactly as it should appear.\n\n"
            "Examples:\n"
            "<code>50</code>\n"
            "<code>50 ⭐</code>\n"
            "<code>100 Stars</code>\n"
            "<code>NA</code>",
            currency_id=int(currency_id),
            product_id=int(product_id),
        )
        return

    if data.startswith("set_custom:"):
        await ask_text_input(
            query,
            "set_custom_price",
            "✏️ <b>CUSTOM AMOUNT PRICE</b>\n\n"
            "Send the price shown for custom amounts.\n\n"
            "Example: <code>NA</code>",
            currency_id=int(data.split(":", 1)[1]),
        )
        return

    # --------------------------------------------------------
    # TEXTS / BUTTONS / SETTINGS
    # --------------------------------------------------------

    if data == "admin_texts":
        await show_texts(query)
        return

    if data.startswith("edit_text:"):
        key = data.split(":", 1)[1]
        current = get_text(key)

        placeholder_help = (
            "Available placeholders:\n"
            "<code>{shop_name}</code> <code>{admin_username}</code> "
            "<code>{order_number}</code> <code>{category}</code> "
            "<code>{currency}</code> <code>{product}</code> "
            "<code>{amount}</code> <code>{price}</code> "
            "<code>{roblox_username}</code> <code>{customer_name}</code> "
            "<code>{customer_username}</code> <code>{telegram_id}</code> "
            "<code>{created_at}</code> <code>{max_custom_amount}</code>"
        )

        await ask_text_input(
            query,
            "edit_text",
            "📝 <b>EDIT TEXT</b>\n\n"
            f"<b>{clean(TEXT_NAMES.get(key, key))}</b>\n\n"
            "<b>Current:</b>\n"
            f"<code>{clean(current)}</code>\n\n"
            "Send the new text. HTML is supported.\n\n"
            f"{placeholder_help}\n\n"
            "/cancel to stop.",
            key=key,
        )
        return

    if data == "admin_buttons":
        await show_buttons(query)
        return

    if data.startswith("edit_button:"):
        key = data.split(":", 1)[1]
        await ask_text_input(
            query,
            "edit_button",
            "🔘 <b>EDIT BUTTON</b>\n\n"
            f"Current: <b>{clean(get_button(key))}</b>\n\n"
            "Send the new button text.",
            key=key,
        )
        return

    if data == "admin_settings":
        await show_settings(query)
        return

    if data == "change_shop_name":
        await ask_text_input(
            query,
            "change_shop_name",
            "🏷️ <b>SHOP NAME</b>\n\n"
            "Send the new shop name.",
        )
        return

    if data == "change_admin_username":
        await ask_text_input(
            query,
            "change_admin_username",
            "👤 <b>SUPPORT USERNAME</b>\n\n"
            "Send the username customers should recognize.\n"
            "Example: <code>@yourusername</code>",
        )
        return

    if data == "change_recipient":
        await ask_text_input(
            query,
            "change_recipient",
            "🆔 <b>ORDER RECIPIENT CHAT ID</b>\n\n"
            "Send the numeric Telegram chat ID that should receive new orders.",
        )
        return

    if data == "change_max_amount":
        await ask_text_input(
            query,
            "change_max_amount",
            "🔢 <b>MAX CUSTOM AMOUNT</b>\n\n"
            "Send the maximum allowed custom Robux amount.\n"
            "Example: <code>1000000</code>",
        )
        return

    if data == "toggle_cleanup":
        current = get_setting("delete_user_messages", "1")
        set_setting("delete_user_messages", "0" if current == "1" else "1")
        await show_settings(query)
        return

    # --------------------------------------------------------
    # ADMINS
    # --------------------------------------------------------

    if data == "admin_admins":
        await show_admins(query)
        return

    if data == "add_admin":
        await ask_text_input(
            query,
            "add_admin_id",
            "➕ <b>ADD ADMIN</b>\n\n"
            "Send the admin's numeric Telegram ID.\n\n"
            "Example: <code>123456789</code>",
        )
        return

    if data.startswith("remove_admin_confirm:"):
        target_id = int(data.split(":", 1)[1])
        await edit_screen(
            query,
            user_id,
            f"⚠️ <b>REMOVE ADMIN?</b>\n\n"
            f"Telegram ID: <code>{target_id}</code>",
            InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "✅ Remove",
                            callback_data=f"remove_admin:{target_id}",
                        ),
                        InlineKeyboardButton(
                            "↩️ Keep",
                            callback_data="admin_admins",
                        ),
                    ]
                ]
            ),
        )
        return

    if data.startswith("remove_admin:"):
        target_id = int(data.split(":", 1)[1])

        if BOOTSTRAP_ADMIN_CHAT_ID:
            try:
                if target_id == int(BOOTSTRAP_ADMIN_CHAT_ID):
                    await query.answer(
                        "The bootstrap admin cannot be removed.",
                        show_alert=True,
                    )
                    return
            except ValueError:
                pass

        remove_admin(target_id)
        await show_admins(query)
        return

    # --------------------------------------------------------
    # ORDERS
    # --------------------------------------------------------

    if data == "admin_orders":
        orders = get_orders(25)

        if not orders:
            await edit_screen(
                query,
                user_id,
                "📋 <b>ORDERS</b>\n\nNo orders yet.",
                InlineKeyboardMarkup(back_to_admin()),
            )
            return

        rows = []
        for order in orders:
            status_icon = (
                "⏳"
                if order["status"] == "awaiting_confirmation"
                else "✅"
                if order["status"] == "confirmed"
                else "❌"
            )
            rows.append(
                [
                    InlineKeyboardButton(
                        f"{status_icon} {order['order_number']} · {order['roblox_username']}",
                        callback_data=f"order:{order['order_number']}",
                    )
                ]
            )

        rows.extend(back_to_admin())

        await edit_screen(
            query,
            user_id,
            "📋 <b>RECENT ORDERS</b>\n\n"
            "Tap an order to view its full details.",
            InlineKeyboardMarkup(rows),
        )
        return

    if data.startswith("order:"):
        order_number = data.split(":", 1)[1]
        order = get_order(order_number)

        if not order:
            await query.answer("Order not found.", show_alert=True)
            return

        await edit_screen(
            query,
            user_id,
            "🔔 <b>ORDER DETAILS</b>\n\n"
            f"🔐 Code: <code>{clean(order['order_number'])}</code>\n"
            f"🗂️ Category: <b>{clean(order['category'])}</b>\n"
            f"💱 Payment: <b>{clean(order['currency'])}</b>\n"
            f"📦 Product: <b>{clean(order['product'])}</b>\n"
            f"💰 Amount: <b>{order['robux_amount']:,} Robux</b>\n"
            f"💵 Price: <b>{clean(order['price'])}</b>\n"
            f"👤 Roblox: <code>{clean(order['roblox_username'])}</code>\n"
            f"👤 Customer: <b>{clean(order['telegram_name'])}</b>\n"
            f"📱 Telegram: <b>{clean('@' + order['telegram_username']) if order['telegram_username'] else 'No username'}</b>\n"
            f"🆔 Chat ID: <code>{order['telegram_id']}</code>\n"
            f"📅 Created: <b>{clean(order['created_at'])}</b>\n"
            f"⏳ Status: <b>{clean(order['status'])}</b>",
            InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "✅ Confirmed",
                            callback_data=f"order_status:confirmed:{order_number}",
                        ),
                        InlineKeyboardButton(
                            "❌ Rejected",
                            callback_data=f"order_status:rejected:{order_number}",
                        ),
                    ],
                    [
                        InlineKeyboardButton(
                            "↩️ Orders",
                            callback_data="admin_orders",
                        )
                    ],
                ]
            ),
        )
        return

    if data.startswith("order_status:"):
        _, status, order_number = data.split(":", 2)
        order = get_order(order_number)
        if not order:
            await query.answer("Order not found.", show_alert=True)
            return

        set_order_status(order_number, status)

        # Notify customer using the same secret code.
        notification = {
            "confirmed": (
                "✅ <b>ORDER CONFIRMED</b>\n\n"
                "🔐 Code: <code>{order_number}</code>\n\n"
                "Your order has been confirmed."
            ),
            "rejected": (
                "❌ <b>ORDER REJECTED</b>\n\n"
                "🔐 Code: <code>{order_number}</code>\n\n"
                "Your order was rejected by the team. Please contact support if you need help."
            ),
        }[status].format(order_number=clean(order_number))

        try:
            await context.bot.send_message(
                chat_id=order["telegram_id"],
                text=notification,
                parse_mode="HTML",
            )
        except TelegramError as error:
            logger.warning(
                "Could not notify customer for order %s: %s",
                order_number,
                error,
            )

        await edit_screen(
            query,
            user_id,
            "✅ <b>ORDER UPDATED</b>\n\n"
            f"Order <code>{clean(order_number)}</code> is now <b>{clean(status)}</b>.",
            InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "📋 Orders",
                            callback_data="admin_orders",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "⚙️ Admin Panel",
                            callback_data="admin",
                        )
                    ],
                ]
            ),
        )
        return


# ============================================================
# ADMIN TEXT INPUT
# ============================================================

async def handle_admin_input(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    session,
    text,
):
    user = update.effective_user
    user_id = user.id
    action = session.get("action")

    if action == "add_category_name":
        name = text.strip()
        if not name:
            return

        try:
            conn = db()
            max_order = conn.execute(
                "SELECT COALESCE(MAX(sort_order), 0) AS n FROM categories"
            ).fetchone()["n"]

            cursor = conn.execute(
                """
                INSERT INTO categories
                (name, button_text, description, enabled, sort_order)
                VALUES (?, ?, '', 1, ?)
                """,
                (name, name, max_order + 1),
            )
            category_id = cursor.lastrowid
            conn.commit()
            conn.close()
        except sqlite3.IntegrityError:
            await remember_screen(
                context.bot,
                update.effective_chat.id,
                user_id,
                "⚠️ <b>CATEGORY ALREADY EXISTS</b>\n\n"
                "Choose another name.",
            )
            return

        sessions.pop(user_id, None)
        await remember_screen(
            context.bot,
            update.effective_chat.id,
            user_id,
            "✅ <b>CATEGORY CREATED</b>\n\n"
            f"🗂️ {clean(name)}",
            InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "🗂️ Categories",
                            callback_data="admin_categories",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "⚙️ Admin Panel",
                            callback_data="admin",
                        )
                    ],
                ]
            ),
        )
        return

    if action == "rename_category":
        category_id = session["category_id"]
        try:
            conn = db()
            conn.execute(
                "UPDATE categories SET name = ? WHERE id = ?",
                (text, category_id),
            )
            conn.commit()
            conn.close()
        except sqlite3.IntegrityError:
            await remember_screen(
                context.bot,
                update.effective_chat.id,
                user_id,
                "⚠️ Another category already uses that name.",
            )
            return

        sessions.pop(user_id, None)
        await remember_screen(
            context.bot,
            update.effective_chat.id,
            user_id,
            "✅ <b>CATEGORY UPDATED</b>",
            InlineKeyboardMarkup(
                [[
                    InlineKeyboardButton(
                        "🗂️ Categories",
                        callback_data="admin_categories",
                    )
                ]]
            ),
        )
        return

    if action == "category_button":
        conn = db()
        conn.execute(
            "UPDATE categories SET button_text = ? WHERE id = ?",
            (text, session["category_id"]),
        )
        conn.commit()
        conn.close()

        sessions.pop(user_id, None)
        await remember_screen(
            context.bot,
            update.effective_chat.id,
            user_id,
            "✅ <b>CATEGORY BUTTON UPDATED</b>",
            InlineKeyboardMarkup(
                [[
                    InlineKeyboardButton(
                        "🗂️ Categories",
                        callback_data="admin_categories",
                    )
                ]]
            ),
        )
        return

    if action == "category_desc":
        description = "" if text == "-" else text
        conn = db()
        conn.execute(
            "UPDATE categories SET description = ? WHERE id = ?",
            (description, session["category_id"]),
        )
        conn.commit()
        conn.close()

        sessions.pop(user_id, None)
        await remember_screen(
            context.bot,
            update.effective_chat.id,
            user_id,
            "✅ <b>CATEGORY DESCRIPTION UPDATED</b>",
            InlineKeyboardMarkup(
                [[
                    InlineKeyboardButton(
                        "🗂️ Categories",
                        callback_data="admin_categories",
                    )
                ]]
            ),
        )
        return

    if action == "add_product_name":
        session["product_name"] = text
        session["action"] = "add_product_amount"

        await remember_screen(
            context.bot,
            update.effective_chat.id,
            user_id,
            "🔢 <b>PRODUCT AMOUNT</b>\n\n"
            "Send the amount of Robux.\n"
            "Example: <code>2000</code>",
        )
        return

    if action == "add_product_amount":
        cleaned = text.replace(",", "").replace(" ", "")

        if not cleaned.isdigit():
            await remember_screen(
                context.bot,
                update.effective_chat.id,
                user_id,
                "⚠️ Please enter numbers only.\n\nExample: <code>2000</code>",
            )
            return

        amount = int(cleaned)
        if amount <= 0:
            await remember_screen(
                context.bot,
                update.effective_chat.id,
                user_id,
                "⚠️ Amount must be greater than 0.",
            )
            return

        category_id = session["category_id"]

        conn = db()
        max_order = conn.execute(
            """
            SELECT COALESCE(MAX(sort_order), 0) AS n
            FROM products
            WHERE category_id = ?
            """,
            (category_id,),
        ).fetchone()["n"]

        cursor = conn.execute(
            """
            INSERT INTO products
            (name, button_text, amount, category_id, description, enabled, sort_order)
            VALUES (?, ?, ?, ?, '', 1, ?)
            """,
            (
                session["product_name"],
                session["product_name"],
                amount,
                category_id,
                max_order + 1,
            ),
        )
        product_id = cursor.lastrowid

        currencies = conn.execute("SELECT id FROM currencies").fetchall()
        for currency in currencies:
            conn.execute(
                """
                INSERT OR IGNORE INTO prices(currency_id, product_id, price)
                VALUES (?, ?, 'NA')
                """,
                (currency["id"], product_id),
            )

        conn.commit()
        conn.close()
        sessions.pop(user_id, None)

        await remember_screen(
            context.bot,
            update.effective_chat.id,
            user_id,
            "✅ <b>PRODUCT CREATED</b>\n\n"
            f"📦 {clean(session['product_name'])}\n"
            f"🔢 {amount:,} Robux",
            InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "📦 Products",
                            callback_data=f"category_products:{category_id}",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "⚙️ Admin Panel",
                            callback_data="admin",
                        )
                    ],
                ]
            ),
        )
        return

    if action == "rename_product":
        conn = db()
        conn.execute(
            "UPDATE products SET name = ? WHERE id = ?",
            (text, session["product_id"]),
        )
        conn.commit()
        conn.close()

        sessions.pop(user_id, None)
        await remember_screen(
            context.bot,
            update.effective_chat.id,
            user_id,
            "✅ <b>PRODUCT NAME UPDATED</b>",
            InlineKeyboardMarkup(
                [[
                    InlineKeyboardButton(
                        "📦 Products",
                        callback_data="admin_products",
                    )
                ]]
            ),
        )
        return

    if action == "product_button":
        conn = db()
        conn.execute(
            "UPDATE products SET button_text = ? WHERE id = ?",
            (text, session["product_id"]),
        )
        conn.commit()
        conn.close()

        sessions.pop(user_id, None)
        await remember_screen(
            context.bot,
            update.effective_chat.id,
            user_id,
            "✅ <b>PRODUCT BUTTON UPDATED</b>",
            InlineKeyboardMarkup(
                [[
                    InlineKeyboardButton(
                        "📦 Products",
                        callback_data="admin_products",
                    )
                ]]
            ),
        )
        return

    if action == "change_product_amount":
        cleaned = text.replace(",", "").replace(" ", "")
        if not cleaned.isdigit() or int(cleaned) <= 0:
            await remember_screen(
                context.bot,
                update.effective_chat.id,
                user_id,
                "⚠️ Enter a positive whole number.",
            )
            return

        amount = int(cleaned)
        conn = db()
        conn.execute(
            "UPDATE products SET amount = ? WHERE id = ?",
            (amount, session["product_id"]),
        )
        conn.commit()
        conn.close()

        sessions.pop(user_id, None)
        await remember_screen(
            context.bot,
            update.effective_chat.id,
            user_id,
            f"✅ <b>AMOUNT UPDATED</b>\n\n"
            f"New amount: <b>{amount:,} Robux</b>",
            InlineKeyboardMarkup(
                [[
                    InlineKeyboardButton(
                        "📦 Products",
                        callback_data="admin_products",
                    )
                ]]
            ),
        )
        return

    if action == "product_desc":
        description = "" if text == "-" else text
        conn = db()
        conn.execute(
            "UPDATE products SET description = ? WHERE id = ?",
            (description, session["product_id"]),
        )
        conn.commit()
        conn.close()

        sessions.pop(user_id, None)
        await remember_screen(
            context.bot,
            update.effective_chat.id,
            user_id,
            "✅ <b>PRODUCT DESCRIPTION UPDATED</b>",
            InlineKeyboardMarkup(
                [[
                    InlineKeyboardButton(
                        "📦 Products",
                        callback_data="admin_products",
                    )
                ]]
            ),
        )
        return

    if action == "add_currency_name":
        name = text.strip()
        if not name:
            return

        try:
            conn = db()
            max_order = conn.execute(
                "SELECT COALESCE(MAX(sort_order), 0) AS n FROM currencies"
            ).fetchone()["n"]

            cursor = conn.execute(
                """
                INSERT INTO currencies
                (name, button_text, enabled, sort_order)
                VALUES (?, ?, 1, ?)
                """,
                (name, name, max_order + 1),
            )
            currency_id = cursor.lastrowid

            products = conn.execute(
                "SELECT id FROM products"
            ).fetchall()

            for product in products:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO prices(currency_id, product_id, price)
                    VALUES (?, ?, 'NA')
                    """,
                    (currency_id, product["id"]),
                )

            conn.execute(
                """
                INSERT OR IGNORE INTO custom_prices(currency_id, price)
                VALUES (?, 'NA')
                """,
                (currency_id,),
            )

            conn.commit()
            conn.close()
        except sqlite3.IntegrityError:
            await remember_screen(
                context.bot,
                update.effective_chat.id,
                user_id,
                "⚠️ That payment method already exists.",
            )
            return

        sessions.pop(user_id, None)
        await remember_screen(
            context.bot,
            update.effective_chat.id,
            user_id,
            "✅ <b>PAYMENT METHOD CREATED</b>\n\n"
            f"💱 {clean(name)}",
            InlineKeyboardMarkup(
                [[
                    InlineKeyboardButton(
                        "💱 Payment Methods",
                        callback_data="admin_currencies",
                    )
                ]]
            ),
        )
        return

    if action == "rename_currency":
        currency_id = session["currency_id"]
        try:
            conn = db()
            conn.execute(
                "UPDATE currencies SET name = ? WHERE id = ?",
                (text, currency_id),
            )
            conn.commit()
            conn.close()
        except sqlite3.IntegrityError:
            await remember_screen(
                context.bot,
                update.effective_chat.id,
                user_id,
                "⚠️ Another payment method already uses that name.",
            )
            return

        sessions.pop(user_id, None)
        await remember_screen(
            context.bot,
            update.effective_chat.id,
            user_id,
            "✅ <b>PAYMENT METHOD UPDATED</b>",
            InlineKeyboardMarkup(
                [[
                    InlineKeyboardButton(
                        "💱 Payment Methods",
                        callback_data="admin_currencies",
                    )
                ]]
            ),
        )
        return

    if action == "currency_button":
        conn = db()
        conn.execute(
            "UPDATE currencies SET button_text = ? WHERE id = ?",
            (text, session["currency_id"]),
        )
        conn.commit()
        conn.close()

        sessions.pop(user_id, None)
        await remember_screen(
            context.bot,
            update.effective_chat.id,
            user_id,
            "✅ <b>PAYMENT BUTTON UPDATED</b>",
            InlineKeyboardMarkup(
                [[
                    InlineKeyboardButton(
                        "💱 Payment Methods",
                        callback_data="admin_currencies",
                    )
                ]]
            ),
        )
        return

    if action == "set_price":
        price = text.strip() or "NA"
        set_price(
            session["currency_id"],
            session["product_id"],
            price,
        )

        sessions.pop(user_id, None)
        await remember_screen(
            context.bot,
            update.effective_chat.id,
            user_id,
            f"✅ <b>PRICE UPDATED</b>\n\n"
            f"New price: <b>{clean(price)}</b>",
            InlineKeyboardMarkup(
                [[
                    InlineKeyboardButton(
                        "💰 Prices",
                        callback_data="admin_prices",
                    )
                ]]
            ),
        )
        return

    if action == "set_custom_price":
        price = text.strip() or "NA"
        set_custom_price(
            session["currency_id"],
            price,
        )

        sessions.pop(user_id, None)
        await remember_screen(
            context.bot,
            update.effective_chat.id,
            user_id,
            f"✅ <b>CUSTOM PRICE UPDATED</b>\n\n"
            f"New price: <b>{clean(price)}</b>",
            InlineKeyboardMarkup(
                [[
                    InlineKeyboardButton(
                        "💰 Prices",
                        callback_data="admin_prices",
                    )
                ]]
            ),
        )
        return

    if action == "edit_text":
        set_text(session["key"], text)
        key = session["key"]
        sessions.pop(user_id, None)

        await remember_screen(
            context.bot,
            update.effective_chat.id,
            user_id,
            f"✅ <b>TEXT UPDATED</b>\n\n"
            f"Section: <b>{clean(TEXT_NAMES.get(key, key))}</b>",
            InlineKeyboardMarkup(
                [[
                    InlineKeyboardButton(
                        "📝 Texts / Steps",
                        callback_data="admin_texts",
                    )
                ]]
            ),
        )
        return

    if action == "edit_button":
        set_button(session["key"], text)
        key = session["key"]
        sessions.pop(user_id, None)

        await remember_screen(
            context.bot,
            update.effective_chat.id,
            user_id,
            f"✅ <b>BUTTON UPDATED</b>\n\n"
            f"Button: <b>{clean(BUTTON_NAMES.get(key, key))}</b>",
            InlineKeyboardMarkup(
                [[
                    InlineKeyboardButton(
                        "🔘 Buttons",
                        callback_data="admin_buttons",
                    )
                ]]
            ),
        )
        return

    if action == "change_shop_name":
        set_setting("shop_name", text)
        sessions.pop(user_id, None)

        await remember_screen(
            context.bot,
            update.effective_chat.id,
            user_id,
            "✅ <b>SHOP NAME UPDATED</b>",
            InlineKeyboardMarkup(
                [[
                    InlineKeyboardButton(
                        "🏪 Settings",
                        callback_data="admin_settings",
                    )
                ]]
            ),
        )
        return

    if action == "change_admin_username":
        username = text if text.startswith("@") else "@" + text
        set_setting("admin_username", username)
        sessions.pop(user_id, None)

        await remember_screen(
            context.bot,
            update.effective_chat.id,
            user_id,
            "✅ <b>SUPPORT USERNAME UPDATED</b>",
            InlineKeyboardMarkup(
                [[
                    InlineKeyboardButton(
                        "🏪 Settings",
                        callback_data="admin_settings",
                    )
                ]]
            ),
        )
        return

    if action == "change_recipient":
        cleaned = text.strip()
        if not cleaned.lstrip("-").isdigit():
            await remember_screen(
                context.bot,
                update.effective_chat.id,
                user_id,
                "⚠️ Chat ID must be numeric.",
            )
            return

        set_setting("order_recipient_chat_id", cleaned)
        sessions.pop(user_id, None)

        await remember_screen(
            context.bot,
            update.effective_chat.id,
            user_id,
            "✅ <b>ORDER RECIPIENT UPDATED</b>",
            InlineKeyboardMarkup(
                [[
                    InlineKeyboardButton(
                        "🏪 Settings",
                        callback_data="admin_settings",
                    )
                ]]
            ),
        )
        return

    if action == "change_max_amount":
        cleaned = text.replace(",", "").replace(" ", "")
        if not cleaned.isdigit() or int(cleaned) <= 0:
            await remember_screen(
                context.bot,
                update.effective_chat.id,
                user_id,
                "⚠️ Please enter a positive whole number.",
            )
            return

        set_setting("max_custom_amount", str(int(cleaned)))
        sessions.pop(user_id, None)

        await remember_screen(
            context.bot,
            update.effective_chat.id,
            user_id,
            "✅ <b>CUSTOM AMOUNT LIMIT UPDATED</b>",
            InlineKeyboardMarkup(
                [[
                    InlineKeyboardButton(
                        "🏪 Settings",
                        callback_data="admin_settings",
                    )
                ]]
            ),
        )
        return

    if action == "add_admin_id":
        cleaned = text.strip()
        if not cleaned.isdigit():
            await remember_screen(
                context.bot,
                update.effective_chat.id,
                user_id,
                "⚠️ Telegram ID must contain numbers only.",
            )
            return

        target_id = int(cleaned)
        add_admin(target_id)

        sessions.pop(user_id, None)
        await remember_screen(
            context.bot,
            update.effective_chat.id,
            user_id,
            "✅ <b>ADMIN ADDED</b>\n\n"
            f"🆔 <code>{target_id}</code>",
            InlineKeyboardMarkup(
                [[
                    InlineKeyboardButton(
                        "👮 Admins",
                        callback_data="admin_admins",
                    )
                ]]
            ),
        )
        return


# ============================================================
# CUSTOMER TEXT INPUT / ORDER CREATION
# ============================================================

def generate_order_number():
    # 10 random uppercase base32-ish characters, with no predictable
    # sequence. Example: SRP-X7KQ9M2P4A
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"

    for _ in range(20):
        token = "".join(secrets.choice(alphabet) for _ in range(10))
        candidate = f"SRP-{token}"

        if not get_order(candidate):
            return candidate

    raise RuntimeError("Could not generate a unique order number.")


async def create_order(update, context, session, username):
    user = update.effective_user
    user_id = user.id

    currency = get_currency(session.get("currency_id"))
    category = get_category(session.get("category_id"))

    if not currency or not category:
        return None

    order_number = generate_order_number()
    created_at = datetime.now().strftime("%d.%m.%Y %H:%M:%S")

    telegram_username = user.username or ""
    telegram_name = user.full_name or ""

    conn = db()
    conn.execute(
        """
        INSERT INTO orders
        (
            order_number,
            telegram_id,
            telegram_username,
            telegram_name,
            category,
            currency,
            product,
            robux_amount,
            price,
            roblox_username,
            status,
            created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            order_number,
            user_id,
            telegram_username,
            telegram_name,
            category["name"],
            currency["name"],
            session["product"],
            int(session["amount"]),
            session["price"],
            username,
            "awaiting_confirmation",
            created_at,
        ),
    )
    conn.commit()
    conn.close()

    recipient = get_setting("order_recipient_chat_id", "").strip()

    if not recipient and BOOTSTRAP_ADMIN_CHAT_ID:
        recipient = BOOTSTRAP_ADMIN_CHAT_ID

    if recipient:
        try:
            admin_text = render_text(
                "admin_new_order",
                order_number=order_number,
                category=category["name"],
                currency=currency["name"],
                product=session["product"],
                amount=int(session["amount"]),
                price=session["price"],
                roblox_username=username,
                created_at=created_at,
                customer_name=telegram_name,
                customer_username=(
                    f"@{telegram_username}"
                    if telegram_username
                    else "No username"
                ),
                telegram_id=user_id,
            )

            await context.bot.send_message(
                chat_id=int(recipient),
                text=admin_text,
                parse_mode="HTML",
            )

            return {
                "order_number": order_number,
                "admin_notified": True,
                "admin_error": "",
            }

        except (TelegramError, ValueError) as error:
            logger.error(
                "Admin notification failed for order %s: %s",
                order_number,
                error,
            )
            return {
                "order_number": order_number,
                "admin_notified": False,
                "admin_error": str(error),
            }

    return {
        "order_number": order_number,
        "admin_notified": False,
        "admin_error": "No order recipient chat ID is configured.",
    }


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    user = update.effective_user
    user_id = user.id
    text = update.message.text.strip()

    session = sessions.get(user_id)

    # Remove the user's text message when permitted.
    await clear_user_message(update)

    if text.lower() == "/cancel":
        sessions.pop(user_id, None)

        await remember_screen(
            context.bot,
            update.effective_chat.id,
            user_id,
            render_text("cancelled"),
            main_keyboard(),
        )
        return

    # --------------------------------------------------------
    # ADMIN INPUT
    # --------------------------------------------------------

    if is_admin(user) and session and session.get("action"):
        await handle_admin_input(
            update,
            context,
            session,
            text,
        )
        return

    # --------------------------------------------------------
    # CUSTOMER INPUT
    # --------------------------------------------------------

    if not session:
        await remember_screen(
            context.bot,
            update.effective_chat.id,
            user_id,
            render_text("no_session"),
            main_keyboard(),
        )
        return

    if session.get("waiting") == "custom_amount":
        cleaned = text.replace(",", "").replace(" ", "")

        if not cleaned.isdigit():
            await remember_screen(
                context.bot,
                update.effective_chat.id,
                user_id,
                render_text(
                    "invalid_amount",
                    max_custom_amount=get_setting(
                        "max_custom_amount",
                        "1000000",
                    ),
                ),
                cancel_keyboard(),
            )
            return

        amount = int(cleaned)
        max_amount = int(
            get_setting("max_custom_amount", "1000000")
        )

        if amount <= 0 or amount > max_amount:
            await remember_screen(
                context.bot,
                update.effective_chat.id,
                user_id,
                render_text(
                    "invalid_amount",
                    max_custom_amount=max_amount,
                ),
                cancel_keyboard(),
            )
            return

        session["amount"] = amount
        session["product"] = "Custom Amount"
        session["price"] = get_custom_price(
            session["currency_id"]
        )
        session["waiting"] = "username"

        await remember_screen(
            context.bot,
            update.effective_chat.id,
            user_id,
            render_text("username"),
            cancel_keyboard(),
        )
        return

    if session.get("waiting") == "username":
        username = text.lstrip("@")

        if (
            len(username) < 3
            or len(username) > 20
            or not all(
                character.isalnum() or character == "_"
                for character in username
            )
        ):
            await remember_screen(
                context.bot,
                update.effective_chat.id,
                user_id,
                render_text("invalid_username"),
                cancel_keyboard(),
            )
            return

        try:
            order_result = await create_order(
                update,
                context,
                session,
                username,
            )
        except Exception:
            logger.exception("Failed to create order")
            order_result = None

        if not order_result:
            await remember_screen(
                context.bot,
                update.effective_chat.id,
                user_id,
                render_text("order_error"),
                main_keyboard(),
            )
            return

        sessions.pop(user_id, None)

        # If admin notification failed, the order still exists and is visible
        # inside the admin Orders panel after the recipient is fixed.
        if not order_result["admin_notified"]:
            logger.warning(
                "Order %s created without a successful admin notification: %s",
                order_result["order_number"],
                order_result["admin_error"],
            )

        await remember_screen(
            context.bot,
            update.effective_chat.id,
            user_id,
            render_text(
                "confirmation",
                order_number=order_result["order_number"],
            ),
            InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            get_button("new_order", "💱 New Order"),
                            callback_data="exchange",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            get_button("home", "🏠 Main Menu"),
                            callback_data="home",
                        )
                    ],
                ]
            ),
        )
        return




from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

_LEGACY_INIT_DB = init_db
_LEGACY_CALLBACK_HANDLER = callback_handler
_LEGACY_TEXT_HANDLER = text_handler

MODE_CLASSIC = "classic"
MODE_P2P = "p2p"
MODE_INHERIT = "inherit"
P2P_OPEN = "open"
P2P_ACTIVE = P2P_OPEN
P2P_RESERVED = "reserved"
P2P_COMPLETED = "completed"
P2P_SOLD = P2P_COMPLETED
P2P_REJECTED = "rejected"
P2P_CANCELLED = "cancelled"


def init_db():
    _LEGACY_INIT_DB()
    conn = db()
    ensure_column(conn, "categories", "mode", "TEXT NOT NULL DEFAULT 'classic'")
    ensure_column(conn, "products", "subcategory_id", "INTEGER")
    ensure_column(conn, "products", "mode", "TEXT NOT NULL DEFAULT 'inherit'")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS subcategories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            button_text TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            mode TEXT NOT NULL DEFAULT 'inherit',
            enabled INTEGER NOT NULL DEFAULT 1,
            sort_order INTEGER NOT NULL DEFAULT 0,
            UNIQUE(category_id, name)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS p2p_fee_tiers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            min_quantity INTEGER NOT NULL,
            max_quantity INTEGER,
            fee_type TEXT NOT NULL DEFAULT 'percent',
            fee_value TEXT NOT NULL DEFAULT '0',
            enabled INTEGER NOT NULL DEFAULT 1,
            sort_order INTEGER NOT NULL DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS p2p_offers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            offer_number TEXT UNIQUE NOT NULL,
            seller_id INTEGER NOT NULL,
            seller_username TEXT NOT NULL DEFAULT '',
            seller_name TEXT NOT NULL DEFAULT '',
            category_id INTEGER,
            subcategory_id INTEGER,
            product_id INTEGER,
            category_name TEXT NOT NULL DEFAULT '',
            subcategory_name TEXT NOT NULL DEFAULT '',
            product_name TEXT NOT NULL DEFAULT '',
            source_amount INTEGER NOT NULL,
            target_currency_id INTEGER,
            target_name TEXT NOT NULL DEFAULT '',
            target_amount TEXT NOT NULL DEFAULT '0',
            fee_type TEXT NOT NULL DEFAULT 'percent',
            fee_value TEXT NOT NULL DEFAULT '0',
            fee_amount TEXT NOT NULL DEFAULT '0',
            buyer_total TEXT NOT NULL DEFAULT '0',
            status TEXT NOT NULL DEFAULT 'open',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS p2p_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_number TEXT UNIQUE NOT NULL,
            offer_id INTEGER NOT NULL,
            offer_number TEXT NOT NULL,
            seller_id INTEGER NOT NULL,
            buyer_id INTEGER NOT NULL,
            source_amount INTEGER NOT NULL,
            target_name TEXT NOT NULL DEFAULT '',
            target_amount TEXT NOT NULL DEFAULT '0',
            fee_amount TEXT NOT NULL DEFAULT '0',
            buyer_total TEXT NOT NULL DEFAULT '0',
            status TEXT NOT NULL DEFAULT 'awaiting_confirmation',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_subcat_cat ON subcategories(category_id, sort_order, id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_p2p_offer_status ON p2p_offers(status, id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_p2p_offer_seller ON p2p_offers(seller_id, status, id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_p2p_trade_status ON p2p_trades(status, id)")

    # Keep the current shop identity and remove the old payment-method wording.
    row = conn.execute("SELECT value FROM settings WHERE key='shop_name'").fetchone()
    if not row or row["value"] in {"R$ EXCHANGE", "SVXchange", "SVExchange"}:
        conn.execute("INSERT INTO settings(key,value) VALUES('shop_name','SRPExchange') ON CONFLICT(key) DO UPDATE SET value='SRPExchange'")
    old_currency = "💱 <b>SELECT PAYMENT METHOD</b>\n\nChoose how you want to pay for your exchange."
    row = conn.execute("SELECT value FROM texts WHERE key='currency'").fetchone()
    if row and row["value"] == old_currency:
        conn.execute("UPDATE texts SET value=? WHERE key='currency'", ("🔄 <b>WHAT ARE YOU EXCHANGING FOR?</b>\n\nChoose what you want in return.",))
    old_how = "ℹ️ <b>HOW IT WORKS</b>\n\n1️⃣ Choose a category.\n2️⃣ Choose a payment method.\n3️⃣ Choose an amount or enter a custom amount.\n4️⃣ Send your Roblox username.\n5️⃣ Your order is sent to the team and awaits confirmation."
    row = conn.execute("SELECT value FROM texts WHERE key='how'").fetchone()
    if row and row["value"] == old_how:
        conn.execute("UPDATE texts SET value=? WHERE key='how'", ("ℹ️ <b>HOW IT WORKS</b>\n\n1️⃣ Choose what you are exchanging.\n2️⃣ Choose what you are exchanging for.\n3️⃣ Choose an amount.\n4️⃣ Send your Roblox username.\n5️⃣ Your exchange is sent and awaits confirmation.\n\n🤝 P2P offers use a separate market.",))
    texts = {
        "p2p_market": "🤝 <b>P2P MARKET</b>\n\nBuy an existing offer or publish your own.\n\nSeller identities are hidden from buyers.",
        "p2p_offer_amount": "💰 <b>OFFER AMOUNT</b>\n\nSend the amount you are offering.\nExample: <code>2500</code>",
        "p2p_offer_target": "🔄 <b>WHAT DO YOU WANT IN RETURN?</b>\n\nChoose what you want from the buyer.",
        "p2p_offer_target_amount": "💵 <b>REQUESTED AMOUNT</b>\n\nSend how much you want in return.\nExample: <code>250</code>",
        "p2p_preview": "🔎 <b>CHECK OFFER</b>\n\nOffering: <b>{source_amount:,} {product}</b>\nWants: <b>{target_amount} {target_name}</b>\nFee: <b>{fee_amount} {target_name}</b>\nBuyer total: <b>{buyer_total} {target_name}</b>\n\nThe fee is added to the buyer total.",
        "p2p_published": "✅ <b>OFFER PUBLISHED</b>\n\nOffer: <code>{offer_number}</code>\n\nYour offer is live. Your identity is hidden from buyers.",
        "p2p_empty": "🤝 <b>P2P MARKET</b>\n\nThere are no active offers right now.",
        "p2p_details": "🤝 <b>P2P OFFER</b>\n\nOffer: <code>{offer_number}</code>\nOffering: <b>{source_amount:,} {product}</b>\nWants: <b>{target_amount} {target_name}</b>\nFee: <b>{fee_amount} {target_name}</b>\nBuyer total: <b>{buyer_total} {target_name}</b>\n\nSeller identity is hidden.",
        "p2p_buy_confirm": "🛒 <b>BUY THIS OFFER?</b>\n\nOffering: <b>{source_amount:,} {product}</b>\nYou exchange: <b>{buyer_total} {target_name}</b>\n\nSeller identity stays hidden.",
        "p2p_bought": "✅ <b>P2P ORDER SENT</b>\n\nTrade: <code>{trade_number}</code>\n\nYour purchase request was sent and is awaiting confirmation.",
        "p2p_seller_notice": "🔔 <b>P2P OFFER SELECTED</b>\n\nOffer: <code>{offer_number}</code>\nTrade: <code>{trade_number}</code>\n\nA buyer selected your offer. Please wait for confirmation.",
        "p2p_my_offers": "📋 <b>MY P2P OFFERS</b>\n\nYour offers are shown below.",
        "p2p_cancelled": "✅ <b>OFFER CANCELLED</b>\n\nThe offer is no longer available.",
    }
    for key, value in texts.items():
        conn.execute("INSERT OR IGNORE INTO texts(key,value) VALUES(?,?)", (key,value))
    buttons = {
        "p2p_market":"🤝 P2P Market",
        "p2p_browse":"🔎 Browse Offers",
        "p2p_publish":"➕ Publish Offer",
        "p2p_my_offers":"📋 My Offers",
        "p2p_buy":"✅ Buy Offer",
        "p2p_confirm":"✅ Publish",
    }
    for key, value in buttons.items():
        conn.execute("INSERT OR IGNORE INTO buttons(key,value) VALUES(?,?)", (key,value))
    if conn.execute("SELECT COUNT(*) AS n FROM p2p_fee_tiers").fetchone()["n"] == 0:
        conn.executemany("INSERT INTO p2p_fee_tiers(min_quantity,max_quantity,fee_type,fee_value,enabled,sort_order) VALUES(?,?,?,?,1,?)", [(1,999,"percent","5",1),(1000,9999,"percent","4",2),(10000,None,"percent","3",3)])
    conn.commit(); conn.close()


def get_subcategories(category_id, enabled_only=False):
    conn=db()
    sql = "SELECT * FROM subcategories WHERE category_id=? AND enabled=1 ORDER BY sort_order,id" if enabled_only else "SELECT * FROM subcategories WHERE category_id=? ORDER BY sort_order,id"
    rows=conn.execute(sql,(category_id,)).fetchall(); conn.close(); return rows


def get_subcategory(sub_id):
    conn=db(); row=conn.execute("SELECT * FROM subcategories WHERE id=?",(sub_id,)).fetchone(); conn.close(); return row


def get_products_ctx(category_id, subcategory_id=None, enabled_only=True):
    conn=db(); sql="SELECT * FROM products WHERE category_id=?"; args=[category_id]
    if subcategory_id is None: sql += " AND subcategory_id IS NULL"
    else: sql += " AND subcategory_id=?"; args.append(subcategory_id)
    if enabled_only: sql += " AND enabled=1"
    sql += " ORDER BY sort_order,id"; rows=conn.execute(sql,tuple(args)).fetchall(); conn.close(); return rows


def cat_mode(category_id):
    c=get_category(category_id); return c["mode"] if c and "mode" in c.keys() else MODE_CLASSIC


def sub_mode(sub_id):
    s=get_subcategory(sub_id)
    if not s: return MODE_CLASSIC
    return cat_mode(s["category_id"]) if s["mode"]==MODE_INHERIT else s["mode"]


def prod_mode(product_id):
    p=get_product(product_id)
    if not p: return MODE_CLASSIC
    if p["mode"]!=MODE_INHERIT: return p["mode"]
    if p["subcategory_id"]: return sub_mode(p["subcategory_id"])
    return cat_mode(p["category_id"])


def mode_label(mode):
    return {MODE_CLASSIC:"🔄 Classic", MODE_P2P:"🤝 P2P", MODE_INHERIT:"↪️ Inherit"}.get(mode, "🔄 Classic")


def has_p2p_category(category_id):
    if cat_mode(category_id)==MODE_P2P: return True
    for s in get_subcategories(category_id,False):
        if sub_mode(s["id"])==MODE_P2P: return True
        if any(prod_mode(p["id"])==MODE_P2P for p in get_products_ctx(category_id,s["id"],False)): return True
    return any(prod_mode(p["id"])==MODE_P2P for p in get_products_ctx(category_id,None,False))


def dec(v, default=Decimal("0")):
    try:
        x=Decimal(str(v).replace(",",".").strip())
        return x if x.is_finite() else default
    except (InvalidOperation,ValueError,TypeError): return default


def fmt_dec(v):
    x=dec(v).quantize(Decimal("0.0001"),rounding=ROUND_HALF_UP)
    s=format(x,"f").rstrip("0").rstrip(".")
    return s or "0"


def fee_for(quantity,target_amount):
    conn=db(); tiers=conn.execute("SELECT * FROM p2p_fee_tiers WHERE enabled=1 ORDER BY min_quantity DESC,sort_order,id").fetchall(); conn.close()
    q=int(quantity); target=dec(target_amount); tier=None
    for t in tiers:
        if q>=int(t["min_quantity"]) and (t["max_quantity"] is None or q<=int(t["max_quantity"])):
            tier=t; break
    if not tier: return {"type":"percent","value":"0","amount":Decimal("0")}
    val=dec(tier["fee_value"]); amount=val if tier["fee_type"]=="fixed" else (target*val/Decimal("100")).quantize(Decimal("0.0001"),rounding=ROUND_HALF_UP)
    return {"type":tier["fee_type"],"value":fmt_dec(val),"amount":max(amount,Decimal("0"))}


def p2p_code(prefix="P2P",table="p2p_offers",column="offer_number"):
    alphabet="ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    for _ in range(50):
        value=prefix+"-"+"".join(secrets.choice(alphabet) for _ in range(10))
        conn=db(); exists=conn.execute(f"SELECT 1 FROM {table} WHERE {column}=?",(value,)).fetchone(); conn.close()
        if not exists: return value
    raise RuntimeError("Could not generate a unique P2P code")


# Customer keyboard overrides.
def main_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(get_button("exchange","🔄 Exchange"),callback_data="exchange")],
        [InlineKeyboardButton(get_button("p2p_market","🤝 P2P Market"),callback_data="p2p_market")],
        [InlineKeyboardButton(get_button("how","ℹ️ How It Works"),callback_data="how")],
    ])


def exchange_categories_keyboard():
    btn=[]
    for c in get_categories(True):
        label=("🤝 "+c["button_text"]) if has_p2p_category(c["id"]) else c["button_text"]
        btn.append(InlineKeyboardButton(label,callback_data=f"category:{c['id']}"))
    return two_column_keyboard(btn,[[InlineKeyboardButton(get_button("home","🏠 Main Menu"),callback_data="home")]])


def target_keyboard(back="exchange", p2p=False):
    arr=[]
    for c in get_currencies(True):
        arr.append([InlineKeyboardButton(c["button_text"],callback_data=(f"p2p_target:{c['id']}" if p2p else f"exchange_for:{c['id']}"))])
    arr.append([InlineKeyboardButton(get_button("back","↩️ Back"),callback_data=back)])
    return InlineKeyboardMarkup(arr)


def sub_keyboard(category_id):
    arr=[]
    for s in get_subcategories(category_id,True):
        label=("🤝 "+s["button_text"]) if sub_mode(s["id"])==MODE_P2P else s["button_text"]
        arr.append([InlineKeyboardButton(label,callback_data=f"subcategory:{s['id']}")])
    arr.append([InlineKeyboardButton(get_button("back","↩️ Back"),callback_data="exchange")])
    return InlineKeyboardMarkup(arr)


def product_keyboard_ctx(category_id,sub_id,currency_id=None,p2p_only=False):
    arr=[]
    for p in get_products_ctx(category_id,sub_id,True):
        pm=prod_mode(p["id"])
        if p2p_only and pm!=MODE_P2P: continue
        label=p["button_text"]
        if pm==MODE_P2P: label="🤝 "+label
        elif currency_id:
            rate=get_price(currency_id,p["id"])
            if rate and rate.upper()!="NA": label=f"{label} · {rate}"
        arr.append(InlineKeyboardButton(label,callback_data=f"product:{p['id']}" if not p2p_only else f"p2p_product:{p['id']}"))
    if not p2p_only and (not sub_id and cat_mode(category_id)!=MODE_P2P or sub_id and sub_mode(sub_id)!=MODE_P2P):
        arr.append(InlineKeyboardButton(get_button("custom","✏️ Custom Amount"),callback_data="custom"))
    back=f"subcategory:{sub_id}" if sub_id else f"category:{category_id}"
    return two_column_keyboard(arr,[[InlineKeyboardButton(get_button("back","↩️ Back"),callback_data=back)]])

# ============================================================
# CUSTOMER START / CLASSIC EXCHANGE
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    sessions.pop(user_id, None)
    await clear_user_message(update)
    await remember_screen(
        context.bot,
        update.effective_chat.id,
        user_id,
        render_text("welcome"),
        main_keyboard(),
    )


async def admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user):
        await clear_user_message(update)
        await remember_screen(
            context.bot,
            update.effective_chat.id,
            update.effective_user.id,
            "⛔ <b>ACCESS DENIED</b>",
        )
        return
    sessions.pop(update.effective_user.id, None)
    await clear_user_message(update)
    await remember_screen(
        context.bot,
        update.effective_chat.id,
        update.effective_user.id,
        "⚙️ <b>ADMIN PANEL</b>\n\n"
        "Clean controls for products, categories, P2P and shop settings.\n"
        "All changes are saved immediately.",
        admin_keyboard(),
    )


async def show_exchange(query):
    sessions.pop(query.from_user.id, None)
    await edit_screen(
        query,
        query.from_user.id,
        "🔄 <b>WHAT ARE YOU EXCHANGING?</b>\n\n"
        "Choose what you want to exchange.",
        exchange_categories_keyboard(),
    )


async def show_how(query):
    await edit_screen(
        query,
        query.from_user.id,
        render_text("how"),
        InlineKeyboardMarkup([
            [InlineKeyboardButton(get_button("exchange", "🔄 Exchange"), callback_data="exchange")],
            [InlineKeyboardButton(get_button("p2p_market", "🤝 P2P Market"), callback_data="p2p_market")],
            [InlineKeyboardButton(get_button("home", "🏠 Main Menu"), callback_data="home")],
        ]),
    )


async def show_category_customer(query, category_id, flow="classic"):
    category = get_category(category_id)
    if not category or not category["enabled"]:
        await query.answer("This category is unavailable.", show_alert=True)
        return

    subs = get_subcategories(category_id, True)
    session = session_for(query.from_user.id)
    session.update({
        "category_id": category_id,
        "category_name": category["name"],
        "flow": flow,
    })

    if flow == "p2p":
        # A P2P category may have P2P subcategories, but products directly
        # under the category are also allowed.
        p2p_products = [p for p in get_products_ctx(category_id, None, True)
                        if prod_mode(p["id"]) == MODE_P2P]
        p2p_subs = [s for s in subs if sub_mode(s["id"]) == MODE_P2P or
                    any(prod_mode(p["id"]) == MODE_P2P for p in get_products_ctx(category_id, s["id"], True))]
        if subs and (p2p_subs or p2p_products):
            rows = []
            for s in p2p_subs:
                rows.append([InlineKeyboardButton("🤝 " + s["button_text"], callback_data=f"p2p_subcategory:{s['id']}")])
            if p2p_products:
                for p in p2p_products:
                    rows.append([InlineKeyboardButton("🤝 " + p["button_text"], callback_data=f"p2p_product:{p['id']}")])
            rows.append([InlineKeyboardButton(get_button("back", "↩️ Back"), callback_data="p2p_market")])
            await edit_screen(query, query.from_user.id,
                "🤝 <b>P2P · " + clean(category["name"]) + "</b>\n\nChoose what you want to trade.",
                InlineKeyboardMarkup(rows))
            return

        await show_p2p_products_context(query, category_id, None)
        return

    if subs:
        session["waiting"] = "subcategory"
        await edit_screen(
            query,
            query.from_user.id,
            "🗂️ <b>CHOOSE A SUBCATEGORY</b>\n\n"
            "Then choose what you are exchanging for.",
            sub_keyboard(category_id),
        )
        return

    session["waiting"] = "target"
    await edit_screen(
        query,
        query.from_user.id,
        "🎯 <b>WHAT ARE YOU EXCHANGING FOR?</b>\n\n"
        "Choose what you want in return.",
        target_keyboard("exchange"),
    )


async def show_subcategory_customer(query, subcategory_id, flow="classic"):
    sub = get_subcategory(subcategory_id)
    if not sub or not sub["enabled"]:
        await query.answer("This subcategory is unavailable.", show_alert=True)
        return

    category = get_category(sub["category_id"])
    session = session_for(query.from_user.id)
    session.update({
        "category_id": sub["category_id"],
        "category_name": category["name"] if category else "",
        "subcategory_id": subcategory_id,
        "subcategory_name": sub["name"],
        "flow": flow,
    })

    if flow == "p2p" or sub_mode(subcategory_id) == MODE_P2P:
        await show_p2p_products_context(query, sub["category_id"], subcategory_id)
        return

    session["waiting"] = "target"
    await edit_screen(
        query,
        query.from_user.id,
        "🎯 <b>WHAT ARE YOU EXCHANGING FOR?</b>\n\n"
        "Choose what you want in return.",
        target_keyboard(f"category:{sub['category_id']}")
    )


async def show_classic_products(query, currency_id):
    session = sessions.get(query.from_user.id, {})
    category_id = session.get("category_id")
    sub_id = session.get("subcategory_id")
    currency = get_currency(currency_id)
    if not category_id or not currency or not currency["enabled"]:
        await query.answer("Please start the exchange again.", show_alert=True)
        return

    products = [p for p in get_products_ctx(category_id, sub_id, True)
                if prod_mode(p["id"]) == MODE_CLASSIC]
    p2p_products = [p for p in get_products_ctx(category_id, sub_id, True)
                    if prod_mode(p["id"]) == MODE_P2P]

    session.update({
        "currency_id": currency_id,
        "currency_name": currency["name"],
        "waiting": "product",
        "flow": "classic",
    })

    buttons = []
    for product in products:
        price = get_price(currency_id, product["id"])
        label = product["button_text"]
        if price and price.upper() != "NA":
            label = f"{label} · {price}"
        buttons.append(InlineKeyboardButton(label, callback_data=f"product:{product['id']}"))

    for product in p2p_products:
        buttons.append(InlineKeyboardButton("🤝 " + product["button_text"], callback_data=f"p2p_product:{product['id']}"))

    if products:
        text = "📦 <b>CHOOSE THE EXCHANGE</b>\n\nChoose the amount you want to exchange."
    else:
        text = render_text("no_products")

    bottom = [[InlineKeyboardButton(get_button("back", "↩️ Back"), callback_data=(f"category:{category_id}" if not sub_id else f"subcategory:{sub_id}"))]]
    await edit_screen(query, query.from_user.id, text, two_column_keyboard(buttons, bottom))


async def show_p2p_product_menu(query, product_id):
    product = get_product(product_id)
    if not product or not product["enabled"] or prod_mode(product_id) != MODE_P2P:
        await query.answer("This P2P product is unavailable.", show_alert=True)
        return

    session = session_for(query.from_user.id)
    session.update({
        "category_id": product["category_id"],
        "subcategory_id": product["subcategory_id"],
        "product_id": product_id,
        "product": product["name"],
        "flow": "p2p",
    })

    await edit_screen(
        query,
        query.from_user.id,
        "🤝 <b>" + clean(product["name"]) + "</b>\n\n"
        "Create your own offer or browse existing offers.",
        InlineKeyboardMarkup([
            [InlineKeyboardButton(get_button("p2p_browse", "🔎 Browse Offers"), callback_data=f"p2p_offers:{product_id}")],
            [InlineKeyboardButton(get_button("p2p_publish", "➕ Publish Offer"), callback_data=f"p2p_publish_product:{product_id}")],
            [InlineKeyboardButton(get_button("p2p_my_offers", "📋 My Offers"), callback_data="p2p_my_offers")],
            [InlineKeyboardButton(get_button("back", "↩️ Back"), callback_data="p2p_market")],
        ])
    )


# ============================================================
# P2P OFFER / TRADE HELPERS
# ============================================================

def get_p2p_offer(offer_id):
    conn = db()
    row = conn.execute("SELECT * FROM p2p_offers WHERE id = ?", (offer_id,)).fetchone()
    conn.close()
    return row


def get_p2p_trade(trade_id):
    conn = db()
    row = conn.execute("SELECT * FROM p2p_trades WHERE id = ?", (trade_id,)).fetchone()
    conn.close()
    return row


def active_p2p_offers(product_id):
    conn = db()
    rows = conn.execute(
        """
        SELECT * FROM p2p_offers
        WHERE product_id = ? AND status = ?
        ORDER BY created_at DESC, id DESC
        """,
        (product_id, P2P_ACTIVE),
    ).fetchall()
    conn.close()
    return rows


def my_p2p_offers(telegram_id):
    conn = db()
    rows = conn.execute(
        """
        SELECT * FROM p2p_offers
        WHERE seller_id = ?
        ORDER BY id DESC
        LIMIT 50
        """,
        (telegram_id,),
    ).fetchall()
    conn.close()
    return rows


# ============================================================
# P2P MARKET CUSTOMER FLOW
# ============================================================

async def show_p2p_market(query):
    await edit_screen(
        query,
        query.from_user.id,
        render_text("p2p_market"),
        InlineKeyboardMarkup([
            [InlineKeyboardButton(get_button("p2p_browse", "🔎 Browse P2P Offers"), callback_data="p2p_browse_categories")],
            [InlineKeyboardButton(get_button("p2p_publish", "➕ Publish an Offer"), callback_data="p2p_publish_categories")],
            [InlineKeyboardButton(get_button("p2p_my_offers", "📋 My Offers"), callback_data="p2p_my_offers")],
            [InlineKeyboardButton(get_button("home", "🏠 Main Menu"), callback_data="home")],
        ])
    )


async def show_p2p_categories(query, publish=False):
    categories = []
    for c in get_categories(True):
        if publish:
            if has_p2p_category(c["id"]):
                categories.append(c)
        else:
            if has_p2p_category(c["id"]):
                categories.append(c)
    if not categories:
        await edit_screen(query, query.from_user.id, render_text("p2p_empty"), InlineKeyboardMarkup([
            [InlineKeyboardButton(get_button("back", "↩️ Back"), callback_data="p2p_market")]
        ]))
        return

    rows = []
    for c in categories:
        rows.append([InlineKeyboardButton("🤝 " + c["button_text"], callback_data=f"p2p_category:{c['id']}")])
    rows.append([InlineKeyboardButton(get_button("back", "↩️ Back"), callback_data="p2p_market")])
    await edit_screen(query, query.from_user.id, "🤝 <b>P2P MARKET</b>\n\nChoose a category.", InlineKeyboardMarkup(rows))


async def show_p2p_category(query, category_id):
    await show_category_customer(query, category_id, flow="p2p")


async def show_p2p_products_context(query, category_id, sub_id):
    session = session_for(query.from_user.id)
    session.update({"category_id": category_id, "subcategory_id": sub_id, "flow": "p2p"})
    products = [p for p in get_products_ctx(category_id, sub_id, True) if prod_mode(p["id"]) == MODE_P2P]
    if not products:
        await edit_screen(query, query.from_user.id, render_text("p2p_empty"), InlineKeyboardMarkup([
            [InlineKeyboardButton(get_button("back", "↩️ Back"), callback_data=(f"p2p_category:{category_id}" if sub_id is None else f"p2p_subcategory:{sub_id}"))]
        ]))
        return

    rows = [[InlineKeyboardButton("🤝 " + p["button_text"], callback_data=f"p2p_product:{p['id']}")] for p in products]
    rows.append([InlineKeyboardButton(get_button("back", "↩️ Back"), callback_data=(f"p2p_category:{category_id}" if sub_id is None else f"p2p_subcategory:{sub_id}"))])
    await edit_screen(query, query.from_user.id, "🤝 <b>CHOOSE WHAT YOU ARE OFFERING</b>\n\nSelect the item you want to list.", InlineKeyboardMarkup(rows))


async def show_p2p_offers(query, product_id):
    product = get_product(product_id)
    if not product:
        await query.answer("Product not found.", show_alert=True)
        return
    offers = active_p2p_offers(product_id)
    if not offers:
        await edit_screen(query, query.from_user.id, "🤝 <b>NO OFFERS YET</b>\n\nYou can publish the first offer.", InlineKeyboardMarkup([
            [InlineKeyboardButton(get_button("p2p_publish", "➕ Publish Offer"), callback_data=f"p2p_publish_product:{product_id}")],
            [InlineKeyboardButton(get_button("back", "↩️ Back"), callback_data=f"product:{product_id}")],
        ]))
        return

    rows = []
    for offer in offers:
        fee_info = fee_for(offer["source_amount"], offer["target_amount"]); fee_total = fee_info["amount"]
        label = f"{offer['source_amount']:,} → {fmt_dec(offer['target_amount'])} {offer['target_name']} (+{fmt_dec(fee_total)} fee)"
        rows.append([InlineKeyboardButton(label, callback_data=f"p2p_offer:{offer['id']}")])
    rows.append([InlineKeyboardButton(get_button("p2p_publish", "➕ Publish Offer"), callback_data=f"p2p_publish_product:{product_id}")])
    rows.append([InlineKeyboardButton(get_button("back", "↩️ Back"), callback_data=f"product:{product_id}")])
    await edit_screen(query, query.from_user.id, "🤝 <b>P2P OFFERS</b>\n\nNo seller identities are shown to buyers.", InlineKeyboardMarkup(rows))


async def show_p2p_offer(query, offer_id):
    offer = get_p2p_offer(offer_id)
    if not offer or offer["status"] != P2P_ACTIVE:
        await query.answer("This offer is no longer available.", show_alert=True)
        return
    fee_info = fee_for(offer["source_amount"], offer["target_amount"]); fee_total = fee_info["amount"]
    total = dec(offer["target_amount"]) + fee_total
    text = render_text(
        "p2p_details",
        source_amount=f"{offer['source_amount']:,}",
        product=offer["product_name"],
        target_amount=fmt_dec(offer["target_amount"]),
        target_name=offer["target_name"],
        fee_amount=fmt_dec(fee_total),
        buyer_total=fmt_dec(total),
        order_number=offer["offer_number"],
    )
    await edit_screen(query, query.from_user.id, text, InlineKeyboardMarkup([
        [InlineKeyboardButton(get_button("p2p_buy", "🛒 Buy Offer"), callback_data=f"p2p_buy_confirm:{offer_id}")],
        [InlineKeyboardButton(get_button("back", "↩️ Back"), callback_data=f"p2p_offers:{offer['product_id']}")],
    ]))


async def show_p2p_buy_confirm(query, offer_id):
    offer = get_p2p_offer(offer_id)
    if not offer or offer["status"] != P2P_ACTIVE:
        await query.answer("This offer is no longer available.", show_alert=True)
        return
    fee_info = fee_for(offer["source_amount"], offer["target_amount"]); fee_total = fee_info["amount"]
    total = dec(offer["target_amount"]) + fee_total
    text = render_text(
        "p2p_buy_confirm",
        source_amount=f"{offer['source_amount']:,}",
        product=offer["product_name"],
        target_amount=fmt_dec(offer["target_amount"]),
        target_name=offer["target_name"],
        fee_amount=fmt_dec(fee_total),
        buyer_total=fmt_dec(total),
        order_number=offer["offer_number"],
    )
    await edit_screen(query, query.from_user.id, text, InlineKeyboardMarkup([
        [InlineKeyboardButton(get_button("p2p_confirm", "✅ Confirm Purchase"), callback_data=f"p2p_buy:{offer_id}")],
        [InlineKeyboardButton(get_button("back", "↩️ Back"), callback_data=f"p2p_offer:{offer_id}")],
    ]))


async def show_my_p2p_offers(query):
    offers = my_p2p_offers(query.from_user.id)
    rows = []
    for offer in offers:
        status = {P2P_ACTIVE: "🟢", P2P_RESERVED: "🟠", P2P_SOLD: "✅", P2P_CANCELLED: "⚪"}.get(offer["status"], "⚪")
        rows.append([InlineKeyboardButton(f"{status} {offer['offer_number']} · {offer['source_amount']:,}", callback_data=f"my_p2p_offer:{offer['id']}")])
    if not rows:
        text = render_text("p2p_empty")
    else:
        text = render_text("p2p_my_offers")
    rows.append([InlineKeyboardButton(get_button("back", "↩️ Back"), callback_data="p2p_market")])
    await edit_screen(query, query.from_user.id, text, InlineKeyboardMarkup(rows))


async def show_my_p2p_offer(query, offer_id):
    offer = get_p2p_offer(offer_id)
    if not offer or offer["seller_id"] != query.from_user.id:
        await query.answer("Offer not found.", show_alert=True)
        return
    fee_info = fee_for(offer["source_amount"], offer["target_amount"]); fee_total = fee_info["amount"]
    text = (
        "📋 <b>MY P2P OFFER</b>\n\n"
        f"🔐 Code: <code>{clean(offer['offer_number'])}</code>\n"
        f"📦 Offering: <b>{clean(offer['product_name'])}</b>\n"
        f"💰 Amount: <b>{offer['source_amount']:,}</b>\n"
        f"🎯 Wants: <b>{fmt_dec(offer['target_amount'])} {clean(offer['target_name'])}</b>\n"
        f"💸 Buyer fee: <b>{fmt_dec(fee_total)}</b>\n"
        f"📌 Status: <b>{clean(offer['status'])}</b>"
    )
    rows = []
    if offer["status"] == P2P_ACTIVE:
        rows.append([InlineKeyboardButton("🗑️ Cancel Offer", callback_data=f"p2p_cancel_offer:{offer_id}")])
    rows.append([InlineKeyboardButton(get_button("back", "↩️ Back"), callback_data="p2p_my_offers")])
    await edit_screen(query, query.from_user.id, text, InlineKeyboardMarkup(rows))


# ============================================================
# P2P PUBLISH WIZARD
# ============================================================

async def p2p_publish_start(query, product_id):
    product = get_product(product_id)
    if not product or not product["enabled"] or prod_mode(product_id) != MODE_P2P:
        await query.answer("This product is unavailable for P2P.", show_alert=True)
        return
    session = session_for(query.from_user.id)
    session.update({
        "flow": "p2p_publish",
        "category_id": product["category_id"],
        "subcategory_id": product["subcategory_id"],
        "product_id": product_id,
        "product": product["name"],
        "waiting": "p2p_offer_amount",
    })
    await edit_screen(query, query.from_user.id, render_text("p2p_offer_amount", product=product["name"]), cancel_keyboard())


async def show_p2p_publish_products(query):
    await show_p2p_categories(query, publish=True)


async def create_p2p_offer(user, session):
    amount = int(session["p2p_source_amount"])
    target_amount = dec(session["p2p_target_amount"])
    if target_amount <= 0:
        raise ValueError("Target amount must be positive")
    code = p2p_code("P2P", "p2p_offers", "offer_number")
    while get_p2p_offer_by_code(code):
        code = p2p_code("P2P", "p2p_offers", "offer_number")
    now = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
    product = get_product(session["product_id"])
    category = get_category(session["category_id"])
    sub = get_subcategory(session["subcategory_id"]) if session.get("subcategory_id") else None
    conn = db()
    conn.execute(
        """
        INSERT INTO p2p_offers
        (offer_number,seller_id,seller_username,seller_name,category_name,subcategory_name,
         product_id,product_name,source_amount,target_name,target_amount,status,created_at,updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            code, user.id, user.username or "", user.full_name or "",
            category["name"] if category else "",
            sub["name"] if sub else "",
            product["id"], product["name"], amount,
            session["p2p_target_name"], str(target_amount), P2P_ACTIVE,
            now, now,
        ),
    )
    conn.commit()
    conn.close()
    return code


async def p2p_preview(query):
    session = sessions.get(query.from_user.id, {})
    fee_info = fee_for(session["p2p_source_amount"], session["p2p_target_amount"]); fee_total = fee_info["amount"]
    target = dec(session["p2p_target_amount"])
    total = target + fee_total
    text = render_text(
        "p2p_preview",
        product=session["product"],
        source_amount=f"{session['p2p_source_amount']:,}",
        target_amount=fmt_dec(target),
        target_name=session["p2p_target_name"],
        fee_amount=fmt_dec(fee_total),
        buyer_total=fmt_dec(total),
    )
    await edit_screen(query, query.from_user.id, text, InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Publish Offer", callback_data="p2p_publish_confirm")],
        [InlineKeyboardButton(get_button("cancel", "❌ Cancel"), callback_data="p2p_market")],
    ]))


async def buy_p2p_offer(query, context, offer_id):
    buyer = query.from_user
    now = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
    trade_code = p2p_code("TRD", "p2p_trades", "trade_number")
    offer_for_fee = get_p2p_offer(offer_id)
    if not offer_for_fee:
        await query.answer("Offer not found.", show_alert=True)
        return
    fee_total = fee_for(offer_for_fee["source_amount"], offer_for_fee["target_amount"])["amount"]

    conn = db()
    # Atomic reservation prevents two buyers from purchasing the same offer.
    updated = conn.execute(
        """
        UPDATE p2p_offers
        SET status = ?, buyer_id = ?, buyer_username = ?, buyer_name = ?, updated_at = ?
        WHERE id = ? AND status = ?
        """,
        (P2P_RESERVED, buyer.id, buyer.username or "", buyer.full_name or "", now, offer_id, P2P_ACTIVE),
    ).rowcount
    if updated != 1:
        conn.rollback()
        conn.close()
        await query.answer("Someone else already took this offer.", show_alert=True)
        return

    offer = conn.execute("SELECT * FROM p2p_offers WHERE id = ?", (offer_id,)).fetchone()
    total = dec(offer["target_amount"]) + fee_total
    conn.execute(
        """
        INSERT INTO p2p_trades
        (trade_code,offer_id,seller_id,buyer_id,source_amount,target_name,target_amount,fee,total_amount,status,created_at,updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (trade_code,offer_id,offer["seller_id"],buyer.id,offer["source_amount"],offer["target_name"],offer["target_amount"],str(fee_total),str(total),"pending",now,now),
    )
    trade_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
    conn.commit()
    conn.close()

    recipient = get_setting("order_recipient_chat_id", "").strip() or BOOTSTRAP_ADMIN_CHAT_ID
    admin_text = (
        "🤝 <b>NEW P2P TRADE</b>\n\n"
        f"🔐 Trade: <code>{trade_code}</code>\n"
        f"📦 Offer: <b>{clean(offer['product_name'])}</b>\n"
        f"💰 Seller offers: <b>{offer['source_amount']:,}</b>\n"
        f"🎯 Wants: <b>{clean(offer['target_amount'])} {clean(offer['target_name'])}</b>\n"
        f"💸 Fee: <b>{fmt_dec(fee_total)}</b>\n"
        f"💳 Buyer total: <b>{fmt_dec(total)} {clean(offer['target_name'])}</b>\n\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        f"👤 Seller: <b>{clean(offer['seller_name'])}</b>\n"
        f"📱 Seller: <b>{clean('@' + offer['seller_username']) if offer['seller_username'] else 'No username'}</b>\n"
        f"🆔 Seller ID: <code>{offer['seller_id']}</code>\n\n"
        f"👤 Buyer: <b>{clean(buyer.full_name or '')}</b>\n"
        f"📱 Buyer: <b>{clean('@' + buyer.username) if buyer.username else 'No username'}</b>\n"
        f"🆔 Buyer ID: <code>{buyer.id}</code>"
    )
    if recipient:
        try:
            await context.bot.send_message(chat_id=int(recipient), text=admin_text, parse_mode="HTML")
        except Exception as error:
            logger.error("P2P admin notification failed: %s", error)

    # Buyer never receives seller identity.
    await remember_screen(
        context.bot,
        query.message.chat_id,
        buyer.id,
        render_text(
            "p2p_bought",
            trade_code=trade_code,
            source_amount=f"{offer['source_amount']:,}",
            product=offer["product_name"],
            target_amount=fmt_dec(offer["target_amount"]),
            target_name=offer["target_name"],
            fee_amount=fmt_dec(fee_total),
            buyer_total=fmt_dec(total),
        ),
        InlineKeyboardMarkup([[InlineKeyboardButton(get_button("p2p_market", "🤝 P2P Market"), callback_data="p2p_market")], [InlineKeyboardButton(get_button("home", "🏠 Main Menu"), callback_data="home")]]),
    )

    # Seller is notified but buyer identity is never disclosed.
    try:
        await context.bot.send_message(
            chat_id=offer["seller_id"],
            text=render_text(
                "p2p_seller_notice",
                trade_code=trade_code,
                source_amount=f"{offer['source_amount']:,}",
                product=offer["product_name"],
                target_amount=fmt_dec(offer["target_amount"]),
                target_name=offer["target_name"],
            ),
            parse_mode="HTML",
        )
    except Exception as error:
        logger.warning("Could not notify P2P seller: %s", error)


# Helper because offer codes are independent of the main orders table.
def get_p2p_offer_by_code(code):
    conn = db()
    row = conn.execute("SELECT 1 FROM p2p_offers WHERE offer_number = ?", (code,)).fetchone()
    conn.close()
    return row

# ============================================================
# ADMIN: P2P MANAGEMENT
# ============================================================

def p2p_admin_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💸 Fee Rules", callback_data="admin_p2p_fees")],
        [InlineKeyboardButton("🤝 Offers", callback_data="admin_p2p_offers")],
        [InlineKeyboardButton("🔄 Trades", callback_data="admin_p2p_trades")],
        [InlineKeyboardButton("↩️ Admin Panel", callback_data="admin")],
    ])


async def show_p2p_admin(query):
    await edit_screen(query, query.from_user.id,
        "🤝 <b>P2P MANAGEMENT</b>\n\n"
        "Manage marketplace fees, offers and trades.",
        p2p_admin_keyboard())


async def show_p2p_fee_tiers(query):
    conn=db(); tiers=conn.execute("SELECT * FROM p2p_fee_tiers ORDER BY min_quantity,sort_order,id").fetchall(); conn.close()
    rows=[[InlineKeyboardButton("➕ Add Fee Rule",callback_data="p2p_fee_add")]]
    for t in tiers:
        max_q="∞" if t["max_quantity"] is None else f"{t['max_quantity']:,}"
        typ="%" if t["fee_type"]=="percent" else "fixed"
        status="🟢" if t["enabled"] else "🔴"
        rows.append([InlineKeyboardButton(f"{status} {t['min_quantity']:,}–{max_q} · {t['fee_value']}{typ}",callback_data=f"p2p_fee_edit:{t['id']}")])
    rows.append([InlineKeyboardButton("↩️ P2P Management",callback_data="admin_p2p")])
    await edit_screen(query,query.from_user.id,
        "💸 <b>P2P FEE RULES</b>\n\n"
        "Fees are calculated from the buyer's requested amount.\n"
        "The quantity band is the amount of the offered item.",
        InlineKeyboardMarkup(rows))


async def p2p_fee_add_start(query):
    start_admin_action(query.from_user.id,"p2p_fee_min")
    await edit_screen(query,query.from_user.id,"➕ <b>ADD FEE RULE</b>\n\nSend the minimum offered quantity.\nExample: <code>1</code>")


async def show_p2p_fee_edit(query, tier_id):
    conn=db(); t=conn.execute("SELECT * FROM p2p_fee_tiers WHERE id=?",(tier_id,)).fetchone(); conn.close()
    if not t:
        await query.answer("Fee rule not found.",show_alert=True); return
    max_q="∞" if t["max_quantity"] is None else f"{t['max_quantity']:,}"
    status="🟢 Enabled" if t["enabled"] else "🔴 Disabled"
    rows=[
        [InlineKeyboardButton("🔢 Min Quantity",callback_data=f"p2p_fee_min_edit:{tier_id}"), InlineKeyboardButton("🔢 Max Quantity",callback_data=f"p2p_fee_max_edit:{tier_id}")],
        [InlineKeyboardButton("📐 Type",callback_data=f"p2p_fee_type:{tier_id}"), InlineKeyboardButton("💸 Value",callback_data=f"p2p_fee_value:{tier_id}")],
        [InlineKeyboardButton("🟢 / 🔴 Toggle",callback_data=f"p2p_fee_toggle:{tier_id}")],
        [InlineKeyboardButton("🗑️ Delete",callback_data=f"p2p_fee_delete:{tier_id}")],
        [InlineKeyboardButton("↩️ Fee Rules",callback_data="admin_p2p_fees")],
    ]
    await edit_screen(query,query.from_user.id,
        "💸 <b>EDIT FEE RULE</b>\n\n"
        f"Range: <b>{t['min_quantity']:,} – {max_q}</b>\n"
        f"Type: <b>{clean(t['fee_type'])}</b>\n"
        f"Value: <b>{clean(t['fee_value'])}</b>\n"
        f"Status: <b>{status}</b>",InlineKeyboardMarkup(rows))


async def show_p2p_fee_type(query,tier_id):
    await edit_screen(query,query.from_user.id,
        "📐 <b>FEE TYPE</b>\n\nChoose how the fee is calculated.",
        InlineKeyboardMarkup([
            [InlineKeyboardButton("Percentage %",callback_data=f"p2p_fee_type_set:{tier_id}:percent")],
            [InlineKeyboardButton("Fixed amount",callback_data=f"p2p_fee_type_set:{tier_id}:fixed")],
            [InlineKeyboardButton("↩️ Fee Rule",callback_data=f"p2p_fee_edit:{tier_id}")],
        ]))


async def show_admin_p2p_offers(query):
    conn=db(); offers=conn.execute("SELECT * FROM p2p_offers ORDER BY id DESC LIMIT 30").fetchall(); conn.close()
    rows=[]
    for o in offers:
        icon={P2P_OPEN:"🟢",P2P_RESERVED:"🟠",P2P_COMPLETED:"✅",P2P_REJECTED:"🔴",P2P_CANCELLED:"⚪"}.get(o["status"],"⚪")
        rows.append([InlineKeyboardButton(f"{icon} {o['offer_number']} · {o['source_amount']:,} {o['product_name']}",callback_data=f"admin_p2p_offer:{o['id']}")])
    rows.append([InlineKeyboardButton("↩️ P2P Management",callback_data="admin_p2p")])
    text="🤝 <b>P2P OFFERS</b>\n\nNo offers." if not offers else "🤝 <b>P2P OFFERS</b>\n\nTap an offer for full admin-only details."
    await edit_screen(query,query.from_user.id,text,InlineKeyboardMarkup(rows))


async def show_admin_p2p_offer(query,offer_id):
    o=get_p2p_offer(offer_id)
    if not o:
        await query.answer("Offer not found.",show_alert=True); return
    fee=fee_for(o["source_amount"],o["target_amount"])["amount"]
    rows=[]
    if o["status"]==P2P_OPEN:
        rows.append([InlineKeyboardButton("❌ Cancel",callback_data=f"admin_p2p_cancel_offer:{offer_id}")])
    rows.append([InlineKeyboardButton("↩️ Offers",callback_data="admin_p2p_offers")])
    await edit_screen(query,query.from_user.id,
        "🤝 <b>P2P OFFER</b>\n\n"
        f"🔐 Offer: <code>{clean(o['offer_number'])}</code>\n"
        f"📦 {clean(o['product_name'])}: <b>{o['source_amount']:,}</b>\n"
        f"🎯 Wants: <b>{clean(o['target_amount'])} {clean(o['target_name'])}</b>\n"
        f"💸 Current fee: <b>{fmt_dec(fee)} {clean(o['target_name'])}</b>\n"
        f"📌 Status: <b>{clean(o['status'])}</b>\n"
        f"📅 Created: <b>{clean(o['created_at'])}</b>\n\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        f"👤 Seller: <b>{clean(o['seller_name'])}</b>\n"
        f"📱 @{clean(o['seller_username']) if o['seller_username'] else 'No username'}\n"
        f"🆔 Seller ID: <code>{o['seller_id']}</code>",InlineKeyboardMarkup(rows))


async def show_admin_p2p_trades(query):
    conn=db(); trades=conn.execute("SELECT * FROM p2p_trades ORDER BY id DESC LIMIT 30").fetchall(); conn.close()
    rows=[]
    for t in trades:
        rows.append([InlineKeyboardButton(f"{t['trade_number']} · {t['status']}",callback_data=f"admin_p2p_trade:{t['id']}")])
    rows.append([InlineKeyboardButton("↩️ P2P Management",callback_data="admin_p2p")])
    text="🔄 <b>P2P TRADES</b>\n\nNo trades." if not trades else "🔄 <b>P2P TRADES</b>\n\nTap a trade for details."
    await edit_screen(query,query.from_user.id,text,InlineKeyboardMarkup(rows))


async def show_admin_p2p_trade(query,trade_id):
    conn=db(); t=conn.execute("SELECT * FROM p2p_trades WHERE id=?",(trade_id,)).fetchone(); conn.close()
    if not t:
        await query.answer("Trade not found.",show_alert=True); return
    o=get_p2p_offer(t["offer_id"])
    rows=[]
    if t["status"]=="awaiting_confirmation":
        rows.append([InlineKeyboardButton("✅ Confirm",callback_data=f"p2p_trade_status:completed:{trade_id}"),InlineKeyboardButton("❌ Reject",callback_data=f"p2p_trade_status:rejected:{trade_id}")])
    rows.append([InlineKeyboardButton("↩️ Trades",callback_data="admin_p2p_trades")])
    await edit_screen(query,query.from_user.id,
        "🔄 <b>P2P TRADE</b>\n\n"
        f"🔐 Trade: <code>{clean(t['trade_number'])}</code>\n"
        f"🔐 Offer: <code>{clean(t['offer_number'])}</code>\n"
        f"💰 Seller offers: <b>{t['source_amount']:,}</b>\n"
        f"🎯 Buyer provides: <b>{clean(t['buyer_total'])} {clean(t['target_name'])}</b>\n"
        f"💸 Fee: <b>{clean(t['fee_amount'])}</b>\n"
        f"📌 Status: <b>{clean(t['status'])}</b>\n\n"
        f"👤 Seller ID: <code>{t['seller_id']}</code>\n"
        f"👤 Buyer ID: <code>{t['buyer_id']}</code>",InlineKeyboardMarkup(rows))

# ============================================================
# CALLBACK HANDLER (P2P + CLASSIC OVERRIDE)
# ============================================================

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    user = query.from_user
    user_id = user.id

    # ------------------------------
    # Customer navigation
    # ------------------------------
    if data == "noop":
        return
    if data == "home":
        sessions.pop(user_id, None)
        await edit_screen(query,user_id,render_text("welcome"),main_keyboard())
        return
    if data == "exchange":
        await show_exchange(query)
        return
    if data == "how":
        await show_how(query)
        return
    if data == "p2p_market":
        sessions.pop(user_id,None)
        await show_p2p_market(query)
        return
    if data == "p2p_browse_categories":
        await show_p2p_categories(query,False)
        return
    if data == "p2p_publish_categories":
        await show_p2p_categories(query,True)
        return
    if data == "p2p_my_offers":
        await show_my_p2p_offers(query)
        return
    if data.startswith("p2p_category:"):
        await show_p2p_category(query,int(data.split(":",1)[1]))
        return
    if data.startswith("p2p_subcategory:"):
        await show_p2p_products_context(query, get_subcategory(int(data.split(":",1)[1]))["category_id"], int(data.split(":",1)[1]))
        return
    if data.startswith("p2p_product:"):
        await show_p2p_product_menu(query,int(data.split(":",1)[1]))
        return
    if data.startswith("p2p_offers:"):
        await show_p2p_offers(query,int(data.split(":",1)[1]))
        return
    if data.startswith("p2p_offer:"):
        await show_p2p_offer(query,int(data.split(":",1)[1]))
        return
    if data.startswith("p2p_buy_confirm:"):
        offer=get_p2p_offer(int(data.split(":",1)[1]))
        if offer and offer["seller_id"]==user_id:
            await query.answer("You cannot buy your own offer.",show_alert=True); return
        await show_p2p_buy_confirm(query,int(data.split(":",1)[1]))
        return
    if data.startswith("p2p_buy:"):
        offer=get_p2p_offer(int(data.split(":",1)[1]))
        if not offer:
            await query.answer("Offer not found.",show_alert=True); return
        if offer["seller_id"]==user_id:
            await query.answer("You cannot buy your own offer.",show_alert=True); return
        await buy_p2p_offer(query,context,int(data.split(":",1)[1]))
        return
    if data.startswith("p2p_cancel_offer:"):
        offer_id=int(data.split(":",1)[1]); offer=get_p2p_offer(offer_id)
        if not offer or offer["seller_id"]!=user_id:
            await query.answer("Offer not found.",show_alert=True); return
        conn=db(); updated=conn.execute("UPDATE p2p_offers SET status=?,updated_at=? WHERE id=? AND seller_id=? AND status=?",(P2P_CANCELLED,datetime.now().strftime("%d.%m.%Y %H:%M:%S"),offer_id,user_id,P2P_OPEN)).rowcount; conn.commit(); conn.close()
        if updated:
            await edit_screen(query,user_id,render_text("p2p_cancelled"),InlineKeyboardMarkup([[InlineKeyboardButton("🤝 P2P Market",callback_data="p2p_market")]]))
        else:
            await query.answer("This offer can no longer be cancelled.",show_alert=True)
        return
    if data.startswith("my_p2p_offer:"):
        await show_my_p2p_offer(query,int(data.split(":",1)[1]))
        return
    if data.startswith("p2p_publish_product:"):
        await p2p_publish_start(query,int(data.split(":",1)[1]))
        return
    if data == "p2p_publish_confirm":
        session=sessions.get(user_id,{})
        if session.get("waiting")!="p2p_preview":
            await query.answer("Please start the offer again.",show_alert=True); return
        try:
            offer_number=await create_p2p_offer(user,session)
        except Exception as error:
            logger.exception("Could not publish P2P offer")
            await query.answer("Could not publish the offer.",show_alert=True); return
        offer=get_p2p_offer_by_code(offer_number,True)
        recipient=get_setting("order_recipient_chat_id","").strip() or BOOTSTRAP_ADMIN_CHAT_ID
        if recipient and offer:
            try:
                fee_total=fee_for(offer["source_amount"],offer["target_amount"])["amount"]
                buyer_total=dec(offer["target_amount"])+fee_total
                admin_text=(
                    "🤝 <b>NEW P2P OFFER</b>\n\n"
                    f"🔐 Offer: <code>{offer_number}</code>\n"
                    f"📦 Category: <b>{clean(offer['category_name'])}</b>\n"
                    f"🗂️ Subcategory: <b>{clean(offer['subcategory_name']) if offer['subcategory_name'] else 'None'}</b>\n"
                    f"📦 Product: <b>{clean(offer['product_name'])}</b>\n"
                    f"💰 Offering: <b>{offer['source_amount']:,}</b>\n"
                    f"🎯 Wants: <b>{clean(offer['target_amount'])} {clean(offer['target_name'])}</b>\n"
                    f"💸 Buyer fee: <b>{fmt_dec(fee_total)} {clean(offer['target_name'])}</b>\n"
                    f"💳 Buyer total: <b>{fmt_dec(buyer_total)} {clean(offer['target_name'])}</b>\n"
                    f"📅 Created: <b>{clean(offer['created_at'])}</b>\n\n"
                    f"👤 Seller: <b>{clean(offer['seller_name'])}</b>\n"
                    f"📱 @{clean(offer['seller_username']) if offer['seller_username'] else 'No username'}\n"
                    f"🆔 Seller ID: <code>{offer['seller_id']}</code>"
                )
                await context.bot.send_message(chat_id=int(recipient),text=admin_text,parse_mode="HTML")
            except Exception as error:
                logger.error("Could not notify admin of P2P offer: %s",error)
        sessions.pop(user_id,None)
        await edit_screen(query,user_id,render_text("p2p_published",offer_number=offer_number),InlineKeyboardMarkup([
            [InlineKeyboardButton("📋 My Offers",callback_data="p2p_my_offers")],
            [InlineKeyboardButton("🤝 P2P Market",callback_data="p2p_market")],
        ]))
        return
    if data.startswith("p2p_target:"):
        session=sessions.get(user_id,{})
        if session.get("flow")!="p2p_publish":
            await query.answer("Please start the offer again.",show_alert=True); return
        currency=get_currency(int(data.split(":",1)[1]))
        if not currency or not currency["enabled"]:
            await query.answer("This target is unavailable.",show_alert=True); return
        session["p2p_target_currency_id"]=currency["id"]
        session["p2p_target_name"]=currency["name"]
        session["waiting"]="p2p_target_amount"
        await edit_screen(query,user_id,render_text("p2p_offer_target_amount",target_name=currency["name"]),cancel_keyboard())
        return

    # Classic: category -> (optional subcategory) -> exchange target -> product.
    if data.startswith("category:"):
        cid=int(data.split(":",1)[1]); c=get_category(cid)
        if not c or not c["enabled"]:
            await query.answer("This category is unavailable.",show_alert=True); return
        if cat_mode(cid)==MODE_P2P:
            await show_category_customer(query,cid,"p2p")
        else:
            await show_category_customer(query,cid,"classic")
        return
    if data.startswith("subcategory:"):
        sid=int(data.split(":",1)[1]); s=get_subcategory(sid)
        if not s or not s["enabled"]:
            await query.answer("This subcategory is unavailable.",show_alert=True); return
        if sub_mode(sid)==MODE_P2P:
            await show_p2p_products_context(query,s["category_id"],sid)
        else:
            await show_subcategory_customer(query,sid,"classic")
        return
    if data.startswith("exchange_for:"):
        await show_classic_products(query,int(data.split(":",1)[1])); return
    if data.startswith("product:"):
        pid=int(data.split(":",1)[1]); p=get_product(pid); session=sessions.get(user_id)
        if not p or not session or not session.get("waiting")=="product":
            await query.answer("Please start the exchange again.",show_alert=True); return
        if not p["enabled"] or p["category_id"]!=session.get("category_id") or (p["subcategory_id"] or None)!=(session.get("subcategory_id") or None):
            await query.answer("This exchange is unavailable.",show_alert=True); return
        if prod_mode(pid)==MODE_P2P:
            await show_p2p_product_menu(query,pid); return
        price=get_price(session["currency_id"],pid)
        session.update({"product_id":pid,"product":p["name"],"amount":p["amount"],"price":price,"waiting":"username","flow":"classic"})
        await edit_screen(query,user_id,render_text("username"),cancel_keyboard()); return
    if data == "custom":
        session=sessions.get(user_id,{})
        if not session.get("currency_id") or session.get("flow")!="classic":
            await query.answer("Custom amounts are available for classic exchanges only.",show_alert=True); return
        session["waiting"]="custom_amount"
        await edit_screen(query,user_id,render_text("custom"),cancel_keyboard()); return

    # ------------------------------
    # Admin security gate
    # ------------------------------
    if data.startswith(ADMIN_PREFIXES) or data.startswith("p2p_fee_") or data.startswith("admin_p2p") or data.startswith("p2p_trade_status"):
        if not is_admin(user):
            await query.answer("⛔ Access denied.",show_alert=True); return

    # Admin home / existing panels.
    if data == "admin":
        await edit_screen(query,user_id,"⚙️ <b>ADMIN PANEL</b>\n\nManage the shop, classic exchanges and P2P market from your phone.",admin_keyboard()); return
    if data == "admin_p2p":
        await show_p2p_admin(query); return
    if data == "admin_p2p_fees":
        await show_p2p_fee_tiers(query); return
    if data == "p2p_fee_add":
        await p2p_fee_add_start(query); return
    if data.startswith("p2p_fee_edit:"):
        await show_p2p_fee_edit(query,int(data.split(":",1)[1])); return
    if data.startswith("p2p_fee_type:"):
        await show_p2p_fee_type(query,int(data.split(":",1)[1])); return
    if data.startswith("p2p_fee_type_new:"):
        typ=data.split(":",1)[1]
        session=sessions.get(user_id,{})
        if session.get("action")!="p2p_fee_type_new":
            await query.answer("Please start the fee rule again.",show_alert=True); return
        session["fee_type"]=typ; session["action"]="p2p_fee_value_new"
        await edit_screen(query,user_id,"💸 <b>FEE VALUE</b>\n\nSend the value. For example <code>5</code> for 5%.")
        return
    if data.startswith("p2p_fee_type_set:"):
        _,tid,typ=data.split(":"); conn=db(); conn.execute("UPDATE p2p_fee_tiers SET fee_type=? WHERE id=?",(typ,int(tid))); conn.commit(); conn.close(); await show_p2p_fee_edit(query,int(tid)); return
    if data.startswith("p2p_fee_toggle:"):
        tid=int(data.split(":",1)[1]); conn=db(); conn.execute("UPDATE p2p_fee_tiers SET enabled=CASE WHEN enabled=1 THEN 0 ELSE 1 END WHERE id=?",(tid,)); conn.commit(); conn.close(); await show_p2p_fee_edit(query,tid); return
    if data.startswith("p2p_fee_delete:"):
        tid=int(data.split(":",1)[1]); conn=db(); conn.execute("DELETE FROM p2p_fee_tiers WHERE id=?",(tid,)); conn.commit(); conn.close(); await show_p2p_fee_tiers(query); return
    if data.startswith("p2p_fee_min_edit:"):
        await ask_text_input(query,"p2p_fee_min_edit","🔢 <b>MINIMUM QUANTITY</b>\n\nSend the minimum offered quantity.",tier_id=int(data.split(":",1)[1])); return
    if data.startswith("p2p_fee_max_edit:"):
        await ask_text_input(query,"p2p_fee_max_edit","🔢 <b>MAXIMUM QUANTITY</b>\n\nSend the maximum offered quantity, or <code>-</code> for unlimited.",tier_id=int(data.split(":",1)[1])); return
    if data.startswith("p2p_fee_value:"):
        await ask_text_input(query,"p2p_fee_value","💸 <b>FEE VALUE</b>\n\nSend the fee value. Example: <code>5</code>",tier_id=int(data.split(":",1)[1])); return
    if data == "admin_p2p_offers":
        await show_admin_p2p_offers(query); return
    if data.startswith("admin_p2p_offer:"):
        await show_admin_p2p_offer(query,int(data.split(":",1)[1])); return
    if data.startswith("admin_p2p_cancel_offer:"):
        oid=int(data.split(":",1)[1]); conn=db(); conn.execute("UPDATE p2p_offers SET status=?,updated_at=? WHERE id=? AND status=?",(P2P_CANCELLED,datetime.now().strftime("%d.%m.%Y %H:%M:%S"),oid,P2P_OPEN)); conn.commit(); conn.close(); await show_admin_p2p_offers(query); return
    if data == "admin_p2p_trades":
        await show_admin_p2p_trades(query); return
    if data.startswith("admin_p2p_trade:"):
        await show_admin_p2p_trade(query,int(data.split(":",1)[1])); return
    if data.startswith("p2p_trade_status:"):
        _,status,tid=data.split(":",2); tid=int(tid)
        conn=db(); trade=conn.execute("SELECT * FROM p2p_trades WHERE id=?",(tid,)).fetchone()
        if not trade:
            conn.close(); await query.answer("Trade not found.",show_alert=True); return
        now=datetime.now().strftime("%d.%m.%Y %H:%M:%S")
        if status=="completed":
            conn.execute("UPDATE p2p_trades SET status=?,updated_at=? WHERE id=? AND status='awaiting_confirmation'",(P2P_COMPLETED,now,tid))
            conn.execute("UPDATE p2p_offers SET status=?,updated_at=? WHERE id=?",(P2P_COMPLETED,now,trade["offer_id"]))
        else:
            conn.execute("UPDATE p2p_trades SET status=?,updated_at=? WHERE id=? AND status='awaiting_confirmation'",(P2P_REJECTED,now,tid))
            conn.execute("UPDATE p2p_offers SET status=?,buyer_id=NULL,buyer_username='',buyer_name='',updated_at=? WHERE id=?",(P2P_OPEN,now,trade["offer_id"]))
        conn.commit(); conn.close()
        if status=="completed":
            buyer_msg=f"✅ <b>P2P TRADE CONFIRMED</b>\n\nTrade: <code>{clean(trade['trade_number'])}</code>\n\nYour P2P trade has been confirmed."
            seller_msg=f"✅ <b>P2P TRADE CONFIRMED</b>\n\nTrade: <code>{clean(trade['trade_number'])}</code>\n\nYour offer trade has been confirmed."
        else:
            buyer_msg=f"❌ <b>P2P TRADE REJECTED</b>\n\nTrade: <code>{clean(trade['trade_number'])}</code>\n\nThe trade was rejected."
            seller_msg=f"🔄 <b>P2P OFFER REOPENED</b>\n\nOffer: <code>{clean(trade['offer_number'])}</code>\n\nThe attempted trade was rejected and your offer is live again."
        try: await context.bot.send_message(chat_id=trade["buyer_id"],text=buyer_msg,parse_mode="HTML")
        except Exception: pass
        try: await context.bot.send_message(chat_id=trade["seller_id"],text=seller_msg,parse_mode="HTML")
        except Exception: pass
        await show_admin_p2p_trade(query,tid); return

    # ------------------------------
    # Enhanced category/subcategory/product admin screens
    # ------------------------------
    if data == "admin_categories":
        await show_admin_categories(query); return
    if data.startswith("edit_category:"):
        await show_edit_category(query,int(data.split(":",1)[1])); return
    if data.startswith("category_products:"):
        await category_product_list(query,int(data.split(":",1)[1])); return
    if data == "add_category":
        await start_add_category(query); return
    if data.startswith("add_subcategory:"):
        await ask_text_input(query,"add_subcategory","➕ <b>ADD SUBCATEGORY</b>\n\nSend the subcategory name.",category_id=int(data.split(":",1)[1])); return
    if data.startswith("edit_subcategory:"):
        await show_edit_subcategory(query,int(data.split(":",1)[1])); return
    if data.startswith("subcategory_products:"):
        await show_admin_subcategory_products(query,int(data.split(":",1)[1])); return
    if data.startswith("rename_subcategory:"):
        await ask_text_input(query,"rename_subcategory","🔤 <b>RENAME SUBCATEGORY</b>\n\nSend the new name.",subcategory_id=int(data.split(":",1)[1])); return
    if data.startswith("subcategory_button:"):
        await ask_text_input(query,"subcategory_button","🔘 <b>SUBCATEGORY BUTTON</b>\n\nSend the new button text.",subcategory_id=int(data.split(":",1)[1])); return
    if data.startswith("subcategory_desc:"):
        await ask_text_input(query,"subcategory_desc","📝 <b>SUBCATEGORY DESCRIPTION</b>\n\nSend a description, or <code>-</code> for none.",subcategory_id=int(data.split(":",1)[1])); return
    if data.startswith("subcategory_mode:"):
        sid=int(data.split(":",1)[1]); s=get_subcategory(sid); modes=[MODE_INHERIT,MODE_CLASSIC,MODE_P2P]; new=modes[(modes.index(s["mode"]) + 1)%len(modes)] if s else MODE_INHERIT
        conn=db(); conn.execute("UPDATE subcategories SET mode=? WHERE id=?",(new,sid)); conn.commit(); conn.close(); await show_edit_subcategory(query,sid); return
    if data.startswith("toggle_subcategory:"):
        sid=int(data.split(":",1)[1]); conn=db(); conn.execute("UPDATE subcategories SET enabled=CASE WHEN enabled=1 THEN 0 ELSE 1 END WHERE id=?",(sid,)); conn.commit(); conn.close(); await show_edit_subcategory(query,sid); return
    if data.startswith("delete_subcategory:"):
        sid=int(data.split(":",1)[1]); count=len([p for p in get_products_ctx(get_subcategory(sid)["category_id"],sid,False)]) if get_subcategory(sid) else 0
        if count:
            await query.answer("Move or delete its products first.",show_alert=True); return
        conn=db(); conn.execute("DELETE FROM subcategories WHERE id=?",(sid,)); conn.commit(); conn.close(); await show_admin_categories(query); return
    if data.startswith("category_mode:"):
        cid=int(data.split(":",1)[1]); current=cat_mode(cid); new=MODE_P2P if current==MODE_CLASSIC else MODE_CLASSIC
        conn=db(); conn.execute("UPDATE categories SET mode=? WHERE id=?",(new,cid)); conn.commit(); conn.close(); await show_edit_category(query,cid); return
    if data.startswith("product_mode:"):
        pid=int(data.split(":",1)[1]); p=get_product(pid); modes=[MODE_INHERIT,MODE_CLASSIC,MODE_P2P]; new=modes[(modes.index(p["mode"])+1)%3] if p else MODE_INHERIT
        conn=db(); conn.execute("UPDATE products SET mode=? WHERE id=?",(new,pid)); conn.commit(); conn.close(); await show_edit_product(query,pid); return
    if data.startswith("add_product_root:"):
        await start_add_product_context(query,int(data.split(":",1)[1]),None); return
    if data.startswith("add_product_sub:"):
        sid=int(data.split(":",1)[1]); s=get_subcategory(sid)
        if not s: await query.answer("Subcategory not found.",show_alert=True); return
        await start_add_product_context(query,s["category_id"],sid); return
    if data.startswith("move_product:"):
        await show_move_product(query,int(data.split(":",1)[1])); return
    if data.startswith("move_product_to:"):
        _,pid,kind,owner=data.split(":",3); pid=int(pid); owner=int(owner) if owner!="root" else None
        conn=db(); conn.execute("UPDATE products SET subcategory_id=? WHERE id=?",(owner,pid)); conn.commit(); conn.close(); await show_edit_product(query,pid); return

    # Existing legacy admin callbacks remain supported.
    await _LEGACY_CALLBACK_HANDLER(update,context)

# ============================================================
# ENHANCED ADMIN CATEGORY / SUBCATEGORY / PRODUCT SCREENS
# ============================================================

async def show_admin_categories(query):
    rows=[[InlineKeyboardButton("➕ Add Category",callback_data="add_category")]]
    for c in get_categories(False):
        status="🟢" if c["enabled"] else "🔴"
        rows.append([InlineKeyboardButton(f"{status} {c['button_text']} · {mode_label(cat_mode(c['id']))}",callback_data=f"edit_category:{c['id']}")])
    rows.append([InlineKeyboardButton("↩️ Admin Panel",callback_data="admin")])
    await edit_screen(query,query.from_user.id,
        "🗂️ <b>CATEGORIES</b>\n\n"
        "Categories can contain subcategories and products.\n"
        "A category can run as Classic or P2P.",InlineKeyboardMarkup(rows))


def category_admin_keyboard(category_id):
    c=get_category(category_id)
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔤 Name",callback_data=f"rename_category:{category_id}"),InlineKeyboardButton("🔘 Button",callback_data=f"category_button:{category_id}")],
        [InlineKeyboardButton("📝 Description",callback_data=f"category_desc:{category_id}"),InlineKeyboardButton(f"🔄 Mode: {mode_label(cat_mode(category_id))}",callback_data=f"category_mode:{category_id}")],
        [InlineKeyboardButton("📁 Subcategories / Products",callback_data=f"category_products:{category_id}")],
        [InlineKeyboardButton("🟢 / 🔴 Enable / Disable",callback_data=f"toggle_category:{category_id}")],
        [InlineKeyboardButton("⬆️ Up",callback_data=f"category_up:{category_id}"),InlineKeyboardButton("⬇️ Down",callback_data=f"category_down:{category_id}")],
        [InlineKeyboardButton("🗑️ Delete Category",callback_data=f"delete_category:{category_id}")],
        [InlineKeyboardButton("↩️ Categories",callback_data="admin_categories")]
    ])


async def show_edit_category(query,category_id):
    c=get_category(category_id)
    if not c:
        await query.answer("Category not found.",show_alert=True); return
    subs=get_subcategories(category_id,False)
    products=get_products_ctx(category_id,None,False)
    await edit_screen(query,query.from_user.id,
        "🗂️ <b>EDIT CATEGORY</b>\n\n"
        f"🏷️ Name: <b>{clean(c['name'])}</b>\n"
        f"📌 Mode: <b>{mode_label(cat_mode(category_id))}</b>\n"
        f"📁 Subcategories: <b>{len(subs)}</b>\n"
        f"📦 Root products: <b>{len(products)}</b>\n"
        f"📌 Status: <b>{'Enabled' if c['enabled'] else 'Disabled'}</b>",category_admin_keyboard(category_id))


def subcategory_admin_keyboard(sub_id):
    s=get_subcategory(sub_id)
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔤 Name",callback_data=f"rename_subcategory:{sub_id}"),InlineKeyboardButton("🔘 Button",callback_data=f"subcategory_button:{sub_id}")],
        [InlineKeyboardButton("📝 Description",callback_data=f"subcategory_desc:{sub_id}")],
        [InlineKeyboardButton(f"🔄 Mode: {mode_label(s['mode'])}",callback_data=f"subcategory_mode:{sub_id}")],
        [InlineKeyboardButton("📦 Products",callback_data=f"subcategory_products:{sub_id}")],
        [InlineKeyboardButton("🟢 / 🔴 Enable / Disable",callback_data=f"toggle_subcategory:{sub_id}")],
        [InlineKeyboardButton("🗑️ Delete",callback_data=f"delete_subcategory:{sub_id}")],
        [InlineKeyboardButton("↩️ Category",callback_data=f"edit_category:{s['category_id']}")]
    ])


async def show_edit_subcategory(query,sub_id):
    s=get_subcategory(sub_id)
    if not s:
        await query.answer("Subcategory not found.",show_alert=True); return
    products=get_products_ctx(s["category_id"],sub_id,False)
    await edit_screen(query,query.from_user.id,
        "🗂️ <b>EDIT SUBCATEGORY</b>\n\n"
        f"🏷️ Name: <b>{clean(s['name'])}</b>\n"
        f"📌 Mode: <b>{mode_label(s['mode'])}</b>\n"
        f"📦 Products: <b>{len(products)}</b>\n"
        f"📌 Status: <b>{'Enabled' if s['enabled'] else 'Disabled'}</b>",subcategory_admin_keyboard(sub_id))


async def category_product_list(query,category_id):
    c=get_category(category_id)
    if not c:
        await query.answer("Category not found.",show_alert=True); return
    rows=[
        [InlineKeyboardButton("➕ Add Subcategory",callback_data=f"add_subcategory:{category_id}")],
        [InlineKeyboardButton("➕ Add Product (root)",callback_data=f"add_product_root:{category_id}")]
    ]
    for s in get_subcategories(category_id,False):
        status="🟢" if s["enabled"] else "🔴"
        rows.append([InlineKeyboardButton(f"{status} 📁 {s['button_text']} · {mode_label(sub_mode(s['id']))}",callback_data=f"edit_subcategory:{s['id']}")])
    for p in get_products_ctx(category_id,None,False):
        status="🟢" if p["enabled"] else "🔴"
        rows.append([InlineKeyboardButton(f"{status} 📦 {p['button_text']} · {mode_label(p['mode'])}",callback_data=f"edit_product:{p['id']}")])
    rows.append([InlineKeyboardButton("↩️ Category",callback_data=f"edit_category:{category_id}")])
    await edit_screen(query,query.from_user.id,
        f"📁 <b>{clean(c['name'])}</b>\n\nManage subcategories and root products.",InlineKeyboardMarkup(rows))


async def show_admin_subcategory_products(query,sub_id):
    s=get_subcategory(sub_id)
    if not s:
        await query.answer("Subcategory not found.",show_alert=True); return
    rows=[[InlineKeyboardButton("➕ Add Product",callback_data=f"add_product_sub:{sub_id}")]]
    for p in get_products_ctx(s["category_id"],sub_id,False):
        status="🟢" if p["enabled"] else "🔴"
        rows.append([InlineKeyboardButton(f"{status} 📦 {p['button_text']} · {mode_label(p['mode'])}",callback_data=f"edit_product:{p['id']}")])
    rows.append([InlineKeyboardButton("↩️ Subcategory",callback_data=f"edit_subcategory:{sub_id}")])
    await edit_screen(query,query.from_user.id,
        f"📦 <b>{clean(s['name'])}</b>\n\nManage its products.",InlineKeyboardMarkup(rows))


async def start_add_product_context(query,category_id,subcategory_id=None):
    start_admin_action(query.from_user.id,"add_product_name",category_id=category_id,subcategory_id=subcategory_id)
    parent="subcategory" if subcategory_id else "category"
    await edit_screen(query,query.from_user.id,
        "➕ <b>ADD PRODUCT</b>\n\nSend the product name.\nExample: <code>2,000 Robux</code>\n\n"
        f"Parent: <b>{parent}</b>")


async def show_move_product(query,product_id):
    p=get_product(product_id)
    if not p:
        await query.answer("Product not found.",show_alert=True); return
    rows=[]
    for c in get_categories(False):
        rows.append([InlineKeyboardButton(f"🗂️ {c['name']} · root",callback_data=f"move_product_to:{product_id}:root:root")])
        for s in get_subcategories(c["id"],False):
            rows.append([InlineKeyboardButton(f"↳ {c['name']} / {s['name']}",callback_data=f"move_product_to:{product_id}:sub:{s['id']}")])
    rows.append([InlineKeyboardButton("↩️ Product",callback_data=f"edit_product:{product_id}")])
    await edit_screen(query,query.from_user.id,"🗂️ <b>MOVE PRODUCT</b>\n\nChoose its new location.",InlineKeyboardMarkup(rows))


async def show_edit_product(query,product_id):
    p=get_product(product_id)
    if not p:
        await query.answer("Product not found.",show_alert=True); return
    c=get_category(p["category_id"]) if p["category_id"] else None
    s=get_subcategory(p["subcategory_id"]) if p["subcategory_id"] else None
    await edit_screen(query,query.from_user.id,
        "📦 <b>EDIT PRODUCT</b>\n\n"
        f"🏷️ Name: <b>{clean(p['name'])}</b>\n"
        f"🔘 Button: <b>{clean(p['button_text'])}</b>\n"
        f"🔢 Amount: <b>{p['amount']:,} Robux</b>\n"
        f"🗂️ Category: <b>{clean(c['name']) if c else 'None'}</b>\n"
        f"📁 Subcategory: <b>{clean(s['name']) if s else 'Root'}</b>\n"
        f"🔄 Mode: <b>{mode_label(p['mode'])}</b>\n"
        f"📌 Status: <b>{'Enabled' if p['enabled'] else 'Disabled'}</b>",
        InlineKeyboardMarkup([
            [InlineKeyboardButton("🔤 Name",callback_data=f"rename_product:{product_id}"),InlineKeyboardButton("🔘 Button",callback_data=f"product_button:{product_id}")],
            [InlineKeyboardButton("🔢 Amount",callback_data=f"amount_product:{product_id}"),InlineKeyboardButton("📝 Description",callback_data=f"product_desc:{product_id}")],
            [InlineKeyboardButton(f"🔄 Mode: {mode_label(p['mode'])}",callback_data=f"product_mode:{product_id}")],
            [InlineKeyboardButton("🗂️ Move",callback_data=f"move_product:{product_id}"),InlineKeyboardButton("💰 Prices",callback_data=f"product_prices:{product_id}")],
            [InlineKeyboardButton("🟢 / 🔴 Enable / Disable",callback_data=f"toggle_product:{product_id}")],
            [InlineKeyboardButton("🗑️ Delete Product",callback_data=f"delete_product:{product_id}")],
            [InlineKeyboardButton("↩️ Products",callback_data=f"category_products:{p['category_id']}")]
        ]))


# ============================================================
# ADMIN INPUT OVERRIDE
# ============================================================

# Capture the original admin input handler before overriding it.
_LEGACY_HANDLE_ADMIN_INPUT = handle_admin_input

async def handle_admin_input(update,context,session,text):
    user=update.effective_user; uid=user.id; action=session.get("action")

    if action=="add_subcategory":
        category_id=session["category_id"]
        try:
            conn=db(); n=conn.execute("SELECT COALESCE(MAX(sort_order),0) AS n FROM subcategories WHERE category_id=?",(category_id,)).fetchone()["n"]
            conn.execute("INSERT INTO subcategories(category_id,name,button_text,description,mode,enabled,sort_order) VALUES(?,?,?,'',?,1,?)",(category_id,text,text,MODE_INHERIT,n+1)); conn.commit(); conn.close()
        except sqlite3.IntegrityError:
            await remember_screen(context.bot,update.effective_chat.id,uid,"⚠️ A subcategory with that name already exists."); return
        sessions.pop(uid,None); await remember_screen(context.bot,update.effective_chat.id,uid,"✅ <b>SUBCATEGORY CREATED</b>",InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Category",callback_data=f"category_products:{category_id}")]])); return

    if action=="rename_subcategory":
        sid=session["subcategory_id"]
        try:
            conn=db(); conn.execute("UPDATE subcategories SET name=? WHERE id=?",(text,sid)); conn.commit(); conn.close()
        except sqlite3.IntegrityError:
            await remember_screen(context.bot,update.effective_chat.id,uid,"⚠️ That subcategory name already exists."); return
        sessions.pop(uid,None); await remember_screen(context.bot,update.effective_chat.id,uid,"✅ <b>SUBCATEGORY UPDATED</b>",InlineKeyboardMarkup([[InlineKeyboardButton("📁 Subcategory",callback_data=f"edit_subcategory:{sid}")]])); return

    if action in {"subcategory_button","subcategory_desc"}:
        sid=session["subcategory_id"]; field="button_text" if action=="subcategory_button" else "description"; value="" if action=="subcategory_desc" and text=="-" else text
        conn=db(); conn.execute(f"UPDATE subcategories SET {field}=? WHERE id=?",(value,sid)); conn.commit(); conn.close(); sessions.pop(uid,None)
        await remember_screen(context.bot,update.effective_chat.id,uid,"✅ <b>SUBCATEGORY UPDATED</b>",InlineKeyboardMarkup([[InlineKeyboardButton("📁 Subcategory",callback_data=f"edit_subcategory:{sid}")]])); return

    if action in {"add_product_name","add_product_amount"} and "subcategory_id" in session:
        if action=="add_product_name":
            session["product_name"]=text; session["action"]="add_product_amount"
            await remember_screen(context.bot,update.effective_chat.id,uid,"🔢 <b>PRODUCT AMOUNT</b>\n\nSend the amount of Robux.\nExample: <code>2000</code>"); return
        cleaned=text.replace(",","").replace(" ","")
        if not cleaned.isdigit() or int(cleaned)<=0:
            await remember_screen(context.bot,update.effective_chat.id,uid,"⚠️ Enter a positive whole number."); return
        amount=int(cleaned); cat=session["category_id"]; sub=session.get("subcategory_id")
        conn=db(); n=conn.execute("SELECT COALESCE(MAX(sort_order),0) AS n FROM products WHERE category_id=? AND ((subcategory_id IS NULL AND ? IS NULL) OR subcategory_id=?)",(cat,sub,sub)).fetchone()["n"]
        cur=conn.execute("INSERT INTO products(name,button_text,amount,category_id,description,subcategory_id,mode,enabled,sort_order) VALUES(?,?,?,?,'',?, ?,1,?)",(session["product_name"],session["product_name"],amount,cat,sub,MODE_INHERIT,n+1)); pid=cur.lastrowid
        for c in conn.execute("SELECT id FROM currencies").fetchall(): conn.execute("INSERT OR IGNORE INTO prices(currency_id,product_id,price) VALUES(?,?,'NA')",(c["id"],pid))
        conn.commit(); conn.close(); sessions.pop(uid,None)
        back=f"subcategory_products:{sub}" if sub else f"category_products:{cat}"
        await remember_screen(context.bot,update.effective_chat.id,uid,"✅ <b>PRODUCT CREATED</b>\n\n"+clean(session["product_name"]),InlineKeyboardMarkup([[InlineKeyboardButton("📦 Products",callback_data=back)], [InlineKeyboardButton("⚙️ Admin Panel",callback_data="admin")]])); return

    if action.startswith("p2p_fee_"):
        if action=="p2p_fee_min":
            cleaned=text.replace(",","").replace(" ","")
            if not cleaned.isdigit() or int(cleaned)<=0:
                await remember_screen(context.bot,update.effective_chat.id,uid,"⚠️ Enter a positive whole number."); return
            session["min_quantity"]=int(cleaned); session["action"]="p2p_fee_max"
            await remember_screen(context.bot,update.effective_chat.id,uid,"🔢 <b>MAXIMUM QUANTITY</b>\n\nSend the maximum quantity, or <code>-</code> for unlimited."); return
        if action=="p2p_fee_max":
            cleaned=text.replace(",","").replace(" ","")
            if cleaned=="-": session["max_quantity"]=None
            elif cleaned.isdigit() and int(cleaned)>=session["min_quantity"]: session["max_quantity"]=int(cleaned)
            else: await remember_screen(context.bot,update.effective_chat.id,uid,"⚠️ Invalid maximum quantity."); return
            session["action"]="p2p_fee_type_new"; await edit_screen_from_context(context,update,"📐 <b>FEE TYPE</b>\n\nChoose percentage or fixed amount.",uid,InlineKeyboardMarkup([[InlineKeyboardButton("Percentage %",callback_data="p2p_fee_type_new:percent")],[InlineKeyboardButton("Fixed amount",callback_data="p2p_fee_type_new:fixed")]])); return
        if action=="p2p_fee_value_new":
            val=dec(text)
            if val<0:
                await remember_screen(context.bot,update.effective_chat.id,uid,"⚠️ Fee cannot be negative."); return
            try:
                conn=db(); n=conn.execute("SELECT COALESCE(MAX(sort_order),0) AS n FROM p2p_fee_tiers").fetchone()["n"]
                conn.execute("INSERT INTO p2p_fee_tiers(min_quantity,max_quantity,fee_type,fee_value,enabled,sort_order) VALUES(?,?,?,?,1,?)",(session["min_quantity"],session.get("max_quantity"),session["fee_type"],fmt_dec(val),n+1)); conn.commit(); conn.close()
            except Exception:
                try: conn.close()
                except Exception: pass
                await remember_screen(context.bot,update.effective_chat.id,uid,"⚠️ Could not create that fee rule."); return
            sessions.pop(uid,None); await remember_screen(context.bot,update.effective_chat.id,uid,"✅ <b>FEE RULE CREATED</b>",InlineKeyboardMarkup([[InlineKeyboardButton("💸 Fee Rules",callback_data="admin_p2p_fees")]])); return
        if action=="p2p_fee_value":
            val=dec(text)
            if val<0: await remember_screen(context.bot,update.effective_chat.id,uid,"⚠️ Fee cannot be negative."); return
            conn=db(); conn.execute("UPDATE p2p_fee_tiers SET fee_value=? WHERE id=?",(fmt_dec(val),session["tier_id"])); conn.commit(); conn.close(); tid=session["tier_id"]; sessions.pop(uid,None); await remember_screen(context.bot,update.effective_chat.id,uid,"✅ <b>FEE UPDATED</b>",InlineKeyboardMarkup([[InlineKeyboardButton("💸 Fee Rules",callback_data="admin_p2p_fees")]])); return
        if action=="p2p_fee_min_edit":
            cleaned=text.replace(",","").replace(" ","")
            if not cleaned.isdigit() or int(cleaned)<=0: await remember_screen(context.bot,update.effective_chat.id,uid,"⚠️ Invalid minimum."); return
            tid=session["tier_id"]; conn=db(); conn.execute("UPDATE p2p_fee_tiers SET min_quantity=? WHERE id=?",(int(cleaned),tid)); conn.commit(); conn.close(); sessions.pop(uid,None); await remember_screen(context.bot,update.effective_chat.id,uid,"✅ <b>MINIMUM UPDATED</b>",InlineKeyboardMarkup([[InlineKeyboardButton("💸 Fee Rule",callback_data=f"p2p_fee_edit:{tid}")]])); return
        if action=="p2p_fee_max_edit":
            tid=session["tier_id"]; cleaned=text.replace(",","").replace(" ",""); maxq=None if cleaned=="-" else (int(cleaned) if cleaned.isdigit() else -1); 
            if maxq==-1: await remember_screen(context.bot,update.effective_chat.id,uid,"⚠️ Invalid maximum."); return
            current=conn=db(); minq=current.execute("SELECT min_quantity FROM p2p_fee_tiers WHERE id=?",(tid,)).fetchone()["min_quantity"]; 
            if maxq is not None and maxq<minq: current.close(); await remember_screen(context.bot,update.effective_chat.id,uid,"⚠️ Maximum must be at least the minimum."); return
            current.execute("UPDATE p2p_fee_tiers SET max_quantity=? WHERE id=?",(maxq,tid)); current.commit(); current.close(); sessions.pop(uid,None); await remember_screen(context.bot,update.effective_chat.id,uid,"✅ <b>MAXIMUM UPDATED</b>",InlineKeyboardMarkup([[InlineKeyboardButton("💸 Fee Rule",callback_data=f"p2p_fee_edit:{tid}")]])); return

    await _LEGACY_HANDLE_ADMIN_INPUT(update,context,session,text)


async def edit_screen_from_context(context,update,text,user_id,reply_markup):
    await remember_screen(context.bot,update.effective_chat.id,user_id,text,reply_markup)


# Replace the duplicated legacy customer text path with a P2P-aware wrapper.
async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    user=update.effective_user; uid=user.id; text=update.message.text.strip(); session=sessions.get(uid)
    await clear_user_message(update)
    if text.lower()=="/cancel":
        sessions.pop(uid,None); await remember_screen(context.bot,update.effective_chat.id,uid,render_text("cancelled"),main_keyboard()); return
    if is_admin(user) and session and session.get("action"):
        await handle_admin_input(update,context,session,text); return
    if not session:
        await remember_screen(context.bot,update.effective_chat.id,uid,render_text("no_session"),main_keyboard()); return

    waiting=session.get("waiting")
    if waiting=="p2p_offer_amount":
        cleaned=text.replace(",","").replace(" ","")
        if not cleaned.isdigit() or int(cleaned)<=0 or int(cleaned)>1000000000:
            await remember_screen(context.bot,update.effective_chat.id,uid,"⚠️ Send a valid positive whole-number amount.",cancel_keyboard()); return
        session["p2p_source_amount"]=int(cleaned); session["waiting"]="p2p_target"
        await remember_screen(context.bot,update.effective_chat.id,uid,render_text("p2p_offer_target"),target_keyboard("p2p_product:"+str(session["product_id"]),True)); return
    if waiting=="p2p_target_amount":
        val=dec(text)
        if val<=0:
            await remember_screen(context.bot,update.effective_chat.id,uid,"⚠️ Send a positive number.",cancel_keyboard()); return
        session["p2p_target_amount"]=str(val); session["waiting"]="p2p_preview"
        # Reuse a screen-only preview helper by building the text here.
        fee=fee_for(session["p2p_source_amount"],str(val))["amount"]; total=val+fee
        await remember_screen(context.bot,update.effective_chat.id,uid,render_text("p2p_preview",product=session["product"],source_amount=f"{session['p2p_source_amount']:,}",target_amount=fmt_dec(val),target_name=session["p2p_target_name"],fee_amount=fmt_dec(fee),buyer_total=fmt_dec(total)),InlineKeyboardMarkup([[InlineKeyboardButton("✅ Publish Offer",callback_data="p2p_publish_confirm")],[InlineKeyboardButton(get_button("cancel","❌ Cancel"),callback_data="p2p_market")]])); return

    # Classic custom amount and username are kept compatible with the old shop.
    if waiting=="custom_amount" and session.get("flow")=="classic":
        cleaned=text.replace(",","").replace(" ","")
        max_amount=int(get_setting("max_custom_amount","1000000"))
        if not cleaned.isdigit() or int(cleaned)<=0 or int(cleaned)>max_amount:
            await remember_screen(context.bot,update.effective_chat.id,uid,render_text("invalid_amount",max_custom_amount=max_amount),cancel_keyboard()); return
        session["amount"]=int(cleaned); session["product"]="Custom Amount"; session["price"]=get_custom_price(session["currency_id"]); session["waiting"]="username"
        await remember_screen(context.bot,update.effective_chat.id,uid,render_text("username"),cancel_keyboard()); return
    if waiting=="username" and session.get("flow")=="classic":
        username=text.lstrip("@")
        if len(username)<3 or len(username)>20 or not all(c.isalnum() or c=="_" for c in username):
            await remember_screen(context.bot,update.effective_chat.id,uid,render_text("invalid_username"),cancel_keyboard()); return
        try:
            result=await create_order(update,context,session,username)
        except Exception:
            logger.exception("Classic order creation failed"); result=None
        if not result:
            await remember_screen(context.bot,update.effective_chat.id,uid,render_text("order_error"),main_keyboard()); return
        sessions.pop(uid,None)
        await remember_screen(context.bot,update.effective_chat.id,uid,render_text("confirmation",order_number=result["order_number"]),InlineKeyboardMarkup([[InlineKeyboardButton(get_button("new_order","🔄 New Exchange"),callback_data="exchange")],[InlineKeyboardButton(get_button("home","🏠 Main Menu"),callback_data="home")]])); return

    if is_admin(user) and session and session.get("action"):
        await _LEGACY_HANDLE_ADMIN_INPUT(update,context,session,text)
        return
    await remember_screen(context.bot,update.effective_chat.id,uid,render_text("no_session"),main_keyboard())

# ============================================================
# FINAL ADMIN MENU OVERRIDE
# ============================================================

def admin_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📦 Products",callback_data="admin_products"),InlineKeyboardButton("🗂️ Categories",callback_data="admin_categories")],
        [InlineKeyboardButton("💱 Exchange Targets",callback_data="admin_currencies"),InlineKeyboardButton("💰 Prices",callback_data="admin_prices")],
        [InlineKeyboardButton("🤝 P2P Market",callback_data="admin_p2p"),InlineKeyboardButton("📝 Texts / Steps",callback_data="admin_texts")],
        [InlineKeyboardButton("🔘 Buttons",callback_data="admin_buttons"),InlineKeyboardButton("🏪 Shop Settings",callback_data="admin_settings")],
        [InlineKeyboardButton("👮 Admins",callback_data="admin_admins"),InlineKeyboardButton("📋 Orders",callback_data="admin_orders")],
        [InlineKeyboardButton("🏠 Shop Preview",callback_data="home")],
    ])


# ============================================================
# PATCHES FOR DATABASE / P2P REFERENCES
# ============================================================

# Ensure P2P button values are available through the existing button editor.
# New installs are seeded in init_db(); existing databases get these rows too.
_original_init_db_overlay = init_db

def init_db():
    _original_init_db_overlay()
    conn=db()
    ensure_column(conn, "p2p_offers", "buyer_id", "INTEGER")
    ensure_column(conn, "p2p_offers", "buyer_username", "TEXT NOT NULL DEFAULT ''")
    ensure_column(conn, "p2p_offers", "buyer_name", "TEXT NOT NULL DEFAULT ''")
    for key,value in {
        "p2p_market":"🤝 P2P Market",
        "p2p_browse":"🔎 Browse Offers",
        "p2p_publish":"➕ Publish Offer",
        "p2p_my_offers":"📋 My Offers",
        "p2p_buy":"🛒 Buy Offer",
        "p2p_confirm":"✅ Publish Offer",
    }.items():
        conn.execute("INSERT OR IGNORE INTO buttons(key,value) VALUES(?,?)",(key,value))
    conn.commit(); conn.close()


# Patch the two places where the P2P schemas use snapshots rather than live names.
# Re-declare these small helpers after the compatibility init wrapper.
def get_p2p_offer_by_code(code, silent=False):
    conn=db(); row=conn.execute("SELECT * FROM p2p_offers WHERE offer_number=?",(code,)).fetchone(); conn.close(); return row


# ============================================================
# SAFE P2P BUY / PUBLISH CODE PATHS
# ============================================================

async def create_p2p_offer(user, session):
    amount=int(session["p2p_source_amount"])
    target_amount=dec(session["p2p_target_amount"])
    if target_amount<=0: raise ValueError("Target amount must be positive")
    code=p2p_code("P2P","p2p_offers","offer_number")
    while get_p2p_offer_by_code(code): code=p2p_code("P2P","p2p_offers","offer_number")
    now=datetime.now().strftime("%d.%m.%Y %H:%M:%S")
    product=get_product(session["product_id"]); category=get_category(session["category_id"])
    sub=get_subcategory(session["subcategory_id"]) if session.get("subcategory_id") else None
    fee=fee_for(amount,str(target_amount))["amount"]
    conn=db()
    conn.execute("""
        INSERT INTO p2p_offers
        (offer_number,seller_id,seller_username,seller_name,category_id,subcategory_id,product_id,
         category_name,subcategory_name,product_name,source_amount,target_currency_id,target_name,target_amount,
         fee_type,fee_value,fee_amount,buyer_total,status,created_at,updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, ?,?)
    """,(
        code,user.id,user.username or "",user.full_name or "",session["category_id"],session.get("subcategory_id"),product["id"],
        category["name"] if category else "",sub["name"] if sub else "",product["name"],amount,session.get("p2p_target_currency_id"),
        session["p2p_target_name"],str(target_amount),"percent",fmt_dec(fee_for(amount,str(target_amount))["value"]),str(fee),str(target_amount+fee),P2P_OPEN,now,now
    ))
    conn.commit(); conn.close()
    return code


async def buy_p2p_offer(query,context,offer_id):
    buyer=query.from_user
    now=datetime.now().strftime("%d.%m.%Y %H:%M:%S")
    offer_for_fee=get_p2p_offer(offer_id)
    if not offer_for_fee or offer_for_fee["status"]!=P2P_OPEN:
        await query.answer("This offer is no longer available.",show_alert=True); return
    if offer_for_fee["seller_id"]==buyer.id:
        await query.answer("You cannot buy your own offer.",show_alert=True); return
    fee_total=fee_for(offer_for_fee["source_amount"],offer_for_fee["target_amount"])["amount"]
    trade_code=p2p_code("TRD","p2p_trades","trade_number")
    conn=db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        updated=conn.execute("""
            UPDATE p2p_offers SET status=?,updated_at=?
            WHERE id=? AND status=?
        """,(P2P_RESERVED,now,offer_id,P2P_OPEN)).rowcount
        if updated!=1:
            conn.rollback(); conn.close(); await query.answer("Someone else already took this offer.",show_alert=True); return
        offer=conn.execute("SELECT * FROM p2p_offers WHERE id=?",(offer_id,)).fetchone()
        # Buyer identity is stored only for admin/trade handling.
        conn.execute("UPDATE p2p_offers SET buyer_id=?,buyer_username=?,buyer_name=? WHERE id=?",(buyer.id,buyer.username or "",buyer.full_name or "",offer_id))
        total=dec(offer["target_amount"])+fee_total
        conn.execute("""
            INSERT INTO p2p_trades
            (trade_number,offer_id,offer_number,seller_id,buyer_id,source_amount,target_name,target_amount,fee_amount,buyer_total,status,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """,(trade_code,offer_id,offer["offer_number"],offer["seller_id"],buyer.id,offer["source_amount"],offer["target_name"],offer["target_amount"],str(fee_total),str(total),"awaiting_confirmation",now,now))
        conn.commit()
    except Exception:
        conn.rollback()
        conn.close()
        raise
    conn.close()

    recipient=get_setting("order_recipient_chat_id","").strip() or BOOTSTRAP_ADMIN_CHAT_ID
    admin_text=(
        "🤝 <b>NEW P2P TRADE</b>\n\n"
        f"🔐 Trade: <code>{trade_code}</code>\n"
        f"🔐 Offer: <code>{offer['offer_number']}</code>\n"
        f"📦 Offer: <b>{clean(offer['product_name'])}</b>\n"
        f"💰 Seller offers: <b>{offer['source_amount']:,}</b>\n"
        f"🎯 Buyer provides: <b>{clean(offer['target_amount'])} {clean(offer['target_name'])}</b>\n"
        f"💸 Fee: <b>{fmt_dec(fee_total)} {clean(offer['target_name'])}</b>\n"
        f"💳 Buyer total: <b>{fmt_dec(total)} {clean(offer['target_name'])}</b>\n\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        f"👤 Seller: <b>{clean(offer['seller_name'])}</b>\n"
        f"📱 @{clean(offer['seller_username']) if offer['seller_username'] else 'No username'}\n"
        f"🆔 Seller ID: <code>{offer['seller_id']}</code>\n\n"
        f"👤 Buyer: <b>{clean(buyer.full_name or '')}</b>\n"
        f"📱 @{clean(buyer.username) if buyer.username else 'No username'}\n"
        f"🆔 Buyer ID: <code>{buyer.id}</code>"
    )
    if recipient:
        try: await context.bot.send_message(chat_id=int(recipient),text=admin_text,parse_mode="HTML")
        except Exception as error: logger.error("P2P admin notification failed: %s",error)

    await edit_screen(query,buyer.id,render_text("p2p_bought",trade_number=trade_code),InlineKeyboardMarkup([
        [InlineKeyboardButton(get_button("p2p_market","🤝 P2P Market"),callback_data="p2p_market")],
        [InlineKeyboardButton(get_button("home","🏠 Main Menu"),callback_data="home")]
    ]))
    try:
        await context.bot.send_message(chat_id=offer["seller_id"],text=render_text("p2p_seller_notice",offer_number=offer["offer_number"],trade_number=trade_code),parse_mode="HTML")
    except Exception: pass

# ============================================================
# P2P FEE SNAPSHOT + EDITABLE P2P TEXT/BUTTON LISTS
# ============================================================

def offer_fee_amount(offer):
    return dec(offer["fee_amount"])


# Store the fee rule used when an offer is published, so later admin changes
# affect new offers without retroactively changing live offers.
async def create_p2p_offer(user, session):
    amount=int(session["p2p_source_amount"])
    target_amount=dec(session["p2p_target_amount"])
    if target_amount<=0: raise ValueError("Target amount must be positive")
    fee_info=fee_for(amount,str(target_amount))
    code=p2p_code("P2P","p2p_offers","offer_number")
    while get_p2p_offer_by_code(code): code=p2p_code("P2P","p2p_offers","offer_number")
    now=datetime.now().strftime("%d.%m.%Y %H:%M:%S")
    product=get_product(session["product_id"]); category=get_category(session["category_id"])
    sub=get_subcategory(session["subcategory_id"]) if session.get("subcategory_id") else None
    conn=db()
    conn.execute("""
        INSERT INTO p2p_offers
        (offer_number,seller_id,seller_username,seller_name,category_id,subcategory_id,product_id,
         category_name,subcategory_name,product_name,source_amount,target_currency_id,target_name,target_amount,
         fee_type,fee_value,fee_amount,buyer_total,status,created_at,updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """,(
        code,user.id,user.username or "",user.full_name or "",session["category_id"],session.get("subcategory_id"),product["id"],
        category["name"] if category else "",sub["name"] if sub else "",product["name"],amount,session.get("p2p_target_currency_id"),
        session["p2p_target_name"],str(target_amount),fee_info["type"],fee_info["value"],str(fee_info["amount"]),str(target_amount+fee_info["amount"]),P2P_OPEN,now,now
    ))
    conn.commit(); conn.close()
    return code


def p2p_text_names():
    names=dict(TEXT_NAMES)
    names.update({
        "p2p_market":"🤝 P2P Market",
        "p2p_offer_amount":"💰 P2P Offer Amount",
        "p2p_offer_target":"🎯 P2P What You Want",
        "p2p_offer_target_amount":"💵 P2P Requested Amount",
        "p2p_preview":"🔎 P2P Preview",
        "p2p_published":"✅ P2P Published",
        "p2p_empty":"🤝 P2P Empty Market",
        "p2p_details":"📋 P2P Offer Details",
        "p2p_buy_confirm":"🛒 P2P Buy Confirmation",
        "p2p_bought":"✅ P2P Purchase Sent",
        "p2p_seller_notice":"🔔 P2P Seller Notice",
        "p2p_my_offers":"📋 P2P My Offers",
        "p2p_cancelled":"✅ P2P Offer Cancelled",
    }); return names


async def show_texts(query):
    rows=[]
    for key,label in p2p_text_names().items():
        rows.append([InlineKeyboardButton(label,callback_data=f"edit_text:{key}")])
    rows.append([InlineKeyboardButton("↩️ Admin Panel",callback_data="admin")])
    await edit_screen(query,query.from_user.id,"📝 <b>TEXTS / STEPS</b>\n\nEvery customer-facing step and P2P message can be edited here.",InlineKeyboardMarkup(rows))


async def show_buttons(query):
    names=dict(BUTTON_NAMES)
    names.update({
        "p2p_market":"🤝 P2P Market",
        "p2p_browse":"🔎 Browse Offers",
        "p2p_publish":"➕ Publish Offer",
        "p2p_my_offers":"📋 My Offers",
        "p2p_buy":"🛒 Buy Offer",
        "p2p_confirm":"✅ Publish Offer",
    })
    rows=[]
    for key,label in names.items(): rows.append([InlineKeyboardButton(f"{label}: {get_button(key)}",callback_data=f"edit_button:{key}")])
    rows.append([InlineKeyboardButton("↩️ Admin Panel",callback_data="admin")])
    await edit_screen(query,query.from_user.id,"🔘 <b>BUTTON EDITOR</b>\n\nRename customer buttons without editing code.",InlineKeyboardMarkup(rows))

# ============================================================
# FINAL P2P SCREEN / TRADE OVERRIDES
# ============================================================

async def show_p2p_offers(query,product_id):
    product=get_product(product_id)
    if not product: await query.answer("Product not found.",show_alert=True); return
    offers=active_p2p_offers(product_id)
    if not offers:
        await edit_screen(query,query.from_user.id,
            "🤝 <b>NO P2P OFFERS</b>\n\nThere are no active offers for this item yet.",
            InlineKeyboardMarkup([
                [InlineKeyboardButton(get_button("p2p_publish","➕ Publish Offer"),callback_data=f"p2p_publish_product:{product_id}")],
                [InlineKeyboardButton(get_button("back","↩️ Back"),callback_data=f"product:{product_id}")],
            ])); return
    rows=[]
    for o in offers:
        fee=offer_fee_amount(o); total=dec(o["buyer_total"])
        rows.append([InlineKeyboardButton(f"{o['source_amount']:,} → {fmt_dec(o['target_amount'])} {o['target_name']} · {fmt_dec(total)} total",callback_data=f"p2p_offer:{o['id']}")])
    rows += [
        [InlineKeyboardButton(get_button("p2p_publish","➕ Publish Offer"),callback_data=f"p2p_publish_product:{product_id}")],
        [InlineKeyboardButton(get_button("back","↩️ Back"),callback_data=f"product:{product_id}")],
    ]
    await edit_screen(query,query.from_user.id,"🤝 <b>P2P OFFERS</b>\n\nSeller identities are hidden from buyers.",InlineKeyboardMarkup(rows))


async def show_p2p_offer(query,offer_id):
    o=get_p2p_offer(offer_id)
    if not o or o["status"]!=P2P_OPEN:
        await query.answer("This offer is no longer available.",show_alert=True); return
    await edit_screen(query,query.from_user.id,render_text("p2p_details",offer_number=o["offer_number"],source_amount=f"{o['source_amount']:,}",product=o["product_name"],target_amount=fmt_dec(o["target_amount"]),target_name=o["target_name"],fee_amount=fmt_dec(offer_fee_amount(o)),buyer_total=fmt_dec(o["buyer_total"])),InlineKeyboardMarkup([
        [InlineKeyboardButton(get_button("p2p_buy","🛒 Buy Offer"),callback_data=f"p2p_buy_confirm:{offer_id}")],
        [InlineKeyboardButton(get_button("back","↩️ Back"),callback_data=f"p2p_offers:{o['product_id']}")],
    ]))


async def show_p2p_buy_confirm(query,offer_id):
    o=get_p2p_offer(offer_id)
    if not o or o["status"]!=P2P_OPEN:
        await query.answer("This offer is no longer available.",show_alert=True); return
    await edit_screen(query,query.from_user.id,render_text("p2p_buy_confirm",offer_number=o["offer_number"],source_amount=f"{o['source_amount']:,}",product=o["product_name"],target_amount=fmt_dec(o["target_amount"]),target_name=o["target_name"],fee_amount=fmt_dec(offer_fee_amount(o)),buyer_total=fmt_dec(o["buyer_total"])),InlineKeyboardMarkup([
        [InlineKeyboardButton(get_button("p2p_buy","✅ Confirm Purchase"),callback_data=f"p2p_buy:{offer_id}")],
        [InlineKeyboardButton(get_button("back","↩️ Back"),callback_data=f"p2p_offer:{offer_id}")],
    ]))


async def buy_p2p_offer(query,context,offer_id):
    buyer=query.from_user; now=datetime.now().strftime("%d.%m.%Y %H:%M:%S")
    trade_code=p2p_code("TRD","p2p_trades","trade_number")
    conn=db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        offer=conn.execute("SELECT * FROM p2p_offers WHERE id=?",(offer_id,)).fetchone()
        if not offer or offer["status"]!=P2P_OPEN:
            conn.rollback(); conn.close(); await query.answer("This offer is no longer available.",show_alert=True); return
        if offer["seller_id"]==buyer.id:
            conn.rollback(); conn.close(); await query.answer("You cannot buy your own offer.",show_alert=True); return
        updated=conn.execute("UPDATE p2p_offers SET status=?,buyer_id=?,buyer_username=?,buyer_name=?,updated_at=? WHERE id=? AND status=?",(P2P_RESERVED,buyer.id,buyer.username or "",buyer.full_name or "",now,offer_id,P2P_OPEN)).rowcount
        if updated!=1:
            conn.rollback(); conn.close(); await query.answer("Someone else already took this offer.",show_alert=True); return
        fee=offer_fee_amount(offer); total=dec(offer["buyer_total"])
        conn.execute("INSERT INTO p2p_trades(trade_number,offer_id,offer_number,seller_id,buyer_id,source_amount,target_name,target_amount,fee_amount,buyer_total,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",(trade_code,offer_id,offer["offer_number"],offer["seller_id"],buyer.id,offer["source_amount"],offer["target_name"],offer["target_amount"],str(fee),str(total),"awaiting_confirmation",now,now))
        conn.commit()
    except Exception:
        conn.rollback(); conn.close(); raise
    conn.close()
    recipient=get_setting("order_recipient_chat_id","").strip() or BOOTSTRAP_ADMIN_CHAT_ID
    if recipient:
        admin_text=(
            "🤝 <b>NEW P2P TRADE</b>\n\n"
            f"🔐 Trade: <code>{trade_code}</code>\n"
            f"🔐 Offer: <code>{offer['offer_number']}</code>\n"
            f"📦 Item: <b>{clean(offer['product_name'])}</b>\n"
            f"💰 Seller offers: <b>{offer['source_amount']:,}</b>\n"
            f"🎯 Buyer provides: <b>{clean(offer['target_amount'])} {clean(offer['target_name'])}</b>\n"
            f"💸 Fee: <b>{fmt_dec(fee)} {clean(offer['target_name'])}</b>\n"
            f"💳 Buyer total: <b>{fmt_dec(total)} {clean(offer['target_name'])}</b>\n\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            f"👤 Seller: <b>{clean(offer['seller_name'])}</b>\n"
            f"📱 @{clean(offer['seller_username']) if offer['seller_username'] else 'No username'}\n"
            f"🆔 Seller ID: <code>{offer['seller_id']}</code>\n\n"
            f"👤 Buyer: <b>{clean(buyer.full_name or '')}</b>\n"
            f"📱 @{clean(buyer.username) if buyer.username else 'No username'}\n"
            f"🆔 Buyer ID: <code>{buyer.id}</code>"
        )
        try: await context.bot.send_message(chat_id=int(recipient),text=admin_text,parse_mode="HTML")
        except Exception as error: logger.error("P2P admin notification failed: %s",error)
    await edit_screen(query,buyer.id,render_text("p2p_bought",trade_number=trade_code),InlineKeyboardMarkup([
        [InlineKeyboardButton(get_button("p2p_market","🤝 P2P Market"),callback_data="p2p_market")],
        [InlineKeyboardButton(get_button("home","🏠 Main Menu"),callback_data="home")]
    ]))
    try:
        await context.bot.send_message(chat_id=offer["seller_id"],text=render_text("p2p_seller_notice",offer_number=offer["offer_number"],trade_number=trade_code),parse_mode="HTML")
    except Exception as error: logger.warning("P2P seller notice failed: %s",error)

# ============================================================
# FINAL HIERARCHY SAFETY / MOVE OVERRIDES
# ============================================================

async def show_move_product(query,product_id):
    p=get_product(product_id)
    if not p:
        await query.answer("Product not found.",show_alert=True); return
    rows=[]
    for c in get_categories(False):
        rows.append([InlineKeyboardButton(f"🗂️ {c['name']} · root",callback_data=f"move_product_to:{product_id}:root:{c['id']}")])
        for s in get_subcategories(c["id"],False):
            rows.append([InlineKeyboardButton(f"↳ {c['name']} / {s['name']}",callback_data=f"move_product_to:{product_id}:sub:{c['id']}:{s['id']}")])
    rows.append([InlineKeyboardButton("↩️ Product",callback_data=f"edit_product:{product_id}")])
    await edit_screen(query,query.from_user.id,"🗂️ <b>MOVE PRODUCT</b>\n\nChoose its new location.",InlineKeyboardMarkup(rows))


_PREVIOUS_CALLBACK_HANDLER = callback_handler

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query=update.callback_query
    data=query.data or ""
    user=query.from_user
    if data.startswith("move_product_to:"):
        if not is_admin(user):
            await query.answer("⛔ Access denied.",show_alert=True); return
        parts=data.split(":")
        if len(parts)==4:
            _,pid,kind,cid=parts; pid=int(pid); cid=int(cid); sid=None
        else:
            _,pid,kind,cid,sid=parts; pid=int(pid); cid=int(cid); sid=int(sid)
        p=get_product(pid)
        c=get_category(cid)
        if not p or not c:
            await query.answer("Destination not found.",show_alert=True); return
        if sid is not None:
            s=get_subcategory(sid)
            if not s or s["category_id"]!=cid:
                await query.answer("Destination not found.",show_alert=True); return
        conn=db(); conn.execute("UPDATE products SET category_id=?,subcategory_id=? WHERE id=?",(cid,sid,pid)); conn.commit(); conn.close()
        await show_edit_product(query,pid); return

    if data.startswith("delete_category:"):
        if not is_admin(user):
            await query.answer("⛔ Access denied.",show_alert=True); return
        cid=int(data.split(":",1)[1]); c=get_category(cid)
        if not c:
            await query.answer("Category not found.",show_alert=True); return
        product_count=0
        for p in get_products_ctx(cid,None,False): product_count+=1
        sub_count=len(get_subcategories(cid,False))
        if product_count or sub_count:
            await query.answer("Remove its products and subcategories first.",show_alert=True); return
        conn=db(); conn.execute("DELETE FROM categories WHERE id=?",(cid,)); conn.commit(); conn.close(); await show_admin_categories(query); return

    await _PREVIOUS_CALLBACK_HANDLER(update,context)

# ============================================================
# EXCHANGE-TARGET ADMIN WORDING (NO CUSTOMER PAYMENT LANGUAGE)
# ============================================================

async def show_admin_currencies(query):
    rows=[[InlineKeyboardButton("➕ Add Exchange Target",callback_data="add_currency")]]
    for c in get_currencies(False):
        status="🟢" if c["enabled"] else "🔴"
        rows.append([InlineKeyboardButton(f"{status} {c['button_text']}",callback_data=f"edit_currency:{c['id']}")])
    rows.append([InlineKeyboardButton("↩️ Admin Panel",callback_data="admin")])
    await edit_screen(query,query.from_user.id,"🎯 <b>EXCHANGE TARGETS</b>\n\nManage what customers can exchange for.",InlineKeyboardMarkup(rows))


def currency_admin_keyboard(currency_id):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔤 Name",callback_data=f"rename_currency:{currency_id}"),InlineKeyboardButton("🔘 Button",callback_data=f"currency_button:{currency_id}")],
        [InlineKeyboardButton("🟢 / 🔴 Enable / Disable",callback_data=f"toggle_currency:{currency_id}")],
        [InlineKeyboardButton("⬆️ Up",callback_data=f"currency_up:{currency_id}"),InlineKeyboardButton("⬇️ Down",callback_data=f"currency_down:{currency_id}")],
        [InlineKeyboardButton("💰 Prices",callback_data=f"prices_currency:{currency_id}")],
        [InlineKeyboardButton("🗑️ Delete Target",callback_data=f"delete_currency:{currency_id}")],
        [InlineKeyboardButton("↩️ Exchange Targets",callback_data="admin_currencies")],
    ])


async def show_edit_currency(query,currency_id):
    c=get_currency(currency_id)
    if not c: await query.answer("Exchange target not found.",show_alert=True); return
    await edit_screen(query,query.from_user.id,"🎯 <b>EDIT EXCHANGE TARGET</b>\n\n"+f"🏷️ Name: <b>{clean(c['name'])}</b>\n"+f"🔘 Button: <b>{clean(c['button_text'])}</b>\n"+f"📌 Status: <b>{'Enabled' if c['enabled'] else 'Disabled'}</b>",currency_admin_keyboard(currency_id))


async def show_admin_prices(query):
    rows=[[InlineKeyboardButton(f"🎯 {c['name']}",callback_data=f"prices_currency:{c['id']}")] for c in get_currencies(False)]
    rows.append([InlineKeyboardButton("↩️ Admin Panel",callback_data="admin")])
    await edit_screen(query,query.from_user.id,"💰 <b>PRICES</b>\n\nSet the price for each product based on what the customer is exchanging for.",InlineKeyboardMarkup(rows))


async def show_currency_prices(query,currency_id):
    c=get_currency(currency_id)
    if not c: await query.answer("Exchange target not found.",show_alert=True); return
    rows=[]
    for p in get_products(None,False):
        rows.append([InlineKeyboardButton(f"{p['button_text']} → {get_price(currency_id,p['id'])}",callback_data=f"set_price:{currency_id}:{p['id']}")])
    rows.append([InlineKeyboardButton(f"✏️ Custom Amount → {get_custom_price(currency_id)}",callback_data=f"set_custom:{currency_id}")])
    rows.append([InlineKeyboardButton("↩️ Exchange Targets",callback_data="admin_currencies")])
    await edit_screen(query,query.from_user.id,f"💰 <b>PRICES · {clean(c['name'])}</b>\n\nTap a price to change it.",InlineKeyboardMarkup(rows))



# ============================================================
# FINAL TEXT / ADMIN TARGET LABEL OVERRIDE
# ============================================================

def p2p_text_names():
    names=dict(TEXT_NAMES)
    names["currency"]="🎯 Exchange Target Step"
    names.update({
        "p2p_market":"🤝 P2P Market",
        "p2p_offer_amount":"💰 P2P Offer Amount",
        "p2p_offer_target":"🎯 P2P What You Want",
        "p2p_offer_target_amount":"💵 P2P Requested Amount",
        "p2p_preview":"🔎 P2P Preview",
        "p2p_published":"✅ P2P Published",
        "p2p_empty":"🤝 P2P Empty Market",
        "p2p_details":"📋 P2P Offer Details",
        "p2p_buy_confirm":"🛒 P2P Buy Confirmation",
        "p2p_bought":"✅ P2P Purchase Sent",
        "p2p_seller_notice":"🔔 P2P Seller Notice",
        "p2p_my_offers":"📋 P2P My Offers",
        "p2p_cancelled":"✅ P2P Offer Cancelled",
    })
    return names


async def add_currency_start(query):
    await ask_text_input(
        query,
        "add_currency_name",
        "➕ <b>ADD EXCHANGE TARGET</b>\n\nSend the name of what customers can exchange for.\n\nExample: <code>USDT</code>",
    )


_PREVIOUS_CALLBACK_HANDLER_2 = callback_handler

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query=update.callback_query
    data=query.data or ""
    user=query.from_user
    if data.startswith("rename_currency:"):
        if not is_admin(user):
            await query.answer("⛔ Access denied.",show_alert=True); return
        await ask_text_input(query,"rename_currency","🔤 <b>EDIT EXCHANGE TARGET NAME</b>\n\nSend the new name.",currency_id=int(data.split(":",1)[1])); return
    if data.startswith("currency_button:"):
        if not is_admin(user):
            await query.answer("⛔ Access denied.",show_alert=True); return
        await ask_text_input(query,"currency_button","🔘 <b>EDIT EXCHANGE TARGET BUTTON</b>\n\nSend the new button text.",currency_id=int(data.split(":",1)[1])); return
    await _PREVIOUS_CALLBACK_HANDLER_2(update,context)


# ============================================================
# ERROR HANDLER
# ============================================================

async def error_handler(update, context):
    logger.error(
        "Unhandled exception:",
        exc_info=context.error,
    )


# ============================================================
# RUN
# ============================================================

def run():
    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN is missing from Railway Variables."
        )

    if BOT_TOKEN == "BOT_TOKEN":
        raise RuntimeError(
            "BOT_TOKEN is still set to the placeholder 'BOT_TOKEN'."
        )

    init_db()
    logger.info("Database initialized.")

    application = (
        Application
        .builder()
        .token(BOT_TOKEN)
        .build()
    )

    application.add_handler(
        CommandHandler("start", start)
    )

    application.add_handler(
        CommandHandler("admin", admin)
    )

    application.add_handler(
        CallbackQueryHandler(callback_handler)
    )

    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            text_handler,
        )
    )

    application.add_error_handler(error_handler)

    logger.info("SRPExchange bot starting...")

    application.run_polling(
        drop_pending_updates=True
    )


if __name__ == "__main__":
    run()

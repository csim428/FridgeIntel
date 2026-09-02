import asyncio
import contextlib
import os
import sqlite3

import flet as ft

# Total number of item slots the fridge can hold.
CAPACITY = 24

# Items that have been saved into the fridge: {"name": str, "qty": int}
items: list[dict] = []

# Items staged on the insert screen but not saved yet. Kept at module level so
# navigating away and back does not throw away an unsaved draft.
pending: list[dict] = []

BG = "#14487f"
CARD_BG = "#0f3560"
DRAFT_BG = "#1d5a9c"
ACCENT = "#7ec8ff"


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


def db_path() -> str:
    """Durable storage dir when packaged; the working directory otherwise."""
    return os.path.join(os.getenv("FLET_APP_STORAGE_DATA", "."), "fridge.db")


@contextlib.contextmanager
def _db():
    """Open the database, commit on success, and always close."""
    con = sqlite3.connect(db_path())
    try:
        with con:
            yield con
    finally:
        con.close()


def init_db() -> None:
    with _db() as con:
        con.execute(
            "CREATE TABLE IF NOT EXISTS items ("
            "  name TEXT PRIMARY KEY,"
            "  qty  INTEGER NOT NULL"
            ")"
        )


def load_items() -> None:
    """Replace the in-memory fridge with whatever is on disk."""
    init_db()
    with _db() as con:
        rows = con.execute("SELECT name, qty FROM items ORDER BY rowid").fetchall()
    items.clear()
    items.extend({"name": name, "qty": qty} for name, qty in rows)


def persist() -> None:
    """Mirror the saved fridge to disk. The unsaved draft is never written."""
    with _db() as con:
        con.execute("DELETE FROM items")
        con.executemany(
            "INSERT INTO items (name, qty) VALUES (?, ?)",
            [(item["name"], item["qty"]) for item in items],
        )


# ---------------------------------------------------------------------------
# Fridge logic (no Flet involved, so it can be exercised on its own)
# ---------------------------------------------------------------------------


def used_slots() -> int:
    """Slots taken up by saved items."""
    return sum(item["qty"] for item in items)


def pending_slots() -> int:
    """Slots reserved by staged, not-yet-saved items."""
    return sum(item["qty"] for item in pending)


def free_slots() -> int:
    """Slots still available once saved and staged items are counted."""
    return CAPACITY - used_slots() - pending_slots()


def _merge_into(target: list[dict], name: str, qty: int) -> None:
    for item in target:
        if item["name"].lower() == name.lower():
            item["qty"] += qty
            return
    target.append({"name": name, "qty": qty})


def stage_item(name: str, qty_text: str) -> str:
    """Validate input and stage it for saving. Returns a status message."""
    name = (name or "").strip()
    if not name:
        return "Enter an item name."
    try:
        qty = int((qty_text or "1").strip())
    except ValueError:
        return "Quantity must be a whole number."
    if qty < 1:
        return "Quantity must be at least 1."
    if qty > free_slots():
        return f"Not enough room - {free_slots()} slot(s) left."

    _merge_into(pending, name, qty)
    return f"Staged {qty} x {name}. Press Save items to commit."


def adjust_pending(item: dict, delta: int) -> str:
    """Nudge a staged item's quantity; drops the row when it reaches zero."""
    if delta > 0 and delta > free_slots():
        return f"Not enough room - {free_slots()} slot(s) left."
    item["qty"] += delta
    if item["qty"] < 1:
        pending.remove(item)
        return f"Removed {item['name']} from the draft."
    return ""


def remove_pending(item: dict) -> str:
    if item in pending:
        pending.remove(item)
        return f"Removed {item['name']} from the draft."
    return ""


def discard_pending() -> str:
    count = len(pending)
    pending.clear()
    return f"Discarded {count} unsaved item(s)." if count else "Nothing to discard."


def save_pending() -> str:
    """Commit every staged item into the fridge."""
    if not pending:
        return "Nothing to save yet."
    saved = pending_slots()
    rows = len(pending)
    for item in list(pending):
        _merge_into(items, item["name"], item["qty"])
    pending.clear()
    persist()
    return f"Saved {saved} item(s) across {rows} row(s)."


def adjust_item(item: dict, delta: int) -> str:
    """Nudge a saved item's quantity; drops the row when it reaches zero."""
    if delta > 0 and delta > free_slots():
        return f"Not enough room - {free_slots()} slot(s) left."
    item["qty"] += delta
    if item["qty"] < 1:
        items.remove(item)
        persist()
        return f"Removed {item['name']} from the fridge."
    persist()
    return ""


def remove_item(item: dict) -> str:
    if item in items:
        items.remove(item)
        persist()
        return f"Removed {item['name']} from the fridge."
    return ""


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------


def main(page: ft.Page):
    page.title = "FridgeIntel"
    page.bgcolor = BG

    load_items()

    def go(route: str):
        return lambda: asyncio.create_task(page.push_route(route))

    def qty_row(item, on_minus, on_plus, on_delete, accent, bgcolor=CARD_BG):
        """Name + quantity stepper + delete, used by both item lists."""
        return ft.Container(
            bgcolor=bgcolor,
            border_radius=8,
            padding=ft.Padding(left=14, right=6, top=6, bottom=6),
            width=460,
            content=ft.Row(
                alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                controls=[
                    ft.Text(
                        item["name"],
                        size=18,
                        color=ft.Colors.WHITE,
                        expand=True,
                        no_wrap=True,
                    ),
                    ft.Row(
                        spacing=0,
                        controls=[
                            ft.IconButton(
                                icon=ft.Icons.REMOVE,
                                icon_color=ft.Colors.WHITE70,
                                tooltip="Decrease quantity",
                                on_click=on_minus,
                            ),
                            ft.Text(
                                f"{item['qty']}",
                                size=18,
                                width=32,
                                color=accent,
                                text_align=ft.TextAlign.CENTER,
                            ),
                            ft.IconButton(
                                icon=ft.Icons.ADD,
                                icon_color=ft.Colors.WHITE70,
                                tooltip="Increase quantity",
                                on_click=on_plus,
                            ),
                            ft.IconButton(
                                icon=ft.Icons.DELETE_OUTLINE,
                                icon_color=ft.Colors.WHITE70,
                                tooltip="Remove item",
                                on_click=on_delete,
                            ),
                        ],
                    ),
                ],
            ),
        )

    # ----- welcome view -------------------------------------------------
    def welcome_view() -> ft.View:
        return ft.View(
            route="/",
            bgcolor=BG,
            controls=[
                ft.SafeArea(
                    expand=True,
                    content=ft.Container(
                        expand=True,
                        alignment=ft.Alignment.CENTER,
                        bgcolor=BG,
                        content=ft.Column(
                            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                            alignment=ft.MainAxisAlignment.CENTER,
                            spacing=16,
                            controls=[
                                ft.Text(
                                    "FridgeIntel",
                                    size=72,
                                    weight=ft.FontWeight.BOLD,
                                    color=ft.Colors.WHITE,
                                    text_align=ft.TextAlign.CENTER,
                                ),
                                ft.Text(
                                    "Welcome!",
                                    size=28,
                                    color=ACCENT,
                                    text_align=ft.TextAlign.CENTER,
                                ),
                                ft.Container(height=24),
                                ft.Button(
                                    content=ft.Text(
                                        "Insert items",
                                        size=28,
                                        width=380,
                                        text_align=ft.TextAlign.CENTER,
                                    ),
                                    on_click=go("/insert"),
                                ),
                                ft.Button(
                                    content=ft.Text(
                                        "View capacity",
                                        size=28,
                                        width=380,
                                        text_align=ft.TextAlign.CENTER,
                                    ),
                                    on_click=go("/capacity"),
                                ),
                            ],
                        ),
                    ),
                )
            ],
        )

    # ----- insert view --------------------------------------------------
    def insert_view() -> ft.View:
        name_field = ft.TextField(
            label="Item name", width=300, autofocus=True, color=ft.Colors.WHITE
        )
        qty_field = ft.TextField(
            label="Qty",
            value="1",
            width=110,
            keyboard_type=ft.KeyboardType.NUMBER,
            color=ft.Colors.WHITE,
        )
        summary = ft.Text("", size=16, color=ft.Colors.WHITE70)
        status = ft.Text("", size=16, color=ACCENT)
        draft_header = ft.Text("", size=20, color=ft.Colors.ORANGE_300)
        draft_list = ft.Column(spacing=8)
        saved_list = ft.Column(spacing=8)
        save_button = ft.Button(
            content=ft.Text("Save items", size=20, width=180,
                            text_align=ft.TextAlign.CENTER),
            icon=ft.Icons.SAVE,
        )
        discard_button = ft.Button(
            content=ft.Text("Discard draft", size=20, width=180,
                            text_align=ft.TextAlign.CENTER),
        )

        def refresh(message: str | None = None):
            """Rebuild both lists in place and repaint."""
            if message is not None:
                status.value = message
            summary.value = (
                f"{used_slots()} saved  |  {pending_slots()} unsaved  |  "
                f"{free_slots()} free of {CAPACITY}"
            )
            draft_header.value = f"Unsaved draft ({len(pending)})"

            if pending:
                draft_list.controls = [
                    qty_row(
                        item,
                        stepper(item, -1, adjust_pending),
                        stepper(item, +1, adjust_pending),
                        deleter(item, remove_pending),
                        ft.Colors.ORANGE_300,
                        DRAFT_BG,
                    )
                    for item in pending
                ]
            else:
                draft_list.controls = [
                    ft.Text(
                        "Nothing staged. Added items land here first.",
                        size=16,
                        color=ft.Colors.WHITE70,
                    )
                ]

            if items:
                saved_list.controls = [
                    qty_row(
                        item,
                        stepper(item, -1, adjust_item),
                        stepper(item, +1, adjust_item),
                        deleter(item, remove_item),
                        ACCENT,
                    )
                    for item in items
                ]
            else:
                saved_list.controls = [
                    ft.Text("The fridge is empty.", size=16, color=ft.Colors.WHITE70)
                ]

            save_button.disabled = not pending
            discard_button.disabled = not pending
            page.update()

        def stepper(item, delta, fn):
            def handler(e=None):
                refresh(fn(item, delta) or None)

            return handler

        def deleter(item, fn):
            def handler(e=None):
                refresh(fn(item))

            return handler

        def add_item(e=None):
            message = stage_item(name_field.value, qty_field.value)
            if message.startswith("Staged"):
                name_field.value = ""
                qty_field.value = "1"
            refresh(message)

        def save_items(e=None):
            refresh(save_pending())

        def discard_items(e=None):
            refresh(discard_pending())

        name_field.on_submit = add_item
        qty_field.on_submit = add_item
        save_button.on_click = save_items
        discard_button.on_click = discard_items
        refresh("")

        return ft.View(
            route="/insert",
            bgcolor=BG,
            appbar=ft.AppBar(
                title=ft.Text("Insert items"), bgcolor=CARD_BG, color=ft.Colors.WHITE
            ),
            controls=[
                ft.SafeArea(
                    expand=True,
                    content=ft.Container(
                        expand=True,
                        padding=24,
                        alignment=ft.Alignment.TOP_CENTER,
                        content=ft.Column(
                            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                            spacing=14,
                            scroll=ft.ScrollMode.AUTO,
                            controls=[
                                summary,
                                ft.Row(
                                    alignment=ft.MainAxisAlignment.CENTER,
                                    spacing=12,
                                    controls=[
                                        name_field,
                                        qty_field,
                                        ft.IconButton(
                                            icon=ft.Icons.ADD,
                                            icon_color=ft.Colors.WHITE,
                                            tooltip="Stage item",
                                            on_click=add_item,
                                        ),
                                    ],
                                ),
                                status,
                                ft.Divider(color=ACCENT),
                                draft_header,
                                draft_list,
                                ft.Row(
                                    alignment=ft.MainAxisAlignment.CENTER,
                                    spacing=12,
                                    controls=[save_button, discard_button],
                                ),
                                ft.Divider(color=ACCENT),
                                ft.Text("Already in the fridge", size=20, color=ACCENT),
                                saved_list,
                                ft.Container(height=12),
                                ft.Button(
                                    content=ft.Text("Back to home", size=18, width=200,
                                                    text_align=ft.TextAlign.CENTER),
                                    on_click=go("/"),
                                ),
                            ],
                        ),
                    ),
                )
            ],
        )

    # ----- capacity view ------------------------------------------------
    def capacity_view() -> ft.View:
        headline = ft.Text("", size=32, weight=ft.FontWeight.BOLD, color=ft.Colors.WHITE)
        bar = ft.ProgressBar(width=460, height=18, bgcolor=CARD_BG)
        pct_text = ft.Text("", size=18, color=ACCENT)
        item_list = ft.Column(spacing=8)

        def refresh(e=None):
            used = used_slots()
            pct = used / CAPACITY if CAPACITY else 0
            headline.value = f"{used} of {CAPACITY} slots used"
            bar.value = min(pct, 1.0)
            bar.color = (
                ft.Colors.GREEN_400
                if pct < 0.6
                else ft.Colors.AMBER_400
                if pct < 0.9
                else ft.Colors.RED_400
            )
            pct_text.value = f"{pct * 100:.0f}% full"
            if items:
                item_list.controls = [
                    qty_row(
                        item,
                        stepper(item, -1),
                        stepper(item, +1),
                        deleter(item),
                        ACCENT,
                    )
                    for item in items
                ]
            else:
                item_list.controls = [
                    ft.Text("The fridge is empty.", size=20, color=ft.Colors.WHITE70)
                ]
            page.update()

        def stepper(item, delta):
            def handler(e=None):
                adjust_item(item, delta)
                refresh()

            return handler

        def deleter(item):
            def handler(e=None):
                remove_item(item)
                refresh()

            return handler

        refresh()

        return ft.View(
            route="/capacity",
            bgcolor=BG,
            appbar=ft.AppBar(
                title=ft.Text("Current capacity"), bgcolor=CARD_BG, color=ft.Colors.WHITE
            ),
            controls=[
                ft.SafeArea(
                    expand=True,
                    content=ft.Container(
                        expand=True,
                        padding=24,
                        alignment=ft.Alignment.TOP_CENTER,
                        content=ft.Column(
                            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                            spacing=16,
                            scroll=ft.ScrollMode.AUTO,
                            controls=[
                                headline,
                                bar,
                                pct_text,
                                ft.Divider(color=ACCENT),
                                item_list,
                                ft.Container(height=12),
                                ft.Button(
                                    content=ft.Text("Back to home", size=18, width=200,
                                                    text_align=ft.TextAlign.CENTER),
                                    on_click=go("/"),
                                ),
                            ],
                        ),
                    ),
                )
            ],
        )

    # ----- routing ------------------------------------------------------
    def route_change():
        page.views.clear()
        page.views.append(welcome_view())
        if page.route == "/insert":
            page.views.append(insert_view())
        elif page.route == "/capacity":
            page.views.append(capacity_view())
        page.update()

    async def view_pop(e):
        if e.view is not None:
            page.views.remove(e.view)
            await page.push_route(page.views[-1].route)

    page.on_route_change = route_change
    page.on_view_pop = view_pop

    route_change()


if __name__ == "__main__":
    ft.run(main)

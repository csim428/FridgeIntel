"""FridgeIntel -- a shared fridge for one household.

Every roommate's phone talks to the same Supabase project, so what one person
adds the others see. The only thing kept on the device is the unsaved draft
(and the login session); the fridge itself lives in Postgres.

Network calls are pushed onto a worker thread with asyncio.to_thread so a slow
connection never freezes the UI.
"""

import asyncio

import flet as ft

from fridge_api import ApiError, FridgeApi

api = FridgeApi()

# Server state, refreshed from fridge_state(). Cached so a view can render
# immediately and repaint when the fetch lands.
state: dict = {"capacity": 0, "items": [], "history": []}
me: dict = {}

# Items staged on this device but not saved yet. Deliberately local: a draft is
# one person's shopping trip, not something the other roommates should see.
pending: list[dict] = []

# --- palette ---------------------------------------------------------------
BG = "#FFFFFF"
SURFACE = "#F8F9FB"
BORDER = "#E5E8EE"
TEXT = "#101828"
MUTED = "#667085"
ACCENT = "#2F6BFF"
ACCENT_SOFT = "#EEF3FF"
DRAFT_TEXT = "#B54708"
DRAFT_BG = "#FFFAEB"
DRAFT_BORDER = "#FEDF89"
GREEN = "#12B76A"
AMBER = "#F79009"
RED = "#F04438"


# ---------------------------------------------------------------------------
# Local draft logic (no Flet, no network -- testable on its own)
# ---------------------------------------------------------------------------


def used_slots() -> int:
    return sum(i["qty"] for i in state["items"])


def pending_slots() -> int:
    return sum(i["qty"] for i in pending)


def free_slots() -> int:
    return state["capacity"] - used_slots() - pending_slots()


def stage_item(name: str, qty_text: str) -> str:
    """Validate and stage locally. The server re-checks capacity on save."""
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

    for item in pending:
        if item["name"].lower() == name.lower():
            item["qty"] += qty
            break
    else:
        pending.append({"name": name, "qty": qty})
    return f"Staged {qty} x {name}."


def adjust_pending(item: dict, delta: int) -> str:
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


# ---------------------------------------------------------------------------
# Shared styling
# ---------------------------------------------------------------------------


def primary_style():
    return ft.ButtonStyle(
        bgcolor=ACCENT, color=ft.Colors.WHITE, elevation=0,
        shape=ft.RoundedRectangleBorder(radius=12),
        padding=ft.Padding(left=22, right=22, top=20, bottom=20),
    )


def quiet_style():
    return ft.ButtonStyle(
        bgcolor=SURFACE, color=TEXT, elevation=0,
        shape=ft.RoundedRectangleBorder(radius=12),
        padding=ft.Padding(left=22, right=22, top=20, bottom=20),
        side=ft.BorderSide(1, BORDER),
    )


def field(label, **kw):
    return ft.TextField(
        label=label, border_radius=12, filled=True, fill_color=SURFACE,
        border_color=BORDER, focused_border_color=ACCENT, color=TEXT,
        text_size=15, **kw,
    )


def section_label(text, color=MUTED):
    return ft.Text(text, size=13, weight=ft.FontWeight.W_600, color=color)


def main(page: ft.Page):
    page.title = "FridgeIntel"
    page.theme_mode = ft.ThemeMode.LIGHT
    page.theme = ft.Theme(color_scheme_seed=ACCENT)
    page.bgcolor = BG

    def content_width() -> float:
        """Usable width for cards and fields.

        Phones are ~375pt wide; fixed widths pushed the Qty box and the Add
        button off-screen. Fluid up to a cap so it still looks right on a
        tablet or in a desktop browser.
        """
        # 24pt of padding each side, plus slack: Flet clips a Row that
        # overflows instead of shrinking it, so exact-fit maths is fragile.
        available = (page.width or 420) - 60
        return max(240.0, min(500.0, available))

    def go(route: str):
        return lambda: asyncio.create_task(page.push_route(route))

    async def call(fn, *args):
        """Run a blocking API call off the UI thread."""
        return await asyncio.to_thread(fn, *args)

    async def refresh_state():
        state.update(await call(api.fridge_state))

    def app_bar(title, actions=None):
        return ft.AppBar(
            title=ft.Text(title, size=17, weight=ft.FontWeight.W_600, color=TEXT),
            bgcolor=BG, color=TEXT, elevation=0, center_title=True,
            toolbar_height=60, actions=actions or [],
        )

    def screen(*controls, scroll=True):
        return ft.SafeArea(
            expand=True,
            content=ft.Container(
                expand=True,
                padding=ft.Padding(left=24, right=24, top=8, bottom=32),
                alignment=ft.Alignment.TOP_CENTER,
                content=ft.Column(
                    horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                    spacing=14,
                    scroll=ft.ScrollMode.AUTO if scroll else None,
                    controls=list(controls),
                ),
            ),
        )

    def item_row(item, on_minus, on_plus, on_delete, draft=False):
        subtitle = None if draft else item.get("added_by")
        name_block = [
            ft.Text(item["name"], size=15, weight=ft.FontWeight.W_500,
                    color=DRAFT_TEXT if draft else TEXT, no_wrap=True,
                    max_lines=1, overflow=ft.TextOverflow.ELLIPSIS)
        ]
        if subtitle:
            name_block.append(ft.Text(f"added by {subtitle}", size=12, color=MUTED))
        return ft.Container(
            bgcolor=DRAFT_BG if draft else SURFACE,
            border=ft.Border.all(1, DRAFT_BORDER if draft else BORDER),
            border_radius=14,
            padding=ft.Padding(left=16, right=4, top=8, bottom=8),
            width=content_width(),
            content=ft.Row(
                alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                controls=[
                    ft.Column(spacing=1, expand=True, controls=name_block),
                    ft.Row(spacing=0, controls=[
                        ft.IconButton(icon=ft.Icons.REMOVE, icon_color=MUTED,
                                      icon_size=18, tooltip="Decrease",
                                      on_click=on_minus),
                        ft.Container(width=40, alignment=ft.Alignment.CENTER,
                                     content=ft.Text(f"{item['qty']}", size=15,
                                                     weight=ft.FontWeight.W_600,
                                                     color=DRAFT_TEXT if draft else TEXT)),
                        ft.IconButton(icon=ft.Icons.ADD, icon_color=MUTED,
                                      icon_size=18, tooltip="Increase",
                                      on_click=on_plus),
                        ft.IconButton(icon=ft.Icons.CLOSE, icon_color=MUTED,
                                      icon_size=18, tooltip="Remove",
                                      on_click=on_delete),
                    ]),
                ],
            ),
        )

    def empty_note(text):
        return ft.Container(width=content_width(), alignment=ft.Alignment.CENTER,
                            padding=ft.Padding(left=16, right=16, top=18, bottom=18),
                            content=ft.Text(text, size=14, color=MUTED))

    # ----- login --------------------------------------------------------
    def login_view() -> ft.View:
        cw = content_width()
        email = field("Email", width=cw, autofocus=True,
                      keyboard_type=ft.KeyboardType.EMAIL)
        password = field("Password", width=cw, password=True,
                         can_reveal_password=True)
        status = ft.Text("", size=14, color=RED, width=cw,
                         text_align=ft.TextAlign.CENTER)
        spinner = ft.ProgressRing(width=18, height=18, stroke_width=2,
                                  color=ACCENT, visible=False)

        async def attempt(fn):
            status.value = ""
            spinner.visible = True
            page.update()
            try:
                await call(fn, email.value, password.value)
                await after_login()
            except ApiError as e:
                status.value = str(e)
            finally:
                spinner.visible = False
                page.update()

        async def do_login(e=None):
            await attempt(api.sign_in)

        async def do_signup(e=None):
            await attempt(api.sign_up)

        password.on_submit = do_login

        return ft.View(
            route="/login", bgcolor=BG,
            controls=[screen(
                ft.Container(height=40),
                ft.Container(width=76, height=76, bgcolor=ACCENT_SOFT,
                             border_radius=22, alignment=ft.Alignment.CENTER,
                             content=ft.Icon(ft.Icons.KITCHEN_OUTLINED, size=36,
                                             color=ACCENT)),
                ft.Container(height=16),
                ft.Text("FridgeIntel", size=36, weight=ft.FontWeight.BOLD, color=TEXT),
                ft.Text("Sign in to your household fridge", size=15, color=MUTED),
                ft.Container(height=20),
                email, password, spinner, status,
                ft.Container(height=4),
                ft.Button(content=ft.Text("Log in", size=16,
                                          weight=ft.FontWeight.W_600, width=cw - 48,
                                          text_align=ft.TextAlign.CENTER),
                          style=primary_style(), on_click=do_login),
                ft.Button(content=ft.Text("Create an account", size=16,
                                          weight=ft.FontWeight.W_600, width=cw - 48,
                                          text_align=ft.TextAlign.CENTER),
                          style=quiet_style(), on_click=do_signup),
            )],
        )

    # ----- household setup ----------------------------------------------
    def setup_view() -> ft.View:
        cw = content_width()
        your_name = field("Your name", width=cw, autofocus=True)
        house_name = field("Household name", width=cw, hint_text="e.g. Apt 4B")
        join_code = field("Join code", width=cw,
                          hint_text="paste the code from a roommate")
        status = ft.Text("", size=14, color=RED, width=cw,
                         text_align=ft.TextAlign.CENTER)
        code_out = ft.Container(visible=False, width=cw, bgcolor=ACCENT_SOFT,
                                border_radius=12, padding=16,
                                content=ft.Column(spacing=6, controls=[]))

        async def run(fn, *args):
            status.value = ""
            page.update()
            try:
                result = await call(fn, *args)
                return result, True
            except ApiError as e:
                status.value = str(e)
                page.update()
                return None, False

        async def do_create(e=None):
            code, ok = await run(api.create_household, house_name.value,
                                 your_name.value)
            if not ok:
                return
            code_out.visible = True
            code_out.content.controls = [
                section_label("SHARE THIS CODE WITH YOUR ROOMMATES", ACCENT),
                ft.Text(str(code), size=15, weight=ft.FontWeight.W_600,
                        color=TEXT, selectable=True),
                ft.Text("They enter it under Join a household.", size=13,
                        color=MUTED),
                ft.Button(content=ft.Text("Continue to the fridge", size=15,
                                          weight=ft.FontWeight.W_600, width=220,
                                          text_align=ft.TextAlign.CENTER),
                          style=primary_style(), on_click=lambda e=None:
                          asyncio.create_task(after_login())),
            ]
            page.update()

        async def do_join(e=None):
            _, ok = await run(api.join_household, join_code.value, your_name.value)
            if ok:
                await after_login()

        return ft.View(
            route="/setup", bgcolor=BG,
            appbar=app_bar("Set up your household", actions=[
                ft.IconButton(icon=ft.Icons.LOGOUT, icon_color=MUTED,
                              tooltip="Sign out", on_click=do_sign_out)]),
            controls=[screen(
                ft.Text("One roommate creates the household. The rest join with "
                        "the code it gives back.", size=14, color=MUTED, width=cw,
                        text_align=ft.TextAlign.CENTER),
                ft.Container(height=8),
                your_name,
                status,
                ft.Divider(color=BORDER),
                section_label("CREATE A HOUSEHOLD"),
                house_name,
                ft.Button(content=ft.Text("Create", size=15,
                                          weight=ft.FontWeight.W_600, width=200,
                                          text_align=ft.TextAlign.CENTER),
                          style=primary_style(), on_click=do_create),
                code_out,
                ft.Divider(color=BORDER),
                section_label("JOIN A HOUSEHOLD"),
                join_code,
                ft.Button(content=ft.Text("Join", size=15,
                                          weight=ft.FontWeight.W_600, width=200,
                                          text_align=ft.TextAlign.CENTER),
                          style=quiet_style(), on_click=do_join),
            )],
        )

    # ----- welcome ------------------------------------------------------
    def welcome_view() -> ft.View:
        roommates = ", ".join(me.get("roommates") or []) or "just you so far"
        return ft.View(
            route="/", bgcolor=BG,
            controls=[ft.SafeArea(expand=True, content=ft.Container(
                expand=True, alignment=ft.Alignment.CENTER, padding=32,
                content=ft.Column(
                    horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                    alignment=ft.MainAxisAlignment.CENTER, spacing=0,
                    controls=[
                        ft.Container(width=76, height=76, bgcolor=ACCENT_SOFT,
                                     border_radius=22,
                                     alignment=ft.Alignment.CENTER,
                                     content=ft.Icon(ft.Icons.KITCHEN_OUTLINED,
                                                     size=36, color=ACCENT)),
                        ft.Container(height=24),
                        ft.Text(me.get("household_name") or "FridgeIntel", size=40,
                                weight=ft.FontWeight.BOLD, color=TEXT,
                                text_align=ft.TextAlign.CENTER),
                        ft.Container(height=8),
                        ft.Text(f"Shared with {roommates}", size=15, color=MUTED,
                                width=content_width(),
                                text_align=ft.TextAlign.CENTER),
                        ft.Container(height=36),
                        ft.Button(content=ft.Text("Insert items", size=16,
                                                  weight=ft.FontWeight.W_600,
                                                  width=content_width() - 44,
                                                  text_align=ft.TextAlign.CENTER),
                                  style=primary_style(), on_click=go("/insert")),
                        ft.Container(height=12),
                        ft.Button(content=ft.Text("View capacity", size=16,
                                                  weight=ft.FontWeight.W_600,
                                                  width=content_width() - 44,
                                                  text_align=ft.TextAlign.CENTER),
                                  style=quiet_style(), on_click=go("/capacity")),
                        ft.Container(height=20),
                        ft.TextButton("Sign out", on_click=do_sign_out),
                    ],
                )))],
        )

    # ----- insert -------------------------------------------------------
    def insert_view() -> ft.View:
        cw = content_width()
        # name + qty + add button must add up to cw, or the add button ends up
        # off-screen on a phone.
        name_field = field("Item name", width=cw - 72 - 44 - 24, autofocus=True)
        qty_field = field("Qty", width=72, value="1",
                          keyboard_type=ft.KeyboardType.NUMBER)
        recent_dd = ft.Dropdown(
            label="Used before", hint_text="Pick a saved item", width=cw,
            enable_filter=True, editable=True, border_radius=12, filled=True,
            fill_color=SURFACE, border_color=BORDER, focused_border_color=ACCENT,
            color=TEXT, text_size=15, leading_icon=ft.Icons.HISTORY,
        )
        summary = ft.Text("", size=14, color=MUTED)
        status = ft.Text("", size=14, color=ACCENT, width=cw,
                         text_align=ft.TextAlign.CENTER)
        draft_label = section_label("", DRAFT_TEXT)
        draft_list = ft.Column(spacing=10)
        saved_list = ft.Column(spacing=10)
        # Each Button carries 22pt of padding per side, so the label may only
        # be as wide as what is left after that and the gap between them.
        half = (cw - 10) / 2 - 44
        save_button = ft.Button(
            content=ft.Text("Save", size=15, weight=ft.FontWeight.W_600,
                            width=half, text_align=ft.TextAlign.CENTER),
            style=primary_style())
        discard_button = ft.Button(
            content=ft.Text("Discard", size=15, weight=ft.FontWeight.W_600,
                            width=half, text_align=ft.TextAlign.CENTER),
            style=quiet_style())

        def paint(message=None, error=False):
            if message is not None:
                status.value = message
                status.color = RED if error else ACCENT
            summary.value = (f"{used_slots()} in fridge  ·  {pending_slots()} unsaved"
                             f"  ·  {free_slots()} free of {state['capacity']}")
            draft_label.value = f"UNSAVED DRAFT ({len(pending)})"
            recent_dd.options = [ft.DropdownOption(key=n) for n in state["history"]]
            recent_dd.disabled = not state["history"]

            draft_list.controls = [
                item_row(i, local(i, -1, adjust_pending), local(i, +1, adjust_pending),
                         local(i, None, remove_pending), draft=True)
                for i in pending
            ] or [empty_note("Nothing staged. Added items land here first.")]

            saved_list.controls = [
                item_row(i, server(i, -1), server(i, +1), server(i, None))
                for i in state["items"]
            ] or [empty_note("The fridge is empty.")]

            save_button.disabled = not pending
            discard_button.disabled = not pending
            page.update()

        def local(item, delta, fn):
            def handler(e=None):
                paint(fn(item, delta) if delta is not None else fn(item))
            return handler

        def server(item, delta):
            async def handler(e=None):
                try:
                    if delta is None:
                        await call(api.remove_item, item["id"])
                    else:
                        await call(api.adjust_item, item["id"], delta)
                    await refresh_state()
                    paint("")
                except ApiError as ex:
                    paint(str(ex), error=True)
            return handler

        async def do_save(e=None):
            try:
                await call(api.save_items, list(pending))
                pending.clear()
                await refresh_state()
                paint("Saved. Your roommates can see it now.")
            except ApiError as ex:
                paint(str(ex), error=True)

        def do_discard(e=None):
            paint(discard_pending())

        def pick_recent(e=None):
            if recent_dd.value:
                name_field.value = recent_dd.value
                paint(f"Loaded {recent_dd.value}. Set a quantity and add it.")

        def add_item(e=None):
            message = stage_item(name_field.value, qty_field.value)
            if message.startswith("Staged"):
                name_field.value = ""
                qty_field.value = "1"
                recent_dd.value = None
            paint(message, error=not message.startswith("Staged"))

        async def do_refresh(e=None):
            await refresh_state()
            paint("Refreshed.")

        name_field.on_submit = add_item
        qty_field.on_submit = add_item
        recent_dd.on_select = pick_recent
        save_button.on_click = do_save
        discard_button.on_click = do_discard
        paint("")

        return ft.View(
            route="/insert", bgcolor=BG,
            appbar=app_bar("Insert items", actions=[
                ft.IconButton(icon=ft.Icons.REFRESH, icon_color=MUTED,
                              tooltip="Refresh", on_click=do_refresh)]),
            controls=[screen(
                summary,
                ft.Row(alignment=ft.MainAxisAlignment.CENTER, spacing=8,
                       controls=[recent_dd]),
                ft.Row(alignment=ft.MainAxisAlignment.CENTER, spacing=8,
                       width=cw, controls=[
                    name_field, qty_field,
                    ft.IconButton(icon=ft.Icons.ADD, icon_color=ft.Colors.WHITE,
                                  bgcolor=ACCENT, icon_size=20, width=44, height=44,
                                  tooltip="Stage item", on_click=add_item)]),
                status,
                ft.Row(width=cw, alignment=ft.MainAxisAlignment.START,
                       controls=[draft_label]),
                draft_list,
                ft.Row(alignment=ft.MainAxisAlignment.CENTER, spacing=10,
                       width=cw, controls=[save_button, discard_button]),
                ft.Container(height=6),
                ft.Row(width=cw, alignment=ft.MainAxisAlignment.START,
                       controls=[section_label("IN THE FRIDGE")]),
                saved_list,
                ft.Container(height=8),
                ft.Button(content=ft.Text("Back to home", size=15,
                                          weight=ft.FontWeight.W_600, width=cw - 48,
                                          text_align=ft.TextAlign.CENTER),
                          style=quiet_style(), on_click=go("/")),
            )],
        )

    # ----- capacity -----------------------------------------------------
    def capacity_view() -> ft.View:
        cw = content_width()
        headline = ft.Text("", size=40, weight=ft.FontWeight.BOLD, color=TEXT)
        sub = ft.Text("", size=15, color=MUTED)
        bar = ft.ProgressBar(width=cw, height=12, bgcolor=SURFACE, border_radius=8)
        item_list = ft.Column(spacing=10)
        status = ft.Text("", size=14, color=RED)

        def paint(message="", error=True):
            cap = state["capacity"] or 0
            used = used_slots()
            pct = used / cap if cap else 0
            headline.value = f"{used} / {cap}"
            sub.value = f"{pct * 100:.0f}% full  ·  {cap - used} slots free"
            bar.value = min(pct, 1.0)
            bar.color = GREEN if pct < 0.6 else AMBER if pct < 0.9 else RED
            status.value = message
            status.color = RED if error else MUTED
            item_list.controls = [
                item_row(i, server(i, -1), server(i, +1), server(i, None))
                for i in state["items"]
            ] or [empty_note("The fridge is empty.")]
            page.update()

        def server(item, delta):
            async def handler(e=None):
                try:
                    if delta is None:
                        await call(api.remove_item, item["id"])
                    else:
                        await call(api.adjust_item, item["id"], delta)
                    await refresh_state()
                    paint("")
                except ApiError as ex:
                    paint(str(ex))
            return handler

        async def do_refresh(e=None):
            await refresh_state()
            paint("")

        paint("")

        return ft.View(
            route="/capacity", bgcolor=BG,
            appbar=app_bar("Current capacity", actions=[
                ft.IconButton(icon=ft.Icons.REFRESH, icon_color=MUTED,
                              tooltip="Refresh", on_click=do_refresh)]),
            controls=[screen(
                ft.Container(height=8), headline, sub,
                ft.Container(height=8), bar,
                ft.Container(height=8), status,
                ft.Row(width=cw, alignment=ft.MainAxisAlignment.START,
                       controls=[section_label("IN THE FRIDGE")]),
                item_list,
                ft.Container(height=8),
                ft.Button(content=ft.Text("Back to home", size=15,
                                          weight=ft.FontWeight.W_600, width=cw - 48,
                                          text_align=ft.TextAlign.CENTER),
                          style=quiet_style(), on_click=go("/")),
            )],
        )

    # ----- session flow -------------------------------------------------
    async def show(route: str):
        """Navigate, or just rebuild when we are already on that route.

        push_route only fires on_route_change when the route actually changes,
        so pushing the route we are already on would leave the old view up.
        """
        if page.route == route:
            route_change()
        else:
            await page.push_route(route)

    async def after_login():
        """Decide where a signed-in roommate lands."""
        me.clear()
        me.update(await call(api.whoami))
        if not me.get("household_id"):
            await show("/setup")
            return
        await refresh_state()
        # Keep the route the app was opened on. A PWA gets reloaded often, and
        # landing back on the home screen every time would be annoying.
        await show(page.route if page.route in ("/insert", "/capacity") else "/")

    async def do_sign_out(e=None):
        await call(api.sign_out)
        me.clear()
        pending.clear()
        state.update({"capacity": 0, "items": [], "history": []})
        await show("/login")

    # ----- routing ------------------------------------------------------
    def route_change():
        page.views.clear()
        route = page.route

        if not api.signed_in:
            page.views.append(login_view())
        elif not me.get("household_id"):
            page.views.append(setup_view())
        else:
            page.views.append(welcome_view())
            if route == "/insert":
                page.views.append(insert_view())
            elif route == "/capacity":
                page.views.append(capacity_view())
        page.update()

    async def view_pop(e):
        if e.view is not None:
            page.views.remove(e.view)
            await page.push_route(page.views[-1].route)

    def on_resize(e=None):
        # Widths are computed at build time, so rebuild when the size changes
        # (rotating a phone, resizing a browser window).
        route_change()

    page.on_route_change = route_change
    page.on_view_pop = view_pop
    page.on_resize = on_resize

    async def boot():
        """Resume a saved login if there is one, otherwise show the login screen."""
        try:
            if await call(api.restore_session):
                await after_login()
                return
        except ApiError:
            pass
        await show("/login")

    asyncio.create_task(boot())
    route_change()


if __name__ == "__main__":
    ft.run(main)

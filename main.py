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


# --- palette ---------------------------------------------------------------
# Sixteen friendly food avatars. Stored by key, so the stored value stays
# valid even if the icon set is restyled later.
AVATARS: dict[str, str] = {
    "apple": ft.Icons.APPLE,
    "egg": ft.Icons.EGG_ALT,
    "bread": ft.Icons.BAKERY_DINING,
    "burger": ft.Icons.LUNCH_DINING,
    "ramen": ft.Icons.RAMEN_DINING,
    "icecream": ft.Icons.ICECREAM,
    "pizza": ft.Icons.LOCAL_PIZZA,
    "cookie": ft.Icons.COOKIE,
    "cake": ft.Icons.CAKE,
    "coffee": ft.Icons.COFFEE,
    "tea": ft.Icons.EMOJI_FOOD_BEVERAGE,
    "sushi": ft.Icons.SET_MEAL,
    "rice": ft.Icons.RICE_BOWL,
    "brunch": ft.Icons.BRUNCH_DINING,
    "tapas": ft.Icons.TAPAS,
    "smoothie": ft.Icons.BLENDER,
}

# Icons a household can pick for itself.
HOUSE_ICONS: dict[str, str] = {
    "kitchen": ft.Icons.KITCHEN,
    "home": ft.Icons.HOME_ROUNDED,
    "soup": ft.Icons.SOUP_KITCHEN,
    "dinner": ft.Icons.DINNER_DINING,
    "cafe": ft.Icons.LOCAL_CAFE,
    "bar": ft.Icons.LOCAL_BAR,
    "microwave": ft.Icons.MICROWAVE,
    "produce": ft.Icons.AGRICULTURE,
}


def avatar_icon(key: str | None) -> str:
    return AVATARS.get(key or "apple", ft.Icons.APPLE)


def house_icon(key: str | None) -> str:
    return HOUSE_ICONS.get(key or "kitchen", ft.Icons.KITCHEN)


# Key under which each browser keeps its own login.
TOKEN_KEY = "fridgeintel.refresh_token"

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
GREEN_BG = "#ECFDF3"
GREEN_BORDER = "#A6F4C5"
GREEN_TEXT = "#027A48"
AMBER = "#F79009"
RED = "#F04438"


# ---------------------------------------------------------------------------
# Per-session state (no Flet, no network -- testable on its own)
# ---------------------------------------------------------------------------


class Fridge:
    """One roommate's view of the fridge, plus their local unsaved draft.

    Deliberately an instance rather than module globals: when the app is
    hosted on a server, a single Python process serves every roommate at
    once, and shared globals would mean shared logins and shared drafts.
    """

    def __init__(self):
        self.capacity = 0
        self.items: list[dict] = []
        # {"name": str, "category": str | None} -- things this household has
        # bought before.
        self.history: list[dict] = []
        # The shared starter list, same for every household.
        self.catalog: list[dict] = []
        # Staged but not saved. One person's shopping trip, not shared state.
        self.pending: list[dict] = []

    def apply(self, server_state: dict) -> None:
        self.capacity = server_state.get("capacity") or 0
        self.items = server_state.get("items") or []
        self.history = server_state.get("history") or []
        self.catalog = server_state.get("catalog") or []

    def used_slots(self) -> int:
        return sum(i["qty"] for i in self.items)

    def pending_slots(self) -> int:
        return sum(i["qty"] for i in self.pending)

    def free_slots(self) -> int:
        return self.capacity - self.used_slots() - self.pending_slots()

    def category_for(self, name: str) -> str | None:
        """Best known category for a name: what we used last, else the catalog."""
        low = (name or "").strip().lower()
        for row in self.history:
            if row["name"].lower() == low and row.get("category"):
                return row["category"]
        for row in self.catalog:
            if row["name"].lower() == low:
                return row["category"]
        return None

    def stage(self, name: str, qty_text: str, category: str | None = None) -> str:
        """Validate and stage locally. The server re-checks capacity on save."""
        name = (name or "").strip()
        if not name:
            return "Enter an item name."
        # Blank and whitespace-only both mean "left it alone", so both get 1.
        text = (qty_text or "").strip() or "1"
        try:
            qty = int(text)
        except ValueError:
            return "Quantity must be a whole number."
        if qty < 1:
            return "Quantity must be at least 1."
        if qty > self.free_slots():
            return f"Not enough room - {self.free_slots()} slot(s) left."

        category = (category or "").strip() or self.category_for(name)
        for item in self.pending:
            if item["name"].lower() == name.lower():
                item["qty"] += qty
                item["category"] = item.get("category") or category
                break
        else:
            self.pending.append({"name": name, "qty": qty, "category": category})
        return f"Staged {qty} x {name}."

    def adjust_pending(self, item: dict, delta: int) -> str:
        if delta > 0 and delta > self.free_slots():
            return f"Not enough room - {self.free_slots()} slot(s) left."
        item["qty"] += delta
        if item["qty"] < 1:
            self.pending.remove(item)
            return f"Removed {item['name']} from the draft."
        return ""

    def remove_pending(self, item: dict) -> str:
        if item in self.pending:
            self.pending.remove(item)
            return f"Removed {item['name']} from the draft."
        return ""

    def discard_pending(self) -> str:
        count = len(self.pending)
        self.pending.clear()
        return (f"Discarded {count} unsaved item(s)." if count
                else "Nothing to discard.")

    def suggestions(self, typed: str) -> list[dict]:
        """Names matching what has been typed, ranked and tagged by source.

        Returns dicts of {name, category, seen}, where `seen` marks something
        the household has bought before as opposed to a plain catalog entry.
        Prefix matches come first -- what someone typing "mi" for Milk expects.
        """
        text = (typed or "").strip().lower()
        if not text:
            return []

        known = {r["name"].lower() for r in self.history}
        out: list[dict] = []
        seen_names: set[str] = set()

        def collect(rows, seen_flag):
            starts, contains = [], []
            for row in rows:
                name = row["name"]
                low = name.lower()
                if low == text or low in seen_names:
                    continue
                entry = {"name": name, "category": row.get("category"),
                         "seen": seen_flag}
                if low.startswith(text):
                    starts.append(entry)
                elif text in low:
                    contains.append(entry)
            for e in starts + contains:
                seen_names.add(e["name"].lower())
                out.append(e)

        collect(self.history, True)
        # Catalog entries the household has already used are covered above.
        collect([c for c in self.catalog if c["name"].lower() not in known], False)
        return out[:8]


# ---------------------------------------------------------------------------
# Shared styling
# ---------------------------------------------------------------------------


def primary_style(pad: int = 22):
    return ft.ButtonStyle(
        bgcolor=ACCENT, color=ft.Colors.WHITE, elevation=0,
        shape=ft.RoundedRectangleBorder(radius=12),
        padding=ft.Padding(left=pad, right=pad, top=20, bottom=20),
    )


def quiet_style(pad: int = 22):
    return ft.ButtonStyle(
        bgcolor=SURFACE, color=TEXT, elevation=0,
        shape=ft.RoundedRectangleBorder(radius=12),
        padding=ft.Padding(left=pad, right=pad, top=20, bottom=20),
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
    # One of each per browser session, never module-level: the server process
    # is shared by every roommate.
    api = FridgeApi()
    fridge = Fridge()
    me: dict = {}
    # Captured here, on the app's event loop, so worker threads can hand work
    # back to it.
    loop = asyncio.get_running_loop()

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

    def remember_token(refresh_token, email):
        """Store the session against this browser only.

        shared_preferences is per client, unlike a file on the server, which
        every roommate's session would otherwise share.

        This runs on the worker thread that asyncio.to_thread created, where
        there is no running event loop, so the write has to be handed back to
        the app's loop rather than scheduled with create_task.
        """
        async def write():
            # Storage can be unavailable (private browsing, blocked cookies).
            # Losing "stay logged in" is survivable; crashing is not.
            try:
                if refresh_token:
                    await page.shared_preferences.set(TOKEN_KEY, refresh_token)
                else:
                    await page.shared_preferences.remove(TOKEN_KEY)
            except Exception:
                pass

        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            asyncio.create_task(write())          # already on the app's loop
        else:
            asyncio.run_coroutine_threadsafe(write(), loop)

    api.on_token_change = remember_token

    async def refresh_state():
        fridge.apply(await call(api.fridge_state))

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
        caption = " · ".join(x for x in (
            item.get("category"),
            None if draft else (f"added by {subtitle}" if subtitle else None),
        ) if x)
        if caption:
            name_block.append(ft.Text(caption, size=12, color=MUTED,
                                      no_wrap=True, max_lines=1,
                                      overflow=ft.TextOverflow.ELLIPSIS))
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

    def invite_card() -> ft.Container:
        """The join code, available any time -- not just at household creation.

        The code is the household id, which whoami() already returns, so
        roommates three and four can be invited long after setup.
        """
        code = me.get("household_id") or ""
        note = ft.Text("They enter this code when they sign up.", size=12,
                       color=MUTED)

        async def copy(e=None):
            try:
                await page.clipboard.set(code)
                note.value = "Copied to clipboard."
                note.color = GREEN
            except Exception:
                # Clipboard access can be refused (e.g. an insecure origin).
                note.value = "Could not copy -- select the code above instead."
                note.color = RED
            page.update()

        return ft.Container(
            width=content_width(), bgcolor=SURFACE,
            border=ft.Border.all(1, BORDER), border_radius=14,
            padding=ft.Padding(left=16, right=8, top=12, bottom=12),
            content=ft.Column(spacing=6, controls=[
                section_label("INVITE A ROOMMATE"),
                ft.Row(alignment=ft.MainAxisAlignment.SPACE_BETWEEN, controls=[
                    ft.Text(code, size=12, color=TEXT, selectable=True,
                            expand=True, max_lines=2),
                    ft.IconButton(icon=ft.Icons.CONTENT_COPY, icon_color=MUTED,
                                  icon_size=18, tooltip="Copy join code",
                                  on_click=copy),
                ]),
                note,
            ]),
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
                                     content=ft.Icon(
                                         house_icon(me.get("household_icon")),
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
                        ft.Container(height=28),
                        invite_card(),
                        ft.Container(height=12),
                        ft.Button(content=ft.Text("Settings", size=16,
                                                  weight=ft.FontWeight.W_600,
                                                  width=content_width() - 48,
                                                  text_align=ft.TextAlign.CENTER),
                                  icon=ft.Icons.SETTINGS_OUTLINED,
                                  style=quiet_style(), on_click=go("/settings")),
                        ft.Container(height=8),
                        ft.TextButton("Sign out", on_click=do_sign_out),
                    ],
                )))],
        )

    # ----- insert -------------------------------------------------------
    def insert_view() -> ft.View:
        cw = content_width()
        # name + qty + add button must add up to cw, or the add button ends up
        # off-screen on a phone.
        name_field = field("Item name", width=cw - 72 - 44 - 24, autofocus=True,
                           hint_text="Start typing...")
        cat_field = field("Category", width=cw - 44 - 16,
                          hint_text="optional, e.g. Snacks")
        qty_field = field("Qty", width=72, value="1",
                          keyboard_type=ft.KeyboardType.NUMBER)
        # Suggestions from what the household has bought before, filtered as
        # you type. Tapping one fills the name field; typing something new is
        # always allowed, since the fridge gets unfamiliar items all the time.
        suggestions = ft.Row(wrap=True, spacing=8, run_spacing=8, width=cw,
                             visible=False)
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
            summary.value = (f"{fridge.used_slots()} in fridge  ·  {fridge.pending_slots()} unsaved"
                             f"  ·  {fridge.free_slots()} free of {fridge.capacity}")
            draft_label.value = f"UNSAVED DRAFT ({len(fridge.pending)})"

            draft_list.controls = [
                item_row(i, local(i, -1, fridge.adjust_pending), local(i, +1, fridge.adjust_pending),
                         local(i, None, fridge.remove_pending), draft=True)
                for i in fridge.pending
            ] or [empty_note("Nothing staged. Added items land here first.")]

            saved_list.controls = [
                item_row(i, server(i, -1), server(i, +1), server(i, None))
                for i in fridge.items
            ] or [empty_note("The fridge is empty.")]

            save_button.disabled = not fridge.pending
            discard_button.disabled = not fridge.pending
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
                await call(api.save_items, list(fridge.pending))
                fridge.pending.clear()
                suggestions.controls = []
                suggestions.visible = False
                await refresh_state()
                paint("Saved. Your roommates can see it now.")
            except ApiError as ex:
                paint(str(ex), error=True)

        def do_discard(e=None):
            paint(fridge.discard_pending())

        def suggestion_chip(entry: dict) -> ft.Chip:
            """Green if the household has bought it before, plain if it is
            only in the shared starter list."""
            seen = entry["seen"]
            def choose(e=None):
                name_field.value = entry["name"]
                cat_field.value = entry.get("category") or ""
                refresh_suggestions()
                qty_field.focus()

            label = entry["name"]
            if entry.get("category"):
                label += f"  ·  {entry['category']}"
            return ft.Chip(
                label=ft.Text(label, size=13,
                              color=GREEN_TEXT if seen else MUTED),
                bgcolor=GREEN_BG if seen else SURFACE,
                border_side=ft.BorderSide(1, GREEN_BORDER if seen else BORDER),
                on_click=choose,
            )

        def refresh_suggestions(e=None):
            found = fridge.suggestions(name_field.value)
            suggestions.controls = [suggestion_chip(n) for n in found]
            suggestions.visible = bool(found)
            page.update()

        def add_item(e=None):
            message = fridge.stage(name_field.value, qty_field.value,
                                   cat_field.value)
            if message.startswith("Staged"):
                name_field.value = ""
                qty_field.value = "1"
                cat_field.value = ""
                suggestions.controls = []
                suggestions.visible = False
            paint(message, error=not message.startswith("Staged"))

        async def do_refresh(e=None):
            try:
                await refresh_state()
                paint("Refreshed.")
            except ApiError as ex:
                paint(str(ex), error=True)

        name_field.on_change = refresh_suggestions
        name_field.on_submit = add_item
        qty_field.on_submit = add_item
        cat_field.on_submit = add_item
        save_button.on_click = do_save
        discard_button.on_click = do_discard
        paint("")

        return ft.View(
            route="/insert", bgcolor=BG,
            appbar=app_bar("Insert items", actions=[
                ft.IconButton(icon=ft.Icons.REFRESH, icon_color=MUTED,
                              tooltip="Refresh", on_click=do_refresh),
                ft.IconButton(icon=ft.Icons.SETTINGS_OUTLINED, icon_color=MUTED,
                              tooltip="Settings", on_click=go("/settings"))]),
            controls=[screen(
                summary,
                ft.Row(alignment=ft.MainAxisAlignment.CENTER, spacing=8,
                       width=cw, controls=[
                    name_field, qty_field,
                    ft.IconButton(icon=ft.Icons.ADD, icon_color=ft.Colors.WHITE,
                                  bgcolor=ACCENT, icon_size=20, width=44, height=44,
                                  tooltip="Stage item", on_click=add_item)]),
                suggestions,
                ft.Row(alignment=ft.MainAxisAlignment.CENTER, spacing=8,
                       width=cw, controls=[cat_field]),
                ft.Text("Green means your household has bought it before.",
                        size=12, color=MUTED, width=cw,
                        text_align=ft.TextAlign.CENTER),
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

    # ----- settings -----------------------------------------------------
    def settings_view() -> ft.View:
        cw = content_width()
        status = ft.Text("", size=14, color=RED, width=cw,
                         text_align=ft.TextAlign.CENTER)
        owner = bool(me.get("is_owner"))

        def say(message, error=True):
            status.value = message
            status.color = RED if error else GREEN
            page.update()

        def card(*controls):
            return ft.Container(
                width=cw, bgcolor=SURFACE, border=ft.Border.all(1, BORDER),
                border_radius=14,
                padding=ft.Padding(left=16, right=16, top=14, bottom=14),
                content=ft.Column(spacing=10, controls=list(controls)),
            )

        def picker(options: dict, current: str, on_pick) -> ft.Row:
            """A wrap of icon choices with the current one highlighted."""
            def cell(key, icon):
                chosen = key == current
                return ft.Container(
                    width=52, height=52, border_radius=14,
                    bgcolor=ACCENT_SOFT if chosen else BG,
                    border=ft.Border.all(2 if chosen else 1,
                                         ACCENT if chosen else BORDER),
                    alignment=ft.Alignment.CENTER,
                    tooltip=key,
                    content=ft.Icon(icon, size=24,
                                    color=ACCENT if chosen else MUTED),
                    on_click=lambda e=None, k=key: on_pick(k),
                )
            return ft.Row(wrap=True, spacing=8, run_spacing=8, width=cw - 32,
                          controls=[cell(k, v) for k, v in options.items()])

        # ---- you ----------------------------------------------------------
        username = field("Username", width=cw - 32, value=me.get("username") or "")
        chosen_avatar = {"key": me.get("avatar") or "apple"}
        avatar_row = ft.Row()

        def draw_avatars():
            avatar_row.controls = [picker(AVATARS, chosen_avatar["key"], pick_avatar)]
            page.update()

        def pick_avatar(key):
            chosen_avatar["key"] = key
            draw_avatars()

        async def save_profile(e=None):
            try:
                await call(api.set_profile, username.value, chosen_avatar["key"])
                me.update(await call(api.whoami))
                say("Profile saved.", error=False)
            except ApiError as ex:
                say(str(ex))

        draw_avatars()

        # ---- household ----------------------------------------------------
        house_name = field("Household name", width=cw - 32,
                           value=me.get("household_name") or "")
        chosen_house = {"key": me.get("household_icon") or "kitchen"}
        house_row = ft.Row()

        def draw_house_icons():
            house_row.controls = [picker(HOUSE_ICONS, chosen_house["key"],
                                         pick_house)]
            page.update()

        def pick_house(key):
            chosen_house["key"] = key
            draw_house_icons()

        async def save_house(e=None):
            try:
                await call(api.update_household, house_name.value,
                           chosen_house["key"])
                me.update(await call(api.whoami))
                say("Household updated.", error=False)
            except ApiError as ex:
                say(str(ex))

        draw_house_icons()

        # ---- members -------------------------------------------------------
        member_list = ft.Column(spacing=8)

        async def load_members(e=None):
            try:
                people = await call(api.members)
            except ApiError as ex:
                say(str(ex)); return
            rows = []
            for p in people:
                tags = []
                if p["is_owner"]:
                    tags.append("owner")
                if p["is_you"]:
                    tags.append("you")
                actions = []
                if owner and not p["is_you"]:
                    actions.append(ft.IconButton(
                        icon=ft.Icons.STAR_OUTLINE, icon_color=MUTED, icon_size=18,
                        tooltip="Make owner",
                        on_click=make_owner(p)))
                    actions.append(ft.IconButton(
                        icon=ft.Icons.PERSON_REMOVE_OUTLINED, icon_color=MUTED,
                        icon_size=18, tooltip="Remove from household",
                        on_click=kick(p)))
                rows.append(ft.Container(
                    bgcolor=BG, border=ft.Border.all(1, BORDER), border_radius=12,
                    padding=ft.Padding(left=12, right=4, top=6, bottom=6),
                    content=ft.Row(
                        alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                        controls=[
                            ft.Row(spacing=10, expand=True, controls=[
                                ft.Container(
                                    width=34, height=34, border_radius=10,
                                    bgcolor=ACCENT_SOFT,
                                    alignment=ft.Alignment.CENTER,
                                    content=ft.Icon(avatar_icon(p["avatar"]),
                                                    size=18, color=ACCENT)),
                                ft.Column(spacing=0, expand=True, controls=[
                                    ft.Text(p["name"], size=15,
                                            weight=ft.FontWeight.W_500,
                                            color=TEXT, no_wrap=True,
                                            max_lines=1,
                                            overflow=ft.TextOverflow.ELLIPSIS),
                                    ft.Text(" · ".join(tags) or " ", size=12,
                                            color=MUTED),
                                ]),
                            ]),
                            ft.Row(spacing=0, controls=actions),
                        ])))
            member_list.controls = rows or [
                ft.Text("Nobody here yet.", size=14, color=MUTED)]
            page.update()

        def kick(person):
            async def handler(e=None):
                try:
                    await call(api.remove_member, person["user_id"])
                    say(f"Removed {person['name']}.", error=False)
                    await load_members()
                except ApiError as ex:
                    say(str(ex))
            return handler

        def make_owner(person):
            async def handler(e=None):
                try:
                    await call(api.transfer_ownership, person["user_id"])
                    me.update(await call(api.whoami))
                    say(f"{person['name']} owns the household now.", error=False)
                    route_change()
                except ApiError as ex:
                    say(str(ex))
            return handler

        # ---- leaving and deleting ------------------------------------------
        confirm = {"action": None}
        confirm_note = ft.Text("", size=13, color=RED, width=cw - 32)
        confirm_row = ft.Row(spacing=8, visible=False)

        def ask(action, message):
            def handler(e=None):
                confirm["action"] = action
                confirm_note.value = message
                confirm_row.visible = True
                page.update()
            return handler

        def cancel_confirm(e=None):
            confirm["action"] = None
            confirm_note.value = ""
            confirm_row.visible = False
            page.update()

        async def do_confirm(e=None):
            action = confirm["action"]
            cancel_confirm()
            try:
                if action == "leave":
                    await call(api.leave_household)
                elif action == "delete_house":
                    await call(api.delete_household)
                elif action == "delete_account":
                    await call(api.delete_account)
                    me.clear()
                    fridge.pending.clear()
                    fridge.apply({})
                    await show("/login")
                    return
                me.clear()
                me.update(await call(api.whoami))
                fridge.pending.clear()
                fridge.apply({})
                await show("/setup")
            except ApiError as ex:
                say(str(ex))

        confirm_row.controls = [
            ft.Button(content=ft.Text("Yes, do it", size=14,
                                      weight=ft.FontWeight.W_600, width=90,
                                      text_align=ft.TextAlign.CENTER),
                      style=ft.ButtonStyle(
                          bgcolor=RED, color=ft.Colors.WHITE, elevation=0,
                          shape=ft.RoundedRectangleBorder(radius=12),
                          padding=ft.Padding(left=12, right=12, top=18, bottom=18)),
                      on_click=do_confirm),
            ft.Button(content=ft.Text("Cancel", size=14,
                                      weight=ft.FontWeight.W_600, width=70,
                                      text_align=ft.TextAlign.CENTER),
                      style=quiet_style(pad=12), on_click=cancel_confirm),
        ]

        def danger_button(label, handler):
            return ft.Button(
                content=ft.Text(label, size=14, weight=ft.FontWeight.W_600,
                                width=cw - 80, text_align=ft.TextAlign.CENTER),
                style=ft.ButtonStyle(
                    bgcolor=BG, color=RED, elevation=0,
                    shape=ft.RoundedRectangleBorder(radius=12),
                    padding=ft.Padding(left=16, right=16, top=18, bottom=18),
                    side=ft.BorderSide(1, DRAFT_BORDER)),
                on_click=handler)

        danger = [danger_button(
            "Leave this household",
            ask("leave", "You will lose access to this fridge. The items stay."))]
        if owner:
            danger.append(danger_button(
                "Delete this household",
                ask("delete_house",
                    "Deletes the fridge and its items for everyone. Permanent.")))
        danger.append(danger_button(
            "Delete my account",
            ask("delete_account", "Deletes your account for good. Permanent.")))

        asyncio.create_task(load_members())

        return ft.View(
            route="/settings", bgcolor=BG,
            appbar=app_bar("Settings"),
            controls=[screen(
                status,
                ft.Row(width=cw, alignment=ft.MainAxisAlignment.START,
                       controls=[section_label("YOU")]),
                card(
                    username,
                    ft.Text("Pick an avatar", size=13, color=MUTED),
                    avatar_row,
                    ft.Button(content=ft.Text("Save profile", size=14,
                                              weight=ft.FontWeight.W_600,
                                              width=cw - 80,
                                              text_align=ft.TextAlign.CENTER),
                              style=primary_style(pad=16), on_click=save_profile),
                ),
                ft.Row(width=cw, alignment=ft.MainAxisAlignment.START,
                       controls=[section_label("HOUSEHOLD")]),
                card(
                    house_name,
                    ft.Text("Pick an icon", size=13, color=MUTED),
                    house_row,
                    ft.Button(content=ft.Text("Save household", size=14,
                                              weight=ft.FontWeight.W_600,
                                              width=cw - 80,
                                              text_align=ft.TextAlign.CENTER),
                              style=primary_style(pad=16), on_click=save_house,
                              disabled=not owner),
                    ft.Text("" if owner else
                            "Only the person who created the household can change this.",
                            size=12, color=MUTED),
                ),
                ft.Row(width=cw, alignment=ft.MainAxisAlignment.START,
                       controls=[section_label("MEMBERS")]),
                card(member_list),
                ft.Row(width=cw, alignment=ft.MainAxisAlignment.START,
                       controls=[section_label("LEAVING", RED)]),
                card(*danger, confirm_note, confirm_row),
                ft.Container(height=8),
                ft.Button(content=ft.Text("Back to home", size=15,
                                          weight=ft.FontWeight.W_600,
                                          width=cw - 48,
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
        cap_field = field("Slots", width=70, value="",
                          keyboard_type=ft.KeyboardType.NUMBER)
        cap_save = ft.Button(
            content=ft.Text("Save", size=15, weight=ft.FontWeight.W_600,
                            width=38, text_align=ft.TextAlign.CENTER),
            style=primary_style(pad=12))
        cap_cancel = ft.Button(
            content=ft.Text("Cancel", size=15, weight=ft.FontWeight.W_600,
                            width=48, text_align=ft.TextAlign.CENTER),
            style=quiet_style(pad=12))
        editor = ft.Container(
            visible=False, width=cw, bgcolor=SURFACE,
            border=ft.Border.all(1, BORDER), border_radius=14,
            padding=ft.Padding(left=16, right=16, top=12, bottom=12),
            content=ft.Column(spacing=8, controls=[
                section_label("HOW MANY SLOTS DOES THE FRIDGE HOLD?"),
                ft.Row(spacing=8, controls=[cap_field, cap_save, cap_cancel]),
                ft.Text("Shared with everyone in the household.", size=12,
                        color=MUTED),
            ]),
        )

        def paint(message="", error=True):
            cap = fridge.capacity or 0
            used = fridge.used_slots()
            pct = used / cap if cap else 0
            headline.value = f"{used} / {cap}"
            sub.value = f"{pct * 100:.0f}% full  ·  {cap - used} slots free"
            bar.value = min(pct, 1.0)
            bar.color = GREEN if pct < 0.6 else AMBER if pct < 0.9 else RED
            status.value = message
            status.color = RED if error else MUTED
            item_list.controls = [
                item_row(i, server(i, -1), server(i, +1), server(i, None))
                for i in fridge.items
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
            try:
                await refresh_state()
                paint("")
            except ApiError as ex:
                paint(str(ex))

        def toggle_editor(e=None):
            editor.visible = not editor.visible
            cap_field.value = str(fridge.capacity or "")
            status.value = ""
            page.update()

        async def save_capacity(e=None):
            try:
                await call(api.set_capacity, cap_field.value)
                await refresh_state()
                editor.visible = False
                paint(f"Capacity is now {fridge.capacity}.", error=False)
            except ApiError as ex:
                paint(str(ex))

        cap_field.on_submit = save_capacity
        cap_save.on_click = save_capacity
        cap_cancel.on_click = toggle_editor

        paint("")

        return ft.View(
            route="/capacity", bgcolor=BG,
            appbar=app_bar("Current capacity", actions=[
                ft.IconButton(icon=ft.Icons.TUNE, icon_color=MUTED,
                              tooltip="Change capacity", on_click=toggle_editor),
                ft.IconButton(icon=ft.Icons.REFRESH, icon_color=MUTED,
                              tooltip="Refresh", on_click=do_refresh),
                ft.IconButton(icon=ft.Icons.SETTINGS_OUTLINED, icon_color=MUTED,
                              tooltip="Settings", on_click=go("/settings"))]),
            controls=[screen(
                ft.Container(height=8), headline, sub,
                ft.Container(height=8), bar,
                ft.Container(height=8), editor, status,
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
        try:
            me.clear()
            me.update(await call(api.whoami))
            if not me.get("household_id"):
                await show("/setup")
                return
            await refresh_state()
        except ApiError:
            # Could not reach the server, or the session is no longer good.
            await call(api.sign_out)
            me.clear()
            await show("/login")
            return
        # Keep the route the app was opened on. A PWA gets reloaded often, and
        # landing back on the home screen every time would be annoying.
        await show(page.route
                   if page.route in ("/insert", "/capacity", "/settings")
                   else "/")

    async def do_sign_out(e=None):
        try:
            await call(api.sign_out)
        except ApiError:
            pass                      # local-only; never block signing out
        me.clear()
        fridge.pending.clear()
        fridge.apply({})
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
            elif route == "/settings":
                page.views.append(settings_view())
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
            saved = await page.shared_preferences.get(TOKEN_KEY)
        except Exception:
            saved = None
        try:
            if saved and await call(api.resume, saved):
                await after_login()
                return
        except ApiError:
            pass
        await show("/login")

    asyncio.create_task(boot())
    route_change()


if __name__ == "__main__":
    ft.run(main)

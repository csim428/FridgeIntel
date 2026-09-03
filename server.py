"""ASGI entrypoint for hosting FridgeIntel.

Run locally:
    uvicorn server:app --host 0.0.0.0 --port 8000

The Python runs here on the server, not in the browser, so the data layer
works exactly as it does on a desktop. Each connected roommate gets their own
call to main(page) and therefore their own FridgeApi and Fridge instance.

Credentials come from SUPABASE_URL and SUPABASE_ANON_KEY in the environment;
see config.example.py for the local-development alternative.
"""

import flet as ft

from main import main

app = ft.run(main, view=ft.AppView.WEB_BROWSER, export_asgi_app=True)

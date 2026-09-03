"""ASGI entrypoint for hosting FridgeIntel.

Run locally:
    uvicorn server:app --host 0.0.0.0 --port 8000

The Python runs here on the server, not in the browser, so the data layer
works exactly as it does on a desktop. Each connected roommate gets their own
call to main(page) and therefore their own FridgeApi and Fridge instance.

Credentials come from SUPABASE_URL and SUPABASE_ANON_KEY in the environment;
see config.example.py for the local-development alternative.
"""

from flet_web.fastapi import app as flet_asgi_app

from main import main

# app_name / app_short_name feed the PWA manifest and the iOS web-app title.
# Without them the home-screen icon is labelled "Flet", which is not much use
# to a roommate looking for the fridge app.
app = flet_asgi_app(
    main,
    app_name="FridgeIntel",
    app_short_name="FridgeIntel",
    app_description="Shared fridge for the household",
)

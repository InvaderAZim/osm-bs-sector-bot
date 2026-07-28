import launcher
import postgres_backend  # uses DATABASE_URL for persistent users, SQLite only as fallback
import address_search_v2  # applies robust address-search overrides before startup
import map_picker_v2  # enables choosing any point directly on the map
import cancel_everywhere  # keeps Cancel visible and returns to the previous menu
import map_route_fix  # serves the interactive sector map without template errors
import radius_choice_v2  # asks for 1, 3, 5 or 10 km before visualization
import mini_app  # full Telegram Mini App interface
import access_enforcement  # immediately blocks revoked users in the Mini App and APIs
import broadcast_messages  # lets administrators send messages to all registered users
import user_profiles  # shows all users with buttons that open their Telegram profiles
import webhook_mode  # uses Telegram webhooks instead of unstable long polling

api = launcher.api

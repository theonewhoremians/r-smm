from urllib.parse import parse_qs, urlsplit

from app import App, ensure_db


class handler(App):
    def prepare(self):
        route = parse_qs(urlsplit(self.path).query).get("route")
        if route:
            self.path = "/api/" + route[0].lstrip("/")
        try:
            ensure_db()
            return True
        except Exception as error:
            print("R-SMM database unavailable:", type(error).__name__)
            self.respond(503, {"error": "The server database is not configured yet."})
            return False

    def do_GET(self):
        if self.prepare():
            super().do_GET()

    def do_POST(self):
        if self.prepare():
            super().do_POST()

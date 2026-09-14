"""Production WSGI entrypoint; importing workers never constructs this app."""
from webapp.public.server import create_app

app = create_app()

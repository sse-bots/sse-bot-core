import os
from threading import Thread
from flask import Flask

# This creates a global Flask app instance
app = Flask(__name__)


@app.route("/")
def home():
    return "Bot is running on Render!"


def run():
    # Render dynamically assigns a port
    port = int(os.environ.get("PORT", 8080))
    # Run the web server without the reloader in a background thread
    app.run(host="0.0.0.0", port=port, use_reloader=False)


def keep_alive():
    """
    Starts the Flask web server in a separate daemon thread so it doesn't
    block your Discord bot or prevent clean shutdowns on redeploy.
    """
    server_thread = Thread(target=run, daemon=True)
    server_thread.start()

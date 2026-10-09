import os
from urllib.parse import urlparse, urlunparse

import requests
from flask import Flask, jsonify, redirect, render_template, request, session, url_for

app = Flask(__name__, template_folder=".")
app.secret_key = os.environ.get("SESSION_SECRET", "local-dev-change-this-secret")
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "false").lower() == "true",
    MAX_CONTENT_LENGTH=32 * 1024,
)

COLDIQ_API_URL = "https://api.coldiq.com/v1/limadata/enrich/person"


@app.before_request
def require_app_password():
    if request.endpoint in {"login", "health", "static"}:
        return None
    if not os.environ.get("APP_PASSWORD"):
        return render_template("setup_error.html"), 503
    if not session.get("authenticated"):
        return redirect(url_for("login", next=request.path))
    return None


@app.get("/health")
def health():
    return jsonify({"ok": True})


@app.route("/login", methods=["GET", "POST"])
def login():
    configured = bool(os.environ.get("APP_PASSWORD"))
    error = None
    if request.method == "POST":
        if not configured:
            error = "Set APP_PASSWORD in your hosting environment first."
        elif request.form.get("password", "") == os.environ.get("APP_PASSWORD"):
            session.clear()
            session["authenticated"] = True
            next_path = request.args.get("next", "/")
            if not next_path.startswith("/") or next_path.startswith("//"):
                next_path = "/"
            return redirect(next_path)
        else:
            error = "That password didn't match. Try again."
    return render_template("login.html", error=error, configured=configured)


@app.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


def normalize_linkedin_url(raw_url):
    raw_url = (raw_url or "").strip()
    if not raw_url:
        raise ValueError("Paste a LinkedIn profile URL first.")
    if len(raw_url) > 2048:
        raise ValueError("That URL is too long.")
    if not raw_url.lower().startswith(("https://", "http://")):
        raw_url = "https://" + raw_url

    parsed = urlparse(raw_url)
    hostname = (parsed.hostname or "").lower()
    if not (hostname == "linkedin.com" or hostname.endswith(".linkedin.com")):
        raise ValueError("Use a LinkedIn profile URL, such as https://www.linkedin.com/in/name.")
    if not parsed.path or parsed.path == "/":
        raise ValueError("That looks like the LinkedIn homepage, not a profile URL.")

    # Strip tracking parameters and fragments before sending the URL to the API.
    return urlunparse(("https", parsed.netloc, parsed.path.rstrip("/"), "", "", ""))


@app.post("/api/enrich")
def enrich():
    api_key = os.environ.get("COLDIQ_API_KEY", "").strip()
    if not api_key:
        return jsonify({"error": "COLDIQ_API_KEY is not configured in the hosting environment."}), 503

    body = request.get_json(silent=True) or {}
    try:
        linkedin_url = normalize_linkedin_url(body.get("linkedin_url", ""))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    include_work_email = bool(body.get("include_work_email", True))
    include_phone = bool(body.get("include_phone", False))
    payload = {
        "linkedin_url": linkedin_url,
        "include_work_email": include_work_email,
        "include_phone": include_phone,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    try:
        response = requests.post(COLDIQ_API_URL, headers=headers, json=payload, timeout=60)
    except requests.Timeout:
        return jsonify({"error": "ColdIQ took too long to respond. Please try again."}), 504
    except requests.RequestException as exc:
        app.logger.warning("ColdIQ request failed: %s", exc.__class__.__name__)
        return jsonify({"error": "Could not reach ColdIQ. Check the server logs and try again."}), 502

    try:
        result = response.json()
    except ValueError:
        result = {"response_text": response.text[:12000]}

    if not response.ok:
        return jsonify({
            "error": f"ColdIQ returned HTTP {response.status_code}.",
            "details": result,
        }), 502

    return jsonify({"result": result})


@app.get("/")
def index():
    return render_template("index.html")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=False)

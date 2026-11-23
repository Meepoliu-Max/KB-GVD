"""Start an isolated Render review service with a single ordinary login."""

import os

def main() -> None:
    if os.environ.get("RENDER") != "true":
        raise SystemExit("Use this entry point only on Render")

    from kbrefiner.api.deps import get_settings_store, get_user_store, require_login_enabled
    from kbrefiner.config import get_settings

    if not require_login_enabled():
        raise SystemExit("Render review requires Render and mandatory login")

    settings_store = get_settings_store()
    if settings_store.get("access_allow_register"):
        raise SystemExit("Render review requires self-registration to be closed")

    email = os.environ.get("KBREFINER_REVIEW_EMAIL", "kbrefiner-review@example.com").strip().lower()
    password = os.environ.get("KBREFINER_REVIEW_PASSWORD", "")
    if "@" not in email or len(password) < 16:
        raise SystemExit("Configure a review account and a password of at least 16 characters")

    users = get_user_store()
    account = users.get_by_email(email)
    if account is None:
        users.create(email=email, username="评审体验", password=password)
    elif account["role"] != "user" or users.verify_login(email, password) is None:
        raise SystemExit("Review account does not match the configured ordinary login")

    for key, limit in {
        "quota_task_daily": 10,
        "quota_upload_daily": 10,
        "quota_token_daily": 100_000,
        "quota_file_size_mb": 5,
    }.items():
        if settings_store.meta(key) is None:
            settings_store.set(key, limit, updated_by="render-review")

    port = os.environ.get("PORT", str(get_settings().app_port))
    os.execvp("uvicorn", ["uvicorn", "kbrefiner.main:app", "--host", "0.0.0.0", "--port", port])


if __name__ == "__main__":
    main()

"""Start an isolated Render review service with dedicated reviewer logins."""

import os


def ensure_account(users, *, email: str, password: str, name: str, role: str) -> None:
    if "@" not in email or len(password) < 16:
        raise SystemExit("Configure a review login and a password of at least 16 characters")

    account = users.get_by_email(email)
    if account is None:
        users.create(email=email, username=name, password=password, role=role)
    elif account["role"] != role or users.verify_login(email, password) is None:
        raise SystemExit("An existing review account does not match its configured login")


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
    users = get_user_store()
    ensure_account(users, email=email, password=password, name="评审体验", role="user")

    admin_password = os.environ.get("KBREFINER_REVIEW_ADMIN_PASSWORD", "")
    if admin_password:
        admin_email = os.environ.get(
            "KBREFINER_REVIEW_ADMIN_EMAIL", "kbrefiner-review-admin@example.com",
        ).strip().lower()
        ensure_account(
            users, email=admin_email, password=admin_password, name="后台评审", role="admin",
        )

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

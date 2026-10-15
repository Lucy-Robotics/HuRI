import os
import platform
import uuid
from pathlib import Path


def get_config_dir() -> Path:
    """Cross-platform config directory."""
    system = platform.system()

    if system == "Windows":
        # TODO: To be tested -> also consider language-specific if needed
        base = os.environ.get("APPDATA", os.path.expanduser("~/AppData/Roaming"))
    elif system == "Darwin":
        # TODO: To be tested -> also consider language-specific if needed
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config"))

    config_dir = Path(base) / "huri"
    config_dir.mkdir(parents=True, exist_ok=True)
    return config_dir


def load_user_id(path: str | None = None) -> str | None:
    """Load existing _user_id, or return None if new user."""
    id_file: Path

    if path is None:
        id_file = get_config_dir() / "_user_id"
    else:
        id_file = Path(path)
    if id_file.exists():
        uid = id_file.read_text().strip()
        if uid:
            return uid
    return None


def save_user_id(_user_id: str, path: str | None = None):
    id_file: Path

    if path is None:
        id_file = get_config_dir() / "_user_id"
    else:
        id_file = Path(path)

    id_file.write_text(_user_id)
    if platform.system() != "Windows":
        id_file.chmod(0o600)


def get_or_create_and_save_user_id(path: str | None = None) -> str:
    """Load existing or generate new _user_id."""
    uid = load_user_id(path)
    if uid:
        return uid
    uid = str(uuid.uuid4())
    save_user_id(uid, path)
    return uid

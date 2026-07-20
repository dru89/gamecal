"""Config loading: config.toml at the repo/deploy root, path overridable via env."""

import os
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_PATH = Path(__file__).resolve().parents[2] / "config.toml"


@dataclass
class SteamConfig:
    api_key: str = ""
    steam_id: str = ""


@dataclass
class IgdbConfig:
    client_id: str = ""
    client_secret: str = ""


@dataclass
class SyncConfig:
    platforms: list[str] = field(default_factory=list)


@dataclass
class NotifyConfig:
    ntfy_url: str = ""  # e.g. https://ntfy.sh/<your-private-topic>


@dataclass
class Config:
    steam: SteamConfig
    igdb: IgdbConfig
    sync: SyncConfig
    notify: NotifyConfig
    data_dir: Path

    @property
    def ledger_path(self) -> Path:
        return self.data_dir / "ledger.db"


def load(path: str | os.PathLike | None = None) -> Config:
    cfg_path = Path(path or os.environ.get("GAMECAL_CONFIG", DEFAULT_PATH))
    raw: dict = {}
    if cfg_path.exists():
        with open(cfg_path, "rb") as f:
            raw = tomllib.load(f)
    else:
        print(f"warning: no config file at {cfg_path}, using empty config", file=sys.stderr)

    data_dir = Path(os.environ.get("GAMECAL_DATA", cfg_path.parent / "data"))
    data_dir.mkdir(parents=True, exist_ok=True)

    return Config(
        steam=SteamConfig(**raw.get("steam", {})),
        igdb=IgdbConfig(**raw.get("igdb", {})),
        sync=SyncConfig(**raw.get("sync", {})),
        notify=NotifyConfig(**raw.get("notify", {})),
        data_dir=data_dir,
    )

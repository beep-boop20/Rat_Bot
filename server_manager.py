import json
from typing import Dict, List, Optional

from paths import data_path

SERVERS_FILE = data_path("servers.json")


class ServerManager:
    def __init__(self):
        self.servers: List[Dict] = []
        self.current_server_id: Optional[int] = None
        self._last_loaded_mtime: Optional[float] = None
        self._load_servers(force=True)

    def _get_file_mtime(self) -> Optional[float]:
        try:
            return SERVERS_FILE.stat().st_mtime
        except OSError:
            return None

    def _normalize_server(self, server: Dict) -> Optional[Dict]:
        try:
            guild_id = int(server["id"])
        except (KeyError, TypeError, ValueError):
            return None

        name = str(server.get("name") or f"Server {guild_id}")
        icon_url = server.get("icon_url")
        db_path = server.get("db_path") or f"ratbot_{guild_id}.db"

        return {
            "id": guild_id,
            "name": name,
            "icon_url": icon_url,
            "db_path": db_path,
        }

    def _load_servers(self, force: bool = False) -> None:
        file_mtime = self._get_file_mtime()
        if not force and file_mtime == self._last_loaded_mtime:
            return

        if not SERVERS_FILE.exists():
            self.servers = []
            self.current_server_id = None
            self._last_loaded_mtime = file_mtime
            return

        try:
            with SERVERS_FILE.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            print(f"Error loading {SERVERS_FILE}: {exc}")
            self.servers = []
            self.current_server_id = None
            self._last_loaded_mtime = file_mtime
            return

        normalized_servers = []
        for raw_server in data.get("servers", []):
            normalized = self._normalize_server(raw_server)
            if normalized:
                normalized_servers.append(normalized)

        self.servers = normalized_servers
        try:
            current_server_id = data.get("current_server_id")
            self.current_server_id = int(current_server_id) if current_server_id is not None else None
        except (TypeError, ValueError):
            self.current_server_id = None

        if self.current_server_id and not any(server["id"] == self.current_server_id for server in self.servers):
            self.current_server_id = self.servers[0]["id"] if self.servers else None

        self._last_loaded_mtime = file_mtime

    def _ensure_loaded(self) -> None:
        self._load_servers()

    def _save_servers(self) -> None:
        data = {
            "servers": self.servers,
            "current_server_id": self.current_server_id,
        }
        temp_file = SERVERS_FILE.with_suffix(f"{SERVERS_FILE.suffix}.tmp")
        try:
            with temp_file.open("w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=4, ensure_ascii=False)
            temp_file.replace(SERVERS_FILE)
            self._last_loaded_mtime = self._get_file_mtime()
        except OSError as exc:
            print(f"Error saving {SERVERS_FILE}: {exc}")
            try:
                temp_file.unlink()
            except OSError:
                pass

    def add_server(self, guild_id: int, name: Optional[str] = None, icon_url: str = None) -> Dict:
        self._ensure_loaded()
        guild_id = int(guild_id)
        server = self.get_server(guild_id)

        if server:
            server["name"] = name or server.get("name") or f"Server {guild_id}"
            if icon_url is not None:
                server["icon_url"] = icon_url
        else:
            server = {
                "id": guild_id,
                "name": name or f"Server {guild_id}",
                "icon_url": icon_url,
                "db_path": f"ratbot_{guild_id}.db",
            }
            self.servers.append(server)

        if self.current_server_id is None:
            self.current_server_id = guild_id

        self._save_servers()
        return server

    def remove_server(self, guild_id: int) -> None:
        self._ensure_loaded()
        guild_id = int(guild_id)
        self.servers = [server for server in self.servers if server["id"] != guild_id]
        if self.current_server_id == guild_id:
            self.current_server_id = self.servers[0]["id"] if self.servers else None
        self._save_servers()

    def get_server(self, guild_id: int) -> Optional[Dict]:
        self._ensure_loaded()
        guild_id = int(guild_id)
        for server in self.servers:
            if server["id"] == guild_id:
                return server
        return None

    def get_all_servers(self) -> List[Dict]:
        self._ensure_loaded()
        return [dict(server) for server in self.servers]

    def set_current_server(self, guild_id: int) -> Dict:
        self._ensure_loaded()
        server = self.get_server(guild_id)
        if not server:
            raise ValueError(f"Server {guild_id} not found")
        self.current_server_id = server["id"]
        self._save_servers()
        return server

    def get_current_server(self) -> Optional[Dict]:
        self._ensure_loaded()
        if self.current_server_id is None:
            return self.servers[0] if self.servers else None
        return self.get_server(self.current_server_id)

    def get_db_path(self, guild_id: int) -> str:
        self._ensure_loaded()
        server = self.get_server(guild_id)
        if server:
            return server.get("db_path", f"ratbot_{guild_id}.db")
        return f"ratbot_{guild_id}.db"


server_manager = ServerManager()

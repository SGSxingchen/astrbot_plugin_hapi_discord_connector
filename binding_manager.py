"""会话订阅管理器：记录 session 与 Discord 窗口的多对多 join/leave 关系。"""


class BindingManager:
    """管理 session -> 多窗口订阅 与 窗口 -> 多 session 订阅。"""

    def __init__(self):
        self._session_owners: dict[str, list[str]] = {}  # {session_id: [umo, ...]}
        self._window_sessions: dict[str, list[str]] = {}  # {umo: [session_id, ...]}
        self._session_flavor: dict[str, str] = {}  # {session_id: flavor}

    @staticmethod
    def _append_unique(items: list[str], value: str) -> None:
        if value and value not in items:
            items.append(value)

    def join(self, session_id: str, umo: str, flavor: str):
        """让窗口加入 session 订阅；双向关系幂等维护。"""
        sid = str(session_id or "").strip()
        target_umo = str(umo or "").strip()
        if not sid or not target_umo:
            return

        owners = self._session_owners.setdefault(sid, [])
        self._append_unique(owners, target_umo)

        sessions = self._window_sessions.setdefault(target_umo, [])
        self._append_unique(sessions, sid)

        flavor_value = str(flavor or "unknown").strip() or "unknown"
        self._session_flavor[sid] = flavor_value

    def leave(self, session_id: str, umo: str):
        """让窗口退出 session 订阅；幂等。"""
        sid = str(session_id or "").strip()
        target_umo = str(umo or "").strip()
        if not sid or not target_umo:
            return

        owners = self._session_owners.get(sid)
        if owners is not None:
            self._session_owners[sid] = [owner for owner in owners if owner != target_umo]
            if not self._session_owners[sid]:
                self._session_owners.pop(sid, None)
                self._session_flavor.pop(sid, None)

        sessions = self._window_sessions.get(target_umo)
        if sessions is not None:
            self._window_sessions[target_umo] = [s for s in sessions if s != sid]
            if not self._window_sessions[target_umo]:
                self._window_sessions.pop(target_umo, None)

    def leave_window(self, umo: str):
        """窗口退出所有 session 订阅；幂等。"""
        target_umo = str(umo or "").strip()
        if not target_umo:
            return
        for sid in list(self._window_sessions.get(target_umo, [])):
            self.leave(sid, target_umo)

    def leave_session(self, session_id: str) -> list[str]:
        """session 被所有窗口退出；返回此前订阅窗口。"""
        sid = str(session_id or "").strip()
        if not sid:
            return []
        owners = list(self._session_owners.get(sid, []))
        for umo in owners:
            self.leave(sid, umo)
        self._session_flavor.pop(sid, None)
        return owners

    def get_owners(self, session_id: str) -> list[str]:
        """获取订阅该 session 的所有窗口。"""
        return list(self._session_owners.get(str(session_id or ""), []))

    def get_window_sessions(self, umo: str) -> list[str]:
        """获取窗口已加入的所有 session ID。"""
        return list(self._window_sessions.get(str(umo or ""), []))

    def get_session_flavor(self, session_id: str) -> str | None:
        """获取 session 的 flavor。"""
        return self._session_flavor.get(str(session_id or ""))

    def filter_by_flavor(self, sessions: list[dict], flavor: str) -> list[dict]:
        """按 flavor 过滤 session 列表。"""
        if flavor == "all":
            return sessions
        return [s for s in sessions if s.get("metadata", {}).get("flavor") == flavor]

    def get_all_bindings(self) -> dict[str, list[str]]:
        """获取所有订阅关系。"""
        return {sid: list(owners) for sid, owners in self._session_owners.items()}

    def reset_all_states(self):
        """重置所有订阅状态。"""
        self._session_owners.clear()
        self._window_sessions.clear()
        self._session_flavor.clear()

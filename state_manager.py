"""用户状态和通知窗口订阅管理"""

import inspect

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from .binding_manager import BindingManager

NOTIFICATION_ROUTE_FLAVORS = ("claude", "codex", "gemini")


class StateManager:
    """管理用户状态、session join/leave 订阅、通知路由。"""

    def __init__(self, kv_helper, binding_mgr: BindingManager):
        self.kv = kv_helper
        self.binding_mgr = binding_mgr
        self._user_states_cache: dict[str, dict] = {}
        # Persisted, user-independent index of known notification windows.
        self._primary_windows_cache: list[str] = []
        self._flavor_primary_windows_cache: dict[str, list[str]] = {}
        self._session_owners = binding_mgr._session_owners

    # ──── 持久化 ────

    async def persist_session_owners(self):
        """持久化 session -> 多窗口订阅关系。"""
        await self.kv.put_kv_data("dhapi_session_owners", self.binding_mgr.get_all_bindings())

    async def _delete_kv_key(self, key: str):
        """删除 KV key；优先使用 delete_kv_data，兼容旧 KV helper 的 put None。"""
        delete = getattr(self.kv, "delete_kv_data", None)
        if callable(delete):
            result = delete(key)
            if inspect.isawaitable(result):
                await result
            return
        await self.kv.put_kv_data(key, None)

    async def _list_legacy_window_state_keys(self, known_umos: list[str]) -> list[str]:
        """列出旧 dhapi_window_state_* key。

        AstrBot KV 若提供 list_keys/list_kv_keys/get_kv_keys 就按 prefix 真删除；
        否则只能删除本次迁移能从 owner/known_chats/default windows 推导出的固定 key。
        测试 KV 暴露 data 时也按 prefix 扫描。
        """
        keys: list[str] = []
        seen: set[str] = set()

        def add(key: str):
            if key.startswith("dhapi_window_state_") and key not in seen:
                seen.add(key)
                keys.append(key)

        for umo in known_umos:
            target = str(umo or "").strip()
            if target:
                add(f"dhapi_window_state_{target}")

        for method_name in ("list_keys", "list_kv_keys", "get_kv_keys"):
            method = getattr(self.kv, method_name, None)
            if not callable(method):
                continue
            try:
                result = method("dhapi_window_state_")
            except TypeError:
                result = method()
            if inspect.isawaitable(result):
                result = await result
            if isinstance(result, list):
                for key in result:
                    add(str(key))
                return keys

        data = getattr(self.kv, "data", None)
        if isinstance(data, dict):
            for key in data.keys():
                add(str(key))

        return keys

    async def _delete_known_window_state_keys(self, umos: list[str]):
        """尽力清理旧的 dhapi_window_state_{umo} KV。"""
        for key in await self._list_legacy_window_state_keys(umos):
            await self._delete_kv_key(key)

    # ──── Session 订阅 ────

    async def join_session(self, session_id: str, umo: str, flavor: str):
        """当前窗口加入 session 订阅；操作和通知订阅不再独占。"""
        self.binding_mgr.join(session_id, umo, flavor)
        logger.info(
            "[dhapi] join_session sid=%s umo=%s flavor=%s owners=%s",
            session_id[:8] if session_id else "",
            umo,
            flavor,
            self.binding_mgr.get_owners(session_id),
        )
        await self.persist_session_owners()

    async def leave_session(self, session_id: str, umo: str):
        """窗口退出指定 session 订阅。"""
        self.binding_mgr.leave(session_id, umo)
        logger.info(
            "[dhapi] leave_session sid=%s umo=%s owners=%s",
            session_id[:8] if session_id else "",
            umo,
            self.binding_mgr.get_owners(session_id),
        )
        await self.persist_session_owners()

    async def leave_window(self, umo: str):
        """窗口退出所有 session 订阅。"""
        self.binding_mgr.leave_window(umo)
        await self.persist_session_owners()
        await self._delete_known_window_state_keys([umo])

    async def leave_all_session_owners(self, session_id: str):
        """session 被所有窗口退出；通常在删除/归档后调用。"""
        released_umos = self.binding_mgr.leave_session(session_id)
        await self.persist_session_owners()
        await self._delete_known_window_state_keys(released_umos)

    # ──── 用户状态 ────

    def get_user_state(self, event: AstrMessageEvent) -> dict:
        sender_id = str(event.get_sender_id())
        return self._user_states_cache.get(sender_id, {})

    async def set_user_state(self, event: AstrMessageEvent, **kwargs):
        sender_id = str(event.get_sender_id())
        state = dict(self._user_states_cache.get(sender_id, {}))
        if kwargs:
            state.update(kwargs)
            self._user_states_cache[sender_id] = state
            await self.kv.put_kv_data(f"dhapi_user_state_{sender_id}", state)
            await self.persist_notification_windows_index()
        elif sender_id not in self._user_states_cache:
            self._user_states_cache[sender_id] = state

        # 维护 known_users 列表
        known = [str(uid) for uid in await self.kv.get_kv_data("dhapi_known_users", [])]
        if sender_id not in known:
            known.append(sender_id)
            await self.kv.put_kv_data("dhapi_known_users", known)

    async def ensure_primary_session(self, event: AstrMessageEvent):
        """确保用户已有默认通知窗口；仅首次自动设置。"""
        sender_id = str(event.get_sender_id())
        umo = event.unified_msg_origin
        state = self._user_states_cache.get(sender_id, {})
        if not state.get("primary_umo"):
            await self.set_user_state(event, primary_umo=umo)
            logger.info(
                "设置用户 %s 的主会话: %s",
                sender_id,
                umo[:20] if len(umo) > 20 else umo,
            )
        else:
            await self.set_user_state(event)

    async def set_primary_window(self, event: AstrMessageEvent, umo: str | None = None):
        """显式设置当前用户的主通知窗口。"""
        target_umo = umo or event.unified_msg_origin
        await self.set_user_state(event, primary_umo=target_umo)
        logger.info(
            "[dhapi] set_primary_window user=%s umo=%s",
            event.get_sender_id(),
            target_umo,
        )

    # ──── 状态查询 ────

    def primary_umo(self, event: AstrMessageEvent) -> str | None:
        """获取当前用户配置的默认通知窗口。"""
        state = self.get_user_state(event)
        primary_umo = state.get("primary_umo")
        return str(primary_umo) if primary_umo else None

    @staticmethod
    def normalized_flavor_primary_umos(state: dict) -> dict[str, str]:
        """Normalize persisted flavor -> default window mappings."""
        raw = state.get("flavor_primary_umos", {})
        if not isinstance(raw, dict):
            return {}

        normalized: dict[str, str] = {}
        for flavor, umo in raw.items():
            flavor_key = str(flavor).strip().lower()
            target_umo = str(umo).strip() if umo is not None else ""
            if flavor_key in NOTIFICATION_ROUTE_FLAVORS and target_umo:
                normalized[flavor_key] = target_umo
        return normalized

    def flavor_primary_umos(self, event: AstrMessageEvent) -> dict[str, str]:
        """Get current user's flavor-specific default notification windows."""
        return self.normalized_flavor_primary_umos(self.get_user_state(event))

    def flavor_primary_umo(
        self, event: AstrMessageEvent, flavor: str | None
    ) -> str | None:
        """Get current user's flavor-specific default notification window."""
        if not flavor:
            return None
        return self.flavor_primary_umos(event).get(str(flavor).strip().lower())

    def resolve_target_sid(
        self, event: AstrMessageEvent, session_id: str | None
    ) -> tuple[str | None, str]:
        """解析工具操作目标 session。

        传入 session_id 时直接返回该 sid；未传时仅当当前窗口恰好 join 一个
        session 才返回它，否则返回 no_session / ambiguous。
        """
        explicit_sid = str(session_id or "").strip()
        if explicit_sid:
            return explicit_sid, "ok"

        joined = self.binding_mgr.get_window_sessions(event.unified_msg_origin)
        if len(joined) == 1:
            return joined[0], "ok"
        if not joined:
            return None, "no_session"
        return None, "ambiguous"

    def visible_sessions_for_window(
        self, event: AstrMessageEvent, sessions_cache: list[dict]
    ) -> list[dict]:
        """返回当前窗口已订阅或可通过默认路由接收通知的 session 列表。"""
        current_umo = event.unified_msg_origin
        primary_umo = self.primary_umo(event)
        flavor_umos = self.flavor_primary_umos(event)
        visible_sessions: list[dict] = []

        for session in sessions_cache:
            sid = session.get("id")
            if not sid:
                continue

            owners = self.binding_mgr.get_owners(sid)
            if current_umo in owners:
                visible_sessions.append(session)
                continue

            if owners:
                continue

            flavor = str(session.get("metadata", {}).get("flavor", "")).strip().lower()
            flavor_umo = flavor_umos.get(flavor)
            if flavor_umo:
                if flavor_umo == current_umo:
                    visible_sessions.append(session)
                continue

            if primary_umo == current_umo:
                visible_sessions.append(session)

        return visible_sessions

    # ──── 路由管理 ────

    @staticmethod
    def _append_unique(targets: list[str], seen: set[str], umo: str | None):
        target_umo = str(umo).strip() if umo is not None else ""
        if target_umo and target_umo not in seen:
            seen.add(target_umo)
            targets.append(target_umo)

    def _rebuild_notification_windows_index(self):
        """Rebuild in-memory fallback notification windows from user states."""
        primary_targets: list[str] = []
        primary_seen: set[str] = set()
        flavor_targets: dict[str, list[str]] = {}
        flavor_seen: dict[str, set[str]] = {}

        for state in self._user_states_cache.values():
            self._append_unique(primary_targets, primary_seen, state.get("primary_umo"))
            for flavor, umo in self.normalized_flavor_primary_umos(state).items():
                flavor_targets.setdefault(flavor, [])
                flavor_seen.setdefault(flavor, set())
                self._append_unique(flavor_targets[flavor], flavor_seen[flavor], umo)

        if primary_targets:
            self._primary_windows_cache = primary_targets
        if flavor_targets:
            self._flavor_primary_windows_cache = flavor_targets

    async def persist_notification_windows_index(self):
        """Persist user-independent notification fallback windows."""
        self._rebuild_notification_windows_index()
        await self.kv.put_kv_data("dhapi_primary_windows", self._primary_windows_cache)
        await self.kv.put_kv_data(
            "dhapi_flavor_primary_windows", self._flavor_primary_windows_cache
        )

    def get_flavor_primary_windows(self, flavor: str | None) -> list[str]:
        """Return all configured default windows for the given flavor across users."""
        if not flavor:
            return []

        flavor_key = str(flavor).strip().lower()
        targets: list[str] = []
        seen: set[str] = set()
        for state in self._user_states_cache.values():
            self._append_unique(
                targets, seen, self.normalized_flavor_primary_umos(state).get(flavor_key)
            )
        for umo in self._flavor_primary_windows_cache.get(flavor_key, []):
            self._append_unique(targets, seen, umo)
        return targets

    def get_primary_windows(self) -> list[str]:
        """返回所有用户当前生效的默认通知窗口（去重后）。"""
        targets: list[str] = []
        seen: set[str] = set()
        for state in self._user_states_cache.values():
            self._append_unique(targets, seen, state.get("primary_umo"))
        for umo in self._primary_windows_cache:
            self._append_unique(targets, seen, umo)
        for umo in self.binding_mgr._window_sessions.keys():
            self._append_unique(targets, seen, umo)
        return targets

    def select_notification_targets(
        self, session_id: str, sessions_cache: list[dict]
    ) -> list[str]:
        """根据 session 选择最终通知窗口；多订阅窗口群发，返回值已去重。"""
        reason = "none"
        targets: list[str] = []
        seen: set[str] = set()

        if session_id:
            owners = self.binding_mgr.get_owners(session_id)
            if owners:
                for umo in owners:
                    self._append_unique(targets, seen, umo)
                reason = "session_owners"
            else:
                session = next(
                    (s for s in sessions_cache if s.get("id") == session_id),
                    None,
                )
                flavor = session.get("metadata", {}).get("flavor") if session else None
                flavor_targets = self.get_flavor_primary_windows(
                    str(flavor).strip().lower() if flavor else None
                )
                if flavor_targets:
                    for umo in flavor_targets:
                        self._append_unique(targets, seen, umo)
                    reason = f"flavor_primary:{flavor}"

        if not targets:
            for umo in self.get_primary_windows():
                self._append_unique(targets, seen, umo)
            if targets:
                reason = "primary"

        logger.info(
            "[dhapi] select_notification_targets sid=%s reason=%s targets=%s owners=%s primary_windows=%s",
            session_id[:8] if session_id else "global",
            reason,
            targets,
            self.binding_mgr.get_owners(session_id) if session_id else [],
            self.get_primary_windows(),
        )
        return targets

    @staticmethod
    def format_umo_for_display(umo: str | None, max_len: int = 40) -> str:
        if not umo:
            return ""
        return umo[:max_len] + "..." if len(umo) > max_len else umo

    def user_route_summary_lines(self, event: AstrMessageEvent) -> list[str]:
        """Format current user's default notification routing summary."""
        state = self.get_user_state(event)
        lines: list[str] = []

        primary = state.get("primary_umo")
        if primary:
            lines.append(f"默认发送窗口: {self.format_umo_for_display(str(primary))}")

        flavor_routes = self.normalized_flavor_primary_umos(state)
        if flavor_routes:
            lines.append("Flavor 默认窗口:")
            for flavor in sorted(flavor_routes):
                lines.append(
                    f"  {flavor}: {self.format_umo_for_display(flavor_routes[flavor])}"
                )

        return lines

    # ──── 数据加载 ────

    async def load_all(self):
        """从 KV 加载所有状态。"""
        primary_windows = await self.kv.get_kv_data("dhapi_primary_windows", [])
        if isinstance(primary_windows, list):
            seen: set[str] = set()
            self._primary_windows_cache = []
            for umo in primary_windows:
                self._append_unique(self._primary_windows_cache, seen, umo)

        flavor_windows = await self.kv.get_kv_data("dhapi_flavor_primary_windows", {})
        if isinstance(flavor_windows, dict):
            self._flavor_primary_windows_cache = {}
            for flavor, umos in flavor_windows.items():
                flavor_key = str(flavor).strip().lower()
                if flavor_key not in NOTIFICATION_ROUTE_FLAVORS or not isinstance(
                    umos, list
                ):
                    continue
                targets: list[str] = []
                seen: set[str] = set()
                for umo in umos:
                    self._append_unique(targets, seen, umo)
                if targets:
                    self._flavor_primary_windows_cache[flavor_key] = targets

        known_users = await self.kv.get_kv_data("dhapi_known_users", [])
        for uid in known_users:
            uid = str(uid)
            state = await self.kv.get_kv_data(f"dhapi_user_state_{uid}", None)
            if isinstance(state, dict):
                # 旧 current_* 字段只在 migrate_legacy_owner_state 中处理；运行态不再使用。
                self._user_states_cache[uid] = state

        stored_session_owners = await self.kv.get_kv_data("dhapi_session_owners", {})
        if isinstance(stored_session_owners, dict):
            for sid, umos in stored_session_owners.items():
                sid = str(sid or "").strip()
                if not sid:
                    continue
                owner_list: list[str]
                if isinstance(umos, list):
                    owner_list = [str(umo).strip() for umo in umos if str(umo).strip()]
                elif isinstance(umos, str):
                    owner_list = [umos.strip()] if umos.strip() else []
                else:
                    owner_list = []
                for umo in owner_list:
                    self.binding_mgr.join(sid, umo, self.binding_mgr.get_session_flavor(sid) or "unknown")

        # 忽略并尽力清理已知旧窗口当前状态 KV。
        legacy_umos: list[str] = []
        known_chats = await self.kv.get_kv_data("dhapi_known_chats", [])
        if isinstance(known_chats, list):
            legacy_umos.extend(known_chats)
        legacy_umos.extend(self.get_primary_windows())
        for owners in self.binding_mgr._session_owners.values():
            legacy_umos.extend(owners)
        await self._delete_known_window_state_keys(legacy_umos)

        await self.persist_notification_windows_index()

    async def migrate_legacy_owner_state(self):
        """数据迁移：旧 owner/current_session 状态 → join/leave 多对多订阅模型。"""
        migrated = False
        legacy_umos: list[str] = []

        for uid, state in list(self._user_states_cache.items()):
            modified = False

            if "notify_umo" in state and not state.get("primary_umo"):
                state["primary_umo"] = state["notify_umo"]
                modified = True
                logger.info("迁移用户 %s: notify_umo → primary_umo", uid)

            if "notify_umo" in state:
                del state["notify_umo"]
                modified = True

            old_session = state.get("current_session")
            old_flavor = state.get("current_flavor") or "unknown"
            if old_session:
                target_umo = state.get("primary_umo")
                owners = self.binding_mgr.get_owners(str(old_session))
                if owners:
                    target_umo = owners[-1]
                if target_umo:
                    legacy_umos.append(str(target_umo))
                    self.binding_mgr.join(str(old_session), str(target_umo), str(old_flavor))
                    migrated = True
                    logger.info(
                        "迁移用户 %s: current_session → join[%s]",
                        uid,
                        str(target_umo)[:20],
                    )

            if "current_session" in state:
                del state["current_session"]
                modified = True
            if "current_flavor" in state:
                del state["current_flavor"]
                modified = True

            if modified:
                self._user_states_cache[uid] = state
                await self.kv.put_kv_data(f"dhapi_user_state_{uid}", state)
                migrated = True

        known_chats = await self.kv.get_kv_data("dhapi_known_chats", [])
        if known_chats:
            for umo in known_chats:
                await self._delete_kv_key(f"dhapi_chat_binding_{umo}")
            legacy_umos.extend(str(umo) for umo in known_chats)
            logger.info("已清理 %d 个废弃的 chat_binding/window_state 数据", len(known_chats))
            migrated = True

        legacy_umos.extend(self.get_primary_windows())
        for owners in self.binding_mgr._session_owners.values():
            legacy_umos.extend(owners)
        await self._delete_known_window_state_keys(legacy_umos)

        if migrated:
            await self.persist_session_owners()
            await self.persist_notification_windows_index()
            logger.info("数据迁移完成")

"""SessionMap — bidirectional zed_sid <-> child_sid index (corr-2)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from playmaker.acp.pending import PendingTables

if TYPE_CHECKING:
    from playmaker.acp.child import ChildHandle


ZedSid = str
ChildSid = str


@dataclass
class ChildSession:
    """One Zed session backed by one child subprocess.

    (corr-2) Lifetime ends only on:
      - Zed's `session/close` response (if agent declared
        sessionCapabilities.close, which both Claude and Codex do); OR
      - child unexpected death; OR
      - playmaker shutdown.
    Cancel never closes the mapping (corr-1).
    (corr-15) Multiple in-flight prompts to the same session are valid
    when promptQueueing is declared — pending tables are keyed by
    jsonrpc id, not sessionId.
    """

    child_sid: ChildSid
    handle: "ChildHandle"
    pending: PendingTables = field(default_factory=PendingTables)
    closed: bool = False


class SessionMap:
    """Bidirectional index keyed by both sides' session ids.

    Phase-1 only knows its own children's child_sids. Unknown sids in
    `session/list` responses (Phase 2) need cross-child aggregation.
    """

    def __init__(self) -> None:
        self._by_zed: dict[ZedSid, ChildSession] = {}
        self._by_child: dict[ChildSid, ZedSid] = {}

    @staticmethod
    def mint_zed_sid() -> ZedSid:
        # UUID4 — opaque to Zed. We MUST NOT reuse the child's sid as
        # our zed-side sid, because then a multi-child Phase-2 proxy
        # would have collisions across children.
        return str(uuid.uuid4())

    def register(self, *, zed_sid: ZedSid, session: ChildSession) -> None:
        if zed_sid in self._by_zed:
            raise ValueError(f"zed_sid already registered: {zed_sid}")
        if session.child_sid in self._by_child:
            raise ValueError(f"child_sid already registered: {session.child_sid}")
        self._by_zed[zed_sid] = session
        self._by_child[session.child_sid] = zed_sid

    def unregister(self, zed_sid: ZedSid) -> ChildSession | None:
        session = self._by_zed.pop(zed_sid, None)
        if session is not None:
            self._by_child.pop(session.child_sid, None)
        return session

    def by_zed(self, zed_sid: ZedSid) -> ChildSession | None:
        return self._by_zed.get(zed_sid)

    def by_child(self, child_sid: ChildSid) -> tuple[ZedSid, ChildSession] | None:
        zed_sid = self._by_child.get(child_sid)
        if zed_sid is None:
            return None
        return zed_sid, self._by_zed[zed_sid]

    def all_sessions(self) -> list[ChildSession]:
        return list(self._by_zed.values())

    def __len__(self) -> int:
        return len(self._by_zed)

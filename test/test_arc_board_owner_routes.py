"""Owner routes for the arc board console (the ``/api/arc-board`` prefix).

The members' ``/api/arcs`` routes are covered in ``test_arcs_routes.py``; these
are the dashboard owner's browser routes: the owner gate, the owner's writes
(create, add-member, answer, accept) and the redaction of agent text on the way
out.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from dashboard_owner_helpers import as_owner

MEMBER = "chat-arc-member"
PEER = "chat-arc-peer"
NEW = "chat-arc-new"
SECRET = "sk-ant-api03-" + "A" * 40


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))


class FakeChannel:
    """Just enough of ``Channel`` for the arc routes: attach, post, remove."""

    def __init__(self, cid: str, topic: str = "") -> None:
        from types import SimpleNamespace

        self.id, self.topic = cid, topic
        self.members: dict[str, Any] = {}
        self.orchestrator_id: str | None = None
        self.posts: list[dict[str, Any]] = []
        self._ns = SimpleNamespace

    def attach_session(self, session_key, role, agent_name="", listen_mode="all"):
        if any(m.session_key == session_key for m in self.members.values()):
            return None
        mid = f"m{len(self.members) + 1}"
        member = self._ns(id=mid, session_key=session_key, role=role, listen_mode=listen_mode)
        self.members[mid] = member
        self.orchestrator_id = self.orchestrator_id or mid
        return member

    async def post(self, from_id, content, **kwargs):
        self.posts.append({"from_id": from_id, "content": content, **kwargs})

    def remove_agent(self, agent_id, reason=""):
        return self.members.pop(agent_id, None) is not None


class FakeChannels:
    def __init__(self) -> None:
        self.channels: dict[str, FakeChannel] = {}
        self.closed: list[str] = []

    def create(self, topic: str) -> FakeChannel:
        ch = FakeChannel(f"c{len(self.channels) + 1:07d}", topic)
        self.channels[ch.id] = ch
        return ch

    def get(self, cid: str) -> FakeChannel | None:
        return self.channels.get(cid)

    def close(self, cid: str) -> bool:
        self.closed.append(cid)
        return self.channels.pop(cid, None) is not None


@pytest.fixture()
def channels() -> FakeChannels:
    manager = FakeChannels()
    # The shared channel the pilot arcs use, with the member lane already in it.
    shared = FakeChannel("ch1", "shared")
    shared.attach_session(f"dashboard:{MEMBER}", "builder")
    manager.channels["ch1"] = shared
    return manager


@pytest_asyncio.fixture()
async def client(channels: FakeChannels):
    from types import SimpleNamespace

    from kiro_crew.dashboard.routes import arc_board as routes

    # The gate reads state.owner_id (must stay ""); a raw MagicMock would make it
    # truthy and flip the gate to the 401 stale-owner branch.
    slots = {
        key: SimpleNamespace(key=key, agent="tenon", linked_session_key="")
        for key in (MEMBER, PEER, NEW)
    }
    app = web.Application()
    as_owner(app)
    app["state"] = SimpleNamespace(
        owner_id="",
        channel_manager=channels,
        _slots=slots,
        notify=lambda *args, **kwargs: None,
    )
    routes.register(app)
    async with TestClient(TestServer(app)) as client:
        yield client


def _created(payload: dict[str, Any]) -> dict[str, Any]:
    return payload["arc"]


async def _make_arc(client: TestClient, **over: Any) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "title": "Console pilot",
        "goal": "ship the board",
        "reason": "the pilot",
        "lineage": "owner's product goal",
        "autonomy": 2,
        "channel": {"mode": "existing", "id": "ch1"},
        "milestones": [
            {"name": "m1", "criteria": "works"},
            {"name": "m2", "criteria": "proven"},
        ],
    }
    spec.update(over)
    response = await client.post("/api/arc-board/arcs", json=spec)
    assert response.status == 200, await response.text()
    return _created(await response.json())


@pytest.mark.asyncio
async def test_list_is_empty_until_an_arc_exists(client: TestClient) -> None:
    response = await client.get("/api/arc-board/arcs")
    assert response.status == 200
    assert (await response.json())["arcs"] == []


@pytest.mark.asyncio
async def test_create_adds_members_and_lists_the_arc(client: TestClient) -> None:
    arc = await _make_arc(
        client,
        members=[
            {"session_key": MEMBER, "role": "builder"},
            {"session_key": PEER, "role": "reviewer"},
        ],
    )
    assert set(arc["members"]) == {MEMBER, PEER}
    response = await client.get("/api/arc-board/arcs")
    rows = (await response.json())["arcs"]
    assert [row["id"] for row in rows] == [arc["id"]]
    assert rows[0]["milestone"] == "m1"
    assert rows[0]["milestones_total"] == 2
    assert rows[0]["members"] == 2


@pytest.mark.asyncio
async def test_create_refuses_a_spec_without_a_title(client: TestClient) -> None:
    response = await client.post("/api/arc-board/arcs", json={"goal": "no title"})
    assert response.status == 400
    assert (await response.json())["code"] == "arc_validation"


@pytest.mark.asyncio
async def test_detail_covers_brief_entries_asks_and_claims(client: TestClient) -> None:
    from kiro_crew import arcs

    arc = await _make_arc(client, members=[{"session_key": MEMBER, "role": "builder"}])
    arcs.append_entry(
        arc["id"],
        MEMBER,
        {"kind": "ASK", "text": "which store?", "to": arcs.OWNER, "step": "board"},
    )
    arcs.append_entry(
        arc["id"],
        MEMBER,
        {
            "kind": "CLAIM",
            "text": "on the feed",
            "step": "feed",
            "checked": ["board spec"],
        },
    )
    response = await client.get(f"/api/arc-board/arcs/{arc['id']}")
    assert response.status == 200
    body = await response.json()
    assert body["arc"]["id"] == arc["id"]
    assert body["briefs"][MEMBER].startswith("Goal lineage")
    assert [lane["key"] for lane in body["lanes"]] == [MEMBER]
    assert body["lanes"][0]["claim"]["step"] == "feed"
    assert body["lanes"][0]["entries"] == 2
    assert body["collisions"] == []
    assert [e["kind"] for e in body["entries"]] == ["ASK", "CLAIM"]
    assert [a["to"] for a in body["open_asks"]] == [arcs.OWNER]
    assert [c["author"] for c in body["open_claims"]] == [MEMBER]
    assert body["open_asks"][0]["seen_by"] == []


@pytest.mark.asyncio
async def test_detail_entry_text_is_redacted(client: TestClient) -> None:
    from kiro_crew import arcs

    arc = await _make_arc(client, members=[{"session_key": MEMBER, "role": "builder"}])
    arcs.append_entry(arc["id"], MEMBER, {"kind": "FACT", "text": f"key {SECRET}"})
    body = await (await client.get(f"/api/arc-board/arcs/{arc['id']}")).json()
    assert SECRET not in json.dumps(body)


@pytest.mark.asyncio
async def test_detail_for_unknown_arc_is_404(client: TestClient) -> None:
    response = await client.get("/api/arc-board/arcs/arc_" + "0" * 32)
    assert response.status == 404
    assert (await response.json())["code"] == "arc_not_found"


@pytest.mark.asyncio
async def test_add_member_and_re_role(client: TestClient) -> None:
    arc = await _make_arc(client)
    response = await client.post(
        f"/api/arc-board/arcs/{arc['id']}/members",
        json={"session_key": MEMBER, "role": "builder"},
    )
    assert response.status == 200
    arc = (await response.json())["arc"]
    assert arc["members"][MEMBER]["role"] == "builder"
    response = await client.post(
        f"/api/arc-board/arcs/{arc['id']}/members",
        json={"session_key": MEMBER, "role": "integrator"},
    )
    assert (await response.json())["arc"]["members"][MEMBER]["role"] == "integrator"


@pytest.mark.asyncio
async def test_add_member_refuses_the_owner_key(client: TestClient) -> None:
    arc = await _make_arc(client)
    response = await client.post(
        f"/api/arc-board/arcs/{arc['id']}/members",
        json={"session_key": "owner", "role": "x"},
    )
    assert response.status == 400
    assert (await response.json())["code"] == "arc_validation"


@pytest.mark.asyncio
async def test_answer_appends_a_fact_and_notifies(client: TestClient) -> None:
    from kiro_crew import arcs

    arc = await _make_arc(client, members=[{"session_key": MEMBER, "role": "builder"}])
    ask = arcs.append_entry(
        arc["id"],
        MEMBER,
        {"kind": "ASK", "text": "which store?", "to": arcs.OWNER, "step": "board"},
    )
    response = await client.post(
        f"/api/arc-board/arcs/{arc['id']}/answer",
        json={"ask_id": ask["id"], "text": "the durable one"},
    )
    assert response.status == 200, await response.text()
    body = await response.json()
    entry = body["entry"]
    assert entry["author"] == arcs.OWNER
    assert entry["kind"] == "FACT"
    assert entry["reply_to"] == ask["id"]
    assert body["delivery"]["outcome"] == "posted"
    assert body["delivery"]["recipients"] == [MEMBER]
    # The answer closes the ask for everyone, owner included.
    assert arcs.open_asks(arc["id"]) == []


@pytest.mark.asyncio
async def test_answer_to_an_unknown_ask_is_400(client: TestClient) -> None:
    arc = await _make_arc(client)
    response = await client.post(
        f"/api/arc-board/arcs/{arc['id']}/answer",
        json={"ask_id": "ae_" + "0" * 32, "text": "no such ask"},
    )
    assert response.status == 400
    assert (await response.json())["code"] == "arc_validation"


async def _accept(client: TestClient, arc_id: str, expected: int) -> web.Response:
    return await client.post(
        f"/api/arc-board/arcs/{arc_id}/accept", json={"expected_index": expected}
    )


@pytest.mark.asyncio
async def test_accept_advances_then_refuses_when_complete(client: TestClient) -> None:
    arc = await _make_arc(client)
    response = await _accept(client, arc["id"], 0)
    assert response.status == 200
    assert (await response.json())["arc"]["milestone_index"] == 1
    response = await _accept(client, arc["id"], 1)
    assert (await response.json())["arc"]["milestone_index"] == 2
    response = await _accept(client, arc["id"], 2)
    assert response.status == 409
    assert (await response.json())["code"] == "arc_conflict"


@pytest.mark.asyncio
async def test_a_second_accept_of_the_same_milestone_does_not_skip_one(
    client: TestClient,
) -> None:
    """A double click sends the displayed index twice; only the first advances."""
    from kiro_crew import arcs

    arc = await _make_arc(client)
    assert (await _accept(client, arc["id"], 0)).status == 200
    response = await _accept(client, arc["id"], 0)
    assert response.status == 409
    assert (await response.json())["code"] == "arc_conflict"
    assert arcs.read_arc(arc["id"])["milestone_index"] == 1


@pytest.mark.asyncio
async def test_accept_requires_the_displayed_index(client: TestClient) -> None:
    arc = await _make_arc(client)
    response = await client.post(f"/api/arc-board/arcs/{arc['id']}/accept", json={})
    assert response.status == 400


@pytest.mark.asyncio
async def test_create_with_a_bad_lane_creates_nothing(client: TestClient) -> None:
    response = await client.post(
        "/api/arc-board/arcs",
        json={
            "title": "t",
            "milestones": [{"name": "m1"}],
            "members": [
                {"session_key": MEMBER, "role": "ok"},
                {"session_key": "owner", "role": "x"},
            ],
        },
    )
    assert response.status == 400
    assert (await (await client.get("/api/arc-board/arcs")).json())["arcs"] == []


@pytest.mark.asyncio
async def test_remove_member_keeps_its_entries(client: TestClient) -> None:
    from kiro_crew import arcs

    arc = await _make_arc(
        client,
        members=[
            {"session_key": MEMBER, "role": "builder"},
            {"session_key": PEER, "role": "r"},
        ],
    )
    arcs.append_entry(arc["id"], PEER, {"kind": "FACT", "text": "found it"})
    response = await client.post(
        f"/api/arc-board/arcs/{arc['id']}/members/remove", json={"session_key": PEER}
    )
    assert response.status == 200
    assert set((await response.json())["arc"]["members"]) == {MEMBER}
    assert [e["author"] for e in arcs.read_entries(arc["id"])] == [PEER]
    response = await client.post(
        f"/api/arc-board/arcs/{arc['id']}/members/remove", json={"session_key": PEER}
    )
    assert response.status == 400


@pytest.mark.asyncio
async def test_autonomy_changes_and_validates(client: TestClient) -> None:
    arc = await _make_arc(client)
    url = f"/api/arc-board/arcs/{arc['id']}/autonomy"
    response = await client.post(url, json={"autonomy": 3})
    assert (await response.json())["arc"]["autonomy"] == 3
    assert (await client.post(url, json={"autonomy": 4})).status == 400
    assert (await client.post(url, json={"autonomy": True})).status == 400


@pytest.mark.asyncio
async def test_note_posts_an_owner_fact(client: TestClient) -> None:
    from kiro_crew import arcs

    arc = await _make_arc(client, members=[{"session_key": MEMBER, "role": "builder"}])
    response = await client.post(
        f"/api/arc-board/arcs/{arc['id']}/note", json={"text": "M1 needs the 360 walk"}
    )
    assert response.status == 200, await response.text()
    entry = (await response.json())["entry"]
    assert (entry["author"], entry["kind"]) == (arcs.OWNER, "FACT")
    assert arcs.read_entries(arc["id"])[-1]["text"] == "M1 needs the 360 walk"


@pytest.mark.asyncio
async def test_detail_reports_collisions_and_milestone_reports(
    client: TestClient,
) -> None:
    from kiro_crew import arcs

    arc = await _make_arc(
        client,
        members=[
            {"session_key": MEMBER, "role": "builder"},
            {"session_key": PEER, "role": "r"},
        ],
    )
    a = arcs.append_entry(
        arc["id"],
        MEMBER,
        {"kind": "CLAIM", "text": "x", "step": "Feed", "checked": ["s"]},
    )
    b = arcs.append_entry(
        arc["id"],
        PEER,
        {"kind": "CLAIM", "text": "y", "step": "feed ", "checked": ["s"]},
    )
    arcs.append_entry(
        arc["id"],
        MEMBER,
        {
            "kind": "DONE",
            "text": "m1 done",
            "link": "https://x/pull/1",
            "completes_milestone": True,
        },
    )
    arcs.append_entry(
        arc["id"], MEMBER, {"kind": "DONE", "text": "a step", "link": "https://x/pull/0"}
    )
    body = await (await client.get(f"/api/arc-board/arcs/{arc['id']}")).json()
    assert [sorted(g) for g in body["collisions"]] == [sorted([a["id"], b["id"]])]
    assert [e["text"] for e in body["milestone_reports"]] == ["m1 done"]


@pytest.mark.asyncio
async def test_report_counts_the_board(client: TestClient) -> None:
    from kiro_crew import arcs

    arc = await _make_arc(client, members=[{"session_key": MEMBER, "role": "builder"}])
    arcs.append_entry(arc["id"], MEMBER, {"kind": "ASK", "text": "q?", "to": arcs.OWNER})
    arcs.append_entry(arc["id"], MEMBER, {"kind": "FAIL", "text": "dead end"})
    response = await client.get(f"/api/arc-board/arcs/{arc['id']}/report")
    assert response.status == 200
    body = await response.json()
    assert body["entries"] == 2
    assert body["by_kind"] == {"ASK": 1, "FAIL": 1}
    assert body["by_lane"] == {MEMBER: {"ASK": 1, "FAIL": 1}}
    assert body["ask_age"]["open_asks"] == 1
    assert body["reading_load_words"] == 1


@pytest.mark.parametrize(
    "link",
    [
        "javascript:alert(1)",
        "  JavaScript:x",
        "java\tscript:x",
        "data:text/html,x",
        "vbscript:x",
    ],
)
def test_store_refuses_an_executable_link(link: str) -> None:
    from kiro_crew import arcs

    arc = arcs.create_arc(
        {"title": "t", "milestones": [{"name": "m"}]},
        members=[{"session_key": MEMBER, "role": "b"}],
    )
    with pytest.raises(arcs.ArcError):
        arcs.append_entry(arc["id"], MEMBER, {"kind": "FACT", "text": "x", "link": link})


@pytest.mark.parametrize(
    "link",
    ["https://github.com/o/r/pull/1 — note", "Branch tenon/m1: see notes", "/home/x/y"],
)
def test_store_keeps_evidence_links(link: str) -> None:
    from kiro_crew import arcs

    arc = arcs.create_arc(
        {"title": "t", "milestones": [{"name": "m"}]},
        members=[{"session_key": MEMBER, "role": "b"}],
    )
    entry = arcs.append_entry(arc["id"], MEMBER, {"kind": "FACT", "text": "x", "link": link})
    assert entry["link"] == link


@pytest.mark.asyncio
async def test_every_owner_route_is_gated() -> None:
    """A non-owner caller is refused before the store, on every route.

    A gate regression would let the request fall through to the (empty) store
    and answer 200; the 403 + code assertions are what pin the gate itself.
    """
    app = web.Application()
    as_owner(app)
    from kiro_crew.dashboard.routes import arc_board as routes

    routes.register(app)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        headers = {"X-Test-User": "somebody-else"}
        for method, path, body in (
            ("GET", "/api/arc-board/arcs", None),
            ("POST", "/api/arc-board/arcs", {"title": "t"}),
            ("GET", f"/api/arc-board/arcs/{MEMBER}", None),
            (
                "POST",
                f"/api/arc-board/arcs/{MEMBER}/members",
                {"session_key": MEMBER, "role": "r"},
            ),
            (
                "POST",
                f"/api/arc-board/arcs/{MEMBER}/answer",
                {"ask_id": "ae_" + "0" * 32, "text": "t"},
            ),
            ("POST", f"/api/arc-board/arcs/{MEMBER}/accept", {"expected_index": 0}),
            ("GET", f"/api/arc-board/arcs/{MEMBER}/report", None),
            (
                "POST",
                f"/api/arc-board/arcs/{MEMBER}/members/remove",
                {"session_key": MEMBER},
            ),
            ("POST", f"/api/arc-board/arcs/{MEMBER}/autonomy", {"autonomy": 2}),
            ("POST", f"/api/arc-board/arcs/{MEMBER}/note", {"text": "t"}),
            ("POST", f"/api/arc-board/arcs/{MEMBER}/settings", {"repo": "o/r"}),
        ):
            if method == "GET":
                response = await client.get(path, headers=headers)
            elif body is None:
                response = await client.post(path, headers=headers)
            else:
                response = await client.post(path, json=body, headers=headers)
            assert response.status == 403, (method, path, response.status)
            assert (await response.json())["code"] == "owner_only"
    finally:
        await client.close()


# ── The arc's channel, kickoffs, sign-off and autonomy ──


async def _create(client: TestClient, **over: Any):
    spec: dict[str, Any] = {"title": "Own channel", "milestones": [{"name": "m1"}, {"name": "m2"}]}
    spec.update(over)
    return await client.post("/api/arc-board/arcs", json=spec)


@pytest.mark.asyncio
async def test_create_opens_an_own_channel_and_kicks_off_each_lane(
    client: TestClient, channels: FakeChannels
) -> None:
    response = await _create(client, members=[{"session_key": PEER, "role": "reviewer"}])
    assert response.status == 200, await response.text()
    body = await response.json()
    arc = body["arc"]
    assert arc["channel_owned"] is True
    ch = channels.get(arc["channel_id"])
    assert ch is not None and ch.topic == "Arc: Own channel"
    assert [m.session_key for m in ch.members.values()] == [f"dashboard:{PEER}"]
    assert body["lanes"] == [{"key": PEER, "attached": True, "kickoff": "posted"}]
    (post,) = ch.posts
    assert post["from_id"] == "human" and post["mention"] == ["m1"]
    assert "Own channel" in post["content"] and "reviewer" in post["content"]


@pytest.mark.asyncio
async def test_create_without_a_channel(client: TestClient, channels: FakeChannels) -> None:
    response = await _create(
        client, channel={"mode": "none"}, members=[{"session_key": PEER, "role": "r"}]
    )
    body = await response.json()
    assert body["arc"]["channel_id"] == ""
    assert body["lanes"][0]["reason"] == "no_channel"
    assert set(channels.channels) == {"ch1"}


@pytest.mark.asyncio
async def test_a_failed_create_closes_the_channel_it_opened(
    client: TestClient, channels: FakeChannels
) -> None:
    response = await _create(client, autonomy=9)
    assert response.status == 400
    assert len(channels.closed) == 1 and set(channels.channels) == {"ch1"}


@pytest.mark.asyncio
async def test_create_refuses_an_unknown_existing_channel(client: TestClient) -> None:
    response = await _create(client, channel={"mode": "existing", "id": "nope"})
    assert response.status == 400


@pytest.mark.asyncio
async def test_add_lane_joins_and_kickoff_is_optional(
    client: TestClient, channels: FakeChannels
) -> None:
    arc = (await (await _create(client)).json())["arc"]
    ch = channels.get(arc["channel_id"])
    url = f"/api/arc-board/arcs/{arc['id']}/members"
    body = await (await client.post(url, json={"session_key": NEW, "role": "kit"})).json()
    assert body["lane"]["attached"] is True and body["lane"]["kickoff"] == "posted"
    body = await (
        await client.post(url, json={"session_key": PEER, "role": "r", "kickoff": False})
    ).json()
    assert body["lane"] == {"key": PEER, "attached": True, "kickoff": "skipped"}
    assert len(ch.posts) == 1 and len(ch.members) == 2


@pytest.mark.asyncio
async def test_a_lane_with_no_live_session_stays_on_the_arc(client: TestClient) -> None:
    arc = (await (await _create(client)).json())["arc"]
    response = await client.post(
        f"/api/arc-board/arcs/{arc['id']}/members", json={"session_key": "chat-gone", "role": "r"}
    )
    body = await response.json()
    assert "chat-gone" in body["arc"]["members"]
    assert body["lane"]["reason"] == "session_not_found"


@pytest.mark.asyncio
async def test_removing_a_lane_leaves_only_an_own_channel(
    client: TestClient, channels: FakeChannels
) -> None:
    created = await _create(client, members=[{"session_key": PEER, "role": "r"}])
    own = (await created.json())["arc"]
    shared = await _make_arc(client, members=[{"session_key": MEMBER, "role": "b"}])
    url = "/api/arc-board/arcs/{}/members/remove"
    body = await (await client.post(url.format(own["id"]), json={"session_key": PEER})).json()
    assert body["left_channel"] is True
    assert channels.get(own["channel_id"]).members == {}
    body = await (await client.post(url.format(shared["id"]), json={"session_key": MEMBER})).json()
    assert body["left_channel"] is False
    assert len(channels.get("ch1").members) == 1


@pytest.mark.asyncio
async def test_open_channel_gives_a_channelless_arc_its_own(
    client: TestClient, channels: FakeChannels
) -> None:
    created = await _create(
        client, channel={"mode": "none"}, members=[{"session_key": PEER, "role": "r"}]
    )
    arc = (await created.json())["arc"]
    url = f"/api/arc-board/arcs/{arc['id']}/channel"
    body = await (await client.post(url, json={})).json()
    ch = channels.get(body["arc"]["channel_id"])
    assert body["arc"]["channel_owned"] is True
    assert body["lanes"] == [{"key": PEER, "attached": True, "kickoff": "skipped"}]
    assert ch.posts == []
    response = await client.post(url, json={})
    assert response.status == 409


@pytest.mark.asyncio
async def test_detail_shows_the_channel_and_who_is_in_it(client: TestClient) -> None:
    arc = await _make_arc(
        client,
        members=[{"session_key": MEMBER, "role": "b"}, {"session_key": "chat-gone", "role": "x"}],
    )
    body = await (await client.get(f"/api/arc-board/arcs/{arc['id']}")).json()
    assert body["channel"]["id"] == "ch1" and body["channel"]["open"] is True
    assert body["channel"]["owned"] is False
    assert {lane["key"]: lane["in_channel"] for lane in body["lanes"]} == {
        MEMBER: True,
        "chat-gone": False,
    }


@pytest.mark.asyncio
async def test_owner_answer_posts_as_the_channel_human(
    client: TestClient, channels: FakeChannels
) -> None:
    from kiro_crew import arcs

    created = await _create(client, members=[{"session_key": PEER, "role": "r"}])
    arc = (await created.json())["arc"]
    ch = channels.get(arc["channel_id"])
    # PEER is the channel's orchestrator, which a non-human sender could never reach.
    assert ch.orchestrator_id == "m1"
    ask = arcs.append_entry(arc["id"], PEER, {"kind": "ASK", "text": "q?", "to": arcs.OWNER})
    response = await client.post(
        f"/api/arc-board/arcs/{arc['id']}/answer", json={"ask_id": ask["id"], "text": "yes"}
    )
    assert (await response.json())["delivery"]["recipients"] == [PEER]
    assert ch.posts[-1]["from_id"] == "human" and ch.posts[-1]["mention"] == ["m1"]


@pytest.mark.asyncio
async def test_autonomy_change_leaves_one_board_note(client: TestClient) -> None:
    from kiro_crew import arcs

    arc = await _make_arc(client)
    url = f"/api/arc-board/arcs/{arc['id']}/autonomy"
    body = await (await client.post(url, json={"autonomy": 3})).json()
    assert body["note"]["text"] == "Autonomy is now 3 (was 2)."
    body = await (await client.post(url, json={"autonomy": 3})).json()
    assert body["note"] is None
    assert [e["text"] for e in arcs.read_entries(arc["id"])] == ["Autonomy is now 3 (was 2)."]


def test_only_a_done_can_complete_a_milestone() -> None:
    from kiro_crew import arcs

    arc = arcs.create_arc(
        {"title": "t", "milestones": [{"name": "m"}]},
        members=[{"session_key": MEMBER, "role": "b"}],
    )
    with pytest.raises(arcs.ArcError):
        arcs.append_entry(
            arc["id"], MEMBER, {"kind": "FACT", "text": "x", "completes_milestone": True}
        )
    with pytest.raises(arcs.ArcError):
        arcs.append_entry(
            arc["id"],
            MEMBER,
            {"kind": "DONE", "text": "x", "link": "https://x", "completes_milestone": "yes"},
        )


@pytest.mark.parametrize("autonomy,shown", [(2, False), (3, True)])
def test_autonomy_three_brief_lists_the_milestones_ahead(autonomy: int, shown: bool) -> None:
    from kiro_crew import arc_brief, arcs

    arc = arcs.create_arc(
        {
            "title": "t",
            "autonomy": autonomy,
            "milestones": [{"name": "first", "criteria": "a"}, {"name": "second", "criteria": "b"}],
        },
        members=[{"session_key": MEMBER, "role": "b"}],
    )
    brief = arc_brief.render_brief(arc, MEMBER)
    assert ("Milestones after this one:\n- second: b" in brief) is shown


@pytest.mark.asyncio
async def test_nudge_is_armed_only_when_asked(client: TestClient, monkeypatch) -> None:
    import kiro_crew.autonudge as autonudge
    import kiro_crew.autonudge_authz as authz

    calls: list[dict[str, Any]] = []

    async def fake_add(**kwargs: Any):
        calls.append(kwargs)
        return object(), None, 200

    monkeypatch.setattr(autonudge, "get_instance", lambda: object())
    monkeypatch.setattr(authz, "authorize_and_add_nudge", fake_add)
    arc = (await (await _create(client)).json())["arc"]
    url = f"/api/arc-board/arcs/{arc['id']}/members"
    body = await (await client.post(url, json={"session_key": PEER, "role": "r"})).json()
    assert "nudge" not in body["lane"] and calls == []
    body = await (
        await client.post(url, json={"session_key": NEW, "role": "r", "nudge": True})
    ).json()
    assert body["lane"]["nudge"] == "set"
    (call,) = calls
    assert call["slot_key"] == NEW and call["replace_existing"] is False
    assert call["idle_secs"] == 600 and call["max_cycles"] > 0
    assert "Own channel" in call["message"]


@pytest.mark.asyncio
async def test_a_refused_nudge_is_reported_not_raised(client: TestClient, monkeypatch) -> None:
    import kiro_crew.autonudge as autonudge
    import kiro_crew.autonudge_authz as authz

    async def refuse(**kwargs: Any):
        return None, "a loop is already armed on this session", 409

    monkeypatch.setattr(autonudge, "get_instance", lambda: object())
    monkeypatch.setattr(authz, "authorize_and_add_nudge", refuse)
    response = await _create(client, members=[{"session_key": PEER, "role": "r", "nudge": True}])
    assert response.status == 200
    (lane,) = (await response.json())["lanes"]
    assert lane["nudge"] == "failed" and "already armed" in lane["nudge_error"]


@pytest.mark.asyncio
async def test_settings_set_repo_and_coordinator(client: TestClient) -> None:
    arc = await _make_arc(client, members=[{"session_key": MEMBER, "role": "Baton"}])
    url = f"/api/arc-board/arcs/{arc['id']}/settings"
    body = await (await client.post(url, json={"repo": "o/r", "coordinator": MEMBER})).json()
    assert (body["arc"]["repo"], body["arc"]["coordinator"]) == ("o/r", MEMBER)
    assert (await client.post(url, json={"coordinator": PEER})).status == 400
    assert (await client.post(url, json={"repo": "no repo"})).status == 400
    body = await (await client.post(url, json={"coordinator": ""})).json()
    assert body["arc"]["coordinator"] == "" and body["arc"]["repo"] == "o/r"

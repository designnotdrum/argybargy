"""Agent-facing HTTP surface: discovery, auth, addressing, delivery, turn-taking."""
import datetime as dt

import pytest
from pydantic import ValidationError

from argybargy.app import VERSION, PresenceBody
from argybargy.settings import settings


# ----------------------------------------------------------------- discovery
def test_health_reports_ok_and_version(client):
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["version"] == VERSION


def test_manifest_is_self_documenting_and_unauthenticated(client):
    r = client.get("/")
    assert r.status_code == 200
    body = r.json()
    assert body["auth"]["header"].startswith("Authorization: Bearer")
    paths = " ".join(str(v) for v in body["endpoints"])
    for expected in ("/messages", "/peers", "/whoami", "/history", "/presence"):
        assert expected in paths


def test_openapi_and_docs_served_by_default(client):
    assert client.get("/openapi.json").status_code == 200
    assert client.get("/docs").status_code == 200


def test_openapi_presence_requestbody_describes_presence_body(client):
    """POST /presence takes Starlette's raw `Request` and parses the body by hand, after
    `_touch()`, so no payload shape — badly-shaped object, non-object JSON, or invalid
    JSON syntax — can eat a peer's heartbeat; see the comment on `presence()`. Taking a
    raw `Request` means FastAPI can't derive a schema for the route on its own; `app.py`
    patches the generated document to restore it. Guard the patch: without it, this
    schema silently disappears and /docs stops describing the real shape."""
    schema = client.get("/openapi.json").json()["paths"]["/presence"]["post"]["requestBody"]
    schema = schema["content"]["application/json"]["schema"]

    state_variants = schema["properties"]["state"]["anyOf"]
    state_enum = next(v["enum"] for v in state_variants if "enum" in v)
    assert set(state_enum) == {"idle", "working", "blocked"}

    note_variants = schema["properties"]["note"]["anyOf"]
    note_max_length = next(v["maxLength"] for v in note_variants if "maxLength" in v)
    assert note_max_length == settings.status_note_max


# ---------------------------------------------------------------------- auth
def test_protected_routes_reject_missing_and_bogus_codes(client):
    for path in ("/whoami", "/peers", "/history", "/messages?wait=0"):
        assert client.get(path).status_code == 401, path
        assert client.get(path, headers={"Authorization": "Bearer nope"}).status_code == 401, path


def test_malformed_authorization_header_rejected(client, make_code):
    code, _ = make_code("malformed")
    for value in (f"Basic {code}", "Bearer", "Bearer  ", "Bearer wrong"):
        assert client.get("/whoami", headers={"Authorization": value}).status_code == 401, value


def test_bare_code_without_bearer_prefix_is_accepted(client, make_code):
    """Documented leniency: agents that forget the 'Bearer ' prefix still work."""
    code, _ = make_code("lenient")
    assert client.get("/whoami", headers={"Authorization": code}).json()["name"] == "lenient"


def test_whoami_returns_identity_and_capabilities(client, make_code):
    _, auth = make_code("cap-agent", capabilities="reads QB; runs SQL")
    assert client.get("/whoami", headers=auth).json() == {
        "name": "cap-agent", "room": "default", "capabilities": "reads QB; runs SQL",
        "status": None, "status_note": None, "status_stale": False,
    }


def test_revoked_code_stops_working_immediately(client, make_code):
    code, auth = make_code("short-lived")
    assert client.get("/whoami", headers=auth).status_code == 200
    from argybargy.app import code_store
    code_store.revoke("short-lived")
    assert client.get("/whoami", headers=auth).status_code == 401


def test_expired_code_rejected(client, make_code):
    past = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)
    _, auth = make_code("ghost", expires_at=past)
    assert client.get("/whoami", headers=auth).status_code == 401


# ------------------------------------------------------------------- peers
def test_peers_lists_roommates_with_presence_and_capabilities(client, make_code):
    _, a = make_code("peers-a", room="peersroom", capabilities="planner")
    _, b = make_code("peers-b", room="peersroom")
    client.get("/whoami", headers=a)
    client.get("/whoami", headers=b)
    peers = client.get("/peers", headers=a).json()["peers"]
    names = {p["name"] for p in peers}
    assert {"peers-a", "peers-b"} <= names
    me = next(p for p in peers if p["name"] == "peers-a")
    assert me["capabilities"] == "planner"
    assert me["online"] is True
    assert isinstance(me["seconds_since_seen"], (int, float))


def test_peers_never_leaks_other_rooms(client, make_code):
    _, inside = make_code("inside", room="roomx")
    make_code("outsider", room="roomy")
    client.get("/whoami", headers=inside)
    names = {p["name"] for p in client.get("/peers", headers=inside).json()["peers"]}
    assert "outsider" not in names


# ---------------------------------------------------------------- messaging
def test_direct_message_reaches_target(client, make_code):
    _, a = make_code("dm-a", room="dmroom")
    _, b = make_code("dm-b", room="dmroom")
    assert client.post("/messages", headers=a, json={"to": "dm-b", "text": "ping"}).json()["ok"]
    got = client.get("/messages?since=0&wait=0", headers=b).json()
    assert "ping" in [m["text"] for m in got["messages"]]


def test_sender_never_receives_its_own_message(client, make_code):
    _, a = make_code("echo-a", room="echoroom")
    make_code("echo-b", room="echoroom")
    client.post("/messages", headers=a, json={"to": "all", "text": "hello"})
    mine = client.get("/messages?since=0&wait=0", headers=a).json()["messages"]
    assert all(m["from"] != "echo-a" for m in mine)


def test_broadcast_reaches_every_other_peer(client, make_code):
    _, a = make_code("bc-a", room="bcroom")
    _, b = make_code("bc-b", room="bcroom")
    _, c = make_code("bc-c", room="bcroom")
    client.post("/messages", headers=a, json={"to": "all", "text": "hear ye"})
    for who in (b, c):
        texts = [m["text"] for m in client.get("/messages?since=0&wait=0", headers=who).json()["messages"]]
        assert "hear ye" in texts


def test_direct_message_is_not_visible_to_third_party(client, make_code):
    """A private DM must not leak to another agent in the same room."""
    _, a = make_code("priv-a", room="privroom")
    make_code("priv-b", room="privroom")
    _, c = make_code("priv-c", room="privroom")
    client.post("/messages", headers=a, json={"to": "priv-b", "text": "for your eyes only"})
    seen = [m["text"] for m in client.get("/messages?since=0&wait=0", headers=c).json()["messages"]]
    assert "for your eyes only" not in seen


def test_messages_never_cross_rooms(client, make_code):
    _, a = make_code("iso-a", room="iso-one")
    _, b = make_code("iso-b", room="iso-two")
    client.post("/messages", headers=a, json={"to": "all", "text": "room one only"})
    seen = [m["text"] for m in client.get("/messages?since=0&wait=0", headers=b).json()["messages"]]
    assert "room one only" not in seen
    assert "room one only" not in [m["text"] for m in client.get("/history", headers=b).json()["messages"]]


def test_cursor_advances_and_since_excludes_old_messages(client, make_code):
    _, a = make_code("cur-a", room="curroom")
    _, b = make_code("cur-b", room="curroom")
    client.post("/messages", headers=a, json={"to": "all", "text": "first"})
    first = client.get("/messages?since=0&wait=0", headers=b).json()
    cursor = first["cursor"]
    assert cursor > 0
    assert client.get(f"/messages?since={cursor}&wait=0", headers=b).json()["messages"] == []
    client.post("/messages", headers=a, json={"to": "all", "text": "second"})
    nxt = client.get(f"/messages?since={cursor}&wait=0", headers=b).json()
    assert [m["text"] for m in nxt["messages"]] == ["second"]
    assert nxt["cursor"] > cursor


def test_history_is_room_scoped_and_respects_limit(client, make_code):
    _, a = make_code("hist-a", room="histroom")
    for i in range(5):
        client.post("/messages", headers=a, json={"to": "all", "text": f"h{i}"})
    body = client.get("/history?limit=3", headers=a).json()
    assert body["room"] == "histroom"
    assert [m["text"] for m in body["messages"]] == ["h2", "h3", "h4"]


# -------------------------------------------------------------- turn-taking
def test_expects_reply_defaults_and_explicit_override(client, make_code):
    _, a = make_code("turn-a", room="turnroom")
    make_code("turn-b", room="turnroom")
    broadcast = client.post("/messages", headers=a, json={"to": "all", "text": "fyi"}).json()
    assert broadcast["message"]["expects_reply"] == "none"
    direct = client.post("/messages", headers=a, json={"to": "turn-b", "text": "hi"}).json()
    assert direct["message"]["expects_reply"] == "turn-b"
    open_q = client.post("/messages", headers=a,
                         json={"to": "all", "text": "q", "expects_reply": "anyone"}).json()
    assert open_q["message"]["expects_reply"] == "anyone"


def test_claim_is_atomic_first_responder_wins(client, make_code):
    _, a = make_code("claim-a", room="claimroom")
    _, b = make_code("claim-b", room="claimroom")
    _, c = make_code("claim-c", room="claimroom")
    seq = client.post("/messages", headers=a,
                      json={"to": "all", "text": "who?", "expects_reply": "anyone"}).json()["message"]["seq"]

    won = client.post(f"/messages/{seq}/claim", headers=b)
    assert won.status_code == 200
    assert won.json()["won"] is True
    assert won.json()["claimed_by"] == "claim-b"

    for loser in (c, a):
        lost = client.post(f"/messages/{seq}/claim", headers=loser)
        assert lost.status_code == 409
        assert lost.json()["won"] is False
        assert lost.json()["claimed_by"] == "claim-b"


def test_claim_unknown_sequence_is_404(client, make_code):
    _, a = make_code("claim-404", room="claim404room")
    assert client.post("/messages/999999/claim", headers=a).status_code == 404


def test_claim_cannot_reach_into_another_room(client, make_code):
    _, a = make_code("xr-a", room="xr-one")
    _, b = make_code("xr-b", room="xr-two")
    seq = client.post("/messages", headers=a,
                      json={"to": "all", "text": "mine", "expects_reply": "anyone"}).json()["message"]["seq"]
    assert client.post(f"/messages/{seq}/claim", headers=b).status_code == 404


# ------------------------------------------------------------------ presence
def test_presence_body_validates_state_enum_and_note_length():
    empty = PresenceBody()
    assert empty.state is None and empty.note is None
    assert empty.model_fields_set == set()

    full = PresenceBody(state="working", note="reviewing PR #2")
    assert full.state == "working" and full.note == "reviewing PR #2"

    cleared = PresenceBody(state=None)
    assert cleared.model_fields_set == {"state"}

    with pytest.raises(ValidationError):
        PresenceBody(state="done")

    with pytest.raises(ValidationError):
        PresenceBody(note="x" * (settings.status_note_max + 1))


def test_presence_bare_heartbeat_touches_only(client, make_code):
    code, auth = make_code("beat")
    r = client.post("/presence", headers=auth)
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["status"] is None and body["status_note"] is None
    peers = client.get("/peers", headers=auth).json()["peers"]
    me = next(p for p in peers if p["name"] == "beat")
    assert me["online"] is True and me["status"] is None


def test_presence_sets_status_and_note(client, make_code):
    code, auth = make_code("worker1")
    r = client.post("/presence", headers=auth, json={"state": "working", "note": "reviewing PR #2"})
    assert r.status_code == 200
    assert r.json()["status"] == "working" and r.json()["status_note"] == "reviewing PR #2"
    peers = client.get("/peers", headers=auth).json()["peers"]
    me = next(p for p in peers if p["name"] == "worker1")
    assert me["status"] == "working" and me["status_note"] == "reviewing PR #2"


def test_presence_absent_field_leaves_status_unchanged(client, make_code):
    code, auth = make_code("worker2")
    client.post("/presence", headers=auth, json={"state": "blocked", "note": "waiting on auth"})
    r = client.post("/presence", headers=auth, json={})
    assert r.status_code == 200
    assert r.json()["status"] == "blocked" and r.json()["status_note"] == "waiting on auth"


def test_presence_null_clears_status(client, make_code):
    code, auth = make_code("worker3")
    client.post("/presence", headers=auth, json={"state": "idle", "note": "ready"})
    r = client.post("/presence", headers=auth, json={"state": None})
    assert r.status_code == 200
    assert r.json()["status"] is None
    peers = client.get("/peers", headers=auth).json()["peers"]
    me = next(p for p in peers if p["name"] == "worker3")
    assert me["status"] is None


def test_presence_null_note_leaves_state_untouched(client, make_code):
    code, auth = make_code("worker6")
    client.post("/presence", headers=auth, json={"state": "working", "note": "reviewing PR #2"})
    r = client.post("/presence", headers=auth, json={"note": None})
    assert r.status_code == 200
    assert r.json()["status_note"] is None and r.json()["status"] == "working"
    peers = client.get("/peers", headers=auth).json()["peers"]
    me = next(p for p in peers if p["name"] == "worker6")
    assert me["status_note"] is None and me["status"] == "working"


def test_presence_null_state_leaves_note_untouched(client, make_code):
    """The mixed case: one field explicit-null (cleared), the other absent
    (unchanged), in a single call — the only case where state_provided and
    note_provided must differ within one request."""
    code, auth = make_code("worker7")
    client.post("/presence", headers=auth, json={"state": "blocked", "note": "waiting on review"})
    r = client.post("/presence", headers=auth, json={"state": None})
    assert r.status_code == 200
    assert r.json()["status"] is None and r.json()["status_note"] == "waiting on review"
    peers = client.get("/peers", headers=auth).json()["peers"]
    me = next(p for p in peers if p["name"] == "worker7")
    assert me["status"] is None and me["status_note"] == "waiting on review"


def test_presence_invalid_state_rejected(client, make_code):
    code, auth = make_code("worker4")
    r = client.post("/presence", headers=auth, json={"state": "thinking"})
    assert r.status_code == 422


def test_presence_note_over_max_length_rejected(client, make_code):
    code, auth = make_code("worker5")
    over = "x" * (settings.status_note_max + 1)
    r = client.post("/presence", headers=auth, json={"note": over})
    assert r.status_code == 422


def test_presence_invalid_state_still_counts_as_a_heartbeat(client, make_code, admin_headers):
    """A malformed body must not cost the peer its liveness: the 422 is real (state
    is still rejected), but the request still touches presence — otherwise an agent
    with a persistent payload bug in its own heartbeat call would stay offline forever."""
    code, auth = make_code("worker4b")
    r = client.post("/presence", headers=auth, json={"state": "thinking"})
    assert r.status_code == 422
    room = client.get("/admin/state", headers=admin_headers).json()["peers"]["default"]
    me = next(p for p in room if p["name"] == "worker4b")
    assert me["online"] is True
    assert me["seconds_since_seen"] < 1.0


def test_presence_over_long_note_still_counts_as_a_heartbeat(client, make_code, admin_headers):
    code, auth = make_code("worker5b")
    over = "x" * (settings.status_note_max + 1)
    r = client.post("/presence", headers=auth, json={"note": over})
    assert r.status_code == 422
    room = client.get("/admin/state", headers=admin_headers).json()["peers"]["default"]
    me = next(p for p in room if p["name"] == "worker5b")
    assert me["online"] is True
    assert me["seconds_since_seen"] < 1.0


def test_presence_non_object_json_body_now_counts_as_a_heartbeat(client, make_code, admin_headers):
    """The object-shape hole this endpoint used to have: a body that parses as JSON but
    isn't a JSON object (a bare list here) used to fail FastAPI's own `dict`-typed
    validation *before* the handler ran, so `_touch()` never happened and the heartbeat
    was silently dropped — the exact failure mode this endpoint exists to prevent. `body`
    is now typed `Any`, so FastAPI accepts the list and lets the handler touch first,
    validate second, same as it already did for a badly-shaped object body."""
    code, auth = make_code("worker8b")
    r = client.post("/presence", headers=auth, json=[1, 2, 3])
    assert r.status_code == 422
    assert r.json()["detail"][0]["loc"] == ["body"]
    room = client.get("/admin/state", headers=admin_headers).json()["peers"]["default"]
    me = next(p for p in room if p["name"] == "worker8b")
    assert me["online"] is True


def test_presence_bare_json_string_body_now_counts_as_a_heartbeat(client, make_code, admin_headers):
    """Same hole, a different non-object shape (a bare JSON string)."""
    code, auth = make_code("worker9b")
    r = client.post("/presence", headers=auth, json="hello")
    assert r.status_code == 422
    assert r.json()["detail"][0]["loc"] == ["body"]
    room = client.get("/admin/state", headers=admin_headers).json()["peers"]["default"]
    me = next(p for p in room if p["name"] == "worker9b")
    assert me["online"] is True


def test_presence_syntactically_invalid_json_still_counts_as_a_heartbeat(client, make_code, admin_headers):
    """The last gap: a body that isn't valid JSON at all. `presence()` takes a raw `Request`
    and parses the body by hand *after* `_touch()`, so a syntax error still 422s but no
    longer costs the peer its liveness — same as the other bad-shape cases above. The 422
    error itself must stay indistinguishable from one FastAPI raises on its own for the
    same failure (compare `test_malformed_json_matches_fastapis_own_422_shape` below)."""
    code, auth = make_code("worker10b")
    r = client.post("/presence", headers={**auth, "Content-Type": "application/json"}, content=b"{not json")
    assert r.status_code == 422
    assert r.json()["detail"][0]["type"] == "json_invalid"
    room = client.get("/admin/state", headers=admin_headers).json()["peers"]["default"]
    me = next(p for p in room if p["name"] == "worker10b")
    assert me["online"] is True
    assert me["seconds_since_seen"] < 1.0


def test_malformed_json_matches_fastapis_own_422_shape(client, make_code):
    """`/presence` parses its body by hand instead of letting FastAPI do it (see the comment
    on `presence()`), specifically so it can touch presence before a JSON syntax error is
    even discovered. That hand-rolled parsing must still fail exactly the way FastAPI's own
    body parsing would: same status, same `detail` structure, same error `type`/`loc`
    rooting/`msg`/`ctx` keys — a client can't tell the difference. `/messages` takes a real
    Pydantic body (`SendBody`), so FastAPI parses it, giving us a reference 422 for the
    identical malformed-JSON input to diff against."""
    code, auth = make_code("worker11b")
    malformed = b"{not json"
    headers = {**auth, "Content-Type": "application/json"}

    reference = client.post("/messages", headers=headers, content=malformed)
    ours = client.post("/presence", headers=headers, content=malformed)

    assert reference.status_code == ours.status_code == 422
    ref_body, our_body = reference.json(), ours.json()
    assert set(our_body.keys()) == set(ref_body.keys()) == {"detail"}
    assert len(our_body["detail"]) == len(ref_body["detail"]) == 1
    ref_err, our_err = ref_body["detail"][0], our_body["detail"][0]
    assert set(our_err.keys()) == set(ref_err.keys())
    for key in ("type", "loc", "msg", "input", "ctx"):
        assert our_err[key] == ref_err[key], key


def test_presence_rate_limited_429(client, make_code):
    code, auth = make_code("flapper")
    last = None
    for i in range(settings.status_rate_max + 3):
        last = client.post("/presence", headers=auth, json={"state": "working", "note": f"tick {i}"})
        if last.status_code == 429:
            break
    assert last.status_code == 429
    assert last.headers.get("Retry-After")
    assert last.json()["detail"]["error"] == "rate_limited"


def test_whoami_reflects_own_status(client, make_code):
    code, auth = make_code("selfcheck")
    client.post("/presence", headers=auth, json={"state": "working", "note": "reading the brief"})
    me = client.get("/whoami", headers=auth).json()
    assert me["status"] == "working" and me["status_note"] == "reading the brief"
    assert me["status_stale"] is False


def test_peers_status_alongside_capabilities(client, make_code):
    code, auth = make_code("capstatus", capabilities="reads QB; runs SQL")
    client.post("/presence", headers=auth, json={"state": "working", "note": "querying QB"})
    peers = client.get("/peers", headers=auth).json()["peers"]
    me = next(p for p in peers if p["name"] == "capstatus")
    assert "reads QB" in me["capabilities"]
    assert me["status"] == "working" and me["status_note"] == "querying QB"


def test_status_scoped_per_room(client, make_code):
    code_r1, auth_r1 = make_code("dualroom", room="r1")
    code_r2, auth_r2 = make_code("dualroom", room="r2")
    client.post("/presence", headers=auth_r1, json={"state": "working", "note": "in r1"})
    client.post("/presence", headers=auth_r2, json={"state": "blocked", "note": "in r2"})
    peers_r1 = client.get("/peers", headers=auth_r1).json()["peers"]
    peers_r2 = client.get("/peers", headers=auth_r2).json()["peers"]
    me_r1 = next(p for p in peers_r1 if p["name"] == "dualroom")
    me_r2 = next(p for p in peers_r2 if p["name"] == "dualroom")
    assert me_r1["status"] == "working" and me_r1["status_note"] == "in r1"
    assert me_r2["status"] == "blocked" and me_r2["status_note"] == "in r2"

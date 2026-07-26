"""Admin surface: gating, key lifecycle, operator messages, audit, token rotation."""
import os
import stat

import pytest

from argybargy.app import VERSION

ADMIN_GETS = ["/admin/state", "/admin/stats", "/admin/audit"]
ADMIN_POSTS = [
    ("/admin/invite", {"name": "x"}),
    ("/admin/revoke", {"target": "x"}),
    ("/admin/say", {"text": "x"}),
    ("/admin/regenerate-token", {}),
    ("/admin/delete-room", {"room": "x"}),
]


# ------------------------------------------------------------------- gating
@pytest.mark.parametrize("path", ADMIN_GETS)
def test_admin_get_requires_token(client, path):
    assert client.get(path).status_code == 401
    assert client.get(path, headers={"X-Admin-Token": "wrong"}).status_code == 401


@pytest.mark.parametrize("path,body", ADMIN_POSTS)
def test_admin_post_requires_token(client, path, body):
    assert client.post(path, json=body).status_code == 401
    assert client.post(path, json=body, headers={"X-Admin-Token": "wrong"}).status_code == 401


def test_agent_code_is_not_admin_credential(client, make_code):
    """An agent bearer code must never unlock the admin surface."""
    code, _ = make_code("not-an-admin")
    assert client.get("/admin/state", headers={"X-Admin-Token": code}).status_code == 401


# ------------------------------------------------------------- key lifecycle
def test_invite_mints_working_key_with_room_and_capabilities(client, admin_headers):
    r = client.post("/admin/invite", headers=admin_headers,
                    json={"name": "carol", "room": "sales", "capabilities": "researcher"})
    assert r.status_code == 200
    body = r.json()
    code = body["code"]
    me = client.get("/whoami", headers={"Authorization": f"Bearer {code}"}).json()
    assert me == {
        "name": "carol", "room": "sales", "capabilities": "researcher",
        "status": None, "status_note": None, "status_stale": False,
    }
    client.post("/admin/revoke", headers=admin_headers, json={"target": "carol"})


def test_invite_rejects_bad_expiry(client, admin_headers):
    r = client.post("/admin/invite", headers=admin_headers, json={"name": "bad-exp", "expires": "bogus"})
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "bad_request"


def test_invite_accepts_expiry_presets(client, admin_headers):
    for preset in ("10m", "30m", "60m", "1d", "1w", "1mo", "never"):
        r = client.post("/admin/invite", headers=admin_headers,
                        json={"name": f"exp-{preset}", "expires": preset})
        assert r.status_code == 200, preset
        client.post("/admin/revoke", headers=admin_headers, json={"target": f"exp-{preset}"})


def test_revoke_by_name_then_code_invalidates_access(client, admin_headers):
    code = client.post("/admin/invite", headers=admin_headers, json={"name": "revoke-me"}).json()["code"]
    auth = {"Authorization": f"Bearer {code}"}
    assert client.get("/whoami", headers=auth).status_code == 200
    assert client.post("/admin/revoke", headers=admin_headers, json={"target": "revoke-me"}).json()["revoked"] >= 1
    assert client.get("/whoami", headers=auth).status_code == 401


def test_revoke_by_full_code_works(client, admin_headers):
    code = client.post("/admin/invite", headers=admin_headers, json={"name": "revoke-by-code"}).json()["code"]
    assert client.post("/admin/revoke", headers=admin_headers, json={"target": code}).json()["revoked"] >= 1
    assert client.get("/whoami", headers={"Authorization": f"Bearer {code}"}).status_code == 401


def test_revoke_unknown_target_is_zero_not_error(client, admin_headers):
    r = client.post("/admin/revoke", headers=admin_headers, json={"target": "never-existed"})
    assert r.status_code == 200
    assert r.json()["revoked"] == 0


def test_revoke_clears_status_so_a_reissued_name_starts_clean(client, admin_headers):
    """A name that's revoked and reissued in the same room must not inherit the
    previous incarnation's status note (e.g. a stale 'blocked: waiting on DB creds')."""
    code = client.post("/admin/invite", headers=admin_headers, json={"name": "reincarnate"}).json()["code"]
    auth = {"Authorization": f"Bearer {code}"}
    client.post("/presence", headers=auth, json={"state": "blocked", "note": "waiting on DB creds"})
    assert client.get("/whoami", headers=auth).json()["status"] == "blocked"

    assert client.post("/admin/revoke", headers=admin_headers, json={"target": "reincarnate"}).json()["revoked"] >= 1

    new_code = client.post("/admin/invite", headers=admin_headers, json={"name": "reincarnate"}).json()["code"]
    new_auth = {"Authorization": f"Bearer {new_code}"}
    me = client.get("/whoami", headers=new_auth).json()
    assert me["status"] is None and me["status_note"] is None
    client.post("/admin/revoke", headers=admin_headers, json={"target": "reincarnate"})
def test_admin_delete_room_removes_messages_and_codes(client, admin_headers):
    r = client.post("/admin/invite", headers=admin_headers, json={"name": "doomed", "room": "delroom"})
    code = r.json()["code"]
    auth = {"Authorization": f"Bearer {code}"}
    assert client.get("/whoami", headers=auth).status_code == 200
    client.post("/messages", headers=auth, json={"to": "all", "text": "last words"})

    resp = client.post("/admin/delete-room", headers=admin_headers, json={"room": "delroom"})
    assert resp.status_code == 200
    assert resp.json() == {"room": "delroom", "deleted_messages": 1, "deleted_codes": 1}

    state = client.get("/admin/state", headers=admin_headers).json()
    assert all(c["room"] != "delroom" for c in state["codes"])
    assert all(m["room"] != "delroom" for m in state["messages"])
    assert client.get("/whoami", headers=auth).status_code == 401


def test_admin_delete_room_unknown_room_is_zero_not_error(client, admin_headers):
    r = client.post("/admin/delete-room", headers=admin_headers, json={"room": "never-existed-room"})
    assert r.status_code == 200
    assert r.json() == {"room": "never-existed-room", "deleted_messages": 0, "deleted_codes": 0}


# ------------------------------------------------------------------- state
def test_admin_state_shape(client, admin_headers):
    body = client.get("/admin/state", headers=admin_headers).json()
    for key in ("version", "public_url", "hash_codes", "peers", "codes", "messages"):
        assert key in body, key
    assert body["version"] == VERSION
    assert isinstance(body["peers"], dict)
    assert isinstance(body["codes"], list)


def test_admin_state_includes_status(client, admin_headers, make_code):
    code, auth = make_code("adminview")
    client.post("/presence", headers=auth, json={"state": "idle", "note": "free"})
    state = client.get("/admin/state", headers=admin_headers).json()
    room_peers = state["peers"]["default"]
    me = next(p for p in room_peers if p["name"] == "adminview")
    assert me["status"] == "idle" and me["status_note"] == "free"


def test_admin_stats_counts(client, admin_headers):
    body = client.get("/admin/stats", headers=admin_headers).json()
    assert body["version"] == VERSION
    assert body["codes"] >= 0
    assert body["uptime_seconds"] >= 0
    assert "messages" in body


# --------------------------------------------------------------- operator say
def test_admin_say_delivers_to_room(client, admin_headers, make_code):
    _, b = make_code("say-target", room="sayroom")
    client.post("/admin/say", headers=admin_headers,
                json={"room": "sayroom", "to": "all", "text": "from the operator"})
    texts = [m["text"] for m in client.get("/messages?since=0&wait=0", headers=b).json()["messages"]]
    assert "from the operator" in texts


def test_admin_say_supports_sender_and_expects_reply(client, admin_headers, make_code):
    _, b = make_code("say-b", room="sayroom2")
    client.post("/admin/say", headers=admin_headers,
                json={"room": "sayroom2", "to": "say-b", "text": "your turn",
                      "sender": "titus", "expects_reply": "say-b"})
    msgs = client.get("/messages?since=0&wait=0", headers=b).json()["messages"]
    got = next(m for m in msgs if m["text"] == "your turn")
    assert got["from"] == "titus"
    assert got["expects_reply"] == "say-b"


# -------------------------------------------------------------------- audit
def test_audit_records_invite_and_revoke(client, admin_headers):
    client.post("/admin/invite", headers=admin_headers, json={"name": "auditee"})
    client.post("/admin/revoke", headers=admin_headers, json={"target": "auditee"})
    events = client.get("/admin/audit", headers=admin_headers).json()["events"]
    actions = {e["action"] for e in events}
    assert "invite" in actions
    assert "revoke" in actions
    assert all({"ts", "action"} <= set(e) for e in events)


def test_audit_records_failed_admin_auth(client, admin_headers):
    client.get("/admin/state", headers={"X-Admin-Token": "definitely-wrong"})
    events = client.get("/admin/audit", headers=admin_headers).json()["events"]
    assert any("admin" in e["action"] and "fail" in e["action"] for e in events), \
        f"expected a failed-admin-auth event, saw {sorted({e['action'] for e in events})}"


def test_audit_limit_is_honoured(client, admin_headers):
    events = client.get("/admin/audit?limit=1", headers=admin_headers).json()["events"]
    assert len(events) <= 1


def test_admin_delete_room_audit_logged(client, admin_headers):
    client.post("/admin/invite", headers=admin_headers, json={"name": "auditvictim", "room": "delroom-audit"})
    client.post("/admin/delete-room", headers=admin_headers, json={"room": "delroom-audit"})
    events = client.get("/admin/audit", headers=admin_headers).json()["events"]
    assert any(e["action"] == "delete_room" and e["room"] == "delroom-audit" for e in events)


# ------------------------------------------------------- admin token on disk
def test_admin_token_file_is_owner_only():
    from argybargy.paths import ADMIN_TOKEN_PATH
    mode = stat.S_IMODE(os.stat(ADMIN_TOKEN_PATH).st_mode)
    assert mode == 0o600, f"admin token file is {oct(mode)}, expected 0o600"


def test_regenerate_rotates_token_and_invalidates_the_old_one(client, admin_headers):
    """Runs last-ish: mutates the shared header dict so later tests keep working."""
    old = admin_headers["X-Admin-Token"]
    new = client.post("/admin/regenerate-token", headers=admin_headers).json()["admin_token"]
    assert new and new != old
    admin_headers["X-Admin-Token"] = new          # keep the session fixture valid
    assert client.get("/admin/state", headers={"X-Admin-Token": old}).status_code == 401
    assert client.get("/admin/state", headers=admin_headers).status_code == 200

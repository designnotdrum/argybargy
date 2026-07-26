"""Dashboard tests: static invariants, JS units, and real-browser behaviour.

Driven by Playwright *from pytest* — one toolchain (uv + pytest), no Node.
Install the browser once with:  uv run playwright install chromium
"""
import json
import re
import time

import pytest

from argybargy import app as appmod
from argybargy.dashboard import DASHBOARD_HTML
from argybargy.hub import ONLINE_WINDOW_SECONDS

playwright_api = pytest.importorskip("playwright.sync_api")


# ============================================================ static analysis
# These need no browser: they guard the "single file, no build, offline" promise.

def test_page_is_self_contained_no_external_requests():
    external = re.findall(r'(?:src|href)\s*=\s*["\']?(?:https?:)?//', DASHBOARD_HTML)
    assert not external, f"dashboard must not reference external hosts: {external}"
    assert "@import" not in DASHBOARD_HTML
    assert not re.search(r'url\(\s*["\']?https?:', DASHBOARD_HTML)


def test_page_has_no_dynamic_code_execution():
    assert "eval(" not in DASHBOARD_HTML
    assert "new Function" not in DASHBOARD_HTML


def test_page_only_talks_to_the_six_admin_endpoints():
    called = set(re.findall(r'fetch\(\s*"(/[^"]*)"', DASHBOARD_HTML))
    called |= set(re.findall(r'api\(\s*"(/[^"]*)"', DASHBOARD_HTML))
    assert called == {
        "/admin/state", "/admin/say", "/admin/invite",
        "/admin/revoke", "/admin/regenerate-token", "/admin/delete-room",
    }, called


def test_no_unsubstituted_template_placeholders():
    for placeholder in ("__CSS__", "__ICONS__", "__LOGOS__"):
        assert placeholder not in DASHBOARD_HTML


def test_brand_and_title_present():
    assert "<title>Argybargy — Admin</title>" in DASHBOARD_HTML
    assert "Argybargy" in DASHBOARD_HTML


# ==================================================================== fixtures
@pytest.fixture(scope="session")
def seeded(client, admin_headers):
    """A room with vendor-branded agents, unbranded agents, and a claimed turn."""
    room = "uiroom"
    codes = {}
    for name, caps in [("claude-ui", "planner"), ("codex-ui", "reviewer"),
                       ("gemini-ui", "research"), ("hermes-ui", "no vendor logo")]:
        r = client.post("/admin/invite", headers=admin_headers,
                        json={"name": name, "room": room, "capabilities": caps})
        codes[name] = r.json()["code"]

    def auth(n):
        return {"Authorization": f"Bearer {codes[n]}"}

    for n in codes:
        client.get("/whoami", headers=auth(n))

    seq = client.post("/messages", headers=auth("claude-ui"),
                      json={"to": "all", "text": "ship it?", "expects_reply": "anyone"}
                      ).json()["message"]["seq"]
    client.post(f"/messages/{seq}/claim", headers=auth("codex-ui"))
    client.post("/messages", headers=auth("codex-ui"),
                json={"to": "claude-ui", "text": "regex bug first"})
    client.post("/messages", headers=auth("claude-ui"),
                json={"to": "all", "text": "fair enough", "expects_reply": "anyone"})
    client.post("/messages", headers=auth("hermes-ui"), json={"to": "all", "text": "unbranded here"})
    return {"room": room, "codes": codes}


@pytest.fixture
def dash(page, live_server, admin_headers, seeded):
    """Dashboard loaded, authenticated, parked on the seeded room."""
    token = admin_headers["X-Admin-Token"]
    page.add_init_script(f"localStorage.setItem('cc_admin', {token!r});")
    page.goto(f"{live_server}/dashboard")
    page.wait_for_selector(f'[data-room="{seeded["room"]}"]', timeout=15000)
    page.click(f'[data-room="{seeded["room"]}"]')
    page.wait_for_selector(".conv-msg", timeout=15000)
    return page


# ================================================================== JS units
# The dashboard exposes its pure helpers on window.__argy for testing.

def test_hue_is_deterministic_and_in_range(dash):
    a = dash.evaluate("window.__argy.hueFor('alice')")
    b = dash.evaluate("window.__argy.hueFor('alice')")
    c = dash.evaluate("window.__argy.hueFor('bob')")
    assert a == b
    assert a != c
    assert 0 <= a < 360


@pytest.mark.parametrize("seconds,expected", [(0, "now"), (0.4, "now"), (5, "5s"),
                                              (59, "59s"), (60, "1m"), (3599, "59m"), (7200, "2h")])
def test_last_seen_formatting(dash, seconds, expected):
    assert dash.evaluate(f"window.__argy.lastSeen({seconds})") == expected


@pytest.mark.parametrize("delta_ms,expected", [(0, "0:00"), (5000, "0:05"),
                                               (65000, "1:05"), (3600000, "60:00")])
def test_elapsed_formatting(dash, delta_ms, expected):
    assert dash.evaluate(f"window.__argy.elapsedSince(0, {delta_ms})") == expected


def test_elapsed_never_goes_negative(dash):
    assert dash.evaluate("window.__argy.elapsedSince(1000, 0)") == "0:00"


@pytest.mark.parametrize("name,expected", [
    ("claude-planner", "brand"), ("anthropic-bot", "brand"), ("codex-reviewer", "brand"),
    ("gpt-worker", "brand"), ("openai-x", "brand"), ("qwen-local", "brand"),
    ("gemini-scout", "brand"), ("cursor-dev", "brand"), ("opencode-x", "brand"),
    ("operator", "person"), ("some-human", "person"),
])
def test_known_vendors_and_person_resolve_to_glyphs(dash, name, expected):
    assert dash.evaluate(f"(window.__argy.glyphFor({name!r})||{{}}).kind") == expected


@pytest.mark.parametrize("name", ["hermes", "llama-farm", "mistral-worker", "zzz"])
def test_unknown_agents_fall_back_to_no_glyph(dash, name):
    """No glyph => the renderer draws the lettered monogram."""
    assert dash.evaluate(f"window.__argy.glyphFor({name!r})") is None
    assert dash.evaluate(f"window.__argy.brandAccent({name!r})") is None


def test_dedupe_keeps_the_liveliest_sighting(dash):
    result = dash.evaluate("""window.__argy.dedupe([
      {name:'a',room:'r1',life:'offline',online:false,secondsSinceSeen:90,hue:1,justJoined:false},
      {name:'a',room:'r2',life:'online', online:true, secondsSinceSeen:0, hue:1,justJoined:true},
      {name:'b',room:'r1',life:'fading', online:false,secondsSinceSeen:3, hue:2,justJoined:false}
    ]).map(function(x){return x.name+':'+x.life})""")
    assert sorted(result) == ["a:online", "b:fading"]


def test_dedupe_donates_status_from_the_freshest_sighting(dash):
    result = dash.evaluate("""window.__argy.dedupe([
      {name:'a',room:'r1',life:'offline',online:false,secondsSinceSeen:300,hue:1,justJoined:false,
       status:'blocked',statusNote:'waiting on auth'},
      {name:'a',room:'r2',life:'online', online:true, secondsSinceSeen:2, hue:1,justJoined:true,
       status:'working',statusNote:'reviewing PR #2'}
    ]).map(function(x){return [x.status,x.statusNote]})[0]""")
    assert result == ["working", "reviewing PR #2"]
@pytest.mark.parametrize("name,expected", [
    ("build", True), ("room-42_v2", True), ("  build  ", True),
    ("", False), ("   ", False), ("Build", False),
    ("room name", False), ("a" * 64, True), ("a" * 65, False),
])
def test_room_name_validation(dash, name, expected):
    assert dash.evaluate(f"window.__argy.isValidRoomName({name!r})") is expected


# ==================================================== mention pure logic
# Ported from the design spec's mapping table. dash.evaluate() against
# window.__argy is this repo's only unit-test mechanism (see the JS units
# section above) — there is no separate JS test runner to run these in.

def test_resolve_wire_payload_zero_chips_broadcasts(dash):
    assert dash.evaluate("window.__argy.resolveWirePayload([])") == {
        "to": "all", "expects_reply": None,
    }


@pytest.mark.parametrize("chips,expected", [
    ([{"id": "b", "name": "bob", "isEveryone": False, "marker": "default"}],
     {"to": "bob", "expects_reply": None}),
    ([{"id": "b", "name": "bob", "isEveryone": False, "marker": "anyone"}],
     {"to": "bob", "expects_reply": "anyone"}),
    ([{"id": "b", "name": "bob", "isEveryone": False, "marker": "off"}],
     {"to": "bob", "expects_reply": "none"}),
    ([{"id": "e", "name": "everyone", "isEveryone": True, "marker": "default"}],
     {"to": "all", "expects_reply": None}),
    ([{"id": "e", "name": "everyone", "isEveryone": True, "marker": "anyone"}],
     {"to": "all", "expects_reply": "anyone"}),
    ([{"id": "b", "name": "bob", "isEveryone": False, "marker": "default"},
      {"id": "a", "name": "alice", "isEveryone": False, "marker": "default"}],
     {"to": "all", "expects_reply": "bob"}),
    ([{"id": "b", "name": "bob", "isEveryone": False, "marker": "default"},
      {"id": "a", "name": "alice", "isEveryone": False, "marker": "anyone"}],
     {"to": "all", "expects_reply": "anyone"}),
    ([{"id": "b", "name": "bob", "isEveryone": False, "marker": "default"},
      {"id": "a", "name": "alice", "isEveryone": False, "marker": "off"}],
     {"to": "all", "expects_reply": "none"}),
    ([{"id": "e", "name": "everyone", "isEveryone": True, "marker": "default"},
      {"id": "b", "name": "bob", "isEveryone": False, "marker": "default"}],
     {"to": "all", "expects_reply": "bob"}),
], ids=[
    "one-peer-default", "one-peer-anyone", "one-peer-off",
    "everyone-alone-default", "everyone-alone-tapped",
    "two-peers-default-first-responds", "two-peers-marker-on-second",
    "two-peers-second-off", "everyone-plus-peer",
])
def test_resolve_wire_payload_matrix(dash, chips, expected):
    result = dash.evaluate(f"window.__argy.resolveWirePayload({json.dumps(chips)})")
    assert result == expected


@pytest.mark.parametrize("marker,expected", [
    ("default", {"to": "bob", "expects_reply": None}),
    ("anyone", {"to": "bob", "expects_reply": "anyone"}),
    ("off", {"to": "bob", "expects_reply": "none"}),
])
def test_resolve_dm_payload(dash, marker, expected):
    result = dash.evaluate(f"window.__argy.resolveDmPayload('bob', {marker!r})")
    assert result == expected


@pytest.mark.parametrize("to,expects_reply,expected", [
    ("all", None, "none"),   # regression test for the bug this spec fixes
    ("bob", None, "bob"),
    ("all", "anyone", "anyone"),
    ("bob", "none", "none"),
])
def test_resolve_expects_for_display(dash, to, expects_reply, expected):
    arg = "null" if expects_reply is None else repr(expects_reply)
    result = dash.evaluate(f"window.__argy.resolveExpectsForDisplay({to!r}, {arg})")
    assert result == expected


@pytest.mark.parametrize("dm_agent,to,expected", [
    ("bob", "all", "bob"),
    (None, "all", "everyone"),
    (None, "bob", "bob"),
])
def test_resolve_to_for_display(dash, dm_agent, to, expected):
    arg = "null" if dm_agent is None else repr(dm_agent)
    result = dash.evaluate(f"window.__argy.resolveToForDisplay({arg}, {to!r})")
    assert result == expected


def test_filter_mention_candidates_empty_query_pins_everyone_first(dash):
    result = dash.evaluate(
        "window.__argy.filterMentionCandidates('', ['claude-ui','codex-ui','gemini-ui'])"
    )
    assert result == [
        {"name": "everyone", "isEveryone": True},
        {"name": "claude-ui", "isEveryone": False},
        {"name": "codex-ui", "isEveryone": False},
        {"name": "gemini-ui", "isEveryone": False},
    ]


def test_filter_mention_candidates_prefix_match(dash):
    result = dash.evaluate(
        "window.__argy.filterMentionCandidates('cod', ['claude-ui','codex-ui','gemini-ui'])"
    )
    assert result == [{"name": "codex-ui", "isEveryone": False}]


def test_filter_mention_candidates_eve_matches_only_everyone(dash):
    result = dash.evaluate(
        "window.__argy.filterMentionCandidates('eve', ['claude-ui','codex-ui'])"
    )
    assert result == [{"name": "everyone", "isEveryone": True}]


def test_filter_mention_candidates_no_peers_still_pins_everyone(dash):
    result = dash.evaluate("window.__argy.filterMentionCandidates('', [])")
    assert result == [{"name": "everyone", "isEveryone": True}]


def test_filter_mention_candidates_excludes_an_already_committed_peer(dash):
    result = dash.evaluate(
        "window.__argy.filterMentionCandidates("
        "'', ['claude-ui','codex-ui','gemini-ui'], ['codex-ui'])"
    )
    assert result == [
        {"name": "everyone", "isEveryone": True},
        {"name": "claude-ui", "isEveryone": False},
        {"name": "gemini-ui", "isEveryone": False},
    ]


def test_filter_mention_candidates_excludes_everyone_once_committed(dash):
    result = dash.evaluate(
        "window.__argy.filterMentionCandidates('', ['claude-ui','codex-ui'], ['everyone'])"
    )
    assert result == [
        {"name": "claude-ui", "isEveryone": False},
        {"name": "codex-ui", "isEveryone": False},
    ]


@pytest.mark.parametrize("text,caret,expected", [
    ("@bo", 3, {"start": 0, "query": "bo"}),
    ("hey @bo", 7, {"start": 4, "query": "bo"}),
    ("email@domain", 12, None),
    ("hey @bob following up", 10, None),
    ("just plain text", 6, None),
    ("@bob", 0, None),
], ids=[
    "at-start", "at-after-whitespace", "not-a-word-boundary",
    "space-closes-trigger", "no-at-at-all", "caret-before-at",
])
def test_find_active_trigger(dash, text, caret, expected):
    result = dash.evaluate(f"window.__argy.findActiveTrigger({text!r}, {caret})")
    assert result == expected


def test_cycle_reply_marker_three_state(dash):
    assert dash.evaluate("window.__argy.cycleReplyMarker('default')") == "anyone"
    assert dash.evaluate("window.__argy.cycleReplyMarker('anyone')") == "off"
    assert dash.evaluate("window.__argy.cycleReplyMarker('off')") == "default"


def test_cycle_chip_marker_peer_is_three_state(dash):
    chip = {"id": "b", "name": "bob", "isEveryone": False, "marker": "default"}
    assert dash.evaluate(f"window.__argy.cycleChipMarker({json.dumps(chip)})") == "anyone"
    chip["marker"] = "anyone"
    assert dash.evaluate(f"window.__argy.cycleChipMarker({json.dumps(chip)})") == "off"
    chip["marker"] = "off"
    assert dash.evaluate(f"window.__argy.cycleChipMarker({json.dumps(chip)})") == "default"


def test_cycle_chip_marker_everyone_is_two_state(dash):
    chip = {"id": "e", "name": "everyone", "isEveryone": True, "marker": "default"}
    assert dash.evaluate(f"window.__argy.cycleChipMarker({json.dumps(chip)})") == "anyone"
    chip["marker"] = "anyone"
    assert dash.evaluate(f"window.__argy.cycleChipMarker({json.dumps(chip)})") == "default"


def test_tap_chip_marker_moves_marker_and_clears_other_peer_chips(dash):
    chips = [
        {"id": "b", "name": "bob", "isEveryone": False, "marker": "anyone"},
        {"id": "a", "name": "alice", "isEveryone": False, "marker": "default"},
    ]
    result = dash.evaluate(f"window.__argy.tapChipMarker({json.dumps(chips)}, 'a')")
    by_id = {c["id"]: c for c in result}
    assert by_id["a"]["marker"] == "anyone"
    assert by_id["b"]["marker"] == "default"


def test_tap_chip_marker_leaves_everyone_chip_untouched(dash):
    chips = [
        {"id": "e", "name": "everyone", "isEveryone": True, "marker": "anyone"},
        {"id": "b", "name": "bob", "isEveryone": False, "marker": "default"},
    ]
    result = dash.evaluate(f"window.__argy.tapChipMarker({json.dumps(chips)}, 'b')")
    by_id = {c["id"]: c for c in result}
    assert by_id["e"]["marker"] == "anyone"
    assert by_id["b"]["marker"] == "anyone"


def test_tap_chip_marker_unknown_id_is_a_no_op(dash):
    chips = [{"id": "b", "name": "bob", "isEveryone": False, "marker": "default"}]
    result = dash.evaluate(f"window.__argy.tapChipMarker({json.dumps(chips)}, 'missing')")
    assert result == chips


@pytest.mark.parametrize("chips,body,expected", [
    ([], "hello", "hello"),
    ([{"id": "b", "name": "bob", "isEveryone": False, "marker": "default"}], "", "@bob"),
    ([{"id": "b", "name": "bob", "isEveryone": False, "marker": "default"}], "ping",
     "@bob ping"),
    ([{"id": "b", "name": "bob", "isEveryone": False, "marker": "default"},
      {"id": "a", "name": "alice", "isEveryone": False, "marker": "default"}], "",
     "@bob @alice"),
    ([{"id": "b", "name": "bob", "isEveryone": False, "marker": "default"},
      {"id": "a", "name": "alice", "isEveryone": False, "marker": "default"}], "sync up",
     "@bob @alice sync up"),
    ([], "", ""),
], ids=[
    "no-chips", "one-chip-no-body", "one-chip-with-body",
    "two-chips-no-body", "two-chips-with-body", "nothing-at-all",
])
def test_serialize_message_text(dash, chips, body, expected):
    result = dash.evaluate(f"window.__argy.serializeMessageText({json.dumps(chips)}, {body!r})")
    assert result == expected


def test_next_chip_id_is_unique_per_call(dash):
    a, b = dash.evaluate("[window.__argy.nextChipId(), window.__argy.nextChipId()]")
    assert a != b


# =================================================== mention composer UI

def test_typing_at_opens_mention_popup_and_filters(dash):
    dash.fill("#composerInput", "@")
    dash.wait_for_selector('[data-testid="mention-popup"]')
    assert dash.locator('[data-testid="mention-popup"]').is_visible()
    names = dash.locator('[data-testid="mention-candidate"]').all_inner_texts()
    assert names == ["everyone", "claude-ui", "codex-ui", "gemini-ui", "hermes-ui"]
    dash.fill("#composerInput", "@cod")
    candidates = dash.locator('[data-testid="mention-candidate"]')
    assert candidates.count() == 1
    assert "codex-ui" in candidates.first.inner_text()


def test_mention_popup_stays_within_the_viewport_when_opened_from_the_composer(dash):
    """Positioning bug: the popup used to open unconditionally *below* the
    trigger via a hardcoded top:calc(100% + 4px) override, but the composer
    sits at the very bottom of this full-height layout — there is no room
    below it, so the popup ran off the bottom of the viewport.

    A plain .is_visible() check isn't enough to catch that: Playwright
    treats an element as visible as soon as any part of it intersects the
    viewport, so a popup hanging mostly off-screen can still report
    visible=true. Assert the real bounding box against the viewport height
    instead — that's the only thing that actually proves it isn't clipped.
    """
    viewport = dash.viewport_size
    dash.fill("#composerInput", "@")
    dash.wait_for_selector('[data-testid="mention-popup"]')
    popup = dash.locator('[data-testid="mention-popup"]')
    assert popup.is_visible()
    box = popup.bounding_box()
    assert box is not None, "popup has no layout box"
    assert box["y"] >= 0, f"popup top ({box['y']}) is above the top of the viewport"
    bottom = box["y"] + box["height"]
    assert bottom <= viewport["height"], (
        f"popup bottom ({bottom}) runs off the {viewport['height']}px-tall "
        "viewport — it should have opened above the composer instead"
    )


def test_mention_candidates_never_include_the_operator(dash):
    dash.fill("#composerInput", "@")
    names = dash.locator('[data-testid="mention-candidate"]').all_inner_texts()
    assert "operator" not in names
    # No offline-peer fixture exists in this suite (an invited-but-never-
    # touched agent never appears as a peer at all — see hub.py, agents only
    # show up once "seen"), so this test can only prove the filter matches
    # the removed to-menu's exact online-peer set, not exercise a live
    # offline-exclusion case. That's the same filter, reused verbatim
    # (dashboard.py's onlinePeerNamesInRoom mirrors :509-511 exactly).
    assert set(names) == {"everyone", "claude-ui", "codex-ui", "gemini-ui", "hermes-ui"}


def test_clicking_a_candidate_commits_a_mention_chip(dash):
    dash.fill("#composerInput", "@cod")
    dash.click('[data-testid="mention-candidate"]')
    assert dash.locator('[data-testid="mention-popup"]').is_hidden()
    chip = dash.locator('[data-testid="mention-chip"]')
    assert chip.count() == 1
    assert "codex-ui" in chip.inner_text()
    # Committing removes "@cod" from the plain-text input — the chip lives in
    # the rail, not inline in the text (see the top-of-plan [design call]).
    assert dash.locator("#composerInput").input_value() == ""


def test_committing_a_chip_returns_focus_to_the_input_for_continued_typing(dash):
    dash.fill("#composerInput", "@cod")
    dash.click('[data-testid="mention-candidate"]')
    assert dash.evaluate("document.activeElement.id") == "composerInput"
    dash.keyboard.type(" ping")
    assert dash.locator("#composerInput").input_value() == " ping"


def test_committing_a_second_chip_stacks_in_commit_order(dash):
    dash.fill("#composerInput", "@cod")
    dash.click('[data-testid="mention-candidate"]')
    dash.keyboard.type("@cla")
    dash.click('[data-testid="mention-candidate"]')
    chips = dash.locator('[data-testid="mention-chip"]').all_inner_texts()
    assert len(chips) == 2
    assert "codex-ui" in chips[0]
    assert "claude-ui" in chips[1]


def test_a_peer_already_chipped_does_not_reappear_as_a_mention_candidate(dash):
    """Review finding: filterMentionCandidates didn't exclude names already in
    S.chips, so the same peer could be committed a second time — two
    @codex-ui chips silently escalate the payload from targeted to broadcast
    (resolveWirePayload's two-or-more-peer-chips rule). Excluding it from the
    candidate list is what makes that escalation impossible from the popup."""
    dash.fill("#composerInput", "@cod")
    dash.click('[data-testid="mention-candidate"]')  # commits codex-ui
    assert dash.locator('[data-testid="mention-chip"]').count() == 1
    dash.click("#composerInput")  # commit already refocuses, but be explicit
    dash.keyboard.type("@")
    dash.wait_for_selector('[data-testid="mention-popup"]')
    names = dash.locator('[data-testid="mention-candidate"]').all_inner_texts()
    assert "codex-ui" not in names
    assert names == ["everyone", "claude-ui", "gemini-ui", "hermes-ui"]


def test_everyone_already_chipped_does_not_reappear_as_a_mention_candidate(dash):
    """Review finding, second consequence: a second @everyone chip is a real,
    tappable, functionally dead control — resolveWirePayload only ever reads
    chips[0] among everyone-chips. Excluding "everyone" once it's committed
    removes the dead control from the popup entirely."""
    dash.fill("#composerInput", "@")
    dash.click('[data-testid="mention-candidate"]')  # "everyone" is pinned first, commits it
    assert dash.locator('[data-testid="mention-chip"]').count() == 1
    dash.click("#composerInput")  # commit already refocuses, but be explicit
    dash.keyboard.type("@")
    dash.wait_for_selector('[data-testid="mention-popup"]')
    names = dash.locator('[data-testid="mention-candidate"]').all_inner_texts()
    assert "everyone" not in names
    assert names == ["claude-ui", "codex-ui", "gemini-ui", "hermes-ui"]


def test_commit_only_plain_at_mentions_with_no_interaction_send_literally_to_all(
    dash, client, admin_headers, seeded
):
    dash.fill("#composerInput", "@codex-ui is faster than @claude-ui")
    assert dash.locator('[data-testid="mention-chip"]').count() == 0
    dash.click("#sendBtn")
    dash.wait_for_timeout(500)
    assert dash.locator('[data-testid="mention-chip"]').count() == 0
    msgs = client.get("/admin/state", headers=admin_headers).json()["messages"]
    sent = [m for m in msgs if m["text"] == "@codex-ui is faster than @claude-ui"
            and m["room"] == seeded["room"]]
    assert sent, "literal text should have reached the relay unparsed"
    assert sent[0]["to"] == "all"


def test_switching_rooms_resets_uncommitted_and_committed_mention_state(dash, seeded):
    dash.fill("#composerInput", "@cod")
    dash.click('[data-testid="mention-candidate"]')
    assert dash.locator('[data-testid="mention-chip"]').count() == 1
    dash.click(f'[data-room="{seeded["room"]}"]')
    assert dash.locator('[data-testid="mention-chip"]').count() == 0
    assert dash.locator('[data-testid="mention-popup"]').is_hidden()


def test_mention_rail_appears_only_while_chips_are_committed(dash, seeded):
    """Nick's call: the rail is absent (not just visually empty) at rest,
    appears the instant a chip commits, and disappears again once the last
    chip is gone — minimum chrome while typing an ordinary message. This
    task's own commit mechanism (click a candidate) and its own reset
    mechanism (switching rooms clears S.chips — the test right above this
    one) are enough to exercise all three states without reaching for Task
    3's decompose/backspace removal, which doesn't exist yet at this point
    in the branch. Task 3's own backspace test adds one more assertion of
    the same fact via genuine single-chip removal, once that exists."""
    rail = dash.locator('[data-testid="mention-rail"]')
    assert rail.is_hidden()
    dash.fill("#composerInput", "@cod")
    dash.click('[data-testid="mention-candidate"]')
    assert rail.is_visible()
    assert dash.locator('[data-testid="mention-chip"]').count() == 1
    dash.click(f'[data-room="{seeded["room"]}"]')
    assert rail.is_hidden()


def test_dm_view_does_not_open_the_mention_popup(dash):
    dash.click('[data-agent="codex-ui"]')
    dash.fill("#composerInput", "@")
    assert dash.locator('[data-testid="mention-popup"]').is_hidden()


def test_committing_via_tab_produces_the_same_chip_as_a_click(dash):
    dash.fill("#composerInput", "@cod")
    dash.press("#composerInput", "Tab")
    chip = dash.locator('[data-testid="mention-chip"]')
    assert chip.count() == 1
    assert "codex-ui" in chip.inner_text()


def test_committing_via_enter_produces_a_chip_and_does_not_send(dash, client, admin_headers, seeded):
    dash.fill("#composerInput", "@cod")
    dash.press("#composerInput", "Enter")
    chip = dash.locator('[data-testid="mention-chip"]')
    assert chip.count() == 1
    assert "codex-ui" in chip.inner_text()
    # A DOM substring check against the timeline collides with an earlier
    # test's already-sent "@codex-ui is faster than @claude-ui" message in
    # this session-scoped room (test_commit_only_plain_at_mentions_with_no_interaction_send_literally_to_all,
    # above) — "@cod" is a substring of "@codex-ui". Check the exact message
    # list via the admin API instead, matching that neighboring test's own
    # technique, so this only fails if "@cod" was itself sent as a message.
    msgs = client.get("/admin/state", headers=admin_headers).json()["messages"]
    assert not any(m["text"] == "@cod" for m in msgs)


def test_arrow_down_then_enter_commits_the_second_candidate(dash):
    dash.fill("#composerInput", "@")
    # Popup order (Task 2's test proved this): everyone, claude-ui, codex-ui,
    # gemini-ui, hermes-ui — one ArrowDown from the default highlight (0)
    # lands on claude-ui.
    dash.press("#composerInput", "ArrowDown")
    dash.press("#composerInput", "Enter")
    chip = dash.locator('[data-testid="mention-chip"]')
    assert chip.count() == 1
    assert "claude-ui" in chip.inner_text()


def test_escape_leaves_the_typed_text_as_plain_text(dash):
    dash.fill("#composerInput", "@cod")
    dash.press("#composerInput", "Escape")
    assert dash.locator('[data-testid="mention-popup"]').is_hidden()
    assert dash.locator('[data-testid="mention-chip"]').count() == 0
    assert dash.locator("#composerInput").input_value() == "@cod"


def test_enter_with_the_popup_closed_still_sends(dash):
    dash.fill("#composerInput", "plain message")
    dash.press("#composerInput", "Enter")
    # doSend() is a two-hop async chain (POST /admin/say, then poll()'s GET
    # /admin/state, then render) — same as every other send-assertion test
    # in this file (see test_commit_only_plain_at_mentions_with_no_interaction_send_literally_to_all
    # above, which waits 500ms after a #sendBtn click for the identical
    # reason). Asserting immediately races the network round trip.
    dash.wait_for_timeout(500)
    assert dash.locator('[data-testid="timeline"]').inner_text().find("plain message") >= 0


def test_backspace_decomposes_the_last_chip_then_removes_the_bare_at(dash):
    dash.fill("#composerInput", "@cod")
    dash.press("#composerInput", "Tab")
    assert dash.locator('[data-testid="mention-chip"]').count() == 1
    assert dash.locator('[data-testid="mention-rail"]').is_visible()
    # Tab-commit prevents the browser's default tab-focus-move and
    # commitMentionCandidate() synchronously refocuses #composerInput either
    # way (see the top-of-plan Architecture section) — press() also
    # auto-focuses its target selector, so this specific sequence has no
    # focus trap to guard against. (Contrast Task 5's chip-tap test, where
    # the trap is real and an explicit refocus is required.)
    dash.press("#composerInput", "Backspace")
    assert dash.locator('[data-testid="mention-chip"]').count() == 0
    # Task 2's test (test_mention_rail_appears_only_while_chips_are_committed)
    # proved this via a room-switch reset, the only removal mechanism it had
    # available; decomposeLastChip() is the genuine single-chip removal path
    # this correction asked to see covered, and it self-refocuses the input
    # (no click involved), so no explicit refocus is needed around this
    # assertion either.
    assert dash.locator('[data-testid="mention-rail"]').is_hidden()
    assert dash.locator('[data-testid="mention-popup"]').is_visible()
    assert dash.locator("#composerInput").input_value() == "@"
    dash.press("#composerInput", "Backspace")
    assert dash.locator('[data-testid="mention-popup"]').is_hidden()
    assert dash.locator("#composerInput").input_value() == ""


def test_preview_strip_matches_the_actual_payload_for_a_plain_broadcast(
    dash, client, admin_headers, seeded
):
    strip = dash.locator('[data-testid="preview-strip"]')
    assert "everyone" in strip.inner_text()
    assert strip.inner_text().endswith("reply expected: none")
    dash.fill("#composerInput", "hello room")
    dash.press("#composerInput", "Enter")
    dash.wait_for_timeout(500)
    msgs = client.get("/admin/state", headers=admin_headers).json()["messages"]
    sent = [m for m in msgs if m["text"] == "hello room" and m["room"] == seeded["room"]]
    assert sent, "message should have reached the relay"
    assert sent[0]["to"] == "all"
    assert sent[0]["expects_reply"] == "none"


def test_preview_strip_shows_the_resolved_target_for_a_committed_chip(dash):
    # Display-only at this point in the branch — doSend() still sources
    # to/expects_reply from the old pills until Task 5's cutover, so this
    # test asserts the strip's TEXT only, not an actual sent payload (that
    # assertion belongs to Task 5, once the strip and the real send path
    # agree).
    dash.fill("#composerInput", "@cod")
    dash.press("#composerInput", "Tab")
    strip = dash.locator('[data-testid="preview-strip"]')
    assert "codex-ui" in strip.inner_text()
    assert strip.inner_text().endswith("reply expected: codex-ui")


def test_dm_view_preview_strip_tap_cycles_the_reply_marker_display(dash):
    dash.click('[data-agent="codex-ui"]')
    strip = dash.locator('[data-testid="preview-strip"]')
    assert strip.inner_text().endswith("reply expected: codex-ui")
    strip.click()
    assert strip.inner_text().endswith("reply expected: anyone")
    strip.click()
    assert strip.inner_text().endswith("reply expected: none")
    strip.click()
    assert strip.inner_text().endswith("reply expected: codex-ui")


def test_room_view_preview_strip_is_not_tappable(dash):
    strip = dash.locator('[data-testid="preview-strip"]')
    before = strip.inner_text()
    strip.click()
    assert strip.inner_text() == before


def test_targeted_mention_matches_the_actual_payload_the_bug_regression(
    dash, client, admin_headers, seeded
):
    """The bug this whole redesign fixes: a targeted send used to show
    'expects · —' while the relay actually resolved an obligated responder.
    The preview strip and the real stored message must agree, and both must
    show the target, not '—'/none."""
    dash.fill("#composerInput", "@cod")
    dash.press("#composerInput", "Tab")
    strip = dash.locator('[data-testid="preview-strip"]')
    assert "codex-ui" in strip.inner_text()
    assert strip.inner_text().endswith("reply expected: codex-ui")
    dash.keyboard.type(" ping")
    dash.keyboard.press("Enter")
    msgs = client.get("/admin/state", headers=admin_headers).json()["messages"]
    sent = [m for m in msgs if m["text"] == "@codex-ui ping" and m["room"] == seeded["room"]]
    assert sent, "message should have reached the relay"
    assert sent[0]["to"] == "codex-ui"
    assert sent[0]["expects_reply"] == "codex-ui"


def test_two_peer_chips_force_to_all_in_the_actual_payload(
    dash, client, admin_headers, seeded
):
    dash.fill("#composerInput", "@cod")
    dash.press("#composerInput", "Tab")
    dash.keyboard.type("@cla")
    dash.press("#composerInput", "Tab")
    assert dash.locator('[data-testid="mention-chip"]').count() == 2
    dash.keyboard.type(" sync up")
    dash.keyboard.press("Enter")
    msgs = client.get("/admin/state", headers=admin_headers).json()["messages"]
    sent = [m for m in msgs if m["text"] == "@codex-ui @claude-ui sync up"
            and m["room"] == seeded["room"]]
    assert sent, "message should have reached the relay"
    assert sent[0]["to"] == "all"
    assert sent[0]["expects_reply"] == "codex-ui"


def test_tapping_a_chip_marker_changes_the_actual_sent_payload(
    dash, client, admin_headers, seeded
):
    dash.fill("#composerInput", "@cod")
    dash.press("#composerInput", "Tab")
    chip = dash.locator('[data-testid="mention-chip"]')
    chip.click()
    # .conv-chip__marker is styled text-transform:uppercase, so Playwright's
    # rendered inner_text() comes back as "ANYONE" even though the DOM text
    # content the app actually sets is lowercase "anyone" — compare
    # case-insensitively rather than fighting a legitimate CSS rule.
    assert "anyone" in chip.inner_text().lower()
    # Clicking the chip moves DOM focus onto the chip button itself —
    # data-chip-tap deliberately does not refocus #composerInput (see the
    # top-of-plan Architecture section: this is the one genuine focus-trap
    # surface in this implementation). Global page.keyboard.type()/press()
    # send to whatever currently has focus, so without this explicit click
    # back into the input the keystrokes below would silently land on the
    # chip button instead.
    dash.click("#composerInput")
    dash.keyboard.type("ping")
    dash.keyboard.press("Enter")
    msgs = client.get("/admin/state", headers=admin_headers).json()["messages"]
    # sent[-1], not sent[0]: the earlier bug-regression test in this same
    # session-scoped room sends the identical text "@codex-ui ping" with a
    # different (untapped) expects_reply, so the first match by text can be
    # that older message rather than the one this test just sent. recent()
    # returns ascending by id, so the last match is the one just sent.
    sent = [m for m in msgs if m["text"] == "@codex-ui ping" and m["room"] == seeded["room"]]
    assert sent, "message should have reached the relay"
    assert sent[-1]["to"] == "codex-ui"
    assert sent[-1]["expects_reply"] == "anyone"


def test_dm_preview_strip_tap_changes_the_actual_sent_payload(
    dash, client, admin_headers, seeded
):
    dash.click('[data-agent="codex-ui"]')
    dash.locator('[data-testid="preview-strip"]').click()  # default -> anyone
    dash.fill("#composerInput", "hey")
    dash.press("#composerInput", "Enter")
    msgs = client.get("/admin/state", headers=admin_headers).json()["messages"]
    sent = [m for m in msgs if m["text"] == "hey" and m["to"] == "codex-ui"]
    assert sent, "message should have reached the relay"
    assert sent[-1]["expects_reply"] == "anyone"


def test_everyone_mention_reply_marker_cycles(dash):
    """Replaces the removed test_expects_pill_cycles: the room-broadcast
    reply-expected toggle is now reached via the @everyone chip instead of
    the deleted #expectsPill, but the same capability (broadcast, is a reply
    expected from anyone) must stay reachable and correct."""
    dash.fill("#composerInput", "@")
    dash.click('[data-testid="mention-candidate"]')  # "everyone" is pinned first
    chip = dash.locator('[data-testid="mention-chip"]')
    assert chip.count() == 1
    strip = dash.locator('[data-testid="preview-strip"]')
    assert strip.inner_text().endswith("reply expected: none")
    chip.click()
    assert strip.inner_text().endswith("reply expected: anyone")
    chip.click()
    assert strip.inner_text().endswith("reply expected: none")


def test_mention_text_in_sent_messages_is_highlighted(dash, client, seeded):
    auth = {"Authorization": f"Bearer {seeded['codes']['claude-ui']}"}
    client.post("/messages", headers=auth,
                json={"to": "all", "text": "hey @codex-ui can you check this"})
    dash.wait_for_timeout(3500)
    highlighted = dash.locator(".conv-mention-text", has_text="@codex-ui")
    assert highlighted.count() >= 1


# ============================================================ rendering
def test_sidebar_lists_rooms_and_agents(dash, seeded):
    assert dash.locator(f'[data-room="{seeded["room"]}"]').count() == 1
    for name in seeded["codes"]:
        assert dash.locator(f'[data-agent="{name}"]').count() == 1, name


def test_vendor_agents_render_a_logo_and_unknown_ones_a_monogram(dash):
    assert dash.locator('[data-agent="claude-ui"] svg.agent-logo').count() == 1
    assert dash.locator('[data-agent="codex-ui"] svg.agent-logo').count() == 1
    assert dash.locator('[data-agent="hermes-ui"] svg.agent-logo').count() == 0
    assert dash.locator('[data-agent="hermes-ui"] .sb-av').inner_text().strip() == "HE"


def test_messages_render_with_claimed_and_expects_badges(dash):
    body = dash.locator(".conv-timeline").inner_text()
    assert "ship it?" in body
    assert "regex bug first" in body
    assert dash.locator('[data-badge="claimed"]').count() >= 1
    assert "codex-ui" in dash.locator('[data-badge="claimed"]').first.inner_text().lower()
    assert dash.locator('[data-badge="expects"]').count() >= 1
    assert re.match(r"\d+:\d{2}", dash.locator(".badge-pill__timer").first.inner_text())


def test_direct_messages_show_their_recipient(dash):
    assert "→ claude-ui" in dash.locator(".conv-timeline").inner_text()


def test_consecutive_messages_group_under_one_author(dash, client, admin_headers, seeded):
    code = seeded["codes"]["gemini-ui"]
    auth = {"Authorization": f"Bearer {code}"}
    for i in range(3):
        client.post("/messages", headers=auth, json={"to": "all", "text": f"grouped-{i}"})
    dash.wait_for_timeout(3500)
    group = dash.locator(".conv-group", has_text="grouped-0")
    assert group.count() >= 1
    assert group.first.locator(".conv-msg").count() >= 3, "one avatar, three message rows"


def test_empty_room_shows_a_friendly_placeholder(dash, client, admin_headers):
    client.post("/admin/invite", headers=admin_headers, json={"name": "lonely", "room": "emptyroom"})
    dash.wait_for_timeout(3500)
    dash.click('[data-room="emptyroom"]')
    assert "Nothing in #emptyroom yet" in dash.locator(".conv-empty").inner_text()


def test_status_subtitle_renders_live_for_an_online_agent(dash, client, admin_headers, seeded):
    code = client.post("/admin/invite", headers=admin_headers,
                       json={"name": "status-ui", "room": seeded["room"]}).json()["code"]
    client.post("/presence", headers={"Authorization": f"Bearer {code}"},
                json={"state": "working", "note": "reviewing PR #2"})
    dash.wait_for_timeout(3500)
    row = dash.locator('[data-agent="status-ui"]')
    assert row.locator(".sb-astatus").inner_text() == "working: reviewing PR #2"
    client.post("/admin/revoke", headers=admin_headers, json={"target": "status-ui"})


def test_status_subtitle_renders_past_tense_once_the_agent_goes_stale(dash, client, admin_headers, seeded):
    code = client.post("/admin/invite", headers=admin_headers,
                       json={"name": "stale-status-ui", "room": seeded["room"]}).json()["code"]
    auth = {"Authorization": f"Bearer {code}"}
    client.post("/presence", headers=auth, json={"state": "blocked", "note": "waiting on auth"})
    # Force this peer's presence past ONLINE_WINDOW_SECONDS so the relay marks
    # it offline (and status_stale) without a real-time sleep — same technique
    # as Task 2's test_offline_peer_status_renders_stale. `live_server` and
    # `client` share the same in-memory Hub instance (conftest.py's
    # docstring), so this mutation is visible to the dashboard's next poll.
    appmod.hub._last_seen[seeded["room"]]["stale-status-ui"] = time.monotonic() - (ONLINE_WINDOW_SECONDS + 5)
    dash.wait_for_timeout(3500)
    dash.click("#recentToggle")
    row = dash.locator('[data-testid="recent-offline-list"] [data-agent="stale-status-ui"]')
    assert row.locator(".sb-astatus").inner_text() == "was blocked: waiting on auth"
    client.post("/admin/revoke", headers=admin_headers, json={"target": "stale-status-ui"})


# ============================================================== interaction
def test_clicking_an_agent_opens_a_filtered_direct_view(dash):
    dash.click('[data-agent="codex-ui"]')
    assert dash.locator('[data-testid="channel-title"]').inner_text() == "codex-ui"
    assert dash.locator(".conv-header__filterchip").count() == 1
    assert dash.locator("#composerInput").get_attribute("placeholder") == "Message @codex-ui"
    # The old locked #toPill is gone (removed with the cutover to @mention
    # chips) — its replacement is the always-visible wire-preview strip,
    # which must show the DM's locked target truthfully.
    assert "codex-ui" in dash.locator('[data-testid="preview-strip"]').inner_text()
    dash.click("#backToRoom")
    assert dash.locator('[data-testid="channel-title"]').inner_text() == "uiroom"


def test_invite_action_offers_agents_from_other_rooms_not_the_admin_panel(dash, client, admin_headers, seeded):
    # An agent that exists on the mesh but is not in the room being viewed.
    client.post("/admin/invite", headers=admin_headers, json={"name": "elsewhere-bot", "room": "someotherroom"})
    dash.wait_for_timeout(3500)

    dash.click("#convInviteBtn")
    dash.wait_for_selector('[data-testid="invite-picker"]')
    # The admin drawer must NOT be what opens.
    assert dash.locator("#adRoot").count() == 0

    candidates = dash.locator('[data-testid="invite-candidates"]')
    assert candidates.locator('[data-invite-name="elsewhere-bot"]').count() == 1

    dash.click('[data-invite-name="elsewhere-bot"]')
    dash.wait_for_selector(".ad-resultbox", timeout=10000)

    codes = client.get("/admin/state", headers=admin_headers).json()["codes"]
    assert any(c["name"] == "elsewhere-bot" and c["room"] == seeded["room"] for c in codes)

    # The human's next step is the connect instruction, not a bare token.
    assert "Authorization: Bearer" in dash.locator("#ipInstruction").inner_text()

    # And the agent is visibly in the room straight away, marked as pending.
    dash.click("#ipClose")
    row = dash.locator('[data-invited="elsewhere-bot"]')
    assert row.count() == 1
    assert "not connected yet" in row.inner_text()


def test_create_room_offers_existing_agents_and_shows_only_a_code_for_them(dash, client, admin_headers, seeded):
    dash.click("#openCreateRoom")
    dash.wait_for_selector("#crRoot")

    # Agents already on the mesh are pickable rather than retyped.
    existing = sorted(seeded["codes"])[0]
    dash.click(f'[data-create-pick="{existing}"]')
    assert dash.locator("#crName").input_value() == existing

    dash.fill("#crRoom", "picked-room")
    dash.click("#crSubmit")
    dash.wait_for_selector("#crOut .ad-resultbox", timeout=10000)

    # Known agent: it already speaks the protocol, so it only needs the code —
    # not the whole connect spiel.
    out = dash.locator("#crOut").inner_text()
    assert "code for the new room" in out
    assert "Authorization: Bearer" not in out

    codes = client.get("/admin/state", headers=admin_headers).json()["codes"]
    assert any(c["name"] == existing and c["room"] == "picked-room" for c in codes)


def test_create_room_gives_a_brand_new_agent_the_full_connect_instruction(dash):
    dash.click("#openCreateRoom")
    dash.wait_for_selector("#crRoot")
    dash.fill("#crRoom", "greenfield")
    dash.fill("#crName", "never-seen-before")
    dash.click("#crSubmit")
    dash.wait_for_selector("#crOut .ad-resultbox", timeout=10000)
    assert "Authorization: Bearer" in dash.locator("#crOut").inner_text()


def test_invite_picker_excludes_agents_already_in_the_room(dash, seeded):
    dash.click("#convInviteBtn")
    dash.wait_for_selector('[data-testid="invite-picker"]')
    for name in seeded["codes"]:
        assert dash.locator(f'[data-invite-name="{name}"]').count() == 0


def test_theme_toggle_applies_and_persists(dash, live_server):
    dash.click("#theme-light")
    assert dash.evaluate("document.documentElement.getAttribute('data-theme')") == "light"
    assert dash.evaluate("localStorage.getItem('cc_theme')") == "light"
    dash.reload()
    dash.wait_for_selector(".sb-root")
    assert dash.evaluate("document.documentElement.getAttribute('data-theme')") == "light"
    dash.click("#theme-dark")
    assert dash.evaluate("document.documentElement.getAttribute('data-theme')") == "dark"


def test_operator_can_send_a_message_to_the_room(dash, client, admin_headers, seeded):
    dash.fill("#composerInput", "operator says hello")
    dash.click("#sendBtn")
    dash.wait_for_timeout(500)
    assert dash.locator("#composerInput").input_value() == ""
    msgs = client.get("/admin/state", headers=admin_headers).json()["messages"]
    sent = [m for m in msgs if m["text"] == "operator says hello" and m["room"] == seeded["room"]]
    assert sent, "message should have reached the relay"
    assert sent[0]["from"] == "operator"


def test_archiving_a_room_moves_it_into_the_archived_disclosure_and_back(dash, client, admin_headers):
    client.post("/admin/invite", headers=admin_headers, json={"name": "archivee", "room": "archiveroom"})
    # Wait for the next poll to surface the room rather than sleeping a fixed
    # interval — under full-suite load a bare 3.5s wait races the 3s poll.
    dash.wait_for_selector('[data-room="archiveroom"]', timeout=15000)

    # The ⋯ trigger is revealed on row hover, so hover before clicking it —
    # without this Playwright fails actionability on a display:none element.
    dash.hover('[data-room="archiveroom"]')
    dash.click('[data-room-menu="archiveroom"]')
    dash.click('[data-archive-room="archiveroom"]')

    room_list = dash.locator('[data-testid="room-list"]')
    assert room_list.locator('[data-room="archiveroom"]').count() == 0

    archived_toggle = dash.locator("#archivedToggle")
    assert archived_toggle.is_visible()
    dash.click("#archivedToggle")
    archived_list = dash.locator('[data-testid="archived-room-list"]')
    assert archived_list.locator('[data-room="archiveroom"]').count() == 1

    dash.click('[aria-label="Restore archiveroom"]')
    assert room_list.locator('[data-room="archiveroom"]').count() == 1


def test_delete_room_requires_typing_the_exact_room_name_to_confirm(dash, client, admin_headers):
    client.post("/admin/invite", headers=admin_headers, json={"name": "condemned", "room": "condemned-room"})
    dash.wait_for_selector('[data-room="condemned-room"]', timeout=15000)

    dash.hover('[data-room="condemned-room"]')
    dash.click('[data-room-menu="condemned-room"]')
    dash.click('[data-delete-room="condemned-room"]')

    dialog = dash.locator('[data-testid="delete-room-dialog"]')
    assert dialog.is_visible()
    confirm = dash.locator("#drdConfirm")
    assert confirm.is_disabled()

    dash.fill("#drdConfirmInput", "wrong-name")
    assert confirm.is_disabled()

    dash.fill("#drdConfirmInput", "condemned-room")
    assert confirm.is_enabled()

    confirm.click()
    dash.wait_for_selector('[data-testid="delete-room-dialog"]', state="detached", timeout=15000)

    state = client.get("/admin/state", headers=admin_headers).json()
    assert all(c["room"] != "condemned-room" for c in state["codes"])


def test_delete_room_dialog_can_be_cancelled(dash, client, admin_headers):
    client.post("/admin/invite", headers=admin_headers, json={"name": "spared", "room": "spared-room"})
    dash.wait_for_selector('[data-room="spared-room"]', timeout=15000)

    dash.hover('[data-room="spared-room"]')
    dash.click('[data-room-menu="spared-room"]')
    dash.click('[data-delete-room="spared-room"]')
    dash.wait_for_selector('[data-testid="delete-room-dialog"]')
    dash.click("#drdCancel")
    assert dash.locator('[data-testid="delete-room-dialog"]').count() == 0

    state = client.get("/admin/state", headers=admin_headers).json()
    assert any(c["room"] == "spared-room" for c in state["codes"]), "cancel must not delete anything"


def test_deleting_the_room_you_are_viewing_from_a_dm_does_not_leave_a_dangling_view(dash, client, admin_headers):
    """Every other delete test deletes some other room. This one deletes the room
    currently being viewed while parked in a DM sub-view inside it — the case where
    S.view kept pointing at a room that no longer existed, the composer stayed live,
    and there was no reachable listener left for whatever got sent into it."""
    code = client.post("/admin/invite", headers=admin_headers,
                        json={"name": "doomed-agent", "room": "doomed-room"}).json()["code"]
    # A merely-invited (not-yet-connected) agent renders as data-invited, not
    # data-agent, and isn't clickable into a DM — so make it check in first.
    client.get("/whoami", headers={"Authorization": f"Bearer {code}"})
    dash.wait_for_selector('[data-room="doomed-room"]', timeout=15000)

    dash.click('[data-room="doomed-room"]')
    dash.wait_for_selector('[data-agent="doomed-agent"]', timeout=15000)
    dash.click('[data-agent="doomed-agent"]')
    assert dash.locator('[data-testid="channel-title"]').inner_text() == "doomed-agent"

    dash.hover('[data-room="doomed-room"]')
    dash.click('[data-room-menu="doomed-room"]')
    dash.click('[data-delete-room="doomed-room"]')
    dash.wait_for_selector('[data-testid="delete-room-dialog"]')
    dash.fill("#drdConfirmInput", "doomed-room")
    dash.click("#drdConfirm")
    dash.wait_for_selector('[data-testid="delete-room-dialog"]', state="detached", timeout=15000)

    # The next poll must snap the dangling DM view back to a room that still
    # exists — not leave the header/composer pointed at a room nobody can read.
    dash.wait_for_function(
        "() => document.querySelector('[data-testid=\"channel-title\"]').textContent !== 'doomed-agent'",
        timeout=15000,
    )
    assert dash.locator("#backToRoom").count() == 0
    assert dash.locator(".conv-header__filterchip").count() == 0
    placeholder = dash.locator("#composerInput").get_attribute("placeholder")
    assert placeholder != "Message @doomed-agent"
    assert placeholder.startswith("Message #")
    assert dash.locator('[data-room="doomed-room"]').count() == 0


def test_sidebar_and_conversation_pane_show_a_create_room_cta_with_zero_rooms(page, live_server, admin_headers):
    token = admin_headers["X-Admin-Token"]
    page.add_init_script(f"localStorage.setItem('cc_admin', {token!r});")
    page.goto(f"{live_server}/dashboard")
    page.wait_for_selector(".sb-root")
    page.evaluate("""() => window.__setState({
        codes: [], hash_codes: false, messages: [], peers: {}, public_url: 'u'
    })""")

    assert "No rooms yet" in page.locator('[data-testid="sidebar"]').inner_text()
    assert "No rooms yet" in page.locator('[data-testid="conversation-pane"]').inner_text()

    # The sidebar states the fact only — the "+" in the Rooms header is already
    # the create affordance there, so a second button would be redundant. The
    # conversation pane carries the actual call to action.
    assert page.locator("#sbEmptyCreateRoom").count() == 0
    page.click("#convNoRoomCreate")
    page.wait_for_selector("#crRoot")


def test_mobile_viewport_collapses_the_sidebar_into_a_drawer(dash):
    dash.set_viewport_size({"width": 375, "height": 812})
    nav = dash.locator("#navWrap")
    assert "-translate-x-full" in nav.get_attribute("class")
    dash.click("#navOpen")
    assert "translate-x-0" in nav.get_attribute("class")
    assert dash.locator("#navScrim").is_visible()
    assert dash.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1"), \
        "no horizontal overflow on mobile"


# ============================================================== admin drawer
def test_drawer_mints_and_revokes_a_key(dash, client, admin_headers):
    dash.click("#openDrawer")
    dash.wait_for_selector("#adRoot")
    dash.fill("#adName", "ui-minted")
    dash.fill("#adCap", "made in the browser")
    dash.select_option("#adExpiry", "1d")
    dash.click("#adMint")
    dash.wait_for_selector(".ad-resultbox", timeout=10000)

    codes = client.get("/admin/state", headers=admin_headers).json()["codes"]
    minted = [c for c in codes if c["name"] == "ui-minted"]
    assert minted, "mint should have created a key"
    assert minted[0]["capabilities"] == "made in the browser"

    dash.wait_for_selector('[data-revoke="ui-minted"]', timeout=10000)
    dash.click('[data-revoke="ui-minted"]')           # arm
    dash.click('[data-revoke="ui-minted"]')           # confirm
    dash.wait_for_timeout(800)
    names = [c["name"] for c in client.get("/admin/state", headers=admin_headers).json()["codes"]]
    assert "ui-minted" not in names


def test_drawer_shows_public_url_and_key_count(dash, client, admin_headers):
    dash.click("#openDrawer")
    dash.wait_for_selector("#adRoot")
    assert dash.locator("#adUrl").inner_text().startswith("http")
    expected = len(client.get("/admin/state", headers=admin_headers).json()["codes"])
    assert dash.locator("#adKeyCount").inner_text().strip() == f"· {expected}"


def test_sidebar_plus_creates_a_room_and_shows_the_mint_result(dash, client, admin_headers):
    dash.click("#openCreateRoom")
    dash.wait_for_selector("#crRoot")
    submit = dash.locator("#crSubmit")
    assert submit.is_disabled()

    dash.fill("#crRoom", "launchpad")
    assert submit.is_disabled(), "still needs an agent name"
    dash.fill("#crName", "scout")
    assert submit.is_enabled()

    submit.click()
    dash.wait_for_selector("#crOut .ad-resultbox", timeout=10000)

    codes = client.get("/admin/state", headers=admin_headers).json()["codes"]
    minted = [c for c in codes if c["name"] == "scout" and c["room"] == "launchpad"]
    assert minted, "mint should have created launchpad"


def test_create_room_warns_but_does_not_block_on_a_duplicate_name(dash, seeded):
    dash.click("#openCreateRoom")
    dash.wait_for_selector("#crRoot")
    dash.fill("#crRoom", seeded["room"])
    assert "already exists" in dash.locator("#crHint").inner_text()
    dash.fill("#crName", "another-agent")
    assert dash.locator("#crSubmit").is_enabled(), "duplicate name warns, does not block"


def test_bad_token_surfaces_an_error_state(page, live_server):
    page.add_init_script("localStorage.setItem('cc_admin','totally-wrong');")
    page.goto(f"{live_server}/dashboard")
    page.wait_for_selector("#connDot.error", timeout=15000)
    assert page.locator('[data-agent]').count() == 0


# ==================================================================== security
def test_message_text_is_never_interpreted_as_html(dash, client, seeded):
    """Agent-supplied text must render as text — no injection into the operator's page."""
    payload = '<img src=x onerror="window.__xss=1"><script>window.__xss=1</script>'
    auth = {"Authorization": f"Bearer {seeded['codes']['claude-ui']}"}
    client.post("/messages", headers=auth, json={"to": "all", "text": payload})
    dash.wait_for_timeout(3500)
    assert dash.evaluate("window.__xss") is None, "agent text executed as script"
    assert dash.locator(".conv-timeline img").count() == 0
    assert payload in dash.locator(".conv-timeline").inner_text(), "should render literally"


def test_agent_names_are_never_interpreted_as_html(dash, client, admin_headers):
    evil = '<img src=x onerror="window.__xss2=1">'
    client.post("/admin/invite", headers=admin_headers, json={"name": evil, "room": "uiroom"})
    dash.wait_for_timeout(3500)
    assert dash.evaluate("window.__xss2") is None
    assert dash.locator(".sb-root img").count() == 0
    client.post("/admin/revoke", headers=admin_headers, json={"target": evil})


def test_capabilities_are_never_interpreted_as_html(dash, client, admin_headers):
    client.post("/admin/invite", headers=admin_headers,
                json={"name": "capsy-xss", "room": "uiroom",
                      "capabilities": '<img src=x onerror="window.__xss3=1">'})
    dash.click("#openDrawer")
    dash.wait_for_selector("#adRoot")
    dash.wait_for_timeout(3500)
    assert dash.evaluate("window.__xss3") is None
    assert dash.locator(".ad-kcap img").count() == 0
    client.post("/admin/revoke", headers=admin_headers, json={"target": "capsy-xss"})


def test_status_note_is_never_interpreted_as_html(dash, client, admin_headers, seeded):
    code = client.post("/admin/invite", headers=admin_headers,
                       json={"name": "status-note-xss", "room": seeded["room"]}).json()["code"]
    client.post("/presence", headers={"Authorization": f"Bearer {code}"},
                json={"state": "working", "note": '<img src=x onerror="window.__xss4=1">'})
    dash.wait_for_timeout(3500)
    assert dash.evaluate("window.__xss4") is None
    assert dash.locator(".sb-astatus img").count() == 0
    client.post("/admin/revoke", headers=admin_headers, json={"target": "status-note-xss"})


def test_dashboard_makes_no_third_party_requests(page, live_server, admin_headers):
    seen = []
    page.on("request", lambda r: seen.append(r.url))
    token = admin_headers["X-Admin-Token"]
    page.add_init_script(f"localStorage.setItem('cc_admin', {token!r});")
    page.goto(f"{live_server}/dashboard")
    page.wait_for_selector(".sb-root")
    page.wait_for_timeout(1500)
    offsite = [u for u in seen if not u.startswith(live_server) and not u.startswith("data:")]
    assert not offsite, f"dashboard reached off-origin: {offsite}"


def test_page_has_no_console_errors(page, live_server, admin_headers):
    errors = []
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append(str(e)))
    token = admin_headers["X-Admin-Token"]
    page.add_init_script(f"localStorage.setItem('cc_admin', {token!r});")
    page.goto(f"{live_server}/dashboard")
    page.wait_for_selector(".sb-root")
    page.wait_for_timeout(2000)
    assert not errors, errors


# =============================================================== accessibility
def test_interactive_controls_have_accessible_names(dash):
    for selector in ('[data-room]', '[data-agent]', "#openDrawer", "#theme-auto", "#sendBtn"):
        el = dash.locator(selector).first
        name = el.get_attribute("aria-label") or el.inner_text().strip()
        assert name, f"{selector} has no accessible name"


def test_active_room_is_marked_for_assistive_tech(dash, seeded):
    active = dash.locator(f'[data-room="{seeded["room"]}"]')
    assert active.get_attribute("aria-current") == "true"

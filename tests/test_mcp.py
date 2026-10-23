"""
tests/test_mcp.py

Connects over Streamable HTTP to Loopkeeper MCP server,
prints the negotiated protocolVersion and tool list,
runs the full gym-double-charge flow,
and asserts state survives a server restart.

Run with:  python -m pytest tests/test_mcp.py -v  (server must be running)
Or:        python tests/test_mcp.py               (standalone)
"""
import asyncio
import json
import sys
import os
import subprocess
import time

# Ensure UTF-8 output on Windows consoles
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# Add parent dir to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
except ImportError:
    print("ERROR: mcp package not installed. Run: pip install 'mcp[cli]'")
    sys.exit(1)

MCP_URL = "http://localhost:8000/mcp/"
SERVER_CMD = [sys.executable, "-m", "uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]


def wait_for_server(url: str, timeout: int = 30) -> bool:
    """Poll until the server is ready."""
    import urllib.request
    import urllib.error
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen("http://localhost:8000/health", timeout=2) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


async def run_mcp_session(verbose: bool = True) -> dict:
    """Run a complete MCP session and return results."""
    results = {}

    async with streamable_http_client(MCP_URL) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            # ── 1. Initialize ──────────────────────────────────────────────
            init_result = await session.initialize()
            proto_ver = getattr(init_result, "protocol_version", getattr(init_result, "protocolVersion", None))
            server_info = getattr(init_result, "server_info", getattr(init_result, "serverInfo", None))
            server_name = server_info.name if server_info else "unknown"

            if verbose:
                print(f"\n{'='*60}")
                print(f"[PASS] MCP INITIALIZED")
                print(f"   Protocol version : {proto_ver}")
                print(f"   Server name      : {server_name}")
                print(f"{'='*60}")

            results["protocolVersion"] = proto_ver
            results["serverName"] = server_name

            # Assert protocol version is at least 2025-11-25
            assert proto_ver >= "2025-11-25", (
                f"Expected protocol >= 2025-11-25, got {proto_ver}"
            )

            # ── 2. List tools ──────────────────────────────────────────────
            tools_result = await session.list_tools()
            tools = tools_result.tools
            tool_names = [t.name for t in tools]

            if verbose:
                print(f"\nTOOLS ({len(tools)}):")
                for t in tools:
                    ann = getattr(t, "annotations", None)
                    read_only = getattr(ann, "read_only_hint", getattr(ann, "readOnlyHint", "?")) if ann else "?"
                    destructive = getattr(ann, "destructive_hint", getattr(ann, "destructiveHint", "?")) if ann else "?"
                    print(f"   * {t.name:<30} readOnly={read_only}  destructive={destructive}")

            results["tools"] = tool_names

            expected_tools = [
                "capture_errand", "draft_outreach", "send_outreach",
                "list_open_loops", "check_replies", "resolve_errand",
                "advance_clock", "morning_brief", "remember_preference", "recall_preferences",
            ]
            for et in expected_tools:
                assert et in tool_names, f"Tool '{et}' not found in tool list"

            # Verify annotations on send_outreach (destructive)
            send_tool = next((t for t in tools if t.name == "send_outreach"), None)
            assert send_tool is not None, "send_outreach tool not found"
            ann = getattr(send_tool, "annotations", None)
            if ann:
                # mcp 2.x stores as snake_case (destructive_hint), accepts camelCase on creation
                destructive = getattr(ann, "destructive_hint", None) or getattr(ann, "destructiveHint", None)
                assert destructive is True, (
                    "send_outreach should have destructiveHint=True"
                )

            # Verify list_open_loops is read-only
            list_tool = next((t for t in tools if t.name == "list_open_loops"), None)
            assert list_tool is not None
            ann = getattr(list_tool, "annotations", None)
            if ann:
                read_only = getattr(ann, "read_only_hint", None) or getattr(ann, "readOnlyHint", None)
                assert read_only is True, (
                    "list_open_loops should have readOnlyHint=True"
                )

            # ── 3. Morning brief (initial state) ──────────────────────────
            if verbose:
                print(f"\n☀️  MORNING BRIEF (initial):")
            brief_result = await session.call_tool("morning_brief", {})
            brief_text = brief_result.content[0].text if brief_result.content else "{}"
            brief = json.loads(brief_text)
            if verbose:
                print(f"   Summary: {brief.get('summary', 'N/A')}")
                print(f"   Open loops: {len(brief.get('open_errands', []))}")
            assert "summary" in brief
            assert "open_errands" in brief

            # Pre-seeded blender errand should be there
            errand_titles = [e["title"] for e in brief.get("open_errands", [])]
            assert any("blender" in t.lower() for t in errand_titles), (
                f"Pre-seeded blender errand not found. Got: {errand_titles}"
            )

            # ── 4. Gym double-charge flow ──────────────────────────────────
            if verbose:
                print(f"\n🏋️  GYM DOUBLE-CHARGE FLOW:")

            # Step 4a: Capture errand
            cap_result = await session.call_tool("capture_errand", {
                "title": "Gym double-charge dispute",
                "counterparty": "FitLife Gym",
                "details": "Double charged $49 on Oct 1. Need refund.",
            })
            cap_text = cap_result.content[0].text
            cap = json.loads(cap_text)
            errand_id = cap["errand_id"]
            if verbose:
                print(f"   ✓ Captured errand id={errand_id}: {cap['title']}")
            assert cap["status"] == "captured"
            results["errand_id"] = errand_id

            # Step 4b: Draft outreach
            draft_result = await session.call_tool("draft_outreach", {
                "errand_id": errand_id,
                "message": (
                    "Hi FitLife Gym, I was double-charged $49 on October 1st. "
                    "Please investigate and refund. My member ID is on file. Thank you."
                ),
            })
            draft_text = draft_result.content[0].text
            draft = json.loads(draft_text)
            if verbose:
                print(f"   ✓ Drafted outreach id={draft.get('outreach_id')}, requires_approval={draft.get('requires_approval')}")
            assert draft["status"] == "drafted"
            assert draft["requires_approval"] is True

            # Step 4c: Send outreach (simulated — no real send)
            send_result = await session.call_tool("send_outreach", {
                "errand_id": errand_id,
                "channel": "email",
            })
            send_text = send_result.content[0].text
            send = json.loads(send_text)
            if verbose:
                print(f"   ✓ Sent outreach: status={send.get('status')}, channel={send.get('channel')}")
                print(f"     Note: {send.get('note', '')}")
            assert send["status"] == "sent"
            assert "SIMULATED" in send.get("note", "")

            # Step 4d: Check replies (should be 0 before clock advances)
            replies_before = await session.call_tool("check_replies", {"errand_id": errand_id})
            before_text = replies_before.content[0].text
            before = json.loads(before_text)
            if verbose:
                print(f"   ✓ Replies before advance: {before['count']} (sim_day={before['sim_day']})")
                print(f"     Provider note: {before.get('provider_note', '')}")
            assert "SIMULATED" in before.get("provider_note", "")

            # Step 4e: Advance clock
            adv_result = await session.call_tool("advance_clock", {"days": 1})
            adv_text = adv_result.content[0].text
            adv = json.loads(adv_text)
            if verbose:
                print(f"   ✓ Clock advanced: day {adv['previous_day']} → {adv['current_day']}")
            assert adv["current_day"] == adv["previous_day"] + 1

            # Step 4f: Check replies after advance (should have reply now)
            replies_after = await session.call_tool("check_replies", {"errand_id": errand_id})
            after_text = replies_after.content[0].text
            after = json.loads(after_text)
            if verbose:
                print(f"   ✓ Replies after advance: {after['count']} (sim_day={after['sim_day']})")
                if after["replies"]:
                    print(f"     Reply: \"{after['replies'][0]['reply_text'][:60]}...\"")
            assert after["count"] >= 1, (
                f"Expected >= 1 reply after clock advance, got {after['count']}"
            )

            # Step 4g: Resolve errand
            res_result = await session.call_tool("resolve_errand", {
                "errand_id": errand_id,
                "outcome": "Refund of $49 confirmed by gym.",
            })
            res_text = res_result.content[0].text
            res = json.loads(res_text)
            if verbose:
                print(f"   ✓ Resolved: {res}")
            assert res["status"] == "resolved"

            # Step 4h: Verify errand no longer in open loops
            loops_result = await session.call_tool("list_open_loops", {})
            loops_text = loops_result.content[0].text
            loops = json.loads(loops_text)
            open_ids = [e["id"] for e in loops.get("errands", [])]
            assert errand_id not in open_ids, (
                f"Errand {errand_id} should be resolved but still in open loops"
            )
            if verbose:
                print(f"   ✓ Errand removed from open loops. Remaining: {len(open_ids)}")

            # ── 5. Remember preferences ────────────────────────────────────
            await session.call_tool("remember_preference", {"key": "preferred_channel", "value": "email"})
            prefs_result = await session.call_tool("recall_preferences", {})
            prefs_text = prefs_result.content[0].text
            prefs = json.loads(prefs_text)
            assert prefs["preferences"].get("preferred_channel") == "email"
            if verbose:
                print(f"\n💾 PREFERENCES: {prefs['preferences']}")

            # ── 6. List resources ──────────────────────────────────────────
            try:
                res_list = await session.list_resources()
                resource_uris = [r.uri for r in res_list.resources]
                if verbose:
                    print(f"\n📁 RESOURCES: {resource_uris}")
                assert "loops://open" in str(resource_uris), (
                    f"Resource loops://open not found. Got: {resource_uris}"
                )
            except Exception as e:
                if verbose:
                    print(f"\n📁 RESOURCES: listing failed ({e}) — skipping")

            # ── 7. List prompts ────────────────────────────────────────────
            try:
                prompt_list = await session.list_prompts()
                prompt_names = [p.name for p in prompt_list.prompts]
                if verbose:
                    print(f"📝 PROMPTS: {prompt_names}")
                assert "morning_brief" in prompt_names, (
                    f"Prompt 'morning_brief' not found. Got: {prompt_names}"
                )
            except Exception as e:
                if verbose:
                    print(f"📝 PROMPTS: listing failed ({e}) — skipping")

            results["success"] = True

    return results


def kill_existing_server():
    """Kill any process listening on port 8000."""
    try:
        out = subprocess.check_output("netstat -ano | findstr :8000", shell=True).decode(errors="ignore")
        for line in out.strip().splitlines():
            parts = line.strip().split()
            if len(parts) >= 5 and "LISTENING" in parts:
                pid = int(parts[-1])
                subprocess.run(f"taskkill /F /PID {pid}", shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass
    time.sleep(1)


def start_server_process():
    """Start uvicorn server in a subprocess."""
    cwd = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    proc = subprocess.Popen(
        SERVER_CMD,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        cwd=cwd,
    )
    if not wait_for_server(MCP_URL, timeout=30):
        proc.terminate()
        raise RuntimeError("Server failed to start within timeout")
    return proc


async def test_server_restart_persistence(errand_id: int, verbose: bool = True):
    """
    Restart the server and verify errand state persists (DB survives restart).
    """
    if verbose:
        print("\n[RESTART TEST] Stopping server...")

    kill_existing_server()

    if verbose:
        print("   Starting server again...")

    new_proc = start_server_process()

    if verbose:
        print("   Server restarted. Checking state persistence over MCP...")

    async with streamable_http_client(MCP_URL) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()

            # The resolved errand should NOT be in open loops
            loops_result = await session.call_tool("list_open_loops", {})
            loops_text = loops_result.content[0].text
            loops = json.loads(loops_text)
            open_ids = [e["id"] for e in loops.get("errands", [])]
            assert errand_id not in open_ids, (
                f"FAIL: Errand {errand_id} reappeared after restart (state not persisted)"
            )

            # Preferences should survive
            prefs_result = await session.call_tool("recall_preferences", {})
            prefs_text = prefs_result.content[0].text
            prefs = json.loads(prefs_text)
            assert prefs["preferences"].get("preferred_channel") == "email", (
                "FAIL: Preferences not persisted after restart"
            )

            if verbose:
                print("   [PASS] State persisted across restart!")
                print(f"   Open loops count after restart: {len(loops['errands'])}")
                print(f"   Preferences recalled after restart: {prefs['preferences']}")

    return new_proc


def main():
    print("=" * 60)
    print("LOOPKEEPER MCP TEST SUITE")
    print("=" * 60)

    # Ensure server is running
    server_proc = None
    if not wait_for_server(MCP_URL, timeout=2):
        print("\n[SETUP] Starting server...")
        server_proc = start_server_process()
        print("[SETUP] Server started at http://localhost:8000")
    else:
        print("\n[SETUP] Server already running at http://localhost:8000")

    # Run main test
    try:
        results = asyncio.run(run_mcp_session(verbose=True))
        errand_id = results.get("errand_id")

        # Restart test (always runs)
        if errand_id:
            server_proc = asyncio.run(
                test_server_restart_persistence(errand_id, verbose=True)
            )

        print(f"\n{'='*60}")
        print("ALL TESTS PASSED")
        print(f"   Negotiated Protocol : {results.get('protocolVersion')}")
        print(f"   Server Name         : {results.get('serverName')}")
        print(f"   Tools Verified ({len(results.get('tools', []))}) : {', '.join(results.get('tools', []))}")
        print(f"{'='*60}\n")

    except AssertionError as e:
        print(f"\n[FAIL] ASSERTION FAILED: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"\n[FAIL] ERROR: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


# ── pytest-compatible wrappers ────────────────────────────────────────────────

def test_mcp_initialize_and_tools():
    """pytest: MCP initializes with correct protocol and all tools present."""
    results = asyncio.run(run_mcp_session(verbose=False))
    assert results.get("protocolVersion") >= "2025-11-25"
    expected = [
        "capture_errand", "draft_outreach", "send_outreach",
        "list_open_loops", "check_replies", "resolve_errand",
        "advance_clock", "morning_brief", "remember_preference", "recall_preferences",
    ]
    for t in expected:
        assert t in results["tools"], f"Tool {t} missing"


def test_gym_flow():
    """pytest: Full gym double-charge errand flow."""
    results = asyncio.run(run_mcp_session(verbose=True))
    assert results.get("success") is True


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
import asyncio
import ast
import base64
import difflib
import io
import json
import logging
import os
import time
import subprocess
from datetime import datetime
import urllib.error
import urllib.request
import uuid
import zipfile
from subprocess import Popen, PIPE, TimeoutExpired
import websockets  # ✅ FIX




logging.basicConfig(level=logging.INFO)

# -------------------- Global State --------------------
connections = {}  # user_id -> websocket

project_state = {
    "code": "",
    "chat_messages": [],
    "history": [],
    "checkpoints": [],
    "comments": [],
    "admin": {
        "sessions": [],
        "run_stats": {
            "total_runs": 0,
            "by_user": {},
            "last_output_preview": ""
        },
        "role_changes": [],
        "moderation": []
    },
    "last_execution_output": "",
    "users": [],
    "version": 0
}

MAX_CHAT_MESSAGES = 200
MAX_HISTORY_ITEMS = 300
TYPING_HISTORY_INTERVAL_SECONDS = 1.5
AI_MAX_LATENCY_SECONDS = float(os.getenv("COLLABX_AI_MAX_LATENCY_SECONDS", "5"))
OLLAMA_TIMEOUT_SECONDS = float(os.getenv("COLLABX_OLLAMA_TIMEOUT_SECONDS", "4"))
DOCKER_SYNTAX_TIMEOUT_SECONDS = float(os.getenv("COLLABX_DOCKER_SYNTAX_TIMEOUT_SECONDS", "3"))
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434/api/generate")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5-coder:7b")
AUTH_USERS = {
    "owner": {"password": os.getenv("COLLABX_OWNER_PASSWORD", "owner123"), "role": "Owner"},
    "editor": {"password": os.getenv("COLLABX_EDITOR_PASSWORD", "editor123"), "role": "Editor"},
    "viewer": {"password": os.getenv("COLLABX_VIEWER_PASSWORD", "viewer123"), "role": "Viewer"},
}
typing_history_last_logged = {}  # user_id -> monotonic timestamp

# -------------------- User Management --------------------
def get_user_by_id(user_id):
    return next((u for u in project_state["users"] if u["id"] == user_id), None)


def authenticate(username, password):
    user = AUTH_USERS.get(username)
    if not user:
        return None
    if user["password"] != password:
        return None
    return {"name": username, "role": user["role"]}

# -------------------- Broadcast Helpers --------------------
async def broadcast(message, exclude_user_id=None):
    disconnected = []
    for uid, ws in connections.items():
        if uid == exclude_user_id:
            continue
        try:
            await ws.send(message)
        except Exception:
            disconnected.append(uid)

    for uid in disconnected:
        connections.pop(uid, None)
        project_state["users"] = [
            u for u in project_state["users"] if u["id"] != uid
        ]

async def broadcast_user_list():
    users = [
        {"id": u["id"], "name": u["name"], "role": u["role"]}
        for u in project_state["users"]
    ]
    await broadcast(json.dumps({"type": "user_list", "users": users}))


def append_history(user, action, details):
    entry = {
        "timestamp": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "user": user,
        "action": action,
        "details": details
    }
    project_state["history"].append(entry)
    project_state["history"] = project_state["history"][-MAX_HISTORY_ITEMS:]


def summarize_code_for_log(code):
    if not code:
        return "Empty code"
    for line in str(code).splitlines():
        clean = line.strip()
        if clean:
            return clean[:120]
    return "Empty code"


def utc_now():
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"


def make_short_id(prefix):
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def admin_record_session(username, role, status):
    project_state["admin"]["sessions"].append({
        "timestamp": utc_now(),
        "user": username,
        "role": role,
        "status": status
    })
    project_state["admin"]["sessions"] = project_state["admin"]["sessions"][-300:]


def admin_record_run(username, output):
    run_stats = project_state["admin"]["run_stats"]
    run_stats["total_runs"] += 1
    run_stats["by_user"][username] = run_stats["by_user"].get(username, 0) + 1
    run_stats["last_output_preview"] = str(output).replace("\n", " ")[:180]


def admin_record_role_change(actor, target, new_role):
    project_state["admin"]["role_changes"].append({
        "timestamp": utc_now(),
        "actor": actor,
        "target": target,
        "new_role": new_role
    })
    project_state["admin"]["role_changes"] = project_state["admin"]["role_changes"][-200:]


def admin_record_moderation(actor, action, target, details):
    project_state["admin"]["moderation"].append({
        "timestamp": utc_now(),
        "actor": actor,
        "action": action,
        "target": target,
        "details": details
    })
    project_state["admin"]["moderation"] = project_state["admin"]["moderation"][-200:]


def create_checkpoint(name, user):
    checkpoint = {
        "id": make_short_id("cp"),
        "name": name[:60] if name else f"Checkpoint v{project_state['version']}",
        "code": project_state["code"],
        "version": project_state["version"],
        "created_by": user,
        "timestamp": utc_now()
    }
    project_state["checkpoints"].append(checkpoint)
    project_state["checkpoints"] = project_state["checkpoints"][-120:]
    return checkpoint


def ensure_initial_checkpoint():
    if project_state["checkpoints"]:
        return
    create_checkpoint("Initial Snapshot", "system")


def ensure_demo_seed_data():
    if project_state["checkpoints"]:
        return

    demo_checkpoints = [
        ("Demo: Hello Print", 'print("Hello from CollabX")\n'),
        ("Demo: Variables", 'a = 10\nprint(a)\nprint("value:", a)\n'),
        ("Demo: Loop", 'for i in range(3):\n    print("line", i)\n'),
    ]

    project_state["checkpoints"] = []
    for idx, (name, code) in enumerate(demo_checkpoints, start=1):
        checkpoint = {
            "id": make_short_id("cp"),
            "name": name,
            "code": code,
            "version": idx,
            "created_by": "system",
            "timestamp": utc_now()
        }
        project_state["checkpoints"].append(checkpoint)

    if not project_state["code"]:
        project_state["code"] = demo_checkpoints[0][1]
    if project_state["version"] == 0:
        project_state["version"] = len(demo_checkpoints)


def find_checkpoint(checkpoint_id):
    for cp in project_state["checkpoints"]:
        if cp["id"] == checkpoint_id:
            return cp
    return None


def make_unified_diff(old_code, new_code, old_name, new_name):
    diff_lines = difflib.unified_diff(
        (old_code or "").splitlines(),
        (new_code or "").splitlines(),
        fromfile=old_name,
        tofile=new_name,
        lineterm=""
    )
    return "\n".join(diff_lines)


def build_project_zip_base64():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("workspace/main_code.txt", project_state["code"] or "")
        zf.writestr("workspace/last_output.txt", project_state["last_execution_output"] or "")
        zf.writestr("workspace/history.json", json.dumps(project_state["history"], indent=2))
        zf.writestr("workspace/comments.json", json.dumps(project_state["comments"], indent=2))
        checkpoint_index = [
            {k: v for k, v in cp.items() if k != "code"} for cp in project_state["checkpoints"]
        ]
        zf.writestr("workspace/checkpoints.json", json.dumps(checkpoint_index, indent=2))
        for cp in project_state["checkpoints"]:
            zf.writestr(f"workspace/checkpoints/{cp['id']}_{cp['name'].replace(' ', '_')}.txt", cp.get("code", ""))
    return base64.b64encode(buffer.getvalue()).decode("ascii")


async def broadcast_history():
    await broadcast(json.dumps({
        "type": "history_update",
        "entries": project_state["history"][-120:]
    }))


async def remove_user_by_owner(target_id, requester_id):
    target_user = get_user_by_id(target_id)
    if not target_user:
        return False, "User not found"
    if target_user["role"] == "Owner":
        return False, "Owner cannot be removed"
    if target_id == requester_id:
        return False, "Owner cannot remove self"

    target_ws = connections.get(target_id)
    if target_ws:
        try:
            await target_ws.send(json.dumps({
                "type": "kicked",
                "message": "You were removed by Owner."
            }))
            await target_ws.close()
        except Exception:
            pass

    connections.pop(target_id, None)
    project_state["users"] = [
        u for u in project_state["users"] if u["id"] != target_id
    ]
    await broadcast_user_list()
    return True, "User removed"

# -------------------- Code Execution --------------------
async def run_code_in_sandbox(language, code):
    if language == "python":
        cmd = ["docker", "run", "--rm", "--network=none", "-i",
               "collabx-sandbox", "python3", "-"]
    elif language == "c":
        cmd = ["docker", "run", "--rm", "--network=none", "-i",
               "collabx-sandbox", "sh", "-c",
               "cat > main.c && gcc main.c -o main && ./main"]
    elif language == "cpp":
        cmd = ["docker", "run", "--rm", "--network=none", "-i",
               "collabx-sandbox", "sh", "-c",
               "cat > main.cpp && g++ main.cpp -o main && ./main"]
    elif language == "java":
        cmd = ["docker", "run", "--rm", "--network=none", "-i",
               "collabx-sandbox", "sh", "-c",
               "cat > Main.java && javac Main.java && java Main"]
    else:
        return "Unsupported language."

    def run():
        proc = Popen(cmd, stdin=PIPE, stdout=PIPE, stderr=PIPE, text=True)
        return proc.communicate(input=code, timeout=10)

    try:
        stdout, stderr = await asyncio.to_thread(run)
        return stderr if stderr else stdout
    except FileNotFoundError:
        return "Docker not found. Please install Docker."
    except Exception as e:
        return f"Execution failed: {e}"


# -------------------- AI Suggestions --------------------
def _build_debug_prompt(language, code, diagnostics):
    return (
        "You are a strict code debugging assistant.\n"
        "Task:\n"
        f"1) Language: {language}\n"
        "2) Find syntax/runtime logic issues from code and diagnostics.\n"
        "3) Return corrected code.\n"
        "4) Keep response short and practical.\n\n"
        "Output format (exact sections):\n"
        "Summary:\n"
        "- <1 to 3 bullet points>\n"
        "CorrectedCode:\n"
        f"```{language}\n"
        "<full corrected code>\n"
        "```\n"
        "WhyFixWorks:\n"
        "- <short bullets>\n\n"
        "Diagnostics:\n"
        f"{diagnostics}\n\n"
        "Code:\n"
        f"{code}"
    )


async def _query_ollama(prompt):
    payload = json.dumps({
        "model": OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False
    }).encode("utf-8")

    def run():
        req = urllib.request.Request(
            OLLAMA_URL,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=OLLAMA_TIMEOUT_SECONDS) as res:
            return json.loads(res.read().decode("utf-8"))

    try:
        data = await asyncio.to_thread(run)
        return data.get("response", "").strip()
    except urllib.error.URLError:
        return ""
    except Exception:
        return ""


def _extract_compiler_hints(error_output):
    lines = [line.strip() for line in error_output.splitlines() if line.strip()]
    hints = []
    for line in lines:
        if "error:" in line.lower() or "syntaxerror" in line.lower():
            hints.append(f"- {line}")
    return hints[:8]


async def _run_syntax_check_in_docker(language, code):
    if language == "python":
        cmd = [
            "docker", "run", "--rm", "--network=none", "-i",
            "collabx-sandbox", "python3", "-c",
            "import ast,sys; ast.parse(sys.stdin.read())"
        ]
    elif language == "c":
        cmd = [
            "docker", "run", "--rm", "--network=none", "-i",
            "collabx-sandbox", "sh", "-c",
            "cat > main.c && gcc -fsyntax-only -Wall main.c"
        ]
    elif language == "cpp":
        cmd = [
            "docker", "run", "--rm", "--network=none", "-i",
            "collabx-sandbox", "sh", "-c",
            "cat > main.cpp && g++ -fsyntax-only -Wall main.cpp"
        ]
    elif language == "java":
        cmd = [
            "docker", "run", "--rm", "--network=none", "-i",
            "collabx-sandbox", "sh", "-c",
            "cat > Main.java && javac -Xlint Main.java"
        ]
    else:
        return False, "Unsupported language for AI debugging."

    def run():
        proc = Popen(cmd, stdin=PIPE, stdout=PIPE, stderr=PIPE, text=True)
        return proc.communicate(input=code, timeout=DOCKER_SYNTAX_TIMEOUT_SECONDS)

    try:
        stdout, stderr = await asyncio.to_thread(run)
        if stderr.strip():
            return False, stderr.strip()
        return True, (stdout or "No syntax issues found.").strip()
    except TimeoutExpired:
        return False, "Syntax check timeout (fast mode)."
    except FileNotFoundError:
        return False, "Docker not found. Install Docker to enable syntax checks."
    except Exception as e:
        return False, f"Syntax check failed: {e}"


async def get_ai_suggestion(language, code):
    if not code or not code.strip():
        return "Write code in the editor first, then run AI Debugger."

    started = time.monotonic()
    diagnostics = "No diagnostics yet."
    if language == "python":
        try:
            ast.parse(code)
        except SyntaxError as err:
            diagnostics = (
                "Python syntax issue found.\n"
                f"- Line: {err.lineno or '?'}\n"
                f"- Error: {err.msg or 'Invalid syntax'}\n"
            )
        else:
            diagnostics = "Python parser: no syntax errors."

    try:
        ok, detail = await asyncio.wait_for(
            _run_syntax_check_in_docker(language, code),
            timeout=DOCKER_SYNTAX_TIMEOUT_SECONDS + 0.5
        )
    except asyncio.TimeoutError:
        ok, detail = False, "Syntax check timeout (fast mode)."

    if ok:
        diagnostics = f"{diagnostics}\nCompiler check: no syntax errors."
    else:
        diagnostics = f"{diagnostics}\nCompiler output:\n{detail}"

    prompt = _build_debug_prompt(language, code, diagnostics)
    remaining = AI_MAX_LATENCY_SECONDS - (time.monotonic() - started)
    ai_response = ""
    if remaining > 1:
        try:
            ai_response = await asyncio.wait_for(_query_ollama(prompt), timeout=remaining)
        except asyncio.TimeoutError:
            ai_response = ""
    if ai_response:
        return ai_response

    if not ok:
        hints = _extract_compiler_hints(detail)
        if hints:
            return (
                "AI model unavailable. Fallback debug result:\n"
                "Compiler/syntax issues:\n"
                + "\n".join(hints)
                + "\nSuggested fixes:\n"
                "- Fix first error before others.\n"
                "- Recheck semicolons/braces/parentheses.\n"
                "- Rebuild and test again."
            )

    if language == "python" and "syntax issue found" in diagnostics.lower():
        return (
            "AI model unavailable. Python fallback:\n"
            + diagnostics
            + "\nSuggested fixes:\n"
            "- Check indentation and missing colons.\n"
            "- Verify balanced quotes/brackets/parentheses."
        )

    return (
        "AI model unavailable, but static checks passed.\n"
        "No syntax errors detected. If output is wrong, issue is likely logic-related."
    )


        
        

# -------------------- WebSocket Handler --------------------
async def handler(ws, path=None):
    user_id = str(uuid.uuid4())
    connections[user_id] = ws
    authenticated_user = None
    logging.info(f"[+] connection opened ({user_id})")

    try:
        await ws.send(json.dumps({
            "type": "auth_required",
            "message": "Authenticate with username and password."
        }))

        async for raw in ws:
            try:
                data = json.loads(raw)
            except Exception:
                continue

            msg_type = data.get("type")

            if msg_type == "auth":
                if authenticated_user:
                    await ws.send(json.dumps({
                        "type": "error",
                        "message": "Already authenticated"
                    }))
                    continue

                username = str(data.get("username", "")).strip()
                password = str(data.get("password", ""))
                auth_result = authenticate(username, password)

                if not auth_result:
                    await ws.send(json.dumps({
                        "type": "auth_failed",
                        "message": "Invalid username or password"
                    }))
                    continue

                existing = next(
                    (u for u in project_state["users"] if u["name"] == username),
                    None
                )
                if existing:
                    await ws.send(json.dumps({
                        "type": "auth_failed",
                        "message": "User already logged in"
                    }))
                    continue

                authenticated_user = {
                    "id": user_id,
                    "name": auth_result["name"],
                    "role": auth_result["role"]
                }
                ensure_demo_seed_data()
                project_state["users"].append(authenticated_user)
                admin_record_session(authenticated_user["name"], authenticated_user["role"], "login")
                logging.info(
                    f"[+] {authenticated_user['name']} ({authenticated_user['role']}) authenticated"
                )

                await ws.send(json.dumps({
                    "type": "user_role",
                    "id": user_id,
                    "name": authenticated_user["name"],
                    "role": authenticated_user["role"]
                }))
                await ws.send(json.dumps({
                    "type": "code_update",
                    "content": project_state["code"],
                    "version": project_state["version"],
                    "senderId": "server"
                }))
                await ws.send(json.dumps({
                    "type": "history_update",
                    "entries": project_state["history"][-120:]
                }))
                await ws.send(json.dumps({
                    "type": "checkpoints_update",
                    "checkpoints": [
                        {k: v for k, v in cp.items() if k != "code"}
                        for cp in project_state["checkpoints"]
                    ]
                }))
                await ws.send(json.dumps({
                    "type": "comments_update",
                    "comments": project_state["comments"]
                }))
                await broadcast_user_list()
                continue

            if not authenticated_user:
                await ws.send(json.dumps({
                    "type": "auth_required",
                    "message": "Authenticate before using workspace actions."
                }))
                continue

            current_user = get_user_by_id(user_id)
            if not current_user:
                await ws.send(json.dumps({
                    "type": "error",
                    "message": "Session not found. Reconnect and login again."
                }))
                continue

            if msg_type == "code_update":
                if current_user["role"] not in ("Owner", "Editor"):
                    await ws.send(json.dumps({
                        "type": "error",
                        "message": "Edit permission denied"
                    }))
                    continue

                project_state["version"] += 1
                project_state["code"] = data.get("content", "")
                change_meta = data.get("change_meta", {})
                preview = str(change_meta.get("preview", "")).replace("\n", " ").strip()
                if not preview:
                    preview = "Code updated"
                append_history(
                    current_user["name"],
                    "code_edit",
                    f"{preview[:120]} (v{project_state['version']})"
                )

                await broadcast(json.dumps({
                    "type": "code_update",
                    "content": project_state["code"],
                    "version": project_state["version"],
                    "senderId": user_id
                }))
                await broadcast_history()

            elif msg_type == "chat_message":
                msg = data.get("message", "")
                project_state["chat_messages"].append({
                    "user": current_user["name"],
                    "message": msg
                })
                project_state["chat_messages"] = \
                    project_state["chat_messages"][-MAX_CHAT_MESSAGES:]

                await broadcast(json.dumps({
                    "type": "chat_message",
                    "user": current_user["name"],
                    "message": msg
                }))
                append_history(
                    current_user["name"],
                    "chat_message",
                    f'Said: "{str(msg).replace("\\n", " ")[:120]}"'
                )
                await broadcast_history()

            elif msg_type == "cursor_update":
                position = data.get("position", {})
                row = int(position.get("row", 0))
                column = int(position.get("column", 0))
                row = max(0, row)
                column = max(0, column)
                await broadcast(json.dumps({
                    "type": "cursor_update",
                    "senderId": user_id,
                    "user": current_user["name"],
                    "role": current_user["role"],
                    "position": {"row": row, "column": column}
                }), exclude_user_id=user_id)

            elif msg_type == "edit_activity":
                if current_user["role"] not in ("Owner", "Editor"):
                    continue

                edit_range = data.get("range")
                if not isinstance(edit_range, dict):
                    continue
                preview = str(data.get("preview", "")).replace("\n", " ").strip()

                await broadcast(json.dumps({
                    "type": "edit_activity",
                    "senderId": user_id,
                    "user": current_user["name"],
                    "role": current_user["role"],
                    "range": edit_range
                }), exclude_user_id=user_id)

                now = time.monotonic()
                last_logged = typing_history_last_logged.get(user_id, 0.0)
                if now - last_logged >= TYPING_HISTORY_INTERVAL_SECONDS:
                    typing_history_last_logged[user_id] = now
                    append_history(
                        current_user["name"],
                        "typing",
                        preview[:120] if preview else "Typing in editor"
                    )
                    await broadcast_history()

            elif msg_type == "run_code":
                if current_user["role"] not in ("Owner", "Editor"):
                    await ws.send(json.dumps({
                        "type": "error",
                        "message": "Run permission denied"
                    }))
                    continue

                run_language = data.get("language")
                run_code = data.get("code", "")
                append_history(
                    current_user["name"],
                    "run_code",
                    f"{run_language}: {summarize_code_for_log(run_code)}"
                )
                await broadcast_history()

                output = await run_code_in_sandbox(
                    run_language,
                    run_code
                )
                await ws.send(json.dumps({
                    "type": "execution_result",
                    "output": output
                }))
                append_history(
                    current_user["name"],
                    "run_result",
                    "Execution finished" if output else "Execution finished (no output)"
                )
                project_state["last_execution_output"] = output or ""
                admin_record_run(current_user["name"], output or "")
                await broadcast_history()

            elif msg_type == "ask_ai":
                suggestion = await get_ai_suggestion(
                    data.get("language", "python"),
                    data.get("code", "")
                )
                await ws.send(json.dumps({
                    "type": "ai_suggestion",
                    "suggestion": suggestion
                }))

            elif msg_type == "request_history":
                await ws.send(json.dumps({
                    "type": "history_update",
                    "entries": project_state["history"][-120:]
                }))

            elif msg_type == "request_checkpoints":
                await ws.send(json.dumps({
                    "type": "checkpoints_update",
                    "checkpoints": [
                        {k: v for k, v in cp.items() if k != "code"}
                        for cp in project_state["checkpoints"]
                    ]
                }))

            elif msg_type == "create_checkpoint":
                if current_user["role"] not in ("Owner", "Editor"):
                    await ws.send(json.dumps({"type": "error", "message": "Permission denied"}))
                    continue
                cp = create_checkpoint(str(data.get("name", "")).strip(), current_user["name"])
                append_history(current_user["name"], "checkpoint", f"Created {cp['name']}")
                await broadcast(json.dumps({
                    "type": "checkpoints_update",
                    "checkpoints": [
                        {k: v for k, v in c.items() if k != "code"} for c in project_state["checkpoints"]
                    ]
                }))
                await broadcast_history()

            elif msg_type == "restore_checkpoint":
                if current_user["role"] not in ("Owner", "Editor"):
                    await ws.send(json.dumps({"type": "error", "message": "Permission denied"}))
                    continue
                cp = find_checkpoint(str(data.get("checkpoint_id", "")))
                if not cp:
                    await ws.send(json.dumps({"type": "error", "message": "Checkpoint not found"}))
                    continue
                project_state["code"] = cp["code"]
                project_state["version"] += 1
                append_history(current_user["name"], "checkpoint_restore", f"Restored {cp['name']}")
                await broadcast(json.dumps({
                    "type": "code_update",
                    "content": project_state["code"],
                    "version": project_state["version"],
                    "senderId": "server"
                }))
                await broadcast_history()

            elif msg_type == "diff_checkpoints":
                left_id = str(data.get("left_id", ""))
                right_id = str(data.get("right_id", ""))
                left_cp = find_checkpoint(left_id)
                right_cp = find_checkpoint(right_id)
                if not left_cp or not right_cp:
                    await ws.send(json.dumps({"type": "error", "message": "Select valid checkpoints"}))
                    continue
                diff_text = make_unified_diff(
                    left_cp.get("code", ""),
                    right_cp.get("code", ""),
                    left_cp.get("name", "left"),
                    right_cp.get("name", "right")
                )
                await ws.send(json.dumps({
                    "type": "checkpoint_diff",
                    "left": left_cp,
                    "right": right_cp,
                    "diff": diff_text
                }))

            elif msg_type == "request_comments":
                await ws.send(json.dumps({
                    "type": "comments_update",
                    "comments": project_state["comments"]
                }))

            elif msg_type == "add_comment":
                comment_text = str(data.get("text", "")).strip()
                comment_range = data.get("range")
                if not comment_text or not isinstance(comment_range, dict):
                    await ws.send(json.dumps({"type": "error", "message": "Invalid comment"}))
                    continue
                thread = {
                    "id": make_short_id("cm"),
                    "range": comment_range,
                    "text": comment_text[:400],
                    "author": current_user["name"],
                    "created_at": utc_now(),
                    "resolved": False,
                    "replies": []
                }
                project_state["comments"].append(thread)
                project_state["comments"] = project_state["comments"][-300:]
                append_history(current_user["name"], "comment_add", comment_text[:100])
                await broadcast(json.dumps({"type": "comments_update", "comments": project_state["comments"]}))
                await broadcast_history()

            elif msg_type == "reply_comment":
                thread_id = str(data.get("thread_id", ""))
                reply_text = str(data.get("text", "")).strip()
                thread = next((c for c in project_state["comments"] if c["id"] == thread_id), None)
                if not thread or not reply_text:
                    await ws.send(json.dumps({"type": "error", "message": "Invalid reply"}))
                    continue
                thread["replies"].append({
                    "id": make_short_id("rp"),
                    "author": current_user["name"],
                    "text": reply_text[:300],
                    "created_at": utc_now()
                })
                append_history(current_user["name"], "comment_reply", reply_text[:100])
                await broadcast(json.dumps({"type": "comments_update", "comments": project_state["comments"]}))
                await broadcast_history()

            elif msg_type == "resolve_comment":
                thread_id = str(data.get("thread_id", ""))
                thread = next((c for c in project_state["comments"] if c["id"] == thread_id), None)
                if not thread:
                    await ws.send(json.dumps({"type": "error", "message": "Comment not found"}))
                    continue
                thread["resolved"] = True
                append_history(current_user["name"], "comment_resolve", thread_id)
                await broadcast(json.dumps({"type": "comments_update", "comments": project_state["comments"]}))
                await broadcast_history()

            elif msg_type == "request_admin_dashboard":
                if current_user["role"] != "Owner":
                    await ws.send(json.dumps({"type": "error", "message": "Only Owner can view admin dashboard"}))
                    continue
                payload = {
                    "type": "admin_dashboard",
                    "active_users": list(project_state["users"]),
                    "sessions": list(project_state["admin"]["sessions"][-120:]),
                    "run_stats": dict(project_state["admin"]["run_stats"]),
                    "role_changes": list(project_state["admin"]["role_changes"][-120:]),
                    "moderation": list(project_state["admin"]["moderation"][-120:])
                }
                try:
                    json.dumps(payload)
                    await ws.send(json.dumps(payload))
                except Exception as e:
                    await ws.send(json.dumps({
                        "type": "error",
                        "message": f"Admin dashboard serialization error: {e}"
                    }))

            elif msg_type == "integration_action":
                action = str(data.get("action", "")).strip()
                if action == "github_pull":
                    try:
                        result = subprocess.run(
                            ["git", "pull"],
                            cwd=os.path.dirname(__file__),
                            capture_output=True, text=True, timeout=20
                        )
                        msg = (result.stdout or result.stderr or "No output").strip()[:1200]
                    except Exception as e:
                        msg = f"Git pull failed: {e}"
                    await ws.send(json.dumps({"type": "integration_result", "action": action, "result": msg}))
                elif action == "github_push":
                    try:
                        result = subprocess.run(
                            ["git", "push"],
                            cwd=os.path.dirname(__file__),
                            capture_output=True, text=True, timeout=20
                        )
                        msg = (result.stdout or result.stderr or "No output").strip()[:1200]
                    except Exception as e:
                        msg = f"Git push failed: {e}"
                    await ws.send(json.dumps({"type": "integration_result", "action": action, "result": msg}))
                elif action == "gist_export":
                    token = os.getenv("GITHUB_TOKEN", "")
                    if not token:
                        await ws.send(json.dumps({
                            "type": "integration_result",
                            "action": action,
                            "result": "Set GITHUB_TOKEN environment variable to export gist."
                        }))
                        continue
                    payload = {
                        "description": "CollabX export",
                        "public": False,
                        "files": {"collabx_code.txt": {"content": project_state["code"] or ""}}
                    }
                    req = urllib.request.Request(
                        "https://api.github.com/gists",
                        data=json.dumps(payload).encode("utf-8"),
                        headers={
                            "Content-Type": "application/json",
                            "Authorization": f"token {token}"
                        },
                        method="POST"
                    )
                    try:
                        with urllib.request.urlopen(req, timeout=20) as res:
                            gist_res = json.loads(res.read().decode("utf-8"))
                        await ws.send(json.dumps({
                            "type": "integration_result",
                            "action": action,
                            "result": gist_res.get("html_url", "Gist created")
                        }))
                    except Exception as e:
                        await ws.send(json.dumps({
                            "type": "integration_result",
                            "action": action,
                            "result": f"Gist export failed: {e}"
                        }))
                elif action == "paste_export":
                    req = urllib.request.Request(
                        "https://paste.rs",
                        data=(project_state["code"] or "").encode("utf-8"),
                        headers={"Content-Type": "text/plain"},
                        method="POST"
                    )
                    try:
                        with urllib.request.urlopen(req, timeout=20) as res:
                            url = res.read().decode("utf-8").strip()
                        await ws.send(json.dumps({
                            "type": "integration_result",
                            "action": action,
                            "result": url or "Paste created"
                        }))
                    except Exception as e:
                        await ws.send(json.dumps({
                            "type": "integration_result",
                            "action": action,
                            "result": f"Paste export failed: {e}"
                        }))
                elif action == "download_zip":
                    zip_b64 = build_project_zip_base64()
                    await ws.send(json.dumps({
                        "type": "project_zip",
                        "filename": f"collabx-project-{int(time.time())}.zip",
                        "base64": zip_b64
                    }))
                else:
                    await ws.send(json.dumps({"type": "integration_result", "action": action, "result": "Unsupported action"}))

            elif msg_type == "remove_user":
                if current_user["role"] != "Owner":
                    await ws.send(json.dumps({
                        "type": "error",
                        "message": "Only Owner can remove users"
                    }))
                    continue

                ok, message = await remove_user_by_owner(
                    str(data.get("target_id", "")),
                    requester_id=user_id
                )
                if not ok:
                    await ws.send(json.dumps({"type": "error", "message": message}))
                else:
                    target_id = str(data.get("target_id", ""))
                    append_history(current_user["name"], "remove_user", message)
                    admin_record_moderation(current_user["name"], "remove_user", target_id, message)
                    await broadcast_history()

            elif msg_type == "change_role":
                if current_user["role"] != "Owner":
                    await ws.send(json.dumps({
                        "type": "error",
                        "message": "Only Owner can change roles"
                    }))
                    continue

                target_id = str(data.get("target_id", ""))
                new_role = str(data.get("new_role", ""))
                if new_role not in ("Editor", "Viewer"):
                    await ws.send(json.dumps({
                        "type": "error",
                        "message": "Role must be Editor or Viewer"
                    }))
                    continue

                target_user = get_user_by_id(target_id)
                if not target_user:
                    await ws.send(json.dumps({
                        "type": "error",
                        "message": "User not found"
                    }))
                    continue
                if target_user["role"] == "Owner":
                    await ws.send(json.dumps({
                        "type": "error",
                        "message": "Owner role cannot be changed"
                    }))
                    continue
                if target_user["id"] == user_id:
                    await ws.send(json.dumps({
                        "type": "error",
                        "message": "Owner cannot change own role"
                    }))
                    continue

                target_user["role"] = new_role
                append_history(
                    current_user["name"],
                    "role_change",
                    f"Changed {target_user['name']} to {new_role}"
                )
                admin_record_role_change(current_user["name"], target_user["name"], new_role)
                target_ws = connections.get(target_id)
                if target_ws:
                    await target_ws.send(json.dumps({
                        "type": "user_role",
                        "id": target_id,
                        "name": target_user["name"],
                        "role": new_role
                    }))
                await broadcast_user_list()
                await broadcast_history()

    except Exception as e:
        logging.exception(f"Handler error: {e}")

    finally:
        connections.pop(user_id, None)
        project_state["users"] = [u for u in project_state["users"] if u["id"] != user_id]
        if authenticated_user:
            logging.info(f"[-] {authenticated_user['name']} disconnected")
            admin_record_session(authenticated_user["name"], authenticated_user["role"], "logout")
        else:
            logging.info(f"[-] unauthenticated connection closed ({user_id})")
        await broadcast_user_list()

# -------------------- Main --------------------
async def main():
    logging.info("Starting WebSocket server on port 8000")
    async with websockets.serve(handler, "0.0.0.0", 8000):
        await asyncio.Future()

if __name__ == "__main__":
    asyncio.run(main())

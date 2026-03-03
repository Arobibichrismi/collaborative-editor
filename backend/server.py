#!/usr/bin/env python3
import asyncio
import ast
import json
import logging
import os
import time
from datetime import datetime
import urllib.error
import urllib.request
import uuid
from subprocess import Popen, PIPE
import websockets  # ✅ FIX




logging.basicConfig(level=logging.INFO)

# -------------------- Global State --------------------
connections = {}  # user_id -> websocket

project_state = {
    "code": "",
    "chat_messages": [],
    "history": [],
    "users": [],
    "version": 0
}

MAX_CHAT_MESSAGES = 200
MAX_HISTORY_ITEMS = 300
TYPING_HISTORY_INTERVAL_SECONDS = 1.5
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
        with urllib.request.urlopen(req, timeout=45) as res:
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
        return proc.communicate(input=code, timeout=10)

    try:
        stdout, stderr = await asyncio.to_thread(run)
        if stderr.strip():
            return False, stderr.strip()
        return True, (stdout or "No syntax issues found.").strip()
    except FileNotFoundError:
        return False, "Docker not found. Install Docker to enable syntax checks."
    except Exception as e:
        return False, f"Syntax check failed: {e}"


async def get_ai_suggestion(language, code):
    if not code or not code.strip():
        return "Write code in the editor first, then run AI Debugger."

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

    ok, detail = await _run_syntax_check_in_docker(language, code)
    if ok:
        diagnostics = f"{diagnostics}\nCompiler check: no syntax errors."
    else:
        diagnostics = f"{diagnostics}\nCompiler output:\n{detail}"

    prompt = _build_debug_prompt(language, code, diagnostics)
    ai_response = await _query_ollama(prompt)
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
                project_state["users"].append(authenticated_user)
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
                    append_history(current_user["name"], "remove_user", message)
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

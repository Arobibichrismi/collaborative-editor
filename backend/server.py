#!/usr/bin/env python3
import asyncio
import json
import logging
import random
import uuid
from subprocess import Popen, PIPE
import websockets  # ✅ FIX
import ollama



logging.basicConfig(level=logging.INFO)

# -------------------- Global State --------------------
connections = {}  # user_id -> websocket

project_state = {
    "code": "",
    "chat_messages": [],
    "users": [],
    "version": 0
}

MAX_CHAT_MESSAGES = 200

# -------------------- User Management --------------------
def make_user(user_id):
    username = f"User {random.randint(100, 999)}"
    if not project_state["users"]:
        role = "Owner"
    elif len(project_state["users"]) == 1:
        role = "Editor"
    else:
        role = "Viewer"
    return {"id": user_id, "name": username, "role": role}

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

# -------------------- AI Placeholder --------------------
async def get_ai_suggestion(code):
    if not code.strip():
        return "Editor is empty. Write some code first!"

    try:
        # We wrap this in to_thread because the library is synchronous
        # and we don't want to freeze your collaborative editor
        response = await asyncio.to_thread(
            ollama.generate,
            model='qwen2.5',
            prompt = f"""
                You are a strict code review assistant.

                Analyze ONLY the code provided below.
                Do NOT repeat the input.
                Do NOT continue templates or comments.
                Do NOT include explanations outside the requested JSON.

                Respond ONLY in valid JSON that exactly matches this schema:

                {{
                "language": string,
                "issues": [
                    {{
                    "problem": string,
                    "improvement": string
                    }}
                ]
                }}

                Code:
                {code}
                """

        )
        
        # 'response' is a dictionary; the text is in the 'response' key
        return response['response']
        
    except Exception as e:
        return f"Local AI Error: {str(e)}"

# -------------------- WebSocket Handler --------------------
async def handler(ws, path=None):
    user_id = str(uuid.uuid4())
    user = make_user(user_id)

    connections[user_id] = ws
    project_state["users"].append(user)

    logging.info(f"[+] {user['name']} ({user['role']}) connected")

    try:
        await ws.send(json.dumps({
            "type": "user_role",
            "id": user_id,
            "name": user["name"],
            "role": user["role"]
        }))

        await ws.send(json.dumps({
            "type": "code_update",
            "content": project_state["code"],
            "version": project_state["version"],
            "senderId": "server"
        }))

        await broadcast_user_list()

        async for raw in ws:
            try:
                data = json.loads(raw)
            except Exception:
                continue

            current_user = next(
                (u for u in project_state["users"] if u["id"] == user_id),
                None
            )
            if not current_user:
                continue

            msg_type = data.get("type")

            if msg_type == "code_update":
                if current_user["role"] not in ("Owner", "Editor"):
                    await ws.send(json.dumps({
                        "type": "error",
                        "message": "Edit permission denied"
                    }))
                    continue

                if data.get("version") != project_state["version"]:
                    await ws.send(json.dumps({
                        "type": "error",
                        "message": "Version conflict"
                    }))
                    continue

                project_state["version"] += 1
                project_state["code"] = data.get("content", "")

                await broadcast(json.dumps({
                    "type": "code_update",
                    "content": project_state["code"],
                    "version": project_state["version"],
                    "senderId": user_id
                }), exclude_user_id=user_id)

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

            elif msg_type == "run_code":
                if current_user["role"] not in ("Owner", "Editor"):
                    await ws.send(json.dumps({
                        "type": "error",
                        "message": "Run permission denied"
                    }))
                    continue

                output = await run_code_in_sandbox(
                    data.get("language"),
                    data.get("code", "")
                )
                await ws.send(json.dumps({
                    "type": "execution_result",
                    "output": output
                }))

            elif msg_type == "ask_ai":
                suggestion = await get_ai_suggestion(data.get("code", ""))
                await ws.send(json.dumps({
                    "type": "ai_suggestion",
                    "suggestion": suggestion
                }))

    except Exception as e:
        logging.exception(f"Handler error: {e}")

    finally:
        connections.pop(user_id, None)
        project_state["users"] = [
            u for u in project_state["users"] if u["id"] != user_id
        ]
        logging.info(f"[-] {user['name']} disconnected")
        await broadcast_user_list()

# -------------------- Main --------------------
async def main():
    logging.info("Starting WebSocket server on port 8000")
    async with websockets.serve(handler, "0.0.0.0", 8000):
        await asyncio.Future()

if __name__ == "__main__":
    asyncio.run(main())

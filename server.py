# server.py
"""
Simple Chat Server (JSON-over-TCP)
- sqlite DB: users, friends, messages
- supports register/login, add_friend, send msg, get_friends, get_history
- stores undelivered messages and delivers them when user connects
"""

import socket
import threading
import json
import sqlite3
import time
from datetime import datetime

HOST = "0.0.0.0"
PORT = 9999

DB_FILE = "server_chat.db"
lock = threading.Lock()  # protect sqlite access and clients dict

# map username -> wfile (to send JSON lines)
clients = {}

# ---------- Database helpers ----------
def init_db():
    with sqlite3.connect(DB_FILE) as conn:
        c = conn.cursor()
        # users: username unique, password plain (for demo only)
        c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            username TEXT PRIMARY KEY,
            password TEXT
        )""")
        # friendships: user -> friend (simple directed or undirected; we'll treat as mutual when adding)
        c.execute("""
        CREATE TABLE IF NOT EXISTS friends (
            user TEXT,
            friend TEXT,
            UNIQUE(user, friend)
        )""")
        # messages: stored persistently. delivered = 0/1
        c.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sender TEXT,
            receiver TEXT,
            text TEXT,
            ts REAL,
            delivered INTEGER DEFAULT 0
        )""")
        conn.commit()

def db_register(username, password):
    with sqlite3.connect(DB_FILE) as conn:
        c = conn.cursor()
        try:
            c.execute("INSERT INTO users(username,password) VALUES(?,?)", (username, password))
            conn.commit()
            return True, "ok"
        except sqlite3.IntegrityError:
            return False, "username_taken"

def db_check_login(username, password):
    with sqlite3.connect(DB_FILE) as conn:
        c = conn.cursor()
        c.execute("SELECT password FROM users WHERE username=?", (username,))
        row = c.fetchone()
        if not row:
            return False, "no_such_user"
        if row[0] != password:
            return False, "bad_password"
        return True, "ok"

def db_add_friend(user, friend):
    with sqlite3.connect(DB_FILE) as conn:
        c = conn.cursor()
        # ensure friend exists
        c.execute("SELECT username FROM users WHERE username=?", (friend,))
        if not c.fetchone():
            return False, "no_such_user"
        try:
            c.execute("INSERT OR IGNORE INTO friends(user, friend) VALUES(?,?)", (user, friend))
            c.execute("INSERT OR IGNORE INTO friends(user, friend) VALUES(?,?)", (friend, user))
            conn.commit()
            return True, "ok"
        except Exception as e:
            return False, "db_error"

def db_get_friends(user):
    with sqlite3.connect(DB_FILE) as conn:
        c = conn.cursor()
        c.execute("SELECT friend FROM friends WHERE user=?", (user,))
        return [row[0] for row in c.fetchall()]

def db_store_message(sender, receiver, text, delivered):
    ts = time.time()
    with sqlite3.connect(DB_FILE) as conn:
        c = conn.cursor()
        c.execute("INSERT INTO messages(sender,receiver,text,ts,delivered) VALUES(?,?,?,?,?)",
                  (sender, receiver, text, ts, 1 if delivered else 0))
        conn.commit()

def db_get_history(user, other, limit=200):
    with sqlite3.connect(DB_FILE) as conn:
        c = conn.cursor()
        c.execute("""SELECT sender, receiver, text, ts FROM messages
                     WHERE (sender=? AND receiver=?) OR (sender=? AND receiver=?)
                     ORDER BY ts ASC LIMIT ?""", (user, other, other, user, limit))
        return c.fetchall()

def db_get_undelivered(username):
    with sqlite3.connect(DB_FILE) as conn:
        c = conn.cursor()
        c.execute("SELECT id, sender, text, ts FROM messages WHERE receiver=? AND delivered=0 ORDER BY ts ASC", (username,))
        rows = c.fetchall()
        # mark as delivered
        c.execute("UPDATE messages SET delivered=1 WHERE receiver=? AND delivered=0", (username,))
        conn.commit()
        return rows

# ---------- Socket/Protocol helpers ----------
def send_json_w(wfile, obj):
    try:
        wfile.write(json.dumps(obj, ensure_ascii=False) + "\n")
        wfile.flush()
    except Exception:
        pass

def handle_client(conn, addr):
    rfile = conn.makefile("r", encoding="utf-8")
    wfile = conn.makefile("w", encoding="utf-8")
    username = None
    try:
        # first message must be auth
        line = rfile.readline()
        if not line:
            return
        try:
            msg = json.loads(line.strip())
        except Exception:
            send_json_w(wfile, {"type":"error","error":"invalid_json"})
            return

        if msg.get("type") != "auth" or msg.get("op") not in ("register","login"):
            send_json_w(wfile, {"type":"error","error":"first_auth_required"})
            return

        op = msg["op"]
        uname = msg.get("username")
        pwd = msg.get("password","")
        if not uname:
            send_json_w(wfile, {"type":"auth","status":"error","error":"no_username"})
            return

        if op == "register":
            ok, reason = db_register(uname, pwd)
            if not ok:
                send_json_w(wfile, {"type":"auth","status":"error","error":reason})
                return
            else:
                send_json_w(wfile, {"type":"auth","status":"ok"})
        else:
            ok, reason = db_check_login(uname, pwd)
            if not ok:
                send_json_w(wfile, {"type":"auth","status":"error","error":reason})
                return
            else:
                send_json_w(wfile, {"type":"auth","status":"ok"})

        username = uname
        # register this connection
        with lock:
            clients[username] = wfile
        print(f"{username} connected from {addr}")

        # send friend list
        friends = db_get_friends(username)
        send_json_w(wfile, {"type":"friends_list","friends":friends})

        # deliver undelivered messages
        undelivered = db_get_undelivered(username)
        for mid, sender, text, ts in undelivered:
            send_json_w(wfile, {"type":"msg","from":sender,"to":username,"text":text,"ts":ts})

        # main loop
        while True:
            line = rfile.readline()
            if not line:
                break
            try:
                msg = json.loads(line.strip())
            except Exception:
                send_json_w(wfile, {"type":"error","error":"invalid_json"})
                continue

            t = msg.get("type")
            if t == "add_friend":
                friend = msg.get("friend")
                ok, reason = db_add_friend(username, friend)
                if ok:
                    # return updated list
                    friends = db_get_friends(username)
                    send_json_w(wfile, {"type":"friends_list","friends":friends})
                else:
                    send_json_w(wfile, {"type":"error","error":reason})
            elif t == "get_history":
                other = msg.get("other")
                hist = db_get_history(username, other)
                # convert to JSON-friendly
                hist2 = [{"from":h[0],"to":h[1],"text":h[2],"ts":h[3]} for h in hist]
                send_json_w(wfile, {"type":"history","other":other,"messages":hist2})
            elif t == "msg":
                to = msg.get("to")
                text = msg.get("text","")
                # if recipient online, send immediately and mark delivered
                with lock:
                    target_w = clients.get(to)
                if target_w:
                    send_json_w(target_w, {"type":"msg","from":username,"to":to,"text":text,"ts":time.time()})
                    db_store_message(username, to, text, delivered=True)
                else:
                    # store undelivered
                    db_store_message(username, to, text, delivered=False)
                    # notify sender that message queued (optional)
                    send_json_w(wfile, {"type":"info","info":"message_queued"})
            else:
                send_json_w(wfile, {"type":"error","error":"unknown_type"})

    except Exception as e:
        print("Client handler error:", e)
    finally:
        if username:
            with lock:
                clients.pop(username, None)
        try:
            conn.close()
        except:
            pass
        print(f"{username} disconnected")

def main():
    init_db()
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind((HOST, PORT))
    s.listen(128)
    print(f"Server listening on {HOST}:{PORT}")
    try:
        while True:
            conn, addr = s.accept()
            t = threading.Thread(target=handle_client, args=(conn, addr), daemon=True)
            t.start()
    except KeyboardInterrupt:
        print("Shutting down")
    finally:
        s.close()

if __name__ == "__main__":
    main()

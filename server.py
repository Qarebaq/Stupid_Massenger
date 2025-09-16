# server.py
"""
Simple Chat Server (JSON-over-TCP) - Enhanced Version with Bot
- sqlite DB: users, friends, messages, groups
- supports register/login, add_friend, send msg, get_friends, get_history
- supports create_group, join_group, leave_group, group_message
- stores undelivered messages and delivers them when user connects
- improved error handling and thread safety
- Added helper bot with @echo and @all commands
"""

import socket
import threading
import json
import sqlite3
import time
import hashlib
import bcrypt
from datetime import datetime

HOST = "0.0.0.0"
PORT = 9999

DB_FILE = "server_chat.db"
lock = threading.Lock()  # protect sqlite access and clients dict

# map username -> wfile (to send JSON lines)
clients = {}

# Bot configuration
BOT_USERNAME = "ChatBot"

# ---------- Database helpers ----------
def init_db():
    with sqlite3.connect(DB_FILE, check_same_thread=False) as conn:
        c = conn.cursor()
        # users: username unique, password hashed
        c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            username TEXT PRIMARY KEY,
            password TEXT,
            is_bot INTEGER DEFAULT 0
        )""")
        # friendships: user -> friend
        c.execute("""
        CREATE TABLE IF NOT EXISTS friends (
            user TEXT,
            friend TEXT,
            UNIQUE(user, friend)
        )""")
        # messages: stored persistently. delivered = 0/1, group_id for group messages
        c.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sender TEXT,
            receiver TEXT,
            group_id TEXT,
            text TEXT,
            ts REAL,
            delivered INTEGER DEFAULT 0
        )""")
        # groups table
        c.execute("""
        CREATE TABLE IF NOT EXISTS groups (
            id TEXT PRIMARY KEY,
            name TEXT,
            creator TEXT,
            created_at REAL
        )""")
        # group members
        c.execute("""
        CREATE TABLE IF NOT EXISTS group_members (
            group_id TEXT,
            username TEXT,
            joined_at REAL,
            UNIQUE(group_id, username)
        )""")
        conn.commit()
        
        # Create bot user if it doesn't exist
        create_bot_user()

def hash_password(password):
    """Hash password using bcrypt for better security"""
    salt = bcrypt.gensalt()
    return bcrypt.hashpw(password.encode('utf-8'), salt).decode('utf-8')

def verify_password(password, hashed):
    """Verify password against bcrypt hash"""
    try:
        return bcrypt.checkpw(password.encode('utf-8'), hashed.encode('utf-8'))
    except:
        # Fallback for old SHA256 hashes (backward compatibility)
        return hashed == hashlib.sha256(password.encode()).hexdigest()

def create_bot_user():
    """Create the bot user in the database"""
    try:
        with sqlite3.connect(DB_FILE, timeout=5.0) as conn:
            c = conn.cursor()
            # Check if bot already exists
            c.execute("SELECT username FROM users WHERE username=?", (BOT_USERNAME,))
            if not c.fetchone():
                # Create bot user with a random password (won't be used for login)
                bot_password = hash_password("bot_internal_password_" + str(time.time()))
                c.execute("INSERT INTO users(username, password, is_bot) VALUES(?,?,1)", 
                         (BOT_USERNAME, bot_password))
                conn.commit()
                print(f"Created bot user: {BOT_USERNAME}")
    except Exception as e:
        print(f"Error creating bot user: {e}")

def db_register(username, password):
    try:
        with sqlite3.connect(DB_FILE, timeout=5.0) as conn:
            c = conn.cursor()
            hashed_pw = hash_password(password)
            c.execute("INSERT INTO users(username,password,is_bot) VALUES(?,?,0)", (username, hashed_pw))
            conn.commit()
            return True, "ok"
    except sqlite3.IntegrityError:
        return False, "username_taken"
    except Exception as e:
        print(f"DB register error: {e}")
        return False, "db_error"

def db_check_login(username, password):
    try:
        with sqlite3.connect(DB_FILE, timeout=5.0) as conn:
            c = conn.cursor()
            c.execute("SELECT password, is_bot FROM users WHERE username=?", (username,))
            row = c.fetchone()
            if not row:
                return False, "no_such_user"
            
            # Don't allow login as bot
            if row[1] == 1:
                return False, "bot_login_not_allowed"
                
            if not verify_password(password, row[0]):
                return False, "bad_password"
            return True, "ok"
    except Exception as e:
        print(f"DB login error: {e}")
        return False, "db_error"

def db_add_friend(user, friend):
    try:
        with sqlite3.connect(DB_FILE, timeout=5.0) as conn:
            c = conn.cursor()
            # ensure friend exists
            c.execute("SELECT username FROM users WHERE username=?", (friend,))
            if not c.fetchone():
                return False, "no_such_user"
            # prevent self-friending
            if user == friend:
                return False, "cannot_friend_self"
            c.execute("INSERT OR IGNORE INTO friends(user, friend) VALUES(?,?)", (user, friend))
            c.execute("INSERT OR IGNORE INTO friends(user, friend) VALUES(?,?)", (friend, user))
            conn.commit()
            return True, "ok"
    except Exception as e:
        print(f"DB add friend error: {e}")
        return False, "db_error"

def db_get_friends(user):
    try:
        with sqlite3.connect(DB_FILE, timeout=5.0) as conn:
            c = conn.cursor()
            c.execute("SELECT friend FROM friends WHERE user=?", (user,))
            return [row[0] for row in c.fetchall()]
    except Exception as e:
        print(f"DB get friends error: {e}")
        return []

def db_create_group(group_id, name, creator):
    try:
        with sqlite3.connect(DB_FILE, timeout=5.0) as conn:
            c = conn.cursor()
            ts = time.time()
            c.execute("INSERT INTO groups(id, name, creator, created_at) VALUES(?,?,?,?)", 
                     (group_id, name, creator, ts))
            c.execute("INSERT INTO group_members(group_id, username, joined_at) VALUES(?,?,?)",
                     (group_id, creator, ts))
            # Add bot to the group
            c.execute("INSERT INTO group_members(group_id, username, joined_at) VALUES(?,?,?)",
                     (group_id, BOT_USERNAME, ts))
            conn.commit()
            return True, "ok"
    except sqlite3.IntegrityError:
        return False, "group_exists"
    except Exception as e:
        print(f"DB create group error: {e}")
        return False, "db_error"

def db_join_group(group_id, username):
    try:
        with sqlite3.connect(DB_FILE, timeout=5.0) as conn:
            c = conn.cursor()
            # check if group exists
            c.execute("SELECT id FROM groups WHERE id=?", (group_id,))
            if not c.fetchone():
                return False, "no_such_group"
            c.execute("INSERT OR IGNORE INTO group_members(group_id, username, joined_at) VALUES(?,?,?)",
                     (group_id, username, time.time()))
            # Make sure bot is also in the group
            c.execute("INSERT OR IGNORE INTO group_members(group_id, username, joined_at) VALUES(?,?,?)",
                     (group_id, BOT_USERNAME, time.time()))
            conn.commit()
            return True, "ok"
    except Exception as e:
        print(f"DB join group error: {e}")
        return False, "db_error"

def db_leave_group(group_id, username):
    try:
        with sqlite3.connect(DB_FILE, timeout=5.0) as conn:
            c = conn.cursor()
            # Don't allow bot to leave groups
            if username == BOT_USERNAME:
                return False, "bot_cannot_leave"
            c.execute("DELETE FROM group_members WHERE group_id=? AND username=?", (group_id, username))
            conn.commit()
            return True, "ok"
    except Exception as e:
        print(f"DB leave group error: {e}")
        return False, "db_error"

def db_get_user_groups(username):
    try:
        with sqlite3.connect(DB_FILE, timeout=5.0) as conn:
            c = conn.cursor()
            c.execute("""SELECT g.id, g.name FROM groups g 
                        JOIN group_members gm ON g.id = gm.group_id 
                        WHERE gm.username=?""", (username,))
            return [{"id": row[0], "name": row[1]} for row in c.fetchall()]
    except Exception as e:
        print(f"DB get user groups error: {e}")
        return []

def db_get_group_members(group_id):
    try:
        with sqlite3.connect(DB_FILE, timeout=5.0) as conn:
            c = conn.cursor()
            c.execute("SELECT username FROM group_members WHERE group_id=?", (group_id,))
            return [row[0] for row in c.fetchall()]
    except Exception as e:
        print(f"DB get group members error: {e}")
        return []

def db_store_message(sender, receiver=None, group_id=None, text="", delivered=False):
    try:
        ts = time.time()
        with sqlite3.connect(DB_FILE, timeout=5.0) as conn:
            c = conn.cursor()
            c.execute("INSERT INTO messages(sender,receiver,group_id,text,ts,delivered) VALUES(?,?,?,?,?,?)",
                      (sender, receiver, group_id, text, ts, 1 if delivered else 0))
            conn.commit()
            return ts
    except Exception as e:
        print(f"DB store message error: {e}")
        return time.time()

def db_get_history(user, other=None, group_id=None, limit=200):
    try:
        with sqlite3.connect(DB_FILE, timeout=5.0) as conn:
            c = conn.cursor()
            if group_id:
                c.execute("""SELECT sender, receiver, group_id, text, ts FROM messages
                            WHERE group_id=? ORDER BY ts ASC LIMIT ?""", (group_id, limit))
            else:
                c.execute("""SELECT sender, receiver, group_id, text, ts FROM messages
                            WHERE ((sender=? AND receiver=?) OR (sender=? AND receiver=?))
                            AND group_id IS NULL
                            ORDER BY ts ASC LIMIT ?""", (user, other, other, user, limit))
            return c.fetchall()
    except Exception as e:
        print(f"DB get history error: {e}")
        return []

def db_get_undelivered(username):
    try:
        with sqlite3.connect(DB_FILE, timeout=5.0) as conn:
            c = conn.cursor()
            c.execute("""SELECT id, sender, receiver, group_id, text, ts FROM messages 
                        WHERE ((receiver=? AND group_id IS NULL) OR 
                              (group_id IN (SELECT group_id FROM group_members WHERE username=?)))
                        AND delivered=0 ORDER BY ts ASC""", (username, username))
            rows = c.fetchall()
            # mark as delivered
            c.execute("""UPDATE messages SET delivered=1 WHERE 
                        ((receiver=? AND group_id IS NULL) OR 
                         (group_id IN (SELECT group_id FROM group_members WHERE username=?)))
                        AND delivered=0""", (username, username))
            conn.commit()
            return rows
    except Exception as e:
        print(f"DB get undelivered error: {e}")
        return []

# ---------- Bot functionality ----------
def handle_bot_command(message_text, sender, group_id):
    """Handle bot commands and return bot response if any"""
    if not message_text.startswith('@'):
        return None
    
    parts = message_text.split(' ', 1)
    command = parts[0].lower()
    
    if command == '@echo':
        if len(parts) > 1:
            echo_text = parts[1]
            return {
                'type': 'group_echo',
                'text': echo_text,
                'group_id': group_id
            }
    
    elif command == '@all':
        if len(parts) > 1:
            broadcast_text = parts[1]
            return {
                'type': 'private_broadcast',
                'text': broadcast_text,
                'group_id': group_id,
                'original_sender': sender
            }
    
    return None

def execute_bot_action(action):
    """Execute bot action"""
    if action['type'] == 'group_echo':
        # Send echo message to group
        group_id = action['group_id']
        text = action['text']
        ts = time.time()
        
        members = db_get_group_members(group_id)
        delivered_count = 0
        
        with lock:
            for member in members:
                if member != BOT_USERNAME:  # don't send to bot itself
                    target_w = clients.get(member)
                    if target_w and send_json_w(target_w, {
                        "type": "group_msg",
                        "from": BOT_USERNAME,
                        "group_id": group_id,
                        "text": text,
                        "ts": ts
                    }):
                        delivered_count += 1
        
        # Store bot message
        db_store_message(BOT_USERNAME, group_id=group_id, text=text, delivered=(delivered_count > 0))
    
    elif action['type'] == 'private_broadcast':
        # Send private message to all group members
        group_id = action['group_id']
        text = action['text']
        original_sender = action['original_sender']
        ts = time.time()
        
        members = db_get_group_members(group_id)
        broadcast_text = f"[Broadcast from {original_sender} in group {group_id}]: {text}"
        
        with lock:
            for member in members:
                if member not in [BOT_USERNAME, original_sender]:  # don't send to bot or original sender
                    target_w = clients.get(member)
                    if target_w:
                        if send_json_w(target_w, {
                            "type": "msg",
                            "from": BOT_USERNAME,
                            "to": member,
                            "text": broadcast_text,
                            "ts": ts
                        }):
                            db_store_message(BOT_USERNAME, receiver=member, text=broadcast_text, delivered=True)
                        else:
                            db_store_message(BOT_USERNAME, receiver=member, text=broadcast_text, delivered=False)
                    else:
                        db_store_message(BOT_USERNAME, receiver=member, text=broadcast_text, delivered=False)

# ---------- Socket/Protocol helpers ----------
def send_json_w(wfile, obj):
    try:
        wfile.write(json.dumps(obj, ensure_ascii=False) + "\n")
        wfile.flush()
        return True
    except Exception as e:
        print(f"Send JSON error: {e}")
        return False

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
        uname = msg.get("username", "").strip()
        pwd = msg.get("password", "")
        
        if not uname or len(uname) < 3:
            send_json_w(wfile, {"type":"auth","status":"error","error":"username_too_short"})
            return
        
        if len(pwd) < 4:
            send_json_w(wfile, {"type":"auth","status":"error","error":"password_too_short"})
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
            if username in clients:
                # disconnect previous connection
                try:
                    old_wfile = clients[username]
                    send_json_w(old_wfile, {"type":"error","error":"logged_in_elsewhere"})
                except:
                    pass
            clients[username] = wfile
        print(f"{username} connected from {addr}")

        # send friend list
        friends = db_get_friends(username)
        send_json_w(wfile, {"type":"friends_list","friends":friends})
        
        # send groups list
        groups = db_get_user_groups(username)
        send_json_w(wfile, {"type":"groups_list","groups":groups})

        # deliver undelivered messages
        undelivered = db_get_undelivered(username)
        for mid, sender, receiver, group_id, text, ts in undelivered:
            if group_id:
                send_json_w(wfile, {"type":"group_msg","from":sender,"group_id":group_id,"text":text,"ts":ts})
            else:
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
                friend = msg.get("friend", "").strip()
                if not friend:
                    send_json_w(wfile, {"type":"error","error":"empty_friend_name"})
                    continue
                ok, reason = db_add_friend(username, friend)
                if ok:
                    friends = db_get_friends(username)
                    send_json_w(wfile, {"type":"friends_list","friends":friends})
                    send_json_w(wfile, {"type":"info","info":f"Added {friend} as friend"})
                else:
                    send_json_w(wfile, {"type":"error","error":reason})
            
            elif t == "create_group":
                group_id = msg.get("group_id", "").strip()
                group_name = msg.get("name", "").strip()
                if not group_id or not group_name:
                    send_json_w(wfile, {"type":"error","error":"empty_group_data"})
                    continue
                ok, reason = db_create_group(group_id, group_name, username)
                if ok:
                    groups = db_get_user_groups(username)
                    send_json_w(wfile, {"type":"groups_list","groups":groups})
                    send_json_w(wfile, {"type":"info","info":f"Created group {group_name} (Bot automatically added)"})
                else:
                    send_json_w(wfile, {"type":"error","error":reason})
            
            elif t == "join_group":
                group_id = msg.get("group_id", "").strip()
                if not group_id:
                    send_json_w(wfile, {"type":"error","error":"empty_group_id"})
                    continue
                ok, reason = db_join_group(group_id, username)
                if ok:
                    groups = db_get_user_groups(username)
                    send_json_w(wfile, {"type":"groups_list","groups":groups})
                    send_json_w(wfile, {"type":"info","info":f"Joined group {group_id}"})
                else:
                    send_json_w(wfile, {"type":"error","error":reason})
            
            elif t == "leave_group":
                group_id = msg.get("group_id", "").strip()
                if not group_id:
                    send_json_w(wfile, {"type":"error","error":"empty_group_id"})
                    continue
                ok, reason = db_leave_group(group_id, username)
                if ok:
                    groups = db_get_user_groups(username)
                    send_json_w(wfile, {"type":"groups_list","groups":groups})
                    send_json_w(wfile, {"type":"info","info":f"Left group {group_id}"})
                else:
                    send_json_w(wfile, {"type":"error","error":reason})
            
            elif t == "get_history":
                other = msg.get("other")
                group_id = msg.get("group_id")
                if group_id:
                    hist = db_get_history(username, group_id=group_id)
                    hist2 = [{"from":h[0],"to":h[1],"group_id":h[2],"text":h[3],"ts":h[4]} for h in hist]
                    send_json_w(wfile, {"type":"history","group_id":group_id,"messages":hist2})
                elif other:
                    hist = db_get_history(username, other)
                    hist2 = [{"from":h[0],"to":h[1],"group_id":h[2],"text":h[3],"ts":h[4]} for h in hist]
                    send_json_w(wfile, {"type":"history","other":other,"messages":hist2})
            
            elif t == "msg":
                to = msg.get("to", "").strip()
                text = msg.get("text", "").strip()
                if not to or not text:
                    send_json_w(wfile, {"type":"error","error":"empty_message_data"})
                    continue
                
                ts = time.time()
                with lock:
                    target_w = clients.get(to)
                if target_w:
                    if send_json_w(target_w, {"type":"msg","from":username,"to":to,"text":text,"ts":ts}):
                        db_store_message(username, receiver=to, text=text, delivered=True)
                    else:
                        db_store_message(username, receiver=to, text=text, delivered=False)
                else:
                    db_store_message(username, receiver=to, text=text, delivered=False)
                    send_json_w(wfile, {"type":"info","info":"message_queued"})
            
            elif t == "group_msg":
                group_id = msg.get("group_id", "").strip()
                text = msg.get("text", "").strip()
                if not group_id or not text:
                    send_json_w(wfile, {"type":"error","error":"empty_group_message_data"})
                    continue
                
                members = db_get_group_members(group_id)
                if username not in members:
                    send_json_w(wfile, {"type":"error","error":"not_group_member"})
                    continue
                
                # Check for bot commands
                bot_action = handle_bot_command(text, username, group_id)
                
                ts = time.time()
                delivered_count = 0
                with lock:
                    for member in members:
                        if member != username:  # don't send to self
                            target_w = clients.get(member)
                            if target_w and send_json_w(target_w, {"type":"group_msg","from":username,"group_id":group_id,"text":text,"ts":ts}):
                                delivered_count += 1
                
                # store message (delivered if at least one member received it)
                db_store_message(username, group_id=group_id, text=text, delivered=(delivered_count > 0))
                
                # Execute bot action if command was detected
                if bot_action:
                    execute_bot_action(bot_action)
                
                if delivered_count < len(members) - 1:
                    send_json_w(wfile, {"type":"info","info":"group_message_partially_queued"})
            
            else:
                send_json_w(wfile, {"type":"error","error":"unknown_type"})

    except Exception as e:
        print(f"Client handler error: {e}")
    finally:
        if username:
            with lock:
                clients.pop(username, None)
        try:
            conn.close()
        except:
            pass
        print(f"{username or 'unknown'} disconnected")

def main():
    init_db()
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind((HOST, PORT))
        s.listen(128)
        print(f"Server listening on {HOST}:{PORT}")
        print(f"Bot '{BOT_USERNAME}' is ready with commands: @echo <text>, @all <text>")
        
        while True:
            conn, addr = s.accept()
            t = threading.Thread(target=handle_client, args=(conn, addr), daemon=True)
            t.start()
    except KeyboardInterrupt:
        print("Shutting down")
    except Exception as e:
        print(f"Server error: {e}")
    finally:
        s.close()

if __name__ == "__main__":
    main()
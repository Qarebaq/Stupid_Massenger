# client.py
"""
Terminal chat client with curses UI - Fixed Version
- Connects to server (JSON-over-TCP)
- Login/register with better validation
- Shows sidebar of friends and groups, main window shows chat with selected friend/group
- Local SQLite per-user caches messages, friends, and groups
- Added group functionality and improved error handling
- FIXED: Immediate message display and duplicate message issues
"""

import socket
import json
import threading
import sqlite3
import time
import curses
import os
import re
from datetime import datetime

SERVER_HOST = "127.0.0.1"
SERVER_PORT = 9999

# ---------- DB (local cache) ----------
def init_local_db(dbfile):
    conn = sqlite3.connect(dbfile, check_same_thread=False)
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS messages (
                 id INTEGER PRIMARY KEY AUTOINCREMENT,
                 sender TEXT, receiver TEXT, group_id TEXT, text TEXT, ts REAL)""")
    c.execute("""CREATE TABLE IF NOT EXISTS friends (friend TEXT PRIMARY KEY)""")
    c.execute("""CREATE TABLE IF NOT EXISTS groups (
                 id TEXT PRIMARY KEY, name TEXT)""")
    conn.commit()
    return conn

def local_store_message(conn, sender, receiver=None, group_id=None, text="", ts=None):
    if ts is None:
        ts = time.time()
    try:
        c = conn.cursor()
        c.execute("INSERT INTO messages(sender,receiver,group_id,text,ts) VALUES(?,?,?,?,?)",
                  (sender, receiver, group_id, text, ts))
        conn.commit()
    except Exception as e:
        print(f"Local store error: {e}")

def local_get_history(conn, me, other=None, group_id=None, limit=200):
    try:
        c = conn.cursor()
        if group_id:
            c.execute("""SELECT sender,receiver,group_id,text,ts FROM messages
                         WHERE group_id=? ORDER BY ts ASC LIMIT ?""", (group_id, limit))
        else:
            c.execute("""SELECT sender,receiver,group_id,text,ts FROM messages
                         WHERE ((sender=? AND receiver=?) OR (sender=? AND receiver=?))
                         AND group_id IS NULL
                         ORDER BY ts ASC LIMIT ?""", (me, other, other, me, limit))
        return c.fetchall()
    except Exception as e:
        print(f"Local get history error: {e}")
        return []

def local_set_friends(conn, friends):
    try:
        c = conn.cursor()
        c.execute("DELETE FROM friends")
        for f in friends:
            c.execute("INSERT OR IGNORE INTO friends(friend) VALUES(?)", (f,))
        conn.commit()
    except Exception as e:
        print(f"Local set friends error: {e}")

def local_get_friends(conn):
    try:
        c = conn.cursor()
        c.execute("SELECT friend FROM friends")
        return [r[0] for r in c.fetchall()]
    except Exception as e:
        print(f"Local get friends error: {e}")
        return []

def local_set_groups(conn, groups):
    try:
        c = conn.cursor()
        c.execute("DELETE FROM groups")
        for g in groups:
            c.execute("INSERT OR IGNORE INTO groups(id, name) VALUES(?,?)", (g["id"], g["name"]))
        conn.commit()
    except Exception as e:
        print(f"Local set groups error: {e}")

def local_get_groups(conn):
    try:
        c = conn.cursor()
        c.execute("SELECT id, name FROM groups")
        return [{"id": r[0], "name": r[1]} for r in c.fetchall()]
    except Exception as e:
        print(f"Local get groups error: {e}")
        return []

# ---------- Network thread ----------
class NetworkThread(threading.Thread):
    def __init__(self, sock, wfile, username, local_conn, ui):
        super().__init__(daemon=True)
        self.sock = sock
        self.wfile = wfile
        self.username = username
        self.local_conn = local_conn
        self.ui = ui
        self.running = True

    def send_json(self, obj):
        try:
            self.wfile.write(json.dumps(obj, ensure_ascii=False) + "\n")
            self.wfile.flush()
            return True
        except Exception as e:
            if self.ui:
                self.ui.log("Send error: " + str(e))
            return False

    def run(self):
        try:
            rfile = self.sock.makefile("r", encoding="utf-8")
            while self.running:
                line = rfile.readline()
                if not line:
                    if self.ui:
                        self.ui.log("Disconnected from server")
                    break
                try:
                    msg = json.loads(line.strip())
                except Exception as e:
                    if self.ui:
                        self.ui.log(f"Bad JSON from server: {e}")
                    continue
                
                t = msg.get("type")
                if t == "friends_list":
                    friends = msg.get("friends", [])
                    local_set_friends(self.local_conn, friends)
                    if self.ui:
                        self.ui.update_friends(friends)
                
                elif t == "groups_list":
                    groups = msg.get("groups", [])
                    local_set_groups(self.local_conn, groups)
                    if self.ui:
                        self.ui.update_groups(groups)
                
                elif t == "history":
                    other = msg.get("other")
                    group_id = msg.get("group_id")
                    messages = msg.get("messages", [])
                    for m in messages:
                        local_store_message(self.local_conn, m["from"], m.get("to"), m.get("group_id"), m["text"], m["ts"])
                    if self.ui and ((other and self.ui.selected_type == "friend" and self.ui.selected_friend == other) or \
                       (group_id and self.ui.selected_type == "group" and self.ui.selected_group == group_id)):
                        self.ui.refresh_chat()
                
                elif t == "msg":
                    sender = msg.get("from")
                    to = msg.get("to")
                    text = msg.get("text")
                    ts = msg.get("ts", time.time())
                    local_store_message(self.local_conn, sender, to, None, text, ts)
                    if self.ui:
                        self.ui.on_incoming(sender, text, is_group=False)
                
                elif t == "group_msg":
                    sender = msg.get("from")
                    group_id = msg.get("group_id")
                    text = msg.get("text")
                    ts = msg.get("ts", time.time())
                    local_store_message(self.local_conn, sender, None, group_id, text, ts)
                    if self.ui:
                        self.ui.on_incoming(sender, text, is_group=True, group_id=group_id)
                
                elif t == "auth":
                    # handled elsewhere
                    pass
                elif t == "error":
                    if self.ui:
                        self.ui.log("[ERROR] " + str(msg.get("error")))
                elif t == "info":
                    if self.ui:
                        self.ui.log("[INFO] " + str(msg.get("info")))
                else:
                    if self.ui:
                        self.ui.log("Unknown message:" + str(msg))
        except Exception as e:
            if self.ui:
                self.ui.log(f"Network thread error: {e}")
        finally:
            self.running = False

    def stop(self):
        self.running = False
        try:
            self.sock.close()
        except:
            pass

# ---------- Enhanced curses UI ----------
class ChatUI:
    def __init__(self, stdscr, send_cb, username, local_conn):
        self.stdscr = stdscr
        self.send_cb = send_cb
        self.username = username
        self.local_conn = local_conn
        curses.curs_set(0)
        self.height, self.width = self.stdscr.getmaxyx()
        
        # layout: left sidebar width, right chat area
        self.sidebar_w = max(25, int(self.width * 0.3))
        self.chat_w = self.width - self.sidebar_w - 1
        
        # windows
        self.win_sidebar = curses.newwin(self.height-3, self.sidebar_w, 0, 0)
        self.win_chat = curses.newwin(self.height-3, self.chat_w, 0, self.sidebar_w+1)
        self.win_input = curses.newwin(3, self.width, self.height-3, 0)
        
        # data
        self.friends = []
        self.groups = []
        self.selected_index = 0
        self.selected_type = "friend"  # "friend" or "group"
        self.selected_friend = None
        self.selected_group = None
        self.logs = []
        self.input_buffer = ""
        self.chat_lines = []
        self.lock = threading.Lock()
        self.sidebar_items = []  # combined friends and groups for display

    def log(self, text):
        with self.lock:
            self.logs.append(f"[{datetime.now().strftime('%H:%M:%S')}] {text}")
            if len(self.logs) > 100:
                self.logs = self.logs[-100:]
        self.redraw()

    def update_sidebar_items(self):
        """Combine friends and groups into sidebar items"""
        self.sidebar_items = []
        if self.friends:
            self.sidebar_items.append(("header", "Friends:"))
            for f in self.friends:
                self.sidebar_items.append(("friend", f))
        if self.groups:
            self.sidebar_items.append(("header", "Groups:"))
            for g in self.groups:
                self.sidebar_items.append(("group", g))

    def set_friends(self, friends):
        with self.lock:
            self.friends = friends
            self.update_sidebar_items()
            # adjust selection if needed
            if self.selected_type == "friend" and self.selected_friend not in friends:
                self._select_first_available()
        self.redraw()

    def update_friends(self, friends):
        self.set_friends(friends)

    def set_groups(self, groups):
        with self.lock:
            self.groups = groups
            self.update_sidebar_items()
            # adjust selection if needed
            if self.selected_type == "group":
                group_ids = [g["id"] if isinstance(g, dict) else g for g in groups]
                if self.selected_group not in group_ids:
                    self._select_first_available()
        self.redraw()

    def update_groups(self, groups):
        self.set_groups(groups)

    def _select_first_available(self):
        """Select first available friend or group"""
        if self.friends:
            self.selected_type = "friend"
            self.selected_friend = self.friends[0]
            self.selected_group = None
            self.selected_index = self._find_item_index("friend", self.friends[0])
        elif self.groups:
            self.selected_type = "group"
            group = self.groups[0]
            self.selected_group = group["id"] if isinstance(group, dict) else group
            self.selected_friend = None
            self.selected_index = self._find_item_index("group", group)
        else:
            self.selected_type = "friend"
            self.selected_friend = None
            self.selected_group = None
            self.selected_index = 0

    def _find_item_index(self, item_type, item_value):
        """Find index of item in sidebar"""
        for i, (t, v) in enumerate(self.sidebar_items):
            if t == item_type:
                if isinstance(v, dict) and v.get("id") == item_value:
                    return i
                elif isinstance(v, str) and v == item_value:
                    return i
        return 0

    def on_incoming(self, sender, text, is_group=False, group_id=None):
        """Handle incoming message"""
        if is_group:
            if group_id == self.selected_group and self.selected_type == "group":
                self.refresh_chat()
            else:
                group_name = self._get_group_name(group_id)
                self.log(f"New in {group_name}: {sender}: {text}")
        else:
            if sender == self.selected_friend and self.selected_type == "friend":
                self.refresh_chat()
            else:
                self.log(f"New from {sender}: {text}")

    def _get_group_name(self, group_id):
        """Get group name by ID"""
        for g in self.groups:
            if isinstance(g, dict) and g.get("id") == group_id:
                return g.get("name", group_id)
        return group_id

    def refresh_chat(self):
        """Refresh chat by reloading history from database"""
        if self.selected_type == "friend" and self.selected_friend:
            hist = local_get_history(self.local_conn, self.username, other=self.selected_friend)
        elif self.selected_type == "group" and self.selected_group:
            hist = local_get_history(self.local_conn, self.username, group_id=self.selected_group)
        else:
            hist = []

        lines = []
        for record in hist:
            sender, receiver, group_id, text, ts = record
            timestamp = datetime.fromtimestamp(ts).strftime('%H:%M')
            if group_id:
                prefix = f"{sender}: " if sender != self.username else "Me: "
            else:
                prefix = "Me: " if sender == self.username else f"{sender}: "
            lines.append(f"[{timestamp}] {prefix}{text}")
        
        self.attach_chat_lines(lines)

    def add_message_to_chat(self, sender, text, is_group=False, group_id=None):
        """Add a single message to current chat display"""
        timestamp = datetime.now().strftime('%H:%M')
        if is_group:
            prefix = f"{sender}: " if sender != self.username else "Me: "
        else:
            prefix = "Me: " if sender == self.username else f"{sender}: "
        
        new_line = f"[{timestamp}] {prefix}{text}"
        
        with self.lock:
            self.chat_lines.append(new_line)
            # Keep only last 200 lines to prevent memory issues
            if len(self.chat_lines) > 200:
                self.chat_lines = self.chat_lines[-200:]
        
        self.redraw()

    def redraw(self):
        try:
            with self.lock:
                self.win_sidebar.clear()
                self.win_chat.clear()
                self.win_input.clear()

                # Sidebar: title + items
                self.win_sidebar.box()
                self.win_sidebar.addstr(0, 2, " Contacts ")
                
                y = 1
                for idx, (item_type, item_value) in enumerate(self.sidebar_items):
                    if y >= self.height - 4:  # prevent overflow
                        break
                    
                    if item_type == "header":
                        try:
                            self.win_sidebar.addstr(y, 1, item_value[:self.sidebar_w-2], curses.A_BOLD)
                        except:
                            pass
                    else:
                        is_selected = (idx == self.selected_index)
                        marker = ">" if is_selected else " "
                        
                        if item_type == "friend":
                            display = f"{marker} {item_value}"
                        else:  # group
                            if isinstance(item_value, dict):
                                display = f"{marker} #{item_value.get('name', item_value.get('id', 'Unknown'))}"
                            else:
                                display = f"{marker} #{item_value}"
                        
                        try:
                            attr = curses.A_REVERSE if is_selected else curses.A_NORMAL
                            self.win_sidebar.addstr(y, 1, display[:self.sidebar_w-2], attr)
                        except:
                            pass
                    y += 1

                # Chat area title
                self.win_chat.box()
                if self.selected_type == "friend" and self.selected_friend:
                    title = f" Chat: {self.selected_friend} "
                elif self.selected_type == "group" and self.selected_group:
                    group_name = self._get_group_name(self.selected_group)
                    title = f" Group: {group_name} "
                else:
                    title = " No chat selected "
                
                self.win_chat.addstr(0, 2, title)

                # Chat lines
                y = 1
                available_height = self.height - 6
                start_idx = max(0, len(self.chat_lines) - available_height)
                
                for line in self.chat_lines[start_idx:]:
                    try:
                        self.win_chat.addstr(y, 1, line[:self.chat_w-2])
                    except:
                        pass
                    y += 1
                    if y >= self.height - 4:
                        break

                # Input area
                self.win_input.box()
                help_text = " Commands: /add <user>, /create <group_id> <name>, /join <group_id>, /leave <group_id>, TAB=switch, /quit "
                self.win_input.addstr(0, 2, help_text[:self.width-4])
                try:
                    cursor_pos = min(len(self.input_buffer), self.width-3)
                    display_start = max(0, len(self.input_buffer) - self.width + 3)
                    display_text = self.input_buffer[display_start:display_start + self.width - 3]
                    self.win_input.addstr(1, 1, display_text)
                except:
                    pass

                # draw
                self.win_sidebar.noutrefresh()
                self.win_chat.noutrefresh()
                self.win_input.noutrefresh()
                curses.doupdate()
        except Exception as e:
            # If redraw fails, just ignore - terminal might be resizing
            pass

    def attach_chat_lines(self, lines):
        with self.lock:
            self.chat_lines = lines
        self.redraw()

    def input_loop(self):
        """Handle keyboard input"""
        while True:
            try:
                ch = self.stdscr.getch()
            except:
                continue
                
            if ch == curses.KEY_RESIZE:
                # handle terminal resize
                self.height, self.width = self.stdscr.getmaxyx()
                self.sidebar_w = max(25, int(self.width * 0.3))
                self.chat_w = self.width - self.sidebar_w - 1
                # recreate windows
                self.win_sidebar = curses.newwin(self.height-3, self.sidebar_w, 0, 0)
                self.win_chat = curses.newwin(self.height-3, self.chat_w, 0, self.sidebar_w+1)
                self.win_input = curses.newwin(3, self.width, self.height-3, 0)
                self.redraw()
                continue
            
            elif ch == curses.KEY_BACKSPACE or ch == 127 or ch == 8:
                if self.input_buffer:
                    self.input_buffer = self.input_buffer[:-1]
                    self.redraw()
            
            elif ch == curses.KEY_ENTER or ch == 10 or ch == 13:
                line = self.input_buffer.strip()
                self.input_buffer = ""
                if line:
                    if line.startswith("/add "):
                        friend = line.split(" ", 1)[1].strip()
                        if friend and re.match(r'^[a-zA-Z0-9_]+$', friend):
                            self.send_cb({"type":"add_friend","friend":friend})
                            self.log(f"Adding friend {friend}...")
                        else:
                            self.log("Invalid friend name. Use only letters, numbers, and underscore.")
                    
                    elif line.startswith("/create "):
                        parts = line.split(" ", 2)
                        if len(parts) >= 3:
                            group_id = parts[1].strip()
                            group_name = parts[2].strip()
                            if group_id and group_name and re.match(r'^[a-zA-Z0-9_]+$', group_id):
                                self.send_cb({"type":"create_group","group_id":group_id,"name":group_name})
                                self.log(f"Creating group {group_name}...")
                            else:
                                self.log("Invalid group ID. Use only letters, numbers, and underscore.")
                        else:
                            self.log("Usage: /create <group_id> <group_name>")
                    
                    elif line.startswith("/join "):
                        group_id = line.split(" ", 1)[1].strip()
                        if group_id and re.match(r'^[a-zA-Z0-9_]+$', group_id):
                            self.send_cb({"type":"join_group","group_id":group_id})
                            self.log(f"Joining group {group_id}...")
                        else:
                            self.log("Invalid group ID.")
                    
                    elif line.startswith("/leave "):
                        group_id = line.split(" ", 1)[1].strip()
                        if group_id:
                            self.send_cb({"type":"leave_group","group_id":group_id})
                            self.log(f"Leaving group {group_id}...")
                        else:
                            self.log("Usage: /leave <group_id>")
                    
                    elif line == "/quit":
                        return "quit"
                    
                    else:
                        # send message to selected friend or group
                        if self.selected_type == "friend" and self.selected_friend:
                            # Store message locally immediately for instant display
                            local_store_message(self.local_conn, self.username, self.selected_friend, None, line)
                            self.add_message_to_chat(self.username, line, is_group=False)
                            self.send_cb({"type":"msg","to":self.selected_friend,"text":line})
                        elif self.selected_type == "group" and self.selected_group:
                            # Store message locally immediately for instant display
                            local_store_message(self.local_conn, self.username, None, self.selected_group, line)
                            self.add_message_to_chat(self.username, line, is_group=True, group_id=self.selected_group)
                            self.send_cb({"type":"group_msg","group_id":self.selected_group,"text":line})
                        else:
                            self.log("No friend or group selected")
                self.redraw()
            
            elif ch == 9:  # TAB: switch selection
                if self.sidebar_items:
                    # find next selectable item (skip headers)
                    current = self.selected_index
                    for _ in range(len(self.sidebar_items)):
                        current = (current + 1) % len(self.sidebar_items)
                        item_type, item_value = self.sidebar_items[current]
                        if item_type != "header":
                            self.selected_index = current
                            if item_type == "friend":
                                self.selected_type = "friend"
                                self.selected_friend = item_value
                                self.selected_group = None
                            else:  # group
                                self.selected_type = "group"
                                self.selected_friend = None
                                if isinstance(item_value, dict):
                                    self.selected_group = item_value["id"]
                                else:
                                    self.selected_group = item_value
                            return "switch"
            
            elif 32 <= ch <= 126 or ch >= 128:
                # regular character input
                try:
                    self.input_buffer += chr(ch)
                    self.redraw()
                except:
                    pass

# ---------- Main client logic ----------
def run_client():
    print("=" * 50)
    print("Welcome to SimpleCLIChat - Enhanced Version")
    print("=" * 50)
    
    while True:
        op = input("Do you want to (l)ogin or (r)egister? ").strip().lower()
        if op in ['l', 'login', 'r', 'register']:
            break
        print("Please enter 'l' for login or 'r' for register.")
    
    while True:
        username = input("Username (3+ chars, letters/numbers/underscore only): ").strip()
        if len(username) >= 3 and re.match(r'^[a-zA-Z0-9_]+$', username):
            break
        print("Username must be at least 3 characters and contain only letters, numbers, and underscore.")
    
    while True:
        password = input("Password (4+ chars): ").strip()
        if len(password) >= 4:
            break
        print("Password must be at least 4 characters.")

    print(f"Connecting to server at {SERVER_HOST}:{SERVER_PORT}...")
    
    try:
        # connect to server
        sock = socket.create_connection((SERVER_HOST, SERVER_PORT), timeout=10)
        wfile = sock.makefile("w", encoding="utf-8")
        rfile = sock.makefile("r", encoding="utf-8")

        # send auth
        if op in ['r', 'register']:
            auth_msg = {"type":"auth","op":"register","username":username,"password":password}
        else:
            auth_msg = {"type":"auth","op":"login","username":username,"password":password}
        
        wfile.write(json.dumps(auth_msg, ensure_ascii=False) + "\n")
        wfile.flush()

        # read response with timeout
        sock.settimeout(5.0)
        line = rfile.readline()
        if not line:
            print("No response from server")
            sock.close()
            return
        
        try:
            resp = json.loads(line.strip())
        except json.JSONDecodeError:
            print("Invalid response from server")
            sock.close()
            return

        if resp.get("type") == "auth" and resp.get("status") == "ok":
            print("✓ Authenticated successfully!")
        else:
            error = resp.get("error", "unknown_error")
            print(f"✗ Authentication failed: {error}")
            sock.close()
            return

        sock.settimeout(None)  # remove timeout for normal operation

        # setup local DB
        dbfile = f"client_{username}.db"
        local_conn = init_local_db(dbfile)

        print("Starting chat interface...")
        time.sleep(1)

        # start network thread
        net = NetworkThread(sock, wfile, username, local_conn, None)
        net.start()

        # run curses UI
        def curses_main(stdscr):
            try:
                ui = ChatUI(stdscr, lambda obj: net.send_json(obj), username, local_conn)
                net.ui = ui
                
                # load cached data
                friends = local_get_friends(local_conn)
                groups = local_get_groups(local_conn)
                ui.set_friends(friends)
                ui.set_groups(groups)

                # initial chat load
                ui.refresh_chat()

                ui.log("Connected! Use TAB to switch between friends/groups")
                ui.redraw()

                # main input loop
                while True:
                    result = ui.input_loop()
                    if result == "quit":
                        break
                    elif result == "switch":
                        # load history of newly selected friend/group without duplicating
                        ui.refresh_chat()
                        if ui.selected_type == "friend" and ui.selected_friend:
                            net.send_json({"type":"get_history","other":ui.selected_friend})
                        elif ui.selected_type == "group" and ui.selected_group:
                            net.send_json({"type":"get_history","group_id":ui.selected_group})
                    
                    # small sleep to prevent high CPU usage
                    time.sleep(0.01)
            
            except Exception as e:
                # fallback error display
                try:
                    stdscr.clear()
                    stdscr.addstr(0, 0, f"UI Error: {str(e)}")
                    stdscr.addstr(1, 0, "Press any key to exit...")
                    stdscr.refresh()
                    stdscr.getch()
                except:
                    pass

        try:
            curses.wrapper(curses_main)
        except KeyboardInterrupt:
            pass
        except Exception as e:
            print(f"UI error: {e}")
        
    except ConnectionRefusedError:
        print(f"✗ Could not connect to server at {SERVER_HOST}:{SERVER_PORT}")
        print("Make sure the server is running.")
    except socket.timeout:
        print("✗ Connection timeout")
    except Exception as e:
        print(f"✗ Connection error: {e}")
    finally:
        if 'net' in locals():
            net.stop()
        if 'sock' in locals():
            try:
                sock.close()
            except:
                pass
        if 'local_conn' in locals():
            try:
                local_conn.close()
            except:
                pass
    
    print("Client exited")

if __name__ == "__main__":
    run_client()
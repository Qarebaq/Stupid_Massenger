# client.py
"""
Terminal chat client with curses UI.
- Connects to server (JSON-over-TCP)
- Login/register
- Shows sidebar of friends, main window shows chat with selected friend
- Local SQLite per-user caches messages and friends
"""

import socket
import json
import threading
import sqlite3
import time
import curses
import os
from datetime import datetime

SERVER_HOST = "127.0.0.1"
SERVER_PORT = 9999

# ---------- DB (local cache) ----------
def init_local_db(dbfile):
    conn = sqlite3.connect(dbfile, check_same_thread=False)
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS messages (
                 id INTEGER PRIMARY KEY AUTOINCREMENT,
                 sender TEXT, receiver TEXT, text TEXT, ts REAL)""")
    c.execute("""CREATE TABLE IF NOT EXISTS friends (friend TEXT PRIMARY KEY)""")
    conn.commit()
    return conn

def local_store_message(conn, sender, receiver, text, ts=None):
    if ts is None:
        ts = time.time()
    c = conn.cursor()
    c.execute("INSERT INTO messages(sender,receiver,text,ts) VALUES(?,?,?,?)",
              (sender, receiver, text, ts))
    conn.commit()

def local_get_history(conn, me, other, limit=200):
    c = conn.cursor()
    c.execute("""SELECT sender,receiver,text,ts FROM messages
                 WHERE (sender=? AND receiver=?) OR (sender=? AND receiver=?)
                 ORDER BY ts ASC LIMIT ?""", (me, other, other, me, limit))
    return c.fetchall()

def local_set_friends(conn, friends):
    c = conn.cursor()
    c.execute("DELETE FROM friends")
    for f in friends:
        c.execute("INSERT OR IGNORE INTO friends(friend) VALUES(?)", (f,))
    conn.commit()

def local_get_friends(conn):
    c = conn.cursor()
    c.execute("SELECT friend FROM friends")
    return [r[0] for r in c.fetchall()]

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
        except Exception as e:
            self.ui.log("Send error: " + str(e))

    def run(self):
        rfile = self.sock.makefile("r", encoding="utf-8")
        while self.running:
            line = rfile.readline()
            if not line:
                self.ui.log("Disconnected from server")
                break
            try:
                msg = json.loads(line.strip())
            except:
                self.ui.log("Bad JSON from server")
                continue
            t = msg.get("type")
            if t == "friends_list":
                friends = msg.get("friends", [])
                local_set_friends(self.local_conn, friends)
                self.ui.update_friends(friends)
            elif t == "history":
                other = msg.get("other")
                messages = msg.get("messages", [])
                for m in messages:
                    local_store_message(self.local_conn, m["from"], m["to"], m["text"], m["ts"])
                if self.ui.selected_friend == other:
                    self.ui.refresh_chat()
            elif t == "msg":
                sender = msg.get("from")
                to = msg.get("to")
                text = msg.get("text")
                ts = msg.get("ts", time.time())
                # store locally
                local_store_message(self.local_conn, sender, to, text, ts)
                # if chat open, refresh UI, else notify
                self.ui.on_incoming(sender, text)
            elif t == "auth":
                # ignored here
                pass
            elif t == "error":
                self.ui.log("[ERROR] " + str(msg.get("error")))
            elif t == "info":
                self.ui.log("[INFO] " + str(msg.get("info")))
            else:
                self.ui.log("Unknown message:" + str(msg))

    def stop(self):
        self.running = False
        try:
            self.sock.close()
        except:
            pass

# ---------- Simple curses UI ----------
class ChatUI:
    def __init__(self, stdscr, send_cb):
        self.stdscr = stdscr
        self.send_cb = send_cb  # callback to send JSON object
        curses.curs_set(0)
        self.height, self.width = self.stdscr.getmaxyx()
        # layout: left sidebar width, right chat area
        self.sidebar_w = max(20, int(self.width * 0.25))
        self.chat_w = self.width - self.sidebar_w - 1
        # windows
        self.win_sidebar = curses.newwin(self.height-3, self.sidebar_w, 0, 0)
        self.win_chat = curses.newwin(self.height-3, self.chat_w, 0, self.sidebar_w+1)
        self.win_input = curses.newwin(3, self.width, self.height-3, 0)
        self.friends = []
        self.selected_index = 0
        self.selected_friend = None
        self.logs = []
        self.input_buffer = ""
        self.chat_lines = []
        self.lock = threading.Lock()

    def log(self, text):
        with self.lock:
            self.logs.append(f"[{datetime.now().strftime('%H:%M:%S')}] {text}")
            if len(self.logs) > 100:
                self.logs = self.logs[-100:]
        self.redraw()

    def set_friends(self, friends):
        with self.lock:
            self.friends = friends
            if friends:
                if self.selected_friend not in friends:
                    self.selected_index = 0
                    self.selected_friend = friends[0]
            else:
                self.selected_index = 0
                self.selected_friend = None
        self.redraw()

    def update_friends(self, friends):
        self.set_friends(friends)

    def on_incoming(self, sender, text):
        # Simple behavior: if sender is current selected friend, refresh chat
        if sender == self.selected_friend:
            self.redraw()
        else:
            # flash in logs
            self.log(f"New from {sender}: {text}")

    def refresh_chat(self):
        # callback (client must store chat in local DB and then request to render)
        self.redraw()

    def redraw(self):
        with self.lock:
            self.win_sidebar.clear()
            self.win_chat.clear()
            self.win_input.clear()

            # Sidebar: title + friends list
            self.win_sidebar.box()
            self.win_sidebar.addstr(0,2," Friends ")
            for idx, f in enumerate(self.friends):
                marker = ">" if idx == self.selected_index else " "
                display = f"{marker} {f}"
                try:
                    self.win_sidebar.addstr(1+idx, 1, display[:self.sidebar_w-2])
                except:
                    pass

            # Chat area title
            self.win_chat.box()
            title = f" Chat: {self.selected_friend if self.selected_friend else 'No friend selected'} "
            self.win_chat.addstr(0,2,title)

            # Chat lines: filled by caller (we'll expect caller to set chat_lines)
            y = 1
            for line in self.chat_lines[-(self.height-6):]:
                try:
                    self.win_chat.addstr(y,1, line[:self.chat_w-2])
                except:
                    pass
                y += 1

            # Input area
            self.win_input.box()
            self.win_input.addstr(0,2," Type message (Enter to send, /add <user> to add friend, TAB to switch friend) ")
            try:
                self.win_input.addstr(1,1, self.input_buffer[:self.width-2])
            except:
                pass

            # draw
            self.win_sidebar.noutrefresh()
            self.win_chat.noutrefresh()
            self.win_input.noutrefresh()
            curses.doupdate()

    def attach_chat_lines(self, lines):
        with self.lock:
            self.chat_lines = lines
        self.redraw()

    def input_loop(self):
        # handle keyboard input
        while True:
            ch = self.stdscr.getch()
            if ch == curses.KEY_BACKSPACE or ch == 127:
                self.input_buffer = self.input_buffer[:-1]
                self.redraw()
            elif ch == curses.KEY_ENTER or ch == 10 or ch == 13:
                line = self.input_buffer.strip()
                self.input_buffer = ""
                if line:
                    if line.startswith("/add "):
                        friend = line.split(" ",1)[1].strip()
                        if friend:
                            self.send_cb({"type":"add_friend","friend":friend})
                            self.log(f"Adding friend {friend}...")
                    elif line == "/quit":
                        return "quit"
                    else:
                        # send message to selected friend
                        if not self.selected_friend:
                            self.log("No friend selected")
                        else:
                            self.send_cb({"type":"msg","to":self.selected_friend,"text":line})
                            # also store locally as outgoing
                            self.log(f"Me -> {self.selected_friend}: {line}")
                self.redraw()
            elif ch == 9:  # TAB: switch friend
                if self.friends:
                    self.selected_index = (self.selected_index + 1) % len(self.friends)
                    self.selected_friend = self.friends[self.selected_index]
                    self.log(f"Selected {self.selected_friend}")
                    return "switch"  # let caller refresh
            elif 32 <= ch <= 126 or ch >= 128:
                self.input_buffer += chr(ch)
                self.redraw()
            # ignore others

# ---------- Main client logic ----------
def run_client():
    # simple login/register prompt (not curses)
    print("Welcome to SimpleCLIChat")
    op = input("Do you want to (l)ogin or (r)egister? ").strip().lower()
    username = input("username: ").strip()
    password = input("password: ").strip()

    # connect to server
    sock = socket.create_connection((SERVER_HOST, SERVER_PORT))
    wfile = sock.makefile("w", encoding="utf-8")
    rfile = sock.makefile("r", encoding="utf-8")

    # send auth
    if op == "r":
        wfile.write(json.dumps({"type":"auth","op":"register","username":username,"password":password}, ensure_ascii=False) + "\n")
    else:
        wfile.write(json.dumps({"type":"auth","op":"login","username":username,"password":password}, ensure_ascii=False) + "\n")
    wfile.flush()

    # read response
    line = rfile.readline()
    if not line:
        print("No response from server")
        sock.close()
        return
    resp = json.loads(line.strip())
    if resp.get("type") == "auth" and resp.get("status") == "ok":
        print("Authenticated")
    else:
        print("Auth failed:", resp)
        sock.close()
        return

    # setup local DB
    dbfile = f"client_{username}.db"
    local_conn = init_local_db(dbfile)

    # start network thread
    ui_holder = {"ui": None}
    net = NetworkThread(sock, wfile, username, local_conn, None)  # placeholder ui, will set after curses init
    net.start()

    # run curses UI
    def curses_main(stdscr):
        ui = ChatUI(stdscr, lambda obj: net.send_json(obj))
        ui_holder["ui"] = ui
        net.ui = ui  # attach ui to network thread
        # load initial friends from local DB (server will send actual list soon)
        friends = local_get_friends(local_conn)
        ui.set_friends(friends)

        # initial selected friend -> first friend if any
        if friends:
            ui.selected_friend = friends[0]
            ui.selected_index = 0

        # initial chat load for selected
        if ui.selected_friend:
            hist = local_get_history(local_conn, username, ui.selected_friend)
            lines = []
            for s,r,t,ts in hist:
                prefix = "Me: " if s==username else f"{s}: "
                lines.append(f"{datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M')} {prefix}{t}")
            ui.attach_chat_lines(lines)

        ui.redraw()

        # main loop: reacts to input and updates chat view when switched
        while True:
            res = ui.input_loop()
            if res == "quit":
                break
            if res == "switch":
                # load history of newly selected friend
                other = ui.selected_friend
                # request history from server (server will respond with history and net thread will store)
                net.send_json({"type":"get_history","other":other})
                # but also show local cached messages now
                hist = local_get_history(local_conn, username, other)
                lines = []
                for s,r,t,ts in hist:
                    prefix = "Me: " if s==username else f"{s}: "
                    lines.append(f"{datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M')} {prefix}{t}")
                ui.attach_chat_lines(lines)
            # periodically refresh chat from local DB if messages arrive
            # here we just sleep small to allow network thread to update and UI to redraw
            time.sleep(0.05)

    try:
        curses.wrapper(curses_main)
    except KeyboardInterrupt:
        pass
    finally:
        net.stop()
        try:
            sock.close()
        except:
            pass
    print("Client exited")

if __name__ == "__main__":
    run_client()


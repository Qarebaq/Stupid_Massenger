# server.py
# پیام‌رسان ساده: سرورِ TCP که نام کاربری‌ها را ثبت می‌کند و پیام‌ها را به هم می‌رساند.
# اجرا: python server.py
import socket
import threading
import json

HOST = "0.0.0.0"   # همهٔ اینترفیس‌ها
PORT = 9999        # پورت سرور (هر عدد آزاد دیگری هم میشه)

# نگه‌داری مشتریان متصل: username -> wfile (یک writer متن برای نوشتن خطوط JSON)
clients = {}
clients_lock = threading.Lock()  # برای همزمانی دسترسی به clients

def send_json(wfile, obj):
    """جمع‌بندی نوشتن JSON به یک writer (یک خط به صورت JSON+'\n')."""
    try:
        # ensure_ascii=False تا متن فارسی درست فرستاده شود
        wfile.write(json.dumps(obj, ensure_ascii=False) + "\n")
        wfile.flush()
    except Exception:
        # اگر نوشتن خطا داد (مثلاً connection بسته شده)، بی‌خیال می‌شویم
        pass

def handle_client(conn, addr):
    """
    هر بار که یک کلاینت وصل می‌شود، این تابع در یک ترد جدا اجرا می‌شود.
    کارها:
      1) منتظر اولین خط می‌ماند که باید حاوی JSON ثبت نام باشد:
         {"type":"register", "username":"ali"}
      2) اگر نام معتبر بود، آن username را ثبت می‌کند و وارد حلقه خواندن پیام‌ها می‌شود.
      3) هر پیامِ نوع "msg" را پردازش می‌کند و یا broadcast می‌کند یا به کاربر خاص می‌فرستد.
    """
    # دو فایل متن (rfile/wfile) روی socket باز می‌کنیم تا راحت‌تر با خطوط کار کنیم.
    rfile = conn.makefile("r", encoding="utf-8")
    wfile = conn.makefile("w", encoding="utf-8")
    username = None

    try:
        # --- مرحلهٔ ثبت نام (اولین پیام از کلاینت باید register باشد) ---
        line = rfile.readline()
        if not line:
            return  # اتصال بسته شد
        try:
            msg = json.loads(line.strip())
        except Exception:
            send_json(wfile, {"type":"error", "error":"invalid_registration_format"})
            return

        if msg.get("type") != "register" or "username" not in msg:
            send_json(wfile, {"type":"error", "error":"first_message_must_be_register"})
            return

        username = msg["username"]

        # بررسی اینکه username قبلاً گرفته نشده باشد (با قفل)
        with clients_lock:
            if username in clients:
                send_json(wfile, {"type":"error", "error":"username_taken"})
                return
            # ثبت writer در دیکشنری clients
            clients[username] = wfile

        print(f"{username} connected from {addr}")
        broadcast_system(f"{username} joined the chat")

        # --- حلقهٔ اصلی: خواندن پیام‌ها ---
        while True:
            line = rfile.readline()
            if not line:
                break  # کلاینت قطع شد
            try:
                msg = json.loads(line.strip())
            except Exception:
                send_json(wfile, {"type":"error", "error":"invalid_json"})
                continue

            # فقط نوع "msg" را پشتیبانی می‌کنیم
            if msg.get("type") == "msg":
                to = msg.get("to")
                text = msg.get("text", "")
                if to == "all":
                    broadcast(username, text)       # پخش همگانی
                else:
                    send_private(username, to, text)  # ارسال خصوصی
            else:
                send_json(wfile, {"type":"error", "error":"unknown_type"})
    finally:
        # زمانی که کلاینت قطع شد: از clients حذف کن و به همه اعلام کن
        if username:
            with clients_lock:
                clients.pop(username, None)
            print(f"{username} disconnected")
            broadcast_system(f"{username} left the chat")
        try:
            conn.close()
        except:
            pass

def broadcast(sender, text):
    """پخش پیام به همهٔ کاربران متصل."""
    obj = {"type":"msg", "from":sender, "to":"all", "text":text}
    with clients_lock:
        # list() تا در حلقه تغییر در clients ایمن باشه
        for user, w in list(clients.items()):
            send_json(w, obj)

def broadcast_system(text):
    """ارسال پیام سیستمی (مثلاً 'ali joined') به همه."""
    obj = {"type":"system", "text": text}
    with clients_lock:
        for user, w in list(clients.items()):
            send_json(w, obj)

def send_private(sender, to, text):
    """ارسال پیام خصوصی از sender به to. اگر گیرنده نباشد، به فرستنده پیغام خطا می‌دهیم."""
    obj = {"type":"msg", "from":sender, "to":to, "text":text}
    with clients_lock:
        w = clients.get(to)
    if w:
        send_json(w, obj)
    else:
        # اگر گیرنده پیدا نشد، به خودِ فرستنده error می‌فرستیم
        with clients_lock:
            wsender = clients.get(sender)
        if wsender:
            send_json(wsender, {"type":"error", "error":"user_not_found", "target": to})

def main():
    sock = socket.socket()
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((HOST, PORT))
    sock.listen()
    print(f"Server listening on {HOST}:{PORT}")
    try:
        while True:
            conn, addr = sock.accept()
            # برای هر اتصال، یک ترد جدا می‌سازیم
            t = threading.Thread(target=handle_client, args=(conn, addr), daemon=True)
            t.start()
    except KeyboardInterrupt:
        print("Shutting down server")
    finally:
        sock.close()

if __name__ == "__main__":
    main()

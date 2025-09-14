# client.py
# کلاینتِ سادهٔ خط‌دستوری برای پیام‌رسان فوق.
# اجرا: python client.py
import socket
import threading
import json
import sys

HOST = "127.0.0.1"   # اگر سرور روی ماشین محلیه، همین باشه؛ برای شبکه، آدرس سرور را بزار
PORT = 9999

def reader(sock):
    """تردی که پیام‌های ورودی از سرور را می‌خواند و چاپ می‌کند."""
    rfile = sock.makefile("r", encoding="utf-8")
    while True:
        line = rfile.readline()
        if not line:
            print("Disconnected from server")
            break
        try:
            msg = json.loads(line.strip())
        except:
            print("Bad message:", line)
            continue

        # انواع پیام‌هایی که ممکن است سرور بفرستد:
        if msg.get("type") == "msg":
            # نمونه: {"type":"msg","from":"ali","to":"kazem","text":"سلام"}
            print(f"[{msg['from']} -> {msg['to']}] {msg['text']}")
        elif msg.get("type") == "system":
            print(f"[SYSTEM] {msg['text']}")
        elif msg.get("type") == "error":
            # نمونه: {"type":"error", "error":"user_not_found", "target":"kazem"}
            err = msg.get("error")
            target = msg.get("target","")
            print(f"[ERROR] {err} {target}")
        else:
            print("Unknown message:", msg)

def main():
    username = input("username (بدون فاصله): ").strip()
    if not username:
        print("username لازم است")
        return

    # اتصال به سرور
    sock = socket.create_connection((HOST, PORT))
    wfile = sock.makefile("w", encoding="utf-8")

    # مرحلهٔ ثبت نام: ارسال JSONِ {"type":"register","username":username}
    wfile.write(json.dumps({"type":"register", "username": username}, ensure_ascii=False) + "\n")
    wfile.flush()

    # ترد خواندن پیام‌ها را روشن می‌کنیم (so the main thread can read user input)
    threading.Thread(target=reader, args=(sock,), daemon=True).start()

    # راهنمایی دستورات
    print("دستورات:")
    print("  /msg <user> <text>   -> ارسال خصوصی به user")
    print("  /all <text>          -> ارسال برای همه")
    print("  /exit                -> قطع اتصال و خروج")

    # حلقهٔ دریافت ورودی از کاربر
    while True:
        try:
            line = input()
        except EOFError:
            break
        if not line:
            continue
        if line.startswith("/msg "):
            parts = line.split(" ", 2)
            if len(parts) < 3:
                print("Usage: /msg user text")
                continue
            to = parts[1]
            text = parts[2]
            obj = {"type":"msg", "to": to, "text": text}
        elif line.startswith("/all "):
            text = line[5:]
            obj = {"type":"msg", "to": "all", "text": text}
        elif line.strip() == "/exit":
            break
        else:
            print("Unknown command. Use /msg or /all or /exit")
            continue

        # ارسال پیام به سرور (به صورت یک خط JSON)
        wfile.write(json.dumps(obj, ensure_ascii=False) + "\n")
        wfile.flush()

    # بسته شدن سوکت و خروج
    try:
        sock.close()
    except:
        pass

if __name__ == "__main__":
    main()

"""Cijin Seaview Guesthouse (旗津海景民宿) - an AI customer service agent (Gradio app).

Every customer question goes to the agent first. The agent decides which tool to call:
  - get_guesthouse_info   look up information   (shop_info table)
  - search_rooms          look up records       (rooms table: rooms and extra services)
  - calculate_stay        calculate in code     (nights x price, weekend surcharge, long-stay discount, extras)
  - transfer_to_human     hand over to a person (front desk phone and LINE)
  - make_booking          change data (extra)   (writes a booking only after the customer confirms)
The model never answers from memory. The panel on the right shows the tool trace.

Run it with:  python app.py
"""

import csv
import json
import os
import re
import sqlite3
from datetime import date, datetime
from pathlib import Path

import gradio as gr
from dotenv import load_dotenv
from openai import OpenAI

BASE_DIR = Path(__file__).resolve().parent

# ---------------------------------------------------------------------------------------
# 1. API setup: settings come from .env (copy .env.example to .env and fill in API_KEY)
# ---------------------------------------------------------------------------------------
load_dotenv(BASE_DIR / ".env")
CHAT_MODEL = os.getenv("MODEL_NAME")
client = OpenAI(api_key=os.getenv("API_KEY") or "none", base_url=os.getenv("LLM_API_URL"), timeout=120.0)

SHOP_NAME = "Cijin Seaview Guesthouse (旗津海景民宿)"
STAFF_CONTACT = "Front desk 07-571-8888 (09:00-21:00), LINE @cijinseaview"

# ---------------------------------------------------------------------------------------
# 2. The database: built again from the CSV files every time the program starts
# ---------------------------------------------------------------------------------------
# check_same_thread=False because Gradio answers requests on worker threads.
connection = sqlite3.connect(BASE_DIR / "guesthouse.db", check_same_thread=False)
connection.row_factory = sqlite3.Row
cursor = connection.cursor()

cursor.execute("DROP TABLE IF EXISTS shop_info")
cursor.execute("CREATE TABLE shop_info (topic TEXT PRIMARY KEY, content TEXT NOT NULL)")
cursor.execute("DROP TABLE IF EXISTS rooms")
cursor.execute("""CREATE TABLE rooms (item_id INTEGER PRIMARY KEY, name TEXT, name_zh TEXT, aliases TEXT,
                  category TEXT, price INTEGER, unit TEXT, max_guests INTEGER, available INTEGER)""")
cursor.execute("""CREATE TABLE IF NOT EXISTS bookings (booking_id INTEGER PRIMARY KEY AUTOINCREMENT,
                  room TEXT, check_in TEXT, nights INTEGER, guests INTEGER, guest_name TEXT, phone TEXT,
                  total INTEGER, created_at TEXT)""")
with open(BASE_DIR / "shop_info.csv", newline="", encoding="utf-8") as f:
    cursor.executemany("INSERT INTO shop_info VALUES (?, ?)",
                       [(r["topic"], r["content"]) for r in csv.DictReader(f)])
with open(BASE_DIR / "rooms.csv", newline="", encoding="utf-8") as f:
    cursor.executemany("INSERT INTO rooms VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                       [(r["item_id"], r["name"], r["name_zh"], r["aliases"], r["category"], r["price"],
                         r["unit"], r["max_guests"] or None, r["available"]) for r in csv.DictReader(f)])
connection.commit()
SHOP_TOPICS = [r["topic"] for r in cursor.execute("SELECT topic FROM shop_info")]
print(f"Ready: {len(SHOP_TOPICS)} information topics, "
      f"{cursor.execute('SELECT COUNT(*) FROM rooms').fetchone()[0]} rooms and services.")


# ---------------------------------------------------------------------------------------
# 3. The tools: plain Python functions. They return data (not sentences).
# ---------------------------------------------------------------------------------------
def _row_to_dict(r):
    return {"name": r["name"], "name_zh": r["name_zh"], "category": r["category"], "price": r["price"],
            "unit": r["unit"], "max_guests": r["max_guests"], "available": bool(r["available"])}


def find_items(keyword, category=None):
    """Forgiving names: match the English name, the Chinese name or any alias (in both directions)."""
    key = (keyword or "").strip().lower()
    rows = cursor.execute("SELECT * FROM rooms" + (" WHERE category = ?" if category else ""),
                          (category,) if category else ()).fetchall()
    if not key:
        return rows
    def names(r):
        return [r["name"].lower(), r["name_zh"]] + [a.strip().lower() for a in r["aliases"].split(";") if a.strip()]
    exact = [r for r in rows if key in names(r)]            # 1) the exact name or an exact alias
    if exact:
        return exact
    return [r for r in rows if any(key in n or n in key for n in names(r))]   # 2) part of a name


def get_guesthouse_info(topic):
    """Return one piece of guesthouse information, such as the check-in time."""
    row = cursor.execute("SELECT content FROM shop_info WHERE topic = ?", (topic,)).fetchone()
    if row is None:
        return {"found": False, "topics_available": SHOP_TOPICS}
    return {"found": True, "topic": topic, "content": row["content"]}


def search_rooms(keyword="", guests=0, category=""):
    """Find rooms or extra services by name (English, Chinese or another common name) and/or number of guests."""
    rows = find_items(keyword, category or None)
    if guests:
        rows = [r for r in rows if r["category"] == "room" and (r["max_guests"] or 0) >= int(guests)]
    if not rows:
        return {"found": False, "note": f"Nothing matches '{keyword}'"
                                        f"{f' for {guests} guests' if guests else ''}. Do not guess."}
    return {"found": True, "items": [_row_to_dict(r) for r in rows]}


WEEKEND_SURCHARGE = 0.20      # Friday and Saturday nights cost 20% more
LONG_STAY_NIGHTS = 3          # 3 nights or more ...
LONG_STAY_DISCOUNT = 0.10     # ... 10% off the room price


def calculate_stay(room, nights, weekend_nights=0, guests=0, extras=None):
    """Price of a stay: weekday nights x price + weekend nights x price x 1.2,
    10% off the room part for 3+ nights, plus extra services (quantity x price)."""
    nights, weekend_nights, guests = int(nights), int(weekend_nights or 0), int(guests or 0)
    if nights < 1 or weekend_nights < 0 or weekend_nights > nights:
        return {"total": None, "note": "The number of nights is not clear. Ask the customer."}
    matches = find_items(room, "room")
    if len(matches) != 1:
        return {"total": None,
                "note": f"Which room is '{room}'? " + ("Not found." if not matches else
                        "Possible rooms: " + ", ".join(r["name"] for r in matches)) + " Ask the customer or use search_rooms."}
    r = matches[0]
    if not r["available"]:
        return {"total": None, "note": f"{r['name']} ({r['name_zh']}) is fully booked. Suggest another room."}
    if guests and guests > r["max_guests"]:
        return {"total": None, "note": f"{r['name']} is for at most {r['max_guests']} guests. Suggest a bigger room."}

    weekday_nights = nights - weekend_nights
    weekend_price = round(r["price"] * (1 + WEEKEND_SURCHARGE))
    room_cost = weekday_nights * r["price"] + weekend_nights * weekend_price
    lines = [f"{weekday_nights} weekday night(s) x NT${r['price']} = NT${weekday_nights * r['price']}"]
    if weekend_nights:
        lines.append(f"{weekend_nights} weekend night(s) x NT${weekend_price} (+20%) = NT${weekend_nights * weekend_price}")
    discount = 0
    if nights >= LONG_STAY_NIGHTS:
        discount = round(room_cost * LONG_STAY_DISCOUNT)
        lines.append(f"Long-stay discount ({nights} nights, 10% off the room) = -NT${discount}")

    extras_cost = 0
    for e in extras or []:
        found = find_items(e.get("name", ""), "service")
        qty = int(e.get("quantity", 1) or 1)
        if len(found) != 1:
            return {"total": None, "note": f"Which service is '{e.get('name')}'? Ask the customer or use search_rooms."}
        s = found[0]
        if not s["available"]:
            return {"total": None, "note": f"{s['name']} ({s['name_zh']}) is not available now."}
        extras_cost += qty * s["price"]
        lines.append(f"{qty} x {s['name']} ({s['name_zh']}, {s['unit']}) x NT${s['price']} = NT${qty * s['price']}")

    total = room_cost - discount + extras_cost
    return {"room": r["name"], "room_zh": r["name_zh"], "nights": nights, "lines": lines, "total": total,
            "currency": "NT$", "deposit_30_percent": round(total * 0.3)}


def transfer_to_human(reason=""):
    """Hand the conversation to a person."""
    return {"message": f"Please contact our staff: {STAFF_CONTACT}.",
            "reason": reason,
            "note": ("The hand-over is done. First tell the customer in one sentence why, then give this contact. "
                     "Do not say that you will connect them: you cannot. Do not call this tool again.")}


def make_booking(room, check_in, nights, guests, guest_name, phone, confirmed=False, weekend_nights=0):
    """Extra feature: a tool that changes data. It writes to the database only after the customer confirms."""
    try:
        day = datetime.strptime(check_in, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return {"booked": False, "note": "The check-in date must look like 2026-11-20. Ask the customer."}
    if day < date.today():
        return {"booked": False, "note": "The check-in date is in the past. Ask the customer."}
    price = calculate_stay(room, nights, weekend_nights, guests)
    if price["total"] is None:
        return {"booked": False, "note": price["note"]}
    summary = {"room": f"{price['room']} ({price['room_zh']})", "check_in": check_in, "nights": int(nights),
               "guests": int(guests), "guest_name": guest_name, "phone": phone, "total": price["total"],
               "deposit_30_percent": price["deposit_30_percent"]}
    if not confirmed:
        return {"booked": False, "needs_confirmation": True, "summary": summary,
                "note": "Show this summary and ask the customer to reply 'yes' to confirm. Nothing is saved yet."}
    cursor.execute("INSERT INTO bookings (room, check_in, nights, guests, guest_name, phone, total, created_at) "
                   "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                   (price["room"], check_in, int(nights), int(guests), guest_name, phone, price["total"],
                    datetime.now().isoformat(timespec="seconds")))
    connection.commit()
    return {"booked": True, "booking_id": cursor.lastrowid, "summary": summary,
            "note": "Tell the customer the booking number and that the 30% deposit is due within 3 days."}


TOOL_FUNCTIONS = {
    "get_guesthouse_info": get_guesthouse_info,
    "search_rooms": search_rooms,
    "calculate_stay": calculate_stay,
    "transfer_to_human": transfer_to_human,
    "make_booking": make_booking,
}

TOOL_SPECS = [
    {"type": "function", "function": {
        "name": "get_guesthouse_info",
        "description": ("Get one piece of guesthouse information: check-in/check-out time, address, how to get here "
                        "(transport), parking, breakfast time, payment methods, deposit, cancellation policy and deposit refunds, pets, "
                        "Wi-Fi, quiet hours and smoking, discounts and weekend prices, facilities (kitchen, laundry, "
                        "luggage), contact phone/LINE."),
        "parameters": {"type": "object",
                       "properties": {"topic": {"type": "string", "enum": SHOP_TOPICS}},
                       "required": ["topic"]}}},
    {"type": "function", "function": {
        "name": "search_rooms",
        "description": ("Look up rooms and extra services (breakfast, bike rental, airport pickup, ferry ticket, BBQ set, "
                        "late checkout, extra bed, pet fee, guided tour): price, unit, maximum guests and whether it is "
                        "available. Use a short name such as 'family', 'seaview', '海景' or 'bike', and/or the number of guests."),
        "parameters": {"type": "object",
                       "properties": {"keyword": {"type": "string", "description": "Short room or service name; empty for all"},
                                      "guests": {"type": "integer", "description": "Number of guests, to find rooms big enough"},
                                      "category": {"type": "string", "enum": ["room", "service"]}},
                       "required": []}}},
    {"type": "function", "function": {
        "name": "calculate_stay",
        "description": ("Calculate the price of a stay (room x nights, weekend surcharge, long-stay discount, plus extra "
                        "services). Always use this for any price total or for a price of more than one night. "
                        "Never calculate prices yourself."),
        "parameters": {"type": "object",
                       "properties": {
                           "room": {"type": "string", "description": "Short room name, e.g. 'family' or '海景雙人房'"},
                           "nights": {"type": "integer", "description": "Total number of nights"},
                           "weekend_nights": {"type": "integer", "description": "How many of the nights are Friday or Saturday nights (0 if unknown)"},
                           "guests": {"type": "integer"},
                           "extras": {"type": "array", "description": "Extra services, e.g. breakfast for 2 people x 2 days = quantity 4",
                                      "items": {"type": "object",
                                                "properties": {"name": {"type": "string"}, "quantity": {"type": "integer"}},
                                                "required": ["name", "quantity"]}}},
                       "required": ["room", "nights"]}}},
    {"type": "function", "function": {
        "name": "transfer_to_human",
        "description": ("Hand over to a staff member. Always use this for complaints (dirty room, broken air conditioner, "
                        "noise, rude staff), refunds or compensation, when the guest asks for a person, the owner or a "
                        "manager (經理、老闆、真人、客訴、退錢), and for guesthouse questions that no other tool can answer."),
        "parameters": {"type": "object",
                       "properties": {"reason": {"type": "string"}}, "required": []}}},
    {"type": "function", "function": {
        "name": "make_booking",
        "description": ("Book a room. First call it with confirmed=false to get a summary and show it to the customer. "
                        "Only after the customer clearly says yes, call it again with confirmed=true."),
        "parameters": {"type": "object",
                       "properties": {
                           "room": {"type": "string"}, "check_in": {"type": "string", "description": "YYYY-MM-DD"},
                           "nights": {"type": "integer"}, "guests": {"type": "integer"},
                           "weekend_nights": {"type": "integer"},
                           "guest_name": {"type": "string"}, "phone": {"type": "string"},
                           "confirmed": {"type": "boolean"}},
                       "required": ["room", "check_in", "nights", "guests", "guest_name", "phone", "confirmed"]}}},
]

SYSTEM_PROMPT = f"""
You are "Xiao Hai (小海)", the online customer service agent of {SHOP_NAME},
a small guesthouse near the Cijin Ferry Pier in Kaohsiung, Taiwan. Prices are in New Taiwan dollars, written like NT$2,400.
Today is {date.today().isoformat()}.

GROUNDING
1. Answer only from tool results. Never answer from memory, and never invent prices, rooms, rules or services.
2. Before you say the guesthouse does or does not have something (a facility, a service, a rule), check it with
   get_guesthouse_info (e.g. topic "facilities") or search_rooms. If the tools do not have it, say plainly that you do
   not have that information and call transfer_to_human to give the staff contact. Do not guess.

SCOPE
3. Only talk about this guesthouse and staying here (rooms, services, getting here, booking).
   Politely refuse other topics (weather, homework, coding, other hotels, travel plans for other cities) without calling
   any tool, and invite the customer to ask about the guesthouse.

ROUTING
4. Questions about check-in, address, transport, parking, breakfast time, payment, deposit, cancellation, pets, Wi-Fi,
   smoking, discounts, facilities or contact -> get_guesthouse_info (choose the closest topic).
5. Questions about which rooms or services exist, their price for one night, how many people fit, or availability
   -> search_rooms.
6. Any total price, more than one night, weekend nights, or room plus extras -> calculate_stay. Never add or multiply
   numbers yourself.
7. Complaints, refunds, asking for a person or a manager, or guesthouse questions no tool can answer -> ALWAYS call
   transfer_to_human. Never only apologise without calling it.
8. Booking a room -> make_booking with confirmed=false first; use confirmed=true only after the customer says yes.

LANGUAGE AND FORMAT
9. Reply in the language the program tells you. In a Chinese reply, use Traditional Chinese and the Chinese names (name_zh).
10. Be warm and short: at most four sentences, or a short list for a price breakdown.
"""

MAX_STEPS = 6          # a confused model must not loop forever


def reply_language(text):
    return "Traditional Chinese (繁體中文)" if re.search(r"[一-鿿]", text) else "English"


def run_agent(chat_history):
    """Answer the last question in chat_history. Returns (answer, trace)."""
    question = chat_history[-1]["content"]
    messages = ([{"role": "system", "content": SYSTEM_PROMPT}]
                + [{"role": m["role"], "content": m["content"]} for m in chat_history[:-1]]
                + [{"role": "system", "content": f"Reply in {reply_language(question)}."}]   # the program picks the language
                + [{"role": "user", "content": question}])
    trace = []

    for step in range(MAX_STEPS):
        message = client.chat.completions.create(
            model=CHAT_MODEL, messages=messages, tools=TOOL_SPECS, temperature=0.2,
        ).choices[0].message

        if not message.tool_calls:                     # plain text: the turn is finished
            return (message.content or "").strip(), trace

        messages.append({"role": "assistant", "content": message.content or "",
                         "tool_calls": [{"id": c.id, "type": "function",
                                         "function": {"name": c.function.name, "arguments": c.function.arguments}}
                                        for c in message.tool_calls]})
        for call in message.tool_calls:
            name = call.function.name
            try:
                arguments = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError:
                arguments = {}
            if name not in TOOL_FUNCTIONS:             # only our own tools may run
                result = {"error": f"Unknown tool '{name}'. Use only the listed tools."}
            else:
                try:
                    result = TOOL_FUNCTIONS[name](**arguments)
                except Exception as error:             # wrong arguments: tell the model, do not crash
                    result = {"error": f"The tool could not run: {error}"}
            trace.append({"step": step + 1, "tool": name, "arguments": arguments, "result": result})
            messages.append({"role": "tool", "tool_call_id": call.id,
                             "content": json.dumps(result, ensure_ascii=False)})

    return f"Sorry, I could not finish that. Please contact our staff: {STAFF_CONTACT}.", trace


# ---------------------------------------------------------------------------------------
# 4. The Gradio page
# ---------------------------------------------------------------------------------------
GREETING = ("Hello! I am Xiao Hai, the online agent of Cijin Seaview Guesthouse. "
            "Ask me about rooms, prices, check-in, getting here or booking.\n\n"
            "您好！我是旗津海景民宿的線上客服小海，房型、價格、入住、交通或訂房都可以問我。")

EMPTY_TRACE = "### Tools used 工具軌跡\n\n(nothing yet)"

SAMPLE_GROUPS = {
    "Information (get_guesthouse_info)": [
        "What time is check-in?",
        "Is there parking?",
        "Can I bring my dog?",
    ],
    "Rooms and services (search_rooms)": [
        "Do you have a room for 4 people?",
        "Is the Seaview Family Suite available?",
        "How much is bike rental?",
    ],
    "Price of a stay (calculate_stay)": [
        "How much are 3 nights in the Family Room, Friday and Saturday included, with breakfast for 4 people for 3 days?",
        "Two nights in a seaview double room on weekdays?",
    ],
    "Hand over, not in the data, not our job": [
        "The room was dirty and I want a refund.",
        "Do you have a swimming pool?",
        "Can you recommend a good hotel in Taipei?",
    ],
}
SAMPLE_GROUPS_ZH = {
    "民宿資訊（get_guesthouse_info）": ["幾點可以入住？", "怎麼從捷運站過去？", "可以刷卡嗎？"],
    "房型與服務（search_rooms）": ["有沒有適合六個人的房間？", "海景房一晚多少錢？", "有機場接送嗎？"],
    "住宿算錢（calculate_stay）": ["家庭房住兩晚，其中一晚是星期六，再加一張床，總共多少錢？",
                             "背包客床位連住五晚多少錢？"],
    "轉真人、沒有資料、不是客服的事": ["我要客訴，櫃台人員態度很差", "你們有提供按摩服務嗎？", "幫我寫一段 Python 程式"],
}


def format_trace(trace):
    """Show the tool calls of the last answer, so the choice of tool is visible."""
    if not trace:
        return "### Tools used 工具軌跡\n\n(no tool was called for this answer)"
    lines = ["### Tools used 工具軌跡", ""]
    for entry in trace:
        lines += [f"**{entry['step']}. {entry['tool']}**", "",
                  f"arguments: `{json.dumps(entry['arguments'], ensure_ascii=False)[:200]}`", "",
                  f"result: `{json.dumps(entry['result'], ensure_ascii=False)[:420]}`", ""]
    return "\n".join(lines)


def start_chat():
    return [{"role": "assistant", "content": GREETING}]


def add_user_message(user_text, chat_history):
    user_text = (user_text or "").strip()
    if not user_text:
        return "", chat_history
    return "", chat_history + [{"role": "user", "content": user_text}]


def answer_question(chat_history):
    if not chat_history or chat_history[-1]["role"] != "user":
        return chat_history, EMPTY_TRACE
    try:
        answer, trace = run_agent(chat_history)
        panel = format_trace(trace)
    except Exception as error:
        answer = f"(system message) API error: {error}"
        panel = "### Tools used 工具軌跡\n\n(the call failed)"
    return chat_history + [{"role": "assistant", "content": answer}], panel


with gr.Blocks(title="Cijin Seaview Guesthouse Agent") as demo:
    gr.Markdown("## 🌊 Cijin Seaview Guesthouse 旗津海景民宿 — AI customer service\n"
                "Every question goes to the agent. Facts and prices come from a SQLite database built from CSV files, "
                "the code calculates the bill, and anything else goes to a person. "
                "The panel on the right shows which tool was used and what it returned.")
    with gr.Row():
        with gr.Column(scale=3):
            # Gradio 5 needs type="messages"; Gradio 6 uses the messages format by default.
            chat_kwargs = {"type": "messages"} if int(gr.__version__.split(".")[0]) < 6 else {}
            chatbot = gr.Chatbot(value=start_chat(), height=460, label="Xiao Hai 小海", **chat_kwargs)
            with gr.Row():
                user_box = gr.Textbox(placeholder="Ask about rooms, prices, check-in ... 請輸入問題",
                                      show_label=False, scale=8)
                send_button = gr.Button("Send", variant="primary", scale=1)
                clear_button = gr.Button("Clear", scale=1)
            gr.Markdown("**Sample questions** (click one, then press Send)")
            with gr.Tabs():
                with gr.Tab("English"):
                    for group, questions in SAMPLE_GROUPS.items():
                        gr.Examples(examples=questions, inputs=user_box, label=group)
                with gr.Tab("中文"):
                    for group, questions in SAMPLE_GROUPS_ZH.items():
                        gr.Examples(examples=questions, inputs=user_box, label=group)
        with gr.Column(scale=2):
            trace_panel = gr.Markdown(EMPTY_TRACE)

    send_button.click(add_user_message, [user_box, chatbot], [user_box, chatbot]).then(
        answer_question, chatbot, [chatbot, trace_panel])
    user_box.submit(add_user_message, [user_box, chatbot], [user_box, chatbot]).then(
        answer_question, chatbot, [chatbot, trace_panel])
    clear_button.click(start_chat, outputs=chatbot).then(
        lambda: ("", EMPTY_TRACE), outputs=[user_box, trace_panel])


if __name__ == "__main__":
    demo.launch(share=os.getenv("GRADIO_SHARE") == "1")

"""A customer service agent for a breakfast shop: Morning Light Breakfast (晨光早餐坊).

Every question goes to the agent first. The agent decides which tool to use:
shop information and the menu come from a SQLite database, the code works out the bill
(add-ons, combo deal, own-cup discount, student discount, delivery fee),
and anything the tools cannot answer is handed to a person.
The panel on the right shows which tools were called and what they returned.

Based on app-gradio.py from Unit G-3 (the fast food agent), with the shop data,
the tools, the rules and the sample questions replaced.

Run it with:  python app.py
"""

import csv
import json
import os
import re
import sqlite3
from pathlib import Path

import gradio as gr
from dotenv import load_dotenv
from openai import OpenAI

HERE = Path(__file__).parent

# ---------------------------------------------------------------------------
# 1. API setup
# ---------------------------------------------------------------------------
# Settings come from the .env file in this folder (copy .env.example to .env first).
load_dotenv(HERE / ".env")
API_KEY = os.getenv("API_KEY")
API_ENDPOINT = os.getenv("LLM_API_URL")
CHAT_MODEL = os.getenv("MODEL_NAME")

client = OpenAI(api_key=API_KEY, base_url=API_ENDPOINT, timeout=60.0)


# ---------------------------------------------------------------------------
# 2. The shop database: two tables, shop_info and menu, built from the CSV files at start-up
# ---------------------------------------------------------------------------
DB_FILE = HERE / "breakfast.db"     # rebuilt every time the app starts, so it is not uploaded to GitHub

# check_same_thread=False because Gradio answers requests on worker threads.
connection = sqlite3.connect(DB_FILE, check_same_thread=False, timeout=10)
connection.row_factory = sqlite3.Row
cursor = connection.cursor()

cursor.execute("DROP TABLE IF EXISTS shop_info")
cursor.execute("""
    CREATE TABLE shop_info (
        topic   TEXT PRIMARY KEY,
        content TEXT NOT NULL
    )
""")
cursor.execute("DROP TABLE IF EXISTS menu")
cursor.execute("""
    CREATE TABLE menu (
        item_id   INTEGER PRIMARY KEY,
        name      TEXT NOT NULL,
        name_zh   TEXT NOT NULL,      -- Chinese name, for questions in Chinese
        category  TEXT NOT NULL,      -- egg_crepe, toast, burger, noodle, snack, drink, add_on
        price     INTEGER NOT NULL,   -- New Taiwan dollars
        available INTEGER NOT NULL    -- 1 = on sale today, 0 = sold out today
    )
""")
with open(HERE / "shop_info.csv", newline="", encoding="utf-8") as f:
    cursor.executemany(
        "INSERT INTO shop_info VALUES (?, ?)",
        [(r["topic"], r["content"]) for r in csv.DictReader(f)],
    )
with open(HERE / "menu.csv", newline="", encoding="utf-8") as f:
    cursor.executemany(
        "INSERT INTO menu VALUES (?, ?, ?, ?, ?, ?)",
        [(int(r["item_id"]), r["name"], r["name_zh"], r["category"], int(r["price"]), int(r["available"]))
         for r in csv.DictReader(f)],
    )
connection.commit()

SHOP_TOPICS = [r["topic"] for r in cursor.execute("SELECT topic FROM shop_info ORDER BY rowid")]
MENU_SIZE = cursor.execute("SELECT COUNT(*) AS n FROM menu").fetchone()["n"]
print(f"Ready: {len(SHOP_TOPICS)} shop topics, {MENU_SIZE} menu items.")


# ---------------------------------------------------------------------------
# 3. The pricing rules used by calculate_order
#    They must match the text in shop_info.csv (combo, own_cup, student_discount, delivery).
# ---------------------------------------------------------------------------
MAIN_CATEGORIES = {"egg_crepe", "toast", "burger", "noodle"}   # a "main item" for the combo deal
ADD_ON_ALLOWED = {"egg_crepe", "toast", "burger"}              # add-ons only go on these
COMBO_DISCOUNT = 10          # NT$ off for each main item + drink pair
OWN_CUP_DISCOUNT = 5         # NT$ off for each drink in the customer's own cup
STUDENT_DISCOUNT = 10        # NT$ off once per order ...
STUDENT_MINIMUM = 80         # ... if the subtotal is at least this
DELIVERY_MINIMUM = 150       # smallest order we deliver
DELIVERY_FEE = 30            # delivery fee ...
FREE_DELIVERY_FROM = 300     # ... free from this subtotal (before discounts)


def staff_phone():
    """The shop phone, read from the database (shop_info, topic 'phone'), so it is written in one place only.
    Change it in shop_info.csv and restart the app; no code changes are needed."""
    row = cursor.execute("SELECT content FROM shop_info WHERE topic = 'phone'").fetchone()
    if row is None:
        return "the shop counter"
    match = re.search(r"\d[\d-]{6,}\d", row["content"])     # e.g. 07-123-4567
    return match.group(0) if match else row["content"]


# ---------------------------------------------------------------------------
# 4. The tools, as plain functions
#    Four rules: return data (not sentences), say clearly when nothing is found,
#    do the maths in code, and never let the model write SQL.
# ---------------------------------------------------------------------------
def get_shop_info(topic):
    """Return one piece of shop information, such as the opening hours or the delivery rules."""
    row = cursor.execute(
        "SELECT topic, content FROM shop_info WHERE topic = ? COLLATE NOCASE",
        (topic.strip(),),
    ).fetchone()
    if row is None:
        return {"found": False, "note": f"There is no shop information about '{topic}'.",
                "topics_available": SHOP_TOPICS}
    return {"found": True, "topic": row["topic"], "content": row["content"]}


ALIASES = {"hashbrown": "hash brown", "coffee": "americano", "oj": "orange juice",
           "nugget": "chicken nugget", "pancake": "crepe"}      # what customers say -> words in the menu names
CATEGORY_ZH = {"蛋餅": "egg_crepe", "吐司": "toast", "漢堡": "burger", "鐵板麵": "noodle",
               "點心": "snack", "飲料": "drink", "加料": "add_on"}
SKIP_ZH = set("杯份個的客一")                             # measure words: "大杯奶茶" = "大奶茶"


def words(text):
    """'2 Large Milk Teas!' -> {'2', 'large', 'milk', 'tea'};  '大杯奶茶' -> {'大', '奶', '茶'}.

    English: lower case, no punctuation, no plural s.  Chinese: each character is one word.
    """
    result = set()
    for word in re.sub(r"[^a-z0-9]+", " ", text.lower()).split():
        if len(word) > 3 and word.endswith("s"):
            word = word[:-1]                              # teas -> tea, fries -> frie
        result.update(ALIASES.get(word, word).split())
    result.update(ch for ch in text if "一" <= ch <= "鿿" and ch not in SKIP_ZH)
    return result


def find_items(text, categories=None):
    """Menu rows that match the text: the English name, the Chinese name, or the category.

    Customers type "large milk tea" or "大杯奶茶", not "Milk Tea large", so we compare words.
    `categories` limits the search, for example to {"add_on"}.
    """
    text = CATEGORY_ZH.get(text.strip(), text)            # 飲料 -> drink
    wanted = words(text)
    if not wanted:
        return []
    rows = cursor.execute("SELECT name, name_zh, category, price, available FROM menu").fetchall()
    if categories:
        rows = [r for r in rows if r["category"] in categories]
    exact = [r for r in rows if text.strip().lower() in (r["name"].lower(), r["name_zh"])]
    if exact:
        return exact
    return [r for r in rows
            if wanted <= words(r["name"]) | words(r["name_zh"]) or wanted == words(r["category"])]


def item_dict(r):
    return {"name": r["name"], "name_zh": r["name_zh"], "category": r["category"],
            "price": r["price"], "available": bool(r["available"])}


def search_menu(keyword):
    """Find menu items by part of a name ('milk tea', '蛋餅') or by category ('drink', '飲料')."""
    rows = find_items(keyword)
    if not rows:
        return {"found": 0, "items": [], "note": f"No menu item matches '{keyword}'."}
    return {"found": len(rows), "items": [item_dict(r) for r in rows]}


def get_price(item_name):
    """Return the price of ONE item, and whether it is on sale today."""
    rows = find_items(item_name)
    if len(rows) == 1:
        return {"found": True, **item_dict(rows[0])}
    if len(rows) > 1:
        return {"found": False, "note": f"'{item_name}' matches several items. Ask the customer which one.",
                "candidates": [f"{r['name']} ({r['name_zh']}) NT${r['price']}" for r in rows]}
    return {"found": False, "note": f"'{item_name}' is not on the menu."}


def calculate_order(items, order_type="takeout", student=False, own_cup=False):
    """Work out the bill of an order, with all the shop's pricing rules.

    items = [{"name": "bacon egg crepe", "quantity": 2, "add_ons": ["cheese"]}, ...]
    The prices come from the database and the arithmetic happens here, so a wrong bill
    cannot come from the model. No total unless every item is clear.
    """
    lines, problems = [], []
    mains = drinks = 0
    subtotal = 0

    for entry in items:
        name = str(entry.get("name", "")).strip()
        quantity = int(entry.get("quantity", 1) or 1)
        found = get_price(name)
        if not found["found"]:
            problems.append({"item": name, **found})              # not on the menu, or several matches
            continue
        if not found["available"]:
            problems.append({"item": name, "note": f"{found['name']} ({found['name_zh']}) is sold out today."})
            continue

        unit = found["price"]
        extras = []
        for add_on in entry.get("add_ons") or []:
            match = find_items(add_on, categories={"add_on"})
            if len(match) != 1:
                problems.append({"item": f"{name} + {add_on}",
                                 "note": f"'{add_on}' is not an add-on. Add-ons: egg, cheese, bacon."})
                continue
            if found["category"] not in ADD_ON_ALLOWED:
                problems.append({"item": f"{name} + {add_on}",
                                 "note": "Add-ons only go on egg crepes, toast and burgers."})
                continue
            extras.append({"name": match[0]["name"], "name_zh": match[0]["name_zh"], "price": match[0]["price"]})
            unit += match[0]["price"]

        line_total = unit * quantity
        lines.append({"name": found["name"], "name_zh": found["name_zh"], "add_ons": extras,
                      "unit_price": unit, "quantity": quantity, "line_total": line_total})
        subtotal += line_total
        if found["category"] in MAIN_CATEGORIES:
            mains += quantity
        if found["category"] == "drink":
            drinks += quantity

    if problems:
        return {"total": None, "problems": problems,
                "note": "No total yet. Ask the customer about these items, then call calculate_order again."}
    if not lines:
        return {"total": None, "note": "The order is empty. Ask the customer what they would like."}

    adjustments = []
    combo_pairs = min(mains, drinks)
    if combo_pairs:
        adjustments.append({"rule": f"combo deal: {combo_pairs} main + drink pair(s)",
                            "amount": -COMBO_DISCOUNT * combo_pairs})
    if own_cup and drinks:
        adjustments.append({"rule": f"own cup: {drinks} drink(s)", "amount": -OWN_CUP_DISCOUNT * drinks})
    if student:
        if subtotal >= STUDENT_MINIMUM:
            adjustments.append({"rule": "student discount", "amount": -STUDENT_DISCOUNT})
        else:
            adjustments.append({"rule": f"student discount not applied: subtotal under NT${STUDENT_MINIMUM}",
                                "amount": 0})

    if order_type == "delivery":
        if subtotal < DELIVERY_MINIMUM:
            return {"total": None, "subtotal": subtotal,
                    "note": (f"Delivery needs a subtotal of at least NT${DELIVERY_MINIMUM}. "
                             f"This order is NT${subtotal}. Ask if they want to add items or pick it up.")}
        fee = 0 if subtotal >= FREE_DELIVERY_FROM else DELIVERY_FEE
        adjustments.append({"rule": "delivery fee" + (" (free from NT$300)" if fee == 0 else ""),
                            "amount": fee})

    total = subtotal + sum(a["amount"] for a in adjustments)
    return {"order_type": order_type, "lines": lines, "subtotal": subtotal,
            "adjustments": adjustments, "total": total}


def transfer_to_human():
    """Hand the conversation to a person."""
    phone = get_shop_info("phone")
    return {"message": f"Please call our staff at {staff_phone()}, or ask at the counter.",
            "phone_info": phone.get("content", ""),
            "note": ("The hand-over is done. First tell the customer in one short sentence why: "
                     "you do not have that information, or the request needs a person. "
                     "Then give this message in the customer's language. Do not say that you will connect them "
                     "or pass them on: you cannot. Do not call this tool again.")}


TOOL_FUNCTIONS = {
    "get_shop_info": get_shop_info,
    "search_menu": search_menu,
    "get_price": get_price,
    "calculate_order": calculate_order,
    "transfer_to_human": transfer_to_human,
}

# What the model sees: names, purpose, arguments.
TOOL_SPECS = [
    {"type": "function", "function": {
        "name": "get_shop_info",
        "description": ("Get one piece of shop information: opening hours (營業時間), last order (最後點餐), "
                        "address (地址), phone (電話), payment (付款方式), dine-in and seats (內用), "
                        "pre-order (預訂), delivery (外送), combo deal (套餐), student discount (學生優惠), "
                        "own cup (自備杯), add-ons (加料), the general list of allergens used in the kitchen (過敏原) or parking (停車)."),
        "parameters": {"type": "object",
                       "properties": {"topic": {"type": "string", "enum": SHOP_TOPICS}},
                       "required": ["topic"]}}},
    {"type": "function", "function": {
        "name": "search_menu",
        "description": ("Search the menu by keyword or category (egg_crepe 蛋餅, toast 吐司, burger 漢堡, "
                        "noodle 鐵板麵, snack 點心, drink 飲料, add_on 加料). Use it when the customer asks "
                        "what is available, or names an item only partly, such as 'milk tea' or '蛋餅'."),
        "parameters": {"type": "object",
                       "properties": {"keyword": {"type": "string"}},
                       "required": ["keyword"]}}},
    {"type": "function", "function": {
        "name": "get_price",
        "description": ("Get the price of ONE item, and whether it is on sale today. "
                        "Use the customer's own words and do not translate them, for example "
                        "'large milk tea' or '大杯奶茶'."),
        "parameters": {"type": "object",
                       "properties": {"item_name": {"type": "string"}},
                       "required": ["item_name"]}}},
    {"type": "function", "function": {
        "name": "calculate_order",
        "description": ("Work out the bill of an order: add-ons, the combo deal, the own-cup discount, "
                        "the student discount and the delivery fee. Always use this for any total, "
                        "even for one item. Never add or subtract prices yourself."),
        "parameters": {"type": "object",
                       "properties": {
                           "items": {"type": "array", "items": {
                               "type": "object",
                               "properties": {
                                   "name": {"type": "string",
                                            "description": "The item in the customer's own words. Do not translate it."},
                                   "quantity": {"type": "integer"},
                                   "add_ons": {"type": "array", "items": {"type": "string"},
                                               "description": "Extras on this item, e.g. ['egg', 'cheese'] or ['加蛋']."}},
                               "required": ["name", "quantity"]}},
                           "order_type": {"type": "string", "enum": ["dine_in", "takeout", "delivery"],
                                          "description": "Use 'takeout' if the customer did not say."},
                           "student": {"type": "boolean",
                                       "description": "True only if the customer says they are a student."},
                           "own_cup": {"type": "boolean",
                                       "description": "True only if the customer brings their own cup."}},
                       "required": ["items"]}}},
    {"type": "function", "function": {
        "name": "transfer_to_human",
        "description": ("Use this for a complaint, a request for the owner or a manager, or a shop question "
                        "that no other tool can answer. ALSO use it whenever a customer says they (or someone) "
                        "have an allergy and asks whether they can eat an item: call get_shop_info('allergy') "
                        "first, then this tool, because only our staff can check the ingredients."),
        "parameters": {"type": "object", "properties": {}, "required": []}}},
]

SYSTEM_PROMPT = f"""
You are the customer service agent of Morning Light Breakfast (晨光早餐坊), a breakfast shop near the
NKUST First Campus in Kaohsiung. All prices are in New Taiwan dollars. Write them like NT$45.

RULES
1. Answer only from tool results. Never answer from memory, and never invent anything.
2. If a tool reports that nothing was found or an item is sold out, say so plainly. Do not guess.
3. Shop questions (hours, address, payment, delivery, discounts, add-ons, parking) -> get_shop_info.
4. Menu questions (items, prices, what is on sale today) -> search_menu or get_price.
5. Use calculate_order for every total, even for one item. Never add or subtract prices yourself.
   If calculate_order returns no total, ask the customer the question it needs.
6. Complaints, a request for the owner, or shop questions no tool can answer -> transfer_to_human.
   A customer with an allergy asks if they can eat an item -> get_shop_info('allergy'), THEN transfer_to_human.
   Never say a food is safe for someone's allergy.
7. Only talk about this shop. Politely refuse other topics, such as the weather, homework or tickets.
8. Answer in the customer's language: English, or Traditional Chinese if the customer writes in Chinese.
   Write product names in the language of the answer: English names in English, Chinese names (name_zh) in Chinese.
   Be friendly, and use three sentences or less; a bill may use a short list.
"""

MAX_STEPS = 6          # a confused model must not loop forever


def language_note(text):
    """The program, not the model, decides the reply language: Chinese characters -> Chinese, else English."""
    if re.search(r"[一-鿿]", text):
        return [{"role": "system", "content": "The customer wrote in Chinese. Reply in Traditional Chinese."}]
    return [{"role": "system", "content": "The customer wrote in English. Reply in English."}]


def to_text(content):
    """Gradio 6 stores a message as a list of parts, e.g. [{"type": "text", "text": "hi"}].
    The LLM API wants plain text, so we join the text parts."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")
    return str(content)


def clean_history(chat_history):
    """Keep only role + plain-text content (Gradio 6 also adds keys such as metadata and options)."""
    return [{"role": m["role"], "content": to_text(m["content"])} for m in chat_history]


def assistant_message(message):
    """The model's tool request, ready to go back into `messages` in the next round.

    We keep the whole message as the API returned it (model_dump), instead of rebuilding it.
    Gemini 3 "thinking" models attach a thought_signature to each tool call
    (tool_calls[i].extra_content.google.thought_signature) and reject the next request
    with error 400 if it is missing.
    """
    data = message.model_dump(exclude_none=True)
    data["role"] = "assistant"
    data["content"] = data.get("content") or ""
    return data


def run_agent(chat_history):
    """Answer the last question in chat_history. Returns (answer, trace)."""
    chat_history = clean_history(chat_history)
    messages = ([{"role": "system", "content": SYSTEM_PROMPT}] + chat_history[:-1]
                + language_note(chat_history[-1]["content"]) + chat_history[-1:])
    trace = []

    for step in range(MAX_STEPS):                      # one step = one API call
        response = client.chat.completions.create(
            model=CHAT_MODEL, messages=messages, tools=TOOL_SPECS, temperature=0.2,
        )
        message = response.choices[0].message

        if not message.tool_calls:                     # plain text: the turn is finished
            return (message.content or "").strip(), trace

        messages.append(assistant_message(message))     # keep the request as it came (with Gemini's thought_signature)

        for call in message.tool_calls:
            name = call.function.name
            try:
                arguments = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError:
                arguments = {}
            function = TOOL_FUNCTIONS.get(name)        # only our own tools are allowed

            if function is None:
                result = {"error": f"unknown tool: {name}"}
            else:
                try:
                    result = function(**arguments)
                except Exception as error:             # bad arguments must not crash the app
                    result = {"error": f"{type(error).__name__}: {error}"}

            trace.append({"step": step + 1, "tool": name,
                          "arguments": arguments, "result": result})
            messages.append({"role": "tool", "tool_call_id": call.id,
                             "content": json.dumps(result, ensure_ascii=False)})

    return f"Sorry, I could not finish that. Please call our staff at {staff_phone()}.", trace


# ---------------------------------------------------------------------------
# 5. The Gradio page
# ---------------------------------------------------------------------------
GREETING = ("Good morning! I am the online agent of Morning Light Breakfast. "
            "Ask me about our hours, the menu, prices, delivery or your order total.\n\n"
            "早安！我是晨光早餐坊的線上客服，營業時間、菜單、價格、外送或點餐金額都可以問我，中文或英文都可以。")

EMPTY_TRACE = "### Tools used\n\n(nothing yet)"

SAMPLE_GROUPS_ZH = {
    "店家資訊（get_shop_info）": [
        "你們幾點開門？星期天有開嗎？",
        "可以刷卡嗎？",
        "有外送嗎？運費怎麼算？",
        "學生有優惠嗎？",
        "自備杯有折扣嗎？",
    ],
    "菜單與價格（search_menu、get_price）": [
        "有哪些蛋餅？",
        "大杯奶茶多少錢？",
        "紅茶多少錢？",                       # two sizes: the agent should ask which one
        "鱈魚堡今天有賣嗎？",                 # on the menu, but sold out today
        "有咖啡嗎？",
    ],
    "點餐算錢（calculate_order）": [
        "兩個培根蛋餅加起司、兩杯大杯奶茶，外帶，我是學生，總共多少？",
        "我要外送：三個卡拉雞腿堡、三杯大杯紅茶、兩份薯條，多少錢？",
        "一個豬肉堡和一杯中杯豆漿，我有帶自己的杯子，內用多少錢？",
        "一份薯餅外送多少錢？",               # under the delivery minimum
        "一個鮪魚蛋餅加一杯奶茶多少錢？",     # milk tea has two sizes: ask first
    ],
    "客訴、過敏、菜單沒有、資料沒有、不是客服的事": [
        "我的蛋餅裡面有頭髮，我要找老闆。",
        "我對花生過敏，可以吃肉鬆吐司嗎？",
        "有賣飯糰嗎？",                       # not on the menu: the answer is "no"
        "老闆叫什麼名字？",                   # not in the data
        "明天高雄會下雨嗎？",
        "可以幫我寫期中報告嗎？",
    ],
    "多輪對話：依序點選，中間不要按 Clear": [
        "卡拉雞腿堡多少錢？",
        "那兩個再加兩杯大杯奶茶呢？",
        "好，那你們最晚幾點可以點餐？",
    ],
}

SAMPLE_GROUPS = {
    "Shop information (get_shop_info)": [
        "What time do you open? Are you open on Sunday?",
        "Can I pay by credit card?",
        "Do you deliver? How much is the delivery fee?",
    ],
    "Menu and prices (search_menu, get_price)": [
        "What drinks do you have?",
        "How much is a large milk tea?",
        "Is the fish burger available today?",
    ],
    "The bill (calculate_order)": [
        "Two bacon egg crepes with extra cheese and two large milk teas, takeout. I'm a student. How much?",
        "Delivery please: one hash brown. How much?",
    ],
    "Not in the data, not our job": [
        "I'm allergic to peanuts. Is the pork floss toast safe for me?",
        "Do you sell rice balls?",
        "What is the weather in Kaohsiung tomorrow?",
    ],
}


def format_trace(trace):
    """Show the tool calls of the last answer, so the choice is visible."""
    if not trace:
        return "### Tools used\n\n(no tool was needed for this answer)"
    lines = ["### Tools used", ""]
    for entry in trace:
        lines.append(f"**{entry['step']}. {entry['tool']}**")
        lines.append("")
        lines.append(f"arguments: `{json.dumps(entry['arguments'], ensure_ascii=False)[:200]}`")
        lines.append("")
        lines.append(f"result: `{json.dumps(entry['result'], ensure_ascii=False)[:400]}`")
        lines.append("")
    return "\n".join(lines)


def start_chat():
    """The first state of the chat box: the greeting only."""
    return [{"role": "assistant", "content": GREETING}]


def add_user_message(user_text, chat_history):
    """Show the question at once and empty the input box."""
    user_text = user_text.strip()
    if not user_text:
        return "", chat_history
    return "", chat_history + [{"role": "user", "content": user_text}]


def answer_question(chat_history):
    """Run the agent on the current conversation."""
    if not chat_history or chat_history[-1]["role"] != "user":
        return chat_history, EMPTY_TRACE
    try:
        answer, trace = run_agent(chat_history)
        panel = format_trace(trace)
    except Exception as error:
        answer = f"(system message) API error: {error}"
        panel = "### Tools used\n\n(the call failed)"
    return chat_history + [{"role": "assistant", "content": answer}], panel


with gr.Blocks(title="Morning Light Breakfast Agent") as demo:
    gr.Markdown(
        "## 🍳 晨光早餐坊 線上客服｜Morning Light Breakfast\n"
        "Every question goes to the agent. Shop information and prices come from a SQLite database, "
        "the code works out the bill, and anything else goes to a person. "
        "The panel on the right shows which tool was used."
    )

    with gr.Row():
        with gr.Column(scale=3):
            chatbot = gr.Chatbot(value=start_chat(), height=430, label="Agent")
            with gr.Row():
                user_box = gr.Textbox(placeholder="問營業時間、菜單、價格或點餐金額… / Ask about the shop or the menu",
                                      show_label=False, scale=8)
                send_button = gr.Button("Send", variant="primary", scale=1)
                clear_button = gr.Button("Clear", scale=1)

            gr.Markdown("**範例問題 Sample questions**（點一題，再按 Send）")
            with gr.Tabs():
                with gr.Tab("中文"):
                    for group, questions in SAMPLE_GROUPS_ZH.items():
                        gr.Examples(examples=questions, inputs=user_box, label=group)
                with gr.Tab("English"):
                    for group, questions in SAMPLE_GROUPS.items():
                        gr.Examples(examples=questions, inputs=user_box, label=group)

        with gr.Column(scale=2):
            trace_panel = gr.Markdown(EMPTY_TRACE)

    # One turn: show the question, then let the agent work.
    for trigger in (send_button.click, user_box.submit):
        trigger(add_user_message, [user_box, chatbot], [user_box, chatbot]).then(
            answer_question, chatbot, [chatbot, trace_panel]
        )

    # Clearing brings the greeting back and empties the trace panel.
    clear_button.click(start_chat, outputs=chatbot).then(
        lambda: ("", EMPTY_TRACE), outputs=[user_box, trace_panel]
    )


if __name__ == "__main__":
    demo.launch()

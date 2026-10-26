"""Test the breakfast agent: does it choose the right tool?

Each test is a question and the tool(s) we would accept. The script asks every question as a
new conversation, reads the tool trace, and reports the score. It also saves the full results
(answer + tools used) to test_results.md, for the report.

Run it with:  python test_agent.py
Free Gemini keys allow only about 15 calls per minute, and one tool question needs at least
2 calls, so the script waits between questions (WAIT_SECONDS).
"""

import json
import time
from datetime import datetime

from app import CHAT_MODEL, run_agent

WAIT_SECONDS = 9

# (kind, question, accepted tools). An empty set means "no tool is needed" (not our job).
TESTS = [
    ("in the data",  "你們幾點開門？星期天有開嗎？",                     {"get_shop_info"}),
    ("in the data",  "可以刷卡嗎？",                                     {"get_shop_info"}),
    ("in the data",  "Do you deliver? How much is the delivery fee?",    {"get_shop_info"}),
    ("in the data",  "學生有優惠嗎？",                                   {"get_shop_info"}),
    ("in the data",  "有哪些蛋餅？",                                     {"search_menu"}),
    ("in the data",  "大杯奶茶多少錢？",                                 {"get_price", "search_menu"}),
    ("in the data",  "鱈魚堡今天有賣嗎？",                               {"get_price", "search_menu"}),
    ("calculation",  "兩個培根蛋餅加起司、兩杯大杯奶茶，外帶，我是學生，總共多少？", {"calculate_order"}),
    ("calculation",  "我要外送：三個卡拉雞腿堡、三杯大杯紅茶、兩份薯條，多少錢？",   {"calculate_order"}),
    ("calculation",  "一個豬肉堡和一杯中杯豆漿，我有帶自己的杯子，內用多少錢？",     {"calculate_order"}),
    ("not in data",  "我對花生過敏，可以吃肉鬆吐司嗎？",                 {"transfer_to_human"}),
    ("not in data",  "老闆叫什麼名字？",                                 {"transfer_to_human"}),
    ("not in data",  "我的蛋餅裡面有頭髮，我要找老闆。",                 {"transfer_to_human"}),
    ("not our job",  "明天高雄會下雨嗎？",                               set()),
    ("not our job",  "可以幫我寫期中報告嗎？",                           set()),
]


def main():
    rows, correct = [], 0
    for number, (kind, question, accepted) in enumerate(TESTS, start=1):
        try:
            answer, trace = run_agent([{"role": "user", "content": question}])
        except Exception as error:                      # e.g. 429 too many requests
            answer, trace = f"(API error) {error}", []
        used = [step["tool"] for step in trace]
        ok = (not used) if not accepted else bool(accepted & set(used))
        correct += ok
        print(f"{'OK  ' if ok else 'MISS'} {number:2d}. [{kind}] {question[:34]:34s} "
              f"accepted {'/'.join(sorted(accepted)) or '(no tool)':30s} used {used}")
        rows.append((number, kind, question, accepted, used, ok, answer, trace))
        time.sleep(WAIT_SECONDS)

    score = f"{correct}/{len(TESTS)} = {correct / len(TESTS):.0%}"
    print(f"\ntool choice = {score}")

    with open("test_results.md", "w", encoding="utf-8") as f:
        f.write(f"# Tool-choice test results\n\nModel: {CHAT_MODEL}  \nTime: {datetime.now():%Y-%m-%d %H:%M}  \n"
                f"Score: **{score}**\n\n")
        f.write("| # | Kind | Question | Accepted | Used | OK |\n|---|---|---|---|---|---|\n")
        for number, kind, question, accepted, used, ok, _, _ in rows:
            f.write(f"| {number} | {kind} | {question} | {', '.join(sorted(accepted)) or '(no tool)'} | "
                    f"{', '.join(used) or '(none)'} | {'✅' if ok else '❌'} |\n")
        f.write("\n## Answers and tool traces\n")
        for number, kind, question, accepted, used, ok, answer, trace in rows:
            f.write(f"\n### {number}. {question}\n\n**Answer:** {answer}\n\n")
            for step in trace:
                f.write(f"- `{step['tool']}` {json.dumps(step['arguments'], ensure_ascii=False)} → "
                        f"`{json.dumps(step['result'], ensure_ascii=False)[:300]}`\n")
    print("Saved: test_results.md")


if __name__ == "__main__":
    main()

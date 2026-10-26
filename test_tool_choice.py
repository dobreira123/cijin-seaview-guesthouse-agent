"""Tool-choice test (like Step 9 of the Unit G-3 notebook).

For each question we write the tool(s) we accept. "none" means the agent should answer
without any tool (a polite refusal). Run it with:  python test_tool_choice.py
"""
import sys
import time

from app import run_agent

TESTS = [
    # (question, accepted tools)
    ("What time is check-in?",                                   {"get_guesthouse_info"}),
    ("請問有停車場嗎？",                                            {"get_guesthouse_info"}),
    ("How do I get there from the MRT?",                         {"get_guesthouse_info"}),
    ("取消訂房可以退訂金嗎？",                                        {"get_guesthouse_info"}),
    ("Do you have a room for 6 people?",                         {"search_rooms"}),
    ("海景雙人房一晚多少錢？",                                        {"search_rooms", "calculate_stay"}),
    ("Is the Seaview Family Suite available?",                   {"search_rooms"}),
    ("How much are 3 nights in the Twin Room with 2 breakfasts each day?", {"calculate_stay"}),
    ("家庭房住兩晚，其中一晚是星期六，總共多少錢？",                         {"calculate_stay"}),
    ("The air conditioner was broken and I want a refund.",      {"transfer_to_human"}),
    ("我想找經理談",                                                {"transfer_to_human"}),
    ("Do you have a swimming pool?",                             {"get_guesthouse_info", "search_rooms", "transfer_to_human"}),
    ("What is the weather in Kaohsiung tomorrow?",               {"none"}),
    ("幫我寫一個 Python 的排序程式",                                   {"none"}),
]

SLEEP_SECONDS = float(sys.argv[1]) if len(sys.argv) > 1 else 0   # free API plans: use e.g. 5


def main():
    passed = 0
    print(f"{'#':>2}  {'result':6}  {'tools called':38}  question")
    print("-" * 100)
    for i, (question, accepted) in enumerate(TESTS, start=1):
        _, trace = run_agent([{"role": "user", "content": question}])
        called = [t["tool"] for t in trace] or ["none"]
        ok = bool(set(called) & accepted)
        passed += ok
        print(f"{i:>2}  {'PASS' if ok else 'MISS':6}  {', '.join(called)[:38]:38}  {question}"
              + ("" if ok else f"   (accepted: {', '.join(sorted(accepted))})"))
        time.sleep(SLEEP_SECONDS)
    print("-" * 100)
    print(f"Score: {passed}/{len(TESTS)} = {passed / len(TESTS):.0%}")


if __name__ == "__main__":
    main()

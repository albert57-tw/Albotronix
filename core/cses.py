"""CSES Problem Set daily problem: the problem list, task statements and study notes.

Problems go in CSES's own order (it is a curriculum: easy to hard, topic by topic), one a day from the
first day the bot posted. Study notes come from Gemini when GEMINI_API_KEY is set (cached per task),
otherwise from the built-in notes for the problem's category.
"""
import datetime
import html
import json
import logging
import re
import time
from dataclasses import dataclass

import config
from core import db, http

log = logging.getLogger(__name__)

BASE = 'https://cses.fi'
BOOK_URL = 'https://cses.fi/book/book.pdf'
SECTION = re.compile(r'<h2>([^<]+)</h2><ul class="task-list">(.*?)</ul>', re.S)
TASK = re.compile(r'<a href="/problemset/task/(\d+)">([^<]+)</a><span class="detail">(\d+) / (\d+)</span>')
LIMIT = re.compile(r'<b>(Time|Memory) limit:</b>\s*([^<]+)</li>')
STATEMENT = re.compile(r'<div class="md">(.*?)</div>', re.S)

# Category -> (Chinese name, what it teaches, chapters of the Competitive Programmer's Handbook)
CATEGORIES = {
    'Introductory Problems': ('入門題', '模擬、數學觀察、完全搜尋（遞迴、回溯），練熟輸入輸出與時間複雜度的估算。',
                              '第 1–5 章（基礎、時間複雜度、排序、資料結構、完全搜尋）'),
    'Sorting and Searching': ('排序與搜尋', '排序後的觀察、二分搜、雙指標、貪心，以及 set／map／priority_queue 的使用。',
                              '第 3、4、6、8 章（排序、資料結構、貪心、均攤分析）'),
    'Dynamic Programming': ('動態規劃', '定義狀態、找出轉移、決定計算順序；從遞迴＋記憶化開始想最直覺。',
                            '第 7 章（動態規劃）'),
    'Graph Algorithms': ('圖論', 'BFS／DFS、最短路徑、最小生成樹、拓撲排序、強連通分量、網路流。',
                         '第 11–13、15–17、20 章'),
    'Range Queries': ('區間查詢', '前綴和、差分、Fenwick tree、線段樹、稀疏表，處理大量區間查詢與修改。',
                      '第 9、28 章（區間查詢、線段樹進階）'),
    'Tree Algorithms': ('樹', '樹的走訪、子樹大小、直徑、樹 DP、LCA、樹上路徑查詢。',
                        '第 14、18 章（樹的演算法、樹上查詢）'),
    'Mathematics': ('數學', '數論（質數、模運算、快速冪）、組合計數、矩陣、機率與博弈。',
                    '第 21–25 章'),
    'String Algorithms': ('字串', '字串雜湊、Z 演算法、KMP、Trie、後綴陣列。', '第 26 章（字串演算法）'),
    'Geometry': ('計算幾何', '向量與外積、線段相交、多邊形面積、凸包，注意整數運算避免浮點誤差。',
                 '第 29、30 章（幾何、掃描線）'),
    'Advanced Techniques': ('進階技巧', '折半搜尋、bitset、平方分割、分治與各種 DP 優化。', '第 27 章（平方根演算法）等'),
    'Sliding Window Problems': ('滑動視窗', '固定或可變長度的視窗、單調佇列、雙指標維護區間資訊。', '第 8 章（均攤分析）'),
    'Interactive Problems': ('互動題', '用有限的詢問次數取得資訊：二分搜、分治、每次詢問都要盡量排除一半的可能。', None),
    'Bitwise Operations': ('位元運算', 'AND／OR／XOR 的性質、逐位元考慮、位元枚舉子集合。', '第 10 章（位元操作）'),
    'Construction Problems': ('構造題', '先從小例子找規律，再證明構造方法一定可行；想清楚什麼情況無解。', None),
    'Advanced Graph Problems': ('進階圖論', '強連通、2-SAT、歐拉路徑、匹配與網路流的進階應用。', '第 16–20 章'),
    'Counting Problems': ('計數', '組合數、排容原理、DP 計數，注意取模。', '第 22 章（組合數學）'),
    'Additional Problems I': ('綜合題 I', '綜合前面各章的技巧，先判斷題目屬於哪一類。', None),
    'Additional Problems II': ('綜合題 II', '綜合前面各章的技巧，難度較高。', None),
}

LESSON_PROMPT = '''你是一位競程教練，要幫台灣的大學生用 CSES 題目學演算法。以下是 CSES 分類「{category}」的題目「{name}」：

{statement}

請用繁體中文（台灣用語）寫一份教學筆記，只輸出 JSON，格式如下：
{{"summary": "題目大意，2–3 句，說清楚輸入、要求什麼",
  "concepts": ["會用到的演算法或觀念名稱", "..."],
  "why": "為什麼會用到這些觀念，1–2 句",
  "hints": ["提示一：思考方向，不要說出解法", "提示二：更具體的做法，但不給完整程式碼"],
  "complexity": "目標時間複雜度，例如 O(n log n)，並用一句話說明為什麼這樣夠快",
  "pitfalls": ["常見陷阱，例如溢位、邊界、超時", "..."]}}
不要給完整程式碼或完整解答。'''


@dataclass
class Task:
    id: str
    name: str
    category: str
    position: int  # 1-based, within its category
    category_size: int
    solved: int
    attempted: int

    @property
    def url(self) -> str:
        return f'{BASE}/problemset/task/{self.id}'

    @property
    def success_rate(self) -> float | None:
        return self.solved / self.attempted * 100 if self.attempted else None


_tasks: tuple[float, list[Task]] = (0, [])


async def tasks() -> list[Task]:
    global _tasks
    fetched, cached = _tasks
    if time.time() - fetched < 86400 and cached:
        return cached
    page = await http.get_text(f'{BASE}/problemset/')
    result = []
    for category, body in SECTION.findall(page):
        found = TASK.findall(body)
        result += [Task(tid, html.unescape(name), category, i, len(found), int(solved), int(attempted))
                   for i, (tid, name, solved, attempted) in enumerate(found, 1)]
    if not result:
        raise ValueError('CSES page layout changed: no tasks found')
    _tasks = (time.time(), result)
    return result


async def today_task() -> tuple[Task, int]:
    """Today's task and how many days in we are (1 = the first day)."""
    today = datetime.datetime.now(config.TZ).date()
    start = await db.get_kv('cses_start')
    if not start:
        start = today.isoformat()
        await db.set_kv('cses_start', start)
    day = (today - datetime.date.fromisoformat(start)).days
    all_tasks = await tasks()
    return all_tasks[day % len(all_tasks)], day + 1


def _plain(fragment: str) -> str:
    """Statement HTML -> plain text (math spans keep their TeX, headings become lines)."""
    text = re.sub(r'<h1[^>]*>(.*?)</h1>', r'\n\1:\n', fragment)
    text = re.sub(r'<li>', '- ', text)
    text = re.sub(r'<[^>]+>', '', text)
    return re.sub(r'\n{3,}', '\n\n', html.unescape(text)).strip()


async def statement(task: Task) -> tuple[str, dict[str, str]]:
    """Plain-text statement and {'Time': '1.00 s', 'Memory': '512 MB'}."""
    page = await http.get_text(task.url)
    body = STATEMENT.search(page)
    return (_plain(body.group(1)) if body else ''), {kind: value.strip() for kind, value in LIMIT.findall(page)}


async def lesson(task: Task, text: str) -> dict | None:
    """Gemini's study notes for a task (cached in the DB), or None without a key or on failure."""
    cached = await db.get_kv(f'cses_lesson:{task.id}')
    if cached:
        return json.loads(cached)
    if not config.GEMINI_API_KEY or not text:
        return None
    try:
        from google import genai
        from google.genai import types
        client = genai.Client(api_key=config.GEMINI_API_KEY)
        response = await client.aio.models.generate_content(
            model=config.GEMINI_MODEL,
            contents=LESSON_PROMPT.format(category=task.category, name=task.name, statement=text[:4000]),
            config=types.GenerateContentConfig(response_mime_type='application/json'))
        notes = json.loads(response.text)
        if not isinstance(notes, dict) or not notes.get('summary'):
            raise ValueError(f'unexpected lesson format: {response.text[:200]}')
    except Exception:
        log.warning('CSES lesson for %s failed', task.id, exc_info=True)
        return None
    await db.set_kv(f'cses_lesson:{task.id}', json.dumps(notes, ensure_ascii=False))
    return notes

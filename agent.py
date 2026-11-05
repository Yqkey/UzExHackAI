"""Ядро агента: LLM (Claude) + инструменты sql / find_entities поверх DuckDB. Только чтение, только по данным."""
import os, re, json, time, pathlib, threading
import duckdb
from rapidfuzz import fuzz
import prompt as P

def _load_env():
    f = pathlib.Path(__file__).parent / ".env"
    if f.exists():
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1); os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
_load_env()

PROVIDER = os.getenv("PROVIDER", "gemini").lower()          # gemini | anthropic
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-flash-latest,gemini-flash-lite-latest")   # можно несколько через запятую: запасные на случай 503/429
GEMINI_MODELS = [m.strip() for m in GEMINI_MODEL.split(",") if m.strip()]
DB_PATH = pathlib.Path(os.getenv("DB_PATH", pathlib.Path(__file__).parent / "data" / "exchange.duckdb"))
MODEL = os.getenv("MODEL", "claude-sonnet-5-5")
DEADLINE = float(os.getenv("DEADLINE_SEC", "80"))      # лимит конкурса — 90 с
MAX_ROWS = 100

_con = duckdb.connect(str(DB_PATH), read_only=True, config={"memory_limit": os.getenv("DUCKDB_MEM", "500MB"), "threads": "2"})
_lock = threading.Lock()

def _q(sql, params=None):
    with _lock:
        cur = _con.cursor()
        res = cur.execute(sql, params or [])
        cols = [d[0] for d in res.description]
        return cols, res.fetchall()

# ---------- справочник для промпта ----------
def build_catalog():
    out = []
    _, r = _q("select product, string_agg(distinct unit, '/'), string_agg(distinct currency, '/'), count(*) from v_deals group by 1 order by 1")
    out.append("Товары (product | единицы | валюты | сделок): " + "; ".join(f"{a} | {b} | {c} | {d}" for a, b, c, d in r))
    for col, label in [("category","category"),("industry","industry"),("state","state"),("auction_type","auction_type"),
                       ("contract_type","contract_type"),("contract_form","contract_form"),("seller_activity","seller_activity"),
                       ("buyer_activity","buyer_activity")]:
        _, r = _q(f'select distinct "{col}" from v_deals order by 1')
        out.append(f"{label}: " + " | ".join(str(x[0]) for x in r))
    _, r = _q("select region_uz, region_ru from regions order by 1")
    out.append("Регионы (region_uz = region_ru): " + "; ".join(f"{a} = {b}" for a, b in r))
    _, r = _q("select min(deal_date), max(deal_date), count(*) from v_deals")
    out.append(f"Период данных: {r[0][0]:%d.%m.%Y} – {r[0][1]:%d.%m.%Y}, сделок: {r[0][2]}")
    cols, _ = _q("select * from v_deals limit 0")
    out.append("Колонки v_deals: " + ", ".join(cols))
    return "\n".join(out)

SYSTEM_PROMPT = P.SYSTEM.replace("{glossary}", P.GLOSSARY_UZ).replace("{catalog}", build_catalog())

# ---------- инструменты ----------
FORBIDDEN = re.compile(r"\b(insert|update|delete|drop|alter|create|attach|copy|pragma|install|load|export|call|set)\b", re.I)

def tool_sql(query: str):
    q = query.strip().rstrip(";")
    if not re.match(r"^(select|with)\b", q, re.I) or FORBIDDEN.search(re.sub(r"'[^']*'", "''", q)):
        return {"error": "Разрешены только SELECT/WITH запросы."}
    try:
        cols, rows = _q(q)
    except Exception as e:
        return {"error": str(e)[:500]}
    trunc = len(rows) > MAX_ROWS
    rows = rows[:MAX_ROWS]
    return {"columns": cols, "rows": [[_jsonable(v) for v in r] for r in rows], "row_count_shown": len(rows), "truncated": trunc}

def _jsonable(v):
    if isinstance(v, (int, float, str)) or v is None: return v
    return str(v)

_TR = str.maketrans({"ё":"е","ў":"u","қ":"q","ғ":"g","ҳ":"h","‘":"'","’":"'","`":"'","ʻ":"'","ʼ":"'"})
_C2L = dict(zip("абвгдежзийклмнопрстуфхцчшщъыьэюяқғҳў", ["a","b","v","g","d","e","j","z","i","y","k","l","m","n","o","p","r","s","t","u","f","x","s","ch","sh","sh","","i","","e","yu","ya","q","g","h","u"]))
def _norm(s):
    s = (s or "").lower().translate(_TR)
    s = "".join(_C2L.get(ch, ch) for ch in s)
    s = re.sub(r"[\"'«»“”]", "", s)
    return re.sub(r"[^0-9a-z/\- ]+", " ", s).replace("-", " ").split() and " ".join(re.sub(r"[^0-9a-z/ ]+", " ", s.replace("-", " ")).split())

_cache = {}
def _entities(kind):
    if kind in _cache: return _cache[kind]
    if kind == "company":
        _, r = _q("select inn, name, region_uz, activity, role from participants")
    elif kind == "broker":
        _, r = _q("select broker_code, name, inn, region_uz from brokers")
    elif kind == "product":
        _, r = _q("select product, product_raw, hs_code from contracts group by all")
    elif kind == "mark":
        _, r = _q("select distinct mark, product from contracts where mark is not null")
    else:
        raise ValueError(kind)
    _cache[kind] = [(row, _norm(" ".join(str(x) for x in row[:2]))) for row in r]
    return _cache[kind]

def tool_find_entities(kind: str, text: str, limit: int = 8):
    if kind not in ("company", "broker", "product", "mark"):
        return {"error": "kind: company | broker | product | mark"}
    t = _norm(text)
    scored = []
    for row, key in _entities(kind):
        s = 100 if t and t in key else max(fuzz.token_set_ratio(t, key), fuzz.partial_ratio(t, key) - 5)
        scored.append((s, row))
    scored.sort(key=lambda x: -x[0])
    top = [(s, r) for s, r in scored[:limit] if s >= 75]
    return {"matches": [{"score": round(s), "row": [str(x) for x in r]} for s, r in top],
            "note": "company: [ИНН, наименование, регион, вид деятельности, роль]; broker: [код, наименование, ИНН, регион]; product: [product, product_raw, ТН ВЭД]; mark: [mark, product]"}

TOOLS = [
    {"name": "sql", "description": "Выполнить SELECT/WITH в DuckDB по витрине v_deals и таблицам contracts, participants, brokers, usd_rates, regions. Возвращает до 100 строк.",
     "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
    {"name": "find_entities", "description": "Нечёткий поиск компании / брокера / товара / марки по названию (кириллица и латиница). Возвращает кандидатов с ИНН/кодом.",
     "input_schema": {"type": "object", "properties": {"kind": {"type": "string", "enum": ["company", "broker", "product", "mark"]}, "text": {"type": "string"}}, "required": ["kind", "text"]}},
]

# ---------- проверка «все числа из данных» ----------
NUM_RE = re.compile(r"\d[\d\s\u00a0]*(?:[.,]\d+)?")
def _num(tok):
    t = tok.replace("\u00a0", "").replace(" ", "")
    if re.search(r",\d{1,2}$", t): t = t.replace(",", ".")
    else: t = t.replace(",", "")
    try: return float(t)
    except ValueError: return None

def ungrounded_numbers(answer, question, results):
    pool = []
    for res in results:
        for row in res.get("rows", []):
            for v in row:
                if isinstance(v, (int, float)): pool.append(float(v))
                elif isinstance(v, str):
                    pool += [x for x in (_num(m) for m in NUM_RE.findall(v)) if x is not None]
    qn = {_num(m) for m in NUM_RE.findall(question)}
    bad = []
    for m in NUM_RE.findall(answer):
        v = _num(m)
        if v is None or v in qn or (2000 <= v <= 2100 and v == int(v)): continue
        if v < 1000 and v == int(v): continue            # мелкие целые (порядковые номера, дни) не проверяем
        if not any(abs(v - p) <= max(0.51, abs(p) * 1e-9) for p in pool):
            bad.append(m.strip())
    return bad

# ---------- цикл агента ----------
_client = None
def client():
    global _client
    if _client is None:
        import anthropic
        _client = anthropic.Anthropic()
    return _client

def ask_anthropic(question: str, history=None):
    t0 = time.time()
    msgs = list(history or []) + [{"role": "user", "content": question}]
    trace, results, retried = [], [], False
    system = [{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}]
    for step in range(10):
        left = DEADLINE - (time.time() - t0)
        if left < 5:
            return _out("Не успел уложиться во время. Попробуйте переформулировать вопрос короче.", trace, t0, False)
        resp = client().messages.create(model=MODEL, max_tokens=1500, system=system, tools=TOOLS, messages=msgs, temperature=0, timeout=max(left, 5))
        msgs.append({"role": "assistant", "content": resp.content})
        calls = [b for b in resp.content if b.type == "tool_use"]
        if not calls:
            text = "".join(b.text for b in resp.content if b.type == "text").strip()
            bad = ungrounded_numbers(text, question, results)
            if bad and not retried and (DEADLINE - (time.time() - t0)) > 15:
                retried = True
                msgs.append({"role": "user", "content": f"Проверка: числа {bad} не найдены в результатах запросов. Посчитай их SQL-запросом или убери из ответа. Не выдумывай."})
                continue
            if bad:
                text += "\n\n⚠ Не все числа удалось подтвердить запросом: " + ", ".join(bad)
            return _out(text, trace, t0, not bad)
        outs = []
        for c in calls:
            res = tool_sql(c.input.get("query", "")) if c.name == "sql" else tool_find_entities(c.input.get("kind", ""), c.input.get("text", ""))
            trace.append({"tool": c.name, "input": c.input, "output": res})
            if c.name == "sql": results.append(res)
            outs.append({"type": "tool_result", "tool_use_id": c.id, "content": json.dumps(res, ensure_ascii=False)})
        msgs.append({"role": "user", "content": outs})
    return _out("Не удалось получить ответ за разумное число шагов.", trace, t0, False)

def _out(text, trace, t0, grounded):
    return {"answer": text, "sql": [t["input"].get("query") for t in trace if t["tool"] == "sql"],
            "trace": trace, "elapsed_sec": round(time.time() - t0, 1), "grounded": grounded}


# ---------- Gemini ----------
_gclient = None
def gclient():
    global _gclient
    if _gclient is None:
        from google import genai
        _gclient = genai.Client()          # ключ из GEMINI_API_KEY (или GOOGLE_API_KEY)
    return _gclient

def _gemini_tools():
    from google.genai import types
    return [types.Tool(function_declarations=[
        types.FunctionDeclaration(name=t["name"], description=t["description"], parameters=t["input_schema"]) for t in TOOLS])]

GEMINI_THINKING = os.getenv("GEMINI_THINKING", "low").lower()      # minimal | low | medium | high | off (off = настройки модели по умолчанию)
CALL_TIMEOUT = float(os.getenv("GEMINI_CALL_TIMEOUT", "30"))        # один вызов модели; зависший вызов не должен съедать все 90 с

def _call_with_timeout(fn, timeout):
    box = {}
    def run():
        try: box["r"] = fn()
        except BaseException as e: box["e"] = e
    t = threading.Thread(target=run, daemon=True); t.start(); t.join(timeout)
    if t.is_alive(): raise TimeoutError("вызов модели завис")
    if "e" in box: raise box["e"]
    return box["r"]

def _thinking_cfg():
    from google.genai import types
    if GEMINI_THINKING in ("", "off", "default"): return None
    lvl = {"minimal": "MINIMAL", "low": "LOW", "medium": "MEDIUM", "high": "HIGH"}.get(GEMINI_THINKING)
    return types.ThinkingConfig(thinking_level=getattr(types.ThinkingLevel, lvl)) if lvl else None

def _gen(contents, config, deadline_left):
    """generate_content: таймаут на каждый вызов, до 6 попыток; при 503/429/504/зависании — следующая модель из списка."""
    last, no_thinking = None, False
    for attempt in range(6):
        model = GEMINI_MODELS[attempt % len(GEMINI_MODELS)]
        cfg = config
        if no_thinking:
            cfg = config.model_copy(update={"thinking_config": None})
        left = deadline_left() - 2
        if left < 4: break
        try:
            return _call_with_timeout(lambda m=model, c=cfg: gclient().models.generate_content(model=m, contents=contents, config=c), min(CALL_TIMEOUT, left))
        except TimeoutError as e:
            last = e
            continue                                    # сразу пробуем другую модель
        except Exception as e:
            last, msg = e, str(e)
            if "thinking" in msg.lower() and not no_thinking:    # модель не поддерживает этот уровень — повтор без него
                no_thinking = True; continue
            if not any(x in msg for x in ("429", "RESOURCE_EXHAUSTED", "500", "503", "504", "UNAVAILABLE", "DEADLINE_EXCEEDED")): raise
            wait = min(2 * (attempt + 1), 6)
            if deadline_left() < wait + 10: break
            time.sleep(wait)
    raise last or TimeoutError("Модель не ответила вовремя")

def ask_gemini(question: str, history=None):
    from google.genai import types
    t0 = time.time()
    left = lambda: DEADLINE - (time.time() - t0)
    contents = list(history or []) + [types.Content(role="user", parts=[types.Part(text=question)])]
    trace, results, retried = [], [], False
    for step in range(10):
        if left() < 5:
            return _out("Не успел уложиться во время. Попробуйте переформулировать вопрос короче.", trace, t0, False)
        config = types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT, tools=_gemini_tools(),
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            thinking_config=_thinking_cfg())
        try:
            resp = _gen(contents, config, left)
        except Exception as e:
            return _out("Ошибка обращения к модели: " + str(e)[:300], trace, t0, False)
        if not resp.candidates or not resp.candidates[0].content:
            return _out("Модель не вернула ответ. Повторите вопрос.", trace, t0, False)
        content = resp.candidates[0].content
        contents.append(content)                       # целиком, вместе с thought-signature
        parts = content.parts or []
        calls = [p.function_call for p in parts if getattr(p, "function_call", None)]
        if not calls:
            text = "".join(p.text for p in parts if getattr(p, "text", None) and not getattr(p, "thought", False)).strip()
            bad = ungrounded_numbers(text, question, results)
            if bad and not retried and left() > 15:
                retried = True
                contents.append(types.Content(role="user", parts=[types.Part(text=f"Проверка: числа {bad} не найдены в результатах запросов. Посчитай их SQL-запросом или убери из ответа. Не выдумывай.")]))
                continue
            if bad:
                text += "\n\n⚠ Не все числа удалось подтвердить запросом: " + ", ".join(bad)
            return _out(text, trace, t0, not bad)
        resp_parts = []
        for c in calls:
            args = dict(c.args or {})
            res = tool_sql(args.get("query", "")) if c.name == "sql" else tool_find_entities(args.get("kind", ""), args.get("text", ""))
            trace.append({"tool": c.name, "input": args, "output": res})
            if c.name == "sql": results.append(res)
            resp_parts.append(types.Part.from_function_response(name=c.name, response={"result": res}))
        contents.append(types.Content(role="user", parts=resp_parts))
    return _out("Не удалось получить ответ за разумное число шагов.", trace, t0, False)

def ask(question: str, history=None):
    return (ask_gemini if PROVIDER == "gemini" else ask_anthropic)(question, history)

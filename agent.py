"""
وكيل التسعير - المرحلة 1
يقرأ طلبات التسوق من بوت تيليجرام، يبحث في السوق عبر Gemini (مجاني) و Tavily (بحث مجاني)،
ويرجع تقرير مقارنة أسعار على نفس المحادثة.

يشتغل كتشغيلة قصيرة (GitHub Actions يشغّله cron-job.org)، ما يحتاج سيرفر شغّال 24 ساعة.
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

# Flash-Lite حده المجاني 500 طلب باليوم، بينما Flash العادي 20 طلب بس
# احتياط لو ما قدرنا نجيب قائمة النماذج من Google
GEMINI_MODELS = ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "gemini-2.5-flash-lite",
                 "gemini-3-flash", "gemini-2.5-flash"]
MAX_OPTIONS = int(os.environ.get("MAX_OPTIONS", "7"))
CHUNK = int(os.environ.get("CHUNK", "8"))  # عدد النتائج في كل طلب لـ Gemini
DIRECT_PAGES = int(os.environ.get("DIRECT_PAGES", "50"))
TIME_BUDGET = int(os.environ.get("TIME_BUDGET", "150"))  # ثواني قبل ما نوقف البحث الإضافي  # صفحات نقرأ سعرها مباشرة
SAUDI_SOURCES = {"أمازون", "نون", "سلة", "زد", "متاجر سعودية", "حراج"}
TG_LIMIT = 3900  # حد تيليجرام 4096 حرف، نخلي هامش
KSA = timezone(timedelta(hours=3))

HELP_TEXT = (
    "وكيل التسعير 🔎\n\n"
    "اكتب اللي تبي تشتريه بأي صيغة، مثال:\n"
    "فلتر جامبو 3 مراحل 20 انش مدخل 1 انش، تحت 500\n\n"
    "أو أرسل رابط منتج أو صورته، وأدور لك عليه بأرخص سعر.\n"
    "يرجع لك 7 خيارات قابلة للشراء الفوري.\n\n"
    "بعد كل تقرير تطلع لك قائمة أرقام: مراقبة السعر، إعادة البحث، تعديل الطلب، تعديل المواقع.\n"
    "أرسل 0 في أي وقت للقائمة."
)

SYSTEM_PROMPT = """أنت وكيل تسعير لمحمد، ساكن في الخبر بالمنطقة الشرقية في السعودية.
مهمتك: من نتائج البحث المرفقة فقط، اجمع كل صفحات المنتجات المرشحة (لين MAX_OPTIONS). فيه مرحلة تحقق بعدك تراجع السعر والتوفر والمطابقة، فلا تكون متشدد زيادة:
- أي نتيجة تبدو صفحة منتج للجهاز المطلوب وفيها سعر (في page أو content) ضيفها.
- استبعد بس: المقالات والأدلة وصفحات الأقسام، والمزادات، واللي مكتوب صراحة إنه نافد، وقطع الغيار والمستهلكات لحالها (إلا إذا طلبها المستخدم).
- خذ السعر من page إذا موجود، وإلا من content. ممنوع تختلق سعر أو رابط، وممنوع الأسعار التقديرية: إذا الرقم مو مكتوب في النتيجة لا تحط المنتج.
- لا تكرر نفس الرابط.

حاول توصل MAX_OPTIONS خيار. قدّم المطابقة للميزانية، وإذا ما كفت كمّل بأقرب خيارات فوقها واكتب في price_note "فوق الميزانية".
نوّع بين المنصات قدر الإمكان، والأولوية للمتاجر السعودية ثم الخارج.

لكل خيار:
- السعر بالريال (حوّل العملات الأجنبية تقريبياً واكتب العملة الأصلية في price_note).
- هل يشمل ضريبة 15%، وهل يشمل الشحن، وهل فيه جمارك محتملة (للطلب من خارج السعودية).
- علي بابا: اذكر الحد الأدنى للطلب (MOQ) في price_note.
- opinion: سطر واحد فقط، رأي استشاري بحت عن ميزة المنتج أو سعره. بدون توصية وبدون اقتراحات.

اكتب النصوص باللهجة السعودية المختصرة.

رجّع JSON فقط بهذا الشكل:
{
  "options": [
    {
      "name": "اسم المنتج",
      "price_sar": 449,
      "price_note": "مثل: قبل الخصم 599، أو 120 دولار، أو MOQ 10",
      "vat": "شامل | غير شامل | غير مذكور",
      "shipping": "مجاني | شامل | +25 ريال | غير مذكور",
      "import_fees": "لا يوجد | محتمل | غير مذكور",
      "condition": "جديد | مستعمل",
      "seller": "اسم البائع أو المتجر",
      "platform": "المنصة",
      "location": "المدينة أو الدولة",
      "specs": "أهم المواصفات المطابقة للطلب",
      "coupon": "كود الخصم إذا وُجد منشور، وإلا فاضي",
      "opinion": "سطر واحد",
      "url": "رابط صفحة الشراء"
    }
  ]
}
رتّب الخيارات من الأرخص للأغلى. price_sar رقم أو null."""

# المصادر المطلوبة: (الاسم، النطاقات، اللغات)
SOURCES = [
    ("أمازون", ["amazon.sa"], ("ar", "en")),
    ("نون", ["noon.com"], ("ar", "en")),
    ("سلة", ["salla.sa"], ("ar", "en")),
    ("زد", ["zid.store"], ("ar", "en")),
    ("متاجر سعودية", None, ("ar", "en")),
    ("حراج", ["haraj.com.sa"], ("ar",)),
    ("ترينديول", ["trendyol.com"], ("ar", "en")),
    ("إيباي", ["ebay.com"], ("en",)),
    ("علي إكسبريس", ["aliexpress.com"], ("en",)),
    ("علي بابا", ["alibaba.com"], ("en",)),
]


# ---------------------------------------------------------------- تيليجرام

class Telegram:
    def __init__(self, token: str, chat_id: str):
        self.base = f"https://api.telegram.org/bot{token}"
        self.file_base = f"https://api.telegram.org/file/bot{token}"
        self.chat_id = str(chat_id)

    def call(self, method: str, **params):
        wait = int(params.get("timeout") or 0)
        r = requests.post(f"{self.base}/{method}", json=params, timeout=60 + wait)
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram {method} failed: {data.get('description')}")
        return data["result"]

    def get_updates(self, offset=None, wait: int = 0):
        params = {"timeout": wait, "allowed_updates": ["message"]}
        if offset is not None:
            params["offset"] = offset
        return self.call("getUpdates", **params)

    def ack(self, update_id: int):
        # طلب getUpdates بـ offset أعلى يؤكد استلام كل اللي قبله عند تيليجرام
        self.call("getUpdates", offset=update_id + 1, limit=1, timeout=0)

    def download(self, file_id: str) -> bytes:
        path = self.call("getFile", file_id=file_id)["file_path"]
        r = requests.get(f"{self.file_base}/{path}", timeout=60)
        r.raise_for_status()
        return r.content

    def send(self, text: str, chat_id=None):
        for chunk in split_message(text):
            self.call(
                "sendMessage",
                chat_id=str(chat_id or self.chat_id),
                text=chunk,
                disable_web_page_preview=True,
            )
            time.sleep(2)  # تفادي حد الإرسال المتتالي


def split_message(text: str, limit: int = TG_LIMIT):
    """يقسم النص على حدود الأسطر بحيث كل جزء أقل من حد تيليجرام."""
    chunks, current = [], ""
    for line in text.split("\n"):
        while len(line) > limit:  # سطر طويل جداً
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current.strip():
        chunks.append(current)
    return chunks


# ---------------------------------------------------------------- البحث (مجاني: Gemini + Tavily)

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
TAVILY_URL = "https://api.tavily.com/search"
TAVILY_EXTRACT_URL = "https://api.tavily.com/extract"
# نسخ المتاجر لدول ثانية (أسعارها بعملة ثانية وغالباً ما توصل السعودية)
FOREIGN_STOREFRONT_RE = re.compile(
    r"noon\.com/(?!saudi)[a-z]+-(?:ar|en)/|amazon\.(?!sa\b)[a-z.]+/|trendyol\.com/(?!ar/|en/)", re.I
)
# صفحات بحث أو أقسام أو مقارنة أسعار: مو صفحة منتج تقدر تشتري منها مباشرة
NON_PRODUCT_URL_RE = re.compile(
    r"/search|[?&](q|k|query|keyword|s)=|/s\?|/category|/categories|/collections?/?(\?|$)|/showroom/|/tag/|/tags/"
    r"|/wholesale/|/popular/|/w/|/shop/?$|/brands?/|/compare"
    r"|pricena\.|priceena|pricespy|pricerunner|idealo\.|google\.[a-z.]+/shopping|yaoota|ubuy\.|desertcart\.|/blog/",
    re.I,
)
HARAJ_AD_RE = re.compile(r"haraj\.com\.sa/(?:en/)?\d{5,}", re.I)


def is_product_url(url: str) -> bool:
    if NON_PRODUCT_URL_RE.search(url):
        return False
    if "haraj.com.sa" in url and not HARAJ_AD_RE.search(url):
        return False  # في حراج نقبل صفحات الإعلانات بس
    return True


UNAVAILABLE_RE = re.compile(
    r"place bid|bid now|auction ends|out of stock|sold out|currently unavailable|"
    r"نفدت الكمية|نفذت الكمية|غير متوفر حالي|المنتج غير متوفر|مزايدة|مزاد|محذوف أو قديم|الإعلان محذوف",
    re.I,
)
MAX_PAGES = int(os.environ.get("MAX_PAGES", "25"))  # كل 5 صفحات = رصيد واحد من Tavily
PRICE_RE = re.compile(r"(?:SAR|SR|ر\.?\s?س|ريال)\s*[\d٠-٩][\d٠-٩,.]*|[\d٠-٩][\d٠-٩,.]*\s*(?:SAR|SR|ر\.?\s?س|ريال)", re.I)

QUERY_PROMPT = """حوّل طلب الشراء التالي لعبارتي بحث قصيرة عن المنتج نفسه (اسم المنتج وأهم مواصفاته فقط، بدون الميزانية):
وحدة بالعربي ووحدة بالإنجليزي.
رجّع JSON فقط: {"ar": "...", "en": "..."}

طلب الشراء:
"""


_MODELS_CACHE = None
_DEAD_MODELS = set()  # نماذج رجعت 404 أو خلص حدها اليومي في هالتشغيلة
_BUSY = {}            # آخر وقت رجع فيه النموذج 503


def _model_rank(name: str):
    """الأولوية: Flash-Lite الجديد (حده المجاني أعلى) ثم Flash، وبعدها Gemma (سعة منفصلة)."""
    if name.startswith("gemma"):
        size = re.search(r"(\d+)b", name)
        return (2, 0, -(int(size.group(1)) if size else 0), name)
    m = re.search(r"gemini-(\d+(?:\.\d+)?)", name)
    version = float(m.group(1)) if m else 0.0
    lite = "lite" in name
    preview = "preview" in name or "exp" in name
    return (0 if lite else 1, 1 if preview else 0, -version, name)


def gemini_models() -> list:
    """النماذج المتاحة فعلاً على المفتاح، مرتبة. نسألها من Google بدل ما نخمن الأسماء."""
    global _MODELS_CACHE
    if os.environ.get("GEMINI_MODEL"):
        return [m.strip() for m in os.environ["GEMINI_MODEL"].split(",") if m.strip()]
    if _MODELS_CACHE is None:
        found = []
        try:
            r = requests.get("https://generativelanguage.googleapis.com/v1beta/models",
                             headers={"x-goog-api-key": os.environ.get("GEMINI_API_KEY", "")},
                             params={"pageSize": 200}, timeout=30)
            r.raise_for_status()
            for m in r.json().get("models", []):
                name = m.get("name", "").replace("models/", "")
                if "generateContent" not in m.get("supportedGenerationMethods", []):
                    continue
                is_gemma = name.startswith("gemma-") and "it" in name and not re.search(r"\b(1b|2b|4b|e2b|e4b)\b|-(1b|2b|4b|e2b|e4b)", name)
                if not (name.startswith("gemini-") and "flash" in name) and not is_gemma:
                    continue
                if re.search(r"image|tts|audio|live|embed|thinking|robotics|computer", name):
                    continue
                found.append(name)
        except Exception as exc:
            print(f"model list failed: {type(exc).__name__}", file=sys.stderr)
        ranked = sorted(set(found), key=_model_rank)
        gemini_ = [m for m in ranked if m.startswith("gemini")][:7]
        gemma_ = [m for m in ranked if m.startswith("gemma")][:2]
        _MODELS_CACHE = (gemini_ + gemma_) or list(GEMINI_MODELS)
        print(f"gemini models: {_MODELS_CACHE}")
    alive = [m for m in _MODELS_CACHE if m not in _DEAD_MODELS] or _MODELS_CACHE
    # النماذج اللي ردت "مشغول" قبل أقل من 3 دقايق نأخرها لآخر القائمة
    now = time.time()
    free = [m for m in alive if now - _BUSY.get(m, 0) > 180]
    busy = [m for m in alive if m not in free]
    return free + busy


_GROQ_MODELS = None
GROQ_PREFERRED = ["gpt-oss-120b", "llama-3.3-70b", "llama-4-maverick", "qwen3", "kimi-k2", "gpt-oss-20b",
                  "llama-4-scout", "llama-3.1-8b"]


def groq_models() -> list:
    """نماذج Groq المتاحة على المفتاح، بترتيب الأفضلية."""
    global _GROQ_MODELS
    if _GROQ_MODELS is None:
        names = []
        try:
            r = requests.get("https://api.groq.com/openai/v1/models",
                             headers={"Authorization": f"Bearer {os.environ['GROQ_API_KEY']}"}, timeout=30)
            r.raise_for_status()
            names = [m["id"] for m in r.json().get("data", [])
                     if not re.search(r"whisper|guard|tts|playai|orpheus|compound|prompt", m["id"], re.I)]
        except Exception as exc:
            print(f"groq model list failed: {type(exc).__name__}", file=sys.stderr)
        rank = lambda n: next((i for i, p in enumerate(GROQ_PREFERRED) if p in n), len(GROQ_PREFERRED))
        _GROQ_MODELS = sorted(names, key=rank)[:3]
        print(f"groq models: {_GROQ_MODELS}", flush=True)
    return _GROQ_MODELS


def groq(prompt: str, system: str = "", json_mode: bool = True) -> str:
    """مزوّد ثاني مجاني وسريع، نستخدمه لما Gemini يزحم."""
    headers = {"Authorization": f"Bearer {os.environ['GROQ_API_KEY']}"}
    messages = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
    last = None
    for model in groq_models():
        body = {"model": model, "messages": messages, "temperature": 0.2}
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        started = time.time()
        r = requests.post("https://api.groq.com/openai/v1/chat/completions", headers=headers, json=body, timeout=90)
        if r.status_code == 400 and json_mode and "json" in r.text.lower():
            body.pop("response_format")  # بعض النماذج ما تدعم وضع JSON
            r = requests.post("https://api.groq.com/openai/v1/chat/completions", headers=headers, json=body, timeout=90)
        print(f"groq {model} {r.status_code} {time.time() - started:.0f}s", flush=True)
        last = r
        if r.status_code in (404, 413, 429, 500, 502, 503, 504):
            continue
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"] or ""
    if last is not None:
        last.raise_for_status()
    raise requests.ConnectionError("no groq models")


def gemini(prompt: str, system: str = "", json_mode: bool = True, image=None) -> str:
    parts = [{"text": prompt}]
    if image:  # (bytes, mime)
        import base64
        parts.insert(0, {"inline_data": {"mime_type": image[1], "data": base64.b64encode(image[0]).decode()}})
    body = {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {"temperature": 0.2},
    }
    if system:  # نحط التعليمات داخل الرسالة: طلبات systemInstruction الكبيرة ترجع 503 كثير في الطبقة المجانية
        parts[-1] = {"text": system + "\n\n----\n\n" + prompt}
    if json_mode:
        body["generationConfig"]["responseMimeType"] = "application/json"
    headers = {"x-goog-api-key": os.environ["GEMINI_API_KEY"]}

    last, busy_streak, tried_groq = None, 0, False
    models = gemini_models()
    if image:  # قراءة الصور: نماذج Flash الكاملة أدق من Lite
        models = sorted(models, key=lambda m: (0 if "flash" in m and "lite" not in m else 1))
    for model in models:
        if busy_streak >= 4:
            break  # Google مزدحم على كل النماذج، ما نضيع وقت أكثر
        if busy_streak >= 1 and not tried_groq and not image and os.environ.get("GROQ_API_KEY"):
            tried_groq = True  # Gemini زحم: نجرب Groq قبل باقي نماذج Gemini
            try:
                return groq(prompt, system, json_mode)
            except Exception as exc:
                print(f"groq failed: {type(exc).__name__}", file=sys.stderr, flush=True)
        url = GEMINI_URL.format(model=model)
        payload = body
        if model.startswith("gemma"):  # Gemma ما يدعم تعليمات النظام ولا وضع JSON
            payload = {"contents": [{"role": "user", "parts": (
                [p for p in parts if "inline_data" in p]
                + [{"text": (system + "\n\n" if system else "") + prompt + "\n\nرجّع JSON فقط."}])}],
                "generationConfig": {"temperature": 0.2}}
        started = time.time()
        for attempt in range(3):
            r = requests.post(url, headers=headers, json=payload, timeout=90)
            if r.status_code in (500, 502, 503, 504) and attempt < 1:
                time.sleep(5)  # ضغط مؤقت: محاولة وحدة سريعة وبعدها ننتقل للنموذج اللي بعده
                continue
            if r.status_code == 429 and "PerDay" not in r.text and attempt < 2:
                time.sleep(20)  # حد الدقيقة، مو حد اليوم
                continue
            break
        print(f"gemini {model} {r.status_code} {time.time() - started:.0f}s", flush=True)
        last = r
        if r.status_code in (404, 429):
            _DEAD_MODELS.add(model)  # ما نضيع وقت عليه مرة ثانية في هالتشغيلة
            busy_streak += 1 if r.status_code == 429 else 0
            continue
        if r.status_code in (500, 502, 503, 504):
            _BUSY[model] = time.time()
            busy_streak += 1
            continue  # مشغول، نجرب النموذج اللي بعده
        busy_streak = 0
        r.raise_for_status()
        parts = r.json()["candidates"][0]["content"]["parts"]
        return "".join(p.get("text", "") for p in parts if not p.get("thought"))
    if not tried_groq and not image and os.environ.get("GROQ_API_KEY"):
        return groq(prompt, system, json_mode)
    last.raise_for_status()


def tavily(query: str, domains=None) -> list:
    body = {
        "api_key": os.environ["TAVILY_API_KEY"],
        "query": query,
        "search_depth": "basic",
        "max_results": 10,
    }
    if domains:
        body["include_domains"] = domains
    else:
        body["country"] = "saudi arabia"
    r = requests.post(TAVILY_URL, json=body, timeout=60)
    if r.status_code == 400 and "country" in r.text:
        body.pop("country")  # بعض الحسابات ما تدعم فلتر الدولة
        r = requests.post(TAVILY_URL, json=body, timeout=60)
    r.raise_for_status()
    return r.json().get("results", [])


def tavily_extract(urls: list) -> dict:
    """يفتح صفحات المنتجات ويرجع نصها: {url: text}"""
    pages = {}
    for k in range(0, len(urls), 20):  # حد Tavily 20 رابط بالطلب
        r = requests.post(
            TAVILY_EXTRACT_URL,
            json={"api_key": os.environ["TAVILY_API_KEY"], "urls": urls[k:k + 20], "extract_depth": "basic"},
            timeout=90,
        )
        r.raise_for_status()
        for item in r.json().get("results", []):
            if item.get("url") and item.get("raw_content"):
                pages[item["url"]] = item["raw_content"]
    return pages


def price_snippets(text: str, head: int = 500, window: int = 180, max_hits: int = 6) -> str:
    """بداية الصفحة (الاسم والمواصفات) + المقاطع اللي حول الأسعار فقط، عشان نوفر حجم الطلب."""
    text = re.sub(r"\s+", " ", text)
    parts = [text[:head]]
    last_end = head
    for m in PRICE_RE.finditer(text):
        if len(parts) > max_hits:
            break
        start = max(m.start() - window, last_end)
        end = min(m.end() + window, len(text))
        if start < end:
            parts.append(text[start:end])
            last_end = end
    return " … ".join(parts)


def product_terms(request_text: str, llm=gemini) -> dict:
    """عبارة بحث عربية وإنجليزية للمنتج."""
    raw = llm(QUERY_PROMPT + request_text).strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        data = parse_result("<json>" + raw + "</json>")
    terms = {}
    if isinstance(data, dict):
        for lang in ("ar", "en"):
            v = data.get(lang)
            if isinstance(v, str) and v.strip():
                terms[lang] = v.strip()
    if not terms:
        print("terms unparsed", file=sys.stderr)
    terms.setdefault("ar", request_text)
    terms.setdefault("en", terms["ar"])
    return terms


def plan_queries(request_text: str, llm=gemini, enabled=None, terms=None) -> list:
    """استعلام لكل مصدر مفعّل ولكل لغة: [(المصدر، الاستعلام، النطاقات)]"""
    terms = terms or product_terms(request_text, llm)
    plan = []
    for name, domains, langs in SOURCES:
        if enabled is not None and name not in enabled:
            continue
        for lang in langs:
            q = terms[lang]
            if domains is None:  # المتاجر السعودية المحلية بدون تحديد موقع
                q += " سعر ريال متجر السعودية" if lang == "ar" else " price SAR Saudi online store"
            else:
                q += " سعر" if lang == "ar" else " price buy"
            plan.append((name, q, domains))
    return plan


# ---------------------------------------------------------------- قنوات تيليجرام

CHANNELS = [c.strip().lstrip("@") for c in os.environ.get("TELEGRAM_CHANNELS", "shopper_ar1").split(",") if c.strip()]
CHANNEL_MAX_AGE_DAYS = int(os.environ.get("CHANNEL_MAX_AGE_DAYS", "365"))  # أقدم من سنة غالباً انتهى
CHANNEL_PRICE_RE = re.compile(r"(?:تصير|ويصير|يصير|تصبح|بسعر|السعر)\s*(?:ب|بـ)?\s*([\d٠-٩][\d٠-٩,.]*)\s*(?:ريال|ر\.س|SAR)?", re.I)
CHANNEL_ANY_PRICE_RE = re.compile(r"([\d٠-٩][\d٠-٩,.]*)\s*(?:ريال|ر\.س|SAR)", re.I)
CHANNEL_CODE_RE = re.compile(r"كود(?:\s*خصم)?\s+([A-Za-z0-9]{4,})")


def _html_text(fragment: str) -> str:
    import html as _h
    t = re.sub(r"<br\s*/?>", "\n", fragment)
    t = re.sub(r"<[^>]+>", "", t)
    return _h.unescape(t).strip()


def parse_channel_page(html_page: str, channel: str) -> list:
    """منشورات صفحة المعاينة العامة للقناة (t.me/s/...)."""
    posts = []
    for block in html_page.split('class="tgme_widget_message_wrap')[1:]:
        post_id = re.search(r'data-post="([^"]+)"', block)
        text_m = re.search(r'class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', block, re.S)
        if not post_id or not text_m:
            continue
        links = re.findall(r'href="(https?://(?!t\.me)[^"]+)"', text_m.group(1))
        date_m = re.search(r'<time[^>]*datetime="([^"]+)"', block)
        posts.append({
            "post": f"https://t.me/{post_id.group(1)}",
            "text": _html_text(text_m.group(1)),
            "link": links[0] if links else "",
            "date": date_m.group(1) if date_m else "",
        })
    return posts


def channel_candidates(channel: str, terms: dict, get=None) -> list:
    """يبحث في القناة ويحوّل المنشورات اللي فيها سعر نهائي لخيارات."""
    get = get or (lambda url: requests.get(url, headers=BROWSER_HEADERS, timeout=20))
    from urllib.parse import quote
    queries = []
    for lang in ("ar", "en"):
        words = (terms.get(lang) or "").split()
        for q in (" ".join(words[:3]), " ".join(words[:2])):
            if q and q not in queries:
                queries.append(q)
    seen, out, found = set(), [], 0
    cutoff = _now() - timedelta(days=CHANNEL_MAX_AGE_DAYS)
    for q in queries[:4]:
        try:
            r = get(f"https://t.me/s/{channel}?q={quote(q)}")
            if r.status_code != 200:
                continue
            posts = parse_channel_page(r.text, channel)
            found += len(posts)
        except Exception as exc:
            print(f"channel {channel} failed: {type(exc).__name__}", file=sys.stderr)
            continue
        for p in posts:
            if p["post"] in seen or not p["link"]:
                continue
            seen.add(p["post"])
            try:
                if p["date"] and datetime.fromisoformat(p["date"]) < cutoff:
                    continue  # عرض قديم
            except ValueError:
                pass
            text = p["text"].translate(AR_DIGITS)
            m = CHANNEL_PRICE_RE.search(text) or None
            prices = [m.group(1)] if m else CHANNEL_ANY_PRICE_RE.findall(text)[-1:]
            price = _num(prices[0]) if prices else None
            if not price:
                continue
            first = re.split(r"[\n.،]", p["text"].strip())[0][:110]
            codes = list(dict.fromkeys(CHANNEL_CODE_RE.findall(text)))[:3]
            day = p["date"][:10]
            out.append({
                "name": first, "price_sar": price, "url": p["link"], "post": p["post"],
                "platform": f"قناة {channel}",
                "seller": re.sub(r"^https?://(www\.)?([^/]+).*$", r"\2", p["link"]),
                "location": "شحن من السعودية" if "من السعودية" in p["text"] else "غير مذكور",
                "condition": "جديد", "vat": "غير مذكور", "shipping": "غير مذكور",
                "import_fees": "غير مذكور", "coupon": " ثم ".join(codes),
                "price_note": f"سعر العرض في القناة بعد الأكواد، بتاريخ {day}، تأكد إنه ما انتهى",
                "content": p["text"][:600],
            })
    print(f"channel {channel}: queries={len(queries[:4])} posts={found} offers={len(out)}", flush=True)
    return out


def research(request_text: str, sources=None, llm=gemini, search=tavily, extract=tavily_extract,
             fetch=None) -> dict:
    t0 = time.time()
    today = datetime.now(KSA).strftime("%Y-%m-%d")
    terms = product_terms(request_text, llm)
    plan = plan_queries(request_text, llm, sources, terms)
    print(f"queries={len(plan)}")

    per_source = {}
    seen = set()
    for name, q, domains in plan:
        try:
            hits = search(q, domains)
        except Exception as exc:
            print(f"search failed ({name}): {type(exc).__name__}", file=sys.stderr)
            continue
        for h in hits:
            url = h.get("url")
            if url and FOREIGN_STOREFRONT_RE.search(url):
                continue  # نسخة دولة ثانية
            if url and not is_product_url(url):
                continue  # صفحة بحث أو قسم أو موقع مقارنة
            if url and url not in seen:
                seen.add(url)
                per_source.setdefault(name, []).append({
                    "source": name,
                    "title": h.get("title", ""),
                    "url": url,
                    "content": (h.get("content") or "")[:400],
                })

    # نوزّع بالتناوب بين المصادر عشان كل منصة يكون لها نصيب من الصفحات المفتوحة
    results = []
    buckets = [list(v) for v in per_source.values()]
    while any(buckets):
        for bucket in buckets:
            if bucket:
                results.append(bucket.pop(0))
    print(f"results={len(results)}")
    if not results:
        return {"options": [], "raw": "ما رجع البحث أي نتائج. جرّب تعيد صياغة الطلب."}

    # نفتح أهم صفحات النتائج ونقرأ السعر من الصفحة نفسها بدل الملخص
    try:
        pages = extract([r["url"] for r in results[:MAX_PAGES]])
    except Exception as exc:
        print(f"extract failed: {type(exc).__name__}", file=sys.stderr)
        pages = {}
    kept = []
    for r in results:
        text = pages.get(r["url"])
        if text and UNAVAILABLE_RE.search(text):
            continue  # مزاد أو نافد، ما نعرضه
        if text:
            r["page"] = price_snippets(text)
        kept.append(r)
    print(f"pages={len(pages)} dropped={len(results) - len(kept)}")
    results = kept

    system = SYSTEM_PROMPT.replace("MAX_OPTIONS", "8")
    page_text = {r["url"]: r.get("page") or r.get("content", "") for r in results}
    page_full = {u: re.sub(r"\s+", " ", t)[:20000] for u, t in pages.items()}  # نص الصفحة نفسها (مو ملخص البحث)

    # 1) مرشحين مباشرين من البيانات المنظمة في صفحات المنتجات: سعر رسمي ومتوفر، بدون ما نحتاج Gemini.
    cache = {}
    base_fetch = fetch or fetch_structured

    def cfetch(u):
        if u not in cache:
            cache[u] = base_fetch(u)
        return cache[u]

    from concurrent.futures import ThreadPoolExecutor
    pool_items = results[:DIRECT_PAGES]
    with ThreadPoolExecutor(max_workers=8) as pool:
        sds = list(pool.map(lambda r: cfetch(r["url"]), pool_items))
    direct = []
    for r, sd in zip(pool_items, sds):
        price = _sar(sd) if sd.get("price") else None
        if price is None or sd.get("available") is False:
            continue
        saudi = r.get("source") in SAUDI_SOURCES or ".sa/" in r["url"] or r["url"].rstrip("/").endswith(".sa")
        direct.append({
            "name": (sd.get("name") or r.get("title") or "")[:120],
            "price_sar": price, "url": r["url"], "platform": r.get("source", ""),
            "seller": re.sub(r"^https?://(www\.)?([^/]+).*$", r"\2", r["url"]),
            "location": "السعودية" if saudi else "خارج السعودية", "condition": "جديد",
            "vat": "غير مذكور", "shipping": "غير مذكور", "import_fees": "لا يوجد" if saudi else "محتمل",
        })
    print(f"direct={len(direct)}", flush=True)
    verified = verify_options(request_text, direct, page_text, llm, cfetch, page_full) if direct else []

    # عروض قنوات تيليجرام (أسعارها من المنشور نفسه، فما نفتح صفحة المتجر)
    for channel in CHANNELS:
        if sources is not None and f"قناة {channel}" not in sources:
            continue
        offers = channel_candidates(channel, terms)
        for o in offers:
            page_text[o["url"]] = o.pop("content")
        if offers:
            verified += verify_options(request_text, offers, page_text, llm, lambda u: {}, {})
    fetch = cfetch

    # نقسم النتائج دفعات صغيرة: الطلبات الكبيرة تطلع "النموذج مشغول" (503) كثير في الطبقة المجانية،
    # والصغيرة تمشي. ونوقف أول ما نكمل 7 خيارات متحقق منها.
    # 2) إذا ما كفّت، نخلي Gemini يستخرج من النتائج دفعات صغيرة
    seen_urls, note = {o["url"] for o in direct}, ""
    for k in range(0, len(results), CHUNK):
        if len(verified) >= MAX_OPTIONS:
            break
        chunk = results[k:k + CHUNK]
        prompt = (
            f"تاريخ اليوم: {today}\n\nطلب الشراء:\n{request_text}\n\n"
            "نتائج البحث (استخدم هذه فقط، ولا تذكر رابط غير موجود فيها):\n"
            + json.dumps(chunk, ensure_ascii=False, indent=0)
        )
        if time.time() - t0 > TIME_BUDGET and verified:
            note = "⏱ وقفت البحث الإضافي عشان ما أطوّل عليك."
            break
        try:
            raw = llm(prompt, system)
        except requests.RequestException as exc:
            print(f"chunk {k // CHUNK} failed: {error_detail(exc)[:120]}", file=sys.stderr, flush=True)
            if not verified:
                raise  # ما عندنا شي نعرضه، نخلي إعادة المحاولة العامة تتصرف
            note = "⚠️ خدمة الذكاء الاصطناعي مزدحمة الحين، فالنتائج أقل من العادة. جرّب \"2 أعد البحث\" بعد شوي."
            break
        result = parse_result("<json>" + raw + "</json>")
        options = result.get("options") if isinstance(result.get("options"), list) else []
        options = [o for o in options if isinstance(o, dict) and o.get("url") and o["url"] not in seen_urls]
        seen_urls.update(o["url"] for o in options)
        print(f"chunk={k // CHUNK} options={len(options)}", flush=True)
        if options:
            verified += verify_options(request_text, options, page_text, llm, fetch, page_full)
        if len(verified) >= MAX_OPTIONS:
            break

    result = {"options": verified, "note": note}
    return result


# ---------------------------------------------------------------- التحقق

# أسعار صرف تقريبية للتحويل لما الصفحة تعرض عملة غير الريال
CURRENCY_TO_SAR = {"SAR": 1.0, "USD": 3.75, "AED": 1.02, "KWD": 12.2, "QAR": 1.03, "BHD": 9.95,
                   "OMR": 9.74, "EUR": 4.05, "GBP": 4.75, "CNY": 0.52, "TRY": 0.11}

# كلمات تدل إن المنتج قطعة أو مستهلكات مو الجهاز الكامل
PART_WORDS = ["شمعات", "شمعة", "خرطوش", "فلاتر بديلة", "طقم فلاتر", "طقم شمعات", "قطع غيار", "قطعة غيار",
              "بيت فلتر", "replacement", "cartridge", "refill", "spare part", "filter set", "housing only"]

BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/130.0 Safari/537.36",
    "Accept-Language": "ar-SA,ar;q=0.9,en;q=0.8",
}

VERIFY_PROMPT = """طلب الشراء: {request}

تحت قائمة منتجات مرشحة مع نص من صفحاتها. لكل منتج قرر بصرامة:
- complete: هل هو الجهاز/المنتج الكامل نفسه المطلوب؟ (false إذا كان قطع غيار، شمعات، فلاتر بديلة، ملحقات، هيكل لحاله، أو طقم مستهلكات)
- matches: false إذا الطلب يحدد ماركة أو موديل والمنتج ماركة ثانية أو بدون ماركة (OEM / ODM / generic / compatible). وكذلك false إذا الاسم أو الصفحة تذكر رقم يخالف الطلب صراحة (مثلاً مرحلتين بدل 3، أو 10 انش بدل 20). إذا المواصفة غير مذكورة اعتبرها true.
- condition: "جديد" أو "مستعمل" حسب الصفحة (إعلانات حراج غالباً مستعمل إلا إذا مكتوب جديد).
- genuine: false إذا الصفحة تقول إنه تقليد أو "طبق الأصل" أو "هاي كوبي" أو "ماستر كوبي"، أو إذا السعر أقل بكثير من سعر السوق المعروف لماركة أصلية (مثلاً سماعة أبل أصلية بأقل من نص سعرها). غير كذا true.
- opinion: سطر واحد قصير بالسعودي، رأي استشاري بحت عن ميزة المنتج أو سعره، بدون توصية.
رجّع JSON فقط: {{"checks": [{{"i": 0, "complete": true, "matches": true, "genuine": true, "opinion": "..."}}]}}

المنتجات:
{items}"""


def _num(v):
    try:
        return float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _find_product(node):
    """يدور على كائن Product داخل JSON-LD."""
    if isinstance(node, list):
        for n in node:
            p = _find_product(n)
            if p:
                return p
    elif isinstance(node, dict):
        t = node.get("@type")
        if t == "Product" or (isinstance(t, list) and "Product" in t):
            return node
        for key in ("@graph", "mainEntity", "itemListElement", "item"):
            if key in node:
                p = _find_product(node[key])
                if p:
                    return p
    return None


def parse_structured_price(html: str) -> dict:
    """السعر الرسمي للمنتج من البيانات المنظمة في الصفحة (JSON-LD أو وسوم meta)."""
    out = {}
    for block in re.findall(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', html, re.S | re.I):
        try:
            product = _find_product(json.loads(block.strip()))
        except (json.JSONDecodeError, ValueError):
            continue
        if not product:
            continue
        offers = product.get("offers") or {}
        if isinstance(offers, list):
            offers = offers[0] if offers else {}
        price = _num(offers.get("price") or offers.get("lowPrice"))
        if price:
            out = {
                "price": price,
                "currency": (offers.get("priceCurrency") or "").upper(),
                "available": None,
                "name": product.get("name", ""),
            }
            avail = str(offers.get("availability", "")).lower()
            if avail:
                out["available"] = "instock" in avail or "limitedavailability" in avail or "preorder" in avail
            return out
    m = re.search(r'(?:product:price:amount|og:price:amount|itemprop=["\']price["\'])[^>]*content=["\']([\d.,]+)', html, re.I) \
        or re.search(r'content=["\']([\d.,]+)["\'][^>]*(?:product:price:amount|og:price:amount)', html, re.I)
    if m and _num(m.group(1)):
        cur = re.search(r'(?:product:price:currency|og:price:currency|itemprop=["\']priceCurrency["\'])[^>]*content=["\']([A-Za-z]{3})', html, re.I)
        out = {"price": _num(m.group(1)), "currency": (cur.group(1) if cur else "").upper(), "available": None, "name": ""}
    return out


def fetch_structured(url: str) -> dict:
    try:
        r = requests.get(url, headers=BROWSER_HEADERS, timeout=15)
        if r.status_code != 200:
            return {}
        if not r.encoding or r.encoding.lower() in ("iso-8859-1", "latin-1", "ascii"):
            r.encoding = "utf-8"  # بعض المواقع ما تحدد الترميز، فيطلع العربي رموز غريبة
        return parse_structured_price(r.text)
    except requests.RequestException:
        return {}


def price_in_text(price, text: str) -> bool:
    """هل الرقم مكتوب فعلاً في نص الصفحة؟ (يمنع الأسعار التقديرية أو المختلقة)"""
    if not isinstance(price, (int, float)) or not text:
        return False
    digits = re.sub(r"[,\u066c\s]", "", (text or "").translate(str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")))
    whole = str(int(price))
    return re.search(rf"(?<!\d){whole}(?:\.\d+)?(?!\d)", digits) is not None


KNOCKOFF_RE = re.compile(
    r"\b(oem|odm|generic|compatible|replica|copy|clone|for airpods|like airpods)\b|تقليد|كوبي|طبق الأصل|متوافق مع|شبيه",
    re.I,
)


def is_knockoff(name: str, request_text: str) -> bool:
    """منتج بدون ماركة أو تقليد، والمستخدم ما طلب كذا."""
    return bool(KNOCKOFF_RE.search(name or "")) and not KNOCKOFF_RE.search(request_text or "")


def is_part(name: str, request_text: str) -> bool:
    name, req = (name or "").lower(), request_text.lower()
    return any(w in name and w not in req for w in PART_WORDS)


def verify_options(request_text, options, page_text, llm=gemini, fetch=None, page_full=None) -> list:
    page_full = page_full or {}
    fetch = fetch or fetch_structured
    # 1) استبعاد القطع والمستهلكات من الاسم
    before = len(options)
    options = [o for o in options if isinstance(o, dict) and not is_part(o.get("name", ""), request_text)
               and not is_knockoff(o.get("name", ""), request_text) and is_product_url(o.get("url", ""))]
    print(f"llm_options={before} after_parts={len(options)}")

    # 2) السعر والتوفر من بيانات الصفحة الرسمية بدل تقدير النموذج
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=8) as pool:
        structured = list(pool.map(lambda o: fetch(o["url"]) if o.get("url") else {}, options))
    checked, oos, unpriced = [], 0, 0
    for o, sd in zip(options, structured):
        if sd.get("available") is False:
            oos += 1
            continue  # نافد حسب الصفحة
        in_page = price_in_text(o.get("price_sar"), page_full.get(o.get("url"), ""))
        if not sd.get("price") and not in_page and not price_in_text(o.get("price_sar"), page_text.get(o.get("url"), "")):
            unpriced += 1
            continue  # سعر ما نقدر نتحقق منه ولا هو مكتوب في الصفحة = ممكن يكون تقدير، نستبعده
        if not sd.get("price") and in_page:
            o["price_verified"] = True  # الرقم مكتوب في نص صفحة المنتج اللي فتحناها
        if sd.get("price"):
            cur = sd.get("currency") or "SAR"
            rate = CURRENCY_TO_SAR.get(cur)
            if rate:
                o["price_sar"] = round(sd["price"] * rate, 2)
                o["price_verified"] = True
                # ملاحظة النموذج عن السعر ما تعود صحيحة بعد ما أخذنا السعر الرسمي من الصفحة
                o["price_note"] = f"تقريبي من {sd['price']:g} {cur}" if cur != "SAR" else ""
        checked.append(o)

    # 3) مراجعة ثانية: الجهاز كامل ويطابق المواصفات؟
    if checked:
        items = "\n".join(
            f"[{i}] {o.get('name', '')} | {o.get('specs', '')} | السعر {o.get('price_sar')} ريال | {page_text.get(o.get('url'), '')[:700]}"
            for i, o in enumerate(checked)
        )
        try:
            raw = llm(VERIFY_PROMPT.format(request=request_text, items=items))
            data = json.loads(raw) if raw.strip().startswith("{") else parse_result("<json>" + raw + "</json>")
            verdict = {c.get("i"): c for c in data.get("checks", []) if isinstance(c, dict)}
            if verdict:
                for i, o in enumerate(checked):
                    if not o.get("opinion") and isinstance(verdict.get(i, {}).get("opinion"), str):
                        o["opinion"] = verdict[i]["opinion"].strip()
                    if verdict.get(i, {}).get("condition") in ("جديد", "مستعمل"):
                        o["condition"] = verdict[i]["condition"]
                checked = [o for i, o in enumerate(checked)
                           if verdict.get(i, {}).get("complete") and verdict.get(i, {}).get("matches")
                           and verdict.get(i, {}).get("genuine", True) is not False]
        except Exception as exc:  # لو فشلت المراجعة نكمل بالموجود
            print(f"verify failed: {type(exc).__name__}", file=sys.stderr)
    print(f"candidates={len(options)} oos={oos} unpriced={unpriced} verified={len(checked)}")
    return checked


def parse_result(text: str) -> dict:
    candidates = []
    for pat in (r"<json>\s*(.*?)\s*</json>", r"```(?:json)?\s*(.*?)\s*```"):
        candidates += re.findall(pat, text, re.S)
    if "{" in text:
        candidates.append(text[text.index("{"):text.rindex("}") + 1])
    for c in candidates:
        c = re.sub(r"^```(?:json)?|```$", "", c.strip()).strip()
        try:
            data = json.loads(c)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            data.setdefault("options", [])
            return data
    return {"options": [], "raw": text.strip()}


# ---------------------------------------------------------------- التقرير

def _price_key(opt):
    p = opt.get("price_sar")
    return p if isinstance(p, (int, float)) else float("inf")


def display_options(result: dict) -> list:
    """الخيارات بنفس الترتيب والترقيم اللي يشوفه المستخدم."""
    # بدون سعر = مو قابل للشراء الفوري، نستبعده
    options = [o for o in result.get("options", []) if isinstance(o.get("price_sar"), (int, float))]
    out, seen = [], set()
    for o in sorted(options, key=_price_key):
        # نفس المنتج بنفس السعر في نفس المنصة (روابط مختلفة) = تكرار
        key = (o.get("platform", ""), round(o["price_sar"]))
        if key in seen or o.get("url") in {x.get("url") for x in out}:
            continue
        seen.add(key)
        out.append(o)
    return out[:MAX_OPTIONS]


def format_report(request_text: str, result: dict) -> str:
    lines = [f"🔎 {request_text[:150]}", ""]
    options = display_options(result)
    if not options:
        lines.append("ما لقيت منتجات متوفرة بسعر شراء فوري تطابق طلبك.")
        return "\n".join(lines)

    for i, o in enumerate(options, 1):
        price = o.get("price_sar")
        price_txt = f"{price:g} ريال" if isinstance(price, (int, float)) else "السعر غير مذكور"
        lines.append(f"{i}) {o.get('name', 'بدون اسم')}")
        mark = "✅ من الصفحة" if o.get("price_verified") else "⚠️ تأكد من السعر في الرابط"
        lines.append(f"💰 {price_txt}" + (f" ({o['price_note']})" if o.get("price_note") else "") + f" {mark}")
        lines.append(
            f"الضريبة: {o.get('vat', 'غير مذكور')} | الشحن: {o.get('shipping', 'غير مذكور')}"
            f" | الجمارك: {o.get('import_fees', 'غير مذكور')}"
        )
        lines.append(
            f"🏪 {o.get('platform', '')} - {o.get('seller', 'غير مذكور')} - {o.get('location', '')}"
            f" | {o.get('condition', '')}"
        )
        if o.get("specs"):
            lines.append(f"المواصفات: {o['specs']}")
        if o.get("coupon"):
            lines.append(f"🎟 كود الخصم: {o['coupon']}")
        if o.get("opinion"):
            lines.append(f"💬 {o['opinion']}")
        if o.get("url"):
            lines.append(o["url"])
        if o.get("post"):
            lines.append(f"المنشور: {o['post']}")
        lines.append("")

    if len(options) < MAX_OPTIONS:
        lines.append(f"لقيت {len(options)} خيارات بس تنطبق عليها الشروط.")
    if result.get("note"):
        lines.append(result["note"])
    return "\n".join(lines).strip()


# ---------------------------------------------------------------- مراقبة الأسعار

STATE_FILE = os.environ.get("STATE_FILE", "state/state.enc")
LEGACY_STATE_FILE = "state/state.json"  # الصيغة القديمة غير المشفرة
WATCH_EVERY = timedelta(hours=int(os.environ.get("WATCH_EVERY_HOURS", "24")))
MAX_WATCHES = 30

WATCH_RE = re.compile(r"^(?:راقب|تابع)\s*(\d+)$")
UNWATCH_RE = re.compile(r"^(?:الغ|ألغ|الغاء|إلغاء|احذف|وقف)\s*(?:مراقبة)?\s*(\d+)$")
LIST_WORDS = {"المراقبة", "مراقبة", "قائمتي", "قائمة المراقبة", "/watch"}


def _fernet():
    """مفتاح التشفير مشتق من توكن البوت (سر موجود أصلاً)، فما نحتاج سر جديد."""
    import base64
    import hashlib
    from cryptography.fernet import Fernet
    secret = os.environ.get("STATE_KEY") or os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not secret:
        return None
    key = base64.urlsafe_b64encode(hashlib.sha256(("pricing-agent-state:" + secret).encode()).digest())
    return Fernet(key)


def load_state(path=None) -> dict:
    path = path or STATE_FILE
    state = {}
    try:
        with open(path, "rb") as f:
            blob = f.read()
        f_ = _fernet()
        state = json.loads(f_.decrypt(blob) if f_ else blob)
    except FileNotFoundError:
        try:  # ترحيل من الملف القديم غير المشفر
            with open(LEGACY_STATE_FILE, encoding="utf-8") as f:
                state = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            state = {}
    except Exception as exc:
        print(f"state unreadable ({type(exc).__name__}), starting fresh", file=sys.stderr)
        state = {}
    return normalize_state(state)


def save_state(state: dict, path=None):
    path = path or STATE_FILE
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    data = json.dumps(state, ensure_ascii=False).encode()
    f_ = _fernet()
    with open(path, "wb") as f:
        f.write(f_.encrypt(data) if f_ else data)
    if os.path.exists(LEGACY_STATE_FILE) and os.path.abspath(path) != os.path.abspath(LEGACY_STATE_FILE):
        os.remove(LEGACY_STATE_FILE)  # ما نخلي نسخة مكشوفة


def normalize_state(state: dict, admin: str = None) -> dict:
    state.setdefault("users", {})
    state.setdefault("pending", [])
    state.setdefault("usage", {})
    # ترحيل الصيغة القديمة (مستخدم واحد) لحساب المدير
    legacy = {k: state.pop(k) for k in ("last_report", "watches", "mode", "last_request", "sources") if k in state}
    if legacy:
        admin = admin or os.environ.get("TELEGRAM_CHAT_ID", "admin")
        state["users"].setdefault(admin, {"name": "محمد"}).update(legacy)
    return state


def user_state(state: dict, chat_id: str, name: str = "") -> dict:
    u = state["users"].setdefault(str(chat_id), {"name": name})
    u.setdefault("last_report", [])
    u.setdefault("watches", [])
    return u


def _now():
    return datetime.now(KSA)


def _sar(sd: dict):
    rate = CURRENCY_TO_SAR.get((sd.get("currency") or "SAR").upper())
    return round(sd["price"] * rate, 2) if rate and sd.get("price") else None


def add_watch(tg, state, n: int, fetch=None) -> str:
    fetch = fetch or fetch_structured
    report = state["last_report"]
    if not 1 <= n <= len(report):
        return f"ما فيه خيار رقم {n} في آخر تقرير." if report else "ما فيه تقرير سابق. أرسل طلب بحث أول."
    item = report[n - 1]
    if any(w["url"] == item["url"] for w in state["watches"]):
        return f"الخيار {n} تحت المراقبة من قبل."
    if len(state["watches"]) >= MAX_WATCHES:
        return f"وصلت الحد ({MAX_WATCHES} منتج). ألغِ مراقبة وحدة أول."
    sd = fetch(item["url"])
    price = _sar(sd)
    if price is None:
        return (f"ما أقدر أراقب الخيار {n}، لأن صفحته ما تعطي سعر قابل للقراءة الآلية.\n"
                f"{item['url']}")
    state["watches"].append({
        "name": item["name"], "url": item["url"], "price_sar": price,
        "available": sd.get("available"), "added": _now().isoformat(),
        "last_checked": _now().isoformat(), "fails": 0,
    })
    return f"✅ صار تحت المراقبة: {item['name']}\nالسعر الحالي {price:g} ريال. بنبهك إذا نزل السعر أو رجع متوفر."


def list_watches(state) -> str:
    if not state["watches"]:
        return "ما عندك منتجات تحت المراقبة."
    lines = ["📌 تحت المراقبة:"]
    for i, w in enumerate(state["watches"], 1):
        lines.append(f"{i}) {w['name']} - {w['price_sar']:g} ريال\n{w['url']}")
    lines.append("\nلإلغاء وحدة: الغ ورقمها، مثل: الغ 1")
    return "\n".join(lines)


def remove_watch(state, n: int) -> str:
    if not 1 <= n <= len(state["watches"]):
        return f"ما فيه رقم {n} في قائمة المراقبة."
    w = state["watches"].pop(n - 1)
    return f"وقّفت مراقبة: {w['name']}"


def check_watches(tg, state, fetch=None, now=None) -> int:
    """يفحص المنتجات اللي صار لها 24 ساعة من آخر فحص، وينبه عند نزول السعر أو رجوع التوفر."""
    fetch = fetch or fetch_structured
    now = now or _now()
    checked = 0
    for w in state["watches"]:
        last = datetime.fromisoformat(w["last_checked"])
        if now - last < WATCH_EVERY:
            continue
        checked += 1
        w["last_checked"] = now.isoformat()
        sd = fetch(w["url"])
        price = _sar(sd)
        if price is None:
            w["fails"] = w.get("fails", 0) + 1
            if w["fails"] == 3:
                tg.send(f"⚠️ ما قدرت أقرأ سعر هذا المنتج 3 أيام ورا بعض، يمكن الرابط تغيّر:\n{w['name']}\n{w['url']}")
            continue
        w["fails"] = 0
        old = w["price_sar"]
        if sd.get("available") is True and w.get("available") is False:
            tg.send(f"🟢 رجع متوفر: {w['name']}\n{price:g} ريال\n{w['url']}")
        elif sd.get("available") is False and w.get("available") is not False:
            tg.send(f"🔴 صار نافد: {w['name']}\n{w['url']}")
        if price < old - 0.01 and sd.get("available") is not False:
            pct = (old - price) / old * 100
            tg.send(f"📉 نزل السعر: {w['name']}\nمن {old:g} إلى {price:g} ريال (-{pct:.0f}%)\n{w['url']}")
        w["price_sar"] = price
        if sd.get("available") is not None:
            w["available"] = sd["available"]
    print(f"watches={len(state['watches'])} checked={checked}")
    return checked


# ---------------------------------------------------------------- المحادثة

SOURCE_NAMES = [name for name, _, _ in SOURCES] + [f"قناة {c}" for c in CHANNELS]
SESSION_IDLE = timedelta(minutes=int(os.environ.get("SESSION_IDLE_MIN", "10")))
SESSION_MAX = timedelta(minutes=int(os.environ.get("SESSION_MAX_MIN", "15")))
URL_RE = re.compile(r"https?://\S+")
AR_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")

MENU = (
    "اختر رقم:\n"
    "1 راقب سعر خيار\n"
    "2 أعد البحث\n"
    "3 عدّل الطلب\n"
    "4 عدّل المواقع\n"
    "5 قائمة المراقبة\n"
    "6 بحث جديد\n"
    "0 القائمة\n\n"
    "وتقدر ترسل رابط منتج أو صورته مباشرة."
)

DESCRIBE_PROMPT = """{what}
حدد المنتج بدقة كطلب شراء قصير للبحث: النوع، الماركة، الموديل، وأهم المواصفات.
- اقرأ أي كتابة أو شعار أو رقم موديل ظاهر على المنتج أو العلبة، واعتمد عليه أولاً.
- لا تخمّن ماركة أو موديل غير ظاهر؛ إذا ما تأكدت اكتب النوع والمواصفات الظاهرة بس.
رجّع JSON فقط: {{"request": "...", "sure": true}}"""


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").translate(AR_DIGITS)).strip()


def sources_text(state) -> str:
    enabled = state.get("sources") or SOURCE_NAMES
    lines = ["المواقع (✅ مفعّل، ❌ موقف):"]
    for i, name in enumerate(SOURCE_NAMES, 1):
        lines.append(f"{i} {'✅' if name in enabled else '❌'} {name}")
    lines.append("\nأرسل رقم أو أكثر للتبديل (مثل: 7 9)، و 0 للحفظ والرجوع.")
    return "\n".join(lines)


def toggle_sources(state, numbers) -> str:
    enabled = list(state.get("sources") or SOURCE_NAMES)
    for n in numbers:
        if 1 <= n <= len(SOURCE_NAMES):
            name = SOURCE_NAMES[n - 1]
            if name in enabled:
                enabled.remove(name)
            else:
                enabled.append(name)
    if not enabled:
        return "لازم يبقى موقع واحد مفعّل على الأقل.\n\n" + sources_text(state)
    state["sources"] = [n for n in SOURCE_NAMES if n in enabled]
    return sources_text(state)


def describe_image(data: bytes, llm=gemini) -> str:
    raw = llm(DESCRIBE_PROMPT.format(what="هذي صورة منتج."), image=(data, "image/jpeg"))
    try:
        req = json.loads(raw).get("request", "")
    except (json.JSONDecodeError, AttributeError):
        req = parse_result("<json>" + raw + "</json>").get("request", "")
    return (req or "").strip()


def describe_link(url: str, fetch=None, extract=None, llm=gemini):
    """يرجع (طلب البحث، سعر الرابط بالريال أو None)."""
    fetch, extract = fetch or fetch_structured, extract or tavily_extract
    sd = fetch(url)
    name, ref = sd.get("name", ""), _sar(sd) if sd.get("price") else None
    if not name:
        try:
            text = extract([url]).get(url, "")[:1500]
        except Exception:
            text = ""
        if text:
            raw = llm(DESCRIBE_PROMPT.format(what=f"هذا نص صفحة منتج:\n{text}"))
            try:
                name = json.loads(raw).get("request", "")
            except (json.JSONDecodeError, AttributeError):
                name = ""
    return name.strip(), ref


RETRY_WAIT = int(os.environ.get("RETRY_WAIT", "20"))


def error_detail(exc) -> str:
    """وصف الخطأ بدون أسرار: نوعه، والخدمة، ورمز الحالة."""
    resp = getattr(exc, "response", None)
    if resp is None:
        return type(exc).__name__
    host = re.sub(r"^https?://([^/]+).*$", r"\1", resp.url or "")
    return f"{type(exc).__name__} {resp.status_code} {host} {resp.text[:200]}"


def handle_request(tg: Telegram, text: str, research_fn=research, sources=None) -> list:
    tg.send(f"استلمت طلبك، جاري البحث في {len(sources or SOURCE_NAMES)} مواقع.")
    try:
        result = research_fn(text, sources=sources)
    except requests.RequestException as exc:
        # غالباً ضغط مؤقت على Gemini أو Tavily: نعيد مرة وحدة تلقائياً قبل ما نبلغ بالخطأ
        print(f"research retry after {error_detail(exc)}", file=sys.stderr, flush=True)
        tg.send("الخدمة مشغولة شوي، أعيد المحاولة تلقائياً...")
        time.sleep(RETRY_WAIT)
        result = research_fn(text, sources=sources)
    tg.send(format_report(text, result))
    return display_options(result)


def do_search(tg, state, request: str, research_fn) -> None:
    state["last_request"] = request
    err = tg.quota_error() if hasattr(tg, "quota_error") else None
    if err:
        tg.send(err)
        return
    if hasattr(tg, "count_search"):
        tg.count_search()
    options = handle_request(tg, request, research_fn, state.get("sources"))
    state["last_report"] = [
        {"name": o.get("name", ""), "url": o.get("url", ""), "price_sar": o.get("price_sar")}
        for o in options
    ]
    tg.send(MENU)


def handle_command(tg, state, text: str, fetch=None) -> bool:
    """أوامر المراقبة النصية (راقب 2 / الغ 1 / المراقبة)."""
    t = norm(text)
    m = WATCH_RE.match(t)
    if m:
        tg.send(add_watch(tg, state, int(m.group(1)), fetch))
        return True
    m = UNWATCH_RE.match(t)
    if m:
        tg.send(remove_watch(state, int(m.group(1))))
        return True
    if t in LIST_WORDS:
        tg.send(list_watches(state))
        return True
    return False


def handle_message(tg, state, msg: dict, research_fn=research, fetch=None, llm=gemini) -> bool:
    """يرجع True إذا انعمل بحث."""
    text = norm(msg.get("text") or msg.get("caption") or "")
    mode = state.get("mode", "")
    numbers = [int(x) for x in text.split()] if text and all(x.isdigit() for x in text.split()) else []

    # صورة منتج
    if msg.get("photo"):
        state["mode"] = ""
        tg.send("استلمت الصورة، أتعرف على المنتج...")
        data = tg.download(msg["photo"][-1]["file_id"])
        req = describe_image(data, llm)
        if not req:
            tg.send("ما قدرت أتعرف على المنتج من الصورة. اكتب اسمه أو أرسل رابطه.")
            return False
        tg.send(f"المنتج اللي فهمته من الصورة: {req}\n(إذا مو صحيح، أرسل 3 بعد التقرير وعدّل الطلب، أو اكتب اسمه مع الصورة)")
        do_search(tg, state, f"{req} {text}".strip(), research_fn)
        return True

    if text in ("0", "القائمة", "قائمة", "/start", "/help"):
        state["mode"] = ""
        tg.send(HELP_TEXT if text in ("/start", "/help") else MENU)
        return False

    if handle_command(tg, state, text, fetch):
        state["mode"] = ""
        return False

    # أوضاع تنتظر رد
    if mode == "watch_pick" and len(numbers) == 1:
        state["mode"] = ""
        tg.send(add_watch(tg, state, numbers[0], fetch) + "\n\n0 للقائمة")
        return False
    if mode == "unwatch_pick" and len(numbers) == 1:
        state["mode"] = ""
        tg.send(remove_watch(state, numbers[0]) + "\n\n0 للقائمة")
        return False
    if mode == "sources" and numbers:
        tg.send(toggle_sources(state, numbers))
        return False
    if mode == "edit" and text and not numbers:
        state["mode"] = ""
        do_search(tg, state, f"{state.get('last_request', '')}، مع التعديل: {text}", research_fn)
        return True

    # القائمة الرئيسية
    if len(numbers) == 1 and 1 <= numbers[0] <= 6:
        choice = numbers[0]
        report, last = state.get("last_report", []), state.get("last_request")
        if choice == 1:
            if not report:
                tg.send("ما فيه تقرير سابق. اكتب طلب بحث أول.")
                return False
            lines = ["أي خيار تبي أراقب سعره؟ أرسل رقمه:"]
            lines += [f"{i} {r['name']} - {r['price_sar']:g} ريال" if isinstance(r.get("price_sar"), (int, float))
                      else f"{i} {r['name']}" for i, r in enumerate(report, 1)]
            state["mode"] = "watch_pick"
            tg.send("\n".join(lines))
        elif choice in (2, 3) and not last:
            tg.send("ما فيه طلب سابق. اكتب طلب بحث أول.")
        elif choice == 2:
            state["mode"] = ""
            do_search(tg, state, last, research_fn)
            return True
        elif choice == 3:
            state["mode"] = "edit"
            tg.send(f"طلبك الحالي:\n{last}\n\nاكتب التعديل، مثل: الميزانية 700، أو مستعمل بس، أو بدون ساعة ضغط.")
        elif choice == 4:
            state["mode"] = "sources"
            tg.send(sources_text(state))
        elif choice == 5:
            tg.send(list_watches(state) + ("\n\nأو أرسل رقم المنتج لإلغاء مراقبته." if state["watches"] else ""))
            state["mode"] = "unwatch_pick" if state["watches"] else ""
        elif choice == 6:
            state["mode"] = "new"
            tg.send("اكتب وش تبي تدور، أو أرسل رابط أو صورة منتج.")
        return False

    if not text:
        return False
    if numbers:  # رقم ما له معنى في الوضع الحالي
        tg.send("ما فهمت الرقم.\n\n" + MENU)
        return False
    mode, state["mode"] = state.get("mode", ""), ""

    # رابط منتج
    m = URL_RE.search(text)
    if m:
        url = m.group(0)
        tg.send("استلمت الرابط، أتعرف على المنتج...")
        req, ref = describe_link(url, fetch, llm=llm)
        if not req:
            tg.send("ما قدرت أقرأ المنتج من الرابط. اكتب اسمه ومواصفاته.")
            return False
        extra = URL_RE.sub("", text).strip()
        tg.send(f"المنتج: {req}" + (f"\nسعره في الرابط: {ref:g} ريال" if ref else ""))
        do_search(tg, state, f"{req} {extra}".strip(), research_fn)
        return True

    # نص حر: يبحث بس إذا اختار "6 بحث جديد" قبلها، وإلا تطلع القائمة
    if mode == "new":
        do_search(tg, state, text, research_fn)
        return True
    tg.send("هلا 👋\n\n" + MENU)
    return False


# ---------------------------------------------------------------- المستخدمين

MONTHLY_SEARCHES = int(os.environ.get("MONTHLY_SEARCHES", "48"))   # حصة Tavily المجانية تقريباً
USER_MONTHLY_SEARCHES = int(os.environ.get("USER_MONTHLY_SEARCHES", "5"))  # لكل عضو غير المدير
ADMIN_RE = re.compile(r"^(قبول|اقبل|موافق|رفض|ارفض|احذف عضو|حذف عضو)\s*(\d*)$")


class ChatTG:
    """يوجّه الرسائل لمحادثة مستخدم معيّن، ويتتبع حصة البحث."""

    def __init__(self, tg, state, chat_id, is_admin):
        self.tg, self.state, self.chat_id, self.is_admin = tg, state, str(chat_id), is_admin

    def send(self, text):
        self.tg.send(text, chat_id=self.chat_id)

    def download(self, file_id):
        return self.tg.download(file_id)

    def _usage(self):
        month = _now().strftime("%Y-%m")
        u = self.state["usage"]
        if u.get("month") != month:
            u.clear()
            u.update({"month": month, "total": 0, "by_user": {}})
        return u

    def quota_error(self):
        u = self._usage()
        if u["total"] >= MONTHLY_SEARCHES:
            return "خلصت حصة البحث المجانية لهذا الشهر لكل الأعضاء. ترجع أول الشهر الجاي."
        if not self.is_admin and u["by_user"].get(self.chat_id, 0) >= USER_MONTHLY_SEARCHES:
            return f"خلصت حصتك لهذا الشهر ({USER_MONTHLY_SEARCHES} بحوث). ترجع أول الشهر الجاي."
        return None

    def count_search(self):
        u = self._usage()
        u["total"] += 1
        u["by_user"][self.chat_id] = u["by_user"].get(self.chat_id, 0) + 1


def members_text(state, admin: str) -> str:
    usage = state["usage"].get("by_user", {}) if state["usage"].get("month") == _now().strftime("%Y-%m") else {}
    lines = [f"الأعضاء (بحوث هذا الشهر: {sum(usage.values())} من {MONTHLY_SEARCHES}):"]
    others = [(cid, u) for cid, u in state["users"].items() if cid != admin]
    for i, (cid, u) in enumerate(others, 1):
        lines.append(f"{i} {u.get('name') or cid} - {usage.get(cid, 0)} بحث")
    if not others:
        lines.append("ما فيه أعضاء غيرك.")
    if state["pending"]:
        lines.append("\nطلبات انضمام:")
        lines += [f"{i} {p['name']}" for i, p in enumerate(state["pending"], 1)]
        lines.append("أرسل: قبول رقم، أو رفض رقم")
    if others:
        lines.append("\nلحذف عضو: احذف عضو ورقمه")
    return "\n".join(lines)


def handle_admin(tg, state, text) -> bool:
    """أوامر المدير: الأعضاء / قبول N / رفض N / احذف عضو N."""
    admin = tg.tg.chat_id
    t = norm(text)
    if t in ("الأعضاء", "الاعضاء", "/members"):
        tg.send(members_text(state, admin))
        return True
    m = ADMIN_RE.match(t)
    if not m:
        return False
    action = {"اقبل": "قبول", "موافق": "قبول", "ارفض": "رفض"}.get(m.group(1), m.group(1))
    if not m.group(2):  # بدون رقم: ينفع بس إذا فيه طلب واحد
        if action in ("قبول", "رفض") and len(state["pending"]) == 1:
            n = 1
        else:
            tg.send(members_text(state, admin) + "\n\nحدد الرقم، مثل: قبول 1")
            return True
    else:
        n = int(m.group(2))
    if action in ("قبول", "رفض"):
        if not 1 <= n <= len(state["pending"]):
            tg.send(f"ما فيه طلب رقم {n}.")
            return True
        p = state["pending"].pop(n - 1)
        if action == "قبول":
            user_state(state, p["id"], p["name"])
            tg.send(f"✅ تمت إضافة {p['name']}.")
            tg.tg.send("✅ تم قبولك في وكيل التسعير.\n\n" + HELP_TEXT, chat_id=p["id"])
        else:
            tg.send(f"رفضت طلب {p['name']}.")
        return True
    others = [cid for cid in state["users"] if cid != admin]
    if not 1 <= n <= len(others):
        tg.send(f"ما فيه عضو رقم {n}.")
        return True
    u = state["users"].pop(others[n - 1])
    tg.send(f"حذفت {u.get('name') or others[n - 1]}.")
    return True


def handle_stranger(tg, state, msg) -> None:
    chat = msg.get("chat") or {}
    cid = str(chat.get("id", ""))
    frm = msg.get("from") or {}
    name = " ".join(x for x in (frm.get("first_name"), frm.get("last_name")) if x) or "بدون اسم"
    if frm.get("username"):
        name += f" (@{frm['username']})"
    if any(p["id"] == cid for p in state["pending"]):
        tg.send("طلبك وصل لمحمد وينتظر موافقته.", chat_id=cid)
        return
    state["pending"].append({"id": cid, "name": name})
    n = len(state["pending"])
    how = "قبول، أو رفض" if n == 1 else f"قبول {n}، أو رفض {n}"
    tg.send(f"🔔 {name} يبي يستخدم وكيل التسعير.\nأرسل كلمة: {how}")
    tg.send("هلا! أرسلت طلبك لمحمد، وبيوصلك إشعار إذا وافق.", chat_id=cid)


# ---------------------------------------------------------------- التشغيل

def run_watch_checks(tg, state, fetch=None):
    for cid in list(state["users"]):
        try:
            check_watches(ChatTG(tg, state, cid, cid == tg.chat_id), user_state(state, cid), fetch)
        except Exception as exc:
            print(f"watch check failed: {type(exc).__name__}", file=sys.stderr)
    save_state(state)


def commit_state():
    """يحفظ الحالة المشفرة في المستودع أثناء التشغيل الطويل (عشان ما تضيع لو انقطعت التشغيلة)."""
    if not os.environ.get("GITHUB_ACTIONS"):
        return
    import subprocess
    cmds = [
        ["git", "add", "-A", "state/"],
        ["git", "-c", "user.name=pricing-agent", "-c", "user.email=pricing-agent@users.noreply.github.com",
         "commit", "-q", "-m", "Update state [skip ci]"],
        ["git", "pull", "--rebase", "-q"],
        ["git", "push", "-q"],
    ]
    try:
        if not subprocess.run(["git", "status", "--porcelain", "state/"], capture_output=True, text=True).stdout:
            return
        for c in cmds:
            subprocess.run(c, check=True, capture_output=True, timeout=60)
    except Exception as exc:
        print(f"state commit failed: {type(exc).__name__}", file=sys.stderr)


def run(tg: Telegram, research_fn=research, state=None, fetch=None, llm=gemini, clock=None) -> int:
    """يعالج الرسائل، ولو فيه محادثة شغالة يبقى يستقبل الردود فوراً لين 10 دقايق بدون رسائل."""
    clock = clock or _now
    state = normalize_state(state, tg.chat_id) if state is not None else load_state()
    start, last_activity, handled, first = clock(), None, 0, True
    # وضع التشغيل الدائم: يبقى يستقبل لين يخلص وقت التشغيلة، وبعدها الـ workflow يشغّل اللي بعدها
    forever = timedelta(minutes=int(os.environ.get("LOOP_MINUTES", "0")))
    last_watch_check = last_commit = start
    if not forever and os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch" \
            and os.environ.get("KEEP_OPEN", "1") == "1":
        last_activity = start  # التشغيل اليدوي يفتح جلسة استماع حتى لو ما فيه رسائل
    while True:
        updates = tg.get_updates(wait=0 if first else 50)
        first = False
        for update in updates:
            uid = update["update_id"]
            msg = update.get("message") or {}
            chat = msg.get("chat") or {}
            chat_id = str(chat.get("id", ""))
            is_admin = chat_id == tg.chat_id
            ctg = ChatTG(tg, state, chat_id, is_admin)
            try:
                if chat.get("type", "private") != "private" or not chat_id:
                    pass  # نتجاهل القروبات
                elif is_admin or chat_id in state["users"]:
                    last_activity = clock()
                    user = user_state(state, chat_id)
                    if is_admin and handle_admin(ctg, state, msg.get("text") or ""):
                        pass
                    elif handle_message(ctg, user, msg, research_fn, fetch, llm):
                        handled += 1
                else:
                    handle_stranger(tg, state, msg)
            except Exception as exc:  # نكمل باقي الرسائل حتى لو وحدة فشلت
                detail = error_detail(exc)
                print(f"update {uid} failed: {detail}", file=sys.stderr, flush=True)
                try:
                    ctg.send("صار خطأ مؤقت في خدمة البحث. أعد المحاولة بعد شوي، أو 0 للقائمة.")
                    tg.send(f"⚠️ خطأ تقني (للمدير): {detail[:300]}")  # عشان نعرف السبب ونصلّحه
                except Exception:
                    pass
            finally:
                tg.ack(uid)  # نأكد الاستلام عشان ما تتكرر الرسالة
                save_state(state)
        now = clock()
        if forever:
            if now - last_watch_check > timedelta(minutes=30):
                last_watch_check = now
                run_watch_checks(tg, state, fetch)
            if now - last_commit > timedelta(minutes=20):
                last_commit = now
                commit_state()
            if now - start > forever:
                break
            continue
        if last_activity is None or now - last_activity > SESSION_IDLE or now - start > SESSION_MAX:
            break

    run_watch_checks(tg, state, fetch)
    save_state(state)
    print(f"handled={handled} users={len(state['users'])}")
    return handled


def main():
    missing = [k for k in ("GEMINI_API_KEY", "TAVILY_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID") if not os.environ.get(k)]
    if missing:
        sys.exit(f"Missing secrets: {', '.join(missing)}")
    tg = Telegram(os.environ["TELEGRAM_BOT_TOKEN"], os.environ["TELEGRAM_CHAT_ID"])
    me = tg.call("getMe")
    print(f"bot=@{me.get('username')}")  # للتأكد إن التوكن حق البوت الصحيح
    if os.environ.get("GROQ_API_KEY"):
        groq_models()  # يتأكد إن مفتاح Groq شغال ويطبع النماذج المتاحة
    hook = tg.call("getWebhookInfo")
    print(f"webhook_set={bool(hook.get('url'))} pending={hook.get('pending_update_count')}")
    if hook.get("url"):
        tg.call("deleteWebhook")  # الويب هوك يمنع getUpdates
    run(tg)


if __name__ == "__main__":
    main()

"""اختبارات بدون إنترنت: تيليجرام و Claude وهميين."""
import json

import pytest
from datetime import timedelta

import agent


@pytest.fixture(autouse=True)
def tmp_state(tmp_path, monkeypatch):
    monkeypatch.setattr(agent, "STATE_FILE", str(tmp_path / "state.enc"))
    monkeypatch.setattr(agent, "LEGACY_STATE_FILE", str(tmp_path / "state.json"))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setattr(agent, "RETRY_WAIT", 0)
    monkeypatch.setattr(agent, "SESSION_IDLE", timedelta(0))  # الاختبارات ما تنتظر جلسة


class FakeTG:
    def __init__(self, updates, chat_id="111"):
        self.updates = updates
        self.chat_id = chat_id
        self.sent = []
        self.sent_to = []
        self.acked = []

    def get_updates(self, offset=None, wait=0):
        out, self.updates = self.updates, []
        return out

    def download(self, file_id):
        return b"img"

    def ack(self, uid):
        self.acked.append(uid)

    def send(self, text, chat_id=None):
        self.sent.append(text)
        self.sent_to.append((str(chat_id or self.chat_id), text))


SAMPLE = {
    "summary": "الأسعار بين 449 و 900",
    "recommendation": "الخيار الأول",
    "options": [
        {"name": "B", "price_sar": 900, "url": "https://b"},
        {"name": "A", "price_sar": 449, "vat": "شامل", "coupon": "SAVE10", "url": "https://a"},
        {"name": "C", "price_sar": None},
    ],
    "warnings": ["تأكد من المخزون"],
}


def upd(uid, chat, text):
    return {"update_id": uid, "message": {"chat": {"id": int(chat)}, "text": text}}


def test_parse_result_json_tag():
    text = "بحثت...\n<json>" + json.dumps(SAMPLE, ensure_ascii=False) + "</json>"
    assert agent.parse_result(text)["options"][0]["name"] == "B"


def test_parse_result_fallback():
    r = agent.parse_result("نص بدون json")
    assert r["options"] == [] and r["raw"] == "نص بدون json"


def test_report_sorted_cheapest_first_no_recommendation():
    data = dict(SAMPLE)
    data["options"] = [dict(o, opinion="سعره ممتاز") for o in SAMPLE["options"]]
    rep = agent.format_report("فلتر", data)
    assert rep.index("1) A") < rep.index("2) B")
    assert "449 ريال" in rep and "SAVE10" in rep and ") C" not in rep  # بدون سعر ينحذف
    assert "💬 سعره ممتاز" in rep
    assert "التوصية" not in rep and "تنبيهات" not in rep and "الخيار الأول" not in rep


def test_split_message():
    text = "\n".join(["x" * 100] * 100)  # ~10k حرف
    parts = agent.split_message(text)
    assert all(len(p) <= agent.TG_LIMIT for p in parts)
    assert "".join(parts).replace("\n", "") == text.replace("\n", "")


def test_run_ignores_strangers_and_acks_all():
    tg = FakeTG([upd(1, "999", "فلتر"), upd(2, "111", "/start"), upd(9, "111", "6"), upd(3, "111", "فلتر جامبو")])
    calls = []
    handled = agent.run(tg, research_fn=lambda t, **k: calls.append(t) or SAMPLE)
    assert handled == 1 and calls == ["فلتر جامبو"]  # الغريب ما ينفذ بحث
    assert tg.acked == [1, 2, 9, 3]
    assert ("111", agent.HELP_TEXT) in tg.sent_to
    assert any(c == "111" and "يبي يستخدم" in m for c, m in tg.sent_to)
    assert any(c == "999" and "أرسلت طلبك" in m for c, m in tg.sent_to)


def test_run_survives_research_error():
    tg = FakeTG([upd(1, "111", "6"), upd(5, "111", "طلب"), upd(2, "111", "6"), upd(6, "111", "طلب2")])

    def boom(_, **k):
        raise ValueError("x")

    agent.run(tg, research_fn=boom)
    assert tg.acked == [1, 5, 2, 6]
    assert sum("صار خطأ" in s for s in tg.sent) == 2
    assert sum("خطأ تقني" in s for s in tg.sent) == 2


def test_research_pipeline_dedup_and_prompt():
    calls = []

    def fake_llm(prompt, system=""):
        calls.append((prompt, system))
        if not system:
            return json.dumps({"ar": "فلتر", "en": "filter"})
        return json.dumps(SAMPLE, ensure_ascii=False)

    def fake_search(q, domains=None):
        tag = (domains or ["local"])[0]
        return [{"title": "t", "url": "https://same", "content": "449 ريال"},
                {"title": q, "url": f"https://{tag}", "content": "x"}]

    result = agent.research("فلتر", llm=fake_llm, search=fake_search,
                            extract=lambda urls: {"https://noon.com": "فلتر جامبو كامل السعر 496 ر.س شامل الضريبة",
                                                  "https://ebay.com": "Jumbo filter Place bid now"},
                            fetch=lambda u: {"price": 100.0, "currency": "SAR"}
                            if u in ("https://noon.com", "https://a", "https://b") else {})
    # noon.com مباشرة من بيانات الصفحة، و A و B من Gemini، و C بدون رابط = ينحذف
    assert sorted(o["url"] for o in result["options"]) == ["https://a", "https://b", "https://noon.com"]
    final_prompt = [p for p, s in calls if s][0]
    assert final_prompt.count("https://same") == 1 and "https://noon.com" in final_prompt
    assert "496 ر.س" in final_prompt
    assert "https://ebay.com" not in final_prompt  # مزاد ينحذف


def test_research_no_results():
    r = agent.research("x", llm=lambda p, s="": '{"ar":"a","en":"a"}', search=lambda q, d=None: [], extract=lambda u: {}, fetch=lambda u: {})
    assert r["options"] == [] and "ما رجع" in r["raw"]


def test_plan_covers_all_sources_both_languages():
    plan = agent.plan_queries("طلب", llm=lambda p, s="": '{"ar":"فلتر","en":"filter"}')
    names = {n for n, _, _ in plan}
    assert len(names) == 10 and len(plan) == 16
    assert ("أمازون", "فلتر سعر", ["amazon.sa"]) in plan
    assert any(n == "علي بابا" and "filter" in q for n, q, _ in plan)


def test_terms_fallback_on_bad_json():
    plan = agent.plan_queries("طلب", llm=lambda p, s="": "not json")
    assert all(q.startswith("طلب") for _, q, _ in plan)

def test_price_snippets_finds_prices():
    text = "فلتر جامبو ثلاث مراحل " + "كلام " * 300 + " السعر 496 ر.س شامل الضريبة " + "نص " * 300 + " SAR 1,299 قبل الخصم"
    snip = agent.price_snippets(text)
    assert "496 ر.س" in snip and "SAR 1,299" in snip and len(snip) < len(text)


def test_research_survives_extract_failure():
    def boom(urls):
        raise RuntimeError("x")
    r = agent.research("x", llm=lambda p, s="": '{"ar":"a","en":"a"}' if not s else '{"options":[]}',
                       search=lambda q, d=None: [{"url": "https://a", "title": "", "content": ""}], extract=boom,
                       fetch=lambda u: {})
    assert r["options"] == []


JSONLD = """<html><script type="application/ld+json">{"@context":"https://schema.org","@graph":[
{"@type":"BreadcrumbList"},{"@type":"Product","name":"Jumbo","offers":{"@type":"Offer","price":"1,200.00",
"priceCurrency":"SAR","availability":"https://schema.org/InStock"}}]}</script></html>"""


def test_parse_structured_price_jsonld():
    sd = agent.parse_structured_price(JSONLD)
    assert sd["price"] == 1200.0 and sd["currency"] == "SAR" and sd["available"] is True


def test_parse_structured_price_meta_and_oos():
    html = '<meta property="product:price:amount" content="399"><meta property="product:price:currency" content="SAR">'
    assert agent.parse_structured_price(html)["price"] == 399.0
    oos = JSONLD.replace("InStock", "OutOfStock")
    assert agent.parse_structured_price(oos)["available"] is False


def test_is_part():
    assert agent.is_part("طقم فلاتر الجامبو 20 إنش", "فلتر جامبو 3 مراحل")
    assert not agent.is_part("فلتر جامبو 3 مراحل مع ساعة", "فلتر جامبو 3 مراحل")
    assert not agent.is_part("طقم شمعات 20 انش", "ابي طقم شمعات 20 انش")


def test_verify_overrides_price_and_filters():
    opts = [
        {"name": "فلتر جامبو 3 مراحل", "price_sar": 211.98, "url": "https://trendyol"},
        {"name": "طقم فلاتر الجامبو", "price_sar": 399, "url": "https://nqa"},
        {"name": "فلتر جامبو مرحلتين", "price_sar": 300, "url": "https://two"},
        {"name": "فلتر جامبو نافد", "price_sar": 350, "url": "https://oos"},
    ]
    fetched = {"https://trendyol": {"price": 1200.0, "currency": "SAR", "available": True},
               "https://oos": {"price": 350.0, "currency": "SAR", "available": False}}
    llm = lambda p, s="": json.dumps({"checks": [{"i": 0, "complete": True, "matches": True},
                                                  {"i": 1, "complete": True, "matches": False}]})
    out = agent.verify_options("فلتر جامبو 3 مراحل", opts, {}, llm=llm, fetch=lambda u: fetched.get(u, {}))
    assert [o["name"] for o in out] == ["فلتر جامبو 3 مراحل"]
    assert out[0]["price_sar"] == 1200.0 and out[0]["price_verified"]


def test_verify_converts_currency():
    out = agent.verify_options("x", [{"name": "a", "url": "u", "price_sar": 1}], {},
                               llm=lambda p, s="": '{"checks":[{"i":0,"complete":true,"matches":true}]}',
                               fetch=lambda u: {"price": 100.0, "currency": "USD"})
    assert out[0]["price_sar"] == 375.0 and "USD" in out[0]["price_note"]


def test_foreign_storefront_filter():
    r = agent.FOREIGN_STOREFRONT_RE
    assert r.search("https://www.noon.com/oman-ar/jumbo/p")
    assert r.search("https://www.noon.com/uae-en/x/p")
    assert not r.search("https://www.noon.com/saudi-ar/x/p")
    assert not r.search("https://www.amazon.sa/dp/B0")
    assert not r.search("https://www.trendyol.com/ar/naqaa/x-p-1")


def _state_with_report():
    return {"last_report": [{"name": "فلتر A", "url": "https://a", "price_sar": 400},
                            {"name": "فلتر B", "url": "https://b", "price_sar": 500}],
            "watches": []}


def test_watch_add_list_remove():
    tg, st = FakeTG([]), _state_with_report()
    fetch = lambda u: {"price": 500.0, "currency": "SAR", "available": True}
    assert agent.handle_command(tg, st, "راقب ٢", fetch)
    assert st["watches"][0]["url"] == "https://b" and "500" in tg.sent[-1]
    agent.handle_command(tg, st, "راقب 2", fetch)
    assert "من قبل" in tg.sent[-1] and len(st["watches"]) == 1
    agent.handle_command(tg, st, "المراقبة", fetch)
    assert "فلتر B" in tg.sent[-1]
    agent.handle_command(tg, st, "الغ 1", fetch)
    assert st["watches"] == []
    assert not agent.handle_command(tg, st, "فلتر جامبو 3 مراحل", fetch)


def test_watch_rejects_unreadable_page():
    tg, st = FakeTG([]), _state_with_report()
    agent.handle_command(tg, st, "راقب 1", lambda u: {})
    assert st["watches"] == [] and "ما أقدر أراقب" in tg.sent[-1]


def test_check_watches_price_drop_and_restock():
    tg = FakeTG([])
    old = (agent._now() - timedelta(hours=25)).isoformat()
    st = {"last_report": [], "watches": [
        {"name": "A", "url": "https://a", "price_sar": 500.0, "available": True, "last_checked": old, "fails": 0},
        {"name": "B", "url": "https://b", "price_sar": 300.0, "available": False, "last_checked": old, "fails": 0},
        {"name": "C", "url": "https://c", "price_sar": 100.0, "available": True,
         "last_checked": agent._now().isoformat(), "fails": 0},
    ]}
    data = {"https://a": {"price": 450.0, "currency": "SAR", "available": True},
            "https://b": {"price": 300.0, "currency": "SAR", "available": True}}
    n = agent.check_watches(tg, st, fetch=lambda u: data.get(u, {}))
    assert n == 2  # C فُحص قريب
    assert any("نزل السعر" in m and "450" in m for m in tg.sent)
    assert any("رجع متوفر" in m for m in tg.sent)
    assert st["watches"][0]["price_sar"] == 450.0


def test_run_stores_last_report_and_persists():
    tg = FakeTG([upd(2, "111", "6"), upd(1, "111", "فلتر")])
    st = {"last_report": [], "watches": []}
    agent.run(tg, research_fn=lambda t, **k: SAMPLE, state=st, fetch=lambda u: {})
    user = st["users"]["111"]
    assert [r["url"] for r in user["last_report"]] == ["https://a", "https://b"]
    assert agent.load_state()["users"]["111"]["last_report"] == user["last_report"]


# ---------------------------------------------------------------- المحادثة بالأرقام

def _chat(state=None):
    tg = FakeTG([])
    calls = []
    def rf(t, **k):
        calls.append((t, k.get("sources")))
        return SAMPLE
    st = state or {"last_report": [], "watches": []}
    say = lambda text, **extra: agent.handle_message(tg, st, {"text": text, **extra}, rf,
                                                     fetch=lambda u: {"price": 400.0, "currency": "SAR", "name": "فلتر جامبو"},
                                                     llm=lambda *a, **k: '{"request": "فلتر جامبو 3 مراحل"}')
    return tg, st, calls, say


def test_menu_flow_search_watch_research_edit():
    tg, st, calls, say = _chat()
    assert not say("هلا")
    assert agent.MENU in tg.sent[-1] and calls == []
    say("6")
    assert st["mode"] == "new"
    assert say("فلتر جامبو 3 مراحل")
    assert tg.sent[-1] == agent.MENU and st["last_request"] == "فلتر جامبو 3 مراحل"
    say("١")  # راقب
    assert st["mode"] == "watch_pick" and "أرسل رقمه" in tg.sent[-1]
    say("2")
    assert st["mode"] == "" and st["watches"][0]["url"] == "https://b"
    assert say("2")  # أعد البحث
    assert calls[-1][0] == "فلتر جامبو 3 مراحل"
    say("3")
    assert st["mode"] == "edit"
    assert say("الميزانية 700")
    assert calls[-1][0] == "فلتر جامبو 3 مراحل، مع التعديل: الميزانية 700"


def test_menu_sources_toggle_applies_to_search():
    tg, st, calls, say = _chat()
    say("4")
    assert st["mode"] == "sources" and "✅ أمازون" in tg.sent[-1]
    say("6 8")  # حراج وإيباي
    assert "❌ حراج" in tg.sent[-1] and "حراج" not in st["sources"]
    say("0")
    assert st["mode"] == ""
    say("6")
    say("شاشة 27")
    assert "حراج" not in calls[-1][1] and "أمازون" in calls[-1][1]


def test_sources_cannot_disable_all():
    tg, st, calls, say = _chat()
    say("4")
    say(" ".join(str(i) for i in range(1, 11)))
    assert "لازم يبقى" in tg.sent[-1]


def test_menu_unwatch_and_no_report_guards():
    tg, st, calls, say = _chat()
    say("1")
    assert "ما فيه تقرير" in tg.sent[-1]
    say("2")
    assert "ما فيه طلب" in tg.sent[-1]
    st["watches"] = [{"name": "X", "url": "u", "price_sar": 1.0}]
    say("5")
    assert st["mode"] == "unwatch_pick"
    say("1")
    assert st["watches"] == []


def test_link_and_photo_requests():
    tg, st, calls, say = _chat()
    assert say("https://shop.sa/p/123 تحت 500")
    assert "سعره في الرابط: 400" in "\n".join(tg.sent)
    tg2, st2, calls2, say2 = _chat()
    agent.handle_message(tg2, st2, {"photo": [{"file_id": "a"}, {"file_id": "b"}]},
                         lambda t, **k: calls2.append(t) or SAMPLE,
                         llm=lambda *a, **k: '{"request": "خلاط كهربائي"}')
    assert calls2 == ["خلاط كهربائي"]


def test_session_keeps_listening_then_stops(monkeypatch):
    monkeypatch.setattr(agent, "SESSION_IDLE", timedelta(minutes=10))
    batches = [[upd(1, "111", "6"), upd(3, "111", "فلتر")], [upd(2, "111", "2")], [], []]
    times = iter([0, 1, 2, 3, 4, 5, 30, 31, 40, 41])  # دقائق

    class TG(FakeTG):
        def get_updates(self, offset=None, wait=0):
            return batches.pop(0) if batches else []
    tg = TG([])
    base = agent._now()
    clock = lambda: base + timedelta(minutes=next(times))
    n = agent.run(tg, research_fn=lambda t, **k: SAMPLE, state={"last_report": [], "watches": []},
                  fetch=lambda u: {}, clock=clock)
    assert n == 2 and tg.acked == [1, 3, 2]


def test_unknown_number_shows_menu():
    tg, st, calls, say = _chat()
    assert not say("9")
    assert calls == [] and agent.MENU in tg.sent[-1]


# ---------------------------------------------------------------- الأعضاء

def _msg(uid, chat, text, first="أحمد"):
    return {"update_id": uid, "message": {"chat": {"id": int(chat), "type": "private"},
                                          "from": {"first_name": first}, "text": text}}


def test_join_accept_and_member_search_isolated():
    st = {"users": {}, "pending": [], "usage": {}}
    tg = FakeTG([_msg(1, "222", "مرحبا")])
    agent.run(tg, research_fn=lambda t, **k: SAMPLE, state=st, fetch=lambda u: {})
    assert st["pending"][0]["id"] == "222" and "222" not in st["users"]
    tg = FakeTG([_msg(2, "111", "قبول 1")])
    agent.run(tg, research_fn=lambda t, **k: SAMPLE, state=st, fetch=lambda u: {})
    assert "222" in st["users"] and st["pending"] == []
    assert any(c == "222" and "تم قبولك" in m for c, m in tg.sent_to)
    tg = FakeTG([_msg(4, "222", "6"), _msg(3, "222", "شاشة 27")])
    agent.run(tg, research_fn=lambda t, **k: SAMPLE, state=st, fetch=lambda u: {})
    assert st["users"]["222"]["last_request"] == "شاشة 27"
    assert st["users"]["111"].get("last_request") is None  # كل واحد مستقل
    assert all(c == "222" for c, m in tg.sent_to)


def test_member_quota_and_admin_unlimited(monkeypatch):
    monkeypatch.setattr(agent, "USER_MONTHLY_SEARCHES", 1)
    st = {"users": {"222": {"name": "أحمد"}}, "pending": [], "usage": {}}
    tg = FakeTG([_msg(11, "222", "6"), _msg(1, "222", "طلب1"), _msg(12, "222", "6"), _msg(2, "222", "طلب2"),
                 _msg(13, "111", "6"), _msg(3, "111", "طلب3"), _msg(14, "111", "6"), _msg(4, "111", "طلب4")])
    calls = []
    agent.run(tg, research_fn=lambda t, **k: calls.append(t) or SAMPLE, state=st, fetch=lambda u: {})
    assert calls == ["طلب1", "طلب3", "طلب4"]
    assert any(c == "222" and "خلصت حصتك" in m for c, m in tg.sent_to)
    assert st["usage"]["total"] == 3


def test_total_quota_blocks_everyone(monkeypatch):
    monkeypatch.setattr(agent, "MONTHLY_SEARCHES", 1)
    st = {"users": {}, "pending": [], "usage": {}}
    tg = FakeTG([_msg(11, "111", "6"), _msg(1, "111", "طلب1"), _msg(12, "111", "6"), _msg(2, "111", "طلب2")])
    calls = []
    agent.run(tg, research_fn=lambda t, **k: calls.append(t) or SAMPLE, state=st, fetch=lambda u: {})
    assert calls == ["طلب1"] and "خلصت حصة البحث المجانية" in tg.sent[-1]


def test_members_list_and_remove():
    st = {"users": {"111": {}, "222": {"name": "أحمد"}}, "pending": [], "usage": {}}
    tg = FakeTG([_msg(1, "111", "الأعضاء"), _msg(2, "111", "احذف عضو 1")])
    agent.run(tg, research_fn=lambda t, **k: SAMPLE, state=st, fetch=lambda u: {})
    assert "1 أحمد" in tg.sent[0] and "222" not in st["users"]


def test_legacy_state_migrates_to_admin():
    st = agent.normalize_state({"last_report": [{"name": "x"}], "watches": [1]}, "111")
    assert st["users"]["111"]["watches"] == [1] and "watches" not in st


def test_accept_without_number_when_single_pending():
    st = {"users": {}, "pending": [{"id": "222", "name": "sara"}], "usage": {}}
    tg = FakeTG([_msg(1, "111", "قبول")])
    agent.run(tg, research_fn=lambda t, **k: SAMPLE, state=st, fetch=lambda u: {})
    assert "222" in st["users"] and st["pending"] == []


def test_accept_without_number_asks_when_many():
    st = {"users": {}, "pending": [{"id": "2", "name": "a"}, {"id": "3", "name": "b"}], "usage": {}}
    tg = FakeTG([_msg(1, "111", "قبول")])
    calls = []
    agent.run(tg, research_fn=lambda t, **k: calls.append(t) or SAMPLE, state=st, fetch=lambda u: {})
    assert len(st["pending"]) == 2 and calls == [] and "حدد الرقم" in tg.sent[-1]


def test_state_is_encrypted_and_migrates(tmp_path):
    legacy = {"users": {"111": {"name": "محمد", "watches": [{"name": "سري"}]}}, "pending": [], "usage": {}}
    with open(agent.LEGACY_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(legacy, f, ensure_ascii=False)
    st = agent.load_state()
    assert st["users"]["111"]["watches"][0]["name"] == "سري"
    agent.save_state(st)
    raw = open(agent.STATE_FILE, "rb").read()
    assert "سري".encode() not in raw and b"111" not in raw  # مشفر
    import os
    assert not os.path.exists(agent.LEGACY_STATE_FILE)
    assert agent.load_state()["users"]["111"]["name"] == "محمد"


def test_wrong_key_starts_fresh(monkeypatch):
    agent.save_state({"users": {"1": {}}, "pending": [], "usage": {}})
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "other")
    assert agent.load_state()["users"] == {}


def test_forever_mode_keeps_listening_until_loop_ends(monkeypatch):
    monkeypatch.setenv("LOOP_MINUTES", "60")
    batches = [[], [upd(1, "111", "6")], [], [upd(2, "111", "طلب")], [], []]
    minutes = iter([0, 5, 10, 20, 31, 40, 50, 61, 62, 63, 64, 65])
    class TG(FakeTG):
        def get_updates(self, offset=None, wait=0):
            return batches.pop(0) if batches else []
    tg = TG([])
    base = agent._now()
    n = agent.run(tg, research_fn=lambda t, **k: SAMPLE, state={"users": {}, "pending": [], "usage": {}},
                  fetch=lambda u: {}, clock=lambda: base + timedelta(minutes=next(minutes)))
    assert n == 1 and tg.acked == [1, 2]  # ما وقف عند أول دفعة فاضية


def test_search_retries_once_on_network_error():
    tg, st, calls, say = _chat()
    n = {"i": 0}
    def flaky(t, **k):
        n["i"] += 1
        if n["i"] == 1:
            raise agent.requests.ConnectionError("busy")
        return SAMPLE
    st["mode"] = "new"
    assert agent.handle_message(tg, st, {"text": "فلتر"}, flaky)
    assert n["i"] == 2 and any("أعيد المحاولة" in m for m in tg.sent)


def test_model_rank_prefers_new_lite_then_flash():
    names = ["gemini-2.5-flash", "gemini-3.5-flash-lite", "gemini-3.1-flash-lite-preview",
             "gemini-2.5-flash-lite", "gemini-3.8-flash", "gemini-3.1-flash-lite"]
    assert sorted(names, key=agent._model_rank) == [
        "gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "gemini-2.5-flash-lite",
        "gemini-3.1-flash-lite-preview", "gemini-3.8-flash", "gemini-2.5-flash"]


def test_price_in_text():
    assert agent.price_in_text(496, "السعر ٤٩٦ ر.س شامل")
    assert agent.price_in_text(1299, "SAR 1,299.00")
    assert not agent.price_in_text(85, "سعر غير مذكور 850 ريال")
    assert not agent.price_in_text(None, "85")


def test_verify_drops_unverifiable_estimated_price():
    opts = [{"name": "قناة U", "price_sar": 85, "url": "https://a"},
            {"name": "قناة U ستانلس", "price_sar": 145.95, "url": "https://b"}]
    pages = {"https://a": "Strut channel options", "https://b": "UPE channel SAR 145.95"}
    out = agent.verify_options("قناة U", opts, pages,
                               llm=lambda p, s="": '{"checks":[{"i":0,"complete":true,"matches":true}]}',
                               fetch=lambda u: {})
    assert [o["url"] for o in out] == ["https://b"]


def test_parse_result_handles_fenced_json_inside_tags():
    r = agent.parse_result('<json>```json\n{"options":[{"name":"x"}]}\n```</json>')
    assert r["options"][0]["name"] == "x"
    r = agent.parse_result('Here you go: {"checks":[{"i":0}]} thanks')
    assert r["checks"][0]["i"] == 0


def test_gemma_ranked_after_gemini():
    names = ["gemma-4-31b-it", "gemini-3.5-flash-lite", "gemma-4-26b-it", "gemini-3.6-flash"]
    assert sorted(names, key=agent._model_rank) == ["gemini-3.5-flash-lite", "gemini-3.6-flash",
                                                    "gemma-4-31b-it", "gemma-4-26b-it"]


def test_direct_structured_candidates_skip_gemini_extraction():
    calls = []
    def llm(prompt, system=""):
        calls.append(system)
        return json.dumps({"ar": "x", "en": "x"}) if "عبارتي" in prompt else '{"checks": []}'
    hits = lambda q, d=None: [{"title": f"p{i}", "url": f"https://shop{i}.sa/p", "content": ""} for i in range(9)]
    r = agent.research("x", llm=llm, search=hits, extract=lambda u: {},
                       fetch=lambda u: {"price": 50.0, "currency": "SAR", "available": True, "name": "منتج"})
    assert len(r["options"]) >= 7 and not any(calls)  # ما احتجنا طلب Gemini الكبير


def test_partial_results_when_gemini_busy():
    def llm(prompt, system=""):
        if system:
            raise agent.requests.HTTPError("503")
        return json.dumps({"ar": "x", "en": "x"}) if "عبارتي" in prompt else '{"checks": []}'
    hits = lambda q, d=None: [{"title": "p", "url": "https://one.sa/p", "content": ""},
                              {"title": "q", "url": "https://two.com/q", "content": ""}]
    r = agent.research("x", llm=llm, search=hits, extract=lambda u: {},
                       fetch=lambda u: {"price": 50.0, "currency": "SAR"} if "one" in u else {})
    assert [o["url"] for o in r["options"]] == ["https://one.sa/p"] and "مزدحمة" in r["note"]
    assert "مزدحمة" in agent.format_report("x", r)


def test_non_product_urls_filtered():
    f = agent.is_product_url
    assert not f("https://haraj.com.sa/search/%D8%B3%D9%85")
    assert f("https://haraj.com.sa/11128963338/jumbo")
    assert not f("https://sa.pricena.com/ar/airpods-pro-2")
    assert not f("https://www.alibaba.com/showroom/stainless-steel-u-channel.html")
    assert not f("https://www.amazon.sa/s?k=airpods")
    assert f("https://www.amazon.sa/dp/B0CHWRXH8B")
    assert f("https://salla.sa/fas.sa/airpods-pro-2/p12345")


def test_verify_rejects_fakes_and_clears_llm_price_note():
    opts = [{"name": "AirPods Pro 2", "price_sar": 269, "url": "https://x.sa/p1", "price_note": "السعر غير مذكور"},
            {"name": "AirPods Pro 2 اصلية", "price_sar": 899, "url": "https://y.sa/p2", "price_note": "غير مذكور"}]
    llm = lambda p, s="": json.dumps({"checks": [
        {"i": 0, "complete": True, "matches": True, "genuine": False},
        {"i": 1, "complete": True, "matches": True, "genuine": True, "opinion": "سعر مناسب"}]})
    out = agent.verify_options("ايربودز برو 2", opts, {}, llm=llm,
                               fetch=lambda u: {"price": 269.0 if "x" in u else 899.0, "currency": "SAR"})
    assert [o["url"] for o in out] == ["https://y.sa/p2"]
    assert out[0]["price_note"] == "" and out[0]["opinion"] == "سعر مناسب"


def test_knockoff_and_duplicates():
    assert agent.is_knockoff("ANC OEM ODM Earphone TWS", "سماعة ابل ايربودز برو 2")
    assert not agent.is_knockoff("Apple AirPods Pro 2", "سماعة ابل ايربودز برو 2")
    r = {"options": [
        {"name": "Apple AirPods Pro 2", "price_sar": 569, "platform": "أمازون", "url": "u1"},
        {"name": "Apple AirPods Pro 2", "price_sar": 569, "platform": "أمازون", "url": "u2"},
        {"name": "Apple AirPods Pro 2", "price_sar": 799, "platform": "نون", "url": "u3"}]}
    assert [o["url"] for o in agent.display_options(r)] == ["u1", "u3"]

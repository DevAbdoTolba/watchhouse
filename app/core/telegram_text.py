"""Localized, family-friendly text for Telegram messages (English / Arabic).

Two goals:
  * Plain wording for non-technical recipients (the family group), not
    surveillance jargon.
  * ONE emoji per *meaning*, shared by the live photo and the event, so the
    same thing always looks the same:
        🚨 someone showed up      👀 still there
        ✅ all clear / done        🎥 video        🕒 latest        ⏳ wait

`t(lang, key, **kw)` formats a template; `who(lang, persons, vehicles)` renders
the subject in the chosen language; `dur(lang, seconds)` renders a duration.
Unknown language falls back to English; unknown key returns itself.
"""

from __future__ import annotations

_STRINGS: dict[str, dict[str, str]] = {
    "en": {
        "alert_live": "🚨 {where}: motion now  {when}",
        "alert":      "🚨 {where}: {who}  {when}",
        "ongoing":    "👀 {where}: still here ({dur})  {when}",
        "ended":      "✅ {where}: all clear — left after {dur}  {when}",
        "clip":       "🎥 {where}",
        "btn_capture":"🎥 Capture",
        "btn_all":    "🎬 All cameras",
        "cb_wait":    "⏳ Sending…",
        "cb_expired": "Sorry, that one's no longer available.",
        "latest":     "🕒 Latest — {where}  {when}",
        "preparing":  "⏳ The video isn't ready yet — I'll send it automatically "
                      "the moment it is. No need to reply again.",
        "expired":    "Sorry, that event's video is no longer saved.",
        "no_clips":   "No video available for this one.",
        "no_more":    "✅ That's all the cameras for this one.",
        "no_footage": "No video for {where} at that time.",
        "send_failed":"Couldn't send {where}: {err}",
        "no_events":  "No events recorded yet.",
        "unknown_cmd":"I didn't understand that. Send /help.",
        "someone":    "someone's here",
        "person":     "1 person",
        "people":     "{n} people",
        "vehicle":    "a vehicle",
        "vehicles":   "{n} vehicles",
        "join":       ", ",
        "sec":        "{n}s",
        "min":        "{n} min",
        "help":       "Tap the buttons under an alert: 🎥 Capture for the video, "
                      "🎬 All cameras for every angle. (Replying to a photo still "
                      "works too.)\n/last — the most recent event.",
    },
    "ar": {
        "alert_live": "🚨 {where}: حركة الآن  {when}",
        "alert":      "🚨 {where}: {who}  {when}",
        "ongoing":    "👀 {where}: ما زال موجودًا ({dur})  {when}",
        "ended":      "✅ {where}: انتهى — غادر بعد {dur}  {when}",
        "clip":       "🎥 {where}",
        "btn_capture":"🎥 الفيديو",
        "btn_all":    "🎬 كل الكاميرات",
        "cb_wait":    "⏳ جارٍ الإرسال…",
        "cb_expired": "عذرًا، لم يعد هذا متاحًا.",
        "latest":     "🕒 الأحدث — {where}  {when}",
        "preparing":  "⏳ الفيديو لم يجهز بعد — سأرسله تلقائيًا فور أن يصبح جاهزًا. "
                      "لا داعي للرد مرة أخرى.",
        "expired":    "عذرًا، لم يعد فيديو هذا الحدث محفوظًا.",
        "no_clips":   "لا يوجد فيديو متاح لهذا الحدث.",
        "no_more":    "✅ هذه كل الكاميرات لهذا الحدث.",
        "no_footage": "لا يوجد فيديو لـ {where} في ذلك الوقت.",
        "send_failed":"تعذّر إرسال {where}: {err}",
        "no_events":  "لا توجد أحداث مسجّلة بعد.",
        "unknown_cmd":"لم أفهم ذلك. أرسل /help.",
        "someone":    "يوجد شخص",
        "person":     "شخص واحد",
        "people":     "{n} أشخاص",
        "vehicle":    "مركبة",
        "vehicles":   "{n} مركبات",
        "join":       "، ",
        "sec":        "{n} ثانية",
        "min":        "{n} دقيقة",
        "help":       "اضغط الأزرار أسفل التنبيه: 🎥 الفيديو للمقطع، "
                      "🎬 كل الكاميرات لكل الزوايا. (الردّ على الصورة ما زال يعمل "
                      "أيضًا.)\n/last — أحدث حدث.",
    },
}


def norm(lang: str | None) -> str:
    """English unless a known language is given (accepts 'ar', 'arabic', …)."""
    s = (lang or "").strip().lower()
    if s.startswith("ar"):
        return "ar"
    return "en"


def t(lang: str | None, key: str, **kw) -> str:
    table = _STRINGS[norm(lang)]
    tmpl = table.get(key) or _STRINGS["en"].get(key, key)
    try:
        return tmpl.format(**kw) if kw else tmpl
    except (KeyError, IndexError):
        return tmpl


def dur(lang: str | None, seconds: float) -> str:
    s = max(0.0, float(seconds))
    if s < 60.0:
        return t(lang, "sec", n=int(round(s)))
    return t(lang, "min", n=int(round(s / 60.0)))


def who(lang: str | None, persons: int, vehicles: int) -> str:
    """'someone's here' / '1 person' / '2 people, a vehicle' in the language."""
    parts: list[str] = []
    if persons == 1:
        parts.append(t(lang, "person"))
    elif persons > 1:
        parts.append(t(lang, "people", n=persons))
    if vehicles == 1:
        parts.append(t(lang, "vehicle"))
    elif vehicles > 1:
        parts.append(t(lang, "vehicles", n=vehicles))
    if not parts:
        return t(lang, "someone")
    return t(lang, "join").join(parts)

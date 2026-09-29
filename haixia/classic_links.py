"""經典與人紀講義、影片逐字稿的離線關聯。低信心候選不入庫。"""

import json
import re
import sqlite3
from collections import Counter, defaultdict

from haixia.classics import comparison_key

COURSES = {
    "傷寒論（宋本）": ("人紀《傷寒論》", "人紀・傷寒論"),
    "金匱要略方論": ("人紀《金匱要略》", "人紀・金匱要略"),
    "黃帝內經素問": ("人紀《黃帝內經》", "人紀・黃帝內經"),
    "黃帝內經靈樞": ("人紀《黃帝內經》", "人紀・黃帝內經"),
    "神農本草經": ("人紀《神農本草經》", "人紀・神農本草經"),
    "針灸大成": ("人紀《針灸教程》", "人紀・針灸"),
}
NUMBER_LINE = re.compile(r"(?m)^\s*([一二三四五六七八九十百千〇零兩两\d]{1,7})\s*[、：:]\s*[「\"“]?(.{8,})$")
CN_DIGITS = {c: i for i, c in enumerate("零一二三四五六七八九")}
CN_DIGITS["〇"] = 0


def chinese_number(value):
    if value.isdigit():
        return int(value)
    value = value.replace("兩", "二").replace("两", "二")
    if all(char in CN_DIGITS for char in value):
        return int("".join(str(CN_DIGITS[char]) for char in value))
    total, section, digit = 0, 0, 0
    for char in value:
        if char in CN_DIGITS:
            digit = CN_DIGITS[char]
        elif char in "十百千":
            place = {"十": 10, "百": 100, "千": 1000}[char]
            section += (digit or 1) * place
            digit = 0
    return total + section + digit


def bigrams(text):
    key = comparison_key(text)
    return set(key[i:i + 2] for i in range(len(key) - 1))


def containment(needle, haystack):
    a, b = bigrams(needle), bigrams(haystack)
    return len(a & b) / len(a) if a else 0.0


def ordered_best(candidates):
    """候選 (來源序,目標序,分數,附加資料)；全域挑選順序一致的最高分路徑。"""
    candidates = sorted(candidates, key=lambda x: (x[0], x[1]))
    if not candidates:
        return []
    dp = [x[2] for x in candidates]
    previous = [-1] * len(candidates)
    for i, current in enumerate(candidates):
        for j in range(i):
            earlier = candidates[j]
            if earlier[0] < current[0] and earlier[1] < current[1] and dp[j] + current[2] > dp[i]:
                dp[i], previous[i] = dp[j] + current[2], j
    position = max(range(len(dp)), key=dp.__getitem__)
    result = []
    while position >= 0:
        result.append(candidates[position])
        position = previous[position]
    return result[::-1]


def lecture_entries(records):
    for record in records:
        for found in NUMBER_LINE.finditer(record["text"]):
            number = chinese_number(found.group(1))
            if not number:
                continue
            body = found.group(2).split("\n", 1)[0].strip("」\"” ")
            if len(comparison_key(body)) >= 8:
                yield {"row": record["row"], "id": record["id"], "number": number,
                       "text": body, "record": record}


def _record_rows(connection, kind, title):
    return [dict(row) for row in connection.execute(
        "SELECT row,id,kind,title,section,text,source,episode,start,\"end\",unit_ids FROM chunks "
        "WHERE kind=? AND title=? ORDER BY row", (kind, title))]


def _ngrams_index(records):
    lookup = defaultdict(set)
    for i, record in enumerate(records):
        for term in bigrams(record["text"]):
            lookup[term].add(i)
    return lookup


def _candidates(text, records, lookup, max_candidates=20):
    terms = bigrams(text)
    counts = Counter(i for term in terms for i in lookup.get(term, ()))
    return [(i, containment(text, records[i]["text"])) for i, _ in counts.most_common(max_candidates)]


def link_course(classics, lectures, transcripts, lecture_threshold=.64, transcript_threshold=.38):
    links = []
    classic_lookup = _ngrams_index(classics)
    entries = list(lecture_entries(lectures))
    candidates = []
    for position, entry in enumerate(entries):
        for index, score in _candidates(entry["text"], classics, classic_lookup, 8):
            if score >= lecture_threshold:
                candidates.append((position, index, score, entry))
    for _source, index, score, entry in ordered_best(candidates):
        links.append((classics[index]["id"], entry["id"], "document", round(score, 3),
                      "條文雙字詞與順序", str(entry["number"])))

    # 金匱與內經講義的實際排版多為「原文一段、解說一段」，沒有編號前綴。
    # 從原文在講義段落中的出現位置比對，同樣施加全書順序限制。
    if len(entries) < 20:
        lecture_lookup = _ngrams_index(lectures)
        direct = []
        for index, classic in enumerate(classics):
            needle = classic["text"][:180]
            if len(comparison_key(needle)) < 12:
                continue
            for target, score in _candidates(needle, lectures, lecture_lookup, 6):
                if score >= .53:
                    direct.append((index, target, score, lectures[target]))
        for index, target, score, record in ordered_best(direct):
            links.append((classics[index]["id"], record["id"], "document", round(score, 3),
                          "講義原文雙字詞與順序", None))

    transcript_lookup = _ngrams_index(transcripts)
    video_candidates = []
    for index, classic in enumerate(classics):
        text = classic["text"][:160]
        if len(comparison_key(text)) < 12:
            continue
        for target, score in _candidates(text, transcripts, transcript_lookup, 7):
            if score >= transcript_threshold:
                video_candidates.append((index, target, score, transcripts[target]))
    for index, target, score, record in ordered_best(video_candidates):
        links.append((classics[index]["id"], record["id"], "transcript", round(score, 3),
                      "逐字稿雙字詞與順序", None))
    return links, len(entries)


def link_terms(classics, lectures, transcripts, terms, limit=3):
    links = []
    for classic in classics:
        section = classic.get("section") or ""
        term = section.split()[-1] if section else ""
        if not term or term not in terms:
            continue
        aliases = {"柴胡": ("柴胡", "茈胡"), "丹砂": ("丹砂", "丹沙")}.get(term, (term,))
        for kind, records in (("document", lectures), ("transcript", transcripts)):
            hits = []
            for position, record in enumerate(records):
                target = record["text"]
                heading = any(re.search(rf"[一二三四五六七八九十百千〇零\d]{{1,5}}[、：]\s*{re.escape(alias)}", target)
                              for alias in aliases)
                early = any(alias in target[:80] or alias in (record.get("section") or "").split()[-1:]
                            for alias in aliases)
                nearby = classic["title"] == "神農本草經" and position > 0 and any(
                    re.search(rf"[一二三四五六七八九十百千〇零\d]{{1,5}}[、：]\s*{re.escape(alias)}",
                              records[position - 1]["text"]) for alias in aliases)
                if classic["title"] == "神農本草經" and kind == "document":
                    relevant = heading or nearby
                else:
                    relevant = early
                if not relevant:
                    continue
                if not any(alias in target for alias in aliases) and not nearby:
                    continue
                coverage = containment(classic["text"][:100], target)
                score = .35 + .25 * bool(heading) + .15 * bool(early) + .1 * bool(nearby) + .15 * coverage
                hits.append((score, record))
            for score, record in sorted(hits, key=lambda item: -item[0])[:limit]:
                links.append((classic["id"], record["id"], kind, round(score, 3),
                              "藥名或穴名", None))
    return links


def link_database(db_path, terms_path):
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    terms = {line.strip() for line in open(terms_path, encoding="utf-8") if line.strip()}
    report = {}
    try:
        connection.execute("DELETE FROM classic_links")
        for name in [*COURSES, "八十一難經"]:
            classics = _record_rows(connection, "classic", name)
            if name in COURSES:
                lecture_title, video_title = COURSES[name]
                lectures = _record_rows(connection, "document", lecture_title)
                transcripts = _record_rows(connection, "transcript", video_title)
            else:
                lectures = [dict(r) for r in connection.execute(
                    "SELECT row,id,kind,title,section,text,source,episode,start,\"end\" FROM chunks "
                    "WHERE kind='document' AND text LIKE '%難經%' ORDER BY row")]
                transcripts = [dict(r) for r in connection.execute(
                    "SELECT row,id,kind,title,section,text,source,episode,start,\"end\" FROM chunks "
                    "WHERE kind='transcript' AND text LIKE '%難經%' ORDER BY row")]
            if name in ("神農本草經", "針灸大成"):
                links = link_terms(classics, lectures, transcripts, terms)
                entries = 0
            elif name == "八十一難經":
                links, entries = [], 0
                for classic in classics:
                    section = classic.get("section") or ""
                    for record in lectures + transcripts:
                        body = record["text"]
                        if re.search(rf"難經[^一二三四五六七八九十百千〇零\d]{{0,8}}(?:第)?{re.escape(section)}", body):
                            links.append((classic["id"], record["id"], record["kind"], .9,
                                          "明示難經篇名", None))
            else:
                links, entries = link_course(classics, lectures, transcripts,
                                             lecture_threshold=.60 if name == "八十一難經" else .64)
            connection.executemany("INSERT OR REPLACE INTO classic_links VALUES (?,?,?,?,?,?)", links)
            counts = dict(connection.execute(
                "SELECT target_kind,count(DISTINCT classic_id) FROM classic_links l "
                "JOIN chunks c ON c.id=l.classic_id WHERE c.title=? GROUP BY target_kind", (name,)).fetchall())
            connected = {row[0] for row in connection.execute(
                "SELECT DISTINCT l.classic_id FROM classic_links l JOIN chunks c ON c.id=l.classic_id "
                "WHERE c.title=?", (name,))}
            unlinked_units = sorted({unit for c in classics if c["id"] not in connected
                                     for unit in json.loads(c.get("unit_ids") or "[]")})
            report[name] = {"經典段數": len(classics), "講義條文數": entries,
                            "講義連結段數": counts.get("document", 0),
                            "逐字稿連結段數": counts.get("transcript", 0),
                            "講義連結率": round(counts.get("document", 0) / len(classics), 4) if classics else 0,
                            "逐字稿連結率": round(counts.get("transcript", 0) / len(classics), 4) if classics else 0,
                            "未連上單位序號": unlinked_units,
                            "未連上": [{"id": c["id"], "出處": c["source"]} for c in classics if c["id"] not in connected]}
        connection.commit()
    finally:
        connection.close()
    return report

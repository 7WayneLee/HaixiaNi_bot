"""文字資料的收錄範圍、標題清理與轉文字。

範圍規則以 raw/ 底下的相對路徑判斷，規則本身不讀檔，可以單獨檢查；
需要比對 MD5 的項目（電子書、地脈道）在 build_index 的檔案去重階段補上原因。
"""

import hashlib
import re
import subprocess
import unicodedata
from dataclasses import dataclass
from pathlib import PurePosixPath

from haixia.textnorm import to_traditional

# 段落去重的保留順序：數字越小越優先。逐字稿不參與段落去重。
GROUPS = {
    "renji": (1, "人紀講義"),
    "tianji": (2, "天紀"),
    "cases": (3, "單篇醫案"),
    "journals": (4, "診療日誌與漢唐日誌"),
    "articles": (5, "文章、事實評論、方劑講解、藥方"),
    "compilations": (6, "彙編"),
    "asr": (0, "影片逐字稿"),
    "lrc": (0, "梁冬對話倪海廈（LRC）"),
    "classic": (0, "經典"),
}

DOC_EXTS = {".doc", ".docx", ".htm", ".html", ".txt"}
OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"

TEXT_ROOT = "文字資料/"
EBOOK_ROOT = "電子書/"
DIR_EBOOKS = "文字資料/01.倪海厦电子书全集/"
DIR_CASES = "文字資料/03.倪海厦诊疗日志 医案/"
DIR_HANTANG = "文字資料/04.倪海厦汉唐中医/"
DIR_COMMENTS = "文字資料/05.倪海厦事实评论/"
DIR_GUOXUE = "文字資料/07.倪海厦国学堂/"
DIR_MP3 = "文字資料/MP3 人纪全/"
CASE_FOLDERS = ("倪海厦08年医案959篇", "倪海厦08年医案358篇", "倪海厦人纪班学的诊疗医案",
                "倪海厦汉唐中医医案-分杂")
# 這個資料夾的檔名是 Big5 位元組被當成 GBK 解讀的亂碼。
MOJIBAKE_FOLDER = "倪海厦08年医案959篇"


@dataclass(frozen=True)
class Rule:
    """一個檔案的判斷結果。include 為 False 時 reason 說明略過原因。"""

    include: bool
    group: str | None = None
    fmt: str | None = None          # doc、pdf、ocr、chm、lrc、txt
    reason: str = ""
    expect_duplicate: bool = False  # 應與範圍內某檔 MD5 相同；不同時要在報告警告


def file_format(rel_path, head=b""):
    """依副檔名判斷格式；沒有副檔名時看檔頭（OLE 就是舊版 Word）。"""
    name = PurePosixPath(rel_path).name
    suffix = PurePosixPath(rel_path).suffix.lower()
    if suffix in {".doc", ".docx", ".htm", ".html"}:
        return "doc"
    if suffix in {".txt", ".pdf", ".lrc", ".chm", ".rar"}:
        return suffix[1:]
    if name.endswith("doc") or head.startswith(OLE_MAGIC):
        return "doc"
    return None


def _misc_group(name):
    """「分杂」資料夾混了單篇醫案、日誌、文章與彙編，依檔名分組。"""
    if re.search(r"诊疗日志(?:\d{8})?\S*症", name) or "事件簿" in name:
        return "cases"
    if name.startswith("【文】") or any(word in name for word in ("精彩选", "文集", "医桉", "医案", "材料整理", "内部案例")):
        return "compilations"
    if "日志" in name or re.search(r"漢唐中醫\s*\d{4}年", name):
        return "journals"
    return "articles"


def _skip(reason, expect_duplicate=False):
    return Rule(False, reason=reason, expect_duplicate=expect_duplicate)


def classify(rel_path, head=b""):
    """回傳 Rule。rel_path 是 raw/ 底下的相對路徑（原始簡體檔名）。"""
    path = rel_path.replace("\\", "/")
    name = PurePosixPath(path).name
    fmt = file_format(path, head)

    if path.startswith(EBOOK_ROOT):
        return _skip("電子書資料夾：應與文字資料裡的檔案位元組相同", expect_duplicate=True)
    if not path.startswith(TEXT_ROOT):
        return _skip("不在文字資料範圍內")
    if fmt == "rar":
        if "平衡针" in name:
            return _skip("王文遠平衡針，非倪師內容")
        if path.startswith(DIR_CASES) and "诊疗日志医案-全" in name:
            return _skip("與已解開的醫案資料夾是同一批檔案（比對結果見 rar_check）")
        return _skip("壓縮檔，未解開")
    if path.startswith(DIR_GUOXUE):
        if "倪海厦对话梁冬" in path and fmt == "lrc":
            return Rule(True, "lrc", "lrc")
        if "刘力红" in path:
            return _skip("劉力紅，非倪師內容")
        if "说白伤寒论" in path:
            return _skip("郭生白《說白傷寒論》，非倪師內容")
        if "萧启红" in path:
            return _skip("梁冬對話蕭啟宏，非倪師內容")
        if "黄帝内经》录音" in path:
            return _skip("梁冬對話徐文兵，非倪師內容")
        return _skip("國學堂其他內容，不在範圍內")
    if path.startswith(DIR_MP3):
        return _skip("人紀版神農本草經掃描檔，應與文字版重複")
    if fmt is None:
        return _skip("無法判斷檔案格式")
    if fmt in {"lrc"}:
        return _skip("不在範圍內的字幕檔")

    if path.startswith(DIR_EBOOKS):
        if "地脉道" in name:
            return _skip("與《天紀》PDF 位元組相同", expect_duplicate=True)
        if name.startswith("人纪") and fmt == "pdf":
            return Rule(True, "renji", "pdf")
        if "天机道" in name and fmt == "pdf":
            return Rule(True, "tianji", "ocr")
        if name.startswith("天纪") and fmt in {"pdf", "doc"}:
            return Rule(True, "tianji", fmt)
        return _skip("電子書全集裡未列入範圍的檔案")

    if path.startswith(DIR_CASES):
        rest = path[len(DIR_CASES):]
        if "/" not in rest:
            if fmt == "chm":
                return Rule(True, "compilations", "chm")
            if "诊疗日志" in name and fmt == "doc":
                return Rule(True, "journals", "doc")
            return _skip("醫案資料夾頂層未列入範圍的檔案")
        folder = rest.split("/", 1)[0]
        if not folder.startswith(CASE_FOLDERS):
            return _skip("醫案資料夾裡未列入範圍的子資料夾")
        if fmt == "pdf" or PurePosixPath(path).suffix.lower() in {".htm", ".html"}:
            if "方剂讲解" in name:
                return Rule(True, "articles", fmt)
            return Rule(True, "compilations", fmt)
        if fmt not in {"doc", "txt"}:
            return _skip("醫案資料夾裡不支援的格式")
        if folder.startswith("倪海厦汉唐中医医案-分杂"):
            return Rule(True, _misc_group(name), fmt)
        return Rule(True, "cases", fmt)

    if path.startswith(DIR_HANTANG):
        if fmt not in {"doc", "pdf"}:
            return _skip("漢唐中醫資料夾裡不支援的格式")
        if "日志" in name:
            return Rule(True, "journals", fmt)
        if "文集及医桉" in name:
            return Rule(True, "compilations", fmt)
        return Rule(True, "articles", fmt)

    if path.startswith(DIR_COMMENTS):
        if fmt == "doc":
            return Rule(True, "articles", "doc")
        return _skip("事實評論資料夾裡不支援的格式")

    return _skip("不在範圍內的資料夾")


# ---------- 標題 ----------

_SOURCE_MARKS = [
    re.compile(r"[-\s]*[\(（]+\s*[\(（]?\s*守候诚实\s*[\)）]?\s*淘宝店\s*(?:doc)?[\)）]*"),
    re.compile(r"[-\s]*[\(（]\s*神州医料库\s*[\)）]"),
    re.compile(r"[-\s]*[\(（]\s*二羊中医馆\s*[\)）]"),
    re.compile(r"二羊中医馆"),
]
_TITLE_FIXES = {"倪海夏": "倪海廈"}


def repair_mojibake(text):
    """把被當成 GBK 解讀的 Big5 檔名轉回來；轉不回來就原樣回傳。"""
    try:
        return text.encode("gbk").decode("big5")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text


def clean_title(name, mojibake=False):
    """去掉副檔名與來源標記，轉成正體。name 是檔名（不含資料夾）。"""
    stem = name
    suffix = PurePosixPath(name).suffix
    if suffix.lower() in {".doc", ".docx", ".htm", ".html", ".txt", ".pdf", ".lrc", ".chm"}:
        stem = name[: -len(suffix)]
    if mojibake:
        stem = repair_mojibake(stem)
    for pattern in _SOURCE_MARKS:
        stem = pattern.sub("", stem)
    stem = re.sub(r"淘宝店doc$", "", stem)
    stem = re.sub(r"\s*([《【])", r"\1", stem)   # 「人纪 《伤寒论》」→「人纪《伤寒论》」
    stem = re.sub(r"\s+", " ", stem).strip(" -_,，")
    title = to_traditional(stem)
    for wrong, right in _TITLE_FIXES.items():
        title = title.replace(wrong, right)
    return title or to_traditional(name)


def title_for(rel_path):
    parts = PurePosixPath(rel_path).parts
    mojibake = MOJIBAKE_FOLDER in rel_path and len(parts) > 3
    title = clean_title(parts[-1], mojibake=mojibake)
    if "天纪  《天纪》" in rel_path and rel_path.lower().endswith(".pdf"):
        return "天紀《地脈道》"
    if not re.search(r"\w", title) and len(parts) > 1:
        # 檔名沒有內容（例如「,」）時改用資料夾名稱。
        title = clean_title(parts[-2], mojibake=mojibake)
    return title


# ---------- 轉文字 ----------

TXT_ENCODINGS = ("utf-8-sig", "utf-16", "gb18030", "big5")

# 依原 doc 的 Wingdings／Symbol 字型位置和句子上下文核對；其他私用碼無可靠字義。
SYMBOL_FONT_CHARS = {"\uf0e0": "→", "\uf0e8": "⇒", "\uf0df": "←",
                     "\uf04a": "☺", "\uf0b2": "•"}


def clean_extracted_text(text):
    """只替換抽取器產生的私用碼與替代字元，保留其餘文字。"""
    text = "".join(SYMBOL_FONT_CHARS.get(char, char) for char in text)

    def replace_unknown(match):
        before = text[match.start() - 1] if match.start() else ""
        after = text[match.end()] if match.end() < len(text) else ""
        if not before or not after or before.isspace() or after.isspace():
            return ""
        return " "

    return re.sub(r"[\ue000-\uf8ff\ufffd]+", replace_unknown, text)


def _garbage_ratio(text):
    """MacRoman 誤解碼常見字元比例；正常省略號、破折號、中點不計。"""
    if not text:
        return 0.0, 0.0
    suspicious = sum((0x80 <= ord(c) <= 0x24f or 0x370 <= ord(c) <= 0x3ff
                      or 0x2200 <= ord(c) <= 0x22ff or c in "ﬁﬂ\uf8ff"
                      or unicodedata.category(c) == "Cc") and c not in "\n\r\t" for c in text)
    han = sum("\u3400" <= c <= "\u9fff" for c in text)
    return suspicious / len(text), han / len(text)


def is_garbage(text, min_length=20):
    """兩種訊號同時成立才丟，避免正常的西文或中文標點誤判。"""
    bad, han = _garbage_ratio(text)
    return len(text) >= min_length and bad >= 0.30 and han < 0.10


_WORD_FIELD = re.compile(r"\x13[^\x14]*\x14")
_WORD_SEPARATORS = re.compile(r"[\r\x07\x0b]+")
_WORD_ALLOWED = re.compile(r"[^\u3400-\u9fff\u2000-\u206f\u3000-\u303f\uff00-\uffef\x20-\x7e\n\t·→⇒←☺•]+")


def recover_word_utf16(data):
    """從 SAT 損壞的 OLE 位元組裡按原順序救出 UTF-16LE 內文。"""
    decoded = data.decode("utf-16le", errors="replace")
    paragraphs = []
    fields = 0
    for piece in _WORD_SEPARATORS.split(decoded):
        piece, count = _WORD_FIELD.subn(" ", piece)
        fields += count
        piece = _WORD_ALLOWED.sub(" ", piece).replace("\n", " ").replace("\t", " ")
        piece = clean_extracted_text(piece).strip()
        if not piece or re.search(r"\b(?:PAGEREF|HYPERLINK|_Toc\d+|MERGEFORMAT)\b", piece):
            continue
        han = sum("\u3400" <= c <= "\u9fff" for c in piece)
        uncommon = sum("\u3400" <= c <= "\u4dbf" for c in piece)
        latin = sum("a" <= c.lower() <= "z" for c in piece)
        if han < 5 or uncommon > han * 0.05 or latin > han * 2 or is_garbage(piece):
            continue
        paragraphs.append(piece)
    return paragraphs, {"recovered_chars": sum(len(p) for p in paragraphs), "field_codes_removed": fields}


def decode_txt(data):
    """依序試 utf-8-sig、utf-16、gb18030、big5。utf-16 只在有 BOM 時採用。"""
    for encoding in TXT_ENCODINGS:
        if encoding == "utf-16" and not data.startswith((b"\xff\xfe", b"\xfe\xff")):
            continue
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ValueError("文字檔編碼無法辨識")


def textutil_text(path, timeout=120):
    """用 macOS textutil 把 doc、docx、htm 轉成純文字。"""
    result = subprocess.run(["textutil", "-convert", "txt", "-stdout", str(path)],
                            capture_output=True, timeout=timeout, check=False)
    if result.returncode != 0:
        raise ValueError(f"textutil 失敗（代碼 {result.returncode}）")
    return result.stdout.decode("utf-8", errors="replace")


def md5_file(path, block=1 << 20):
    digest = hashlib.md5()
    with open(path, "rb") as source:
        while chunk := source.read(block):
            digest.update(chunk)
    return digest.hexdigest()


def _cjk(char):
    return "㐀" <= char <= "鿿" or "豈" <= char <= "﫿" or "　" <= char <= "〿" or "＀" <= char <= "￯"


def join_spans(parts):
    """接起同一列的文字片段；兩邊都是中文時不留空白。"""
    text = ""
    for part in parts:
        part = part.strip()
        if not part:
            continue
        if text and not (_cjk(text[-1]) or _cjk(part[0])):
            text += " "
        text += part
    return text


def _furniture_key(text, x0, y0):
    return (re.sub(r"\d+", "#", text.strip()), round(x0 / 6), round(y0 / 6))


def pdf_pages(path, furniture_ratio=0.3):
    """用 PyMuPDF 取出每頁文字，回傳 [(頁碼, [段落…])]。

    - 座標先依頁面旋轉轉成顯示方向；不是橫書的行（側邊直排字、斜的浮水印）略過。
    - 頁首、頁尾、頁碼在許多頁的同一位置重複出現，依（文字去數字、位置）
      出現的頁數比例判斷後移除。
    - 一張紙印兩頁（中間有整條空白）時，先左欄再右欄。
    - 段落以縮排或上一列明顯較短判斷。
    """
    import pymupdf

    document = pymupdf.open(str(path))
    pages = []
    for page in document:
        matrix = page.rotation_matrix
        origin = pymupdf.Point(0, 0) * matrix
        width = page.rect.width
        spans = []
        seen = {}
        for block in page.get_text("rawdict")["blocks"]:
            for line in block.get("lines", []):
                direction = pymupdf.Point(line["dir"]) * matrix - origin
                if direction.x < 0.9:
                    continue
                for span in line["spans"]:
                    chars = [char for char in span["chars"] if not _overprinted(char, seen)]
                    text = "".join(char["c"] for char in chars)
                    if text.strip():
                        rect = pymupdf.Rect(chars[0]["bbox"])
                        for char in chars[1:]:
                            rect |= pymupdf.Rect(char["bbox"])
                        rect = rect * matrix
                        spans.append((rect.x0, rect.y0, rect.x1, rect.y1, text))
        pages.append((spans, width))
    document.close()

    counts = {}
    for spans, _width in pages:
        for key in {_furniture_key(t, x0, y0) for x0, y0, _x1, _y1, t in spans}:
            counts[key] = counts.get(key, 0) + 1
    threshold = max(3, furniture_ratio * len(pages))
    result = []
    for number, (spans, width) in enumerate(pages, 1):
        body = [s for s in spans if counts[_furniture_key(s[4], s[0], s[1])] < threshold]
        paragraphs = []
        for column in _split_columns(body, width):
            paragraphs += _rows_to_paragraphs(column)
        result.append((number, paragraphs))
    return result


def _overprinted(char, seen, tolerance=1.5):
    """假粗體會把同一個字在幾乎相同的位置重畫好幾次；第二次以後回傳 True。"""
    if char["c"].isspace():
        return False
    x, y = char["origin"]
    cell = (char["c"], round(x / tolerance), round(y / tolerance))
    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            for ox, oy in seen.get((cell[0], cell[1] + dx, cell[2] + dy), ()):
                if abs(ox - x) <= tolerance and abs(oy - y) <= tolerance:
                    return True
    seen.setdefault(cell, []).append((x, y))
    return False


def _split_columns(spans, width, min_gap=15):
    """中間 25%–75% 有一條沒有任何文字跨過的空白帶時，分成左右兩欄。"""
    if len(spans) < 4 or width <= 0:
        return [spans]
    covered = bytearray(int(width) + 2)
    for x0, _y0, x1, _y1, _text in spans:
        for x in range(max(0, int(x0)), min(len(covered), int(x1) + 1)):
            covered[x] = 1
    best, run_start = None, None
    for x in range(int(width * 0.25), int(width * 0.75) + 1):
        if not covered[x]:
            run_start = x if run_start is None else run_start
            if x - run_start + 1 >= min_gap and (best is None or x - run_start > best[1] - best[0]):
                best = (run_start, x)
        else:
            run_start = None
    if best is None:
        return [spans]
    middle = (best[0] + best[1]) / 2
    left = [s for s in spans if s[2] <= middle]
    right = [s for s in spans if s[0] >= middle]
    if not left or not right:
        return [spans]
    return [left, right]


def _rows_to_paragraphs(spans):
    """依 y 座標把片段分列，再依縮排把列接成段落。"""
    rows = []
    for span in sorted(spans, key=lambda s: ((s[1] + s[3]) / 2, s[0])):
        center = (span[1] + span[3]) / 2
        height = max(span[3] - span[1], 1)
        if rows and abs(rows[-1]["center"] - center) < height * 0.5:
            rows[-1]["spans"].append(span)
        else:
            rows.append({"center": center, "spans": [span]})
    lines = []
    for row in rows:
        row_spans = sorted(row["spans"], key=lambda s: s[0])
        text = join_spans([s[4] for s in row_spans])
        if text:
            lines.append((row_spans[0][0], row_spans[-1][2], text))
    if not lines:
        return []
    left = sorted(x0 for x0, _x1, _t in lines)[len(lines) // 2]
    right = sorted(x1 for _x0, x1, _t in lines)[len(lines) // 2]
    paragraphs = []
    current = ""
    previous_short = True
    for x0, x1, text in lines:
        indented = x0 - left > 12
        if current and (indented or previous_short):
            paragraphs.append(current)
            current = ""
        current = join_spans([current, text]) if current else text
        previous_short = right - x1 > 30
    if current:
        paragraphs.append(current)
    return paragraphs


def text_paragraphs(text):
    """把純文字切成段落（每個非空行一段）。"""
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace(" ", "\n").replace(" ", "\n")
    paragraphs = []
    for line in text.split("\n"):
        line = re.sub(r"[ \t　\xa0]+", " ", line).strip()
        if line:
            paragraphs.append(line)
    return paragraphs


def ocr_paragraphs(text):
    """把文字辨識的逐行結果接成段落：短行且以句尾標點結束時才分段。"""
    lines = [line.strip() for line in text.replace("\r", "\n").split("\n") if line.strip()]
    if not lines:
        return []
    typical = sorted(len(line) for line in lines)[len(lines) * 3 // 4]
    paragraphs, current = [], ""
    for line in lines:
        current = join_spans([current, line]) if current else line
        if len(line) < typical * 0.8 and line[-1] in "。！？：」』）)":
            paragraphs.append(current)
            current = ""
    if current:
        paragraphs.append(current)
    return paragraphs


# 來源網站、賣家與版權聲明之類的行，不是內容。
BOILERPLATE = ("仅供网络测试", "僅供網路測試", "守候诚实", "淘宝店", "神州医料库", "二羊中医馆")


_PAGE_MARK = re.compile(r"^(?:第?\s*\d*\s*[页頁]|P\s*\d+\s*-\s*\d[\d ]*|[IVXLC]+|\d+|[-–—\s\d]+)$", re.I)


def is_boilerplate(paragraph):
    """來源與版權聲明、只有頁碼的行、目錄行、沒有任何文字的行。"""
    text = paragraph.strip()
    if not re.search(r"[^\W\d_]", text):
        return True
    if _PAGE_MARK.match(text):
        return True
    # 目錄：點線（連續 6 個以上的點）佔兩成以上；內文裡偶爾出現的「……」不算。
    leaders = sum(len(run) for run in re.findall(r"[·．.…•]{6,}", text))
    if leaders and leaders >= 0.2 * len(text):
        return True
    return len(text) <= 200 and any(mark in text for mark in BOILERPLATE)


def safe_path(rel_path, md5=None):
    """報告用路徑：以病人姓名命名的醫案檔只留資料夾，檔名換成雜湊。"""
    if MOJIBAKE_FOLDER in rel_path:
        folder = rel_path.split(MOJIBAKE_FOLDER, 1)[0] + MOJIBAKE_FOLDER + "…"
        tag = md5[:8] if md5 else hashlib.sha1(rel_path.encode("utf-8")).hexdigest()[:8]
        return f"{folder}/［檔名已隱藏 {tag}］"
    return rel_path

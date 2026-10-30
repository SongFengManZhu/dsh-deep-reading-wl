#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""deep-reading map —— 出一份「这一章值得怎么读」的清单。

## 它和 scan / digest 的分工

  scan    全书级别，几秒，告诉我哪些表坏了、公式多不多、一共多少字符
  digest  一章级别，把表格碎片剥掉，留下作者的话
  map     **流程级别**：把 scan 的结论和这一章的篇幅翻译成一组具体的动作

## 为什么需要 map

我读完十一章、写完那一章的心得之后回头看，当时真正花时间的判断是这几个：

  1. 这一章的主线是什么？（十一章是「先给实验事实，再补理论解释」）
  2. 哪几个地方作者在立论，必须原话引用而不能转述？（稳态近似、平衡假设）
  3. 哪几张表的数据在心得里要用，但 OCR 已经坏了？（表 11.1.1、11.7.1）
  4. 哪里可以略读？（例题的算术过程）

这些判断我每次都重新做一遍。map 不是替我判断，是把判断所需的原料摆到桌面上：
每节多少字符、多少公式、多少定义、多少表坏了、多少图——以及**哪节是这章的重心**。

重心怎么算：一个节的「承重密度」= (公式数 + 定义数 + 推导数) / 字符数 * 1000。
物理化学这种书里，推导密集的节就是重心所在，例题密集的节反而轻。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from scan import (  # noqa: E402
    CHAPTER,
    SECTION,
    TABLE_TITLE,
    FIGURE,
    DEFINE,
    DERIVATION,
    FORMULA,
    FORMULA_NUMBERED,
    split_pages,
    clean_page_lines,
    finish_table,
    count_numbers,
    count_name_tokens,
    is_numeric_line,
    chapter_key,
    _NUM_UNIT as NUM_UNIT,
    _SECTION_JUNK as SECTION_JUNK,
    _strip_trailing_pageno as strip_trailing_pageno,
    _section_title_ok as section_title_ok,
)


_CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6,
           "七": 7, "八": 8, "九": 9, "十": 10}


def _num(s: str):
    """把章号字符串归一到 int：「十一」→11，「2」→2，「二十一」→21；认不出返回 None。"""
    s = (s or "").strip()
    if s.isdigit():
        return int(s)
    if not s or any(ch not in _CN_NUM for ch in s):
        return None
    if "十" not in s:
        return _CN_NUM[s] if len(s) == 1 else None
    a, _, b = s.partition("十")
    tens = _CN_NUM[a] if a else 1
    ones = _CN_NUM[b] if b else 0
    return tens * 10 + ones


def _chapter_matches(ch: dict, want: str):
    """--chapter 过滤：数字等价，或标题命中都算匹配。"""
    wn = _num(want)
    cn = _num(ch["number"])
    if wn is not None and cn is not None and wn == cn:
        return True
    return want == ch["number"] or want in ch["title"]


def build_map(text: str, chapter_filter: str | None = None):
    pages = split_pages(text)

    # ---- 先切章
    # 坑一：页眉里每页都印着「第十一章 化学动力学」，所以「章标题第一次出现」
    # 不等于「章从这里开始」——上册第 233 页只是页眉，真正的章首页在 PDF250。
    # 判据改成：同一条章标题在连续 N 页里**只出现在页首或页尾**，且该页正文里
    # 还带着节标题或大段正文，才算章首。这里用一个更稳的近似：
    # 只有在「这一页之前的页不是同一章」时才认，即标题变化处。
    marks = []      # {pdf, number, title}
    seen_ch = set()
    prev_key = None
    for p in pages:
        body = clean_page_lines(p["lines"])
        hit = None
        for ln in body:
            m = CHAPTER.match(ln)
            if m and len(ln) < 40:
                # 必须走 chapter_key，不能自己 re.sub：章标题里有 OCR 从装饰线
                # 认出来的 «_»（「第九章_统计热力学初步」），不剥就和 scan 的键对不上，
                # --chapter 9 会报「没找到章节」。
                key = chapter_key(m.group(2))
                if key is None:
                    continue
                hit = (m.group(1), key)
                break
        if hit is None:
            continue
        if hit == prev_key:
            # 同一章在后续页反复出现页眉，跳过
            continue
        prev_key = hit
        if hit not in seen_ch:
            seen_ch.add(hit)
            marks.append({"pdf": p["pdf"] or 0, "number": hit[0], "title": hit[1]})

    last_pdf = max((p["pdf"] or 0) for p in pages)
    for i, c in enumerate(marks):
        c["pdf_end"] = marks[i + 1]["pdf"] if i + 1 < len(marks) else last_pdf + 1

    # ---- 每章内部按节切，统计各节的承重密度
    for c in marks:
        # 章号在书里有两种写法：物理化学是「第十一章」（中文数字），
        # 有机化学是「第2章」（阿拉伯数字）。用户敲 --chapter 11 或 十一 都该认，
        # 所以比较前先把两边都归一到阿拉伯数字。
        if chapter_filter and _chapter_matches(c, chapter_filter) is False:
            continue
        seg_pages = [p for p in pages
                     if p["pdf"] is not None and c["pdf"] <= p["pdf"] < c["pdf_end"]]
        secs = []          # 当前节缓冲
        cur = {"id": None, "title": "（章首，未分节）", "chars": 0,
               "formulas": 0, "defines": 0, "derivs": 0, "tables": [], "figures": 0,
               "pdf_from": c["pdf"], "pdf_to": c["pdf"]}
        tbl = None
        # 按节号索引已建立的节记录。原书版式是每个子节页眉都重印父节标题
        # （§2.5 → §2.5.1 → §2.5 再出现 → §2.5.2 …），父节内容被拆成两截。
        # 有了这张表，同号同名的新标题就是把 cur 指回原记录继续累加。
        _sec_by_id = {}
        # 上一段所属的节号，用来区分「跨页页眉重印」（本节→本节，可合并）
        # 与「跨节回来」（父节→子节→父节，必须当新段，否则父节一口吞下
        # 半个章节的字符，实测把 §11.5 算成 48640 字符 / 占全章 56%）。
        last_sec_id = None

        def flush():
            nonlocal cur, tbl
            if cur["chars"] or cur["formulas"]:
                secs.append(cur)
            cur = {"id": None, "title": "（续）", "chars": 0, "formulas": 0,
                   "defines": 0, "derivs": 0, "tables": [], "figures": 0,
                   "pdf_from": 0, "pdf_to": 0}

        for p in seg_pages:
            for ln in clean_page_lines(p["lines"]):
                ms = SECTION.match(ln)
                if ms and len(ln) < 40 and section_title_ok(ms.group(2) or ms.group(4) or ""):
                    if tbl:
                        cur["tables"].append(finish_table(tbl))
                        tbl = None
                    # SECTION 有两组分支：带 §/S 前缀的命中 group1/2（物理化学的
                    # 「§7.9原电池的设计」节号与标题之间没有空格），裸节号的命中
                    # group3/4（有机化学的「2.5 烷烃和环烷烃的物理性质」）。
                    new_id = ms.group(1) or ms.group(3)
                    # 页眉里的节标题常粘连页码：'11.5 醛和酮的化学性质 383'、
                    # '11.8 醌391'（页码前的空格被 OCR 吃掉）。不剥掉的话
                    # 同一节每页都变成一条新记录，§11.5 会被切成五段
                    # （实测：…化学性质 / …化学性质367 / …化学性质371 /
                    # …化学性质383 / …化学性质385）。
                    new_title = strip_trailing_pageno(ms.group(2) or ms.group(4) or "")
                    new_title = re.sub(r"\s+", "", new_title)
                    # 表格数值冒充节号：OCR 打散表格后「0.25 nm」「1.5 倍」单独成行，
                    # 形态与「2.5 烷烃和环烷烃的物理性质」无法用正则区分。
                    # 用结构事实兜底：节号第一段是章号，必落在 [1, 已见最大章号+1]。
                    head = int(re.match(r"\d+", new_id).group())
                    # 章号上限：出错时退化成「不设限」比整份报告崩掉强——
                    # 这份脚本的用途是给人看的提纲，不是校验器。
                    _ch_nums = [int(c["number"]) for c in marks if c["number"].isdigit()]
                    if not _ch_nums:
                        _ch_nums = [99]
                    if head < 1 or head > max(_ch_nums) + 1:
                        continue
                    if NUM_UNIT.match(new_title):
                        continue
                    if SECTION_JUNK.search(new_title):
                        continue
                    # 坑二：同一节标题会在跨页处重复出现（页眉 + 本节续页），
                    # 第一版每遇到就切一刀，结果 §11.8 被切成 1598 字符和 6361 字符两段。
                    #
                    # 但「按 last_sec_id 跳过」也不够：这本书的版式是每个子节页眉都重印
                    # 父节标题（§2.5 → §2.5.1 → §2.5 再出现 → §2.5.2 …），于是父节的
                    # 内容被拆成 750/265、4638/1098 两截散落在子节前后。
                    # 正确做法是「按节号归并」：同号同名的节，无论出现几次，
                    # 字符与计数都累加到同一条记录上，顺序按第一次出现的位置。
                    if new_id in _sec_by_id and _sec_by_id[new_id]["title"] == new_title:
                        # 归并回已建立的节记录。注意：不能只把 cur 指过去——
                        # cur 可能还挂着「本段尚未 flush 的内容」，下一次 flush()
                        # 会把同一个对象再 append 一次，报告里就出现两行同号同字符
                        # 的 §2.5。先把 cur 存掉，再看要不要切。
                        #
                        # 但是「切回去」有陷阱：一旦 cur 指向旧记录，后面整页的
                        # 每一行都会继续累加到旧记录上。§11.5 → §11.5.2 → §11.5
                        # 这种版式下，§11.5 会被反复「切回去」，最后一口吞掉
                        # 48640 字符（占全章 56%），而这个数字里混着 §11.5.2/3/4
                        # 的内容，也混着每页重印的页眉。
                        #
                        # 所以只在「上一段就是这条节」时才合并——那是真正的
                        # 跨页页眉重印（本节 → 本节）。跨节回来（父节 → 子节 →
                        # 父节）不切指针，让它自然成为一个新段，
                        # 最后由下面的收尾合并遍把同号同名的段加起来。
                        if cur is not _sec_by_id[new_id]:
                            if last_sec_id == new_id:
                                # 紧邻重印：同一节的下一页页眉，指针切回原位
                                if cur["chars"] or cur["formulas"]:
                                    secs.append(cur)
                                cur = _sec_by_id[new_id]
                            # 否则（跨节回来）不切，直接往下走当新段
                        tbl = None
                        last_sec_id = new_id
                        continue
                    flush()
                    cur["id"] = new_id
                    cur["title"] = new_title
                    cur["pdf_from"] = p["pdf"]
                    _sec_by_id[new_id] = cur
                    last_sec_id = new_id
                    merging = False
                    continue
                mt = TABLE_TITLE.match(ln)
                if mt and "。" not in ln:
                    if tbl:
                        cur["tables"].append(finish_table(tbl))
                    tbl = {"id": mt.group(1), "title": mt.group(2), "pdf": p["pdf"],
                           "numeric_lines": 0, "name_lines": 0,
                           "num_tokens": 0, "name_tokens": 0, "continued": False}
                    continue
                if tbl:
                    if is_numeric_line(ln):
                        tbl["numeric_lines"] += 1
                        continue
                    nv, nt = count_numbers(ln), count_name_tokens(ln)
                    if nv >= 1:
                        tbl["num_tokens"] += nv
                        if nt >= 2:
                            tbl["name_lines"] += 1
                            tbl["name_tokens"] += nt
                            continue
                    if nt >= 1 and len(ln) <= 60 and "。" not in ln and nv <= 2:
                        tbl["name_lines"] += 1
                        tbl["name_tokens"] += nt
                        continue
                    if len(ln) > 40 or "。" in ln:
                        cur["tables"].append(finish_table(tbl))
                        tbl = None
                cur["chars"] += len(ln)
                cur["pdf_to"] = p["pdf"]
                if FIGURE.match(ln):
                    cur["figures"] += 1
                if FORMULA_NUMBERED.match(ln) or (FORMULA.search(ln) and len(ln) <= 60 and "。" not in ln):
                    cur["formulas"] += 1
                if DEFINE.search(ln) and len(ln) <= 80:
                    cur["defines"] += 1
                if DERIVATION.search(ln) and len(ln) <= 80:
                    cur["derivs"] += 1
        if tbl:
            cur["tables"].append(finish_table(tbl))
        flush()

        # 收尾归并：同号同名的节可能被切成多条（原书每个子节页眉都重印父节标题，
        # §2.5 → §2.5.1 → §2.5 再出现 → §2.5.2，父节被拆成两截）。
        # 前面靠 _sec_by_id 把 cur 指回原记录能减少重复，但 flush() 又会把
        # 已经 append 过的对象再 append 一次。这里做最后一次合并：
        # 按「节号 + 标题」分组，字符与各项计数相加，顺序取第一次出现。
        merged = []
        index = {}
        for s in secs:
            k = (s["id"], s["title"])
            if s["id"] is not None and k in index:
                kept = index[k]
                for f in ("chars", "formulas", "defines", "derivs", "figures"):
                    kept[f] += s[f]
                kept["tables"].extend(s["tables"])
                if s["pdf_from"]:
                    kept["pdf_from"] = min(kept["pdf_from"] or s["pdf_from"], s["pdf_from"])
                if s["pdf_to"]:
                    kept["pdf_to"] = max(kept["pdf_to"], s["pdf_to"])
                continue
            index[k] = s
            merged.append(s)
        secs = merged

        # 承重密度
        total = sum(s["chars"] for s in secs) or 1
        for s in secs:
            load = s["formulas"] + s["defines"] + s["derivs"]
            s["load"] = load
            s["load_density"] = round(load / max(s["chars"], 1) * 1000, 1)
            s["share"] = round(s["chars"] / total * 100, 1)
            # scan.finish_table 降级后返回的是 position 索引，不判「表好不好」，
            # 只给 status ∈ {有表体, 表体极少, 表体不在本页}。map 只需要知道
            # 「哪些表的表体没跟标题在同一页」——这些页要翻原图补数据。
            # 去重：同号同名的节被合并时 tables 会被 extend，同一张表可能进来两次。
            _seen_t = set()
            s["bad_tables"] = []
            for t in s["tables"]:
                tk = (t.get("id"), tuple(t.get("pages") or [t.get("pdf")]))
                if tk in _seen_t:
                    continue
                _seen_t.add(tk)
                if t.get("status") != "有表体":
                    s["bad_tables"].append(t)

        c["sections"] = secs
        c["chars"] = total
        c["pages"] = c["pdf_end"] - c["pdf"]
        c["bad_tables"] = [t for s in secs for t in s["bad_tables"]]

    return [c for c in marks if "sections" in c]


def advise(ch: dict):
    """把统计翻译成一句「这章怎么读」。"""
    secs = ch["sections"]
    if not secs:
        return ["没有分节信息。"]
    heavy = sorted([s for s in secs if s["chars"] >= 1500],
                   key=lambda s: -s["load_density"])[:3]
    tips = []
    if heavy:
        names = "、".join("§%s %s" % (s["id"] or "章首", s["title"][:14]) for s in heavy)
        tips.append("重心在 %s（承重密度最高，先读这几节再回头补细节）" % names)
    bad = ch["bad_tables"]
    if bad:
        # scan 现在只做位置索引，不再判「表好不好」，所以这里也不能说「已损坏」——
        # 那是在替读者下判断。能确定的只有一件事：这些表的表体没跟标题落在同一页，
        # 想用表里的数，得回原图看。
        pages = []
        for t in bad:
            for p in (t.get("pages") or [t["pdf"]]):
                if p not in pages:
                    pages.append(p)
        tips.append("%d 张表的表体不在标题同页，若要用表里的数据必须翻 PDF 第 %s 页看原图"
                    % (len(bad), "、".join(str(p) for p in pages[:6])))
    big = max(secs, key=lambda s: s["chars"])
    tips.append("篇幅最大的是 §%s %s（占 %.0f%%），可以按需略读" % (
        big["id"] or "章首", big["title"][:16], big["share"]))
    if ch["chars"] > 50000:
        tips.append("全章 %d 字符，不要一次读完：按节读，每读完一节先写三行再说" % ch["chars"])
    return tips


def render(chapters, chapter_filter=None):
    out = []
    for c in chapters:
        out.append("=" * 60)
        out.append("第%s章 %s   PDF%d–PDF%d（%d 页 / %d 字符）" % (
            c["number"], c["title"], c["pdf"], c["pdf_end"] - 1, c["pages"], c["chars"]))
        out.append("=" * 60)
        out.append("")
        out.append("%-9s %-22s %7s %6s %4s %4s %4s %4s" % (
            "节号", "标题", "字符", "占比", "公式", "定义", "推导", "图"))
        out.append("-" * 60)
        for s in c["sections"]:
            out.append("%-9s %-22s %7d %5.1f%% %4d %4d %4d %4d" % (
                "§" + (s["id"] or "-"), s["title"][:22], s["chars"], s["share"],
                s["formulas"], s["defines"], s["derivs"], s["figures"]))
        out.append("")
        out.append("怎么读：")
        for t in advise(c):
            out.append("  · " + t)
        bad = c["bad_tables"]
        if bad:
            out.append("")
            out.append("表体不跟标题同页的（要翻原图补数据）：")
            for t in bad:
                where = "、".join("PDF%d" % p for p in (t.get("pages") or [t["pdf"]]))
                out.append("  表 %-9s %-18s %-8s %s" % (
                    t["id"], where, t.get("status", ""), t["title"][:34]))
        out.append("")
    return "\n".join(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="map", description="按章出「怎么读」的清单")
    ap.add_argument("txt", help="OCR 产出的 txt")
    ap.add_argument("--chapter", default=None, help="只看某一章（章号如「十一」或标题）")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--out", default=None)
    args = ap.parse_args(argv)

    if not os.path.isfile(args.txt):
        print("找不到文件：%s" % args.txt, file=sys.stderr)
        return 2
    with open(args.txt, encoding="utf-8", errors="replace") as fh:
        text = fh.read()

    chapters = build_map(text, args.chapter)
    if not chapters:
        print("没找到章节。", file=sys.stderr)
        return 1
    body = json.dumps(chapters, ensure_ascii=False, indent=1) if args.json else render(chapters)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(body)
        print("写入 %s" % args.out, file=sys.stderr)
    else:
        print(body)
    return 0


if __name__ == "__main__":
    sys.exit(main())
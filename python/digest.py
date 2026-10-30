#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""deep-reading digest —— 把一章 OCR 正文压成「值得我亲自读的那部分」。

## 为什么需要它

scan 只能告诉我「这一章有 6.9 万字符、11 张表坏了 3 张」。要写心得，我得真的
读这 6.9 万字符。问题是其中大约 75% 是垃圾：

  表11.7.1某些可燃气体在空气中的爆炸界限
  可燃气体在空气中的体积分数/%
  可燃气体
  爆炸下限
  爆炸上限
  H2
  NHa
  CS2
  1.3
  ...

一整张表被 OCR 拆成了几十行孤立碎片，模型逐行读它，纯烧 token。但这句话不能
直接扔掉：

  表11.7.1中列出某些可燃气体在空气中的爆炸界限。

因为它恰好是「这张表在说什么」的唯一线索，而 scan 已经把这张表判为「损坏」。

## 所以本脚本做三件事

1. **分诊**：把每行归到 prose / formula / table_frag / figure_frag / noise 之一
2. **换化**：表格碎片不逐行保留，但换化成一行「[表 11.7.1 在此，2 页后扫判损坏]」
3. **串联**：把 §11.7 这类节标题、公式编号、定义句、判断句保留原样，因为心得里
   引用的就是这些

输出不是「摘要」——摘要会丢信息。输出是**一份更短的原文**，删掉的只有确定
无信息量的部分。这样我读它读到的仍然是作者的话，不是我自己的转述。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys

# 与 scan.py 共用同一套正则，避免两处判定漂移
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from scan import (  # noqa: E402
    PAGE_MARK,
    PAGE_NO,
    CHAPTER,
    SECTION,
    TABLE_TITLE,
    FIGURE,
    DEFINE,
    FORMULA,
    FORMULA_NUMBERED,
    count_numbers,
    count_name_tokens,
    split_pages,
)

# ---------------------------------------------------------------- 行分类

# 纯符号行：只剩标点、括号、上下标残片
SYMBOL_ONLY = re.compile(r'^[\s\W_]+$|^[a-zA-Z0-9+\-=<>()\[\]{}·/.,，。、；：？！%°√π²³⁻¹]+$')

# 图内标注：坐标轴数值、图例、单字母
AXIS_NOISE = re.compile(r'^[(\[]?\s*[-−+]?\d{1,4}(\.\d+)?\s*[,，]?\s*$')

# 公式编号：(11.8.2)
FORMULA_TAG = re.compile(r'^[（(]\s*\d{1,2}\.\d{1,3}(\.\d{1,3})?\s*[)）]\s*$')

# 一句中文话：有足够多的汉字，且不是被拆碎的名词
CJK = re.compile(r'[\u4e00-\u9fa5]')

# 真句子的标志：句读符号，或至少出现一个「谓语性」的虚词。
# 表格里的列名行「可燃气体在空气中的体积分数/%」汉字占比很高，但它不是句子：
# 没有句号、没有谓词。只看汉字占比会把它当正文，从而把整张表当作「结束了」。
SENTENCE_HINT = re.compile(r'[。，；：？！,;:?!]|(的|了|是|为|即|则|故|由|得|使|可|能|将|在|与|及|其|被|把)')
NON_SENTENCE_TAIL = re.compile(r'[/%]$')


def han_ratio(s: str) -> float:
    if not s:
        return 0.0
    return len(CJK.findall(s)) / len(s)


def classify(line: str) -> str:
    """给一行打标签。顺序很重要：先排除最没信息量的，再认有结构的。"""
    s = line.strip()
    if not s:
        return 'blank'
    if PAGE_NO.match(s) and len(s) <= 4:
        return 'noise'
    if FORMULA_TAG.match(s):
        return 'formula_tag'
    m = TABLE_TITLE.match(s)
    if m and '。' not in s:
        return 'table_title'
    if SECTION.match(s) and len(s) < 40:
        return 'section'
    if CHAPTER.match(s) and len(s) < 40:
        return 'chapter'
    if FIGURE.match(s):
        return 'figure'
    if AXIS_NOISE.match(s):
        return 'noise'
    if SYMBOL_ONLY.match(s):
        return 'noise'
    # 中文正文判定。这里踩过一个必须记住的坑：
# 第一版写成「汉字占比 > 0.45 且长度 >= 30 就算正文，否则看谓词」，
# 结果「下面对此式加以讨论。」「提出了以下反应机理：」这类 14–29 字的论证衔接句
# 全被判成表格碎片扔掉——而心得里恰恰靠这些句子判断作者在哪立论。
# 现在的判据是：**以句号/冒号/分号收尾，或含谓词，就算正文**；
# 只有「以 / % 结尾的列名」和「纯名词堆叠」才算表格碎片。
    if han_ratio(s) > 0.45 and len(s) >= 10:
        if len(s) >= 30:
            return 'prose'
        if NON_SENTENCE_TAIL.search(s) and not re.search(r'[。，；：]', s):
            return 'table_frag'
        if s.endswith(('。', '：', '；', '！', '？', ':', ';')):
            return 'prose'
        if SENTENCE_HINT.search(s):
            return 'prose'
        return 'table_frag'
    # 公式：有等号/关系符，且不太长
    if FORMULA.search(s) and len(s) <= 60 and '。' not in s:
        return 'formula'
    # 短名词行：像是表格里被拆出来的物质名
    if len(s) <= 12 and count_name_tokens(s) >= 1:
        return 'table_frag'
    if len(s) <= 12 and count_numbers(s) == 0:
        return 'table_frag'
    return 'other'


def digest_page(lines, tags_out=None):
    """把一页压成若干条「保留 / 换化」记录。"""
    out = []
    i = 0
    while i < len(lines):
        s = lines[i].strip()
        kind = classify(s)
        if kind == 'table_title':
            # 往后看，直到表体结束，把这段整体换化成一行。
            # 关键的坑：表格碎片行（table_frag）本身也会被 classify 认成
            # "短名词行"，所以这里必须显式把 table_frag 也算作表体，
            # 否则每一行碎片都各自独立、表体计数永远是 0。
            j = i + 1
            body_rows = 0
            while j < len(lines):
                k = classify(lines[j].strip())
                if k in ('prose', 'section', 'chapter', 'table_title', 'formula_tag', 'formula'):
                    break
                if k in ('table_frag', 'noise', 'other'):
                    body_rows += 1
                    j += 1
                    continue
                break
            if body_rows >= 2:
                out.append({
                    'kind': 'table',
                    'text': '[表 %s — 表体 %d 行已省略]' % (s[:44], body_rows),
                    'dropped_lines': body_rows,
                })
                i = j
            else:
                # 只有一两个碎片，那就是个普通行，别硬当成表
                out.append({'kind': 'table_title', 'text': s})
                i += 1
            continue
        if kind == 'noise':
            out.append({'kind': 'noise', 'text': s, 'dropped': True})
        elif kind in ('prose', 'section', 'chapter', 'figure', 'formula', 'formula_tag'):
            out.append({'kind': kind, 'text': s})
        elif kind == 'table_frag':
            out.append({'kind': 'table_frag', 'text': s, 'dropped': True})
        else:
            out.append({'kind': 'other', 'text': s})
        i += 1
    return out


def digest(text: str, pdf_from=None, pdf_to=None):
    pages = split_pages(text)
    kept_pages = []
    stats = {'pages': 0, 'chars_in': 0, 'chars_out': 0,
             'dropped_noise': 0, 'dropped_table_frag': 0, 'dropped_table_body': 0}
    for p in pages:
        no = p['pdf']
        if no is not None:
            if pdf_from is not None and no < pdf_from:
                continue
            if pdf_to is not None and no >= pdf_to:
                continue
        stats['pages'] += 1
        raw = [ln.rstrip('\r') for ln in p['lines']]
        stats['chars_in'] += sum(len(x.strip()) for x in raw)
        rows = digest_page([x for x in raw if x.strip()])
        kept = []
        for r in rows:
            if r.get('dropped'):
                if r['kind'] == 'noise':
                    stats['dropped_noise'] += 1
                else:
                    stats['dropped_table_frag'] += 1
                continue
            if r['kind'] == 'table':
                stats['dropped_table_body'] += r.get('dropped_lines', 0)
            kept.append(r['text'])
        stats['chars_out'] += sum(len(x) for x in kept)
        if kept:
            kept_pages.append({'pdf': no, 'lines': kept})
    return {'pages': kept_pages, 'stats': stats}


def render(result, show_dropped=False):
    out = []
    st = result['stats']
    ratio = (st['chars_out'] / st['chars_in'] * 100) if st['chars_in'] else 0
    out.append('蒸馏结果：%d 页，%d → %d 字符（保留 %.0f%%）'
               % (st['pages'], st['chars_in'], st['chars_out'], ratio))
    out.append('丢弃：噪声行 %d，表格碎片行 %d，表格表体 %d 行'
               % (st['dropped_noise'], st['dropped_table_frag'], st['dropped_table_body']))
    out.append('')
    for p in result['pages']:
        out.append('===== PDF%s =====' % p['pdf'])
        out.extend(p['lines'])
        out.append('')
    return '\n'.join(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog='digest',
                                 description='把一章 OCR 正文压成值得亲自读的那部分')
    ap.add_argument('txt', help='OCR 产出的 txt')
    ap.add_argument('--from', dest='pdf_from', type=int, default=None, help='起始 PDF 页（含）')
    ap.add_argument('--to', dest='pdf_to', type=int, default=None, help='结束 PDF 页（不含）')
    ap.add_argument('--json', action='store_true')
    ap.add_argument('--out', default=None)
    args = ap.parse_args(argv)

    if not os.path.isfile(args.txt):
        print('找不到文件：%s' % args.txt, file=sys.stderr)
        return 2
    with open(args.txt, encoding='utf-8', errors='replace') as fh:
        text = fh.read()

    result = digest(text, args.pdf_from, args.pdf_to)
    body = json.dumps(result, ensure_ascii=False, indent=1) if args.json else render(result)

    if args.out:
        with open(args.out, 'w', encoding='utf-8') as fh:
            fh.write(body)
        print('写入 %s（%d 字符）' % (args.out, len(body)), file=sys.stderr)
    else:
        print(body)
    return 0


if __name__ == '__main__':
    sys.exit(main())
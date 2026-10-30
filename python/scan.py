#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""deep-reading scan —— 从 OCR 文本里提取「骨架」，并标出哪些地方不可信。

它的目的不是读懂书，是在读之前先知道：
  1. 这本书/这一章的结构长什么样（哪几章、多少节、篇幅分布）
  2. 哪些公式、定义、数据、推导是承重墙
  3. 哪些地方 OCR 已经失真，必须回原图核对

最后一条是这个脚本存在的理由。扫描版化学教材里，「表 10.1.1 列出 11 种物质」
却只留下 9 个数值——这种缺口用肉眼看文本是看不出来的，只有数一遍才知道。

用法：
    python scan.py 书.txt
    python scan.py _w_dn_a.txt --json
    python scan.py _w_dn_a.txt --section 10.1
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter, defaultdict

# ---------------------------------------------------------------- 文本清洗

# 页标记有三种历史形态，都要认：
#   ===== 下册 PDF35 =====                （新版 fastocr）
#   ===== 下册 PDF35 / 书p19 =====        （旧版，带书页码）
#   ===== 上册 PDF36 / 书p20 =====
PAGE_MARK = re.compile(r"^=+\s*(.*?)\s*PDF\s*(\d+)\s*(?:/.*?)?\s*=+\s*$")

# 页码行：纯数字，通常 1–4 位，出现在页首页尾
PAGE_NO = re.compile(r"^\d{1,4}$")

# 页眉：常见形态是「第N章 标题」「§N.N 标题」这类重复出现的行
RUNNING_HEAD = re.compile(r"^(第[一二三四五六七八九十百]+章|[§S]\s*\d+(\.\d+)*)\s*\S")

# 章标题：单独成行，行首是「第N章」。中文数字、阿拉伯数字、罗马数字都要认——
# 天津大学《物理化学》用「第十一章」，《有机化学》用「第2章」，
# 《电气工程学概论》用「第Ⅱ1章」（书的整体编序是阿拉伯数字，篇内章序用罗马数字，
# OCR 把 Ⅱ 和 1 并在一行里印成「第Ⅱ1章 模拟信号」）。
#
# 罗马数字必须收：不收的话「第Ⅱ1章 模拟信号」这一行既不是章标题、又是每页
# 反复出现的短行，会被当成页眉整行滤掉——第11章直接从结构里消失。
CHAPTER = re.compile(
    r"^\s*第\s*"
    r"([一二三四五六七八九十]{1,2}|\d{1,2}|[ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩ]+\d{0,2})"
    r"\s*章\s*(\S.*?)\s*$"
)

# 章号认不出来的「第…章」形态：只要长得像章标题，就不许当页眉滤掉。
# 留着它，宁可让章节结构少一条，也别把整章的存在抹掉。
REEF = re.compile(r"^\s*第\s*\S{1,6}\s*章")

# 章标题的三种断法（《有机化学》实测，OCR 把「第N章」与章名拆得毫无规律）：
#   ① 「第二章 烷烃和环烷烃」  章号后带空格，章名同行     ← CHAPTER 原有形态
#   ② 「第二章烷烃和环烷烃」    章名紧贴章号，无空格        ← 粘贴形态
#   ③ 「第二章」单独一行        章名被拆到下一行            ← 断头形态
# ②③ 都逃过了 `第\s*N\s*章\s*(\S.*?)` —— ② 是章号后没有空格就无 \S 可捕，
# ③ 是行尾什么都没有。结果正文里 19 个章有 17 个认不出来，
# 只剩「第八章 有机化合物的波谱分析」「第十四章 β-二羰基化合物」两个有空格的分身。
# 最终章数只报 2 个，而且目录页那些假章反倒成了唯一的记录。
_CHAPTER_HEAD = re.compile(
    r"^\s*第\s*"
    r"([一二三四五六七八九十]{1,2}|\d{1,2}|[ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩ]+\d{0,2})"
    r"\s*章"
    r"(?:[\s_]*(?P<glued>[^\s_].*?))?\s*$"
)

# 节标题：有两种编号制，都要认——
#   §10.1 / § 10.1 / 10.1   点号制（物理化学，OCR 里「§」与号之间、号与标题之间都可能无空格）
#   2.5.4 / 2.1             三级点号制（有机化学，节号直接挂在章下）
# 数字部分限位是为了避免把「10.12 时测得……」这种正文误认成节标题。
#
# 「§」/「S」前缀必须是**强证据**：带前缀时放宽空格要求（§7.9原电池的设计 是物理化学的
# 标准写法，号与标题之间一个空格都没有）；不带前缀时必须至少有一个空格
# （10.1 溶液的表面张力），否则 0.584-42 这种表格数值会被当成节号。
#
# 节号允许段尾带一个小写字母：《物理化学》的目录里会出现「§5.2a」「§5.2b」这类
# 把一节拆成两块的写法，OCR 会把 a/b 粘在节号后面。
SECTION = re.compile(
    r"^\s*(?:[§S]\s*(\d{1,2}(?:\.\d{1,2}){1,2}[a-z]?)\s*(\S.*?)"
    r"|(\d{1,2}(?:\.\d{1,2}){1,2}[a-z]?)\s+(\S.*?))\s*$"
)

# 三级节号（5.1.2 二端网络谐振的特性分析）在《电气工程学概论》里没有「§」前缀、
# 也没有空格，直接贴着标题写。上面那条 SECTION 要求不带前缀时必须有空格，
# 于是这类标题全都认不出来（电气书节数只报了个位数）。
#
# 两级节号同样会贴写（15.1数字集成电路的规格），所以这里统一收 1–3 级：
# 「章.节」「章.节.子节」都算。用 _section3_ok 做内容闸门防止表格数值混入。
SECTION3 = re.compile(
    r"^\s*(\d{1,2}(?:\.(?:[1-9]|\d{2})){1,2})\s*(\S{1,60})\s*$"
)


def _section_title_ok(title: str) -> bool:
    """节标题的内容闸门——SECTION 与 SECTION3 两条路径共用。

    为什么必须抽成函数：这里每一条判据都是踩出来的，而 scan.py 与 map.py 各有
    一份节切分循环。过去 map.py 只复用了正则、没复用闸门，于是同一个 bug 在第
    二个脚本里活了下来——《电气工程学概论》里习题第 7 题
    「15.7 已知某 CMOS逻辑电路的信号频率为10MHz,求其最高扇出能力。」
    长度 38 < 40、SECTION 也匹配得上，scan 那边被句读闸拦住了，map 那边却把它
    当成 §15.7 收进节表（实测报告里出现「§15.7 … 258 字符 14.2%」）。

    判据：
      · 首字必须是汉字或字母——「0.5 108」「2.2 103」这类数值串首字是数字；
      · 不能带句读——带逗号句号的几乎都是题干或正文；
      · 不能是单位/量纲碎片；
      · 不能是「1.5m0」这种公式碎片；
      · 不能是「7.4.1列出了……」这种正文引用句。
    """
    title = (title or "").strip()
    if not title:
        return False
    if not re.match(r"[\u4e00-\u9fffA-Za-z]", title):
        return False
    if re.search(r"[，。；、,;：:]", title):
        return False
    if _NUM_UNIT.match(title) or _SECTION_JUNK.search(title):
        return False
    if len(title) <= 6 and re.match(r"^[a-zA-Z℃°][a-zA-Z0-9℃°'’\-\-－_]*$", title):
        return False
    if re.search(r"(列出|给出|所示|表明|说明|可见)", title):
        return False
    return True


def _section3_ok(s: str):
    """无前缀节号的第二道闸：标题必须像标题。

    SECTION3 比 SECTION 松得多（没有 § 前缀、也不要求空格），所以必须用内容
    特征把表格数值挡回去：
      · 标题首字必须是汉字或字母——「0.5 108」「2.2 103」这类数值串首字是数字，
        而「z域传递函数」是合法标题；
      · 不能带句读——「5.1.2 研究表明，……」是正文；
      · 不能是单位/量纲碎片——交给 _NUM_UNIT 与 _SECTION_JUNK 复判。
    认不出来就返回 None，调用方按「不是节标题」处理。
    """
    m = SECTION3.match(s)
    if not m:
        return None
    title = m.group(2)
    if not _section_title_ok(title):
        return None
    # 「7.4.12 5℃时水溶液中一些电解质……」——表号与表题粘连成一节的情形。
    # 节号段尾的两位以上数字，只有在真三级节号里才合理（如 11.12.3），
    # 而两位的末段多半是 OCR 把「7.4.1」后面的「2」粘上去了。
    # 判据：末段是两位数字且全行以数字/单位开头，就当表格标题处理。
    if re.search(r"\.\d{2}$", m.group(1)) and re.match(r"[\d\-−+]", title):
        return None
    return m

# 节标题的第二道防线：光靠正则会把表格数值当节号。
#   「0.25 nm」「0.584-42」「1.5 倍」——OCR 打散表格后，每个单元格单独成行，
#   「数值 + 空格 + 词」的形态与「2.5 烷烃和环烷烃的物理性质」一模一样。
#   实测有机化学 20 页试点里 §0.25 nm 就是这么混进节列表的（570 字符的假节）。
#
# 判据：节号是「章.节」结构，章号必须真的存在过。有机化学在 PDF60 才开始第二章，
# 那么 0.25 的章号 0 根本不可能是章号；反过来 2.5 的 2 就是第二章。所以节号
# 第一段必须落在 [1, 当前已认出的最大章号 + 1] 区间内。这条判据与书无关、与
# OCR 质量无关，只依赖「章号是有序的」这个结构事实。
_NUM_UNIT = re.compile(r"^\s*[+\-−]?[\d.,]+\s*(?:nm|kJ|mol|cm|mm|V|K|℃|%|倍|次|号|个|mol/L)\b", re.I)

# 节标题的第三道防线：OCR 把公式里「1.7×10⁻⁶」拆成「1.7」和「X10-6」两行，
# 于是「1.7 X10-6」完美符合「节号 + 空格 + 内容」，章号 1 也在合法区间内
# （上册第一章确实存在），_NUM_UNIT 也抓不住（X10-6 不在单位表里）。
# 实测下册第十一章里混进了 §1.7 X10-6、§1.06 X10-s、§5.0 s-'。 三个假节，
# 字符数还都不小（5460 / 1521 / 3127），会污染「重心」排名。
#
# 判据：真节标题是词，不是量纲碎片。标题里出现「X10」「×10」「10-3」这类
# 数量级碎片，或标题只有一个孤零零的符号（s / s-' / ℃），就不是节标题。
_SECTION_JUNK = re.compile(
    r"(?:[X×x]\s*10|10\s*[+\-−]\s*\d)"
    # 标题只是一个量纲碎片：s / s' / s- / s-' / ℃ / K / mol 之类，允许尾巴带句点。
    # 注意 s-' 这种写法（s 撇 短横）在 OCR 里很常见，字符类必须把 '-－’‘ 全收，
    # 少了任何一个，「s-'。」就漏过去，成为一条 3127 字符的假节。
    r"|^[a-zA-Z℃°][a-zA-Z℃°'’\-\-－]*[。.]?$"
)

# 节标题里的正文误判防线：行太长、或以逗号句号收尾的，都不是节标题
SECTION_TAIL = re.compile(r"[，。；、,;]$")

# ---------------------------------------------------------------- 五类信号

# ① 公式：含等号的短行，允许有 LaTeX 化的符号。
# 但「含等号」不够——有机化学的反应式常写成
#   '→ RCOONH4 + 2 Ag↓ + H2O + 3 NH3'（银镜反应）
# 全行只有一个箭头，没有任何等号，照样是这一节最重要的公式。
# 所以候选集是「等号类关系符」∪「反应箭头」。
FORMULA = re.compile(r"[=∝≈≤≥→⇌]")
FORMULA_NUMBERED = re.compile(r"^(.{3,80}?)\s*\((\d{1,2}\.\d{1,3}(?:\.\d{1,3})?[a-z]?)\)\s*$")

# ①b 真公式的「非化学式成分」判据：出现这些就不是结构式碎片。
# 物理化学的公式是纯等式（ΔG = ΔH − TΔS），没有箭头，所以不能用箭头当判据；
# 而有机化学被 OCR 拆碎的结构式（'CH2=C'、'=0'、'o='）里只有元素符号、数字和键线，
# 一个都不会命中下面任何一类。
_PROSE_FORMULA = re.compile(
    r"[\u4e00-\u9fff"            # 汉字：任何中文说明成分
    r"α-ωΑ-ΩΣΔΛΘΦΨΩ"          # 希腊字母：ΔG、ΔH、φ、θ、λ
    r"√∫∑∏∂∞"                # 数学算子
    r"]"
    r"|(?:ln|lg|log|sin|cos|tan|exp|min|max|lim)\b"
    r"|\^\s*[-\d{(]"             # 上标：x^2、10^-3
    r"|/"                         # 分数线：dc/dt、kJ/mol
)

# ①c 叙述句会被误收成公式，因为它们含「=」也含汉字。
# 实测抽样（物理化学下册 859 条）里约 30% 是这种：
#   '上式中令α=-1，且去掉对数，即得'
#   '值，如E°（Zn²+|Zn）=一0.7620V,则△G（T，p）>0,表明当各反应组分均处在'
#   '如果粒子的电子运动与核运动能级不开放，则Um=0,Um.=0,从而这两种运动对'
# 共同特征：含句读（，。；、）或含叙述连接词（则/即/从而/表明/如果/故/但）。
# 真正的公式即使夹了中文也极短（'当T=298K时'），所以再加一条长度上限。
# 另外题干也会漏进来：'11.41某气相反应A十C一→D的机理如下：'
# 特征是以习题号开头、或带「的」「如下」「试」「求」这类问句词、或结尾是冒号。
_NARRATIVE = re.compile(
    r"[，。；、]|(则|即得|从而|表明|如果|故|但是|因为|所以|可见|由此)"
    r"|(的|如下|试|求|计算|写出|指出|说明)"
    r"|(答：|解：|答:|解:)"
    r"|[:：]\s*$"
    r"|^\s*\d{1,3}[.\-]\d{1,3}\s*某?[^\d]"     # 习题号开头：'11.41某气相反应…'
    r"|^\s*\d{1,3}[.\-]\s*[\u4e00-\u9fff]"     # 习题号+中文：'22.一反应进行一次…'
    r"|^\s*[(（]\d{1,2}[)）]\s*[(（]"           # 习题选项：'(5)(CH_2=CH2CH,…'
)
_FORMULA_MAXLEN = 46


def formula_is_fragment(s: str) -> bool:
    """这行是结构式碎片，还是真公式？

    试过三种判据，全部不成立，教训记在这里免得再走一遍：

    1. 「有没有反应箭头」——物理化学的公式全是纯等式（ΔG = ΔH − TΔS），
       一个箭头都没有，带箭头才收会把下册公式从 1301 条砍到 35 条。
    2. 「有没有孤立单字母物理量」——CH3—C=O+ CH2—CHO 里的 C、H、O
       会被逐个漏成「物理量」，整行判成公式。
    3. 「元素配对密度」——CH3CH=CHCOOH、CH=C—COOH 里 COOH、CHO
       不含数字，配对占比永远上不去，照样判错。

    根因是同一个：**「这是不是一个化学式」这件事，在 OCR 把二维结构
    拍平成一行文本时就已经丢了。** 靠字符统计去反推，只会把分类问题
    变成调参问题。

    所以这里不做「是不是真公式」的判决，只做一件笨但诚实的事：
    看这行**有没有非化学式的成分**（汉字、希腊字母、运算符、函数名）。
    有，就是真公式；没有，就归到碎片档并且明说「分不清」。
    宁可把几条纯拉丁字母的公式（p = nkT）算成碎片，也不要把两百条
    结构式碎片算成公式——因为前者只让密度略降，后者会让我拿插图页
    当推导页。
    """
    # 带反应箭头的永远是反应式，不论化学味多重。
    # 有机化学的反应式长这样：'→ RCOONH4 + 2 Ag↓ + H2O + 3 NH3'——
    # 除了箭头全是元素符号，按下面那条判据会被判成碎片，白白丢掉
    # §11.5.3 缩合反应那几张关键的银镜反应式。
    if "→" in s or "⇌" in s:
        return False
    # 叙述句不是公式：含句读或叙述连接词、或长得像一句话。
    # 这一条必须在 _PROSE_FORMULA 之前判——叙述句几乎必然命中汉字分支，
    # 只看汉字的话它们会被当成「有中文成分的真公式」全收进来。
    if _NARRATIVE.search(s) or len(s) > _FORMULA_MAXLEN:
        return True
    return not _PROSE_FORMULA.search(s)

# ② 定义：「称为/称作/简称/定义为/定义式」
DEFINE = re.compile(r"(称为|称作|简称|定义为|定义式|叫做|所谓)")

# ③ 数据表：表标题行。「表」后面必须紧跟编号，标题行不带句号；
#    「表8.5.1给出了几个……」这种正文引用句以「给出了/中列出/列出/是/为」结尾前有句号，
#    必须排除，否则会把上一张表的表体整片清空。
#    两种编号制都要认：表 11.3.2（点号制，物理化学）、表 2-3（横线制，有机化学）。
#
#    关键防线：正文引用句「表 2-5中，反应①是 C—H键断裂……」沿用了同一个编号前缀，
#    只靠「不含句号」挡不住它（它的句号在很后面）。真正的差别是：
#    表标题之后紧跟的是表体（短行、无数值句、无逗号），引用句之后是完整句子。
#    所以用 _TABLE_TITLE_TAIL 排除带「中/所示/给出/列出/可以看/表明」等引用动词的尾巴。
TABLE_TITLE = re.compile(r"^\s*表\s*(\d{1,2}(?:[.\-]\d{1,2}){1,2})\s*([^。]*)$")
_TABLE_TITLE_TAIL = re.compile(
    r"^(?:中|中列出|中给出|所示|可以看出|可见|表明|说明|列出|给出|和|及|与|、|，|,|是|为)"
)

# 表标题与温度粘连：《物理化学》印的是「表 7.4.1 25℃时水溶液中一些电解质……」，
# OCR 把书名号里的空格吃掉，于是变成「表 7.4.125℃时水溶液中……」，表号被读成
# 7.4.12（多一个 2）、7.7.12（多一个 2）。表号只有三段，第四段必是正文/温度的一部分。
#
# 判别依据不是「长得像表号」，而是「表号之后紧跟的字符类型」：
# 真表号后面是空格、标题词或行尾；被粘连的后面紧跟数字或 ℃。
TABLE_TITLE = re.compile(r"^\s*表\s*(\d{1,2}(?:[.\-]\d{1,2}){1,2})\s*([^。]*)$")
_TABLE_TITLE_TAIL = re.compile(
    r"^(?:中|中列出|中给出|所示|可以看出|可见|表明|说明|列出|给出|和|及|与|、|，|,|是|为)"
)

# 表标题与温度粘连：《物理化学》印的是「表 7.4.1 25℃时水溶液中一些电解质……」，
# OCR 把表号与正文之间的空格吃掉，变成「表 7.4.125℃时水溶液中……」，
# 表号会被读成 7.4.12（凭空多出一位 2）。7.10.1 同样被读成 7.10.12。
#
# 这里不用正则表达表号，因为「几位属于表号、几位属于温度」本来就是一个
# 长度切分问题，而正则的贪婪/非贪婪在 `\d` 边界上会互相打架（试过
# `(\d{1,3}(?:\.\d{1,2}){2})(?=\d\s*[℃])`：对 7.4.12 + 5℃ 会同时满足
# 与 7.4.1 + 25℃，无法区分）。改成数数字串长度再切。
_GLUE_HEAD = re.compile(r"^\s*表\s*([\d.\-]+)\s*([℃°C].*)$")


def split_table_id(num: str):
    """把「7.4.125」这种粘连编号切成 (真表号, 被吞掉的温度首位)。

    原文「表 7.4.1 25℃时……」被 OCR 吃掉空格后变成「表 7.4.125℃时……」，
    所以数字串最后一个「5」是 25℃ 的首位，前面的「7.4.12」才是……不对——
    这里必须看清切点：末段「125」= 「1」(真序号) + 「25」(温度)。
    判据：温度必然是 15/20/25/37/100 ℃ 这类整数，末段最后两位能构成
    合法的整十温度时，就把它们判给温度。

    反过来，若末段是 3 位但最后两位不是整十（如 7.4.113），按真表号处理，
    宁可留一个可疑编号，也不编造。
    """
    m = re.match(r"^(\d{1,2})\.(\d{1,2})\.(\d+)$", num)
    if not m:
        return num, ""
    a, b, c = m.group(1), m.group(2), m.group(3)
    if len(c) < 3:
        return num, ""
    # 末两位是整十温度（10/15/20/25/30/35/40/50/60/80/100 取末两位）
    if c[-2:] in _TEMP_TAILS:
        return "%s.%s.%s" % (a, b, c[:-2] or "0"), c[-2:]
    return "%s.%s.%s" % (a, b, c[:-1]), c[-1:]


# 常见实验温度的后两位。只能在粘连判别里用，不能拿来做别的事——
# 它是「25℃」「100℃」这类原文里真的会出现的值，不是任意数字。
_TEMP_TAILS = {
    "10", "15", "20", "25", "30", "35", "37", "40", "45", "50",
    "55", "60", "65", "70", "75", "80", "85", "90", "95", "00",
}
# 续表
TABLE_CONT = re.compile(r"^\s*续\s*表\s*(?:(\d{1,2}[.\-]\d{1,2}[.\-]\d{1,2}))?\s*$")

# ④ 推导：让、则、故、由此可得、代入、整理得
DERIVATION = re.compile(r"(令|则|故|由此可得|将.{0,12}代入|整理得|化简得|可得|得(?=[^\w])|即|于是)")

# ⑤ 图：图 N-N、图 N-N-N、图N.N.N
#
# 三级编号在工科书里是常态：电工学的图号是「图15-5-6」（章-节-序），
# 而物理化学用「图 11.3.1」。原来只写死两级 `(\d{1,2}[-.]\d{1,2})`，
# 遇到三级就把「15-5」当完整编号、剩下的「-6 集成组件热阻与」当标题，
# 报告里于是出现一批 id 重复（15-5 出现两次）且标题以「-」开头的条目。
# 改成「一到三段数字，段间用 - 或 .」后，15-5-6 整体归入 id。
#
# 同时必须把「图15-5-6所示的曲线」这种正文引用挡掉：它后面紧跟的是
# 「所示/中/给/是/可」这类词，不是一个图题。图题的特征是编号后空一格
# 再跟名词短语，或直接跟名词。
FIGURE = re.compile(r"^\s*图\s*(\d{1,2}(?:[-.]\d{1,2}){1,2})\s*([^。]{0,40})$")

# 「图X所示的…」这类正文引用，不是图题。判据：编号后第一段文字以这些词打头。
_FIG_REF_TAIL = re.compile(
    r"^(?:所示|中|所|给(?:出)?|可|是|的|为|和|及|与|在|如|基|显|表|示|画|看|查|题)"
)

# 图号也只有章-序号两段时的兼容：物化的「图11-3」。
_FIG_DUP_ID = re.compile(r"^(\d{1,2}[-.]\d{1,2})[-.]\d{1,2}$")

# 习题/思考题
EXERCISE = re.compile(r"^\s*(思\s*考\s*题|概\s*念\s*题|习\s*题|练\s*习)\s*$")

# 数值：阿拉伯数字，含千位空格、小数、科学计数、正负号、±
NUMERIC = re.compile(r"^[+\-−]?\d[\d\s]*(?:[.,]\d+)?(?:\s*[×x]\s*10[+\-−]?\d+)?(?:\s*±\s*\d+(?:\.\d+)?)?$")


def is_numeric_line(line: str) -> bool:
    s = line.strip().replace("−", "-")
    if not s or len(s) > 24:
        return False
    if not re.search(r"\d", s):
        return False
    return bool(NUMERIC.match(s))


# 一行里的「数值」：允许 5.35、1.704×10、−123、0.0
_NUM_TOKEN = re.compile(r"[+\-−]?\d+(?:\.\d+)?(?:\s*[×x]\s*10[+\-−]?\d+)?")

# 「纯编号行」：整行只有**带括号或明确章节前缀**的编号，没有别的内容。
#
# 这里绝不能放裸的 \d+\.\d+。表格里的数值大量就是 1.3 / 4.4 / 6.7 这种形态，
# 一旦裸匹配就把整张表的数字清空——表 2-6 九个数值只数出三个，被判「整列被吞」。
# 只有「（11.1.1）」「第2章」「§2.4」这类带外部标记的，才算编号。
_CHAPTER_NO_LINE = re.compile(
    r"^[\s【\[]*(?:第?\s*\d{1,2}\s*[章节]|§\s*\d+(?:\.\d+)*|[（(]\s*\d+(?:\.\d+)+\s*[）)])\s*$"
)


def count_numbers(line: str) -> int:
    """数一行里有几个独立数值。

    这是判断「表格被吞了多少」的核心度量：表 11.3.2 那一行该有 6 个键能数值，
    OCR 只剩 5 个，用行数根本看不出来，用数值个数一眼就看出来了。
    """
    s = line.strip()
    if not s or len(s) > 200:
        return 0
    # 排除明显的公式编号行、章节号行。
    #
    # 曾经写成 [0-9]+\.[0-9]+ 就 return 0，结果表格里的小数（1.3 / 4.4 / 6.7 / 2.1×10⁵）
    # 全被当成「节号」吞掉了，一张 9 个数值的表只数出 3 个，误判成「整列被吞」。
    # 正确判据：只有当这一行除了编号之外什么都不剩时，才算编号行。
    if _CHAPTER_NO_LINE.match(s):
        return 0
    return len(_NUM_TOKEN.findall(s))


# 一行里的「列名词」：化学式、物质名、键型、物理量符号
_NAME_HINT = re.compile(
    r"(能|焓|熵|张力|电势|电极|浓度|温度|压力|半径|系数|常数|键|半径|电位|活度|体积|密度|"
    r"热容|黏度|黏|功|自由能|波长|频率|电离|平衡|速率|级数|半衰期|单位|符号|名称|物质|"
    r"H2|O2|N2|CH|Cl|Br|Na|K\+|OH|Y\s*[0-9.]|R\s*[0-9]|θ|φ|σ|π)"
)
_CHEM_FRAG = re.compile(r"\b(?:H|O|N|C|Cl|Br|Na|K|Ca|Mg|S|P|F|I)\d?\b")

# OCR 垃圾记号：双写符号（++ / -- / ····）、省略号、纯标点。
# 这类东西既不是数值也不是物质名，但它夹在表格里时会占掉一个「列名词」的位置，
# 把分母抬高，于是「表其实完好」被判成「整列被吞」。表 2-5 就是这么误判的：
# 原文里 F 列下方 OCR 吐出「++」（本应是连续两个短横），判定器把它当成了列名。
#
# 注意：正则里绝对不能含 \d。曾经写成 [+\-−—·…\.\s\d,，、]*，
# 结果「-130」这种正常数值行也被当成垃圾，数值计数归零，
# 一张完好的表被判成「只有 6 个数值，整列被吞」。
_OCR_JUNK = re.compile(r"^[+\-−—·…\.\s,，、]*$")

# 表头词：出现在表标题之后头几行的「单位/量名」，它们不是数据列名。
# 「∆Hm/(kJ·mol−1)」「反应过程」「卤原子」都属于这一类，命中 _NAME_HINT 但不算数据。
_HEADER_WORD = re.compile(r"(反应过程|反应|过程|卤原子|卤素|化合物|名称|物质|项目|序号|编号|备注|单位)")

# 反应式行：含化学键箭头（→ ⇌ ↑ ↓）或以圈码序号开头，属于表格的说明列，不是数据维度。
_REACTION_LINE = re.compile(r"[→⇌↑↓]|^[\u2460-\u2473]|^[①②③④⑤⑥⑦⑧⑨⑩]")


def _is_junk(s: str) -> bool:
    """判断一行是不是 OCR 垃圾记号（不是数、也不是名字）。"""
    if not s:
        return True
    if _OCR_JUNK.match(s) and len(s) <= 6:
        return True
    # 纯单位行：kJ·mol−1 / mol−1 / (d20)* / n20 这类，不是数据列
    if re.match(r"^[/（(]?\s*(?:kJ|J|K|mol|g|cm|nm|dm|Pa|kPa|MPa)", s) and len(s) <= 24:
        return True
    return False


def count_name_tokens(line: str) -> int:
    """数一行里有几个「列名/物质名」片段。

    表头词单独统计，不计入数据列名——否则「∆Hm/(kJ·mol−1) 反应过程 F Cl Br I」
    会被数成 10 个列名，而它其实只有 4 个数据列（F/Cl/Br/I）。

    反应式行（「① X· + CH4 → HX + CH_3」）也不算列名：它是表格左侧的说明列，
    不是被统计的数据维度。判据是含有化学键箭头或「①」这类序号。
    """
    s = line.strip()
    if not s or len(s) > 200:
        return 0
    if _is_junk(s):
        return 0
    if _HEADER_WORD.search(s) and count_numbers(s) <= 2:
        # 纯表头行（含「反应过程」「卤原子」这类词且几乎没有数值）
        return 0
    if _REACTION_LINE.search(s):
        # 反应式 / 说明列，不计入数据列名
        return 0
    hits = len(_NAME_HINT.findall(s)) + len(_CHEM_FRAG.findall(s))
    return hits


def chapter_key(raw: str):
    """把章标题的原始捕获组清洗成统一的键；不像章标题时返回 None。

    三个必须做的清洗：

      ① 剥掉 OCR 从章首页装饰下划线认出来的 «_»。原书章首页标题下有一条短横线，
         OCR 会把它当字符：「第九章_统计热力学初步」。不剥就和
         「第九章统计热力学初步」变成两条记录，章数多报一个。

      ② 剥掉目录里的引导点与页码：「第二章 烷烃和环烷烃.....」「第五章 芳烃芳香性………153」。
         目录页的章标题与正文章标题长得几乎一样，差别只在尾巴上那串点线和页码。
         不剥的话，目录里的每一条都会被当成一个真章，章数直接翻倍，
         而且章的起始页全指到目录页，后面「这章多重」全部算错。

      ③ 拒绝句读。正文里会出现「第四章推导稀溶液的依数性时，讨论的是非电解质
         溶液……」，前四个字恰好是「第四章」+ 短语，会被 CHAPTER 认成章标题。
         章标题不可能是句子：带逗号/句号/分号的一律不算。

    这个函数必须由 scan.py 与 map.py 共用——map.py 曾经自己用
    re.sub(r"\\s+", "", m.group(2)) 算键，于是 scan 认出的「第九章统计热力学初步」
    与 map 算出的「第九章_统计热力学初步」对不上，--chapter 9 报「没找到章节」。
    """
    t = (raw or "").strip(" _·—－-")
    # 尾部的引导点线与页码：「烷烃和环烷烃.....」「芳烃芳香性………153」「绪论....1」
    t = re.sub(r"[\s.．·…⋯_\-—]{2,}\s*\d*\s*$", "", t)
    t = re.sub(r"\s*\d+$", "", t) if re.search(r"[.．…⋯]{2,}", raw or "") else t
    t = t.strip(" _·—－-.．…⋯")
    # 顿号「、」是并列，不是句读。章名里顿号很常见：
    # 「第十一章 醛、酮和醌」「第十六章 含硫、含磷和含硅有机化合物」
    # 「第二十章 氨基酸、蛋白质和核酸」。
    # 早先把它当句读一律拒绝，这三章直接消失（19 章只报 17 章）。
    # 真正要拒的是句末标点与冒号——那些才是「这行是句子，不是标题」的证据。
    if not t or re.search(r"[。；：,;:]|，", t):
        return None
    return re.sub(r"\s+", "", t)


def split_pages(text: str):
    """把带 `===== 源文件 PDFn =====` 标记的文本切成页。

    完整版 fastocr 输出一定有这个标记；如果整份文本没有标记（例如旧脚本产物
    中间有断档），就整段当一页，并在结果里记 warning。
    """
    pages = []
    cur_no, cur_lines = None, []
    header = None
    for raw in text.split("\n"):
        m = PAGE_MARK.match(raw.strip())
        if m:
            if cur_lines or cur_no is not None:
                pages.append({"pdf": cur_no, "source": header, "lines": cur_lines})
            header, cur_no, cur_lines = m.group(1), int(m.group(2)), []
            continue
        cur_lines.append(raw.rstrip("\r"))
    if cur_lines or cur_no is not None:
        pages.append({"pdf": cur_no, "source": header, "lines": cur_lines})
    return pages


def clean_page_lines(lines):
    """清洗一页的行：去掉页码行、去重「同一行连续重复」（OCR 常见）。"""
    body = []
    prev = None
    for ln in lines:
        s = ln.strip()
        if not s:
            continue
        if PAGE_NO.match(s) and len(s) <= 4:
            continue
        if s == prev:            # OCR 把同一行吐了两遍
            continue
        prev = s
        body.append(s)
    return body


# 行尾粘连的页码：「11.8 醌 389」「11.5 醛和酮的化学性质 383」「11.8 醌391」
# 这是《有机化学》页眉的常态——节号、节名、页码挤在同一行。
# 不剥掉的话，同一节在不同页会记成「醌 389」「醌」「醌391」三条，
# 既污染节名，又让「这节多大」算不出来。
#
# 页码前的空格可有可无：OCR 经常把「醌 391」压成「醌391」。
# 但绝不能剥掉标题本身就以数字结尾的情况（「1,2-加成」结尾是汉字，安全；
# 「康尼查罗反应 2」这种极少见，宁可留着）。
_TRAILING_PAGENO = re.compile(r"\s*\d{1,4}\s*$")


def _strip_trailing_pageno(s: str) -> str:
    """剥掉行尾粘连的页码，但只在剥完仍是「标题」时才剥。

    「醌389」剥完只剩「醌」一个字，看着像误伤，其实这才是真标题
    ——书上这一节就叫「11.8 醌」，页码 389 粘在上面。
    先前用「剥完长度必须 ≥2」去挡误伤，结果单字节名全被退回原样，
    「醌389 / 醌391 / 醌」三条并列。短标题是合法的，这条闸要撤。

    真正要防的是「整行只是数字」：那种情况剥完是空串，自然返回原文。
    """
    t = _TRAILING_PAGENO.sub("", s).strip()
    if t and re.search(r"[\u4e00-\u9fffA-Za-zα-ωΑ-ΩΣΔ]", t):
        return t
    return s


# ---------------------------------------------------------------- 主扫描


def _all_chapter_nums(pages):
    """预先扫一遍全书，收集所有出现过的章号。

    为什么必须先扫：主循环里章标题与节标题是交替出现的，而第一节往往先于
    它所属的章标题被扫到（章首页的正文前几行就是「§11.1 …」）。此时如果用
    「已经扫到的章」去判断节号是否越界，§11.1 就会被判成越界而丢掉。
    """
    nums = set()
    for p in pages:
        for s in clean_page_lines(p["lines"]):
            m = _CHAPTER_HEAD.match(s)
            if m and len(s) < 40 and chapter_key(m.group("glued") or "") is not None:
                # 「第Ⅱ1章」的章号是罗马数字+阿拉伯数字的组合，int() 与 isdigit()
                # 都认不出，统一交给 _cn_to_int；这一步早先是
                # int(n) if n.isdigit() else _cn_to_int(n)，看起来等价，
                # 但 _cn_to_int 当时还不认罗马数字，于是 11 被算成 0 丢掉，
                # 后面 SECTION 的章号区间守卫把 §11.x 全判越界。
                nums.add(_cn_to_int(m.group(1)))
    return {n for n in nums if n} or {1}


_CN_DIGIT = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
             "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}

# 罗马数字：只到 Ⅹ（第10章以内足够，书里最多用到 Ⅳ）——
# 《电气工程学概论》的章号是「篇序号（罗马）+ 章序号（阿拉伯）」。
_ROMAN = {"Ⅰ": 1, "Ⅱ": 2, "Ⅲ": 3, "Ⅳ": 4, "Ⅴ": 5,
          "Ⅵ": 6, "Ⅶ": 7, "Ⅷ": 8, "Ⅸ": 9, "Ⅹ": 10}


def _cn_to_int(s: str) -> int:
    """「十一」→11，「2」→2，「Ⅱ1」→11；认不出返回 0。"""
    s = (s or "").strip()
    if s.isdigit():
        return int(s)
    # 罗马数字 + 阿拉伯数字混排：「Ⅱ1」是书里第 11 章（第Ⅱ篇第1章）。
    # 《电气工程学概论》整本书按阿拉伯数字编章序，章首却印成「第Ⅱ1章」。
    m = re.match(r"^([ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩ]+)(\d{0,2})$", s)
    if m:
        roman, tail = m.group(1), m.group(2)
        if roman not in _ROMAN:
            return 0
        return _ROMAN[roman] * 10 + (int(tail) if tail else 0)
    if not s or any(ch not in _CN_DIGIT for ch in s):
        return 0
    if "十" not in s:
        return _CN_DIGIT[s] if len(s) == 1 else 0
    a, _, b = s.partition("十")
    return (_CN_DIGIT[a] if a else 1) * 10 + (_CN_DIGIT[b] if b else 0)


def scan(text: str):
    pages = split_pages(text)
    has_marks = any(p["pdf"] is not None for p in pages)
    _chapter_nums = _all_chapter_nums(pages)

    # ---- 先把「断头章标题」补齐 ----
    #
    # 《有机化学》每章首页印的是两行：「第二章」/「烷烃和环烷烃」（章名另起一行）。
    # 页眉里也有同样的两行。只看单行，这两行谁都认不出是章标题。
    # 所以先做一遍页内拼接：若某行匹配「第N章」且后面没有章名，
    # 就把它的下一行接上去；接上之后像不像章标题，交给 chapter_key 裁决。
    #
    # 判据必须严：下一行要短（≤24 字）、不含句读、不含数字，
    # 否则「第二章」后面紧跟正文「推导稀溶液的依数性时，讨论的是……」会被粘起来。
    for p in pages:
        lines = p["lines"]
        for i, ln in enumerate(lines):
            m = _CHAPTER_HEAD.match(ln.strip())
            if not m:
                continue
            tail = (m.group("glued") or "").strip()
            if tail:
                continue                     # 已有章名，不必拼接
            if i + 1 >= len(lines):
                continue
            nxt = lines[i + 1].strip()
            if not (2 <= len(nxt) <= 24):
                continue
            # 句读判据里不能有顿号：章名里的顿号是并列，不是句子。
            # 「第十六章 / 含硫、含磷和含硅有机化合物」被这条挡掉过一次，
            # 结果第十六章整章读不到，连带 §16.x 的 23 个节全被判越界
            # （chapter_start 里没有 16）。
            if re.search(r"[。；：,;:]|，", nxt):
                continue
            if CHAPTER.match(nxt) or SECTION.match(nxt):
                continue
            # 粘成「第二章烷烃和环烷烃」；_CHAPTER_HEAD 的 glued 分支能吃掉它
            lines[i] = ln.rstrip() + nxt
            lines[i + 1] = ""                # 占位不删，保持索引稳定

    # ---- 再把「断头节标题」补齐 ----
    #
    # 《有机化学》的节标题也有断头形态：页眉印两行
    #     「11.5」
    #     「醛和酮的化学性质」
    # 只认单行时，这个节号会被 _section3_ok 拒掉（标题是空的），
    # 整节消失。二十页试点里第十一章节数报 0，就是这么来的。
    #
    # 「§11.5」这种带前缀的断头行也一样处理——下一行必须是像标题的短语。
    _BARE_SECTION_NO = re.compile(r"^\s*[§S]?\s*(\d{1,2}(?:\.\d{1,2}){0,2})\s*$")
    for p in pages:
        lines = p["lines"]
        for i, ln in enumerate(lines):
            m = _BARE_SECTION_NO.match(ln.strip())
            if not m:
                continue
            if i + 1 >= len(lines):
                continue
            nxt = lines[i + 1].strip()
            if not (2 <= len(nxt) <= 30):
                continue
            if re.search(r"[。；：,;:]|，", nxt):
                continue
            if re.match(r"^[\d§S]", nxt):
                continue
            if _NUM_UNIT.match(nxt) or _SECTION_JUNK.search(nxt):
                continue
            lines[i] = m.group(1) + " " + nxt
            lines[i + 1] = ""

        # 重建必须在页循环内部。原来这行缩进只有 4 格，落在两个 for 之外，
        # 于是全书只有最后一页的占位空行被清掉，其余页的 lines 里原地留着
        # 那些 ""——clean_page_lines 会滤掉它们，章节标题的拼接看着没事，
        # 但「章标题拼接」那一遍写进 lines[i] 的成果同样只生效于最后一页：
        # 实测《电气工程学概论》PDF387 的「第16章」+「控制系统问题」拼完后
        # clean 出来仍是两行，第十六章整章读不到。
        p["lines"] = [ln for ln in lines if ln.strip()]

    # ---- 目录页预判（必须在主循环之前） ----
    #
    # 为什么要提前：主循环按章号去重，目录页 PDF11–17 先被扫到，
    # 「第二章 烷烃和环烷烃」在那里被记成章号「二」，等到正文 PDF49
    # 的真章标题出现时，章号「二」已被占用，直接被跳过。
    # 后置环节再去剔目录页，就把这一章整条删掉——十九章剩三章就是这么来的。
    #
    # 判据用「一页上有多少个节标题」，这是版式无关的数量级差异：
    #   有机化学 目录 PDF11–17 每页 52–63 个，正文页最多 24 个
    #   物理化学 目录 1 页，正文每页最多 1–2 个
    # 目录页一页挤进几十条「3.3.3 烯烃顺反异构体的命名……76」，
    # 正文一节一页地讲，密度差一个数量级。
    #
    # 阈值定在 30，不是 8。踩过的坑：8 这个数会把正文页误判成目录——
    # 有机化学 PDF35 密度 17、PDF128 18、PDF249 14、PDF322 9、PDF427 24、
    # PDF611 9，这些页里有习题区（「15.1 设图……」「15.2 从图……」）
    # 或表格碎片，短行被当成节标题堆起来，于是整页的合法节被 toc 剔除，
    # 节数从 429 掉到 316。真目录页最低 52，正文最高 24，中间有一道
    # 30 的断层，落在断层上比落在任何具体数字上都安全。
    _toc_density = Counter()
    for p in pages:
        for t in clean_page_lines(p["lines"]):
            m = SECTION.match(t)
            if not (m and len(t) < 40):
                m = SECTION3.match(t) and _section3_ok(t)
            if m and len(t) < 40:
                _toc_density[p["pdf"]] += 1
    _toc_pages = {pg for pg, n in _toc_density.items() if n >= 30}

    # 篇首页（mini 目录）：一页上出现 ≥3 条章标题。
    #
    # 《电气工程学概论》每个「篇」开头一页「本篇内容由以下几章组成：」，
    # 下面把第16–21章列一遍。它没有页码也没有引导点，节密度判据抓不到，
    # 但章标题密度是 4 条，是正文页的十几倍。
    #
    # 这条必须与 _toc_density 一起在主循环之前算完：主循环按章号去重，
    # 385 先被扫到就把「16」占住了，后来 PDF387 的真章首页被当重复丢掉，
    # 报告里第16章整章消失，§16.x 的节也因 chapter_start 缺 16 全被判越界。
    _chap_density = Counter()
    for p in pages:
        if p["pdf"] in _toc_pages:
            continue
        for t in clean_page_lines(p["lines"]):
            if _CHAPTER_HEAD.match(t) and len(t) < 40 and chapter_key(_CHAPTER_HEAD.match(t).group("glued") or "") is not None:
                _chap_density[p["pdf"]] += 1
    _toc_pages |= {pg for pg, n in _chap_density.items() if n >= 3}

    # 统计反复出现的短行 → 页眉
    short_counter = Counter()
    for p in pages:
        for ln in p["lines"][:3] + p["lines"][-2:]:
            s = ln.strip()
            if 2 <= len(s) <= 24:
                short_counter[s] += 1
    head_threshold = max(3, len(pages) // 8)
    # 页眉集合里必须剔除「章节标题」：天津大学《有机化学》每页页眉印「第二章 烷烃和环烷烃」，
    # 它反复出现，会被当成页眉；但它是章标题，是全书结构的锚点，不能删。
    # 判据用 CHAPTER / SECTION 正则本身——凡能被章节正则认出的，一律不当页眉。
    #
    # 第三条例外是电气工程学概论的「第Ⅱ1章 模拟信号」：罗马数字 Ⅱ 被 OCR 认出来，
    # 而 CHAPTER 只认中文数字或阿拉伯数字，于是这个页眉躲过了上面的豁免，
    # 被当成页眉整行丢掉——第11章就从结构里消失了。凡是「第…章」形态的，
    # 哪怕章号认不出，也不当页眉。
    running_heads = {
        s for s, c in short_counter.items()
        if c >= head_threshold
        and not CHAPTER.match(s)
        and not SECTION.match(s)
        and not REEF.match(s)
    }

    chapters = []          # [{number, title, pdf, line_span}]
    sections = []          # [{id, title, pdf, chars}]
    formulas = []          # [{text, number, pdf, susp}]
    fragments = []         # [{text, pdf}] 结构式碎片，不进承重密度
    definitions = []       # [{term, pdf}]
    tables = []            # [{id, title, pdf, rows, numeric_lines, verdict, warnings}]
    figures = []           # [{id, title, pdf}]
    derivs = 0
    total_chars = 0
    body_lines = 0
    cur_table = None
    chapter_seen = set()   # 章节标题在每页页眉都会重印一次，必须只记第一次
    section_seen = set()

    for p in pages:
        body = clean_page_lines(p["lines"])
        pdf = p["pdf"]
        for s in body:
            # 章标题必须在页眉过滤之前认。天津大学《有机化学》每页页眉重印
            # 「第二章 烷烃和环烷烃」，它同时满足「短、反复出现」两条页眉特征，
            # 先过滤页眉就等于把章标题一起吞了——章数会报 0。
            m = _CHAPTER_HEAD.match(s)
            if m and len(s) < 40 and pdf not in _toc_pages:
                key = chapter_key(m.group("glued") or "")
                if key is None:
                    continue
                # 去重键必须用**章号**，不能用章名。
                #
                # 踩过的坑：原代码按章名去重，而目录页 PDF11 先出现「第二章 烷烃和环烷烃」
                # （目录条目），正文 PDF49 的「第二章烷烃和环烷烃」因 key 相同被直接跳过，
                # 于是 chapters 里只剩目录那条记录，随后被 toc_pages 整体剔除，
                # 十九章里有十六章就这样消失，最后只报出 3 章。
                # 页眉每页重印章标题，同号必然多页出现，所以按号收集、由后置环节定页。
                num = m.group(1)
                if num not in chapter_seen:
                    chapter_seen.add(num)
                    chapters.append({"number": num, "title": key, "pdf": pdf})
                else:
                    # 同号重复出现：页眉重印（正常，忽略），或者先命中了「篇首页」
                    # 那种 mini 目录（需要改用后来那条）。
                    #
                    # 《电气工程学概论》PDF385 是第四篇的篇首页，列着第16–21章，
                    # 于是「第16章 控制系统问题」先在那里被记下，真正的章首页
                    # PDF387 到这里被当重复丢掉——报告里第16章整章消失，
                    # 而 §16.x 的节因为 chapter_start 里没有 16 全被判越界。
                    #
                    # 判据：旧记录所在的页若被判为 toc 页，就以新记录（页码更靠后
                    # 且不在 toc 页）覆盖它。toc_pages 在此时已经算完（在主循环
                    # 之前预判过），可以直接查字典。
                    if _toc_pages and pdf not in _toc_pages:
                        for idx_c, old in enumerate(chapters):
                            if old["number"] == num and (old["pdf"] or 0) in _toc_pages:
                                chapters[idx_c] = {"number": num, "title": key, "pdf": pdf}
                                break
                # 这里绝不能碰 cur_table。每页页眉都会重印「第二章 烷烃和环烷烃」，
                # 于是每一页开头都会走到这一行；早先写 cur_table = None，
                # 结果表 2-4 在 PDF69 起头、PDF70 页眉一刷就被清空，
                # 连收尾机会都没有——报告里只有表 2-3/2-5/2-6，独缺 2-4。
                # 章标题与「表体是否结束」无关，表体由「明显不属于表的句子」收尾。
                continue

            if s in running_heads:
                continue
            total_chars += len(s)
            body_lines += 1

            # 表标题：必须带编号（表 8.4.1 / 表11.3.2），否则「中列出了……表8.5.1给出了」
# 这种正文引用句会被当成新表标题，把上一张表的表体统计整片清空。
            #
            # 粘连形态要先拆：原文「表 7.4.1 25℃时……」经 OCR 变成「表 7.4.125℃时……」，
            # 直接交给 TABLE_TITLE 会得到根本不存在的表号 7.4.12。
            # 先用 _GLUE_HEAD 取「纯数字串 + 温度尾巴」，交给 split_table_id 切分。
            glued = _GLUE_HEAD.match(s)
            m = TABLE_TITLE.match(s)
            if glued:
                tid, lost = split_table_id(glued.group(1))
                if lost:
                    # 「表 7.4.125℃时……」→ tid=7.4.1, lost='25'，
                    # 标题要把被吞掉的温度首位拼回来，才是原文的「25℃时……」。
                    rest = s[glued.start(2):]
                    ttl = lost + rest
                    if len(s) <= 70 and not _TABLE_TITLE_TAIL.match(ttl.strip()):
                        if cur_table is not None:
                            tables.append(finish_table(cur_table))
                        cur_table = {
                            "id": tid, "title": ttl, "pdf": pdf, "pages": [pdf],
                            "numeric_lines": 0, "name_lines": 0,
                            "num_tokens": 0, "name_tokens": 0,
                            "continued": False, "row_profile": [],
                        }
                        continue
            if m and len(s) <= 70 and not _TABLE_TITLE_TAIL.match(m.group(2).strip()):
                if cur_table is not None:
                    tables.append(finish_table(cur_table))
                cur_table = {
                    "id": m.group(1),
                    "title": m.group(2),
                    "pdf": pdf,
                    "pages": [pdf],
                    "numeric_lines": 0,
                    "name_lines": 0,
                    "num_tokens": 0,
                    "name_tokens": 0,
                    "continued": False,
                    "row_profile": [],
                }
                continue

            if TABLE_CONT.match(s):
                # 跨页续表：表 2-4 在 PDF69 起头，PDF70 印「续表」接着排。
                #
                # 早先的写法是 tables.append(finish_table(cur_table)) 之后重建一个字典，
                # 结果同一张表在报告里出现两次（同一 id、两个 PDF 页），
                # 读的人会以为是两张表。正确做法是把续页的行并进同一条记录，
                # 只记一个 pages 列表：「表 2-4 在 PDF69、PDF70」。
                if cur_table is not None:
                    cur_table["continued"] = True
                    cur_table["pages"] = cur_table.get("pages") or [cur_table["pdf"]]
                    cur_table["pages"].append(pdf)
                continue

            # 表内行统计：遇到明显不属于表的句子就收尾
            if cur_table is not None:
                # 逐行记录「这一行带几个物质名、几个数值」，供对齐检查用。
                if len(cur_table["row_profile"]) < 80:
                    cur_table["row_profile"].append((count_name_tokens(s), count_numbers(s)))
                if is_numeric_line(s):
                    # 单独成行的数值：既是「数值行」，也要计入数值个数。
                    # 曾经这里只 numeric_lines += 1 就 continue，
                    # 于是表 2-6 那种「一行一个数字」的排法（1.3 / 1.8 / 4.4 / 6.7）
                    # 一个数字都没进 num_tokens，整张完好的表被判成「整列被吞」。
                    cur_table["numeric_lines"] += 1
                    cur_table["num_tokens"] += max(1, count_numbers(s))
                    continue
                nv = count_numbers(s)
                nt = count_name_tokens(s)
                if nv >= 1:
                    # 一行里夹了好几个数：这就是表的一行数据（形态 B 或 A）
                    cur_table["num_tokens"] += nv
                    if nt >= 2:
                        cur_table["name_lines"] += 1
                        cur_table["name_tokens"] += nt
                        continue
                # 表名行/列名行：汉字或化学键词开头，短，无句号
                if nt >= 1 and len(s) <= 60 and "。" not in s and nv <= 2:
                    cur_table["name_lines"] += 1
                    cur_table["name_tokens"] += nt
                    continue
                if len(s) > 40 or "。" in s:
                    tables.append(finish_table(cur_table))
                    cur_table = None
                    # 不 continue：这句话本身也要参与后面的信号

            m = SECTION.match(s)
            if not (m and len(s) < 40):
                # 三级节号无前缀无空格（5.1.2二端网络谐振的特性分析）到这里再认一次。
                m = SECTION3.match(s) and _section3_ok(s)
            elif m:
                # SECTION 分支过去完全没有内容闸门，而它的标题段是 `\S.*?`
                # （连标点一起收），于是带逗号的习题题干整条被当成节标题。
                # 实测《电气工程学概论》20 页试点里
                # 「15.7 已知某 CMOS逻辑电路的信号频率为10MHz,求其最高扇出能力。」
                # 就是这么变成节的——它是习题第 7 题，不是节。
                #
                # 这里把 _section3_ok 的两条最硬的判据补上：带句读的不是标题、
                # 首字必须是汉字或字母。不整个调用 _section3_ok，因为 SECTION
                # 允许标题里有空格（「17.1 基本低压控制电器」），而 SECTION3
                # 的 \S{1,60} 会拒掉空格。
                _st = (m.group(2) or m.group(4) or "")
                if not _section_title_ok(_st):
                    m = None
                elif len(_st.strip()) > 40:
                    m = None
            if m and len(s) < 40:
                # 两组分支：带 §/S 前缀的命中 group1/2，裸节号的命中 group3/4；
                # SECTION3 命中的只有一个组，用 lastindex 区分。
                if m.lastindex == 2 and m.re is SECTION3:
                    sid, stitle = m.group(1), m.group(2)
                else:
                    sid = m.group(1) or m.group(3)
                    stitle = m.group(2) or m.group(4)
                # 页眉行常把页码粘在节名后面：「11.5 醛和酮的化学性质 383」。
                # 不剥的话，同一节在每一页都变成一条新记录
                # （实测 20 页摘录里 11.5 被记成 3 条：无页码、…383、…385），
                # 节数与节的篇幅全部算错。
                stitle = _strip_trailing_pageno(stitle or "").strip()
                head = int(re.match(r"\d+", sid).group())
                # 表格数值冒充节号：「0.25 nm」这种行。见 _NUM_UNIT 注释。
                #
                # 章号上限不能用「已扫到的章」算——章标题与节标题在扫描流里是
                # 交替出现的，第一节往往先于本章标题出现（书里章首页之后的第一个
                # 节号，扫描到它时 chapters 还是空的），此时 max_ch 可能是 1，
                # 于是 §11.1 被判成越界，整本章的节全被丢掉（实测下册节数从 58 → 0）。
                # 所以突查全表：先看整本书里出现过哪些章号。
                if _chapter_nums and (head < 1 or head > max(_chapter_nums) + 1):
                    continue
                if _NUM_UNIT.match(stitle or ""):
                    continue
                if _SECTION_JUNK.search(stitle or ""):
                    continue
                # 「17.1 基本」这种残节：页眉原本是「17.1 基本低压控制电」，
                # OCR 折成两行还把「控」丢了，于是屏幕上是
                #     「17.1 基本」 / 正文 / 「低压控制电」
                # 拼不回来（中间隔着整段正文），但它可以在已有同号节之后被认出
                # 是同一节的残片并丢弃。
                #
                # 判据必须钉死「残片」，不能用「谁是谁的前缀」——同号节在每页页眉
                # 都重印一遍，而标题是高度相似的短语（§2.4 与 §2.4.3 的标题都可能
                # 以「环」开头），松判据会把合法节整批吃掉：
                # 实测《有机化学》节数从 369 掉到 43（丢了 326 节），
                # 只剩「10.33 苯酚」这类表格数值冒充的假节。
                #
                # 真正的残片特征是**短得离谱**：正常节标题 6–20 字，
                # 折行残片只剩 2–4 字。所以要求新标题是旧标题的真前缀，
                # 且长度不到旧标题的一半（「基本」4 字 vs「基本低压控制电器」8 字，
                # 刚好一半以下；而「环烷烃」是「环烷烃的构象」的 30%——
                # 这条仍会误伤，所以再加一条：新标题长度必须 ≤4 字）。
                key = (sid, re.sub(r"\s+", "", stitle))
                if key not in section_seen:
                    prior = [s2 for s2 in sections if s2["id"] == sid]
                    if any(
                        key[1]
                        and len(key[1]) <= 4
                        and p2["title"] != key[1]
                        and p2["title"].startswith(key[1])
                        for p2 in prior
                    ):
                        continue
                    section_seen.add(key)
                    sections.append({"id": key[0], "title": key[1], "pdf": pdf})
                continue

            m = FIGURE.match(s)
            if m:
                # 图题 vs 正文引用：「图15-5-6所示的曲线描绘了…」是引用，
                # 「图16-1-1 逻辑控制系统控制模型」是图题。前者如果收进来，
                # 图数会被虚抬，标题也变成残句。判据见 _FIG_REF_TAIL。
                fig_title = m.group(2).strip()
                if not _FIG_REF_TAIL.match(fig_title):
                    figures.append({"id": m.group(1), "title": fig_title, "pdf": pdf})
                continue

            if EXERCISE.match(s):
                continue

            m = FORMULA_NUMBERED.match(s)
            if m:
                # 带编号的不一定是公式：'22.一反应进行一次，其反应机理中某一
                # 基元反应aA十bB十一→xX十yY十进行两' 也会被 FORMULA_NUMBERED
                # 认成「正文 + (22)」这种编号式，因为它只要求行尾有个括号编号。
                # 先前这条分支直接 append，绕过了 formula_is_fragment，
                # 于是题干混进公式表。这里补一次叙述检查。
                body = m.group(1).strip()
                if not formula_is_fragment(body):
                    formulas.append(
                        {"text": body, "number": m.group(2), "pdf": pdf}
                    )
                continue
            if FORMULA.search(s) and len(s) <= 60 and "。" not in s:
                # 分两档记：完整方程 vs 结构式碎片。
                #
                # 《有机化学》整本书的「公式」几乎全是结构式，OCR 会把一个分子
                # 拆成十来行（CH2=CH—C—CH3 被切成 'CH2=C' / 'H=C—CH3' / '=0'），
                # 逐行收进来就得到 67 条「公式」，其中 26 条连一个完整基团都不是。
                #
                # 这是扫描件的物理限制，文本层救不回来。但把两者混在一起计数会
                # 直接污染「承重密度」——结构式碎片最多的页不是推导最重的页，
                # 而是插图画得最多的页。
                #
                # 判据不能只看「有没有箭头」：物理化学的公式是纯等式
                # （ΔG = ΔH − TΔS），一个箭头都没有，带箭头就收会把它们全判成碎片
                # （实测下册公式从 1301 掉到 35）。所以真正的分辨标准是
                # **这行里有没有非化学式的成分**：希腊字母、中文、数学函数、
                # 对数/积分号——有就是真公式，只剩元素符号与数字就是结构式碎片。
                is_equation = not formula_is_fragment(s)
                if is_equation:
                    formulas.append({"text": s, "number": None, "pdf": pdf})
                else:
                    fragments.append({"text": s, "pdf": pdf})
                continue

            if DEFINE.search(s) and len(s) <= 80:
                definitions.append({"text": s, "pdf": pdf})
                continue

            if DERIVATION.search(s) and len(s) <= 80:
                derivs += 1

    if cur_table is not None:
        tables.append(finish_table(cur_table))

    # ---- 目录页回收 ----
    #
    # 目录页里每条章标题都长得像真章标题（剥掉引导点线之后一模一样），
    # 于是《有机化学》624 页那次出现两个「第八章 有机化合物的波谱」：
    # 一条起点在 PDF11–17（目录），一条起点在 PDF294（正文）。
    # 章数虚报，而且前十几章的起始页全挤在 PDF11–17，
    # 「这章多厚」「这章在哪」这类判断全部失效。
    #
    # 判据用「一页上有多少个节标题」——这是版式无关的数量级差异：
    #   有机化学：目录页 PDF11–17 每页 42–63 个节标题，正文页最多 4 个
    #   物理化学：目录 1 页，正文每页最多 1–2 个
    # 目录页是一页挤进几十条「3.3.3 烯烃顺反异构体的命名……76」，
    # 正文页一节一页地讲，永远不会重叠到这个密度。
    # 所以：节标题数 ≥ 8 的页码判为目录页，其上的章与节一律不算结构。
    #
    # 为什么不按「同一页 ≥3 条章标题」判：目录页 OCR 后章标题常与页码粘连、
    # 或被拆成两行（「第七章 卤代烃… / .225」），一页只认得出 1–2 条，
    # 反而是节标题一条不落地全认出来。
    sections_per_page = Counter(s["pdf"] for s in sections)
    # 阈值与主循环之前那道 _toc_density 保持一致（30），理由见那里的注释：
    # 8 会把正文页误判成目录页。《有机化学》PDF128/427/611 的节密度分别是
    # 18/24/9，用 8 就整页剔掉，节数从 429 掉到 316；真目录页最低 52。
    toc_pages = {p for p, n in sections_per_page.items() if n >= 30}
    per_page = Counter(c["pdf"] for c in chapters)
    # 「同一页出现 ≥3 条章标题」= 篇首页 / mini 目录。
    #
    # 《电气工程学概论》每个「篇」开头有一页「本篇内容由以下几章组成：」，
    # 下面把第 16–21 章列一遍。这一页章标题密度 4 条，但它不是目录格式
    # （没有页码、没有引导点），toc_density 那条节密度判据抓不到它，
    # 只能靠章标题密度这一条兜住。
    toc_pages |= {p for p, n in per_page.items() if n >= 3}

    dropped_toc = 0
    kept_chapters = []
    for c in chapters:
        if c["pdf"] in toc_pages:
            dropped_toc += 1
            continue
        kept_chapters.append(c)

    by_number = {}
    for c in kept_chapters:
        by_number.setdefault(c["number"], []).append(c)

    # 同一章号保留**页码最小**的那条，不是最大的那条。
    #
    # 这里踩过一次大坑：原先写成 max(pdf)，理由是「正文永远在目录之后」。
    # 但每页页眉都会重印「第二章 烷烃和环烷烃」，于是同一章号在全书 40+ 页
    # 各出现一次，max 拿到的是**最后一页的页眉**，不是章首页。
    # 更糟的是目录页 PDF11–17 已被 toc_pages 剔除，剩下的最早一条
    # 就是真正的章首页 PDF49 —— 所以正确方向是 min。
    kept = []
    for number, items in by_number.items():
        kept.append(min(items, key=lambda c: c["pdf"] or 0))
    chapters = sorted(kept, key=lambda c: c["pdf"] or 0)

    # 目录页的节也必须筛掉，否则 771 节里 360 节是目录条目。
    # 顺带把「章起始页之前的节」也算掉——同一条节号在目录与正文各出现一次，
    # section_seen 只留第一次扫到的那条，而第一次扫到的正是目录页。
    chapter_start = {}
    for c in chapters:
        chapter_start[_cn_to_int(c["number"])] = c["pdf"] or 0

    def _sec_ok(s):
        if s["pdf"] in toc_pages:
            return False
        m = re.match(r"\d+", s["id"] or "")
        if not m:
            return True
        # 只用「节号所属的章在本书里出现过」这一条。
        #
        # 早先还要求「节的页码不早于章首页」，那是错的：
        # 扫描一段 20 页摘录时，章首页可能落在切片之后（试点 PDF395–414 里
        # 第十一章首页在 PDF410，前面 PDF395–409 已经在讲 11.5 了），
        # 这条判据会把整片合法的节全删掉，节数报 0。
        # 目录页已经由 toc_pages 单独剔除，这里不必再管页码先后。
        return int(m.group()) in chapter_start

    sections = [s for s in sections if _sec_ok(s)]

    # 公式去重（同一公式可能在正文与小结各出现一次）
    seen = set()
    uniq_formulas = []
    for f in formulas:
        key = (f["text"][:40], f["number"])
        if key in seen:
            continue
        seen.add(key)
        uniq_formulas.append(f)

    return {
        "pages": len(pages),
        "has_page_marks": has_marks,
        "chars": total_chars,
        "body_lines": body_lines,
        "running_head_threshold": head_threshold,
        "running_heads": sorted(running_heads)[:40],
        "chapters": chapters,
        "sections": sections,
        "formulas": uniq_formulas,
        "formula_fragments": len(fragments),
        "definitions": definitions,
        "tables": tables,
        "figures": figures,
        "derivation_lines": derivs,
        "toc_dropped": dropped_toc,
        "warnings": build_warnings(has_marks, len(chapters), len(tables)),
    }


def finish_table(t: dict) -> dict:
    """给一张表做「位置索引」，而不是「健康判决」。

    ============ 为什么放弃判决 ============

    这个函数最初想做的是判断「这张表在 OCR 文本里有没有被吃掉」。20 页试点证明做不到。

    证据（有机化学 PDF60–79，四张表逐字核对原文后）：

      表 2-3 环烷烃摩尔燃烧热   完整，10 行×3 列 ≈ 30 个数    判定 ok ✓（靠比例及格）
      表 2-4 物理常数           跨页续表，≈20 行×4 列          被拆成两条 ✗
      表 2-5 甲烷卤化反应热     完整，3 行×4 列 = 12 个数      判定 ok ✓（但数出 19 个）
      表 2-6 相对反应速率       Cl 行只剩 1 个数，应 12 实得 9  判定 ok ✗（11 个里 2 个是表头凑的）

    三张里错两张。试过的判据及为何不成立：

      1. 总比例（数值个数 / 列名个数）——间接指标。表 2-6 靠表头「CH2」里的数字凑到 11，
         刚好过关；表 2-5 把表头的 2、1 也算进去，数出 19 个而实际只有 12 个。
      2. 每行数值个数应相等（对齐检查）——OCR 打散后行结构每张表形态都不同：
           表2-3  [(1,1),(0,0),(1,1),(0,0)×8,(0,1)...]
           表2-5  [(0,1),(0,0),(1,0),(2,0),(2,0),(1,0)...]
           表2-6  [(1,1),(1,0),(0,0),(1,1),(1,0),(0,1)×4...]
         物质名行与数据行的分离方式毫无规律，这条判据不成立。

    根本原因不是实现 bug，是方法的天花板：扫描件里「哪个数属于哪一列」这个信息，
    在 OCR 变成纯文本那一刻就已经丢失了。事后用文本特征反推，只能靠猜，而猜错的代价
    是——我会以为一张烂表是好的，拿它当依据写心得。

    ============ 所以现在只输出什么 ============

    只回答一个问题：**这一页有表吗，表号和标题是什么。**
    然后由读的人翻回那一页的扫描图，用眼睛看行列。这比任何文本启发式都准，
    而且对我成本极低（一张 2544×1804 的图，看清行列没有问题）。

    仍然保留一个粗糙的「看起来空」标记：如果表标题之后一个数字都没有，
    那大概率是标题与表体被分到了两页（跨页表），提示翻页时注意。
    """
    num_tokens = t.get("num_tokens", 0)       # 数值行里认出来的数字个数
    nums = t["numeric_lines"]
    names = t["name_lines"]

    notes = []
    if num_tokens == 0 and nums == 0 and names == 0:
        status = "表体不在本页"
        notes.append("表标题之后没有任何内容：表体可能在下一页，或整张表未被 OCR 识别")
    elif num_tokens <= 2:
        status = "表体极少"
        notes.append("表标题之后仅 %d 个数值：多为跨页续表，须核对该页与下一页原图"
                     % num_tokens)
    else:
        status = "有表体"

    return {
        "id": t["id"],
        "title": t["title"][:60],
        "pdf": t["pdf"],
        "pages": t.get("pages") or ([t["pdf"]] if t["pdf"] else []),
        "name_lines": names,
        "numeric_lines": nums,
        "name_tokens": t.get("name_tokens", 0),
        "num_tokens": num_tokens,
        "continued": t.get("continued", False),
        "status": status,
        "notes": notes,
    }


def build_warnings(has_marks, n_ch, n_tb):
    w = []
    if not has_marks:
        w.append("整份文本没有页标记：无法定位回原 PDF 页，核对成本高")
    if n_ch == 0:
        w.append("没找到任何「第N章」标题：可能是摘录，或 OCR 把章标题拆碎了")
    if n_tb == 0:
        w.append("没找到任何「表 N.N.N」标题：若这本书本身有数据表，说明表标题行识别失败")
    return w


# ---------------------------------------------------------------- 输出


def render(report: dict, limit_formulas: int = 40, only_section: str | None = None):
    out = []
    out.append("扫描结果")
    out.append("=" * 60)
    out.append(
        "页数 %d / 正文字符 %d / 正文行 %d"
        % (report["pages"], report["chars"], report["body_lines"])
    )
    if report["warnings"]:
        out.append("")
        out.append("!! 注意")
        for w in report["warnings"]:
            out.append("   - " + w)

    out.append("")
    out.append("结构")
    out.append("-" * 60)
    out.append("章 %d 个，节 %d 个" % (len(report["chapters"]), len(report["sections"])))
    for c in report["chapters"]:
        pdf = "PDF%s" % c["pdf"] if c["pdf"] else "?"
        out.append("  第%s章 %s   (%s)" % (c["number"], c["title"][:30], pdf))

    if only_section:
        out.append("")
        out.append("指定节：%s" % only_section)

    out.append("")
    out.append("承重墙（要用但不能全信的）")
    out.append("-" * 60)
    out.append("公式 %d 条，定义句 %d 条，推导句 %d 条，图 %d 张" % (
        len(report["formulas"]), len(report["definitions"]),
        report["derivation_lines"], len(report["figures"])))
    if report.get("formula_fragments"):
        out.append("（另有结构式碎片 %d 条，未计入——扫描件把分子拆成多行，"
                   "碎片救不回完整方程，只当噪声统计）" % report["formula_fragments"])

    # 表格只做「位置索引」，不做健康判决。
    # 判决做不到的原因写在 finish_table 的 docstring 里：扫描件里「哪个数属于哪一列」
    # 这个信息在 OCR 成文本时就丢了，事后反推只能靠猜，而猜错会让我拿一张烂表当依据。
    # 所以这里只回答「哪一页有表、表号标题是什么」，行列由人翻原图看。
    tables = report["tables"]
    out.append("")
    out.append("数据表 %d 张（只标位置，行列须回原图看）" % len(tables))
    if tables:
        out.append("-" * 60)
        for t in tables:
            flag = "" if t["status"] == "有表体" else "  ← " + t["status"]
            where = "、".join("PDF%d" % p for p in t["pages"]) if t["pages"] else "?"
            if t.get("continued"):
                where += "（跨页续表）"
            out.append("  表 %-7s %-18s %s%s" % (
                t["id"], where, t["title"][:32], flag))
            for n in t["notes"]:
                out.append("      %s" % n)
        pages = sorted({p for t in tables for p in t["pages"]})
        if pages:
            out.append("")
            out.append("  须翻原图的页：%s" % "、".join("PDF%d" % p for p in pages))

    if limit_formulas > 0 and report["formulas"]:
        out.append("")
        out.append("公式（前 %d 条，完整看 --json）" % limit_formulas)
        out.append("-" * 60)
        for f in report["formulas"][:limit_formulas]:
            num = "(%s)" % f["number"] if f["number"] else "     "
            out.append("  %-12s PDF%-4s %s" % (num, f["pdf"] or "?", f["text"][:56]))
        if len(report["formulas"]) > limit_formulas:
            out.append("  ... 还有 %d 条" % (len(report["formulas"]) - limit_formulas))
    elif limit_formulas > 0 and not report["formulas"]:
        # 空标题比没有标题更误导——先前这里会打印「公式（前 40 条）」然后一片空白，
        # 看起来像渲染出错，其实是这本这一段真没有可辨认的完整公式。
        out.append("")
        out.append("公式：这一段没有可辨认的完整公式%s" % (
            "（%d 条结构式碎片全在插图里，要看请翻原图）"
            % report["formula_fragments"] if report.get("formula_fragments") else ""))
    return "\n".join(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="scan",
        description="从 OCR 文本提取骨架并标出失真位置（deep-reading wl）",
    )
    ap.add_argument("txt", help="OCR 产出的 txt 路径")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--out", default=None, help="把结果写入文件")
    ap.add_argument("--section", default=None, help="只看某一节（仅影响文本输出）")
    ap.add_argument("--limit-formulas", type=int, default=40, help="文本输出里列多少条公式")
    args = ap.parse_args(argv)

    if not os.path.isfile(args.txt):
        print("找不到文件：%s" % args.txt, file=sys.stderr)
        return 2

    with open(args.txt, encoding="utf-8", errors="replace") as fh:
        text = fh.read()

    report = scan(text)
    body = json.dumps(report, ensure_ascii=False, indent=2) if args.json else render(
        report, args.limit_formulas, args.section
    )

    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(body)
        print("写入 %s" % args.out, file=sys.stderr)
    else:
        print(body)
    return 0


if __name__ == "__main__":
    sys.exit(main())
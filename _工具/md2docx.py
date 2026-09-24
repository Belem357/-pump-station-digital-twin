# -*- coding: utf-8 -*-
"""把项目里的 markdown 交付稿转成 Word（.docx）。

用法：
    python md2docx.py <输入.md> <输出.docx>

支持：标题(#/##/###)、无序列表(-)、有序列表(1.)、引用(>)、
      管道表格(| a | b |)、粗体(**文字**)、水平线(---)。
字号与字体对齐现有四份 PM 文档的中文阅读习惯。
"""
import re
import sys

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.shared import Pt, RGBColor

CN_FONT = '微软雅黑'


def setCnFont(styleOrRun, name=CN_FONT):
    """把字体同时设到 ascii / eastAsia，否则中文不生效。"""
    styleOrRun.font.name = name
    rpr = styleOrRun.element.get_or_add_rPr() if hasattr(styleOrRun, 'element') else None
    if rpr is None:
        rpr = styleOrRun._element.get_or_add_rPr()
    rfonts = rpr.find(qn('w:rFonts'))
    if rfonts is None:
        rfonts = rpr.makeelement(qn('w:rFonts'), {})
        rpr.append(rfonts)
    rfonts.set(qn('w:eastAsia'), name)
    rfonts.set(qn('w:ascii'), name)
    rfonts.set(qn('w:hAnsi'), name)


def addInline(para, text):
    """处理 **粗体**，其余按普通文本写入。"""
    parts = re.split(r'(\*\*.+?\*\*)', text)
    for part in parts:
        if not part:
            continue
        if part.startswith('**') and part.endswith('**') and len(part) > 4:
            run = para.add_run(part[2:-2])
            run.bold = True
        else:
            para.add_run(part)


def splitRow(line):
    """把 | a | b | 拆成 ['a','b']。"""
    cells = line.strip().strip('|').split('|')
    return [c.strip() for c in cells]


def isSepRow(line):
    """判断是不是表格的分隔行 |---|---|。"""
    return bool(re.fullmatch(r'\|[\s:\-|]+\|', line.strip()))


def convert(mdPath, docxPath):
    with open(mdPath, 'r', encoding='utf-8') as f:
        lines = f.read().splitlines()

    doc = Document()
    normal = doc.styles['Normal']
    normal.font.size = Pt(10.5)
    setCnFont(normal)

    for lv in (1, 2, 3):
        st = doc.styles['Heading %d' % lv]
        setCnFont(st)
        st.font.color.rgb = RGBColor(0x1F, 0x38, 0x64)

    i = 0
    n = len(lines)
    inFence = False
    while i < n:
        raw = lines[i]
        line = raw.rstrip()

        # 代码块围栏
        if line.strip().startswith('```'):
            inFence = not inFence
            i += 1
            continue
        if inFence:
            p = doc.add_paragraph()
            p.paragraph_format.left_indent = Pt(14)
            run = p.add_run(line)
            run.font.name = 'Consolas'
            run.font.size = Pt(9)
            i += 1
            continue

        # 空行
        if not line.strip():
            i += 1
            continue

        # 水平线
        if re.fullmatch(r'-{3,}', line.strip()):
            i += 1
            continue

        # 表格：连续以 | 开头的行
        if line.lstrip().startswith('|'):
            block = []
            while i < n and lines[i].lstrip().startswith('|'):
                block.append(lines[i])
                i += 1
            block = [b for b in block if not isSepRow(b)]
            if not block:
                continue
            rows = [splitRow(b) for b in block]
            cols = max(len(r) for r in rows)
            table = doc.add_table(rows=0, cols=cols)
            table.style = 'Table Grid'
            for ri, r in enumerate(rows):
                cells = table.add_row().cells
                for ci in range(cols):
                    txt = r[ci] if ci < len(r) else ''
                    p = cells[ci].paragraphs[0]
                    addInline(p, txt)
                    for run in p.runs:
                        run.font.size = Pt(9)
                        if ri == 0:
                            run.bold = True
            doc.add_paragraph()
            continue

        # 标题
        m = re.match(r'^(#{1,3})\s+(.*)$', line)
        if m:
            lv = len(m.group(1))
            p = doc.add_heading(level=lv)
            addInline(p, m.group(2))
            i += 1
            continue

        # 引用
        if line.lstrip().startswith('>'):
            txt = line.lstrip()[1:].strip()
            p = doc.add_paragraph()
            p.paragraph_format.left_indent = Pt(18)
            addInline(p, txt)
            for run in p.runs:
                run.italic = True
                run.font.color.rgb = RGBColor(0x44, 0x55, 0x66)
            i += 1
            continue

        # 无序列表
        m = re.match(r'^\s*[-*]\s+(.*)$', line)
        if m:
            p = doc.add_paragraph(style='List Bullet')
            addInline(p, m.group(1))
            i += 1
            continue

        # 有序列表
        m = re.match(r'^\s*\d+[.)]\s+(.*)$', line)
        if m:
            p = doc.add_paragraph(style='List Number')
            addInline(p, m.group(1))
            i += 1
            continue

        # 普通段落
        p = doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.LEFT
        addInline(p, line.strip())
        i += 1

    doc.save(docxPath)
    return docxPath


if __name__ == '__main__':
    src, dst = sys.argv[1], sys.argv[2]
    convert(src, dst)
    print('已生成:', dst)

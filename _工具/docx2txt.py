# -*- coding: utf-8 -*-
"""把 .docx 正文抽成纯文本（按段落换行，表格单元格用 | 分隔）。

用途：共享文档里 01 / 03 / 04 / 08 只有 .docx 没有 .md 源稿，
要看内容、要核对字段名，先抽成文本再 grep，比在 Word 里翻快得多。
md2docx.py 是反方向（.md 源稿 → .docx 成品），两个配对使用。

用法：
    python _工具/docx2txt.py "共享文档/03.接口规范与数据字典_v1.1.1.docx" out.txt

注意：输出一律写 UTF-8 文件，不往控制台打印正文——
Windows 控制台默认 GBK，print 中文和 ⚠ 这类符号会直接抛 UnicodeEncodeError。
"""

import sys
import zipfile
import re
import html


def extract(path):
    """把 .docx 的正文抽成纯文本。

    Args:
        path: .docx 文件路径。

    Returns:
        str: 按段落换行的纯文本；表格单元格之间用 " | " 分隔。
    """
    with zipfile.ZipFile(path) as archive:
        xml = archive.read('word/document.xml').decode('utf-8')

    # 先给段落/单元格边界打标记，再统一收敛空白
    xml = xml.replace('</w:tc>', '\t')
    xml = xml.replace('</w:tr>', '\n')
    xml = re.sub(r'</w:p>', '\n', xml)
    xml = re.sub(r'<w:tab[^>]*/>', '\t', xml)
    xml = re.sub(r'<w:br[^>]*/>', '\n', xml)

    # 只保留 w:t 里的文字，其余标签全部丢弃
    parts = []
    for match in re.finditer(r'<w:t[^>]*>(.*?)</w:t>|(\n)|(\t)', xml, re.S):
        if match.group(1) is not None:
            parts.append(match.group(1))
        elif match.group(2) is not None:
            parts.append('\n')
        else:
            parts.append('\t')
    text = html.unescape(''.join(parts))

    lines = []
    for line in text.split('\n'):
        line = re.sub(r'[ \t]*\t[ \t]*', ' | ', line).strip(' |').strip()
        line = re.sub(r'[ ]{2,}', ' ', line)
        if line:
            lines.append(line)
    return '\n'.join(lines)


if __name__ == '__main__':
    # 控制台只报状态，正文写文件；顺便把 stdout 切成 utf-8 兜底
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

    if len(sys.argv) < 3:
        print('用法: python _工具/docx2txt.py <输入.docx> <输出.txt>')
        sys.exit(1)

    with open(sys.argv[2], 'w', encoding='utf-8') as outputFile:
        outputFile.write(extract(sys.argv[1]))
    print('OK -> ' + sys.argv[2])

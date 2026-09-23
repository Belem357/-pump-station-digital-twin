import zipfile, re, os

d = r'E:\桌面\泵房'
files = [
    '泵房数字孪生_第一周工作排班表_v1.2.docx',
    '01.需求规格说明书PRD_v1.1.docx',
    '02.核心交互流程图_v1.1.docx',
    '03.接口规范与数据字典_v1.1.docx',
    '04.项目验收清单Checklist_v1.1.docx',
]

# 旧写法（v1.0 的冲突命名），新文档中只应出现在"修订说明/变更清单"里
bad_patterns = [
    r'"device"\s*:',
    r'"action"\s*:',
    r'\bdevice\s*:\s*"pump',
    r'\baction\s*:\s*"(start|stop|set_)',
    r'tank_level_cm',
]

# 新标准写法，应当出现在相应文档里
good_patterns = ['device_id', 'command', 'tank_level', 'pump_02_temp', 'today_kwh']

for fn in files:
    p = os.path.join(d, fn)
    if not os.path.exists(p):
        print('MISSING:', fn)
        continue
    z = zipfile.ZipFile(p)
    names = z.namelist()
    if 'word/document.xml' not in names:
        print('BROKEN(no document.xml):', fn)
        z.close()
        continue
    xml = z.read('word/document.xml').decode('utf-8')
    txt = re.sub(r'<[^>]+>', '', xml)
    tbl = xml.count('<w:tbl>')

    print('=' * 78)
    print('%s' % fn)
    print('  ZIP条目=%d  表格=%d  正文字符=%d' % (len(names), tbl, len(txt)))

    hits = []
    for pat in bad_patterns:
        for m in re.finditer(pat, txt):
            s = max(0, m.start() - 45)
            e = min(len(txt), m.end() + 45)
            hits.append((pat, txt[s:e].replace('\n', ' ')))
    if not hits:
        print('  旧命名残留: 无 (CLEAN)')
    else:
        print('  旧命名出现 %d 处，逐条看上下文:' % len(hits))
        for pat, ctx in hits:
            print('    [%s] ...%s...' % (pat, ctx))

    present = [g for g in good_patterns if g in txt]
    print('  新标准字段命中: %s' % (', '.join(present) if present else '无'))
    z.close()

print('=' * 78)
print('VERIFY_DONE')

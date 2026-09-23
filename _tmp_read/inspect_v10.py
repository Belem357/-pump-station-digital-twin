import zipfile, re, os, hashlib

p = r'E:\桌面\泵房\泵房数字孪生_第一周工作排班表_v1.0.docx'
print('存在:', os.path.exists(p))
print('大小:', os.path.getsize(p), 'bytes')

z = zipfile.ZipFile(p)
names = z.namelist()
print('ZIP条目数:', len(names))
xml = z.read('word/document.xml').decode('utf-8')
txt = re.sub(r'<[^>]+>', '', xml)
z.close()

print('正文字符数:', len(txt))
print('表格数:', xml.count('<w:tbl>'))
print('---- 版本判定标记 ----')
for kw in ['v1.0', 'v1.1', 'v1.2', '周日', '周三上午', '准备与学习', '12 周怎么走',
           '打通一条最小的数据通路', '产品经理（你）', '产品经理']:
    print('  %-24s %s' % (kw, 'YES' if kw in txt else 'no'))

print('---- 修订元数据 ----')
z = zipfile.ZipFile(p)
if 'docProps/core.xml' in z.namelist():
    core = z.read('docProps/core.xml').decode('utf-8', 'replace')
    for tag in ['creator', 'lastModifiedBy', 'created', 'modified', 'revision']:
        m = re.search(r'<[^>]*%s[^>]*>([^<]*)<' % tag, core)
        print('  %-16s %s' % (tag, m.group(1) if m else '(无)'))
z.close()

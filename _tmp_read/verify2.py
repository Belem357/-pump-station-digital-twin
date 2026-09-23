import zipfile, re, os

d = r'E:\桌面\泵房'

# 每份文档必须包含的关键内容（证明本次修订的核心内容没在转换中丢失）
checks = {
    '01.需求规格说明书PRD_v1.1.docx': [
        '140cm',            # 补定的液位过高阈值
        '历史数据曲线',      # 补入的 F8
        '低液位联锁停泵',    # 补入的 F10
        '12 周里程碑',       # 新增第6节
        '本项目明确不做',    # 新增第4节边界
        'AI 工程师',         # 关于不设AI岗的说明
        '每周三上午',        # 演示节奏
    ],
    '02.核心交互流程图_v1.1.docx': [
        'device_id',
        'command',
        'tank_level',
        '后端判定',          # 报警判定方修订
        '异常与边界场景',    # 新增第五节
        '30 秒',             # 防抖
        '1006',              # 模式冲突错误码
    ],
    '03.接口规范与数据字典_v1.1.docx': [
        'Modbus 地址映射表',
        '40001',             # 具体地址
        'pump_01_temp',      # 补齐的温度标签
        'pump_02_status',    # 补齐的2#泵
        'today_kwh',         # 补齐的耗电量
        '/api/v1/history',
        '/api/v1/alarms',
        '/api/v1/health',
        '错误码表',
        '心跳',
        'valve_inlet_opening',
    ],
    '04.项目验收清单Checklist_v1.1.docx': [
        '环境与共识就绪',    # 新增第0节
        '今日耗电量',
        '后端判定，前端只显示',
        '互斥逻辑双保险',
        '文档与交付',        # 新增第6节
        '83',                # 合计项数
        '1008',
    ],
    '泵房数字孪生_第一周工作排班表_v1.2.docx': [
        '准备与学习',        # 第一周定调
        '12 周怎么走',       # 全局总览
        '互相讲一遍',        # 周六关键活动
        '周三上午',          # 演示时间
        '不要熬夜赶工',
        'AI 工程师',
        '统一字段命名表',
        'ModRSsim2',
    ],
}

allok = True
for fn, kws in checks.items():
    p = os.path.join(d, fn)
    z = zipfile.ZipFile(p)
    xml = z.read('word/document.xml').decode('utf-8')
    txt = re.sub(r'<[^>]+>', '', xml)
    z.close()
    miss = [k for k in kws if k not in txt]
    status = 'ALL_PRESENT' if not miss else 'MISSING: ' + str(miss)
    if miss:
        allok = False
    print('%-46s %s' % (fn[:46], status))

print('=' * 70)
print('RESULT: ' + ('全部关键内容均已写入' if allok else '存在缺失，需检查'))

# -*- coding: utf-8 -*-
"""为六个角色各打一个 zip，放到 _send/ 下，直接发微信/QQ 即可。

用法（在仓库根执行）：
    python _工具/打包发组员.py

产出：
    _send/泵房-①-传感器与设备模拟.zip
    _send/泵房-②-协议解析.zip
    _send/泵房-③-后端.zip
    _send/泵房-④-三维建模.zip
    _send/泵房-⑤-前端Three.js.zip
    _send/泵房-⑥-产品经理.zip

每个压缩包里都有一份 `先看我.txt`，写清楚这个角色该从哪份文档开始看。

⚠️ 但请注意：**发 zip 的组员没法提交代码**（他们没有 .git）。
   能让他们用 git 就用 git 更省事：
       GitHub 仓库 → Settings → Collaborators → 加他们的账号
       然后他们 `git clone` 即可，任务书就在仓库里，改完直接 push。
   zip 只给实在搞不定 git 的人用。
"""
import os
import sys
import zipfile

# 压缩包统一放在这里（.gitignore 已忽略）
OUT_DIR = "_send"

# 这些目录/文件不打包：机器相关、体积大、或本来就是本地产物
EXCLUDE_DIRS = {".venv", "venv", "__pycache__", ".git", ".idea", "node_modules"}
EXCLUDE_FILES = {"data.db", "data.db-shm", "data.db-wal", ".DS_Store"}

# ---------------------------------------------------------------- 公共内容
# 每个角色都拿到的：总排期、三压方案、示意图、README、入门说明
COMMON = [
    "README.md",
    "共享文档/14.第三轮7周总排期与里程碑_v1.1.docx",
    "共享文档/12.三压分区改造方案_v1.2.docx",
    "共享文档/09.泵房设备与数据链路示意图.html",
]

# ---------------------------------------------------------------- 六个角色
# taskBook: 该角色的任务书（md 是源稿，docx 是成品，都给）
# extra:    该角色额外的文件或目录
ROLES = {
    "①-传感器与设备模拟": {
        "taskBook": "角色材料/任务书/①-传感器与设备模拟_7周任务书_v1.0",
        "extra": [
            "共享文档/03.接口规范与数据字典_v1.1.1.docx",
            "共享文档/10.传感器选型说明_v1.2.docx",
            "共享文档/06.开发环境安装手册_v1.1.docx",
            "角色材料/传感器与协议解析/05.Modbus建点表与IO点表_v1.0.docx",
            "角色材料/传感器与协议解析/产品说明书",
            "代码/backend",
        ],
        "start": "先看「①-传感器与设备模拟_7周任务书」的第二节「每周任务」，"
                 "W1 的第一件事是修 `sensorSim.py:313` 那个 bug。"
                 "照《06 开发环境安装手册》把环境装起来（pymodbus 必须 3.6.9）。",
    },
    "②-协议解析": {
        "taskBook": "角色材料/任务书/②-协议解析_7周任务书_v1.0",
        "extra": [
            "共享文档/03.接口规范与数据字典_v1.1.1.docx",
            "共享文档/10.传感器选型说明_v1.2.docx",
            "角色材料/传感器与协议解析/05.Modbus建点表与IO点表_v1.0.docx",
            "角色材料/传感器与协议解析/产品说明书",
            "代码/backend",
        ],
        "start": "先看「②-协议解析_7周任务书」，重点看《03 接口规范》第三、四节和"
                 "《05 建点表》里的四个新手坑（地址 0-based 换算最容易翻车）。",
    },
    "③-后端": {
        "taskBook": "角色材料/任务书/③-后端_7周任务书_v1.0",
        "extra": [
            "共享文档/03.接口规范与数据字典_v1.1.1.docx",
            "共享文档/13.泵房设备清单与数量台账_v1.0.docx",
            "共享文档/11.第二轮功能补全任务分派与验收说明_v1.1.docx",
            "代码/backend",
            "代码/modbus/backendGuide.md",
            "代码/modbus/exampleApi.py",
        ],
        "start": "先看「③-后端_7周任务书」。跑通系统最快的方式是双击 `代码/backend/一键启动.bat`。"
                 "改 `devices.json` 时记得同步改 `main.py` 里 `getDevices()` 的 docstring。",
    },
    "④-三维建模": {
        "taskBook": "角色材料/任务书/④-三维建模_7周任务书_v1.0",
        "extra": [
            "共享文档/03.接口规范与数据字典_v1.1.1.docx",
            "共享文档/13.泵房设备清单与数量台账_v1.0.docx",
            "泵房.glb",
            "代码/frontend/sceneConfig.json",
            "模型升级路线图.md",
            "_工具/fixGlbNodeNames.mjs",
            "_工具/validateGlb.mjs",
            "_工具/validateScene.mjs",
        ],
        "start": "先看「④-三维建模_7周任务书」的第二节「现状」。"
                 "**每次导出 泵房.glb 后必须跑三个校验脚本**，不跑不许提交——"
                 "节点名一旦对不上，前端界面上就只剩一块地面。",
    },
    "⑤-前端Three.js": {
        "taskBook": "角色材料/任务书/⑤-前端Three.js_7周任务书_v1.0",
        "extra": [
            "共享文档/03.接口规范与数据字典_v1.1.1.docx",
            "共享文档/11.第二轮功能补全任务分派与验收说明_v1.1.docx",
            "代码/frontend",
            "代码/backend/main.py",
            "代码/backend/devices.json",
            "_工具/validateScene.mjs",
        ],
        "start": "先看「⑤-前端Three.js_7周任务书」。"
                 "**别双击 scene.html 打开**——用 file:// 加载不了模型。"
                 "要双击根目录的 `一键启动.bat`，然后浏览器开 http://127.0.0.1:8000/ 。",
    },
    "⑥-产品经理": {
        "taskBook": "角色材料/任务书/⑥-产品经理与工程管理_7周任务书_v1.0",
        "extra": [
            "共享文档",
            "角色材料/任务书",
            "_工具/md2docx.py",
            "_工具/push-all.bat",
            "_工具/validateScene.mjs",
        ],
        "start": "先看「⑥-产品经理与工程管理_7周任务书」的第三节。"
                 "**W1 的第一件事是打 git tag**（先于任何代码改动）。"
                 "改完 .md 源稿后用 `python _工具/md2docx.py` 重新生成 .docx。",
    },
}

TXT_TEMPLATE = """泵房数字孪生监控系统 · {role} 任务包
────────────────────────────────────────────

一、你该从哪看起
  {start}

二、这个包里有什么
  · {taskBookDocx}          <-- 你的逐周任务，先看这个
  · 14.第三轮7周总排期与里程碑   <-- 全组的总排期，看你哪天该交什么
  · 12.三压分区改造方案         <-- 技术依据（为什么这么做）
  · 09.泵房设备与数据链路示意图  <-- 一张图看懂全系统，浏览器打开
  · README.md                  <-- 项目总入口
  · （另附你角色要用的代码与资料）

三、几条硬规矩
  1. 每周三上午演示，前一日 20:00 前必须把改动提交上去。
     没提交 = 没做。不提交，别人看不见，没法联调。
  2. 只推 GitHub，不推 Gitee。
  3. 卡住了当天在群里说，不要憋到周三。

四、项目周期
  2026-10-15（周四）→ 2026-12-02（周三），共 7 周，每 7 天一次演示。
  目标：完成「三压分区 × 6 台水泵」改造。

────────────────────────────────────────────
祝顺利。有问题随时在群里问。
"""


def shouldSkip(path):
    """判断某个路径是否该跳过（venv / 缓存 / 本地产物）。

    Args:
        path: 相对仓库根的路径。

    Returns:
        bool: True 表示跳过。
    """
    parts = path.replace("\\", "/").split("/")
    if any(p in EXCLUDE_DIRS for p in parts):
        return True
    if parts[-1] in EXCLUDE_FILES:
        return True
    return False


def norm(path):
    """把路径统一成正斜杠形式。

    必须做这一步：os.walk 在 Windows 上给的是反斜杠，而配置里写的是正斜杠，
    不统一的话 `set()` 去不了重，同一个文件会被打进压缩包两次
    （zip 会报 Duplicate name 警告）。

    Args:
        path: 任意分隔符形式的相对路径。

    Returns:
        str: 正斜杠形式的相对路径。
    """
    return path.replace("\\", "/")


def collect(path, out):
    """把一个文件或目录下的所有文件收集进列表。

    Args:
        path: 相对仓库根的路径（文件或目录）。
        out: 收集结果（会被就地修改），元素是正斜杠形式的相对路径。

    Returns:
        None。
    """
    if not os.path.exists(path):
        print("    [跳过] 找不到: %s" % path)
        return
    if os.path.isfile(path):
        if not shouldSkip(path):
            out.append(norm(path))
        return
    for root, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
        for name in files:
            full = os.path.join(root, name)
            rel = norm(os.path.relpath(full, "."))
            if not shouldSkip(rel):
                out.append(rel)


def buildZip(role, cfg, rootDir):
    """为单个角色生成一个 zip。

    Args:
        role: 角色名，如 "①-传感器与设备模拟"（用作文件名）。
        cfg: 该角色的配置字典（taskBook / extra / start）。
        rootDir: 仓库根目录的绝对路径。

    Returns:
        tuple[str, int, int]: (zip 路径, 文件数, zip 大小字节数)。
    """
    files = []
    for p in COMMON:
        collect(p, files)

    taskMd = cfg["taskBook"] + ".md"
    taskDocx = cfg["taskBook"] + ".docx"
    collect(taskMd, files)
    collect(taskDocx, files)

    for p in cfg["extra"]:
        collect(p, files)

    # 去重并保持顺序稳定
    files = sorted(set(files))

    zipPath = os.path.join(OUT_DIR, "泵房-%s.zip" % role)
    with zipfile.ZipFile(zipPath, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel in files:
            # zip 里统一用正斜杠（zip 规范如此），磁盘上用系统分隔符
            zf.write(os.path.join(rootDir, rel.replace("/", os.sep)), rel)
        zf.writestr(
            "先看我.txt",
            TXT_TEMPLATE.format(
                role=role,
                start=cfg["start"],
                taskBookDocx=os.path.basename(taskDocx),
            ),
        )

    return zipPath, len(files), os.path.getsize(zipPath)


def main():
    """入口：切到仓库根，为六个角色各打一个 zip。"""
    # 脚本在 _工具/ 下，仓库根是它的上一级
    rootDir = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
    os.chdir(rootDir)

    if not os.path.isdir(OUT_DIR):
        os.makedirs(OUT_DIR)

    print("仓库根: %s" % rootDir)
    print("输出到: %s\n" % OUT_DIR)

    total = 0
    for role, cfg in ROLES.items():
        zipPath, count, size = buildZip(role, cfg, rootDir)
        total += 1
        print("  [OK] %-28s %3d 个文件  %6.1f KB" % (os.path.basename(zipPath), count, size / 1024))

    print("\n共生成 %d 个压缩包，直接发微信/QQ 即可。" % total)
    print("\n[提醒] 收到 zip 的组员没法提交代码（包里没有 .git）。")
    print("  能让他们用 git 就用 git：GitHub 仓库 -> Settings -> Collaborators 加他们账号，")
    print("  然后 git clone 即可，任务书就在仓库里，改完直接 push。")


if __name__ == "__main__":
    main()

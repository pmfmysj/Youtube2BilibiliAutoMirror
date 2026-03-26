# =============================================================================
# bili_scheduler_uploader.py —— B站定时上传调度脚本
#
# 功能：
#   扫描 downloads/ 下四个文件夹（videos / shorts / streams / posts 不含）中
#   已翻译的视频文件，每隔 UPLOAD_INTERVAL 秒上传一个至 B站，
#   上传成功后同时删除视频文件和对应的 .txt 文件。
#
# 与其他脚本的衔接：
#   youtube_watcher.py       → 下载视频 + 生成原始 .txt
#   translator.py            → 翻译 .txt（译文在分隔线前，原文在分隔线后）
#   bili_scheduler_uploader.py（本文件）
#                            → 读取已翻译 .txt 的分隔线前内容作为标题/简介上传
#
# .txt 文件格式（由 translator.py 生成）：
#   第一行         → 翻译后的标题        ← 本脚本上传时使用此行
#   第二行起       → 翻译后的简介        ← 本脚本上传时使用此部分
#   （空行）
#   ---以上为AI翻译---
#   原始标题
#   原始简介
#
# 判断"已翻译"的依据：
#   .txt 文件中包含 SEPARATOR（---以上为AI翻译---）
#   未翻译的 .txt 不含此分隔线，会被跳过等待翻译完成
#
# 依赖安装：
#   pip install biliup
#
# 使用前提：
#   已有 bili_credential.json（运行 bili_login.py 扫码生成）
#
# 为什么换用 biliup：
#   bilibili-api-python 在 B站新版上传 API 下返回 406，是已知兼容性问题。
#   biliup 专为 B站投稿设计、持续维护，通过 subprocess 调用，稳定可靠。
# =============================================================================


# ---------- 标准库导入 ----------
import os           # 文件和目录操作
import re           # 正则表达式，从 biliup 输出中提取 BV 号
import json         # 读写 bili_credential.json 和 biliup cookies.json
import random       # 生成随机数实现上传间隔的随机波动
import asyncio      # 异步事件循环（asyncio.sleep 实现定时间隔）
import logging      # 日志输出
import subprocess   # 调用 biliup 命令行工具
import tempfile     # 生成 biliup 所需的临时 cookies.json
from pathlib import Path
from datetime import datetime
# 不再使用 bilibili-api-python（已知在 B站新版 API 下返回 406）


class _RateLimitError(Exception):
    """biliup 账号限流或上传锁冲突，需要较长时间等待后重试。"""
    pass


# =============================================================================
# ① 全局配置区 —— 按需修改
# =============================================================================

SCRIPT_DIR = Path(__file__).parent.resolve()
# 所有路径都相对于本脚本所在目录，无论从哪个工作目录运行都不会出错

# 待上传视频所在的文件夹（与 youtube_watcher.py / translator.py 保持一致）
# posts 文件夹只有 .txt 和图片，没有视频，不列入扫描范围
VIDEO_FOLDERS = [
    str(SCRIPT_DIR / "downloads" / "videos"),    # 普通视频
    str(SCRIPT_DIR / "downloads" / "shorts"),    # Shorts
    str(SCRIPT_DIR / "downloads" / "streams"),   # 直播录像
]

# B站登录凭证文件路径（由 bili_login.py 扫码生成）
CREDENTIAL_FILE      = str(SCRIPT_DIR / "bili_credential.json")
# biliup 登录后在当前目录生成的标准凭证文件
# 运行一次 "cd D:\autoMirror && biliup login" 扫码后自动生成
# 若此文件存在，脚本直接使用它（完全正确的格式），不再手动拼装
BILIUP_COOKIES_FILE  = str(SCRIPT_DIR / "cookies.json")

# 上传日志文件路径
LOG_FILE = str(SCRIPT_DIR / "bili_upload_log.txt")

# 两次上传之间的间隔（秒），默认 600 = 10 分钟
# B站对投稿频率有限制，间隔过短可能触发风控，建议不低于 300 秒
DEFAULT_INTERVAL = 1800 # 15 分钟
 # 反反爬虫 随机波动正负1.5min
UPLOAD_JITTER = 90
UPLOAD_INTERVAL = DEFAULT_INTERVAL + random.randint(-UPLOAD_JITTER, UPLOAD_JITTER)

# 支持的视频文件扩展名（集合，O(1) 查找）
VIDEO_EXTENSIONS = {".mp4", ".flv", ".mkv", ".avi", ".mov", ".wmv"}

# B站投稿分区 ID
# 130 = 音乐综合（参考 https://biliup.github.io/tid-ref.html）
BILI_TID = 130

# 视频标签（B站最多 12 个，每个不超过 20 字符）
BILI_TAGS = ["偶像大师"]

# 分隔线字符串（必须与 translator.py 中的 SEPARATOR 完全一致）
SEPARATOR = "-" * 3 + "以上为AI翻译" + "-" * 3
# 值为：---以上为AI翻译---
# 本脚本根据此分隔线判断文件是否已翻译，并截取分隔线前的译文内容

# B站标题最大字符数（超出部分截断，留 4 字符余量避免边界截断）
BILI_TITLE_MAX  = 76
# B站简介最大字符数（含译文+原文，留 50 字符余量避免边界截断）
BILI_DESC_MAX   = 1950


# =============================================================================
# ② 日志初始化
# =============================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),                                          # 终端
        logging.FileHandler(LOG_FILE, encoding="utf-8"),                 # 文件
    ]
)
log = logging.getLogger(__name__)


# =============================================================================
# ③ 凭证加载
# =============================================================================

def load_credential_dict() -> dict:
    """
    从 bili_credential.json 读取 B站登录凭证，返回原始字典。

    字段说明：
        sessdata      —— 登录 Session，身份令牌（必填）
        bili_jct      —— CSRF Token，写操作防伪造（必填）
        buvid3        —— 设备指纹（必填，缺失导致 406）
        dedeuserid    —— 用户 UID（可选）
        ac_time_value —— 凭证刷新字段（可选）
    """
    with open(CREDENTIAL_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def make_biliup_cookies(cred: dict) -> dict:
    """
    将 bili_credential.json 格式转换为 biliup cookies.json 的完整 schema。

    biliup Rust 核心要求 token_info 包含 mid / expires_in 等字段，
    直接用 "biliup login" 生成的文件是最可靠的方式（见 BILIUP_COOKIES_FILE）。
    本函数作为兜底，在 cookies.json 不存在时使用。
    """
    return {
        "cookie_info": {
            "cookies": [
                {"name": "SESSDATA",             "value": cred.get("sessdata",   "") or ""},
                {"name": "bili_jct",             "value": cred.get("bili_jct",   "") or ""},
                {"name": "buvid3",               "value": cred.get("buvid3",     "") or ""},
                {"name": "DedeUserID",           "value": cred.get("dedeuserid", "") or ""},
                {"name": "DedeUserID__ckMd5",    "value": ""},
            ],
            "domains": []
        },
        "token_info": {
            "access_token":  "",
            "refresh_token": "",
            "expires_in":    0,
            "mid":           int(cred.get("dedeuserid") or 0),
            # mid = 用户 UID，biliup Rust 核心强制要求此字段为整数
        }
    }


# =============================================================================
# ④ 元数据解析
# =============================================================================

def parse_translated_meta(txt_path: Path) -> tuple[str, str] | None:
    """
    读取已翻译的 .txt 文件，提取分隔线**之前**的译文部分作为标题和简介。

    文件格式（由 translator.py 生成）：
        第一行     → 翻译后的标题
        第二行起   → 翻译后的简介
        （空行）
        ---以上为AI翻译---   ← SEPARATOR
        原始内容...

    判断逻辑：
        - 文件中包含 SEPARATOR → 已翻译，取分隔线前的内容
        - 文件中不含 SEPARATOR → 尚未翻译，返回 None 表示跳过

    参数：
        txt_path —— .txt 文件的 Path 对象

    返回：
        (title, desc) 元组，或 None（文件未翻译 / 读取失败时）
    """
    try:
        content = txt_path.read_text(encoding="utf-8")
    except Exception as e:
        log.warning(f"  读取元数据失败 {txt_path.name}: {e}")
        return None

    if SEPARATOR not in content:
        # 分隔线不存在 → 文件尚未经过 translator.py 翻译，跳过
        # 不打印警告，因为这是正常的"等待翻译"状态
        return None

    # ── 标题：取分隔线之前译文的第一行 ────────────────────────────────────────
    translated_part = content.split(SEPARATOR, maxsplit=1)[0].strip()
    # split(..., maxsplit=1)：只在第一个分隔线处切分
    # [0].strip()          ：译文部分，去除首尾空白

    if not translated_part:
        log.warning(f"  译文部分为空，跳过: {txt_path.name}")
        return None

    title = translated_part.splitlines()[0].strip()
    # 译文第一行作为 B站标题

    if not title:
        title = txt_path.stem
        # 标题为空时降级为文件名（不含扩展名）

    # ── 简介：译文 + 分隔线 + 原文，截取前 BILI_DESC_MAX 字符 ─────────────────
    full_content = content.strip()
    # 整个文件内容（含译文、分隔线、原文），去除首尾空白

    # 从第二行起取译文剩余部分（简介的译文部分）
    translated_lines = translated_part.splitlines()
    translated_desc  = "\n".join(translated_lines[1:]).strip()
    # 译文简介部分（标题行之后的内容）

    if translated_desc:
        # 有译文简介：格式为"译文简介 + 空行 + 分隔线 + 原文"
        desc_full = translated_desc + "\n\n" + SEPARATOR + "\n" + content.split(SEPARATOR, 1)[1].strip()
    else:
        # 无译文简介（如纯标题视频）：直接"分隔线 + 原文"
        desc_full = SEPARATOR + "\n" + content.split(SEPARATOR, 1)[1].strip()

    desc = desc_full[:BILI_DESC_MAX]
    # B站简介上限为 BILI_DESC_MAX（2000）字符，本项目统一使用 1900
    # 截断保护：超出部分直接丢弃，不做省略号处理（避免破坏原文结构）

    return title, desc


# =============================================================================
# ⑤ 待上传视频扫描
# =============================================================================

def find_next_video() -> Path | None:
    """
    扫描 VIDEO_FOLDERS，返回最优先上传的已翻译视频。

    排序策略（最新优先）：
        ① 所在子文件夹的最新修改时间（降序）——最近有新文件入库的频道/类型优先
        ② 同一子文件夹内，视频文件的修改时间（降序）——最新下载的视频优先

    这样可以保证刚获取的新视频尽快进入上传队列，
    而不是等待老视频全部上传完毕。

    返回：
        下一个待上传视频的 Path 对象；若无可上传视频则返回 None
    """
    # 按子文件夹收集候选视频：{子文件夹路径: [(视频mtime, 视频路径), ...]}
    folder_map: dict[Path, list[tuple[float, Path]]] = {}

    for folder in VIDEO_FOLDERS:
        if not os.path.exists(folder):
            continue

        for root, _dirs, files in os.walk(folder):
            root_path = Path(root)
            for filename in files:
                if Path(filename).suffix.lower() not in VIDEO_EXTENSIONS:
                    continue

                video_path = root_path / filename
                txt_path   = video_path.with_suffix(".txt")

                if not txt_path.exists():
                    continue

                try:
                    txt_content = txt_path.read_text(encoding="utf-8")
                except Exception:
                    continue

                if SEPARATOR not in txt_content:
                    continue
                    # 尚未翻译，跳过

                mtime = video_path.stat().st_mtime
                folder_map.setdefault(root_path, []).append((mtime, video_path))

    if not folder_map:
        return None

    # 按"子文件夹内最新文件的 mtime"降序排列子文件夹
    # 即：最近有新视频入库的文件夹排在最前面
    sorted_folders = sorted(
        folder_map.items(),
        key=lambda kv: max(t for t, _ in kv[1]),
        reverse=True,   # 降序：最新的文件夹优先
    )

    # 取最新文件夹中 mtime 最大（最新）的视频
    _top_folder, top_videos = sorted_folders[0]
    top_videos.sort(key=lambda x: x[0], reverse=True)   # 同文件夹内也按最新排
    return top_videos[0][1]


# =============================================================================
# ⑥ 上传记录
# =============================================================================

def append_upload_record(video_path: Path, title: str, bvid: str):
    """
    在 LOG_FILE 末尾追加一条上传成功的结构化记录。

    参数：
        video_path —— 已上传的视频文件路径（用于记录文件名）
        title      —— 视频标题（来自 .txt 译文第一行）
        bvid       —— B站返回的 BV 号，如 "BV1xxxxxxxxx"
    """
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    record = (
        f"[{timestamp}] 上传成功\n"
        f"  文件:  {video_path.name}\n"
        f"  标题:  {title}\n"
        f"  BVID:  {bvid}\n"
        f"  路径:  {video_path.parent}\n"
        f"{'─' * 50}\n"
    )
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(record)
    log.info(f"  上传记录已写入日志：{title} → {bvid}")


# =============================================================================
# ⑦ 核心上传函数
# =============================================================================

def upload_one(video_path: Path) -> bool:
    """
    用 biliup CLI 将一个视频上传至 B站，成功后删除视频、.txt 和封面文件。

    为什么用 biliup 而非 bilibili-api-python：
        bilibili-api-python 在 B站新版上传 API 下持续返回 406，
        是已知的库兼容性问题。biliup 专为 B站投稿设计、积极维护，
        通过 subprocess 调用与 yt-dlp 使用方式一致。

    上传流程：
        ① 解析 .txt 获取标题/简介
        ② 读取凭证，写入临时 cookies.json
        ③ 组装 biliup upload 命令并执行
        ④ 从 stdout 解析 BV 号
        ⑤ 写入上传日志
        ⑥ 删除视频、txt、封面文件

    参数：
        video_path —— 待上传视频的 Path 对象

    返回：
        True = 全流程成功；False = 任一步骤失败
    """

    # ── ① 解析译文标题和简介 ─────────────────────────────────────────────────

    txt_path = video_path.with_suffix(".txt")

    meta_result = parse_translated_meta(txt_path)
    if meta_result is None:
        log.error(f"  元数据解析失败，跳过上传: {video_path.name}")
        return False

    title, desc = meta_result
    log.info(f"  标题: {title[:60]}{'…' if len(title) > 60 else ''}")
    log.info(f"  简介: {len(desc)} 字符")

    # ── ② 加载凭证并写入临时 cookies.json ───────────────────────────────────
    # biliup 通过 --cookies 参数指定 cookies 文件路径，格式与 bili_credential.json 略有差异，
    # 用 tempfile 创建临时文件，上传结束后自动清理。

    # 优先使用 biliup login 生成的标准 cookies.json
    # 若不存在则从 bili_credential.json 手动转换
    if os.path.exists(BILIUP_COOKIES_FILE):
        try:
            with open(BILIUP_COOKIES_FILE, encoding="utf-8") as _f:
                cookies_data = json.load(_f)
            log.info(f"  使用 biliup 标准凭证: cookies.json")
        except Exception as e:
            log.error(f"  读取 cookies.json 失败: {e}")
            return False
    else:
        # cookies.json 不存在，从 bili_credential.json 手动构造
        log.info("  未找到 cookies.json，从 bili_credential.json 构造凭证")
        log.info("  建议运行 'cd D:\autoMirror && biliup login' 生成标准凭证文件")
        try:
            cred = load_credential_dict()
        except Exception as e:
            log.error(f"  凭证加载失败: {e}")
            return False
        cookies_data = make_biliup_cookies(cred)

    # ── ③ 组装并执行 biliup upload 命令 ──────────────────────────────────────

    # 封面文件处理
    cover_path = video_path.with_suffix(".jpg")
    cover_arg  = str(cover_path) if cover_path.exists() else ""
    if cover_arg:
        log.info(f"  使用封面: {cover_path.name}")
    else:
        log.info("  未找到封面文件，biliup 将自动截帧")

    # 标签：biliup 用逗号分隔多个标签
    tags_str = ",".join(BILI_TAGS)

    # biliup 使用 --desc 直接传字符串（无文件传入参数）
    # 换行替换为空格，避免 Windows 命令行参数解析错误
    bvid = ""

    with tempfile.TemporaryDirectory() as tmpdir:
        # 临时目录：程序退出或 with 块结束时自动删除所有临时文件

        cookies_file = os.path.join(tmpdir, "cookies.json")
        with open(cookies_file, "w", encoding="utf-8") as f:
            json.dump(cookies_data, f, ensure_ascii=False)
        # biliup 从此 JSON 文件读取 Cookie，格式要求键名为原始 Cookie 名

        # \r\n（Windows换行）和单独 \r 统一转为 \n，确保格式一致
        # 不替换 \n 为空格——biliup 支持简介中的换行，B站会原样显示
        desc_inline = desc[:BILI_DESC_MAX].replace("\r\n", "\n").replace("\r", "\n")
        desc_inline = desc_inline.encode("gbk", errors="ignore").decode("gbk")
        # encode("gbk", errors="ignore")：过滤 GBK 无法编码的字符（emoji、特殊框线等）
        # 防止 biliup 内部日志输出时崩溃，与换行处理无关

        # 防止简介以 "-" 开头被 biliup 参数解析器误认为命令行标志
        # 例如 "---以上为AI翻译---" 出现在简介开头时，biliup 会把 "--" 当作参数前缀
        if desc_inline.lstrip().startswith("-"):
            desc_inline = " " + desc_inline
            # 加一个空格：B站显示时会忽略，biliup 解析时识别为普通字符串值

        # biliup 的凭证机制：从当前工作目录读取 cookies.json，
        # 不支持 --cookies 参数。因此把 cwd 设为临时目录，
        # cookies.json 已写入该目录，biliup 启动时会自动找到它。
        cmd = [
            "biliup", "upload",
            "--tid",         str(BILI_TID),           # 分区 ID（130 = 音乐综合）
            "--tag",         tags_str,                 # 标签
            "--title",       title[:BILI_TITLE_MAX],   # 标题（80字上限）
            "--desc",        desc_inline,              # 简介（保留换行，\r 已标准化为 \n）
            "--submit",      "web",
            # app 模式：模拟 B站客户端 APP 的提交接口
            # 与默认的 web（网页）接口相比，app 接口对投稿频率的限制更宽松，
            # 可有效绕过 code 21566"投稿过于频繁"错误
            # 可选值：app / web / b-cut-android
            # 1=原创, 2=转载（搬运 YouTube 内容选转载，符合 B站规则）
        ]

        if cover_arg:
            cmd += ["--cover", cover_arg]
            # 封面路径（有则传入，无则 biliup 自动截帧）

        cmd.append(str(video_path))
        # 视频文件路径放在最后（绝对路径，cwd 切换不影响）

        log.info(f"  执行上传命令: biliup upload ... {video_path.name}")

        import os as _os
        biliup_env = _os.environ.copy()
        biliup_env["PYTHONUTF8"] = "1"
        # PYTHONUTF8=1：强制 biliup 子进程使用 UTF-8 而非 Windows 默认的 GBK
        # 防止 biliup 内部日志输出时因无法编码 Unicode 字符而崩溃
        biliup_env["PYTHONIOENCODING"] = "utf-8"
        # PYTHONIOENCODING=utf-8：双保险，明确 stdin/stdout/stderr 编码

        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=tmpdir,
                env=biliup_env,
                # env=biliup_env：传入修改后的环境变量（含 PYTHONUTF8=1）
            )
        except FileNotFoundError:
            log.error("  【错误】找不到 biliup 命令，请先安装：pip install biliup")
            return False
        except Exception as e:
            log.error(f"  biliup 执行异常: {e}")
            return False

        # 打印 biliup 的完整输出（含进度信息）
        if proc.stdout:
            for line in proc.stdout.strip().splitlines():
                log.info(f"  [biliup] {line}")
        if proc.stderr:
            for line in proc.stderr.strip().splitlines():
                log.warning(f"  [biliup stderr] {line}")

        if proc.returncode != 0:
            combined = (proc.stdout or "") + (proc.stderr or "")
            if "限流" in combined or "rate" in combined.lower() or "另一个" in combined:
                # 账号级限流/锁冲突：biliup 服务器端有未释放的上传会话
                # 等待时间由调用方的 60 秒改为 5 分钟，给服务器充足时间释放锁
                log.error("  检测到限流或上传锁冲突，请等待数分钟后重试")
                log.error("  原因：之前的失败尝试在服务器端留下了未释放的上传会话")
                raise _RateLimitError("biliup rate limit / lock conflict")
            log.error(f"  biliup 返回错误码 {proc.returncode}，上传失败")
            return False

        # ── ④ 从输出解析 BV 号 ───────────────────────────────────────────────
        combined_output = (proc.stdout or "") + (proc.stderr or "")
        bvid_match = re.search(r'BV[0-9A-Za-z]+', combined_output)
        # BV 号格式：BV 后跟若干字母数字，如 BV1xxxxxxxxx
        bvid = bvid_match.group(0) if bvid_match else "（未解析到）"
        log.info(f"  ✓ 上传成功！BVID: {bvid}")

    # ── ⑤ 写入上传日志 ───────────────────────────────────────────────────────

    append_upload_record(video_path, title, bvid)

    # ── ⑥ 删除本地文件 ───────────────────────────────────────────────────────

    for fpath, label in [
        (video_path,                  "视频文件"),
        (txt_path,                    "元数据文件"),
        (video_path.with_suffix(".jpg"), "封面文件"),
    ]:
        if fpath.exists():
            try:
                fpath.unlink()
                log.info(f"  已删除{label}: {fpath.name}")
            except Exception as e:
                log.warning(f"  {label}删除失败（可手动删除）: {fpath.name}  {e}")

    return True


# =============================================================================
# ⑧ 主循环
# =============================================================================

async def main():
    """
    程序主循环：
        每隔 UPLOAD_INTERVAL 秒扫描一次待上传视频，找到则上传一个后等待，
        未找到则短暂等待后继续扫描。
        Ctrl+C 优雅退出。
    """
    log.info("=" * 60)
    log.info("B站定时上传调度器启动")
    log.info(f"扫描目录: {', '.join(VIDEO_FOLDERS)}")
    log.info(f"上传分区: tid={BILI_TID}（音乐综合）")
    log.info(f"视频标签: {BILI_TAGS}")
    log.info(f"上传间隔: {UPLOAD_INTERVAL} 秒（{UPLOAD_INTERVAL // 60} 分钟）")
    log.info(f"已翻译判据: 文件中包含分隔线 [{SEPARATOR}]")
    log.info("=" * 60)

    # ── 启动检查 ──────────────────────────────────────────────────────────────

    # 检查凭证文件：优先 cookies.json（biliup login 生成），其次 bili_credential.json
    if os.path.exists(BILIUP_COOKIES_FILE):
        log.info(f"找到 biliup 标准凭证文件: {BILIUP_COOKIES_FILE}")
    elif os.path.exists(CREDENTIAL_FILE):
        log.info(f"使用 bili_credential.json 构造凭证（推荐改用 biliup login）")
    else:
        log.error("=" * 60)
        log.error("【错误】未找到任何凭证文件，请选择以下方式之一：")
        log.error("  方式 A（推荐）: cd D:\autoMirror && biliup login  → 扫码生成 cookies.json")
        log.error("  方式 B: 运行 bili_login.py 扫码生成 bili_credential.json")
        log.error("=" * 60)
        return

    # 凭证文件完整性检查（仅在使用 bili_credential.json 时才检查字段）
    if os.path.exists(BILIUP_COOKIES_FILE):
        # 使用 biliup login 生成的 cookies.json，无需检查旧格式字段
        try:
            with open(BILIUP_COOKIES_FILE, encoding="utf-8") as f:
                biliup_cred = json.load(f)
            uid = (biliup_cred.get("token_info", {}) or {}).get("mid", "未知")
            log.info(f"凭证就绪（biliup cookies.json）: uid={uid}")
        except Exception as e:
            log.error(f"【错误】cookies.json 读取失败: {e}")
            return
    else:
        # 使用旧版 bili_credential.json，检查必填字段
        try:
            with open(CREDENTIAL_FILE, encoding="utf-8") as f:
                cred_data = json.load(f)

            missing_fields = [k for k in ("sessdata", "bili_jct") if not cred_data.get(k)]
            if missing_fields:
                log.error(f"【错误】凭证文件缺少必填字段: {missing_fields}，请重新运行 bili_login.py")
                return

            fields_status = {
                "buvid3":        "✓" if cred_data.get("buvid3")        else "✗ 缺失",
                "dedeuserid":    "✓" if cred_data.get("dedeuserid")    else "✗ 缺失",
                "ac_time_value": "✓" if cred_data.get("ac_time_value") else "✗ 缺失（可能导致 406）",
            }
            for field, status in fields_status.items():
                log.info(f"  凭证字段 {field}: {status}")

            if not cred_data.get("ac_time_value"):
                log.warning("ac_time_value 为空，biliup 仍可正常上传（此字段仅用于凭证刷新）")

            log.info(f"凭证文件就绪（bili_credential.json）: uid={cred_data.get('dedeuserid', '未知')}")

        except Exception as e:
            log.error(f"【错误】凭证文件读取失败: {e}")
            return

    # ── 每次运行只上传一个视频，上传完立即结束 ───────────────────────────────
    video = find_next_video()

    if video is None:
        log.info("当前无待上传视频")
        return

    log.info(f"\n{'─' * 50}")
    log.info(f"准备上传: {video.name}")
    log.info(f"所在目录: {video.parent}")

    rate_limited = False
    try:
        success = await asyncio.to_thread(upload_one, video)
    except _RateLimitError:
        success = False
        rate_limited = True

    if success:
        log.info(f"上传完成")
        log.info(f"{'─' * 50}\n")
    else:
        if rate_limited:
            log.error("账号限流/锁冲突，请稍后重试")
        else:
            log.error("本次上传失败")
        log.info(f"{'─' * 50}\n")


# =============================================================================
# ⑨ 程序入口
# =============================================================================

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("\n收到 Ctrl+C，调度器已停止。")

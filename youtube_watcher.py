# =============================================================================
# youtube_watcher.py —— YouTube 频道自动监控下载脚本
#
# 功能：
#   - 轮询监控多个 YouTube 频道，发现新视频/动态时自动下载保存
#   - 视频：下载 mp4 文件 + 同名 .txt（第一行=标题，其余=简介）
#           → 与 bili_auto_uploader.py 的文件格式完全兼容
#   - 动态：保存文本到 .txt，若含图片则新建同名目录保存图片
#   - 通过 seen_ids.json 记录已处理 ID，避免重复下载
#
# 依赖安装：
#   pip install playwright requests
#   playwright install chromium
#   pip install yt-dlp   （或 brew install yt-dlp / apt install yt-dlp）
#
# Cookie 文件：
#   用浏览器插件（如 Get cookies.txt LOCALLY）导出 YouTube 的 cookies，
#   保存为 www.youtube.com_cookies.txt 放在脚本同目录。
#
# 文件保存结构：
#   VIDEOS_FOLDER/          ← 普通视频
#       频道名/
#           视频ID.mp4
#           视频ID.jpg       ← 视频封面（yt-dlp 自动截取最高分辨率缩略图）
#           视频ID.txt       ← 第一行=标题，其余=简介
#   SHORTS_FOLDER/           ← Shorts（≤60 秒竖屏短视频）
#       频道名/
#           视频ID.mp4
#           视频ID.jpg
#           视频ID.txt
#   STREAMS_FOLDER/          ← 直播结束后的录像存档
#       频道名/
#           视频ID.mp4
#           视频ID.jpg
#           视频ID.txt
#   POSTS_FOLDER/            ← 社区动态
#       频道名/
#           动态ID.txt       ← 动态文本内容
#           动态ID/          ← 若有图片则新建此子目录
#               01.jpg
#               02.jpg
# =============================================================================


# ---------- 标准库导入 ----------
import os               # 文件/目录操作
import re               # 正则表达式，用于清理文件名中的非法字符
import json             # 读写 seen_ids.json 状态文件
import random           # 生成随机数，用于请求抖动和检查间隔抖动
import time             # time.sleep() 轮询间隔、时间戳备用 ID
import asyncio          # 异步事件循环
import logging          # 日志输出
import subprocess       # 调用 yt-dlp 命令行工具
import urllib.request   # 标准库 HTTP 下载，用于保存动态图片
from pathlib import Path
from datetime import datetime


# ---------- 第三方库导入 ----------
from playwright.async_api import async_playwright
# playwright：无头浏览器，用于抓取 YouTube 社区动态（社区页面需 JS 渲染，无法直接 requests 抓取）


# =============================================================================
# ① 全局配置区 —— 按需修改
# =============================================================================

# 要监控的频道列表，每项包含：
#   name      ── 用作本地保存的子目录名（建议用英文，避免特殊字符）
#   videos    ── 频道的 /videos 页 URL（普通视频）
#   shorts    ── 频道的 /shorts 页 URL（Shorts 短视频）
#   streams   ── 频道的 /streams 页 URL（直播录像存档）
#   posts     ── 频道的 /posts  页 URL（社区动态）
#   不需要某类内容时，将对应字段设为 None 或直接删除该行即可
CHANNELS = [
#    {
#        "name":    "imas-official",
#        "videos":  "https://www.youtube.com/@imas-official/videos",
#        "shorts":  "https://www.youtube.com/@imas-official/shorts",
#        "streams": "https://www.youtube.com/@imas-official/streams",
#        "posts":   "https://www.youtube.com/@imas-official/posts",
#    },
#    {
#        "name":    "hatsuboshiGakuen",
#        "videos":  "https://www.youtube.com/@hatsuboshi_gakuen/videos",
#        "shorts":  "https://www.youtube.com/@hatsuboshi_gakuen/shorts",
#        "streams": "",
#        "posts":   "https://www.youtube.com/@hatsuboshi_gakuen/posts",
#    },
#    {
#        "name":    "DenonbuChannel",
#        "videos":  "https://www.youtube.com/@denonbu/videos",
#        "shorts":  "",
#        "streams": "https://www.youtube.com/@denonbu/streams",
#        "posts":   "https://www.youtube.com/@denonbu/posts",
#    },
#    {
#        "name":    "Trysail",
#        "videos":  "https://www.youtube.com/@trysail/videos",
#        "shorts":  "",
#        "streams": "",
#        "posts":   "https://www.youtube.com/@trysail/posts",
#    }
    # 继续添加更多频道：
    # {
    #     "name":    "another-channel",
    #     "videos":  "https://www.youtube.com/@another/videos",
    #     "shorts":  "https://www.youtube.com/@another/shorts",
    #     "streams": "https://www.youtube.com/@another/streams",
    #     "posts":   "https://www.youtube.com/@another/posts",
    # },
]

# 【修复1】所有路径相对于"脚本文件所在目录"，而非运行时的工作目录。
# 原来用 "./" 是相对于终端 cd 进去的目录，从其他位置运行时路径会错。
# Path(__file__).parent 永远指向 youtube_watcher.py 所在的文件夹。
SCRIPT_DIR = Path(__file__).parent.resolve()
# Path(__file__)  ：当前脚本文件的路径
# .parent         ：取其所在目录（去掉文件名部分）
# .resolve()      ：转为绝对路径，消除 "../" 等相对符号的歧义

VIDEOS_FOLDER  = str(SCRIPT_DIR / "downloads" / "videos")   # 普通视频及同名 .txt 的根目录
SHORTS_FOLDER  = str(SCRIPT_DIR / "downloads" / "shorts")   # Shorts 短视频的根目录
STREAMS_FOLDER = str(SCRIPT_DIR / "downloads" / "streams")  # 直播录像存档的根目录
POSTS_FOLDER   = str(SCRIPT_DIR / "downloads" / "posts")    # 动态文本及图片的根目录
COOKIES_FILE   = str(SCRIPT_DIR / "www.youtube.com_cookies.txt")  # Cookie 文件（兜底方案）
SEEN_FILE      = str(SCRIPT_DIR / "seen_ids.json")           # 已处理 ID 的状态文件

# ── Cookie 直读配置 ──────────────────────────────────────────────────────────
# yt-dlp 支持直接从浏览器读取当前登录的 Cookie，无需手动导出文件。
# 将 COOKIE_FROM_BROWSER 设为浏览器名称即可实现"实时 Cookie"，
# 彻底解决 Cookie 文件过期导致的验证失败问题。
#
# 支持的浏览器名称：
#   "chrome"   "chromium"  "firefox"  "edge"
#   "opera"    "brave"     "vivaldi"  "safari"（仅 macOS）
#
# 设为 None 则退回使用 COOKIES_FILE 静态文件（旧行为）
COOKIE_FROM_BROWSER = "firefox"
# "chrome" "edge"  "firefox"

# ── 简介截断词配置 ───────────────────────────────────────────────────────────
# 当简介中某一行符合以下任一条件时，从该行开始截断，不保留后续内容：
#   1. 该行包含 5 个或以上连续的 = 号（分隔线）
#   2. 该行包含下列任意一个关键词（精确匹配，大小写不敏感）
# 用途：去除 YouTube 视频简介末尾的时间轴、社交媒体链接、固定免责声明等无关内容
DESC_STOP_PATTERNS = [
    # 在此列表中添加需要触发截断的关键词（每项为字符串）
    # 示例：
    # "Follow us on",
    "「学マス」で検索！",
    "◆アイドルマスターチャンネルとは？",
    "◆What is the Idolmaster Channel?",
]
# 等号分隔线检测阈值：连续 = 号达到此数量时触发截断
DESC_STOP_EQUALS = 999

VIDEO_FETCH_COUNT = 5
# 每次检查时，从频道 /videos 拉取最新的 N 条普通视频进行对比
# 增大此值可减少漏下载的概率，但会稍微增加 yt-dlp 请求时间

SHORTS_FETCH_COUNT = 5
# Shorts 发布频率通常比普通视频高，默认多拉取几条
# YouTube /shorts 页面按发布时间倒序排列，取前 N 条即最新 N 条

STREAMS_FETCH_COUNT = 5
# 直播录像更新频率低，一般只需检查最新的 2-3 条
# 注意：正在直播的视频（is_live）会被自动跳过，等直播结束后下一轮再下载

POST_FETCH_COUNT = 5
# 每次检查时，用 playwright 滚动加载并抓取最新 N 条动态

CHECK_INTERVAL = 60 * 60
# 两次检查之间的间隔（秒），1800 = 30 分钟
# 频繁请求可能被 YouTube 限速，建议不低于 600（10 分钟）

INTERVAL_JITTER = 10 * 60 
# 主循环波动范围 (正负 10 分钟)

REQ_JITTER_MIN = 3
REQ_JITTER_MAX = 10
# 每个频道请求之间的随机抖动范围 (3-10 秒)

# =============================================================================
# ② 日志初始化
# =============================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),                             # 输出到终端
        logging.FileHandler("youtube_watcher.log", encoding="utf-8"),  # 同步写入日志文件
    ]
)
log = logging.getLogger(__name__)


# =============================================================================
# ③ 已处理 ID 的持久化（防止重复下载）
# =============================================================================

def load_seen_ids() -> dict:
    """
    从 SEEN_FILE 读取已处理的视频/动态 ID 记录。

    文件结构示例：
        {
            "imas-official": {
                "video_ids": ["abc123", "def456"],
                "post_ids":  ["Ugxxx", "Ugyyy"]
            }
        }

    若文件不存在（首次运行），返回空字典。
    """
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}
    # 空字典：所有频道尚无任何已处理记录，后续会按需创建各频道的子键


def save_seen_ids(data: dict):
    """
    将最新的已处理 ID 记录写回 SEEN_FILE（覆盖写入）。

    参数：
        data —— load_seen_ids() 返回并经过更新的字典
    """
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        # ensure_ascii=False：允许写入中文等 Unicode 字符
        # indent=2         ：格式化输出，方便人工查看


def get_seen(seen: dict, channel_name: str, key: str) -> set:
    """
    取出指定频道、指定类型（video_ids / post_ids）的已处理 ID 集合。

    参数：
        seen         —— 从 load_seen_ids() 返回的完整字典
        channel_name —— 频道名称（与 CHANNELS 中的 name 字段一致）
        key          —— "video_ids" 或 "post_ids"

    返回：
        set —— 该频道已处理的 ID 集合（不存在时返回空 set）
    """
    return set(seen.get(channel_name, {}).get(key, []))


def mark_seen(seen: dict, channel_name: str, key: str, new_id: str):
    """
    将一个新 ID 添加到已处理集合中（就地修改 seen 字典）。

    参数：
        seen         —— 当前状态字典（会被就地修改）
        channel_name —— 频道名称
        key          —— "video_ids" 或 "post_ids"
        new_id       —— 刚处理完毕、需要标记为"已见"的 ID
    """
    if channel_name not in seen:
        seen[channel_name] = {"video_ids": [], "post_ids": []}
        # 首次遇到该频道时，初始化其子字典

    if new_id not in seen[channel_name].get(key, []):
        seen[channel_name].setdefault(key, []).append(new_id)
        # setdefault：若 key 不存在则先创建空列表，再 append
        # 只有不重复时才追加，避免列表膨胀


# =============================================================================
# ④ 工具函数
# =============================================================================

def sanitize_filename(name: str) -> str:
    """
    清理字符串，使其可以安全用作文件名/目录名。

    替换 Windows/macOS/Linux 均不允许的特殊字符为下划线，
    并去除首尾空白，防止目录创建失败。

    参数：
        name —— 原始字符串（通常是视频标题）

    返回：
        可安全用作文件名的字符串
    """
    name = re.sub(r'[\\/:*?"<>|]', '_', name)
    # 替换文件名非法字符：\ / : * ? " < > |
    # 这些字符在 Windows 下绝对不允许，在 macOS/Linux 下也可能引发问题

    name = name.strip()
    # 去除首尾空格（部分 OS 不允许文件名以空格开头/结尾）

    return name[:200] if len(name) > 200 else name
    # 截断超长文件名（大多数文件系统限制 255 字节，留有余量）


def download_image(url: str, save_path: str) -> bool:
    """
    从给定 URL 下载图片并保存到本地路径。

    使用标准库 urllib.request，无需额外依赖。
    图片 URL 来自 YouTube，末尾追加 "=s0" 参数可请求原始分辨率。

    参数：
        url       —— 图片的完整 HTTP(S) URL
        save_path —— 本地保存路径（含文件名和扩展名）

    返回：
        True = 下载成功；False = 下载失败
    """
    try:
        # YouTube 图片 URL 通常有尺寸后缀（如 =w1280-h720），替换为 =s0 获取原图
        # 若 URL 结尾有 "=w..." 或 "=s..." 参数，统一替换；没有则直接追加 =s0
        if re.search(r'=w\d+', url) or re.search(r'=s\d+', url):
            url = re.sub(r'=(w\d+.*|s\d+.*)', '=s0', url)
        elif '?' not in url and not url.endswith('=s0'):
            url = url + '=s0'

        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        }
        # 伪装为普通浏览器请求，避免被 YouTube CDN 返回 403

        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=30) as resp:
            # timeout=30：30 秒内未响应则放弃，避免卡死
            with open(save_path, "wb") as f:
                f.write(resp.read())
                # 以二进制写入，图片文件不能用文本模式写

        return True

    except Exception as e:
        log.warning(f"    图片下载失败 {url}: {e}")
        return False


# =============================================================================
# ⑤ Cookie 参数构建 & 简介截断
# =============================================================================

# 缓存浏览器 Cookie 可用性测试结果（None=未测试，True=可用，False=失败）
_browser_cookie_ok: bool | None = None


def _test_browser_cookie() -> bool:
    """
    用一条轻量的 yt-dlp 命令测试浏览器 Cookie 是否可读。

    浏览器 Cookie 常见失败原因：
        1. 浏览器正在运行并锁住了 Cookie 数据库（最常见）
        2. yt-dlp 无法解密 Cookie（权限问题）
        3. 浏览器名称拼写错误

    返回 True = 可用；False = 失败
    """
    cmd = [
        "yt-dlp",
        "--cookies-from-browser", COOKIE_FROM_BROWSER,
        "--skip-download",
        "--quiet",
        "--no-warnings",
        "--playlist-items", "1",
        "--flat-playlist",
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
        )
        if result.returncode == 0:
            return True
        stderr = result.stderr.strip()
        if stderr:
            for line in stderr.splitlines()[:5]:
                log.warning(f"  [Cookie 测试] {line}")
        return False
    except Exception as e:
        log.warning(f"  浏览器 Cookie 测试异常: {e}")
        return False


def get_cookie_args() -> list[str]:
    """
    返回 yt-dlp 的 Cookie 参数，带自动降级逻辑。

    优先级：
        1. COOKIE_FROM_BROWSER 配置且测试通过 → --cookies-from-browser（实时读取）
        2. 测试失败                           → 自动降级到静态 Cookie 文件
        3. 静态文件存在                       → --cookies 静态文件
        4. 均不可用                           → 空列表（未登录）

    浏览器测试结果缓存在 _browser_cookie_ok，同一次运行只探测一次。
    """
    global _browser_cookie_ok

    if COOKIE_FROM_BROWSER:
        if _browser_cookie_ok is None:
            log.info(f"  正在测试浏览器 Cookie（{COOKIE_FROM_BROWSER}）…")
            _browser_cookie_ok = _test_browser_cookie()
            if _browser_cookie_ok:
                log.info(f"  ✓ 浏览器 Cookie 可用（{COOKIE_FROM_BROWSER}）")
            else:
                log.warning(f"  ✗ 浏览器 Cookie 失败，自动降级使用静态文件")
                log.warning(f"  常见原因：{COOKIE_FROM_BROWSER} 正在运行并锁住了 Cookie 数据库")
                log.warning("  解决方法：关闭浏览器后重启脚本，或改用静态 Cookie 文件")

        if _browser_cookie_ok:
            return ["--cookies-from-browser", COOKIE_FROM_BROWSER]

    if os.path.exists(COOKIES_FILE):
        return ["--cookies", COOKIES_FILE]

    log.warning("未配置任何可用 Cookie，yt-dlp 将以未登录状态运行（可能触发验证）")
    return []


def truncate_desc(desc: str) -> str:
    """
    按截断规则裁剪视频简介，去除末尾的无关内容。

    截断触发条件（逐行扫描，命中即截断该行及之后所有内容）：
        1. 该行包含连续 DESC_STOP_EQUALS 个（默认5个）或以上的 = 号
        2. 该行包含 DESC_STOP_PATTERNS 中的任意关键词（大小写不敏感）

    参数：
        desc —— 原始简介字符串（可含换行）

    返回：
        截断后的简介字符串（首尾空白已去除）
    """
    if not desc:
        return desc

    # ── 区间删除：去除 [EN Credits.] 到 [JP Credits.] 之间的全部内容 ──────────
    # re.sub 的 re.DOTALL 让 . 匹配换行符，确保跨行区间被完整删除
    # re.IGNORECASE 兼容大小写变体（如 [en credits.]）
    # 删除结果中可能留下连续空行，用 strip() 在最后统一清理
    desc = re.sub(
        r'\[EN Credits\.\].*?\[JP Credits\.\]',
        '',
        desc,
        flags=re.DOTALL | re.IGNORECASE,
    )
    # \[EN Credits\.\] ：匹配字面量 [EN Credits.]（方括号和点号需转义）
    # .*?              ：非贪婪匹配，遇到最近的 [JP Credits.] 即停止
    # \[JP Credits\.\] ：匹配字面量 [JP Credits.]
    desc = re.sub(
        r'EN Credits.*?JP Credits',
        '',
        desc,
        flags=re.DOTALL | re.IGNORECASE,
    )
    # 兼容没有方括号的变体，如 "EN Credits" 和 "JP Credits"

    equals_pattern = re.compile(r"={" + str(DESC_STOP_EQUALS) + r",}")
    # 动态构造正则：匹配 DESC_STOP_EQUALS 个或以上连续 = 号
    # 例如 DESC_STOP_EQUALS=5 时匹配 "=====" 及更长的等号串

    lines = desc.splitlines()
    result_lines = []

    for line in lines:
        # 条件1：检测等号分隔线
        if equals_pattern.search(line):
            break
            # 找到等号分隔线，截断并停止

        # 条件2：检测关键词列表
        line_lower = line.lower()
        hit_keyword = any(kw.lower() in line_lower for kw in DESC_STOP_PATTERNS if kw)
        if hit_keyword:
            break
            # 找到关键词，截断并停止

        result_lines.append(line)
        # 本行未触发截断条件，保留

    return "\n".join(result_lines).strip()
    # 重新拼接保留的行，去除首尾空白


# =============================================================================
# ⑥ 视频信息获取与下载（yt-dlp）
# =============================================================================

def get_channel_videos(channel_url: str, count: int = VIDEO_FETCH_COUNT) -> list[dict]:
    """
    使用 yt-dlp 获取频道最新 N 条视频的元信息。

    【Bug 修复说明】
    原代码使用 --get-id / --get-title / --get-description，
    这些参数在 yt-dlp 新版本中已废弃且输出顺序不固定；
    更严重的是：视频描述中包含换行符时，split('\\n') 会打乱 title/id/desc 的对应关系。

    修复方案：改用 -j（JSON 输出），yt-dlp 对每条视频输出一行标准 JSON，
    用 json.loads() 解析，字段访问精确可靠，不受换行符影响。

    参数：
        channel_url —— 频道的 /videos 页 URL
        count       —— 拉取条数（对应 --playlist-items 1-N）

    返回：
        list of dict，每项包含 id / title / description 字段
        发生错误时返回空列表
    """
    cmd = [
        "yt-dlp",
        *get_cookie_args(),
        # Cookie 参数：优先从浏览器实时读取，兜底使用静态文件
        "--playlist-items", f"1-{count}",  # 只取前 N 条，节省请求时间
        "-j",                           # JSON 模式：每条视频输出一行 JSON
        "--no-warnings",                # 抑制 yt-dlp 的警告输出，保持日志整洁
        "--quiet",                      # 不打印下载进度等无关信息
        "--flat-playlist",              # 只获取元信息，不实际下载视频流（速度快很多）
        channel_url,
    ]

    try:
        _r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if _r.returncode != 0:
            if _r.stderr:
                for _line in _r.stderr.strip().splitlines()[:6]:
                    log.error(f"  [yt-dlp] {_line}")
            raise subprocess.CalledProcessError(_r.returncode, cmd)
        raw = _r.stdout

        videos = []
        for line in raw.strip().splitlines():
            # splitlines()：按换行符分割，每行是一个完整的 JSON 对象
            if not line.strip():
                continue
            try:
                data = json.loads(line)
                live_status = data.get("live_status", "")
                # live_status 字段由 yt-dlp 填充，常见值：
                #   ""             → 普通视频或 Shorts（未涉及直播）
                #   "is_upcoming"  → 预告/Premiere，尚未发布
                #   "is_live"      → 直播进行中（无法下载完整录像，需等结束）
                #   "was_live"     → 直播已结束，可正常下载完整录像存档
                #   "post_live_dvr"→ 直播刚结束，录像正在处理（暂时跳过）

                duration = data.get("duration") or 0
                # duration：视频时长（秒）。yt-dlp 对 Shorts 有时不返回此字段，
                # 用 or 0 确保为数值而非 None，防止后续比较报 TypeError

                videos.append({
                    "id":              data.get("id", ""),
                    "title":           data.get("title", ""),
                    "desc":            data.get("description", ""),
                    "duration":        duration,
                    # 视频时长（秒），Shorts 通常 ≤60s，可作辅助判断
                    "is_premiere":     live_status == "is_upcoming",
                    # True = 尚未发布的预告，跳过并等待正式发布
                    "is_live_now":     live_status == "is_live",
                    # True = 直播进行中，录像尚不完整，跳过等直播结束
                    "is_live_archive": live_status in ("was_live", "post_live_dvr"),
                    # True = 直播已结束，录像可以完整下载
                    # post_live_dvr 是直播刚结束时的过渡状态，通常也可正常下载
                })
            except json.JSONDecodeError:
                continue
                # 个别行解析失败时跳过，不影响其他视频

        return videos

    except subprocess.CalledProcessError as e:
        log.error(f"  yt-dlp 获取视频列表失败: {e}")
        return []


def fetch_full_description(video_id: str) -> str:
    """
    单独获取某个视频的完整简介。

    【为什么需要此函数】
    get_channel_videos() 使用 --flat-playlist 快速扫描播放列表，
    但该模式下 yt-dlp 只从播放列表页面读取基本元信息，
    description 字段仅包含前几行（YouTube 在列表页截断了简介）。

    本函数不使用 --flat-playlist，直接访问单个视频页面，
    可获取 description 字段的完整内容。

    参数：
        video_id —— YouTube 视频 ID，如 "bBG514O08fk"

    返回：
        完整简介字符串；获取失败时返回空字符串
    """
    cmd = [
        "yt-dlp",
        *get_cookie_args(),
        "-j",                    # JSON 输出，包含完整 description 字段
        "--no-playlist",         # 只处理单个视频，不展开播放列表
        "--skip-download",       # 只获取元信息，不下载视频流
        "--no-warnings",
        "--quiet",
        f"https://www.youtube.com/watch?v={video_id}",
    ]
    FETCH_DESC_TIMEOUT = 60
    # 简介获取的最大等待时间（秒）
    # 下载视频后立即请求元数据容易触发 YouTube 限速，超时则直接用截断简介兜底
    # 60 秒已足够正常网络环境下的响应，如需调大可修改此值

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=FETCH_DESC_TIMEOUT,
            # timeout：超过此秒数未返回则强制终止子进程，防止无限卡死
        )
        if result.returncode != 0:
            log.warning(f"  获取简介 yt-dlp 返回错误码 {result.returncode}，将使用截断简介")
            return ""
        data = json.loads(result.stdout.strip())
        raw_desc = data.get("description", "")
        return truncate_desc(raw_desc)
        # truncate_desc()：按配置的等号分隔线和关键词规则裁剪无关末尾内容
    except subprocess.TimeoutExpired:
        log.warning(f"  获取完整简介超时（>{FETCH_DESC_TIMEOUT}s）[{video_id}]，将使用截断简介")
        return ""
    except Exception as e:
        log.warning(f"  获取完整简介失败 [{video_id}]: {e}，将使用截断简介")
        return ""


def download_video(
    video_id: str,
    save_dir: str,
    title: str,
    desc: str,
    label: str = "视频",
) -> bool:
    """
    下载指定 ID 的 YouTube 视频，并在同目录写入同名 .txt 元数据文件。
    此函数被普通视频、Shorts、直播录像三类内容共用，通过 label 参数区分日志。

    【Bug 修复说明 —— 无声音问题】
    原代码格式字符串 "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best" 存在两个问题：
      1. 缺少 --merge-output-format mp4：合并后的容器格式未指定，
         yt-dlp 默认生成 .mkv 而非 .mp4，且部分情况合并失败后静默跳过音轨。
      2. 回退格式 "best" 在某些视频中只包含视频流（无音轨的最高画质流）。

    修复方案：
      - 使用三级回退链，确保有声：
          ① bestvideo[ext=mp4]+bestaudio[ext=m4a]  最优：mp4画质 + m4a音频
          ② bestvideo+bestaudio                    次优：最高画质（任意格式）+ 最高音质
          ③ best                                   保底：单一流（通常含音频）
      - 强制加 --merge-output-format mp4，确保合并结果为 mp4 容器。

    参数：
        video_id  —— YouTube 视频 ID，如 "bBG514O08fk"
        save_dir  —— 保存目录（频道子目录），如 "./downloads/videos/imas-official"
        title     —— 视频标题（写入 .txt 第一行，同时用于日志）
        desc      —— 视频简介（写入 .txt 第二行起）
        label     —— 日志中显示的类型名称，默认"视频"，可传入"Shorts"或"直播录像"

    返回：
        True = 下载成功；False = 失败

    输出文件（保存到 save_dir）：
        {video_id}.mp4  —— 视频本体
        {video_id}.jpg  —— 封面图（--write-thumbnail 自动生成，供 B站上传时使用）
        {video_id}.txt  —— 标题和简介元数据
    """
    os.makedirs(save_dir, exist_ok=True)
    # exist_ok=True：目录已存在时不报错（幂等）

    output_tmpl = os.path.join(save_dir, f"{video_id}.%(ext)s")
    # %(ext)s：yt-dlp 占位符，自动替换为实际扩展名
    # 固定以 video_id 命名，便于后续通过 ID 查找文件

    cmd = [
        "yt-dlp",
        *get_cookie_args(),
        "-f", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best",
        # 格式回退链（详见上方 Bug 修复说明）
        "--merge-output-format", "mp4",
        # 强制将合并后的视频封装为 mp4 容器（修复无声 bug 的关键）
        "--no-playlist",
        # 明确声明只下载单个视频，防止误触发播放列表下载
        "--write-thumbnail",
        # 下载视频的同时保存缩略图（封面图）
        # yt-dlp 会选取该视频所有可用缩略图中分辨率最高的一张
        "--convert-thumbnails", "jpg",
        # 将缩略图统一转换为 jpg 格式（YouTube 封面有时为 webp，B站上传只接受 jpg/png）
        # 转换需要系统安装了 ffmpeg；若未安装则 yt-dlp 跳过转换，保留原格式
        "-o", output_tmpl,
        # 输出模板：视频保存为 {video_id}.mp4，封面自动保存为 {video_id}.jpg
        # yt-dlp 的 --write-thumbnail 使用与视频相同的输出模板，只替换扩展名
        f"https://www.youtube.com/watch?v={video_id}",
    ]

    try:
        log.info(f"  开始下载{label}: [{video_id}] {title}")
        subprocess.run(cmd, check=True)
        # check=True：yt-dlp 以非零退出码退出时抛出 CalledProcessError

    except subprocess.CalledProcessError as e:
        log.error(f"  {label}下载失败 [{video_id}]: {e}")
        return False

    # ── 确认封面文件 ──────────────────────────────────────────────────────────
    cover_path = os.path.join(save_dir, f"{video_id}.jpg")
    if os.path.exists(cover_path):
        log.info(f"  封面已保存: {video_id}.jpg")
    else:
        # 封面未生成的可能原因：
        #   1. ffmpeg 未安装（webp → jpg 转换失败，yt-dlp 跳过保存）
        #   2. 该视频无可用缩略图（极少见）
        #   3. yt-dlp 版本较旧，--convert-thumbnails 行为不同
        # 不影响视频下载本身，B站上传时 cover="" 会触发自动截帧兜底
        log.warning(f"  封面未生成（可能需要安装 ffmpeg）: {video_id}.jpg")

    # ── 写入同名 .txt 元数据文件 ──────────────────────────────────────────────
    # 重新获取完整简介：--flat-playlist 模式下 description 只有前几行，
    # 此处单独请求视频页面以获取未截断的完整内容。
    full_desc = fetch_full_description(video_id)
    if full_desc:
        desc = full_desc
        # 成功获取完整简介，已在 fetch_full_description 内完成截断处理
    else:
        desc = truncate_desc(desc)
        # 兜底简介（来自 --flat-playlist 的截断版）也过一遍截断规则

    txt_path = os.path.join(save_dir, f"{video_id}.txt")
    try:
        with open(txt_path, "w", encoding="utf-8") as f:
            f.write(title + "\n")
            # 第一行：视频标题（bili_auto_uploader.py 读取此行作为 B站标题）

            if desc:
                f.write(desc)
                # 第二行起：视频完整简介（bili_auto_uploader.py 读取此部分作为 B站简介）
        log.info(f"  已保存元数据: {video_id}.txt")

    except Exception as e:
        log.warning(f"  元数据写入失败 [{video_id}]: {e}")
        # 写入失败不影响视频文件，仅打印警告

    return True


# =============================================================================
# ⑦ 社区动态抓取（playwright）
# =============================================================================

async def fetch_community_posts(posts_url: str, count: int = POST_FETCH_COUNT) -> list[dict]:
    """
    使用 playwright 无头浏览器抓取 YouTube 频道社区动态。

    为什么需要 playwright：
        YouTube 社区页面完全由 JavaScript 动态渲染，直接用 requests 只能获取空白页面，
        必须使用真实（或无头）浏览器执行 JS 后才能看到帖子内容。

    抓取内容：
        - 动态 ID（从发布时间链接的 href 中提取）
        - 动态文本内容
        - 图片 URL 列表（单图和多图均支持）

    参数：
        posts_url —— 频道的 /posts 页 URL
        count     —— 最多抓取的动态条数

    返回：
        list of dict，每项包含：
            id         —— 动态 ID 字符串，如 "UgxXXXXXXXXXX"
            text       —— 动态纯文本内容
            image_urls —— 图片 URL 列表（无图时为空列表）
    """
    posts = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            # headless=True：不显示浏览器窗口（服务器/后台运行）
            # 调试时可改为 False 查看浏览器实际行为
        )

        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            # 自定义 User-Agent，减少被 YouTube 识别为爬虫的概率
            locale="zh-CN",
            # 设置浏览器语言为中文，避免 YouTube 返回英文界面干扰选择器
        )

        page = await context.new_page()

        try:
            await page.goto(posts_url, wait_until="networkidle", timeout=30000)
            # wait_until="networkidle"：等待页面网络请求基本停止（动态内容加载完毕）
            # timeout=30000：超过 30 秒则放弃（防止卡死）

            # 等待第一个动态帖子出现，确认页面已渲染
            await page.wait_for_selector(
                "ytd-backstage-post-thread-renderer",
                timeout=15000
            )

            # ── 滚动加载更多动态 ──────────────────────────────────────────────
            # YouTube 社区页面采用懒加载，需要滚动到底部才会加载更多帖子
            loaded = 0
            max_scrolls = count * 2
            # 最多滚动 count*2 次：每次滚动不一定加载一条帖子，留有余量

            for _ in range(max_scrolls):
                elements = await page.query_selector_all("ytd-backstage-post-thread-renderer")
                loaded = len(elements)
                if loaded >= count:
                    break
                    # 已加载足够数量，停止滚动

                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                # 滚动到页面底部，触发懒加载

                await asyncio.sleep(1.5)
                # 等待 1.5 秒让新内容渲染完毕（太短可能还未加载）

            # ── 逐条解析动态 ────────────────────────────────────────────────
            elements = await page.query_selector_all("ytd-backstage-post-thread-renderer")

            for idx, element in enumerate(elements[:count]):
                # 只处理前 count 条，多余的忽略

                # --- 获取动态 ID（多选择器回退策略）---
                # YouTube 会不定期修改页面结构，单一选择器极易失效。
                # 使用多种方式按优先级依次尝试，直到提取到有效 ID 为止。
                post_id = None

                # 策略 1：优先查找 href 包含 "/post/" 的任意链接
                # 这是最稳健的方式，不依赖具体元素 ID
                post_link = await element.query_selector("a[href*='/post/']")
                if post_link:
                    href = await post_link.get_attribute("href")
                    if href and "/post/" in href:
                        # href 示例：/post/UgkxXXXXXXXXXXXX
                        post_id = href.split("/post/")[-1].split("?")[0].strip()

                # 策略 2：旧版选择器 a#published-time-text（部分旧页面仍有效）
                if not post_id:
                    link_elem = await element.query_selector("a#published-time-text")
                    if link_elem:
                        href = await link_elem.get_attribute("href")
                        if href:
                            # href 格式 1：/post/UgxXXXXX → 取最后一段
                            # href 格式 2：/@channel?show_discussion=UgxXXXXX → 取问号前
                            post_id = href.split("/")[-1].split("?")[0].strip()

                # 策略 3：从页面的 ytd-backstage-post-renderer 的 id 属性提取
                if not post_id:
                    inner = await element.query_selector("ytd-backstage-post-renderer")
                    if inner:
                        attr_id = await inner.get_attribute("id")
                        if attr_id:
                            post_id = attr_id.strip()

                # 策略 4：从 share 按钮的 data 属性中提取
                if not post_id:
                    share_btn = await element.query_selector("button[aria-label*='share'], button[aria-label*='共有'], yt-button-shape button")
                    if share_btn:
                        for attr in ("data-post-id", "data-id"):
                            val = await share_btn.get_attribute(attr)
                            if val:
                                post_id = val.strip()
                                break

                # 如果所有策略都失败，用时间戳生成备用 ID（确保不丢失帖子内容）
                if not post_id or not post_id.startswith("Ugk"):
                    # Ugk 是 YouTube 社区帖子 ID 的标准前缀
                    # 如果提取结果不以 Ugk 开头，说明可能提取到了错误的字符串
                    post_id = f"unknown_{int(time.time())}_{idx}"

                # --- 获取动态文本 ---
                text_elem = await element.query_selector("#content-text")
                text = ""
                if text_elem:
                    text = await text_elem.inner_text()
                    text = text.strip()
                    # .strip() 去除首尾空白行

                # --- 获取图片 URL ---
                image_urls = []

                # 尝试多个图片选择器，兼容单图和多图布局
                img_selectors = [
                    "ytd-backstage-image-renderer img",
                    # 单张图片的标准选择器

                    "ytd-post-multi-image-renderer img",
                    # 多图帖子（九宫格布局）

                    "yt-img-shadow img",
                    # 备用选择器，某些版本 YouTube 使用此结构
                ]

                for selector in img_selectors:
                    imgs = await element.query_selector_all(selector)
                    for img in imgs:
                        src = await img.get_attribute("src")
                        # src：图片当前 URL（通常为缩略图低分辨率版本）

                        if not src:
                            src = await img.get_attribute("data-src")
                            # data-src：懒加载图片的真实 URL 有时存在此属性

                        if src and src.startswith("http") and src not in image_urls:
                            image_urls.append(src)
                            # 去重：同一张图可能被多个选择器匹配到

                    if image_urls:
                        break
                        # 找到图片后不再尝试其他选择器

                posts.append({
                    "id":         post_id,
                    "text":       text,
                    "image_urls": image_urls,
                })

        except Exception as e:
            log.error(f"  playwright 抓取动态失败: {e}")
            # 不重新抛出：返回已抓取到的部分结果（可能为空列表）

        finally:
            await browser.close()
            # 无论成功与否都关闭浏览器，释放内存和进程

    return posts


async def save_post(post: dict, save_dir: str):
    """
    将一条社区动态保存到本地：
        - 文本写入 {动态ID}.txt
        - 图片（若有）下载到 {动态ID}/ 子目录，命名为 01.jpg / 02.jpg ...

    参数：
        post     —— fetch_community_posts() 返回的单条动态字典
        save_dir —— 频道动态的保存根目录，如 "./downloads/posts/imas-official"
    """
    os.makedirs(save_dir, exist_ok=True)

    post_id = post["id"]
    text    = post["text"]
    images  = post["image_urls"]

    # ── 写入文本文件 ──────────────────────────────────────────────────────────
    txt_path = os.path.join(save_dir, f"{post_id}.txt")
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(text)
        # 写入动态全文（纯文本，保留换行结构）

    log.info(f"  已保存动态: {post_id}.txt（{len(text)} 字）")

    # ── 下载图片（若有）────────────────────────────────────────────────────────
    if images:
        img_dir = os.path.join(save_dir, post_id)
        os.makedirs(img_dir, exist_ok=True)
        # 新建与动态 ID 同名的子目录存放所有图片

        for idx, url in enumerate(images, start=1):
            # idx 从 1 开始，生成 01 / 02 / ... 的零填充编号

            # 尝试从 URL 中判断图片格式（jpg/png/webp）
            ext = "jpg"  # 默认扩展名
            for fmt in ("png", "webp", "gif"):
                if fmt in url.lower():
                    ext = fmt
                    break

            img_filename = f"{idx:02d}.{ext}"
            # 零填充两位数编号，确保文件按序排列（01, 02, ... 10, 11...）

            img_path = os.path.join(img_dir, img_filename)

            # 在异步上下文中运行同步的 download_image（避免阻塞事件循环）
            success = await asyncio.to_thread(download_image, url, img_path)
            # asyncio.to_thread()：将同步函数放入线程池执行，不阻塞当前协程

            if success:
                log.info(f"    图片 {idx}/{len(images)} 已保存: {img_filename}")


# =============================================================================
# ⑧ 单频道处理逻辑
# =============================================================================

async def process_channel(channel: dict, seen: dict):
    """
    对一个频道执行完整的检查 → 下载流程：
        1. 若配置了 videos URL，检查新视频并下载
        2. 若配置了 posts URL，检查新动态并保存

    参数：
        channel —— CHANNELS 列表中的一项配置字典
        seen    —— 当前的已处理 ID 字典（会被就地修改，调用方负责持久化）
    """
    name = channel["name"]
    log.info(f"▶ 开始处理频道: {name}")


    # ── 处理视频 ──────────────────────────────────────────────────────────────
    if channel.get("videos"):
        log.info(f"  [视频] 正在获取最新 {VIDEO_FETCH_COUNT} 条视频信息…")

        videos = get_channel_videos(channel["videos"])
        # 调用修复后的 yt-dlp JSON 模式获取视频元信息

        seen_video_ids = get_seen(seen, name, "video_ids")
        # 取出该频道已处理的视频 ID 集合，O(1) 查找

        new_count    = 0   # 本轮成功下载数
        skipped_seen = 0   # 因已在 seen_ids 中而跳过的数量
        skipped_pre  = 0   # 因是未发布预告视频而跳过的数量
        failed_count = 0   # 下载失败数

        for video in videos:
            vid = video["id"]

            if not vid:
                continue
                # 无效 ID（yt-dlp 返回空字符串），跳过

            if vid in seen_video_ids:
                skipped_seen += 1
                continue
                # 已在 seen_ids 中：之前已下载过，跳过

            if video.get("is_premiere"):
                # 预告视频（Premieres in X days）：尚未发布，跳过但不标记为已见
                log.info(f"  跳过预告视频（尚未发布）: [{vid}] {video['title']}")
                skipped_pre += 1
                continue

            if video.get("is_live_now"):
                # 首发播放进行中（Premiere 正在直播）：live_status == "is_live"
                # 此时视频以 HLS 直播流形式存在，下载到的是不完整片段，
                # 跳过并不标记已见，等首发结束后 live_status 变为 was_live 再下载完整版
                log.info(f"  跳过首发播放进行中的视频（等结束后再下载）: [{vid}] {video['title']}")
                skipped_pre += 1
                continue

            video_save_dir = os.path.join(VIDEOS_FOLDER, name)
            # 视频保存路径：VIDEOS_FOLDER / 频道名 / 视频ID.mp4

            success = download_video(
                video_id = vid,
                save_dir = video_save_dir,
                title    = video["title"],
                desc     = video["desc"],
            )

            if success:
                mark_seen(seen, name, "video_ids", vid)
                # 下载成功后立即标记为已见，防止下一轮重复下载
                new_count += 1
            else:
                failed_count += 1
                # 下载失败：不标记为已见，下一轮会重试

        # 【修复3】区分"已处理跳过"、"预告跳过"和"失败"，日志更清晰
        log.info(
            f"  [视频] 新下载 {new_count} 条 | "
            f"已处理跳过 {skipped_seen} 条 | "
            f"预告跳过 {skipped_pre} 条 | "
            f"失败 {failed_count} 条"
        )


    # ── 处理 Shorts ──────────────────────────────────────────────────────────
    if channel.get("shorts"):
        log.info(f"  [Shorts] 正在获取最新 {SHORTS_FETCH_COUNT} 条 Shorts 信息…")

        shorts = get_channel_videos(channel["shorts"], count=SHORTS_FETCH_COUNT)
        # 复用 get_channel_videos()：传入 /shorts 页 URL 即可，无需单独的抓取函数
        # yt-dlp 对 Shorts 页面的处理方式与 /videos 完全相同

        seen_short_ids = get_seen(seen, name, "short_ids")
        # 使用独立的 short_ids 键，与普通视频 video_ids 互不干扰
        # 同一个视频在 /videos 和 /shorts 中可能都出现，分开记录避免混淆

        new_count    = 0
        skipped_seen = 0
        skipped_pre  = 0
        failed_count = 0

        for short in shorts:
            sid = short["id"]

            if not sid:
                continue

            if sid in seen_short_ids:
                skipped_seen += 1
                continue

            if short.get("is_premiere"):
                # Shorts 通常不会有预告状态，但以防万一做同样处理
                log.info(f"  跳过预告 Shorts（尚未发布）: [{sid}] {short['title']}")
                skipped_pre += 1
                continue

            if short.get("is_live_now"):
                # Shorts 不应有直播状态，异常情况直接跳过
                log.info(f"  跳过直播中 Shorts: [{sid}] {short['title']}")
                skipped_pre += 1
                continue

            shorts_save_dir = os.path.join(SHORTS_FOLDER, name)
            # 保存到独立的 SHORTS_FOLDER，与普通视频分开存放

            success = download_video(
                video_id = sid,
                save_dir = shorts_save_dir,
                title    = short["title"],
                desc     = short["desc"],
                label    = "Shorts",
                # label 参数仅用于日志显示，让日志中明确标注类型
            )

            if success:
                mark_seen(seen, name, "short_ids", sid)
                new_count += 1
            else:
                failed_count += 1

        log.info(
            f"  [Shorts] 新下载 {new_count} 条 | "
            f"已处理跳过 {skipped_seen} 条 | "
            f"预告跳过 {skipped_pre} 条 | "
            f"失败 {failed_count} 条"
        )


    # ── 处理直播录像 ──────────────────────────────────────────────────────────
    if channel.get("streams"):
        log.info(f"  [直播] 正在获取最新 {STREAMS_FETCH_COUNT} 条直播信息…")

        streams = get_channel_videos(channel["streams"], count=STREAMS_FETCH_COUNT)
        # 复用 get_channel_videos()：传入 /streams 页 URL
        # yt-dlp 返回的每条记录中 live_status 会是 was_live / is_live / is_upcoming

        seen_stream_ids = get_seen(seen, name, "stream_ids")
        # 独立的 stream_ids 键

        new_count    = 0
        skipped_seen = 0
        skipped_live = 0   # 直播进行中，等结束后再下载
        skipped_pre  = 0   # 预定但未开始的直播
        failed_count = 0

        for stream in streams:
            vid = stream["id"]

            if not vid:
                continue

            if vid in seen_stream_ids:
                skipped_seen += 1
                continue

            if stream.get("is_premiere"):
                # 预定但尚未开始的直播（is_upcoming），跳过等开播后再处理
                log.info(f"  跳过预定直播（尚未开始）: [{vid}] {stream['title']}")
                skipped_pre += 1
                continue

            if stream.get("is_live_now"):
                # 直播进行中：录像尚不完整，本轮跳过。
                # 不标记为已见，等直播结束后下一轮检查时 live_status 会变为 was_live，
                # 届时自动进入下载流程。
                log.info(f"  直播进行中，本轮跳过（等结束后下载录像）: [{vid}] {stream['title']}")
                skipped_live += 1
                continue

            if not stream.get("is_live_archive"):
                # 既不是进行中也不是存档的奇怪状态（理论上不应出现），跳过
                log.warning(f"  未知直播状态，跳过: [{vid}] live_status 不明")
                skipped_pre += 1
                continue

            # is_live_archive = True：直播已结束，录像可以完整下载
            streams_save_dir = os.path.join(STREAMS_FOLDER, name)

            success = download_video(
                video_id = vid,
                save_dir = streams_save_dir,
                title    = stream["title"],
                desc     = stream["desc"],
                label    = "直播录像",
            )

            if success:
                mark_seen(seen, name, "stream_ids", vid)
                new_count += 1
            else:
                failed_count += 1

        log.info(
            f"  [直播] 新下载 {new_count} 条 | "
            f"已处理跳过 {skipped_seen} 条 | "
            f"直播中跳过 {skipped_live} 条 | "
            f"预定跳过 {skipped_pre} 条 | "
            f"失败 {failed_count} 条"
        )


    # ── 处理社区动态 ──────────────────────────────────────────────────────────
    if channel.get("posts"):
        log.info(f"  [动态] 正在抓取最新 {POST_FETCH_COUNT} 条社区动态…")

        posts = await fetch_community_posts(channel["posts"])
        # 调用 playwright 抓取动态列表

        seen_post_ids = get_seen(seen, name, "post_ids")

        new_count       = 0  # 本轮成功保存数
        skip_seen_count = 0  # 因已在 seen_ids 中而跳过
        skip_bad_id     = 0  # 因 ID 提取失败（unknown_）而跳过
        post_fail_count = 0  # 保存失败数

        for post in posts:
            pid = post["id"]

            if not pid:
                skip_bad_id += 1
                continue
                # 空 ID，跳过

            if pid.startswith("unknown_"):
                # ID 提取失败（playwright 未能找到 /post/ 链接）
                # 仍然保存内容，用备用 ID 作为文件名，不丢失帖子文本
                log.warning(f"  动态 ID 提取失败，使用备用 ID 保存: {pid[:30]}…")
                # 注意：备用 ID 不存入 seen_ids，下次运行会重新尝试提取
                posts_save_dir = os.path.join(POSTS_FOLDER, name)
                try:
                    await save_post(post, posts_save_dir)
                    new_count += 1
                except Exception as e:
                    log.error(f"  动态保存失败: {e}")
                    post_fail_count += 1
                continue

            if pid in seen_post_ids:
                skip_seen_count += 1
                continue
                # 已处理过的帖子，跳过

            posts_save_dir = os.path.join(POSTS_FOLDER, name)
            # 动态保存路径：POSTS_FOLDER / 频道名 /

            try:
                await save_post(post, posts_save_dir)
                # 写入文本并下载图片

                mark_seen(seen, name, "post_ids", pid)
                # 保存成功后标记为已见

                new_count += 1
            except Exception as e:
                log.error(f"  动态保存失败 [{pid}]: {e}")
                post_fail_count += 1

        # 修正日志：清楚区分各种跳过原因，不再把"ID提取失败"混入"已处理"
        log.info(
            f"  [动态] 新保存 {new_count} 条 | "
            f"已处理跳过 {skip_seen_count} 条 | "
            f"ID提取失败 {skip_bad_id} 条 | "
            f"失败 {post_fail_count} 条"
        )

    log.info(f"✓ 频道 {name} 处理完毕")


# =============================================================================
# ⑨ 主函数 —— 轮询循环
# =============================================================================

async def main():
    """
    程序主循环：
        每隔 CHECK_INTERVAL 秒遍历所有频道，检查并下载新内容。
        Ctrl+C 优雅退出。
    """
    log.info("=" * 60)
    log.info("YouTube 频道监控启动")
    log.info(f"监控频道数：{len(CHANNELS)}")
    log.info(f"普通视频目录：{VIDEOS_FOLDER}")
    log.info(f"Shorts 目录：{SHORTS_FOLDER}")
    log.info(f"直播录像目录：{STREAMS_FOLDER}")
    log.info(f"动态保存目录：{POSTS_FOLDER}")
    _csrc = f"浏览器实时（{COOKIE_FROM_BROWSER}）" if COOKIE_FROM_BROWSER else f"静态文件：{COOKIES_FILE}"
    log.info(f"Cookie 来源：{_csrc}")
    log.info(f"检查间隔：{CHECK_INTERVAL} 秒")
    log.info("=" * 60)

    # ── 启动检查：确认 Cookie 来源 ────────────────────────────────────────────
    if COOKIE_FROM_BROWSER:
        log.info(f"Cookie 模式：从浏览器实时读取（{COOKIE_FROM_BROWSER}），失败时自动降级")
    elif os.path.exists(COOKIES_FILE):
        cookie_size = os.path.getsize(COOKIES_FILE)
        if cookie_size < 200:
            log.warning(f"【警告】Cookie 文件异常偏小（{cookie_size} 字节），请确认格式")
        else:
            log.info(f"Cookie 模式：静态文件（{cookie_size} 字节）")
    else:
        log.error("【错误】未配置任何 Cookie 来源，请在配置区设置 COOKIE_FROM_BROWSER 或放置静态文件")
        return

    # ── 提示：如需重新下载已处理过的内容，删除 seen_ids.json 即可 ─────────────
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE, encoding="utf-8") as f:
            seen_preview = json.load(f)
        total_seen = sum(
            len(v.get("video_ids",  [])) +
            len(v.get("short_ids",  [])) +
            len(v.get("stream_ids", [])) +
            len(v.get("post_ids",   []))
            for v in seen_preview.values()
        )
        log.info(f"状态文件已加载，共记录 {total_seen} 条已处理 ID")
        log.info(f"（如需重新下载所有内容，请删除：{SEEN_FILE}）")
    else:
        log.info("首次运行，尚无历史记录")

    # 确保根目录存在（各频道子目录在下载时按需创建）
    os.makedirs(VIDEOS_FOLDER,  exist_ok=True)
    os.makedirs(SHORTS_FOLDER,  exist_ok=True)
    os.makedirs(STREAMS_FOLDER, exist_ok=True)
    os.makedirs(POSTS_FOLDER,   exist_ok=True)

    # ── 单轮检查（循环由 main.py 控制，此处只执行一次）──────────────────────
    seen = load_seen_ids()

    round_start = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log.info(f"\n{'─'*50}")
    log.info(f"开始检查 @ {round_start}")

    for channel in CHANNELS:
        try:
            jitter_time = random.uniform(REQ_JITTER_MIN, REQ_JITTER_MAX)
            await asyncio.sleep(jitter_time)
            await process_channel(channel, seen)
            save_seen_ids(seen)

            total_ids = sum(
                len(v.get("video_ids",  [])) +
                len(v.get("short_ids",  [])) +
                len(v.get("stream_ids", [])) +
                len(v.get("post_ids",   []))
                for v in seen.values()
            )
            log.info(f"  [状态] seen_ids.json 已更新，当前共记录 {total_ids} 条 ID → {SEEN_FILE}")

        except Exception as e:
            log.error(f"处理频道 {channel['name']} 时发生未预期错误: {e}", exc_info=True)

    log.info(f"本轮检查完成")
    log.info(f"{'─'*50}\n")


# =============================================================================
# ⑩ 程序入口
# =============================================================================

if __name__ == "__main__":
    try:
        asyncio.run(main())
        # asyncio.run()：创建新事件循环并运行 main() 协程，直到完成或异常

    except KeyboardInterrupt:
        log.info("\n收到 Ctrl+C，程序退出。")
        # 捕获 Ctrl+C，打印退出提示后正常结束（不打印错误堆栈）

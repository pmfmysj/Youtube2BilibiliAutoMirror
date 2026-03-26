# =============================================================================
# post_uploader.py —— B站动态自动发布脚本
#
# 功能：
#   扫描 downloads/posts/ 下所有已翻译的 .txt 文件，
#   逐条发布为 B站图文动态（有图则附图，无图则纯文字），
#   发布成功后删除 .txt 和同名图片目录。
#
# 文件结构（由 youtube_watcher.py + translator.py 生成）：
#   downloads/posts/
#       频道名/
#           动态ID.txt      ← 已翻译（含 ================原文================ 分隔线）
#           动态ID/         ← 可选，有图片时存在
#               01.jpg
#               02.jpg
#
# .txt 格式：
#   [翻译后正文]
#
#   ================原文================
#   [原始正文]
#
# 判断"已翻译"的依据：文件中包含分隔线 ================原文================
#
# 依赖安装：
#   pip install bilibili-api-python
#
# 使用前提：
#   已有 bili_credential.json（运行 bili_login.py 扫码生成）
# =============================================================================


# ---------- 标准库导入 ----------
import os
import json
import asyncio
import logging
from pathlib import Path
from datetime import datetime


# ---------- 第三方库导入 ----------
# 切换 bilibili-api-python 的 HTTP 客户端为 httpx。
# 默认的 aiohttp 使用 aiodns 解析 DNS，在 Windows 上与事件循环不兼容，
# 会报 "Could not contact DNS servers"。httpx 使用系统原生 DNS，无此问题。
# 依赖安装：pip install httpx
from bilibili_api import select_client as _select_client
_select_client("httpx")
# select_client() 是官方提供的 HTTP 客户端切换函数，必须在导入其他子模块之前调用

from bilibili_api import dynamic, Credential
from bilibili_api.dynamic import BuildDynamic
from bilibili_api.utils.picture import Picture


# =============================================================================
# ① 配置区
# =============================================================================

SCRIPT_DIR = Path(__file__).parent.resolve()

POSTS_FOLDER    = str(SCRIPT_DIR / "downloads" / "posts")   # 动态文件根目录
CREDENTIAL_FILE = str(SCRIPT_DIR / "bili_credential.json")  # 登录凭证
LOG_FILE        = str(SCRIPT_DIR / "post_upload_log.txt")    # 发布日志
POSTED_FILE     = str(SCRIPT_DIR / "posted_dynamics.json")   # 已发布记录（防重复）

# 两条动态之间的发布间隔（秒）
# B站对动态发布频率有限制，建议不低于 60 秒
POST_INTERVAL = 120

# 分隔线（必须与 translator.py 中的 SEPARATOR 完全一致）
SEPARATOR = "-" * 3 + "以上为AI翻译" + "-" * 3

# B站动态正文最大字符数
BILI_DYNAMIC_MAX = 2000

# 支持的图片格式
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}


# =============================================================================
# ② 日志初始化
# =============================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)


# =============================================================================
# ③ 已发布记录（防止重复发布）
# =============================================================================

def load_posted() -> set:
    """
    读取已发布动态的文件路径集合。
    文件不存在时返回空集合（首次运行）。
    """
    if os.path.exists(POSTED_FILE):
        with open(POSTED_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    return set()


def save_posted(posted: set):
    """将已发布路径集合写回文件。"""
    with open(POSTED_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(posted), f, ensure_ascii=False, indent=2)


# =============================================================================
# ④ 凭证加载
# =============================================================================

def load_credential() -> Credential:
    """
    从 bili_credential.json 读取 B站登录凭证。

    字段说明：
        sessdata      —— 登录 Session（必填）
        bili_jct      —— CSRF Token（必填）
        buvid3        —— 设备指纹（可选）
        dedeuserid    —— 用户 UID（可选）
        ac_time_value —— 凭证刷新字段（可选）
    """
    with open(CREDENTIAL_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    return Credential(
        sessdata      = data["sessdata"],
        bili_jct      = data["bili_jct"],
        buvid3        = data.get("buvid3", "") or "",
        dedeuserid    = data.get("dedeuserid", "") or "",
        ac_time_value = data.get("ac_time_value", "") or "",
    )


# =============================================================================
# ⑤ 元数据解析
# =============================================================================

import re as _url_re_mod
_URL_RE = _url_re_mod.compile(r"https?://\S+")
# 匹配所有 http(s):// 开头直到空白字符的 URL


def trim_to_limit(text: str, limit: int = 990) -> str:
    """
    将文本压缩到 limit 字符以内。
    策略：① 删除全部 URL  ② 仍超限则截断至 limit 字符
    """
    if len(text) <= limit:
        return text

    # 步骤1：删除所有 URL
    text_no_url = _URL_RE.sub("", text)
    # 清理删除 URL 后残留的多余空行
    text_no_url = _url_re_mod.sub(r"\n{3,}", "\n\n", text_no_url).strip()

    if len(text_no_url) <= limit:
        log.info(f"  删除 URL 后从 {len(text)} 字压缩至 {len(text_no_url)} 字")
        return text_no_url

    # 步骤2：删 URL 后仍超限，截断
    truncated = text_no_url[:limit]
    log.warning(f"  删除 URL 后仍超限（{len(text_no_url)} 字），已截断至 {limit} 字")
    return truncated


def parse_post_text(txt_path: Path) -> str | None:
    """
    读取已翻译的动态 .txt 文件，返回译文和原文的完整内容。

    格式（由 translator.py 生成）：
        [译文正文]
        ---以上为AI翻译---
        [原文正文]

    发布内容：译文 + 分隔线 + 原文（截断到 BILI_DYNAMIC_MAX 字符）
    未翻译（不含分隔线）时返回 None，等待 translator.py 处理。

    返回：
        完整文本字符串；未翻译或读取失败返回 None
    """
    try:
        content = txt_path.read_text(encoding="utf-8").strip()
    except Exception as e:
        log.warning(f"  读取文件失败 {txt_path.name}: {e}")
        return None

    if SEPARATOR not in content:
        # 尚未翻译（不含分隔线），跳过等待 translator.py 处理
        return None

    # 验证译文部分不为空
    translated = content.split(SEPARATOR, maxsplit=1)[0].strip()
    if not translated:
        log.warning(f"  译文为空，跳过: {txt_path.name}")
        return None

    # 字数压缩：优先删除 URL，仍超限则截断
    return trim_to_limit(content, limit=1950)


def get_image_paths(txt_path: Path) -> list[Path]:
    """
    查找与 .txt 同名的图片子目录，返回其中所有图片文件的路径列表（按文件名排序）。

    目录结构示例：
        动态ID.txt
        动态ID/
            01.jpg   ← 返回此列表
            02.jpg

    返回：
        图片路径列表（无图片目录时返回空列表）
    """
    img_dir = txt_path.parent / txt_path.stem
    # txt_path.stem：去掉扩展名的文件名，即动态 ID

    if not img_dir.is_dir():
        return []
        # 没有同名目录，说明此动态是纯文字

    images = sorted(
        p for p in img_dir.iterdir()
        if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS
    )
    # sorted()：按文件名排序，确保 01.jpg → 02.jpg → ... 的顺序

    return images


# =============================================================================
# ⑥ 扫描待发布动态
# =============================================================================

def find_pending_posts(posted: set) -> list[Path]:
    """
    扫描 POSTS_FOLDER，找出所有已翻译但尚未发布的 .txt 文件。

    返回：
        按文件修改时间从旧到新排列的待发布 .txt 路径列表
    """
    candidates = []

    if not os.path.exists(POSTS_FOLDER):
        return []

    for root, dirs, files in os.walk(POSTS_FOLDER):
        # 跳过隐藏目录和图片子目录
        # 图片子目录内没有 .txt 文件，os.walk 自然跳过，但显式过滤更安全
        dirs[:] = [d for d in dirs if not d.startswith(".")]

        for filename in files:
            if not filename.endswith(".txt"):
                continue

            abs_path = Path(os.path.abspath(os.path.join(root, filename)))

            if str(abs_path) in posted:
                continue
                # 已发布，跳过

            # 检查是否已翻译（含分隔线）
            try:
                content = abs_path.read_text(encoding="utf-8")
                if SEPARATOR not in content:
                    continue
                    # 尚未翻译，跳过
            except Exception:
                continue

            candidates.append((abs_path.stat().st_mtime, abs_path))

    candidates.sort(key=lambda x: x[0])
    # 按修改时间升序：最旧的动态先发布，保持时间顺序

    return [p for _, p in candidates]


# =============================================================================
# ⑦ 发布单条动态
# =============================================================================

async def publish_post(txt_path: Path, credential: Credential) -> bool:
    """
    将一条动态发布至 B站，发布成功后删除本地文件。

    发布流程：
        ① 读取译文正文
        ② 查找同名图片目录
        ③ 若有图片，逐张上传到 B站图床，获取图片 ID
        ④ 构建 BuildDynamic 对象，附加正文和图片
        ⑤ 调用 dynamic.send_dynamic() 发布
        ⑥ 删除 .txt 和图片目录

    参数：
        txt_path   —— 动态 .txt 文件路径
        credential —— B站登录凭证

    返回：
        True = 发布成功；False = 失败
    """
    # ── ① 读取译文 ────────────────────────────────────────────────────────────
    text = parse_post_text(txt_path)
    if text is None:
        log.error(f"  译文解析失败: {txt_path.name}")
        return False

    # ── ② 查找图片 ────────────────────────────────────────────────────────────
    image_paths = get_image_paths(txt_path)
    log.info(f"  正文: {len(text)} 字  |  图片: {len(image_paths)} 张")

    # ── ③ 上传图片到 B站图床 ─────────────────────────────────────────────────
    uploaded_pics = []
    for i, img_path in enumerate(image_paths, start=1):
        log.info(f"  上传图片 {i}/{len(image_paths)}: {img_path.name}")
        try:
            # 构造 Picture 并填入字节数据
            pic = Picture()
            img_bytes = img_path.read_bytes()
            ext = img_path.suffix.lower().lstrip(".")
            # bilibili-api-python 的 upload_image 实际读取 picture.content
            # 如果 content 属性不可用（BiliAPIFile 问题），使用 url_file 方式兜底
            pic.content = img_bytes           # type: ignore[attr-defined]
            pic.imageType = "jpeg" if ext in ("jpg", "jpeg") else ext   # type: ignore[attr-defined]

            try:
                pic_info = await dynamic.upload_image(
                    image      = pic,
                    credential = credential,
                )
                uploaded_pics.append(pic_info)
            except AttributeError:
                # content/.read() 兼容问题：改用 from_url 方式
                # 先把图片写入临时文件，用 from_file 读取（有时可行）
                import tempfile as _tf
                with _tf.NamedTemporaryFile(suffix=f".{ext}", delete=False) as _tmp:
                    _tmp.write(img_bytes)
                    _tmppath = _tmp.name
                import os as _os2
                try:
                    pic2 = await dynamic.upload_image_file(_tmppath, credential=credential) # type: ignore
                    uploaded_pics.append(pic2)
                except Exception as e2:
                    log.error(f"  图片上传失败 {img_path.name}: {e2}")
                    log.warning("  将以无该图片的形式继续发布")
                finally:
                    _os2.unlink(_tmppath)
        except Exception as e:
            log.error(f"  图片上传失败 {img_path.name}: {e}")
            log.warning("  将以无该图片的形式继续发布")

    # ── ④ 构建动态对象 ───────────────────────────────────────────────────────
    builder = BuildDynamic.empty()
    # BuildDynamic：bilibili-api 的动态构建器，支持链式调用

    builder.add_text(text)
    # add_text()：添加动态正文（支持换行，使用 \n）

    for pic in uploaded_pics:
        builder.add_image(pic)
        # add_image()：附加已上传到图床的图片对象

    # ── ⑤ 发布动态 ───────────────────────────────────────────────────────────
    try:
        # Pylance 根据类型签名提示 send_dynamic 返回非 awaitable，
        # 说明当前安装的版本为同步函数，不需要 await
        import inspect as _inspect
        if _inspect.iscoroutinefunction(dynamic.send_dynamic):
            result = await dynamic.send_dynamic(
                info       = builder,
                credential = credential,
            )
        else:
            result = dynamic.send_dynamic(   # type: ignore[misc]
                info       = builder,
                credential = credential,
            )
        # 兼容两种版本：新版为异步，旧版为同步，运行时自动判断

        dynamic_id: int | str
        if isinstance(result, int):
            dynamic_id = result
        elif isinstance(result, dict):
            dynamic_id = (
                result.get("data", {}).get("dynamic_id", "")
                or result.get("dynamic_id", "未知")
            )
        else:
            dynamic_id = str(result) if result is not None else "未知"

        log.info(f"  ✓ 动态发布成功！dynamic_id: {dynamic_id}")

    except Exception as e:
        log.error(f"  动态发布失败: {e}", exc_info=True)
        return False

    # ── ⑥ 写入发布日志 ──────────────────────────────────────────────────────
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    record = (
        f"[{timestamp}] 发布成功\n"
        f"  文件:     {txt_path.name}\n"
        f"  图片数:   {len(uploaded_pics)}\n"
        f"  dynamic_id: {dynamic_id}\n"
        f"{'─' * 50}\n"
    )
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(record)

    # ── ⑦ 删除本地文件 ──────────────────────────────────────────────────────
    try:
        txt_path.unlink()
        log.info(f"  已删除: {txt_path.name}")
    except Exception as e:
        log.warning(f"  .txt 删除失败（可手动删除）: {e}")

    img_dir = txt_path.parent / txt_path.stem
    if img_dir.is_dir():
        import shutil
        try:
            shutil.rmtree(img_dir)
            # shutil.rmtree()：递归删除整个图片目录
            log.info(f"  已删除图片目录: {img_dir.name}/")
        except Exception as e:
            log.warning(f"  图片目录删除失败（可手动删除）: {e}")

    return True


# =============================================================================
# ⑧ 主函数
# =============================================================================

async def main():
    log.info("=" * 60)
    log.info("B站动态自动发布启动")
    log.info(f"动态目录：{POSTS_FOLDER}")
    log.info(f"发布间隔：{POST_INTERVAL} 秒")
    log.info("=" * 60)

    # ── 启动检查 ──────────────────────────────────────────────────────────────
    if not os.path.exists(CREDENTIAL_FILE):
        log.error(f"【错误】凭证文件不存在：{CREDENTIAL_FILE}")
        log.error("请先运行 bili_login.py 扫码登录")
        return

    try:
        credential = load_credential()
        log.info("凭证加载成功")
    except Exception as e:
        log.error(f"【错误】凭证加载失败：{e}")
        return

    if not os.path.exists(POSTS_FOLDER):
        log.info("动态目录不存在，无需处理")
        return

    # ── 扫描待发布动态 ────────────────────────────────────────────────────────
    posted = load_posted()
    pending = find_pending_posts(posted)

    if not pending:
        log.info("当前无待发布动态（尚未翻译或已全部发布）")
        return

    log.info(f"共找到 {len(pending)} 条待发布动态")

    # ── 逐条发布 ──────────────────────────────────────────────────────────────
    success_count = 0
    fail_count    = 0

    for idx, txt_path in enumerate(pending, start=1):
        try:
            rel = txt_path.relative_to(SCRIPT_DIR)
        except ValueError:
            rel = txt_path

        log.info(f"\n[{idx}/{len(pending)}] {rel}")

        success = await publish_post(txt_path, credential)

        if success:
            posted.add(str(txt_path))
            save_posted(posted)
            # 每发布成功一条立即保存进度，防止中途退出丢失记录
            success_count += 1

            # 若还有下一条，等待间隔
            if idx < len(pending):
                log.info(f"等待 {POST_INTERVAL} 秒后发布下一条…")
                await asyncio.sleep(POST_INTERVAL)
        else:
            fail_count += 1
            # 失败的不加入 posted，下次运行会重试

    # ── 汇总 ──────────────────────────────────────────────────────────────────
    log.info("\n" + "=" * 60)
    log.info(f"发布完成：成功 {success_count} 条，失败 {fail_count} 条")
    if fail_count > 0:
        log.info("失败的动态下次运行时将自动重试")
    log.info("=" * 60)


# =============================================================================
# ⑨ 程序入口
# =============================================================================

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("\n收到 Ctrl+C，已停止。")

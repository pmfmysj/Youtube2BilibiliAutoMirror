# =============================================================================
# fetch_covers.py —— 为已下载视频补充封面
#
# 功能：
#   扫描 downloads/ 下三个视频文件夹（videos / shorts / streams），
#   找到有 .mp4 但缺少同名 .jpg 的文件，通过 yt-dlp 单独下载封面。
#
# 使用场景：
#   在 youtube_watcher.py 添加 --write-thumbnail 参数之前已经下载的视频，
#   没有对应的封面文件，运行本脚本可以一次性补全。
#
# 依赖：
#   yt-dlp（命令行工具）
#   ffmpeg（yt-dlp 将 webp 转 jpg 时需要，建议安装）
#
# 文件保存结构（与 youtube_watcher.py 完全一致）：
#   downloads/
#       videos/  shorts/  streams/
#           频道名/
#               视频ID.mp4   ← 已有
#               视频ID.txt   ← 已有
#               视频ID.jpg   ← 本脚本补充
# =============================================================================

import os
import subprocess
import logging
from pathlib import Path


# =============================================================================
# ① 配置区
# =============================================================================

SCRIPT_DIR = Path(__file__).parent.resolve()
# 所有路径相对于本脚本所在目录

VIDEO_FOLDERS = [
    str(SCRIPT_DIR / "downloads" / "videos"),
    str(SCRIPT_DIR / "downloads" / "shorts"),
    str(SCRIPT_DIR / "downloads" / "streams"),
]

COOKIES_FILE = str(SCRIPT_DIR / "www.youtube.com_cookies.txt")
# YouTube Cookie 文件，与 youtube_watcher.py 共用

VIDEO_EXTENSIONS = {".mp4", ".flv", ".mkv", ".avi", ".mov", ".wmv"}


# =============================================================================
# ② 日志
# =============================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(
            str(SCRIPT_DIR / "fetch_covers.log"), encoding="utf-8"
        ),
    ],
)
log = logging.getLogger(__name__)


# =============================================================================
# ③ 封面下载
# =============================================================================

def fetch_cover(video_id: str, save_dir: str) -> bool:
    """
    用 yt-dlp 下载指定视频 ID 的封面，保存为 {video_id}.jpg。

    原理：
        --write-thumbnail          → 只下载缩略图，不下载视频流
        --skip-download            → 明确跳过视频/音频下载
        --convert-thumbnails jpg   → 将 webp 等格式转为 jpg（需要 ffmpeg）
        -o 模板                    → 保存路径与视频文件同目录、同名

    参数：
        video_id —— YouTube 视频 ID，如 "bBG514O08fk"
        save_dir —— 封面保存目录（与对应 .mp4 在同一目录）

    返回：
        True = 成功（.jpg 文件存在）；False = 失败
    """
    output_tmpl = os.path.join(save_dir, f"{video_id}.%(ext)s")
    # yt-dlp 的 --write-thumbnail 使用此模板命名封面文件，
    # 转换为 jpg 后最终保存为 {video_id}.jpg

    cmd = [
        "yt-dlp",
        "--cookies", COOKIES_FILE,
        "--skip-download",
        # 跳过视频/音频流下载，只处理缩略图
        "--write-thumbnail",
        # 保存视频的最高分辨率缩略图
        "--convert-thumbnails", "jpg",
        # 统一转为 jpg 格式（B站封面只接受 jpg/png）
        "--no-playlist",
        "--no-warnings",
        "--quiet",
        "-o", output_tmpl,
        f"https://www.youtube.com/watch?v={video_id}",
    ]

    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as e:
        log.error(f"  yt-dlp 失败 [{video_id}]: {e}")
        return False

    # 验证文件是否真的生成了
    jpg_path = os.path.join(save_dir, f"{video_id}.jpg")
    if os.path.exists(jpg_path):
        size_kb = os.path.getsize(jpg_path) // 1024
        log.info(f"  ✓ 封面已保存: {video_id}.jpg  ({size_kb} KB)")
        return True
    else:
        # ffmpeg 未安装时，yt-dlp 跳过格式转换，可能生成 .webp 而非 .jpg
        webp_path = os.path.join(save_dir, f"{video_id}.webp")
        if os.path.exists(webp_path):
            log.warning(
                f"  封面保存为 .webp 而非 .jpg（请安装 ffmpeg 以自动转换）: "
                f"{video_id}.webp"
            )
            return True
            # webp 也算成功，uploader 里的兜底逻辑会处理
        log.error(f"  封面文件未生成: {video_id}.jpg")
        return False


# =============================================================================
# ④ 主流程
# =============================================================================

def main():
    log.info("=" * 60)
    log.info("封面补全工具启动")
    log.info(f"Cookie 文件: {COOKIES_FILE}")
    log.info("=" * 60)

    if not os.path.exists(COOKIES_FILE):
        log.error(f"Cookie 文件不存在: {COOKIES_FILE}")
        log.error("请先准备好 www.youtube.com_cookies.txt 再运行本脚本")
        return

    # ── 收集需要补封面的视频 ──────────────────────────────────────────────────
    missing: list[tuple[str, str]] = []
    # 每项为 (video_id, save_dir)

    for folder in VIDEO_FOLDERS:
        if not os.path.exists(folder):
            continue

        for root, _dirs, files in os.walk(folder):
            for filename in files:
                p = Path(filename)
                if p.suffix.lower() not in VIDEO_EXTENSIONS:
                    continue

                video_id = p.stem
                jpg_path = os.path.join(root, f"{video_id}.jpg")
                webp_path = os.path.join(root, f"{video_id}.webp")

                if os.path.exists(jpg_path) or os.path.exists(webp_path):
                    continue
                    # 封面已存在，跳过

                missing.append((video_id, root))

    if not missing:
        log.info("所有视频均已有封面文件，无需处理。")
        return

    log.info(f"共找到 {len(missing)} 个缺少封面的视频，开始下载…\n")

    success = 0
    failed = 0

    for idx, (video_id, save_dir) in enumerate(missing, start=1):
        rel = os.path.relpath(save_dir, SCRIPT_DIR)
        log.info(f"[{idx}/{len(missing)}] {video_id}  ({rel})")

        if fetch_cover(video_id, save_dir):
            success += 1
        else:
            failed += 1

    log.info("\n" + "=" * 60)
    log.info(f"完成：成功 {success} 个，失败 {failed} 个")
    if failed:
        log.info("失败的封面可重新运行本脚本重试")
    log.info("=" * 60)


if __name__ == "__main__":
    main()

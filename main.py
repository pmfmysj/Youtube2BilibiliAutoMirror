# =============================================================================
# main.py —— 自动化流水线总调度
#
# 通过 subprocess 按顺序调用三个脚本，形成流水线：
#
#   ① youtube_watcher.py  —— 检查并下载新视频/动态（单次执行）
#   ② translator.py       —— 翻译所有未翻译的 .txt 文件（单次执行）
#   ③ bili_scheduler_uploader.py —— 上传所有已翻译视频后退出
#
# 三个脚本本身不再包含等待循环，循环由本文件统一控制。
#
# 使用：
#   python main.py
# =============================================================================

import subprocess
import sys
import time
import logging
import random
from pathlib import Path
from datetime import datetime, timedelta


# =============================================================================
# ① 配置
# =============================================================================

SCRIPT_DIR = Path(__file__).parent.resolve()

# 大循环间隔（秒）：每隔多久完整跑一次流水线
CYCLE_INTERVAL = 30 * 60   # 半小时
 # 反反爬虫 随机波动正负5min
INTERVAL_JITTER = 5 * 60 

actual_sleep = CYCLE_INTERVAL + random.randint(-INTERVAL_JITTER, INTERVAL_JITTER)

# =============================================================================
# ② 日志
# =============================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(str(SCRIPT_DIR / "pipeline.log"), encoding="utf-8"),
    ],
)
log = logging.getLogger("main")

PYTHON = sys.executable
# sys.executable：当前 Python 解释器的完整路径，确保子进程使用同一个环境


# =============================================================================
# ③ 调用单个脚本
# =============================================================================

def run_script(name: str) -> bool:
    """
    用 subprocess 运行指定脚本，实时打印输出，返回是否成功。

    参数：
        name —— 脚本文件名，如 "youtube_watcher.py"

    返回：
        True = 脚本以退出码 0 正常结束；False = 出错
    """
    path = SCRIPT_DIR / name
    if not path.exists():
        log.error(f"找不到脚本：{path}")
        return False

    log.info(f"▶ 启动 {name}")
    start = time.time()

    proc = subprocess.run(
        [PYTHON, str(path)],
        cwd=str(SCRIPT_DIR),
        # cwd：将工作目录设为脚本所在目录，确保各脚本内的相对路径正确
    )

    elapsed = time.time() - start
    if proc.returncode == 0:
        log.info(f"✓ {name} 完成（耗时 {elapsed:.0f} 秒）")
        return True
    else:
        log.error(f"✗ {name} 异常退出，退出码 {proc.returncode}（耗时 {elapsed:.0f} 秒）")
        return False


# =============================================================================
# ④ 主循环
# =============================================================================

def main():
    log.info("=" * 60)
    log.info("autoMirror 流水线启动")
    log.info(f"脚本目录：{SCRIPT_DIR}")
    log.info(f"循环间隔：{actual_sleep} 秒（{actual_sleep // 60} 分钟）")
    log.info("=" * 60)

    cycle = 0

    while True:
        cycle += 1
        cycle_start = datetime.now()

        log.info(f"\n{'═' * 60}")
        log.info(f"第 {cycle} 轮流水线 @ {cycle_start.strftime('%Y-%m-%d %H:%M:%S')}")
        log.info(f"{'═' * 60}")

        # ── 阶段一：下载 ──────────────────────────────────────────────────────
        log.info("\n【阶段一】YouTube 检查 & 下载")
        run_script("youtube_watcher.py")
        # 不管成功与否都继续执行后续阶段，
        # watcher 失败只是本轮没有新内容，translator 和 uploader 仍可处理历史文件

        # ── 阶段二：翻译 ──────────────────────────────────────────────────────
        log.info("\n【阶段二】翻译未处理文件")
        run_script("translator.py")

        # ── 阶段三：动态 ───────────────────────────────────────────────────
        log.info("\n【阶段三】发布已翻译动态")
        run_script("post_uploader.py")

        # ── 阶段四：上传 ──────────────────────────────────────────────────────
        log.info("\n【阶段四】上传已翻译视频")
        run_script("bili_scheduler_uploader.py")

        # ── 等待下一轮 ────────────────────────────────────────────────────────
        elapsed   = (datetime.now() - cycle_start).total_seconds()
        wait_secs = max(0, actual_sleep - elapsed)
        # 扣除本轮实际耗时，保持周期稳定；
        # 若本轮（含上传）耗时超过 CYCLE_INTERVAL，立即开始下一轮

        next_time = datetime.now() + timedelta(seconds=wait_secs)
        log.info(f"\n第 {cycle} 轮完成，耗时 {elapsed:.0f} 秒")
        if wait_secs > 0:
            log.info(f"下一轮：{next_time.strftime('%H:%M:%S')}（等待 {wait_secs:.0f} 秒）")
            log.info(f"{'═' * 60}\n")
            time.sleep(wait_secs)
        else:
            log.info("耗时已超出循环间隔，立即开始下一轮")
            log.info(f"{'═' * 60}\n")


# =============================================================================
# ⑤ 程序入口
# =============================================================================

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("\n收到 Ctrl+C，流水线已停止。")

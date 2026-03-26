#!/usr/bin/env python3
"""
B站扫码登录，保存完整凭证（sessdata / bili_jct / buvid3 / dedeuserid / ac_time_value）

修复说明：
  原版脚本未保存 ac_time_value，且 buvid3 在 QrCodeLogin 完成后为 null。
  缺少这两个字段会导致上传时返回 406 错误。

  本版本修复：
    1. 登录完成后调用 bilibili_api.get_buvid() 补全 buvid3
    2. 同时保存 ac_time_value（凭证刷新令牌）
"""

import asyncio
import json
import uuid
import logging
from pathlib import Path

from bilibili_api import get_buvid
from bilibili_api.login_v2 import QrCodeLogin, QrCodeLoginEvents

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

CRED_FILE = Path(__file__).parent / "bili_credential.json"


async def fetch_buvid3(credential) -> str:
    """
    通过 bilibili_api.get_buvid() 获取 buvid3。

    buvid3 是 B站的设备指纹，由服务器分配，上传接口强制要求此字段非空。
    QrCodeLogin 登录流程不会自动填充，需要额外请求一次。

    返回：
        buvid3 字符串；获取失败时返回空字符串
    """
    try:
        buvid_tuple = await get_buvid()
        # get_buvid() 返回 Tuple[str, str]：
        #   [0] = buvid3（B站设备指纹，上传接口必须）
        #   [1] = buvid4

        buvid3 = buvid_tuple[0] if buvid_tuple and buvid_tuple[0] else ""
        if buvid3:
            log.info(f"  buvid3 获取成功: {buvid3[:16]}…")
            return buvid3

        log.warning(f"  get_buvid() 返回了空值: {buvid_tuple}")
        return ""

    except Exception as e:
        log.warning(f"  get_buvid() 调用失败: {e}")
        return ""


async def main():
    log.info("=" * 50)
    log.info("B站扫码登录")
    log.info("=" * 50)

    # ── 生成并显示二维码 ──────────────────────────────────────────────────────
    qr = QrCodeLogin()
    await qr.generate_qrcode()
    print(qr.get_qrcode_terminal())
    print("\n请用 B站 App 扫描上方二维码（180 秒内有效）\n")

    # ── 轮询扫码状态 ──────────────────────────────────────────────────────────
    while True:
        state = await qr.check_state()

        if state == QrCodeLoginEvents.SCAN:
            print("✅ 已扫码，请在手机上点击确认登录…")
        elif state == QrCodeLoginEvents.CONF:
            print("✅ 手机已确认，正在完成登录…")
        elif state == QrCodeLoginEvents.TIMEOUT:
            print("❌ 二维码已超时，请重新运行脚本")
            return
        elif state == QrCodeLoginEvents.DONE:
            print("🎉 登录成功！")
            break

        await asyncio.sleep(2)

    # ── 获取凭证对象 ──────────────────────────────────────────────────────────
    cred = qr.get_credential()
    # cred 是 bilibili_api.Credential 实例
    # 此时 sessdata / bili_jct / dedeuserid / ac_time_value 已填充
    # buvid3 通常为 None，需要额外获取

    log.info("\n正在补全 buvid3…")
    buvid3 = await fetch_buvid3(cred)

    if not buvid3:
        # 所有自动方式均失败：尝试从凭证对象的属性中取
        buvid3 = getattr(cred, "buvid3", None) or ""

    if not buvid3:
        # 最后兜底：生成一个本地 UUID 格式的设备指纹
        # 格式参考正常 buvid3：xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx + 随机后缀
        # 注意：自动生成的 buvid3 在某些 B站接口下可能仍然失效，
        # 建议先尝试上传，若仍 406 则手动抓包获取真实的 buvid3
        raw_uuid = str(uuid.uuid4()).upper()
        buvid3 = raw_uuid + "68870infoc"
        log.warning(f"  无法自动获取 buvid3，已生成本地 UUID 替代: {buvid3[:16]}…")
        log.warning("  若上传仍报 406，请参考 README 手动抓包获取真实 buvid3")

    # ── 保存完整凭证 ──────────────────────────────────────────────────────────
    credential_data = {
        "sessdata":      cred.sessdata      or "",
        "bili_jct":      cred.bili_jct      or "",
        "buvid3":        buvid3,
        "dedeuserid":    cred.dedeuserid    or "",
        "ac_time_value": cred.ac_time_value or "",
        # ac_time_value：凭证刷新令牌，用于 check_refresh() 自动续期
        # 原版脚本未保存此字段，导致凭证过期后无法自动刷新
    }

    with open(CRED_FILE, "w", encoding="utf-8") as f:
        json.dump(credential_data, f, ensure_ascii=False, indent=2)

    # ── 打印凭证摘要（不打印完整敏感值）────────────────────────────────────────
    log.info("\n凭证已保存至: " + str(CRED_FILE))
    log.info("字段状态：")
    for key, val in credential_data.items():
        status = f"{str(val)[:12]}…" if len(str(val)) > 12 else (str(val) or "【空】")
        log.info(f"  {key:16s}: {status}")

    if not credential_data["ac_time_value"]:
        log.warning("⚠ ac_time_value 仍为空，凭证将无法自动刷新（不影响当前使用）")
    if not credential_data["buvid3"] or credential_data["buvid3"].endswith("infoc"):
        if credential_data["buvid3"].startswith(
            tuple("0123456789ABCDEF")
        ) and "68870infoc" in credential_data["buvid3"]:
            log.warning("⚠ buvid3 使用了本地生成的 UUID，若上传报 406 请手动抓包替换")


if __name__ == "__main__":
    asyncio.run(main())

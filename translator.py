# =============================================================================
# translator.py —— 本地 LM Studio 翻译脚本
#
# 功能：
#   扫描 downloads/ 下四个文件夹（videos / shorts / streams / posts）中的
#   所有 .txt 文件，调用 LM Studio 本地模型将内容翻译为简体中文，
#   并将原文附在译文之后，覆盖写入原文件。
#
#   输出格式（视频/Shorts/直播录像的 .txt）：
#       第一行         → 翻译后的标题          ← bili_auto_uploader.py 读取此行
#       第二行起       → 翻译后的简介          ← bili_auto_uploader.py 读取此部分
#       （空行）
#       ================原文================
#       第一行         → 原始标题
#       第二行起       → 原始简介
#
#   输出格式（动态 .txt）：
#       翻译后的正文
#       （空行）
#       ================原文================
#       原始正文
#
# 与其他脚本的衔接：
#   youtube_watcher.py  → 生成原始 .txt 文件
#   translator.py（本文件）→ 翻译并在原文件中追加原文
#   bili_auto_uploader.py → 读取 .txt 上传至 B站（读取第一行为标题，其余为简介）
#
# 依赖：
#   仅使用 Python 标准库（urllib、json、os 等），无需额外安装
#
# 前提：
#   LM Studio 已启动，并在"Local Server"标签页中加载了一个模型
#   默认监听地址：http://localhost:1234
# =============================================================================


# ---------- 标准库导入 ----------
import os           # 文件和目录操作
import re           # 正则表达式，用于清理模型返回文本中的多余前缀
import json         # 与 LM Studio API 交换 JSON 数据；读写翻译进度文件
import time         # 请求失败后的等待重试
import logging      # 统一日志输出
import urllib.request   # 发送 HTTP POST 请求到 LM Studio，无需 requests 库
import urllib.error     # 捕获网络请求异常
from pathlib import Path
from datetime import datetime

# =============================================================================
# 0 术语表 —— 从外部 JSON 文件加载
# =============================================================================
# 术语表统一维护在 glossary.json（与本脚本同目录）。
# JSON 结构：顶层为"分类名 → {原文: 译文}"的嵌套字典。
# 加载后展开为扁平字典 GLOSSARY，供后续使用。
# =============================================================================

SCRIPT_DIR    = Path(__file__).parent.resolve()
GLOSSARY_FILE = SCRIPT_DIR / "glossary.json"

def _load_glossary() -> dict[str, str]:
    """
    从 glossary.json 加载术语表，展开为 {原文: 译文} 的扁平字典。

    JSON 格式支持两种结构：
        - 嵌套结构（带分类）：{ "分类名": { "原文": "译文", ... }, ... }
        - 扁平结构（无分类）：{ "原文": "译文", ... }
        - "_comment" 等以下划线开头的键自动跳过。

    返回：
        扁平化的 {原文: 译文} 字典；文件不存在时返回空字典并打印警告。
    """
    if not GLOSSARY_FILE.exists():
        print(f"[警告] 术语表文件不存在：{GLOSSARY_FILE}，将使用空术语表继续运行。")
        return {}

    with open(GLOSSARY_FILE, "r", encoding="utf-8") as f:
        raw: dict = json.load(f)

    flat: dict[str, str] = {}
    for key, value in raw.items():
        if key.startswith("_"):
            # 跳过注释键（如 "_comment"）
            continue
        if isinstance(value, dict):
            # 嵌套分类结构：展开子字典
            for term, translation in value.items():
                flat[term] = translation
        elif isinstance(value, str):
            # 顶层扁平结构
            flat[key] = value
        # 其他类型忽略

    return flat


# 启动时加载一次，全局可用
GLOSSARY: dict[str, str] = _load_glossary()


# =============================================================================
# 0-A 术语扫描 —— 翻译前提取文本中实际出现的术语
# =============================================================================

def scan_glossary(text: str) -> dict[str, str]:
    """
    扫描 text，返回只包含文本中实际出现的术语子集。

    工作原理：
        匹配时忽略大小写和空格（去掉所有空格后比较），
        使"THE IDOLM@STER"和"theidolm@ster"都能命中同一术语。

    目的：
        将术语表从"几十条"压缩到"本次文本相关的几条"，
        避免把无关术语塞进 system prompt 导致小模型注意力涣散。

    参数：
        text —— 待翻译的原始文本（可以是标题、简介或动态正文）

    返回：
        {原文: 译文} 子字典，仅含文本中出现的术语；未命中时返回空字典
    """
    # 预处理文本：全部转小写并去除空格，用于宽松匹配
    text_normalized = text.lower().replace(" ", "").replace("　", "")
    # 　 是全角空格，日文文本中常见

    matched: dict[str, str] = {}
    for term, translation in GLOSSARY.items():
        term_normalized = term.lower().replace(" ", "").replace("　", "")
        if term_normalized in text_normalized:
            matched[term] = translation
    return matched


def build_glossary_str(matched: dict[str, str]) -> str:
    """
    将命中的术语字典格式化为 Prompt 友好的字符串。

    参数：
        matched —— scan_glossary() 的返回值

    返回：
        多行字符串，每行格式为 "- 原文 -> 译文"；
        字典为空时返回空字符串。
    """
    if not matched:
        return ""
    return "\n".join(f"- {k} -> {v}" for k, v in matched.items())


# =============================================================================
# ① 全局配置区 —— 按需修改
# =============================================================================

# LM Studio 本地服务器地址
# 启动 LM Studio → 点击左侧"Local Server"图标 → 点击"Start Server"
# 默认端口 1234，若你改过端口请同步修改此处
LM_STUDIO_URL = "http://127.0.0.1:1234/v1/chat/completions"

# 模型名称
# 在 LM Studio 的 Local Server 页面可以看到当前加载的模型 ID
# 填入具体名称（如 "qwen2.5-7b-instruct"）或保持 "local-model" 均可，
# LM Studio 会忽略此字段并使用当前已加载的模型
LM_MODEL = "qwen/qwen3-4b-2507"

# 每次 API 请求的最长等待时间（秒）
# 本地模型速度取决于硬件，长文本可能需要较长时间，建议 120～300
REQUEST_TIMEOUT = 360

# 翻译失败时的重试次数和每次重试前的等待秒数
MAX_RETRIES  = 3
RETRY_WAIT   = 5

# 长文本自动分段翻译的字符阈值
# 超过此长度的文本会被切分成多段分别翻译后拼接
# LM Studio 本地模型通常有 context window 限制（4k～32k tokens），
# 以字符数粗略估算：中日文 1 字符 ≈ 1-2 token，英文 1 词 ≈ 1-2 token
# 默认 1500 字符较为保守，可根据你的模型 context size 适当增大
MAX_CHARS_PER_CHUNK = 1500

# 翻译进度文件路径（记录已翻译文件，避免重复翻译）
# 与本脚本放在同一目录
PROGRESS_FILE = str(SCRIPT_DIR / "translated_files.json")

# 四个待翻译文件夹的路径（与 youtube_watcher.py 保持一致）
FOLDERS_TO_TRANSLATE = [
    str(SCRIPT_DIR / "downloads" / "videos"),    # 普通视频的 .txt
    str(SCRIPT_DIR / "downloads" / "shorts"),    # Shorts 的 .txt
    str(SCRIPT_DIR / "downloads" / "streams"),   # 直播录像的 .txt
    str(SCRIPT_DIR / "downloads" / "posts"),     # 社区动态的 .txt
]

# 原文分隔线，插入在译文与原文之间
# 修改此字符串会同步影响最终写入文件的格式
SEPARATOR = "-" * 3 + "以上为AI翻译" + "-" * 3

# 是否跳过已经包含分隔线的文件（说明已经翻译过）
# True（默认）：已翻译的文件不会被重复翻译
# False：强制重新翻译所有文件（将忽略 PROGRESS_FILE）
SKIP_ALREADY_TRANSLATED = True

# --- 新增：自动监测的间隔时间（秒）：10 分钟 = 10 * 60 ---
WATCH_INTERVAL = 10 * 60

# ─── 如何让某些文件重新翻译 ───────────────────────────────────────────────
# 方法 A（重翻全部）：
#   1. 将上方 SKIP_ALREADY_TRANSLATED 改为 False，运行后改回 True
#   2. 或直接删除 translated_files.json，并把所有 .txt 里的
#      "译文部分 + 分隔线" 删掉，只保留原文（分隔线之后的内容）
#
# 方法 B（重翻单个文件）：
#   1. 打开该 .txt 文件
#   2. 删除分隔线（================原文================）及其上方的全部译文
#   3. 从 translated_files.json 中删除该文件的路径记录
#   4. 重新运行脚本即可
#
# ★ 最简单的方法（推荐）：
#   将 SKIP_ALREADY_TRANSLATED 改为 False，脚本会自动从文件中提取原文
#   重新翻译，无需手动编辑任何 .txt 文件。


# =============================================================================
# ② 日志初始化
# =============================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),                                      # 终端输出
        logging.FileHandler("translator.log", encoding="utf-8"),      # 同步写入日志
    ]
)
log = logging.getLogger(__name__)


# =============================================================================
# ③ 翻译进度持久化
# =============================================================================

def load_progress() -> set:
    """
    从 PROGRESS_FILE 读取已翻译文件的路径集合。

    文件结构：["路径1", "路径2", ...]（JSON 数组）
    首次运行时文件不存在，返回空集合。
    """
    if os.path.exists(PROGRESS_FILE):
        with open(PROGRESS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return set(data)
        # 转为 set：O(1) 的 in 查找，比列表快
    return set()


def save_progress(done: set):
    """
    将已翻译文件路径集合写回 PROGRESS_FILE（覆盖写入）。

    参数：
        done —— 当前所有已翻译文件的绝对路径集合
    """
    with open(PROGRESS_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(done), f, ensure_ascii=False, indent=2)
        # sorted()：排序使文件易于人工查阅
        # ensure_ascii=False：允许中文路径字符直接存储


# =============================================================================
# ④ LM Studio API 调用
# =============================================================================

def call_lm_studio(prompt: str, glossary_str: str = "") -> str | None:
    """
    向 LM Studio 本地服务器发送翻译请求，返回模型输出的文本。

    LM Studio 兼容 OpenAI Chat Completions API，请求格式与 OpenAI 完全相同。
    因此本函数直接用标准库 urllib 构造 HTTP POST，无需安装 openai 或 requests 库。

    参数：
        prompt      —— 完整的用户消息文本（已包含翻译指令和待翻译内容）
        glossary_str —— 本次文本命中的术语字符串（由 build_glossary_str 生成）；
                        为空字符串时不在 system prompt 中注入术语表章节

    返回：
        翻译结果字符串（去除首尾空白后）；失败时返回 None
    """
    # 仅在有命中术语时才注入术语表章节，避免无关内容占用 context
    if glossary_str:
        glossary_section = (
            "## Glossary（本次文本专属术语表，严格遵守）\n"
            f"{glossary_str}\n"
        )
    else:
        glossary_section = ""

    payload = {
        "model": LM_MODEL,
        # 模型名称：LM Studio 会使用当前已加载的模型，此字段仅作标识

        "messages": [
            {
                "role": "system",
                "content": (
                    f"## Role\n"
                    f"You are a professional, native-level translator specialized in Japanese/English to Simplified Chinese.\n"
                    f"\n"
                    f"## Constraints\n"
                    f"1. **Output ONLY translation**: No preamble, no explanations, no filler text.\n"
                    f"2. **Glossary Enforcement**: You MUST use the translations provided in the <Glossary> section. Do not improvise.\n"
                    f"3. **Format Integrity**:\n"
                    f"   - Single Paragraph: Return translation directly.\n"
                    f"   - Multi-Paragraph: Use '%%' as a separator to match the source.\n"
                    f"4. **Naming Convention**: For names NOT in the glossary, use 'Translation(Original)'.\n"
                    f"5. **Title Handling**: DO NOT translate the song title in the first line of the text. Keep the song title in its original language.\n"
                    f"\n"
                    f"<Glossary>\n"
                    f"{glossary_section}\n"
                    f"</Glossary>\n"
                    f"\n"
                    f"## Output Format Examples\n"
                    f"- Input: 'Hello' -> Output: '你好'\n"
                    f"- Input: 'Para A\\n%%\\nPara B' -> Output: '翻译A\\n%%\\n翻译B'\n"
                    f"\n"
                    f"## Task\n"
                    f"Translate the following text into Simplified Chinese strictly following the rules:\n"
                    f"\n"
                    f"[TEXT_START]\n"
                    f"{{target_text}}\n"
                    f"[TEXT_END]"
                ),
                # system 消息设定模型角色和行为规范
                # 明确禁止添加前缀，防止模型输出 "Translation:" 等干扰后续解析
            },
            {
                "role": "user",
                "content": prompt,
                # 具体的翻译请求，由调用方构造
            },
        ], 

        "temperature": 0,
        # 温度值：0 = 完全确定性输出，1 = 较有创意
        # 翻译任务适合低温度（0.2～0.4），确保译文稳定、忠实原文

        "max_tokens": 4096,
        # 单次输出最大 token 数，4096 可容纳较长文本
        # 若模型 context 较小（如 4k total），此值应相应降低

        "stream": False,
        # False = 等待完整响应后一次性返回；True = 流式逐 token 输出
        # 简化处理，使用非流式模式
    }

    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    # json.dumps：将 Python 字典序列化为 JSON 字符串
    # .encode("utf-8")：转为字节流，HTTP 请求体需要字节类型

    req = urllib.request.Request(
        LM_STUDIO_URL,
        data=body,
        headers={
            "Content-Type": "application/json",
            # 声明请求体格式为 JSON，LM Studio 需要此 Header
        },
        method="POST",
    )

    for attempt in range(1, MAX_RETRIES + 1):
        # 最多重试 MAX_RETRIES 次
        result: dict | None = None

        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                raw_bytes = resp.read()

            result = json.loads(raw_bytes.decode("utf-8"))

            if result is None:
                log.error("  LM Studio 响应体为 null，无法解析")
                return None

            # 从 OpenAI 格式的响应中提取模型输出文本
            # 响应结构：{"choices": [{"message": {"content": "..."}}]}
            text = result["choices"][0]["message"]["content"]
            text = strip_think_tags(text)
            # 删除 <think>...</think> 推理块（DeepSeek-R1 / Qwen3 thinking 模式会输出此内容）
            return text.strip()

        except urllib.error.URLError as e:
            # 网络连接错误（LM Studio 未启动、端口错误等）
            if attempt == MAX_RETRIES:
                log.error(f"  LM Studio 连接失败（已重试 {MAX_RETRIES} 次）: {e}")
                log.error(f"  请确认 LM Studio 已启动并在 {LM_STUDIO_URL} 监听")
                return None
            log.warning(f"  请求失败（第 {attempt} 次），{RETRY_WAIT} 秒后重试: {e}")
            time.sleep(RETRY_WAIT)

        except (KeyError, IndexError, json.JSONDecodeError) as e:
            # 响应格式异常（模型返回了非预期的 JSON 结构）
            preview = str(result)[:200] if result is not None else "(响应解析前即失败)"
            log.error(f"  响应解析失败: {e}  原始响应: {preview}")
            return None

        except Exception as e:
            # 其他未预期异常（超时等）
            if attempt == MAX_RETRIES:
                log.error(f"  翻译请求异常（已重试 {MAX_RETRIES} 次）: {e}")
                return None
            log.warning(f"  请求异常（第 {attempt} 次），{RETRY_WAIT} 秒后重试: {e}")
            time.sleep(RETRY_WAIT)

    return None


# =============================================================================
# ⑤ 思考标签清除
# =============================================================================

def strip_think_tags(text: str) -> str:
    """
    删除模型输出中的 <think>...</think> 推理块及其全部内容。

    部分推理模型（如 DeepSeek-R1、Qwen3 系列开启 thinking 模式时）会在正式
    回答之前先输出一段 <think>...</think> 形式的内部推理过程，这段内容不属于
    翻译结果，需要在返回给调用方之前剥离。

    支持的格式：
        <think>任意内容（包括换行）</think>
        <Think>...</Think>（大小写不敏感）
        连续多个 <think> 块

    参数：
        text —— 模型原始输出字符串

    返回：
        去除所有 <think>...</think> 块后的干净文本（首尾空白也一并去除）
    """
    cleaned = re.sub(
        r'<think>.*?</think>',   # 匹配 <think> 到 </think> 之间的全部内容
        '',                       # 替换为空字符串（直接删除）
        text,
        flags=re.DOTALL | re.IGNORECASE,
        # re.DOTALL    ：让 "." 匹配换行符，确保多行思考块被完整匹配
        # re.IGNORECASE：同时匹配 <Think>、<THINK> 等大小写变体
    )
    return cleaned.strip()
    # .strip()：删除思考块后，开头可能残留空行，一并清除


# =============================================================================
# ⑦ 文本分段与翻译
# =============================================================================

def split_into_chunks(text: str, max_chars: int = MAX_CHARS_PER_CHUNK) -> list[str]:
    """
    将长文本按段落边界切分为多个不超过 max_chars 字符的片段。

    切分策略：
        优先在段落边界（连续空行）处切分，保持语义完整性。
        若单个段落超过 max_chars，则在句子边界（。！？\n）处进一步切分。
        最坏情况下按字符数强制截断。

    参数：
        text      —— 待切分的原始文本
        max_chars —— 每个片段的最大字符数

    返回：
        切分后的文本片段列表，各片段均不超过 max_chars 字符
    """
    if len(text) <= max_chars:
        return [text]
        # 未超过阈值，直接返回整段，不做切分

    # 按"连续空行"分割成段落（保留段落间的语义边界）
    paragraphs = re.split(r'\n{2,}', text)
    # \n{2,}：匹配两个及以上连续换行符（即空行）

    chunks = []
    current = ""

    for para in paragraphs:
        test = current + ("\n\n" if current else "") + para
        # 尝试将当前段落追加到已有片段中

        if len(test) <= max_chars:
            current = test
            # 追加后未超限，继续累积

        else:
            if current:
                chunks.append(current)
                # 当前累积片段已满，先保存

            if len(para) <= max_chars:
                current = para
                # 新段落本身不超限，开始新片段
            else:
                # 单个段落超过限制，按句子边界进一步切分
                sentences = re.split(r'(?<=[。！？\n])', para)
                # (?<=...)：正向后顾，在句末标点后切分，保留标点在句子末尾

                sub = ""
                for sent in sentences:
                    if len(sub) + len(sent) <= max_chars:
                        sub += sent
                    else:
                        if sub:
                            chunks.append(sub)
                        # 单个句子仍超限时强制截断
                        while len(sent) > max_chars:
                            chunks.append(sent[:max_chars])
                            sent = sent[max_chars:]
                        sub = sent
                current = sub

    if current:
        chunks.append(current)
        # 最后一个未满的片段

    return chunks


def translate_text(text: str, context_hint: str = "") -> str | None:
    """
    翻译给定文本，自动处理长文本分段，并按需注入精简术语表。

    流程：
        1. 对整段原文调用 scan_glossary()，提取本次文本中实际出现的术语
        2. 将命中术语格式化为字符串，传入每次 call_lm_studio() 调用
        3. 对于短文本（≤ MAX_CHARS_PER_CHUNK），直接发送一次请求
        4. 对于长文本，切分后逐段翻译，每段均使用同一份命中术语表

    参数：
        text         —— 待翻译的原始文本
        context_hint —— 给模型的上下文提示，如 "（这是视频标题）"，帮助翻译更准确

    返回：
        翻译后的文本；任意片段翻译失败则返回 None
    """
    text = text.strip()
    if not text:
        return ""
        # 空文本直接返回空字符串，不浪费一次 API 调用

    # ── 术语扫描：提取本次文本命中的术语 ────────────────────────────────────
    matched = scan_glossary(text)
    glossary_str = build_glossary_str(matched)

    if matched:
        log.info(f"    命中术语 {len(matched)} 条：{', '.join(matched.keys())}")
    else:
        log.info("    未命中任何术语，不注入术语表")
    # ─────────────────────────────────────────────────────────────────────────

    chunks = split_into_chunks(text)
    # 切分（短文本直接得到 [text] 单元素列表）

    translated_parts = []

    for i, chunk in enumerate(chunks):
        if len(chunks) > 1:
            log.info(f"    翻译第 {i+1}/{len(chunks)} 段（{len(chunk)} 字符）…")

        hint = f"{context_hint}\n" if context_hint else ""
        prompt = (
            f"{hint}"
            f"请将以下内容翻译为简体中文：\n\n"
            f"{chunk}"
        )
        # 构造翻译 prompt：上下文提示（可选）+ 明确指令 + 待翻译内容

        result = call_lm_studio(prompt, glossary_str=glossary_str)
        if result is None:
            return None
            # 任意片段失败，终止并返回 None，不输出不完整的译文

        translated_parts.append(result)

    return "\n\n".join(translated_parts)
    # 多片段用双换行拼接，恢复原有段落间距


# =============================================================================
# ⑥ 文件类型判断
# =============================================================================

def get_folder_type(filepath: str) -> str:
    """
    根据文件所在的文件夹路径判断内容类型。

    返回值：
        "video"  —— videos / shorts / streams 文件夹下的 .txt
                    格式：第一行=标题，其余=简介
        "post"   —— posts 文件夹下的 .txt
                    格式：纯正文，无固定结构
    """
    norm = filepath.replace("\\", "/").lower()
    # 统一用正斜杠，转小写，方便跨平台字符串比较

    if "/posts/" in norm:
        return "post"
    return "video"
    # videos / shorts / streams 都使用同样的第一行=标题格式


# =============================================================================
# ⑧ 单文件翻译处理
# =============================================================================

def translate_file(filepath: str) -> bool:
    """
    读取一个 .txt 文件，翻译内容，将"译文 + 分隔线 + 原文"写回原文件。

    根据 get_folder_type() 的结果采用不同的翻译策略：
        video 类型：分别翻译标题（第一行）和简介（其余行），保持格式
        post  类型：将整个文本作为整体翻译

    写入格式（video 类型）：
        [翻译后的标题]
        [翻译后的简介]

        ================原文================
        [原始标题]
        [原始简介]

    写入格式（post 类型）：
        [翻译后的正文]

        ================原文================
        [原始正文]

    参数：
        filepath —— 待翻译的 .txt 文件绝对路径

    返回：
        True = 翻译并写入成功；False = 失败（已打印日志）
    """
    # ── 读取原文件内容 ────────────────────────────────────────────────────────
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            original_content = f.read()
    except Exception as e:
        log.error(f"  读取文件失败 {filepath}: {e}")
        return False

    original_content = original_content.strip()
    if not original_content:
        log.warning(f"  文件为空，跳过: {os.path.basename(filepath)}")
        return True
        # 空文件不算失败，直接跳过

    # ── 已翻译检测 / 原文提取 ──────────────────────────────────────────────────
    if SEPARATOR in original_content:
        if SKIP_ALREADY_TRANSLATED:
            # SKIP_ALREADY_TRANSLATED = True（默认）：文件已含分隔线，直接跳过
            log.info(f"  已翻译，跳过: {os.path.basename(filepath)}")
            return True
        else:
            # SKIP_ALREADY_TRANSLATED = False：需要重新翻译
            # 文件格式：[上次译文] \n\n [SEPARATOR] \n [原文]
            # 自动提取分隔线之后的原文作为本次翻译的输入，
            # 无需用户手动编辑文件删除旧译文
            parts_split = original_content.split(SEPARATOR, maxsplit=1)
            # maxsplit=1：只在第一个分隔线处切分，防止原文中恰好含相同字符串时误切
            original_content = parts_split[1].strip() if len(parts_split) > 1 else original_content
            log.info(f"  检测到已有译文，将使用分隔线后的原文重新翻译: {os.path.basename(filepath)}")

    folder_type = get_folder_type(filepath)
    filename    = os.path.basename(filepath)

    # ── 根据文件类型执行翻译 ──────────────────────────────────────────────────
    if folder_type == "video":
        # ----- 视频 / Shorts / 直播录像的 .txt -----
        # 格式：第一行=标题，其余行=简介
        lines = original_content.splitlines()
        # splitlines()：按行分割，每个元素为一行文本（不含换行符）

        original_title = lines[0].strip() if lines else ""
        # 第一行作为标题

        original_desc  = "\n".join(lines[1:]).strip() if len(lines) > 1 else ""
        # 从第二行起拼接为简介字符串

        log.info(f"  翻译标题: {original_title[:50]}{'…' if len(original_title) > 50 else ''}")
        translated_title = translate_text(
            original_title,
            context_hint="（这是一段视频标题，请简洁翻译，保持标题风格）"
        )
        if translated_title is None:
            log.error(f"  标题翻译失败: {filename}")
            return False

        translated_desc = ""
        if original_desc:
            log.info(f"  翻译简介（{len(original_desc)} 字符）…")
            translated_desc = translate_text(
                original_desc,
                context_hint="（这是一段视频简介，请保持原文段落结构）"
            )
            if translated_desc is None:
                log.error(f"  简介翻译失败: {filename}")
                return False

        # 拼装最终内容：
        #   译文标题
        #   译文简介（可能为空）
        #   空行
        #   分隔线
        #   原始标题
        #   原始简介（可能为空）
        parts = [translated_title]
        if translated_desc:
            parts.append(translated_desc)
        parts.append("")                    # 分隔前的空行
        parts.append(SEPARATOR)
        parts.append(original_title)
        if original_desc:
            parts.append(original_desc)

        final_content = "\n".join(parts)

    else:
        # ----- 社区动态 .txt -----
        # 格式：纯正文，整体翻译
        log.info(f"  翻译动态正文（{len(original_content)} 字符）…")
        translated_body = translate_text(
            original_content,
            context_hint="（这是一条 YouTube 社区动态帖子，请保持原文语气和段落结构）"
        )
        if translated_body is None:
            log.error(f"  动态翻译失败: {filename}")
            return False

        # 拼装：译文 + 空行 + 分隔线 + 原文
        final_content = f"{translated_body}\n\n{SEPARATOR}\n{original_content}"

    # ── 写回文件 ──────────────────────────────────────────────────────────────
    try:
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(final_content)
            # 覆盖写入：原文件内容被替换为"译文 + 原文"的完整版本
    except Exception as e:
        log.error(f"  写入文件失败 {filepath}: {e}")
        return False

    log.info(f"  ✓ 翻译完成: {filename}")
    return True


# =============================================================================
# ⑨ 扫描文件夹并批量翻译
# =============================================================================

def scan_and_translate():
    """
    主执行函数：
        1. 遍历 FOLDERS_TO_TRANSLATE 中所有文件夹
        2. 递归查找所有 .txt 文件（跳过已翻译的）
        3. 逐文件调用 translate_file() 翻译
        4. 实时更新进度文件
    """
    log.info("=" * 60)
    log.info("翻译任务启动")
    log.info(f"LM Studio 地址：{LM_STUDIO_URL}")
    log.info(f"翻译进度文件：{PROGRESS_FILE}")
    log.info(f"术语表已加载：{len(GLOSSARY)} 条术语（来自 {GLOSSARY_FILE}）")
    log.info("=" * 60)

    # ── 启动连通性测试 ────────────────────────────────────────────────────────
    log.info("正在测试 LM Studio 连接…")
    test_result = call_lm_studio("请回答：你好")
    # 发送一条简单请求，验证 LM Studio 是否在线、模型是否已加载
    if test_result is None:
        log.error("无法连接到 LM Studio，请检查：")
        log.error("  1. LM Studio 是否已启动")
        log.error("  2. 是否已在 Local Server 标签页加载模型并点击 Start Server")
        log.error(f"  3. 端口是否正确（当前配置：{LM_STUDIO_URL}）")
        return
    log.info(f"LM Studio 连接正常，模型响应：{test_result[:60]}…")

    # ── 加载翻译进度 ──────────────────────────────────────────────────────────
    done_files = load_progress()
    # done_files：上次运行已完成翻译的文件路径集合（绝对路径）

    # ── 收集待翻译文件 ────────────────────────────────────────────────────────
    all_txt_files = []

    for folder in FOLDERS_TO_TRANSLATE:
        if not os.path.exists(folder):
            log.info(f"文件夹不存在，跳过：{folder}")
            continue
            # 某个文件夹不存在（如从未下载过 Shorts）时不报错，直接跳过

        for root, dirs, files in os.walk(folder):
            # os.walk()：递归遍历 folder 下的所有子目录和文件

            # 排除隐藏目录
            dirs[:] = [d for d in dirs if not d.startswith(".")]

            for filename in files:
                if not filename.endswith(".txt"):
                    continue
                    # 只处理 .txt 文件，忽略 .mp4 / .jpg 等其他文件

                abs_path = os.path.abspath(os.path.join(root, filename))
                all_txt_files.append(abs_path)

    total = len(all_txt_files)
    pending = [f for f in all_txt_files if f not in done_files]

    log.info(f"共发现 {total} 个 .txt 文件，待处理 {len(pending)} 个")

    if not pending:
        log.info("所有文件均已翻译，无需处理。")
        return

    # ── 逐文件翻译 ────────────────────────────────────────────────────────────
    success_count = 0
    fail_count    = 0

    for idx, filepath in enumerate(pending, start=1):
        try:
            rel = os.path.relpath(filepath, SCRIPT_DIR)
        except ValueError:
            rel = filepath
            # Windows 上跨盘符时 relpath 会报错，回退为绝对路径

        log.info(f"\n[{idx}/{len(pending)}] {rel}")

        success = translate_file(filepath)

        if success:
            done_files.add(filepath)
            # 成功后立即加入已完成集合

            save_progress(done_files)
            # 每翻译一个文件就持久化一次进度，中途终止时不会丢失已完成的工作

            success_count += 1
        else:
            fail_count += 1
            # 失败时不加入 done_files，下次运行会重试

    # ── 最终汇总 ──────────────────────────────────────────────────────────────
    log.info("\n" + "=" * 60)
    log.info(f"翻译任务完成：成功 {success_count} 个，失败 {fail_count} 个")
    if fail_count > 0:
        log.info("失败的文件下次运行时将自动重试")
    log.info("=" * 60)


# =============================================================================
# ⑩ 程序入口
# =============================================================================

if __name__ == "__main__":
    # 单次执行（循环由 main.py 控制）
    try:
        scan_and_translate()
    except KeyboardInterrupt:
        log.info("\n收到 Ctrl+C，翻译已中断。")

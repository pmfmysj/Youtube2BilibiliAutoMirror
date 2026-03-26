# 全自动搬运邮政公司

[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

## 概要

这是一个由claude sonnet 4.6 extended 用 python 编写的一个全自动托管搬运机器人。 使用它就算你没有精力盯着搬运元看也可以第一时间（或者第一时间后的半小时左右）搬运至你的bilibili账户上。
由此，类似ch邮政公司的账号也许会遇到更加饱和的竞争吧 出于减少蹭够的话语权的目的，构建了该项目

## 原理

```bash
youtube_watcher.py  每隔指定时间定时爬取指定账号（们）的视频/短视频/直播录像/帖子并下载之
translator.py       调用lmstudio使用AI模型自动翻译获取的内容
bili_scheduler_uploader.py 每次上传一个获取到的视频（如果一口气全上传了会被叔叔干）
post_uploader.py           将获取的动态全部上传
main.py                    调用以上代码定时运行
```

## 需要手动执行的

1. 需要一个bilibili账号, 电脑上需要安装python、ffmpeg、yt-dlp、biliup、LMStudio
2. 需要手动获取youtube的cookie以及bilibili的登录相关信息（具体请检索biliup项目中的内容）
3. 需要在youtube_watcher.py中手动输入搬运元的网址和扫描数量
4. 需要在main.py中设置运行间隔 (建议间隔时间不小于30min否则容易被反爬虫)


## 使用方式

```bash
# 克隆仓库
git clone [https://github.com/pmfmysj/Youtube2BilibiliAutoMirror.git](https://github.com/pmfmysj/Youtube2BilibiliAutoMirror.git)

# 进入项目目录
cd 仓库名

# 运行之
python main.py

# 🚀 dsv — DeepSeek Vision CLI

### 让任何不支持视觉的大模型,瞬间获得「真·图片理解」能力

> **DeepSeek API 没有视觉能力?没关系。dsv 直接驱动 DeepSeek 网页版「识图模式」,把图片变成文字描述——一行命令,任何纯文本模型都能"看"图了。**

---

## ✨ 为什么选 dsv?(vs 其他方案)

| | **dsv(本仓库)** | deepseek-view | 纯 API 逆向方案 |
|---|---|---|---|
| **安装依赖** | 🎉 **零安装** | 需 Chromium 300MB + Node + Playwright | 需破解登录验证 |
| **破解 PoW 反爬** | 🎉 **不需要**——浏览器前端自动计算 | 不需要(浏览器方案) | ❌ 必须逆向 DeepSeekHashV1 |
| **登录验证码** | 🎉 **OpenCV 自动破解**(数美选物) | 手动扫码 | 手动/破解 |
| **视觉能力** | ✅ 真·多模态(识图模式) | ✅ | ✅ |
| **会话管理** | ✅ **用后即删**,零污染 | 需手动清理 | 需手动清理 |
| **环境** | ✅ iOS/iSH/Linux/Windows 全通 | 需桌面 Chrome | 纯 HTTP |
| **风控风险** | 🟢 低(真实浏览器指纹) | 🟢 低 | 🔴 高(易封) |

**一句话:唯一做到「零安装 + 不逆向 PoW + 自动过验证码 + 用后即删」的 DeepSeek 识图 CLI。**

---

## 🧠 它是什么

DeepSeek 官方 API 是纯文本模型,**没有图片识别能力**。但 DeepSeek 网页版有一个隐藏的**「识图模式」**(model_type=`vision`,官方标注"图片理解功能内测中")——它是**真正的多模态视觉理解**,不是 OCR:

- ✅ 能识别物体、颜色、形状、空间关系(实测:红圆/绿三角/蓝方块/文字,全部准确)
- ✅ 能描述场景、构图、光影(实测:识别出"两只狗叼树枝"、"樊少皇参加赫章火把节")
- ❌ 快速模式只做 OCR("未提取到文字"),**识图模式才是真视觉**

dsv 通过驱动浏览器自动化,把这个能力暴露成**标准 CLI**,让任何不支持视觉的大模型(DeepSeek API / Claude Code / Codex / 本地模型)都能调用。

```
你的纯文本大模型 → 调用 dsv → DeepSeek 识图模式 → 返回图片描述文本
```

---

## 📦 安装

```bash
# 克隆
git clone https://github.com/menghuanshiguang/deepseek-vision-cli.git
cd deepseek-vision-cli

# 依赖: Python 3 + minis-browser-use(iSH 自带) 或任意浏览器驱动
# 零额外安装!
```

> 核心依赖 `minis-browser-use` 是 iSH(App 内 Linux 环境)自带的浏览器驱动工具,无需 pip 安装任何包。

### 🪟 Windows 支持

Windows 上自动改用 `win_browser.py`(Playwright 驱动**系统自带 Edge**),**零浏览器下载**——不需要 Chromium/Playwright 浏览器包,也不需要 minis-browser-use。

```bash
# 安装依赖(仅需 playwright 库本身, 不执行 playwright install chromium!)
pip install playwright

# 验证码破解所需(仅 --login 时需要)
pip install opencv-python-headless numpy pillow
```

- 自动探测 Edge/Chrome; 若两者皆无, 用环境变量指定: `set DSV_EDGE=C:\path\to\chrome.exe`
- CDP 端口自动随机分配(不占用固定端口, 也不会误连用户已开调试口的浏览器); 可选环境变量: `DSV_EDGE_PROFILE`(默认 `~/.dsv_edge_profile`)
- ⚠️ **登录流程**: `--login` 与 `--verify` 是两次独立命令, 期间浏览器进程需保持存活(登录页状态跨命令复用)。
  若验证码"填不进去", 说明浏览器被中途退出, 在同一终端会话里连续执行即可。
- 其余用法与 iSH 完全一致: `dsv photo.jpg "这是什么?"` / `dsv --login <手机号>` / `dsv --verify <验证码>`

## 🚀 使用

```bash
# 基本用法
dsv <图片路径> [问题]

# 示例
dsv photo.jpg "这是什么动物?什么品种?"
dsv screenshot.png "描述这张图的界面"
dsv chart.png "这是红三兵K线形态吗?"

# 自定义问题
dsv design.png "帮我分析这个UI设计,给出配色和布局建议"
```

**输出**:纯文本到 stdout(供大模型直接解析),日志到 stderr。

**退出码契约**(供调用方 agent 判断,不会傻等):

| 退出码 | 含义 |
|---|---|
| 0 | 成功,stdout 为回答文本 |
| 1 | 用法错误(无图片参数) |
| 2 | 参数缺失(`--login`/`--verify` 未给值) |
| 3 | 短信发送失败(验证未通过/被风控,等 30s 重试) |
| 4 | **token 无效/缺失 → 快速失败**,提示先登录,绝不阻塞 |
| 5 | 并发冲突(另一个 dsv 进程在跑) |
| 6 | **风控熔断中**(已写日志并停止调用),`dsv --risk-clear` 手动解除 |
| 9 | 密码已通过,但触发**新设备保护**需要邮箱验证码;按提示跑 `dsv --verify <码>` |

### 风控熔断(2026-10-28)

一旦命中 DeepSeek 风控信号(频率限制文案、连续人机验证失败、HTTP 429/403),dsv 会:

1. 往 `~/.dsv_risk.log` 追加一行 JSONL(时间 / 阶段 / 原因 / 证据片段 / 冷却时长);
2. 把熔断状态写入 `~/.dsv_breaker.json`(默认冷却 60 分钟);
3. **冷却期内任何调用直接 exit 6**,不再开浏览器、不再打接口 —— 避免"越试越黑"。

```bash
dsv --risk-status   # 看是否熔断中、剩余时间、最近 5 条记录
dsv --risk-log      # 打印日志文件绝对路径
dsv --risk-clear    # 人工确认风险已过, 手动解除
```

可用环境变量覆盖:`DSV_RISK_LOG` / `DSV_BREAKER` / `DSV_RISK_COOLDOWN`(秒)。两个 CLI 共用同一套状态文件。

### 登录(异步,永不阻塞调用方)

> 核心设计:CLI **绝不在"等人工输入"上阻塞**。识图时 token 无效会**快速失败**并提示登录命令,不会卡住调用方 agent。

```bash
# 1. 触发验证码(自动填号 + OpenCV破解数美验证 + 发短信, 约15秒退出)
dsv --login 153****

# 2. 收到短信后, 完成登录(快速退出)
dsv --verify 392050

# 3. 之后正常识图
dsv photo.jpg "这是什么?"

# 邮箱账号(收不到短信): 走「密码登录」Tab
dsv --login you@example.com --password '你的密码'
# 密码也可用环境变量, 避免进 shell history:
DSV_PASSWORD='你的密码' dsv --login you@example.com

# 其他
dsv --logout    # 清除本地 token
dsv --help      # 帮助
```

> **为什么邮箱必须用密码**:DeepSeek 国内版登录页的「验证码登录」只接受手机号
> (输邮箱会提示"手机号码格式不正确");邮箱账号要走「密码登录」Tab(字段为
> "请输入手机号/邮箱地址" + 密码)。CLI 会自动切 Tab、填邮箱和密码并抓 token。

### 配置给不支持视觉的大模型

**Claude Code / Codex**(在 CLAUDE.md 或指令里加):

```markdown
当你需要分析/识别图片内容,而当前模型没有视觉能力时,运行:
`dsv <图片绝对路径> "<问题>"`
命令会返回 DeepSeek 对该图片的文字描述。
```

**或封装为 MCP tool** 暴露一个 `deepseek_vision` 工具。

---

## 🔧 工作原理

```
dsv <image> <prompt>
  │
  ├─ 1. 登录态注入(Authorization: Bearer <token>, 持久化于 ~/.dsv_token)
  ├─ 2. 新建对话(关键: 只有新对话才显示"快速/专家/识图"模式切换 UI)
  ├─ 3. 切换「识图模式」(model_type=vision)
  ├─ 4. 上传图片(大图自动压缩 + 分块注入, 轮询网页端上传成功标志)
  ├─ 5. 提问并发送(轮询网页端发送成功标志)
  ├─ 6. 流式读取回答(轮询网页端回复完成标志)
  └─ 7. 用后即删(自动删除本次会话, 对话列表零污染)
```

## 🍎 适配 OpenMinis(iOS 内置 Linux 沙箱)

dsv 在 **OpenMinis / Minis**(iOS App 内的 iSH Linux 沙箱)环境中完整适配,通过系统自带的 `minis-browser-use` 驱动 App 内置 WebView 操作 DeepSeek 网页版,零额外依赖。

**环境适配要点:**

| 适配项 | 说明 |
|---|---|
| 浏览器驱动 | 调用 `minis-browser-use` CLI(execute_js/navigate),App 内置 WebView |
| execute_js 返回格式 | Minis 桥接层把返回值展平为 `key: value` 行——dsv 已兼容(JSON/分隔符双模式) |
| 手机照片 | EXIF 方向自动转正;HEIC 提示转 JPG |
| 并发保护 | `.dsv.lock` 文件锁,防多进程互相干扰浏览器状态 |
| 快速失败 | 被拒/违规/风控时 30s 内返回明确原因,不傻等 |
| 自动重试 | 上传缩略图未出现自动重注入;识图模式切换失败自动重开对话 |

**与 Minis 技能集成(推荐):**

```bash
# dsv-bridge skill 透明转发壳(自动定位 dsv + 流式日志)
python3 /var/minis/skills/dsv-bridge/scripts/dsv_bridge.py <图片> "提示词"
```

实测环境: iOS / Alpine PRoot,Python 3.11+,OpenCV 用于数美验证码自动破解。

### 关键设计决策

| 难点 | dsv 的解法 |
|---|---|
| **数美登录验证码**(点击图中最小XX) | **OpenCV 颜色分割 + 连通域定位**,无需大模型(纯像素分析秒解) |
| **PoW 反爬**(DeepSeekHashV1) | **不破解!** 用真实浏览器驱动,前端自动计算 PoW |
| **识图模式入口** | 必须先**新建对话**才显示模式切换 UI(旧会话不显示) |
| **大图上传** | 自动压缩到 1024px + base64 分块注入(规避命令行长度限制) |
| **会话堆积** | 识图完成自动 `delete` 该会话(`--keep` 可保留) |

---

## 📊 实测效果

| 测试 | 结果 |
|---|---|
| 几何图形图(红圆/绿三角/蓝方块/HELLO 123) | ✅ 全部准确识别 |
| 动物照片(俯拍两只狗) | ✅ 识别"黄白柯基血统犬 + 灰白澳牧血统犬" |
| 人物照片(樊少皇活动照) | ✅ 识别身份 + 活动 + 背景文字水印 |
| K线图(红三兵) | ✅ 判断形态 + 特征分析 |
| 性能 | ⏱ 单次约 24 秒(含模型生成 15-18s) |

---

## 📁 项目结构

```
deepseek-vision-cli/
├── dsv.py           # 主脚本(单文件, 零依赖)
├── win_browser.py   # Windows 浏览器驱动(Playwright 驱动系统 Edge, 兼容 minis-browser-use 接口)
├── README.md        # 本文件
└── SKILL.md         # 识图流程经验固化(含登录/验证码/PoW 全攻略)
```

## ⚠️ 免责声明

- 本工具仅供**个人学习与研究**使用,通过浏览器自动化操作您自己的 DeepSeek 账号
- 依赖 DeepSeek 网页结构,**官网改版可能失效**,需自行更新选择器
- 请遵守 DeepSeek 服务条款,使用后果自负
- 当 DeepSeek 官方 API 支持视觉后,建议迁移到官方 API

---

## 📜 License

MIT © menghuanshiguang

---

**⭐ 如果这个项目帮到了你,点个 Star 支持一下吧!**

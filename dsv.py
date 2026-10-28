#!/usr/bin/env python3
"""
dsv - DeepSeek C端识图CLI (供不支持视觉的大模型调用)

用法:
  dsv <图片路径> [问题文本]
  dsv /path/to/img.png "这张图里有什么?"
  echo "分析这张图" | dsv /path/to/img.png

  登录:  dsv --login <手机号>  →  dsv --verify <短信验证码>
  风控:  dsv --risk-status 查看熔断状态/最近记录 | dsv --risk-clear 手动解除 | dsv --risk-log 打印日志路径

原理:
  驱动 App 内置 WebView (minis-browser-use) 打开 chat.deepseek.com,
  用识图模式(model_type=vision)上传图片提问,返回视觉模型文本。
  PoW 由浏览器前端自动计算,无需破解。登录态存 token.txt。

输出:
  纯文本到 stdout(视觉模型的回答),日志到 stderr。

退出码:
  0 成功 | 1 参数/图片错误 | 2 用法错误 | 3 发码失败 | 4 token 失效需登录
  5 并发锁占用 | 6 风控熔断中(已写日志并停止调用)

环境变量:
  DSV_TIMEOUT         回答等待上限秒数(默认 120)
  DSV_EDGE            Windows: 指定 Edge/Chrome 可执行文件路径
  DSV_EDGE_PROFILE    Windows: 浏览器 profile 目录(默认 ~/.dsv_edge_profile)
  DSV_RISK_LOG        风控事件日志(JSONL, 默认 ~/.dsv_risk.log)
  DSV_BREAKER         熔断状态文件(默认 ~/.dsv_breaker.json)
  DSV_RISK_COOLDOWN   熔断冷却秒数(默认 3600)
"""
import sys, os, subprocess, json, time, base64, re, urllib.request, urllib.error

# ---------- 配置 ----------
TOKEN_FILE = os.path.expanduser("~/.dsv_token")
BASE = "https://chat.deepseek.com"
if sys.platform == "win32":
    # Windows: 用 Playwright 驱动系统 Edge (单文件, dsv.py 自身作子进程入口), 零浏览器下载
    B = [sys.executable, os.path.abspath(__file__), "--win-browser"]
else:
    B = ["minis-browser-use"]

def log(*a):
    print(*a, file=sys.stderr)


# ---------- 风控熔断 (2026-10-28) ----------
# 目的: 一旦被 DeepSeek 风控(频率限制/人机验证连续失败/HTTP 429/403/页面明确提示),
#   ① 往 RISK_LOG 写一行 JSONL(时间、阶段、原因、证据片段)
#   ② 写熔断状态到 BREAKER_FILE
#   ③ 冷却期内所有调用直接退出(exit 6), 不开浏览器、不碰接口 —— 越试越黑
#   ④ `dsv --risk-status` 查看状态, `dsv --risk-clear` 手动解除
# 环境变量: DSV_RISK_LOG / DSV_BREAKER / DSV_RISK_COOLDOWN(秒, 默认 3600)
EXIT_RISK = 6
RISK_LOG = os.path.expanduser(os.environ.get("DSV_RISK_LOG") or "~/.dsv_risk.log")
BREAKER_FILE = os.path.expanduser(os.environ.get("DSV_BREAKER") or "~/.dsv_breaker.json")
try:
    RISK_COOLDOWN = int(os.environ.get("DSV_RISK_COOLDOWN") or 3600)
except ValueError:
    RISK_COOLDOWN = 3600
# 命中即视为风控的页面/接口特征(中英双语, 尽量只收"明确的限制类"文案)
RISK_HINTS = ("操作过于频繁", "操作频繁", "发送过于频繁", "请求过于频繁", "访问过于频繁",
              "请稍后再试", "请稍后重试", "账号异常", "账号存在异常", "检测到异常",
              "环境异常", "行为异常", "验证过于频繁", "验证失败", "安全验证",
              "too many requests", "rate limit", "unusual activity", "verify you are human")
RISK_HTTP = (403, 429)


def _risk_now():
    return int(time.time())


def risk_state():
    """读取熔断状态(损坏/缺失都按空处理)"""
    try:
        with open(BREAKER_FILE, encoding="utf-8") as f:
            st = json.load(f)
        return st if isinstance(st, dict) else {}
    except Exception:
        return {}


def risk_active():
    """冷却期内返回状态 dict, 否则 {}"""
    st = risk_state()
    try:
        until = int(st.get("disabled_until") or 0)
    except (TypeError, ValueError):
        until = 0
    return st if until > _risk_now() else {}


def risk_clear():
    try:
        if os.path.exists(BREAKER_FILE):
            os.remove(BREAKER_FILE)
        return True
    except Exception as e:
        log("[dsv] 熔断状态清除失败:", e)
        return False


def risk_trip(reason, stage="", evidence=""):
    """命中风控: 落盘 + 熔断。返回退出码 6(调用方 sys.exit 用)"""
    ts = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    rec = {"ts": ts, "stage": stage, "reason": reason,
           "evidence": str(evidence)[:300], "cooldown_s": RISK_COOLDOWN}
    try:
        d = os.path.dirname(RISK_LOG)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(RISK_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:
        log("[dsv] ⚠️ 风控日志写入失败:", e)
    st = risk_state()
    st.update({"disabled_until": _risk_now() + RISK_COOLDOWN, "reason": reason,
               "stage": stage, "last_ts": ts,
               "hits": int(st.get("hits") or 0) + 1})
    try:
        with open(BREAKER_FILE, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False)
    except Exception as e:
        log("[dsv] ⚠️ 熔断状态写入失败:", e)
    log("[dsv] 🛑 触发风控【%s】: %s" % (stage or "-", reason))
    log("[dsv]    已记录: %s" % RISK_LOG)
    log("[dsv]    CLI 熔断 %d 分钟, 期间调用直接退出(exit %d); 手动解除: dsv --risk-clear"
        % (max(1, RISK_COOLDOWN // 60), EXIT_RISK))
    return EXIT_RISK


def risk_scan_text(text, stage=""):
    """页面文案命中风控特征 -> 熔断并返回退出码 6, 否则 None"""
    t = str(text or "")
    for h in RISK_HINTS:
        if h in t:
            return risk_trip(h, stage, t[:300])
    return None


def risk_guard():
    """入口守卫: 冷却期内直接退出, 不开浏览器不发请求"""
    st = risk_active()
    if not st:
        return
    until = time.strftime("%Y-%m-%d %H:%M:%S",
                          time.localtime(int(st.get("disabled_until") or 0)))
    log("[dsv] ⏸ 风控熔断中(至 %s), 本次调用已跳过。" % until)
    log("[dsv]    原因: %s (阶段: %s, 累计 %s 次)"
        % (st.get("reason"), st.get("stage") or "-", st.get("hits") or 1))
    log("[dsv]    查看: dsv --risk-status    手动解除: dsv --risk-clear")
    sys.exit(EXIT_RISK)


def risk_print_status():
    st = risk_state()
    if not st:
        log("[dsv] 无熔断记录(%s 不存在或为空)" % BREAKER_FILE)
    else:
        until = int(st.get("disabled_until") or 0)
        left = max(0, until - _risk_now())
        log("[dsv] 熔断状态: %s" % ("冷却中, 剩余 %d 分钟" % (left // 60) if left else "已过期(可正常使用)"))
        log("[dsv]   原因: %s | 阶段: %s | 累计: %s 次 | 最近: %s"
            % (st.get("reason"), st.get("stage") or "-", st.get("hits") or 1, st.get("last_ts")))
    if os.path.exists(RISK_LOG):
        with open(RISK_LOG, encoding="utf-8") as f:
            lines = f.readlines()
        log("[dsv] 风控日志: %s (共 %d 条)" % (RISK_LOG, len(lines)))
        for ln in lines[-5:]:
            log("[dsv]   " + ln.strip())


def run(args, timeout=120):
    """执行 minis-browser-use 命令,返回 JSON"""
    try:
        r = subprocess.run(B + args, capture_output=True, text=True, timeout=timeout)
        out = r.stdout.strip()
        if not out:
            return {"error": r.stderr[-500:]}
        return json.loads(out)
    except subprocess.TimeoutExpired:
        return {"error": "timeout"}
    except Exception as e:
        return {"error": str(e)}

def js(script):
    """执行页面 JS,返回纯净值"""
    r = run(["execute_js", "--script", script])
    # 递归提取 text
    def extract(v):
        if isinstance(v, dict):
            if "text" in v:
                return clean(v["text"])
            # data 字段继续往下
            for k in ("data", "value", "result"):
                if k in v:
                    return extract(v[k])
            return v
        return v
    def clean(s):
        if isinstance(s, str):
            # 去掉 minis-browser-use 附加的 "\n  tab_id: N" 尾巴
            import re
            s = re.sub(r"\n\s*tab_id:\s*\d+", "", s).strip()
        return s
    return extract(r)

def get_token():
    """读取持久化 token 并调 /api/v0/users/current 校验有效性。

    接口统一外壳: {"code":0,"msg":"","data":{...}}
      - 成功     : code=0  且 data.biz_code=0
      - token失效: code=40003, data=null  ← 旧实现 `d.get("data",{}).get(...)` 在这里
                  抛 `'NoneType' object has no attribute 'get'`, 把真因(40003 invalid token)
                  遮蔽成一句"验证异常", 上层只能瞎猜
      - 缺token  : code=40002 Missing Token
    """
    if not os.path.exists(TOKEN_FILE):
        return None
    with open(TOKEN_FILE) as f:
        t = f.read().strip()
    if not t:
        return None
    # 验证有效性
    try:
        req = urllib.request.Request(BASE + "/api/v0/users/current",
            headers={"Authorization": "Bearer " + t, "accept": "application/json",
                     "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            d = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        if e.code in RISK_HTTP:
            log("[dsv] token 校验返回 HTTP %s" % e.code)
            sys.exit(risk_trip("HTTP %s" % e.code, "token-check", str(e)[:200]))
        log("[dsv] token验证异常(HTTP %s): %s" % (e.code, e))
        return None
    except Exception as e:
        log("[dsv] token验证异常(网络/解析):", e)
        return None

    if not isinstance(d, dict):
        log("[dsv] token校验异常: 响应非 JSON 对象:", str(d)[:120])
        return None
    data = d.get("data")
    if not isinstance(data, dict):
        data = {}
    if d.get("code") == 0 and data.get("biz_code", 0) == 0:
        return t
    log("[dsv] token 校验未通过: code=%s msg=%s" % (d.get("code"), d.get("msg")))
    log("[dsv] 需重新登录: dsv --login <手机号> → dsv --verify <验证码>")
    return None

def ensure_login():
    """确保浏览器处于登录态,注入token"""
    tok = get_token()
    if not tok:
        # 由 main 统一处理(exit 4 + 登录指引, 符合 SKILL.md 契约)
        return None
    # 打开首页
    run(["navigate", "--url", BASE + "/"])
    time.sleep(1.0)
    # 注入 token 到 localStorage 并刷新
    js("localStorage.setItem('userToken', JSON.stringify({value:%s, __version:'0'})); location.reload();" % json.dumps(tok))
    time.sleep(1.5)
    return tok

def open_new_chat():
    """开新对话(关键: 新对话才显示模式切换UI)"""
    # 若当前已是空白新对话(无历史消息)则直接复用, 省一次navigate
    cur = js("""
    var marks=document.querySelectorAll('.ds-markdown');
    return String(marks.length === 0);
    """)
    if str(cur).strip().lower() == "true":
        return
    # 否则导航到根路径(即新对话)
    run(["navigate", "--url", BASE + "/"])
    time.sleep(1.5)
    # 尝试点"开启新对话"按钮
    js("""
    var nodes=[...document.querySelectorAll('div,span,button,[role=button]')];
    var t=null;
    for(var i=0;i<nodes.length;i++){var e=nodes[i];var x=(e.innerText||'').trim();if(x==='开启新对话'){t=e;break;}}
    if(!t) return 'no_btn';
    t.click(); return 'clicked';
    """)
    time.sleep(1.0)  # 等新对话渲染

def switch_vision():
    """切换识图模式(带重试)。若已在识图模式则直接返回 True"""
    # 先检测是否已在识图模式(欢迎语或标签激活态)
    in_vision = js("""
    var t=document.body.innerText;
    var welcome = t.indexOf('使用识图模式开始对话')>=0;
    // 识图模式标签是否有激活样式
    var nodes=[...document.querySelectorAll('div,span,button')];
    var active=false;
    for(var i=0;i<nodes.length;i++){var e=nodes[i];var x=(e.innerText||'').trim();
      if(x==='识图模式'){var c=(''+e.className); if(c.indexOf('active')>=0||c.indexOf('selected')>=0||e.getAttribute('aria-selected')==='true'){active=true;}}}
    return String(welcome || active);
    """)
    if str(in_vision).strip().lower() == "true":
        log("[dsv] 已在识图模式")
        return True
    for attempt in range(3):
        js("""
        var nodes=[...document.querySelectorAll('div,span,button')];
        var t=null;
        for(var i=0;i<nodes.length;i++){var e=nodes[i];var x=(e.innerText||'').trim();
          if(x==='识图模式' && e.children.length===0){t=e;break;}}
        if(!t) return 'not_found';
        t.click(); return 'clicked';
        """)
        time.sleep(0.5)
        r2 = js("String(document.body.innerText.indexOf('使用识图模式开始对话')>=0)")
        if r2 is True or r2 == "true" or r2 == "True":
            return True
        if attempt < 2:
            time.sleep(1)
    return False

def wait_for(js_script, timeout=30, interval=1.0, desc="条件"):
    """轮询执行 JS 直到返回 'true'(网页端状态就绪),超时返回 False"""
    start = time.time()
    while time.time() - start < timeout:
        r = js(js_script)
        if os.environ.get("DSV_DEBUG"):
            log(f"[dsv] wait[{desc}] -> {str(r).strip()!r}")
        if str(r).strip().lower() == "true":
            return True
        time.sleep(interval)
    log(f"[dsv] 等待超时({timeout}s): {desc}")
    return False

def _prep_image(path):
    """压缩单张图片到识别够用大小,返回 (字节串, 文件名, mime)"""
    if path.lower().endswith((".heic", ".heif")):
        log("[dsv] ⚠️ HEIC格式不支持, 请先用iPhone相册导出为JPG再识别")
        raise ValueError("HEIC unsupported")
    from PIL import Image as PILImage, ImageOps as PILImageOps
    tmp = None
    try:
        tmp = path + ".dsv_tmp.jpg"
        im = PILImage.open(path)
        im = PILImageOps.exif_transpose(im)  # 手机照片EXIF旋转转正
        im.thumbnail((1024, 1024), PILImage.LANCZOS)
        im.convert("RGB").save(tmp, "JPEG", quality=70)
        if os.path.getsize(tmp) < os.path.getsize(path):
            path = tmp
        else:
            os.remove(tmp); tmp = None
    except Exception as e:
        log("[dsv] 压缩跳过:", e)
    with open(path, "rb") as f:
        data = f.read()
    if tmp and os.path.exists(tmp):
        os.remove(tmp)
    ext = (path.rsplit(".", 1)[-1].lower() if "." in path else "png")
    mime = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
            "gif": "image/gif", "webp": "image/webp"}.get(ext, "image/jpeg")
    return data, f"img_{len(data)}.{ext}", mime

def _inject_file_to_window(data, name, mime):
    """把单张图的字节串 b64 分块注入到 window.__dsvFiles,返回在数组中的下标"""
    b64 = base64.b64encode(data).decode()
    # Windows 命令行参数上限 32767 字符(实测超限报 WinError 206), 分块必须更小;
    # 留余量避免仓库路径过长时撞上限; iSH/minis-browser-use 保持原 300000 行为不变
    CHUNK = 28000 if sys.platform == "win32" else 300000
    js("if(!window.__dsvFiles) window.__dsvFiles=[];")
    head = f"var i=window.__dsvFiles.length; window.__dsvFiles.push({{b64:'',name:{json.dumps(name)},mime:{json.dumps(mime)}}}); return 'started_' + i;"
    js(head)
    total = len(b64)
    for i in range(0, total, CHUNK):
        chunk = b64[i:i+CHUNK]
        js("var k=window.__dsvFiles.length-1; window.__dsvFiles[k].b64 += %s; return window.__dsvFiles[k].b64.length;" % json.dumps(chunk))
    return total

def upload_images(paths):
    """多图一次注入: 把多张图全部放进一个 FileList,模拟一次选择多个文件"""
    if not paths:
        return "0"
    # 0) 注入所有图片 b64 到 window.__dsvFiles (支持超大图分块,规避命令行限制)
    js("window.__dsvFiles=[];")
    total_chars = 0
    prepped = []
    for p in paths:
        data, name, mime = _prep_image(p)
        prepped.append((data, name, mime))
        n = _inject_file_to_window(data, name, mime)
        total_chars += n
        log(f"[dsv] 已准备 #{len(prepped)} ({name}): {n}字符")
    log(f"[dsv] 共准备 {len(prepped)} 张图, base64 {total_chars}字符")
    # 1) 逐个把 b64 解码成 File 加入一个 DataTransfer
    first_file_js = """
    var fs = window.__dsvFiles;
    window.__dsvDT = new DataTransfer();
    for(var i=0;i<fs.length;i++){
      var b64 = fs[i].b64;
      var binary = atob(b64);
      var arr = new Uint8Array(binary.length);
      for(var j=0;j<binary.length;j++) arr[j]=binary.charCodeAt(j);
      var blob = new Blob([arr], {type: fs[i].mime});
      var f = new File([blob], fs[i].name, {type: fs[i].mime});
      window.__dsvDT.items.add(f);
    }
    return 'dt_' + window.__dsvDT.items.length;
    """
    r = js(first_file_js)
    log("[dsv] DataTransfer 构造:", r)
    # 2) 把 dt.files 赋给 input[type=file]
    js("""
    var dt = window.__dsvDT;
    var input = document.querySelector('input[type=file]');
    if(!input) return 'no_input';
    input.files = dt.files;
    input.dispatchEvent(new Event('change', {bubbles:true}));
    window.__dsvUploaded = 1;
    return 'injected_' + input.files.length;
    """)
    # 3) 等上传完成: 网页端出现 >=len 个 blob 缩略图
    for retry in range(2):
        ok = wait_for(f"""
        var imgs=[...document.querySelectorAll('img')].filter(function(im){{return /^blob:/.test(im.src);}});
        return String(imgs.length >= {len(prepped)});
        """, timeout=30, interval=0.3, desc=f"上传{len(prepped)}张缩略图出现(尝试{retry+1})")
        if ok:
            break
        log(f"[dsv] 上传缩略图未出现, 重试注入({retry+1})...")
        js("""
        var dt = window.__dsvDT;
        var input = document.querySelector('input[type=file]');
        if(!input) return 'no_input';
        input.files = dt.files;
        input.dispatchEvent(new Event('change', {bubbles:true}));
        return 're-injected';
        """)
    else:
        log("[dsv] 警告: 上传缩略图始终未出现(可能部分失败)")
        return "0"
    time.sleep(1)
    return "1"

def send_and_wait(prompt, timeout=None):
    """输入问题,发送(等发送成功),轮询回答直到稳定"""
    if timeout is None:
        timeout = int(os.environ.get("DSV_TIMEOUT", "120") or 120)
    # 记录发送前最后一条回答的内容作基线(数量+文本, 兼容欢迎语占用 .ds-markdown 的场景)
    # 必须在发送前采样: 发送后采样会与快速回复竞态(回复完成时基线直接采到完成态, 轮询永不触发)
    try:
        base_info = js("""
        var m=document.querySelectorAll('.ds-markdown');
        return JSON.stringify({n:m.length, last:m.length? m[m.length-1].innerText : ''});
        """)
        import json as _j2
        _bi = _j2.loads(str(base_info).strip())
        base_n = int(_bi.get("n", 0))
        base_txt = _bi.get("last", "")
        log(f"[dsv] baseline(发送前): n={base_n} len={len(base_txt)}")
    except Exception:
        base_n, base_txt = 0, ""
        log("[dsv] baseline: 0(exc)")
    # 输入问题
    js("""
    var ta=document.querySelector('textarea');
    if(!ta) return 'no_ta';
    var setter=Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,'value').set;
    setter.call(ta, arguments);
    ta.dispatchEvent(new Event('input',{bubbles:true}));
    """.replace("arguments", json.dumps(prompt)))
    time.sleep(0.5)
    # 发送
    r = js("""
    var ta=document.querySelector('textarea');
    var c=ta.closest('div[class*=input],div[class*=composer],div[class*=footer]')||ta.parentElement.parentElement;
    var btns=c.querySelectorAll('div[role=button]');
    for(var i=0;i<btns.length;i++){var x=(''+btns[i].className);
      if(x.indexOf('ds-button--primary')>=0 && x.indexOf('iconLabelPrimary')<0){btns[i].click();return 'sent';}}
    return 'no_send';
    """)
    log("[dsv] 发送:", r)
    # 等发送成功: textarea 被清空 或 出现新消息(网页端发送成功标志)
    sent_ok = wait_for("""
    var ta=document.querySelector('textarea');
    var cleared = !ta || ta.value.length === 0;
    return String(cleared);
    """, timeout=10, interval=0.8, desc="textarea清空(发送成功)")
    if not sent_ok:
        # 兜底: 用 Enter 键发送
        log("[dsv] 按钮发送疑似失败,尝试 Enter")
        js("""
        var ta=document.querySelector('textarea');
        if(!ta) return 'no_ta';
        ta.focus();
        var ev=new KeyboardEvent('keydown',{key:'Enter',code:'Enter',keyCode:13,which:13,bubbles:true,cancelable:true});
        ta.dispatchEvent(ev);
        return 'enter_sent';
        """)
        wait_for("""
        var ta=document.querySelector('textarea');
        return String(!ta || ta.value.length === 0);
        """, timeout=10, interval=0.8, desc="Enter发送后清空")
    # 发送后立即检测错误(违规/被拒时快速失败)
    time.sleep(2)
    err_early = js("""
    var t=document.body.innerText;
    if(t.indexOf('违反使用规范')>=0 || t.indexOf('消息未能发送')>=0){return 'rejected';}
    return 'ok';
    """)
    if str(err_early).strip() == "rejected":
        return "(DeepSeek拒绝处理: 内容违反使用规范或消息未能发送 - 换个图片/提示词)"
    # 等回答开始出现: 消息条数增加 或 最后一条内容变化(流式生成)
    poll_js = """
    var m=document.querySelectorAll('.ds-markdown');
    if(!m.length) return 'false';
    var last = m[m.length-1].innerText;
    return String(m.length > %d || last !== %s);
    """ % (base_n, json.dumps(base_txt))
    appeared = wait_for(poll_js, timeout=timeout, interval=1, desc="回答开始出现")
    if not appeared:
        # 超时: 先看页面是不是弹了风控提示(频率限制/异常环境), 命中就熔断
        page = str(js("return document.body ? (document.body.innerText || '') : '';") or "")
        rc = risk_scan_text(page, "answer")
        if rc:
            sys.exit(rc)
        # 兜底: 直接取最后一条文本
        cur = js("""
        var m=document.querySelectorAll('.ds-markdown');
        return m.length? m[m.length-1].innerText : '';
        """)
        return str(cur) if cur else "(超时未获取回答)"
    # 等回答稳定: 文本长度连续2次不再增长即视为完成(网页端回复结束标志)
    start = time.time()
    last_len = -1
    stable_count = 0
    last_text = ""
    while time.time() - start < timeout:
        # 错误检测: 消息被拒/违规时快速失败,不傻等
        err = js("""
        var t=document.body.innerText;
        if(t.indexOf('违反使用规范')>=0 || t.indexOf('消息未能发送')>=0){return 'rejected';}
        return 'ok';
        """)
        if str(err).strip() == "rejected":
            return "(DeepSeek拒绝处理: 内容违反使用规范或消息未能发送 - 换个图片/提示词)"
        # 完成检测: 生成中"停止生成"按钮存在, 消失且内容>=15字=完成
        js("""
        var btns=[...document.querySelectorAll('div[role=button],button')];
        for(var i=0;i<btns.length;i++){var x=(''+(btns[i].innerText||'')).trim();
          if(x.indexOf('停止')>=0||x.indexOf('Stop')>=0){return 'false';}}
        return 'true';
        """)
        cur = js("""
        var m=document.querySelectorAll('.ds-markdown');
        return m.length? m[m.length-1].innerText : '';
        """)
        s = str(cur)
        l = len(s)
        if l > 0 and l == last_len and s == last_text:
            stable_count += 1
            if stable_count >= 2:  # 长度连续2次不变(~2s) → 回复完成
                return s
        elif l > 0:
            last_len = l
            last_text = s
            stable_count = 0
        time.sleep(0.5)
    # 整段超时仍无稳定回答: 同样检查风控提示
    page = str(js("return document.body ? (document.body.innerText || '') : '';") or "")
    rc = risk_scan_text(page, "answer-timeout")
    if rc:
        sys.exit(rc)
    return last_text if last_text else "(超时未获取回答)"

DEFAULT_PROMPT = ("请逐一分析这张图片:【场景/主体】整体是什么,【细节清单】每个可辨认的对象/人物/文字/符号及位置,"
                  "【文字内容】图中所有文字逐字转写,【颜色/构图】颜色分布与光影,【你的推断】总结图片想表达什么。"
                  "信息密度越高越好,不确定就标注'不确定',用中文条目输出。")

def delete_session(session_id, tok):
    """删除指定会话(用后即删,避免会话列表混乱)"""
    try:
        req = urllib.request.Request(BASE + "/api/v0/chat_session/delete",
            data=json.dumps({"chat_session_id": session_id}).encode(),
            headers={"Authorization": "Bearer " + tok, "accept": "application/json",
                     "content-type": "application/json",
                     "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120"},
            method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            d = json.loads(resp.read())
            return d.get("data", {}).get("biz_code") == 0
    except Exception as e:
        log("[dsv] 删除会话失败:", e)
        return False

def get_current_session_id():
    """从当前页面 URL 提取 chat_session_id"""
    r = js("return location.href;")
    url = str(r) if r else ""
    # 格式: /a/chat/s/<session_id>
    if "/a/chat/s/" in url:
        sid = url.split("/a/chat/s/")[-1].split("?")[0].strip()
        if len(sid) == 36:  # uuid
            return sid
    log("[dsv] 未识别到会话URL:", url[:80])
    return None

def solve_captcha_with_opencv():
    """从页面数美验证弹窗下载验证图, 用OpenCV定位目标, 返回页面点击坐标"""
    # 取验证图 URL 和页面位置
    info = js("""
    var imgs=[...document.querySelectorAll('img')].filter(function(im){return /fengkong/.test(im.src||'');});
    if(!imgs.length) return 'NO_IMG';
    var im=imgs[0]; var b=im.getBoundingClientRect();
    return [im.src, b.x, b.y, b.width, b.height, im.naturalWidth, im.naturalHeight].join('|');
    """)
    if not info or info == 'NO_IMG' or not isinstance(info, str):
        log("[dsv] 未找到验证图")
        return None
    try:
        parts = str(info).strip().split("|")
        d = {"src": parts[0], "x": float(parts[1]), "y": float(parts[2]),
             "w": float(parts[3]), "h": float(parts[4]),
             "nw": float(parts[5]), "nh": float(parts[6])}
    except Exception:
        log("[dsv] 验证图信息解析失败:", info)
        return None
    # 下载图片
    import urllib.request as _u
    import tempfile as _tf
    tmp = os.path.join(_tf.gettempdir(), "dsv_captcha.jpg")
    try:
        _u.urlretrieve(d["src"], tmp)
    except Exception as e:
        log("[dsv] 验证图下载失败:", e)
        return None
    # OpenCV 分析: HSV 提取目标色 → 连通域 → 中心
    try:
        import numpy as np
        from PIL import Image
        from scipy import ndimage
        img = np.array(Image.open(tmp).convert('HSV'), dtype=int)
        H, S, V = img[:,:,0], img[:,:,1], img[:,:,2]
        # 题目文本(从弹窗读)
        text = js("""
        var d=[...document.querySelectorAll('[class*=modal]')].map(function(e){return e.innerText||'';}).join(' ');
        return String(d);
        """)
        # 提取颜色词 (PIL HSV 范围 0-255!)
        text = js("""
        var d=[...document.querySelectorAll('[class*=modal]')].map(function(e){return e.innerText||'';}).join(' ');
        return String(d);
        """)
        # 颜色词 → PIL HSV 掩码 (H: 0-255, 红≈0/255, 黄≈30, 绿≈85, 蓝≈150)
        color = None
        for c in [("红", (H<=20)|(H>=230)), ("黄", (H>=25)&(H<=60)),
                  ("绿", (H>=70)&(H<=140)), ("蓝", (H>=140)&(H<=190))]:
            if c[0] in str(text):
                color = c[1]
                log(f"[dsv] 目标色: {c[0]}")
                break
        if color is None:
            color = (H>=25)&(H<=60)  # 默认黄色
        mask = color & (S>50) & (V>80)
        lab, n = ndimage.label(mask.astype(np.uint8))
        comps = []
        for i in range(1, n+1):
            ys, xs = np.where(lab == i)
            if len(ys) >= 30:
                comps.append((len(ys), int(xs.min()), int(xs.max()), int(ys.min()), int(ys.max())))
        if not comps:
            log("[dsv] 未找到目标色物体")
            return None
        comps.sort(reverse=True)
        # 题目含"最小"选面积最小, 否则选最大(通常唯一)
        if "最小" in str(text):
            comp = min(comps)
        else:
            comp = comps[0]
        cx_img = (comp[1] + comp[2]) / 2
        cy_img = (comp[3] + comp[4]) / 2
        # 映射到页面: css = img坐标 × (w/nw) + 左上角
        scale_x = d["w"] / d["nw"]
        scale_y = d["h"] / d["nh"]
        px = d["x"] + cx_img * scale_x
        py = d["y"] + cy_img * scale_y
        log(f"[dsv] 验证目标: 图片中心({int(cx_img)},{int(cy_img)}) → 页面({int(px)},{int(py)})")
        return (int(px), int(py))
    except Exception as e:
        log("[dsv] OpenCV分析失败:", e)
        return None

# 发码成功标志: 出现倒计时「N 秒后可再次获取」。
# 新版前端文案不固定(秒后重新获取/秒后再发), 用宽松列表 + 正则兜底
CODE_SENT_HINTS = ("秒后可再次获取", "秒后可重新获取", "秒后重新获取",
                   "秒后可再次发送", "秒后重发", "Resend after", "Resend in")
CODE_SENT_RE = re.compile(r"\d+\s*(?:秒|s)\s*后\s*(?:可)?\s*(?:再次|重新)?\s*(?:获取|发送|重发)")
# 明确失败提示: 命中就不必再等, 也不必再点验证码
SEND_ERR_HINTS = ("发送过于频繁", "请求过于频繁", "操作过于频繁", "操作频繁",
                  "请稍后再试", "发送失败", "验证失败", "网络异常",
                  "Too many requests", "try again later")


def _page_text():
    return str(js("return document.body ? (document.body.innerText || '') : '';") or "")


def _wait_code_sent(timeout=12):
    """轮询等待「短信已发出」的标志, 返回 (sent, reason)。

    旧实现点完「发送验证码」只 sleep 3s 看一次文本, 而新版前端倒计时渲染有
    2~6s 延迟 -> 短信其实已经发出, CLI 却报"未发送成功"要求重试(2026-10-27 实测踩到)。
    """
    end = time.time() + timeout
    while time.time() < end:
        t = _page_text()
        if any(h in t for h in CODE_SENT_HINTS) or CODE_SENT_RE.search(t):
            return True, "countdown"
        for h in SEND_ERR_HINTS:
            if h in t:
                return False, "error:" + h
        time.sleep(1)
    return False, "timeout"


def do_login(phone):
    """自动登录: 填手机号 → 破数美验证 → 等短信 → 登录 → 存token"""
    if not phone:
        log("用法: dsv --login <手机号>")
        sys.exit(2)
    log(f"[dsv] 开始登录: {phone[:3]}****{phone[-2:]}")
    # 1. 打开登录页
    run(["navigate", "--url", BASE + "/sign_in"])
    time.sleep(2)
    # 2. 填手机号
    js("""
    var ins=[...document.querySelectorAll('input')];
    var p=ins.find(function(i){return i.placeholder==='请输入手机号';});
    if(!p) return 'NO_INPUT';
    var s=Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set;
    s.call(p,%s); p.dispatchEvent(new Event('input',{bubbles:true}));
    return 'ok';
    """ % json.dumps(phone))
    time.sleep(0.5)
    # 3. 点"发送验证码"
    js("""
    var all=[...document.querySelectorAll('*')];
    for(var i=0;i<all.length;i++){var e=all[i];
      if(e.children.length===0&&(e.textContent||'').trim()==='发送验证码'){e.click();return 'clicked';}}
    return 'NO_BTN';
    """)
    time.sleep(2)
    # 4. 处理数美验证 + 等发码(轮询判定, 一次判断不作为依据)
    sent, why = False, "timeout"
    cap_fail = 0          # 连续人机验证未通过次数(≥3 视为被风控盯上)
    for attempt in range(4):
        ok, why = _wait_code_sent(12 if attempt else 4)
        if ok:
            log("[dsv] ✅ 短信已发送(检测到倒计时)")
            sent = True
            break
        if why.startswith("error:"):
            hint = why.split(":", 1)[1]
            log("[dsv] ⚠️ 页面提示:", hint)
            if hint in RISK_HINTS:
                sys.exit(risk_trip(hint, "login-send-code", hint))
            break
        has = js("""var t=document.body.innerText||''; return String(t.indexOf('点击图中')>=0);""")
        if str(has).strip().lower() != "true":
            log("[dsv] 未检测到验证弹窗, 继续等待发码...")
            continue
        log(f"[dsv] 数美验证出现, OpenCV破解中(第{attempt+1}次)...")
        pt = solve_captcha_with_opencv()
        if not pt:
            cap_fail += 1
            log("[dsv] 验证图分析失败, 等新题...")
            time.sleep(2)
            continue
        js("""
        var el=document.elementFromPoint(%d,%d);
        [['mousedown',1],['mouseup',1],['click',1]].forEach(function(ev){
          var e=new MouseEvent(ev[0],{clientX:%d,clientY:%d,button:0,bubbles:true,cancelable:true,view:window});
          if(el) el.dispatchEvent(e);
        });
        return 'clicked';
        """ % (pt[0], pt[1], pt[0], pt[1]))
        cap_fail += 1
        log("[dsv] 已点击验证目标, 等待发码...")
    # 5. 异步化: 发码后立即退出, 不阻塞调用方!
    #    用户收到短信后, 单独运行: dsv --verify <验证码> 完成登录
    if not sent:
        if cap_fail >= 3:
            sys.exit(risk_trip("连续 %d 次人机验证未通过(疑似被风控)" % cap_fail,
                               "login-captcha"))
        log("[dsv] ⚠️ 未能确认短信是否已发送(风控未过/触发频率限制/倒计时未出现)。")
        log("[dsv]    若手机已收到验证码, 请直接运行: dsv --verify <验证码>")
        log("[dsv]    否则等待 30s 后重试: dsv --login <手机号>")
        sys.exit(3)
    log("[dsv] ⏭ 短信已发送。CLI 立即退出(不阻塞调用方)。")
    log("[dsv] 收到验证码后, 请运行: dsv --verify <验证码>")
    log("[dsv] (验证码 5 分钟内有效)")
    return True

def do_verify(code):
    """用短信验证码完成登录(独立命令, 快速执行不阻塞)"""
    if not code:
        log("用法: dsv --verify <短信验证码>")
        sys.exit(2)
    log("[dsv] 使用验证码完成登录...")
    # 确保在登录页
    url = js("return location.href;")
    if "sign_in" not in str(url):
        run(["navigate", "--url", BASE + "/sign_in"])
        time.sleep(2)
    # 1. 填验证码
    js("""
    var ins=[...document.querySelectorAll('input')];
    var c=ins.find(function(i){return i.placeholder==='请输入验证码';});
    if(!c) return 'NO_INPUT';
    var s=Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set;
    s.call(c,%s); c.dispatchEvent(new Event('input',{bubbles:true}));
    return 'ok';
    """ % json.dumps(code))
    time.sleep(0.5)
    # 2. 点登录
    js("""
    var all=[...document.querySelectorAll('div,span,button,[role=button]')];
    for(var i=0;i<all.length;i++){var e=all[i];
      if((e.innerText||'').trim()==='登录'&&e.children.length===0){e.click();return 'clicked';}}
    return 'NO_BTN';
    """)
    time.sleep(4)
    # 3. 检查是否登录成功, 提取token
    url = js("return location.href;")
    if str(url).find("chat.deepseek.com/") >= 0 and "sign_in" not in str(url):
        tok = js("""var t=localStorage.getItem('userToken'); return t? JSON.parse(t).value : '';""")
        if tok:
            with open(TOKEN_FILE, "w") as f:
                f.write(str(tok).strip())
            os.chmod(TOKEN_FILE, 0o600)
            log(f"[dsv] ✅ 登录成功! token已保存 ({len(str(tok).strip())}字符)")
            return True
        else:
            log("[dsv] 登录后未获取到 token")
    else:
        log("[dsv] 登录可能失败, 当前URL:", str(url)[:80])
        err = js("""var t=document.body.innerText; return String(t.slice(-150));""")
        log("[dsv] 页面提示:", str(err)[:150])
    return False

LOCK_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".dsv.lock")

def acquire_lock():
    """简单并发锁: 已有dsv在跑则退出, 避免浏览器状态互相干扰"""
    try:
        fd = os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return True
    except FileExistsError:
        log("[dsv] ⚠️ 另一个dsv进程正在运行, 请等待完成或删除 .dsv.lock")
        return False

def release_lock():
    try:
        os.remove(LOCK_FILE)
    except OSError:
        pass

def main():
    T0 = time.time()
    def lap(msg):
        log(f"[dsv] ⏱ {msg}: {time.time()-T0:.1f}s")
    args = sys.argv[1:]
    keep = "--keep" in args
    args = [a for a in args if a != "--keep"]
    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        return
    if args[0] == "--risk-status":
        risk_print_status()
        return
    if args[0] == "--risk-clear":
        risk_clear()
        log("[dsv] ✅ 已手动解除风控熔断")
        return
    if args[0] == "--risk-log":
        print(os.path.abspath(RISK_LOG))
        return
    # 风控熔断守卫: 冷却期内直接退出(exit 6), 不开浏览器/不碰接口
    risk_guard()
    if args[0] == "--login":
        phone = args[1] if len(args) > 1 else None
        do_login(phone)
        return
    if args[0] == "--verify":
        code = args[1] if len(args) > 1 else None
        do_verify(code)
        return
    if args[0] == "--logout":
        if os.path.exists(TOKEN_FILE):
            os.remove(TOKEN_FILE)
            log("[dsv] 已删除本地 token")
        return
    # 参数解析: 凡能匹配到本地文件/通配符的, 归为"图片"; 其余为 prompt
    import glob as _glob
    imgs, prompt_parts = [], []
    for a in args:
        if a.startswith("-"):
            continue
        # 通配符展开
        if any(c in a for c in "*?["):
            hits = _glob.glob(a)
            if hits:
                imgs.extend(hits)
                continue
        if os.path.exists(a) and os.path.isfile(a):
            imgs.append(a)
        else:
            prompt_parts.append(a)
    # 若无显式图片, 从 stdin 读取 base64? 不做, 直接报错友好提示
    imgs = list(dict.fromkeys(imgs))  # 去重
    if not imgs:
        # 兼容 stdin 管道: 图片路径不存在时提示多图用法
        print(__doc__ + "\n提示: 用法 dsv <图片1> [图片2 ...] [提示词]; 支持通配符 dsv --all '*.png' '描述' ", file=sys.stderr)
        sys.exit(1)
    prompt = " ".join(prompt_parts) if prompt_parts else DEFAULT_PROMPT
    for i in range(len(imgs)):
        if not os.path.exists(imgs[i]):
            print(f"图片不存在: {imgs[i]}", file=sys.stderr)
            sys.exit(1)
    if len(imgs) > 1:
        log(f"[dsv] 📚 多图模式: 共 {len(imgs)} 张, 将一次会话发送给识图模型")
    locked = False
    try:
        # 参数校验通过后才拿锁(校验失败无需锁, 不会残留 .dsv.lock)
        if not acquire_lock():
            sys.exit(5)
        locked = True
        tok = ensure_login(); lap('登录')
        if not tok:
            # 快速失败: 绝不阻塞调用方等待人工输入!
            log("[dsv] ❌ token 无效/缺失, 无法识图")
            log("[dsv] 请先完成登录(不阻塞): dsv --login <手机号> → 收到短信后 → dsv --verify <验证码>")
            sys.exit(4)
        open_new_chat(); lap('新对话')
        if not switch_vision():
            log("[dsv] 未找到识图模式标签, 重新开对话重试...")
            time.sleep(1.5)
            open_new_chat()
            time.sleep(1.0)
            if not switch_vision():
                log("[dsv] 警告: 识图模式切换失败,可能UI变动或页面异常")
        up = upload_images(imgs); lap('上传')
        log("[dsv] 上传状态:", up)
        if up == 0:
            log("[dsv] 警告: 未检测到上传缩略图")
        # 组装多图提示词(含每图文件名,让模型清楚对应关系)
        if len(imgs) > 1 and prompt_parts and not prompt_parts[0].startswith("请") and "图" not in prompt_parts[0]:
            pass  # 用户已给自定义提示词,不强改
        ans = send_and_wait(prompt); lap('回答')
        print(ans)
        # 用后即删(默认删除本次会话, --keep 保留)
        if not keep:
            sid = get_current_session_id()
            if sid:
                if delete_session(sid, tok):
                    log("[dsv] 已删除会话:", sid)
                else:
                    log("[dsv] 会话删除失败(可手动清理):", sid)
            else:
                log("[dsv] 未获取到会话ID,跳过删除")
    finally:
        # 任何路径(含 sys.exit/异常)都必须释放锁, 否则后续调用全被卡死
        if locked:
            release_lock()

# ================= Windows 浏览器驱动 (Playwright/Edge) =================
import sys, os, json, subprocess, time, urllib.request

# ---------- 配置 ----------
EDGE_CANDIDATES = [
    os.environ.get("DSV_EDGE", ""),
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]
EDGE = next((p for p in EDGE_CANDIDATES if p and os.path.exists(p)), None)
USER_DIR = os.path.expanduser(os.environ.get("DSV_EDGE_PROFILE", "~/.dsv_edge_profile"))
EDGE_LOG = os.path.join(USER_DIR, "edge_err.log")

_PW = None


def _pw():
    """playwright 单例(坑4: 每次 start() 会残留 driver, 同进程重连报 asyncio loop 错误)"""
    global _PW
    if _PW is None:
        from playwright.sync_api import sync_playwright
        _PW = sync_playwright().start()
    return _PW


def _get_port():
    """从 user-data-dir/DevToolsActivePort 读实际 CDP 端口(Chromium 启动时写入, 随机分配)"""
    try:
        with open(os.path.join(USER_DIR, "DevToolsActivePort")) as f:
            return int(f.readline().strip())
    except Exception:
        return None


def _connect():
    port = _get_port()
    if not port:
        raise ConnectionError("no cdp port")
    return _pw().chromium.connect_over_cdp(f"http://127.0.0.1:{port}")


def kill_stale():
    """杀掉占用本 profile 的残留浏览器进程。
    只匹配命令行里含 user-data-dir 的进程, 不影响用户日常 Edge/Chrome。
    注意: PowerShell -like 中反斜杠是字面字符, 不能转义; 但 [ ] 是通配符, 需反引号转义。"""
    # 路径中 [ ] * ? 是 -like 通配符, 用反引号转义; 单引号防注入
    esc = (USER_DIR.replace("[", "`[").replace("]", "`]")
                  .replace("*", "`*").replace("?", "`?").replace("'", "''"))
    ps = ("Get-CimInstance Win32_Process -Filter \"Name='msedge.exe' OR Name='chrome.exe'\" | "
          "Where-Object { $_.CommandLine -like '*%s*' } | "
          "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }" % esc)
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, timeout=30)
    time.sleep(1.0)


def get_page():
    """连接常驻浏览器, 返回可用页面(优先当前激活页, 退而取第一个)"""
    browser = _connect()
    ctx = browser.contexts[0]
    for p in ctx.pages:
        if not p.is_closed() and p.url and p.url != "about:blank":
            return p
    return ctx.pages[0] if ctx.pages else ctx.new_page()


def ensure_browser():
    """确保 Edge 在跑且可连接; 失败则清理残留并重启"""
    global _PW
    if not EDGE:
        print(json.dumps({"error": "未找到 Edge/Chrome。请安装 Edge, 或用环境变量 DSV_EDGE 指定浏览器路径。"}, ensure_ascii=True))
        sys.exit(1)
    try:
        return _connect()
    except Exception:
        # 坑4: 清理失败的 playwright driver, 否则重连报 asyncio loop 错误
        if _PW is not None:
            try:
                _PW.stop()
            except Exception:
                pass
            _PW = None
        kill_stale()
        os.makedirs(os.path.dirname(EDGE_LOG), exist_ok=True)
        # --remote-debugging-port=0: 随机端口, 从 DevToolsActivePort 读实际端口。
        # 避免固定 9222 撞上用户已开调试口的浏览器(连错浏览器=操作真实用户页面)。
        with open(EDGE_LOG, "a", encoding="utf-8") as ef:
            subprocess.Popen([EDGE, "--remote-debugging-port=0",
                              f"--user-data-dir={USER_DIR}", "--headless=new",
                              "--disable-blink-features=AutomationControlled", "about:blank"],
                             stdout=subprocess.DEVNULL, stderr=ef)
        # 轮询 DevToolsActivePort 文件出现(Chromium 启动完成后写入)
        for _ in range(60):
            port = _get_port()
            if port:
                try:
                    urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2)
                    break
                except Exception:
                    pass
            time.sleep(0.5)
        for _ in range(30):
            try:
                return _connect()
            except Exception:
                time.sleep(0.5)
        raise RuntimeError("浏览器 CDP 连接失败, 详见 %s" % EDGE_LOG)


def win_main():
    # 子进程入口: dsv.py --win-browser <cmd> ... (argv[1] 是 --win-browser, 裁剪掉)
    sys.argv = [sys.argv[0]] + sys.argv[2:]
    if len(sys.argv) < 2:
        print(json.dumps({"error": "no cmd"}, ensure_ascii=True))
        return
    cmd = sys.argv[1]
    ensure_browser()
    try:
        if cmd == "navigate":
            url = sys.argv[sys.argv.index("--url") + 1]
            page = get_page()
            page.goto(url, timeout=60000)
            print(json.dumps({"ok": True, "url": page.url}, ensure_ascii=True))
        elif cmd == "execute_js":
            script = sys.argv[sys.argv.index("--script") + 1]
            page = get_page()
            # 坑2: 裸 return 语句包成箭头函数体
            out = page.evaluate("() => { %s }" % script)
            print(json.dumps({"text": str(out)}, ensure_ascii=True))
        else:
            print(json.dumps({"error": "unknown cmd %s" % cmd}, ensure_ascii=True))
    except Exception as e:
        print(json.dumps({"error": "%s: %s" % (cmd, e)}, ensure_ascii=True))



if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--win-browser":
        # Windows 浏览器驱动子进程模式(单文件)
        win_main()
    else:
        main()

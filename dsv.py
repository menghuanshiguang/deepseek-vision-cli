#!/usr/bin/env python3
"""
dsv - DeepSeek C端识图CLI (供不支持视觉的大模型调用)

用法:
  dsv <图片路径> [问题文本]
  dsv /path/to/img.png "这张图里有什么?"
  echo "分析这张图" | dsv /path/to/img.png

原理:
  驱动 App 内置 WebView (minis-browser-use) 打开 chat.deepseek.com,
  用识图模式(model_type=vision)上传图片提问,返回视觉模型文本。
  PoW 由浏览器前端自动计算,无需破解。登录态存 token.txt。

输出:
  纯文本到 stdout(视觉模型的回答),日志到 stderr。
"""
import sys, os, subprocess, json, time, base64, urllib.request

# ---------- 配置 ----------
TOKEN_FILE = os.path.expanduser("~/.dsv_token")
BASE = "https://chat.deepseek.com"
B = ["minis-browser-use"]

def log(*a):
    print(*a, file=sys.stderr)

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
    """读取持久化 token,无效则提示登录"""
    if not os.path.exists(TOKEN_FILE):
        return None
    with open(TOKEN_FILE) as f:
        t = f.read().strip()
    # 验证有效性
    try:
        req = urllib.request.Request(BASE + "/api/v0/users/current",
            headers={"Authorization": "Bearer " + t, "accept": "application/json",
                     "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            d = json.loads(resp.read())
            if d.get("data", {}).get("biz_code") == 0:
                return t
    except Exception as e:
        log("[dsv] token验证异常:", e)
    return None

def ensure_login():
    """确保浏览器处于登录态,注入token"""
    tok = get_token()
    if not tok:
        log("[dsv] token 无效或缺失,请先运行 dsv --login")
        sys.exit(2)
    # 打开首页
    run(["navigate", "--url", BASE + "/"])
    time.sleep(1.2)
    # 注入 token 到 localStorage 并刷新
    js("localStorage.setItem('userToken', JSON.stringify({value:%s, __version:'0'})); location.reload();" % json.dumps(tok))
    time.sleep(2)
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
    time.sleep(1.5)  # 等新对话渲染

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
        r = js("""
        var nodes=[...document.querySelectorAll('div,span,button')];
        var t=null;
        for(var i=0;i<nodes.length;i++){var e=nodes[i];var x=(e.innerText||'').trim();
          if(x==='识图模式' && e.children.length===0){t=e;break;}}
        if(!t) return 'not_found';
        t.click(); return 'clicked';
        """)
        time.sleep(1)
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
        if str(r).strip().lower() == "true":
            return True
        time.sleep(interval)
    log(f"[dsv] 等待超时({timeout}s): {desc}")
    return False

def upload_image(path):
    """上传图片到识图模式输入框,等待网页端上传完成"""
    # 统一压缩到识别够用的大小(1024px, 质量70, 通常<250KB)
    tmp = None
    try:
        from PIL import Image as PILImage
        tmp = path + ".dsv_tmp.jpg"
        im = PILImage.open(path)
        im.thumbnail((1024, 1024), PILImage.LANCZOS)
        im.convert("RGB").save(tmp, "JPEG", quality=70)
        if os.path.getsize(tmp) < os.path.getsize(path):
            path = tmp
            log(f"[dsv] 已压缩: {os.path.getsize(tmp)//1024}KB")
    except Exception as e:
        log("[dsv] 压缩跳过:", e)
    # 读取图片转 base64
    with open(path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    if tmp and os.path.exists(tmp):
        os.remove(tmp)
    # 推断 mime
    ext = path.rsplit(".", 1)[-1].lower()
    mime = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
            "gif": "image/gif", "webp": "image/webp"}.get(ext, "image/jpeg")
    # 分块注入 base64 到 window.__b64 (命令行参数限制 ~128KB, 用100KB块)
    CHUNK = 100000
    js("window.__b64='';")
    total = len(b64)
    nchunks = (total + CHUNK - 1) // CHUNK
    for i in range(0, total, CHUNK):
        chunk = b64[i:i+CHUNK]
        js("window.__b64 += %s; return String(window.__b64.length);" % json.dumps(chunk))
    log(f"[dsv] base64注入完成: {total}字符, {nchunks}块")
    js(f"""
    var b64 = window.__b64;
    var binary = atob(b64);
    var arr = new Uint8Array(binary.length);
    for (var i=0;i<binary.length;i++) arr[i]=binary.charCodeAt(i);
    var blob = new Blob([arr], {{type:'{mime}'}});
    var f = new File([blob], 'img.{ext}', {{type:'{mime}'}});
    var dt = new DataTransfer(); dt.items.add(f);
    var input = document.querySelector('input[type=file]');
    input.files = dt.files;
    input.dispatchEvent(new Event('change', {{bubbles:true}}));
    window.__dsvUploaded = 1;
    return 'injected';
    """)
    # 等上传完成: 网页端出现 blob 缩略图(上传成功标志)
    ok = wait_for("""
    var imgs=[...document.querySelectorAll('img')].filter(function(im){return /^blob:/.test(im.src);});
    return String(imgs.length > 0);
    """, timeout=30, interval=0.6, desc="上传缩略图出现")
    if not ok:
        log("[dsv] 警告: 未检测到上传缩略图(可能上传失败)")
        return "0"
    # 等上传真正完成: 缩略图旁若有"上传中"状态则等待消失
    time.sleep(1)
    return "1"

def send_and_wait(prompt, timeout=120):
    """输入问题,发送(等发送成功),轮询回答直到稳定"""
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
    # 记录发送前最后一条回答的长度作基线
    baseline = int(js("""
    var m=document.querySelectorAll('.ds-markdown');
    return String(m.length? m[m.length-1].innerText.length : 0);
    """) or 0)
    # 等回答开始出现(网页端出现新的回复内容)
    appeared = wait_for("""
    var m=document.querySelectorAll('.ds-markdown');
    if(!m.length) return 'false';
    return String(m[m.length-1].innerText.length > %d);
    """ % baseline, timeout=timeout, interval=1, desc="回答开始出现")
    if not appeared:
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
        time.sleep(1.0)
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
    if args[0] == "--login":
        print("请用浏览器打开", BASE, "登录后,将 localStorage 的 userToken.value 写入", TOKEN_FILE)
        return
    img = args[0]
    prompt = " ".join(args[1:]) if len(args) > 1 else DEFAULT_PROMPT
    if not os.path.exists(img):
        print(f"图片不存在: {img}", file=sys.stderr)
        sys.exit(1)
    tok = ensure_login(); lap('登录')
    if not tok:
        sys.exit(2)
    open_new_chat(); lap('新对话')
    if not switch_vision():
        log("[dsv] 警告: 未找到识图模式标签,可能UI变动")
    up = upload_image(img); lap('上传')
    log("[dsv] 上传状态:", up)
    if up == 0:
        log("[dsv] 警告: 未检测到上传缩略图")
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

if __name__ == "__main__":
    main()

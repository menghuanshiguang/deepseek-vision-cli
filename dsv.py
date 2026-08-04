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

def _prep_image(path):
    """压缩单张图片到识别够用大小,返回 (字节串, 文件名, mime)"""
    from PIL import Image as PILImage
    tmp = None
    try:
        tmp = path + ".dsv_tmp.jpg"
        im = PILImage.open(path)
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
    CHUNK = 100000
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
    ok = wait_for(f"""
    var imgs=[...document.querySelectorAll('img')].filter(function(im){{return /^blob:/.test(im.src);}});
    return String(imgs.length >= {len(prepped)});
    """, timeout=30, interval=0.6, desc=f"上传{len(prepped)}张缩略图出现")
    if not ok:
        log("[dsv] 警告: 未检测到全部上传缩略图(可能部分失败)")
        return "0"
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

def solve_captcha_with_opencv():
    """从页面数美验证弹窗下载验证图, 用OpenCV定位目标, 返回页面点击坐标"""
    # 取验证图 URL 和页面位置
    info = js("""
    var imgs=[...document.querySelectorAll('img')].filter(function(im){return /fengkong/.test(im.src||'');});
    if(!imgs.length) return 'NO_IMG';
    var im=imgs[0]; var b=im.getBoundingClientRect();
    return JSON.stringify({src:im.src, x:b.x, y:b.y, w:b.width, h:b.height, nw:im.naturalWidth, nh:im.naturalHeight});
    """)
    if not info or info == 'NO_IMG' or not isinstance(info, str):
        log("[dsv] 未找到验证图")
        return None
    try:
        import json as _j
        d = _j.loads(str(info).strip())
    except Exception:
        log("[dsv] 验证图信息解析失败:", info)
        return None
    # 下载图片
    import urllib.request as _u
    tmp = "/tmp/dsv_captcha.jpg"
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
    # 4. 检查是否弹数美验证, 破解之
    for attempt in range(3):
        has = js("""var t=document.body.innerText; return String(t.indexOf('点击图中')>=0);""")
        if str(has).strip().lower() == "true":
            log(f"[dsv] 数美验证出现, OpenCV破解中(第{attempt+1}次)...")
            pt = solve_captcha_with_opencv()
            if pt:
                js("""
                var el=document.elementFromPoint(%d,%d);
                [['mousedown',1],['mouseup',1],['click',1]].forEach(function(ev){
                  var e=new MouseEvent(ev[0],{clientX:%d,clientY:%d,button:0,bubbles:true,cancelable:true,view:window});
                  if(el) el.dispatchEvent(e);
                });
                return 'clicked';
                """ % (pt[0], pt[1], pt[0], pt[1]))
                time.sleep(3)
                # 验证是否通过(弹窗消失 + 出现倒计时)
                ok = js("""var t=document.body.innerText; return String(t.indexOf('秒后可再次获取')>=0);""")
                if str(ok).strip().lower() == "true":
                    log("[dsv] ✅ 验证通过, 短信已发送!")
                    break
                else:
                    log("[dsv] 验证后未检测到倒计时, 重试...")
            else:
                log("[dsv] 验证图分析失败, 等新题...")
                time.sleep(3)
        else:
            # 无验证 → 可能直接发码了
            ok2 = js("""var t=document.body.innerText; return String(t.indexOf('秒后可再次获取')>=0);""")
            if str(ok2).strip().lower() == "true":
                log("[dsv] ✅ 短信已发送(无验证)")
                break
            time.sleep(2)
    # 5. 异步化: 发码后立即退出, 不阻塞调用方!
    #    用户收到短信后, 单独运行: dsv --verify <验证码> 完成登录
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
    tok = ensure_login(); lap('登录')
    if not tok:
        # 快速失败: 绝不阻塞调用方等待人工输入!
        log("[dsv] ❌ token 无效/缺失, 无法识图")
        log("[dsv] 请先完成登录(不阻塞): dsv --login <手机号> → 收到短信后 → dsv --verify <验证码>")
        sys.exit(4)
    open_new_chat(); lap('新对话')
    if not switch_vision():
        log("[dsv] 警告: 未找到识图模式标签,可能UI变动")
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

if __name__ == "__main__":
    main()

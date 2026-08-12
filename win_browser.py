#!/usr/bin/env python3
"""
win_browser.py - Windows 版浏览器驱动 (Playwright 驱动系统 Edge)

为 dsv.py 提供与 iSH 的 minis-browser-use 相同的命令行接口:
  win_browser.py navigate --url <url>
  win_browser.py execute_js --script <js脚本>

设计要点(踩坑后固化):
1. 驱动系统自带 Edge(channel=CDP 常驻), 零浏览器下载, 与 iSH 版"零安装"理念一致。
   若机器上装了 Chrome 且无 Edge, 可用环境变量 DSV_EDGE 指定浏览器路径。
2. dsv.py 的 JS 全是 "return xxx;" 裸语句, Playwright 不接受, 统一包成箭头函数体。
3. 输出一律 ensure_ascii JSON, 避免 Windows cp936 管道解码乱码。
4. playwright 实例做模块级单例; 连接失败时 stop() 清理, 否则同进程重连报 asyncio loop 错误。
5. Edge 常驻复用固定 user-data-dir, 登录态/页面状态跨命令保持;
   启动前按 profile 路径清理残留进程(不影响用户日常 Edge)。
6. CDP 端口随机分配(--remote-debugging-port=0, 从 DevToolsActivePort 读取),
   避免误连用户已开调试端口(9222 等)的真实浏览器。
"""
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


def main():
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
    main()

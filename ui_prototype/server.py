#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""碎片档案 · Web 案情板服务器（零依赖，Python 标准库）。

架构（网络连接复用 C 核心，本文件不重写任何网络协议）：
- 静态文件服务（根 = 项目根，前端 /ui_prototype、状态 /logs、证物 /ui_prototype/session_*）
- 案件库 API：扫描 session_* 目录，读 manifest.bin + file_3，抽取标题/摘要/真凶（本地功能）
- 现场生成新案：后台线程 subprocess 调 ai_gen_case.py（本地功能）
- 命令桥：spawn ./client_transfer，把浏览器操作翻译成 client 的 stdin 命令，
  再轮询 client 每次重绘落盘的 ui_state.json（UI_JSON_PATH）回推给前端。
  —— 进房/传输/择一的网络传输全部由 C 核心 P2P（room_server + relay_server + transfer）完成。

运行（需在 WSL/Linux 先 `make transfer` 产出 room_server / relay_server / client_transfer）：
    python ui_prototype/server.py          # 默认 8080
访问：http://localhost:8080/ui_prototype/index.html
"""
import http.server
import json
import os
import re
import socket
import struct
import subprocess
import sys
import threading
import time
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)                      # Fragment_Archive/
PYGEN = os.path.join(ROOT, "src", "ai_gen")       # 案件生成脚本目录
ENV_PATH = os.path.join(PYGEN, ".env")            # 本地密钥文件（被 .gitignore 排除，不入库）
CASES_DIR = os.path.join(ROOT, "cases")           # 统一案件存储目录
DL_DIR = os.path.join(ROOT, "ui_prototype")       # 玩家下载证物缓存目录（C 核心 --data-dir）
SUSPECTS = ["赵枢", "钱映", "孙棠"]
LOCK = threading.Lock()

# 可选案件类型（第一个为“随机”）。中文标签给前端选择，透传给生成脚本。
CASE_TYPES = ["随机", "民国租界公馆", "英伦庄园", "蒸汽时代的实验室", "偏远山间别墅",
              "南方旧宅梅雨季", "邮轮头等舱", "赛博朋克暗巷诊所", "雪山中的旅馆"]

# C 核心二进制（make transfer 产物）
CLIENT_BIN = os.path.join(ROOT, "client_transfer")
ROOM_BIN = os.path.join(ROOT, "room_server")
RELAY_BIN = os.path.join(ROOT, "relay_server")
DEVNULL = subprocess.DEVNULL


# ---------------------------------------------------------------------------
# 案件库（本地功能，不涉及网络）
# ---------------------------------------------------------------------------
def _read_manifest(sdir):
    p = os.path.join(sdir, "manifest.bin")
    if not os.path.isfile(p):
        return None
    b = open(p, "rb").read()
    if len(b) < 37:
        return None
    sid, seed_hash, fn = struct.unpack_from(">I32sB", b, 0)
    off = 37
    files = []
    for _ in range(fn):
        if off + 41 > len(b):
            break
        fid, ch, kind, fsize, _fsha, pc = struct.unpack_from(">BBBI32sH", b, off)
        off += 41 + pc * 32
        files.append({"file_id": fid, "chapter": ch, "kind": kind, "size": fsize})
    return {"session_id": sid, "seed_hash": seed_hash.hex(), "files": files}


def _read_text3(sdir):
    p = os.path.join(sdir, "file_3")
    if not os.path.isfile(p):
        return ""
    return open(p, "rb").read().decode("utf-8", "ignore")


def _title_from(body):
    first = body.split("\n", 1)[0].strip()
    m = re.search(r"案由[:：]\s*([^\s，,。；;【]+)", first)
    if m:
        return m.group(1).strip()
    m = re.search(r"编号[:：]\s*([^\s，,。；;【]+)", first)
    if m:
        return m.group(1).strip()
    m = re.search(r"【([^】]+)】", first)
    if m:
        t = m.group(1).strip()
        for junk in ("案件卷宗", "案卷正文", "案卷"):
            t = t.replace(junk, "")
        t = t.strip(" ：:，,。")
        if t:
            return t
    return first[:16] or "未命名案件"


def _extract_case(sdir):
    m = _read_manifest(sdir)
    if not m:
        return None
    txt = _read_text3(sdir)
    body = txt.split("[判据]")[0].strip()
    murderer = None
    i = txt.find("[判据] 真凶：")
    if i >= 0:
        name = txt[i + len("[判据] 真凶："):].split("\n", 1)[0].strip()
        if name in SUSPECTS:
            murderer = name
    return {
        "sid": "%08x" % m["session_id"],
        "title": _title_from(body),
        "summary": body[:160],
        "files": m["files"],
        "murderer": murderer,
        "seed_hash": m["seed_hash"],
    }


def _find_session_dir(sid):
    # 统一案件目录优先；其次玩家下载缓存（历史/现场预览可能尚未下载到缓存）
    for base in (CASES_DIR, DL_DIR):
        d = os.path.join(base, "session_" + sid)
        if os.path.isdir(d) and os.path.isfile(os.path.join(d, "manifest.bin")):
            return d
    return None


def list_cases():
    """只扫描统一案件目录 cases/，有内容才返回。"""
    seen = {}
    if not os.path.isdir(CASES_DIR):
        return []
    try:
        names = sorted(os.listdir(CASES_DIR))
    except OSError:
        return []
    for name in names:
        if not name.startswith("session_"):
            continue
        sdir = os.path.join(CASES_DIR, name)
        c = _extract_case(sdir)
        if not c or c["sid"] in seen:
            continue
        c["mtime"] = os.path.getmtime(os.path.join(sdir, "manifest.bin"))
        seen[c["sid"]] = c
    return sorted(seen.values(), key=lambda c: c["mtime"], reverse=True)


def _latest_sid():
    cs = list_cases()
    return cs[0]["sid"] if cs else None


# ---------------------------------------------------------------------------
# 本地密钥（.env）读写：仅返回"是否已配置/是否为空"，绝不把密钥明文回传前端
# ---------------------------------------------------------------------------
_ENV_KEYS = ("ARK_API_KEY", "ARK_MODEL", "ARK_LLM_MODEL")


def _read_env():
    """读 .env 成 dict（不存在则空）。只解析 KEY=VALUE，忽略注释/空行。"""
    vals = {}
    if os.path.isfile(ENV_PATH):
        with open(ENV_PATH, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                vals[k.strip()] = v.strip()
    return vals


def _key_status():
    """给前端的密钥状态：只报已配置与否和掩码，不回传明文。"""
    vals = _read_env()

    def mask(v):
        if not v:
            return ""
        return (v[:4] + "****" + v[-2:]) if len(v) > 8 else "****"

    return {
        "configured": any(vals.get(k) for k in _ENV_KEYS),
        "has_api_key": bool(vals.get("ARK_API_KEY")),
        "fields": {k: mask(vals.get(k, "")) for k in _ENV_KEYS},
    }


def _write_env(updates):
    """updates: {KEY: value}。
    约定：键不在 updates 中 → 保持原值不变；值为非空串 → 写入/覆盖；空串 → 删除该键。"""
    vals = _read_env()
    for k in _ENV_KEYS:
        if k not in updates:
            continue                              # 前端没提交该字段 → 保留现状
        v = (updates.get(k) or "").strip()
        if v:
            vals[k] = v
        else:
            vals.pop(k, None)
    lines = [
        "# 方舟（Ark）云端接口凭证 —— 由网页设置写入；本文件已被 .gitignore 排除，请勿提交",
        "# 仅配置 ARK_API_KEY 即可：出图/剧本会自动降级为程序化生成，无需填写下面两项。",
    ]
    for k, comment in (
        ("ARK_API_KEY", "# 方舟 API 密钥"),
        ("ARK_MODEL", "# 文生图接入点 endpoint id（可选）"),
        ("ARK_LLM_MODEL", "# 大模型接入点 endpoint id（可选）"),
    ):
        if vals.get(k):
            lines += [comment, f"{k}={vals[k]}"]
    with open(ENV_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    try:
        os.chmod(ENV_PATH, 0o600)
    except OSError:
        pass
    return _key_status()


# ---------------------------------------------------------------------------
# 现场生成新案（本地功能，后台线程）
# ---------------------------------------------------------------------------
GEN_BUSY = {"busy": False}
GEN_LAST = {"error": "", "sid": ""}


def _generate_async(genre=""):
    if GEN_BUSY["busy"]:
        return False
    genre = (genre or "").strip()
    if genre and genre not in CASE_TYPES[1:]:
        genre = ""
    GEN_BUSY["busy"] = True
    GEN_LAST["error"] = ""
    GEN_LAST["sid"] = ""

    def work():
        os.makedirs(CASES_DIR, exist_ok=True)
        before = {c["sid"] for c in list_cases()}
        try:
            argv = [sys.executable, "ai_gen_case.py", "--base", CASES_DIR,
                    "--dotenv", os.path.join(PYGEN, ".env")]
            if genre:
                argv += ["--genre", genre]
            r = subprocess.run(argv, cwd=PYGEN, capture_output=True,
                               text=True, timeout=600)
            if r.returncode != 0:
                detail = (r.stderr or r.stdout or "").strip().splitlines()
                GEN_LAST["error"] = (detail[-1] if detail else
                                     f"生成进程退出码 {r.returncode}")[:200]
                return
            # 成功：找出本次新增的 sid（可能不止一个目录时取最新）
            fresh = [c for c in list_cases() if c["sid"] not in before]
            if not fresh:
                GEN_LAST["error"] = "生成结束但未发现新案卷（请检查磁盘/权限）"
                return
            GEN_LAST["sid"] = fresh[0]["sid"]
        except Exception as e:
            GEN_LAST["error"] = str(e)[:200]
        finally:
            GEN_BUSY["busy"] = False

    threading.Thread(target=work, daemon=True).start()
    return True


# ---------------------------------------------------------------------------
# C 核心服务自动拉起（room_server / relay_server 常驻，进程级单例）
# ---------------------------------------------------------------------------
SERVICES = []


def _port_open(port):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.3)
    try:
        s.connect(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _is_elf(p):
    try:
        with open(p, "rb") as f:
            return f.read(4) == b"\x7fELF"
    except OSError:
        return False


def _is_runnable(p):
    """能否真正执行：Windows 下 ELF（WSL 产物）不可直接运行。"""
    if not os.path.isfile(p):
        return False
    if os.name == "nt" and _is_elf(p):
        return False
    return True


# transfer.c 经 event() 打到 stdout 的事件词（原样 "TRANSFER <WORD> a b"），
# 网络日志据此把 ui_state 里的事件还原成真实英文命令行。
TRANSFER_WORDS = {
    "UDP_READY", "TCP_CONNECTED", "RELAY_CONNECTED", "UDP_FALLBACK", "UDP_RETRY",
    "REQUEST", "PIECE_OK", "HASH_REJECT", "FILE_COMPLETE", "FILE_HASH_FAIL",
    "MANIFEST_READY", "RESUME_BLOCK", "DOWNLOAD_ON", "DOWNLOAD_PAUSED",
    "SOURCE_LOST", "REQUEST_TIMEOUT", "ANNOUNCE_TCP", "WAITING_FOR_AVAILABLE_SOURCE",
}


def ensure_services():
    """确保 room_server / relay_server 在跑（未监听且二进制存在则拉起）。"""
    started = []
    for name, port, binpath in (
        ("room_server", 37401, ROOM_BIN),
        ("relay_server", 37405, RELAY_BIN),
    ):
        if _is_runnable(binpath) and not _port_open(port):
            try:
                p = subprocess.Popen([binpath], cwd=ROOT,
                                     stdout=DEVNULL, stderr=DEVNULL)
                SERVICES.append(p)
                started.append(name)
            except OSError:
                pass
    return started


# ---------------------------------------------------------------------------
# 命令桥：一个 Bridge = 一个 client_transfer 进程 = 一个玩家
# ---------------------------------------------------------------------------
BRIDGES = {}  # id -> Bridge


class Bridge:
    def __init__(self, nick, role, host, session_dir, mode):
        self.id = uuid.uuid4().hex[:10]
        self.nick = (nick or "detective")[:16]
        self.role = role              # "host" | "player"
        self.host = host or "localhost"
        self.session_dir = session_dir
        self.mode = mode              # "" | tcp | udp | relay
        self.proc = None
        self.state_path = os.path.join(ROOT, "logs", "ui_state_%s.json" % self.id)
        self.log_lines = []
        # 详细网络日志：独立于命令行可视化案板，记录带 IP/端口的连接过程，并自动落盘
        self.netlog_path = os.path.join(ROOT, "logs", "netlog_%s.log" % self.id)
        self.net_lines = []
        self._seen_events = set()
        self._peer_route = {}   # peer_id -> "TCP" / "UDP" / "RELAY"（据连接事件推断当前数据路径）
        self._pending = {}      # peer_id -> {"piece":k, "t":开始时间, "warned":bool}
        self._missing_seen = set()  # 已提示过“待下载”的文件号
        self._stop_mon = threading.Event()
        self._mu = threading.Lock()
        self.started = False

    def start(self):
        argv = [CLIENT_BIN, "--nick", self.nick,
                "--data-dir", os.path.join(ROOT, "ui_prototype")]
        if self.mode:
            argv += ["--mode", self.mode]
        if self.role == "host":
            argv.append("--host")
            if self.session_dir:
                argv += ["--session", self.session_dir]
        env = dict(os.environ)
        env["UI_JSON_PATH"] = self.state_path

        self.proc = subprocess.Popen(
            argv, cwd=ROOT, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, env=env,
        )
        self.started = True
        threading.Thread(target=self._pump_stdout, daemon=True).start()
        threading.Thread(target=self._monitor_state, daemon=True).start()
        # 1) 中文说明 + 下一行原样打印真正执行的命令行（绝对路径保持原样，与实际 spawn 完全一致）
        try:
            import shlex
            cmdline = shlex.join(argv)
        except Exception:
            cmdline = " ".join(argv)
        self._net("启动游戏核心 client_transfer：角色=%s；Tracker 稍后用 join 命令指定（%s:%s）；模式=%s",
                  "房主" if self.role == "host" else "侦探",
                  self.host, 37401,
                  self.mode or "自动（局域网TCP→UDP打洞→中继）")
        self._net("$ %s", cmdline, ts=False)
        self._net("工作目录 cwd=%s", ROOT, ts=False)
        self._net("环境变量 UI_JSON_PATH=%s（状态落盘文件，前端据此渲染）", self.state_path, ts=False)
        # 2) 进房命令（中文说明 + 真实英文 stdin 命令由 command() 统一记录）
        join_cmd = "join %s %s%s" % (self.nick, self.host,
                                     " host" if self.role == "host" else "")
        self.command(join_cmd)
        return self.id

    # --- 详细网络日志：时间戳 + 中文步骤 + IP/端口，独立文件落盘 ---
    def _net(self, msg, *args, **kw):
        try:
            text = msg % args if args else str(msg)
        except Exception:
            text = str(msg)
        if kw.get("ts") is False:
            # 续行（如真实命令/英文事件）：对齐到时间戳之后，不再重复打时间
            line = "        %s" % text
        else:
            line = "[%s] %s" % (time.strftime("%H:%M:%S"), text)
        with self._mu:
            self.net_lines.append(line)
            if len(self.net_lines) > 2000:
                self.net_lines = self.net_lines[-2000:]
        try:
            os.makedirs(os.path.dirname(self.netlog_path), exist_ok=True)
            with open(self.netlog_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass

    @staticmethod
    def _peer_addr(p):
        """把一个 peer 的私网/反射/TCP/UDP 端点拼成可读串。"""
        parts = []
        priv = p.get("priv_ip")
        if priv and priv != "-":
            parts.append("私网 %s:%s(TCP)/%s(UDP)" % (
                priv, p.get("tcp_port"), p.get("udp_port")))
        refl = p.get("refl_ip")
        if refl and refl != "-" and p.get("refl_port"):
            parts.append("反射 %s:%s" % (refl, p.get("refl_port")))
        return "；".join(parts) if parts else "地址尚未探测（反射 -:0）"

    @staticmethod
    def _fname(f):
        """从 img_path 取可读文件名，失败退回编号。"""
        p = (f.get("img_path") or "").replace("\\", "/")
        base = p.rsplit("/", 1)[-1]
        return base or ("文件%s" % f.get("file_id"))

    def _track_event(self, raw):
        """据连接/传输事件维护对端当前数据路径和“在途请求”，供细分日志与卡顿监控。
        注意：C 的 TCP_CONNECTED 事件两个参数是 (本机id, 对端id)，其余多为 (对端id, 块)。"""
        toks = raw.split()
        w = toks[0] if toks else ""
        nums = [int(x) for x in toks[1:] if x.lstrip("-").isdigit()]
        peer, piece, route = None, None, None
        if w == "TCP_CONNECTED":
            peer, route = (nums[1] if len(nums) >= 2 else None), "TCP直连"
        elif w == "UDP_READY":
            peer, route = (nums[0] if nums else None), "UDP打洞"
        elif w == "UDP_FALLBACK":
            peer, route = (nums[0] if nums else None), "中继"
        elif w == "RELAY_CONNECTED":
            peer, route = (nums[0] if nums else None), "中继"
        elif w == "REQUEST":
            peer = nums[0] if nums else None
            piece = nums[1] if len(nums) > 1 else None
        elif w in ("PIECE_OK", "HASH_REJECT", "SOURCE_LOST", "REQUEST_TIMEOUT"):
            peer = nums[0] if nums else None
            piece = nums[1] if len(nums) > 1 else None
        else:
            return
        if peer is not None and route is not None:
            self._peer_route[peer] = route
        if w == "REQUEST":
            self._pending[peer] = {"piece": piece, "t": time.time(), "warned": False, "done": False}
        elif w in ("PIECE_OK", "SOURCE_LOST", "REQUEST_TIMEOUT"):
            self._pending.pop(peer, None)

    def _monitor_state(self):
        """轮询 ui_state JSON，把连接/路径/证物变化翻译成带端点的详细日志。
        数据全部来自 C 核心已写出的状态，无需改动或重编译 C。"""
        last_online = None
        known_peers = {}
        last_files = {}
        while not self._stop_mon.wait(0.6):
            st = self.state()
            if not st:
                continue
            # 1) Tracker 在线状态翻转
            online = bool(st.get("room_online"))
            if online != last_online:
                if last_online is not None:
                    if online:
                        self._net("✓ 已连接 Tracker %s:%s，进房成功（peer=%s，房间=%s，局号=%s）",
                                  self.host, 37401, st.get("my_peer_id"),
                                  st.get("room_id"), st.get("session_id"))
                    else:
                        self._net("✗ 与 Tracker %s:%s 的连接断开（已知玩家之间仍可继续传块）",
                                  self.host, 37401)
                last_online = online
            # 2) 成员增删（带完整端点）
            for p in st.get("peers", []):
                pid = p.get("peer_id")
                sig = (p.get("nick"), p.get("priv_ip"), p.get("tcp_port"),
                       p.get("udp_port"), p.get("refl_ip"), p.get("refl_port"))
                if known_peers.get(pid) != sig:
                    tag = "（我）" if pid == st.get("my_peer_id") else ""
                    self._net("成员 peer%s %s%s：%s",
                              pid, p.get("nick") or "", tag, self._peer_addr(p))
                    known_peers[pid] = sig
            for pid in list(known_peers.keys()):
                if not any(p.get("peer_id") == pid for p in st.get("peers", [])):
                    self._net("成员 peer%s 已离开房间", pid)
                    known_peers.pop(pid, None)
            # 3) 证物状态：清单里缺失的文件先提示“待下载”，集齐后提示完成+来源
            for f in st.get("files", []):
                fid = f.get("file_id")
                have = bool(f.get("have"))
                src = (f.get("source_nick") or "").strip()
                kind_txt = "图像" if f.get("kind") == 1 else "文档"
                name = self._fname(f)
                if not have:
                    if fid not in self._missing_seen:
                        self._missing_seen.add(fid)
                        self._net("待接收：证物文件%s《%s》（%s，%s 字节）——尚未持有，等待在线成员提供",
                                  fid, name, kind_txt, f.get("size"))
                    last_files[fid] = (False, src)
                else:
                    sig = (True, src)
                    if last_files.get(fid) != sig:
                        src_txt = "本地自带" if src in ("", "local") else ("来自 " + src)
                        self._net("接收成功：证物文件%s《%s》（%s，%s 字节）全部块校验通过，%s",
                                  fid, name, kind_txt, f.get("size"), src_txt)
                    last_files[fid] = sig
            # 4) 新事件：中文说明在前，真实英文事件在下一行（不带时间戳）；同时维护路径/在途状态
            peers = {p.get("peer_id"): p for p in st.get("peers", [])}
            for raw in st.get("events", []):
                if raw in self._seen_events:
                    continue
                self._seen_events.add(raw)
                word = raw.split(" ", 1)[0]
                eng = ("TRANSFER " + raw) if word in self.TRANSFER_WORDS else raw
                self._net(self._explain_event(raw, peers))
                self._net(eng, ts=False)
                self._track_event(raw)
            # 5) 卡顿监控：某块“已请求”超过阈值仍无回应，主动指出卡在哪一对端/哪个块
            now = time.time()
            for pid, info in list(self._pending.items()):
                if info.get("warned") or info.get("done"):
                    continue
                wait = now - info["t"]
                p = peers.get(pid)
                pname = (p.get("nick") if p else None) or ("peer%s" % pid)
                route = self._peer_route.get(pid, "未知路径")
                if wait >= 3.0:
                    self._net("⚠ 已等待 %.1f 秒：向 %s（%s）请求的块%s 仍未收到，可能对端无此块/路径不通，稍后将重试或换源",
                              wait, pname, route, info["piece"])
                    info["warned"] = True

    def _explain_event(self, raw, peers):
        word, _, rest = raw.partition(" ")
        nums = rest.split()
        a = int(nums[0]) if nums and nums[0].lstrip("-").isdigit() else 0
        b = int(nums[1]) if len(nums) > 1 and nums[1].lstrip("-").isdigit() else 0
        # C 的 TCP_CONNECTED 参数是 (本机id, 对端id)，对端取 b；其余事件对端取 a
        peer_id = b if word == "TCP_CONNECTED" else a
        p = peers.get(peer_id)

        def nick(pid=None):
            pp = p if pid is None else peers.get(pid)
            return (pp.get("nick") if pp else None) or ("peer%s" % (peer_id if pid is None else pid))

        def refl_ep():
            if p and p.get("refl_ip") and p.get("refl_ip") != "-" and p.get("refl_port"):
                return "%s:%s" % (p.get("refl_ip"), p.get("refl_port"))
            return ""

        def priv_ep():
            if p and p.get("priv_ip") and p.get("priv_ip") != "-":
                return "%s:%s(UDP)/%s(TCP)" % (p.get("priv_ip"), p.get("udp_port"), p.get("tcp_port"))
            return ""

        def ep_full():
            r, q = refl_ep(), priv_ep()
            if r and q:
                return "%s，反射 %s" % (q, r)
            return r or q or "地址尚未探测"

        def src():   # 接收方向：数据来源端点
            return "来源 %s（%s）" % (nick(), ep_full())

        def dst():   # 发送方向：目标端点
            return "目标 %s（%s）" % (nick(), ep_full())

        def route():
            return self._peer_route.get(peer_id, "自动路径")

        table = {
            # ---- 建链阶段：发起 → 建立/失败 → 兜底 ----
            "UDP_READY": lambda: "① 通道就绪：与 %s 的 UDP 打洞成功，P2P 直连可用（反射 %s），此后数据不经 Tracker"
                                 % (nick(), refl_ep() or priv_ep() or "未知"),
            "TCP_CONNECTED": lambda: "① 通道就绪：与 %s 的 TCP 直连已建立（%s）" % (nick(), ep_full()),
            "RELAY_CONNECTED": lambda: "① 通道就绪：已连上中继服务器 %s:37405（CMD_RELAY_BIND 已发，打洞失败时经此 C/S 中转）"
                                       % self.host,
            "UDP_RETRY": lambda: "… 正在向 %s 重发 UDP 打洞探测（第 %d 次，最多 5 次）" % (nick(), b),
            "UDP_FALLBACK": lambda: "② 打洞失败：与 %s 的 UDP 打洞重试 %d 次仍不通，切换到中继兜底（%s:37405）"
                                    % (nick(), b, self.host),
            # ---- 收文件阶段：请求(开始接收) → 逐块成功/失败 → 整文件完成 ----
            "REQUEST": lambda: "② 开始接收：向 %s 请求块%s（路径=%s）" % (dst(), b, route()),
            "PIECE_OK": lambda: "③ 接收成功：收到 %s 的块%s（路径=%s），SHA256 校验通过并已写盘" % (src(), b, route()),
            "HASH_REJECT": lambda: "✗ 块校验失败：来自 %s 的块%s 长度或 SHA256 不符，已丢弃并会重新请求" % (src(), b),
            "FILE_COMPLETE": lambda: "④ 文件完成：文件%s 所有块集齐且整文件 SHA256 校验通过，上传来源=%s"
                                     % (a, nick(b) if b else "本地"),
            "FILE_HASH_FAIL": lambda: "✗ 文件%s 块虽集齐，但整文件 SHA256 校验失败，未标记为完成" % a,
            "MANIFEST_READY": lambda: "清单就绪：已解析清单（%s 字节，3 个文件），即将核对本地缺块" % a,
            "RESUME_BLOCK": lambda: "↺ 本地命中：文件%s 块%s 已在本地缓存且校验通过，无需网络下载" % (a, b),
            "DOWNLOAD_ON": lambda: "▶ 下载开关打开：开始调度缺失块的请求",
            "DOWNLOAD_PAUSED": lambda: "⏸ 下载开关关闭：暂停请求新块（已在途的不受影响）",
            "SOURCE_LOST": lambda: "✗ 来源掉线：%s 在传输块%s 时连接断开，2 秒后重试或更换来源" % (src(), b),
            "REQUEST_TIMEOUT": lambda: "✗ 请求超时：%s 的块%s 等待回应超时（路径=%s），放弃该来源并重试" % (dst(), b, route()),
            "ANNOUNCE_TCP": lambda: "↔ 通告发现：经 Tracker %s:37401 拿到房主通告，正在向房主索取清单" % self.host,
            "WAITING_FOR_AVAILABLE_SOURCE": lambda: "… 等待来源：当前没有任何在线成员持有缺块，挂起下载直到有人上线",
        }
        fn = table.get(word)
        if fn:
            try:
                return fn()
            except Exception:
                return raw
        return raw

    def _pump_stdout(self):
        hb_logged = 0
        for line in self.proc.stdout:
            line = line.rstrip("\n")
            if not line:
                continue
            with self._mu:
                self.log_lines.append(line)
                self.log_lines = self.log_lines[-200:]
            # TRANSFER 事件已由状态监控处理，跳过避免重复；TRANSFER_STATS 单独解析
            if line.startswith("TRANSFER "):
                continue
            if line == "ROOM_HEARTBEAT":
                hb_logged += 1
                if hb_logged == 1:
                    self._net("保活：已向 Tracker 发送 ROOM_HEARTBEAT（每 5 秒一次，后续省略，不影响数据）")
                    self._net("ROOM_HEARTBEAT", ts=False)
                continue
            if line.startswith("TRANSFER_STATS"):
                self._net("传输统计：%s", self._explain_stats(line))
                self._net(line, ts=False)
                continue
            if line.split(" ", 1)[0] in ("TRACKER_OFFLINE", "ROOM_ERROR"):
                if line.startswith("TRACKER_OFFLINE"):
                    self._net("✗ Tracker 不可达（控制连接异常，已建立的 P2P 传块不受影响）")
                elif line.startswith("ROOM_ERROR"):
                    self._net("✗ Tracker 返回错误：%s" % line[len("ROOM_ERROR"):].strip())
                self._net(line, ts=False)

    def running(self):
        return self.proc is not None and self.proc.poll() is None

    @staticmethod
    def _explain_stats(line):
        """把 TRANSFER_STATS 行里的 key=value 翻成中文摘要。"""
        kv = {}
        for tok in line.split()[1:]:
            if "=" in tok:
                k, v = tok.split("=", 1)
                kv[k] = v
        downs = ", ".join("%s=%s" % (k, kv[k]) for k in kv if k.startswith("from_"))
        return ("本机 peer=%s；上行 TCP=%s / UDP=%s / 中继=%s 字节，合计 %s；"
                "下载耗时 %s 秒；坏块拒绝 %s 次；各来源下行 %s" % (
                    kv.get("peer", "-"), kv.get("tcp_up", 0), kv.get("udp_up", 0),
                    kv.get("relay_up", 0), kv.get("total_up", 0),
                    kv.get("elapsed", "-"), kv.get("hash_reject", 0),
                    downs or "无"))

    def _explain_stdin(self, cmd):
        """把一条 stdin 命令翻译成中文说明（命令本身另起一行打印）。"""
        parts = cmd.split()
        w = parts[0] if parts else ""
        rest = parts[1:]
        table = {
            "join":   lambda: ("以房主身份建房并进房，Tracker=%s" if len(rest) >= 3 and rest[2] == "host"
                               else "以玩家身份进房，Tracker=%s") % (rest[1] if len(rest) >= 2 else "?"),
            "probe":  lambda: "向 Tracker 发送 PROBE，查询本机反射地址",
            "announce": lambda: "向 Tracker 通告本机持有的块清单",
            "next":   lambda: "推进剧情到下一节点",
            "answer": lambda: "提交剧情问答题答案：%s" % (" ".join(rest) if rest else ""),
            "choose": lambda: "在分支项中选择：%s" % (" ".join(rest) if rest else ""),
            "get":    lambda: "开始下载所有缺失的块",
            "pause":  lambda: "暂停下载",
            "stats":  lambda: "查看传输统计",
            "redraw": lambda: "手动重绘命令行案板",
            "quit":   lambda: "退出对局",
        }
        fn = table.get(w)
        if fn:
            try:
                return fn()
            except Exception:
                pass
        return "执行命令"

    def command(self, cmd):
        cmd = (cmd or "").strip()
        if not cmd:
            return False, "空命令"
        if not self.running():
            return False, "client_transfer 已退出"
        # 先写中文说明，再另起一行（不带时间戳）原样记录下发给 client_transfer 的英文 stdin 命令
        self._net("%s", self._explain_stdin(cmd))
        self._net(">>> %s", cmd, ts=False)
        try:
            self.proc.stdin.write(cmd + "\n")
            self.proc.stdin.flush()
            return True, ""
        except (OSError, ValueError) as e:
            return False, str(e)

    def state(self):
        if not os.path.isfile(self.state_path):
            return None
        try:
            with open(self.state_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def log(self):
        with self._mu:
            return list(self.log_lines[-100:])

    def netlog(self, after=0):
        """返回 (全部详细日志行, 最新行号)；after 用于前端增量拉取。"""
        with self._mu:
            lines = list(self.net_lines)
        return lines[after:], len(lines)

    def quit(self):
        self._stop_mon.set()
        if self.running():
            try:
                self.proc.stdin.write("quit\n")
                self.proc.stdin.flush()
            except OSError:
                pass
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.terminate()
        self.started = False


def bridge_capability():
    return {
        "available": _is_runnable(CLIENT_BIN),
        "client": os.path.isfile(CLIENT_BIN),
        "room_server": os.path.isfile(ROOM_BIN),
        "relay_server": os.path.isfile(RELAY_BIN),
        "services_running": _port_open(37401),
        "wsl_hint": os.name == "nt" and _is_elf(CLIENT_BIN),
    }


def shutdown_bridges():
    with LOCK:
        for b in list(BRIDGES.values()):
            b.quit()
        BRIDGES.clear()
        for p in SERVICES:
            try:
                p.terminate()
            except OSError:
                pass
        SERVICES.clear()


def _sniff_content_type(fid, data):
    """证物文件无扩展名，按内容魔数判断真实类型。
    file_1/file_2 并不固定为 JPEG（实测 file_2 可能是 PNG），
    不能凭 file_id 写死 image/jpeg。"""
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if fid in ("1", "2"):
        return "image/jpeg"
    return "text/plain; charset=utf-8"


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
class GameHandler(http.server.BaseHTTPRequestHandler):
    server_version = "FragmentArchive/1.0"

    def _send(self, code, obj=None, ctype="application/json", raw=None):
        self.send_response(code)
        self.send_header("Cache-Control", "no-store")
        if raw is not None:
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        else:
            data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    def _json_body(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        if n <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    # --- 路由 ---
    def do_GET(self):
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path == "/api/cases":
            self._send(200, {"cases": list_cases()})
        elif path == "/api/suspects":
            self._send(200, {"suspects": SUSPECTS})
        elif path == "/api/case-types":
            self._send(200, {"types": CASE_TYPES})
        elif path == "/api/generate":
            self._send(200, {"busy": GEN_BUSY["busy"], "error": GEN_LAST["error"],
                             "sid": GEN_LAST["sid"], "has_api_key": _key_status()["has_api_key"]})
        elif path == "/api/settings/key":
            self._send(200, _key_status())
        elif path == "/api/bridge/capability":
            self._send(200, bridge_capability())
        elif path.startswith("/api/cases/"):
            sid = path.rsplit("/", 1)[1]
            sdir = _find_session_dir(sid)
            if not sdir:
                self._send(404, {"error": "not found"})
            else:
                self._send(200, {"case": _extract_case(sdir)})
        elif path.startswith("/api/bridge/") and path.endswith("/state"):
            bid = path.split("/")[3]
            b = BRIDGES.get(bid)
            st = b.state() if b else None
            self._send(200, {"bridge_id": bid, "running": bool(b and b.running()),
                             "state": st}) if b else self._send(404, {"error": "bridge not found"})
        elif path.startswith("/api/bridge/") and path.endswith("/log"):
            bid = path.split("/")[3]
            b = BRIDGES.get(bid)
            self._send(200, {"lines": b.log()}) if b else self._send(404, {"error": "bridge not found"})
        elif path.startswith("/api/bridge/") and path.endswith("/netlog"):
            bid = path.split("/")[3]
            b = BRIDGES.get(bid)
            if not b:
                self._send(404, {"error": "bridge not found"})
            else:
                qs = {}
                if "?" in self.path:
                    for kv in self.path.split("?", 1)[1].split("&"):
                        if "=" in kv:
                            k, v = kv.split("=", 1)
                            qs[k] = v
                try:
                    after = int(qs.get("after", "0"))
                except ValueError:
                    after = 0
                lines, total = b.netlog(after)
                self._send(200, {"lines": lines, "after": after, "total": total,
                                 "log_file": os.path.relpath(b.netlog_path, ROOT)})
        elif path.startswith("/api/evidence/"):
            parts = path.split("/")
            if len(parts) == 5:
                sid, fid = parts[3], parts[4]
                sdir = _find_session_dir(sid)
                fpath = os.path.join(sdir, "file_" + fid) if sdir else ""
                if not fpath or not os.path.isfile(fpath):
                    self._send(404, {"error": "not found"})
                    return
                data = open(fpath, "rb").read()
                ctype = _sniff_content_type(fid, data)
                self._send(200, raw=data, ctype=ctype)
            else:
                self._send(400, {"error": "bad path"})
        else:
            self._serve_static()

    def do_POST(self):
        path = self.path.split("?", 1)[0].rstrip("/")
        body = self._json_body()
        if path == "/api/generate":
            ok = _generate_async(body.get("genre") or "")
            self._send(200 if ok else 409,
                       {"ok": ok, "error": GEN_LAST["error"]})
        elif path == "/api/settings/key":
            try:
                updates = {}
                for jkey, envkey in (("ark_api_key", "ARK_API_KEY"),
                                     ("ark_model", "ARK_MODEL"),
                                     ("ark_llm_model", "ARK_LLM_MODEL")):
                    if jkey in body:                 # 仅当前端提交了该字段时才覆盖
                        updates[envkey] = body[jkey]
                self._send(200, _write_env(updates))
            except OSError as e:
                self._send(500, {"error": f"写入 .env 失败：{e}"})
        elif path == "/api/bridge/start":
            self._bridge_start(body)
        elif path.startswith("/api/bridge/") and path.endswith("/command"):
            self._bridge_command(path.split("/")[3], body)
        elif path.startswith("/api/bridge/") and path.endswith("/quit"):
            self._bridge_quit(path.split("/")[3])
        else:
            self._send(404, {"error": "not found"})

    # --- 命令桥动作 ---
    def _bridge_start(self, b):
        if not _is_runnable(CLIENT_BIN):
            cap = bridge_capability()
            hint = ("检测到 WSL 编译的 ELF 产物，Windows 无法直接执行。请在 WSL/Linux 下运行 `python ui_prototype/server.py`。"
                    if cap["wsl_hint"] else
                    "未找到 client_transfer。请先在 WSL/Linux 下运行 `make transfer`。")
            self._send(409, {"error": hint, "capability": cap})
            return
        role = "host" if b.get("role") == "host" else "player"
        session_dir = None
        if role == "host":
            sid = (b.get("session") or "").strip()
            if sid:
                session_dir = _find_session_dir(sid)
                if not session_dir:
                    self._send(400, {"error": "案件不存在，请重新选择"})
                    return
        mode = b.get("mode") or ""
        if mode not in ("", "tcp", "udp", "relay"):
            mode = ""
        ensure_services()
        br = Bridge((b.get("nick") or "").strip(), role,
                    (b.get("host") or "").strip(), session_dir, mode)
        try:
            br.start()
        except (OSError, ValueError) as e:
            self._send(500, {"error": "启动 client_transfer 失败：%s" % e})
            return
        with LOCK:
            BRIDGES[br.id] = br
        self._send(200, {"bridge_id": br.id, "role": role,
                         "state_path": br.state_path,
                         "join_sent": True})

    def _bridge_command(self, bid, b):
        br = BRIDGES.get(bid)
        if not br:
            self._send(404, {"error": "bridge not found"})
            return
        ok, err = br.command(b.get("cmd"))
        self._send(200 if ok else 409, {"ok": ok, "error": err})

    def _bridge_quit(self, bid):
        br = BRIDGES.get(bid)
        if br:
            br.quit()
            with LOCK:
                BRIDGES.pop(bid, None)
        self._send(200, {"ok": True})

    # --- 静态文件 ---
    def _serve_static(self):
        path = self.path.split("?", 1)[0]
        if path == "/":
            path = "/ui_prototype/index.html"
        rel = path.lstrip("/")
        full = os.path.normpath(os.path.join(ROOT, rel))
        if not full.startswith(ROOT) or not os.path.isfile(full):
            self._send(404, {"error": "not found"})
            return
        ext = os.path.splitext(full)[1].lower()
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".json": "application/json; charset=utf-8",
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".svg": "image/svg+xml",
        }.get(ext, "application/octet-stream")
        data = open(full, "rb").read()
        self._send(200, raw=data, ctype=ctype)


def main():
    port = int(os.environ.get("PORT", "8080"))
    httpd = http.server.ThreadingHTTPServer(("", port), GameHandler)
    print("碎片档案 · Web 案情板  http://localhost:%d/ui_prototype/index.html" % port)
    print("案件库：已扫描 %d 案" % len(list_cases()))
    cap = bridge_capability()
    print("C 核心命令桥：client_transfer=%s room_server=%s relay_server=%s（需在 WSL/Linux `make transfer`）"
          % (cap["client"], cap["room_server"], cap["relay_server"]))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        shutdown_bridges()
        httpd.server_close()


if __name__ == "__main__":
    main()

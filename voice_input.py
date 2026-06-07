#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
voice_input.py — Groq Whisper 版 音声入力（メニューバー常駐・Fnトリガー）

操作: 【Fn（地球儀）キーを押している間】録音 →【離すと】文字起こし→カーソル位置に貼り付け
      （AquaVoiceと同じ hold-to-talk。Fnは「何もしない」設定推奨）

メニューバーの 🎤 から状態確認・終了ができる。
設定（GROQ_API_KEY / vocab.txt）は ~/bin/voice_input/ に置く。バンドル化しても参照先は固定。
"""

import os
import re
import json
import time
import struct
import array
import collections
import http.client
import subprocess
import tempfile
import threading
from pathlib import Path

import rumps
import Quartz
from AppKit import NSPasteboard, NSPasteboardTypeString

# ===== 設定（編集可。CONFIG_DIRは固定＝.app化しても効く）=====
CONFIG_DIR = Path.home() / "bin" / "voice_input"
ENV_FILE = CONFIG_DIR / ".env"
VOCAB_FILE = CONFIG_DIR / "vocab.txt"
LOG_FILE = CONFIG_DIR / "voice_input.log"

FFMPEG = "/opt/homebrew/bin/ffmpeg"
GROQ_HOST = "api.groq.com"
GROQ_PATH = "/openai/v1/audio/transcriptions"
GROQ_CHAT_PATH = "/openai/v1/chat/completions"
GROQ_MODEL = "whisper-large-v3-turbo"
POLISH_MODEL = "llama-3.3-70b-versatile"   # 日本語校正用のGroq LLM
LANGUAGE = "ja"

# ローカルWhisper（whisper.cpp server・オフライン・モデル常駐で高速）
WHISPER_SERVER = "/opt/homebrew/bin/whisper-server"
WHISPER_MODEL = str(CONFIG_DIR / "models" / "ggml-large-v3-turbo-q5_0.bin")
LOCAL_PORT = 8765
DEFAULT_ENGINE = "local"  # "local"=オフライン高速(既定) / "groq"=クラウド(フォールバック)

KEEPWARM_SEC = 30      # Groq接続を温め続ける間隔（秒）
FN_FLAG = 0x800000     # Fn（地球儀）キーの修飾フラグ

# 操作: 既定=ホールド(押している間だけ録音・離すと確定)。ダブルクリックでハンズフリー
# (押しっぱなし不要で喋り続けられ、無音で自動停止)。両方が常に使える。
DEFAULT_POLISH = False    # 日本語校正(メニューでON可・+約0.5s/要ネット)。既定OFF=今まで通り速い
DOUBLE_CLICK_SEC = 0.4    # この閾値: 押下間隔<これ=ダブルクリック / 押下時間>=これ=ホールド
SILENCE_STOP_SEC = 1.3    # ハンズフリー時、発話後この秒数の無音で自動停止
NOSPEECH_TIMEOUT = 4.0    # ハンズフリー開始後まったく喋らなければ自動キャンセル
VOICE_RMS = 500           # これ以上のRMSを「発話あり」とみなす（マイク環境で調整可）
SOUND_START = "/System/Library/Sounds/Tink.aiff"   # 録音開始の合図音
SOUND_STOP = "/System/Library/Sounds/Pop.aiff"     # 録音停止の合図音
SOUND_VOL = "0.35"        # 合図音の音量（0〜1）
MIN_SEC = 0.3          # これ未満の録音は無視（誤爆防止）
PASTE_AFTER = True     # 文字起こし後にCmd+Vで自動貼り付け

# 常時マイク（リングバッファ）方式: ffmpeg起動待ち(約0.4s)を無くし「押した瞬間から録れる」
SAMPLE_RATE = 16000
BYTES_PER_SEC = SAMPLE_RATE * 2   # mono s16le = 2byte/sample
CHUNK = 4096
PREROLL_SEC = 0.35     # 押下直前ぶんも拾う（出だしの欠け防止）
# ================


def log(msg: str):
    ts = time.strftime("%H:%M:%S")
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {msg}\n")
    except Exception:
        pass


def load_env():
    key = os.environ.get("GROQ_API_KEY")
    if key:
        return key.strip()
    if ENV_FILE.exists():
        for raw in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line.startswith("GROQ_API_KEY"):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None


def load_vocab() -> str:
    if not VOCAB_FILE.exists():
        return ""
    words = [w.strip() for w in VOCAB_FILE.read_text(encoding="utf-8").splitlines()
             if w.strip() and not w.strip().startswith("#")]
    if not words:
        return ""
    return "次の固有名詞が登場します: " + "、".join(words) + "。"


def detect_mic_index() -> str:
    try:
        out = subprocess.run(
            [FFMPEG, "-f", "avfoundation", "-list_devices", "true", "-i", ""],
            capture_output=True, text=True,
            encoding="utf-8", errors="replace").stderr
    except Exception as e:
        log(f"デバイス取得失敗: {e}")
        return "0"
    audio = False
    fallback = None
    for line in out.splitlines():
        if "audio devices" in line:
            audio = True
            continue
        if audio:
            m = re.search(r"\[(\d+)\]\s+(.*)$", line)
            if m:
                idx, name = m.group(1), m.group(2)
                if fallback is None:
                    fallback = idx
                if "マイク" in name or "Microphone" in name or "MacBook" in name:
                    log(f"マイク検出: [{idx}] {name}")
                    return idx
    return fallback or "0"


def _safe_unlink(path):
    try:
        if path and os.path.exists(path):
            os.unlink(path)
    except Exception:
        pass


def _make_wav(pcm):
    """生PCM(s16le mono 16k)にWAVヘッダを付けてバイト列で返す"""
    n = len(pcm)
    return (b"RIFF" + struct.pack("<I", 36 + n) + b"WAVE"
            + b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, SAMPLE_RATE, BYTES_PER_SEC, 2, 16)
            + b"data" + struct.pack("<I", n) + pcm)


def _rms(chunk):
    """s16le チャンクの音量(RMS)。VADの発話検出に使う"""
    if not chunk:
        return 0.0
    a = array.array("h")
    a.frombytes(chunk[: len(chunk) // 2 * 2])
    if not a:
        return 0.0
    return (sum(x * x for x in a) / len(a)) ** 0.5


class Recorder:
    """マイクを開きっぱなしにして生PCMをリングバッファに貯め、区間（＋プリロール）を
    メモリから切り出す。ffmpeg起動待ち(約0.4s)が無く押した瞬間から録れる。
    VAD有効時は発話後の無音を検出して自動停止コールバックを呼ぶ。"""
    def __init__(self, mic_index):
        self.mic_index = mic_index
        self.proc = None
        self.lock = threading.Lock()
        ring_chunks = int(PREROLL_SEC * BYTES_PER_SEC / CHUNK) + 2
        self.ring = collections.deque(maxlen=ring_chunks)
        self.capturing = None
        self.vad = False
        self.on_auto_stop = None
        self._spoke = False
        self._last_voice = 0.0
        self._t_start = 0.0
        self._reader_thread = None

    def _ensure_stream(self):
        # proc が死んでいる or reader スレッドが死んでいたら作り直す（フリーズ自己修復）
        if (self.proc and self.proc.poll() is None
                and self._reader_thread and self._reader_thread.is_alive()):
            return
        try:
            if self.proc:
                self.proc.kill()
        except Exception:
            pass
        cmd = [FFMPEG, "-nostdin", "-f", "avfoundation",
               "-i", f":{self.mic_index}", "-ac", "1", "-ar", str(SAMPLE_RATE),
               "-f", "s16le", "-"]
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL)
        self._reader_thread = threading.Thread(target=self._reader, args=(self.proc,), daemon=True)
        self._reader_thread.start()
        log("マイク常時ストリーム開始")

    def _reader(self, proc):
        while True:
            try:
                chunk = proc.stdout.read(CHUNK)
            except Exception:
                chunk = b""
            if not chunk:
                try:
                    proc.kill()    # ffmpegも止めて次のstart()で確実に作り直させる
                except Exception:
                    pass
                log("マイクreader終了→次回再起動")
                return
            with self.lock:
                self.ring.append(chunk)
                if self.capturing is not None:
                    self.capturing.append(chunk)
                    vad = self.vad
            if self.capturing is not None and vad and _rms(chunk) > VOICE_RMS:
                self._spoke = True
                self._last_voice = time.time()

    def start(self, vad=False, on_auto_stop=None):
        self._ensure_stream()
        self.vad = vad
        self.on_auto_stop = on_auto_stop
        self._spoke = False
        self._t_start = self._last_voice = time.time()
        with self.lock:
            self.capturing = list(self.ring)  # プリロール込みで開始
        if vad:
            threading.Thread(target=self._vad_loop, daemon=True).start()

    def enable_vad(self, on_auto_stop):
        """録音継続中にVAD(無音自動停止)を後から有効化（ダブルクリックでハンズフリー化）"""
        self.on_auto_stop = on_auto_stop
        self._spoke = False
        self._t_start = self._last_voice = time.time()
        self.vad = True
        threading.Thread(target=self._vad_loop, daemon=True).start()

    def _vad_loop(self):
        while True:
            time.sleep(0.1)
            with self.lock:
                if self.capturing is None:
                    return  # 既に停止済み
            now = time.time()
            # 発話後、一定無音 → 自動停止 ／ 無発話のまま時間切れ → キャンセル相当の自動停止
            if (self._spoke and now - self._last_voice > SILENCE_STOP_SEC) or \
               (not self._spoke and now - self._t_start > NOSPEECH_TIMEOUT):
                cb = self.on_auto_stop
                if cb:
                    cb()
                return

    def stop(self):
        with self.lock:
            if self.capturing is None:
                return None
            pcm = b"".join(self.capturing)
            self.capturing = None
        was_vad, spoke = self.vad, self._spoke
        self.vad = False
        dur = len(pcm) / BYTES_PER_SEC
        rms = _rms(pcm)
        if was_vad and not spoke:
            log(f"無発話→破棄 dur={dur:.1f}s RMS={rms:.0f}")
            return None
        if len(pcm) < int(MIN_SEC * BYTES_PER_SEC):
            log(f"短すぎ→破棄 dur={dur:.1f}s RMS={rms:.0f}")
            return None
        log(f"■ 区間 {dur:.1f}s RMS={rms:.0f}")
        return _make_wav(pcm)


# --- Groq通信: 接続を使い回してTLS握手を省く（curl毎回より約2倍速い）---
_conn = None
_conn_lock = threading.Lock()


def _get_conn():
    global _conn
    if _conn is None:
        _conn = http.client.HTTPSConnection(GROQ_HOST, timeout=30)
    return _conn


def _reset_conn():
    global _conn
    try:
        if _conn:
            _conn.close()
    except Exception:
        pass
    _conn = None


def prewarm():
    """起動時にTLS接続を温めておく（1回目から速く）"""
    try:
        with _conn_lock:
            _get_conn().connect()
        log("接続プリウォーム完了")
    except Exception as e:
        _reset_conn()
        log(f"プリウォーム失敗: {e}")


def _multipart(fields, audio_bytes):
    bd = "----viFormBoundary8a7c2f"
    crlf = b"\r\n"
    body = b""
    for name, val in fields.items():
        body += b"--" + bd.encode() + crlf
        body += f'Content-Disposition: form-data; name="{name}"'.encode() + crlf + crlf
        body += str(val).encode("utf-8") + crlf
    body += b"--" + bd.encode() + crlf
    body += b'Content-Disposition: form-data; name="file"; filename="a.wav"' + crlf
    body += b"Content-Type: audio/wav" + crlf + crlf
    body += audio_bytes + crlf
    body += b"--" + bd.encode() + b"--" + crlf
    return bd, body


def transcribe(audio, api_key, prompt):
    """Groqクラウドで文字起こし（audio=wavバイト列）"""
    fields = {"model": GROQ_MODEL, "language": LANGUAGE,
              "response_format": "json", "temperature": "0"}
    if prompt:
        fields["prompt"] = prompt
    bd, body = _multipart(fields, audio)
    headers = {"Authorization": f"Bearer {api_key}",
               "Content-Type": f"multipart/form-data; boundary={bd}"}
    t0 = time.time()
    for attempt in (1, 2):  # 接続が切れてたら1回だけ張り直して再試行
        try:
            with _conn_lock:
                conn = _get_conn()
                conn.request("POST", GROQ_PATH, body=body, headers=headers)
                resp = conn.getresponse()
                raw = resp.read()
            data = json.loads(raw.decode("utf-8", "replace"))
            break
        except (http.client.HTTPException, OSError, ValueError) as e:
            _reset_conn()
            if attempt == 2:
                log(f"通信失敗: {e}")
                return None
    dt = time.time() - t0
    if "error" in data:
        log(f"Groqエラー: {data['error']}")
        return None
    text = (data.get("text") or "").strip()
    log(f"⚡ {dt:.2f}s 「{text}」")
    return text


_POLISH_SYS = (
    "あなたは日本語の校正者。ユーザーの音声入力の文字起こしを、意味を変えずに自然で"
    "読みやすい日本語に整える。フィラー(えー、あの、まあ、なんか等)を除き、明らかな"
    "誤変換・助詞の誤り・冗長な繰り返しを直す。内容や情報は足さない・削らない。"
    "敬体/常体は元の文に合わせる。整えた本文だけを返す(説明・引用符・前置きは付けない)。"
)


def polish_text(text, api_key):
    """Groqの高速LLMで文字起こしを自然な日本語に整える（要ネット・+約0.5s）"""
    body = json.dumps({
        "model": POLISH_MODEL,
        "messages": [
            {"role": "system", "content": _POLISH_SYS},
            {"role": "user", "content": text},
        ],
        "temperature": 0.2,
    }).encode("utf-8")
    headers = {"Authorization": f"Bearer {api_key}",
               "Content-Type": "application/json"}
    t0 = time.time()
    for attempt in (1, 2):
        try:
            with _conn_lock:
                conn = _get_conn()
                conn.request("POST", GROQ_CHAT_PATH, body=body, headers=headers)
                raw = conn.getresponse().read()
            data = json.loads(raw.decode("utf-8", "replace"))
            break
        except (http.client.HTTPException, OSError, ValueError):
            _reset_conn()
            if attempt == 2:
                log("校正失敗→原文を使用")
                return None
    try:
        out = data["choices"][0]["message"]["content"].strip()
    except Exception:
        return None
    log(f"校正 {time.time()-t0:.2f}s")
    return out or None


# --- ローカルWhisperサーバ（モデル常駐・whisper.cpp server）---
_server_proc = None
_lconn = None
_lconn_lock = threading.Lock()


def start_local_server():
    """whisper-server をモデル常駐で起動（インファレンスのみ＝高速）"""
    global _server_proc
    if _server_proc and _server_proc.poll() is None:
        return
    if not (os.path.exists(WHISPER_SERVER) and os.path.exists(WHISPER_MODEL)):
        log("whisper-server/モデル未準備 → ローカル無効")
        return
    _server_proc = subprocess.Popen(
        [WHISPER_SERVER, "-m", WHISPER_MODEL, "--host", "127.0.0.1",
         "--port", str(LOCAL_PORT), "-l", LANGUAGE,
         "-t", "4", "-bs", "1", "-bo", "1", "-nf",
         "-nt", "-sns", "-fa"],   # 高速&幻聴抑制: 貪欲法・フォールバック禁止・非音声抑制・flash-attn（-mc 0はvocab無効化のため不可）
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    log("ローカルサーバ起動（モデルロード中…数秒で準備完了）")


def stop_local_server():
    global _server_proc
    try:
        if _server_proc:
            _server_proc.terminate()
    except Exception:
        pass
    _server_proc = None


def _get_lconn():
    global _lconn
    if _lconn is None:
        _lconn = http.client.HTTPConnection("127.0.0.1", LOCAL_PORT, timeout=30)
    return _lconn


def _reset_lconn():
    global _lconn
    try:
        if _lconn:
            _lconn.close()
    except Exception:
        pass
    _lconn = None


def transcribe_local(audio, prompt):
    """ローカル whisper-server で文字起こし（audio=wavバイト列）。
    通信失敗(サーバ未起動等)=None / 無音="" / 成功=テキスト。"""
    fields = {"temperature": "0", "response_format": "json", "language": LANGUAGE}
    if prompt:
        fields["prompt"] = prompt
    bd, body = _multipart(fields, audio)
    headers = {"Content-Type": f"multipart/form-data; boundary={bd}"}
    t0 = time.time()
    for attempt in (1, 2):
        try:
            with _lconn_lock:
                conn = _get_lconn()
                conn.request("POST", "/inference", body=body, headers=headers)
                raw = conn.getresponse().read()
            data = json.loads(raw.decode("utf-8", "replace"))
            break
        except (http.client.HTTPException, OSError, ValueError):
            _reset_lconn()
            if attempt == 2:
                log("ローカル未応答 → Groqへフォールバック")
                return None
    text = (data.get("text") or "").strip()
    log(f"⚡(local) {time.time()-t0:.2f}s 「{text}」")
    return text


def keepwarm_loop(api_key):
    """Groq接続を定期的に温めてkeep-aliveを維持（放置後の1回目も速く）"""
    headers = {"Authorization": f"Bearer {api_key}"}
    while True:
        time.sleep(KEEPWARM_SEC)
        try:
            with _conn_lock:
                conn = _get_conn()
                conn.request("GET", "/openai/v1/models", headers=headers)
                conn.getresponse().read()
        except (http.client.HTTPException, OSError):
            _reset_conn()


def _play(path):
    """合図音を非ブロッキングで鳴らす（録音開始/停止のフィードバック）"""
    try:
        subprocess.Popen(["afplay", "-v", SOUND_VOL, path],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def paste_text(text):
    """NSPasteboardに入れて Cmd+V を Quartz で送出（pbcopy+sleepより約26ms速い）"""
    pb = NSPasteboard.generalPasteboard()
    pb.clearContents()
    pb.setString_forType_(text, NSPasteboardTypeString)
    if PASTE_AFTER:
        src = Quartz.CGEventSourceCreate(Quartz.kCGEventSourceStateHIDSystemState)
        V = 9  # macOS keycode for 'v'
        down = Quartz.CGEventCreateKeyboardEvent(src, V, True)
        Quartz.CGEventSetFlags(down, Quartz.kCGEventFlagMaskCommand)
        up = Quartz.CGEventCreateKeyboardEvent(src, V, False)
        Quartz.CGEventSetFlags(up, Quartz.kCGEventFlagMaskCommand)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, down)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, up)


# === 無音時のWhisper幻聴フレーズ（完全一致したら貼らずに捨てる）===
HALLUCINATIONS = {
    "ご視聴ありがとうございました", "ご視聴ありがとうございます",
    "ご清聴ありがとうございました", "ご清聴ありがとうございます",
    "最後までご視聴いただきありがとうございます",
    "チャンネル登録お願いします", "チャンネル登録よろしくお願いします",
    "次の動画でお会いしましょう",
}

# === 音声コマンド（その発話"まるごと"がこの語の時だけ発動）===
KEY_RETURN = 36
KEY_Z = 6


def _post_key(keycode, flags=0):
    src = Quartz.CGEventSourceCreate(Quartz.kCGEventSourceStateHIDSystemState)
    down = Quartz.CGEventCreateKeyboardEvent(src, keycode, True)
    up = Quartz.CGEventCreateKeyboardEvent(src, keycode, False)
    if flags:
        Quartz.CGEventSetFlags(down, flags)
        Quartz.CGEventSetFlags(up, flags)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, down)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, up)


def _type_unicode(s):
    src = Quartz.CGEventSourceCreate(Quartz.kCGEventSourceStateHIDSystemState)
    for ev_down in (True, False):
        ev = Quartz.CGEventCreateKeyboardEvent(src, 0, ev_down)
        Quartz.CGEventKeyboardSetUnicodeString(ev, len(s), s)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)


def _cmd_newline():
    _post_key(KEY_RETURN)


def _cmd_undo():
    _post_key(KEY_Z, Quartz.kCGEventFlagMaskCommand)


def _cmd_period():
    _type_unicode("。")


def _cmd_comma():
    _type_unicode("、")


COMMANDS = {}
for _w in ("改行", "かいぎょう", "新しい行", "あたらしい行"):
    COMMANDS[_w] = _cmd_newline
for _w in ("取り消し", "とりけし", "今の消して", "いまのけして", "アンドゥ"):
    COMMANDS[_w] = _cmd_undo
for _w in ("まる", "句点"):
    COMMANDS[_w] = _cmd_period
for _w in ("てん", "読点"):
    COMMANDS[_w] = _cmd_comma


def _norm(t):
    """前後の句読点・空白を落として比較用に正規化"""
    return t.strip().strip("。、.!！?？ 　\n\t").strip()


def handle_text(text, polish=False, api_key=None):
    """幻聴除去 → コマンド判定 → (任意で校正) → 全角化 → 貼り付け"""
    if not text:
        return
    norm = _norm(text)
    if norm in HALLUCINATIONS:
        log(f"幻聴除去: 「{text}」")
        return
    cmd = COMMANDS.get(norm)
    if cmd:
        log(f"コマンド実行: {norm}")
        cmd()
        return
    if polish and api_key:
        p = polish_text(text, api_key)
        if p:
            text = p
    text = text.replace("!", "！").replace("?", "？")  # 半角記号→全角（日本語表記に合わせる）
    paste_text(text)


class FnWatcher:
    """Quartz CGEventTap で Fn の押下/解放を検知（pynputでは拾えないため）"""
    def __init__(self, on_down, on_up):
        self.on_down = on_down
        self.on_up = on_up
        self.fn = False
        self.tap = None

    def _cb(self, proxy, etype, event, refcon):
        if etype in (Quartz.kCGEventTapDisabledByTimeout,
                     Quartz.kCGEventTapDisabledByUserInput):
            if self.tap:
                Quartz.CGEventTapEnable(self.tap, True)
            return event
        flags = Quartz.CGEventGetFlags(event)
        fn = bool(flags & FN_FLAG)
        if fn and not self.fn:
            self.fn = True
            try:
                self.on_down()
            except Exception as e:
                log(f"on_down err: {e}")
        elif not fn and self.fn:
            self.fn = False
            try:
                self.on_up()
            except Exception as e:
                log(f"on_up err: {e}")
        return event

    def start(self) -> bool:
        mask = Quartz.CGEventMaskBit(Quartz.kCGEventFlagsChanged)
        self.tap = Quartz.CGEventTapCreate(
            Quartz.kCGSessionEventTap, Quartz.kCGHeadInsertEventTap,
            Quartz.kCGEventTapOptionListenOnly, mask, self._cb, None)
        if not self.tap:
            return False  # 入力監視の許可なし
        src = Quartz.CFMachPortCreateRunLoopSource(None, self.tap, 0)
        Quartz.CFRunLoopAddSource(
            Quartz.CFRunLoopGetMain(), src, Quartz.kCFRunLoopCommonModes)
        Quartz.CGEventTapEnable(self.tap, True)
        return True


class VoiceApp(rumps.App):
    def __init__(self):
        super().__init__("🎤", quit_button=None)
        self.engine = DEFAULT_ENGINE
        self.polish = DEFAULT_POLISH
        self.is_recording = False
        self.handsfree = False
        self._t_down = 0.0
        self._pending = None
        self.status_item = rumps.MenuItem("待機中")
        self.engine_item = rumps.MenuItem(self._engine_label(), callback=self._toggle_engine)
        self.polish_item = rumps.MenuItem(self._polish_label(), callback=self._toggle_polish)
        self.menu = [self.status_item, self.engine_item, self.polish_item,
                     None, rumps.MenuItem("終了", callback=self._quit)]
        self.api_key = load_env()
        self.prompt = load_vocab()
        self.recorder = Recorder(detect_mic_index())
        self.watcher = FnWatcher(self._on_down, self._on_up)
        log("=== 起動 ===")

    def _engine_label(self):
        return "エンジン: " + ("ローカル(オフライン)" if self.engine == "local" else "Groq(クラウド)")

    def _polish_label(self):
        return "日本語校正: " + ("ON" if self.polish else "OFF")

    def _toggle_engine(self, _):
        self.engine = "local" if self.engine == "groq" else "groq"
        self.engine_item.title = self._engine_label()
        log(f"エンジン切替→ {self.engine}")

    def _toggle_polish(self, _):
        self.polish = not self.polish
        self.polish_item.title = self._polish_label()
        log(f"校正切替→ {self.polish}")

    def start_tap(self):
        if not self.watcher.start():
            self._status("⚠ 入力監視の許可が必要")
            rumps.notification(
                "音声入力", "入力監視の許可が必要です",
                "システム設定 > プライバシーとセキュリティ > 入力監視 で voice_input をONにして再起動してください")
        elif not self.api_key:
            self._status("⚠ GROQ_API_KEY 未設定")
        else:
            self._status("待機中（Fnで録音）")

    def _status(self, s):
        self.status_item.title = s

    def _on_down(self):
        if self.is_recording:
            if self.handsfree:
                self._end()              # ハンズフリー中の押下 = 手動停止
            else:
                self._engage_handsfree() # 短押し直後の2回目 = ダブルクリック→ハンズフリー化
            return
        self._t_down = time.time()
        self._begin()                    # 1回目（ホールドか1回目タップか未確定・とりあえず録音開始）

    def _on_up(self):
        if not self.is_recording or self.handsfree:
            return
        held = time.time() - self._t_down
        if held >= DOUBLE_CLICK_SEC:
            self._end()                  # ホールド: 離して確定（従来どおり・即時）
        else:
            self._start_pending()        # 短いタップ: ダブルクリック待ち（録音は継続）

    def _begin(self):
        if self.is_recording:
            return
        self.is_recording = True
        self.handsfree = False
        self.title = "🔴"
        _play(SOUND_START)
        self.recorder.start(vad=False)

    def _engage_handsfree(self):
        self._cancel_pending()
        self.handsfree = True
        _play(SOUND_START)               # 2つ目のチン = ハンズフリー開始の合図
        self.recorder.enable_vad(self._auto_stop)
        log("ハンズフリー開始（無音で自動停止）")

    def _start_pending(self):
        self._cancel_pending()
        self._pending = threading.Timer(DOUBLE_CLICK_SEC, self._pending_fire)
        self._pending.daemon = True
        self._pending.start()

    def _cancel_pending(self):
        if self._pending:
            self._pending.cancel()
            self._pending = None

    def _pending_fire(self):
        self._end()                      # 2回目が来なかった → 単発の短押しとして確定

    def _auto_stop(self):
        self._end()                      # VAD(無音)スレッドから呼ばれる

    def _end(self):
        if not self.is_recording:
            return
        self._cancel_pending()
        self.is_recording = False
        self.handsfree = False
        self.title = "🎤"
        _play(SOUND_STOP)
        wav = self.recorder.stop()
        if wav:
            threading.Thread(target=self._process, args=(wav,), daemon=True).start()

    def _process(self, audio):
        text = None
        if self.engine == "local":
            text = transcribe_local(audio, self.prompt)   # None=失敗→Groq, ""=無音
        if text is None:
            text = transcribe(audio, self.api_key, self.prompt)
        handle_text(text, self.polish, self.api_key)   # 幻聴除去・コマンド・校正・貼り付け

    def _quit(self, _):
        stop_local_server()
        rumps.quit_application()


def main():
    os.environ.setdefault("LC_ALL", "en_US.UTF-8")
    os.environ.setdefault("LANG", "en_US.UTF-8")
    app = VoiceApp()
    app.start_tap()  # 入力監視タップをメインrun loopに仕掛けてから起動
    start_local_server()  # ローカルWhisperを常駐起動（モデルロード開始）
    threading.Thread(target=prewarm, daemon=True).start()  # Groq接続を温める（フォールバック用）
    if app.api_key:
        threading.Thread(target=keepwarm_loop, args=(app.api_key,), daemon=True).start()
    app.run()


if __name__ == "__main__":
    main()

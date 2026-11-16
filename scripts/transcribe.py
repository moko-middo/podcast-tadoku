"""content/podcasts/ の音声を文字起こし・和訳して、同名の .json に書き出す。

GitHub Actions から実行される（.github/workflows/deploy.yml）。
- 文字起こし: faster-whisper（単語ごとのタイミング付き）。モデルは環境変数 WHISPER_MODEL（既定 small.en）
- 和訳: 環境変数 GEMINI_API_KEY があれば Gemini API で 1 文ずつ訳す。なければ訳なしで書き出し、
  あとからキーを設定すると次回の実行で訳だけを追加する
- スクリプト: 音声と同じ名前の .txt があれば、英文はスクリプトのものを使い、Whisper の時刻を割り当てる。
  英文の次の行に日本語があれば、それを和訳として使う（Gemini は使わない）
- WAV / FLAC は容量削減のため MP3 に変換し、元ファイルは削除する

使い方:
  python3 scripts/transcribe.py           # 未処理の音声をすべて処理
  python3 scripts/transcribe.py --check   # 処理待ちの件数だけを出力
  python3 scripts/transcribe.py --skip-translate   # 文字起こしだけ（和訳は次の実行で）
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PODCAST_DIR = ROOT / "content" / "podcasts"
AUDIO_EXT = {".mp3", ".m4a", ".aac", ".ogg", ".opus", ".webm", ".wav", ".flac"}
CONVERT_EXT = {".wav", ".flac"}

WHISPER_MODEL = os.environ.get("WHISPER_MODEL") or "small.en"
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
# 先頭から順に試し、混雑していたら次のモデルへ（環境変数 GEMINI_MODEL で先頭を指定できる）
GEMINI_MODELS = list(dict.fromkeys(m for m in [os.environ.get("GEMINI_MODEL"), "gemini-flash-latest", "gemini-flash-lite-latest"] if m))
TRANSLATE_BATCH = 40


def ffmpeg() -> str:
    """システムの ffmpeg、なければ imageio-ffmpeg 同梱のもの（GitHub Actions のランナーには ffmpeg がない）"""
    if path := shutil.which("ffmpeg"):
        return path
    import imageio_ffmpeg

    return imageio_ffmpeg.get_ffmpeg_exe()

# 文末とみなさない略語
ABBREV = re.compile(r"^(?:mr|mrs|ms|dr|prof|st|vs|etc|e\.g|i\.e|u\.s|no)\.$", re.I)
SENTENCE_END = re.compile(r"[.?!][\"'”’)\]]*$")


def audio_files() -> list[Path]:
    return sorted(p for p in PODCAST_DIR.iterdir() if p.suffix.lower() in AUDIO_EXT) if PODCAST_DIR.exists() else []


def transcript_path(audio: Path) -> Path:
    return audio.with_suffix(".json")


def load(path: Path) -> dict | None:
    return json.loads(path.read_text("utf-8")) if path.exists() else None


def save(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", "utf-8")


def pending() -> list[Path]:
    out = []
    for audio in audio_files():
        t = load(transcript_path(audio))
        if (
            t is None
            or (script_path(audio).exists() and t.get("scriptHash") != script_hash(audio))
            or (GEMINI_API_KEY and any("ja" not in s for s in t["sentences"]))
        ):
            out.append(audio)
    return out


# ── 音声 ──────────────────────────────────────────


def convert_to_mp3(audio: Path) -> Path:
    mp3 = audio.with_suffix(".mp3")
    if mp3.exists():
        sys.exit(f"{mp3.name} が既にあるため {audio.name} を変換できません。どちらかの名前を変えてください")
    subprocess.run(
        [ffmpeg(), "-hide_banner", "-loglevel", "error", "-i", str(audio), "-ac", "1", "-b:a", "96k", str(mp3)],
        check=True,
    )
    audio.unlink()
    print(f"  {audio.name} -> {mp3.name}")
    return mp3


# ── 文字起こし ────────────────────────────────────

_model = None


def whisper():
    global _model
    if _model is None:
        from faster_whisper import WhisperModel

        print(f"  loading whisper model {WHISPER_MODEL} ...")
        _model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
    return _model


def decode(audio: Path):
    """ffmpeg で 16kHz モノラルの波形にする（faster-whisper 内蔵の PyAV 読み込みは版の相性で壊れやすいため使わない）"""
    import numpy as np

    pcm = subprocess.run(
        [ffmpeg(), "-hide_banner", "-loglevel", "error", "-i", str(audio), "-ac", "1", "-ar", "16000", "-f", "f32le", "-"],
        check=True,
        capture_output=True,
    ).stdout
    return np.frombuffer(pcm, dtype=np.float32)


def transcribe(audio: Path) -> tuple[list[tuple[float, float, str]], float]:
    segments, info = whisper().transcribe(decode(audio), language="en", word_timestamps=True, beam_size=5)
    words = [(w.start, w.end, w.word) for seg in segments for w in (seg.words or [])]
    return words, info.duration


def to_sentences(words: list[tuple[float, float, str]], max_words: int = 25) -> list[dict]:
    """単語列を文に分ける。文末記号・長い無音・長すぎる文（読点）で区切る"""
    sentences: list[dict] = []
    cur: list[tuple[float, float, str]] = []

    def flush() -> None:
        if cur:
            sentences.append(
                {
                    "start": round(cur[0][0], 2),
                    "end": round(cur[-1][1], 2),
                    "text": "".join(w for _, _, w in cur).strip(),
                    "words": [[round(s, 2), round(e, 2), w] for s, e, w in cur],
                }
            )
            cur.clear()

    for i, (s, e, w) in enumerate(words):
        cur.append((s, e, w))
        token = w.strip()
        nxt = words[i + 1] if i + 1 < len(words) else None
        ends = bool(SENTENCE_END.search(token)) and not ABBREV.match(token)
        long_pause = nxt is not None and nxt[0] - e > 1.5
        too_long = len(cur) >= max_words and token.endswith((",", ";", ":"))
        if ends or long_pause or too_long or len(cur) >= max_words * 2:
            flush()
    flush()
    return sentences


# ── スクリプト（自分で用意した英文・和訳） ──────────────

JAPANESE = re.compile(r"[\u3040-\u30ff\u3400-\u9fff\uff01-\uff60]")
SPEAKER = re.compile(r"^(?:\*\*)?([A-Za-z][A-Za-z0-9 .]{0,19})(?:\*\*)?\s*[:：]\s+")


def script_path(audio: Path) -> Path:
    return audio.with_suffix(".txt")


def script_hash(audio: Path) -> str:
    return hashlib.sha1(script_path(audio).read_bytes()).hexdigest()[:12]


def parse_script(text: str) -> list[dict]:
    """英文 1 行 = 1 文。その次の行が日本語ならその文の和訳。空行や見出し記号は無視する"""
    lines = [line.strip() for line in text.lstrip("\ufeff").splitlines()]
    lines = [line for line in lines if not line.startswith("#")]  # Markdown の見出し
    lines = [re.sub(r"^[-*>•]\s+", "", line).replace("**", "").strip() for line in lines]
    # 「Host: ...」のような話者名は、同じ名前が何度も出てくるときだけ外す（「Note: ...」のような文を壊さないため）
    labels = [m.group(1) for line in lines if (m := SPEAKER.match(line))]
    common = {label for label in labels if labels.count(label) >= 3}
    items: list[dict] = []
    for line in lines:
        if not line:
            continue
        if JAPANESE.search(line):
            if items:  # 英文より前の日本語（タイトルなど）は無視
                items[-1]["ja"] = (items[-1].get("ja", "") + line).strip()
            continue
        if (m := SPEAKER.match(line)) and m.group(1) in common:
            line = line[m.end():]
        items.append({"text": line})
    return items


def _norm(word: str) -> str:
    return re.sub(r"[^a-z0-9']", "", word.lower().replace("’", "'"))


def align(items: list[dict], heard: list[list], duration: float) -> list[dict]:
    """スクリプトの単語に、聞き取った単語（[開始, 終了, 語]）の時刻を割り当てる。
    一致した語はその時刻、聞き間違えた語は対応する範囲を按分、聞き取れなかった語は前後の間を按分"""
    tokens = [(i, w) for i, item in enumerate(items) for w in item["text"].split()]
    times: list[tuple[float, float] | None] = [None] * len(tokens)
    heard_any = [False] * len(items)  # 音声と対応がとれた語があるか
    a = [_norm(w[2]) for w in heard]
    b = [_norm(w) for _, w in tokens]
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag == "equal":
            for k in range(j2 - j1):
                times[j1 + k] = (heard[i1 + k][0], heard[i1 + k][1])
                heard_any[tokens[j1 + k][0]] = True
        elif tag == "replace":
            start, end = heard[i1][0], heard[i2 - 1][1]
            step = (end - start) / (j2 - j1)
            for k in range(j2 - j1):
                times[j1 + k] = (start + step * k, start + step * (k + 1))
    # 残り（聞き取れなかった語）は前後の時刻の間に並べる
    j = 0
    while j < len(times):
        if times[j] is not None:
            j += 1
            continue
        k = j
        while k < len(times) and times[k] is None:
            k += 1
        start = times[j - 1][1] if j > 0 else 0.0
        end = times[k][0] if k < len(times) else max(duration, start)
        step = max(end - start, 0) / (k - j)
        for n in range(j, k):
            times[n] = (start + step * (n - j), start + step * (n - j + 1))
        j = k
    # 時刻が逆戻りしないようにそろえる
    last = 0.0
    for n, (s0, e0) in enumerate(times):
        s0 = max(s0, last)
        e0 = max(e0, s0)
        times[n] = (s0, e0)
        last = s0

    # 最初と最後の、音声にまったく出てこない行（タイトルやメモ）は外す
    first = heard_any.index(True) if True in heard_any else 0
    last_i = len(items) - 1 - heard_any[::-1].index(True) if True in heard_any else len(items) - 1
    sentences = []
    for i, item in enumerate(items):
        if i < first or i > last_i:
            continue
        ws = [[round(times[n][0], 2), round(times[n][1], 2), " " + w] for n, (si, w) in enumerate(tokens) if si == i]
        if not ws:
            continue
        sentence = {"start": ws[0][0], "end": ws[-1][1], "text": item["text"], "words": ws}
        if item.get("ja"):
            sentence["ja"] = item["ja"]
        sentences.append(sentence)
    return sentences


def apply_script(audio: Path, data: dict) -> bool:
    """スクリプトがあり、前回から変わっていれば文を作り直す"""
    path = script_path(audio)
    if not path.exists() or data.get("scriptHash") == script_hash(audio):
        return False
    items = parse_script(path.read_text("utf-8", errors="replace"))
    if not items:
        print(f"  {path.name} に英文が見つからないため使いません")
        return False
    heard = [w for s in data["sentences"] for w in s["words"]]
    data["sentences"] = align(items, heard, data["duration"])
    data["scriptHash"] = script_hash(audio)
    with_ja = sum("ja" in s for s in data["sentences"])
    print(f"  スクリプト {path.name}: {len(data['sentences'])} 文（和訳あり {with_ja}）")
    return True


# ── Gemini（和訳・タイトル） ────────────────────────


def gemini(prompt: str, as_json: bool) -> str:
    """混雑（429 / 5xx）やタイムアウトは待って再試行し、それでもだめなら次のモデルに切り替える"""
    body = {"contents": [{"parts": [{"text": prompt}]}]}
    if as_json:
        body["generationConfig"] = {"responseMimeType": "application/json"}
    last_error = ""
    for model in GEMINI_MODELS:
        req = urllib.request.Request(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "x-goog-api-key": GEMINI_API_KEY},
        )
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=90) as res:
                    data = json.load(res)
                return data["candidates"][0]["content"]["parts"][0]["text"]
            except urllib.error.HTTPError as e:
                if e.code not in (429, 500, 502, 503, 504):
                    raise RuntimeError(f"Gemini API error {e.code}: {e.read().decode()[:500]}") from None
                last_error = f"{model}: {e.code}"
            except (TimeoutError, urllib.error.URLError) as e:
                last_error = f"{model}: {e}"
            if attempt < 2:
                wait = 20 * (attempt + 1)
                print(f"  {last_error}, {wait}s 待って再試行")
                time.sleep(wait)
        print(f"  {model} が応答しないため、次のモデルを試します")
    raise RuntimeError(f"Gemini API が混雑していて応答がありません（{last_error}）")


def translate(sentences: list[dict], on_progress=lambda: None) -> None:
    todo = [i for i, s in enumerate(sentences) if "ja" not in s]
    for b in range(0, len(todo), TRANSLATE_BATCH):
        idx = todo[b : b + TRANSLATE_BATCH]
        context = " ".join(sentences[i]["text"] for i in range(max(0, idx[0] - 3), idx[0]))
        items = [sentences[i]["text"] for i in idx]
        prompt = (
            "以下は英語ポッドキャストを文ごとに区切った文字起こしです。英語学習者向けに、各文を自然な日本語に訳してください。\n"
            "音声認識の誤りがありそうな箇所は文脈から推測して訳してください。\n"
            f"入力と同じ順番・同じ件数（{len(items)} 件）の、日本語訳の文字列だけからなる JSON 配列で答えてください。\n\n"
            + (f"直前の文脈（訳さない）: {context}\n\n" if context else "")
            + json.dumps(items, ensure_ascii=False)
        )
        for attempt in range(3):
            result = json.loads(gemini(prompt, as_json=True))
            if isinstance(result, list) and len(result) == len(items) and all(isinstance(r, str) for r in result):
                break
            print(f"  訳の件数が合わないため再試行 ({len(result) if isinstance(result, list) else '?'} / {len(items)})")
        else:
            raise RuntimeError("Gemini の訳を取得できませんでした")
        for i, ja in zip(idx, result):
            sentences[i]["ja"] = ja.strip()
        on_progress()  # Actions の時間切れで止められても、訳せた分は残す
        print(f"  translated {min(b + TRANSLATE_BATCH, len(todo))}/{len(todo)}")


def make_title(sentences: list[dict]) -> str | None:
    head = " ".join(s["text"] for s in sentences[:40])
    prompt = (
        "Give a short, catchy English title (at most 8 words) for the podcast episode that starts like this. "
        "Answer with the title only, without quotes.\n\n" + head
    )
    title = gemini(prompt, as_json=False).strip().strip('"').strip()
    return title or None


# ── main ─────────────────────────────────────────


def default_title(audio: Path) -> str:
    return re.sub(r"[-_]+", " ", audio.stem).strip()


class TranslationError(Exception):
    """文字起こしはできたが和訳に失敗した（次回の実行で再試行される）"""


def process(audio: Path) -> None:
    print(f"[podcast] {audio.name}")
    if audio.suffix.lower() in CONVERT_EXT:
        audio = convert_to_mp3(audio)
    path = transcript_path(audio)
    data = load(path)

    if data is None:
        started = time.time()
        words, duration = transcribe(audio)
        data = {
            "title": default_title(audio),
            "audio": audio.name,
            "duration": round(duration, 2),
            "createdAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "whisperModel": WHISPER_MODEL,
            "sentences": to_sentences(words),
        }
        save(path, data)
        print(f"  {len(words)} words, {len(data['sentences'])} sentences ({time.time() - started:.0f}s)")

    if apply_script(audio, data):
        save(path, data)

    if GEMINI_API_KEY:
        if data["title"] == default_title(audio):
            try:
                data["title"] = make_title(data["sentences"]) or data["title"]
            except RuntimeError as e:
                print(f"  タイトル生成に失敗: {e}")
        try:
            translate(data["sentences"], lambda: save(path, data))
        except RuntimeError as e:
            raise TranslationError(e) from None
        finally:
            save(path, data)  # 途中で失敗しても訳せた分は残す（残りは次回）


def main() -> None:
    global GEMINI_API_KEY
    if "--skip-translate" in sys.argv:
        GEMINI_API_KEY = ""
    todo = pending()
    if "--check" in sys.argv:
        print(len(todo))
        return
    if not todo:
        print("[podcast] 処理待ちの音声はありません")
        return
    if not GEMINI_API_KEY and "--skip-translate" not in sys.argv:
        print("[podcast] GEMINI_API_KEY が未設定のため、和訳なしで文字起こしだけ行います")
    failed, untranslated = [], []
    for audio in todo:
        try:
            process(audio)
        except TranslationError as e:
            print(f"  和訳に失敗（次回の実行で再試行）: {e}")
            untranslated.append(audio.name)
        except Exception as e:  # 1 本の失敗で他のエピソードを止めない
            print(f"  失敗: {e}")
            failed.append(audio.name)
    if untranslated:
        # 和訳がなくても再生はできるので、サイトの公開は止めない
        print(f"::warning::和訳できなかったエピソード: {', '.join(untranslated)}（次に Actions が動いたときに再試行します）")
    if failed:
        sys.exit(f"[podcast] 失敗: {', '.join(failed)}")


if __name__ == "__main__":
    main()

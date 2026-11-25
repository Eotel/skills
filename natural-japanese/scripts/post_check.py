#!/usr/bin/env -S uv run --no-project --no-config --python >=3.12
from __future__ import annotations

import argparse
from hashlib import sha256
from pathlib import Path
import subprocess
import sys
import tempfile
from uuid import uuid4


ROOT = Path(__file__).resolve().parent.parent.parent


def read_draft(path: Path) -> bytes:
    if not path.is_file():
        raise ValueError(f"原稿のファイルを確認できません。{path}")
    return path.read_bytes()


def stdin_block(role: str, data: bytes) -> bytes:
    return f"<<<yomiyasu:{role}>>>\n".encode() + data + (b"\n" if data and not data.endswith((b"\n", b"\r")) else b"")


def verify_receipt(final: Path, receipt: Path) -> int:
    if receipt.is_file() and receipt.read_text(encoding="ascii") == sha256(read_draft(final)).hexdigest():
        return 0
    print(f"post_check: 投稿文の検査記録がありません。post_check.py --final {str(final)!r} を実行してください。",
          file=sys.stderr)
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="投稿文の表現を検査し、yomiyasu の実行・照合記録を作成します。")
    parser.add_argument("--final", required=True, type=Path, help="投稿する最終稿（UTF-8）")
    parser.add_argument("--source", type=Path, help="推敲前の原文（UTF-8）。新規作成では省略")
    parser.add_argument("--material", action="append", type=Path, default=[], help="照合する資料。複数指定可")
    parser.add_argument("--doc-type", help="yomiyasu に渡す文書の種類。例: pr, mail")
    parser.add_argument("--verify-only", action="store_true", help="最終稿の内容を、作成済みの検査記録と照合")
    args = parser.parse_args()
    if args.verify_only and (args.source or args.material or args.doc_type):
        parser.error("--verify-only には --final だけを指定してください。")

    try:
        final = args.final.expanduser().resolve()
        receipt = Path.home() / ".cache/agent-hooks/post-check" / sha256(str(final).encode()).hexdigest()
        if args.verify_only:
            return verify_receipt(final, receipt)

        draft = read_draft(final)
        if not draft.decode("utf-8-sig").strip():
            raise ValueError("最終稿が空です。")
        natural = ROOT / "natural-japanese/scripts/check_japanese.py"
        yomiyasu = ROOT / "yomiyasu/scripts/yomiyasu_check.py"
        subprocess.run([sys.executable, str(natural), "-"], input=draft, check=True)

        inputs = []
        if args.source:
            inputs.append(stdin_block("original", read_draft(args.source.expanduser())))
        inputs.extend(stdin_block("material", read_draft(path.expanduser())) for path in args.material)
        if not inputs:
            inputs.append(stdin_block("material", b""))
        inputs.append(b"<<<yomiyasu:final>>>\n" + draft)
        run_id = "post-check-" + uuid4().hex
        cli = [sys.executable, str(yomiyasu)]
        run = [*cli, "run", "--stdin", "--run-id", run_id]
        if args.doc_type:
            run.extend(["--doc-type", args.doc_type])
        subprocess.run(run, input=b"".join(inputs), check=True)
        subprocess.run([*cli, "verify", "--final", "-", "--run-id", run_id], input=draft, check=True)
        if read_draft(final) != draft:
            raise ValueError("検査中に最終稿が変更されました。再実行してください。")

        receipt.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", encoding="ascii", dir=receipt.parent, delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(sha256(draft).hexdigest())
        try:
            temporary.replace(receipt)
        finally:
            temporary.unlink(missing_ok=True)
    except subprocess.CalledProcessError as error:
        return error.returncode
    except (OSError, ValueError, RuntimeError) as error:
        print(f"post_check: {error}", file=sys.stderr)
        return 2
    print("投稿文の検査と最終稿の照合を記録しました。表示された候補を確認してください。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

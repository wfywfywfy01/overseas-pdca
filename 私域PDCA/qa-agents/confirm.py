"""交互式 Memory 确认工具 — 逐条审批 Hermes 的待确认建议。"""

import re
import sys
from pathlib import Path

AGENTS_DIR = Path(__file__).parent

AGENTS = ["behavior-guard", "sop-checker", "quality-scorer"]

PENDING_MARKERS = ["待主管确认", "待主管审批"]

# 确认后，将内容从"待确认"区移入对应的已确认区
CONFIRM_TARGET = {
    "behavior-guard": "已确认禁用词",
    "sop-checker":    "已确认阈值",
    "quality-scorer": "已确认发现",
}


def extract_pending_blocks(text: str) -> list[tuple[int, int, str]]:
    """返回 [(start_line, end_line, block_text), ...] 的待确认块列表。"""
    lines = text.splitlines()
    blocks = []
    i = 0
    while i < len(lines):
        if any(marker in lines[i] for marker in PENDING_MARKERS) and lines[i].startswith("###"):
            start = i
            i += 1
            while i < len(lines) and not (lines[i].startswith("###") or lines[i].startswith("## ")):
                i += 1
            block = "\n".join(lines[start:i]).strip()
            if block:
                blocks.append((start, i, block))
        else:
            i += 1
    return blocks


def show_block(agent: str, block: str):
    print(f"\n{'='*60}")
    print(f"Agent: {agent}")
    print(f"{'─'*60}")
    # 打印块内容，跳过标题行
    for line in block.splitlines()[1:]:
        if line.strip():
            print(f"  {line}")
    print(f"{'─'*60}")


def remove_block_from_memory(path: Path, start: int, end: int):
    lines = path.read_text(encoding="utf-8").splitlines()
    del lines[start:end]
    path.write_text("\n".join(lines), encoding="utf-8")


def append_confirmed(agent: str, content: str):
    path = AGENTS_DIR / agent / "MEMORY.md"
    from datetime import datetime
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    section = CONFIRM_TARGET.get(agent, "已确认")
    entry = f"\n### [{ts}] {section}（主管已批准）\n{content.strip()}\n"
    with open(path, "a", encoding="utf-8") as f:
        f.write(entry)


def run():
    any_pending = False
    for agent in AGENTS:
        path = AGENTS_DIR / agent / "MEMORY.md"
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        blocks = extract_pending_blocks(text)
        if not blocks:
            continue

        for start, end, block in blocks:
            any_pending = True
            show_block(agent, block)
            print("  [Y] 确认生效  [N] 忽略跳过  [D] 删除此条  [Q] 退出")
            while True:
                choice = input("  你的选择: ").strip().upper()
                if choice in ("Y", "N", "D", "Q"):
                    break
                print("  请输入 Y / N / D / Q")

            if choice == "Q":
                print("\n已退出，未处理的条目保持原状。")
                sys.exit(0)
            elif choice == "Y":
                # 提取实际内容（去掉 ### 标题行）
                content_lines = block.splitlines()[1:]
                content = "\n".join(content_lines).strip()
                append_confirmed(agent, content)
                remove_block_from_memory(path, start, end)
                print("  ✅ 已确认并写入生效区")
            elif choice == "D":
                remove_block_from_memory(path, start, end)
                print("  🗑  已删除")
            else:
                print("  ⏭  已跳过")

    if not any_pending:
        print("✅ 所有 Agent MEMORY 中无待确认内容。")


if __name__ == "__main__":
    run()

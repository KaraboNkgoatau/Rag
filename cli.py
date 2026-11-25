"""Command-line interface.

  python cli.py "question"   answer one question and exit
  python cli.py              interactive mode; empty line to quit
"""
import sys

from rag.engine import Engine, LLMError


def show(engine: Engine, question: str) -> None:
    try:
        res = engine.ask(question)
    except LLMError as e:
        print(f"\nERROR: {e}")
        return
    print("\n" + res["answer"])
    for s in res["sources"]:
        print(f"  [{s['n']}] {s['citation']}")


def main() -> None:
    engine = Engine()
    if len(sys.argv) > 1:
        show(engine, " ".join(sys.argv[1:]))
        return
    print(f"Indexed {len(engine.chunks)} chunks. Empty line to quit.")
    while question := input("\nQ> ").strip():
        show(engine, question)


if __name__ == "__main__":
    main()

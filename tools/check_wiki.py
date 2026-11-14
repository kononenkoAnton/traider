"""Проверка вики: uv run --with python-frontmatter python tools/check_wiki.py

Разбирает статьи той же библиотекой, что и WikiKnowledge, и падает на первой ошибке.
"""
import pathlib
import re
import sys

import frontmatter

ROOT = pathlib.Path(__file__).resolve().parent.parent / "wiki"
MARKERS = ("<!-- human:start -->", "<!-- human:end -->", "<!-- ai:start -->", "<!-- ai:end -->")


def main() -> None:
    arts = {}
    for path in sorted([*ROOT.glob("articles/*.md"), *ROOT.glob("categories/*.md")]):
        post = frontmatter.load(path)
        meta = post.metadata
        assert meta["id"] == path.stem, f"{path}: id не совпадает с именем файла"
        assert "/" not in meta["id"], f"{path}: в id есть /"
        want = "category" if path.parent.name == "categories" else "leaf"
        assert meta["type"] == want, f"{path}: type должен быть {want}"
        for field in ("title", "tags", "categories", "created", "modified"):
            assert field in meta, f"{path}: нет поля {field}"
        assert meta["id"] not in arts, f"{path}: id повторяется"
        arts[meta["id"]] = (meta, post.content)

    for aid, (meta, body) in arts.items():
        for cat in meta["categories"]:
            assert cat in arts and arts[cat][0]["type"] == "category", f"{aid}: нет категории {cat}"
        for link in re.findall(r"\[\[([^\]|#]+)", body):
            if ":" in link:  # src:, file:, gdrive: — внешние источники, здесь не проверяются
                continue
            assert link in arts, f"{aid}: битая ссылка [[{link}]]"
        if meta["type"] == "category":
            for marker in MARKERS:
                assert body.count(marker) == 1, f"{aid}: маркер {marker} должен встречаться один раз"
            ai_block = body.split(MARKERS[2])[1].split(MARKERS[3])[0]
            for member, (member_meta, _) in arts.items():
                if aid not in member_meta["categories"]:
                    continue
                assert f"[[{member}" in ai_block, f"{aid}: статья {member} не упомянута в блоке ai"
                assert str(member_meta["modified"]) <= str(meta["modified"]), (
                    f"{aid}: категория старше статьи {member}, обнови блок ai и modified"
                )
    print(f"вики в порядке: {len(arts)} статей")


if __name__ == "__main__":
    try:
        main()
    except AssertionError as err:
        sys.exit(f"ошибка вики: {err}")

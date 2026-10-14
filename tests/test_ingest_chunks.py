"""Разбиение документов на фрагменты."""

from pathlib import Path

from assistant.ingest.chunks import CHUNK_CHARS, exact_terms, split_document
from assistant.ingest.dataset import read_dataset
from datagen.world import World


def test_text_before_first_heading_is_separate_chunk() -> None:
    pieces = split_document("D1", "# Название\n\nВводный текст.\n\n## 1. Раздел\n\nТекст раздела.")
    assert [(p.chunk_id, p.heading, p.body) for p in pieces] == [
        ("D1#0", "", "Вводный текст."),
        ("D1#1", "1. Раздел", "Текст раздела."),
    ]


def test_title_line_alone_is_not_a_chunk() -> None:
    pieces = split_document("D1", "# Название\n\n## Первый\n\nа\n\n## Второй\n\nб\n")
    assert [p.chunk_id for p in pieces] == ["D1#1", "D1#2"]
    assert all("Название" not in p.body for p in pieces)


def test_document_without_headings_is_one_chunk() -> None:
    pieces = split_document("D1", "Просто текст\nв две строки.")
    assert [(p.chunk_id, p.section_no, p.part_no) for p in pieces] == [("D1#0", 0, 1)]
    assert pieces[0].body == "Просто текст\nв две строки."


def test_long_section_is_split_by_paragraphs() -> None:
    paragraphs = [f"Абзац {number}. " + "слово " * 20 for number in range(1, 9)]
    body = "## Длинный раздел\n\n" + "\n\n".join(paragraphs)
    pieces = split_document("D1", body, limit=400)
    assert [p.chunk_id for p in pieces] == [f"D1#1.{n}" for n in range(1, len(pieces) + 1)]
    assert len(pieces) > 1
    assert all(p.heading == "Длинный раздел" for p in pieces)
    assert all(len(p.heading) + len(p.body) <= 400 for p in pieces)
    # Ничего не потеряно и не переставлено.
    assert "\n\n".join(p.body for p in pieces) == "\n\n".join(paragraphs)


def test_table_on_part_boundary_keeps_its_header() -> None:
    head = "| Причина | Часов |\n|---|---|"
    rows = [f"| причина номер {number} | {number},5 |" for number in range(1, 31)]
    body = "## Таблица\n\nПеред таблицей.\n\n" + head + "\n" + "\n".join(rows) + "\n\nПосле."
    pieces = split_document("D1", body, limit=300)
    table_parts = [p for p in pieces if "| причина номер" in p.body]
    assert len(table_parts) > 1
    for piece in table_parts:
        # Шапка и разделитель идут с каждой частью таблицы.
        assert head in piece.body
        assert len(piece.heading) + len(piece.body) <= 300
    found = [line for p in pieces for line in p.body.split("\n") if line.startswith("| причина")]
    assert found == rows


def test_table_right_under_caption_keeps_its_header() -> None:
    head = "| Причина | Часов |  \n| --- | ---: |"
    rows = [f"| причина номер {number} | {number},5 |" for number in range(1, 31)]
    body = "## Таблица\n\nПодпись без пустой строки:\n" + head + "\n" + "\n".join(rows)
    pieces = split_document("D1", body, limit=300)
    table_parts = [p for p in pieces if "| причина номер" in p.body]
    assert len(table_parts) > 1
    assert all(head in p.body for p in table_parts)
    assert "Подпись без пустой строки:" in pieces[0].body


def test_table_with_rows_longer_than_limit_still_fits() -> None:
    # Потолок важнее повтора шапки: строка, которая не помещается, режется как текст.
    head = "| Показатель | Описание |\n|---|---|"
    rows = [f"| показатель {number} | " + "длинное описание " * 30 + "|" for number in range(1, 6)]
    pieces = split_document("D1", "## Таблица\n\n" + head + "\n" + "\n".join(rows), limit=300)
    assert len(pieces) > 5
    assert all(len(p.heading) + len(p.body) <= 300 for p in pieces)
    text = " ".join(p.body for p in pieces)
    assert all(f"показатель {number} " in text for number in range(1, 6))


def test_single_long_line_is_cut_at_spaces() -> None:
    line = " ".join(f"слово{number}" for number in range(200))
    pieces = split_document("D1", f"## Раздел\n\n{line}", limit=200)
    assert len(pieces) > 1
    assert all(len(p.body) <= 200 for p in pieces)
    assert " ".join(p.body for p in pieces) == line


def test_exact_terms_are_identifiers_and_numbers() -> None:
    terms = exact_terms("Экскаватор XY-7 модели ЖК-10, ёмкость 12,5 м3, план 137 400, пункт 4.2")
    # Похожие кириллические буквы — латинские: «к» в «жк», «м» в «м3».
    assert terms == sorted({"xy-7", "жk-10", "12.5", "m3", "137", "400", "4.2"})
    assert exact_terms("Слова без цифр и дефис-слова") == []
    # Косая черта идентификаторы не склеивает; запятая и точка в числе — одно и то же.
    assert exact_terms("XY-7/XY-8, ЖК-10/ЖК-12") == ["xy-7", "xy-8", "жk-10", "жk-12"]
    # Кириллица и латиница в идентификаторе дают один термин.
    assert exact_terms("ХУ-7") == exact_terms("XY-7") == ["xy-7"]
    assert exact_terms("38,1 и 38.1") == ["38.1"]


def test_generated_documents_fit_the_limit_and_keep_their_text(
    dataset: tuple[Path, World],
) -> None:
    root, _ = dataset
    for doc in read_dataset(root, 1).documents:
        pieces = split_document(doc.doc_id, doc.body)
        assert pieces, doc.doc_id
        assert len({p.chunk_id for p in pieces}) == len(pieces)
        assert all(len(p.heading) + len(p.body) <= CHUNK_CHARS for p in pieces), doc.doc_id
        # Каждая непустая строка документа, кроме названия и заголовков, есть во фрагментах.
        lines = [line for line in doc.body.split("\n") if line.strip() and not line.startswith("#")]
        kept = {line for p in pieces for line in p.body.split("\n")}
        assert all(line in kept for line in lines), doc.doc_id

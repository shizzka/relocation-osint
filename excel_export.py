from pathlib import Path

import xlsxwriter
from xlsxwriter.utility import xl_col_to_name

from core import CRITERIA


def numeric_score(value):
    return type(value) in (int, float) and 0 <= value <= 10


def export_workbook(data, output):
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with xlsxwriter.Workbook(str(output), {"strings_to_formulas": False, "strings_to_urls": False}) as workbook:
        ranking = workbook.add_worksheet("Рейтинг")
        sheet = workbook.add_worksheet("Подробная матрица")
        details = workbook.add_worksheet("Подробный анализ")
        sources = workbook.add_worksheet("Источники")
        header = workbook.add_format({"bold": True, "bg_color": "#D9E1F2", "border": 1})
        weight = workbook.add_format({"bold": True, "bg_color": "#FFF2CC", "border": 1})
        wrap = workbook.add_format({"text_wrap": True, "valign": "top"})
        percent = workbook.add_format({"num_format": "0%"})
        number = workbook.add_format({"num_format": "0.00"})
        ranking_header = workbook.add_format({'bold': True, 'font_color': '#FFFFFF', 'bg_color': '#1F4E78', 'border': 1, 'text_wrap': True, 'valign': 'vcenter'})
        ranking_group = workbook.add_format({'bold': True, 'font_color': '#FFFFFF', 'bg_color': '#5B9BD5', 'border': 1, 'align': 'center'})
        ranking_pending = workbook.add_format({'font_color': '#9C6500', 'bg_color': '#FFEB9C', 'align': 'center', 'text_wrap': True})
        ranking_ready = workbook.add_format({'font_color': '#006100', 'bg_color': '#C6EFCE', 'align': 'center', 'text_wrap': True})
        ranking_center = workbook.add_format({'align': 'center', 'text_wrap': True})
        ranking_score = workbook.add_format({'align': 'center', 'num_format': '0.0'})
        ranking_links = workbook.add_format({'font_color': '#0563C1', 'underline': 1, 'align': 'center'})

        ranking_metadata = ["Направление", "Итог (0–10)", "Статус", "Покрытие", "Уверенность", "Анализ", "Источники"]
        ranking.merge_range(0, 0, 0, len(ranking_metadata) - 1, "Сводный рейтинг направлений", ranking_header)
        ranking.merge_range(0, len(ranking_metadata), 0, len(ranking_metadata) + len(CRITERIA) - 1, "Оценки по критериям (0–10)", ranking_group)
        ranking_headers = ranking_metadata + [f"{criterion_id:02d}\n{name}" for criterion_id, (name, _, _) in CRITERIA.items()]
        ranking.write_row(1, 0, ranking_headers, ranking_header)
        ranking.write(2, 0, "ВЕСА →", weight)
        ranking.write(2, 1, "Меняйте веса справа", weight)
        ranking.merge_range(2, 2, 2, len(ranking_metadata) - 1, "Неполные данные не получают итоговый балл", weight)
        ranking_score_columns = list(range(len(ranking_metadata), len(ranking_metadata) + len(CRITERIA)))
        for column in ranking_score_columns:
            ranking.write_number(2, column, 1, weight)
            ranking.data_validation(2, column, 2, column, {'validate': 'decimal', 'criteria': 'between', 'minimum': 0, 'maximum': 1000, 'error_type': 'stop', 'error_title': 'Некорректный вес', 'error_message': 'Введите число от 0 до 1000', 'ignore_blank': False})
        columns = ["Направление", "ИТОГОВЫЙ БАЛЛ (0–10)", "Покрытие по весам", "Уверенность"]
        score_columns = []
        for criterion_id, (name, scope, _) in CRITERIA.items():
            score_columns.append(len(columns))
            prefix = f"{criterion_id}. {name} [{scope}]"
            columns.extend([prefix + " (Score)", prefix + " (Резюме)"])
        columns.extend(["Ред-флаги", "Подробный анализ", "Источники"])
        sheet.write_row(0, 0, columns, header)
        sheet.write(1, 0, "ВЕСА →", weight)
        for column in score_columns:
            sheet.write_number(1, column, 1, weight)
            sheet.set_column(column, column, 10)
            sheet.set_column(column + 1, column + 1, 40, wrap)
            sheet.data_validation(1, column, 1, column, {"validate": "decimal", "criteria": "between", "minimum": 0, "maximum": 1000, "error_type": "stop", "error_title": "Некорректный вес", "error_message": "Введите число от 0 до 1000", "ignore_blank": False})
        details.write_row(0, 0, ["Направление", "Тип отчёта", "Полный анализ", "Ред-флаги", "Provenance"], header)
        sources.write_row(0, 0, ["Направление", "Критерии", "ID", "Источник", "URL", "Тип", "Source score", "Дата получения", "Дата публикации", "Дата действия", "Материал"], header)
        detail_row = 1
        source_row = 1
        for row_index, (key, report) in enumerate(data.items(), start=2):
            city = report.get("city")
            label = f'{report.get("country", key)} / {city}' if city else key + " (страна; города нет)"
            sheet.write_string(row_index, 0, label, wrap)
            criteria = {}
            for item in report.get("scores", []):
                criterion_id = item.get("id")
                if criterion_id not in CRITERIA or criterion_id in criteria:
                    raise ValueError(f"{key}: неизвестный или повторный критерий {criterion_id}")
                criteria[criterion_id] = item
            known = 0
            total = 0
            ranking_values = {}
            for criterion_id, column in zip(CRITERIA, score_columns):
                item = criteria.get(criterion_id, {})
                value = item.get("score")
                if value is not None and not numeric_score(value):
                    raise ValueError(f"{key}: некорректная оценка {criterion_id}")
                if numeric_score(value):
                    sheet.write_number(row_index, column, value)
                    known += 1
                    total += value
                else:
                    sheet.write_blank(row_index, column, None)
                ranking_values[criterion_id] = value
                summary = item.get("summary") or "Нет данных — не является оценкой 0"
                if item.get("confidence"):
                    summary += "\nУверенность: " + item["confidence"]
                if item.get("manual_check"):
                    summary += "\nНужна ручная проверка"
                indices = item.get("citation_indices", [])
                links = [citation.get("url") for citation in report.get("citations", []) if citation.get("id") in indices and citation.get("url")]
                if links:
                    summary += "\n" + "\n".join(links)
                sheet.write_string(row_index, column + 1, summary[:32767], wrap)
            excel_row = row_index + 1
            letters = [xl_col_to_name(column) for column in score_columns]
            denominator = "+".join(f"{letter}$2" for letter in letters)
            known_weights = "+".join(f"IF(ISNUMBER({letter}{excel_row}),{letter}$2,0)" for letter in letters)
            numerator = "+".join(f"IF(ISNUMBER({letter}{excel_row}),{letter}{excel_row}*{letter}$2,0)" for letter in letters)
            formula = f'=IF(({denominator})=0,"НЕТ ВЕСОВ",IF(({known_weights})<({denominator}),"НЕДОСТАТОЧНО ДАННЫХ",({numerator})/({denominator})))'
            cached_score = total / known if known == len(CRITERIA) else "НЕДОСТАТОЧНО ДАННЫХ"
            sheet.write_formula(row_index, 1, formula, number, cached_score)
            sheet.write_formula(row_index, 2, f'=IF(({denominator})=0,0,({known_weights})/({denominator}))', percent, known / len(CRITERIA))
            sheet.write_string(row_index, 3, report.get("confidence", "не оценена (старый отчёт)"))
            sheet.write_string(row_index, len(columns) - 3, report.get("red_flags", "")[:32767], wrap)

            ranking_row = row_index + 1
            ranking_excel_row = ranking_row + 1
            ranking.write_string(ranking_row, 0, label, wrap)
            for criterion_id, column in zip(CRITERIA, ranking_score_columns):
                value = ranking_values[criterion_id]
                if numeric_score(value):
                    ranking.write_number(ranking_row, column, value, ranking_score)
                else:
                    ranking.write_blank(ranking_row, column, None)
            ranking_letters = [xl_col_to_name(column) for column in ranking_score_columns]
            ranking_weight_sum = "+".join(f"{letter}$3" for letter in ranking_letters)
            ranking_known_weights = "+".join(f"IF(ISNUMBER({letter}{ranking_excel_row}),{letter}$3,0)" for letter in ranking_letters)
            ranking_weighted_scores = "+".join(f"IF(ISNUMBER({letter}{ranking_excel_row}),{letter}{ranking_excel_row}*{letter}$3,0)" for letter in ranking_letters)
            ranking_formula = f'=IF(({ranking_weight_sum})=0,"НЕТ ВЕСОВ",IF(({ranking_known_weights})<({ranking_weight_sum}),"НУЖНЫ ДАННЫЕ",({ranking_weighted_scores})/({ranking_weight_sum})))'
            complete = known == len(CRITERIA)
            ranking_cached = total / known if complete else "НУЖНЫ ДАННЫЕ"
            ranking.write_formula(ranking_row, 1, ranking_formula, ranking_score if complete else ranking_pending, ranking_cached)
            ranking.write_string(ranking_row, 2, "Готово" if complete else f"Не хватает {len(CRITERIA) - known} оценок", ranking_ready if complete else ranking_pending)
            ranking.write_number(ranking_row, 3, known / len(CRITERIA), percent)
            ranking.write_string(ranking_row, 4, report.get("confidence", "не оценена"), ranking_center)
            detail_start = detail_row + 1
            detail_reports = [("Итог", report)]
            if report.get("country_layer"):
                detail_reports.extend([("Country layer", report["country_layer"]), ("City layer", report["city_layer"])])
            for kind, detail_report in detail_reports:
                analysis = detail_report.get("detailed_analysis", "")
                chunks = [analysis[index:index + 32000] for index in range(0, len(analysis), 32000)] or [""]
                for chunk in chunks:
                    provenance = str(detail_report.get("research_provenance") or detail_report.get("provenance") or {"judge": detail_report.get("judge_model"), "models": detail_report.get("sources", []), "independent_retrieval": detail_report.get("independent_retrieval", False)})
                    details.write_row(detail_row, 0, [label, kind, chunk, detail_report.get("red_flags", "")[:32767], provenance[:32767]], wrap)
                    detail_row += 1
            sheet.write_url(row_index, len(columns) - 2, f"internal:'Подробный анализ'!A{detail_start}", string="Открыть полный анализ")
            source_start = source_row + 1
            for citation in report.get("citations", []):
                url = citation.get("url") or ""
                referred_ids = [str(item["id"]) for item in criteria.values() if citation.get("id") in item.get("citation_indices", [])]
                fields = [label, ", ".join(referred_ids) or "Привязка отсутствует", citation.get("id"), citation.get("title") or "", url, citation.get("source_type") or "не проверен", citation.get("source_score"), citation.get("retrieved_at") or report.get("judged_at") or "", citation.get("published_at") or citation.get("date") or "", citation.get("effective_at") or "не установлена", citation.get("content_kind") or "старый поисковый результат"]
                sources.write_row(source_row, 0, fields, wrap)
                if url.startswith(("https://", "http://")):
                    sources.write_url(source_row, 4, url, string=url)
                source_row += 1
            if source_row + 1 > source_start:
                sheet.write_url(row_index, len(columns) - 1, f"internal:'Источники'!A{source_start}", string="Открыть ссылки")
            else:
                sheet.write_string(row_index, len(columns) - 1, "Нет источников")
            ranking.write_url(ranking_row, 5, f"internal:'Подробный анализ'!A{detail_start}", ranking_links, string="Открыть")
            ranking.write_url(ranking_row, 6, f"internal:'Источники'!A{source_start}", ranking_links, string="Открыть")
        ranking.set_column(0, 0, 30, wrap)
        ranking.set_column(1, 1, 14, ranking_center)
        ranking.set_column(2, 2, 18, ranking_center)
        ranking.set_column(3, 4, 12, ranking_center)
        ranking.set_column(5, 6, 12, ranking_center)
        ranking.set_column(len(ranking_metadata), len(ranking_headers) - 1, 9, ranking_score)
        ranking.set_row(0, 24)
        ranking.set_row(1, 40)
        ranking.freeze_panes(3, len(ranking_metadata))
        ranking.autofilter(1, 0, len(data) + 2, len(ranking_headers) - 1)
        ranking.set_zoom(85)
        sheet.set_column(0, 0, 32, wrap)
        sheet.set_column(1, 3, 25)
        sheet.set_column(len(columns) - 3, len(columns) - 1, 40, wrap)
        sheet.freeze_panes(2, 4)
        sheet.autofilter(0, 0, len(data) + 1, len(columns) - 1)
        details.set_column(0, 1, 30, wrap)
        details.set_column(2, 3, 100, wrap)
        details.set_column(4, 4, 60, wrap)
        details.freeze_panes(1, 2)
        details.autofilter(0, 0, detail_row - 1, 4)
        sources.set_column(0, 3, 30, wrap)
        sources.set_column(4, 4, 90, wrap)
        sources.set_column(5, 10, 25, wrap)
        sources.freeze_panes(1, 2)
        sources.autofilter(0, 0, max(source_row - 1, 1), 10)

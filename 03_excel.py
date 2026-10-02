import argparse

from core import ROOT, read_json
from excel_export import export_workbook


def main():
    parser = argparse.ArgumentParser(description="Экспорт Relocation OSINT в Excel")
    parser.add_argument("--input", help="Путь к master.json или master_v2.json")
    parser.add_argument("--output", help="Путь к xlsx")
    args = parser.parse_args()
    source = args.input or ROOT / "data/out/master_v2.json"
    if not args.input and not source.exists():
        source = ROOT / "data/out/master.json"
        print("City-отчётов ещё нет: экспортируем старые country-данные с маркировкой")
    output = args.output or ROOT / "data/out/Relocation_Master.xlsx"
    data = read_json(source)
    if not data:
        print("Нет данных для экспорта")
        return 1
    export_workbook(data, output)
    print(f"Excel готов: {output}. Балл 0..10; веса — во второй строке.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

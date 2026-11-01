"""Fixed-version source reader and independent field-for-field reconciliation."""
import csv
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_EVEN
import hashlib
import json
from pathlib import Path
import xml.etree.ElementTree as ET
from zipfile import ZipFile

SOURCE_SHA256 = '716eaf9ad4a3cf60dc9c9c8a20ed9a917c545a1b5f139f40ed91d2104faa3ff7'


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def row_digest(value):
    return hashlib.sha256(canonical(value).encode('utf-8')).hexdigest()


def workbook_rows(path):
    ns = {'m': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
    with ZipFile(path) as archive:
        workbook = ET.fromstring(archive.read('xl/workbook.xml'))
        properties = workbook.find('m:workbookPr', ns)
        if properties is not None and properties.get('date1904') in ('true', '1'):
            raise ValueError('Unsupported 1904 date system')
        shared = ET.fromstring(archive.read('xl/sharedStrings.xml'))
        strings = [''.join(x.itertext()) for x in shared]
        sheet = ET.fromstring(archive.read('xl/worksheets/sheet1.xml'))
        result = []
        headers = None
        for row in sheet.findall('m:sheetData/m:row', ns):
            cells = {}
            for cell in row:
                col = ''.join(x for x in cell.get('r', '') if x.isalpha())
                element = cell.find('m:v', ns)
                value = element.text if element is not None else ''
                if cell.get('t') == 's' and value:
                    value = strings[int(value)]
                cells[col] = value or ''
            if headers is None:
                headers = cells
            else:
                result.append((int(row.attrib['r']), {name: cells.get(col, '') for col, name in headers.items()}))
        return result


def amount(value):
    if value == '':
        return None
    return int((Decimal(value) * 100).quantize(Decimal(1), rounding=ROUND_HALF_EVEN))


def normalize(row):
    raw = row['Transaction Date']
    if raw.isdigit():
        decoded = datetime(1899, 12, 30) + timedelta(days=int(raw))
        parsed = decoded.replace(month=decoded.day, day=decoded.month)
        transform = 'excel_serial_then_swap_day_month'
    else:
        parsed = datetime.strptime(raw, '%d/%m/%Y')
        transform = 'parse_dd_mm_yyyy_text'
    return dict(source_transaction_number=row['Transaction Number'], transaction_date=parsed.date().isoformat(),
                source_date_raw=raw, date_transform=transform, transaction_type_raw=row['Transaction Type'],
                description_raw=row['Transaction Description'], debit_minor=amount(row['Debit Amount']),
                credit_minor=amount(row['Credit Amount']), balance_minor=amount(row['Balance']),
                category_raw=row['Category'], location_city_raw=row['Location City'],
                location_country_raw=row['Location Country'], currency='')


def reconcile(project_root):
    root = Path(project_root)
    files = {'workbook': root / 'data/external/moneydata/laramee26openBankTransactionData.xlsx',
             'date_comparison': root / 'data/external/moneydata/date_comparison.json',
             'normalized_csv': root / 'data/working/moneydata_v1/transactions.csv',
             'previous_validation': root / 'data/working/moneydata_v1/validation.json'}
    hashes = {key: digest(path) for key, path in files.items()}
    if hashes['workbook'] != SOURCE_SHA256:
        raise ValueError('Unexpected MoneyData source version')
    comparison = json.loads(files['date_comparison'].read_text(encoding='utf-8'))
    previous = json.loads(files['previous_validation'].read_text(encoding='utf-8'))
    if not (comparison['source_sha256'] == SOURCE_SHA256 and comparison['unexplained_date_differences'] == 0
            and comparison['aligned_amount_balance_rows'] == comparison['workbook_rows'] == comparison['reference_rows'] == 6567):
        raise ValueError('Insufficient fixed-version date evidence')
    if previous['csv_sha256'] != hashes['normalized_csv'] or previous['source_comparison_sha256'] != hashes['date_comparison']:
        raise ValueError('Research copy fingerprint mismatch')
    raw = workbook_rows(files['workbook'])
    records = []
    blank_rows = 0
    for source_row, values in raw:
        if not values['Transaction Date']:
            if any(values.values()):
                raise ValueError('Nonempty undated source row')
            blank_rows += 1
            continue
        record = normalize(values)
        records.append((source_row, values, record))
    with files['normalized_csv'].open(encoding='utf-8-sig', newline='') as handle:
        normalized = list(csv.DictReader(handle))
    if len(records) != len(normalized) or len(records) != 6567:
        raise ValueError('Missing or extra rows')
    for (_, _, expected), actual in zip(records, normalized):
        stringified = {k: '' if v is None else str(v) for k, v in expected.items()}
        if actual != stringified:
            raise ValueError('Source/CSV field mismatch: ' + expected['source_transaction_number'])
    transactions = []
    for ordinal, (source_row, raw_row, record) in enumerate(records):
        transactions.append({**record, 'source_row': source_row, 'source_order': ordinal,
                             'source_row_sha256': row_digest(raw_row), 'account_id': None})
    if len({x['source_transaction_number'] for x in transactions}) != 6567:
        raise ValueError('Duplicate source identifiers')
    for newer, older in zip(transactions, transactions[1:]):
        if newer['transaction_date'] < older['transaction_date']:
            raise ValueError('Unexpected source ordering')
        expected = older['balance_minor'] - (newer['debit_minor'] or 0) + (newer['credit_minor'] or 0)
        if expected != newer['balance_minor']:
            raise ValueError('Adjacent balance mismatch')
    source_manifest = {key: {'path': str(path.relative_to(root)).replace('\\', '/'), 'sha256': hashes[key]}
                       for key, path in files.items()}
    return transactions, source_manifest, {'rows_compared': len(records), 'fields_compared': len(normalized[0]),
        'field_mismatches': 0, 'ignored_empty_rows': blank_rows, 'adjacent_pairs_checked': 6566,
        'adjacent_balance_mismatches': 0, 'date_rule': 'Fixed-source Excel serial day/month repair; saved author-reference comparison',
        'reference_url': comparison['reference_url'], 'reference_sha256': comparison['reference_sha256']}

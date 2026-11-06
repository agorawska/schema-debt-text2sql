import re
import sys

TABLE_NAME = r'[`"\[]?([\w$]+)[`"\]]?'
QUALIFIED_NAME = r'(?:[`"\[]?[\w$]+[`"\]]?\.)?' + TABLE_NAME

CREATE_TABLE_RE = re.compile(
    r'create\s+(?:temporary\s+)?table\s+(?:if\s+not\s+exists\s+)?' + QUALIFIED_NAME,
    re.IGNORECASE)
STATEMENT_TABLE_RES = [
    CREATE_TABLE_RE,
    re.compile(r'alter\s+table\s+(?:only\s+)?' + QUALIFIED_NAME, re.IGNORECASE),
    re.compile(r'comment\s+on\s+column\s+' + QUALIFIED_NAME + r'\.', re.IGNORECASE),
    re.compile(r'create\s+(?:unique\s+|fulltext\s+|spatial\s+)?index\s+\S+\s+on\s+'
               + QUALIFIED_NAME, re.IGNORECASE),
    re.compile(r'table structure for table\s+' + TABLE_NAME, re.IGNORECASE),
]

FLAGS = re.IGNORECASE | re.MULTILINE

# SQL string literal: '...', handling \' and ''
SQL_STRING = r"'(?:[^'\\]|\\.|'')*'"


def remove(pattern, content, flags=FLAGS, repl=''):
    """re.sub that keeps the newline characters of the removed fragment.

    This keeps the line numbering of the processed content in sync with
    the input file until empty lines are removed at the very end.
    """
    def _repl(m):
        replacement = m.expand(repl) if repl else ''
        return replacement + '\n' * m.group(0).count('\n')
    return re.sub(pattern, _repl, content, flags=flags)


def map_lines_to_tables(content):
    """Return a list: line index (0-based) -> name of the table the statement refers to."""
    lines_table = [None] * (content.count('\n') + 1)
    pos = 0
    for statement in content.split(';'):
        stripped = statement.lstrip()
        if stripped:
            # the statement starts at its first non-whitespace character
            first = pos + len(statement) - len(stripped)
            start_line = content.count('\n', 0, first)
            end_line = content.count('\n', 0, pos + len(statement))
            table = None
            for rx in STATEMENT_TABLE_RES:
                m = rx.search(statement)
                if m:
                    table = m.group(1)
                    break
            for i in range(start_line, end_line + 1):
                lines_table[i] = table
        pos += len(statement) + 1
    return lines_table


def table_names(content):
    return [m.group(1).lower() for m in CREATE_TABLE_RE.finditer(content)]


def column_count(content):
    """Column count per table (lines in CREATE TABLE that start with a column name)."""
    counts = {}
    for m in re.finditer(r'create\s+table\s+' + QUALIFIED_NAME + r'\s*\((.*?)\n\)',
                         content, flags=re.IGNORECASE | re.DOTALL):
        body = m.group(2)
        cols = [line for line in body.split('\n')
                if re.match(r'\s*[`"]?\w+[`"]?\s+\w', line)
                and not re.match(r'\s*(primary|foreign|constraint|unique|key|index|'
                                 r'fulltext|spatial|check)\b', line, re.IGNORECASE)]
        counts[m.group(1).lower()] = len(cols)
    return counts


def _cols(text):
    return tuple(c.strip().strip('`"').lower() for c in text.split(','))


def required_indexes(content):
    """(table, columns) referenced by an FK and not covered by a prefix of the table's PK.

    InnoDB requires an index on referenced columns, so such a UNIQUE/KEY
    must be kept for the DDL to remain executable.
    """
    pks = {}
    for m in re.finditer(r'create\s+table\s+' + QUALIFIED_NAME + r'\s*\((.*?)\n\)',
                         content, flags=re.IGNORECASE | re.DOTALL):
        pk = re.search(r'primary\s+key\s*\(([^)]*)\)', m.group(2), re.IGNORECASE)
        pks[m.group(1).lower()] = _cols(pk.group(1)) if pk else ()
    required = set()
    for m in re.finditer(r'references\s+' + QUALIFIED_NAME + r'\s*\(([^)]*)\)',
                         content, flags=re.IGNORECASE):
        table, cols = m.group(1).lower(), _cols(m.group(2))
        if pks.get(table, ())[:len(cols)] != cols:
            required.add((table, cols))
    return required


def remove_unless_required(pattern, content, required):
    """Like remove(), but keeps an index if an FK requires it."""
    lines_table = map_lines_to_tables(content)

    def _repl(m):
        line_no = content.count('\n', 0, m.start())
        table = (lines_table[line_no] or '').lower()
        cols = re.search(r'\(([^)]*)\)', m.group(0))
        if cols and (table, _cols(cols.group(1))) in required:
            return m.group(0)
        return '\n' * m.group(0).count('\n')
    return re.sub(pattern, _repl, content, flags=FLAGS)


def simplify_ddl(input_file, output_file, removed_file):
    with open(input_file, 'r', encoding='utf-8') as f:
        original = f.read()

    content = original

    # 0. Remove dump noise: comments and MySQL conditional statements (/*!40101 SET ... */;)
    content = remove(r'^[ \t]*--.*$', content)
    content = remove(r'/\*!\d+.*?\*/;?', content, flags=FLAGS | re.DOTALL)
    content = remove(r'/\*.*?\*/', content, flags=FLAGS | re.DOTALL)

    # 1. Remove database name
    content = re.sub(r'(?i)\b(table|references|on)\s+[`"]?[a-zA-Z0-9_]+[`"]?\.', r'\1 ', content)

    # 2. Remove indexes other than PK and FK (also UNIQUE)
    #    a) standalone: CREATE [UNIQUE|FULLTEXT|SPATIAL] INDEX ... ;
    content = remove(r'create(?:\s+unique)?(?:\s+fulltext)?(?:\s+spatial)?\s+index[^;]+;',
                     content, flags=FLAGS | re.DOTALL)
    #    b) inline in CREATE TABLE: [UNIQUE|FULLTEXT|SPATIAL] KEY/INDEX ..., UNIQUE (...)
    #       (except indexes required by FKs - the DDL would not execute without them)
    required = required_indexes(original)
    content = remove_unless_required(
        r'^[ \t]*(?:(?:unique|fulltext|spatial)[ \t]+)?(?:key|index)\b.*$', content, required)
    content = remove_unless_required(
        r'^[ \t]*(?:constraint[ \t]+\S+[ \t]+)?unique\b.*$', content, required)
    #    c) column-level UNIQUE
    content = remove(r'[ \t]+unique(?:[ \t]+key)?\b(?=[ \t]*(?:,|$|not\b|null\b|primary\b|'
                     r'references\b|check\b))', content)

    # 3. Remove table options (ENGINE, AUTO_INCREMENT=, CHARSET, COLLATE, ROW_FORMAT, COMMENT=...)
    content = remove(r'(?<=\))[ \t]*engine\s*=[^;]*', content)
    content = remove(r'\s*(?:default\s+)?(?:collate|charset|character\s+set)\s*=\s*\w+', content)

    # 4. Remove unnecessary noise (column attributes that do not change the data)
    noise_patterns = [
        r'[ \t]+auto_increment\b',
        r'[ \t]+GENERATED\s+(?:ALWAYS|BY\s+DEFAULT)\s+AS\s+IDENTITY',
        r'[ \t]+unsigned\b',
        r'[ \t]+zerofill\b',
        r'[ \t]+default\s+(?:' + SQL_STRING + r'|-?\d+(?:\.\d+)?|b\'[01]*\'|true|false|null'
        r'|CURRENT_TIMESTAMP(?:\(\d*\))?|now\(\))',
        r'[ \t]+on\s+update\s+CURRENT_TIMESTAMP(?:\(\d*\))?',
        r'[ \t]+character\s+set\s+\w+',  # inline column charset
        r'[ \t]+collate\s+\w+',          # inline column collate
        r'[ \t]+comment\s+' + SQL_STRING,
        r'[ \t]+using\s+(?:btree|hash)\b',
    ]
    for pattern in noise_patterns:
        content = remove(pattern, content)

    # 5. Generated columns: keep the column and its type, drop the expression.
    #    Single-line only (no DOTALL!) - a greedy multi-line match swallowed whole tables.
    content = remove(r'[ \t]+(?:generated\s+always\s+)?as\s+\(.*\)(?:[ \t]+(?:stored|virtual|persistent))?'
                     r'(?=[ \t]*,?[ \t]*$)', content)

    # 6. Foreign keys: drop constraint name and referential actions, keep the relationship
    content = remove(r'\bconstraint[ \t]+\S+[ \t]+(?=foreign\s+key)', content)
    content = remove(r'(?:[ \t]+on\s+(?:delete|update)\s+'
                     r'(?:cascade|restrict|no\s+action|set\s+null|set\s+default))+', content)

    # 7. Display width of integers (int(11) -> int). tinyint(1) stays - it means boolean.
    content = re.sub(r'\b(int|integer|bigint|smallint|mediumint)\s*\(\d+\)', r'\1', content,
                     flags=re.IGNORECASE)
    content = re.sub(r'\btinyint\s*\((?!1\))\d+\)', 'tinyint', content, flags=re.IGNORECASE)

    # 8. Clean up commas left by removed lines
    content = remove(r',\s*,', content, repl=',')
    content = remove(r',(?=\s*\)\s*;)', content)

    # --- Dump removed lines ---
    original_lines = original.split('\n')
    processed_lines = content.split('\n')
    assert len(original_lines) == len(processed_lines), 'Line mapping lost'

    lines_table = map_lines_to_tables(original)
    removed = [
        (lines_table[i] or '-', i + 1, orig)
        for i, (orig, new) in enumerate(zip(original_lines, processed_lines))
        if orig.strip() and not new.strip()
    ]
    with open(removed_file, 'w', encoding='utf-8') as f:
        for table, line_no, text in removed:
            f.write(f'table: {table}\n'
                    f'line in input file: {line_no}\n'
                    f'{text}\n\n')
    print(f'Removed {len(removed)} lines -> {removed_file}')

    # --- Write result ---
    content = '\n'.join(line.rstrip() for line in content.split('\n'))
    content = re.sub(r'\n\s*\n+', '\n', content)
    result = content.strip() + '\n'
    if re.search(r'foreign_key_checks\s*=\s*0', original, re.IGNORECASE):
        # tables are in alphabetical order, FKs point to tables created later
        result = 'SET FOREIGN_KEY_CHECKS=0;\n' + result
    with open(output_file, 'w', encoding='utf-8') as f:
        f.write(result)
    print(f'Size: {len(original)} -> {len(result)} characters '
          f'({100 * len(result) / len(original):.1f}%)')

    # --- Check table (and column) counts ---
    ok = True
    before = table_names(original)
    after = table_names(result)
    print(f'Table count: before = {len(before)}, after = {len(after)}')
    if len(before) != len(after) or set(before) != set(after):
        ok = False
        missing = sorted(set(before) - set(after))
        extra = sorted(set(after) - set(before))
        print('ERROR: table count/set differs after reduction!', file=sys.stderr)
        if missing:
            print(f'  missing: {", ".join(missing)}', file=sys.stderr)
        if extra:
            print(f'  extra: {", ".join(extra)}', file=sys.stderr)

    cols_before = column_count(original)
    cols_after = column_count(result)
    diff = {t: (n, cols_after.get(t)) for t, n in cols_before.items() if cols_after.get(t) != n}
    if diff:
        ok = False
        print('ERROR: column count differs:', file=sys.stderr)
        for t, (b, a) in sorted(diff.items()):
            print(f'  {t}: {b} -> {a}', file=sys.stderr)

    if not ok:
        sys.exit(1)
    print(f'OK: table and column counts match '
          f'({sum(cols_after.values())} columns).')
    print('Done.')


if __name__ == '__main__':
    args = sys.argv[1:]
    simplify_ddl(args[0] if len(args) > 0 else 'dump.sql',
                 args[1] if len(args) > 1 else 'simplified_dump.sql',
                 args[2] if len(args) > 2 else 'removed_lines.txt')

"""Checks the texts that a customer or an installer of the box reads.

Two rules. First, a text of the box page takes the same {placeholders} in English and in
Vietnamese, so that a translation never shows a hole or a stray "{name}". Second, no text
names the vendor of core in any way other than the exact words "Odoo Community", the name of
the customer's own server kind. The bare vendor name in any case, its paid edition, its web
site and a series number such as 18.0 are all findings.

The allowed words are narrow. "Odoo Community" passes only where it names a kind of server:
at the end of the text, before one of the punctuation marks . , / ), or before a word that starts
with a lower-case letter of any script, as in "Odoo Community server". Anything else after it is a finding:
a further capitalised word ("Odoo Community Edition", "Odoo Community IoT Box"), a possessive
("Odoo Community's IoT Box", with either apostrophe), a hyphen ("Odoo Community-Edition") or a
bracket ("Odoo Community (Edition)"), as these name a product and not a kind of server. A
pattern cannot judge meaning, so a text that merely sits close to these shapes is for a reader
to judge.

Run as python3 -I box/build/check_page_texts.py [ROOT]. ROOT is the directory the paths below
are read from; it defaults to the repository. Each finding is printed to stderr with its file,
its key or line and the offending word, and the exit status is 1 when there is any.
"""
import json
import re
import sys
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[2]
I18N_DIRECTORY = Path('viin_hw_homepage', 'static', 'src', 'app', 'i18n')
JSON_TEXTS = [I18N_DIRECTORY / 'en.json', I18N_DIRECTORY / 'vi.json']
HTML_TEXTS = [
    Path('viin_hw_homepage', 'views', 'index.html'),
    Path('viin_hw_homepage', 'views', 'logs.html'),
    Path('box', 'system', 'usr', 'share', 'viindoo-iot', 'box-down.html'),
    Path('viin_hw_drivers', 'views', 'pos_display.html'),
]
PLACEHOLDER = re.compile(r'\{[A-Za-z_][A-Za-z0-9_]*\}')
# "Odoo Community" is matched first, case-sensitively, so that it is consumed and never reported
# as a whole. It is allowed only where it names a kind of server: at the end of the text, before
# one of the punctuation marks . , / ), or before a word that starts with a lower-case letter of
# any script (str.islower, checked in vendor_words). Anything else after it (a possessive such as
# 's, a hyphen, a bracket, a digit, a capitalised word in any script) is reported as "Odoo".
VENDOR_WORD = re.compile(
    r'(?P<kind>(?-i:Odoo Community)(?=\s*$|[.,/)]|\s+\S))'
    r'|(?P<word>odoo\.com|odoo|enterprise|(?<![\d.])\d{2}\.0(?!\d))',
    re.IGNORECASE,
)


def placeholders(text):
    return sorted(PLACEHOLDER.findall(text))


def differences(english, vietnamese):
    return [
        f'{key}: en {placeholders(english[key])}, vi {placeholders(vietnamese[key])}'
        for key in sorted(english.keys() & vietnamese.keys())
        if placeholders(english[key]) != placeholders(vietnamese[key])
    ]


def vendor_words(text):
    """Return the vendor words in text, in order of appearance; an empty list when there are none."""
    words = []
    for match in VENDOR_WORD.finditer(text):
        if match.group('word'):
            words.append(match.group('word'))
        elif match.group('kind'):
            following = text[match.end():].lstrip()
            if following and following[0] not in '.,/)' and not following[0].islower():
                words.append('Odoo')
    return words


def main(argv):
    root = Path(argv[1]) if len(argv) > 1 else REPOSITORY
    texts = {
        language: json.loads(Path(root, I18N_DIRECTORY, f'{language}.json').read_text(encoding='utf-8'))
        for language in ('en', 'vi')
    }
    found = differences(texts['en'], texts['vi'])
    for line in found:
        print(f'placeholders differ in {line}', file=sys.stderr)
    print(f'{len(texts["en"].keys() & texts["vi"].keys())} keys compared, {len(found)} differ')

    vendor_findings = []
    for path in JSON_TEXTS:
        values = json.loads(Path(root, path).read_text(encoding='utf-8'))
        for key, value in values.items():
            vendor_findings += [
                f'{path.as_posix()} key "{key}": vendor word "{word}"' for word in vendor_words(value)
            ]
    for path in HTML_TEXTS:
        lines = Path(root, path).read_text(encoding='utf-8').splitlines()
        for number, line in enumerate(lines, start=1):
            vendor_findings += [
                f'{path.as_posix()} line {number}: vendor word "{word}"' for word in vendor_words(line)
            ]
    for line in vendor_findings:
        print(line, file=sys.stderr)
    print(f'{len(JSON_TEXTS) + len(HTML_TEXTS)} files checked for vendor words, {len(vendor_findings)} findings')
    return 1 if found or vendor_findings else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))

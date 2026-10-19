"""Checks the texts that a customer or an installer of the box reads.

Three rules. First, every language file of the box page (en.json, vi.json and any other .json
next to them) holds the same keys, so that a text is never missing in one language; a key found
in some files and not in another is a finding that names the key and the file it is missing from.
Second, a text of the box page takes the same {placeholders} in English and in
Vietnamese, so that a translation never shows a hole or a stray "{name}". Third, no text
names the vendor of core: the vendor name in any case and in any phrase (a kind of server
included, as the box is a Viindoo product and names Viindoo servers only), its paid edition,
its web site and a series number such as 18.0 are all findings. A pattern cannot judge meaning,
so a text that merely contains one of these words, such as "an enterprise network", is a
finding too and is reworded.

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
HTML_TEXTS = [
    Path('viin_hw_homepage', 'views', 'index.html'),
    Path('viin_hw_homepage', 'views', 'logs.html'),
    Path('box', 'system', 'usr', 'share', 'viindoo-iot', 'box-down.html'),
    Path('viin_hw_drivers', 'views', 'pos_display.html'),
]
PLACEHOLDER = re.compile(r'\{[A-Za-z_][A-Za-z0-9_]*\}')
VENDOR_WORD = re.compile(r'odoo\.com|odoo|enterprise|(?<![\d.])\d{2}\.0(?!\d)', re.IGNORECASE)


def placeholders(text):
    return sorted(PLACEHOLDER.findall(text))


def differences(english, vietnamese):
    return [
        f'{key}: en {placeholders(english[key])}, vi {placeholders(vietnamese[key])}'
        for key in sorted(english.keys() & vietnamese.keys())
        if placeholders(english[key]) != placeholders(vietnamese[key])
    ]


def missing_keys(texts):
    """Return one line per key that some language file holds and another lacks; texts maps a file name to its keys."""
    every_key = set().union(*texts.values()) if texts else set()
    return [
        f'{I18N_DIRECTORY.as_posix()}/{name} lacks key "{key}"'
        for key in sorted(every_key)
        for name in sorted(texts)
        if key not in texts[name]
    ]


def vendor_words(text):
    """Return the vendor words in text, in order of appearance; an empty list when there are none."""
    return VENDOR_WORD.findall(text)


def main(argv):
    root = Path(argv[1]) if len(argv) > 1 else REPOSITORY
    files = sorted(Path(root, I18N_DIRECTORY).glob('*.json'))
    texts = {file.name: json.loads(file.read_text(encoding='utf-8')) for file in files}
    missing = missing_keys(texts)
    for line in missing:
        print(f'key missing in {line}', file=sys.stderr)
    print(f'{len(texts)} language files compared, {len(missing)} keys missing')

    found = differences(texts['en.json'], texts['vi.json'])
    for line in found:
        print(f'placeholders differ in {line}', file=sys.stderr)
    print(f'{len(texts["en.json"].keys() & texts["vi.json"].keys())} keys compared, {len(found)} differ')

    vendor_findings = []
    for file in files:
        for key, value in texts[file.name].items():
            vendor_findings += [
                f'{(I18N_DIRECTORY / file.name).as_posix()} key "{key}": vendor word "{word}"'
                for word in vendor_words(value)
            ]
    for path in HTML_TEXTS:
        lines = Path(root, path).read_text(encoding='utf-8').splitlines()
        for number, line in enumerate(lines, start=1):
            vendor_findings += [
                f'{path.as_posix()} line {number}: vendor word "{word}"' for word in vendor_words(line)
            ]
    for line in vendor_findings:
        print(line, file=sys.stderr)
    print(f'{len(files) + len(HTML_TEXTS)} files checked for vendor words, {len(vendor_findings)} findings')
    return 1 if missing or found or vendor_findings else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))

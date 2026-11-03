"""Normalize visually identical repeated-letter identifiers without intent routing."""
import re
import unicodedata


def normalize_identifiers(query):
    query=unicodedata.normalize('NFKC',query)
    confusables=str.maketrans('АВСЕНКМОРТХУ','ABCEHKMOPTXY')
    def token(match):
        original=match.group(0);label=original.upper().translate(confusables)
        if 2<=len(label)<=12 and label.isascii() and label.isalpha() and len(set(label))==1:return label
        return original
    return re.sub(r'(?<!\w)[A-Za-zАаВвСсЕеНнКкМмОоРрТтХхУу]{2,12}(?!\w)',token,query)

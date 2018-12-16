# encoding: utf8

"""
Normalizes LEGI data stored in an SQLite DB
"""

from __future__ import division, print_function, unicode_literals

from argparse import ArgumentParser
from functools import reduce
import json
import re

from .articles import (
    article_num, article_num_extra, article_num_multi, article_num_multi_sub,
    article_titre, legifrance_url_article,
)
from .html import bad_space_re, drop_bad_space, split_first_paragraph
from .roman import ROMAN_PATTERN as roman_num
from .spelling import spellcheck
from .titles import NATURE_MAP_R_SD, gen_titre, normalize_title, parse_titre
from .utils import (
    ascii_spaces_re, connect_db, filter_nonalnum, mimic_case, nonword_re,
    show_match, strip_down, strip_prefix, upper_words_percentage,
)


def normalize_article_numbers(db, dry_run=False, log_path=None):
    print("> Normalisation des numéros des articles...")

    article_num_multi_re = re.compile(article_num_multi)
    article_num_multi_sub_re = re.compile(article_num_multi_sub)
    article_titre_partial_re = re.compile(r"^%s(?!\w)" % article_titre, re.U)

    quotes_re = re.compile(r'(^| )"([^"]+)"(?=\W|$)', re.U)

    def replace_quotes(m):
        return '%s« %s »' % (m.group(1), m.group(2).strip())

    space_around_dash_re = re.compile((
        r"(?:[0-9]+|%(roman_num)s)(?:- | -)(?:[0-9]+|%(roman_num)s\b)"
    ) % dict(roman_num=roman_num))

    extraneous_dash_re = re.compile(r" -(%(article_num)s)" % globals())

    upper_word_re = re.compile((
        r"\b(?!AOC |FRA\. |%s(?:[ ,;:.)-]|$))(?:À|(?:[DL]')?[A-ZÀÂÇÈÉÊËÎÔÛÜ]{2,})\b"
    ) % roman_num, re.U)

    def lower(m):
        return m.group(0).lower()

    WORD_CORRECTIONS = {
        'equivalence': 'équivalence',
        'etat': 'état',
        'execution': 'exécution',
        'metier': 'métier',
        'preambule': 'préambule',
        'referentiel': 'référentiel',
    }
    word_correction_re = re.compile(r"\b(%s)(s?)\b" % '|'.join(WORD_CORRECTIONS), re.I)

    def word_corrector(m):
        word = m.group(1)
        correct_word = WORD_CORRECTIONS.get(word.lower())
        return mimic_case(word, correct_word) + m.group(2)

    missing_space_re = re.compile(r"\b(%s)(\((?:[A-Z]{2}|suite)\))" % roman_num)

    def add_missing_space(m):
        return '%s %s' % m.groups()

    TITLE_REPLACEMENTS = {
        'LEGIARTI000006326743': "",
        'LEGIARTI000006804495': "Annexe 1 à l'article R513-7",
        'LEGIARTI000006804497': "Annexe 2 à l'article R513-7",
        'LEGIARTI000006831539': 'Annexe IV bis',
        'LEGIARTI000006893436': "Annexe II",
        'LEGIARTI000006934477': "Articles 3 bis, 4 à 16, 16 bis à 16 sexies",
        'LEGIARTI000006934489': "Annexes II à X",
        'LEGIARTI000006570272':
            "Annexe I, Tableau d'équivalence des classes et échelons de sous-préfet et d'administrateur civil",
        'LEGIARTI000019895551':
            "Annexe II, Tableau relatif à l'avancement d'échelon des sous-préfets",
        'LEGIARTI000019151586': "Annexe II : Habitats humides",
        'LEGIARTI000021189469': "Annexe 1 : AOC «\xa0Moulis\xa0»",
        'LEGIARTI000023411984': "Annexe I : Hépatite B",
        'LEGIARTI000023411988': "Annexe II : Hépatite C",
    }

    case_normalization_re = re.compile((
        r"\b("
        r"annex[eé]s?|art(?:icle)?|tableaux?|états?|introduction|préambule|addendum|"
        r"appendice|informative|législatifs?|extraits|directive|technique"
        r")\b"
    ), re.I)

    def normalize_case(m):
        word = m.group(1)
        if m.start(1) == 0:
            return word.title()
        else:
            return word.lower() if word.isupper() else word

    article_num_extra_re = re.compile(article_num_extra, re.I)

    annexe_num_double_re = re.compile((
        r"Annexe à l'article (%(article_num)s(?: \([^)]+\))?) Annexe (%(article_num)s)"
    ) % dict(article_num=article_num))

    special_case_re = re.compile((
        r"(AOC|FRA\.)( (?:« )?[A-ZÀÂÇÈÉÊËÎÔÛÜ-]{2,}(?: [A-ZÀÂÇÈÉÊËÎÔÛÜ-]{2,})*)"
    ), re.U)

    def special_case_sub(m):
        return m.group(1) + m.group(2).title()

    annexe_suffix_re = re.compile((
        r"^(?P<art_num>(?=[LDRA])%(article_num)s),? [Aa]nnexe(?: (?P<annexe_num>%(roman_num)s|[0-9]+))?$"
    ) % globals())

    article_position_re = re.compile((
        r"(?:ANNEXE(?: %(article_num)s)?,? )?"
        r"(?:\(?(?:"
            r"(?:CHAPITRE|PAR(?:\.|AGRAPHE)|TITRE|PARTIE)(?: %(article_num)s)?|"
            r"(?:PREMIERE|DEUXIEME|TROISIEME) PARTIE"
        r")\)?(?:,? |$))+"
        r"(?:(?P<article>ART(?:\.|ICLE) )?(?P<num>[0-9]+)\.?|(?P<intro>INTRODUCTION))?"
    ) % dict(article_num=article_num))

    standard_num_with_garbage_re = re.compile(
        r"\b(LO|[RD]\*{1,2}|[LDRA])(?:\. ?|\.? )([0-9]{3,})\b"
    )
    def drop_garbage(m):
        return m.group(1) + m.group(2)

    range_re = re.compile(r"^\([0-9]+ à [0-9]+\)")

    counts = {}
    def count(k, n=1):
        try:
            counts[k] += n
        except KeyError:
            counts[k] = n

    changes = {}
    def add_change(k):
        try:
            changes[k] += 1
        except KeyError:
            changes[k] = 1
        if dry_run:
            return
        update_article({'num': k[1]})

    def update_article(data):
        if dry_run:
            return
        db.update('articles', {'id': article_id}, data)

    q = db.all("""
        SELECT id, cid, num
          FROM articles
         WHERE length(num) > 0
    """)
    for article_id, cid, orig_num in q:
        num = orig_num
        if ascii_spaces_re.search(num):
            num = ascii_spaces_re.sub(' ', num)
        if '*suite*' in num:
            num = num.replace('*suite*', '(suite)')  # exemple: LEGIARTI000006668354
        if num and num[0] == '*' and num[-1] == '*':
            num = num.strip('*')
        if 'à Lot-et-G.' in num:
            num = num.replace('à Lot-et-G.', 'à Lot-et-Garonne')
        num = num.strip(' .:')
        if not num:
            count('empty')
            add_change((orig_num, num))
            continue

        if " à L'article " in num:
            num = num.replace(" à L'article ", " à l'article ")
        if '–' in num:
            num = num.replace('–', '-')
        if ',,' in num:
            num = num.replace(',,', ',')
        if space_around_dash_re.search(num):
            num = space_around_dash_re.sub(drop_bad_space, num)
        if extraneous_dash_re.search(num):
            # exemple: "ANNEXE -IV" (LEGIARTI000006535355)
            num = extraneous_dash_re.sub(r' \1', num)
        if num.startswith('AOC ') and '..' in num:
            # exemple: 'AOC " Côtes du Roussillon .."' (LEGIARTI000021231010)
            num = num.replace('..', '')
        if bad_space_re.search(num):
            num = bad_space_re.sub(drop_bad_space, num)
        if quotes_re.search(num):
            num = quotes_re.sub(replace_quotes, num)
        if standard_num_with_garbage_re.search(num):
            num = standard_num_with_garbage_re.sub(drop_garbage, num)
        if num[1:3] == '.-':
            # exemple: LEGIARTI000036496662
            num = num.replace('.-', '-')
        if num != orig_num:
            count('removed or replaced bad character(s)')

        if article_id in TITLE_REPLACEMENTS:
            num = TITLE_REPLACEMENTS[article_id]
            count('replaced num (hardcoded)')
            add_change((orig_num, num))
            continue
        elif num == '(suite Ib)':  # LEGIARTI000030127261
            num = 'Ib (suite)'
            count('corrected (suite)')
        elif cid == 'LEGITEXT000006074493' and num.endswith(' STATUT ANNEXE'):
            num = num.replace(' STATUT ANNEXE', ' du statut annexe')
            count('corrected (statut annexe)')
            add_change((orig_num, num))
            continue
        elif cid == 'JORFTEXT000020692049':
            if range_re.match(num):
                num = "Tableau annexe - départements %s" % num[1:-1]
                count('replaced split article num (hardcoded)')
                add_change((orig_num, num))
                continue
        elif cid == 'JORFTEXT000027513723' and num == 'Annexe IIII':
            num = 'Annexe III'
            count('fixed annexe num (hardcoded)')
            add_change((orig_num, num))
            continue
        elif cid == 'JORFTEXT000000325199' and num.endswith(', annexe'):
            num = num[:-8]
            count('removed suffix \'annexe\' (hardcoded)')
            add_change((orig_num, num))
            continue
        elif cid == 'JORFTEXT000000735207' and num == 'annexe ii':
            num = 'Annexe II'
            count('uppercased roman number (hardcoded)')
            add_change((orig_num, num))
            continue

        first_word = num[:num.find(' ')]
        if first_word.lower() == 'article':
            num = num[8:]
            count("dropped first word 'article'")

        if 'ANNEXE' in num:
            if num == 'ANNEXE TABLEAU':
                num = 'Tableau annexe'
                count('lowercased, and reversed word order')
                add_change((orig_num, num))
                continue
            num = num.replace("ANNEXE A L'ARTICLE", "Annexe à l'article")
            num = num.replace(" ET ANNEXE", " et annexe")
            num = num.replace("ANNEXE N°", "Annexe n°")
            num = num.replace("ANNEXE(s)", "Annexes")
            num = num.replace("ANNEXES( 1)", "Annexes (1)")

        position_match = article_position_re.match(num)
        if position_match:
            if len(position_match.group(0)) != len(num):
                print("Warning: capture partielle (article_position_re): %r" % show_match(position_match))
            # texte mal découpé, exemple: JORFTEXT000000316939
            if position_match.group('article'):
                num = position_match.group('num')
                assert num
            elif position_match.group('intro'):
                num = 'Introduction'
            else:
                num = ''
            count('dropped position')
            add_change((orig_num, num))
            continue

        if missing_space_re.search(num):
            num = missing_space_re.sub(add_missing_space, num)
            count('added missing space')

        if word_correction_re.search(num):
            num = word_correction_re.sub(word_corrector, num)
            count('added missing accent(s)')

        if special_case_re.search(num):
            num = special_case_re.sub(special_case_sub, num)
            count('titlecased')
            add_change((orig_num, num))
            continue
        else:
            is_title = (
                num[:4] in ('AOC ', 'FRA.', 'CA d', 'TPI ') or
                num == 'CA Aix-en-Provence' or
                num.startswith('Annexe AOC ')
            )
            if is_title:
                count('skipped detected title')
                if num != orig_num:
                    add_change((orig_num, num))
                continue

        if article_num_extra_re.search(num):
            num = article_num_extra_re.sub(lower, num)
            count('lowercased (extra)')

        if case_normalization_re.search(num):
            num2 = case_normalization_re.sub(normalize_case, num)
            if article_titre_partial_re.match(num2) or not upper_word_re.search(num2):
                num = num2
                count('lowercased (simple)')
            del num2

        if cid == 'LEGITEXT000006074201' and num.lower().startswith('annexe 22, '):
            num = "%s de l'annexe 22" % num[len('ANNEXE 22, '):]
            count('moved prefix \'annexe\' to suffix (hardcoded)')

        annexe_suffix_match = annexe_suffix_re.match(num)
        if annexe_suffix_match:
            matches = annexe_suffix_match.groupdict()
            if matches['annexe_num']:
                num = "Annexe %(annexe_num)s à l'article %(art_num)s" % matches
            else:
                num = "Annexe à l'article %(art_num)s" % matches
            del matches
            count('moved suffix \'annexe\' to prefix')
            add_change((orig_num, num))
            continue

        multi_num_match = article_num_multi_re.match(num)
        if multi_num_match:
            base_num_match = article_num_multi_sub_re.match(num)
            if base_num_match:
                num = "%s (%s)" % (base_num_match.group(1), base_num_match.group(2))
                count('split base number and aliases')
                add_change((orig_num, num))
                continue
            if len(multi_num_match.group(0)) != len(num):
                url = legifrance_url_article(article_id, cid)
                print("Warning: capture partielle de multiples numéros: %r   %s" %
                      (show_match(multi_num_match), url))
            count('detected a multi-article')
            continue

        if annexe_num_double_re.search(num):
            num = annexe_num_double_re.sub(r"Annexe \2 à l'article \1", num)
            count('collapsed double number')
            add_change((orig_num, num))
            continue

        m = article_titre_partial_re.match(num)
        if m:
            is_full_match = len(m.group(0)) == len(num)
            if not is_full_match:
                offset = m.end(0)
                part1, part2 = num[:offset], num[offset:]
                is_full_match = (
                    part2[:3] == ' : ' or
                    part2.startswith(' relative ') or
                    part2.startswith(' relatif ')
                )
                if is_full_match:
                    if upper_word_re.search(part2):
                        if spellcheck(part2):
                            num = part1 + upper_word_re.sub(lower, part2)
                            count('lowercased subtitle (spellcheck)')
                        else:
                            count('still uppercase')
                            url = legifrance_url_article(article_id, cid)
                            print("Warning: still uppercase:", repr(num), ' ', url)
                elif part2.startswith(' aux articles '):
                    # titre tronqué, on essaye de le compléter en extrayant le
                    # premier paragraphe du contenu de l'article
                    html = db.one(
                        "SELECT bloc_textuel FROM articles WHERE id = ?", (article_id,)
                    )
                    paragraph, rest = split_first_paragraph(html)
                    paragraph = paragraph.replace('\n', ' ')
                    m3 = article_titre_partial_re.match(paragraph)
                    if m3 and paragraph.startswith(orig_num) and len(m3.group(0)) == len(paragraph):
                        num = standard_num_with_garbage_re.sub(drop_garbage, paragraph)
                        add_change((orig_num, num))
                        assert rest
                        update_article({'bloc_textuel': rest})
                        count('completed truncated title, and removed it from bloc_textuel')
                        continue
                    url = legifrance_url_article(article_id, cid)
                    if m3:
                        print("Warning: échec de la récupération du titre: %r   %s" % (show_match(m3), url))
                    else:
                        print("Warning: échec de la récupération du titre: %r   %s" % (paragraph, url))
            if is_full_match:
                count('matched article_titre regexp')
                if num != orig_num:
                    add_change((orig_num, num))
                continue
            else:
                url = legifrance_url_article(article_id, cid)
                print("Warning: capture partielle du numéro: %r   %s" % (show_match(m), url))

        if upper_word_re.search(num):
            if spellcheck(num):
                num = upper_word_re.sub(lower, num)
                num = num[0].upper() + num[1:]
                count('lowercased (spellcheck)')
            else:
                count('still uppercase')
                url = legifrance_url_article(article_id, cid)
                print("Warning: still uppercase:", repr(num), ' ', url)

        if num != orig_num:
            add_change((orig_num, num))

    if log_path:
        with open(log_path, 'w') as log:
            for change, count in sorted(changes.items()):
                if count == 1:
                    log.write('%r => %r\n' % change)
                else:
                    log.write('%r => %r (%i×)\n' % (change[0], change[1], count))

    print('Done. Result: ' + json.dumps(counts, indent=4, sort_keys=True))


def normalize_text_titles(db, dry_run=False):
    print("> Normalisation des titres des textes...")

    TEXTES_VERSIONS_BRUTES_BITS = {
        'nature': 1,
        'titre': 2,
        'titrefull': 4,
        'autorite': 8,
        'num': 16,
        'date_texte': 32,
    }

    update_counts = {}
    def count_update(k):
        try:
            update_counts[k] += 1
        except KeyError:
            update_counts[k] = 1

    updates = {}
    orig_values = {}
    q = db.all("""
        SELECT id, titre, titrefull, titrefull_s, nature, num, date_texte, autorite
          FROM textes_versions
    """)
    for row in q:
        text_id, titre_o, titrefull_o, titrefull_s_o, nature_o, num, date_texte, autorite = row
        titre, titrefull, nature = titre_o, titrefull_o, nature_o
        len_titre = len(titre)
        if len(titrefull) > len_titre:
            if titrefull[len_titre:][:1] != ' ' and titrefull[:len_titre] == titre:
                # Add missing space
                titrefull = titre + ' ' + titrefull[len_titre:]
        titre, titrefull = normalize_title(titre), normalize_title(titrefull)
        len_titre = len(titre)
        if titrefull[:len_titre] != titre:
            if len_titre > len(titrefull):
                titrefull = titre
            elif nonword_re.sub('', titrefull) == nonword_re.sub('', titre):
                titre = titrefull
                len_titre = len(titre)
            elif strip_down(titre) == strip_down(titrefull[:len_titre]):
                has_upper_1 = upper_words_percentage(titre) > 0
                has_upper_2 = upper_words_percentage(titrefull[:len_titre]) > 0
                if has_upper_1 ^ has_upper_2:
                    if has_upper_1:
                        titre = titrefull[:len_titre]
                    else:
                        titrefull = titre + titrefull[len_titre:]
                elif not (has_upper_1 or has_upper_2):
                    n_upper_1 = len([c for c in titre if c.isupper()])
                    n_upper_2 = len([c for c in titrefull if c.isupper()])
                    if n_upper_1 > n_upper_2:
                        titrefull = titre + titrefull[len_titre:]
                    elif n_upper_2 > n_upper_1:
                        titre = titrefull[:len_titre]
        if upper_words_percentage(titre) > 0.2:
            print('Échec: titre "', titre, '" contient beaucoup de mots en majuscule', sep='')
        if nature != 'CODE':
            anomaly = [False]
            def anomaly_cb(titre, k, v1, v2):
                print('Incohérence: ', k, ': "', v1, '" ≠ "', v2, '"\n'
                      '       dans: "', titre, '"', sep='')
                anomaly[0] = True
            d1, endpos1 = parse_titre(titre, anomaly_cb)
            if not d1 and titre != 'Annexe' or d1 and endpos1 < len_titre:
                print('Fail: regex did not fully match titre "', titre, '"', sep='')
            d2, endpos2 = parse_titre(titrefull, anomaly_cb)
            if not d2:
                print('Fail: regex did not match titrefull "', titrefull, '"', sep='')
            if d1 or d2:
                def get_key(key, ignore_not_found=False):
                    g1, g2 = d1.get(key), d2.get(key)
                    if not (g1 or g2) and not ignore_not_found:
                        print('Échec: ', key, ' trouvé ni dans "', titre, '" (titre) ni dans "', titrefull, '" (titrefull)', sep='')
                        anomaly[0] = True
                        return
                    if g1 is None or g2 is None:
                        return g1 if g2 is None else g2
                    if strip_down(g1) == strip_down(g2):
                        return g1
                    if key == 'nature' and g1.split()[0] == g2.split()[0]:
                        return g1 if len(g1) > len(g2) else g2
                    if key == 'calendar':
                        return 'republican'
                    print('Incohérence: ', key, ': "', g1, '" ≠ "', g2, '"\n',
                          '      titre: "', titre, '"\n',
                          '  titrefull: "', titrefull, '"',
                          sep='')
                    anomaly[0] = True
                annexe = get_key('annexe', ignore_not_found=True)
                nature_d = strip_down(get_key('nature'))
                nature_d = NATURE_MAP_R_SD.get(nature_d, nature_d).upper()
                if nature_d and nature_d != nature:
                    if not nature:
                        nature = nature_d
                    elif nature_d.split('_')[0] == nature.split('_')[0]:
                        if len(nature_d) > len(nature):
                            nature = nature_d
                    else:
                        print('Incohérence: nature: "', nature_d, '" (detectée) ≠ "', nature, '" (donnée)', sep='')
                        anomaly[0] = True
                num_d = get_key('numero', ignore_not_found=True)
                if num_d and num_d != num and num_d != date_texte:
                    if not num or not num[0].isdigit():
                        if not annexe:  # On ne veut pas donner le numéro d'un décret à son annexe
                            if '-' in num_d or nature == 'DECISION':
                                orig_values['num'] = num
                                updates['num'] = num = num_d
                                count_update('num')
                    elif num[-1] == '.' and num[:-1] == num_d:
                        orig_values['num'] = num
                        updates['num'] = num = num_d
                        count_update('num')
                    else:
                        print('Incohérence: numéro: "', num_d, '" (detecté) ≠ "', num, '" (donné)', sep='')
                        anomaly[0] = True
                date_texte_d = get_key('date')
                calendar = get_key('calendar')
                if date_texte_d:
                    if not date_texte or date_texte == '2999-01-01':
                        orig_values['date_texte'] = date_texte
                        updates['date_texte'] = date_texte = date_texte_d
                        count_update('date_texte')
                    elif date_texte_d != date_texte:
                        print('Incohérence: date: "', date_texte_d, '" (detectée) ≠ "', date_texte, '" (donnée)', sep='')
                        anomaly[0] = True
                autorite_d = get_key('autorite', ignore_not_found=True)
                if autorite_d:
                    autorite_d = strip_down(autorite_d)
                    if not autorite_d.startswith('ministeriel'):
                        autorite_d = strip_prefix(autorite_d, 'du ').upper()
                        if not autorite:
                            orig_values['autorite'] = autorite
                            updates['autorite'] = autorite = autorite_d
                            count_update('autorite')
                        elif autorite != autorite_d:
                            print('Incohérence: autorité "', autorite_d, '" (detectée) ≠ "', autorite, '" (donnée)', sep='')
                            anomaly[0] = True
                if not anomaly[0]:
                    titre = gen_titre(annexe, nature, num, date_texte, calendar, autorite)
                    len_titre = len(titre)
                    titrefull_p2 = titrefull[endpos2:]
                    if titrefull_p2 and titrefull_p2[0] != ' ':
                        titrefull_p2 = ' ' + titrefull_p2
                    titrefull = titre + titrefull_p2
        titrefull_s = filter_nonalnum(titrefull)
        if titre != titre_o:
            count_update('titre')
            orig_values['titre'] = titre_o
            updates['titre'] = titre
        if titrefull != titrefull_o:
            count_update('titrefull')
            orig_values['titrefull'] = titrefull_o
            updates['titrefull'] = titrefull
        if nature != nature_o:
            count_update('nature')
            orig_values['nature'] = nature_o
            updates['nature'] = nature
        if titrefull_s != titrefull_s_o:
            updates['titrefull_s'] = titrefull_s
        if updates:
            if not dry_run:
                db.update("textes_versions", dict(id=text_id), updates)
            updates.clear()
            if orig_values:
                # Save the original non-normalized data in textes_versions_brutes
                bits = (TEXTES_VERSIONS_BRUTES_BITS[k] for k in orig_values)
                orig_values['bits'] = reduce(int.__or__, bits)
                orig_values.update(db.one("""
                    SELECT id, dossier, cid, mtime
                      FROM textes_versions
                     WHERE id = ?
                """, (text_id,), to_dict=True))
                if not dry_run:
                    db.insert("textes_versions_brutes", orig_values, replace=True)
                orig_values.clear()

    print('Done. Updated %i values: %s' %
          (sum(update_counts.values()), json.dumps(update_counts, indent=4)))


if __name__ == '__main__':
    p = ArgumentParser()
    p.add_argument('db')
    p.add_argument('what', default='all', choices=['all', 'articles_num', 'textes_titres'])
    p.add_argument('--dry-run', action='store_true', default=False)
    p.add_argument('--log-path', default='/dev/stdout')
    args = p.parse_args()

    db = connect_db(args.db)
    try:
        with db:
            if args.what in ('all', 'textes_titres'):
                normalize_text_titles(db, dry_run=args.dry_run)
            if args.what in ('all', 'articles_num'):
                normalize_article_numbers(db, dry_run=args.dry_run, log_path=args.log_path)
            if args.dry_run:
                raise KeyboardInterrupt
    except KeyboardInterrupt:
        pass
